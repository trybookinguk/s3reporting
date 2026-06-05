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

# Pre-aggregated account_metrics tables.
#
# getAccountMetricsDuck in the dashboard used to run these four GROUP BY scans
# over the full 4.85M-row bookings table on every cold request (~6s). We move
# the scans here — they run once per materialise — and the endpoint just
# SELECTs the small results (~8.7k + ~94k rows) and does its JS post-processing
# (tier scoring etc.) on those. Endpoint cold time drops from ~6s to <100ms.
#
# Date windows are baked at materialise time relative to "today" in
# Europe/London. That's correct: the dashboard data is "as of" the last
# materialise, and the page already labels it "Data as of …". The endpoint
# must therefore NOT recompute these windows — it reads the baked sums.
# (activity_rating / account_age are still computed live from now() in JS,
# since those are elapsed-time fields, not windowed sums.)
#
# is_bo / txn_date / total_fees mirror the expressions in warehouse_duck.ts
# exactly so the materialised path is byte-equivalent to the old live path.
_DUCKDB_ACCOUNT_METRICS_AGG = """
INSTALL icu; LOAD icu;

-- Source rows with London-local txn date + Box-Office flag + ex-VAT fees,
-- computed once so the conditional aggregates below reference cheap columns.
CREATE TEMP TABLE _am_src AS
SELECT
    AccountId AS aid,
    PaymentReceived,
    (COALESCE(BookingFee,0)+COALESCE(CardFee,0)
     +COALESCE(ProcessingFee,0)+COALESCE(TicketFee,0))/1.20 AS total_fees,
    TicketQuantity,
    TransactionDate,
    EventId,
    GatewayGroup,
    AccountPostcode,
    (TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS txn_date,
    CAST(EXTRACT(year FROM
        (TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE
    ) AS INTEGER) AS txn_year,
    CASE
        WHEN UPPER(COALESCE(TRIM(PaymentType),'')) LIKE '%CARD PRESENT%'
             OR UPPER(COALESCE(TRIM(PaymentType),'')) = 'CASH' THEN 1
        ELSE 0
    END AS is_bo
FROM dst.bookings
WHERE Status = 'Successful' AND AccountId IS NOT NULL;

-- "today" in Europe/London and the rolling-window cutoffs, baked once.
CREATE TEMP TABLE _am_cut AS
SELECT
    d AS today,
    d - INTERVAL 365 DAY AS cut365,
    d - INTERVAL 730 DAY AS cut730,
    d - INTERVAL 180 DAY AS cut180
FROM (SELECT (now() AT TIME ZONE 'Europe/London')::DATE AS d);

-- 1. Per-account aggregate (windowed conditional sums). One row per account.
CREATE TABLE dst.account_metrics_agg AS
SELECT
    s.aid,
    SUM(s.total_fees) AS fees_lifetime,
    SUM(COALESCE(s.PaymentReceived,0)) AS revenue_lifetime,
    SUM(COALESCE(s.TicketQuantity,0)) AS tickets_lifetime,
    COUNT(*) AS txns_lifetime,
    COUNT(DISTINCT s.EventId) AS events_lifetime,
    COUNT(DISTINCT s.txn_year) AS years_active,
    CAST(MIN(s.TransactionDate) AS VARCHAR) AS first_txn,
    CAST(MAX(s.TransactionDate) AS VARCHAR) AS last_txn,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN s.total_fees ELSE 0 END) AS fees_current,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN COALESCE(s.PaymentReceived,0) ELSE 0 END) AS revenue_current,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN COALESCE(s.TicketQuantity,0) ELSE 0 END) AS tickets_current,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN 1 ELSE 0 END) AS txns_current,
    COUNT(DISTINCT CASE WHEN s.txn_date >= c.cut365 THEN s.EventId END) AS events_current,
    SUM(CASE WHEN s.txn_date >= c.cut730 AND s.txn_date < c.cut365 THEN s.total_fees ELSE 0 END) AS fees_previous,
    SUM(CASE WHEN s.txn_date >= c.cut730 AND s.txn_date < c.cut365 THEN COALESCE(s.PaymentReceived,0) ELSE 0 END) AS revenue_previous,
    SUM(CASE WHEN s.txn_date >= c.cut730 AND s.txn_date < c.cut365 THEN COALESCE(s.TicketQuantity,0) ELSE 0 END) AS tickets_previous,
    SUM(CASE WHEN s.is_bo = 1 THEN s.total_fees ELSE 0 END) AS fees_bo_lifetime,
    SUM(CASE WHEN s.is_bo = 1 THEN COALESCE(s.PaymentReceived,0) ELSE 0 END) AS revenue_bo_lifetime,
    SUM(CASE WHEN s.is_bo = 1 THEN COALESCE(s.TicketQuantity,0) ELSE 0 END) AS tickets_bo_lifetime,
    SUM(CASE WHEN s.is_bo = 1 THEN 1 ELSE 0 END) AS txns_bo_lifetime,
    CAST(MAX(CASE WHEN s.is_bo = 1 THEN s.TransactionDate END) AS VARCHAR) AS last_bo_txn,
    SUM(CASE WHEN s.is_bo = 1 AND s.txn_date >= c.cut365 THEN s.total_fees ELSE 0 END) AS fees_bo_current,
    SUM(CASE WHEN s.is_bo = 1 AND s.txn_date >= c.cut365 THEN COALESCE(s.PaymentReceived,0) ELSE 0 END) AS revenue_bo_current,
    SUM(CASE WHEN s.is_bo = 1 AND s.txn_date >= c.cut365 THEN COALESCE(s.TicketQuantity,0) ELSE 0 END) AS tickets_bo_current,
    SUM(CASE WHEN s.txn_date >= c.cut180 THEN COALESCE(s.PaymentReceived,0) ELSE 0 END) AS recent_180_paid_revenue
FROM _am_src s CROSS JOIN _am_cut c
GROUP BY s.aid;

-- 2. Dominant-gateway feeder: per (account, gateway) txn count.
CREATE TABLE dst.account_metrics_gateway AS
SELECT aid, GatewayGroup AS gw, COUNT(*) AS n
FROM _am_src
GROUP BY aid, GatewayGroup;

-- 3. Box-Office % feeder: per-account BO vs total Successful count.
CREATE TABLE dst.account_metrics_bopct AS
SELECT aid, SUM(is_bo) AS bo, COUNT(*) AS total
FROM _am_src
GROUP BY aid;

-- 4. Price-band feeder: per (account, event) revenue + tickets.
CREATE TABLE dst.account_metrics_eventrev AS
SELECT aid, EventId, SUM(COALESCE(PaymentReceived,0)) AS rev, SUM(COALESCE(TicketQuantity,0)) AS tix
FROM _am_src
WHERE EventId IS NOT NULL
GROUP BY aid, EventId;

-- 5. Postcode-area feeder: first-by-MIN non-empty AccountPostcode per account.
CREATE TABLE dst.account_metrics_postcode AS
SELECT aid, MIN(AccountPostcode) AS pc
FROM _am_src
WHERE AccountPostcode IS NOT NULL AND AccountPostcode != ''
GROUP BY aid;

DROP TABLE _am_src;
DROP TABLE _am_cut;
"""


