"""
S3 data loading functionality for TryBooking reporting.

This module handles:
- S3 file downloads with caching
- Optimized data types for memory efficiency  
- Chunked reading for large files

Usage:
    from modules.utils.s3_data_loader import get_s3_client, load_booking_data_chunks
    from modules.booking_aggregator import BookingAggregator
    
    # Load and process booking data
    s3_client = get_s3_client()
    aggregator = BookingAggregator(...)
    
    for chunk in load_booking_data_chunks(s3_client, 's3_key.csv'):
        aggregator.process_chunk(chunk)
    
    metrics = aggregator.finalize_metrics()
"""
import boto3
import pandas as pd
import os
import pickle
import json
import logging
from datetime import datetime
from .config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET, UK_TZ

logger = logging.getLogger(__name__)

# Try to import psutil for memory monitoring (optional)
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


def get_optimized_dtypes():
    """
    Return optimized data types for booking data columns.
    These types reduce memory usage by 30-40% compared to default pandas types.
    """
    return {
        # Use float32 for currency values (sufficient precision for amounts)
        'PaymentReceived': 'float32',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32',
        'Surcharge': 'float32',
        'ProcessingFeeSurcharge': 'float32',
        
        # Use int32 for IDs and quantities (handles up to 2 billion)
        'AccountId': 'int32',
        'TicketQuantity': 'int32',
        
        # Use nullable Int64 for optional IDs
        'EventId': 'Int64',
        'DonationCampaignId': 'Int64',
        'CustomerId': 'Int64',
        'GiftCertificateId': 'Int64',
        
        # Keep int64 for transaction IDs (may need larger range)
        'BookingTransactionId': 'int64',
        'BookingId': 'int64'
    }


def get_categorical_columns():
    """
    Return columns that should be treated as categorical for memory optimization.
    These are typically low-cardinality string columns.
    """
    return [
        'Industry', 'SubIndustry', 'TransactionType', 'PaymentType',
        'Status', 'GatewayName', 'IPCountry', 'BookingCountryCode',
        'Wallet', 'GiftCertificateTypeName', 'DGRStatus', 'Gateway Group'
    ]


def log_memory_usage(context=""):
    """Log current memory usage of the process."""
    if not HAS_PSUTIL:
        return
    
    try:
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        if context:
            print(f"  Memory usage ({context}): {memory_mb:.1f} MB")
        else:
            print(f"  Memory usage: {memory_mb:.1f} MB")
    except Exception:
        # Skip if psutil fails
        pass


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
        cache_age_days = (datetime.now(UK_TZ).timestamp() - cache_meta.get('cached_at', 0)) / 86400
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
            'cached_at': datetime.now(UK_TZ).timestamp()
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
    
    # Get file size for progress logging
    file_size = fetch_s3_file_info(s3_client, key)
    print(f"Downloading from S3: {key}")
    print(f"  File size: {file_size / 1024 / 1024:.1f} MB")
    
    # Download from S3
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    
    # Get optimized data types
    dtypes = get_optimized_dtypes()
    
    # Read CSV in chunks to avoid double reading
    print("  Reading CSV data...")
    chunks = []
    date_cols = None
    chunk_size = 100000
    
    for i, chunk in enumerate(pd.read_csv(obj['Body'], 
                                         chunksize=chunk_size,
                                         low_memory=False)):
        # On first chunk, identify date columns and categorical columns
        if i == 0:
            date_cols = []
            if 'TransactionDate' in chunk.columns:
                date_cols.append('TransactionDate')
            if 'EventDate' in chunk.columns:
                date_cols.append('EventDate')
            if 'DateTimeCreated' in chunk.columns:
                date_cols.append('DateTimeCreated')
            
            # Identify columns that exist in the data
            existing_dtypes = {k: v for k, v in dtypes.items() if k in chunk.columns}
            
            # Get categorical columns
            categorical_cols = get_categorical_columns()
            
            # Convert string columns to category if they have low cardinality
            for col in categorical_cols:
                if col in chunk.columns:
                    # Check cardinality on first chunk
                    if chunk[col].nunique() < 100:
                        existing_dtypes[col] = 'category'
        
        # Apply dtypes to chunk
        for col, dtype in existing_dtypes.items():
            if col in chunk.columns:
                try:
                    if dtype == 'category':
                        chunk[col] = chunk[col].astype('category')
                    else:
                        chunk[col] = chunk[col].astype(dtype)
                except Exception:
                    # Skip if conversion fails
                    pass
        
        # Parse date columns
        if date_cols:
            for col in date_cols:
                if col in chunk.columns:
                    chunk[col] = pd.to_datetime(chunk[col])
        
        chunks.append(chunk)
        
        if (i + 1) % 10 == 0:
            print(f"  Processed {(i + 1) * chunk_size:,} rows...")
    
    # Combine all chunks - filter out empty chunks to avoid FutureWarning
    print("  Combining chunks...")
    non_empty_chunks = [chunk for chunk in chunks if not chunk.empty]
    if non_empty_chunks:
        df = pd.concat(non_empty_chunks, ignore_index=True)
    else:
        # Handle edge case where all chunks are empty
        df = pd.DataFrame()
    print(f"  Total rows loaded: {len(df):,}")
    
    # Log memory usage
    log_memory_usage("after loading")
    
    # Cache if enabled
    if use_cache:
        cache_path = get_cache_path(key)
        print(f"  Caching to: {os.path.basename(cache_path)}")
        with open(cache_path, 'wb') as f:
            pickle.dump(df, f)
        # Save metadata
        save_cache_metadata(s3_client, key)
    
    return df


