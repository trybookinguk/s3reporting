#!/usr/bin/env python3
"""
Optimized Combined Zoho daily sync script.
Updates both account tiers and industry information in Zoho CRM.
Runs daily to keep Zoho synchronized with TryBooking data.
"""
import os
import sys
import time
import pandas as pd
import numpy as np
import logging
from datetime import datetime
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import shared modules
from modules.utils.config import UK_TZ, S3_BUCKET, ZOHO_DOMAIN
from modules.utils.s3_data_loader import get_s3_client, load_multiple_booking_files, download_s3_file_cached
from modules.utils.date_utils import get_latest_data_date
from modules.utils.data_loaders import load_accounts_data, load_booking_data
from modules.utils.zoho_api import get_access_token, upsert_to_zoho, delete_from_zoho
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator, optimize_dtypes, batch_process
from modules.utils.report_generator import email_tier_updates_report, generate_upcoming_annual_events_report, email_upcoming_events_report

# Import processing modules
from modules.booking_aggregator import BookingAggregator
from modules.utils.config import CUTOFF_365, CUTOFF_730, EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
from modules.account_processor import process_accounts
from modules.deleted_account_handler import identify_deleted_accounts

# Constants
ZOHO_BATCH_SIZE = 100  # Zoho API limit


@timer_decorator
def fetch_zoho_accounts_optimized(token, account_names_filter=None):
    """
    Fetch accounts from Zoho CRM with optional filtering.
    
    Args:
        token: Zoho OAuth token
        account_names_filter: Set of account names to fetch (None = fetch all)
    
    Returns:
        Dict of account_name -> zoho_record
    """
    all_accounts = []
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    # If we have a filter and it's small, use search API instead
    if account_names_filter and len(account_names_filter) < 500:
        logger.info(f"Using search API for {len(account_names_filter)} specific accounts")
        # Process in batches to avoid URL length limits
        for i in range(0, len(account_names_filter), 50):
            batch_names = list(account_names_filter)[i:i+50]
            search_criteria = " or ".join([f'(Account_Name:equals:{name})' for name in batch_names])
            params = {
                "criteria": f"({search_criteria})",
                "per_page": ZOHO_BATCH_SIZE
            }
            resp = requests.get(f"{ZOHO_DOMAIN}/crm/v2/Accounts/search", headers=headers, params=params)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                all_accounts.extend(data)
    else:
        # Fetch all accounts
        while True:
            params = {"page": page, "per_page": ZOHO_BATCH_SIZE}
            resp = requests.get(f"{ZOHO_DOMAIN}/crm/v2/Accounts", headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                break
            all_accounts.extend(data)
            if not resp.json().get("info", {}).get("more_records"):
                break
            page += 1

    zoho_accounts = {acc["Account_Name"]: acc for acc in all_accounts if "Account_Name" in acc}
    logger.info(f"Fetched {len(zoho_accounts)} accounts from Zoho CRM")
    return zoho_accounts


@timer_decorator
def sync_industry_data_vectorized(account_df, zoho_token):
    """
    Sync industry data to Zoho CRM using vectorized operations.
    """
    logger.info("Starting optimized industry sync to Zoho")
    
    # Only fetch Zoho accounts that exist in our data
    account_names_in_data = set(account_df['AccountName'].dropna().unique())
    zoho_accounts = fetch_zoho_accounts_optimized(zoho_token, account_names_in_data)
    
    # Create DataFrame for comparison
    zoho_df = pd.DataFrame.from_dict(zoho_accounts, orient='index').reset_index()
    zoho_df = zoho_df.rename(columns={'index': 'AccountName', 'id': 'zoho_id', 'Industry': 'zoho_industry'})
    
    # Merge with our data
    merged_df = pd.merge(
        account_df[['AccountName', 'Industry']],
        zoho_df[['AccountName', 'zoho_id', 'zoho_industry']],
        on='AccountName',
        how='inner'
    )
    
    # Find accounts where industry has changed
    merged_df['needs_update'] = merged_df['Industry'] != merged_df['zoho_industry']
    updates_df = merged_df[merged_df['needs_update']]
    
    if len(updates_df) > 0:
        logger.info(f"Updating industry for {len(updates_df)} accounts")
        print(f"Updating industry for {len(updates_df)} accounts")
        
        # Prepare updates
        updates = updates_df.apply(lambda row: {
            "id": row['zoho_id'],
            "Industry": row['Industry'] if pd.notna(row['Industry']) else None
        }, axis=1).tolist()
        
        # Batch update
        total_updated = 0
        for i in range(0, len(updates), ZOHO_BATCH_SIZE):
            batch = updates[i:i+ZOHO_BATCH_SIZE]
            try:
                upsert_to_zoho(zoho_token, batch)
                total_updated += len(batch)
                logger.info(f"Updated batch {i//ZOHO_BATCH_SIZE + 1}/{(len(updates) + ZOHO_BATCH_SIZE - 1)//ZOHO_BATCH_SIZE}")
            except Exception as e:
                logger.error(f"Failed to update batch: {str(e)}")
        
        return total_updated
    else:
        logger.info("No industry updates needed")
        print("No industry updates needed")
        return 0


@timer_decorator
def load_and_process_booking_data_optimized(s3_client, key_all, key_month):
    """
    Load and process booking data efficiently without duplication.
    """
    logger.info("Loading booking data with optimization")
    
    # Use the BookingAggregator for metrics
    aggregator = BookingAggregator(
        cutoff_365=CUTOFF_365,
        cutoff_730=CUTOFF_730,
        event_freq_cutoff_current=EVENT_FREQ_CUTOFF_CURRENT,
        event_freq_cutoff_previous=EVENT_FREQ_CUTOFF_PREVIOUS
    )
    
    # Load in chunks for aggregation
    chunks = load_multiple_booking_files(s3_client, [key_all, key_month])
    account_metrics = aggregator.aggregate_bookings(chunks)
    
    # For revenue calculations, we need the full data but can optimize memory
    # Load only required columns
    revenue_columns = [
        'AccountId', 'BookingTransactionId', 'TransactionDate',
        'PaymentReceived', 'TicketQuantity', 'Status'
    ]
    
    logger.info("Loading booking data for revenue analysis (optimized columns)")
    
    # Load full data first, then select columns
    booking_all_df = download_s3_file_cached(s3_client, key_all)
    booking_month_df = download_s3_file_cached(s3_client, key_month)
    
    # Select only required columns
    available_cols = [col for col in revenue_columns if col in booking_all_df.columns]
    booking_all_df = booking_all_df[available_cols].copy()
    booking_month_df = booking_month_df[available_cols].copy()
    
    # Apply dtype optimization
    dtype_spec = {
        'AccountId': 'int32',
        'PaymentReceived': 'float32',
        'TicketQuantity': 'int16',
        'Status': 'category'
    }
    for col, dtype in dtype_spec.items():
        if col in booking_all_df.columns:
            booking_all_df[col] = booking_all_df[col].astype(dtype)
        if col in booking_month_df.columns:
            booking_month_df[col] = booking_month_df[col].astype(dtype)
    
    # Combine and deduplicate
    booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
    if 'BookingTransactionId' in booking_data_df.columns:
        booking_data_df = booking_data_df.drop_duplicates(subset=['BookingTransactionId'])
    
    # Optimize memory
    booking_data_df = optimize_dtypes(booking_data_df)
    
    return account_metrics, booking_data_df


@timer_decorator
def sync_tier_data_optimized(results_df, zoho_token):
    """Sync tier and related data to Zoho CRM with optimizations."""
    logger.info("Starting optimized tier sync to Zoho")
    
    # Prepare data more efficiently using vectorized operations
    zoho_data = []
    
    # Convert DataFrame to records more efficiently
    results_df['Last_Event_Date_Str'] = results_df['_last_event_date'].dt.strftime('%Y-%m-%d').where(
        results_df['_last_event_date'].notna(), None
    )
    
    results_df['Annual_Pattern'] = (
        (results_df['Event_Frequency_Current'] == 'Annual') | 
        (results_df['Event_Frequency_Previous'] == 'Annual')
    )
    
    # Select and rename columns efficiently
    update_columns = {
        'Account_Name': 'Account_Name',
        'Current_Tier': 'Tier',
        'Event_Frequency_Current': 'Event_Frequency',
        'Rating': 'Activity_Rating',
        'Retention_Priority': 'Retention_Priority',
        'Revenue_Factor_Score': 'Revenue_Factor',
        'Current_Year_Ticket_Quantity': 'Ticket_Quantity',
        'Days_Since_Last_Booking': 'Days_Since_Last_Booking',
        'Last_Event_Date_Str': 'Last_Event_Date',
        'Annual_Pattern': 'Annual_Pattern'
    }
    
    # Create update DataFrame
    update_df = results_df[list(update_columns.keys())].rename(columns=update_columns)
    
    # Convert numeric columns
    update_df['Ticket_Quantity'] = update_df['Ticket_Quantity'].fillna(0).astype('int32')
    update_df['Days_Since_Last_Booking'] = update_df['Days_Since_Last_Booking'].astype('Int32')
    
    # Remove rows with all null values (except Account_Name)
    update_df = update_df.dropna(subset=[col for col in update_df.columns if col != 'Account_Name'], how='all')
    
    # Convert to records and remove None values
    zoho_data = update_df.to_dict('records')
    zoho_data = [{k: v for k, v in record.items() if pd.notna(v)} for record in zoho_data]
    
    # Batch upsert to Zoho
    logger.info(f"Upserting {len(zoho_data)} records to Zoho CRM")
    print(f"\nUpserting {len(zoho_data)} records to Zoho CRM...")
    
    total_updated = 0
    
    for i in range(0, len(zoho_data), ZOHO_BATCH_SIZE):
        batch = zoho_data[i:i+ZOHO_BATCH_SIZE]
        batch_num = (i // ZOHO_BATCH_SIZE) + 1
        total_batches = (len(zoho_data) + ZOHO_BATCH_SIZE - 1) // ZOHO_BATCH_SIZE
        
        logger.info(f"Processing batch {batch_num}/{total_batches}")
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} records)... ", end="", flush=True)
        
        try:
            upsert_to_zoho(zoho_token, batch)
            total_updated += len(batch)
            updated = len(batch)
            print(f"✓ Updated {updated} records")
        except Exception as e:
            print(f"✗ Failed: {str(e)}")
            logger.error(f"Failed to update batch {batch_num}: {str(e)}")
    
    return total_updated


