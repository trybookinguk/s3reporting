#!/usr/bin/env python3
"""
Account Risk Report.

Reads an "Admin Account Balance" export (filename <date>_AccountBalance.csv),
joins each account to the warehouse's events, and appends risk columns
describing the span from the account's last completed event to its last
future (scheduled) event, and the ticketing activity in between.

For each account (matched by account name), over Successful bookings, using
two bookend dates relative to now():
  - past bookend   = the last event date in the PAST   (max EventDate < now)
  - future bookend = the last event date in the FUTURE  (max EventDate >= now)

it computes:

  - Past Completed Event Date      : the past bookend (last completed event)
  - Total Completed Events         : distinct events with EventDate < now
  - Future Latest Event Date       : the future bookend (last scheduled event)
  - Events Between Dates           : distinct events in the inclusive window
                                     between the two bookends (may be many)
  - Tickets Sold Between Dates     : sum of TicketQuantity in that window
  - Ticket Sales GBP Between Dates : sum of PaymentReceived in that window
                                     (the "carrying ticket sales balance")

The window is inclusive of both bookend dates. If a bookend is missing (no
past or no future events) the window collapses to the single date that exists
and the missing bookend column is left blank. Accounts with no events at all
get blank/zero values.

Input/Output: by default the Account Balance CSV is read from the SharePoint
"Platform Data/Users and Accounts" folder, the risk report is written back to
that same folder as <date>_AccountRiskReport.csv, and a LINK to it is emailed
to henry@trybooking.co.uk (which auto-CCs Kathryn). Flags allow reading a local
input file, skipping the email, or also saving a local copy.

Usage:
    python3 account_risk_report.py                      # newest balance CSV on SharePoint
    python3 account_risk_report.py --date 20260810      # a specific date's file
    python3 account_risk_report.py --folder "Platform Data/Some Folder"   # override
    python3 account_risk_report.py --local-input in.csv --no-email --output out.csv
"""
import argparse
import csv
import io
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

DEFAULT_EMAIL = "henry@trybooking.co.uk"

# New columns appended to each row, in order.
NEW_COLUMNS = [
    "Past Completed Event Date",
    "Total Completed Events",
    "Future Latest Event Date",
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
        "GROUP BY b.AccountId, b.EventId, b.EventDate"
    )
    return sql, False