def load_booking_data_chunks(s3_client, key, use_cache=True, chunk_size=100000):
    """
    Load booking data from S3 and yield chunks.
    
    This function handles all the data loading concerns including:
    - S3 download
    - Caching
    - Data type optimization
    - Chunking for memory efficiency
    
    Args:
        s3_client: Boto3 S3 client
        key: S3 key for the CSV file
        use_cache: Whether to use cached files if available (default: True)
        chunk_size: Number of rows per chunk (default: 100,000)
        
    Yields:
        pd.DataFrame: Chunks of booking data with optimized types
    """
    # Check if caching is disabled via environment
    if os.environ.get('NO_CACHE', '').lower() in ['1', 'true', 'yes']:
        use_cache = False
        logger.info("Caching disabled by NO_CACHE environment variable")
    
    # Get optimized data types to reduce memory usage
    dtypes = get_optimized_dtypes()
    
    # Get categorical columns
    categorical_cols = get_categorical_columns()
    
    logger.info(f"Loading data from {key}...")
    file_size = fetch_s3_file_info(s3_client, key)
    logger.info(f"File size: {file_size / 1024 / 1024:.1f} MB")
    print(f"\nLoading {key}...")
    print(f"  File size: {file_size / 1024 / 1024:.1f} MB")
    
    if use_cache:
        cache_path = get_cache_path(key)
        if os.path.exists(cache_path) and is_cache_valid(s3_client, key, cache_path):
            logger.info(f"Using cached file: {os.path.basename(cache_path)}")
            print(f"  Using cached file: {os.path.basename(cache_path)}")
            # For cached files, read the pickle and create chunks
            with open(cache_path, 'rb') as f:
                cached_df = pickle.load(f)
            
            # Yield chunks from cached dataframe
            for start in range(0, len(cached_df), chunk_size):
                yield cached_df.iloc[start:start + chunk_size].copy()
            return
        else:
            # Download from S3 but also cache the data
            logger.info(f"Downloading {key} from S3 and caching")
            print(f"  Downloading from S3 and caching...")
            
            # Read all chunks and cache
            all_chunks = []
            
            # First pass: read headers to identify columns
            header_obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            header_df = pd.read_csv(header_obj['Body'], nrows=0)
            
            # Identify which dtypes to use based on available columns
            existing_dtypes = {k: v for k, v in dtypes.items() if k in header_df.columns}
            
            # Identify categorical columns that exist
            existing_categorical = [col for col in categorical_cols if col in header_df.columns]
            
            # Identify date columns
            date_cols = []
            for col in ['TransactionDate', 'EventDate', 'DateTimeCreated']:
                if col in header_df.columns:
                    date_cols.append(col)
            
            # Get fresh object for reading
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            
            chunk_count = 0
            for chunk in pd.read_csv(obj['Body'], chunksize=chunk_size,
                                   dtype=existing_dtypes,
                                   parse_dates=date_cols,
                                   low_memory=False):
                # Convert categorical columns
                for col in existing_categorical:
                    if col in chunk.columns:
                        chunk[col] = chunk[col].astype('category')
                
                all_chunks.append(chunk)
                chunk_count += 1
                
                if chunk_count % 10 == 0:
                    print(f"    Read {chunk_count * chunk_size:,} rows...")
            
            # Save to cache - filter out empty chunks and all-NA chunks to avoid FutureWarning
            non_empty_chunks = [
                chunk for chunk in all_chunks 
                if not chunk.empty and not chunk.isna().all().all()
            ]
            if non_empty_chunks:
                full_df = pd.concat(non_empty_chunks, ignore_index=True)
            else:
                # Handle edge case where all chunks are empty
                full_df = pd.DataFrame()
            print(f"  Saving to cache: {os.path.basename(cache_path)} ({len(full_df):,} rows)")
            with open(cache_path, 'wb') as f:
                pickle.dump(full_df, f)
            # Save metadata
            save_cache_metadata(s3_client, key)
            
            # Yield the chunks we already loaded
            for chunk in all_chunks:
                yield chunk
    else:
        # No caching - direct streaming from S3
        print(f"  Streaming directly from S3 (no caching)...")
        
        # First pass: read headers to identify columns
        header_obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        header_df = pd.read_csv(header_obj['Body'], nrows=0)
        
        # Identify which dtypes to use based on available columns
        existing_dtypes = {k: v for k, v in dtypes.items() if k in header_df.columns}
        
        # Identify categorical columns that exist
        existing_categorical = [col for col in categorical_cols if col in header_df.columns]
        
        # Identify date columns
        date_cols = []
        for col in ['TransactionDate', 'EventDate', 'DateTimeCreated']:
            if col in header_df.columns:
                date_cols.append(col)
        
        # Get fresh object for streaming
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        # Stream chunks directly
        for chunk in pd.read_csv(obj['Body'], chunksize=chunk_size,
                               dtype=existing_dtypes,
                               parse_dates=date_cols,
                               low_memory=False):
            # Convert categorical columns
            for col in existing_categorical:
                if col in chunk.columns:
                    chunk[col] = chunk[col].astype('category')
            
            yield chunk


