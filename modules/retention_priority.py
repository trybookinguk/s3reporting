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
        "At Risk": 7,
        "Outreach": 4,
        "New": 3,
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
    
    # Handle string revenue scores
    string_revenue_mask = df['revenue_drop_score'].astype(str).str.contains('evere|ignificant|oderate', na=False)
    revenue_score_map = {'Severe': 3, 'Significant': 2, 'Moderate': 1, 'Stable': 0}
    
    if string_revenue_mask.any():
        revenue_scores[string_revenue_mask] = df.loc[string_revenue_mask, 'revenue_drop_score'].map(revenue_score_map).fillna(0)
    
    # Handle numeric revenue scores
    numeric_revenue_mask = pd.to_numeric(df['revenue_drop_score'], errors='coerce').notna()
    if numeric_revenue_mask.any():
        revenue_scores[numeric_revenue_mask] = pd.to_numeric(
            df.loc[numeric_revenue_mask, 'revenue_drop_score'], errors='coerce'
        ).clip(0, 3).fillna(0)
    
    # Validate rapid drop alert
    rapid_drop_alerts = pd.to_numeric(df['rapid_drop_alert'], errors='coerce').fillna(0).clip(0, 3)
    
    # Calculate base priority (vectorized)
    priority_scores = (tier_weight_series * (rating_severity_series + revenue_scores)).astype('int64')
    
    # High-value tier minimum priority (vectorized)
    high_value_tiers = ['Key Account', 'High Value', 'Tier 4', 'Tier 3']
    high_value_mask = df['Current_Tier'].isin(high_value_tiers) & (df['Rating'] != 'Churned')
    priority_scores[high_value_mask] = np.maximum(priority_scores[high_value_mask], 10)
    
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
            
            # Major drop boost
            if major_drop_mask.any():
                major_drop_indices = indices_with_drops[major_drop_mask[tier_drop_mask]]
                priority_scores[major_drop_indices] += 15
            
            # Significant drop boost
            if significant_drop_mask.any():
                significant_drop_indices = indices_with_drops[significant_drop_mask[tier_drop_mask]]
                priority_scores[significant_drop_indices] += 10
            
            # Minor drop boost
            if minor_drop_mask.any():
                minor_drop_indices = indices_with_drops[minor_drop_mask[tier_drop_mask]]
                priority_scores[minor_drop_indices] += 5
    
    # Annual reachout boost (vectorized)
    annual_mask = (
        (df['Event_Frequency_Current'] == 'Annual') &
        df['months_active_current'].notna() &
        (~df['has_event_creation_current']) &
        (df['Rating'] != 'Churned')
    )
    
    if annual_mask.any():
        current_month = datetime.now().month
        month_name_to_number = {calendar.month_name[i]: i for i in range(1, 13)}
        
        # Check each annual account
        for idx in df[annual_mask].index:
            months_active = df.loc[idx, 'months_active_current']
            if isinstance(months_active, list):
                for active_month_name in months_active:
                    if active_month_name in month_name_to_number:
                        active_month_num = month_name_to_number[active_month_name]
                        months_until_event = (active_month_num - current_month) % 12
                        
                        if months_until_event in [1, 2]:
                            priority_scores[idx] = max(priority_scores[idx], 18)
                            break
    
    # Rapid drop alert boosting (vectorized)
    rapid_drop_high_mask = rapid_drop_alerts >= 2
    priority_scores[rapid_drop_high_mask] = np.maximum(priority_scores[rapid_drop_high_mask], 70)
    
    # Critical rapid drops for key accounts
    critical_rapid_mask = (
        (rapid_drop_alerts == 3) & 
        df['Current_Tier'].isin(['Key Account', 'High Value'])
    )
    priority_scores[critical_rapid_mask] = 90
    
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
    categories = pd.cut(
        priority_scores,
        bins=[-np.inf, 0, 10, 15, 20, np.inf],
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
        return "Stable" if current_revenue > 0 else "Severe"
    
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