_DUCKDB_DAILY_METRICS_AGG = """
-- daily_metrics: three day-grouped feeders + a one-row date-bounds table.
-- Built from dst.bookings / dst.accounts, London-local dates. The series end
-- ("today") is baked here so the reader's dense fill matches the build instant.
CREATE TABLE dst.daily_metrics_agg AS
SELECT
    CAST((TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS date,
    ROUND(SUM(COALESCE(BookingFee,0)+COALESCE(CardFee,0)
             +COALESCE(ProcessingFee,0)+COALESCE(TicketFee,0))/1.20, 2) AS total_fees,
    ROUND(SUM(COALESCE(PaymentReceived,0)), 2) AS total_revenue,
    CAST(SUM(COALESCE(TicketQuantity,0)) AS INTEGER) AS total_tickets,
    COUNT(*) AS total_transactions,
    COUNT(DISTINCT AccountId) AS accounts_selling,
    COUNT(DISTINCT EventId) AS events_with_sales
FROM dst.bookings
WHERE Status = 'Successful'
GROUP BY 1;

CREATE TABLE dst.daily_metrics_accounts AS
SELECT
    CAST((DateTimeCreated AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS date,
    COUNT(*) AS new_accounts,
    SUM(CASE WHEN FirstEventCreation IS NOT NULL AND CAST(FirstEventCreation AS VARCHAR) != ''
             THEN 1 ELSE 0 END) AS new_accounts_with_events
FROM dst.accounts
WHERE DateTimeCreated IS NOT NULL AND CAST(DateTimeCreated AS VARCHAR) != ''
GROUP BY 1;

CREATE TABLE dst.daily_metrics_sales AS
SELECT
    CAST((a.DateTimeCreated AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS date,
    COUNT(*) AS new_accounts_with_sales
FROM dst.accounts a
WHERE a.DateTimeCreated IS NOT NULL AND CAST(a.DateTimeCreated AS VARCHAR) != ''
  AND a.Id IN (
    SELECT DISTINCT AccountId
    FROM dst.bookings
    WHERE AccountId IS NOT NULL AND Status = 'Successful'
  )
GROUP BY 1;

CREATE TABLE dst.daily_metrics_bounds AS
SELECT
    (SELECT MIN(d) FROM (
        SELECT CAST((TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS d FROM dst.bookings
        UNION ALL
        SELECT CAST((DateTimeCreated AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS d FROM dst.accounts
    )) AS start_date,
    CAST((now() AT TIME ZONE 'Europe/London')::DATE AS VARCHAR) AS end_date;
"""