def load_multiple_booking_files(s3_client, keys, use_cache=True, chunk_size=100000):
    """
    Load multiple booking files and yield chunks from all of them.
    
    This is a convenience function for loading multiple S3 files in sequence,
    commonly used when processing both historical and current month data.
    
    Args:
        s3_client: Boto3 S3 client
        keys: List of S3 keys to load
        use_cache: Whether to use cached files if available (default: True)
        chunk_size: Number of rows per chunk (default: 100,000)
        
    Yields:
        pd.DataFrame: Chunks from all files in sequence
    """
    for key in keys:
        if key:  # Skip None/empty keys
            logger.info(f"Loading chunks from {key}")
            try:
                for chunk in load_booking_data_chunks(s3_client, key, use_cache, chunk_size):
                    yield chunk
            except Exception as e:
                # If BookingDataAll fails, try to find alternative files
                if 'BookingDataAll' in key and 'NoSuchKey' in str(e):
                    logger.info(f"Primary BookingDataAll file not found, searching for alternatives...")
                    
                    # Extract year/month from the key
                    parts = key.split('/')
                    if len(parts) >= 3:
                        year, month = parts[0], parts[1]
                        prefix = f"{year}/{month}/"
                        
                        try:
                            # List all objects in the month folder
                            response = s3_client.list_objects_v2(
                                Bucket=S3_BUCKET,
                                Prefix=prefix
                            )
                            
                            # Find all BookingDataAll files in the month
                            booking_data_all_files = []
                            if 'Contents' in response:
                                for obj in response['Contents']:
                                    obj_key = obj['Key']
                                    if 'BookingDataAll-TBUK.csv' in obj_key:
                                        booking_data_all_files.append(obj_key)
                            
                            if booking_data_all_files:
                                # Sort to get the earliest available file
                                booking_data_all_files.sort()
                                alternative_key = booking_data_all_files[0]
                                logger.info(f"Found alternative BookingDataAll: {alternative_key}")
                                
                                # Extract the day from filename
                                filename_part = alternative_key.split('/')[-1]
                                if len(filename_part) >= 8:
                                    day_part = filename_part[6:8]
                                    logger.info(f"Note: Expected file on day 01 but using file from day {day_part}")
                                
                                # Try loading the alternative file
                                for chunk in load_booking_data_chunks(s3_client, alternative_key, use_cache, chunk_size):
                                    yield chunk
                            else:
                                logger.warning(f"No BookingDataAll files found in {prefix}")
                                logger.warning("Skipping BookingDataAll - will only process current month data")
                        except Exception as list_error:
                            logger.error(f"Error searching for alternatives: {list_error}")
                            logger.warning("Skipping BookingDataAll - will only process current month data")
                else:
                    # For other errors or non-BookingDataAll files, re-raise
                    raise
