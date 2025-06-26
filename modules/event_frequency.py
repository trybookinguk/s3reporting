"""
Event frequency analysis utilities for TryBooking accounts.
Analyzes patterns of event activity based on months with sessions.
"""
from datetime import datetime
import pandas as pd
import logging

logger = logging.getLogger(__name__)


# Frequency thresholds (months with events)
FREQUENCY_THRESHOLDS = {
    'Continuous': (10, 12),    # 10-12 months
    'Regular': (5, 9),         # 5-9 months  
    'Seasonal': (2, 4),        # 2-4 months
    'Annual': (1, 1),          # 1 month
    'Inactive': (0, 0)         # 0 months
}


def classify_event_frequency(month_count, has_event_creation=False):
    """
    Convert active month count to pattern classification.
    
    Args:
        month_count: Number of unique months with event sessions
        has_event_creation: True if event was created in period but no bookings (kept for compatibility)
        
    Returns:
        str: Frequency classification (Continuous/Regular/Seasonal/Annual/Inactive)
    """
    # Note: "New" classification is now handled in activity_rating.py based on account creation date
    if month_count == 0:
        return "Inactive"
    elif month_count == 1:
        return "Annual"
    elif month_count <= 4:
        return "Seasonal"
    elif month_count <= 9:
        return "Regular"
    else:  # 10-12 months
        return "Continuous"


def extract_event_months_from_dates(event_dates):
    """
    Extract unique (year, month) tuples from a list of event dates.
    
    Args:
        event_dates: List/Series of datetime objects
        
    Returns:
        set: Set of (year, month) tuples
    """
    months = set()
    for date in event_dates:
        if pd.notna(date):
            if isinstance(date, str):
                date = pd.to_datetime(date)
            months.add((date.year, date.month))
    return months


def calculate_monthly_distribution(event_months):
    """
    Analyze which months of the year have events.
    
    Args:
        event_months: Set of (year, month) tuples
        
    Returns:
        dict: Month number (1-12) to count of years with events in that month
    """
    month_distribution = {}
    for year, month in event_months:
        month_distribution[month] = month_distribution.get(month, 0) + 1
    return month_distribution


def get_seasonal_pattern(event_months):
    """
    Determine if events follow a seasonal pattern.
    
    Args:
        event_months: Set of (year, month) tuples
        
    Returns:
        str: Description of seasonal pattern (e.g., "Summer", "Winter", "Spring/Autumn")
    """
    if not event_months:
        return "No events"
    
    # Get just the month numbers
    months = sorted(set(month for _, month in event_months))
    
    if len(months) >= 10:
        return "Year-round"
    
    # Define seasons (UK)
    seasons = {
        'Winter': [12, 1, 2],
        'Spring': [3, 4, 5],
        'Summer': [6, 7, 8],
        'Autumn': [9, 10, 11]
    }
    
    # Find which seasons have events
    active_seasons = []
    for season, season_months in seasons.items():
        if any(month in months for month in season_months):
            active_seasons.append(season)
    
    if len(active_seasons) == 1:
        return active_seasons[0]
    elif len(active_seasons) == 2 and set(active_seasons) == {'Summer', 'Winter'}:
        return "Summer/Winter"
    elif len(active_seasons) == 2:
        return '/'.join(active_seasons)
    else:
        return "Multi-seasonal"


def predict_next_event_month(event_months, last_event_date=None):
    """
    Predict when the next event might occur based on historical patterns.
    
    Args:
        event_months: Set of (year, month) tuples from past events
        last_event_date: Date of the most recent event
        
    Returns:
        tuple: (predicted_month, confidence) or (None, None) if cannot predict
    """
    if not event_months:
        return None, None
    
    # Get month distribution
    month_dist = calculate_monthly_distribution(event_months)
    
    # For annual events (single month pattern)
    if len(month_dist) == 1:
        predicted_month = list(month_dist.keys())[0]
        confidence = "High"
        return predicted_month, confidence
    
    # For seasonal patterns (2-4 months)
    if len(month_dist) <= 4:
        # Find the most common month
        most_common_month = max(month_dist.items(), key=lambda x: x[1])[0]
        confidence = "Medium"
        return most_common_month, confidence
    
    # For regular/continuous patterns, prediction is less reliable
    return None, None


def get_months_active_fingerprint(event_months):
    """
    Get a sorted list of unique months (1-12) where events have occurred.
    Useful for understanding seasonal patterns.
    
    Args:
        event_months: Set of (year, month) tuples
        
    Returns:
        list: Sorted list of month numbers (1-12)
    """
    if not event_months:
        return []
    
    unique_months = sorted(set(month for _, month in event_months))
    return unique_months


def format_months_active_for_zoho(months_list):
    """
    Format months list for Zoho multi-select field.
    
    Args:
        months_list: List of month numbers (1-12)
        
    Returns:
        list: List of full month names (e.g., ["January", "March", "July", "December"])
    """
    if not months_list:
        return []
    
    # Map month numbers to full names
    month_names = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December"
    }
    
    # Convert month numbers to names and return as list
    month_name_list = [month_names[month] for month in sorted(months_list) if month in month_names]
    
    return month_name_list