_DUCKDB_MONTHLY_METRICS_AGG = """
-- Pre-aggregated monthly_metrics tables.
--
-- getMonthlyMetricsDuck used to run three heavy GROUP BY scans over the full
-- 4.85M-row bookings table plus the data-start bound on every cold request. We
-- move those scans here — they run once per materialise — and the endpoint just
-- SELECTs the small results and does its JS post-processing (London-month
-- bucketing, activation windows, tier-qualified counts) on those.
--
-- The London-local year-month buckets are baked here via icu's AT TIME ZONE,
-- reproducing SQLite's strftime('%Y-%m', date(...,'localtime')) exactly. The
-- end-of-range month is NOT baked — the endpoint computes "now" in Europe/London
-- live so the table runs through the current month, matching the SQLite path's
-- date('now','localtime'). first_sale_dt is kept as a VARCHAR ISO string so the
-- JS days-to-first-sale arithmetic round-trips identically.
INSTALL icu; LOAD icu;

-- 1. Per-account lifetime feeder (sales detection + tier qualification).
--    lt uses ALL successful bookings (incl. NULL EventId); ev/per_event use
--    only non-null EventId rows — mirrors the SQLite CTE split exactly.
CREATE TABLE dst.monthly_metrics_acct AS
WITH lt AS (
    SELECT AccountId,
        CAST(MIN(TransactionDate) AS VARCHAR) AS first_sale_dt,
        CAST(SUM(COALESCE(TicketQuantity, 0)) AS INTEGER) AS tickets_lifetime
    FROM dst.bookings
    WHERE Status = 'Successful' AND AccountId IS NOT NULL
    GROUP BY AccountId
), per_event AS (
    SELECT AccountId, EventId,
        SUM(COALESCE(PaymentReceived, 0)) AS event_revenue
    FROM dst.bookings
    WHERE Status = 'Successful' AND AccountId IS NOT NULL AND EventId IS NOT NULL
    GROUP BY AccountId, EventId
), ev AS (
    SELECT AccountId,
        COUNT(*) AS events_lifetime,
        MAX(event_revenue) AS max_event_paid_revenue
    FROM per_event
    GROUP BY AccountId
)
SELECT
    lt.AccountId AS aid,
    lt.first_sale_dt,
    lt.tickets_lifetime,
    COALESCE(ev.events_lifetime, 0) AS events_lifetime,
    COALESCE(ev.max_event_paid_revenue, 0) AS max_event_paid_revenue
FROM lt LEFT JOIN ev ON ev.AccountId = lt.AccountId;

-- 2. Per-month booking aggregate (London year-month). One row per month.
CREATE TABLE dst.monthly_metrics_agg AS
SELECT
    strftime((TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE, '%Y-%m') AS ym,
    CAST(SUM(COALESCE(TicketQuantity, 0)) AS INTEGER) AS total_tickets,
    ROUND(SUM(COALESCE(PaymentReceived, 0)), 2) AS total_revenue,
    ROUND(SUM(COALESCE(BookingFee,0)+COALESCE(CardFee,0)
        +COALESCE(ProcessingFee,0)+COALESCE(TicketFee,0))/1.20, 2) AS total_fees,
    COUNT(*) AS total_txns,
    COUNT(DISTINCT EventId) AS events_with_sales,
    COUNT(DISTINCT AccountId) AS accounts_selling
FROM dst.bookings WHERE Status = 'Successful'
GROUP BY ym;

-- 3. Per-(month, event) revenue feeder for the free/paid event split.
CREATE TABLE dst.monthly_metrics_event_split AS
SELECT
    strftime((TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE, '%Y-%m') AS ym,
    EventId,
    SUM(COALESCE(PaymentReceived, 0)) AS rev
FROM dst.bookings WHERE Status = 'Successful' AND EventId IS NOT NULL
GROUP BY ym, EventId;

-- 4. Data-start bound: earliest London year-month across bookings + accounts.
--    Single-row table the endpoint reads to begin its month loop.
CREATE TABLE dst.monthly_metrics_bounds AS
SELECT MIN(d) AS data_start FROM (
    SELECT MIN(strftime((TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE, '%Y-%m')) AS d
    FROM dst.bookings
    UNION ALL
    SELECT MIN(strftime((DateTimeCreated AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE, '%Y-%m')) AS d
    FROM dst.accounts
);

-- In _materialise_duckdb()'s SQL, after {_DUCKDB_ACCOUNT_METRICS_AGG}, add:
--   {_DUCKDB_MONTHLY_METRICS_AGG}
"""


