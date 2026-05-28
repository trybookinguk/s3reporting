#!/usr/bin/env python3
"""
Main runner for Zoho tier updates.
Calculates account tiers, event frequencies, and activity ratings.
"""
import argparse
import time
import pandas as pd
import logging
import os
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import from our modules
from modules.utils.config import UK_TZ, TEST_MODE, TIER_OWNERS, REPORTS_DIR
from modules.utils.data_loader import get_s3_client, load_multiple_booking_files, download_s3_file_cached
from modules.booking_aggregator import BookingAggregator
from modules.utils.config import CUTOFF_365, CUTOFF_730, EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
from modules.account_processor import process_accounts
from modules.utils.zoho_api import get_access_token, upsert_to_zoho
from modules.utils.report_generator import generate_upcoming_annual_events_report, email_upcoming_events_report, email_tier_updates_report
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator
from modules.industry_revenue_report import generate_industry_revenue_reports
from modules.tier_calculator_v2 import calculate_composite_tiers
from modules import tier_snapshot, tier_history, tier_movement_email
from modules.zoho_account_links import lookup_account_urls
from modules.utils.sharepoint import authenticate_graph

SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")
ZOHO_ORG_ID = os.environ.get("ZOHO_ORG_ID")

# Tier system feature flag. v1 keeps the legacy taxonomy
# (Key Account / High Value / Tier 4..1 / NIL) produced by process_accounts.
# v2 swaps Current_Tier/Previous_Tier for the composite calculator's
# Tier 1..5 / Free / Nil labels and adds Tier_Movement to the upsert payload.
# All other fields (Rating, Event_Frequency_*, Retention_Priority, …) are
# unchanged regardless of the flag.
TIER_SYSTEM = os.environ.get("TIER_SYSTEM", "v1").lower()


def _apply_v2_tiers(updates: pd.DataFrame, account_metrics: dict) -> pd.DataFrame:
    """Overlay v2 composite tiers + Tier_Movement onto the v1 updates frame.

    Joins by Account_Name (which process_accounts sets to the AccountId as a
    string). Accounts present in the v1 frame but missing from v2 keep their
    v1 tier values and get an empty Tier_Movement — better than dropping them
    from the Zoho upsert.
    """
    v2_df = calculate_composite_tiers(account_metrics)
    if v2_df.empty:
        logger.warning("v2 calculator returned empty result — falling back to v1 tiers.")
        updates['Tier_Movement'] = ''
        return updates

    v2_lookup = v2_df.assign(_aid_str=v2_df['AccountId'].astype(int).astype(str))
    v2_lookup = v2_lookup.set_index('_aid_str')[['Current_Tier', 'Previous_Tier', 'Tier_Movement']]

    aid_str = updates['Account_Name'].astype(str)
    matched = v2_lookup.reindex(aid_str)
    matched.index = updates.index

    updates = updates.copy()
    # Only overwrite where v2 produced a value; preserve v1 otherwise so
    # accounts the v2 calculator skipped (e.g. zero lifetime tickets) still
    # get upserted with whatever v1 decided.
    has_v2 = matched['Current_Tier'].notna()
    updates.loc[has_v2, 'Current_Tier'] = matched.loc[has_v2, 'Current_Tier']
    updates.loc[has_v2, 'Previous_Tier'] = matched.loc[has_v2, 'Previous_Tier']
    updates['Tier_Movement'] = matched['Tier_Movement'].fillna('')

    logger.info("Applied v2 tiers to %d of %d accounts (rest kept v1 fallback).",
                int(has_v2.sum()), len(updates))
    return updates


def _build_revenue_ranks(v2_df):
    """Compute current and 12-months-ago revenue ranks per AccountId.

    Returns {AccountId: {rank_current, rank_current_prev, rank_lifetime,
    rank_lifetime_prev}}. Ranks are 1-indexed against the *paid-activated*
    population (revenue > 0 in the relevant period), so #1 is the highest
    revenue. Accounts without revenue in a period get None for that period's
    rank — the email caller can treat that as "no rank to display".
    """
    out = {}
    if v2_df is None or v2_df.empty:
        return out

    def _ranks_for_column(col_name):
        if col_name not in v2_df.columns:
            return {}
        col = pd.to_numeric(v2_df[col_name], errors="coerce")
        # Only rank accounts with positive revenue in that period — anything
        # else has no meaningful "rank by revenue".
        mask = col > 0
        if not mask.any():
            return {}
        # rank: highest revenue = 1
        ranks = col[mask].rank(method="min", ascending=False).astype(int)
        ids = v2_df.loc[mask, "AccountId"].astype(int)
        return dict(zip(ids, ranks))

    rank_current = _ranks_for_column("Revenue_Current")
    rank_current_prev = _ranks_for_column("Revenue_Current_Prev")
    rank_lifetime = _ranks_for_column("Revenue_Lifetime")
    rank_lifetime_prev = _ranks_for_column("Revenue_Lifetime_Prev")

    for aid in v2_df["AccountId"].astype(int):
        out[aid] = {
            "rank_current": rank_current.get(aid),
            "rank_current_prev": rank_current_prev.get(aid),
            "rank_lifetime": rank_lifetime.get(aid),
            "rank_lifetime_prev": rank_lifetime_prev.get(aid),
        }
    return out