def compute_account_metrics(conn: sqlite3.Connection, now: date) -> dict:
    """Return {normalised_account_name: metrics_dict}.

    Bookends per account (relative to `now`): the last PAST event date
    (EventDate < now) and the last FUTURE event date (EventDate >= now). The
    inclusive window between them may span many events.
    """
    sql, by_name = _event_rollup_query(conn)

    # events_by_acct[key] = list of (event_date, tickets, revenue), one per event
    events_by_acct = defaultdict(list)

    id_to_name = {}
    if not by_name:
        for _id, name in conn.execute("SELECT Id, AccountName FROM accounts"):
            if _id is not None:
                id_to_name[_id] = name

    for acct, _event_id, event_date_raw, tickets, revenue in conn.execute(sql):
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
        past_dates = [e[0] for e in events if e[0] < now]
        future_dates = [e[0] for e in events if e[0] >= now]
        past_bookend = max(past_dates) if past_dates else None
        future_bookend = max(future_dates) if future_dates else None

        # Inclusive window between the two bookends. If a bookend is missing,
        # the window collapses to whichever single bookend exists.
        bookends = [d for d in (past_bookend, future_bookend) if d is not None]
        lo, hi = min(bookends), max(bookends)
        in_window = [e for e in events if lo <= e[0] <= hi]

        # Count distinct completed events (EventDate < now). past_dates counts
        # event-date rows; use distinct-by-date is not right (multiple events
        # can share a date), so count rows — one row per (event, date).
        total_completed = len(past_dates)

        metrics[key] = {
            "Past Completed Event Date": past_bookend.isoformat() if past_bookend else "",
            "Total Completed Events": total_completed,
            "Future Latest Event Date": future_bookend.isoformat() if future_bookend else "",
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


def _rows_from_text(text: str) -> list:
    """Parse CSV text (Account Balance export, possibly BOM-prefixed) into rows."""
    return list(csv.reader(io.StringIO(text)))


def _rows_from_bytes(data: bytes) -> list:
    """Parse CSV bytes (utf-8, tolerating a BOM) into rows."""
    return _rows_from_text(data.decode("utf-8-sig"))


def build_report_csv(rows: list, metrics: dict) -> tuple:
    """Append the risk columns to each account row.

    `rows` is the parsed Account Balance CSV (list of lists). Returns
    (csv_text, matched, total). The text can be uploaded, written, or emailed;
    nothing is written here.
    """
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

    buf = io.StringIO()
    w = csv.writer(buf)
    for b in banner:
        w.writerow(b)
    w.writerow(header + NEW_COLUMNS)
    w.writerows(out_rows)
    return buf.getvalue(), matched, total


def _ymd_from_name(name: str):
    """Return the YYYYMMDD prefix of a <date>_AccountBalance.csv name, or None."""
    m = re.match(r"(\d{8})_AccountBalance\.csv$", os.path.basename(name))
    return m.group(1) if m else None


def _latest_balance_filename(names: list):
    """Pick the newest <date>_AccountBalance.csv from a list of filenames."""
    dated = [(_ymd_from_name(n), n) for n in names]
    dated = [(ymd, n) for ymd, n in dated if ymd]
    return max(dated)[1] if dated else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Account Risk Report from an Account Balance CSV on SharePoint.")
    parser.add_argument("--folder", default="Platform Data/Users and Accounts",
                        help="SharePoint folder (in the Platform Data drive) to read the "
                             "balance CSV from and write the report to. "
                             "Default: 'Platform Data/Users and Accounts'.")
    parser.add_argument("--date", help="Balance file date YYYYMMDD (default: newest on SharePoint).")
    parser.add_argument("--local-input", help="Read the balance CSV from this local path instead of SharePoint.")
    parser.add_argument("--db", help="Path to the SQLite warehouse (default: warehouse.default_db_path()).")
    parser.add_argument("--as-of", help="Treat this date as 'now' (YYYY-MM-DD). Default: today.")
    parser.add_argument("--email", default=DEFAULT_EMAIL,
                        help=f"Recipient for the report link (default: {DEFAULT_EMAIL}).")
    parser.add_argument("--no-email", action="store_true", help="Skip sending the email.")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip uploading to SharePoint (requires --output).")
    parser.add_argument("--output", help="Also write the report CSV to this local path.")
    args = parser.parse_args()

    if args.as_of:
        try:
            now = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: --as-of must be YYYY-MM-DD (got '{args.as_of}').")
            return 1
    else:
        now = date.today()

    # Resolve the warehouse.
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

    # SharePoint credentials/drive (needed unless purely local + no upload).
    need_sharepoint = (not args.local_input) or (not args.no_upload)
    token = drive_id = None
    if need_sharepoint:
        try:
            from modules.utils import sharepoint
            from modules.utils.config import SHAREPOINT_DRIVE_ID
        except Exception as e:
            print(f"Error: cannot import SharePoint helper ({e}).")
            return 1
        if not SHAREPOINT_DRIVE_ID:
            print("Error: SHAREPOINT_DRIVE_ID not set in the environment.")
            return 1
        drive_id = SHAREPOINT_DRIVE_ID
        token = sharepoint.authenticate_graph()
        if not token:
            print("Error: could not authenticate to Microsoft Graph.")
            return 1

    # --- Obtain the Account Balance CSV -------------------------------------
    if args.local_input:
        if not os.path.exists(args.local_input):
            print(f"Error: local input not found: {args.local_input}")
            return 1
        ymd = _ymd_from_name(args.local_input)
        if not ymd:
            print(f"Error: '{args.local_input}' is not a <YYYYMMDD>_AccountBalance.csv file.")
            return 1
        with open(args.local_input, "rb") as f:
            balance_bytes = f.read()
        print(f"Read balance CSV from local file {args.local_input}")
    else:
        if args.date:
            balance_name = f"{args.date}_AccountBalance.csv"
        else:
            names = sharepoint.list_files(token, drive_id, args.folder)
            balance_name = _latest_balance_filename(names)
            if not balance_name:
                print(f"Error: no <date>_AccountBalance.csv found in SharePoint '{args.folder}'.")
                return 1
        ymd = _ymd_from_name(balance_name)
        balance_bytes = sharepoint.download_file(token, drive_id, balance_name, args.folder)
        if balance_bytes is None:
            print(f"Error: '{balance_name}' not found in SharePoint '{args.folder}'.")
            return 1
        print(f"Downloaded {balance_name} from SharePoint '{args.folder}'.")

    report_filename = f"{ymd}_AccountRiskReport.csv"

    # --- Compute + build -----------------------------------------------------
    print(f"Reading events from {db_path} (now = {now.isoformat()}; past = EventDate < now, Successful)...")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        metrics = compute_account_metrics(conn, now)
    finally:
        conn.close()
    print(f"Computed risk metrics for {len(metrics):,} accounts with events.")

    csv_text, matched, total = build_report_csv(_rows_from_bytes(balance_bytes), metrics)
    print(f"Matched {matched:,}/{total:,} account rows to warehouse events.")

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
        print(f"Wrote {args.output}")

    # --- Upload to SharePoint + email the link -------------------------------
    web_url = None
    if not args.no_upload:
        ok = sharepoint.upload(token, drive_id, report_filename, csv_text.encode("utf-8"), args.folder)
        if not ok:
            print(f"Error: failed to upload {report_filename} to SharePoint.")
            return 1
        print(f"Uploaded {report_filename} to SharePoint '{args.folder}'.")
        web_url = sharepoint.get_web_url(token, drive_id, report_filename, args.folder)

    if not args.no_email:
        try:
            from modules.utils.email_utils import send_html_email
        except Exception as e:
            print(f"Error: cannot import email helper ({e}); use --no-email to skip.")
            return 1
        subject = f"Account Risk Report {ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        link_html = (f'<a href="{web_url}">{report_filename}</a>' if web_url
                     else f"{report_filename} (in SharePoint '{args.folder}')")
        body = (
            f"<p>FYI - the Account Risk Report is ready: {link_html}</p>"
            f"<p>As of {now.isoformat()}: {matched:,} of {total:,} accounts matched to "
            f"warehouse events. Each account row carries its last completed event date, "
            f"last future event date, and the events / tickets / ticket-sales (£) in the "
            f"window between them.</p>"
        )
        try:
            send_html_email(to=args.email, subject=subject, html_content=body)
        except Exception as e:
            print(f"Error sending email: {e}")
            return 1
        print(f"Emailed report link to {args.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
