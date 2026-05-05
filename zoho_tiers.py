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
from modules.utils.config import UK_TZ, TEST_MODE, TIER_OWNERS
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


def _build_account_meta_lookup(booking_data_df, account_lookup, account_ids):
    """Build per-account metadata for the tier-movement emails.

    Returns a dict keyed by AccountId with: account_name, industry, sub_industry,
    last_ticket_sale, last_event_created, tickets_365d.
    """
    bk = booking_data_df
    if bk is not None and not bk.empty:
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

    out = {}
    for aid in account_ids:
        meta = account_lookup.get(aid, {}) if account_lookup else {}
        out[int(aid)] = {
            "account_name": meta.get("AccountName"),
            "industry": meta.get("Industry"),
            "sub_industry": meta.get("SubIndustry"),
            "last_ticket_sale": last_sale.get(aid) or last_sale.get(int(aid)),
            "last_event_created": meta.get("LastEventCreation"),
            "tickets_365d": tickets_365.get(aid) or tickets_365.get(int(aid)) or 0,
        }
    return out


def _run_tier_movement_pipeline(account_metrics, account_lookup, booking_data_df,
                                zoho_token, dry_run: bool = False):
    """Run the v2 calculator, detect changes, send emails, update SharePoint state.

    Self-contained — failure here does not bubble up into the main run.
    Assumes Zoho upsert succeeded; safe to call after.

    `dry_run`: if True, skip all SharePoint writes (history/snapshot remain
    untouched). Useful for local validation without affecting production state.
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

    history = tier_history.load_history(graph_token, SHAREPOINT_DRIVE_ID)
    today = datetime.now(UK_TZ).date()
    tier_history.append_day(history, today, v2_df)

    # TEST_MODE preview fallback: if there are no real T1/T2-touching moves
    # today, surface the most recent historical one so a TEST_MODE run still
    # produces a representative email. Real production runs (TEST_MODE=false)
    # remain quiet on no-movement days.
    if relevant.empty and TEST_MODE:
        # Sampler: one of each owner-relevant transition shape. For drops,
        # we key on the *previous* tier (the owned band the account left) —
        # current_tier varies (T3, T4, T5...) and isn't the interesting
        # piece for a preview email.
        sample_kinds = [
            {"direction": "up",   "current_tier":  "Tier 1"},  # promotion to top
            {"direction": "up",   "current_tier":  "Tier 2"},  # promotion into T2
            {"direction": "down", "previous_tier": "Tier 1"},  # dropped out of T1
            {"direction": "down", "previous_tier": "Tier 2"},  # dropped out of T2
        ]
        samples = tier_history.find_sample_moves_per_kind(history, sample_kinds)
        if samples:
            logger.info("TEST_MODE: no real movement today; previewing %d "
                        "sample historical moves (%s).",
                        len(samples),
                        ", ".join(f"{s['previous_tier']}->{s['current_tier']}" for s in samples))
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
        account_meta = _build_account_meta_lookup(
            booking_data_df, account_lookup, relevant["AccountId"].tolist()
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


def main(dry_run: bool = False):
    """Main execution function."""
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
        
        # Load booking data once for both aggregation and revenue analysis
        print("\nLoading booking data...")
        logger.info("Starting booking data loading")
        booking_data_df = None
        try:
            # Load both BookingDataAll and current month BookingData
            from modules.utils.data_loader import load_booking_data

            print("Loading BookingDataAll...")
            logger.info("Loading BookingDataAll")
            booking_all_df = load_booking_data(s3_client, report_date, data_type='BookingDataAll')

            print("Loading current month BookingData...")
            booking_month_df = load_booking_data(s3_client, report_date, data_type='BookingData')

            # Combine and remove duplicates based on BookingTransactionId
            print("Combining booking data and removing duplicates...")
            logger.info("Combining booking data files")
            booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
            initial_count = len(booking_data_df)
            booking_data_df = booking_data_df.drop_duplicates(subset='BookingTransactionId')
            duplicates_removed = initial_count - len(booking_data_df)
            logger.info(f"Removed {duplicates_removed:,} duplicate transactions, {len(booking_data_df):,} remaining")
            print(f"Removed {duplicates_removed:,} duplicate transactions")

            # Free memory from separate DataFrames
            del booking_all_df, booking_month_df

            # Process data using optimized chunked approach for aggregation
            logger.info("Starting account metrics aggregation")
            print("\nProcessing booking data for account metrics...")
            aggregator = BookingAggregator(
                cutoff_365=CUTOFF_365,
                cutoff_730=CUTOFF_730,
                event_freq_cutoff_current=EVENT_FREQ_CUTOFF_CURRENT,
                event_freq_cutoff_previous=EVENT_FREQ_CUTOFF_PREVIOUS
            )

            # Convert DataFrame to chunks for aggregator (reuse loaded data)
            def df_to_chunks(df, chunk_size=100000):
                """Convert DataFrame to chunks iterator"""
                for i in range(0, len(df), chunk_size):
                    yield df.iloc[i:i + chunk_size].copy()

            chunks = df_to_chunks(booking_data_df, chunk_size=100000)
            account_metrics = aggregator.aggregate_bookings(chunks)

            logger.info(f"Total unique accounts found: {len(account_metrics):,}")
            print(f"\nTotal unique accounts found: {len(account_metrics):,}")

            # Prepare booking data for revenue analysis
            print("\nPreparing booking data for revenue analysis...")
            logger.info("Preparing booking data for revenue analysis")

            # Create a copy for revenue analysis (to preserve original data)
            # Only keep necessary columns for revenue analysis to save memory
            revenue_cols = ['AccountId', 'TransactionDate', 'PaymentReceived', 'BookingFee',
                          'CardFee', 'ProcessingFee', 'TicketFee', 'EventId', 'TicketQuantity']
            # Keep only columns that exist in the dataframe
            available_cols = [col for col in revenue_cols if col in booking_data_df.columns]
            booking_data_df = booking_data_df[available_cols].copy()
            
            # Calculate total revenue if component columns exist
            if all(col in booking_data_df.columns for col in ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']):
                booking_data_df['Revenue'] = (booking_data_df['BookingFee'] + 
                                             booking_data_df['CardFee'] + 
                                             booking_data_df['ProcessingFee'] + 
                                             booking_data_df['TicketFee'])
            elif 'PaymentReceived' in booking_data_df.columns:
                # Fallback to PaymentReceived if fee columns not available
                booking_data_df['Revenue'] = booking_data_df['PaymentReceived']
            
            # TransactionDate is already in UTC datetime format from load_booking_data
            # No need to convert again
            
            # Merge industry information from Accounts data
            print("Merging industry information...")
            if 'Industry' in account_df.columns and 'SubIndustry' in account_df.columns:
                # Prepare account data for merge
                account_industry_df = account_df[['Id', 'Industry', 'SubIndustry']].copy()
                account_industry_df.rename(columns={'Id': 'AccountId'}, inplace=True)
                
                # Convert AccountId to string for consistent merging
                booking_data_df['AccountId'] = booking_data_df['AccountId'].astype(str)
                account_industry_df['AccountId'] = account_industry_df['AccountId'].astype(str)
                
                # Merge industry data
                booking_data_df = booking_data_df.merge(
                    account_industry_df,
                    on='AccountId',
                    how='left'
                )
                
                # Log missing industry data
                missing_industry = booking_data_df['Industry'].isna().sum()
                if missing_industry > 0:
                    print(f"WARNING: {missing_industry:,} transactions missing industry data")
            
            # Add year and month columns from TransactionDate for performance
            if 'TransactionDate' in booking_data_df.columns:
                booking_data_df['Year'] = booking_data_df['TransactionDate'].dt.year
                booking_data_df['Month'] = booking_data_df['TransactionDate'].dt.month
                
                # Filter to last 2 years of data for performance
                two_years_ago = pd.Timestamp.now('UTC') - pd.DateOffset(years=2)
                original_count = len(booking_data_df)
                booking_data_df = booking_data_df[booking_data_df['TransactionDate'] >= two_years_ago]
                removed_count = original_count - len(booking_data_df)
                logger.info(f"Filtered to last 2 years: {len(booking_data_df):,} transactions (removed {removed_count:,})")
                print(f"Filtered to last 2 years: {len(booking_data_df):,} transactions (removed {removed_count:,})")
            
            logger.info(f"Prepared {len(booking_data_df):,} transactions for revenue analysis")
            print(f"Prepared booking data: {len(booking_data_df):,} transactions for revenue analysis")
            
        except Exception as e:
            logger.warning(f"Failed to load full booking data for revenue analysis: {str(e)}")
            print(f"WARNING: Failed to load full booking data for revenue analysis: {str(e)}")
            print("Will proceed with basic revenue calculations only")
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
    
    # Save results to CSV for audit
    csv_filename = f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
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
        report_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
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
    
    # Generate industry revenue reports
    print("\n=== Industry Revenue Reports ===")
    try:
        if booking_data_df is not None and not booking_data_df.empty:
            logger.info("Generating industry revenue reports")
            print("Generating industry revenue reports...")
            
            # Generate the reports and save as CSVs
            from modules.industry_revenue_report import generate_industry_revenue_csv_files
            csv_files = generate_industry_revenue_csv_files(
                booking_data_df, 
                account_df, 
                updates,
                report_date
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
                                    zoho_token, dry_run=dry_run)
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
        skip_event_metrics=True,  # v2 calculator doesn't use them — large speedup
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
    args = parser.parse_args()

    if args.rebuild_history:
        end = pd.Timestamp(args.history_to) if args.history_to else pd.Timestamp.now(UK_TZ).normalize()
        start = pd.Timestamp(args.history_from) if args.history_from else end - pd.DateOffset(years=12)
        _replay_history(start, end, dry_run=args.dry_run,
                        resume=not args.rebuild_from_scratch)
    else:
        main(dry_run=args.dry_run)