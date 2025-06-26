"""
Vectorized retention priority scoring for TryBooking accounts.
Ultra-fast bulk pandas operations for prioritizing retention efforts.
"""
import pandas as pd
import numpy as np
from datetime import datetime
import calendar


def calculate_retention_priorities(df):
    """
    Vectorized retention priority calculation for entire DataFrame.
    
    Args:
        df: DataFrame with columns:
            - Current_Tier, Previous_Tier, Rating
            - revenue_drop_score, rapid_drop_alert
            - Event_Frequency_Current, months_active_current
            - has_event_creation_current
    
    Returns:
        pd.Series: Retention priority scores for all accounts
    """
    # Input validation
    required_columns = ['Current_Tier', 'Rating', 'revenue_drop_score', 'rapid_drop_alert']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    
    # Initialize result series
    priority_scores = pd.Series(0, index=df.index, dtype='int64')
    
    # Define tier weights (vectorized mapping)
    tier_weights = {
        "Key Account": 5,
        "High Value": 4, 
        "Tier 4": 3,
        "Tier 3": 2,
        "Tier 2": 1,
        "Tier 1": 1,
        "NIL": 1
    }
    
    # Define rating severity (vectorized mapping)
    rating_severity = {
        "Churned": -1,
        "At Risk": 5,     # Reduced from 7
        "Outreach": 3,    # Reduced from 4
        "New": 2,         # Reduced from 3
        "Returned": 1,
        "Active": 0,
        "Inactive": 0
    }
    
    # Vectorized tier weight calculation
    tier_weight_series = df['Current_Tier'].map(tier_weights).fillna(1)
    
    # Vectorized rating severity calculation  
    rating_severity_series = df['Rating'].map(rating_severity).fillna(0)
    
    # Vectorized revenue score handling
    revenue_scores = pd.Series(0, index=df.index)
    
    # Handle string revenue scores first (takes precedence)
    string_revenue_mask = df['revenue_drop_score'].astype(str).str.match(r'^(Severe|Significant|Moderate|Stable)$', na=False)
    revenue_score_map = {'Severe': 3, 'Significant': 2, 'Moderate': 1, 'Stable': 0}
    
    if string_revenue_mask.any():
        revenue_scores[string_revenue_mask] = df.loc[string_revenue_mask, 'revenue_drop_score'].map(revenue_score_map).fillna(0)
    
    # Handle numeric revenue scores only for non-string values
    numeric_revenue_mask = (~string_revenue_mask) & pd.to_numeric(df['revenue_drop_score'], errors='coerce').notna()
    if numeric_revenue_mask.any():
        revenue_scores[numeric_revenue_mask] = pd.to_numeric(
            df.loc[numeric_revenue_mask, 'revenue_drop_score'], errors='coerce'
        ).clip(0, 3).fillna(0)
    
    # Validate rapid drop alert
    rapid_drop_alerts = pd.to_numeric(df['rapid_drop_alert'], errors='coerce').fillna(0).clip(0, 3)
    
    # Calculate base priority (vectorized)
    # Use a more balanced formula to prevent excessive scores
    # Old formula: tier_weight × (rating_severity + revenue_score) could yield 40+
    # New formula: (tier_weight × 2) + rating_severity + revenue_score yields max ~18
    priority_scores = (
        (tier_weight_series * 2) + rating_severity_series + revenue_scores
    ).astype('int64')
    
    # Note: Removed minimum score logic as the new additive formula 
    # already produces appropriate base scores for high-value accounts
    
    # Tier drop boost (vectorized)
    tier_hierarchy = ["NIL", "Tier 1", "Tier 2", "Tier 3", "Tier 4", "High Value", "Key Account"]
    tier_to_index = {tier: i for i, tier in enumerate(tier_hierarchy)}
    
    # Calculate tier drops
    has_previous_tier = df['Previous_Tier'].notna() & (df['Previous_Tier'] != df['Current_Tier'])
    
    if has_previous_tier.any():
        current_tier_indices = df.loc[has_previous_tier, 'Current_Tier'].map(tier_to_index).fillna(0)
        previous_tier_indices = df.loc[has_previous_tier, 'Previous_Tier'].map(tier_to_index).fillna(0)
        
        tier_drops = previous_tier_indices - current_tier_indices
        tier_drop_mask = tier_drops > 0
        
        if tier_drop_mask.any():
            # Apply tier drop boosts
            major_drop_mask = tier_drops >= 3
            significant_drop_mask = (tier_drops >= 2) & (tier_drops < 3)
            minor_drop_mask = (tier_drops >= 1) & (tier_drops < 2)
            
            indices_with_drops = has_previous_tier[has_previous_tier].index
            
            # Create full boolean masks for safe indexing
            full_major_mask = has_previous_tier.copy()
            full_major_mask.loc[has_previous_tier] = tier_drop_mask & major_drop_mask
            full_significant_mask = has_previous_tier.copy()
            full_significant_mask.loc[has_previous_tier] = tier_drop_mask & significant_drop_mask
            full_minor_mask = has_previous_tier.copy()
            full_minor_mask.loc[has_previous_tier] = tier_drop_mask & minor_drop_mask
            
            # Apply boosts using full boolean masks (reduced values)
            priority_scores[full_major_mask] += 8      # Reduced from 15
            priority_scores[full_significant_mask] += 5  # Reduced from 10
            priority_scores[full_minor_mask] += 3       # Reduced from 5
    
    # Annual reachout boost (fully vectorized)
    annual_mask = (
        (df['Event_Frequency_Current'] == 'Annual') &
        df['months_active_current'].notna() &
        (~df['has_event_creation_current']) &
        (df['Rating'] != 'Churned')
    )
    
    if annual_mask.any():
        current_month = datetime.now().month
        month_name_to_number = {calendar.month_name[i]: i for i in range(1, 13)}
        
        # Optimized annual reachout boost
        annual_boost_mask = pd.Series(False, index=df.index)
        
        # Process annual accounts with optimized list comprehensions
        annual_accounts = df[annual_mask]
        months_to_check = {1, 2}  # Months away to trigger boost
        
        # Vectorized processing using list comprehension for efficiency
        for idx in annual_accounts.index:
            months_active = annual_accounts.loc[idx, 'months_active_current']
            if isinstance(months_active, list) and months_active:
                # Efficient month conversion and timing check
                month_numbers = [
                    month_name_to_number[month_name] 
                    for month_name in months_active 
                    if isinstance(month_name, str) and month_name in month_name_to_number
                ]
                
                if month_numbers:
                    # Check if any event is 1-2 months away (vectorized calculation)
                    months_until = {(month_num - current_month) % 12 for month_num in month_numbers}
                    if months_until & months_to_check:  # Set intersection for efficiency
                        annual_boost_mask[idx] = True
        
        # Apply boost using vectorized maximum (reduced value)
        if annual_boost_mask.any():
            priority_scores[annual_boost_mask] = np.maximum(priority_scores[annual_boost_mask], 12)
    
    # Rapid drop alert boosting (vectorized)
    # Moderate rapid drops (score 2)
    moderate_rapid_mask = rapid_drop_alerts == 2
    priority_scores[moderate_rapid_mask] = np.maximum(priority_scores[moderate_rapid_mask], 15)
    
    # Severe rapid drops (score 3)
    severe_rapid_mask = rapid_drop_alerts == 3
    priority_scores[severe_rapid_mask] = np.maximum(priority_scores[severe_rapid_mask], 20)
    
    # High-value accounts with significant drops (score 2) - boost to High/Very High boundary
    high_value_significant_mask = (
        (rapid_drop_alerts == 2) & 
        df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4'])
    )
    priority_scores[high_value_significant_mask] = np.maximum(priority_scores[high_value_significant_mask], 19)
    
    # Critical rapid drops for top accounts (score 3) - definitely Very High
    critical_rapid_mask = (
        (rapid_drop_alerts == 3) & 
        df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4'])
    )
    priority_scores[critical_rapid_mask] = 23  # Near max to ensure Very High
    
    # Cap maximum score to prevent excessive "Very High" classifications
    # This ensures a more reasonable distribution across priority levels
    MAX_PRIORITY_SCORE = 25
    priority_scores = priority_scores.clip(upper=MAX_PRIORITY_SCORE)
    
    return priority_scores


