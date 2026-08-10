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

It also adds Account_ID (matched from the account name; 'Err' if no match),
Exposure (Balance - Ticket Sales GBP Between Dates), and three age-bucketed
exposure columns keyed by days since the last completed event (age = now -
past bookend): "Exposure 90Days+" (age>=90), "Exposure 60Days+" (60..89),
"Exposure 30Days" (30..59). Rows are sorted high->low by 90Days+, then 60Days+,
then 30Days. Input files are named <YYYY-MM-DD>_AccountBalance.csv.

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

# Metric columns produced by compute_account_metrics(), in order.
METRIC_COLUMNS = [
    "Past Completed Event Date",
    "Total Completed Events",
    "Future Latest Event Date",
    "Events Between Dates",
    "Tickets Sold Between Dates",
    "Ticket Sales GBP Between Dates",
]

# Exposure = Balance - Ticket Sales GBP Between Dates, then bucketed by how many
# days have elapsed since the last completed event (age = now - past bookend):
#   >= 90 -> "Exposure 90Days+"; 60..89 -> "Exposure 60Days+"; 30..59 -> "Exposure 30Days".
EXPOSURE_COLUMNS = ["Exposure", "Exposure 90Days+", "Exposure 60Days+", "Exposure 30Days"]

# Full set appended to each row, in output order: Account_ID, the metrics, exposure.
APPENDED_COLUMNS = ["Account_ID"] + METRIC_COLUMNS + EXPOSURE_COLUMNS


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


def load_name_to_id(conn: sqlite3.Connection) -> dict:
    """Return {normalised_account_name: AccountId}, preferring the accounts
    snapshot, falling back to booking rows for names not in that snapshot."""
    m = {}
    for sql in (
        "SELECT AccountName, Id FROM accounts",
        "SELECT AccountName, MIN(AccountId) FROM bookings "
        "WHERE AccountName IS NOT NULL AND AccountId IS NOT NULL GROUP BY AccountName",
    ):
        try:
            for name, _id in conn.execute(sql):
                if name and _id is not None:
                    m.setdefault(_norm(name), _id)
        except sqlite3.OperationalError:
            continue
    return m


def _parse_money(value) -> float:
    """Parse a currency-ish cell ('85,181.98', '£1330.00', '0.00') to float, or None."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("£", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


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


def build_report_csv(rows: list, metrics: dict, name_to_id: dict, now: date) -> tuple:
    """Append the risk columns to each account row and sort by exposure age.

    `rows` is the parsed Account Balance CSV (list of lists). Adds Account_ID
    (matched by name; 'Err' if none), the event metrics, Exposure (Balance -
    Ticket Sales GBP Between Dates) and the age-bucketed exposure columns, then
    sorts rows high->low by Exposure 90Days+, 60Days+, 30Days.

    Returns (csv_text, matched, total). Nothing is written here.
    """
    header_idx = _find_header_index(rows)
    banner = rows[:header_idx]
    header = rows[header_idx]
    data_rows = rows[header_idx + 1:]

    def col_index(label):
        for i, h in enumerate(header):
            if h.strip().casefold() == label:
                return i
        return None

    acct_col = col_index("account")
    acct_col = 0 if acct_col is None else acct_col
    balance_col = col_index("balance")

    def _sort_num(v):
        return float(v) if v != "" else float("-inf")

    matched = total = 0
    built = []  # (sort_key_tuple, output_row)
    for row in data_rows:
        if not row or all(not c.strip() for c in row):
            continue  # drop blank separator rows (sorting reorders anyway)
        total += 1
        name = row[acct_col] if acct_col < len(row) else ""
        key = _norm(name)
        m = metrics.get(key)
        if m:
            matched += 1

        acct_id = name_to_id.get(key, "Err")
        metric_vals = [(m[c] if m else "") for c in METRIC_COLUMNS]

        balance = _parse_money(row[balance_col]) if (balance_col is not None and balance_col < len(row)) else None
        rev_between = float(m["Ticket Sales GBP Between Dates"]) if m else 0.0
        if balance is None:
            exposure = e90 = e60 = e30 = ""
        else:
            exposure = round(balance - rev_between, 2)
            past = _parse_date(m["Past Completed Event Date"]) if (m and m["Past Completed Event Date"]) else None
            age = (now - past).days if past else None
            e90 = exposure if (age is not None and age >= 90) else ""
            e60 = exposure if (age is not None and 60 <= age < 90) else ""
            e30 = exposure if (age is not None and 30 <= age < 60) else ""

        out_row = row + [acct_id] + metric_vals + [exposure, e90, e60, e30]
        built.append(((_sort_num(e90), _sort_num(e60), _sort_num(e30)), out_row))

    # Sort high -> low by 90Days+, then 60Days+, then 30Days.
    built.sort(key=lambda t: t[0], reverse=True)

    buf = io.StringIO()
    w = csv.writer(buf)
    for b in banner:
        w.writerow(b)
    w.writerow(header + APPENDED_COLUMNS)
    w.writerows(r for _, r in built)
    return buf.getvalue(), matched, total


def _date_str_from_name(name: str):
    """Return the YYYY-MM-DD prefix of a <date>_AccountBalance.csv name, or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_AccountBalance\.csv$", os.path.basename(name), re.I)
    return m.group(1) if m else None


def _is_balance_file(name: str) -> bool:
    """True if a filename looks like an Account Balance CSV (separator-agnostic)."""
    base = os.path.basename(name).lower()
    return base.endswith(".csv") and "accountbalance" in re.sub(r"[ _\-]", "", base)


def _latest_balance_filename(names: list):
    """Pick the newest Account Balance CSV (by YYYY-MM-DD date) from a name list."""
    cands = [n for n in names if _is_balance_file(n)]
    if not cands:
        return None
    dated = [(_date_str_from_name(n), n) for n in cands]
    with_dates = [(d, n) for d, n in dated if d]
    if with_dates:
        return max(with_dates)[1]  # ISO date strings sort chronologically
    return sorted(cands)[-1]


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
        date_str = _date_str_from_name(args.local_input) or now.isoformat()
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
                print(f"Error: no Account Balance CSV found in SharePoint '{args.folder}'.")
                print("  Files present in that folder:")
                for n in sorted(names):
                    print(f"    - {n}")
                return 1
        date_str = _date_str_from_name(balance_name) or now.isoformat()
        balance_bytes = sharepoint.download_file(token, drive_id, balance_name, args.folder)
        if balance_bytes is None:
            print(f"Error: '{balance_name}' not found in SharePoint '{args.folder}'.")
            return 1
        print(f"Downloaded {balance_name} from SharePoint '{args.folder}'.")

    report_filename = f"{date_str}_AccountRiskReport.csv"

    # --- Compute + build -----------------------------------------------------
    print(f"Reading events from {db_path} (now = {now.isoformat()}; past = EventDate < now, Successful)...")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        metrics = compute_account_metrics(conn, now)
        name_to_id = load_name_to_id(conn)
    finally:
        conn.close()
    print(f"Computed risk metrics for {len(metrics):,} accounts; {len(name_to_id):,} name->id mappings.")

    csv_text, matched, total = build_report_csv(
        _rows_from_bytes(balance_bytes), metrics, name_to_id, now)
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
        subject = f"Account Risk Report {date_str}"
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
