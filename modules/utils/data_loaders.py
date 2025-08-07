"""
Standardized data loading functions for TryBooking reports.
"""
import pandas as pd
from .s3_data_loader import download_s3_file_cached
from .date_utils import get_file_date_info, get_latest_data_date
from .performance import optimize_dtypes, timer_decorator


@timer_decorator
def load_accounts_data(s3_client, target_date=None):
    """
    Load and preprocess accounts data from S3.
    
    Args:
        s3_client: Boto3 S3 client
        target_date: Date to load data for (defaults to yesterday)
    
    Returns:
        DataFrame with preprocessed accounts data
    """
    if target_date is None:
        # Default to yesterday (latest available data)
        target_date = get_latest_data_date()
    
    date_info = get_file_date_info(target_date)
    filename = f"{date_info['file_prefix']}-Accounts-TBUK.csv"
    s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
    
    print(f"Loading accounts data from S3: {s3_key}")
    df = download_s3_file_cached(s3_client, s3_key)
    
    # Standardize datetime columns
    df['DateTimeCreated'] = pd.to_datetime(df['DateTimeCreated'], errors='coerce', utc=True)
    
    # Handle FirstEventCreation carefully (can be null)
    df['FirstEventCreation'] = pd.to_datetime(df['FirstEventCreation'], errors='coerce', utc=True)
    
    # Handle LastEventCreation if present
    if 'LastEventCreation' in df.columns:
        df['LastEventCreation'] = pd.to_datetime(df['LastEventCreation'], errors='coerce', utc=True)
    
    # Optimize data types for memory efficiency
    df = optimize_dtypes(df)
    
    return df


@timer_decorator
def load_booking_data(s3_client, target_date=None, data_type='BookingData'):
    """
    Load and preprocess booking data from S3.
    
    Args:
        s3_client: Boto3 S3 client
        target_date: Date to load data for (defaults to yesterday)
        data_type: Either 'BookingData' or 'BookingDataAll'
    
    Returns:
        DataFrame with preprocessed booking data including calculated TotalFees
    """
    if target_date is None:
        # Default to yesterday (latest available data)
        target_date = get_latest_data_date()
    
    date_info = get_file_date_info(target_date)
    
    # BookingDataAll has a special naming convention with 01 suffix
    if data_type == 'BookingDataAll':
        filename = f"{date_info['file_prefix']}01-{data_type}-TBUK.csv"
    else:
        filename = f"{date_info['file_prefix']}-{data_type}-TBUK.csv"
    
    s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
    
    print(f"Loading {data_type} from S3: {s3_key}")
    
    # Try to download the file, with fallback for BookingDataAll
    try:
        df = download_s3_file_cached(s3_client, s3_key)
    except Exception as e:
        # If BookingDataAll fails, try to find alternative files in the same month
        if data_type == 'BookingDataAll' and 'NoSuchKey' in str(e):
            print(f"  Primary BookingDataAll file not found, searching for alternatives...")
            
            # Try other days in the same month (e.g., if 01 doesn't exist, try 05)
            from .s3_data_loader import S3_BUCKET
            import boto3
            
            try:
                # List all objects in the month folder
                prefix = f"{date_info['folder_year']}/{date_info['folder_month']}/"
                response = s3_client.list_objects_v2(
                    Bucket=S3_BUCKET,
                    Prefix=prefix
                )
                
                # Find all BookingDataAll files in the month
                booking_data_all_files = []
                if 'Contents' in response:
                    for obj in response['Contents']:
                        key = obj['Key']
                        if 'BookingDataAll-TBUK.csv' in key:
                            booking_data_all_files.append(key)
                
                if booking_data_all_files:
                    # Sort to get the earliest available file
                    booking_data_all_files.sort()
                    alternative_key = booking_data_all_files[0]
                    print(f"  Found alternative BookingDataAll: {alternative_key}")
                    # Extract the day from filename like "20250805-BookingDataAll-TBUK.csv"
                    filename_part = alternative_key.split('/')[-1]
                    if len(filename_part) >= 8:
                        day_part = filename_part[6:8]
                        print(f"  Note: Expected file on day 01 but using file from day {day_part}")
                    df = download_s3_file_cached(s3_client, alternative_key)
                else:
                    print(f"  No BookingDataAll files found in {prefix}")
                    print(f"  This might indicate the monthly BookingDataAll report hasn't been generated yet.")
                    raise e
            except Exception as list_error:
                print(f"  Error searching for alternatives: {list_error}")
                raise e
        else:
            raise
    
    # Convert TransactionDate
    df['TransactionDate'] = pd.to_datetime(df['TransactionDate'], errors='coerce', utc=True)
    
    # Convert all fee columns to numeric
    fee_columns = ['PaymentReceived', 'TicketQuantity', 'BookingFee', 'CardFee', 
                   'ProcessingFee', 'TicketFee', 'Surcharge', 'ProcessingFeeSurcharge']
    
    for col in fee_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    # Calculate total fees (all fee columns that exist)
    fee_components = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    existing_fees = [col for col in fee_components if col in df.columns]
    df['TotalFees'] = df[existing_fees].sum(axis=1)
    
    # Add EventDate as datetime if present
    if 'EventDate' in df.columns:
        df['EventDate'] = pd.to_datetime(df['EventDate'], errors='coerce', utc=True)
    
    # Optimize data types for memory efficiency
    df = optimize_dtypes(df)
    
    return df


