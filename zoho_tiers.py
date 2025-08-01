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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import from our modules
from modules.utils.config import UK_TZ
from modules.utils.s3_data_loader import get_s3_client, load_multiple_booking_files, download_s3_file_cached
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
        'MAILGUN_SMTP_LOGIN', 'MAILGUN_SMTP_PASSWORD',
        'MAILGUN_DOMAIN'
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
    
    # S3 keys
    key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
    key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
    key_account = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Load Account report for LastEventCreation data
        print(f"Loading Account report from: {key_account}")
        logger.info(f"Loading Account report from S3: {key_account}")
        account_df = download_s3_file_cached(s3_client, key_account)
        
        # Create lookup dictionary: AccountId -> {LastEventCreation, Industry, DateTimeCreated, AccountName, AccountStatus}
        account_lookup = {}
        required_cols = ['Id', 'LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        optional_cols = ['AccountName', 'AccountStatus']
        
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
        
        # Process data using optimized chunked approach
        logger.info("Starting optimized booking data processing")
        # Process booking data using the new clean API
        print("\nProcessing booking data...")
        aggregator = BookingAggregator(
            cutoff_365=CUTOFF_365,
            cutoff_730=CUTOFF_730,
            event_freq_cutoff_current=EVENT_FREQ_CUTOFF_CURRENT,
            event_freq_cutoff_previous=EVENT_FREQ_CUTOFF_PREVIOUS
        )
        
        # Load and process chunks from both files
        chunks = load_multiple_booking_files(s3_client, [key_all, key_month])
        account_metrics = aggregator.aggregate_bookings(chunks)
        
        logger.info(f"Total unique accounts found: {len(account_metrics):,}")
        print(f"\nTotal unique accounts found: {len(account_metrics):,}")
        
        # Load full booking data for revenue factor calculations
        print("\nLoading full booking data for revenue analysis...")
        logger.info("Starting revenue analysis data loading")
        booking_data_df = None
        try:
            # Load both BookingDataAll and current month BookingData
            print(f"Loading BookingDataAll from: {key_all}")
            logger.info(f"Loading BookingDataAll for revenue analysis")
            booking_all_df = download_s3_file_cached(s3_client, key_all)
            
            print(f"Loading BookingData from: {key_month}")
            booking_month_df = download_s3_file_cached(s3_client, key_month)
            
            # Combine and remove duplicates based on BookingTransactionId
            print("Combining booking data and removing duplicates...")
            logger.info("Combining booking data files")
            booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
            initial_count = len(booking_data_df)
            booking_data_df = booking_data_df.drop_duplicates(subset='BookingTransactionId')
            duplicates_removed = initial_count - len(booking_data_df)
            logger.info(f"Removed {duplicates_removed:,} duplicate transactions, {len(booking_data_df):,} remaining")
            print(f"Removed {duplicates_removed:,} duplicate transactions")
            
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
            
            # Ensure TransactionDate is datetime
            if 'TransactionDate' in booking_data_df.columns:
                booking_data_df['TransactionDate'] = pd.to_datetime(booking_data_df['TransactionDate'])
            
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
                two_years_ago = pd.Timestamp.now() - pd.DateOffset(years=2)
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
            from modules.utils.config import MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN
            if all([MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN]):
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
        from modules.utils.config import MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN
        if all([MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN]):
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
            
            # Generate the ZIP file
            zip_buffer = generate_industry_revenue_reports(
                booking_data_df, 
                account_df, 
                updates,
                report_date
            )
            
            # Save the ZIP file
            zip_filename = f"industry_revenue_reports_{report_date.strftime('%Y%m')}.zip"
            with open(zip_filename, 'wb') as f:
                f.write(zip_buffer.getvalue())
            
            logger.info(f"Saved industry revenue reports to: {zip_filename}")
            print(f"✓ Industry revenue reports saved to: {zip_filename}")
            
            # Log file size
            file_size_mb = os.path.getsize(zip_filename) / (1024 * 1024)
            print(f"  File size: {file_size_mb:.1f} MB")
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