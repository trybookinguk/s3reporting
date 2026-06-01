#!/usr/bin/env python3
"""
Daily data preparation — first cron job of the morning.

Refreshes the local S3 cache and builds the combined booking dataset
(BookingDataAll + current-month BookingData, de-duped) into a single pickle.
The staggered jobs that follow (s3_to_sharepoint, zoho_industry, zoho_tiers,
generate_dashboard_data) then reuse this work instead of each re-downloading
and re-combining from scratch.

Two things make the downstream jobs cheap once this has run:
  - The combined pickle is built once here and loaded directly by
    load_combined_booking_data in the tier and dashboard scripts.
  - Setting CACHE_TRUST_TODAY=1 on the downstream jobs lets them skip the
    per-file head_object validation, trusting that this job refreshed the
    cache earlier today.

Usage:
    python3 prepare_data.py                # Refresh cache + rebuild combined pickle
    python3 prepare_data.py --no-combined  # Refresh per-file cache only
"""

import argparse
import logging
import sys

import pandas as pd

from modules.utils.config import UK_TZ
from modules.utils.data_loader import (
    get_loader,
    load_accounts,
    load_users,
    load_combined_booking_data,
)
from modules import warehouse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("prepare-data")


def main(build_combined: bool = False, build_warehouse: bool = True) -> int:
    started = pd.Timestamp.now(UK_TZ)
    log.info("Data preparation started at %s", started.strftime("%Y-%m-%d %H:%M:%S %Z"))

    # Force a fresh look at S3 regardless of any CACHE_TRUST_TODAY set for the
    # downstream jobs — this job is the one that establishes "today's" cache.
    loader = get_loader()
    log.info("Cache dir: %s", loader.cache_dir)
    log.info("Data dir:  %s", loader.data_dir)

    report_date = pd.Timestamp.now(UK_TZ).normalize()

    # Refresh the per-file caches the other jobs rely on. download_s3_file /
    # load_* re-download only if the S3 ETag changed (or cache >7 days old),
    # so on an unchanged day these are just head_object calls.
    try:
        log.info("Refreshing Accounts cache...")
        accounts_df = load_accounts(report_date)
        log.info("  Accounts: %d rows", len(accounts_df))
    except Exception as e:
        log.error("Failed to refresh Accounts: %s", e)
        return 1

    users_df = None
    try:
        log.info("Refreshing Users cache...")
        users_df = load_users(report_date)
        log.info("  Users: %d rows", len(users_df))
    except Exception as e:
        # Users isn't needed by every downstream job — warn, don't fail the run.
        log.warning("Failed to refresh Users (continuing): %s", e)

    if build_combined:
        try:
            log.info("Building combined booking dataset...")
            combined = load_combined_booking_data(report_date, force_rebuild=True)
            log.info("  Combined booking rows: %d", len(combined))
        except Exception as e:
            log.error("Failed to build combined booking data: %s", e)
            return 1

    if build_warehouse:
        try:
            _update_warehouse(report_date, accounts_df, users_df)
        except Exception as e:
            log.error("Failed to update warehouse: %s", e)
            return 1

        # Materialise to DuckDB for the dashboard. SQLite stores everything as
        # BLOB which forces row-by-row processing on every dashboard query.
        # DuckDB's columnar format reads ~50x faster on the analytical workload
        # the dashboard runs (multi-table joins, percentile ranks, time
        # bucketing across all 5M bookings).
        try:
            _materialise_duckdb()
        except Exception as e:
            # Don't fail the run — dashboard falls back to SQLite (it still
            # works, just slowly). Failure email will fire from the cron
            # wrapper, but the upstream Zoho/SharePoint jobs that depend on
            # SQLite stay on the green path.
            log.error("DuckDB materialise failed (dashboard stays on SQLite): %s", e)

    elapsed = (pd.Timestamp.now(UK_TZ) - started).total_seconds()
    log.info("Data preparation complete in %.1fs", elapsed)
    return 0