_DUCKDB_PRICE_BANDS_AGG = """
-- price_bands: pre-classified event-year grain. One row per (EventId, year)
-- with its price band, fees/revenue/tickets, the MIN-account owner and that
-- account's Industry. Both result sets in getPriceBandsDuck (summary and
-- by_industry) are cheap final GROUP BYs over this table. Year uses the
-- Europe/London-local transaction date to match SQLite's strftime+localtime.
CREATE TABLE dst.price_bands_agg AS
WITH event_year AS (
    SELECT
        b.EventId,
        CAST(EXTRACT(year FROM
            (b.TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE
        ) AS INTEGER) AS year,
        SUM(COALESCE(b.PaymentReceived,0)) AS revenue,
        SUM(COALESCE(b.TicketQuantity,0)) AS tickets,
        SUM(COALESCE(b.BookingFee,0)+COALESCE(b.CardFee,0)
           +COALESCE(b.ProcessingFee,0)+COALESCE(b.TicketFee,0))/1.20 AS fees,
        MIN(b.AccountId) AS account_id
    FROM dst.bookings b
    WHERE b.Status = 'Successful' AND b.EventId IS NOT NULL
    GROUP BY b.EventId, year
), banded AS (
    SELECT *,
        revenue / CASE WHEN tickets = 0 THEN 1.0 ELSE tickets END AS avg_ticket_price
    FROM event_year
), classified AS (
    SELECT *,
        CASE
            -- ROUND(...,2)=0 snaps refund-cancelled sub-penny dust (native-double
            -- sums leave e.g. -7e-15) to Free, matching SQLite's text-parsed zero.
            -- Other bands keep raw boundaries to preserve the inter-band Unknown gap.
            WHEN ROUND(avg_ticket_price, 2) = 0 THEN 'Free'
            WHEN avg_ticket_price BETWEEN 0.01 AND 9.99 THEN '£1-£9.99'
            WHEN avg_ticket_price BETWEEN 10 AND 24.99 THEN '£10-£24.99'
            WHEN avg_ticket_price BETWEEN 25 AND 49.99 THEN '£25-£49.99'
            WHEN avg_ticket_price >= 50 THEN '£50+'
            ELSE 'Unknown'
        END AS price_band
    FROM banded
)
SELECT
    c.EventId,
    c.year,
    c.price_band,
    c.revenue,
    c.tickets,
    c.fees,
    c.account_id,
    a.Industry AS industry
FROM classified c
LEFT JOIN dst.accounts a ON c.account_id = a.Id;
"""