def batch_classify_frequencies(event_months_data, batch_size=50000):
    """
    Process event frequency classifications in batches with progress logging.
    
    Args:
        event_months_data: List of event month sets
        batch_size: Number of accounts to process per batch
        
    Returns:
        List of frequency classifications
    """
    import time
    
    total_accounts = len(event_months_data)
    classifications = []
    
    logger.info(f"Starting frequency classification for {total_accounts:,} accounts")
    start_time = time.time()
    
    for i in range(0, total_accounts, batch_size):
        batch_start_time = time.time()
        batch_end = min(i + batch_size, total_accounts)
        batch = event_months_data[i:batch_end]
        
        # Process batch
        batch_classifications = [classify_event_frequency(len(months) if isinstance(months, set) else 0) 
                                 for months in batch]
        classifications.extend(batch_classifications)
        
        # Log progress with timing
        batch_time = time.time() - batch_start_time
        progress_pct = (batch_end / total_accounts) * 100
        accounts_per_sec = len(batch) / batch_time if batch_time > 0 else 0
        
        logger.info(f"Classified {batch_end:,} of {total_accounts:,} accounts ({progress_pct:.1f}%) - "
                   f"{accounts_per_sec:,.0f} accounts/sec")
    
    # Log summary statistics
    total_time = time.time() - start_time
    freq_counts = {}
    for freq in classifications:
        freq_counts[freq] = freq_counts.get(freq, 0) + 1
    
    logger.info(f"Frequency classification complete in {total_time:.1f}s ({total_accounts/total_time:,.0f} accounts/sec)")
    logger.info("Frequency distribution:")
    for freq_type in ['Continuous', 'Regular', 'Seasonal', 'Annual', 'Inactive']:
        if freq_type in freq_counts:
            count = freq_counts[freq_type]
            pct = (count / total_accounts) * 100
            logger.info(f"  {freq_type}: {count:,} accounts ({pct:.1f}%)")
    
    return classifications


def get_frequency_summary(accounts_df, current_col='event_months_current', previous_col='event_months_previous'):
    """
    Generate a summary of event frequency patterns across all accounts.
    
    Args:
        accounts_df: DataFrame with event month data
        current_col: Column name for current period months
        previous_col: Column name for previous period months
        
    Returns:
        dict: Summary statistics by frequency type
    """
    import time
    
    summary = {
        'current_period': {},
        'previous_period': {},
        'transitions': {}
    }
    
    total_accounts = len(accounts_df)
    logger.info(f"Analyzing event frequency patterns for {total_accounts:,} accounts")
    start_time = time.time()
    
    # Process in chunks for large datasets
    chunk_size = 10000
    current_freq_list = []
    previous_freq_list = []
    
    for i in range(0, total_accounts, chunk_size):
        chunk_end = min(i + chunk_size, total_accounts)
        chunk = accounts_df.iloc[i:chunk_end]
        
        # Current period distribution
        chunk_current = chunk[current_col].apply(
            lambda x: classify_event_frequency(len(x) if isinstance(x, set) else 0)
        )
        current_freq_list.extend(chunk_current.tolist())
        
        # Previous period distribution
        chunk_previous = chunk[previous_col].apply(
            lambda x: classify_event_frequency(len(x) if isinstance(x, set) else 0)
        )
        previous_freq_list.extend(chunk_previous.tolist())
        
        if (i + chunk_size) % 50000 == 0 or chunk_end == total_accounts:
            progress_pct = (chunk_end / total_accounts) * 100
            logger.info(f"Analyzed {chunk_end:,} of {total_accounts:,} accounts ({progress_pct:.1f}%)")
    
    # Create Series from lists
    current_freq = pd.Series(current_freq_list)
    previous_freq = pd.Series(previous_freq_list)
    
    summary['current_period'] = current_freq.value_counts().to_dict()
    summary['previous_period'] = previous_freq.value_counts().to_dict()
    
    # Transitions (e.g., Annual -> Seasonal)
    transitions = pd.crosstab(previous_freq, current_freq)
    summary['transitions'] = transitions.to_dict()
    
    # Log frequency distribution
    logger.info("Current period frequency distribution:")
    for freq_type in ['Continuous', 'Regular', 'Seasonal', 'Annual', 'Inactive']:
        if freq_type in summary['current_period']:
            count = summary['current_period'][freq_type]
            pct = (count / total_accounts) * 100
            logger.info(f"  {freq_type}: {count:,} accounts ({pct:.1f}%)")
    
    # Count significant transitions
    significant_transitions = 0
    improved_accounts = 0  # Moved to higher frequency
    declined_accounts = 0  # Moved to lower frequency
    
    freq_order = ['Inactive', 'Annual', 'Seasonal', 'Regular', 'Continuous']
    
    for prev_type in transitions.index:
        for curr_type in transitions.columns:
            if prev_type != curr_type:
                count = transitions.loc[prev_type, curr_type]
                if count > 0:
                    significant_transitions += count
                    
                    prev_idx = freq_order.index(prev_type) if prev_type in freq_order else -1
                    curr_idx = freq_order.index(curr_type) if curr_type in freq_order else -1
                    
                    if curr_idx > prev_idx:
                        improved_accounts += count
                    elif curr_idx < prev_idx:
                        declined_accounts += count
    
    total_time = time.time() - start_time
    logger.info(f"Analysis complete in {total_time:.1f}s")
    logger.info(f"Found {significant_transitions:,} accounts with frequency pattern changes:")
    logger.info(f"  Improved frequency: {improved_accounts:,} accounts")
    logger.info(f"  Declined frequency: {declined_accounts:,} accounts")
    
    return summary