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
        # Convert to list once for efficiency
        filter_list = list(account_names_filter)
        
        # Process in batches to avoid URL length limits
        for i in range(0, len(filter_list), 50):
            batch_names = filter_list[i:i+50]
            # Build criteria more efficiently using join
            criteria_parts = [f'(Account_Name:equals:{name})' for name in batch_names]
            search_criteria = f"({' or '.join(criteria_parts)})"
            
            params = {
                "criteria": search_criteria,
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
    logger.debug(f"Fetched {len(zoho_accounts)} accounts from Zoho CRM")
    return zoho_accounts


@timer_decorator
def sync_industry_data_vectorized(account_df, zoho_token):
    """
    Sync account data (industry, status, dates) to Zoho CRM using vectorized operations.
    Replaces the functionality of zoho_industry.py
    """
    logger.info("Starting account data sync to Zoho")
    
    # Filter out rows without valid IDs upfront
    valid_mask = account_df['Id'].notna()
    valid_df = account_df[valid_mask].copy()
    
    if len(valid_df) == 0:
        logger.info("No valid accounts to sync")
        return 0
    
    # Vectorized data preparation
    # Convert IDs to strings
    valid_df['account_id'] = valid_df['Id'].astype('Int64').astype(str)
    
    # Vectorized string operations
    valid_df['business_name'] = valid_df['AccountName'].fillna('').astype(str).str.strip()
    valid_df.loc[valid_df['business_name'] == '', 'business_name'] = None
    
    valid_df['industry'] = valid_df['Industry'].astype(str).where(valid_df['Industry'].notna(), None)
    valid_df['subindustry'] = valid_df['SubIndustry'].astype(str).where(valid_df['SubIndustry'].notna(), None)
    valid_df['status'] = valid_df['AccountStatus'].astype(str).where(valid_df['AccountStatus'].notna(), None)
    
    # Vectorized date conversions - all at once
    date_columns = {
        'DateTimeCreated': 'created',
        'LastLogIn': 'last_login',
        'FirstEventCreation': 'first_event',
        'LastEventCreation': 'last_event'
    }
    
    # Vectorized date conversions for all columns at once
    existing_date_cols = [col for col in date_columns.keys() if col in valid_df.columns]
    
    if existing_date_cols:
        # Convert all date columns to datetime in one operation
        date_conversions = {}
        for orig_col in existing_date_cols:
            date_conversions[orig_col] = pd.to_datetime(valid_df[orig_col], errors='coerce')
        
        # Apply conversions and format as ISO date strings
        for orig_col, new_col in date_columns.items():
            if orig_col in existing_date_cols:
                valid_df[new_col] = date_conversions[orig_col].dt.date.astype(str)
                valid_df.loc[date_conversions[orig_col].isna(), new_col] = None
    
    # Fetch existing Zoho accounts
    account_ids_in_data = set(valid_df['account_id'].unique())
    zoho_accounts = fetch_zoho_accounts_optimized(zoho_token, account_ids_in_data)
    
    # Create a DataFrame for comparison
    zoho_df = pd.DataFrame([
        {
            'account_id': acc_id,
            'zoho_Business_Name': data.get('Business_Name'),
            'zoho_Industry': data.get('Industry'),
            'zoho_SubIndustry': data.get('SubIndustry'),
            'zoho_Account_Status': data.get('Account_Status'),
            'zoho_DateTimeCreated': data.get('DateTimeCreated'),
            'zoho_Last_Login': data.get('Last_Login'),
            'zoho_First_Event_Creation_Date': data.get('First_Event_Creation_Date'),
            'zoho_Last_Event_Creation_Date': data.get('Last_Event_Creation_Date'),
            'exists_in_zoho': True
        }
        for acc_id, data in zoho_accounts.items()
    ])
    
    # Merge with our data
    if not zoho_df.empty:
        merged = valid_df.merge(zoho_df, on='account_id', how='left')
        merged['exists_in_zoho'] = merged['exists_in_zoho'].fillna(False)
    else:
        merged = valid_df.copy()
        merged['exists_in_zoho'] = False
    
    # Vectorized comparison for changes
    # For new accounts (not in Zoho)
    new_accounts_mask = ~merged['exists_in_zoho']
    
    # For existing accounts, check what changed
    field_mappings = {
        'business_name': 'Business_Name',
        'industry': 'Industry', 
        'subindustry': 'SubIndustry',
        'status': 'Account_Status',
        'created': 'DateTimeCreated',
        'last_login': 'Last_Login',
        'first_event': 'First_Event_Creation_Date',
        'last_event': 'Last_Event_Creation_Date'
    }
    
    # Create change detection columns
    for local_col, zoho_field in field_mappings.items():
        zoho_col = f'zoho_{zoho_field}'
        if zoho_col in merged.columns:
            # Special handling for Industry/SubIndustry - direct comparison
            if zoho_field in ['Industry', 'SubIndustry']:
                merged[f'changed_{local_col}'] = (merged[local_col] != merged[zoho_col])
            else:
                # For other fields, handle string comparison with strip
                merged[f'changed_{local_col}'] = False
                
                # Where both values exist, compare them
                both_exist = merged[local_col].notna() & merged[zoho_col].notna()
                merged.loc[both_exist, f'changed_{local_col}'] = (
                    merged.loc[both_exist, local_col].astype(str).str.strip() != 
                    merged.loc[both_exist, zoho_col].astype(str).str.strip()
                )
                
                # Where new value exists but zoho doesn't
                new_not_zoho = merged[local_col].notna() & merged[zoho_col].isna()
                merged.loc[new_not_zoho, f'changed_{local_col}'] = True
                
                # Where zoho exists but new doesn't (setting to None)
                zoho_not_new = merged[local_col].isna() & merged[zoho_col].notna()
                merged.loc[zoho_not_new, f'changed_{local_col}'] = True
    
    # Determine which accounts need updates
    change_columns = [col for col in merged.columns if col.startswith('changed_')]
    if change_columns:
        merged['has_changes'] = merged[change_columns].any(axis=1)
    else:
        merged['has_changes'] = False
    
    # Build updates list efficiently
    updates_needed = merged[new_accounts_mask | merged['has_changes']]
    
    if len(updates_needed) == 0:
        logger.info("No account data updates needed")
        return 0
    
    # More vectorized update building
    # Split into new vs existing accounts for different processing
    new_accounts = updates_needed[~updates_needed['exists_in_zoho']]
    existing_accounts = updates_needed[updates_needed['exists_in_zoho']]
    
    updates = []
    
    # Process new accounts - include all non-null fields
    if len(new_accounts) > 0:
        new_updates = new_accounts[['account_id'] + list(field_mappings.keys())].copy()
        new_updates = new_updates.rename(columns={'account_id': 'Account_Name', **field_mappings})
        # Convert to dict records, removing NaN values
        new_records = new_updates.to_dict('records')
        updates.extend([{k: v for k, v in record.items() if pd.notna(v)} for record in new_records])
    
    # Process existing accounts - only changed fields
    if len(existing_accounts) > 0:
        for idx, row in existing_accounts.iterrows():
            update = {"Account_Name": row['account_id']}
            # Only add changed fields
            for local_col, zoho_field in field_mappings.items():
                if row.get(f'changed_{local_col}', False):
                    value = row[local_col]
                    update[zoho_field] = value if pd.notna(value) else None
            
            if len(update) > 1:  # More than just Account_Name
                updates.append(update)
    
    
    if len(updates) > 0:
        logger.info(f"Updating {len(updates)} accounts with industry and account data")
        
        # Batch update using the shared upsert function
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
        logger.info("No account data updates needed")
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
    
    # Apply dtype optimization - vectorized
    dtype_spec = {
        'AccountId': 'int32',
        'PaymentReceived': 'float32',
        'TicketQuantity': 'int16',
        'Status': 'category'
    }
    
    # Get columns that exist in each dataframe
    all_df_cols = [col for col in dtype_spec.keys() if col in booking_all_df.columns]
    month_df_cols = [col for col in dtype_spec.keys() if col in booking_month_df.columns]
    
    # Apply dtypes in one operation per dataframe
    if all_df_cols:
        booking_all_df = booking_all_df.astype({col: dtype_spec[col] for col in all_df_cols})
    if month_df_cols:
        booking_month_df = booking_month_df.astype({col: dtype_spec[col] for col in month_df_cols})
    
    # Combine and deduplicate
    booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
    if 'BookingTransactionId' in booking_data_df.columns:
        booking_data_df = booking_data_df.drop_duplicates(subset=['BookingTransactionId'])
    
    # Optimize memory
    booking_data_df = optimize_dtypes(booking_data_df)
    
    # Log memory usage
    memory_mb = booking_data_df.memory_usage(deep=True).sum() / 1024 / 1024
    logger.debug(f"Peak booking data memory usage: {memory_mb:.1f} MB")
    
    return account_metrics, booking_data_df


@timer_decorator
def sync_tier_data_optimized(results_df, zoho_token):
    """Sync tier and related data to Zoho CRM with optimizations."""
    logger.info("Starting optimized tier sync to Zoho")
    
    # Prepare data more efficiently using vectorized operations
    zoho_data = []
    
    # Note: Last_Event_Date_Str, Annual_Pattern, and Retention_Priority_Score 
    # are now created in process_accounts and already exist in results_df
    
    # Log that these fields already exist
    if 'Retention_Priority_Score' in results_df.columns:
        logger.debug(f"Retention_Priority_Score already exists (sample values: {results_df['Retention_Priority_Score'].head(3).tolist()})")
    
    # Select columns for Zoho update - DO NOT rename, keep original field names
    update_columns = {
        'Account_Name': 'Account_Name',
        'Current_Tier': 'Current_Tier',  # Keep original name
        'Previous_Tier': 'Previous_Tier',
        'Event_Frequency_Current': 'Event_Frequency_Current',  # Keep original name
        'Event_Frequency_Previous': 'Event_Frequency_Previous',
        'Rating': 'Rating',  # Keep original name
        'Retention_Priority': 'Retention_Priority',
        'Ticket_Quantity': 'Ticket_Quantity',
        'Last_Year_Ticket_Quantity': 'Last_Year_Ticket_Quantity',
        'Years_Loyalty': 'Years_Loyalty',
        'Days_Since_Last_Booking': 'Days_Since_Last_Booking',
        'Last_Event_Date_Str': 'Last_Event_Date',
        'Annual_Pattern': 'Annual_Pattern',
        'Months_Active': 'Months_Active'
    }
    
    # Add optional columns if they exist
    if 'Retention_Priority_Score' in results_df.columns:
        update_columns['Retention_Priority_Score'] = 'Retention_Priority_Score'
    
    # Create a copy for Zoho updates (don't modify original)
    zoho_updates_df = results_df.copy()
    
    # Remove hidden columns (those starting with underscore)
    hidden_cols = [col for col in zoho_updates_df.columns if col.startswith('_')]
    zoho_updates_df = zoho_updates_df.drop(columns=hidden_cols, errors='ignore')
    logger.debug(f"Removed {len(hidden_cols)} hidden columns for Zoho upload")
    
    # Select only the columns we need for Zoho
    update_df = zoho_updates_df[list(update_columns.keys())].rename(columns=update_columns)
    
    # Convert numeric columns using vectorized operations
    update_df['Ticket_Quantity'] = update_df['Ticket_Quantity'].fillna(0).astype('int32')
    update_df['Days_Since_Last_Booking'] = update_df['Days_Since_Last_Booking'].astype('Int32')
    
    # Remove rows with all null values (except Account_Name) - vectorized
    data_cols = [col for col in update_df.columns if col != 'Account_Name']
    has_data = update_df[data_cols].notna().any(axis=1)
    update_df = update_df[has_data].copy()
    
    # Instead of converting to dict and filtering, use the DataFrame directly
    # The upsert_to_zoho function accepts DataFrames
    
    if len(update_df) == 0:
        logger.info("No tier updates needed")
        return 0
    
    # Batch upsert to Zoho using DataFrame directly
    logger.info(f"Upserting {len(update_df)} records to Zoho CRM")
    print(f"\nUpserting {len(update_df)} records to Zoho CRM...")
    
    total_updated = 0
    
    # Process DataFrame in chunks
    for i in range(0, len(update_df), ZOHO_BATCH_SIZE):
        batch_df = update_df.iloc[i:i+ZOHO_BATCH_SIZE]
        batch_num = (i // ZOHO_BATCH_SIZE) + 1
        total_batches = (len(update_df) + ZOHO_BATCH_SIZE - 1) // ZOHO_BATCH_SIZE
        
        logger.info(f"Processing batch {batch_num}/{total_batches}")
        print(f"  Batch {batch_num}/{total_batches} ({len(batch_df)} records)... ", end="", flush=True)
        
        try:
            upsert_to_zoho(zoho_token, batch_df)
            total_updated += len(batch_df)
            print(f"✓ Updated {len(batch_df)} records")
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
    
    logger.info(f"Zoho Daily Sync (Optimized) Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    
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
        
        # S3 keys
        key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
        key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
        
        # Load Account data using optimized loader
        logger.info("Loading Account report...")
        account_df = load_accounts_data(s3_client, report_date)
        logger.info(f"Loaded {len(account_df):,} accounts")
        
        # Handle deleted accounts first
        deleted_accounts = account_df[
            (account_df['AccountName'] == 'Account Deleted') & 
            (account_df['AccountStatus'] == 'Closed')
        ]
        deleted_ids = []
        if len(deleted_accounts) > 0:
            logger.info(f"Processing {len(deleted_accounts)} deleted accounts")
            # Get the account IDs of deleted accounts
            deleted_ids = deleted_accounts['Id'].astype(str).tolist()
            # Delete from Zoho
            deletion_results = delete_from_zoho(zoho_token, deleted_ids)
            logger.info(f"Deleted: {deletion_results['successful']} accounts")
            if deletion_results['failed'] > 0:
                logger.warning(f"Failed to delete: {deletion_results['failed']} accounts")
        
        # Remove deleted accounts from the dataframe so they won't be upserted
        if deleted_ids:
            account_df = account_df[~account_df['Id'].astype(str).isin(deleted_ids)]
            logger.info(f"Filtered out {len(deleted_ids)} deleted accounts from further processing")
        
        # === INDUSTRY SYNC (Optimized) ===
        logger.info("Starting Industry Sync")
        industry_updates = sync_industry_data_vectorized(account_df, zoho_token)
        
        # === TIER SYNC (Optimized) ===
        logger.info("Starting Tier and Metrics Sync")
        
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
        logger.info("Processing booking data (optimized)...")
        account_metrics, booking_data_df = load_and_process_booking_data_optimized(
            s3_client, key_all, key_month
        )
        
        logger.info(f"Total unique accounts found: {len(account_metrics):,}")
        
        # Process accounts to calculate tiers and metrics
        logger.info("Processing accounts for tier calculations")
        results_df = process_accounts(account_metrics, account_lookup, booking_data_df)
        
        # Save tier updates CSV BEFORE any modifications
        csv_filename = f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
        # Correctly exclude underscore-prefixed detail columns
        columns_to_exclude = ['_rapid_drop_details', '_revenue_drop_details']
        csv_columns = [col for col in results_df.columns if col not in columns_to_exclude]
        
        # Debug: Log columns being saved
        logger.debug(f"Columns in results_df: {list(results_df.columns)}")
        logger.debug(f"Columns being saved to CSV: {csv_columns}")
        
        # Verify Days_Since_Last_Booking is present
        if 'Days_Since_Last_Booking' not in csv_columns:
            logger.error("Days_Since_Last_Booking missing from CSV columns!")
        
        results_df[csv_columns].to_csv(csv_filename, index=False)
        logger.info(f"Saved tier updates to {csv_filename}")
        
        # Sync tier data to Zoho (optimized) - this will modify results_df
        tier_updates = sync_tier_data_optimized(results_df, zoho_token)
        
        # Summary statistics (from original zoho_tiers.py)
        if not results_df.empty:
            tier_counts = results_df['Current_Tier'].value_counts()
            tier_summary = {}
            for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
                count = tier_counts.get(tier, 0)
                pct = (count / len(results_df) * 100) if len(results_df) > 0 else 0
                tier_summary[tier] = f"{count} ({pct:.1f}%)"
            logger.info(f"Tier distribution: {tier_summary}")
            
            # Tier changes
            tier_changes = results_df[results_df['Current_Tier'] != results_df['Previous_Tier']]
            logger.info(f"Tier changes: {len(tier_changes):,} accounts")
            
            # Log tier change summary
            if len(tier_changes) > 0:
                tier_change_summary = tier_changes.groupby(['Previous_Tier', 'Current_Tier']).size().to_dict()
                logger.info(f"Tier change details: {tier_change_summary}")
            
            # Retention priority statistics
            if 'Retention_Priority' in results_df.columns:
                priority_counts = results_df['Retention_Priority'].value_counts().to_dict()
                logger.info(f"Retention priority distribution: {priority_counts}")
            
            # Annual events statistics
            annual_count = len(results_df[results_df['Event_Frequency_Current'] == 'Annual'])
            annual_prev_count = len(results_df[results_df['Event_Frequency_Previous'] == 'Annual'])
            logger.info(f"Annual accounts - current: {annual_count}, previous: {annual_prev_count}")
        
        # Generate and send reports
        logger.info("Generating and sending reports")
        
        # Email tier updates report
        email_tier_updates_report(results_df, csv_filename)
        logger.info("Emailed tier updates report with retention priorities")
        
        # Generate upcoming annual events report
        upcoming_df = generate_upcoming_annual_events_report(results_df)
        if not upcoming_df.empty:
            annual_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
            upcoming_df.to_csv(annual_filename, index=False)
            logger.info(f"Upcoming annual events needing outreach: {len(upcoming_df)}")
            email_upcoming_events_report(upcoming_df, annual_filename)
            logger.info("Emailed upcoming annual events report")
        else:
            logger.info("No upcoming annual events requiring outreach in next 30 days")
        
        # Summary
        elapsed_time = time.time() - start_time
        logger.info(f"Zoho Daily Sync completed successfully in {elapsed_time:.1f} seconds")
        logger.info(f"Summary - Industry updates: {industry_updates}, Tier updates: {tier_updates}, Deleted accounts: {len(deleted_accounts)}")
        
    except Exception as e:
        logger.error(f"Error in Zoho Daily Sync: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()