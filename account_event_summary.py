#!/usr/bin/env python3
"""
Account Event Summary.

Reads the warehouse (the nightly-ingested S3 booking + account data) and emails
a CSV with one row per account:

  - Account ID        : accounts.Id
  - Latest Event ID   : EventId of the account's most recent PAST event
  - Last Event Date   : date of that most recent past event (EventDate < now)
  - Next Event Date   : date of the account's soonest UPCOMING event (>= now)
  - Last LogIn Date    : accounts.LastLogIn

Events come from all of the account's bookings (any status); dates are the
London-naive EventDate stored in the warehouse, emitted as YYYY-MM-DD.

Usage (source .env first so Azure email vars are present):
    set -a && source /root/s3reporting/.env && set +a
    python3 account_event_summary.py                      # email to henry@trybooking.co.uk
    python3 account_event_summary.py --email x@trybooking.co.uk
    python3 account_event_summary.py --no-email --output out.csv
"""
import argparse
import csv
import io
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

DEFAULT_EMAIL = "henry@trybooking.co.uk"
COLUMNS = ["Account ID", "Latest Event ID", "Last Event Date",
           "Next Event Date", "Last LogIn Date"]


def _parse_date(value):
    """Parse an ISO-ish stored timestamp to a date, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("T", " ")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _date_str(value):
    d = _parse_date(value)
    return d.isoformat() if d else ""


def build_summary(conn: sqlite3.Connection, now: date) -> list:
    """Return a list of rows (matching COLUMNS) — one per account."""
    # Account universe + last login.
    accounts = {}
    for _id, last_login in conn.execute("SELECT Id, LastLogIn FROM accounts"):
        if _id is not None:
            accounts[_id] = last_login

    # Distinct events per account (any booking status).
    events = defaultdict(list)  # AccountId -> list of (event_date, EventId)
    for aid, eid, edate in conn.execute(
        "SELECT AccountId, EventId, EventDate FROM bookings "
        "WHERE AccountId IS NOT NULL AND EventId IS NOT NULL AND EventDate IS NOT NULL "
        "GROUP BY AccountId, EventId, EventDate"
    ):
        d = _parse_date(edate)
        if d is not None:
            events[aid].append((d, eid))

    rows = []
    for aid in sorted(accounts):
        evs = events.get(aid, [])
        past = [(d, e) for d, e in evs if d < now]
        future = [d for d, e in evs if d >= now]
        # Most recent past event (break date ties by larger EventId).
        if past:
            last_date, last_eid = max(past, key=lambda t: (t[0], t[1]))
        else:
            last_date, last_eid = None, None
        next_date = min(future) if future else None

        rows.append([
            aid,
            last_eid if last_eid is not None else "",
            last_date.isoformat() if last_date else "",
            next_date.isoformat() if next_date else "",
            _date_str(accounts[aid]),
        ])
    return rows


def rows_to_csv(rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerows(rows)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Email an account event-summary CSV built from the warehouse.")
    parser.add_argument("--db", help="Path to the SQLite warehouse (default: warehouse.default_db_path()).")
    parser.add_argument("--as-of", help="Treat this date as 'now' (YYYY-MM-DD). Default: today.")
    parser.add_argument("--email", default=DEFAULT_EMAIL,
                        help=f"Recipient for the CSV (default: {DEFAULT_EMAIL}).")
    parser.add_argument("--no-email", action="store_true", help="Skip emailing; write to --output instead.")
    parser.add_argument("--output", help="Also write the CSV to this local path.")
    args = parser.parse_args()

    if args.as_of:
        try:
            now = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: --as-of must be YYYY-MM-DD (got '{args.as_of}').")
            return 1
    else:
        now = date.today()

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

    print(f"Reading warehouse {db_path} (now = {now.isoformat()})...")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = build_summary(conn, now)
    finally:
        conn.close()
    print(f"Built summary for {len(rows):,} accounts.")

    csv_text = rows_to_csv(rows)
    report_filename = f"account_event_summary_{now.isoformat()}.csv"

    if args.output or args.no_email:
        output_path = args.output or os.path.join(os.getcwd(), report_filename)
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
        print(f"Wrote {output_path}")

    if not args.no_email:
        try:
            from modules.utils.email_utils import send_html_email
        except Exception as e:
            print(f"Error: cannot import email helper ({e}); use --no-email to write a file.")
            return 1
        subject = f"Account Event Summary {now.isoformat()}"
        body = (
            f"<p>FYI - the account event summary is attached ({report_filename}).</p>"
            f"<p>{len(rows):,} accounts. Columns: Account ID, Latest Event ID, Last Event "
            f"Date, Next Event Date, Last LogIn Date (as of {now.isoformat()}).</p>"
        )
        attachment = (report_filename, csv_text.encode("utf-8"), "text", "csv")
        try:
            send_html_email(to=args.email, subject=subject, html_content=body,
                            attachments=[attachment])
        except Exception as e:
            print(f"Error sending email: {e}")
            return 1
        print(f"Emailed {report_filename} to {args.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
