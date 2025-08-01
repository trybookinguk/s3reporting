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
    df = download_s3_file_cached(s3_client, s3_key)
    
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