_DUCKDB_DORMANCY_AGG = """
-- Dormancy: one row per account with days-since-last-txn, recent-180d paid
-- revenue, and account age in months. Date windows are baked at materialise
-- time. SQLite's getDormancy compares in UTC (julianday('now') /
-- datetime('now') are UTC, and TransactionDate/DateTimeCreated are naive-UTC),
-- so we diff against (now() AT TIME ZONE 'UTC') — NO localtime conversion.
CREATE TABLE dst.dormancy_agg AS
WITH per_acct AS (
    SELECT
        a.Id AS aid,
        a.Industry AS industry,
        a.DateTimeCreated AS created_ts,
        (SELECT MAX(b.TransactionDate) FROM dst.bookings b
            WHERE b.AccountId = a.Id AND b.Status = 'Successful') AS last_txn_ts,
        (SELECT COALESCE(SUM(b.PaymentReceived), 0) FROM dst.bookings b
            WHERE b.AccountId = a.Id AND b.Status = 'Successful'
              AND b.TransactionDate >= (now() AT TIME ZONE 'UTC') - INTERVAL 180 DAY) AS recent_paid
    FROM dst.accounts a
)
SELECT
    aid,
    industry,
    CASE
        WHEN last_txn_ts IS NULL THEN NULL
        ELSE EPOCH((now() AT TIME ZONE 'UTC') - last_txn_ts) / 86400.0
    END AS days_since,
    recent_paid,
    CASE
        WHEN created_ts IS NULL THEN NULL
        ELSE EPOCH((now() AT TIME ZONE 'UTC') - created_ts) / 86400.0 / 30.44
    END AS age_months
FROM per_acct;
"""