def categorize_priorities(priority_scores):
    """
    Vectorized priority categorization.
    
    Args:
        priority_scores: Series of numeric priority scores
        
    Returns:
        pd.Series: Priority categories
    """
    # Use pd.cut for efficient categorization
    # Adjusted thresholds for better distribution
    categories = pd.cut(
        priority_scores,
        bins=[-np.inf, 0, 5, 10, 18, np.inf],
        labels=['Excluded', 'Low', 'Medium', 'High', 'Very High'],
        include_lowest=True
    ).astype(str)
    
    # Handle negative scores specifically
    categories[priority_scores < 0] = 'Excluded'
    
    return categories


def calculate_revenue_drop_category(current_revenue, previous_revenue):
    """
    Calculate the revenue drop category based on current vs previous revenue.
    
    Args:
        current_revenue: Revenue in current period
        previous_revenue: Revenue in previous period
        
    Returns:
        str: Revenue drop category (Severe/Significant/Moderate/Stable)
    """
    # Handle edge cases
    if previous_revenue <= 0:
        if current_revenue > 0:
            return "Stable"  # Growth from zero
        else:
            return "Stable"  # No revenue in either period - not a drop
    
    # Calculate percentage drop
    drop_percentage = ((previous_revenue - current_revenue) / previous_revenue) * 100
    
    # Categorize based on drop percentage
    if drop_percentage > 75:
        return "Severe"
    elif drop_percentage > 50:
        return "Significant"
    elif drop_percentage > 25:
        return "Moderate"
    else:
        return "Stable"


def get_revenue_drop_score(revenue_drop_category):
    """
    Get the score for a revenue drop category.
    
    Args:
        revenue_drop_category: Revenue drop category string
        
    Returns:
        int: Revenue drop score (0-3)
    """
    revenue_scores = {
        "Severe": 3,       # >75% drop
        "Significant": 2,  # 50-75% drop
        "Moderate": 1,     # 25-50% drop
        "Stable": 0        # <25% drop or growth
    }
    
    return revenue_scores.get(revenue_drop_category, 0)