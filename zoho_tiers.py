#!/usr/bin/env python3
"""
Main runner for Zoho tier updates.
Calculates account tiers, event frequencies, and activity ratings.
"""
import time
import pandas as pd
import logging
import os
from datetime import datetime

# Disable caching to avoid stale data issues
os.environ['NO_CACHE'] = '1'

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import from our modules
from modules.utils.config import UK_TZ
from modules.utils.data_loader import get_s3_client, load_multiple_booking_files, download_s3_file_cached
from modules.booking_aggregator import BookingAggregator
from modules.utils.config import CUTOFF_365, CUTOFF_730, EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
from modules.account_processor import process_accounts
from modules.utils.zoho_api import get_access_token, upsert_to_zoho
from modules.utils.report_generator import generate_upcoming_annual_events_report, email_upcoming_events_report, email_tier_updates_report
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator
from modules.industry_revenue_report import generate_industry_revenue_reports


def main():
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
        
        # Create lookup dictionary: AccountId -> {LastEventCreation, Industry, DateTimeCreated, AccountName, AccountStatus, LastLogIn}
        account_lookup = {}
        required_cols = ['Id', 'LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        optional_cols = ['AccountName', 'AccountStatus', 'LastLogIn']

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
    
    if not zoho_updates.empty:
        # Get Zoho token and update
        try:
            print("\nAuthenticating with Zoho...")
            logger.info("Authenticating with Zoho API")
            token = get_access_token()
            
            print("Updating Zoho CRM...")
            logger.info(f"Updating {len(zoho_updates):,} records in Zoho CRM")
            upsert_to_zoho(token, zoho_updates)
            
        except Exception as e:
            logger.error(f"Zoho update failed: {str(e)}")
            print(f"ERROR: Zoho update failed: {str(e)}")
            import traceback
            traceback.print_exc()
    else:
        logger.info("No updates required")
        print("No updates required.")
    
    # Performance stats
    elapsed_time = time.time() - start_time
    logger.info(f"Zoho Tier Update completed in {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")
    print(f"\n=== Completed at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Total execution time: {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")


if __name__ == "__main__":
    main()