def filter_successful_transactions(df):
    """Filter booking data to only include successful transactions."""
    if 'Status' in df.columns:
        return df[df['Status'] == 'Successful'].copy()
    return df


@timer_decorator
def load_account_balance_data(s3_client, target_date=None):
    """
    Load and preprocess account balance data from S3.
    
    Args:
        s3_client: Boto3 S3 client
        target_date: Date to load data for (defaults to yesterday)
    
    Returns:
        DataFrame with preprocessed account balance data
    """
    if target_date is None:
        # Default to yesterday (latest available data)
        target_date = get_latest_data_date()
    
    date_info = get_file_date_info(target_date)
    filename = f"{date_info['file_prefix']}-accountbalance-TBUK.csv"
    s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
    
    print(f"Loading account balance data from S3: {s3_key}")
    df = download_s3_file_cached(s3_client, s3_key)
    
    # Ensure AccountId is numeric
    if 'AccountId' in df.columns:
        df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')
    
    # AccountBalance should be numeric
    if 'AccountBalance' in df.columns:
        df['AccountBalance'] = pd.to_numeric(df['AccountBalance'], errors='coerce').fillna(0)
    
    # Optimize data types for memory efficiency
    df = optimize_dtypes(df)
    
    return df


@timer_decorator
def load_account_movement_daily_data(s3_client, target_date=None):
    """
    Load and preprocess account movement daily data from S3.
    
    Args:
        s3_client: Boto3 S3 client
        target_date: Date to load data for (defaults to yesterday)
    
    Returns:
        DataFrame with preprocessed account movement data
    """
    if target_date is None:
        # Default to yesterday (latest available data)
        target_date = get_latest_data_date()
    
    date_info = get_file_date_info(target_date)
    # AccountMovementDaily uses YYYYMMDD format
    filename = f"{target_date.strftime('%Y%m%d')}-AccountMovementDaily-TBUK.csv"
    s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
    
    print(f"Loading account movement daily data from S3: {s3_key}")
    df = download_s3_file_cached(s3_client, s3_key)
    
    # Check if first row is diagnostic (contains summary/header info)
    # If so, skip it
    if len(df) > 0:
        first_row = df.iloc[0]
        # Check if first row has many NaN values or looks like a header
        if first_row.isna().sum() > len(df.columns) * 0.5:
            print("  Skipping diagnostic first row in AccountMovementDaily")
            df = df.iloc[1:].copy()
            df.reset_index(drop=True, inplace=True)
    
    # Ensure AccountId is numeric
    if 'AccountId' in df.columns:
        df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')
    
    # Convert Pending to numeric
    if 'Pending' in df.columns:
        df['Pending'] = pd.to_numeric(df['Pending'], errors='coerce').fillna(0)
    
    # Optimize data types for memory efficiency
    df = optimize_dtypes(df)
    
    return df


@timer_decorator
def load_users_data(s3_client, target_date=None):
    """
    Load and preprocess users data from S3.
    
    Args:
        s3_client: Boto3 S3 client
        target_date: Date to load data for (defaults to yesterday)
    
    Returns:
        DataFrame with preprocessed users data
    """
    if target_date is None:
        # Default to yesterday (latest available data)
        target_date = get_latest_data_date()
    
    date_info = get_file_date_info(target_date)
    filename = f"{date_info['file_prefix']}-Users-TBUK.csv"
    s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
    
    print(f"Loading users data from S3: {s3_key}")
    df = download_s3_file_cached(s3_client, s3_key)
    
    # Ensure AccountId is numeric
    if 'AccountId' in df.columns:
        df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')
    
    # UserId should be kept as string (will be prefixed with uk_)
    if 'UserId' in df.columns:
        df['UserId'] = df['UserId'].astype(str)
    
    # Clean up IsDeleted column
    if 'IsDeleted' in df.columns:
        df['IsDeleted'] = df['IsDeleted'].astype(str)
    
    # Optimize data types for memory efficiency
    df = optimize_dtypes(df)
    
    return df