def _update_warehouse(report_date, accounts_df, users_df) -> None:
    """Maintain the SQLite warehouse: seed bookings once, then daily upsert.

    - bookings: if the table is empty, seed it from the full BookingDataAll;
      then upsert the current-month BookingData by BookingTransactionId so
      revised rows (Status/fee changes) are corrected in place. Prior-month
      rows are never deleted — the daily file just doesn't contain them.
    - accounts / users: full-replace snapshots (current state, not a log).
    """
    db_path = warehouse.default_db_path()
    log.info("Updating warehouse at %s", db_path)
    loader = get_loader()
    conn = warehouse.connect(db_path)
    try:
        before = warehouse.summary(conn)
        log.info("  Warehouse before: %s", before)

        # Bookings are streamed in chunks — never the whole frame in memory.
        # On a 4 GB Pi the full BookingDataAll (~4.6M rows / ~2 GB) cannot be
        # held as one DataFrame, so we ingest directly from the chunk
        # generator. use_cache is irrelevant here (load_booking_chunks streams
        # from S3); we deliberately avoid load_booking_data, which would
        # materialise the whole file.

        # Seed from BookingDataAll on first run only.
        if before["bookings_rows"] == 0:
            log.info("  bookings table empty — seeding from BookingDataAll (streamed)...")
            chunks = loader.load_booking_chunks(
                target_date=report_date, data_type="BookingDataAll", chunk_size=100000
            )
            stats = warehouse.upsert_bookings_chunks(conn, chunks, seed=True)
            log.info("    Seed: %s", stats)

        # Daily delta: upsert the current-month BookingData (cumulative-to-date).
        # The job runs every day (see deploy/pi-crontab), so each day's run picks
        # up the previous day's bookings as they land in the cumulative file.
        #
        # Month boundary: a day's data lands in the cumulative file the *next*
        # morning, so the final day of month M (e.g. 31 May) only appears in M's
        # file on the 1st of M+1 — and the new month's file doesn't exist until
        # the 2nd. So on the 1st the current-month file 404s; we must fall back to
        # the *previous* month's file to capture that final day. This upsert is
        # index-keyed and idempotent (INSERT OR REPLACE on BookingTransactionId),
        # so re-reading last month's file just corrects/fills its last day.
        log.info("  Upserting current-month BookingData (streamed)...")

        def _upsert_booking_month(target_date, label):
            chunks = loader.load_booking_chunks(
                target_date=target_date, data_type="BookingData", chunk_size=100000
            )
            stats = warehouse.upsert_bookings_chunks(conn, chunks)
            log.info("    %s: %s", label, stats)

        try:
            _upsert_booking_month(report_date, "Daily")
        except Exception as e:
            error_str = str(e)
            if 'NoSuchKey' not in error_str and '404' not in error_str and 'Not Found' not in error_str:
                raise
            prev_month_date = (report_date - pd.DateOffset(months=1)).normalize()
            log.warning(
                "    Current-month BookingData not published yet (expected on the "
                "1st) — falling back to previous month (%s) to capture its final "
                "day.", prev_month_date.strftime("%Y%m"),
            )
            try:
                _upsert_booking_month(prev_month_date, "Daily (previous month)")
            except Exception as e2:
                error_str2 = str(e2)
                if 'NoSuchKey' not in error_str2 and '404' not in error_str2 and 'Not Found' not in error_str2:
                    raise
                log.warning(
                    "    Previous-month BookingData also unavailable — skipping "
                    "daily delta: %s", e2
                )

        # Snapshots (these frames are small — accounts/users are tens of MB).
        if accounts_df is not None:
            warehouse.replace_snapshot(conn, "accounts", accounts_df, key="Id")
        if users_df is not None:
            warehouse.replace_snapshot(conn, "users", users_df)

        # PPC attribution from GA4 — isolated so a GA4 outage (auth, rate
        # limit, network) doesn't roll back the rest of the day's ingest.
        # Yesterday's ppc_attribution rows stay in place if GA4 fails today.
        try:
            from modules import ga4_ingest
            n = ga4_ingest.refresh_ppc(conn, lookback_days=30)
            log.info("  PPC attribution: refreshed %d rows", n)
        except Exception as e:
            log.error("  PPC refresh raised (skipping): %s", e)

        log.info("  Warehouse after: %s", warehouse.summary(conn))
    finally:
        conn.close()


# DuckDB destination path: sits next to warehouse.db so the dashboard's
# WAREHOUSE_DUCK_DB env var defaults to a predictable location.
def _duckdb_path() -> str:
    import os
    sqlite_path = warehouse.default_db_path()
    return os.path.join(os.path.dirname(sqlite_path), "warehouse_duck.db")


