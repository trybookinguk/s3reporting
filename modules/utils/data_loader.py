"""
Unified data loading module for TryBooking reporting.

This module combines all data loading functionality:
- S3 file operations with caching
- Memory-optimized chunked reading
- Business logic for specific data types (accounts, bookings, balance, users)
- Automatic fallback for missing BookingDataAll files

Usage:
    from modules.utils.unified_data_loader import UnifiedDataLoader
    
    loader = UnifiedDataLoader()
    
    # Load full DataFrames
    accounts_df = loader.load_accounts(target_date)
    booking_df = loader.load_booking_data(target_date, data_type='BookingDataAll')
    
    # Load chunks for large data processing
    for chunk in loader.load_booking_chunks(s3_key):
        process(chunk)
"""
import boto3
import pandas as pd
import os
import pickle
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Iterator, Optional, Tuple
import pytz

# Import configurations
from .config import (
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET, UK_TZ,
    CUTOFF_365, CUTOFF_730, EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
)
from .date_utils import get_file_date_info, get_latest_data_date
from .performance import optimize_dtypes, timer_decorator

logger = logging.getLogger(__name__)

# Try to import psutil for memory monitoring (optional)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


# ========== Fallback Helper Functions ==========

def calculate_previous_month(year: int, month: int) -> Tuple[int, int]:
    """Calculate the previous month from given year and month."""
    if month == 1:
        return year - 1, 12
    else:
        return year, month - 1


def find_booking_files_in_month(s3_client, bucket: str, year: int, month: int,
                                require_non_empty: bool = False) -> Tuple[List[str], List[str]]:
    """Find BookingDataAll and BookingData files in a specific month.

    Args:
        require_non_empty: If True, exclude files with zero ContentLength. Used by the
            fallback walk-back so empty CSVs (which fail to parse) don't terminate the search.
    """
    prefix = f"{year:04d}/{month:02d}/"

    try:
        response = s3_client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix
        )

        booking_data_all_files = []
        booking_data_files = []

        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                if require_non_empty and obj.get('Size', 0) == 0:
                    continue
                if 'BookingDataAll-TBUK.csv' in key:
                    booking_data_all_files.append(key)
                elif 'BookingData-TBUK.csv' in key and 'BookingDataAll' not in key:
                    booking_data_files.append(key)

        booking_data_all_files.sort()
        booking_data_files.sort()

        return booking_data_all_files, booking_data_files

    except Exception as e:
        logger.error(f"Error listing files in {prefix}: {e}")
        return [], []


MAX_FALLBACK_MONTHS = 6


def get_fallback_keys(s3_client, bucket: str, current_year: int, current_month: int) -> Tuple[Optional[str], List[str]]:
    """
    Get S3 keys for fallback data when BookingDataAll is missing or empty.

    Walks back up to MAX_FALLBACK_MONTHS months from (current_year, current_month) looking
    for a BookingDataAll file. Once found, collects the latest BookingData file from each
    intermediate month (between the BookingDataAll month and the original request month,
    inclusive) so the combined dataset covers everything up to the present.

    Args:
        current_year: Year of the folder where we originally looked for BookingDataAll
        current_month: Month of the folder where we originally looked for BookingDataAll

    Returns:
        Tuple of (BookingDataAll key, list of BookingData keys ordered oldest-first).
        Either may be None/empty if nothing was found within the lookback window.

    Example:
        If looking at 2025/08 and BookingDataAll is missing in 2025/08, 2025/07, and 2025/06,
        but present in 2025/05:
        - Returns 2025/05/BookingDataAll
        - Returns [2025/06/BookingData, 2025/07/BookingData, 2025/08/BookingData]
    """
    booking_all_key: Optional[str] = None
    booking_all_year_month: Optional[Tuple[int, int]] = None

    # Walk back month-by-month until we find a non-empty BookingDataAll file or hit the
    # lookback cap. We filter on Size > 0 here because empty CSVs (the bug we're working
    # around) still appear in S3 listings — without this filter the loop would "find" them
    # and stop searching.
    search_year, search_month = calculate_previous_month(current_year, current_month)
    for step in range(MAX_FALLBACK_MONTHS):
        logger.info(f"Searching for BookingDataAll in {search_year:04d}/{search_month:02d}/ "
                    f"(step {step + 1}/{MAX_FALLBACK_MONTHS})")
        booking_all_files, _ = find_booking_files_in_month(
            s3_client, bucket, search_year, search_month, require_non_empty=True
        )
        if booking_all_files:
            booking_all_key = booking_all_files[-1]  # Newest in that month
            booking_all_year_month = (search_year, search_month)
            logger.info(f"Found BookingDataAll: {booking_all_key}")
            break
        search_year, search_month = calculate_previous_month(search_year, search_month)

    if booking_all_key is None:
        logger.warning(f"No BookingDataAll found within {MAX_FALLBACK_MONTHS} months of "
                       f"{current_year:04d}/{current_month:02d}/")

    # Collect the latest BookingData file from each month after the BookingDataAll month,
    # up to and including the original request month
    booking_data_keys: List[str] = []
    if booking_all_year_month is not None:
        start_year, start_month = calculate_next_month(*booking_all_year_month)
    else:
        # No BookingDataAll found — still try to gather BookingData for the original request
        # month so callers can at least surface partial data.
        start_year, start_month = current_year, current_month

    cursor_year, cursor_month = start_year, start_month
    while (cursor_year, cursor_month) <= (current_year, current_month):
        _, booking_data_files = find_booking_files_in_month(
            s3_client, bucket, cursor_year, cursor_month
        )
        if booking_data_files:
            latest = booking_data_files[-1]  # Latest snapshot in the month
            booking_data_keys.append(latest)
            logger.info(f"Found BookingData for {cursor_year:04d}/{cursor_month:02d}/: {latest}")
        else:
            logger.info(f"No BookingData found in {cursor_year:04d}/{cursor_month:02d}/")
        cursor_year, cursor_month = calculate_next_month(cursor_year, cursor_month)

    if booking_all_key is None and not booking_data_keys:
        logger.warning("No fallback data found")

    return booking_all_key, booking_data_keys


