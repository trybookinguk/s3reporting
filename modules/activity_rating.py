"""
Activity rating determination logic for TryBooking accounts.
Analyzes account behavior patterns to classify activity status.
"""
import pandas as pd


def determine_activity_rating(current_freq, previous_freq, days_since_last, has_historical, 
                            avg_lead_days=60, last_event_date=None):
    """
    Determine activity rating based on event patterns and creation lead times.
    
    Args:
        current_freq: Event frequency for current period (Continuous/Regular/Seasonal/Annual/New/Inactive)
        previous_freq: Event frequency for previous period
        days_since_last: Days since last booking/transaction
        has_historical: Whether account has historical activity
        avg_lead_days: Average days between event creation and event date
        last_event_date: Date of the last event (for annual predictions)
    
    Returns:
        str: Activity rating (Active/At Risk/Churned/Returned/New/Inactive)
    """
    # Active: Any current activity
    if current_freq != "Inactive":
        return "Active"
    
    # New: No historical activity and recent account
    if previous_freq == "Inactive" and not has_historical:
        return "New" if days_since_last < 365 else "Inactive"
    
    # Returned: Was inactive, now active
    if current_freq != "Inactive" and previous_freq == "Inactive" and has_historical:
        return "Returned"
    
    # At Risk/Churned logic for accounts that were active but now inactive
    if previous_freq != "Inactive" and current_freq == "Inactive":
        # Special handling for annual/seasonal events
        if previous_freq in ["Annual", "Seasonal"] and last_event_date:
            # Calculate when they should have created their next event
            expected_next_event = last_event_date + pd.Timedelta(days=365)
            expected_creation_date = expected_next_event - pd.Timedelta(days=avg_lead_days)
            days_past_expected_creation = (pd.Timestamp.now().date() - expected_creation_date).days
            
            # At Risk if past expected creation but not too far past
            if 0 < days_past_expected_creation <= 90:
                return "At Risk"
            elif days_past_expected_creation > 90:
                return "Churned"
            else:
                # Not yet time to create next event
                return "Active"
        
        # For regular/continuous events, use simpler time-based logic
        if days_since_last < 180:
            return "At Risk"
        else:
            return "Churned"
    
    return "Inactive"


def get_rating_transition_summary(results_df):
    """
    Generate a summary of rating transitions.
    
    Args:
        results_df: DataFrame with current and previous ratings
        
    Returns:
        dict: Summary of rating transitions and counts
    """
    if 'Rating' not in results_df.columns:
        return {}
    
    rating_counts = results_df['Rating'].value_counts()
    summary = {
        'distribution': rating_counts.to_dict(),
        'total_at_risk': rating_counts.get('At Risk', 0),
        'total_churned': rating_counts.get('Churned', 0),
        'total_active': rating_counts.get('Active', 0),
        'total_new': rating_counts.get('New', 0),
        'total_returned': rating_counts.get('Returned', 0)
    }
    
    return summary


def identify_priority_accounts(results_df):
    """
    Identify accounts that need immediate attention.
    
    Args:
        results_df: DataFrame with ratings and tiers
        
    Returns:
        DataFrame: Priority accounts requiring outreach
    """
    priority_conditions = (
        # High-value accounts at risk
        ((results_df['Rating'] == 'At Risk') & 
         (results_df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4']))) |
        
        # Recently churned high-value accounts
        ((results_df['Rating'] == 'Churned') & 
         (results_df['Current_Tier'].isin(['Key Account', 'High Value'])) &
         (results_df.get('_days_since_last', 999) < 365))
    )
    
    if priority_conditions.any():
        return results_df[priority_conditions].sort_values(
            ['Current_Tier', 'Rating'], 
            ascending=[True, True]
        )
    
    return pd.DataFrame()