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

All three are emailed to henry@trybooking.co.uk (auto-CCs Kathryn) with a
summary and any schema drift in the body; --email changes the recipient and
--no-email skips it. TEST_MODE=true prefixes the subject with [TEST].

Sample VALUES are not written to the Columns sheet/CSV unless --show-samples is
passed: the exports hold customer data (emails, names, IPs) and the columns CSV
is meant to be shareable.

3. Latest   : the workbook also gets a "BookingDataAll latest 20" sheet: the
             20 most recent transactions (by TransactionDate, newest first) in
             the newest non-empty BookingDataAll. The file isn't guaranteed to
             be in date order, so it is streamed end to end (~2 GB, a few
             minutes on the Pi) keeping only the top N. --latest-quick reads
             just the end of the file instead (fast, but only "last in file").
             These are RAW rows (customer IDs, IP addresses) — xlsx only, never
             the CSVs. --latest-rows 0 turns it off; --latest-report picks other
             reports.

Usage (source .env first for the AWS + Azure email credentials):
    set -a && source /root/s3reporting/.env && set +a
    python3 s3_inventory.py                       # whole bucket, latest file per report
    python3 s3_inventory.py --prefix 2026/11/     # one month only
    python3 s3_inventory.py --all-files           # column dump for EVERY file, not just latest
    python3 s3_inventory.py --local-dir ./exports # inspect local CSVs instead of S3
    python3 s3_inventory.py --no-email            # write files only
    python3 s3_inventory.py --latest-rows 50      # 50 most recent BookingDataAll transactions
    python3 s3_inventory.py --latest-quick        # last rows in the file, no full read
    python3 s3_inventory.py --latest-rows 0       # no raw-row sheet
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
DEFAULT_EMAIL = "henry@trybooking.co.uk"
XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
DEFAULT_SAMPLE_BYTES = 512 * 1024
DEFAULT_SAMPLE_ROWS = 500
DEFAULT_TAIL_REPORTS = ["BookingDataAll"]
DEFAULT_TAIL_ROWS = 20
LATEST_DATE_COLUMNS = ("TransactionDate", "DateCreated", "DateTimeCreated")
LATEST_CHUNK_ROWS = 200000
TAIL_START_BYTES = 256 * 1024
TAIL_MAX_BYTES = 8 * 1024 * 1024
SCHEMA_DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "s3_report_schemas.md")

# File name: YYYYMM[DD]-<report>-TBUK.csv
# (older/ad-hoc exports such as 202610-AccountFeatureList.csv omit "-TBUK")
_NAME_RE = re.compile(r"^(?P<period>\d{6}(?:\d{2})?)-(?P<report>.+?)(?:-TBUK)?\.csv$", re.I)

# Preamble files: line 1 is gateway-batch metadata, real header on line 2.
_PREAMBLE_MARKERS = ("first gateway id", "start booking transaction id")

