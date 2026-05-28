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
        log.info("  Upserting current-month BookingData (streamed)...")
        month_chunks = loader.load_booking_chunks(
            target_date=report_date, data_type="BookingData", chunk_size=100000
        )
        stats = warehouse.upsert_bookings_chunks(conn, month_chunks)
        log.info("    Daily: %s", stats)

        # Snapshots (these frames are small — accounts/users are tens of MB).
        if accounts_df is not None:
            warehouse.replace_snapshot(conn, "accounts", accounts_df, key="Id")
        if users_df is not None:
            warehouse.replace_snapshot(conn, "users", users_df)

        log.info("  Warehouse after: %s", warehouse.summary(conn))
    finally:
        conn.close()


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

    sys.exit(main(build_combined=args.combined,
                  build_warehouse=not args.no_warehouse))
