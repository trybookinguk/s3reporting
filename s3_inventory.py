#!/usr/bin/env python3
"""
S3 Inventory.

Dumps what is in the TryBooking S3 bucket and which data columns each report
carries, with a file reference for every column.

1. Files   : every object in the bucket (or under --prefix): key, report type,
             period, size, last modified.
2. Columns : for the newest file of each report type, the header and a small
             sample of rows are read with a ranged GET (default first 512 KB,
             never the whole file — BookingDataAll is ~2 GB). One row per
             column with:
               - file reference (s3://bucket/key) and column position
               - whether the column is documented in docs/s3_report_schemas.md,
                 and its description there
               - inferred type, % filled and distinct values in the sample
               - how the column can be used by the fraud risk report
             Documented columns that are missing from the live file are listed
             too ("Missing from file"), so schema drift is visible.

Outputs (in --output-dir, default REPORTS_DIR):
    <date>_S3Inventory.xlsx            Summary / Files / Columns sheets
    <date>_S3Inventory_files.csv
    <date>_S3Inventory_columns.csv

Sample VALUES are not written unless --show-samples is passed: the exports hold
customer data (emails, names, IPs) and the outputs are meant to be shareable.

Usage (source .env first for the AWS credentials):
    set -a && source /root/s3reporting/.env && set +a
    python3 s3_inventory.py                       # whole bucket, latest file per report
    python3 s3_inventory.py --prefix 2026/11/     # one month only
    python3 s3_inventory.py --all-files           # column dump for EVERY file, not just latest
    python3 s3_inventory.py --local-dir ./exports # inspect local CSVs instead of S3
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import OrderedDict
from datetime import date, datetime

import pandas as pd

DEFAULT_BUCKET = "produk-rdsextracts-438255373632"
DEFAULT_SAMPLE_BYTES = 512 * 1024
DEFAULT_SAMPLE_ROWS = 500
SCHEMA_DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "s3_report_schemas.md")

# File name: YYYYMM[DD]-<report>-TBUK.csv
_NAME_RE = re.compile(r"^(?P<period>\d{6}(?:\d{2})?)-(?P<report>.+?)-TBUK\.csv$", re.I)

# Preamble files: line 1 is gateway-batch metadata, real header on line 2.
_PREAMBLE_MARKERS = ("first gateway id", "start booking transaction id")

# How a column can feed the fraud risk report (modules/fraud_risk.py).
# Keyed on column name (case-insensitive); applies to whichever report has it.
FRAUD_USE = {
    "ipcountry": "Booking Outside UK Test; Booking IP Location Test (vs booking address)",
    "ipaddress": "IP-level checks (repeat IPs across accounts); needs geo-lookup for country",
    "status": "Booking Failed Transactions Test (Status = Failed)",
    "paymenttype": "Quicksale Test (if quicksale shows in PaymentType); box office vs online",
    "bookingcountrycode": "Booking address country for location tests (always GBR in UK)",
    "gatewayid": "Chargeback matching (PayPal txn / Stripe charge); joins Stripe signals export",
    "gatewayreference": "Chargeback matching fallback",
    "bookingurlid": "Chargeback matching to Invoice ID / Stripe Description (booking GUID)",
    "bookingid": "Chargeback matching fallback",
    "bookingtransactionid": "Booking key; joins signals file",
    "accountid": "Per-account grouping (Event Organiser Account Number)",
    "eventid": "Per-event grouping",
    "eventname": "Event label in report",
    "eventdate": "Event scope (recent/upcoming) and amount-at-risk window",
    "transactiondate": "Last-N bookings ordering; dispute ratio window",
    "paymentreceived": "Paid bookings, gross sales, amount at risk",
    "ticketquantity": "Tickets per event",
    "gatewaygroup": "Gateway (Stripe Connect / Default) segmentation",
    "gatewayname": "Payment gateway used (Stripe / PayPal)",
    "wallet": "Apple/Google Pay share (lower card-fraud risk)",
    "customerid": "Repeat-buyer / velocity checks",
    "lastlogin": "Last login only — not enough for the Account Logins Test",
    "lastlogindatetime": "Last login only — not enough for the Account Logins Test",
    "fullbalance": "Exposure: amount at risk",
    "balance": "Exposure: available balance",
    "salesforupcomingevents": "Exposure: amount at risk",
    "exposure": "Exposure: amount at risk",
    "futuredays": "Exposure horizon",
    "accountname": "Account label in report",
    "accountstatus": "Account context (closed / activated)",
    "datetimecreated": "Account age (new-account risk)",
    "isverified": "Account verification status",
    "bankaccountisdeleted": "Bank account removed (payout risk)",
    "bankaccountstatusid": "Bank account status (payout risk)",
    "cardtype": "Card brand only — no issuing country",
    "cardno": "Last 4 digits only",
    "gatewaymessage": "Gateway decline / failure reasons",
    "amount": "Transaction amount",
    "pending": "Funds in transfer (exposure)",
    "riskscore": "Average Risk Score Test",
    "cardcountry": "Booking Credit Card Location / Outside UK Tests",
    "cardissuecountry": "Booking Credit Card Location / Outside UK Tests",
    "browsertimezone": "Booking Browser Time Zone Test",
    "emaildeliveryfailed": "Booking Failed Email Delivery Test",
    "isquicksale": "Quicksale Test",
    "secondfactorenabled": "Account security (2FA) context",
    "smsmultifactorenabled": "Account security (MFA) context",
    "lastpasswordchangedate": "Account takeover context",
}


# ---------------------------------------------------------------------------
# Known schema (docs/s3_report_schemas.md)
# ---------------------------------------------------------------------------

def _report_key(name: str) -> str:
    """Normalise a report name for matching: 'scanapp-users' == 'ScanAppUsers'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load_known_schema(path: str = SCHEMA_DOC) -> dict:
    """{report_key: OrderedDict(column -> description)} from the schema doc.

    Reads each '### Title — `YYYYMM-name-TBUK.csv`' section's column table. A
    section that says 'Identical schema to X' / 'Same … schema as X' inherits X.
    """
    if not os.path.exists(path):
        return {}
    known, aliases = {}, {}
    current = None
    title_of = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^###\s+(.+?)\s+—\s+`([^`]+)`", line)
            if m:
                fm = re.search(r"-(.+?)-TBUK", m.group(2))
                current = _report_key(fm.group(1) if fm else m.group(1))
                title_of[_report_key(m.group(1))] = current
                known.setdefault(current, OrderedDict())
                continue
            if current is None:
                continue
            same = re.search(r"(?:Identical schema to|schema as)\s+([A-Za-z]+)", line)
            if same and not known[current]:
                aliases[current] = _report_key(same.group(1))
            m = re.match(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*$", line)
            if m:
                known[current][m.group(1)] = m.group(2)
    for k, target in aliases.items():
        target = title_of.get(target, target)
        if not known.get(k) and known.get(target):
            known[k] = OrderedDict(known[target])
    return {k: v for k, v in known.items() if v}


# ---------------------------------------------------------------------------
# File sources (S3 or local directory)
# ---------------------------------------------------------------------------

class S3Source:
    def __init__(self, bucket: str):
        import boto3
        try:
            from modules.utils.config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
        except Exception:
            AWS_ACCESS_KEY_ID = AWS_SECRET_ACCESS_KEY = None
        kwargs = {}
        if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
            kwargs = dict(aws_access_key_id=AWS_ACCESS_KEY_ID,
                          aws_secret_access_key=AWS_SECRET_ACCESS_KEY)
        self.client = boto3.client("s3", **kwargs)
        self.bucket = bucket

    def ref(self, key):
        return f"s3://{self.bucket}/{key}"

    def list(self, prefix: str = ""):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix or ""):
            for o in page.get("Contents", []):
                yield {"Key": o["Key"], "SizeBytes": int(o["Size"]),
                       "LastModified": o["LastModified"].strftime("%Y-%m-%d %H:%M:%S")}

    def head_bytes(self, key: str, n: int) -> tuple:
        """(bytes, truncated?) — first n bytes via a ranged GET."""
        resp = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{n - 1}")
        data = resp["Body"].read()
        total = resp.get("ContentRange", "").rsplit("/", 1)[-1]
        truncated = total.isdigit() and int(total) > len(data)
        return data, truncated


