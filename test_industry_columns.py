#!/usr/bin/env python3
"""
Test script to verify Industry and SubIndustry columns in booking data.
This can be run locally or in GitHub Actions to verify the S3 export format.
"""
import os
import sys

# Check if we're in GitHub Actions or local environment
if 'GITHUB_ACTIONS' in os.environ:
    # In GitHub Actions, pandas should be installed
    import pandas as pd
    from modules.utils.config import UK_TZ
    from modules.utils.s3_data_loader import get_s3_client
else:
    print("This script is designed to run in GitHub Actions environment.")
    print("To run locally, ensure you have:")
    print("  - pandas installed")
    print("  - AWS credentials set")
    sys.exit(0)


def check_booking_columns():
    """Check what columns are present in the booking data."""
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Determine current month
        today = pd.Timestamp.now(UK_TZ).normalize()
        if today.day == 1:
            report_date = today - pd.Timedelta(days=1)
        else:
            report_date = today
        
        prefix = report_date.strftime("%Y%m")
        year = report_date.strftime("%Y")
        month = report_date.strftime("%m")
        
        # Check BookingData file
        key_booking = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
        print(f"Checking columns in: {key_booking}")
        
        # Read just the header row
        obj = s3_client.get_object(Bucket='produk-rdsextracts-438255373632', Key=key_booking)
        header_df = pd.read_csv(obj['Body'], nrows=0)
        
        print(f"\nTotal columns: {len(header_df.columns)}")
        print("\nAll columns:")
        for i, col in enumerate(header_df.columns, 1):
            print(f"  {i:2d}. {col}")
        
        # Check for specific columns
        print("\nChecking for industry-related columns:")
        industry_cols = ['Industry', 'SubIndustry', 'Gateway Group']
        for col in industry_cols:
            if col in header_df.columns:
                print(f"  ✓ {col} - FOUND")
            else:
                print(f"  ✗ {col} - NOT FOUND")
        
        # Check BookingDataAll file
        key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
        print(f"\n\nChecking columns in: {key_all}")
        
        obj_all = s3_client.get_object(Bucket='produk-rdsextracts-438255373632', Key=key_all)
        header_all_df = pd.read_csv(obj_all['Body'], nrows=0)
        
        # Compare columns
        if set(header_df.columns) == set(header_all_df.columns):
            print("✓ BookingData and BookingDataAll have the same columns")
        else:
            print("✗ BookingData and BookingDataAll have different columns!")
            only_in_data = set(header_df.columns) - set(header_all_df.columns)
            only_in_all = set(header_all_df.columns) - set(header_df.columns)
            if only_in_data:
                print(f"  Only in BookingData: {only_in_data}")
            if only_in_all:
                print(f"  Only in BookingDataAll: {only_in_all}")
        
        return True
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=== TryBooking S3 Export Column Verification ===\n")
    success = check_booking_columns()
    sys.exit(0 if success else 1)