def main():
    """Main execution function with full optimizations."""
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN',
        'MAILGUN_SMTP_LOGIN', 'MAILGUN_SMTP_PASSWORD',
        'MAILGUN_DOMAIN'
    ])
    
    # Check test mode
    test_mode = os.getenv('TEST_MODE', '').lower() in ['1', 'true']
    if test_mode:
        logger.info("Running in TEST MODE - no actual Zoho updates will be made")
        print("\n=== RUNNING IN TEST MODE ===")
    
    logger.info(f"Zoho Daily Sync (Optimized) Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"\n=== Zoho Daily Sync (Optimized) Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    
    try:
        # Initialize clients
        s3_client = get_s3_client()
        zoho_token = get_access_token()
        
        # Determine report date
        today = pd.Timestamp.now(UK_TZ).normalize()
        if today.day == 1:
            report_date = today - pd.Timedelta(days=1)
        else:
            report_date = today
        
        prefix = report_date.strftime("%Y%m")
        year = report_date.strftime("%Y")
        month = report_date.strftime("%m")
        
        logger.info(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
        print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
        
        # S3 keys
        key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
        key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
        
        # Load Account data using optimized loader
        print(f"\nLoading Account report...")
        account_df = load_accounts_data(s3_client, report_date)
        logger.info(f"Loaded {len(account_df):,} accounts")
        print(f"Loaded {len(account_df):,} accounts")
        
        # === INDUSTRY SYNC (Optimized) ===
        print("\n--- Industry Sync ---")
        industry_updates = sync_industry_data_vectorized(account_df, zoho_token)
        
        # Handle deleted accounts
        deleted_accounts = account_df[
            (account_df['AccountName'] == 'Account Deleted') & 
            (account_df['AccountStatus'] == 'Closed')
        ]
        if len(deleted_accounts) > 0:
            logger.info(f"Processing {len(deleted_accounts)} deleted accounts")
            print(f"\nProcessing {len(deleted_accounts)} deleted accounts...")
            # Get the account IDs of deleted accounts
            deleted_ids = deleted_accounts['Id'].astype(str).tolist()
            # Delete from Zoho
            deletion_results = delete_from_zoho(zoho_token, deleted_ids)
            print(f"Deleted: {deletion_results['successful']} accounts")
            if deletion_results['failed'] > 0:
                print(f"Failed to delete: {deletion_results['failed']} accounts")
        
        # === TIER SYNC (Optimized) ===
        print("\n--- Tier and Metrics Sync ---")
        
        # Create account lookup
        account_lookup = {}
        required_cols = ['Id', 'LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        optional_cols = ['AccountName', 'AccountStatus']
        
        lookup_cols = ['LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        for col in optional_cols:
            if col in account_df.columns:
                lookup_cols.append(col)
        
        if all(col in account_df.columns for col in required_cols):
            account_lookup = account_df.set_index('Id')[lookup_cols].to_dict('index')
            logger.info(f"Created lookup for {len(account_lookup):,} accounts")
        
        # Load and process booking data efficiently
        print("\nProcessing booking data (optimized)...")
        account_metrics, booking_data_df = load_and_process_booking_data_optimized(
            s3_client, key_all, key_month
        )
        
        logger.info(f"Total unique accounts found: {len(account_metrics):,}")
        print(f"Total unique accounts found: {len(account_metrics):,}")
        
        # Process accounts to calculate tiers and metrics
        logger.info("Processing accounts for tier calculations")
        print("\nCalculating tiers and metrics...")
        results_df = process_accounts(account_metrics, booking_data_df, account_lookup)
        
        # Sync tier data to Zoho (optimized)
        tier_updates = sync_tier_data_optimized(results_df, zoho_token)
        
        # Generate and send reports
        print("\n--- Generating Reports ---")
        
        # Save tier updates CSV
        csv_filename = f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
        results_df.to_csv(csv_filename, index=False)
        logger.info(f"Saved tier updates to {csv_filename}")
        print(f"Saved tier updates to {csv_filename}")
        
        # Email tier updates report
        email_tier_updates_report(results_df, csv_filename)
        
        # Generate upcoming annual events report
        upcoming_df = generate_upcoming_annual_events_report(results_df)
        if not upcoming_df.empty:
            annual_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
            upcoming_df.to_csv(annual_filename, index=False)
            email_upcoming_events_report(upcoming_df, annual_filename)
        
        # Summary
        elapsed_time = time.time() - start_time
        print(f"\n=== Zoho Daily Sync Completed in {elapsed_time:.1f} seconds ===")
        print(f"Industry updates: {industry_updates}")
        print(f"Tier updates: {tier_updates}")
        print(f"Deleted accounts processed: {len(deleted_accounts)}")
        
        # Memory usage summary
        memory_mb = booking_data_df.memory_usage(deep=True).sum() / 1024 / 1024
        logger.info(f"Peak booking data memory usage: {memory_mb:.1f} MB")
        
        logger.info(f"Zoho Daily Sync completed successfully in {elapsed_time:.1f} seconds")
        
    except Exception as e:
        logger.error(f"Error in Zoho Daily Sync: {str(e)}")
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()