class LocalSource:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def ref(self, key):
        return os.path.join(self.root, key)

    def list(self, prefix: str = ""):
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                if prefix and not key.startswith(prefix):
                    continue
                st = os.stat(full)
                yield {"Key": key, "SizeBytes": st.st_size,
                       "LastModified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}

    def head_bytes(self, key: str, n: int) -> tuple:
        full = os.path.join(self.root, key)
        with open(full, "rb") as f:
            data = f.read(n)
        return data, os.path.getsize(full) > len(data)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def classify(key: str) -> dict:
    """Report type + period from an S3 key."""
    name = key.rsplit("/", 1)[-1]
    m = _NAME_RE.match(name)
    if not m:
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        return {"ReportType": "(other)", "Period": "", "PeriodType": "", "FileType": ext}
    period = m.group("period")
    return {"ReportType": m.group("report"), "Period": period,
            "PeriodType": "Daily snapshot" if len(period) == 8 else "Monthly",
            "FileType": "csv"}


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def parse_head(data: bytes, truncated: bool, max_rows: int) -> tuple:
    """(header, rows, preamble_line) from the first bytes of a CSV."""
    text = data.decode("utf-8-sig", errors="replace")
    if truncated and "\n" in text:
        text = text[: text.rfind("\n")]  # drop the partial last line
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if r and any(c.strip() for c in r)]
    if not rows:
        return [], [], ""
    preamble = ""
    first = ",".join(rows[0]).lower()
    if any(m in first for m in _PREAMBLE_MARKERS) and len(rows) > 1:
        preamble = ",".join(rows[0])
        rows = rows[1:]
    header = [h.strip().lstrip("﻿") for h in rows[0]]
    return header, rows[1:1 + max_rows], preamble