def _build_account_meta_lookup(booking_data_df, account_lookup, account_ids,
                               v2_df=None, revenue_ranks=None,
                               last_sale_override=None, tickets_365_override=None):
    """Build per-account metadata for the tier-movement emails.

    Returns a dict keyed by AccountId with: account_name, industry, sub_industry,
    last_ticket_sale, last_event_created, tickets_365d, account_created,
    years_loyalty, plus revenue rank fields if `revenue_ranks` is provided.

    `last_sale_override` / `tickets_365_override`: pre-computed per-account maps
    (warehouse path) — when given, used instead of scanning booking_data_df,
    which on the warehouse path is only a 90-day frame and lacks the all-time
    last-sale. last_sale values are tz-stripped to match the legacy behaviour.
    """
    bk = booking_data_df
    if last_sale_override is not None or tickets_365_override is not None:
        # Warehouse path: maps already computed via grouped SQL. Strip tz on
        # last_sale to match the legacy frame-derived path (which did the same).
        last_sale = {}
        for aid, ts in (last_sale_override or {}).items():
            ts = pd.Timestamp(ts)
            if ts.tzinfo is not None:
                ts = ts.tz_convert(None)
            last_sale[aid] = ts
        tickets_365 = dict(tickets_365_override or {})
    elif bk is not None and not bk.empty:
        # Successful txns only — failed txns shouldn't drive "last ticket sale"
        if 'Status' in bk.columns:
            bk_ok = bk[bk['Status'] == 'Successful']
        else:
            bk_ok = bk
        bk_ok = bk_ok.copy()
        bk_ok['AccountId'] = pd.to_numeric(bk_ok['AccountId'], errors='coerce').astype('Int64')

        # Normalise TransactionDate so the cutoff comparison works regardless
        # of whether upstream loaded it as tz-aware or tz-naive. Upstream
        # behaviour drifts depending on the loader path (the v1 revenue
        # analysis can strip the tz before this helper runs); strip it here
        # too so we own a consistent reference point.
        tx_dates = pd.to_datetime(bk_ok['TransactionDate'], errors='coerce')
        if getattr(tx_dates.dt, 'tz', None) is not None:
            tx_dates = tx_dates.dt.tz_convert(None)
        bk_ok = bk_ok.assign(TransactionDate=tx_dates)

        last_sale = bk_ok.groupby('AccountId')['TransactionDate'].max().to_dict()

        cutoff_365 = pd.Timestamp.now('UTC').tz_localize(None).normalize() - pd.Timedelta(days=365)
        bk_recent = bk_ok[bk_ok['TransactionDate'] >= cutoff_365]
        if 'TicketQuantity' in bk_recent.columns:
            tickets_365 = bk_recent.groupby('AccountId')['TicketQuantity'].sum().to_dict()
        else:
            tickets_365 = {}
    else:
        last_sale = {}
        tickets_365 = {}

    # Pull years_loyalty (capped at 5) per account from v2_df if available.
    years_loyalty_lookup = {}
    if v2_df is not None and not v2_df.empty and "Years_Loyalty" in v2_df.columns:
        years_loyalty_lookup = dict(zip(
            v2_df["AccountId"].astype(int),
            v2_df["Years_Loyalty"].fillna(0).astype(int),
        ))

    out = {}
    for aid in account_ids:
        meta = account_lookup.get(aid, {}) if account_lookup else {}
        ranks = (revenue_ranks or {}).get(int(aid), {})
        out[int(aid)] = {
            "account_name": meta.get("AccountName"),
            "industry": meta.get("Industry"),
            "sub_industry": meta.get("SubIndustry"),
            "last_ticket_sale": last_sale.get(aid) or last_sale.get(int(aid)),
            "last_event_created": meta.get("LastEventCreation"),
            "tickets_365d": tickets_365.get(aid) or tickets_365.get(int(aid)) or 0,
            "account_created": meta.get("DateTimeCreated"),
            "years_loyalty": years_loyalty_lookup.get(int(aid)),
            "rank_current": ranks.get("rank_current"),
            "rank_current_prev": ranks.get("rank_current_prev"),
            "rank_lifetime": ranks.get("rank_lifetime"),
            "rank_lifetime_prev": ranks.get("rank_lifetime_prev"),
        }
    return out


