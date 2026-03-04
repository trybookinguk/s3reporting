#!/usr/bin/env python3
"""
Simplified tier calculation runner (v2).

Loads booking and account data from S3, computes a single weighted composite
score per account, and assigns tiers 1-5 based on percentile rank.

This is a dry-run script — it outputs a CSV and summary statistics but does
NOT update Zoho CRM.

Usage:
    python3 zoho_tiers_v2.py
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

from modules.utils.config import (
    UK_TZ, CUTOFF_365, CUTOFF_730,
    EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS,
)
from modules.utils.data_loader import (
    load_booking_data, download_s3_file_cached, get_s3_client,
    find_booking_files_in_month, S3_BUCKET, calculate_previous_month,
)
from modules.utils.validation import validate_environment_variables
from modules.booking_aggregator import BookingAggregator
from modules.tier_calculator_v2 import calculate_composite_tiers


def main():
    """Main execution function."""
    start_time = time.time()

    # Only AWS credentials needed for dry run
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID',
        'AWS_SECRET_ACCESS_KEY',
    ])

    logger.info(f"Tier v2 calculation started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"\n=== Tier v2 Calculation Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")

    # --- Determine report date ---
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

    try:
        s3_client = get_s3_client()

        # --- Load Account report (Id + AccountName only) ---
        key_account = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
        print(f"Loading Account report from: {key_account}")
        account_df = download_s3_file_cached(s3_client, key_account)

        account_name_lookup = {}
        if 'Id' in account_df.columns and 'AccountName' in account_df.columns:
            account_name_lookup = (
                account_df.set_index('Id')['AccountName'].to_dict()
            )
            logger.info(f"Loaded {len(account_name_lookup):,} account names")
            print(f"Loaded {len(account_name_lookup):,} account names")
        else:
            logger.warning("Account report missing Id or AccountName columns")

        # --- Load booking data ---
        print("\nLoading booking data...")
        booking_all_df = load_booking_data(s3_client, report_date, data_type='BookingDataAll')
        print("Loading current month BookingData...")
        booking_month_df = load_booking_data(s3_client, report_date, data_type='BookingData')

        print("Combining booking data and removing duplicates...")
        booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
        initial_count = len(booking_data_df)
        booking_data_df = booking_data_df.drop_duplicates(subset='BookingTransactionId')
        duplicates_removed = initial_count - len(booking_data_df)
        logger.info(f"Removed {duplicates_removed:,} duplicate transactions, {len(booking_data_df):,} remaining")
        print(f"Removed {duplicates_removed:,} duplicate transactions")

        del booking_all_df, booking_month_df

        # --- Aggregate bookings ---
        print("\nAggregating booking metrics...")
        aggregator = BookingAggregator(
            cutoff_365=CUTOFF_365,
            cutoff_730=CUTOFF_730,
            event_freq_cutoff_current=EVENT_FREQ_CUTOFF_CURRENT,
            event_freq_cutoff_previous=EVENT_FREQ_CUTOFF_PREVIOUS,
        )

        def df_to_chunks(df, chunk_size=100000):
            for i in range(0, len(df), chunk_size):
                yield df.iloc[i:i + chunk_size].copy()

        account_metrics = aggregator.aggregate_bookings(df_to_chunks(booking_data_df))
        del booking_data_df

        logger.info(f"Total unique accounts: {len(account_metrics):,}")
        print(f"Total unique accounts: {len(account_metrics):,}")

    except Exception as e:
        logger.error(f"Failed to load/process S3 data: {e}")
        print(f"ERROR: Failed to load/process S3 data: {e}")
        import traceback
        traceback.print_exc()
        return

    # --- Calculate composite tiers ---
    print("\nCalculating composite tiers...")
    results = calculate_composite_tiers(account_metrics)

    if results.empty:
        print("No results produced — check data pipeline.")
        return

    # Enrich with account display name
    results['Account_Name'] = results['AccountId'].astype(str)
    results['Account_Display_Name'] = results['AccountId'].map(account_name_lookup).fillna('')

    # Reorder columns to match spec
    output_columns = [
        'Account_Name',
        'Account_Display_Name',
        'Current_Tier',
        'Previous_Tier',
        'Tier_Movement',
        'Composite_Score',
        'Previous_Composite_Score',
        'Revenue_Current',
        'Revenue_Lifetime',
        'Tickets_Current',
        'Years_Loyalty',
        'A_Percentile',
        'B_Percentile',
        'C_Percentile',
    ]
    results = results[output_columns]

    # --- Output CSV ---
    csv_filename = f"tier_v2_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    results.to_csv(csv_filename, index=False)
    logger.info(f"Saved tier v2 results to: {csv_filename}")
    print(f"\nSaved tier v2 results to: {csv_filename}")

    # --- Summary statistics ---
    total = len(results)
    tier_counts = results['Current_Tier'].value_counts()

    print("\nTier Distribution:")
    for tier in ['Tier 1', 'Tier 2', 'Tier 3', 'Tier 4', 'Tier 5', 'Nil']:
        count = tier_counts.get(tier, 0)
        pct = (count / total * 100) if total > 0 else 0
        print(f"  {tier}: {count:,} ({pct:.1f}%)")

    # Movement summary
    movement_counts = results['Tier_Movement'].value_counts()
    print("\nTier Movement:")
    for label in ['Improved 2+ tiers', 'Improved 1 tier', 'No Change',
                   'Dropped 1 tier', 'Dropped 2+ tiers']:
        count = movement_counts.get(label, 0)
        print(f"  {label}: {count:,}")

    # Score stats
    activated = results[results['Current_Tier'] != 'Nil']
    if not activated.empty:
        print(f"\nComposite Score (activated accounts):")
        print(f"  Mean:   {activated['Composite_Score'].mean():.2f}")
        print(f"  Median: {activated['Composite_Score'].median():.2f}")
        print(f"  Min:    {activated['Composite_Score'].min():.2f}")
        print(f"  Max:    {activated['Composite_Score'].max():.2f}")

    # --- High-engagement free accounts report ---
    MIN_FREE_TICKETS = 1000
    free_accounts = results[
        (results['Revenue_Current'] == 0) &
        (results['Tickets_Current'] >= MIN_FREE_TICKETS)
    ].copy()

    if not free_accounts.empty:
        free_accounts = free_accounts.sort_values(
            ['Tickets_Current', 'Years_Loyalty'], ascending=[False, False]
        )
        free_report = free_accounts[[
            'Account_Name',
            'Account_Display_Name',
            'Tickets_Current',
            'Years_Loyalty',
        ]]
        free_csv = f"free_high_engagement_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
        free_report.to_csv(free_csv, index=False)
        print(f"\nHigh-engagement free accounts ({MIN_FREE_TICKETS}+ tickets): {len(free_report)}")
        print(f"Saved to: {free_csv}")
        for _, row in free_report.head(5).iterrows():
            print(f"  {row['Account_Display_Name']:45s}  Tix: {row['Tickets_Current']:>6,}  Yrs: {row['Years_Loyalty']}")
        if len(free_report) > 5:
            print(f"  ... and {len(free_report) - 5} more")
    else:
        print(f"\nNo free accounts with {MIN_FREE_TICKETS}+ tickets found.")

    elapsed = time.time() - start_time
    logger.info(f"Tier v2 calculation completed in {elapsed:.1f}s")
    print(f"\n=== Completed at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Total execution time: {elapsed:.1f}s ({elapsed / 60:.1f} minutes)")


if __name__ == "__main__":
    main()