_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?\d*\.\d+$|^-?\d+\.\d*$")
_BOOL = {"true", "false", "0", "1", "yes", "no"}
_DT = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?|^\d{1,2}/\d{1,2}/\d{4}")


def infer_type(values) -> str:
    vals = [v.strip() for v in values if v is not None and v.strip() not in ("", "NULL", "null")]
    if not vals:
        return "empty"
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    if all(_INT.match(v) for v in vals):
        return "boolean (0/1)" if set(vals) <= {"0", "1"} else "integer"
    if all(_INT.match(v) or _DEC.match(v) for v in vals):
        return "decimal"
    if all(_DT.match(v) for v in vals):
        return "datetime" if any(":" in v for v in vals) else "date"
    if all(v.lower() in _BOOL for v in vals):
        return "boolean"
    return "text"


def column_rows(src, f: dict, header, rows, preamble, known: dict, show_samples: bool):
    """One output row per column of one file."""
    rep_key = _report_key(f["ReportType"])
    doc = known.get(rep_key, {})
    doc_lower = {k.lower(): (k, v) for k, v in doc.items()}
    out = []
    seen = set()
    for i, col in enumerate(header):
        vals = [r[i] if i < len(r) else "" for r in rows]
        filled = [v for v in vals if v.strip() not in ("", "NULL", "null")]
        d = doc_lower.get(col.lower())
        seen.add(col.lower())
        row = OrderedDict(
            ReportType=f["ReportType"], FileReference=src.ref(f["Key"]), Period=f["Period"],
            Position=i + 1, Column=col,
            Documented="Yes" if d else ("New (not in docs)" if doc else "No schema doc"),
            Description=(d[1] if d else ""),
            InferredType=infer_type(vals),
            FilledPct=round(100.0 * len(filled) / len(vals), 1) if vals else None,
            DistinctInSample=len(set(filled)),
            SampleRows=len(rows),
            FraudRiskUse=FRAUD_USE.get(col.lower(), ""),
            HasPreamble="Yes" if preamble else "",
        )
        if show_samples:
            row["SampleValue"] = filled[0] if filled else ""
        out.append(row)
    for col, desc in doc.items():
        if col.lower() not in seen:
            row = OrderedDict(
                ReportType=f["ReportType"], FileReference=src.ref(f["Key"]), Period=f["Period"],
                Position=None, Column=col, Documented="Missing from file", Description=desc,
                InferredType="", FilledPct=None, DistinctInSample=None, SampleRows=len(rows),
                FraudRiskUse=FRAUD_USE.get(col.lower(), ""), HasPreamble="Yes" if preamble else "",
            )
            if show_samples:
                row["SampleValue"] = ""
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build(src, prefix: str, all_files: bool, sample_bytes: int, sample_rows: int,
          show_samples: bool, known: dict, log=print) -> tuple:
    files = []
    for o in src.list(prefix):
        o.update(classify(o["Key"]))
        o["Size"] = _human(o["SizeBytes"])
        files.append(o)
    log(f"Listed {len(files):,} files under '{prefix or '/'}'.")
    files_df = pd.DataFrame(files, columns=["Key", "ReportType", "Period", "PeriodType", "FileType",
                                             "SizeBytes", "Size", "LastModified"])
    if files_df.empty:
        return files_df, pd.DataFrame()
    files_df = files_df.sort_values(["ReportType", "Period", "Key"], ascending=[True, False, True])

    csvs = files_df[files_df["FileType"] == "csv"].copy()
    if all_files:
        targets = csvs
    else:
        # Newest file per report type (case-insensitive name), by period then modified time.
        csvs["_rk"] = csvs["ReportType"].map(_report_key)
        targets = (csvs.sort_values(["Period", "LastModified"], ascending=False)
                   .drop_duplicates("_rk").sort_values("ReportType"))
    files_df["ColumnsDumped"] = files_df["Key"].isin(set(targets["Key"])).map({True: "Yes", False: ""})

    col_rows = []
    for f in targets.to_dict("records"):
        try:
            data, truncated = src.head_bytes(f["Key"], sample_bytes)
        except Exception as e:
            log(f"  ! {f['Key']}: could not read ({e})")
            continue
        header, rows, preamble = parse_head(data, truncated, sample_rows)
        col_rows.extend(column_rows(src, f, header, rows, preamble, known, show_samples))
        log(f"  {f['Key']}: {len(header)} columns, {len(rows)} sample rows"
            + (" (preamble skipped)" if preamble else ""))
    return files_df, pd.DataFrame(col_rows)


