#!/usr/bin/env python3
"""
Compare old (v1) and new (v2) tier systems side by side.

Loads data once, runs both tier calculations, and outputs a combined CSV
showing how each account is classified under both systems.

Usage:
    python3 compare_tiers.py
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
)
from modules.utils.validation import validate_environment_variables
from modules.booking_aggregator import BookingAggregator
from modules.account_processor import process_accounts
from modules.tier_calculator_v2 import calculate_composite_tiers


def main():
    start_time = time.time()

    validate_environment_variables([
        'AWS_ACCESS_KEY_ID',
        'AWS_SECRET_ACCESS_KEY',
    ])

    print(f"\n=== Tier Comparison Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")

    # --- Determine report date ---
    today = pd.Timestamp.now(UK_TZ).normalize()
    report_date = today - pd.Timedelta(days=1) if today.day == 1 else today

    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")
    print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")

    try:
        s3_client = get_s3_client()

        # --- Load Account report ---
        key_account = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
        print(f"\nLoading Account report from: {key_account}")
        account_df = download_s3_file_cached(s3_client, key_account)

        # Account name lookup for v2
        account_name_lookup = {}
        if 'Id' in account_df.columns and 'AccountName' in account_df.columns:
            account_name_lookup = account_df.set_index('Id')['AccountName'].to_dict()
            print(f"Loaded {len(account_name_lookup):,} account names")

        # Account lookup for v1 (needs more columns)
        account_lookup = {}
        required_cols = ['Id', 'LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        optional_cols = ['AccountName', 'AccountStatus', 'LastLogIn']
        lookup_cols = ['LastEventCreation', 'Industry', 'Postcode', 'DateTimeCreated']
        for col in optional_cols:
            if col in account_df.columns:
                lookup_cols.append(col)
        if all(col in account_df.columns for col in required_cols):
            account_lookup = account_df.set_index('Id')[lookup_cols].to_dict('index')

        # --- Load booking data ---
        print("\nLoading booking data...")
        booking_all_df = load_booking_data(s3_client, report_date, data_type='BookingDataAll')
        print("Loading current month BookingData...")
        booking_month_df = load_booking_data(s3_client, report_date, data_type='BookingData')

        print("Combining and deduplicating...")
        booking_data_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
        initial_count = len(booking_data_df)
        booking_data_df = booking_data_df.drop_duplicates(subset='BookingTransactionId')
        print(f"Removed {initial_count - len(booking_data_df):,} duplicates, {len(booking_data_df):,} remaining")

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
        print(f"Total unique accounts: {len(account_metrics):,}")

        # --- Prepare booking data for v1 revenue analysis ---
        revenue_cols = ['AccountId', 'TransactionDate', 'PaymentReceived', 'BookingFee',
                        'CardFee', 'ProcessingFee', 'TicketFee', 'EventId', 'TicketQuantity']
        available_cols = [col for col in revenue_cols if col in booking_data_df.columns]
        revenue_df = booking_data_df[available_cols].copy()

        if all(col in revenue_df.columns for col in ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']):
            revenue_df['Revenue'] = (revenue_df['BookingFee'] + revenue_df['CardFee'] +
                                     revenue_df['ProcessingFee'] + revenue_df['TicketFee'])

        if 'Industry' in account_df.columns and 'SubIndustry' in account_df.columns:
            account_industry_df = account_df[['Id', 'Industry', 'SubIndustry']].copy()
            account_industry_df.rename(columns={'Id': 'AccountId'}, inplace=True)
            revenue_df['AccountId'] = revenue_df['AccountId'].astype(str)
            account_industry_df['AccountId'] = account_industry_df['AccountId'].astype(str)
            revenue_df = revenue_df.merge(account_industry_df, on='AccountId', how='left')

        if 'TransactionDate' in revenue_df.columns:
            revenue_df['Year'] = revenue_df['TransactionDate'].dt.year
            revenue_df['Month'] = revenue_df['TransactionDate'].dt.month
            two_years_ago = pd.Timestamp.now('UTC') - pd.DateOffset(years=2)
            revenue_df = revenue_df[revenue_df['TransactionDate'] >= two_years_ago]

        del booking_data_df

    except Exception as e:
        logger.error(f"Failed to load/process S3 data: {e}")
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return

    # ================================================================
    # Run v1 (old system)
    # ================================================================
    print("\n--- Running v1 (old tier system) ---")
    v1_results = process_accounts(account_metrics, account_lookup, revenue_df)
    print(f"v1 produced {len(v1_results):,} accounts")

    # ================================================================
    # Run v2 (new system)
    # ================================================================
    print("\n--- Running v2 (new tier system) ---")
    v2_results = calculate_composite_tiers(account_metrics)
    v2_results['Account_Display_Name'] = v2_results['AccountId'].map(account_name_lookup).fillna('')
    print(f"v2 produced {len(v2_results):,} accounts")

    # ================================================================
    # Merge and compare
    # ================================================================
    print("\n--- Merging results ---")

    # Normalise account ID for join — strip trailing .0 from float-to-string
    def normalise_id(val):
        s = str(val)
        if s.endswith('.0'):
            s = s[:-2]
        return s

    v1_results['_join_id'] = v1_results['Account_Name'].apply(normalise_id)
    v2_results['_join_id'] = v2_results['AccountId'].apply(normalise_id)

    comparison = v1_results[['_join_id', 'Current_Tier', 'Previous_Tier']].merge(
        v2_results[['_join_id', 'Account_Display_Name', 'Current_Tier', 'Previous_Tier',
                     'Composite_Score', 'Revenue_Current', 'Revenue_Lifetime',
                     'Tickets_Current', 'Years_Loyalty']],
        on='_join_id',
        how='outer',
        suffixes=('_v1', '_v2')
    )
    comparison.rename(columns={'_join_id': 'Account_Name'}, inplace=True)

    # --- Map v1 tier names to v2 equivalents for like-for-like comparison ---
    V1_TO_V2_MAP = {
        'Key Account': 'Tier 1',
        'High Value': 'Tier 2',
        'Tier 4': 'Tier 3',
        'Tier 3': 'Tier 4',
        'Tier 2': 'Tier 5',
        'Tier 1': 'Tier 5',
        'NIL': 'Nil',
    }
    comparison['Current_Tier_v1_mapped'] = comparison['Current_Tier_v1'].map(V1_TO_V2_MAP)
    comparison['Tier_Changed'] = comparison['Current_Tier_v1_mapped'] != comparison['Current_Tier_v2']

    # --- Output CSV ---
    csv_filename = f"tier_comparison_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    comparison.to_csv(csv_filename, index=False)
    print(f"\nSaved comparison to: {csv_filename}")
    print(f"Total accounts: {len(comparison):,}")

    # --- Summary ---
    matched = comparison.dropna(subset=['Current_Tier_v1_mapped', 'Current_Tier_v2'])
    changed = matched[matched['Tier_Changed']]
    print(f"Accounts in both systems: {len(matched):,}")
    print(f"Tier changed: {len(changed):,} ({len(changed)/len(matched)*100:.1f}%)")
    print(f"Tier unchanged: {len(matched) - len(changed):,}")

    # Cross-tab with mapped v1 tiers
    tier_order = ['Tier 1', 'Tier 2', 'Tier 3', 'Tier 4', 'Tier 5', 'Free', 'Nil']
    # Matrix with original v1 tier names
    v1_tier_order = ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']
    print("\nMigration matrix (v1 original names → v2 columns):")
    ct_orig = pd.crosstab(
        matched['Current_Tier_v1'],
        matched['Current_Tier_v2'],
        margins=True,
        margins_name='Total',
    )
    row_order_orig = [t for t in v1_tier_order if t in ct_orig.index] + ['Total']
    col_order = [t for t in tier_order if t in ct_orig.columns] + ['Total']
    ct_orig = ct_orig.reindex(index=row_order_orig, columns=col_order, fill_value=0)
    print(ct_orig.to_string())

    # Also save as CSV
    ct_orig.index.name = 'v1_Tier'
    ct_orig.to_csv('tier_migration_matrix.csv')
    print("\nSaved tier_migration_matrix.csv")

    # Movement summary
    print("\nMovement summary:")
    tier_num = {'Tier 1': 1, 'Tier 2': 2, 'Tier 3': 3, 'Tier 4': 4, 'Tier 5': 5, 'Free': 6, 'Nil': 7}
    matched = matched.copy()
    matched['v1_num'] = matched['Current_Tier_v1_mapped'].map(tier_num)
    matched['v2_num'] = matched['Current_Tier_v2'].map(tier_num)
    matched['delta'] = matched['v1_num'] - matched['v2_num']
    promoted = matched[matched['delta'] > 0]
    demoted = matched[matched['delta'] < 0]
    same = matched[matched['delta'] == 0]
    print(f"  Promoted (better tier in v2): {len(promoted):,} ({len(promoted)/len(matched)*100:.1f}%)")
    print(f"  Same tier:                    {len(same):,} ({len(same)/len(matched)*100:.1f}%)")
    print(f"  Demoted (worse tier in v2):   {len(demoted):,} ({len(demoted)/len(matched)*100:.1f}%)")

    # Show some interesting movers
    if len(changed) > 0:
        print(f"\nSample tier changes (first 10):")
        for _, row in changed.head(10).iterrows():
            name = row.get('Account_Display_Name', row['Account_Name'])
            print(f"  {str(name):40s}  v1: {str(row['Current_Tier_v1']):12s} ({str(row['Current_Tier_v1_mapped']):6s}) → v2: {str(row['Current_Tier_v2']):8s}  "
                  f"Rev: £{row.get('Revenue_Current', 0):>7,.0f}  Tix: {row.get('Tickets_Current', 0):>5,.0f}")

    elapsed = time.time() - start_time
    print(f"\n=== Completed in {elapsed:.1f}s ({elapsed / 60:.1f} minutes) ===")


if __name__ == "__main__":
    main()
