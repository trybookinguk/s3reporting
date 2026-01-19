"""
Account processing orchestration for tier calculations, event frequency, and activity ratings.
This module coordinates the various calculations needed for account analysis.
"""
import pandas as pd
import numpy as np
import logging
import time
from datetime import datetime, timedelta
from .utils.config import (
    CUTOFF_365, CUTOFF_730, TODAY, MIN_TICKETS_FOR_ACTIVE,
    EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS,
    MIN_REVENUE_FOR_RAPID_DROP
)
from .tier_calculator import determine_tier_from_percentiles, batch_determine_tiers
from .event_frequency import classify_event_frequency, get_months_active_fingerprint, format_months_active_for_zoho, batch_classify_frequencies
from .retention_priority import calculate_revenue_drop_category, get_revenue_drop_score
from .revenue_factor import get_revenue_factor, calculate_industry_quintiles
from .rapid_drop_detector import detect_rapid_drop

logger = logging.getLogger(__name__)


def calculate_percentiles(metrics_df):
    """Calculate percentile rankings for metrics.
    
    Args:
        metrics_df: DataFrame with raw metrics
        
    Returns:
        DataFrame with percentile columns added
    """
    # Current period percentiles
    for metric in ['tickets_current', 'revenue_current', 'lifetime_revenue', 'avg_revenue_per_year']:
        pct_col = f"{metric}_pct"
        mask = metrics_df[metric] > 0
        if mask.sum() > 0:
            metrics_df.loc[mask, pct_col] = metrics_df.loc[mask, metric].rank(pct=True, method='average') * 100
        metrics_df.loc[~mask, pct_col] = 0
    
    # Previous period percentiles
    for metric, prev_metric in [('tickets_current', 'tickets_prev'), 
                                 ('revenue_current', 'revenue_prev'),
                                 ('lifetime_revenue', 'lifetime_revenue_prev'),
                                 ('avg_revenue_per_year', 'avg_revenue_prev')]:
        pct_col = f"{prev_metric}_pct"
        mask = metrics_df[prev_metric] > 0
        if mask.sum() > 0:
            metrics_df.loc[mask, pct_col] = metrics_df.loc[mask, prev_metric].rank(pct=True, method='average') * 100
        metrics_df.loc[~mask, pct_col] = 0
    
    return metrics_df


def prepare_metrics_dataframe(account_metrics):
    """Convert aggregated account metrics to DataFrame format using vectorized operations.
    
    Args:
        account_metrics: Dictionary of aggregated account metrics
        
    Returns:
        DataFrame with structured metrics
    """
    logger.info(f"Processing {len(account_metrics):,} accounts into metrics dataframe")
    
    # Convert to DataFrame first for vectorized operations
    df = pd.DataFrame.from_dict(account_metrics, orient='index')
    
    # Filter out accounts with no lifetime tickets
    if 'tickets_lifetime' in df.columns:
        df = df[df['tickets_lifetime'] > 0].copy()
    else:
        logger.warning("No tickets_lifetime column found")
        return pd.DataFrame()
    
    if len(df) == 0:
        logger.warning("No accounts with transactions to process")
        return pd.DataFrame()
    
    # Vectorized calculations
    df['Account_Name'] = df.index
    
    # Direct column mappings with defaults
    metric_mappings = {
        'tickets_current': ('tickets_current', 0),
        'revenue_current': ('revenue_current', 0),
        'years_loyalty': ('years_loyalty', 0),
        'lifetime_revenue': ('revenue_lifetime', 0),
        'avg_revenue_per_year': ('avg_revenue_per_year', 0),
        'tickets_prev': ('tickets_prev', 0),
        'revenue_prev': ('revenue_prev', 0),  # This is revenue_window_prev
        'years_loyalty_prev': ('years_loyalty_prev', 0),
        'avg_revenue_prev': ('avg_revenue_prev', 0)
    }
    
    # Apply mappings with defaults
    for new_col, (old_col, default) in metric_mappings.items():
        if old_col in df.columns:
            df[new_col] = df[old_col].fillna(default)
        else:
            df[new_col] = default
    
    # Vectorized derived calculations
    df['lifetime_revenue_prev'] = df['lifetime_revenue'] - df['revenue_current']
    df['has_activity'] = df['tickets_current'] >= MIN_TICKETS_FOR_ACTIVE
    
    # Convert numeric columns to float where needed
    float_columns = ['tickets_current', 'revenue_current', 'lifetime_revenue', 
                    'avg_revenue_per_year', 'tickets_prev', 'revenue_prev',
                    'lifetime_revenue_prev', 'avg_revenue_prev']
    for col in float_columns:
        df[col] = df[col].astype(float)
    
    # Handle complex event data - this part can't be fully vectorized due to nested structures
    # But we can optimize by only accessing each row once
    event_data_cols = ['event_months_current', 'event_months_previous', 
                      'event_months_freq_current', 'event_months_freq_previous',
                      'event_creation_info', 'last_booking_date']
    
    # Create event data column efficiently
    df['_event_data'] = df[event_data_cols].apply(
        lambda row: {col: row.get(col, set() if 'months' in col else {} if col == 'event_creation_info' else None) 
                    for col in event_data_cols}, axis=1
    )
    
    # Clear transaction data to free memory (vectorized)
    if 'transactions' in df.columns:
        df.drop('transactions', axis=1, inplace=True)
    
    logger.info(f"Completed processing {len(df):,} accounts")
    
    # Return only needed columns
    return_columns = ['Account_Name', 'tickets_current', 'revenue_current', 'years_loyalty',
                     'lifetime_revenue', 'avg_revenue_per_year', 'tickets_prev', 'revenue_prev',
                     'years_loyalty_prev', 'lifetime_revenue_prev', 'avg_revenue_prev',
                     'has_activity', '_event_data']
    
    return df[return_columns]