def summary_frame(files_df, cols_df, source_label, prefix):
    rows = [("Source", source_label), ("Prefix", prefix or "(whole bucket)"),
            ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("Files", len(files_df)),
            ("Total size", _human(int(files_df["SizeBytes"].sum())) if len(files_df) else "0 B"),
            ("", "")]
    if len(files_df):
        g = files_df.groupby("ReportType").agg(Files=("Key", "count"), Bytes=("SizeBytes", "sum"),
                                               Earliest=("Period", "min"), Latest=("Period", "max"))
        for rt, r in g.iterrows():
            ncols = int(((cols_df["ReportType"] == rt) & cols_df["Position"].notna()).sum()) \
                if len(cols_df) else 0
            rows.append((rt, f"{r.Files} files, {_human(int(r.Bytes))}, periods "
                             f"{r.Earliest or '-'} to {r.Latest or '-'}, {ncols} columns"))
    if len(cols_df):
        rows += [("", ""),
                 ("Columns not in schema doc", int((cols_df["Documented"] == "New (not in docs)").sum())),
                 ("Documented columns missing from file", int((cols_df["Documented"] == "Missing from file").sum())),
                 ("Columns with a fraud-risk use", int((cols_df["FraudRiskUse"] != "").sum()))]
    return pd.DataFrame(rows, columns=["Item", "Value"])


def write_xlsx(path, sheets):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1F3864")
                c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.freeze_panes = "A2"
            if ws.max_row > 1 and name != "Summary":
                ws.auto_filter.ref = ws.dimensions
            for i, h in enumerate(df.columns, start=1):
                sample = [str(v) for v in df.iloc[:300, i - 1].tolist()] if len(df) else []
                width = max([len(str(h))] + [len(s) for s in sample]) + 2
                ws.column_dimensions[get_column_letter(i)].width = min(max(width, 8), 70)


def main() -> int:
    p = argparse.ArgumentParser(description="Dump S3 bucket files and their data columns.")
    p.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"S3 bucket (default {DEFAULT_BUCKET}).")
    p.add_argument("--prefix", default="", help="Only keys under this prefix, e.g. 2026/11/.")
    p.add_argument("--local-dir", help="Inspect CSVs in this local folder instead of S3.")
    p.add_argument("--all-files", action="store_true",
                   help="Dump columns for every CSV, not just the newest per report type.")
    p.add_argument("--sample-bytes", type=int, default=DEFAULT_SAMPLE_BYTES,
                   help="Bytes read from the start of each file (default 512 KB).")
    p.add_argument("--sample-rows", type=int, default=DEFAULT_SAMPLE_ROWS,
                   help="Max rows used to infer types / fill rates (default 500).")
    p.add_argument("--show-samples", action="store_true",
                   help="Include one example value per column (contains customer data).")
    p.add_argument("--schema-doc", default=SCHEMA_DOC, help="Schema markdown to compare against.")
    p.add_argument("--output-dir", help="Output folder (default REPORTS_DIR).")
    args = p.parse_args()

    if args.local_dir:
        src, label = LocalSource(args.local_dir), f"local folder {os.path.abspath(args.local_dir)}"
    else:
        src, label = S3Source(args.bucket), f"s3://{args.bucket}"

    known = load_known_schema(args.schema_doc)
    print(f"Schema doc: {len(known)} report types documented.")
    try:
        files_df, cols_df = build(src, args.prefix, args.all_files, args.sample_bytes,
                                  args.sample_rows, args.show_samples, known)
    except Exception as e:
        print(f"Error listing {label}: {e}")
        return 1
    if files_df.empty:
        print("No files found.")
        return 1

    out_dir = args.output_dir
    if not out_dir:
        try:
            from modules.utils.config import REPORTS_DIR
            out_dir = REPORTS_DIR
        except Exception:
            out_dir = "reports"
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"{date.today().isoformat()}_S3Inventory")
    files_df.to_csv(f"{stem}_files.csv", index=False)
    cols_df.to_csv(f"{stem}_columns.csv", index=False)
    summary = summary_frame(files_df, cols_df, label, args.prefix)
    write_xlsx(f"{stem}.xlsx", [("Summary", summary), ("Files", files_df), ("Columns", cols_df)])
    print(f"Wrote {stem}.xlsx, {stem}_files.csv, {stem}_columns.csv")
    print(f"{len(files_df):,} files; {len(cols_df):,} column rows across "
          f"{cols_df['FileReference'].nunique() if len(cols_df) else 0} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
