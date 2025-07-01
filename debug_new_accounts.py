#!/usr/bin/env python3
"""
Debug script to analyze why new accounts count is 397 instead of 400.
"""
import pandas as pd
from datetime import datetime
import pytz
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached
from modules.utils.date_utils import get_last_month_dates, get_file_date_info
from modules.utils.data_loaders import load_accounts_data

def main():
    print("=== Debugging New Accounts Count ===\n")
    
    # Initialize S3 client
    s3_client = get_s3_client()
    
    # Get date ranges
    dates = get_last_month_dates()
    print(f"Analyzing date range: {dates['last_month_start']} to {dates['last_month_end']}")
    print(f"Timezone info: start.tz={dates['last_month_start'].tz}, end.tz={dates['last_month_end'].tz}\n")
    
    # Load accounts data
    accounts_df = load_accounts_data(s3_client, dates['last_month_end'])
    print(f"Total accounts loaded: {len(accounts_df):,}")
    
    # Check DateTimeCreated column
    print(f"\nDateTimeCreated column info:")
    print(f"- Data type: {accounts_df['DateTimeCreated'].dtype}")
    print(f"- Has timezone: {accounts_df['DateTimeCreated'].dt.tz is not None}")
    if accounts_df['DateTimeCreated'].dt.tz:
        print(f"- Timezone: {accounts_df['DateTimeCreated'].dt.tz}")
    
    # Get min/max dates in the data
    min_date = accounts_df['DateTimeCreated'].min()
    max_date = accounts_df['DateTimeCreated'].max()
    print(f"\nDate range in data: {min_date} to {max_date}")
    
    # Filter for last month using the same logic as monthly_reporting.py
    last_month_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= dates['last_month_start']) & 
        (accounts_df['DateTimeCreated'] <= dates['last_month_end'])
    ].copy()
    
    print(f"\nAccounts created in {dates['month_name']}: {len(last_month_accounts):,}")
    
    # Look at edge cases - accounts created at the very beginning and end of the month
    print("\n=== Edge Case Analysis ===")
    
    # First day of month
    first_day_start = dates['last_month_start']
    first_day_end = dates['last_month_start'].replace(hour=23, minute=59, second=59)
    first_day_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= first_day_start) & 
        (accounts_df['DateTimeCreated'] <= first_day_end)
    ]
    print(f"\nAccounts created on first day ({first_day_start.date()}): {len(first_day_accounts)}")
    
    # Last day of month
    last_day_start = dates['last_month_end'].replace(hour=0, minute=0, second=0)
    last_day_end = dates['last_month_end']
    last_day_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= last_day_start) & 
        (accounts_df['DateTimeCreated'] <= last_day_end)
    ]
    print(f"Accounts created on last day ({last_day_start.date()}): {len(last_day_accounts)}")
    
    # Check for accounts just outside the boundaries
    print("\n=== Boundary Analysis ===")
    
    # Just before start
    before_start = accounts_df[
        (accounts_df['DateTimeCreated'] >= dates['last_month_start'] - pd.Timedelta(hours=24)) & 
        (accounts_df['DateTimeCreated'] < dates['last_month_start'])
    ]
    print(f"Accounts created in 24h before month start: {len(before_start)}")
    if len(before_start) > 0:
        print("Sample dates just before month:")
        for idx, row in before_start.head(5).iterrows():
            print(f"  - {row['DateTimeCreated']}")
    
    # Just after end
    after_end = accounts_df[
        (accounts_df['DateTimeCreated'] > dates['last_month_end']) & 
        (accounts_df['DateTimeCreated'] <= dates['last_month_end'] + pd.Timedelta(hours=24))
    ]
    print(f"\nAccounts created in 24h after month end: {len(after_end)}")
    if len(after_end) > 0:
        print("Sample dates just after month:")
        for idx, row in after_end.head(5).iterrows():
            print(f"  - {row['DateTimeCreated']}")
    
    # Check for duplicates
    print("\n=== Duplicate Analysis ===")
    if 'Id' in accounts_df.columns:
        duplicate_ids = accounts_df['Id'].duplicated().sum()
        print(f"Duplicate account IDs in full dataset: {duplicate_ids}")
        
        # Check duplicates in filtered data
        duplicate_ids_filtered = last_month_accounts['Id'].duplicated().sum()
        print(f"Duplicate account IDs in filtered dataset: {duplicate_ids_filtered}")
    
    # Distribution by day
    print("\n=== Daily Distribution ===")
    last_month_accounts['Day'] = last_month_accounts['DateTimeCreated'].dt.date
    daily_counts = last_month_accounts.groupby('Day').size().sort_index()
    print("Accounts created per day:")
    for day, count in daily_counts.items():
        print(f"  {day}: {count}")
    
    # Show timezone conversion examples
    print("\n=== Timezone Conversion Examples ===")
    print("Sample of DateTimeCreated values from filtered accounts:")
    for idx, row in last_month_accounts.head(5).iterrows():
        dt = row['DateTimeCreated']
        print(f"  - {dt} (hour={dt.hour}, tz={dt.tz})")
    
    # SQL-like count to match expected 400
    print("\n=== Alternative Counting Methods ===")
    
    # Method 1: Using string comparison on date only
    date_str_start = dates['last_month_start'].strftime('%Y-%m-01')
    date_str_end = dates['last_month_end'].strftime('%Y-%m-%d')
    accounts_df['DateOnly'] = accounts_df['DateTimeCreated'].dt.strftime('%Y-%m-%d')
    method1_count = len(accounts_df[
        (accounts_df['DateOnly'] >= date_str_start) & 
        (accounts_df['DateOnly'] <= date_str_end)
    ])
    print(f"Method 1 (date string comparison): {method1_count}")
    
    # Method 2: Using month and year
    target_month = dates['last_month_start'].month
    target_year = dates['last_month_start'].year
    method2_count = len(accounts_df[
        (accounts_df['DateTimeCreated'].dt.month == target_month) & 
        (accounts_df['DateTimeCreated'].dt.year == target_year)
    ])
    print(f"Method 2 (month/year match): {method2_count}")
    
    # Summary
    print("\n=== SUMMARY ===")
    print(f"Expected count: 400")
    print(f"Actual count: {len(last_month_accounts)}")
    print(f"Difference: {400 - len(last_month_accounts)}")
    print(f"\nLikely causes:")
    print(f"- Timezone conversion issues: {len(before_start) + len(after_end)} accounts near boundaries")
    print(f"- Duplicate IDs removed: {duplicate_ids_filtered if 'Id' in accounts_df.columns else 'N/A'}")
    print(f"- Data filtering in pipeline: Check if source data has exactly 400")

if __name__ == "__main__":
    main()