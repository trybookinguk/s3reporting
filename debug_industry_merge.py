#!/usr/bin/env python3
"""
Debug script to investigate why Industry column is missing after merge.
"""
import pandas as pd
from datetime import datetime
from modules.utils.config import UK_TZ
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached
from modules.utils.industry_utils import prepare_booking_data_with_industry


def main():
    print("=== Debugging Industry Merge Issue ===\n")
    
    # Initialize S3 client
    s3_client = get_s3_client()
    
    # Determine report date
    today = pd.Timestamp.now(UK_TZ).normalize()
    if today.day == 1:
        report_date = today - pd.Timedelta(days=1)
    else:
        report_date = today
    
    # Load accounts data
    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")
    key_account = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
    
    print(f"Loading accounts from: {key_account}")
    accounts_df = download_s3_file_cached(s3_client, key_account)
    print(f"Accounts loaded: {len(accounts_df):,} records")
    
    # Check columns in accounts
    print(f"\nAccount columns ({len(accounts_df.columns)} total):")
    for i, col in enumerate(accounts_df.columns):
        print(f"  {i+1}. {col}")
    
    # Check if Industry exists
    if 'Industry' in accounts_df.columns:
        print(f"\n✓ Industry column FOUND in accounts data")
        # Check Industry values
        print(f"\nIndustry value counts:")
        industry_counts = accounts_df['Industry'].value_counts(dropna=False)
        print(f"  Total unique industries: {len(industry_counts)}")
        print(f"  Non-null industries: {accounts_df['Industry'].notna().sum():,}")
        print(f"  Null industries: {accounts_df['Industry'].isna().sum():,}")
        print("\nTop 10 industries:")
        for industry, count in industry_counts.head(10).items():
            print(f"  - {industry}: {count:,}")
    else:
        print(f"\n✗ Industry column NOT FOUND in accounts data!")
        # Check for similar column names
        print("\nChecking for similar column names:")
        for col in accounts_df.columns:
            if 'industry' in col.lower() or 'sector' in col.lower():
                print(f"  - Found: {col}")
    
    # Check SubIndustry too
    if 'SubIndustry' in accounts_df.columns:
        print(f"\n✓ SubIndustry column FOUND in accounts data")
    else:
        print(f"\n✗ SubIndustry column NOT FOUND in accounts data!")
    
    # Load a small sample of booking data
    key_booking = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
    print(f"\n\nLoading booking sample from: {key_booking}")
    
    # Read just first 1000 rows for testing
    obj = s3_client.get_object(Bucket='produk-rdsextracts-438255373632', Key=key_booking)
    booking_sample = pd.read_csv(obj['Body'], nrows=1000)
    print(f"Booking sample loaded: {len(booking_sample):,} records")
    
    # Check columns in bookings
    print(f"\nBooking columns ({len(booking_sample.columns)} total):")
    for i, col in enumerate(booking_sample.columns):
        print(f"  {i+1}. {col}")
    
    # Check if Industry already exists in bookings
    if 'Industry' in booking_sample.columns:
        print(f"\n✓ Industry column ALREADY EXISTS in booking data!")
        print("  This suggests the S3 export already includes Industry")
    else:
        print(f"\n✗ Industry column NOT in booking data (expected)")
    
    # Test the merge function
    print("\n\nTesting merge function...")
    merged_sample = prepare_booking_data_with_industry(booking_sample, accounts_df)
    
    print(f"\nMerge results:")
    print(f"  Original booking rows: {len(booking_sample):,}")
    print(f"  Merged booking rows: {len(merged_sample):,}")
    print(f"  Columns before merge: {len(booking_sample.columns)}")
    print(f"  Columns after merge: {len(merged_sample.columns)}")
    
    # Check if Industry was added
    if 'Industry' in merged_sample.columns:
        print(f"\n✓ Industry column EXISTS after merge")
        # Check how many got matched
        matched = merged_sample['Industry'].notna().sum()
        print(f"  Matched records: {matched:,} ({matched/len(merged_sample)*100:.1f}%)")
    else:
        print(f"\n✗ Industry column MISSING after merge!")
        # List new columns added by merge
        new_cols = set(merged_sample.columns) - set(booking_sample.columns)
        if new_cols:
            print(f"  New columns added by merge: {new_cols}")
        else:
            print(f"  NO new columns added by merge!")
    
    # Check data types
    print("\n\nChecking AccountId data types:")
    print(f"  Booking AccountId type: {booking_sample['AccountId'].dtype}")
    print(f"  Booking AccountId sample: {booking_sample['AccountId'].head(5).tolist()}")
    print(f"  Accounts Id type: {accounts_df['Id'].dtype}")
    print(f"  Accounts Id sample: {accounts_df['Id'].head(5).tolist()}")
    
    # Test direct merge
    print("\n\nTesting direct merge without function:")
    booking_sample_copy = booking_sample.copy()
    booking_sample_copy['AccountId'] = booking_sample_copy['AccountId'].astype(str)
    
    account_industry = accounts_df[['Id', 'Industry']].copy()
    account_industry['Id'] = account_industry['Id'].astype(str)
    
    direct_merge = booking_sample_copy.merge(
        account_industry,
        left_on='AccountId',
        right_on='Id',
        how='left',
        indicator=True  # Add merge indicator
    )
    
    print(f"\nDirect merge results:")
    print(f"  Merge indicator counts:")
    print(direct_merge['_merge'].value_counts())
    
    if 'Industry' in direct_merge.columns:
        print(f"\n✓ Industry column exists in direct merge")
    else:
        print(f"\n✗ Industry column missing in direct merge")
        print(f"  Columns in merged data: {list(direct_merge.columns)}")


if __name__ == "__main__":
    main()