def calculate_next_month(year: int, month: int) -> Tuple[int, int]:
    """Calculate the next month from given year and month."""
    if month == 12:
        return year + 1, 1
    else:
        return year, month + 1


def try_load_with_fallback(s3_client, bucket: str, primary_key: str, 
                          load_func, current_year: int, current_month: int) -> pd.DataFrame:
    """Try to load BookingDataAll with automatic fallback to previous month if needed."""
    # First try to load the primary key
    df = None
    try:
        df = load_func(s3_client, primary_key)
        if df is not None and not df.empty:
            return df
    except pd.errors.EmptyDataError:
        pass  # Treat empty files the same as missing files — fall through to fallback
    except Exception as e:
        error_str = str(e)
        if 'NoSuchKey' not in error_str and '404' not in error_str and 'Not Found' not in error_str:
            raise
    
    # If we get here, primary file is missing or empty
    logger.warning(f"BookingDataAll is missing or empty in folder {current_year:04d}/{current_month:02d}/")
    logger.info("Attempting fallback using older data...")

    # Get fallback keys (one BookingDataAll + N BookingData files going forward)
    prev_all_key, booking_data_keys = get_fallback_keys(
        s3_client, bucket, current_year, current_month
    )

    # Load fallback data
    dfs_to_combine = []

    if prev_all_key:
        try:
            prev_all_df = load_func(s3_client, prev_all_key)
            if prev_all_df is not None and not prev_all_df.empty:
                dfs_to_combine.append(prev_all_df)
                logger.info(f"Loaded {len(prev_all_df):,} records from BookingDataAll ({prev_all_key})")
        except Exception as e:
            logger.error(f"Failed to load BookingDataAll ({prev_all_key}): {e}")

    for data_key in booking_data_keys:
        try:
            data_df = load_func(s3_client, data_key)
            if data_df is not None and not data_df.empty:
                dfs_to_combine.append(data_df)
                logger.info(f"Loaded {len(data_df):,} records from BookingData ({data_key})")
        except Exception as e:
            logger.error(f"Failed to load BookingData ({data_key}): {e}")

    # Combine and deduplicate
    if dfs_to_combine:
        logger.info(f"Combining {len(dfs_to_combine)} fallback data sources...")
        combined_df = pd.concat(dfs_to_combine, ignore_index=True)
        
        # Remove duplicates if BookingTransactionId exists (primary key for bookings)
        if 'BookingTransactionId' in combined_df.columns:
            initial_count = len(combined_df)
            combined_df = combined_df.drop_duplicates(subset=['BookingTransactionId'], keep='last')
            duplicates_removed = initial_count - len(combined_df)
            if duplicates_removed > 0:
                logger.info(f"Removed {duplicates_removed:,} duplicate transactions")
        
        logger.warning(f"⚠️  Using fallback data combination")
        logger.info(f"Successfully created fallback dataset ({len(combined_df):,} total records)")
        
        return combined_df
    else:
        raise ValueError(f"Unable to load BookingDataAll for current month or fallback from previous month")


def yield_chunks_with_fallback(s3_client, bucket: str, primary_key: str,
                              load_chunks_func, current_year: int, current_month: int,
                              chunk_size: int = 100000) -> Iterator[pd.DataFrame]:
    """
    Yield chunks of BookingDataAll with automatic fallback if needed.

    Fallback strategy: If BookingDataAll is missing, walks back up to MAX_FALLBACK_MONTHS
    months to find a populated BookingDataAll, then forward-fills with the latest
    BookingData snapshot from each intermediate month up to the original request month.
    """
    # First try to load chunks from the primary key
    found_primary = False
    try:
        for chunk in load_chunks_func(s3_client, primary_key, chunk_size):
            found_primary = True
            yield chunk
        if found_primary:
            return  # Successfully loaded primary file
    except pd.errors.EmptyDataError:
        pass  # Treat empty files the same as missing files — fall through to fallback
    except Exception as e:
        error_str = str(e)
        if 'NoSuchKey' not in error_str and '404' not in error_str and 'Not Found' not in error_str:
            raise

    # If we get here, primary file is missing
    logger.warning(f"BookingDataAll is missing in folder {current_year:04d}/{current_month:02d}/")
    logger.info("Attempting fallback using older data...")

    # Get fallback keys (one BookingDataAll + N BookingData files going forward)
    prev_all_key, booking_data_keys = get_fallback_keys(
        s3_client, bucket, current_year, current_month
    )

    found_fallback = False

    if prev_all_key:
        try:
            logger.info(f"Loading chunks from BookingDataAll: {prev_all_key}")
            for chunk in load_chunks_func(s3_client, prev_all_key, chunk_size):
                found_fallback = True
                yield chunk
        except Exception as e:
            logger.error(f"Failed to load BookingDataAll ({prev_all_key}): {e}")

    for data_key in booking_data_keys:
        try:
            logger.info(f"Loading chunks from BookingData: {data_key}")
            for chunk in load_chunks_func(s3_client, data_key, chunk_size):
                found_fallback = True
                yield chunk
        except Exception as e:
            logger.error(f"Failed to load BookingData ({data_key}): {e}")

    if found_fallback:
        logger.warning(f"⚠️  Using fallback data combination")
    else:
        logger.warning("No fallback data available - proceeding without BookingDataAll")