def _run_tier_movement_pipeline(account_metrics, account_lookup, booking_data_df,
                                zoho_token, dry_run: bool = False,
                                meta_cutoff_365_iso: str = None):
    """Run the v2 calculator, detect changes, send emails, update SharePoint state.

    Self-contained — failure here does not bubble up into the main run.
    Assumes Zoho upsert succeeded; safe to call after.

    `dry_run`: if True, skip all SharePoint writes (history/snapshot remain
    untouched). Useful for local validation without affecting production state.

    `meta_cutoff_365_iso`: when set (warehouse path), per-account last-sale and
    365-day ticket maps are queried from the warehouse for the email-relevant
    accounts instead of scanning booking_data_df (which is then only a 90-day
    frame). The value is the UTC ISO cutoff for the 365-day ticket sum.
    """
    if not SHAREPOINT_DRIVE_ID:
        logger.warning("SHAREPOINT_DRIVE_ID not set — skipping tier-movement pipeline.")
        return

    graph_token = authenticate_graph()
    if not graph_token:
        logger.warning("Graph auth failed — skipping tier-movement pipeline.")
        return

    logger.info("Running v2 composite tier calculation for snapshot/email pipeline...")
    v2_df = calculate_composite_tiers(account_metrics)
    if v2_df.empty:
        logger.warning("v2 calculator returned empty result — nothing to snapshot.")
        return

    # Attach AccountName for nicer email rendering and snapshot storage
    name_lookup = {
        int(aid): meta.get("AccountName")
        for aid, meta in (account_lookup or {}).items()
        if meta.get("AccountName")
    }
    v2_df = v2_df.copy()
    v2_df["Account_Name"] = v2_df["AccountId"].astype(int).map(name_lookup)

    previous_snapshot = tier_snapshot.load_previous_snapshot(graph_token, SHAREPOINT_DRIVE_ID)
    is_first_run = not previous_snapshot
    changes = tier_snapshot.detect_changes(previous_snapshot, v2_df)
    relevant = tier_snapshot.filter_email_relevant_moves(changes)
    logger.info("Tier movements: %d total, %d email-relevant (T1/T2-touching).",
                len(changes), len(relevant))

    # Load history early — needed both for cooldown suppression (below) and
    # for email chart rendering / TEST_MODE replay (further down).
    history = tier_history.load_history(graph_token, SHAREPOINT_DRIVE_ID)
    today = datetime.now(UK_TZ).date()

    # Cooldown suppression: mute boundary flip-flops that already moved owned
    # tier within the cooldown window, while always letting sustained climbs
    # into new-ground tiers through. Inferred statelessly from tier history.
    # Skipped on first run (no baseline; handled by the guard below anyway).
    if not is_first_run and not relevant.empty:
        relevant = tier_snapshot.suppress_repetitive_moves(relevant, history, today)

    # First-run guard: with no baseline snapshot, every account looks "new",
    # so every T1/T2 account would generate a new-direction email. That's
    # noise, not signal — suppress sends and let tomorrow's diff be the
    # first real one. The snapshot is still saved at the end so tomorrow
    # has a baseline. TEST_MODE bypasses this so we can still preview a
    # historical movement on day zero.
    if is_first_run and not TEST_MODE and not relevant.empty:
        logger.info("First run (no previous snapshot) — suppressing %d new-direction "
                    "emails. Snapshot will be saved as the baseline; real movement "
                    "detection starts from the next run.", len(relevant))
        relevant = relevant.iloc[0:0]

    tier_history.append_day(history, today, v2_df)

    # TEST_MODE preview fallback: if there are no real T1/T2-touching moves
    # today, surface the most recent historical one so a TEST_MODE run still
    # produces a representative email. Real production runs (TEST_MODE=false)
    # remain quiet on no-movement days.
    if relevant.empty and TEST_MODE:
        # Replay the latest real-world day with owned-tier movement. Walks
        # tier_history.json backwards day-by-day until it finds one where
        # at least one account moved into or out of a Tier 1 / Tier 2
        # state, then surfaces every such move on that day. Gives a
        # production-realistic preview (one or many emails) rather than
        # synthesised controlled examples.
        day_iso, samples = tier_history.find_latest_day_with_relevant_moves(
            history, owned_tiers=TIER_OWNERS.keys()
        )
        if samples:
            logger.info("TEST_MODE: no real movement today; replaying %d "
                        "real moves from %s.", len(samples), day_iso)
            rows = []
            for sample in samples:
                # Carry the account name from v2 data if we still have it
                name_match = v2_df.loc[v2_df["AccountId"] == sample["AccountId"], "Account_Name"]
                if not name_match.empty and pd.notna(name_match.iloc[0]):
                    sample["Account_Name"] = name_match.iloc[0]
                rows.append({
                    "AccountId": sample["AccountId"],
                    "Account_Name": sample["Account_Name"],
                    "previous_tier": sample["previous_tier"],
                    "current_tier": sample["current_tier"],
                    "direction": sample["direction"],
                })
            relevant = pd.DataFrame(rows)
        else:
            logger.info("TEST_MODE: no real movement today and no historical "
                        "T1/T2 movements found in history file.")

    if not relevant.empty:
        revenue_ranks = _build_revenue_ranks(v2_df)
        relevant_ids = relevant["AccountId"].tolist()
        last_sale_override = tickets_365_override = None
        if meta_cutoff_365_iso is not None:
            # Warehouse path: query last-sale + 365d tickets for just these
            # movers rather than scanning a frame.
            from modules import warehouse
            conn = warehouse.connect()
            try:
                last_sale_override, tickets_365_override = (
                    warehouse.account_last_sale_and_tickets(
                        conn, meta_cutoff_365_iso, account_ids=relevant_ids)
                )
            finally:
                conn.close()
        account_meta = _build_account_meta_lookup(
            booking_data_df, account_lookup, relevant_ids,
            v2_df=v2_df, revenue_ranks=revenue_ranks,
            last_sale_override=last_sale_override,
            tickets_365_override=tickets_365_override,
        )
        zoho_urls = lookup_account_urls(
            zoho_token, ZOHO_ORG_ID, relevant["AccountId"].tolist()
        ) if ZOHO_ORG_ID else {}

        sent, failed = tier_movement_email.send_movement_emails(
            relevant, history, account_meta, zoho_urls
        )
        logger.info("Tier-movement emails: %d sent, %d failed.", sent, failed)

    # Persist state — skipped only when --dry-run is passed. TEST_MODE alone
    # still writes (it only redirects emails); dry-run is the explicit opt-out.
    if dry_run:
        logger.info("--dry-run: skipping SharePoint writes "
                    "(tier_history.json, tier_snapshot.json untouched).")
    else:
        tier_history.save_history(graph_token, SHAREPOINT_DRIVE_ID, history)
        tier_snapshot.save_snapshot(graph_token, SHAREPOINT_DRIVE_ID, v2_df)
        logger.info("Tier history and snapshot saved.")


