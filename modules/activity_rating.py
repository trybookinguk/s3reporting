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
        # Ensure account_created_date is in datetime format
        created_dates = pd.to_datetime(df.loc[valid_created_dates, 'account_created_date'], errors='coerce')
        valid_dates_mask = created_dates.notna()
        if valid_dates_mask.any():
            calculated_days = (
                pd.Timestamp(today) - created_dates[valid_dates_mask]
            ).dt.days
            # Prevent future dates from creating negative days
            calculated_days = calculated_days.clip(lower=0)
            days_since_created[created_dates[valid_dates_mask].index] = calculated_days
    
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
    at_risk_recent = recently_created_inactive & (days_since_created >= 15) & (days_since_created <= 28)
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
        
        # Education industry detection (optimized vectorized)
        is_education_industry = (subset['Industry'] == 'Education')
        
        # Optimized education pattern detection
        is_education_pattern_vec = pd.Series(False, index=subset.index)
        valid_months_mask = subset['months_active_historical'].notna()
        
        if valid_months_mask.any():
            # Pre-define sets for efficient intersection
            term_months = frozenset(range(9, 13)) | frozenset(range(1, 7))  # Sept-Dec, Jan-June
            summer_months = frozenset([7, 8])  # July, August
            
            # Vectorized pattern check using list comprehension
            months_data = subset.loc[valid_months_mask, 'months_active_historical']
            pattern_results = [
                (len(set(months) & term_months) >= 4 and len(set(months) & summer_months) <= 1)
                if isinstance(months, list) and months else False
                for months in months_data
            ]
            is_education_pattern_vec.loc[valid_months_mask] = pattern_results
        
        is_education = is_education_industry | is_education_pattern_vec
        
        # Scottish postcode detection (fully vectorized)
        scottish_areas = {'AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 
                         'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'}
        
        # Vectorized postcode processing
        valid_postcodes = subset['Postcode'].notna() & (subset['Postcode'] != '')
        is_scottish = pd.Series(False, index=subset.index)
        
        if valid_postcodes.any():
            # Extract letter portion using vectorized string operations
            letter_portions = (
                subset.loc[valid_postcodes, 'Postcode']
                .str.upper()
                .str.extract(r'^([A-Z]{1,2})', expand=False)
                .fillna('')
            )
            is_scottish[valid_postcodes] = letter_portions.isin(scottish_areas)
        
        # Summer programs detection (optimized vectorized)
        runs_summer_programs = pd.Series(False, index=subset.index)
        valid_months_mask = subset['months_active_historical'].notna()
        
        if valid_months_mask.any():
            summer_months = frozenset([7, 8])
            # Vectorized summer detection using list comprehension
            months_data = subset.loc[valid_months_mask, 'months_active_historical']
            summer_results = [
                bool(set(months) & summer_months)
                if isinstance(months, list) and months else False
                for months in months_data
            ]
            runs_summer_programs.loc[valid_months_mask] = summer_results
        
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
    
    # Final validation: check for accounts that remained 'Active' without proper classification
    potential_issues = (
        (ratings == 'Active') &
        (df['Event_Frequency_Current'] == 'Inactive') & 
        df['has_historical']
    )
    if potential_issues.any():
        ratings[potential_issues] = 'Dormant'  # More appropriate than 'Active'
    
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
    
    return letter_portion in scottish_areas