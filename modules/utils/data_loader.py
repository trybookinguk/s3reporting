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


def find_booking_files_in_month(s3_client, bucket: str, year: int, month: int) -> Tuple[List[str], List[str]]:
    """Find BookingDataAll and BookingData files in a specific month."""
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


def get_fallback_keys(s3_client, bucket: str, current_year: int, current_month: int) -> Tuple[Optional[str], Optional[str]]:
    """Get S3 keys for fallback data when current month's BookingDataAll is missing."""
    prev_year, prev_month = calculate_previous_month(current_year, current_month)
    
    logger.info(f"Attempting fallback to {prev_year:04d}-{prev_month:02d} data")
    
    booking_all_files, booking_data_files = find_booking_files_in_month(
        s3_client, bucket, prev_year, prev_month
    )
    
    prev_booking_all_key = booking_all_files[0] if booking_all_files else None
    prev_booking_data_key = booking_data_files[-1] if booking_data_files else None  # Last day of month
    
    if prev_booking_all_key:
        logger.info(f"Found previous month's BookingDataAll: {prev_booking_all_key}")
    if prev_booking_data_key:
        logger.info(f"Found previous month's BookingData: {prev_booking_data_key}")
    
    if not prev_booking_all_key and not prev_booking_data_key:
        logger.warning("No fallback data found in previous month")
    
    return prev_booking_all_key, prev_booking_data_key


def try_load_with_fallback(s3_client, bucket: str, primary_key: str, 
                          load_func, current_year: int, current_month: int) -> pd.DataFrame:
    """Try to load BookingDataAll with automatic fallback to previous month if needed."""
    # First try to load the primary key
    df = None
    try:
        df = load_func(s3_client, primary_key)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        if 'NoSuchKey' not in str(e):
            raise
    
    # If we get here, primary file is missing or empty
    logger.warning(f"BookingDataAll is missing or empty for {current_year:04d}-{current_month:02d}")
    logger.info("Attempting to use previous month's data as fallback...")
    
    # Get fallback keys
    prev_all_key, prev_data_key = get_fallback_keys(
        s3_client, bucket, current_year, current_month
    )
    
    # Load fallback data
    dfs_to_combine = []
    
    if prev_all_key:
        try:
            prev_all_df = load_func(s3_client, prev_all_key)
            if prev_all_df is not None and not prev_all_df.empty:
                dfs_to_combine.append(prev_all_df)
                logger.info(f"Loaded {len(prev_all_df):,} records from previous BookingDataAll")
        except Exception as e:
            logger.error(f"Failed to load previous BookingDataAll: {e}")
    
    if prev_data_key:
        try:
            prev_data_df = load_func(s3_client, prev_data_key)
            if prev_data_df is not None and not prev_data_df.empty:
                dfs_to_combine.append(prev_data_df)
                logger.info(f"Loaded {len(prev_data_df):,} records from previous BookingData")
        except Exception as e:
            logger.error(f"Failed to load previous BookingData: {e}")
    
    # Combine and deduplicate
    if dfs_to_combine:
        logger.info(f"Combining {len(dfs_to_combine)} fallback data sources...")
        combined_df = pd.concat(dfs_to_combine, ignore_index=True)
        
        # Remove duplicates if BookingUrlId exists
        if 'BookingUrlId' in combined_df.columns:
            initial_count = len(combined_df)
            combined_df = combined_df.drop_duplicates(subset=['BookingUrlId'], keep='last')
            duplicates_removed = initial_count - len(combined_df)
            if duplicates_removed > 0:
                logger.info(f"Removed {duplicates_removed:,} duplicate transactions")
        
        prev_year, prev_month = calculate_previous_month(current_year, current_month)
        logger.warning(f"⚠️  Using {prev_year:04d}-{prev_month:02d} complete data as fallback")
        logger.info(f"Successfully created fallback dataset ({len(combined_df):,} total records)")
        
        return combined_df
    else:
        raise ValueError(f"Unable to load BookingDataAll for current month or fallback from previous month")


def yield_chunks_with_fallback(s3_client, bucket: str, primary_key: str,
                              load_chunks_func, current_year: int, current_month: int,
                              chunk_size: int = 100000) -> Iterator[pd.DataFrame]:
    """Yield chunks of BookingDataAll with automatic fallback to previous month if needed."""
    # First try to load chunks from the primary key
    found_primary = False
    try:
        for chunk in load_chunks_func(s3_client, primary_key, chunk_size):
            found_primary = True
            yield chunk
        if found_primary:
            return  # Successfully loaded primary file
    except Exception as e:
        if 'NoSuchKey' not in str(e):
            raise
    
    # If we get here, primary file is missing
    logger.warning(f"BookingDataAll is missing for {current_year:04d}-{current_month:02d}")
    logger.info("Attempting to use previous month's data as fallback...")
    
    # Get fallback keys
    prev_all_key, prev_data_key = get_fallback_keys(
        s3_client, bucket, current_year, current_month
    )
    
    found_fallback = False
    
    # Yield chunks from previous BookingDataAll
    if prev_all_key:
        try:
            logger.info(f"Loading chunks from previous BookingDataAll: {prev_all_key}")
            for chunk in load_chunks_func(s3_client, prev_all_key, chunk_size):
                found_fallback = True
                yield chunk
        except Exception as e:
            logger.error(f"Failed to load previous BookingDataAll: {e}")
    
    # Yield chunks from previous BookingData
    if prev_data_key:
        try:
            logger.info(f"Loading chunks from previous BookingData: {prev_data_key}")
            for chunk in load_chunks_func(s3_client, prev_data_key, chunk_size):
                found_fallback = True
                yield chunk
        except Exception as e:
            logger.error(f"Failed to load previous BookingData: {e}")
    
    if found_fallback:
        prev_year, prev_month = calculate_previous_month(current_year, current_month)
        logger.warning(f"⚠️  Using {prev_year:04d}-{prev_month:02d} complete data as fallback")
    else:
        logger.warning("No fallback data available - proceeding without BookingDataAll")