_DUCKDB_CONCENTRATION_AGG = """
-- === concentration_agg (getConcentrationDuck) ============================
-- Two feeders for the tier-concentration endpoint, baked daily. Reuses the
-- same _am_src London-local rollup if present; if you add this block AFTER
-- _DUCKDB_ACCOUNT_METRICS_AGG you can reference _am_src/_am_cut, but to keep
-- this self-contained (and runnable independently) it rebuilds its own temp.

INSTALL icu; LOAD icu;

CREATE TEMP TABLE _conc_src AS
SELECT
    AccountId AS aid,
    PaymentReceived,
    (COALESCE(BookingFee,0)+COALESCE(CardFee,0)
     +COALESCE(ProcessingFee,0)+COALESCE(TicketFee,0))/1.20 AS total_fees,
    TicketQuantity,
    (TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE AS txn_date,
    CAST(EXTRACT(year FROM
        (TransactionDate AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/London')::DATE
    ) AS INTEGER) AS year
FROM dst.bookings
WHERE Status = 'Successful' AND AccountId IS NOT NULL;

CREATE TEMP TABLE _conc_cut AS
SELECT
    d - INTERVAL 365 DAY AS cut365,
    d - INTERVAL 730 DAY AS cut730
FROM (SELECT (now() AT TIME ZONE 'Europe/London')::DATE AS d);

-- 1. Per-account aggregate (v2-scoring inputs). One row per ticketed account.
CREATE TABLE dst.concentration_account_agg AS
SELECT
    s.aid,
    SUM(s.total_fees) AS revenue_lifetime,
    SUM(s.TicketQuantity) AS tickets_lifetime,
    COUNT(DISTINCT s.year) AS years_loyalty,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN s.total_fees ELSE 0 END) AS revenue_current,
    SUM(CASE WHEN s.txn_date >= c.cut365 THEN s.TicketQuantity ELSE 0 END) AS tickets_current,
    SUM(CASE WHEN s.txn_date >= c.cut730 AND s.txn_date < c.cut365
             THEN s.total_fees ELSE 0 END) AS revenue_previous,
    SUM(CASE WHEN s.txn_date >= c.cut730 AND s.txn_date < c.cut365
             THEN s.TicketQuantity ELSE 0 END) AS tickets_previous
FROM _conc_src s CROSS JOIN _conc_cut c
GROUP BY s.aid
HAVING SUM(s.TicketQuantity) > 0;

-- 2. Per-(account, year) booking rollup for the tier / year-tier sums.
--    Lossless vs the SQLite per-row iterate(): the JS only Set-dedupes aid and
--    sums fees/revenue, both grouped by aid and year.
CREATE TABLE dst.concentration_booking_agg AS
SELECT
    aid,
    year,
    SUM(COALESCE(PaymentReceived, 0)) AS revenue,
    SUM(total_fees) AS fees
FROM _conc_src
GROUP BY aid, year;

DROP TABLE _conc_src;
DROP TABLE _conc_cut;
"""


