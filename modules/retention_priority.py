"""
Retention priority scoring for TryBooking accounts.
"""
import pandas as pd
import numpy as np
from datetime import datetime
import calendar


def calculate_retention_priorities(df):
    """
    Retention priority calculation for entire DataFrame.
    
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
    
    # Define tier weights
    tier_weights = {
        "Key Account": 5,
        "High Value": 4, 
        "Tier 4": 3,
        "Tier 3": 2,
        "Tier 2": 1,
        "Tier 1": 1,
        "NIL": 1
    }
    
    # Define rating severity
    # Hybrid AU/UK rating values
    rating_severity = {
        # Excluded from priority scoring
        "Churned": -1,
        "Suspended or Closed": -1,
        "Unactivated": -1,
        "Never Logged In": -1,
        "Never Transacted": -1,
        # Active priority ratings
        "At Risk": 5,
        "Outreach": 3,
        "Re-Activated": 2,  # Was "Returned" - renamed to match AU
        "Active Paid": 0,
        "Active Free": 0,
        # Legacy values for backwards compatibility
        "Returned": 2,
        "New": 2,
        "Active": 0,
        "Inactive": 0
    }
    
    # Tier weight calculation
    tier_weight_series = df['Current_Tier'].map(tier_weights).fillna(1)
    
    # Rating severity calculation
    rating_severity_series = df['Rating'].map(rating_severity).fillna(0)
    
    # Revenue score handling
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
    
    # Clear rapid drop alerts for new accounts (shouldn't have year-over-year comparisons)
    if 'Years_Loyalty' in df.columns:
        is_new_account = df['Years_Loyalty'] <= 1
        rapid_drop_alerts[is_new_account] = 0
    
    # Also clear for accounts that upgraded from NIL tier
    if 'Previous_Tier' in df.columns:
        upgraded_from_nil = (
            (df['Previous_Tier'].isna() | (df['Previous_Tier'] == '') | (df['Previous_Tier'] == 'NIL')) &
            df['Current_Tier'].notna() & (df['Current_Tier'] != '') & (df['Current_Tier'] != 'NIL')
        )
        rapid_drop_alerts[upgraded_from_nil] = 0
    
    # Clear rapid drop alerts for schools during summer holidays
    if 'Industry' in df.columns:
        current_date = datetime.now()
        current_month = current_date.month
        
        # Education accounts get rapid drop alerts suppressed June-September
        is_education = df['Industry'] == 'Education'
        is_summer_period = current_month in [6, 7, 8, 9]
        
        if is_summer_period:
            rapid_drop_alerts[is_education] = 0
    
    # Reduce rapid drop alerts for accounts still selling tickets (even if free)
    # These accounts are still active, just not generating revenue recently
    if 'tickets_current' in df.columns and 'tickets_prev' in df.columns:
        # Check if account maintains significant ticket activity
        has_ticket_activity = (
            (df['tickets_current'] >= 100) &  # Still selling meaningful tickets
            (df['tickets_current'] >= df['tickets_prev'] * 0.5)  # At least 50% of previous volume
        )
        
        # If they have good ticket activity but rapid drop alert, reduce severity
        # Score 3 → 2, Score 2 → 1, Score 1 → 0
        rapid_drop_alerts[has_ticket_activity & (rapid_drop_alerts > 0)] -= 1
    
    # Adjust rapid drop alerts based on account patterns
    if 'Event_Frequency_Current' in df.columns:
        # Only apply rapid drop detection to Continuous/Regular accounts (as originally designed)
        is_continuous_regular = df['Event_Frequency_Current'].isin(['Continuous', 'Regular'])
        
        # Clear rapid drops for Annual/Seasonal accounts - they naturally have periods of no activity
        is_annual_seasonal = df['Event_Frequency_Current'].isin(['Annual', 'Seasonal'])
        rapid_drop_alerts[is_annual_seasonal] = 0
        
        # For accounts that changed from Continuous/Regular to Annual/Seasonal, this is concerning
        # Keep their rapid drop alerts as this indicates a significant pattern change
        if 'Event_Frequency_Previous' in df.columns:
            pattern_degraded = (
                df['Event_Frequency_Previous'].isin(['Continuous', 'Regular']) &
                df['Event_Frequency_Current'].isin(['Annual', 'Seasonal'])
            )
            # For these accounts, restore the rapid drop alert if it was cleared
            if 'rapid_drop_alert' in df.columns:
                original_alerts = pd.to_numeric(df['rapid_drop_alert'], errors='coerce').fillna(0).clip(0, 3)
                rapid_drop_alerts[pattern_degraded] = original_alerts[pattern_degraded]
    
    # For Continuous/Regular accounts with rapid drops, check if it's seasonal
    # This would require comparing to same period last year
    # Without that data, we keep the rapid drop alert but can reduce severity based on context
    # The revenue_drop_score (year-over-year) provides additional context
    
    # Calculate base priority
    # Use a more balanced formula to prevent excessive scores
    # Old formula: tier_weight × (rating_severity + revenue_score) could yield 40+
    # New formula: (tier_weight × 2) + rating_severity + revenue_score yields max ~18
    priority_scores = (
        (tier_weight_series * 2) + rating_severity_series + revenue_scores
    ).astype('int64')
    
    # Note: Removed minimum score logic as the new additive formula 
    # already produces appropriate base scores for high-value accounts
    
    # Tier drop boost
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
            
            # Apply boosts using full boolean masks
            priority_scores[full_major_mask] += 6      # Major tier drop boost
            priority_scores[full_significant_mask] += 4  # Significant tier drop boost
            priority_scores[full_minor_mask] += 2       # Minor tier drop boost
    
    # Annual reachout boost
    annual_mask = (
        (df['Event_Frequency_Current'] == 'Annual') &
        df['months_active_current'].notna() &
        (~df['has_event_creation_current']) &
        (~df['Rating'].isin(['Churned', 'Suspended or Closed', 'Unactivated', 'Never Logged In', 'Never Transacted']))
    )
    
    if annual_mask.any():
        current_date = datetime.now()
        current_month = current_date.month
        current_day = current_date.day
        month_name_to_number = {calendar.month_name[i]: i for i in range(1, 13)}
        
        # Annual reachout boost with lead time consideration
        annual_accounts = df[annual_mask].copy()
        
        # Get average lead days, default to 60 if not available
        if 'avg_lead_days' in annual_accounts.columns:
            annual_accounts['lead_days'] = annual_accounts['avg_lead_days'].fillna(60)
        else:
            annual_accounts['lead_days'] = 60
        
        # Month processing with lead time
        def check_upcoming_months_with_lead(row):
            months_list = row['months_active_current']
            lead_days = row['lead_days'] if pd.notna(row['lead_days']) else 60
            
            if not isinstance(months_list, list) or not months_list:
                return False
            
            # Convert month names to numbers
            month_numbers = [
                month_name_to_number.get(month_name, 0)
                for month_name in months_list
                if isinstance(month_name, str)
            ]
            
            if not month_numbers:
                return False
            
            # Check if we need to reach out based on lead time
            for month_num in month_numbers:
                # Calculate days until the first of the event month
                if month_num >= current_month:
                    # Event is this year
                    days_until_event = (month_num - current_month) * 30 + (1 - current_day)
                else:
                    # Event is next year
                    days_until_event = ((12 - current_month) + month_num) * 30 + (1 - current_day)
                
                # Calculate when they typically start selling
                days_until_sales_start = days_until_event - lead_days
                
                # Boost if:
                # 1. Sales should start in next 30-60 days (early warning), OR
                # 2. We're past when sales should have started but event hasn't happened yet
                if days_until_sales_start <= 60 and days_until_event > 0:
                    return True
            return False
        
        # Apply check with lead time
        annual_boost_mask = annual_accounts.apply(check_upcoming_months_with_lead, axis=1)
        
        # Apply boost using element-wise maximum
        # Set to 11 to ensure High priority (10-15 range)
        boost_indices = annual_accounts[annual_boost_mask].index
        if len(boost_indices) > 0:
            priority_scores.loc[boost_indices] = np.maximum(priority_scores.loc[boost_indices], 11)
    
    # Rapid drop alert boosting
    # Moderate rapid drops (score 2)
    moderate_rapid_mask = rapid_drop_alerts == 2
    priority_scores[moderate_rapid_mask] = np.maximum(priority_scores[moderate_rapid_mask], 13)
    
    # Severe rapid drops (score 3)
    severe_rapid_mask = rapid_drop_alerts == 3
    priority_scores[severe_rapid_mask] = np.maximum(priority_scores[severe_rapid_mask], 17)
    
    # High-value accounts with significant drops (score 2) - boost but check other factors
    high_value_significant_mask = (
        (rapid_drop_alerts == 2) & 
        df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4'])
    )
    # Only push to Very High if they also have other risk factors
    # Otherwise keep in High priority
    at_risk_mask = df['Rating'].isin(['At Risk', 'Outreach'])
    
    # If At Risk/Outreach + rapid drop = Very High
    high_value_sig_at_risk = high_value_significant_mask & at_risk_mask
    priority_scores[high_value_sig_at_risk] = np.maximum(priority_scores[high_value_sig_at_risk], 16)
    
    # If Active but rapid drop = High priority  
    high_value_sig_active = high_value_significant_mask & (~at_risk_mask)
    priority_scores[high_value_sig_active] = np.maximum(priority_scores[high_value_sig_active], 13)
    
    # Critical rapid drops for top accounts (score 3) - Very High only if truly at risk
    if 'revenue_current' in df.columns:
        # Check for actual revenue decline (not just ticket drops)
        has_revenue_decline = pd.Series(True, index=df.index)  # Default to True
        
        if 'revenue_prev' in df.columns:
            # Exclude accounts with revenue growth or stable low revenue
            has_revenue_decline = (
                (df['revenue_current'] < df['revenue_prev']) &  # Revenue decreased
                (df['revenue_prev'] >= 100)  # And previous revenue was meaningful
            )
        
        # Also exclude accounts that upgraded tiers (NIL to something)
        tier_upgraded = pd.Series(False, index=df.index)
        if 'Previous_Tier' in df.columns:
            tier_upgraded = (
                (df['Previous_Tier'].isna() | (df['Previous_Tier'] == '') | (df['Previous_Tier'] == 'NIL')) &
                df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1'])
            )
        
        # For rapid drop score 3, also check the revenue drop category
        # Don't treat as critical if revenue drop is only Moderate or Stable
        has_severe_revenue_drop = pd.Series(False, index=df.index)
        if 'revenue_drop_score' in df.columns:
            # Check for Severe or Significant drops only
            severe_drop_string = df['revenue_drop_score'].isin(['Severe', 'Significant'])
            severe_drop_numeric = pd.to_numeric(df['revenue_drop_score'], errors='coerce') >= 2
            has_severe_revenue_drop = severe_drop_string | severe_drop_numeric
        
        critical_rapid_mask = (
            (rapid_drop_alerts == 3) & 
            df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4']) &
            has_revenue_decline &
            has_severe_revenue_drop &  # Must have severe/significant revenue drop too
            (~tier_upgraded)  # Exclude tier upgrades
        )
        priority_scores[critical_rapid_mask] = 19  # Top of Very High range
    else:
        # Fallback if revenue columns not available
        critical_rapid_mask = (
            (rapid_drop_alerts == 3) & 
            df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4'])
        )
        priority_scores[critical_rapid_mask] = 19
    
    # School summer holiday adjustments
    if 'Industry' in df.columns:
        education_mask = df['Industry'] == 'Education'
        
        if education_mask.any():
            current_date = datetime.now()
            current_month = current_date.month
            current_day = current_date.day
            
            # Determine Scottish schools (postcodes starting with specific letters)
            scottish_postcodes = ['AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE']
            
            # Check both AccountPostcode and EventPostcode for Scottish locations
            is_scottish = pd.Series(False, index=df.index)
            
            if 'AccountPostcode' in df.columns:
                account_scottish = df['AccountPostcode'].fillna('').str.upper().str[:2].isin(scottish_postcodes) | \
                                   df['AccountPostcode'].fillna('').str.upper().str[:1].isin(['G'])  # G postcodes are single letter
                is_scottish |= account_scottish
            
            if 'EventPostcode' in df.columns:
                event_scottish = df['EventPostcode'].fillna('').str.upper().str[:2].isin(scottish_postcodes) | \
                                 df['EventPostcode'].fillna('').str.upper().str[:1].isin(['G'])  # G postcodes are single letter
                is_scottish |= event_scottish
            
            # Define holiday periods
            # Scottish schools: mid-June (15th) to mid-August (15th)
            scottish_holiday = (
                ((current_month == 6) & (current_day >= 15)) |
                (current_month == 7) |
                ((current_month == 8) & (current_day <= 15))
            )
            
            # English/Welsh schools: July to early September (7th)
            english_holiday = (
                (current_month == 7) |
                (current_month == 8) |
                ((current_month == 9) & (current_day <= 7))
            )
            
            # Apply summer holiday priority reduction
            scottish_schools_on_holiday = education_mask & is_scottish & scottish_holiday
            english_schools_on_holiday = education_mask & (~is_scottish) & english_holiday
            
            # Cap priority scores for schools on holiday to ensure "Low" priority
            # Max score of 5 ensures they fall into "Low" category (threshold is 5)
            schools_on_holiday = scottish_schools_on_holiday | english_schools_on_holiday
            priority_scores[schools_on_holiday] = np.minimum(priority_scores[schools_on_holiday], 5)
    
    # Cap maximum score to prevent excessive "Very High" classifications
    # This ensures a more reasonable distribution across priority levels
    MAX_PRIORITY_SCORE = 20
    priority_scores = priority_scores.clip(upper=MAX_PRIORITY_SCORE)
    
    return priority_scores


def categorize_priorities(priority_scores):
    """
    Priority categorisation.
    
    Args:
        priority_scores: Series of numeric priority scores
        
    Returns:
        pd.Series: Priority categories
    """
    # Use pd.cut for efficient categorization
    # Thresholds: 0-5=Low, 6-10=Medium, 11-15=High, 16-20=Very High
    categories = pd.cut(
        priority_scores,
        bins=[-np.inf, 0, 5, 10, 15, 20, np.inf],
        labels=['Excluded', 'Low', 'Medium', 'High', 'Very High', 'Critical'],
        include_lowest=True
    ).astype(str)
    
    # Handle negative scores specifically
    categories[priority_scores < 0] = 'Excluded'
    
    # Merge Critical into Very High to maintain 5 categories
    categories[categories == 'Critical'] = 'Very High'
    
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