def main(dry_run: bool = False, use_combined: bool = False):
    """Main execution function.

    `use_combined`: when True, load the full combined booking pickle via
    load_combined_booking_data (the legacy path, kept as the validation
    reference — needs enough RAM to hold the whole frame, so it OOMs the 4 GB
    Pi). Default False reads the SQLite warehouse in a memory-bounded way:
    the aggregator streams from it, and the row-hungry consumers
    (industry-revenue, account-meta, rapid-drop) get filtered/grouped queries.
    """
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    logger.info(f"Zoho Tier Update Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"\n=== Zoho Tier Update Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    
    # Determine report date
    # If running on the 1st, use previous month's data
    # Otherwise, use current month's data
    today = pd.Timestamp.now(UK_TZ).normalize()
    if today.day == 1:
        # Use last day of previous month
        report_date = today - pd.Timedelta(days=1)
    else:
        # Use current month
        report_date = today

    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")

    logger.info(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
    print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
    
    try:
        # Initialize S3 client first
        s3_client = get_s3_client()
        
        # S3 keys
        key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
        key_account = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
        
        # Find BookingDataAll file dynamically
        # Check both old and new locations and use the newest file
        from modules.utils.data_loader import find_booking_files_in_month, S3_BUCKET, calculate_previous_month

        # Check new location (previous month's folder)
        prev_year, prev_month = calculate_previous_month(int(year), int(month))
        new_location_files, _ = find_booking_files_in_month(s3_client, S3_BUCKET, prev_year, prev_month)

        # Check old location (current month's folder)
        old_location_files, _ = find_booking_files_in_month(s3_client, S3_BUCKET, int(year), int(month))

        # Combine and sort all BookingDataAll files by name (newest last)
        all_booking_all_files = sorted(new_location_files + old_location_files)

        if all_booking_all_files:
            key_all = all_booking_all_files[-1]  # Use the newest file
            logger.info(f"Found BookingDataAll file: {key_all}")
        else:
            key_all = None
            logger.info(f"No BookingDataAll file found in {prev_year:04d}/{prev_month:02d}/ or {year}/{month}/")
        
        # Load Account report for LastEventCreation data
        print(f"Loading Account report from: {key_account}")
        logger.info(f"Loading Account report from S3: {key_account}")
        account_df = download_s3_file_cached(s3_client, key_account)
        
        # Create lookup dictionary: AccountId -> {LastEventCreation, Industry, SubIndustry, DateTimeCreated, AccountName, AccountStatus, LastLogIn}
        account_lookup = {}
        required_cols = ['Id', 'LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        optional_cols = ['AccountName', 'AccountStatus', 'LastLogIn', 'SubIndustry']

        # Determine which columns to include in lookup
        lookup_cols = ['LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        for col in optional_cols:
            if col in account_df.columns:
                lookup_cols.append(col)
        
        if all(col in account_df.columns for col in required_cols):
            account_lookup = account_df.set_index('Id')[lookup_cols].to_dict('index')
            logger.info(f"Loaded {len(account_lookup):,} accounts with metadata")
            print(f"Loaded {len(account_lookup):,} accounts with LastEventCreation, Industry, Postcode and DateTimeCreated data")
            
            # Check for deleted accounts
            if 'AccountName' in account_df.columns and 'AccountStatus' in account_df.columns:
                deleted_accounts = account_df[
                    (account_df['AccountName'] == 'Account Deleted') & 
                    (account_df['AccountStatus'] == 'Closed')
                ]
                if len(deleted_accounts) > 0:
                    logger.info(f"Found {len(deleted_accounts)} deleted accounts that will be excluded from Zoho upserts")
                    print(f"Found {len(deleted_accounts)} deleted accounts that will be excluded from Zoho upserts")
        else:
            missing_cols = [col for col in required_cols if col not in account_df.columns]
            logger.warning(f"Account report missing columns: {missing_cols}")
            print(f"WARNING: Account report missing columns: {missing_cols}")
        
        # Load booking data for aggregation + the revenue consumers.
        print("\nLoading booking data...")
        logger.info("Starting booking data loading")
        booking_data_df = None      # full/2-year frame (combined path only)
        rapid_drop_df = None        # small ~90-day frame for rapid-drop (both paths)
        industry_metrics_365 = None # per-account 365d aggregate (warehouse path)
        wh_meta = None              # (last_sale, tickets_365) maps (warehouse path)
        try:
            aggregator = BookingAggregator(
                cutoff_365=CUTOFF_365,
                cutoff_730=CUTOFF_730,
                event_freq_cutoff_current=EVENT_FREQ_CUTOFF_CURRENT,
                event_freq_cutoff_previous=EVENT_FREQ_CUTOFF_PREVIOUS
            )

            if use_combined:
                # Reference path: full combined pickle, fed to the aggregator in
                # slices. Holds the whole frame in memory (OOMs a 4 GB Pi) —
                # kept only for validating the warehouse path on a dev box.
                from modules.utils.data_loader import load_combined_booking_data
                print("Loading combined booking data (--combined reference path)...")
                logger.info("Loading combined booking data (--combined)")
                booking_data_df = load_combined_booking_data(report_date)
                logger.info(f"Combined booking data: {len(booking_data_df):,} transactions")

                def df_to_chunks(df, chunk_size=100000):
                    for i in range(0, len(df), chunk_size):
                        yield df.iloc[i:i + chunk_size].copy()
                account_metrics = aggregator.aggregate_bookings(
                    df_to_chunks(booking_data_df, chunk_size=100000)
                )

                # Build the 2-year revenue frame exactly as before (reference).
                revenue_cols = ['AccountId', 'TransactionDate', 'PaymentReceived', 'BookingFee',
                              'CardFee', 'ProcessingFee', 'TicketFee', 'EventId', 'TicketQuantity']
                available_cols = [c for c in revenue_cols if c in booking_data_df.columns]
                booking_data_df = booking_data_df[available_cols].copy()
                if all(c in booking_data_df.columns for c in ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']):
                    booking_data_df['Revenue'] = (booking_data_df['BookingFee'] + booking_data_df['CardFee'] +
                                                  booking_data_df['ProcessingFee'] + booking_data_df['TicketFee'])
                elif 'PaymentReceived' in booking_data_df.columns:
                    booking_data_df['Revenue'] = booking_data_df['PaymentReceived']
                if 'Industry' in account_df.columns and 'SubIndustry' in account_df.columns:
                    aidf = account_df[['Id', 'Industry', 'SubIndustry']].rename(columns={'Id': 'AccountId'}).copy()
                    booking_data_df['AccountId'] = pd.to_numeric(booking_data_df['AccountId'], errors='coerce').astype('Int64')
                    aidf['AccountId'] = pd.to_numeric(aidf['AccountId'], errors='coerce').astype('Int64')
                    booking_data_df = booking_data_df.merge(aidf, on='AccountId', how='left')
                if 'TransactionDate' in booking_data_df.columns:
                    booking_data_df['Year'] = booking_data_df['TransactionDate'].dt.year
                    booking_data_df['Month'] = booking_data_df['TransactionDate'].dt.month
                    two_years_ago = pd.Timestamp.now('UTC') - pd.DateOffset(years=2)
                    booking_data_df = booking_data_df[booking_data_df['TransactionDate'] >= two_years_ago]
                rapid_drop_df = booking_data_df  # rapid-drop reads from the same frame
                logger.info(f"Prepared {len(booking_data_df):,} transactions for revenue analysis")
            else:
                # Warehouse path: stream the aggregator from SQLite, and serve
                # each row-hungry consumer a purpose-built filtered/grouped query
                # — never materialise the full (or 2-year) frame.
                from modules import warehouse
                print("Streaming booking data from warehouse for aggregation...")
                logger.info("Aggregating from warehouse (streamed)")
                conn = warehouse.connect()
                try:
                    agg_cols = ['BookingTransactionId', 'AccountId', 'TicketQuantity',
                                'BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee',
                                'TransactionDate', 'EventId', 'EventDate', 'Status']
                    account_metrics = aggregator.aggregate_bookings(
                        warehouse.iter_bookings(conn, columns=agg_cols, chunk_size=100000)
                    )

                    # 365-day per-account aggregate for the industry report.
                    cut365 = (pd.Timestamp.now('UTC') - pd.Timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')
                    industry_metrics_365 = warehouse.account_metrics_365(conn, cut365)

                    # Small ~90-day frame for rapid-drop (AccountId/TransactionDate/
                    # PaymentReceived only; rapid-drop looks back at most 84 days).
                    cut90 = (pd.Timestamp.now('UTC') - pd.Timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
                    rd_parts = list(warehouse.iter_bookings(
                        conn, columns=['AccountId', 'TransactionDate', 'PaymentReceived'],
                        where="TransactionDate >= ? AND Status = 'Successful'",
                        params=(cut90,), chunk_size=200000,
                    ))
                    rapid_drop_df = (pd.concat(rd_parts, ignore_index=True)
                                     if rd_parts else pd.DataFrame(
                                         columns=['AccountId', 'TransactionDate', 'PaymentReceived']))
                    # Merge industry so process_accounts' `Industry in metrics_df`
                    # path behaves the same (it reads tiers, not booking industry,
                    # but keep the column present for parity).
                    if 'Industry' in account_df.columns:
                        aidf = account_df[['Id', 'Industry']].rename(columns={'Id': 'AccountId'}).copy()
                        rapid_drop_df['AccountId'] = pd.to_numeric(rapid_drop_df['AccountId'], errors='coerce').astype('Int64')
                        aidf['AccountId'] = pd.to_numeric(aidf['AccountId'], errors='coerce').astype('Int64')
                        rapid_drop_df = rapid_drop_df.merge(aidf, on='AccountId', how='left')

                    # Stash a connection-free callable for account-meta later.
                    wh_meta = cut365
                finally:
                    conn.close()
                booking_data_df = rapid_drop_df  # what process_accounts/industry see

            logger.info(f"Total unique accounts found: {len(account_metrics):,}")
            print(f"\nTotal unique accounts found: {len(account_metrics):,}")

        except Exception as e:
            logger.warning(f"Failed to load booking data for revenue analysis: {str(e)}")
            print(f"WARNING: Failed to load booking data for revenue analysis: {str(e)}")
            print("Will proceed with basic revenue calculations only")
            import traceback
            traceback.print_exc()
            booking_data_df = None
        
    except Exception as e:
        logger.error(f"Failed to process S3 files: {str(e)}")
        print(f"ERROR: Failed to process S3 files: {str(e)}")
        import traceback
        traceback.print_exc()
        return
    
    # Process accounts: calculate tiers, event frequencies, and activity ratings
    logger.info("Starting main account processing")
    updates = process_accounts(account_metrics, account_lookup, booking_data_df)

    # Feature-flagged tier system. v1 keeps process_accounts' tier output as-is
    # and sends no Tier_Movement to Zoho. v2 overlays the composite calculator's
    # Tier 1..5/Free/Nil labels plus a Tier_Movement column. Non-tier fields
    # (Rating, Event_Frequency_*, Retention_Priority, …) come from v1 either way.
    if TIER_SYSTEM == "v2":
        logger.info("TIER_SYSTEM=v2 — overlaying composite tiers + Tier_Movement")
        print("\n[TIER_SYSTEM=v2] Overlaying composite tiers + Tier_Movement onto Zoho payload")
        updates = _apply_v2_tiers(updates, account_metrics)
    else:
        logger.info("TIER_SYSTEM=v1 (default) — sending legacy tier taxonomy to Zoho")

    # Save results to CSV for audit. Reports land in REPORTS_DIR (./reports by
    # default, /root/reporting/reports on the Pi) — this replaces the GitHub
    # Actions artifact upload that previously captured them.
    os.makedirs(REPORTS_DIR, exist_ok=True)
    csv_filename = os.path.join(
        REPORTS_DIR,
        f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv",
    )
    # Exclude internal/debugging columns from CSV
    columns_to_exclude = ['rapid_drop_details', 'revenue_details', 'revenue_drop_details']
    csv_columns = [col for col in updates.columns if col not in columns_to_exclude]
    updates[csv_columns].to_csv(csv_filename, index=False)
    logger.info(f"Saved tier calculations to: {csv_filename}")
    print(f"\nSaved tier calculations to: {csv_filename}")
    
    # Summary statistics
    tier_counts = updates['Current_Tier'].value_counts()
    print("\nTier Distribution:")
    for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
        count = tier_counts.get(tier, 0)
        pct = (count / len(updates) * 100) if len(updates) > 0 else 0
        print(f"  {tier}: {count:,} ({pct:.1f}%)")
    
    # Tier changes
    tier_changes = updates[updates['Current_Tier'] != updates['Previous_Tier']]
    print(f"\nTier Changes: {len(tier_changes):,} accounts")
    
    # Show some tier change examples
    if len(tier_changes) > 0:
        print("\nExample tier changes (first 5):")
        for _, row in tier_changes.head(5).iterrows():
            print(f"  Account {row['Account_Name']}: {row['Previous_Tier']} → {row['Current_Tier']}")
    
    # Retention priority statistics (already printed in process_accounts)
    # Show top very high priority accounts
    if 'Retention_Priority' in updates.columns:
        very_high_accounts = updates[updates['Retention_Priority'] == 'Very High']
        if len(very_high_accounts) > 0:
            print(f"\nTop Very High Priority Accounts (showing first 5 of {len(very_high_accounts)}):")
            top_very_high = very_high_accounts.nlargest(5, '_retention_priority_score')
            for _, row in top_very_high.iterrows():
                print(f"  Account {row['Account_Name']}: {row['Current_Tier']}, {row['Rating']}, Score: {row['_retention_priority_score']}")
    
    # Generate annual events report
    print("\n=== Annual Events Report ===")
    # First show how many annual accounts we have
    annual_count = len(updates[updates['Event_Frequency_Current'] == 'Annual'])
    annual_prev_count = len(updates[updates['Event_Frequency_Previous'] == 'Annual'])
    print(f"Annual accounts (current): {annual_count}")
    print(f"Annual accounts (previous): {annual_prev_count}")
    
    # Show tier filter impact
    tier_3_plus = ['Key Account', 'High Value', 'Tier 4', 'Tier 3']
    annual_tier_3_plus = len(updates[
        ((updates['Event_Frequency_Current'] == 'Annual') | 
         (updates['Event_Frequency_Previous'] == 'Annual')) &
        (updates['Current_Tier'].isin(tier_3_plus))
    ])
    print(f"Annual accounts that are Tier 3+: {annual_tier_3_plus}")
    
    annual_report = generate_upcoming_annual_events_report(updates)
    if not annual_report.empty:
        report_filename = os.path.join(
            REPORTS_DIR,
            f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv",
        )
        annual_report.to_csv(report_filename, index=False)
        print(f"Upcoming annual events needing outreach: {len(annual_report)}")
        
        try:
            # Check if email credentials are configured
            from modules.utils.config import AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX
            if all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX]):
                email_upcoming_events_report(annual_report, report_filename)
                print(f"📧 Emailed upcoming annual events report")
            else:
                print("Email credentials not configured - skipping email")
        except Exception as e:
            logger.warning(f"Failed to email annual events report: {str(e)}")
            print(f"WARNING: Failed to email annual events report: {str(e)}")
    else:
        logger.info("No upcoming annual events requiring outreach")
        print("No upcoming annual events requiring outreach in next 30 days")
    
    # Send tier updates email report
    try:
        # Check if email credentials are configured
        from modules.utils.config import AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX
        if all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX]):
            email_tier_updates_report(updates, csv_filename)
            print(f"📧 Emailed tier updates report with retention priorities")
        else:
            print("Email credentials not configured - skipping tier updates email")
    except Exception as e:
        logger.warning(f"Failed to email tier updates report: {str(e)}")
        print(f"WARNING: Failed to email tier updates report: {str(e)}")
    
    # Generate industry revenue reports. Warehouse path passes the pre-computed
    # 365-day per-account aggregate (industry_metrics_365); combined path passes
    # the booking frame and lets the function aggregate it.
    print("\n=== Industry Revenue Reports ===")
    try:
        have_industry_source = industry_metrics_365 is not None or (
            booking_data_df is not None and not booking_data_df.empty)
        if have_industry_source:
            logger.info("Generating industry revenue reports")
            print("Generating industry revenue reports...")

            # Generate the reports and save as CSVs
            from modules.industry_revenue_report import generate_industry_revenue_csv_files
            csv_files = generate_industry_revenue_csv_files(
                booking_data_df,
                account_df,
                updates,
                report_date,
                reports_dir=REPORTS_DIR,
                account_metrics_365=industry_metrics_365,
            )
            
            logger.info(f"Generated {len(csv_files)} industry revenue CSV files")
            print(f"✓ Generated {len(csv_files)} industry revenue CSV files")
            
            # List the generated files
            for csv_file in csv_files[:5]:  # Show first 5
                print(f"  - {csv_file}")
            if len(csv_files) > 5:
                print(f"  ... and {len(csv_files) - 5} more")
        else:
            logger.warning("Booking data not available for industry revenue reports")
            print("WARNING: Booking data not available for industry revenue reports")
    except Exception as e:
        logger.error(f"Failed to generate industry revenue reports: {str(e)}")
        print(f"ERROR: Failed to generate industry revenue reports: {str(e)}")
        import traceback
        traceback.print_exc()
    
    # Clean up hidden fields before Zoho upload, but keep retention priority score
    # First, create zoho_updates as a copy to avoid modifying the original
    zoho_updates = updates.copy()
    
    # Note: Deleted account handling has been moved to zoho_industry.py
    # where it makes more sense as part of account-level data sync
    
    # Rename _retention_priority_score to Retention_Priority_Score for Zoho
    if '_retention_priority_score' in zoho_updates.columns:
        zoho_updates['Retention_Priority_Score'] = zoho_updates['_retention_priority_score']
        print(f"✓ Added Retention_Priority_Score to Zoho updates (sample values: {zoho_updates['Retention_Priority_Score'].head(3).tolist()})")
    
    # Remove hidden columns after adding the renamed column
    hidden_cols = [col for col in zoho_updates.columns if col.startswith('_')]
    zoho_updates = zoho_updates.drop(columns=hidden_cols, errors='ignore')
    logger.info(f"Removing {len(hidden_cols)} hidden columns before Zoho upload")
    print(f"\nRemoving {len(hidden_cols)} hidden columns before Zoho upload")
    
    # Log the columns being sent to Zoho
    zoho_columns = list(zoho_updates.columns)
    logger.info(f"Columns being sent to Zoho: {zoho_columns}")
    if 'Retention_Priority_Score' in zoho_columns:
        print("✓ Retention_Priority_Score will be sent to Zoho")
    
    zoho_token = None
    if not zoho_updates.empty:
        # Get Zoho token and update
        try:
            print("\nAuthenticating with Zoho...")
            logger.info("Authenticating with Zoho API")
            zoho_token = get_access_token()

            print("Updating Zoho CRM...")
            logger.info(f"Updating {len(zoho_updates):,} records in Zoho CRM")
            upsert_to_zoho(zoho_token, zoho_updates)

        except Exception as e:
            logger.error(f"Zoho update failed: {str(e)}")
            print(f"ERROR: Zoho update failed: {str(e)}")
            import traceback
            traceback.print_exc()
    else:
        logger.info("No updates required")
        print("No updates required.")

    # Tier-movement detection + per-account emails (v2 schema). Runs only after
    # the Zoho upsert path completes; isolated in its own try-except so SharePoint
    # or email failures don't take down the rest of the run.
    try:
        if zoho_token is None:
            zoho_token = get_access_token()
        _run_tier_movement_pipeline(account_metrics, account_lookup, booking_data_df,
                                    zoho_token, dry_run=dry_run,
                                    meta_cutoff_365_iso=wh_meta)
    except Exception as e:
        logger.error(f"Tier-movement pipeline failed: {e}")
        print(f"ERROR: Tier-movement pipeline failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Performance stats
    elapsed_time = time.time() - start_time
    logger.info(f"Zoho Tier Update completed in {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")
    print(f"\n=== Completed at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Total execution time: {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")


def _compute_one_day(target_date_iso: str, bookings: pd.DataFrame):
    """Worker for the parallel replay. Pure function over a per-day cutoff.

    Returns (target_date_iso, v2_df) or (target_date_iso, None) if the slice
    has no usable data.
    """
    from datetime import date as _date, timedelta
    target_date = _date.fromisoformat(target_date_iso)
    target_ts = pd.Timestamp(target_date).tz_localize('UTC')
    cutoff_365 = target_date - timedelta(days=365)
    cutoff_730 = cutoff_365 - timedelta(days=365)
    freq_current = target_date.replace(day=1) - timedelta(days=365)
    freq_previous = freq_current - timedelta(days=365)

    bk_slice = bookings[bookings['TransactionDate'] <= target_ts]
    if bk_slice.empty:
        return target_date_iso, None

    aggregator = BookingAggregator(
        cutoff_365=cutoff_365,
        cutoff_730=cutoff_730,
        event_freq_cutoff_current=freq_current,
        event_freq_cutoff_previous=freq_previous,
        skip_event_metrics=True,  # v2 calculator doesn't use them
    )
    aggregator.process_chunk(bk_slice)
    metrics = aggregator.finalize_metrics()
    if not metrics:
        return target_date_iso, None

    v2_df = calculate_composite_tiers(metrics)
    if v2_df.empty:
        return target_date_iso, None
    return target_date_iso, v2_df


# Replay tunables. Threads is conservative — pandas releases the GIL on
# heavy ops so 4-8 threads can saturate an M-series machine without the
# memory blowup that a process pool incurs. Checkpoint every ~year of
# replay so an interrupted run loses at most that much work.
_REPLAY_THREAD_WORKERS = 6
_REPLAY_CHECKPOINT_EVERY = 365


def _replay_history(start_date: pd.Timestamp, end_date: pd.Timestamp,
                    dry_run: bool = False, resume: bool = True) -> None:
    """One-off rebuild of the columnar tier_history.json file.

    Replay strategy: load the all-time booking dataset once (using the existing
    fallback walk-back if BookingDataAll is empty), then for each target date
    in [start_date, end_date] filter the bookings to TransactionDate <= target,
    re-run the BookingAggregator with target-relative cutoffs, and feed the
    aggregator output into the v2 calculator. Each day's tier results become
    one column in the history file.

    Skips Zoho/email entirely. Writes only tier_history.json — unless
    `dry_run` is True, in which case the rebuilt file is computed but not
    uploaded.

    Resumability: if a history file already exists on SharePoint and `resume`
    is True (default), the replay starts from the day after the last column
    already in the file. Periodic checkpoint uploads happen every
    _REPLAY_CHECKPOINT_EVERY days so an interrupted run loses at most that
    many days of work. Pass resume=False (or use --rebuild-from-scratch) to
    discard any existing file and start fresh.

    Parallelism: per-day computation runs in a ThreadPoolExecutor. Pandas
    releases the GIL on aggregation, so threads scale on multi-core machines
    without the pickle/memory cost of a process pool. Results are appended
    to the history dict sequentially in date order — the executor preserves
    order via .map().
    """
    from concurrent.futures import ThreadPoolExecutor
    from modules.utils.data_loader import load_booking_data

    if not SHAREPOINT_DRIVE_ID:
        logger.error("SHAREPOINT_DRIVE_ID not set — cannot upload tier history.")
        return
    graph_token = authenticate_graph()
    if not graph_token:
        logger.error("Graph auth failed — cannot upload tier history.")
        return

    # Normalise the bounding timestamps to tz-naive — the replay treats
    # these as calendar markers, and reconstructed checkpoints come back as
    # tz-naive iso strings, so mixing the two raises TypeError on comparison.
    if start_date.tzinfo is not None:
        start_date = start_date.tz_localize(None)
    if end_date.tzinfo is not None:
        end_date = end_date.tz_localize(None)

    logger.info("Loading all-time booking data (one-shot, cached)...")
    bookings = load_booking_data(target_date=end_date.to_pydatetime(),
                                 data_type='BookingDataAll')
    if bookings is None or bookings.empty:
        logger.error("No booking data loaded — cannot rebuild history.")
        return

    if 'TransactionDate' not in bookings.columns:
        logger.error("BookingDataAll missing TransactionDate column.")
        return
    bookings = bookings.copy()
    bookings['TransactionDate'] = pd.to_datetime(bookings['TransactionDate'], errors='coerce', utc=True)
    bookings = bookings.dropna(subset=['TransactionDate'])
    logger.info("Loaded %d transactions, range %s to %s",
                len(bookings),
                bookings['TransactionDate'].min().date(),
                bookings['TransactionDate'].max().date())

    # Resume from existing checkpoint if present. The HistoryBuilder accepts
    # the existing columnar file as a seed; further per-day adds skip the
    # quadratic re-pad cost that the in-place append_day path incurs.
    builder = tier_history.HistoryBuilder()
    effective_start = start_date
    if resume:
        existing = tier_history.load_history(graph_token, SHAREPOINT_DRIVE_ID)
        if existing.get("days"):
            builder = tier_history.HistoryBuilder(seed=existing)
            last_day_iso = existing["days"][-1]
            resume_from = pd.Timestamp(last_day_iso) + pd.Timedelta(days=1)
            if resume_from > end_date:
                logger.info("Existing history already covers %s through %s — nothing to do.",
                            existing["days"][0], last_day_iso)
                return
            if resume_from > start_date:
                logger.info("Resuming from existing checkpoint: %d days already in history "
                            "(last = %s). Replaying from %s to %s.",
                            len(existing["days"]), last_day_iso,
                            resume_from.date(), end_date.date())
                effective_start = resume_from

    daterange = pd.date_range(start=effective_start, end=end_date, freq='D')
    if len(daterange) == 0:
        logger.info("Nothing to replay.")
        return

    logger.info("Replaying tier calculation across %d daily cutoffs "
                "(%s → %s) using %d threads, checkpoint every %d days.",
                len(daterange), effective_start.date(), end_date.date(),
                _REPLAY_THREAD_WORKERS, _REPLAY_CHECKPOINT_EVERY)

    target_isos = [d.date().isoformat() for d in daterange]

    days_since_checkpoint = 0
    days_completed = 0
    with ThreadPoolExecutor(max_workers=_REPLAY_THREAD_WORKERS) as executor:
        # executor.map preserves submission order. The builder doesn't care
        # about insertion order (it sorts days at materialisation time), so
        # this is purely so progress logs read chronologically.
        for target_iso, v2_df in executor.map(
            lambda d: _compute_one_day(d, bookings), target_isos
        ):
            days_completed += 1
            if v2_df is None:
                continue
            target_date = pd.Timestamp(target_iso).date()
            builder.add_day(target_date, v2_df)
            days_since_checkpoint += 1

            if days_completed % 30 == 0 or days_completed == len(daterange):
                logger.info("  Progress: %d/%d days (%s) — %d accounts, %d days in builder.",
                            days_completed, len(daterange), target_iso,
                            builder.account_count(), builder.day_count())

            if (not dry_run) and days_since_checkpoint >= _REPLAY_CHECKPOINT_EVERY:
                logger.info("  Checkpoint upload (%d days since last)...",
                            days_since_checkpoint)
                tier_history.save_history(
                    graph_token, SHAREPOINT_DRIVE_ID, builder.to_history_dict()
                )
                days_since_checkpoint = 0

    final_history = builder.to_history_dict()
    if dry_run:
        logger.info("Replay complete. --dry-run: skipping upload "
                    "(%d accounts × %d days computed, not persisted).",
                    len(final_history['accounts']), len(final_history['days']))
    else:
        logger.info("Replay complete. Final upload (%d accounts × %d days)...",
                    len(final_history['accounts']), len(final_history['days']))
        tier_history.save_history(graph_token, SHAREPOINT_DRIVE_ID, final_history)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Daily Zoho tier update + tier-movement pipeline."
    )
    parser.add_argument(
        "--rebuild-history",
        action="store_true",
        help="Replay v2 tier calculation across a date range and rebuild "
             "tier_history.json from scratch. Skips Zoho/email.",
    )
    parser.add_argument(
        "--history-from",
        type=str,
        default=None,
        help="Start date for --rebuild-history (YYYY-MM-DD). Default: 12 years ago.",
    )
    parser.add_argument(
        "--history-to",
        type=str,
        default=None,
        help="End date for --rebuild-history (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline but skip SharePoint writes (tier_history.json, "
             "tier_snapshot.json untouched). Email sending still happens — pair "
             "with TEST_MODE=true to redirect emails to the test recipient.",
    )
    parser.add_argument(
        "--rebuild-from-scratch",
        action="store_true",
        help="With --rebuild-history: discard any existing tier_history.json on "
             "SharePoint and start fresh. Default behaviour resumes from the "
             "last day already in the file.",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Use the legacy combined-pickle booking load instead of the SQLite "
             "warehouse. Holds the full frame in memory (OOMs a 4 GB Pi) — for "
             "validating the warehouse path against the old output on a dev box.",
    )
    args = parser.parse_args()

    if args.rebuild_history:
        end = pd.Timestamp(args.history_to) if args.history_to else pd.Timestamp.now(UK_TZ).normalize()
        start = pd.Timestamp(args.history_from) if args.history_from else end - pd.DateOffset(years=12)
        _replay_history(start, end, dry_run=args.dry_run,
                        resume=not args.rebuild_from_scratch)
    else:
        main(dry_run=args.dry_run, use_combined=args.combined)