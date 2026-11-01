#!/usr/bin/env python3
"""
Warehouse column backfill.

Refills booking columns that earlier ingests stored incorrectly, without
re-seeding the whole warehouse. Built for BookingUrlId: the S3 exports carry
the booking GUID there (text), but the warehouse used to coerce it to an
integer, so every existing row holds NULL. The fraud report needs it to match
PayPal "Invoice ID" / Stripe dispute "Description" to bookings.

How it works
------------
- Finds the newest non-empty BookingDataAll snapshot, plus every monthly
  BookingData file from that snapshot's month onward (newer files win).
- Streams each file from S3 reading ONLY BookingTransactionId and the
  requested columns, in chunks — memory stays at one chunk even for the
  ~2 GB BookingDataAll.
- UPDATEs the existing rows by BookingTransactionId (never inserts or
  deletes), committing per chunk.
- Adds no indexes (the warehouse is copied nightly to DuckDB and backed up;
  see warehouse._RETIRED_INDEXES) and drops the one an earlier version built.

Run it once after deploying the BookingUrlId fix, outside the 02:00-03:30
nightly jobs (it writes to the same warehouse):

    set -a && source /root/s3reporting/.env && set +a
    python3 warehouse_backfill.py                       # BookingUrlId from S3
    python3 warehouse_backfill.py --dry-run             # list the files it would read
    python3 warehouse_backfill.py --local-file BookingDataAll.csv
    python3 warehouse_backfill.py --columns BookingUrlId,GatewayId
"""
import argparse
import logging
import os
import re
import sqlite3
import sys
import time

import pandas as pd

from modules import warehouse

log = logging.getLogger("warehouse_backfill")

DEFAULT_BUCKET = "produk-rdsextracts-438255373632"
_ALL_RE = re.compile(r"(?:^|/)(\d{8})-BookingDataAll-TBUK\.csv$", re.I)
_MONTH_RE = re.compile(r"(?:^|/)(\d{6})-BookingData-TBUK\.csv$", re.I)
KEY = warehouse.BOOKINGS_KEY


def find_source_keys(objects) -> list:
    """Pick [BookingDataAll snapshot, monthly BookingData…] in apply order.

    ``objects`` is an iterable of {"Key", "Size"}. Empty snapshots are skipped
    (TryBooking sometimes writes a 0-byte BookingDataAll).
    """
    snapshots, months = [], []
    for o in objects:
        m = _ALL_RE.search(o["Key"])
        if m and int(o.get("Size", 1)) > 0:
            snapshots.append((m.group(1), o["Key"]))
            continue
        m = _MONTH_RE.search(o["Key"])
        if m and int(o.get("Size", 1)) > 0:
            months.append((m.group(1), o["Key"]))
    keys = []
    since = "000000"
    if snapshots:
        snap_date, snap_key = max(snapshots)
        keys.append(snap_key)
        since = snap_date[:6]
    keys += [k for period, k in sorted(months) if period >= since]
    return keys


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def apply_chunks(conn, chunks, columns) -> dict:
    """UPDATE bookings from frames holding KEY + columns. Returns counts."""
    sets = ", ".join(f'"{c}"=?' for c in columns)
    sql = f'UPDATE bookings SET {sets} WHERE "{KEY}"=?'
    read = updated = 0
    for chunk in chunks:
        chunk.columns = [str(c).strip().lstrip("﻿") for c in chunk.columns]
        missing = [c for c in [KEY] + columns if c not in chunk.columns]
        if missing:
            raise ValueError(f"source file lacks columns {missing}")
        ids = pd.to_numeric(chunk[KEY], errors="coerce")
        rows = []
        for i, bt in enumerate(ids):
            if pd.isna(bt):
                continue
            rows.append([_clean(chunk[c].iat[i]) for c in columns] + [int(bt)])
        before = conn.total_changes
        with conn:
            conn.executemany(sql, rows)
        read += len(rows)
        updated += conn.total_changes - before
        log.info("    %s rows read, %s rows updated so far", f"{read:,}", f"{updated:,}")
    return {"read": read, "updated": updated}


