#!/usr/bin/env python3
"""
Main runner for Zoho tier updates.
Calculates account tiers, event frequencies, and activity ratings.
"""
import time
import pandas as pd
from datetime import datetime

# Import from our modules
from modules.config import UK_TZ
from modules.s3_data_loader import get_s3_client, process_booking_data_optimized, download_s3_file_cached
from modules.tier_calculator import calculate_metrics_from_aggregated
from modules.zoho_api import get_access_token, upsert_to_zoho
from modules.report_generator import generate_upcoming_annual_events_report, email_upcoming_events_report


def main():
    """Main execution function."""
    start_time = time.time()
    
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

    print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
    
    # S3 keys
    key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
    key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
    key_account = f"{year}/{month}/{prefix}-Account-TBUK.csv"
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Load Account report for LastEventCreation data
        print(f"Loading Account report from: {key_account}")
        account_df = download_s3_file_cached(s3_client, key_account)
        
        # Create lookup dictionary: AccountId -> {LastEventCreation}
        account_lookup = {}
        if 'Id' in account_df.columns and 'LastEventCreation' in account_df.columns:
            account_lookup = account_df.set_index('Id')[['LastEventCreation']].to_dict('index')
            print(f"Loaded {len(account_lookup):,} accounts with LastEventCreation data")
        else:
            print("WARNING: Account report missing required columns (Id, LastEventCreation)")
        
        # Process data using optimized chunked approach
        account_metrics = process_booking_data_optimized(s3_client, key_all, key_month)
        
        print(f"\nTotal unique accounts found: {len(account_metrics):,}")
        
    except Exception as e:
        print(f"ERROR: Failed to process S3 files: {str(e)}")
        import traceback
        traceback.print_exc()
        return
    
    # Calculate metrics and tiers with account lookup
    updates = calculate_metrics_from_aggregated(account_metrics, account_lookup)
    
    # Save results to CSV for audit
    csv_filename = f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    updates.to_csv(csv_filename, index=False)
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
    
    # Generate annual events report
    print("\n=== Annual Events Report ===")
    # First show how many annual accounts we have
    annual_count = len(updates[updates['Event_Frequency_Current'] == 'Annual'])
    annual_prev_count = len(updates[updates['Event_Frequency_Previous'] == 'Annual'])
    print(f"Annual accounts (current): {annual_count}")
    print(f"Annual accounts (previous): {annual_prev_count}")
    
    # Show revenue filter impact
    annual_with_revenue = len(updates[
        ((updates['Event_Frequency_Current'] == 'Annual') | 
         (updates['Event_Frequency_Previous'] == 'Annual')) &
        ((updates.get('_revenue_current', 0) >= 100) | 
         (updates.get('_revenue_prev', 0) >= 100))
    ])
    print(f"Annual accounts with £100+ revenue: {annual_with_revenue}")
    
    annual_report = generate_upcoming_annual_events_report(updates)
    if not annual_report.empty:
        report_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
        annual_report.to_csv(report_filename, index=False)
        print(f"Upcoming annual events needing outreach: {len(annual_report)}")
        
        try:
            # Check if email credentials are configured
            from modules.config import MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN
            if all([MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN]):
                email_upcoming_events_report(annual_report, report_filename)
                print(f"📧 Emailed upcoming annual events report")
            else:
                print("Email credentials not configured - skipping email")
        except Exception as e:
            print(f"WARNING: Failed to email annual events report: {str(e)}")
    else:
        print("No upcoming annual events requiring outreach in next 30 days")
    
    # Clean up hidden fields before Zoho upload
    hidden_cols = [col for col in updates.columns if col.startswith('_')]
    zoho_updates = updates.drop(columns=hidden_cols, errors='ignore')
    print(f"\nRemoving {len(hidden_cols)} hidden columns before Zoho upload")
    
    if not zoho_updates.empty:
        # Get Zoho token and update
        try:
            print("\nAuthenticating with Zoho...")
            token = get_access_token()
            
            print("Updating Zoho CRM...")
            upsert_to_zoho(token, zoho_updates)
            
        except Exception as e:
            print(f"ERROR: Zoho update failed: {str(e)}")
            import traceback
            traceback.print_exc()
    else:
        print("No updates required.")
    
    # Performance stats
    elapsed_time = time.time() - start_time
    print(f"\n=== Completed at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Total execution time: {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")


if __name__ == "__main__":
    main()