class UnifiedDataLoader:
    """Unified data loader for all TryBooking data operations."""
    
    # Optimized data types for memory efficiency
    # Using float types for numeric columns that may contain NA values
    # (pandas can't convert NA to int directly, but can to float)
    # For pd.read_csv, we need to use object type for IDs then convert
    OPTIMIZED_DTYPES = {
        'AccountId': 'float32',  # Changed from int32 to handle NAs
        'EventId': 'object',  # Read as object, convert to int64 later  
        'BookingTransactionId': 'float64',  # Changed from int64 to handle NAs
        'BookingId': 'float64',  # Changed from int64 to handle NAs
        'TicketQuantity': 'float32',  # Changed from int32 to handle NAs
        'PaymentReceived': 'float32',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32',
        'Surcharge': 'float32',
        'ProcessingFeeSurcharge': 'float32',
        'Year': 'float32',  # Changed from int16 to handle NAs
        'DonationCampaignId': 'object',  # Read as object, convert later
        'CustomerId': 'object',  # Read as object, convert later
        'GatewayId': 'object',
        'GiftCertificateId': 'object',
    }
    
    # Columns that should be converted to Int64 after reading
    NULLABLE_INT_COLUMNS = ['EventId', 'DonationCampaignId', 'CustomerId']
    
    # Categorical columns for further memory optimization
    CATEGORICAL_COLUMNS = [
        'Status', 'TransactionType', 'PaymentType', 'Industry', 
        'SubIndustry', 'Gateway Group', 'DGRStatus', 'IPCountry',
        'BookingCountryCode', 'Wallet', 'GatewayName',
        'GiftCertificateTypeName', 'AccountPostcode', 'EventPostcode'
    ]
    
    def __init__(self):
        """Initialize the unified data loader."""
        self.s3_client = self._get_s3_client()
        # Cache dir is configurable so the Pi can use a persistent location
        # (e.g. /root/reporting/.cache) shared across the staggered cron jobs.
        self.cache_dir = os.environ.get("S3_CACHE_DIR", ".cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        # Directory for the prebuilt combined-booking pickle. Defaults under the
        # cache dir; the prepare_data.py job writes it once each morning so the
        # tier and dashboard jobs don't each re-combine BookingDataAll + the
        # current month from scratch.
        self.data_dir = os.environ.get("DATA_DIR", os.path.join(self.cache_dir, "prepared"))
        os.makedirs(self.data_dir, exist_ok=True)
    
    def _get_s3_client(self):
        """Get S3 client with the IAM service-user credentials from .env."""
        return boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )
    
    # ========== Caching Functions ==========
    
    def _get_cache_path(self, key: str) -> str:
        """Get cache file path for an S3 key."""
        safe_filename = key.replace('/', '_')
        return os.path.join(self.cache_dir, f"{safe_filename}.pkl")
    
    def _get_cache_metadata_path(self, key: str) -> str:
        """Get cache metadata file path."""
        cache_path = self._get_cache_path(key)
        return f"{cache_path}.meta"
    
    def _is_cache_valid(self, key: str, cache_path: str) -> bool:
        """Check if cached file is still valid."""
        if not os.path.exists(cache_path):
            return False
        
        # Check if NO_CACHE is set
        if os.getenv('NO_CACHE', '0') == '1':
            logger.info("NO_CACHE=1 set, skipping cache")
            return False
        
        metadata_path = self._get_cache_metadata_path(key)
        if not os.path.exists(metadata_path):
            return False
        
        try:
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            # Check cache age (7 days max)
            cache_time = datetime.fromisoformat(metadata['cache_time'])
            if datetime.now() - cache_time > timedelta(days=7):
                logger.info(f"Cache expired for {key} (>7 days old)")
                return False

            # Trust-cache shortcut: when CACHE_TRUST_TODAY=1 and this entry was
            # cached today, treat it as fresh without a head_object round-trip.
            # The prepare_data.py job refreshes the cache first thing each
            # morning, so downstream jobs can trust it for the rest of the day.
            # Falls through to the ETag check below for entries not cached today.
            if os.getenv('CACHE_TRUST_TODAY', '0') == '1':
                if cache_time.date() == datetime.now().date():
                    logger.info(f"CACHE_TRUST_TODAY: trusting today's cache for {key} (no head_object)")
                    return True

            # Verify S3 file hasn't changed
            s3_response = self.s3_client.head_object(Bucket=S3_BUCKET, Key=key)
            s3_etag = s3_response['ETag'].strip('"')
            s3_size = s3_response['ContentLength']
            
            if metadata.get('etag') != s3_etag or metadata.get('size') != s3_size:
                logger.info(f"S3 file changed for {key}")
                return False
            
            return True
            
        except Exception as e:
            logger.debug(f"Cache validation error: {e}")
            return False
    
    def _save_cache_metadata(self, key: str):
        """Save metadata for cached file."""
        try:
            s3_response = self.s3_client.head_object(Bucket=S3_BUCKET, Key=key)
            metadata = {
                'cache_time': datetime.now().isoformat(),
                'etag': s3_response['ETag'].strip('"'),
                'size': s3_response['ContentLength'],
                's3_key': key
            }
            
            metadata_path = self._get_cache_metadata_path(key)
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f)
        except Exception as e:
            logger.warning(f"Could not save cache metadata: {e}")
    
    def clear_cache(self):
        """Clear all cached files."""
        if os.path.exists(self.cache_dir):
            import shutil
            shutil.rmtree(self.cache_dir)
            os.makedirs(self.cache_dir, exist_ok=True)
            logger.info("Cache cleared")
    
    # ========== Low-level S3 Functions ==========
    
    @timer_decorator
    def download_s3_file(self, key: str, use_cache: bool = True, skiprows: Optional[int] = None) -> pd.DataFrame:
        """
        Download and load a CSV file from S3 with caching.

        Args:
            key: S3 key of the file
            use_cache: Whether to use cache if available
            skiprows: Number of rows to skip at the start of the file (e.g. for metadata rows)

        Returns:
            DataFrame with the file contents
        """
        cache_path = self._get_cache_path(key)
        
        # Try to use cache
        if use_cache and self._is_cache_valid(key, cache_path):
            logger.info(f"Using cached file: {os.path.basename(cache_path)}")
            try:
                with open(cache_path, 'rb') as f:
                    df = pickle.load(f)
                logger.info(f"  Loaded {len(df):,} rows from cache")
                
                # Convert nullable int columns even when loading from cache
                for col in self.NULLABLE_INT_COLUMNS:
                    if col in df.columns:
                        try:
                            df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')
                        except Exception:
                            pass  # Keep as-is if conversion fails
                
                return df
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
        
        # Download from S3
        logger.info(f"Downloading {key} from S3...")
        
        # Get file info (may fail if file doesn't exist)
        try:
            response = self.s3_client.head_object(Bucket=S3_BUCKET, Key=key)
            file_size_mb = response['ContentLength'] / (1024 * 1024)
            logger.info(f"  File size: {file_size_mb:.1f} MB")
        except self.s3_client.exceptions.NoSuchKey:
            logger.warning(f"File not found: {key}")
            raise
        except Exception as e:
            if '404' in str(e) or 'Not Found' in str(e):
                logger.warning(f"File not found: {key}")
                raise
            else:
                # For other errors, log but continue
                logger.warning(f"Could not get file info: {e}")
        
        # Read with optimized dtypes
        chunks = []
        chunk_size = 100000
        
        obj = self.s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        try:
            for i, chunk in enumerate(pd.read_csv(
                obj['Body'],
                chunksize=chunk_size,
                dtype=self.OPTIMIZED_DTYPES,
                low_memory=False,
                on_bad_lines='warn',  # Handle malformed CSV lines
                skiprows=skiprows  # Skip metadata rows if specified
            )):
                # Apply categorical dtypes
                for col in self.CATEGORICAL_COLUMNS:
                    if col in chunk.columns:
                        try:
                            chunk[col] = chunk[col].astype('category')
                        except Exception:
                            # Skip categorical conversion if it fails
                            pass
                
                # Convert nullable int columns
                for col in self.NULLABLE_INT_COLUMNS:
                    if col in chunk.columns:
                        try:
                            chunk[col] = pd.to_numeric(chunk[col], errors='coerce').astype('Int64')
                        except Exception:
                            pass  # Keep as object if conversion fails
                
                chunks.append(chunk)
                
                if (i + 1) % 10 == 0:
                    logger.info(f"  Processed {(i + 1) * chunk_size:,} rows...")
            
            # Combine chunks
            logger.info("  Combining chunks...")
            non_empty_chunks = [chunk for chunk in chunks if not chunk.empty]
            if non_empty_chunks:
                df = pd.concat(non_empty_chunks, ignore_index=True)
            else:
                df = pd.DataFrame()
        except pd.errors.EmptyDataError:
            logger.warning(f"CSV file is empty (no columns): {key}")
            raise
        except Exception as e:
            logger.error(f"Failed to read CSV file {key}: {e}")
            # Try reading without dtype optimization as fallback
            logger.info("Retrying without dtype optimization...")
            obj = self.s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            df = pd.read_csv(obj['Body'], low_memory=False, on_bad_lines='warn')
        
        logger.info(f"  Total rows loaded: {len(df):,}")
        
        # Save to cache
        if use_cache and len(df) > 0:
            try:
                logger.info(f"  Saving to cache: {os.path.basename(cache_path)}")
                with open(cache_path, 'wb') as f:
                    pickle.dump(df, f)
                self._save_cache_metadata(key)
            except Exception as e:
                logger.warning(f"Failed to save cache: {e}")
        
        return df
    
    def load_chunks(self, key: str, use_cache: bool = True, 
                   chunk_size: int = 100000) -> Iterator[pd.DataFrame]:
        """
        Load S3 file in chunks for memory-efficient processing.
        
        Args:
            key: S3 key of the file
            use_cache: Whether to use cache if available
            chunk_size: Rows per chunk
            
        Yields:
            DataFrame chunks
        """
        cache_path = self._get_cache_path(key)
        
        # If cached, load full file and yield chunks
        if use_cache and self._is_cache_valid(key, cache_path):
            logger.info(f"Loading from cache and chunking: {os.path.basename(cache_path)}")
            try:
                with open(cache_path, 'rb') as f:
                    df = pickle.load(f)
                
                # Convert nullable int columns even when loading from cache
                for col in self.NULLABLE_INT_COLUMNS:
                    if col in df.columns:
                        try:
                            df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')
                        except Exception:
                            pass  # Keep as-is if conversion fails
                
                # Yield chunks from cached DataFrame
                for i in range(0, len(df), chunk_size):
                    yield df.iloc[i:i + chunk_size].copy()
                return
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
        
        # Stream from S3
        logger.info(f"Streaming chunks from S3: {key}")
        obj = self.s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        for chunk in pd.read_csv(
            obj['Body'],
            chunksize=chunk_size,
            dtype=self.OPTIMIZED_DTYPES,
            low_memory=False
        ):
            # Apply categorical dtypes
            for col in self.CATEGORICAL_COLUMNS:
                if col in chunk.columns:
                    chunk[col] = chunk[col].astype('category')
            
            # Convert nullable int columns
            for col in self.NULLABLE_INT_COLUMNS:
                if col in chunk.columns:
                    try:
                        chunk[col] = pd.to_numeric(chunk[col], errors='coerce').astype('Int64')
                    except Exception:
                        pass  # Keep as object if conversion fails
            
            # Parse dates
            if 'TransactionDate' in chunk.columns:
                chunk['TransactionDate'] = pd.to_datetime(chunk['TransactionDate'], errors='coerce', utc=True)
            if 'EventDate' in chunk.columns:
                chunk['EventDate'] = pd.to_datetime(chunk['EventDate'], errors='coerce', utc=True)
            
            yield chunk
    
    # ========== High-level Business Functions ==========
    
    @timer_decorator
    def load_accounts(self, target_date: Optional[datetime] = None) -> pd.DataFrame:
        """Load and preprocess accounts data."""
        if target_date is None:
            target_date = get_latest_data_date()
        
        date_info = get_file_date_info(target_date)
        filename = f"{date_info['file_prefix']}-Accounts-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"

        logger.info(f"Loading accounts data from S3: {s3_key}")
        try:
            df = self.download_s3_file(s3_key)
        except Exception as e:
            # The monthly Accounts file for the new month isn't published until the
            # 2nd, so on the 1st we still need the previous month's snapshot. Fall
            # back to it when the current month's file isn't there yet.
            error_str = str(e)
            if 'NoSuchKey' not in error_str and '404' not in error_str and 'Not Found' not in error_str:
                raise
            prev_year, prev_month = calculate_previous_month(
                int(date_info['folder_year']), int(date_info['folder_month'])
            )
            prev_prefix = f"{prev_year:04d}{prev_month:02d}"
            prev_key = f"{prev_year:04d}/{prev_month:02d}/{prev_prefix}-Accounts-TBUK.csv"
            logger.info(
                "Current month's Accounts file not found; falling back to previous month: %s",
                prev_key,
            )
            df = self.download_s3_file(prev_key)
        
        # Standardize column names
        if 'Id' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = df['Id']
        elif 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = df['AccountID']

        # Parse dates
        date_columns = ['DateTimeCreated', 'AccountDateTimeCreated']
        for col in date_columns:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce', utc=True)
        
        # Optimize memory
        df = optimize_dtypes(df)
        
        return df
    
    @timer_decorator
    def load_booking_data(self, target_date: Optional[datetime] = None, 
                         data_type: str = 'BookingData') -> pd.DataFrame:
        """
        Load booking data with automatic fallback for BookingDataAll.
        
        Args:
            target_date: Date to load data for
            data_type: 'BookingData' or 'BookingDataAll'
            
        Returns:
            DataFrame with booking data
        """
        if target_date is None:
            target_date = get_latest_data_date()
        
        date_info = get_file_date_info(target_date)

        # For BookingDataAll, dynamically find the file and use fallback logic
        if data_type == 'BookingDataAll':
            # BookingDataAll file location changed in September 2025:
            # - Old: Next month's folder with day 01 (e.g., 2025/09/20250901)
            # - New: Same month's folder with last day (e.g., 2025/08/20250831)
            # Check both locations and use the newest file available
            current_year = int(date_info['folder_year'])
            current_month = int(date_info['folder_month'])

            # Check new location (previous month's folder)
            prev_year, prev_month = calculate_previous_month(current_year, current_month)
            new_location_files, _ = find_booking_files_in_month(
                self.s3_client, S3_BUCKET, prev_year, prev_month
            )

            # Check old location (current month's folder)
            old_location_files, _ = find_booking_files_in_month(
                self.s3_client, S3_BUCKET, current_year, current_month
            )

            # Combine and sort all BookingDataAll files by name (which sorts by date)
            all_files = sorted(new_location_files + old_location_files)

            if all_files:
                # Use the newest (last) file
                s3_key = all_files[-1]
                logger.info(f"Found {data_type} file: {s3_key}")
                # Set year/month to the folder where we found the file for fallback logic
                if s3_key in new_location_files:
                    year, month = prev_year, prev_month
                else:
                    year, month = current_year, current_month
            else:
                # No file found - will trigger fallback logic below
                s3_key = None
                year, month = prev_year, prev_month
                logger.info(f"No {data_type} file found in {year:04d}/{month:02d}/ or {current_year:04d}/{current_month:02d}/")
            
            df = try_load_with_fallback(
                s3_client=self.s3_client,
                bucket=S3_BUCKET,
                primary_key=s3_key,  # May be None if file not found
                load_func=lambda client, key: self.download_s3_file(key) if key else None,
                current_year=year,
                current_month=month
            )
        else:
            # Regular BookingData - standard filename
            filename = f"{date_info['file_prefix']}-{data_type}-TBUK.csv"
            s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
            logger.info(f"Loading {data_type} from S3: {s3_key}")
            df = self.download_s3_file(s3_key)
        
        # Process booking data
        df['TransactionDate'] = pd.to_datetime(df['TransactionDate'], errors='coerce', utc=True)
        
        # Calculate total fees
        fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        for col in fee_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        
        existing_fees = [col for col in fee_columns if col in df.columns]
        df['TotalFees'] = df[existing_fees].sum(axis=1)
        
        if 'EventDate' in df.columns:
            df['EventDate'] = pd.to_datetime(df['EventDate'], errors='coerce', utc=True)
        
        df = optimize_dtypes(df)

        return df

    def _combined_booking_path(self) -> str:
        """Local path for the prebuilt combined-booking pickle."""
        return os.path.join(self.data_dir, "combined_booking.pkl")

    def _combined_booking_meta_path(self) -> str:
        return self._combined_booking_path() + ".meta"

    def _combined_is_fresh(self) -> bool:
        """True if the combined pickle exists and was built today."""
        if os.getenv('NO_CACHE', '0') == '1':
            return False
        path = self._combined_booking_path()
        meta = self._combined_booking_meta_path()
        if not (os.path.exists(path) and os.path.exists(meta)):
            return False
        try:
            with open(meta, 'r') as f:
                built = datetime.fromisoformat(json.load(f)['built_time'])
            return built.date() == datetime.now().date()
        except Exception as e:
            logger.debug(f"Combined-booking meta unreadable: {e}")
            return False

    def load_combined_booking_data(self, target_date: Optional[datetime] = None,
                                   force_rebuild: bool = False) -> pd.DataFrame:
        """Return the de-duped union of BookingDataAll + current-month BookingData.

        Centralises the concat/de-dupe that zoho_tiers.py and
        generate_dashboard_data.py each used to do independently. When a
        combined pickle built earlier today exists (written by prepare_data.py),
        it's returned directly; otherwise the union is built here and — unless
        NO_CACHE=1 — pickled for the rest of the day's jobs to reuse.

        De-dupe keeps the last row per BookingTransactionId, so a current-month
        row supersedes its BookingDataAll counterpart (the current-month export
        is the fresher one, updated daily).
        """
        path = self._combined_booking_path()

        if not force_rebuild and self._combined_is_fresh():
            logger.info("Loading prebuilt combined booking data from %s", path)
            try:
                with open(path, 'rb') as f:
                    df = pickle.load(f)
                logger.info("  Loaded %d combined rows from prepared pickle", len(df))
                return df
            except Exception as e:
                logger.warning("Failed to load combined pickle, rebuilding: %s", e)

        logger.info("Building combined booking data (BookingDataAll + current month)...")
        booking_all_df = self.load_booking_data(target_date, data_type='BookingDataAll')
        booking_month_df = self.load_booking_data(target_date, data_type='BookingData')

        combined = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
        del booking_all_df, booking_month_df
        if 'BookingTransactionId' in combined.columns:
            before = len(combined)
            combined = combined.drop_duplicates(subset=['BookingTransactionId'], keep='last')
            logger.info("  Combined %d rows (removed %d duplicates)",
                        len(combined), before - len(combined))
        else:
            logger.warning("BookingTransactionId missing — combined frame not de-duplicated")

        if os.getenv('NO_CACHE', '0') == '1':
            logger.info("NO_CACHE=1 — not persisting combined pickle")
            return combined

        try:
            with open(path, 'wb') as f:
                pickle.dump(combined, f)
            with open(self._combined_booking_meta_path(), 'w') as f:
                json.dump({'built_time': datetime.now().isoformat(),
                           'rows': len(combined)}, f)
            logger.info("  Saved combined booking pickle: %s (%d rows)", path, len(combined))
        except Exception as e:
            logger.warning("Failed to save combined pickle: %s", e)

        return combined

    def load_booking_chunks(self, target_date: Optional[datetime] = None,
                          data_type: str = 'BookingData',
                          chunk_size: int = 100000) -> Iterator[pd.DataFrame]:
        """
        Load booking data in chunks with automatic fallback.
        
        Args:
            target_date: Date to load data for
            data_type: 'BookingData' or 'BookingDataAll'
            chunk_size: Rows per chunk
            
        Yields:
            DataFrame chunks
        """
        if target_date is None:
            target_date = get_latest_data_date()
        
        date_info = get_file_date_info(target_date)

        # For BookingDataAll, dynamically find the file
        if data_type == 'BookingDataAll':
            # Check both old and new locations for BookingDataAll
            current_year = int(date_info['folder_year'])
            current_month = int(date_info['folder_month'])

            # Check new location (previous month's folder)
            prev_year, prev_month = calculate_previous_month(current_year, current_month)
            new_location_files, _ = find_booking_files_in_month(
                self.s3_client, S3_BUCKET, prev_year, prev_month
            )

            # Check old location (current month's folder)
            old_location_files, _ = find_booking_files_in_month(
                self.s3_client, S3_BUCKET, current_year, current_month
            )

            # Combine and sort all BookingDataAll files by name (newest last)
            all_files = sorted(new_location_files + old_location_files)

            if all_files:
                # Use the newest file (will fall back below if it's empty)
                s3_key = all_files[-1]
                logger.info(f"Found {data_type} file for chunks: {s3_key}")
            else:
                s3_key = None
                logger.warning(f"No {data_type} file found in {prev_year:04d}/{prev_month:02d}/ or {current_year:04d}/{current_month:02d}/")

            # Use the same walk-back fallback as the non-chunked path so an
            # empty/missing BookingDataAll doesn't blow up the chunked feed
            # (the latest file is sometimes 0 bytes; the combine path walks
            # back month-by-month — match it here).
            def _load_chunks(client, key, cs):
                return self.load_chunks(key, chunk_size=cs)

            for chunk in yield_chunks_with_fallback(
                s3_client=self.s3_client, bucket=S3_BUCKET,
                primary_key=s3_key or "", load_chunks_func=_load_chunks,
                current_year=current_year, current_month=current_month,
                chunk_size=chunk_size,
            ):
                self._process_booking_chunk(chunk)
                yield chunk
            return
        else:
            # Regular BookingData - standard filename
            filename = f"{date_info['file_prefix']}-{data_type}-TBUK.csv"
            s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
            logger.info(f"Loading chunks for {data_type} from S3: {s3_key}")

        # Yield chunks from the file
        for chunk in self.load_chunks(s3_key, chunk_size=chunk_size):
            self._process_booking_chunk(chunk)
            yield chunk
    
    def _process_booking_chunk(self, chunk: pd.DataFrame):
        """Process a booking data chunk."""
        # Calculate fees
        fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        for col in fee_columns:
            if col in chunk.columns:
                chunk[col] = pd.to_numeric(chunk[col], errors='coerce').fillna(0)
        
        existing_fees = [col for col in fee_columns if col in chunk.columns]
        if existing_fees:
            chunk['TotalFees'] = chunk[existing_fees].sum(axis=1)
    
    def load_multiple_files(self, keys: List[str], chunk_size: int = 100000) -> Iterator[pd.DataFrame]:
        """
        Load multiple files and yield chunks from all.
        
        Args:
            keys: List of S3 keys
            chunk_size: Rows per chunk
            
        Yields:
            DataFrame chunks from all files
        """
        for key in keys:
            if not key:
                continue
                
            logger.info(f"Loading chunks from {key}")
            
            # Check if BookingDataAll needs fallback
            if 'BookingDataAll' in key:
                parts = key.split('/')
                if len(parts) >= 3:
                    try:
                        year = int(parts[0])
                        month = int(parts[1])
                        
                        for chunk in yield_chunks_with_fallback(
                            s3_client=self.s3_client,
                            bucket=S3_BUCKET,
                            primary_key=key,
                            load_chunks_func=lambda client, k, cs: self.load_chunks(k, chunk_size=cs),
                            current_year=year,
                            current_month=month,
                            chunk_size=chunk_size
                        ):
                            yield chunk
                    except (ValueError, IndexError):
                        # Fall back to regular loading
                        for chunk in self.load_chunks(key, chunk_size=chunk_size):
                            yield chunk
            else:
                # Regular file
                for chunk in self.load_chunks(key, chunk_size=chunk_size):
                    yield chunk
    
    @timer_decorator
    def load_balance(self, target_date: Optional[datetime] = None) -> pd.DataFrame:
        """Load account balance data."""
        if target_date is None:
            target_date = get_latest_data_date()
        
        date_info = get_file_date_info(target_date)
        filename = f"{date_info['file_prefix']}-accountbalance-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
        
        logger.info(f"Loading account balance from S3: {s3_key}")
        df = self.download_s3_file(s3_key)
        
        # Standardize columns
        if 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountID'], errors='coerce')
        elif 'AccountId' in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')
        
        if 'AccountBalance' in df.columns:
            df['AccountBalance'] = pd.to_numeric(df['AccountBalance'], errors='coerce').fillna(0)
        
        df = optimize_dtypes(df)
        
        return df
    
    @timer_decorator
    def load_users(self, target_date: Optional[datetime] = None) -> pd.DataFrame:
        """Load users data."""
        if target_date is None:
            target_date = get_latest_data_date()

        date_info = get_file_date_info(target_date)
        filename = f"{date_info['file_prefix']}-users-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"

        logger.info(f"Loading users data from S3: {s3_key}")
        try:
            df = self.download_s3_file(s3_key)
        except Exception as e:
            # Like Accounts, the monthly users file for the new month isn't
            # published until the 2nd, so fall back to the previous month on the
            # 1st rather than failing.
            error_str = str(e)
            if 'NoSuchKey' not in error_str and '404' not in error_str and 'Not Found' not in error_str:
                raise
            prev_year, prev_month = calculate_previous_month(
                int(date_info['folder_year']), int(date_info['folder_month'])
            )
            prev_prefix = f"{prev_year:04d}{prev_month:02d}"
            prev_key = f"{prev_year:04d}/{prev_month:02d}/{prev_prefix}-users-TBUK.csv"
            logger.info(
                "Current month's users file not found; falling back to previous month: %s",
                prev_key,
            )
            df = self.download_s3_file(prev_key)

        # Standardize columns
        if 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountID'], errors='coerce')

        df = optimize_dtypes(df)

        return df

    @timer_decorator
    def load_account_movement_daily(self, target_date: Optional[datetime] = None) -> pd.DataFrame:
        """Load account movement daily data (includes pending transfers).

        Note: AccountMovementDaily has a metadata header row that must be skipped.
        """
        if target_date is None:
            target_date = get_latest_data_date()

        date_info = get_file_date_info(target_date)
        # AccountMovementDaily uses full date format: YYYYMMDD-AccountMovementDaily-TBUK.csv
        date_str = target_date.strftime('%Y%m%d')
        filename = f"{date_str}-AccountMovementDaily-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"

        logger.info(f"Loading account movement daily data from S3: {s3_key}")
        # Skip first row - it contains metadata (First Gateway ID, Start Booking Transaction ID, etc.)
        df = self.download_s3_file(s3_key, skiprows=1)

        # Standardize columns
        if 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountID'], errors='coerce')
        elif 'AccountId' in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')

        # Convert numeric columns
        numeric_columns = ['Pending', 'Balance', 'Transferred', 'Refunded']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        df = optimize_dtypes(df)

        return df

    @timer_decorator
    def load_risk_report(self, target_date: Optional[datetime] = None) -> pd.DataFrame:
        """
        Load risk report data.

        Contains:
        - AccountId: Account identifier
        - AccountName: Account name
        - FullBalance: Complete account balance (includes pending)
        - Balance: Available balance
        - SalesForUpcomingEvents: Calculated future sales
        - Exposure: Risk exposure amount
        - FutureDays: Days until next event
        - LastEventDate: Date of most recent event
        """
        if target_date is None:
            target_date = get_latest_data_date()

        date_info = get_file_date_info(target_date)
        filename = f"{date_info['file_prefix']}-RiskReport-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"

        logger.info(f"Loading risk report from S3: {s3_key}")
        df = self.download_s3_file(s3_key)

        # Standardize columns
        if 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountID'], errors='coerce')
        elif 'AccountId' in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountId'], errors='coerce')

        # Convert numeric columns
        numeric_columns = ['FullBalance', 'Balance', 'SalesForUpcomingEvents', 'Exposure']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # Parse dates
        if 'LastEventDate' in df.columns:
            df['LastEventDate'] = pd.to_datetime(df['LastEventDate'], errors='coerce', utc=True)

        df = optimize_dtypes(df)

        return df

    def filter_successful_transactions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter booking data to only successful transactions."""
        if 'Status' in df.columns:
            return df[df['Status'] == 'Successful'].copy()
        return df
    
    def log_memory_usage(self, context: str = ""):
        """Log current memory usage."""
        if PSUTIL_AVAILABLE:
            process = psutil.Process()
            mem_info = process.memory_info()
            logger.info(f"Memory usage {context}: {mem_info.rss / 1024 / 1024:.1f} MB")


# Create a singleton instance for convenience
_loader = None

def get_loader() -> UnifiedDataLoader:
    """Get the singleton UnifiedDataLoader instance."""
    global _loader
    if _loader is None:
        _loader = UnifiedDataLoader()
    return _loader


# Convenience functions that use the singleton
def load_accounts(target_date=None):
    """Load accounts data."""
    return get_loader().load_accounts(target_date)

def load_accounts_data(s3_client=None, target_date=None):
    """Load accounts data (legacy compatibility)."""
    return get_loader().load_accounts(target_date)

def load_booking_data(s3_client=None, target_date=None, data_type='BookingData'):
    """Load booking data (supports legacy s3_client parameter)."""
    return get_loader().load_booking_data(target_date, data_type)

def load_combined_booking_data(target_date=None, force_rebuild=False):
    """De-duped union of BookingDataAll + current-month BookingData.

    Returns a prebuilt pickle when one was created earlier today, otherwise
    builds and caches it. See UnifiedDataLoader.load_combined_booking_data.
    """
    return get_loader().load_combined_booking_data(target_date, force_rebuild=force_rebuild)

def load_balance(target_date=None):
    """Load balance data."""
    return get_loader().load_balance(target_date)

def load_users(target_date=None):
    """Load users data."""
    return get_loader().load_users(target_date)

def load_users_data(s3_client=None, target_date=None):
    """Load users data (legacy compatibility)."""
    return get_loader().load_users(target_date)

def load_account_balance_data(s3_client=None, target_date=None):
    """Load account balance data (legacy compatibility)."""
    return get_loader().load_balance(target_date)

def load_account_movement_daily_data(s3_client=None, target_date=None):
    """Load account movement daily data with pending transfers (legacy compatibility)."""
    return get_loader().load_account_movement_daily(target_date)

def load_risk_report(target_date=None):
    """Load risk report data."""
    return get_loader().load_risk_report(target_date)

def load_risk_report_data(s3_client=None, target_date=None):
    """Load risk report data (legacy compatibility)."""
    return get_loader().load_risk_report(target_date)

def filter_successful_transactions(df):
    """Filter booking data to only successful transactions."""
    return get_loader().filter_successful_transactions(df)

def clear_cache():
    """Clear all cached files."""
    get_loader().clear_cache()

def get_s3_client():
    """Get S3 client (legacy compatibility)."""
    return get_loader().s3_client

def download_s3_file_cached(s3_client, key, use_cache=True):
    """Download S3 file with caching (legacy compatibility)."""
    return get_loader().download_s3_file(key, use_cache)

def load_multiple_booking_files(s3_client, keys, use_cache=True, chunk_size=100000):
    """Load multiple files and yield chunks (legacy compatibility)."""
    return get_loader().load_multiple_files(keys, chunk_size)