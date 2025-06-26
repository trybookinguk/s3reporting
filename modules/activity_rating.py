"""
Vectorized activity rating calculation for TryBooking accounts.
Ultra-fast bulk pandas operations for analyzing account behavior patterns.
"""
import pandas as pd
import numpy as np
from datetime import datetime


def calculate_activity_ratings(df):
    """
    Vectorized activity rating calculation for entire DataFrame.
    
    Args:
        df: DataFrame with columns:
            - Event_Frequency_Current, Event_Frequency_Previous
            - days_since_last, has_historical, avg_lead_days
            - last_event_date, months_active_historical
            - Industry, Current_Tier, Postcode, account_created_date
    
    Returns:
        pd.Series: Activity ratings for all accounts
    """
    # Initialize result series
    ratings = pd.Series('Active', index=df.index)
    
    # Vectorized date calculations
    today = pd.Timestamp.now().date()
    
    # Calculate days since account creation (vectorized)
    days_since_created = pd.Series(None, index=df.index, dtype='float64')
    valid_created_dates = df['account_created_date'].notna()
    if valid_created_dates.any():
        days_since_created[valid_created_dates] = (
            today - df.loc[valid_created_dates, 'account_created_date']
        ).dt.days
    
    # 1. NEW ACCOUNTS (vectorized)
    new_account_mask = (
        (days_since_created <= 14) & 
        (df['Event_Frequency_Current'] == 'Inactive') & 
        (~df['has_historical'])
    )
    ratings[new_account_mask] = 'New'
    
    # 2. RECENTLY CREATED BUT INACTIVE (vectorized)
    recently_created_inactive = (
        days_since_created.notna() & 
        (df['Event_Frequency_Current'] == 'Inactive') & 
        (~df['has_historical']) &
        (~new_account_mask)
    )
    
    # Churned: Created >28 days ago, no activity
    churned_recent = recently_created_inactive & (days_since_created > 28)
    ratings[churned_recent] = 'Churned'
    
    # At Risk: Created 15-28 days ago, no activity  
    at_risk_recent = recently_created_inactive & (days_since_created > 14) & (days_since_created <= 28)
    ratings[at_risk_recent] = 'At Risk'
    
    # 3. RETURNED ACCOUNTS (vectorized)
    returned_mask = (
        (df['Event_Frequency_Current'] != 'Inactive') & 
        (df['Event_Frequency_Previous'] == 'Inactive')
    )
    ratings[returned_mask] = 'Returned'
    
    # 4. HIGH-TIER ANNUAL/SEASONAL ACCOUNTS (vectorized)
    high_tier_mask = df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4', 'Tier 3'])
    annual_seasonal_mask = df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal'])
    has_last_event = df['last_event_date'].notna()
    has_historical_activity = df['Event_Frequency_Previous'] != 'Inactive'
    
    high_tier_annual_seasonal = (
        high_tier_mask & annual_seasonal_mask & has_last_event & has_historical_activity &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask)
    )
    
    if high_tier_annual_seasonal.any():
        # Vectorized date calculations for high-tier accounts
        subset = df[high_tier_annual_seasonal].copy()
        
        # Convert last_event_date to pandas datetime for calculations
        last_event_ts = pd.to_datetime(subset['last_event_date'])
        
        # Calculate expected dates (vectorized)
        avg_leads = subset['avg_lead_days'].fillna(60)
        expected_next_with_grace = (last_event_ts + pd.Timedelta(days=365+30)).dt.date
        expected_sale_start = (last_event_ts + pd.Timedelta(days=365) - pd.to_timedelta(avg_leads, unit='D')).dt.date
        outreach_date = (expected_sale_start - pd.Timedelta(days=30)).dt.date
        
        # Apply conditions (vectorized)
        churned_high_tier = today >= expected_next_with_grace
        at_risk_high_tier = (~churned_high_tier) & (today >= expected_sale_start)
        outreach_high_tier = (~churned_high_tier) & (~at_risk_high_tier) & (today >= outreach_date)
        
        # Update ratings
        high_tier_indices = subset.index
        ratings[high_tier_indices[churned_high_tier]] = 'Churned'
        ratings[high_tier_indices[at_risk_high_tier]] = 'At Risk'
        ratings[high_tier_indices[outreach_high_tier]] = 'Outreach'
    
    # 5. REGULAR/CONTINUOUS ACCOUNTS (vectorized)
    regular_continuous_mask = (
        df['Event_Frequency_Previous'].isin(['Regular', 'Continuous']) &
        has_historical_activity &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask) & (~high_tier_annual_seasonal)
    )
    
    if regular_continuous_mask.any():
        subset = df[regular_continuous_mask].copy()
        
        # Education industry detection (vectorized)
        is_education = (subset['Industry'] == 'Education') | subset['months_active_historical'].apply(
            lambda x: is_education_pattern(x) if isinstance(x, list) and x else False
        )
        
        # Scottish postcode detection (vectorized)
        is_scottish = subset['Postcode'].apply(lambda x: is_scottish_postcode(x) if pd.notna(x) else False)
        
        # Summer programs detection (vectorized)
        runs_summer_programs = subset['months_active_historical'].apply(
            lambda x: any(month in [7, 8] for month in x) if isinstance(x, list) and x else False
        )
        
        # Calculate adjusted days for education accounts
        days_to_check = subset['days_since_last'].copy()
        
        # For education accounts that don't run summer programs, could adjust calculation
        # For now using standard days (complex summer holiday logic can be added later)
        
        # Apply thresholds based on account type (vectorized)
        continuous_accounts = subset['Event_Frequency_Previous'] == 'Continuous'
        regular_accounts = subset['Event_Frequency_Previous'] == 'Regular'
        
        # Continuous account thresholds
        continuous_churned = continuous_accounts & (days_to_check >= 90)
        continuous_at_risk = continuous_accounts & (days_to_check >= 30) & (~continuous_churned)
        
        # Regular account thresholds  
        regular_churned = regular_accounts & (days_to_check >= 180)
        regular_at_risk = regular_accounts & (days_to_check >= 90) & (~regular_churned)
        
        # Update ratings
        reg_cont_indices = subset.index
        ratings[reg_cont_indices[continuous_churned | regular_churned]] = 'Churned'
        ratings[reg_cont_indices[continuous_at_risk | regular_at_risk]] = 'At Risk'
    
    # 6. DEFAULT FALLBACK FOR OTHER PATTERNS (vectorized)
    other_patterns_mask = (
        has_historical_activity &
        (~df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal', 'Regular', 'Continuous', 'Inactive'])) &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask) & 
        (~high_tier_annual_seasonal) & (~regular_continuous_mask)
    )
    
    if other_patterns_mask.any():
        other_churned = other_patterns_mask & (df['days_since_last'] >= 365)
        other_at_risk = other_patterns_mask & (df['days_since_last'] >= 180) & (~other_churned)
        
        ratings[other_churned] = 'Churned'
        ratings[other_at_risk] = 'At Risk'
    
    # 7. INACTIVE ACCOUNTS (vectorized)
    inactive_mask = (
        (df['Event_Frequency_Current'] == 'Inactive') &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask)
    )
    ratings[inactive_mask] = 'Inactive'
    
    return ratings


def is_education_pattern(months_list):
    """Detect education pattern from months active."""
    if not months_list or not isinstance(months_list, list):
        return False
    
    # Education pattern: active during term time, minimal summer activity
    term_months = set(range(9, 13)) | set(range(1, 7))  # Sept-Dec, Jan-June
    summer_months = {7, 8}  # July, August
    
    months_set = set(months_list)
    term_activity = len(months_set & term_months)
    summer_activity = len(months_set & summer_months)
    
    return term_activity >= 4 and summer_activity <= 1


def is_scottish_postcode(postcode):
    """Check if postcode is Scottish."""
    if not postcode or not isinstance(postcode, str):
        return False
    
    scottish_areas = {
        'AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 
        'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'
    }
    
    # Extract letter portion
    area = postcode.strip().upper()
    letter_portion = ''.join(char for char in area if char.isalpha())[:2]
    
    return letter_portion in scottish_areas or letter_portion == 'G'