def _read_csv_chunks(handle, columns, chunk_size):
    wanted = {KEY, *columns}
    return pd.read_csv(handle, usecols=lambda c: str(c).strip().lstrip("﻿") in wanted,
                       dtype=str, chunksize=chunk_size, encoding="utf-8-sig",
                       keep_default_na=False)


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill booking columns in the warehouse from S3.")
    p.add_argument("--db", help="Warehouse path (default: warehouse.default_db_path()).")
    p.add_argument("--columns", default="BookingUrlId",
                   help="Comma-separated booking columns to refill (default BookingUrlId).")
    p.add_argument("--bucket", default=DEFAULT_BUCKET)
    p.add_argument("--key", action="append", default=[],
                   help="Explicit S3 key(s) to read instead of auto-discovery (repeatable).")
    p.add_argument("--local-file", action="append", default=[],
                   help="Read these local CSVs instead of S3 (repeatable, applied in order).")
    p.add_argument("--chunk-size", type=int, default=200000)
    p.add_argument("--dry-run", action="store_true", help="List the source files and stop.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    if KEY in columns:
        print(f"Error: {KEY} is the match key and can't be backfilled.")
        return 1

    s3 = None
    if args.local_file:
        sources = [("local", f) for f in args.local_file]
    else:
        import boto3
        from modules.utils.config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
        kw = ({"aws_access_key_id": AWS_ACCESS_KEY_ID, "aws_secret_access_key": AWS_SECRET_ACCESS_KEY}
              if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY else {})
        s3 = boto3.client("s3", **kw)
        if args.key:
            keys = args.key
        else:
            objs = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=args.bucket):
                objs += [{"Key": o["Key"], "Size": o["Size"]} for o in page.get("Contents", [])]
            keys = find_source_keys(objs)
        sources = [("s3", k) for k in keys]
    if not sources:
        print("Error: no BookingDataAll / BookingData files found.")
        return 1
    print("Source files (applied in this order):")
    for _, ref in sources:
        print(f"  {ref}")
    if args.dry_run:
        return 0

    db_path = args.db or warehouse.default_db_path()
    if not os.path.exists(db_path):
        print(f"Error: warehouse database not found: {db_path}")
        return 1
    conn = warehouse.connect(db_path)
    try:
        if not warehouse._table_exists(conn, "bookings"):
            print("Error: warehouse has no bookings table.")
            return 1
        with conn:
            warehouse._ensure_columns(conn, "bookings", columns)
        t0 = time.time()
        totals = {"read": 0, "updated": 0}
        for kind, ref in sources:
            log.info("Reading %s", ref)
            if kind == "local":
                chunks = _read_csv_chunks(ref, columns, args.chunk_size)
            else:
                body = s3.get_object(Bucket=args.bucket, Key=ref)["Body"]
                chunks = _read_csv_chunks(body, columns, args.chunk_size)
            stats = apply_chunks(conn, chunks, columns)
            log.info("  %s: %s read, %s updated", ref, f"{stats['read']:,}", f"{stats['updated']:,}")
            for k in totals:
                totals[k] += stats[k]
        warehouse.drop_retired_indexes(conn)
        filled = {c: conn.execute(f'SELECT COUNT(*) FROM bookings WHERE "{c}" IS NOT NULL').fetchone()[0]
                  for c in columns}
        total = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    except (sqlite3.Error, ValueError) as e:
        print(f"Error: {e}")
        return 1
    finally:
        conn.close()
    print(f"Done in {time.time() - t0:,.0f}s: {totals['read']:,} source rows, "
          f"{totals['updated']:,} booking rows updated.")
    for c, n in filled.items():
        print(f"  {c}: {n:,} of {total:,} bookings now filled ({100.0 * n / max(total, 1):.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