# Client-match index for the Database Builder's Stage 1 (reporting-dashboard).
#
# Stage 1 matches an uploaded prospect list against our existing client base.
# Doing that live would mean joining accounts + users + the 4.95M-row bookings
# per build run — so we bake a small one-row-per-account index here (same
# pattern as the other *_agg tables) and the dashboard endpoint just SELECTs it.
#
# Per account it carries the signals the matcher corroborates on:
#   - normalised AccountName (+ raw, for display)
#   - AccountStatus, Industry, DateTimeCreated (context shown at the gate)
#   - domains: comma-joined distinct email domains from the Users table
#     (the strongest match signal — exact email-domain hit). Freemail domains
#     are kept here and filtered in JS, so the list stays source-of-truth.
#   - postcode_area: outward area letters, AccountPostcode first then a
#     fallback to the most-common EventPostcode area (most bookings carry a
#     venue postcode but few carry an account postcode). Mirrors the
#     account-then-event precedence in modules/uk_regional_segmentation.py's
#     assign_account_regions; postcode_source records which one won.
#   - last_booked: most recent Successful transaction date
#
# Freshness is inherited from the nightly materialise — same exposure the old
# accounts.json blob already had (brief §4a), no new risk.
_DUCKDB_CLIENT_MATCH_INDEX = """
-- === client_match_index (Database Builder Stage 1) =======================
INSTALL icu; LOAD icu;

-- Per-account email domains from the Users table. Username is the email;
-- take the part after '@', lowercased. Distinct per (account, domain).
CREATE TEMP TABLE _cmi_domains AS
SELECT
    CAST(CAST(AccountId AS VARCHAR) AS BIGINT) AS aid,
    LOWER(TRIM(SPLIT_PART(Username, '@', 2))) AS domain
FROM dst.users
WHERE Username IS NOT NULL AND Username LIKE '%@%';

CREATE TEMP TABLE _cmi_domain_agg AS
SELECT aid, STRING_AGG(DISTINCT domain, ',') AS domains
FROM _cmi_domains
WHERE domain IS NOT NULL AND domain != ''
GROUP BY aid;

-- One scan of Successful bookings → per-row account area (from AccountPostcode)
-- and event area (from EventPostcode), plus the transaction date. The area is
-- the leading 1-2 letters, matching extract_postcode_areas_vectorized's
-- ^[A-Z]{1,2} rule. NULLIF turns an empty extract into a real NULL so the
-- frequency rollups below ignore it.
CREATE TEMP TABLE _cmi_book AS
SELECT
    AccountId AS aid,
    NULLIF(REGEXP_EXTRACT(UPPER(TRIM(AccountPostcode)), '^[A-Z]{1,2}'), '') AS acct_area,
    NULLIF(REGEXP_EXTRACT(UPPER(TRIM(EventPostcode)), '^[A-Z]{1,2}'), '') AS event_area,
    TransactionDate
FROM dst.bookings
WHERE Status = 'Successful' AND AccountId IS NOT NULL;

-- Most-frequent AccountPostcode area per account (the authoritative signal).
CREATE TEMP TABLE _cmi_acct_pc AS
SELECT aid, area FROM (
    SELECT aid, acct_area AS area,
           ROW_NUMBER() OVER (PARTITION BY aid ORDER BY COUNT(*) DESC) AS rn
    FROM _cmi_book WHERE acct_area IS NOT NULL
    GROUP BY aid, acct_area
) WHERE rn = 1;

-- Most-frequent EventPostcode (venue) area per account — the fallback, used
-- only where the account has no AccountPostcode of its own.
CREATE TEMP TABLE _cmi_event_pc AS
SELECT aid, area FROM (
    SELECT aid, event_area AS area,
           ROW_NUMBER() OVER (PARTITION BY aid ORDER BY COUNT(*) DESC) AS rn
    FROM _cmi_book WHERE event_area IS NOT NULL
    GROUP BY aid, event_area
) WHERE rn = 1;

CREATE TEMP TABLE _cmi_lastbooked AS
SELECT aid, CAST(MAX(TransactionDate) AS VARCHAR) AS last_booked
FROM _cmi_book
GROUP BY aid;

-- One row per account. LEFT JOINs so accounts with no bookings/users still
-- appear (name-only match still possible). postcode_area prefers the account's
-- own area and falls back to the venue area; postcode_source flags which.
CREATE TABLE dst.client_match_index AS
SELECT
    a.Id AS aid,
    a.AccountName AS account_name,
    a.AccountStatus AS account_status,
    a.Industry AS industry,
    CAST(a.DateTimeCreated AS VARCHAR) AS created_at,
    d.domains AS domains,
    COALESCE(apc.area, epc.area) AS postcode_area,
    CASE
        WHEN apc.area IS NOT NULL THEN 'account'
        WHEN epc.area IS NOT NULL THEN 'event'
        ELSE NULL
    END AS postcode_source,
    lb.last_booked AS last_booked
FROM dst.accounts a
LEFT JOIN _cmi_domain_agg d ON d.aid = a.Id
LEFT JOIN _cmi_acct_pc apc ON apc.aid = a.Id
LEFT JOIN _cmi_event_pc epc ON epc.aid = a.Id
LEFT JOIN _cmi_lastbooked lb ON lb.aid = a.Id;

DROP TABLE _cmi_domains;
DROP TABLE _cmi_domain_agg;
DROP TABLE _cmi_book;
DROP TABLE _cmi_acct_pc;
DROP TABLE _cmi_event_pc;
DROP TABLE _cmi_lastbooked;
"""


