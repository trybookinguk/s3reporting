"""
Revenue Factor Module - Monitors revenue drops and calculates risk scores.

This module implements revenue drop monitoring with:
- Industry quintile calculations for peer comparison
- Year-over-year (YoY) and rolling average comparisons
- Seasonality handling for education and annual events
- Fallback logic when industry data is insufficient
- Special handling for new accounts and zero revenue normalization
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any
import logging

logger = logging.getLogger(__name__)

def calculate_industry_quintiles(
    booking_df: pd.DataFrame, 
    accounts_df: Optional[pd.DataFrame] = None,
    time_period: str = 'current',
    min_accounts: int = 100
) -> Dict[str, Dict[str, float]]:
    """
    Calculate revenue quintiles for each industry/subindustry.
    
    Args:
        booking_df: DataFrame with booking/transaction data
        accounts_df: Optional DataFrame with account metadata
        time_period: 'current' for last 4 weeks, 'seasonal' for YoY comparison
        min_accounts: Minimum accounts required for valid quintiles (default 100)
    
    Returns:
        Dict mapping industry to quintile thresholds
        Format: {industry: {'Q1': value, 'Q2': value, ...}}
    """
    quintiles = {}
    
    logger.info(f"Calculating industry quintiles for time_period={time_period}")
    
    # Ensure TransactionDate is datetime
    if 'TransactionDate' in booking_df.columns:
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    else:
        logger.error("TransactionDate column not found in booking_df")
        return quintiles
    
    # Determine time period for filtering
    current_date = pd.Timestamp.now()
    if time_period == 'current':
        # Last 4 weeks
        start_date = current_date - timedelta(days=28)
        end_date = current_date
    elif time_period == 'seasonal':
        # Same 4-week period last year for YoY comparison
        start_date = current_date - timedelta(days=365+28)
        end_date = current_date - timedelta(days=365)
    else:
        # Default to current period
        start_date = current_date - timedelta(days=28)
        end_date = current_date
    
    logger.info(f"Time period: {start_date.date()} to {end_date.date()}")
    
    # Filter booking data by time period
    mask = (booking_df['TransactionDate'] >= start_date) & (booking_df['TransactionDate'] <= end_date)
    df_filtered = booking_df[mask].copy()
    
    logger.info(f"Filtered {len(df_filtered):,} transactions for time period")
    
    # Check if Industry/SubIndustry columns exist in booking_df first
    has_industry_in_booking = 'Industry' in df_filtered.columns and 'SubIndustry' in df_filtered.columns
    
    # If not in booking data, merge with accounts data to get industry info
    if not has_industry_in_booking and accounts_df is not None:
        if 'Industry' in accounts_df.columns:
            # Ensure AccountId is the same type in both dataframes
            if 'AccountId' in accounts_df.columns:
                accounts_df['AccountId'] = accounts_df['AccountId'].astype(str)
            df_filtered['AccountId'] = df_filtered['AccountId'].astype(str)
            
            merge_cols = ['AccountId', 'Industry']
            if 'SubIndustry' in accounts_df.columns:
                merge_cols.append('SubIndustry')
            
            df_filtered = df_filtered.merge(
                accounts_df[merge_cols], 
                on='AccountId', 
                how='left',
                suffixes=('', '_account')
            )
            logger.info(f"Merged with accounts data, {df_filtered['Industry'].notna().sum():,} accounts have industry info")
    
    # Include all accounts in industry calculations, including zero-revenue accounts
    all_accounts = set()
    if accounts_df is not None and 'AccountId' in accounts_df.columns:
        all_accounts = set(accounts_df['AccountId'].astype(str))
    
    # Process by grouping type
    for grouping_col in ['SubIndustry', 'Industry']:
        if grouping_col not in df_filtered.columns:
            logger.info(f"Skipping {grouping_col} - column not found")
            continue
        
        # Get unique industries
        industries = df_filtered[grouping_col].dropna().unique()
        logger.info(f"Processing {len(industries)} unique {grouping_col} values")
        
        for industry in industries:
            if pd.isna(industry) or industry == '':
                continue
            
            # Get accounts in this industry
            industry_mask = df_filtered[grouping_col] == industry
            industry_data = df_filtered[industry_mask]
            
            # Aggregate revenue by account for the time period
            account_revenue = industry_data.groupby('AccountId')['PaymentReceived'].sum()
            
            # Include zero-revenue accounts from this industry
            if accounts_df is not None and grouping_col in accounts_df.columns:
                # Get all accounts in this industry
                industry_accounts = accounts_df[
                    accounts_df[grouping_col] == industry
                ]['AccountId'].astype(str)
                
                # Add zero revenue for accounts with no transactions
                for acc_id in industry_accounts:
                    if acc_id not in account_revenue.index:
                        account_revenue[acc_id] = 0.0
            
            # Filter for mature accounts (6+ months old)
            mature_account_revenue = account_revenue.copy()
            if accounts_df is not None and 'DateTimeCreated' in accounts_df.columns:
                # Convert AccountId to string for matching
                accounts_df['AccountId'] = accounts_df['AccountId'].astype(str)
                account_ages = accounts_df[
                    accounts_df['AccountId'].isin(account_revenue.index)
                ].copy()
                
                if len(account_ages) > 0:
                    account_ages['DateTimeCreated'] = pd.to_datetime(account_ages['DateTimeCreated'])
                    account_ages['account_age_days'] = (current_date - account_ages['DateTimeCreated']).dt.days
                    
                    mature_accounts = account_ages[
                        account_ages['account_age_days'] >= 180
                    ]['AccountId']
                    
                    mature_account_revenue = account_revenue[
                        account_revenue.index.isin(mature_accounts)
                    ]
                    
                    logger.info(f"{industry}: {len(mature_account_revenue)} mature accounts out of {len(account_revenue)} total")
            
            # Only calculate quintiles if sufficient mature accounts
            if len(mature_account_revenue) >= min_accounts:
                # Sort revenues for percentile calculation
                sorted_revenues = np.sort(mature_account_revenue.values)
                
                # Calculate quintile thresholds (20th, 40th, 60th, 80th percentiles)
                q_values = np.percentile(sorted_revenues, [20, 40, 60, 80])
                
                # Calculate additional statistics
                zero_count = (mature_account_revenue == 0).sum()
                zero_pct = zero_count / len(mature_account_revenue)
                
                quintile_key = f"{grouping_col}:{industry}"
                if time_period == 'seasonal':
                    quintile_key = f"seasonal_{quintile_key}"
                
                quintiles[quintile_key] = {
                    'Q1': float(q_values[0]),  # Bottom 20%
                    'Q2': float(q_values[1]),  # 20-40%
                    'Q3': float(q_values[2]),  # 40-60%
                    'Q4': float(q_values[3]),  # 60-80%
                    # Q5 is anything above Q4
                    'account_count': len(mature_account_revenue),
                    'zero_revenue_pct': zero_pct,
                    'zero_revenue_count': zero_count,
                    'median_revenue': float(np.median(sorted_revenues)),
                    'mean_revenue': float(np.mean(sorted_revenues)),
                    'grouping_type': grouping_col,
                    'time_period': time_period,
                    'period_start': start_date.isoformat(),
                    'period_end': end_date.isoformat()
                }
                
                logger.info(f"Calculated quintiles for {quintile_key}: "
                          f"Q1={q_values[0]:.2f}, Q2={q_values[1]:.2f}, "
                          f"Q3={q_values[2]:.2f}, Q4={q_values[3]:.2f}, "
                          f"zero_pct={zero_pct:.1%}")
            else:
                logger.info(f"{industry}: Only {len(mature_account_revenue)} mature accounts, "
                          f"need {min_accounts} for quintiles")
    
    logger.info(f"Calculated quintiles for {len(quintiles)} industry/subindustry groups")
    return quintiles

def get_account_quintile(revenue: float, quintile_thresholds: Dict[str, float]) -> int:
    """
    Determine which quintile an account's revenue falls into.
    
    Returns:
        Quintile number (1-5), where 5 is highest revenue
    """
    if revenue <= quintile_thresholds['Q1']:
        return 1
    elif revenue <= quintile_thresholds['Q2']:
        return 2
    elif revenue <= quintile_thresholds['Q3']:
        return 3
    elif revenue <= quintile_thresholds['Q4']:
        return 4
    else:
        return 5

def calculate_revenue_drop_score(
    account_data: pd.DataFrame,
    industry_quintiles: Dict[str, Dict[str, float]],
    account_pattern: str = 'continuous',
    account_info: Optional[Dict[str, any]] = None
) -> Tuple[int, Dict[str, any]]:
    """
    Calculate revenue drop score for an account.
    
    Args:
        account_data: DataFrame with account's revenue history
        industry_quintiles: Industry quintile thresholds
        account_pattern: 'continuous', 'seasonal', or 'annual'
        account_info: Optional account metadata
    
    Returns:
        Tuple of (score, details_dict)
        Score: 0 (no impact) to 3 (severe drop)
    """
    details = {
        'method': None,
        'current_revenue': 0,
        'comparison_revenue': 0,
        'drop_percentage': 0,
        'quintile_drop': 0,
        'account_pattern': account_pattern
    }
    
    # Get account details
    if account_data.empty:
        logger.warning("Empty account data provided")
        return 0, details
    
    # Ensure TransactionDate is datetime
    account_data['TransactionDate'] = pd.to_datetime(account_data['TransactionDate'])
    
    account_id = account_data['AccountId'].iloc[0] if 'AccountId' in account_data.columns else 'unknown'
    
    # Try to get industry from columns
    industry = ''
    subindustry = ''
    if 'Industry' in account_data.columns and not account_data['Industry'].empty:
        industry = str(account_data['Industry'].iloc[0]) if pd.notna(account_data['Industry'].iloc[0]) else ''
    if 'SubIndustry' in account_data.columns and not account_data['SubIndustry'].empty:
        subindustry = str(account_data['SubIndustry'].iloc[0]) if pd.notna(account_data['SubIndustry'].iloc[0]) else ''
    
    logger.debug(f"Processing account {account_id}, industry='{industry}', subindustry='{subindustry}'")
    
    # Calculate current period revenue (last 4 weeks)
    current_date = pd.Timestamp.now()
    current_start = current_date - timedelta(days=28)
    current_revenue = account_data[
        account_data['TransactionDate'] >= current_start
    ]['PaymentReceived'].sum()
    
    details['current_revenue'] = float(current_revenue)
    
    # Calculate account age
    if 'DateTimeCreated' in account_data.columns:
        account_created = pd.to_datetime(account_data['DateTimeCreated']).min()
    else:
        account_created = account_data['TransactionDate'].min()
    
    months_active = (current_date - account_created).days // 30
    details['months_active'] = months_active
    details['account_created'] = account_created.isoformat()
    
    # Determine if education account
    is_education = False
    if industry:
        is_education = ('education' in industry.lower() or 'school' in industry.lower())
    if not is_education and subindustry:
        is_education = ('education' in subindustry.lower() or 'school' in subindustry.lower())
    
    # Get appropriate comparison period
    comparison_start, comparison_end, comparison_type = get_seasonal_comparison_period(
        current_date, account_pattern, months_active, is_education
    )
    details['comparison_type'] = comparison_type
    details['comparison_start'] = comparison_start.isoformat()
    details['comparison_end'] = comparison_end.isoformat()
    
    # Check if we have industry quintiles
    quintile_key = None
    
    # For seasonal comparison, check seasonal quintiles first
    if comparison_type == 'year_over_year' and account_pattern in ['seasonal', 'annual']:
        if subindustry and f"seasonal_SubIndustry:{subindustry}" in industry_quintiles:
            quintile_key = f"seasonal_SubIndustry:{subindustry}"
        elif industry and f"seasonal_Industry:{industry}" in industry_quintiles:
            quintile_key = f"seasonal_Industry:{industry}"
    
    # Fall back to current period quintiles
    if not quintile_key:
        if subindustry and f"SubIndustry:{subindustry}" in industry_quintiles:
            quintile_key = f"SubIndustry:{subindustry}"
        elif industry and f"Industry:{industry}" in industry_quintiles:
            quintile_key = f"Industry:{industry}"
    
    if quintile_key:
        details['method'] = 'industry_quintiles'
        
        # Calculate comparison revenue based on period type
        if comparison_type == 'year_over_year':
            comparison_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] <= comparison_end)
            ]['PaymentReceived'].sum()
        elif comparison_type == 'rolling_average':
            # 12-week period for rolling average
            comparison_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] < comparison_end)
            ]['PaymentReceived'].sum() / 3  # Convert to 4-week equivalent
        else:  # account_lifetime
            # Average over account lifetime
            lifetime_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] < comparison_end)
            ]['PaymentReceived'].sum()
            total_weeks = max(1, (comparison_end - comparison_start).days // 7)
            comparison_revenue = lifetime_revenue * 4 / total_weeks  # 4-week equivalent
        
        details['comparison_revenue'] = float(comparison_revenue)
        
        # Calculate quintiles
        current_quintile = get_account_quintile(current_revenue, industry_quintiles[quintile_key])
        comparison_quintile = get_account_quintile(comparison_revenue, industry_quintiles[quintile_key])
        
        quintile_drop = comparison_quintile - current_quintile
        details['quintile_drop'] = quintile_drop
        details['current_quintile'] = current_quintile
        details['comparison_quintile'] = comparison_quintile
        
        # Apply scoring based on quintile drops
        if quintile_drop >= 4 or (comparison_quintile >= 3 and current_quintile == 1):
            score = 3  # Severe
        elif quintile_drop == 3:
            score = 2  # Significant
        elif quintile_drop == 2:
            score = 1  # Moderate
        else:
            score = 0  # No impact
        
        # Special case: if >30% of industry has zero revenue, no penalty for zero
        if industry_quintiles[quintile_key]['zero_revenue_pct'] > 0.3 and current_revenue == 0:
            score = 0
            details['zero_revenue_common'] = True
        
        # Add quintile info to details
        details['quintile_key'] = quintile_key
        details['industry_stats'] = {
            'zero_revenue_pct': industry_quintiles[quintile_key]['zero_revenue_pct'],
            'account_count': industry_quintiles[quintile_key]['account_count'],
            'median_revenue': industry_quintiles[quintile_key].get('median_revenue', 0),
            'mean_revenue': industry_quintiles[quintile_key].get('mean_revenue', 0)
        }
        
        logger.debug(f"Account {account_id}: Quintile method - current_q={current_quintile}, "
                    f"comparison_q={comparison_quintile}, drop={quintile_drop}, score={score}")
    
    else:
        # Fallback to activity-based thresholds
        details['method'] = 'activity_based'
        details['fallback_reason'] = f"No quintiles for industry='{industry}', subindustry='{subindustry}'"
        
        # Calculate comparison revenue
        if comparison_type == 'year_over_year':
            comparison_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] <= comparison_end)
            ]['PaymentReceived'].sum()
        elif comparison_type == 'rolling_average':
            # 12-week period for rolling average
            comparison_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] < comparison_end)
            ]['PaymentReceived'].sum() / 3  # Convert to 4-week equivalent
        else:  # account_lifetime
            # Average over account lifetime
            lifetime_revenue = account_data[
                (account_data['TransactionDate'] >= comparison_start) &
                (account_data['TransactionDate'] < comparison_end)
            ]['PaymentReceived'].sum()
            total_weeks = max(1, (comparison_end - comparison_start).days // 7)
            comparison_revenue = lifetime_revenue * 4 / total_weeks  # 4-week equivalent
        
        details['comparison_revenue'] = float(comparison_revenue)
        
        # Calculate drop percentage
        if comparison_revenue > 0:
            revenue_ratio = current_revenue / comparison_revenue
            details['revenue_ratio'] = revenue_ratio
            details['drop_percentage'] = (1 - revenue_ratio) * 100
            
            # Apply scoring thresholds
            if account_pattern in ['seasonal', 'annual']:
                # YoY thresholds for seasonal accounts
                if revenue_ratio < 0.3 or (current_revenue == 0 and comparison_revenue > 0):
                    score = 3  # Severe
                elif revenue_ratio < 0.6:
                    score = 2  # Significant
                elif revenue_ratio < 0.8:
                    score = 1  # Moderate
                else:
                    score = 0  # No impact
            else:
                # Rolling average thresholds for continuous accounts
                if revenue_ratio < 0.25 or (current_revenue == 0 and comparison_revenue > 0):
                    score = 3  # Severe
                elif revenue_ratio < 0.5:
                    score = 2  # Significant
                elif revenue_ratio < 0.75:
                    score = 1  # Moderate
                else:
                    score = 0  # No impact
        else:
            # No historical revenue to compare
            details['revenue_ratio'] = 0
            details['drop_percentage'] = 0
            
            if current_revenue == 0 and months_active >= 2:
                # Account old enough to have revenue but doesn't
                score = 1  # Flag zero revenue
                details['zero_revenue_flag'] = True
            else:
                score = 0
        
        logger.debug(f"Account {account_id}: Activity method - ratio={details.get('revenue_ratio', 0):.2f}, "
                    f"score={score}")
    
    return score, details

def get_seasonal_comparison_period(
    current_date: pd.Timestamp,
    account_type: str,
    months_active: int,
    is_education: bool = False,
    is_scottish: bool = False
) -> Tuple[pd.Timestamp, pd.Timestamp, str]:
    """
    Determine the appropriate comparison period based on account type and seasonality.
    
    Args:
        current_date: Current date for comparison
        account_type: 'continuous', 'seasonal', or 'annual'
        months_active: Number of months account has been active
        is_education: Whether account is in education industry
        is_scottish: Whether account is Scottish (for education term dates)
    
    Returns:
        Tuple of (start_date, end_date, comparison_type)
    """
    if months_active >= 12:
        # Year-over-year comparison
        if account_type in ['seasonal', 'annual'] or is_education:
            # Same period last year
            start_date = current_date - timedelta(days=365) - timedelta(days=28)
            end_date = current_date - timedelta(days=365)
            comparison_type = 'year_over_year'
        else:
            # 12-week rolling average for continuous accounts
            start_date = current_date - timedelta(days=112)  # 16 weeks ago
            end_date = current_date - timedelta(days=28)     # 4 weeks ago
            comparison_type = 'rolling_average'
    else:
        # For newer accounts, use average since creation
        start_date = current_date - timedelta(days=months_active * 30)
        end_date = current_date - timedelta(days=28)
        comparison_type = 'account_lifetime'
    
    return start_date, end_date, comparison_type

def handle_education_seasonality(
    revenue_data: pd.DataFrame,
    is_scottish: bool = False
) -> pd.DataFrame:
    """
    Handle special seasonality for education accounts.
    
    Args:
        revenue_data: DataFrame with transaction data
        is_scottish: Whether to use Scottish school calendar
    
    Returns:
        Filtered DataFrame excluding holiday periods
    """
    if revenue_data.empty:
        return revenue_data
    
    # Create a copy to avoid modifying original
    filtered_data = revenue_data.copy()
    
    if is_scottish:
        # Scottish schools: different holiday pattern
        # Summer break: late June to mid-August
        summer_mask = (
            (filtered_data['TransactionDate'].dt.month == 6) & 
            (filtered_data['TransactionDate'].dt.day >= 25)
        ) | (
            filtered_data['TransactionDate'].dt.month == 7
        ) | (
            (filtered_data['TransactionDate'].dt.month == 8) & 
            (filtered_data['TransactionDate'].dt.day <= 15)
        )
    else:
        # English/Welsh schools: July and August
        summer_mask = filtered_data['TransactionDate'].dt.month.isin([7, 8])
    
    # Exclude summer months
    filtered_data = filtered_data[~summer_mask]
    
    # Also exclude major holiday periods (simplified)
    # Christmas break: last 2 weeks of December
    christmas_mask = (
        (filtered_data['TransactionDate'].dt.month == 12) & 
        (filtered_data['TransactionDate'].dt.day >= 15)
    )
    
    filtered_data = filtered_data[~christmas_mask]
    
    return filtered_data

def handle_seasonal_comparison(
    account_data: pd.DataFrame,
    comparison_period: str = 'year_over_year'
) -> pd.DataFrame:
    """
    Handle seasonal comparison logic for specific account types.
    
    Args:
        account_data: Account's transaction history
        comparison_period: Type of comparison ('year_over_year', 'term_to_term', etc.)
    
    Returns:
        Filtered DataFrame for appropriate comparison period
    """
    industry = account_data.get('Industry', '').iloc[0] if not account_data.empty else ''
    subindustry = account_data.get('SubIndustry', '').iloc[0] if 'SubIndustry' in account_data.columns and not account_data.empty else ''
    
    # Check if education account
    is_education = ('education' in industry.lower() or 'school' in industry.lower() or
                   'education' in subindustry.lower() or 'school' in subindustry.lower())
    
    if is_education:
        # Check if Scottish based on postcode or other indicators
        postcode = account_data.get('AccountPostcode', '').iloc[0] if 'AccountPostcode' in account_data.columns and not account_data.empty else ''
        is_scottish = postcode.startswith(('AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'))
        
        account_data = handle_education_seasonality(account_data, is_scottish)
        
    elif 'festival' in industry.lower() or 'annual' in subindustry.lower():
        # For annual events, we need more sophisticated detection
        # This would ideally look at historical patterns
        pass
    
    return account_data

def get_account_revenue_history(
    account_id: str,
    booking_df: pd.DataFrame,
    start_date: Optional[pd.Timestamp] = None,
    end_date: Optional[pd.Timestamp] = None
) -> pd.DataFrame:
    """
    Get revenue history for a specific account.
    
    Args:
        account_id: Account identifier
        booking_df: DataFrame with all booking data
        start_date: Optional start date filter
        end_date: Optional end date filter
    
    Returns:
        DataFrame with account's transaction history
    \"\"\"
    # Convert account_id to string for consistent comparison
    account_id_str = str(account_id)
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    
    # Filter for account
    account_data = booking_df[booking_df['AccountId'] == account_id_str].copy()
    
    if account_data.empty:
        logger.warning(f\"No data found for account {account_id}\")
        return account_data
    
    # Ensure TransactionDate is datetime
    account_data['TransactionDate'] = pd.to_datetime(account_data['TransactionDate'])
    
    # Apply date filters if provided
    if start_date:
        account_data = account_data[account_data['TransactionDate'] >= start_date]
    if end_date:
        account_data = account_data[account_data['TransactionDate'] <= end_date]
    
    return account_data.sort_values('TransactionDate')


def calculate_yoy_comparison(
    account_data: pd.DataFrame,
    reference_date: Optional[pd.Timestamp] = None,
    period_days: int = 28
) -> Dict[str, float]:
    \"\"\"
    Calculate year-over-year revenue comparison.
    
    Args:
        account_data: Account's transaction history
        reference_date: Date to calculate from (default: now)
        period_days: Number of days for comparison period
    
    Returns:
        Dict with current and previous year revenues
    \"\"\"
    if reference_date is None:
        reference_date = pd.Timestamp.now()
    
    # Current period
    current_start = reference_date - timedelta(days=period_days)
    current_revenue = account_data[
        (account_data['TransactionDate'] >= current_start) &
        (account_data['TransactionDate'] <= reference_date)
    ]['PaymentReceived'].sum()
    
    # Same period last year
    previous_start = reference_date - timedelta(days=365 + period_days)
    previous_end = reference_date - timedelta(days=365)
    previous_revenue = account_data[
        (account_data['TransactionDate'] >= previous_start) &
        (account_data['TransactionDate'] <= previous_end)
    ]['PaymentReceived'].sum()
    
    return {
        'current_revenue': float(current_revenue),
        'previous_revenue': float(previous_revenue),
        'yoy_change': float(current_revenue - previous_revenue),
        'yoy_change_pct': float((current_revenue / previous_revenue - 1) * 100) if previous_revenue > 0 else 0
    }


def determine_account_age(
    account_data: pd.DataFrame,
    accounts_df: Optional[pd.DataFrame] = None,
    account_id: Optional[str] = None
) -> Dict[str, Any]:
    \"\"\"
    Determine account age and lifecycle stage.
    
    Args:
        account_data: Account's transaction history
        accounts_df: Optional accounts master data
        account_id: Optional account ID for lookup
    
    Returns:
        Dict with age information and lifecycle stage
    \"\"\"
    current_date = pd.Timestamp.now()
    
    # Try to get creation date from multiple sources
    creation_date = None
    
    # First, check account data itself
    if 'DateTimeCreated' in account_data.columns and not account_data['DateTimeCreated'].empty:
        creation_date = pd.to_datetime(account_data['DateTimeCreated']).min()
    
    # Then check accounts master data
    elif accounts_df is not None and account_id:
        account_id_str = str(account_id)
        if 'AccountId' in accounts_df.columns:
            accounts_df['AccountId'] = accounts_df['AccountId'].astype(str)
            account_info = accounts_df[accounts_df['AccountId'] == account_id_str]
            if not account_info.empty and 'DateTimeCreated' in account_info.columns:
                creation_date = pd.to_datetime(account_info['DateTimeCreated'].iloc[0])
    
    # Fall back to first transaction date
    if creation_date is None or pd.isna(creation_date):
        if not account_data.empty and 'TransactionDate' in account_data.columns:
            creation_date = pd.to_datetime(account_data['TransactionDate']).min()
        else:
            creation_date = current_date  # Default to now if no data
    
    age_days = (current_date - creation_date).days
    age_weeks = age_days // 7
    age_months = age_days // 30
    
    # Determine lifecycle stage
    if age_weeks <= 4:
        stage = 'new_building'
    elif age_weeks <= 8:
        stage = 'new_expected'
    elif age_months <= 6:
        stage = 'establishing'
    elif age_months <= 12:
        stage = 'maturing'
    else:
        stage = 'established'
    
    return {
        'creation_date': creation_date.isoformat(),
        'age_days': age_days,
        'age_weeks': age_weeks,
        'age_months': age_months,
        'lifecycle_stage': stage
    }


def get_revenue_factor(
    current_revenue: float,
    historical_revenue: pd.DataFrame,
    industry_data: Optional[pd.DataFrame] = None,
    account_type: str = 'continuous',
    account_info: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    \"\"\"
    Main function to calculate revenue factor and risk score.
    
    Args:
        current_revenue: Current period revenue
        historical_revenue: DataFrame with historical revenue data
        industry_data: Optional DataFrame with industry peer data
        account_type: Account pattern type ('continuous', 'seasonal', 'annual')
        account_info: Optional account metadata
    
    Returns:
        Dict with revenue factor score and supporting details
    \"\"\"
    result = {
        'score': 0,
        'factor': 'revenue_drop',
        'severity': 'none',
        'details': {}
    }
    
    # Determine account age
    account_id = None
    if not historical_revenue.empty and 'AccountId' in historical_revenue.columns:
        account_id = str(historical_revenue['AccountId'].iloc[0])
    
    age_info = determine_account_age(historical_revenue, None, account_id)
    result['details']['account_age'] = age_info
    
    # Handle new accounts specially
    if age_info['lifecycle_stage'] == 'new_building':
        result['details']['reason'] = 'Account in building phase'
        return result
    elif age_info['lifecycle_stage'] == 'new_expected':
        if current_revenue == 0:
            result['score'] = 1
            result['severity'] = 'moderate'
            result['details']['reason'] = 'Zero revenue in expected phase'
        return result
    
    # Calculate industry quintiles if industry data provided
    industry_quintiles = {}
    if industry_data is not None and not industry_data.empty:
        logger.info(\"Calculating industry quintiles for revenue analysis\")
        
        # Get accounts master data if available
        accounts_df = None
        if account_info and 'accounts_df' in account_info:
            accounts_df = account_info['accounts_df']
        
        # Calculate quintiles for current period
        current_quintiles = calculate_industry_quintiles(
            booking_df=industry_data,
            accounts_df=accounts_df,
            time_period='current',
            min_accounts=100  # Lower threshold for better coverage
        )
        industry_quintiles.update(current_quintiles)
        
        # For seasonal accounts, also calculate seasonal quintiles
        if account_type in ['seasonal', 'annual']:
            seasonal_quintiles = calculate_industry_quintiles(
                booking_df=industry_data,
                accounts_df=accounts_df,
                time_period='seasonal',
                min_accounts=100
            )
            # Merge seasonal quintiles with seasonal prefix
            industry_quintiles.update(seasonal_quintiles)
    
    # Apply seasonal handling if needed
    if account_type in ['seasonal', 'annual'] or \
       ('Industry' in historical_revenue.columns and 
        'education' in str(historical_revenue['Industry'].iloc[0]).lower()):
        processed_data = handle_seasonal_comparison(historical_revenue, account_type)
    else:
        processed_data = historical_revenue
    
    # Calculate revenue drop score
    if not processed_data.empty:
        # Calculate with full context
        score, details = calculate_revenue_drop_score(
            processed_data,
            industry_quintiles,
            account_type,
            account_info
        )
        
        result['score'] = score
        result['details'].update(details)
        
        # Map score to severity
        severity_map = {0: 'none', 1: 'moderate', 2: 'significant', 3: 'severe'}
        result['severity'] = severity_map.get(score, 'none')
        
        # Add YoY comparison for context
        yoy_stats = calculate_yoy_comparison(processed_data)
        result['details']['yoy_comparison'] = yoy_stats
    
    return result

def calculate_new_account_thresholds(
    account_data: pd.DataFrame,
    tier_cohort_data: Optional[pd.DataFrame] = None
) -> Dict[str, any]:
    """
    Special handling for new accounts based on their lifecycle stage.
    
    Week 1-4: Building phase (no scoring)
    Week 5-8: Expected revenue (flag if zero)
    Week 9+: Compare to account tier cohort average
    
    Args:
        account_data: Account's transaction history
        tier_cohort_data: Optional data for accounts in same tier
    
    Returns:
        Dict with thresholds and recommendations
    """
    thresholds = {
        'stage': 'unknown',
        'expected_activity': False,
        'risk_level': 0,
        'weeks_active': 0,
        'recommendation': None
    }
    
    if account_data.empty:
        thresholds['stage'] = 'no_data'
        return thresholds
    
    # Get account creation date
    if 'DateTimeCreated' in account_data.columns:
        account_created = pd.to_datetime(account_data['DateTimeCreated']).min()
    else:
        # Fall back to first transaction date
        account_created = pd.to_datetime(account_data['TransactionDate']).min()
    
    account_age_days = (pd.Timestamp.now() - account_created).days
    weeks_active = account_age_days // 7
    
    thresholds['weeks_active'] = weeks_active
    thresholds['account_age_days'] = account_age_days
    thresholds['account_created'] = account_created.isoformat()
    
    # Calculate revenue in last 4 weeks
    recent_start = pd.Timestamp.now() - timedelta(days=28)
    recent_revenue = account_data[
        pd.to_datetime(account_data['TransactionDate']) >= recent_start
    ]['PaymentReceived'].sum()
    
    thresholds['recent_revenue'] = float(recent_revenue)
    
    if weeks_active <= 4:  # Week 1-4: Building phase
        thresholds['stage'] = 'building'
        thresholds['expected_activity'] = False
        thresholds['recommendation'] = 'Monitor only - account in building phase'
        
    elif weeks_active <= 8:  # Week 5-8: Expected revenue
        thresholds['stage'] = 'expected_revenue'
        thresholds['expected_activity'] = True
        
        if recent_revenue == 0:
            thresholds['risk_level'] = 1
            thresholds['recommendation'] = 'Flag for follow-up - no revenue in expected phase'
        else:
            thresholds['recommendation'] = 'On track - revenue activity detected'
            
    else:  # Week 9+: Established
        thresholds['stage'] = 'established'
        thresholds['expected_activity'] = True
        
        # Compare to tier cohort if available
        if tier_cohort_data is not None and not tier_cohort_data.empty:
            # Ensure TransactionDate is datetime
            tier_cohort_data['TransactionDate'] = pd.to_datetime(tier_cohort_data['TransactionDate'])
            
            # Calculate cohort's average 4-week revenue
            cohort_recent = tier_cohort_data[
                tier_cohort_data['TransactionDate'] >= recent_start
            ].groupby('AccountId')['PaymentReceived'].sum()
            
            if len(cohort_recent) > 0:
                cohort_avg = cohort_recent.mean()
                cohort_median = cohort_recent.median()
                
                thresholds['cohort_avg'] = float(cohort_avg)
                thresholds['cohort_median'] = float(cohort_median)
                thresholds['cohort_size'] = len(cohort_recent)
                
                # Use median for comparison to avoid outlier influence
                if cohort_median > 0:
                    ratio_to_median = recent_revenue / cohort_median
                    thresholds['ratio_to_cohort_median'] = ratio_to_median
                    
                    if ratio_to_median < 0.25:
                        thresholds['risk_level'] = 3
                        thresholds['recommendation'] = 'High risk - significantly below cohort'
                    elif ratio_to_median < 0.5:
                        thresholds['risk_level'] = 2
                        thresholds['recommendation'] = 'Medium risk - below cohort average'
                    elif ratio_to_median < 0.75:
                        thresholds['risk_level'] = 1
                        thresholds['recommendation'] = 'Low risk - slightly below cohort'
                    else:
                        thresholds['recommendation'] = 'Performing well vs cohort'
                else:
                    # Cohort has zero median revenue
                    if recent_revenue > 0:
                        thresholds['recommendation'] = 'Outperforming cohort (cohort median is zero)'
                    else:
                        thresholds['recommendation'] = 'In line with cohort (both zero revenue)'
            else:
                thresholds['recommendation'] = 'No cohort revenue data for comparison'
        else:
            thresholds['recommendation'] = 'No cohort data available for comparison'
    
    return thresholds