# Column list per table. Casts go BLOB -> VARCHAR (numeric/timestamp) or
# BLOB -> BLOB -> decode() (text), because DuckDB's SQLite scanner serialises
# BLOB-as-VARCHAR with hex escapes for anything it doesn't trust (including
# 0x27 apostrophe). decode() round-trips the bytes as UTF-8, which is what
# the SQLite columns actually contain.
_DUCKDB_BOOKINGS_COLS = """
    CAST(CAST(BookingTransactionId AS VARCHAR) AS BIGINT) AS BookingTransactionId,
    CAST(CAST(AccountId AS VARCHAR) AS BIGINT) AS AccountId,
    decode(CAST(AccountName AS BLOB)) AS AccountName,
    CAST(CAST(EventId AS VARCHAR) AS BIGINT) AS EventId,
    decode(CAST(EventName AS BLOB)) AS EventName,
    CAST(CAST(TransactionDate AS VARCHAR) AS TIMESTAMP) AS TransactionDate,
    CAST(CAST(EventDate AS VARCHAR) AS TIMESTAMP) AS EventDate,
    decode(CAST(Status AS BLOB)) AS Status,
    CAST(CAST(PaymentReceived AS VARCHAR) AS DOUBLE) AS PaymentReceived,
    CAST(CAST(BookingFee AS VARCHAR) AS DOUBLE) AS BookingFee,
    CAST(CAST(CardFee AS VARCHAR) AS DOUBLE) AS CardFee,
    CAST(CAST(ProcessingFee AS VARCHAR) AS DOUBLE) AS ProcessingFee,
    CAST(CAST(TicketFee AS VARCHAR) AS DOUBLE) AS TicketFee,
    CAST(CAST(TicketQuantity AS VARCHAR) AS INTEGER) AS TicketQuantity,
    decode(CAST(PaymentType AS BLOB)) AS PaymentType,
    decode(CAST(GatewayGroup AS BLOB)) AS GatewayGroup,
    decode(CAST(GatewayName AS BLOB)) AS GatewayName,
    decode(CAST(EventPostcode AS BLOB)) AS EventPostcode,
    decode(CAST(AccountPostcode AS BLOB)) AS AccountPostcode,
    decode(CAST(Industry AS BLOB)) AS BookingIndustry,
    decode(CAST(SubIndustry AS BLOB)) AS BookingSubIndustry
"""

_DUCKDB_ACCOUNTS_COLS = """
    CAST(CAST(Id AS VARCHAR) AS BIGINT) AS Id,
    decode(CAST(AccountName AS BLOB)) AS AccountName,
    decode(CAST(AccountStatus AS BLOB)) AS AccountStatus,
    CAST(CAST(DateTimeCreated AS VARCHAR) AS TIMESTAMP) AS DateTimeCreated,
    CAST(CAST(LastLogIn AS VARCHAR) AS TIMESTAMP) AS LastLogIn,
    CAST(CAST(FirstEventCreation AS VARCHAR) AS TIMESTAMP) AS FirstEventCreation,
    CAST(CAST(LastEventCreation AS VARCHAR) AS TIMESTAMP) AS LastEventCreation,
    decode(CAST(Industry AS BLOB)) AS Industry,
    decode(CAST(SubIndustry AS BLOB)) AS SubIndustry,
    decode(CAST(GatewayGroup AS BLOB)) AS GatewayGroup,
    decode(CAST(Postcode AS BLOB)) AS Postcode
"""


def _materialise_duckdb() -> None:
    """Build a fresh DuckDB file from the SQLite warehouse.

    Writes to a .tmp path first then renames atomically. The dashboard reads
    the final path, so partial writes never replace a working file.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    import time

    duckdb_bin = shutil.which("duckdb")
    if not duckdb_bin:
        log.warning("duckdb CLI not on PATH — skipping DuckDB materialise.")
        return

    sqlite_path = warehouse.default_db_path()
    if not os.path.exists(sqlite_path):
        log.warning("SQLite warehouse missing at %s — skipping materialise.", sqlite_path)
        return

    target = _duckdb_path()
    # NamedTemporaryFile in the same directory so the rename is atomic on the
    # same filesystem. delete=False because we want the file to persist past
    # the with-block; we delete it ourselves on failure.
    fd, tmp_path = tempfile.mkstemp(suffix=".db.tmp", prefix="warehouse_duck.",
                                    dir=os.path.dirname(target))
    os.close(fd)
    os.unlink(tmp_path)  # duckdb refuses to ATTACH an existing-but-empty file

    sql = f"""