# How a column can feed the fraud risk report (trybookinguk/RiskManagement fraud/risk.py).
# Keyed on column name (case-insensitive); applies to whichever report has it.
FRAUD_USE = {
    "ipcountry": "Booking Outside UK Test; Booking IP Location Test (vs booking address)",
    "ipaddress": "IP-level checks (repeat IPs across accounts); needs geo-lookup for country",
    "status": "Booking Failed Transactions Test (Status = Failed)",
    "paymenttype": "Quicksale Test (if quicksale shows in PaymentType); box office vs online",
    "bookingcountrycode": "Booking address country for IP/card location tests (mostly GBR, not always)",
    "gatewayid": "Chargeback matching (PayPal txn / Stripe charge); joins Stripe signals export",
    "gatewayreference": "Chargeback matching fallback",
    "bookingurlid": "Chargeback matching: booking GUID = PayPal Invoice ID / Stripe Description",
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
    "second factor enabled": "Account security (2FA) context",
    "lastloginmethod": "Login method only — no IP; not enough for the Account Logins Test",
    "lastlogindate": "Last login only — not enough for the Account Logins Test",
    "bankaccountverified": "Payout risk: unverified bank account",
    "bankaccount": "Payout risk: bank account set up",
    "datecreated": "Account age (new-account risk)",
    "provider": "Payment provider (Stripe / Braintree PayPal)",
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

    def open_stream(self, key: str):
        """Binary stream of the whole object (read in chunks by the caller)."""
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def tail_bytes(self, key: str, n: int) -> tuple:
        """(bytes, whole_file?) — last n bytes via a suffix ranged GET."""
        resp = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=-{n}")
        data = resp["Body"].read()
        total = resp.get("ContentRange", "").rsplit("/", 1)[-1]
        return data, not (total.isdigit() and int(total) > len(data))


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

    def open_stream(self, key: str):
        return open(os.path.join(self.root, key), "rb")

    def tail_bytes(self, key: str, n: int) -> tuple:
        full = os.path.join(self.root, key)
        size = os.path.getsize(full)
        with open(full, "rb") as f:
            f.seek(max(0, size - n))
            data = f.read()
        return data, size <= n


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


def _decode(data: bytes) -> str:
    """Decode export bytes: UTF-16 (BOM or NUL-heavy) or UTF-8 (BOM tolerant)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:200].count(b"\x00") > 20:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _cells(row):
    """Row cells with BOMs / zero-width chars / whitespace stripped."""
    return [c.replace("\ufeff", "").replace("\u200b", "").strip() for c in row]


def parse_head(data: bytes, truncated: bool, max_rows: int) -> tuple:
    """(header, rows, preamble) from the first bytes of a CSV.

    Copes with what the inventory has seen in the bucket: a gateway-batch
    metadata line before the header (AccountMovement*, GatewayMovement,
    TransactionReconciliation), stray leading lines that hold only a BOM or a
    single cell, UTF-16 files and non-comma delimiters. The header is taken as
    the first non-preamble row with the most fields among the first few rows.
    """
    text = _decode(data)
    if truncated and "\n" in text:
        text = text[: text.rfind("\n")]  # drop the partial last line
    sample = text[:20000]
    candidates = [","]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        if sniffed != ",":
            candidates.append(sniffed)
    except csv.Error:
        pass

    def _width(d):
        # Typical field count of the first lines under delimiter d.
        widths = sorted(len(r) for r in csv.reader(io.StringIO(sample), delimiter=d) if r)[:40]
        return widths[len(widths) // 2] if widths else 0

    delim = max(candidates, key=_width)  # ties keep the comma
    rows = [_cells(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], [], ""

    skipped = []
    head_window = rows[:6]
    def _is_preamble(r):
        joined = ",".join(r).lower()
        return any(m in joined for m in _PREAMBLE_MARKERS)

    widest = max([sum(1 for c in r if c) for r in head_window if not _is_preamble(r)] or [0])
    idx = 0
    while idx < len(head_window):
        r = head_window[idx]
        is_preamble = _is_preamble(r)
        too_narrow = widest >= 2 and sum(1 for c in r if c) < max(2, widest // 2)
        if (is_preamble or too_narrow) and idx + 1 < len(rows):
            skipped.append(",".join(c for c in r if c))
            idx += 1
            continue
        break
    header = rows[idx]
    # Trim trailing empty header cells (a trailing delimiter on the header line).
    while header and not header[-1]:
        header = header[:-1]
    preamble = " | ".join(skipped)
    if delim != ",":
        preamble = (preamble + " | " if preamble else "") + f"delimiter={delim!r}"
    return header, rows[idx + 1: idx + 1 + max_rows], preamble


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


# Category values are listed for columns with this many distinct values or
# fewer — enough to see formats (Status, PaymentType, IPCountry, …) — but never
# for columns that could hold personal or free-text data.
MAX_LISTED_VALUES = 15
_PII_COLUMN = re.compile(
    r"email|name|user|ip ?address|postcode|address|bank|accnumber|bsb|description|"
    r"filename|phone|mobile|abn|entity|comment|note|password|token|card ?no", re.I)


def category_values(col: str, filled: list) -> str:
    """'Successful (480), Failed (20)' for low-cardinality, non-personal columns."""
    if not filled or _PII_COLUMN.search(col):
        return ""
    counts = {}
    for v in filled:
        counts[v] = counts.get(v, 0) + 1
    if len(counts) > MAX_LISTED_VALUES:
        return ""
    return ", ".join(f"{v} ({n})" for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


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
            ValuesInSample=category_values(col, filled),
            SkippedBeforeHeader=preamble[:200],
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
                FraudRiskUse=FRAUD_USE.get(col.lower(), ""), ValuesInSample="",
                SkippedBeforeHeader=preamble[:200],
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
            + (f" (skipped before header: {preamble[:80]})" if preamble else ""))
    return files_df, pd.DataFrame(col_rows)


def parse_tail(data: bytes, whole_file: bool, header: list, n: int) -> list:
    """Last n complete data rows from the end bytes of a CSV.

    The first (partial) line is dropped unless the read covers the whole file,
    and only rows with the header's field count are kept, so a read that starts
    inside a quoted multi-line field can't shift the columns.
    """
    text = data.decode("utf-8-sig", errors="replace")
    if not whole_file:
        cut = text.find("\n")
        text = text[cut + 1:] if cut >= 0 else ""
    width = len(header)
    rows = [_cells(r) for r in csv.reader(io.StringIO(text))]
    rows = [r[:width] if len(r) > width and not any(r[width:]) else r for r in rows]
    rows = [r for r in rows if any(r) and len(r) == width and r != header]
    return rows[-n:] if n else []


def _newest_nonempty(files_df, report: str):
    cands = files_df[(files_df["ReportType"].map(_report_key) == _report_key(report))
                     & (files_df["FileType"] == "csv") & (files_df["SizeBytes"] > 0)]
    if cands.empty:
        return None
    return cands.sort_values(["Period", "LastModified"], ascending=False).iloc[0]


def _header_of(src, key: str) -> list:
    head, truncated = src.head_bytes(key, DEFAULT_SAMPLE_BYTES)
    return parse_head(head, truncated, 1)[0]


def tail_frame(src, files_df, report: str, n: int, log=print) -> tuple:
    """(last n rows in the file, file reference) for the newest non-empty
    file of ``report``; (None, None) when there is no such file."""
    f = _newest_nonempty(files_df, report)
    if f is None:
        log(f"  ! no non-empty {report} file for the latest-rows sheet")
        return None, None
    head, truncated = src.head_bytes(f["Key"], DEFAULT_SAMPLE_BYTES)
    header, _, _ = parse_head(head, truncated, 1)
    size, rows = TAIL_START_BYTES, []
    while header:
        data, whole = src.tail_bytes(f["Key"], size)
        rows = parse_tail(data, whole, header, n)
        if len(rows) >= n or whole or size >= TAIL_MAX_BYTES:
            break
        size *= 4
    log(f"  {f['Key']}: last {len(rows)} rows in the file")
    return pd.DataFrame(rows, columns=header), src.ref(f["Key"])


def top_latest(chunks, n: int, date_col: str) -> pd.DataFrame:
    """The n rows with the newest ``date_col`` across chunks, newest first.
    Rows whose date doesn't parse are ignored. Ties keep file order."""
    best = None
    for chunk in chunks:
        chunk.columns = [str(c).replace("\ufeff", "").strip() for c in chunk.columns]
        chunk = chunk.assign(_ts=pd.to_datetime(chunk[date_col], errors="coerce", utc=True))
        chunk = chunk[chunk["_ts"].notna()]
        if chunk.empty:
            continue
        top = chunk.nlargest(n, "_ts", keep="last")
        best = top if best is None else pd.concat([best, top]).nlargest(n, "_ts", keep="last")
    if best is None:
        return pd.DataFrame()
    return best.sort_values("_ts", ascending=False, kind="stable").drop(columns="_ts") \
               .reset_index(drop=True)


def latest_frame(src, files_df, report: str, n: int, log=print) -> tuple:
    """(n most recent transactions, file reference, date column) for the
    newest non-empty file of ``report``, streaming the whole file. Falls back
    to the end of the file when it has no date column."""
    f = _newest_nonempty(files_df, report)
    if f is None:
        log(f"  ! no non-empty {report} file for the latest-rows sheet")
        return None, None, None
    header = _header_of(src, f["Key"])
    date_col = next((c for c in LATEST_DATE_COLUMNS if c in header), None)
    if not date_col:
        log(f"  {f['Key']}: no date column — using the last rows in the file")
        df, ref = tail_frame(src, files_df, report, n, log)
        return df, ref, None
    log(f"  {f['Key']}: streaming {f.get('Size', '')} for the {n} latest by {date_col} …")
    with src.open_stream(f["Key"]) as body:
        chunks = pd.read_csv(body, dtype=str, keep_default_na=False, encoding="utf-8-sig",
                             chunksize=LATEST_CHUNK_ROWS, on_bad_lines="skip")
        df = top_latest(chunks, n, date_col)
    log(f"  {f['Key']}: {len(df)} latest rows"
        + (f", {df[date_col].iloc[0]} to {df[date_col].iloc[-1]}" if len(df) else ""))
    return df, src.ref(f["Key"]), date_col


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


def _html_escape(v) -> str:
    import html
    return html.escape("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))


def email_body(files_df, cols_df, label, prefix, filenames) -> str:
    """HTML body: headline numbers, per-report table, and schema drift."""
    cell = 'style="border:1px solid #ccc;padding:4px 8px;text-align:left"'
    head = 'style="border:1px solid #ccc;padding:4px 8px;background:#1F3864;color:#fff;text-align:left"'

    def table(df, cols):
        rows = "".join("<tr>" + "".join(f"<td {cell}>{_html_escape(r[c])}</td>" for c in cols) + "</tr>"
                       for r in df.to_dict("records"))
        return ('<table style="border-collapse:collapse;font-family:Arial;font-size:12px">'
                + "<tr>" + "".join(f"<th {head}>{_html_escape(c)}</th>" for c in cols) + "</tr>"
                + rows + "</table>")

    per_report = (files_df.groupby("ReportType")
                  .agg(Files=("Key", "count"), Bytes=("SizeBytes", "sum"),
                       Earliest=("Period", "min"), Latest=("Period", "max"))
                  .reset_index())
    per_report["Size"] = per_report["Bytes"].map(lambda b: _human(int(b)))
    if len(cols_df):
        ncols = (cols_df[cols_df["Position"].notna()].groupby("ReportType")["Column"]
                 .count().rename("Columns"))
        per_report = per_report.merge(ncols, on="ReportType", how="left")
    else:
        per_report["Columns"] = 0
    per_report["Columns"] = per_report["Columns"].fillna(0).astype(int)
    per_report = per_report.rename(columns={"ReportType": "Report"})

    parts = [
        f"<p>The S3 inventory for <b>{_html_escape(label)}</b> "
        f"({_html_escape(prefix or 'whole bucket')}) is attached: "
        + ", ".join(_html_escape(f) for f in filenames) + ".</p>",
        f"<p><b>{len(files_df):,}</b> files, "
        f"{_human(int(files_df['SizeBytes'].sum()))} in total; "
        f"<b>{int(cols_df['Position'].notna().sum()) if len(cols_df) else 0:,}</b> columns "
        f"listed from {cols_df['FileReference'].nunique() if len(cols_df) else 0} files "
        f"(newest file per report type unless --all-files).</p>",
        "<h3>Reports</h3>",
        table(per_report, ["Report", "Files", "Size", "Earliest", "Latest", "Columns"]),
    ]
    if len(cols_df):
        drift = cols_df[cols_df["Documented"].isin(["New (not in docs)", "Missing from file"])]
        undocumented = sorted(cols_df.loc[cols_df["Documented"] == "No schema doc", "ReportType"].unique())
        parts.append("<h3>Schema drift vs docs/s3_report_schemas.md</h3>")
        if len(drift):
            parts.append(table(drift.rename(columns={"ReportType": "Report", "Documented": "Change"}),
                               ["Report", "Column", "Change", "FileReference"]))
        else:
            parts.append("<p>None — every documented column is present and nothing new appeared.</p>")
        if undocumented:
            parts.append("<p>Report types with no schema doc entry: "
                         + ", ".join(_html_escape(u) for u in undocumented) + ".</p>")
    parts.append("<p>The Columns sheet has each column's file reference, position, description, "
                 "inferred type, % filled and fraud-risk use.</p>")
    return "".join(parts)


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
    p.add_argument("--latest-rows", "--tail-rows", dest="latest_rows", type=int,
                   default=DEFAULT_TAIL_ROWS,
                   help=f"Most recent N transactions sheet per --latest-report "
                        f"(default {DEFAULT_TAIL_ROWS}; 0 = off).")
    p.add_argument("--latest-report", "--tail-report", dest="latest_report", action="append",
                   help="Report type(s) for the latest sheet (default BookingDataAll; repeatable).")
    p.add_argument("--latest-quick", action="store_true",
                   help="Take the last rows in the file (ranged GET) instead of streaming "
                        "the whole file for the most recent by date.")
    p.add_argument("--schema-doc", default=SCHEMA_DOC, help="Schema markdown to compare against.")
    p.add_argument("--output-dir", help="Output folder (default REPORTS_DIR).")
    p.add_argument("--email", default=DEFAULT_EMAIL,
                   help=f"Recipient(s) for the inventory files (default {DEFAULT_EMAIL}).")
    p.add_argument("--no-email", action="store_true", help="Write the files but don't email them.")
    args = p.parse_args()
    import importlib.util
    if importlib.util.find_spec("openpyxl") is None:
        print("Error: openpyxl is not installed — run: pip install --break-system-packages openpyxl")
        return 1

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
    tail_sheets = []
    if args.latest_rows > 0:
        for report in args.latest_report or DEFAULT_TAIL_REPORTS:
            try:
                if args.latest_quick:
                    df, ref = tail_frame(src, files_df, report, args.latest_rows)
                    how = "last rows in the file"
                else:
                    df, ref, date_col = latest_frame(src, files_df, report, args.latest_rows)
                    how = (f"most recent by {date_col}, newest first" if date_col
                           else "last rows in the file (no date column)")
            except Exception as e:
                print(f"  ! {report}: could not read the latest rows ({e})")
                continue
            if df is not None:
                name = f"{report} latest {args.latest_rows}"[:31]
                tail_sheets.append((name, df))
                summary.loc[len(summary)] = [f"{name} (sheet)", f"{len(df)} rows, {how}, from {ref}"]
    write_xlsx(f"{stem}.xlsx", [("Summary", summary), ("Files", files_df), ("Columns", cols_df)]
               + tail_sheets)
    print(f"Wrote {stem}.xlsx, {stem}_files.csv, {stem}_columns.csv")
    print(f"{len(files_df):,} files; {len(cols_df):,} column rows across "
          f"{cols_df['FileReference'].nunique() if len(cols_df) else 0} files.")

    if args.no_email:
        return 0
    attachments = []
    for path, mime in ((f"{stem}.xlsx", XLSX_MIME), (f"{stem}_files.csv", ("text", "csv")),
                       (f"{stem}_columns.csv", ("text", "csv"))):
        with open(path, "rb") as f:
            attachments.append((os.path.basename(path), f.read()) + mime)
    # Graph sendMail caps the request at ~4 MB and attachments go base64 (+33%);
    # the large-attachment path needs Mail.ReadWrite, which the app doesn't have.
    # Attach in priority order (columns CSV, workbook, files CSV) within 2.5 MB
    # raw (~3.3 MB encoded); the rest stay on disk and are named in the email.
    budget = int(2.5 * 1024 * 1024)
    order = {f"{os.path.basename(stem)}_columns.csv": 0, f"{os.path.basename(stem)}.xlsx": 1}
    kept, left_out, used = [], [], 0
    for a in sorted(attachments, key=lambda a: order.get(a[0], 2)):
        if used + len(a[1]) <= budget:
            kept.append(a); used += len(a[1])
        else:
            left_out.append(a[0])
    attachments = kept
    if left_out:
        print(f"Too large to email (kept in {out_dir}): {', '.join(left_out)}")
    subject = f"S3 Inventory {date.today().isoformat()}" + (f" ({args.prefix})" if args.prefix else "")
    if args.show_samples or tail_sheets:
        subject += " - contains booking data"
    try:
        from modules.utils.email_utils import send_html_email
        send_html_email(to=args.email, subject=subject,
                        html_content=email_body(files_df, cols_df, label, args.prefix,
                                                [a[0] for a in attachments])
                        + (f"<p>Not attached (over the email size limit), saved on the Pi in "
                           f"{_html_escape(os.path.abspath(out_dir))}: "
                           f"{_html_escape(', '.join(left_out))}.</p>" if left_out else ""),
                        attachments=attachments or None)
    except Exception as e:
        print(f"Error sending email: {e}")
        return 1
    print(f"Emailed {len(attachments)} inventory files to {args.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
