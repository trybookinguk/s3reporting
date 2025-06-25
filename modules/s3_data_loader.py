"""
S3 data loading functionality for TryBooking tier calculation.
"""
import boto3
import pandas as pd
import os
import pickle
import json
from datetime import datetime
from .config import (
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET, UK_TZ, 
    CUTOFF_365, CUTOFF_730, EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
)


def get_s3_client():
    """Initialize S3 client with credentials."""
    # Check credentials when actually needed
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        raise ValueError("AWS credentials not found in environment variables")
    
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


def get_cache_path(key):
    """Generate cache file path for an S3 key."""
    # Create cache directory if it doesn't exist
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.cache')
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    
    # Replace slashes with underscores for filename
    cache_filename = key.replace('/', '_') + '.pkl'
    return os.path.join(cache_dir, cache_filename)


def get_cache_metadata_path(key):
    """Generate cache metadata file path for an S3 key."""
    cache_path = get_cache_path(key)
    return cache_path + '.meta'


def is_cache_valid(s3_client, key, cache_path):
    """Check if cached file is still valid by comparing timestamps."""
    meta_path = get_cache_metadata_path(key)
    
    # Check if cache and metadata exist
    if not os.path.exists(cache_path) or not os.path.exists(meta_path):
        return False
    
    try:
        # Read cached metadata
        with open(meta_path, 'r') as f:
            cache_meta = json.load(f)
        
        # Get S3 object metadata
        response = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        s3_last_modified = response['LastModified'].timestamp()
        s3_etag = response.get('ETag', '').strip('"')
        
        # Check if file has changed
        if cache_meta.get('last_modified') != s3_last_modified:
            print(f"  Cache outdated: S3 file modified")
            return False
        
        if cache_meta.get('etag') != s3_etag:
            print(f"  Cache outdated: S3 file ETag changed")
            return False
        
        # Check cache age (optional - expire after 7 days)
        cache_age_days = (datetime.now().timestamp() - cache_meta.get('cached_at', 0)) / 86400
        if cache_age_days > 7:
            print(f"  Cache outdated: Cached {cache_age_days:.1f} days ago")
            return False
        
        return True
    except Exception as e:
        print(f"  Error checking cache validity: {e}")
        return False


def save_cache_metadata(s3_client, key):
    """Save metadata about the cached file."""
    meta_path = get_cache_metadata_path(key)
    
    try:
        response = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        metadata = {
            'key': key,
            'last_modified': response['LastModified'].timestamp(),
            'etag': response.get('ETag', '').strip('"'),
            'size': response.get('ContentLength', 0),
            'cached_at': datetime.now().timestamp()
        }
        
        with open(meta_path, 'w') as f:
            json.dump(metadata, f)
    except Exception as e:
        print(f"  Warning: Could not save cache metadata: {e}")


def clear_cache():
    """Clear all cached files and metadata."""
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.cache')
    if os.path.exists(cache_dir):
        import shutil
        shutil.rmtree(cache_dir)
        print("Cache cleared.")


def download_s3_file_cached(s3_client, key, use_cache=True):
    """
    Download an S3 file with optional caching.
    Useful for scripts that just need a simple CSV download.
    
    Args:
        s3_client: Boto3 S3 client
        key: S3 key to download
        use_cache: Whether to use cache (default: True)
    
    Returns:
        pandas DataFrame
    """
    # Check if caching is disabled via environment
    if os.environ.get('NO_CACHE', '').lower() in ['1', 'true', 'yes']:
        use_cache = False
    
    if use_cache:
        cache_path = get_cache_path(key)
        if os.path.exists(cache_path) and is_cache_valid(s3_client, key, cache_path):
            print(f"Using cached file: {os.path.basename(cache_path)}")
            with open(cache_path, 'rb') as f:
                return pickle.load(f)
    
    # Download from S3
    print(f"Downloading from S3: {key}")
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    # Parse date columns if they exist
    date_cols = []
    if 'TransactionDate' in pd.read_csv(obj['Body'], nrows=0).columns:
        date_cols.append('TransactionDate')
    if 'EventDate' in pd.read_csv(obj['Body'], nrows=0).columns:
        date_cols.append('EventDate')
    
    # Reset stream position
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    df = pd.read_csv(obj['Body'], parse_dates=date_cols if date_cols else None)
    
    # Cache if enabled
    if use_cache:
        cache_path = get_cache_path(key)
        print(f"Caching to: {os.path.basename(cache_path)}")
        with open(cache_path, 'wb') as f:
            pickle.dump(df, f)
        # Save metadata
        save_cache_metadata(s3_client, key)
    
    return df


