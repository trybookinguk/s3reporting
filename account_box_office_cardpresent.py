#!/usr/bin/env python3
"""
Box Office (Card Present) usage by account — rolling last 365 days.

Ranks every account by how much it has taken on Box Office *Card Present*
transactions over the rolling year, written to a CSV. Card Present only — Cash
is excluded.

Definitions (consistent with the rest of the codebase):
  - Box Office Card Present = PaymentType starts with "CardPresent" (covers
    CardPresent, CardPresentTTPi, CardPresentTTPa). Cash is NOT included.
  - Revenue   = sum of PaymentReceived (ticket value, excluding fees).
  - Fees      = sum of (BookingFee + CardFee + ProcessingFee + TicketFee),
    inc VAT — the total the organiser is charged.
  - Period    = last 365 days, Europe/London, relative to today.

Reads from the local SQLite warehouse (built by prepare_data.py) and pushes the
aggregation down into SQL — it never loads the full booking history into memory,
so it runs comfortably on the Pi.

Output: box_office_cardpresent_accounts.csv, ranked by fees (inc VAT) descending.

Usage:
    python3 account_box_office_cardpresent.py              # rank all accounts
    python3 account_box_office_cardpresent.py 19815        # just one account
    ACCOUNT_ID=19815 python3 account_box_office_cardpresent.py
"""

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import warehouse

UK_TZ = pytz.timezone("Europe/London")
OUTPUT_CSV = "box_office_cardpresent_accounts.csv"


def main() -> None:
    # Optional single-account filter (CLI arg or ACCOUNT_ID env var).
    account_filter = os.environ.get("ACCOUNT_ID")
    if len(sys.argv) > 1:
        account_filter = sys.argv[1]
    account_filter = int(account_filter) if account_filter else None

    today = datetime.now(UK_TZ).date()
    # Cutoff as a UTC instant — TransactionDate is stored as ISO-8601 UTC, so a
    # string comparison against a UTC ISO cutoff orders correctly.
    cutoff_iso = (pd.Timestamp.now("UTC") - pd.Timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_date = (datetime.now(UK_TZ) - timedelta(days=365)).date()

    # Push the whole thing into one grouped SQL query: card-present + successful
    # + within the window, aggregated per account. Only the small result comes
    # back — no full-frame load, so this is memory-safe on the Pi.
    where = (
        "PaymentType LIKE 'CardPresent%' "
        "AND Status = 'Successful' "
        "AND TransactionDate >= ?"
    )
    if account_filter is not None:
        where += " AND AccountId = ?"
        params = (cutoff_iso, account_filter)
    else:
        params = (cutoff_iso,)

    select_sql = (
        "AccountId, "
        "MAX(AccountName) AS account_name, "
        "ROUND(COALESCE(SUM(BookingFee),0)+COALESCE(SUM(CardFee),0)"
        "+COALESCE(SUM(ProcessingFee),0)+COALESCE(SUM(TicketFee),0), 2) AS fees_inc_vat, "
        "ROUND(COALESCE(SUM(PaymentReceived),0), 2) AS revenue, "
        "COALESCE(SUM(TicketQuantity),0) AS tickets, "
        "COUNT(*) AS transactions, "
        "MAX(TransactionDate) AS last_txn"
    )

    conn = warehouse.connect()
    try:
        out = warehouse.read_bookings_grouped(
            conn, select_sql, where=where, params=params, group_by="AccountId"
        )
    finally:
        conn.close()

    if out.empty:
        scope = f"account {account_filter}" if account_filter is not None else "any account"
        sys.exit(f"No Card Present transactions found for {scope} "
                 f"between {cutoff_date.isoformat()} and {today.isoformat()}.")

    out["account_id"] = pd.to_numeric(out["AccountId"], errors="coerce").astype("Int64")
    out["account_name"] = out["account_name"].fillna("")
    out["tickets"] = pd.to_numeric(out["tickets"], errors="coerce").fillna(0).astype(int)
    # last_txn comes back as a UTC ISO string; show the London date.
    out["last_txn"] = (
        pd.to_datetime(out["last_txn"], errors="coerce", utc=True)
        .dt.tz_convert(UK_TZ).dt.date.astype(str)
    )

    out = out.sort_values("fees_inc_vat", ascending=False)
    out = out[[
        "account_id", "account_name", "fees_inc_vat", "revenue",
        "tickets", "transactions", "last_txn",
    ]]

    out.to_csv(OUTPUT_CSV, index=False)

    # Console summary.
    print(f"\nBox Office (Card Present), {cutoff_date.isoformat()} to {today.isoformat()} (rolling 365 days)")
    print(f"  Accounts: {len(out):,}")
    print(f"  Total fees (inc VAT): £{out['fees_inc_vat'].sum():,.2f}")
    print(f"  Total revenue:        £{out['revenue'].sum():,.2f}")
    print(f"  Total tickets:        {int(out['tickets'].sum()):,}")
    print(f"  Ranked by fees (inc VAT), descending. Written to {OUTPUT_CSV}\n")

    # Show the top of the table inline (or the single account, if filtered).
    head = out.head(15)
    print(f"  {'Account':<40} {'Fees inc VAT':>13} {'Tickets':>9}")
    print(f"  {'-' * 40} {'-' * 13} {'-' * 9}")
    for _, row in head.iterrows():
        label = f"{int(row['account_id'])} {row['account_name']}"[:40]
        print(f"  {label:<40} £{row['fees_inc_vat']:>11,.2f} {row['tickets']:>9,}")
    if len(out) > len(head):
        print(f"  ... and {len(out) - len(head):,} more in {OUTPUT_CSV}")
    print()


if __name__ == "__main__":
    main()
