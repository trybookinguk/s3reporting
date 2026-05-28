"""
/daily_metrics — per-day top-level metrics, matching generate_dashboard_data.py
build_daily_metrics() output shape exactly so the Svelte app can switch URLs
without code changes.

Output: list of {date, new_accounts, new_accounts_with_events,
new_accounts_with_sales, total_fees, total_revenue, total_tickets,
total_transactions, accounts_selling, events_with_sales}.

Notes
-----
- Dates are Europe/London calendar days. The warehouse stores TransactionDate /
  DateTimeCreated as UTC ISO strings; we convert via SQLite ``localtime`` which
  honours BST/GMT including DST transitions when the system timezone is
  Europe/London (which the Pi is).
- ``total_fees`` is fees ex-VAT (sum of the four fee columns divided by 1.20),
  matching the legacy builder.
- Only Status='Successful' transactions count towards booking metrics, also
  matching the legacy.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.app import db


router = APIRouter()


@router.get("/daily_metrics")
def daily_metrics() -> list[dict]:
    with db() as conn:
        # Booking aggregates per London date, Successful only.
        booking_rows = conn.execute("""
            SELECT
                date(TransactionDate, 'localtime') AS date,
                ROUND(SUM(COALESCE(BookingFee,0)+COALESCE(CardFee,0)
                         +COALESCE(ProcessingFee,0)+COALESCE(TicketFee,0))/1.20, 2) AS total_fees,
                ROUND(SUM(COALESCE(PaymentReceived,0)), 2) AS total_revenue,
                CAST(SUM(COALESCE(TicketQuantity,0)) AS INTEGER) AS total_tickets,
                COUNT(*) AS total_transactions,
                COUNT(DISTINCT AccountId) AS accounts_selling,
                COUNT(DISTINCT EventId) AS events_with_sales
            FROM bookings
            WHERE Status = 'Successful'
            GROUP BY date(TransactionDate, 'localtime')
        """).fetchall()
        booking_by_date = {
            row[0]: {
                "total_fees": row[1] or 0.0,
                "total_revenue": row[2] or 0.0,
                "total_tickets": int(row[3] or 0),
                "total_transactions": int(row[4] or 0),
                "accounts_selling": int(row[5] or 0),
                "events_with_sales": int(row[6] or 0),
            }
            for row in booking_rows
        }

        # Account-creation counts per London date.
        acct_rows = conn.execute("""
            SELECT
                date(DateTimeCreated, 'localtime') AS date,
                COUNT(*) AS new_accounts,
                SUM(CASE WHEN FirstEventCreation IS NOT NULL AND FirstEventCreation != ''
                         THEN 1 ELSE 0 END) AS new_accounts_with_events
            FROM accounts
            WHERE DateTimeCreated IS NOT NULL AND DateTimeCreated != ''
            GROUP BY date(DateTimeCreated, 'localtime')
        """).fetchall()
        acct_by_date = {
            row[0]: {
                "new_accounts": int(row[1] or 0),
                "new_accounts_with_events": int(row[2] or 0),
            }
            for row in acct_rows
        }

        # new_accounts_with_sales: how many of each day's new accounts ever made
        # a sale. The legacy builder uses `account_id in set(booking_account_ids)`.
        # SQL equivalent: COUNT accounts created on date D whose Id is in
        # (SELECT DISTINCT AccountId FROM bookings).
        sales_rows = conn.execute("""
            SELECT
                date(a.DateTimeCreated, 'localtime') AS date,
                COUNT(*) AS new_accounts_with_sales
            FROM accounts a
            WHERE a.DateTimeCreated IS NOT NULL AND a.DateTimeCreated != ''
              AND CAST(a.Id AS INTEGER) IN (
                  SELECT DISTINCT CAST(AccountId AS INTEGER)
                  FROM bookings
                  WHERE AccountId IS NOT NULL AND Status = 'Successful'
              )
            GROUP BY date(a.DateTimeCreated, 'localtime')
        """).fetchall()
        sales_by_date = {row[0]: int(row[1] or 0) for row in sales_rows}

        # Earliest date across both signals = start of the data range, matching
        # how the legacy builder derives data_start.
        bounds = conn.execute("""
            SELECT
                MIN(d) FROM (
                    SELECT MIN(date(TransactionDate, 'localtime')) AS d FROM bookings
                    UNION ALL
                    SELECT MIN(date(DateTimeCreated, 'localtime')) AS d FROM accounts
                )
        """).fetchone()
        start_date = bounds[0]
        end_date = conn.execute("SELECT date('now', 'localtime')").fetchone()[0]

    # Materialise a complete date range so the dashboard's date axis isn't
    # gappy. Generate dates in Python; the result is small (one entry per day
    # since the earliest data point — typically a few thousand).
    from datetime import date, timedelta
    if not start_date or not end_date:
        return []
    sd = date.fromisoformat(start_date)
    ed = date.fromisoformat(end_date)

    out = []
    one_day = timedelta(days=1)
    d = sd
    while d <= ed:
        iso = d.isoformat()
        b = booking_by_date.get(iso, {})
        a = acct_by_date.get(iso, {})
        out.append({
            "date": iso,
            "new_accounts": a.get("new_accounts", 0),
            "new_accounts_with_events": a.get("new_accounts_with_events", 0),
            "new_accounts_with_sales": sales_by_date.get(iso, 0),
            "total_fees": b.get("total_fees", 0.0),
            "total_revenue": b.get("total_revenue", 0.0),
            "total_tickets": b.get("total_tickets", 0),
            "total_transactions": b.get("total_transactions", 0),
            "accounts_selling": b.get("accounts_selling", 0),
            "events_with_sales": b.get("events_with_sales", 0),
        })
        d += one_day
    return out