INSTALL sqlite;
LOAD sqlite;
ATTACH '{sqlite_path}' AS src (TYPE sqlite, READ_ONLY);
ATTACH '{tmp_path}' AS dst;

CREATE TABLE dst.bookings AS SELECT {_DUCKDB_BOOKINGS_COLS} FROM src.bookings;
CREATE TABLE dst.accounts AS SELECT {_DUCKDB_ACCOUNTS_COLS} FROM src.accounts;

-- ppc_attribution and users come back with native types (the Python ingest
-- writes them via pandas-typed to_sql), so a plain SELECT * is sufficient.
CREATE TABLE dst.ppc_attribution AS SELECT * FROM src.ppc_attribution;
CREATE TABLE dst.users AS SELECT * FROM src.users;

-- Indexes that match the SQLite hot paths. DuckDB's planner mostly doesn't
-- need them at this scale (columnar scans are fast), but the AccountId index
-- helps point lookups like sales_commission_report.py's WHERE AccountId IN ().
CREATE INDEX bookings_account ON dst.bookings (AccountId);
CREATE INDEX bookings_event ON dst.bookings (EventId);
CREATE INDEX accounts_id ON dst.accounts (Id);
"""

    log.info("Materialising DuckDB warehouse at %s...", target)
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            [duckdb_bin],
            input=sql,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"duckdb exited {result.returncode}: stderr={result.stderr.strip()}"
            )
    except Exception:
        # Clean up the partial file so the next run starts clean.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    # Atomic rename — the dashboard's mtime-watch will see the new file
    # immediately and re-open it.
    os.replace(tmp_path, target)
    size_mb = os.path.getsize(target) / (1024 * 1024)
    elapsed = time.perf_counter() - t0
    log.info("  DuckDB written: %.1f MB in %.1fs", size_mb, elapsed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Daily S3 cache refresh + SQLite warehouse update."
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Also build the combined booking pickle. OFF by default — it holds "
             "the full ~4.6M-row frame in memory and OOMs a 4 GB Pi. The "
             "warehouse is the memory-safe replacement.",
    )
    parser.add_argument(
        "--no-warehouse",
        action="store_true",
        help="Skip updating the SQLite warehouse (warehouse.db).",
    )
    parser.add_argument(
        "--seed-from-pickle",
        action="store_true",
        help="Bulk-seed the warehouse bookings table from an existing "
             "combined_booking.pkl (much faster than the streaming seed; ~minutes "
             "vs hour+). Use this once on a dev box, then scp warehouse.db to "
             "the Pi so the Pi only ever does daily upserts.",
    )
    parser.add_argument(
        "--seed-ppc",
        action="store_true",
        help="One-off: backfill ppc_attribution from GA4 going back ~700 days "
             "(to mid-2024, the legacy start date). Use this once after the "
             "warehouse is seeded; daily runs only refresh the last 30 days.",
    )
    args = parser.parse_args()

    if args.seed_from_pickle:
        import pickle
        from modules import warehouse
        from modules.utils.data_loader import get_loader
        loader = get_loader()
        pkl_path = loader._combined_booking_path()
        if not __import__('os').path.exists(pkl_path):
            log.error("No combined pickle at %s — run `prepare_data.py --combined` first.", pkl_path)
            sys.exit(1)
        log.info("Loading combined pickle from %s ...", pkl_path)
        with open(pkl_path, 'rb') as f:
            df = pickle.load(f)
        log.info("  Loaded %d rows", len(df))
        conn = warehouse.connect()
        try:
            warehouse.seed_bookings_from_frame(conn, df)
            log.info("Warehouse summary: %s", warehouse.summary(conn))
        finally:
            conn.close()
        sys.exit(0)

    if args.seed_ppc:
        from modules import warehouse, ga4_ingest
        conn = warehouse.connect()
        try:
            n = ga4_ingest.refresh_ppc(conn, lookback_days=700)
            log.info("PPC backfill: %d rows", n)
        finally:
            conn.close()
        sys.exit(0)

    sys.exit(main(build_combined=args.combined,
                  build_warehouse=not args.no_warehouse))
