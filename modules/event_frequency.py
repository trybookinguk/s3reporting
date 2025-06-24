"""
Event frequency analysis utilities for TryBooking accounts.
Analyzes patterns of event activity based on months with sessions.
"""
from datetime import datetime
import pandas as pd


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
        has_event_creation: True if event was created in period but no bookings
        
    Returns:
        str: Frequency classification (Continuous/Regular/Seasonal/Annual/New/Inactive)
    """
    if month_count == 0:
        if has_event_creation:
            return "New"  # Event created but no activity yet
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
        str: Comma-separated full month names (e.g., "January,March,July,December")
    """
    if not months_list:
        return ""
    
    # Map month numbers to full names
    month_names = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December"
    }
    
    # Convert month numbers to names and join
    month_name_list = [month_names[month] for month in sorted(months_list) if month in month_names]
    
    return ",".join(month_name_list)


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
    summary = {
        'current_period': {},
        'previous_period': {},
        'transitions': {}
    }
    
    # Current period distribution
    current_freq = accounts_df[current_col].apply(
        lambda x: classify_event_frequency(len(x) if isinstance(x, set) else 0)
    )
    summary['current_period'] = current_freq.value_counts().to_dict()
    
    # Previous period distribution
    previous_freq = accounts_df[previous_col].apply(
        lambda x: classify_event_frequency(len(x) if isinstance(x, set) else 0)
    )
    summary['previous_period'] = previous_freq.value_counts().to_dict()
    
    # Transitions (e.g., Annual -> Seasonal)
    transitions = pd.crosstab(previous_freq, current_freq)
    summary['transitions'] = transitions.to_dict()
    
    return summary