def process_booking_data_optimized(s3_client, key_all, key_month, use_cache=True):
    """
    Process booking data with true streaming aggregation.
    Only keeps aggregated metrics in memory, not raw transactions.
    
    Args:
        s3_client: Boto3 S3 client
        key_all: S3 key for all-time booking data
        key_month: S3 key for current month booking data
        use_cache: Whether to use cached files if available (default: True)
    """
    print("\nOptimized streaming processing for large files...")
    
    # Debug: Print cutoff dates
    print(f"  DEBUG: CUTOFF_365 = {CUTOFF_365}")
    print(f"  DEBUG: CUTOFF_730 = {CUTOFF_730}")
    print(f"  DEBUG: EVENT_FREQ_CUTOFF_CURRENT = {EVENT_FREQ_CUTOFF_CURRENT}")
    print(f"  DEBUG: EVENT_FREQ_CUTOFF_PREVIOUS = {EVENT_FREQ_CUTOFF_PREVIOUS}")
    
    # Check if caching is disabled via environment
    if os.environ.get('NO_CACHE', '').lower() in ['1', 'true', 'yes']:
        use_cache = False
        print("  Caching disabled by NO_CACHE environment variable")
    
    # Define data types to reduce memory usage
    dtypes = {
        'BookingTransactionId': 'int64',
        'AccountId': 'int32',
        'EventId': 'Int64',  # Nullable integer type - note lowercase 'd'
        'TicketQuantity': 'int16',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32'
    }
    
    # Store only aggregated metrics per account
    account_metrics = {}
    chunk_size = 100000  # Process 100k rows at a time
    
    for key in [key_all, key_month]:
        print(f"\nProcessing {key}...")
        file_size = fetch_s3_file_info(s3_client, key)
        print(f"  File size: {file_size / 1024 / 1024:.1f} MB")
        
        total_rows = 0
        chunks_iter = None
        
        if use_cache:
            cache_path = get_cache_path(key)
            if os.path.exists(cache_path) and is_cache_valid(s3_client, key, cache_path):
                print(f"  Using cached file: {os.path.basename(cache_path)}")
                # For cached files, read the pickle and create chunks
                with open(cache_path, 'rb') as f:
                    cached_df = pickle.load(f)
                
                # Create chunk iterator from cached dataframe
                def chunk_generator():
                    for start in range(0, len(cached_df), chunk_size):
                        yield cached_df.iloc[start:start + chunk_size].copy()
                
                chunks_iter = chunk_generator()
            else:
                # Download from S3 but also cache the data
                print(f"  Downloading from S3 and caching...")
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
                
                # Read all chunks and cache
                all_chunks = []
                for chunk in pd.read_csv(obj['Body'], chunksize=chunk_size,
                                       dtype=dtypes,
                                       parse_dates=['TransactionDate', 'EventDate'],
                                       low_memory=False):
                    all_chunks.append(chunk)
                
                # Save to cache
                full_df = pd.concat(all_chunks, ignore_index=True)
                print(f"  Saving to cache: {os.path.basename(cache_path)}")
                with open(cache_path, 'wb') as f:
                    pickle.dump(full_df, f)
                # Save metadata
                save_cache_metadata(s3_client, key)
                
                chunks_iter = iter(all_chunks)
        else:
            # No caching - direct streaming from S3
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            chunks_iter = pd.read_csv(obj['Body'], chunksize=chunk_size,
                                    dtype=dtypes,
                                    parse_dates=['TransactionDate', 'EventDate'],
                                    low_memory=False)
        
        # Process chunks
        for chunk_num, chunk in enumerate(chunks_iter):
            # Debug: Check columns on first chunk
            if chunk_num == 0:
                print(f"  DEBUG: Columns in data: {list(chunk.columns)}")
                print(f"  DEBUG: Has EventId: {'EventId' in chunk.columns}")
                print(f"  DEBUG: Has EventDate: {'EventDate' in chunk.columns}")
            
            # Ensure TransactionDate is datetime (already parsed from CSV with parse_dates)
            # Keep in UTC as per data source
            chunk['Revenue'] = chunk['BookingFee'] + chunk['CardFee'] + chunk['ProcessingFee'] + chunk['TicketFee']
            chunk['Year'] = chunk['TransactionDate'].dt.year
            
            # Drop duplicates within chunk
            chunk = chunk.drop_duplicates(subset='BookingTransactionId')
            
            # Aggregate by account
            for account_id, group in chunk.groupby('AccountId'):
                if account_id not in account_metrics:
                    account_metrics[account_id] = {
                        # Pre-aggregated metrics instead of raw transactions
                        'tickets_current': 0,
                        'revenue_current': 0.0,
                        'tickets_prev': 0,
                        'revenue_prev': 0.0,
                        'tickets_lifetime': 0,
                        'revenue_lifetime': 0.0,
                        'years': set(),
                        'years_pre_cutoff': set(),
                        'seen_tx_ids': set(),
                        'event_months_current': set(),  # (year, month) tuples for current period (tier calculation)
                        'event_months_previous': set(),  # (year, month) tuples for previous period (tier calculation)
                        'event_months_freq_current': set(),  # (year, month) tuples for current period (frequency calculation)
                        'event_months_freq_previous': set(),  # (year, month) tuples for previous period (frequency calculation)
                        'event_creation_info': {},  # Keep for lead time calculations
                        'last_booking_date': None,
                        'first_booking_date': None
                    }
                
                metrics = account_metrics[account_id]
                
                # Filter out already seen transactions
                new_tx_mask = ~group['BookingTransactionId'].isin(metrics['seen_tx_ids'])
                new_transactions = group[new_tx_mask]
                
                if len(new_transactions) > 0:
                    # Update seen transaction IDs
                    metrics['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
                    
                    # Aggregate metrics instead of storing raw data
                    for _, tx in new_transactions.iterrows():
                        tx_date = tx['TransactionDate'].date()
                        
                        # Update lifetime metrics
                        metrics['tickets_lifetime'] += tx['TicketQuantity']
                        metrics['revenue_lifetime'] += tx['Revenue']
                        metrics['years'].add(tx['Year'])
                        
                        # Update period-specific metrics
                        if tx_date >= CUTOFF_365:
                            metrics['tickets_current'] += tx['TicketQuantity']
                            metrics['revenue_current'] += tx['Revenue']
                        elif tx_date >= CUTOFF_730:
                            metrics['tickets_prev'] += tx['TicketQuantity']
                            metrics['revenue_prev'] += tx['Revenue']
                        
                        if tx_date < CUTOFF_365:
                            metrics['years_pre_cutoff'].add(tx['Year'])
                        
                        # Update booking dates
                        if metrics['last_booking_date'] is None or tx['TransactionDate'] > metrics['last_booking_date']:
                            metrics['last_booking_date'] = tx['TransactionDate']
                        if metrics['first_booking_date'] is None or tx['TransactionDate'] < metrics['first_booking_date']:
                            metrics['first_booking_date'] = tx['TransactionDate']
                    
                    # Debug columns for first few accounts
                    if chunk_num == 0 and account_id in list(account_metrics.keys())[:1]:
                        print(f"    DEBUG: new_transactions columns: {list(new_transactions.columns)}")
                    
                    # Process event data if EventId and EventDate columns exist
                    if 'EventId' in new_transactions.columns and 'EventDate' in new_transactions.columns:
                        event_data = new_transactions[['EventId', 'TransactionDate', 'EventDate']].copy()
                        # Filter out rows without EventDate
                        event_data = event_data[pd.notna(event_data['EventDate'])]
                        
                        # Debug: Check if we have event data
                        if chunk_num == 0 and account_id in list(account_metrics.keys())[:3]:
                            print(f"    DEBUG: Account {account_id} - event_data rows: {len(event_data)}")
                            if len(event_data) > 0:
                                print(f"    DEBUG: First EventDate: {event_data['EventDate'].iloc[0]}")
                                print(f"    DEBUG: EventDate type: {type(event_data['EventDate'].iloc[0])}")
                        
                        if len(event_data) > 0:
                            # EventDate is already parsed as datetime from CSV
                            
                            # Extract year-month tuples from EventDate for frequency analysis
                            event_data['event_year_month'] = event_data['EventDate'].apply(
                                lambda x: (x.year, x.month)
                            )
                            
                            # Classify into current/previous based on TransactionDate
                            # For tier calculations (rolling window)
                            current_mask = event_data['TransactionDate'].dt.date >= CUTOFF_365
                            previous_mask = (event_data['TransactionDate'].dt.date >= CUTOFF_730) & (~current_mask)
                            
                            # Update month sets for tier calculations
                            current_months = set(event_data[current_mask]['event_year_month'].unique())
                            previous_months = set(event_data[previous_mask]['event_year_month'].unique())
                            
                            account_metrics[account_id]['event_months_current'].update(current_months)
                            account_metrics[account_id]['event_months_previous'].update(previous_months)
                            
                            # For event frequency calculations (month boundary)
                            freq_current_mask = event_data['TransactionDate'].dt.date >= EVENT_FREQ_CUTOFF_CURRENT
                            freq_previous_mask = (event_data['TransactionDate'].dt.date >= EVENT_FREQ_CUTOFF_PREVIOUS) & (~freq_current_mask)
                            
                            # Update month sets for frequency calculations
                            freq_current_months = set(event_data[freq_current_mask]['event_year_month'].unique())
                            freq_previous_months = set(event_data[freq_previous_mask]['event_year_month'].unique())
                            
                            account_metrics[account_id]['event_months_freq_current'].update(freq_current_months)
                            account_metrics[account_id]['event_months_freq_previous'].update(freq_previous_months)
                            
                            # Also track event creation info for lead time calculations
                            event_groups = event_data[pd.notna(event_data['EventId'])].groupby('EventId')
                            
                            for event_id, group in event_groups:
                                event_id_key = int(event_id) if pd.notna(event_id) else None
                                if event_id_key and event_id_key not in account_metrics[account_id]['event_creation_info']:
                                    first_booking = group['TransactionDate'].min()
                                    event_date = group['EventDate'].iloc[0]
                                    lead_days = (event_date.date() - first_booking.date()).days
                                    account_metrics[account_id]['event_creation_info'][event_id_key] = {
                                        'first_booking': first_booking,
                                        'event_date': event_date,
                                        'lead_days': max(lead_days, 0)
                                    }
            
            total_rows += len(chunk)
            if chunk_num % 10 == 0:
                print(f"  Processed {total_rows:,} rows, tracking {len(account_metrics):,} accounts...")
        
        print(f"  Total rows processed: {total_rows:,}")
    
    # Convert sets to counts and prepare data for tier calculator
    print("\nFinalizing metrics...")
    for account_id, metrics in account_metrics.items():
        # Calculate derived metrics
        metrics['years_loyalty'] = len(metrics['years'])
        metrics['years_loyalty_prev'] = len(metrics['years_pre_cutoff'])
        
        # Calculate average revenue per year
        if metrics['years_loyalty'] > 0:
            metrics['avg_revenue_per_year'] = metrics['revenue_lifetime'] / metrics['years_loyalty']
        else:
            metrics['avg_revenue_per_year'] = 0
            
        if metrics['years_loyalty_prev'] > 0:
            # Revenue up to previous period
            revenue_up_to_prev = metrics['revenue_lifetime'] - metrics['revenue_current']
            metrics['avg_revenue_prev'] = revenue_up_to_prev / metrics['years_loyalty_prev']
        else:
            metrics['avg_revenue_prev'] = 0
        
        # Clean up temporary fields (keep for event tracking)
        metrics['seen_tx_ids'] = len(metrics['seen_tx_ids'])  # Just keep count
        
        # Debug: sample month tracking
        if account_id in list(account_metrics.keys())[:3]:
            curr_months = len(metrics.get('event_months_current', set()))
            prev_months = len(metrics.get('event_months_previous', set()))
            print(f"  Account {account_id}: {curr_months} active months (current), {prev_months} active months (previous)")
    
    return account_metrics