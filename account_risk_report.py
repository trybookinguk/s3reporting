#!/usr/bin/env python3
"""
Account Risk Report.

Reads an "Admin Account Balance" export (filename <date>_AccountBalance.csv),
joins each account to the warehouse's completed events, and appends risk
columns describing the account's two most-recent completed events and the
activity between them.

For each account (matched by account name) it computes, over COMPLETED events
(EventDate before the report/file date, from Successful bookings only):

  - Past Completed Event Date        : the most recent completed event date
  - Total Completed Events           : count of distinct completed events
  - Next Latest Completed Event Date : the second most recent completed date
  - Events Between Dates             : distinct events in the inclusive window
                                       [next-latest date .. latest date]
  - Tickets Sold Between Dates       : sum of TicketQuantity in that window
  - Ticket Sales GBP Between Dates   : sum of PaymentReceived in that window
                                       (the "carrying ticket sales balance")

The window is inclusive of both endpoint dates. If an account has only one
distinct completed event date, the window degenerates to that single date and
"Next Latest Completed Event Date" is left blank. Accounts with no completed
events get blank/zero values.

Output: <date>_AccountRiskReport.csv (date taken from the input filename).

Usage:
    python3 account_risk_report.py <path-to>_AccountBalance.csv
    python3 account_risk_report.py in.csv --db /path/warehouse.db --output out.csv
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

# New columns appended to each row, in order.
NEW_COLUMNS = [
    "Past Completed Event Date",
    "Total Completed Events",
    "Next Latest Completed Event Date",
    "Events Between Dates",
    "Tickets Sold Between Dates",
    "Ticket Sales GBP Between Dates",
]


def _norm(name: str) -> str:
    """Normalise an account name for matching (trim + case-fold)."""
    return (name or "").strip().casefold()


def _parse_date(value: str):
    """Parse an ISO-ish stored date string to a date, or None."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Stored as 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' (SQLite has no datetime).
    try:
        return datetime.fromisoformat(text.replace("T", " ")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _event_rollup_query(conn: sqlite3.Connection):
    """Return SQL + whether the join is by AccountName (True) or AccountId.

    Prefers bookings.AccountName; falls back to joining the accounts snapshot
    when the bookings table has no AccountName column.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
    if "AccountName" in cols:
        sql = (
            "SELECT AccountName AS acct, EventId, EventDate, "
            "       COALESCE(SUM(TicketQuantity), 0) AS tickets, "
            "       COALESCE(SUM(PaymentReceived), 0) AS revenue "
            "FROM bookings "
            "WHERE Status = 'Successful' AND EventDate IS NOT NULL "
            "  AND EventId IS NOT NULL AND AccountName IS NOT NULL "
            "  AND DATE(EventDate) < DATE(?) "
            "GROUP BY AccountName, EventId, EventDate"
        )
        return sql, True
    # Fallback: aggregate by AccountId, caller maps id -> name via accounts.
    sql = (
        "SELECT b.AccountId AS acct, b.EventId, b.EventDate, "
        "       COALESCE(SUM(b.TicketQuantity), 0) AS tickets, "
        "       COALESCE(SUM(b.PaymentReceived), 0) AS revenue "
        "FROM bookings b "
        "WHERE b.Status = 'Successful' AND b.EventDate IS NOT NULL "
        "  AND b.EventId IS NOT NULL AND b.AccountId IS NOT NULL "
        "  AND DATE(b.EventDate) < DATE(?) "
        "GROUP BY b.AccountId, b.EventId, b.EventDate"
    )
    return sql, False


def compute_account_metrics(conn: sqlite3.Connection, cutoff: date) -> dict:
    """Return {normalised_account_name: metrics_dict} for completed events."""
    sql, by_name = _event_rollup_query(conn)

    # events_by_acct[key] = list of (event_date, tickets, revenue), one per event
    events_by_acct = defaultdict(list)

    id_to_name = {}
    if not by_name:
        for _id, name in conn.execute("SELECT Id, AccountName FROM accounts"):
            if _id is not None:
                id_to_name[_id] = name

    for acct, _event_id, event_date_raw, tickets, revenue in conn.execute(sql, (cutoff.isoformat(),)):
        d = _parse_date(event_date_raw)
        if d is None:
            continue
        if by_name:
            key = _norm(acct)
        else:
            name = id_to_name.get(acct)
            if name is None:
                continue
            key = _norm(name)
        events_by_acct[key].append((d, float(tickets or 0), float(revenue or 0)))

    metrics = {}
    for key, events in events_by_acct.items():
        # Distinct completed event dates, newest first.
        distinct_dates = sorted({e[0] for e in events}, reverse=True)
        latest = distinct_dates[0]
        next_latest = distinct_dates[1] if len(distinct_dates) > 1 else None

        window_start = next_latest if next_latest is not None else latest
        in_window = [e for e in events if window_start <= e[0] <= latest]

        metrics[key] = {
            "Past Completed Event Date": latest.isoformat(),
            "Total Completed Events": len(events),
            "Next Latest Completed Event Date": next_latest.isoformat() if next_latest else "",
            "Events Between Dates": len(in_window),
            "Tickets Sold Between Dates": int(round(sum(e[1] for e in in_window))),
            "Ticket Sales GBP Between Dates": round(sum(e[2] for e in in_window), 2),
        }
    return metrics


def _find_header_index(rows: list) -> int:
    """Index of the header row (the one whose first cell is 'Account')."""
    for i, row in enumerate(rows):
        if row and row[0].strip().lstrip("﻿").casefold() == "account":
            return i
    raise ValueError("Could not find the 'Account,...' header row in the input CSV.")


def augment_csv(input_path: str, output_path: str, metrics: dict) -> tuple:
    """Append the risk columns to each account row. Returns (matched, total)."""
    with open(input_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header_idx = _find_header_index(rows)
    banner = rows[:header_idx]
    header = rows[header_idx]
    data_rows = rows[header_idx + 1:]

    # Locate the account-name column (defensive; it's normally column 0).
    try:
        acct_col = next(
            i for i, h in enumerate(header) if h.strip().casefold() == "account"
        )
    except StopIteration:
        acct_col = 0

    blank = {c: "" for c in NEW_COLUMNS}
    matched = total = 0
    out_rows = []
    for row in data_rows:
        if not row or all(not c.strip() for c in row):
            out_rows.append(row)  # preserve blank separator rows verbatim
            continue
        total += 1
        acct_name = row[acct_col] if acct_col < len(row) else ""
        m = metrics.get(_norm(acct_name))
        if m:
            matched += 1
        vals = m or blank
        out_rows.append(row + [vals[c] for c in NEW_COLUMNS])

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for b in banner:
            w.writerow(b)
        w.writerow(header + NEW_COLUMNS)
        w.writerows(out_rows)

    return matched, total


def _derive_output_path(input_path: str, output_arg) -> tuple:
    """Return (output_path, cutoff_date) derived from the input filename date."""
    base = os.path.basename(input_path)
    m = re.search(r"(\d{8})_AccountBalance\.csv$", base)
    if not m:
        raise ValueError(
            f"Input filename '{base}' does not match <YYYYMMDD>_AccountBalance.csv"
        )
    ymd = m.group(1)
    cutoff = datetime.strptime(ymd, "%Y%m%d").date()
    output_path = output_arg or os.path.join(os.getcwd(), f"{ymd}_AccountRiskReport.csv")
    return output_path, cutoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Account Risk Report from an Account Balance CSV.")
    parser.add_argument("input_csv", help="Path to <date>_AccountBalance.csv")
    parser.add_argument("--db", help="Path to the SQLite warehouse (default: warehouse.default_db_path()).")
    parser.add_argument("--output", help="Output CSV path (default: <date>_AccountRiskReport.csv in cwd).")
    args = parser.parse_args()

    if not os.path.exists(args.input_csv):
        print(f"Error: input file not found: {args.input_csv}")
        return 1

    try:
        output_path, cutoff = _derive_output_path(args.input_csv, args.output)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    db_path = args.db
    if not db_path:
        try:
            from modules import warehouse
            db_path = warehouse.default_db_path()
        except Exception as e:
            print(f"Error: could not resolve warehouse path ({e}); pass --db.")
            return 1
    if not os.path.exists(db_path):
        print(f"Error: warehouse database not found: {db_path}")
        return 1

    print(f"Reading events from {db_path} (completed = EventDate < {cutoff.isoformat()}, Successful)...")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        metrics = compute_account_metrics(conn, cutoff)
    finally:
        conn.close()
    print(f"Computed risk metrics for {len(metrics):,} accounts with completed events.")

    matched, total = augment_csv(args.input_csv, output_path, metrics)
    print(f"Matched {matched:,}/{total:,} account rows to warehouse events.")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
