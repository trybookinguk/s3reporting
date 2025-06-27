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
    # Accounts created in the last 14 days with no bookings
    new_account_mask = (
        (days_since_created <= 14) & 
        (days_since_created >= 0) &  # Exclude invalid/future dates
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
    # Only mark as Returned if they truly existed before and went away
    
    # For accounts to be "Returned", they must have:
    # 1. Current activity (not inactive)
    # 2. Previous period was inactive
    # 3. Actually existed before (not just selling tickets for a future event)
    
    # Years_Loyalty alone can be misleading for annual events that sell across periods
    # Better indicators of truly returning:
    existed_before = pd.Series(False, index=df.index)  # Default to False
    
    if 'Years_Loyalty' in df.columns:
        # Only consider returned if they have 2+ years AND had a previous tier
        if 'Previous_Tier' in df.columns:
            had_meaningful_previous = (
                (df['Years_Loyalty'] > 1) &
                df['Previous_Tier'].notna() & 
                (df['Previous_Tier'] != '') & 
                (df['Previous_Tier'] != 'NIL')
            )
            existed_before = had_meaningful_previous
        else:
            # Fallback if no Previous_Tier column
            existed_before = df['Years_Loyalty'] > 1
    
    # Only consider "Returned" if previous frequency was explicitly 'Inactive' (not null/blank)
    had_inactive_previous = (
        df['Event_Frequency_Previous'].notna() & 
        (df['Event_Frequency_Previous'] == 'Inactive')
    )
    
    returned_mask = (
        (df['Event_Frequency_Current'] != 'Inactive') & 
        had_inactive_previous &
        existed_before
    )
    ratings[returned_mask] = 'Returned'
    
    # 4. HIGH-TIER ANNUAL/SEASONAL ACCOUNTS (vectorized)
    high_tier_mask = df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4', 'Tier 3'])
    # Handle null values in Event_Frequency_Previous
    annual_seasonal_mask = (
        df['Event_Frequency_Previous'].notna() & 
        df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal'])
    )
    has_last_event = df['last_event_date'].notna()
    # Historical activity means they had a non-inactive previous frequency (including null is ok)
    has_historical_activity = (
        df['Event_Frequency_Previous'].isna() |  # No previous data is ok
        (df['Event_Frequency_Previous'] != 'Inactive')
    )
    
    # Only check annual/seasonal patterns for accounts that are currently inactive
    currently_inactive = df['Event_Frequency_Current'] == 'Inactive'
    
    high_tier_annual_seasonal = (
        high_tier_mask & annual_seasonal_mask & has_last_event & has_historical_activity &
        currently_inactive &  # Only check if currently inactive
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
        expected_sale_start_ts = (last_event_ts + pd.Timedelta(days=365) - pd.to_timedelta(avg_leads, unit='D'))
        expected_sale_start = expected_sale_start_ts.dt.date
        # Changed: Outreach now starts 60 days before expected sales (matching retention priority)
        outreach_date = (expected_sale_start_ts - pd.Timedelta(days=60)).dt.date
        
        # Apply conditions (vectorized)
        churned_high_tier = today >= expected_next_with_grace
        # Changed: At Risk is now between expected sales start and event date
        at_risk_high_tier = (~churned_high_tier) & (today >= expected_sale_start)
        # Outreach is 60 days before sales start
        outreach_high_tier = (~churned_high_tier) & (~at_risk_high_tier) & (today >= outreach_date)
        
        # Update ratings
        high_tier_indices = subset.index
        ratings[high_tier_indices[churned_high_tier]] = 'Churned'
        ratings[high_tier_indices[at_risk_high_tier]] = 'At Risk'
        ratings[high_tier_indices[outreach_high_tier]] = 'Outreach'
    
    # 5. REGULAR/CONTINUOUS ACCOUNTS (vectorized)
    regular_continuous_mask = (
        df['Event_Frequency_Previous'].notna() &  # Must have previous frequency data
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
            is_scottish.loc[valid_postcodes] = letter_portions.isin(scottish_areas).astype(bool)
        
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
        
        # Check current activity levels (if columns available)
        has_minimal_activity = pd.Series(True, index=subset.index)  # Default to considering gap
        
        if 'tickets_current' in df.columns and 'revenue_current' in df.columns:
            # Account has meaningful current activity if tickets >= 10 OR revenue >= £100
            has_meaningful_activity = (
                (subset['tickets_current'] >= 10) | 
                (subset['revenue_current'] >= 100)
            )
            has_minimal_activity = ~has_meaningful_activity
        
        # Continuous account thresholds
        # Only mark as churned if minimal/no current activity
        continuous_churned = continuous_accounts & (days_to_check >= 90) & has_minimal_activity
        continuous_at_risk = continuous_accounts & (days_to_check >= 30) & (~continuous_churned)
        
        # Regular account thresholds  
        # Only mark as churned if minimal/no current activity
        regular_churned = regular_accounts & (days_to_check >= 180) & has_minimal_activity
        regular_at_risk = regular_accounts & (days_to_check >= 90) & (~regular_churned)
        
        # Update ratings
        reg_cont_indices = subset.index
        ratings[reg_cont_indices[continuous_churned | regular_churned]] = 'Churned'
        ratings[reg_cont_indices[continuous_at_risk | regular_at_risk]] = 'At Risk'
    
    # 6. DEFAULT FALLBACK FOR OTHER PATTERNS (vectorized)
    # This catches any unusual patterns or accounts with null previous frequency
    other_patterns_mask = (
        has_historical_activity &
        (df['Event_Frequency_Previous'].isna() |  # No previous data
         (~df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal', 'Regular', 'Continuous', 'Inactive']))) &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask) & 
        (~high_tier_annual_seasonal) & (~regular_continuous_mask)
    )
    
    if other_patterns_mask.any():
        # Check for meaningful current activity
        has_minimal_activity_other = pd.Series(True, index=df.index)
        
        if 'tickets_current' in df.columns and 'revenue_current' in df.columns:
            has_meaningful_activity_other = (
                (df['tickets_current'] >= 10) | 
                (df['revenue_current'] >= 100)
            )
            has_minimal_activity_other = ~has_meaningful_activity_other
        
        # Only mark as churned if minimal/no current activity
        other_churned = other_patterns_mask & (df['days_since_last'] >= 365) & has_minimal_activity_other
        other_at_risk = other_patterns_mask & (df['days_since_last'] >= 180) & (~other_churned)
        
        ratings[other_churned] = 'Churned'
        ratings[other_at_risk] = 'At Risk'
    
    # 7. TIER LOSS AND SEVERE DROP CHECK (vectorized)
    # Check for accounts that have lost tier or had severe drops regardless of event frequency
    
    # Tier loss - accounts that had a tier but now don't
    has_lost_tier = (
        (df['Current_Tier'].isna() | (df['Current_Tier'] == '') | (df['Current_Tier'] == 'NIL')) &
        (df['Previous_Tier'].notna()) & 
        (df['Previous_Tier'] != '') & 
        (df['Previous_Tier'] != 'NIL')
    )
    
    # Check for severe revenue/ticket drops (only if previous revenue was meaningful)
    has_severe_drop = pd.Series(False, index=df.index)
    if 'revenue_drop_score' in df.columns and 'revenue_prev' in df.columns:
        # Only consider severe drops if previous revenue was >= £100
        had_meaningful_revenue = df['revenue_prev'] >= 100
        
        # Handle both string and numeric revenue drop scores
        severe_drop_string = df['revenue_drop_score'] == 'Severe'
        severe_drop_numeric = pd.to_numeric(df['revenue_drop_score'], errors='coerce') >= 3
        
        # Only flag as severe drop if they had meaningful revenue to lose
        has_severe_drop = (severe_drop_string | severe_drop_numeric) & had_meaningful_revenue
    
    # Check for zero activity despite having events
    has_zero_activity = pd.Series(False, index=df.index)
    if 'tickets_current' in df.columns and 'revenue_current' in df.columns:
        has_zero_activity = (
            (df['tickets_current'] == 0) & 
            (df['revenue_current'] == 0) &
            (df['Event_Frequency_Current'] != 'Inactive')
        )
    
    # Accounts that lost tier AND are inactive = Churned
    tier_loss_inactive = has_lost_tier & (df['Event_Frequency_Current'] == 'Inactive')
    ratings[tier_loss_inactive] = 'Churned'
    
    # Accounts that lost tier but still have some frequency = At Risk
    # OR accounts with severe drops/zero activity = At Risk
    at_risk_conditions = (
        (has_lost_tier & (df['Event_Frequency_Current'] != 'Inactive')) |
        (has_severe_drop & has_zero_activity) |
        (has_lost_tier & has_severe_drop)
    )
    # Don't override if already marked as Churned
    at_risk_mask = at_risk_conditions & (ratings != 'Churned')
    ratings[at_risk_mask] = 'At Risk'
    
    # 8. INACTIVE ACCOUNTS (vectorized)
    # Only mark as inactive if they haven't been classified already
    inactive_mask = (
        (df['Event_Frequency_Current'] == 'Inactive') &
        (~new_account_mask) & (~recently_created_inactive) & (~returned_mask) & 
        (~tier_loss_inactive) & (~at_risk_mask)
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
    
    # Fix misclassified "Returned" accounts that are actually new annual accounts
    # These are accounts with Years_Loyalty <= 1 that got marked as Returned
    if 'Years_Loyalty' in df.columns:
        misclassified_returned = (
            (ratings == 'Returned') & 
            (df['Years_Loyalty'] <= 1)
        )
        if misclassified_returned.any():
            # If they're currently active, mark as Active
            # If they're at risk based on other criteria, keep that
            ratings[misclassified_returned & (df['Event_Frequency_Current'] != 'Inactive')] = 'Active'
    
    # SAFETY NET: Override clearly incorrect 'Churned' ratings
    # This should rarely trigger if the earlier logic is correct
    # If this triggers frequently, it indicates a bug in the classification logic above
    
    # Check for accounts marked as 'Churned' but have significant current activity
    if 'tickets_current' in df.columns and 'revenue_current' in df.columns:
        # Only override if there's substantial current activity
        MIN_TICKETS_FOR_ACTIVE_OVERRIDE = 10
        MIN_REVENUE_FOR_ACTIVE_OVERRIDE = 100
        
        incorrectly_churned = (
            (ratings == 'Churned') & 
            ((df['tickets_current'] >= MIN_TICKETS_FOR_ACTIVE_OVERRIDE) |
             (df['revenue_current'] >= MIN_REVENUE_FOR_ACTIVE_OVERRIDE))
        )
        
        if incorrectly_churned.any():
            # Log warning as this indicates a potential bug in the classification logic
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Safety net triggered: {incorrectly_churned.sum()} accounts marked as 'Churned' despite significant activity")
            
            # Vectorized determination of whether accounts should be 'Active' or 'At Risk'
            subset = df[incorrectly_churned]
            
            # Calculate ratios where possible
            has_prev_revenue = ('revenue_prev' in df.columns) & (subset['revenue_prev'] > 0)
            has_prev_tickets = ('tickets_prev' in df.columns) & (subset['tickets_prev'] > 0)
            
            # Default to 'Active' for all incorrectly churned accounts
            ratings[incorrectly_churned] = 'Active'
            
            # Mark as 'At Risk' if activity has dropped >50%
            if 'revenue_prev' in df.columns:
                # Check which of the incorrectly churned accounts have previous revenue
                has_prev_revenue_mask = df.loc[incorrectly_churned, 'revenue_prev'] > 0
                
                if has_prev_revenue_mask.any():
                    # Calculate revenue ratios for those accounts
                    indices_with_prev = df[incorrectly_churned][has_prev_revenue_mask].index
                    revenue_ratios = (
                        df.loc[indices_with_prev, 'revenue_current'] / 
                        df.loc[indices_with_prev, 'revenue_prev']
                    )
                    
                    # Find which ones have dropped >50%
                    at_risk_indices = indices_with_prev[revenue_ratios < 0.5]
                    ratings[at_risk_indices] = 'At Risk'
    
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