# === retention_agg (getRetentionDuck) ====================================
# The CS /retention worklist shows the *exact* retention priority the pandas
# tier pipeline computes (retention_priority.py), not a JS re-derivation.
# zoho_tiers.py writes that figure into the SQLite warehouse's
# retention_priority table (keyed by account_id); here we just copy it across
# verbatim so the dashboard can SELECT it.
#
# Guarded with a SQLite-side existence check: the table only appears after the
# first zoho_tiers run, and that job runs *after* this materialise (≈02:45 vs
# ≈02:00), so on a fresh Pi — or the very first day — the source table won't
# exist yet. The IF-absent branch creates an empty, correctly-typed agg table
# so the dashboard join degrades to NULL priority rather than erroring.
_DUCKDB_RETENTION_AGG = """
CREATE TABLE dst.retention_agg AS
SELECT
    CAST(account_id AS VARCHAR) AS account_id,
    retention_priority,
    TRY_CAST(retention_priority_score AS INTEGER) AS retention_priority_score
FROM src.retention_priority;
"""

# Empty fallback used when src.retention_priority does not yet exist.
_DUCKDB_RETENTION_AGG_EMPTY = """
CREATE TABLE dst.retention_agg (
    account_id VARCHAR,
    retention_priority VARCHAR,
    retention_priority_score INTEGER
);
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

    # Resolve the duckdb binary. shutil.which() relies on PATH, which is minimal
    # under cron (no /usr/local/bin) — that silently froze the DuckDB warehouse
    # for days (the dashboard reads it; cron skipped the materialise nightly while
    # interactive runs worked). Fall back to known install locations, and allow an
    # explicit override via DUCKDB_BIN, before giving up.
    duckdb_bin = (
        os.environ.get("DUCKDB_BIN")
        or shutil.which("duckdb")
        or next((p for p in ("/usr/local/bin/duckdb", "/usr/bin/duckdb",
                             "/opt/homebrew/bin/duckdb") if os.path.exists(p)), None)
    )
    if not duckdb_bin:
        log.error(
            "duckdb binary not found (checked DUCKDB_BIN, PATH, and "
            "/usr/local/bin, /usr/bin, /opt/homebrew/bin) — DuckDB materialise "
            "SKIPPED. The dashboard warehouse will go stale. Install duckdb or "
            "set DUCKDB_BIN."
        )
        return

    sqlite_path = warehouse.default_db_path()
    if not os.path.exists(sqlite_path):
        log.warning("SQLite warehouse missing at %s — skipping materialise.", sqlite_path)
        return

    # retention_priority is written by zoho_tiers.py, which runs *after* this
    # job, so on a fresh Pi / first day the source table won't exist yet. Probe
    # SQLite directly and pick the copy-across vs empty-shell fragment — a
    # missing table inside the single duckdb CLI run would otherwise abort the
    # whole materialise.
    import sqlite3
    retention_sql = _DUCKDB_RETENTION_AGG_EMPTY
    try:
        _probe = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            exists = _probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_priority'"
            ).fetchone()
            if exists:
                retention_sql = _DUCKDB_RETENTION_AGG
            else:
                log.info(
                    "retention_priority table not present yet (zoho_tiers hasn't "
                    "run) — materialising an empty retention_agg."
                )
        finally:
            _probe.close()
    except Exception as e:
        log.warning("Could not probe retention_priority table (%s) — empty agg.", e)

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

{_DUCKDB_ACCOUNT_METRICS_AGG}

{_DUCKDB_DAILY_METRICS_AGG}

{_DUCKDB_MONTHLY_METRICS_AGG}

{_DUCKDB_PRICE_BANDS_AGG}

{_DUCKDB_DORMANCY_AGG}

{_DUCKDB_CONCENTRATION_AGG}

{_DUCKDB_CLIENT_MATCH_INDEX}

{retention_sql}
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
