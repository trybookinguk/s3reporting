"""
S3 data loading functionality for TryBooking tier calculation.
"""
import boto3
import pandas as pd
from datetime import datetime
from .config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET, UK_TZ, CUTOFF_365, CUTOFF_730


def get_s3_client():
    """Initialize S3 client with credentials."""
    return boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )


def fetch_s3_file_info(s3_client, key):
    """Get file size without downloading."""
    try:
        response = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        return response['ContentLength']
    except Exception:
        return 0


def process_booking_data_optimized(s3_client, key_all, key_month):
    """Process booking data using chunked reading and optimized memory usage."""
    print("\nOptimized processing for large files...")
    
    # Define data types to reduce memory usage
    dtypes = {
        'BookingTransactionId': 'int64',
        'AccountId': 'int32',
        'EventId': 'Int64',  # Nullable integer type
        'TicketQuantity': 'int16',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32'
    }
    
    # Process in chunks and aggregate by account
    account_metrics = {}
    chunk_size = 100000  # Process 100k rows at a time
    
    for key in [key_all, key_month]:
        print(f"\nProcessing {key}...")
        file_size = fetch_s3_file_info(s3_client, key)
        print(f"  File size: {file_size / 1024 / 1024:.1f} MB")
        
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        # First, peek at the columns to verify structure
        first_chunk = pd.read_csv(obj['Body'], nrows=5)
        available_columns = list(first_chunk.columns)
        print(f"  Sample columns: {available_columns[:10]}...")
        print(f"  Total columns: {len(available_columns)}")
        
        # Only use dtypes for columns that exist
        actual_dtypes = {col: dtype for col, dtype in dtypes.items() if col in available_columns}
        print(f"  Using dtypes for: {list(actual_dtypes.keys())}")
        
        # Re-fetch the object for actual processing
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        total_rows = 0
        for chunk_num, chunk in enumerate(pd.read_csv(obj['Body'], chunksize=chunk_size, 
                                                      dtype=actual_dtypes, 
                                                      parse_dates=['TransactionDate'], 
                                                      low_memory=False)):
            # Add timezone info
            chunk['TransactionDate'] = pd.to_datetime(chunk['TransactionDate'], utc=True).dt.tz_convert(UK_TZ)
            chunk['Revenue'] = chunk['BookingFee'] + chunk['CardFee'] + chunk['ProcessingFee'] + chunk['TicketFee']
            chunk['Year'] = chunk['TransactionDate'].dt.year
            
            # Drop duplicates within chunk
            chunk = chunk.drop_duplicates(subset='BookingTransactionId')
            
            # Aggregate by account
            for account_id, group in chunk.groupby('AccountId'):
                if account_id not in account_metrics:
                    account_metrics[account_id] = {
                        'transactions': [],
                        'seen_tx_ids': set(),
                        'event_ids_current': set(),
                        'event_ids_previous': set(),
                        'event_creation_info': {},
                        'last_booking_date': None
                    }
                
                # Filter out already seen transactions
                new_transactions = group[~group['BookingTransactionId'].isin(account_metrics[account_id]['seen_tx_ids'])]
                
                if len(new_transactions) > 0:
                    # Store only essential columns to save memory
                    essential_cols = ['TransactionDate', 'Revenue', 'TicketQuantity', 'Year', 'BookingTransactionId']
                    if 'EventId' in new_transactions.columns:
                        essential_cols.append('EventId')
                    if 'EventDate' in new_transactions.columns:
                        essential_cols.append('EventDate')
                    
                    essential_data = new_transactions[essential_cols].copy()
                    account_metrics[account_id]['transactions'].append(essential_data)
                    account_metrics[account_id]['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
                    
                    # Update last booking date
                    last_booking = new_transactions['TransactionDate'].max()
                    if account_metrics[account_id]['last_booking_date'] is None or last_booking > account_metrics[account_id]['last_booking_date']:
                        account_metrics[account_id]['last_booking_date'] = last_booking
                    
                    # Process events if EventId column exists
                    if 'EventId' in new_transactions.columns and 'EventDate' in new_transactions.columns:
                        event_data = new_transactions[['EventId', 'TransactionDate', 'EventDate']].copy()
                        event_data = event_data[pd.notna(event_data['EventId'])]
                        
                        if len(event_data) > 0:
                            # Vectorized period classification
                            current_mask = event_data['TransactionDate'].dt.date >= CUTOFF_365
                            previous_mask = (event_data['TransactionDate'].dt.date >= CUTOFF_730) & (~current_mask)
                            
                            # Update event sets
                            current_events = event_data[current_mask]['EventId'].dropna().astype(int).unique()
                            previous_events = event_data[previous_mask]['EventId'].dropna().astype(int).unique()
                            account_metrics[account_id]['event_ids_current'].update(current_events)
                            account_metrics[account_id]['event_ids_previous'].update(previous_events)
                            
                            # Group by EventId to find first booking per event
                            event_groups = event_data[pd.notna(event_data['EventDate'])].groupby('EventId')
                            
                            for event_id, group in event_groups:
                                event_id_key = int(event_id) if pd.notna(event_id) else None
                                if event_id_key and event_id_key not in account_metrics[account_id]['event_creation_info']:
                                    first_booking = group['TransactionDate'].min()
                                    event_date = group['EventDate'].iloc[0]
                                    lead_days = (pd.to_datetime(event_date).date() - first_booking.date()).days
                                    account_metrics[account_id]['event_creation_info'][event_id_key] = {
                                        'first_booking': first_booking,
                                        'event_date': pd.to_datetime(event_date),
                                        'lead_days': max(lead_days, 0)
                                    }
            
            total_rows += len(chunk)
            if chunk_num % 10 == 0:
                print(f"  Processed {total_rows:,} rows...")
        
        print(f"  Total rows processed: {total_rows:,}")
        
        # Debug: sample event tracking
        if len(account_metrics) > 0:
            sample_accounts = list(account_metrics.keys())[:3]
            print("\n  Sample event tracking:")
            for acc_id in sample_accounts:
                curr_events = len(account_metrics[acc_id].get('event_ids_current', set()))
                prev_events = len(account_metrics[acc_id].get('event_ids_previous', set()))
                print(f"    Account {acc_id}: {curr_events} current events, {prev_events} previous events")
    
    return account_metrics