class UnifiedDataLoader:
    """Unified data loader for all TryBooking data operations."""
    
    # Optimized data types for memory efficiency
    OPTIMIZED_DTYPES = {
        'AccountId': 'int32',
        'EventId': 'float32',  
        'BookingTransactionId': 'int64',
        'BookingId': 'int64',
        'TicketQuantity': 'int32',
        'PaymentReceived': 'float32',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32',
        'Surcharge': 'float32',
        'ProcessingFeeSurcharge': 'float32',
        'Year': 'int16',
        'DonationCampaignId': 'float32',
        'CustomerId': 'float32',
        'GatewayId': 'object',
        'GiftCertificateId': 'object',
    }
    
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
        self.cache_dir = ".cache"
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def _get_s3_client(self):
        """Get S3 client with credentials."""
        return boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
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
    def download_s3_file(self, key: str, use_cache: bool = True) -> pd.DataFrame:
        """
        Download and load a CSV file from S3 with caching.
        
        Args:
            key: S3 key of the file
            use_cache: Whether to use cache if available
            
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
                return df
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
        
        # Download from S3
        logger.info(f"Downloading {key} from S3...")
        
        # Get file info
        response = self.s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        file_size_mb = response['ContentLength'] / (1024 * 1024)
        logger.info(f"  File size: {file_size_mb:.1f} MB")
        
        # Read with optimized dtypes
        chunks = []
        chunk_size = 100000
        
        obj = self.s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        
        for i, chunk in enumerate(pd.read_csv(
            obj['Body'],
            chunksize=chunk_size,
            dtype=self.OPTIMIZED_DTYPES,
            low_memory=False
        )):
            # Apply categorical dtypes
            for col in self.CATEGORICAL_COLUMNS:
                if col in chunk.columns:
                    chunk[col] = chunk[col].astype('category')
            
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
        df = self.download_s3_file(s3_key)
        
        # Standardize column names
        if 'AccountID' in df.columns:
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
        
        # BookingDataAll has special naming with 01 suffix
        if data_type == 'BookingDataAll':
            filename = f"{date_info['file_prefix']}01-{data_type}-TBUK.csv"
        else:
            filename = f"{date_info['file_prefix']}-{data_type}-TBUK.csv"
        
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
        
        logger.info(f"Loading {data_type} from S3: {s3_key}")
        
        # For BookingDataAll, use fallback logic
        if data_type == 'BookingDataAll':
            year = int(date_info['folder_year'])
            month = int(date_info['folder_month'])
            
            df = try_load_with_fallback(
                s3_client=self.s3_client,
                bucket=S3_BUCKET,
                primary_key=s3_key,
                load_func=lambda client, key: self.download_s3_file(key),
                current_year=year,
                current_month=month
            )
        else:
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
        
        # BookingDataAll has special naming
        if data_type == 'BookingDataAll':
            filename = f"{date_info['file_prefix']}01-{data_type}-TBUK.csv"
        else:
            filename = f"{date_info['file_prefix']}-{data_type}-TBUK.csv"
        
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
        
        logger.info(f"Loading chunks for {data_type} from S3: {s3_key}")
        
        # For BookingDataAll, use fallback logic
        if data_type == 'BookingDataAll':
            year = int(date_info['folder_year'])
            month = int(date_info['folder_month'])
            
            for chunk in yield_chunks_with_fallback(
                s3_client=self.s3_client,
                bucket=S3_BUCKET,
                primary_key=s3_key,
                load_chunks_func=lambda client, key, chunk_size: self.load_chunks(key, chunk_size=chunk_size),
                current_year=year,
                current_month=month,
                chunk_size=chunk_size
            ):
                # Process chunk
                self._process_booking_chunk(chunk)
                yield chunk
        else:
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
        filename = f"{date_info['file_prefix']}-AccountUserRelationship-TBUK.csv"
        s3_key = f"{date_info['folder_year']}/{date_info['folder_month']}/{filename}"
        
        logger.info(f"Loading users data from S3: {s3_key}")
        df = self.download_s3_file(s3_key)
        
        # Standardize columns
        if 'AccountID' in df.columns and 'AccountId' not in df.columns:
            df['AccountId'] = pd.to_numeric(df['AccountID'], errors='coerce')
        
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

def load_balance(target_date=None):
    """Load balance data."""
    return get_loader().load_balance(target_date)

def load_users(target_date=None):
    """Load users data."""
    return get_loader().load_users(target_date)

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