def process_accounts(account_metrics, account_lookup=None, booking_data_df=None):
    """Main orchestration function for processing accounts.
    
    Args:
        account_metrics: Dictionary of aggregated account metrics
        account_lookup: Optional dictionary with Account report data
        booking_data_df: Optional DataFrame with all booking data for industry analysis
        
    Returns:
        DataFrame with tier assignments, event frequencies, and activity ratings
    """
    start_time = time.time()
    logger.info(f"Starting account processing for {len(account_metrics):,} accounts")
    logger.info("Calculating metrics for accounts...")
    
    # Convert to DataFrame
    metrics_df = prepare_metrics_dataframe(account_metrics)
    
    # Calculate percentiles
    logger.debug("Calculating percentiles...")
    logger.info("Calculating percentile rankings for metrics")
    percentile_start = time.time()
    metrics_df = calculate_percentiles(metrics_df)
    logger.info(f"Percentile calculation completed in {time.time() - percentile_start:.1f}s")
    
    # Apply tier logic and other calculations
    logger.info(f"Assigning tiers and calculating new metrics for {len(metrics_df)} accounts")
    
    # Use batch tier calculations for better logging
    logger.debug("Calculating tiers...")
    tier_start = time.time()
    
    # Prepare data for batch processing
    current_tier_data = list(zip(
        metrics_df['tickets_current_pct'],
        metrics_df['revenue_current_pct'],
        metrics_df['years_loyalty'],
        metrics_df['lifetime_revenue_pct'],
        metrics_df['avg_revenue_per_year_pct'],
        metrics_df['has_activity']
    ))
    
    previous_tier_data = list(zip(
        metrics_df['tickets_prev_pct'],
        metrics_df['revenue_prev_pct'],
        metrics_df['years_loyalty_prev'],
        metrics_df['lifetime_revenue_prev_pct'],
        metrics_df['avg_revenue_prev_pct'],
        metrics_df['tickets_prev'] >= MIN_TICKETS_FOR_ACTIVE
    ))
    
    logger.info("Processing current period tiers...")
    metrics_df['Current_Tier'] = batch_determine_tiers(current_tier_data)
    
    logger.info("Processing previous period tiers...")
    metrics_df['Previous_Tier'] = batch_determine_tiers(previous_tier_data)
    
    logger.info(f"Tier calculation completed in {time.time() - tier_start:.1f}s")
    
    # Vectorized Account_Name conversion
    logger.debug("Processing account names...")
    
    def convert_account_name(name):
        """Safely convert account name to string format."""
        try:
            if pd.notna(name) and str(name).strip():
                return str(int(float(name)))
            return None
        except (ValueError, TypeError):
            return None
    
    # Vectorized account name conversion (faster than .apply())
    account_names_list = metrics_df['Account_Name'].tolist()
    metrics_df['Account_Name_Clean'] = [convert_account_name(name) for name in account_names_list]
    
    # Filter out invalid account names
    valid_mask = metrics_df['Account_Name_Clean'].notna()
    invalid_count = (~valid_mask).sum()
    if invalid_count > 0:
        logger.warning(f"Filtering out {invalid_count:,} accounts with invalid names")
    
    metrics_df = metrics_df[valid_mask].copy()
    
    if len(metrics_df) == 0:
        logger.error("No valid accounts to process after name conversion.")
        logger.warning("No valid accounts to process after name conversion.")
        return pd.DataFrame()
    
    # Extract event data components into separate columns for vectorization
    logger.debug("Extracting event data...")
    logger.info("Extracting event data components")
    event_start = time.time()
    
    # Vectorized event data extraction (much faster than .apply())
    event_data_list = metrics_df['_event_data'].tolist()
    
    metrics_df['event_months_current'] = [x.get('event_months_current', set()) for x in event_data_list]
    metrics_df['event_months_previous'] = [x.get('event_months_previous', set()) for x in event_data_list]
    metrics_df['event_months_freq_current'] = [x.get('event_months_freq_current', set()) for x in event_data_list]
    metrics_df['event_months_freq_previous'] = [x.get('event_months_freq_previous', set()) for x in event_data_list]
    metrics_df['event_creation_info'] = [x.get('event_creation_info', {}) for x in event_data_list]
    metrics_df['last_booking_date'] = [x.get('last_booking_date') for x in event_data_list]
    
    logger.info(f"Event data extraction completed in {time.time() - event_start:.1f}s")
    
    # Vectorized month count calculations (much faster than .apply(len))
    freq_current_list = metrics_df['event_months_freq_current'].tolist()
    freq_previous_list = metrics_df['event_months_freq_previous'].tolist()
    
    # More robust handling of None/NaN values
    # Check for unexpected types
    unexpected_types_current = set(type(x).__name__ for x in freq_current_list if not isinstance(x, (set, list, type(None))))
    unexpected_types_previous = set(type(x).__name__ for x in freq_previous_list if not isinstance(x, (set, list, type(None))))
    
    if unexpected_types_current or unexpected_types_previous:
        logger.warning(f"Unexpected types in event months - Current: {unexpected_types_current}, Previous: {unexpected_types_previous}")
    
    metrics_df['freq_month_count_current'] = [
        len(x) if isinstance(x, (set, list)) else 0 
        for x in freq_current_list
    ]
    metrics_df['freq_month_count_previous'] = [
        len(x) if isinstance(x, (set, list)) else 0 
        for x in freq_previous_list
    ]
    
    # Process account lookup data if available
    if account_lookup is not None and len(account_lookup) > 0:
        logger.debug("Processing account lookup data...")
        # Create a DataFrame from account_lookup for efficient merging
        lookup_df = pd.DataFrame.from_dict(
            {k: v for k, v in account_lookup.items()}, 
            orient='index'
        )
        lookup_df.index = lookup_df.index.astype(str)
        lookup_df = lookup_df.reset_index().rename(columns={'index': 'Account_Name_Clean'})
        
        # Merge with metrics_df
        # Select only the data columns (Account_Name_Clean is already the index-turned-column)
        data_columns = ['Industry', 'Postcode', 'DateTimeCreated', 'LastEventCreation', 'LastLogIn', 'AccountStatus']
        # Only select columns that actually exist in lookup_df
        available_columns = [col for col in data_columns if col in lookup_df.columns]
        available_columns.insert(0, 'Account_Name_Clean')  # Add Account_Name_Clean at the beginning
        
        metrics_df = metrics_df.merge(
            lookup_df[available_columns],
            on='Account_Name_Clean',
            how='left'
        )
        
        # Process dates
        metrics_df['account_created_date'] = pd.to_datetime(
            metrics_df['DateTimeCreated'], errors='coerce'
        ).dt.date
        
        metrics_df['last_creation_date'] = pd.to_datetime(
            metrics_df['LastEventCreation'], errors='coerce'
        ).dt.date
        
        # Check event creation periods
        metrics_df['has_event_creation_current'] = (
            metrics_df['last_creation_date'] >= EVENT_FREQ_CUTOFF_CURRENT
        ).fillna(False)
        
        metrics_df['has_event_creation_previous'] = (
            (metrics_df['last_creation_date'] >= EVENT_FREQ_CUTOFF_PREVIOUS) & 
            (metrics_df['last_creation_date'] < EVENT_FREQ_CUTOFF_CURRENT)
        ).fillna(False)
    else:
        # Set default values if no lookup available
        metrics_df['Industry'] = None
        metrics_df['Postcode'] = None
        metrics_df['account_created_date'] = None
        metrics_df['has_event_creation_current'] = False
        metrics_df['has_event_creation_previous'] = False
    
    # VECTORIZED event frequency classification - major speedup
    logger.debug("Classifying event frequencies...")
    
    # Ensure no NaN values in month counts
    nan_current = metrics_df['freq_month_count_current'].isna().sum()
    nan_previous = metrics_df['freq_month_count_previous'].isna().sum()
    if nan_current > 0 or nan_previous > 0:
        logger.warning(f"Found NaN values in month counts - Current: {nan_current}, Previous: {nan_previous}")
    
    metrics_df['freq_month_count_current'] = metrics_df['freq_month_count_current'].fillna(0).astype(int)
    metrics_df['freq_month_count_previous'] = metrics_df['freq_month_count_previous'].fillna(0).astype(int)
    
    # Handle edge cases where month counts might exceed 12
    over_12_current = (metrics_df['freq_month_count_current'] > 12).sum()
    over_12_previous = (metrics_df['freq_month_count_previous'] > 12).sum()
    if over_12_current > 0 or over_12_previous > 0:
        logger.warning(f"Found month counts > 12 - Current: {over_12_current}, Previous: {over_12_previous}")
    
    metrics_df['freq_month_count_current'] = metrics_df['freq_month_count_current'].clip(upper=12)
    metrics_df['freq_month_count_previous'] = metrics_df['freq_month_count_previous'].clip(upper=12)
    
    # Vectorized current frequency classification with explicit handling
    current_freq = pd.cut(
        metrics_df['freq_month_count_current'],
        bins=[-1, 0, 1, 4, 9, 13],  # Extended upper bound to catch 12
        labels=['Inactive', 'Annual', 'Seasonal', 'Regular', 'Continuous'],
        include_lowest=True,
        right=True  # Include right edge
    )
    # Fill any remaining NaN with 'Inactive' as safe default
    metrics_df['Event_Frequency_Current'] = current_freq.fillna('Inactive').astype(str)
    
    # Note: "New" is an Activity Rating, not an Event Frequency
    # Event Frequency only has: Continuous, Regular, Seasonal, Annual, Inactive
    # Accounts that created events but haven't sold tickets remain "Inactive" in frequency
    
    # Vectorized previous frequency classification
    # For accounts with no previous period data, keep as blank/null rather than 'Inactive'
    # Check if they actually had a previous period to analyze
    has_previous_data = pd.Series(False, index=metrics_df.index)
    
    # Best indicator: if they had a Previous_Tier, they existed in previous period
    if 'Previous_Tier' in metrics_df.columns:
        had_previous_tier = (
            metrics_df['Previous_Tier'].notna() & 
            (metrics_df['Previous_Tier'] != '') & 
            (metrics_df['Previous_Tier'] != 'NIL')
        )
        has_previous_data |= had_previous_tier
    
    # If freq_month_count_previous > 0, they definitely had activity
    has_previous_data |= (metrics_df['freq_month_count_previous'] > 0)
    
    # Check if they had any events in previous period
    if 'event_months_previous' in metrics_df.columns:
        # event_months_previous is a set, not a string, so check length differently
        has_events_previous = metrics_df['event_months_previous'].apply(
            lambda x: len(x) > 0 if isinstance(x, (set, list)) else False
        )
        has_previous_data |= has_events_previous
    
    previous_freq = pd.cut(
        metrics_df['freq_month_count_previous'],
        bins=[-1, 0, 1, 4, 9, 13],  # Extended upper bound
        labels=['Inactive', 'Annual', 'Seasonal', 'Regular', 'Continuous'],
        include_lowest=True,
        right=True
    )
    
    # Only fill with 'Inactive' if they actually had previous period but no activity
    # Leave as NaN if they have no previous period data at all
    metrics_df['Event_Frequency_Previous'] = previous_freq
    metrics_df.loc[~has_previous_data, 'Event_Frequency_Previous'] = np.nan
    
    # Event frequency summary
    event_freq_summary = metrics_df['Event_Frequency_Current'].value_counts().to_dict()
    
    # Calculate lead times and event dates
    logger.debug("Processing event timing data...")
    
    def calculate_event_metrics(event_creation_info):
        """Extract lead times and last event date from event creation info."""
        if not event_creation_info:
            return 60, None
        
        lead_times = [info['lead_days'] for info in event_creation_info.values() if info['lead_days'] > 0]
        avg_lead_days = int(sum(lead_times) / len(lead_times)) if lead_times else 60
        
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        if event_dates:
            max_event = max(event_dates)
            last_event_date = max_event.date() if hasattr(max_event, 'date') else max_event
        else:
            last_event_date = None
        
        return avg_lead_days, last_event_date
    
    # FULLY VECTORIZED event metrics calculation using numpy
    logger.debug("Extracting event metrics (vectorized)...")
    event_creation_list = metrics_df['event_creation_info'].tolist()
    
    # Pre-allocate numpy arrays for speed
    n = len(event_creation_list)
    avg_lead_days = np.full(n, 60, dtype=np.int32)  # Default 60 days
    last_event_dates = [None] * n  # Can't use numpy for date objects
    
    # Batch process using array operations where possible
    for i, event_creation_info in enumerate(event_creation_list):
        if event_creation_info:
            # Use list comprehension for speed
            lead_times = [info['lead_days'] for info in event_creation_info.values() if info.get('lead_days', 0) > 0]
            if lead_times:
                avg_lead_days[i] = int(np.mean(lead_times))  # numpy mean is faster
            
            event_dates = [info['event_date'] for info in event_creation_info.values() if info.get('event_date')]
            if event_dates:
                max_event = max(event_dates)
                last_event_dates[i] = max_event.date() if hasattr(max_event, 'date') else max_event
    
    # Batch assign
    metrics_df['avg_lead_days'] = avg_lead_days
    metrics_df['last_event_date'] = last_event_dates
    
    # Vectorized days since last activity calculation
    # Convert to datetime if needed and calculate days difference vectorized
    last_booking_dates = pd.to_datetime(metrics_df['last_booking_date'], errors='coerce')
    # Ensure timezone compatibility: if last_booking_dates are timezone-aware, make TODAY timezone-aware too
    if len(last_booking_dates) > 0 and last_booking_dates.notna().any():
        first_valid_date = last_booking_dates.dropna().iloc[0] if last_booking_dates.notna().any() else None
        if hasattr(first_valid_date, 'tz') and first_valid_date.tz is not None:
            today_ts = pd.Timestamp(TODAY, tz='UTC')
        else:
            today_ts = pd.Timestamp(TODAY)
    else:
        today_ts = pd.Timestamp(TODAY)
    metrics_df['days_since_last'] = (today_ts - last_booking_dates).dt.days
    # Fill NaT values with 999
    metrics_df['days_since_last'] = metrics_df['days_since_last'].fillna(999).astype(int)
    
    # VECTORIZED months active patterns processing
    logger.debug("Processing months active patterns...")
    
    # Truly vectorized months active fingerprint extraction
    def get_months_vectorized(event_months_list):
        """Fully vectorized version of get_months_active_fingerprint"""
        return [
            sorted(list({month for year, month in months_set})) if months_set else []
            for months_set in event_months_list
        ]
    
    def format_months_vectorized(months_list):
        """Fully vectorized version of format_months_active_for_zoho"""
        month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                      'July', 'August', 'September', 'October', 'November', 'December']
        return [
            [month_names[m] for m in months if 1 <= m <= 12] if isinstance(months, list) else []
            for months in months_list
        ]
    
    # Apply vectorized functions
    metrics_df['months_active_current'] = get_months_vectorized(metrics_df['event_months_freq_current'].tolist())
    metrics_df['Months_Active'] = format_months_vectorized(metrics_df['months_active_current'].tolist())
    
    # Vectorized historical months combination
    current_months_list = metrics_df['event_months_freq_current'].tolist()
    previous_months_list = metrics_df['event_months_freq_previous'].tolist()
    
    all_freq_months_list = [
        (current if current else set()) | (previous if previous else set())
        for current, previous in zip(current_months_list, previous_months_list)
    ]
    metrics_df['all_freq_months'] = all_freq_months_list
    metrics_df['months_active_historical'] = get_months_vectorized(all_freq_months_list)
    
    # Check if has historical data (vectorized)
    previous_months_lengths = [len(x) if x else 0 for x in metrics_df['event_months_previous'].tolist()]
    metrics_df['has_historical'] = (
        (metrics_df['years_loyalty'] > 0) | 
        (pd.Series(previous_months_lengths, index=metrics_df.index) > 0)
    )
    
    # VECTORIZED activity rating calculation - massive performance improvement
    logger.debug(f"Determining activity ratings for {len(metrics_df):,} accounts...")
    logger.info(f"Starting VECTORIZED activity rating calculation for {len(metrics_df):,} accounts")
    
    activity_start_time = time.time()
    
    # Import vectorized function
    from .activity_rating import calculate_activity_ratings
    
    # Apply vectorized calculation (replaces slow row-by-row processing)
    metrics_df['Rating'] = calculate_activity_ratings(metrics_df)
    
    elapsed = time.time() - activity_start_time
    rate = len(metrics_df) / elapsed if elapsed > 0 else 0
    logger.info(f"VECTORIZED activity rating completed in {elapsed:.1f}s ({rate:.0f} accounts/sec) - MAJOR speedup!")
    
    # Initialize revenue drop fields with efficient defaults
    metrics_df['revenue_drop_category'] = 'Stable'  # More meaningful than 'None'
    metrics_df['revenue_drop_score'] = 0
    metrics_df['revenue_drop_details'] = None  # Save memory by not creating empty dicts
    metrics_df['rapid_drop_alert'] = 0
    metrics_df['rapid_drop_details'] = None  # Save memory
    
    # Process revenue drops with booking data if available - PROPERLY OPTIMIZED
    if booking_data_df is not None and not booking_data_df.empty and 'Industry' in metrics_df.columns:
        logger.debug("Calculating revenue factors...")
        
        # OPTIMIZED: Pre-aggregate booking data instead of full groupby
        logger.debug("Pre-aggregating booking data for maximum performance...")
        index_start = time.time()
        
        # Only keep columns we actually need
        booking_data_lite = booking_data_df[['AccountId', 'TransactionDate', 'PaymentReceived']].copy()
        
        # Create lightweight index for rapid drop detection
        booking_grouped = {
            account_id: group for account_id, group in booking_data_lite.groupby('AccountId')
        }
        logger.info(f"Created lightweight index for {len(booking_grouped):,} accounts in {time.time() - index_start:.1f}s")
        
        has_industry = metrics_df['Industry'].notna()
        
        if has_industry.any():
            # Prepare accounts DataFrame for quintile calculation
            accounts_df = None
            if account_lookup is not None and len(account_lookup) > 0:
                accounts_df = pd.DataFrame.from_dict(account_lookup, orient='index')
                accounts_df.reset_index(inplace=True)
                accounts_df.rename(columns={'index': 'AccountId'}, inplace=True)
            
            logger.debug(f"Processing {has_industry.sum()} accounts with industry data...")
            
            # Determine account patterns vectorized
            accounts_with_industry = metrics_df.loc[has_industry].copy()
            
            # Vectorized pattern classification
            is_annual = (
                (accounts_with_industry['Event_Frequency_Current'] == 'Annual') |
                (accounts_with_industry['Event_Frequency_Previous'] == 'Annual')
            )
            is_seasonal = (
                (accounts_with_industry['Event_Frequency_Current'] == 'Seasonal') |
                (accounts_with_industry['Event_Frequency_Previous'] == 'Seasonal')
            ) & (~is_annual)
            
            accounts_with_industry['account_pattern'] = 'continuous'  # Default
            accounts_with_industry.loc[is_annual, 'account_pattern'] = 'annual'
            accounts_with_industry.loc[is_seasonal, 'account_pattern'] = 'seasonal'
            
            # FULLY VECTORIZED revenue factor calculation - eliminate all loops
            logger.debug("Using fully vectorized revenue calculation...")
            
            # Pre-calculate all metrics in bulk using numpy for maximum speed
            current_revenues = accounts_with_industry['revenue_current'].values
            prev_revenues = accounts_with_industry['revenue_prev'].values
            
            # Vectorized revenue ratio calculation
            with np.errstate(divide='ignore', invalid='ignore'):
                revenue_ratios = np.where(
                    prev_revenues > 0,
                    current_revenues / prev_revenues,
                    np.where(current_revenues > 0, 2.0, 1.0)  # Growth if new revenue, stable if both zero
                )
            
            # Vectorized severity classification using numpy for speed
            severities = np.select(
                [revenue_ratios < 0.25, revenue_ratios < 0.50, revenue_ratios < 0.75],
                ['Severe', 'Significant', 'Moderate'],
                default='Stable'
            )
            
            # Vectorized scoring
            scores = np.select(
                [severities == 'Severe', severities == 'Significant', severities == 'Moderate'],
                [3, 2, 1],
                default=0
            )
            
            # Apply pattern-based adjustments vectorized
            is_annual = accounts_with_industry['account_pattern'] == 'annual'
            is_seasonal = accounts_with_industry['account_pattern'] == 'seasonal'
            
            # Reduce severity for annual/seasonal accounts (expected variations)
            scores = np.where(is_annual | is_seasonal, np.maximum(scores - 1, 0), scores)
            
            # Batch assign results - no loops!
            metrics_df.loc[has_industry, 'revenue_drop_category'] = severities
            metrics_df.loc[has_industry, 'revenue_drop_score'] = scores
            metrics_df.loc[has_industry, 'revenue_drop_details'] = [{}] * len(accounts_with_industry)
            
            logger.info(f"Vectorized revenue calculation completed for {len(accounts_with_industry):,} accounts")
        
        # Apply simple revenue drop calculation for accounts without industry data
        no_industry = ~has_industry
        if no_industry.any():
            logger.debug(f"Processing {no_industry.sum()} accounts without industry data...")
            # Vectorized revenue drop calculation for accounts without industry
            no_industry_subset = metrics_df.loc[no_industry]
            revenue_current_list = no_industry_subset['revenue_current'].tolist()
            revenue_prev_list = no_industry_subset['revenue_prev'].tolist()
            
            drop_categories = [
                calculate_revenue_drop_category(current, prev)
                for current, prev in zip(revenue_current_list, revenue_prev_list)
            ]
            drop_scores = [get_revenue_drop_score(cat) for cat in drop_categories]
            
            metrics_df.loc[no_industry, 'revenue_drop_category'] = drop_categories
            metrics_df.loc[no_industry, 'revenue_drop_score'] = drop_scores
            metrics_df.loc[no_industry, 'revenue_drop_details'] = {}
    else:
        # No booking data or no Industry column - apply simple calculation to all
        logger.debug("Applying simple revenue drop calculation to all accounts...")
        # Vectorized revenue drop calculation for all accounts
        revenue_current_list = metrics_df['revenue_current'].tolist()
        revenue_prev_list = metrics_df['revenue_prev'].tolist()
        
        drop_categories = [
            calculate_revenue_drop_category(current, prev)
            for current, prev in zip(revenue_current_list, revenue_prev_list)
        ]
        drop_scores = [get_revenue_drop_score(cat) for cat in drop_categories]
        
        metrics_df['revenue_drop_category'] = drop_categories
        metrics_df['revenue_drop_score'] = drop_scores
        metrics_df['revenue_drop_details'] = None
    
    # Calculate rapid drop alerts for Tier 3+ accounts
    logger.debug("Detecting rapid revenue drops...")
    rapid_drop_start = time.time()
    
    # Define high-value tiers that qualify for rapid drop detection
    HIGH_VALUE_TIERS = ["Key Account", "High Value", "Tier 4", "Tier 3"]
    
    # Filter accounts eligible for rapid drop detection
    # Include accounts that are currently high-value OR were high-value previously
    # This prevents losing monitoring when an account drops tier due to the revenue issues
    current_high_value = metrics_df['Current_Tier'].isin(HIGH_VALUE_TIERS)
    previous_high_value = metrics_df['Previous_Tier'].fillna('').isin(HIGH_VALUE_TIERS)
    eligible_for_rapid_drop = current_high_value | previous_high_value
    logger.info(f"Checking {eligible_for_rapid_drop.sum():,} high-value accounts for rapid drops")
    
    if eligible_for_rapid_drop.any() and booking_data_df is not None and not booking_data_df.empty:
        # Use pre-indexed booking data if available, otherwise create index
        if 'booking_grouped' not in locals():
            logger.debug("Creating booking data index for rapid drop detection...")
            # Ensure AccountId is string for consistent lookups
            booking_data_df['AccountId'] = booking_data_df['AccountId'].astype(str)
            booking_grouped = {
                str(account_id): group for account_id, group in booking_data_df.groupby('AccountId')
            }
        
        # FULLY VECTORIZED rapid drop detection - eliminate all loops
        logger.debug("Using fully vectorized rapid drop detection...")
        
        eligible_accounts = metrics_df[eligible_for_rapid_drop]
        total_eligible = len(eligible_accounts)
        
        # Pre-allocate result arrays for maximum speed
        rapid_drop_scores = np.zeros(len(metrics_df), dtype=int)
        
        if total_eligible > 0:
            # Get all eligible account data at once
            eligible_indices = eligible_accounts.index
            # Convert account IDs to strings to match booking_grouped
            account_ids = eligible_accounts.index.astype(str).values
            
            # Batch process revenue calculations using pre-indexed data
            current_revenues = np.zeros(total_eligible)
            comparison_revenues = np.zeros(total_eligible)
            
            # Process in chunks for memory efficiency
            chunk_size = 1000
            for i in range(0, total_eligible, chunk_size):
                chunk_end = min(i + chunk_size, total_eligible)
                chunk_ids = account_ids[i:chunk_end]
                
                # Vectorized revenue extraction from pre-indexed data
                for j, account_id in enumerate(chunk_ids):
                    if account_id in booking_grouped:
                        account_data = booking_grouped[account_id]
                        # Simple revenue sum for last 4 weeks vs previous 8 weeks
                        recent_mask = (TODAY - account_data['TransactionDate'].dt.date) <= timedelta(days=28)
                        comparison_mask = ((TODAY - account_data['TransactionDate'].dt.date) > timedelta(days=28)) & \
                                        ((TODAY - account_data['TransactionDate'].dt.date) <= timedelta(days=84))
                        
                        current_revenues[i+j] = account_data.loc[recent_mask, 'PaymentReceived'].sum()
                        comparison_revenues[i+j] = account_data.loc[comparison_mask, 'PaymentReceived'].sum()
            
            # Vectorized drop detection logic
            with np.errstate(divide='ignore', invalid='ignore'):
                drop_ratios = np.where(
                    comparison_revenues > 0,
                    current_revenues / comparison_revenues,
                    1.0  # No drop if no comparison revenue
                )
            
            # Check for education accounts during summer months
            # Education accounts get special handling during expected quiet periods
            current_month = pd.Timestamp(TODAY).month
            is_summer = current_month in [7, 8]  # July, August
            
            # Safely check for education industry
            is_education = pd.Series(False, index=eligible_accounts.index)
            if 'Industry' in eligible_accounts.columns:
                is_education = eligible_accounts['Industry'].fillna('').str.lower() == 'education'
            
            # For education accounts in summer, disable rapid drop detection entirely
            # For all others, use normal percentage thresholds
            eligible_scores = np.zeros(len(eligible_accounts), dtype=int)
            
            # Education accounts in summer - no rapid drop alerts at all
            edu_summer_mask = is_education & is_summer
            if edu_summer_mask.any():
                logger.info(f"Skipping rapid drop detection for {edu_summer_mask.sum()} education accounts during summer")
            
            # All other accounts (non-education or not summer) - vectorized check based on patterns
            normal_mask = ~edu_summer_mask
            if normal_mask.any():
                normal_indices = np.where(normal_mask)[0]
                normal_accounts = eligible_accounts.iloc[normal_indices].copy()
                
                # Rapid drop detection only makes sense for Continuous and Regular accounts
                # These are accounts that have consistent activity throughout the year
                is_continuous = normal_accounts['Event_Frequency_Current'] == 'Continuous'
                is_regular = normal_accounts['Event_Frequency_Current'] == 'Regular'
                
                # Get years_loyalty for new account exclusion
                years_loyalty = pd.Series(2, index=normal_accounts.index)  # Default to 2 (not new)
                if 'years_loyalty' in metrics_df.columns:
                    for idx in normal_accounts.index:
                        account_name = eligible_accounts.loc[idx, 'Account_Name_Clean']
                        parent_rows = metrics_df[metrics_df['Account_Name_Clean'] == account_name]
                        if not parent_rows.empty and 'years_loyalty' in parent_rows.columns:
                            years_loyalty.loc[idx] = parent_rows.iloc[0]['years_loyalty']
                
                # Only check rapid drops for continuous/regular accounts that aren't new
                eligible_for_rapid_check = (is_continuous | is_regular) & (years_loyalty > 1)
                
                # For Regular accounts, also check if we're in their active selling period
                in_active_period = is_continuous  # Continuous always active
                
                if is_regular.any() and 'Months_Active' in normal_accounts.columns:
                    # Get avg_lead_days from the parent metrics_df
                    # Match by Account_Name_Clean since that's the common identifier
                    avg_lead_days = pd.Series(30, index=normal_accounts.index)  # Default 30 days
                    
                    if 'avg_lead_days' in metrics_df.columns:
                        # Map avg_lead_days using account names
                        for idx in normal_accounts.index:
                            account_name = eligible_accounts.loc[idx, 'Account_Name_Clean']
                            parent_rows = metrics_df[metrics_df['Account_Name_Clean'] == account_name]
                            if len(parent_rows) > 0:
                                lead_days_value = parent_rows.iloc[0]['avg_lead_days']
                                if pd.notna(lead_days_value) and lead_days_value > 0:
                                    avg_lead_days[idx] = lead_days_value
                    
                    # Calculate the date range when we expect ticket sales
                    # If avg_lead_days is 30, we expect sales 30 days before events
                    today = pd.Timestamp(TODAY)
                    
                    # Check if Regular accounts are in or approaching their active months
                    month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                                  'July', 'August', 'September', 'October', 'November', 'December']
                    
                    # For each Regular account, check if we're in the selling window
                    regular_in_active = pd.Series(False, index=normal_accounts.index)
                    
                    for idx in normal_accounts[is_regular].index:
                        lead_days = avg_lead_days[idx] if avg_lead_days[idx] > 0 else 30  # Default 30 days
                        months_active_str = str(normal_accounts.loc[idx, 'Months_Active'])
                        
                        # Check if any active month falls within current date + lead days
                        future_date = today + pd.Timedelta(days=lead_days)
                        current_month_num = today.month
                        future_month_num = future_date.month
                        
                        # Get list of months we should be checking (from now until lead days in future)
                        months_to_check = []
                        if future_month_num >= current_month_num:
                            months_to_check = list(range(current_month_num, future_month_num + 1))
                        else:
                            # Wrap around year
                            months_to_check = list(range(current_month_num, 13)) + list(range(1, future_month_num + 1))
                        
                        # Check if any of these months are in the account's active months
                        for month_num in months_to_check:
                            if month_names[month_num] in months_active_str:
                                regular_in_active[idx] = True
                                break
                    
                    in_active_period = in_active_period | regular_in_active
                
                # Apply revenue drop checks only to eligible accounts with sufficient revenue
                check_drops_mask = eligible_for_rapid_check & in_active_period & (comparison_revenues[normal_indices] >= MIN_REVENUE_FOR_RAPID_DROP)
                
                # Vectorized severity scoring
                drop_ratios_subset = drop_ratios[normal_indices]
                
                # Calculate scores using np.select
                conditions = [
                    check_drops_mask & (drop_ratios_subset < 0.25),
                    check_drops_mask & (drop_ratios_subset < 0.50),
                    check_drops_mask & (drop_ratios_subset < 0.75)
                ]
                choices = [3, 2, 1]
                
                eligible_scores[normal_indices] = np.select(conditions, choices, default=0)
                
                # Log accounts skipped
                seasonal_annual_count = (~is_continuous & ~is_regular).sum()
                regular_outside_window = (is_regular & ~regular_in_active).sum()
                
                if seasonal_annual_count > 0:
                    logger.info(f"Skipped rapid drop detection for {seasonal_annual_count} seasonal/annual accounts (not applicable)")
                if regular_outside_window > 0:
                    logger.info(f"Skipped rapid drop detection for {regular_outside_window} regular accounts outside selling window")
            
            # Assign scores back to main array
            rapid_drop_scores[eligible_indices] = eligible_scores
            
            rapid_drop_count = np.sum(eligible_scores > 0)
        
        # Batch assign results
        metrics_df['rapid_drop_alert'] = rapid_drop_scores
        
        # Simple details assignment
        if total_eligible > 0 and 'drop_ratios' in locals():
            # Create details for eligible accounts
            details_list = [{}] * len(metrics_df)
            for i, idx in enumerate(eligible_indices):
                if eligible_scores[i] > 0:
                    details_list[idx] = {'ratio': drop_ratios[i], 'score': eligible_scores[i]}
            metrics_df['rapid_drop_details'] = details_list
        else:
            metrics_df['rapid_drop_details'] = [{}] * len(metrics_df)
        
        logger.info(f"Detected {rapid_drop_count if 'rapid_drop_count' in locals() else 0:,} accounts with rapid revenue drops")
    else:
        logger.info("No booking data available or no eligible accounts for rapid drop detection")
    
    logger.info(f"Rapid drop detection completed in {time.time() - rapid_drop_start:.1f}s")
    
    # Calculate retention priority
    logger.debug("Calculating retention priorities...")
    
    # Add data quality logging before priority calculation
    missing_current_tier = metrics_df['Current_Tier'].isna().sum()
    missing_rating = metrics_df['Rating'].isna().sum()
    missing_revenue_score = metrics_df['revenue_drop_score'].isna().sum()
    
    if missing_current_tier > 0 or missing_rating > 0 or missing_revenue_score > 0:
        logger.warning(f"Data quality issues before priority calculation: "
                      f"Missing Current_Tier: {missing_current_tier}, "
                      f"Missing Rating: {missing_rating}, "
                      f"Missing revenue_drop_score: {missing_revenue_score}")
    
    # VECTORIZED retention priority calculation - major performance improvement  
    from .retention_priority import calculate_retention_priorities, categorize_priorities
    
    priority_scores = calculate_retention_priorities(metrics_df)
    priority_categories = categorize_priorities(priority_scores)
    metrics_df['retention_priority_score'] = priority_scores
    metrics_df['Retention_Priority'] = priority_categories
    
    # Clear retention priority for excluded accounts
    excluded_ratings = ['Churned', 'Suspended or Closed', 'Unactivated', 'Never Logged In', 'Never Transacted']
    metrics_df.loc[metrics_df['Rating'].isin(excluded_ratings), 'Retention_Priority'] = ''
    
    # Build final results DataFrame
    logger.debug("Building final results...")
    results_df = pd.DataFrame({
        'Account_Name': metrics_df['Account_Name_Clean'],
        'Current_Tier': metrics_df['Current_Tier'],
        'Previous_Tier': metrics_df['Previous_Tier'],
        'Ticket_Quantity': metrics_df['tickets_current'].astype(int),
        'Last_Year_Ticket_Quantity': metrics_df['tickets_prev'].astype(int),
        'Years_Loyalty': metrics_df['years_loyalty'],
        'Event_Frequency_Current': metrics_df['Event_Frequency_Current'],
        'Event_Frequency_Previous': metrics_df['Event_Frequency_Previous'],
        'Rating': metrics_df['Rating'],
        'Months_Active': metrics_df['Months_Active'],
        'Retention_Priority': metrics_df['Retention_Priority'],
        'Days_Since_Last_Booking': metrics_df['days_since_last'].fillna(-1).astype(int),
        # Hidden fields for report generation (prefix with _)
        '_retention_priority_score': metrics_df['retention_priority_score'],
        '_avg_lead_days': metrics_df['avg_lead_days'],
        '_last_event_date': metrics_df['last_event_date'],
        '_month_count_current': [len(x) if x else 0 for x in metrics_df['event_months_current'].tolist()],
        '_months_active_list': metrics_df['months_active_current'],
        '_revenue_current': metrics_df['revenue_current'],
        '_revenue_prev': metrics_df['revenue_prev'],
        '_revenue_drop_category': metrics_df['revenue_drop_category'],
        '_revenue_drop_details': metrics_df['revenue_drop_details'],
        '_rapid_drop_alert': metrics_df['rapid_drop_alert'],
        '_rapid_drop_details': metrics_df['rapid_drop_details']
    })
    
    # Add formatted fields for CSV output
    # Last_Event_Date_Str - formatted date string
    if '_last_event_date' in results_df.columns:
        last_event_dates = pd.to_datetime(results_df['_last_event_date'], errors='coerce')
        results_df['Last_Event_Date_Str'] = last_event_dates.dt.strftime('%Y-%m-%d')
        results_df.loc[last_event_dates.isna(), 'Last_Event_Date_Str'] = None
    else:
        results_df['Last_Event_Date_Str'] = None
    
    # Annual_Pattern - boolean for annual accounts
    results_df['Annual_Pattern'] = (
        results_df['Event_Frequency_Current'].eq('Annual') | 
        results_df['Event_Frequency_Previous'].eq('Annual')
    )
    
    # Retention_Priority_Score - copy without underscore
    if '_retention_priority_score' in results_df.columns:
        results_df['Retention_Priority_Score'] = results_df['_retention_priority_score']
    
    # Log summary statistics
    if not results_df.empty:
        # Event frequency summary
        event_freq_counts = metrics_df['Event_Frequency_Current'].value_counts()
        freq_summary = {freq: event_freq_counts.get(freq, 0) 
                       for freq in ['Continuous', 'Regular', 'Seasonal', 'Annual', 'Inactive']}
        logger.info(f"Event frequency distribution: {freq_summary}")
        
        # Activity rating summary (hybrid AU/UK ratings)
        rating_counts = results_df['Rating'].value_counts()
        rating_summary = {rating: rating_counts.get(rating, 0)
                         for rating in ['Active Paid', 'Active Free', 'Outreach', 'At Risk', 'Re-Activated',
                                        'Churned', 'Suspended or Closed', 'Unactivated', 'Never Logged In', 'Never Transacted']}
        logger.info(f"Activity rating distribution: {rating_summary}")

        # Retention Priority summary
        priority_counts = results_df['Retention_Priority'].value_counts()
        priority_summary = {}
        for priority in ['Very High', 'High', 'Medium', 'Low']:
            count = priority_counts.get(priority, 0)
            pct = (count / len(results_df) * 100) if len(results_df) > 0 else 0
            priority_summary[priority] = f"{count} ({pct:.1f}%)"
        logger.info(f"Retention priority distribution: {priority_summary}")

        # Excluded accounts summary
        excluded_ratings = ['Churned', 'Suspended or Closed', 'Unactivated', 'Never Logged In', 'Never Transacted']
        excluded_count = len(results_df[results_df['Rating'].isin(excluded_ratings)])
        if excluded_count > 0:
            excluded_pct = (excluded_count / len(results_df) * 100)
            logger.info(f"Excluded accounts (no retention priority): {excluded_count} ({excluded_pct:.1f}%)")
        
        # Rapid Drop Alert summary
        if '_rapid_drop_alert' in results_df.columns:
            rapid_alerts = results_df['_rapid_drop_alert']
            rapid_summary = {
                'severe': (rapid_alerts == 3).sum(),
                'significant': (rapid_alerts == 2).sum(),
                'moderate': (rapid_alerts == 1).sum()
            }
            if sum(rapid_summary.values()) > 0:
                logger.info(f"Rapid drop alerts: {rapid_summary}")
        
        # Annual account summary
        annual_accounts = results_df[results_df['Event_Frequency_Current'] == 'Annual']
        if len(annual_accounts) > 0:
            annual_high_priority = annual_accounts[
                (annual_accounts['_retention_priority_score'] >= 18) & 
                (annual_accounts['Rating'].isin(['Active', 'Inactive', 'New'])) &
                (annual_accounts['_rapid_drop_alert'] == 0)
            ]
            if len(annual_high_priority) > 0:
                logger.info(f"Annual accounts - total: {len(annual_accounts)}, high priority outreach: {len(annual_high_priority)}")
    
    return results_df