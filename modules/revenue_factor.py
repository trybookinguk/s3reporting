"""
Revenue Factor Module - Strategic Revenue Analysis and Risk Assessment.

This module implements strategic revenue analysis focused on:
- Industry quintile calculations for peer benchmarking
- Year-over-year (YoY) comparisons for long-term trends
- Seasonal pattern analysis for education and annual events
- Lifecycle-aware assessment for different account stages
- Peer group comparisons for relative performance evaluation

Key Features:
- Compares accounts against industry peers using quintile analysis
- Handles seasonal patterns appropriately (YoY for seasonal, rolling avg for continuous)
- Considers account lifecycle stage (new vs established accounts)
- Provides strategic risk scoring based on sustained underperformance

This module is designed for strategic risk assessment and long-term trend analysis,
NOT for operational alerts or rapid drop detection. For rapid revenue drop detection,
use the rapid_drop_detector module instead.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any
from dataclasses import dataclass, field
import logging
import time
from .utils.config import (
    MIN_ACCOUNTS_FOR_QUINTILES, MATURE_ACCOUNT_AGE_DAYS,
    ACCOUNT_LIFECYCLE_STAGES
)

logger = logging.getLogger(__name__)


# ============= Data Classes =============
@dataclass
class RevenueMetrics:
    """Container for strategic revenue metrics"""
    current: float = 0.0
    comparison: float = 0.0
    ratio: float = 0.0
    change_percentage: float = 0.0
    
    def calculate_ratio(self):
        """Calculate ratio and change percentage for trend analysis"""
        if self.comparison > 0:
            self.ratio = self.current / self.comparison
            self.change_percentage = (self.ratio - 1) * 100  # Positive for growth, negative for decline
        else:
            self.ratio = 0.0
            self.change_percentage = 0.0 if self.current == 0 else 100.0


@dataclass
class QuintileInfo:
    """Container for quintile data"""
    thresholds: Dict[str, float] = field(default_factory=dict)
    account_count: int = 0
    zero_revenue_pct: float = 0.0
    median_revenue: float = 0.0
    mean_revenue: float = 0.0
    grouping_type: str = ""
    time_period: str = ""
    period_start: str = ""
    period_end: str = ""
    

@dataclass 
class AccountInfo:
    """Container for account metadata"""
    id: str
    industry: str = ""
    subindustry: str = ""
    pattern: str = "continuous"
    age_days: int = 0
    lifecycle_stage: str = "unknown"
    is_education: bool = False
    

# ============= Utility Functions =============
def ensure_datetime(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Ensure a column is datetime type"""
    if column in df.columns:
        df[column] = pd.to_datetime(df[column])
    return df


def filter_by_date_range(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, 
                        date_col: str = 'TransactionDate') -> pd.DataFrame:
    """Filter DataFrame by date range"""
    df = ensure_datetime(df, date_col)
    return df[(df[date_col] >= start) & (df[date_col] <= end)]


def calculate_revenue_sum(df: pd.DataFrame, revenue_col: str = 'PaymentReceived') -> float:
    """Calculate total revenue from DataFrame"""
    return float(df[revenue_col].sum()) if not df.empty else 0.0


def get_account_info_from_data(account_data: pd.DataFrame, account_id: str) -> AccountInfo:
    """Extract account information from data"""
    info = AccountInfo(id=str(account_id))
    
    if account_data.empty:
        return info
        
    first_row = account_data.iloc[0]
    
    # Extract industry info
    if 'Industry' in account_data.columns:
        info.industry = str(first_row.get('Industry', '')) if pd.notna(first_row.get('Industry')) else ''
    if 'SubIndustry' in account_data.columns:
        info.subindustry = str(first_row.get('SubIndustry', '')) if pd.notna(first_row.get('SubIndustry')) else ''
    
    # Determine if education
    info.is_education = any(
        'education' in s.lower() or 'school' in s.lower() 
        for s in [info.industry, info.subindustry] if s
    )
    
    # Calculate age
    if 'DateTimeCreated' in account_data.columns:
        created = pd.to_datetime(first_row['DateTimeCreated'])
        info.age_days = (pd.Timestamp.now() - created).days
    else:
        first_transaction = pd.to_datetime(account_data['TransactionDate']).min()
        info.age_days = (pd.Timestamp.now() - first_transaction).days
    
    # Determine lifecycle stage
    age_weeks = info.age_days // 7
    if age_weeks <= ACCOUNT_LIFECYCLE_STAGES['new_building']:
        info.lifecycle_stage = 'new_building'
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES['new_expected']:
        info.lifecycle_stage = 'new_expected'
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES['establishing']:
        info.lifecycle_stage = 'establishing'
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES['maturing']:
        info.lifecycle_stage = 'maturing'
    else:
        info.lifecycle_stage = 'established'
        
    return info


# ============= Legacy Interface Functions =============
def get_account_quintile(revenue: float, quintile_thresholds: Dict[str, float]) -> int:
    """
    Determine which quintile an account's revenue falls into.
    Maintained for backward compatibility.
    """
    if revenue <= quintile_thresholds.get('Q1', 0):
        return 1
    elif revenue <= quintile_thresholds.get('Q2', 0):
        return 2
    elif revenue <= quintile_thresholds.get('Q3', 0):
        return 3
    elif revenue <= quintile_thresholds.get('Q4', 0):
        return 4
    else:
        return 5


def get_seasonal_comparison_period(
    current_date: pd.Timestamp,
    account_type: str,
    months_active: int,
    is_education: bool = False,
    is_scottish: bool = False
) -> Tuple[pd.Timestamp, pd.Timestamp, str]:
    """
    Determine the appropriate comparison period based on account type and seasonality.
    Maintained for backward compatibility.
    """
    if months_active >= 12:
        if account_type in ['seasonal', 'annual'] or is_education:
            start_date = current_date - timedelta(days=365) - timedelta(days=28)
            end_date = current_date - timedelta(days=365)
            comparison_type = 'year_over_year'
        else:
            start_date = current_date - timedelta(days=112)
            end_date = current_date - timedelta(days=28)
            comparison_type = 'rolling_average'
    else:
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
    Maintained for backward compatibility.
    """
    if revenue_data.empty:
        return revenue_data
    
    filtered_data = revenue_data.copy()
    
    if is_scottish:
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
        summer_mask = filtered_data['TransactionDate'].dt.month.isin([7, 8])
    
    filtered_data = filtered_data[~summer_mask]
    
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
    """Handle seasonal comparison logic. Maintained for backward compatibility."""
    # Simply return the data as-is - seasonal handling is done in scoring logic
    return account_data


def get_account_revenue_history(
    account_id: str,
    booking_df: pd.DataFrame,
    start_date: Optional[pd.Timestamp] = None,
    end_date: Optional[pd.Timestamp] = None
) -> pd.DataFrame:
    """Get revenue history for a specific account."""
    account_id_str = str(account_id)
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    
    account_data = booking_df[booking_df['AccountId'] == account_id_str].copy()
    
    if account_data.empty:
        logger.warning(f"No data found for account {account_id}")
        return account_data
    
    account_data['TransactionDate'] = pd.to_datetime(account_data['TransactionDate'])
    
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
    """Calculate year-over-year revenue comparison."""
    if reference_date is None:
        reference_date = pd.Timestamp.now()
    
    current_start = reference_date - timedelta(days=period_days)
    current_revenue = account_data[
        (account_data['TransactionDate'] >= current_start) &
        (account_data['TransactionDate'] <= reference_date)
    ]['PaymentReceived'].sum()
    
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
    """Determine account age and lifecycle stage."""
    current_date = pd.Timestamp.now()
    
    creation_date = None
    
    if 'DateTimeCreated' in account_data.columns and not account_data['DateTimeCreated'].empty:
        creation_date = pd.to_datetime(account_data['DateTimeCreated']).min()
    
    elif accounts_df is not None and account_id:
        account_id_str = str(account_id)
        if 'AccountId' in accounts_df.columns:
            accounts_df['AccountId'] = accounts_df['AccountId'].astype(str)
            account_info = accounts_df[accounts_df['AccountId'] == account_id_str]
            if not account_info.empty and 'DateTimeCreated' in account_info.columns:
                creation_date = pd.to_datetime(account_info['DateTimeCreated'].iloc[0])
    
    if creation_date is None or pd.isna(creation_date):
        if not account_data.empty and 'TransactionDate' in account_data.columns:
            creation_date = pd.to_datetime(account_data['TransactionDate']).min()
        else:
            creation_date = current_date
    
    age_days = (current_date - creation_date).days
    age_weeks = age_days // 7
    age_months = age_days // 30
    
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


def calculate_new_account_thresholds(
    account_data: pd.DataFrame,
    tier_cohort_data: Optional[pd.DataFrame] = None
) -> Dict[str, Any]:
    """Special handling for new accounts based on their lifecycle stage."""
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
    
    if 'DateTimeCreated' in account_data.columns:
        account_created = pd.to_datetime(account_data['DateTimeCreated']).min()
    else:
        account_created = pd.to_datetime(account_data['TransactionDate']).min()
    
    account_age_days = (pd.Timestamp.now() - account_created).days
    weeks_active = account_age_days // 7
    
    thresholds['weeks_active'] = weeks_active
    thresholds['account_age_days'] = account_age_days
    thresholds['account_created'] = account_created.isoformat()
    
    recent_start = pd.Timestamp.now() - timedelta(days=28)
    recent_revenue = account_data[
        pd.to_datetime(account_data['TransactionDate']) >= recent_start
    ]['PaymentReceived'].sum()
    
    thresholds['recent_revenue'] = float(recent_revenue)
    
    if weeks_active <= 4:
        thresholds['stage'] = 'building'
        thresholds['expected_activity'] = False
        thresholds['recommendation'] = 'Monitor only - account in building phase'
        
    elif weeks_active <= 8:
        thresholds['stage'] = 'expected_revenue'
        thresholds['expected_activity'] = True
        
        if recent_revenue == 0:
            thresholds['risk_level'] = 1
            thresholds['recommendation'] = 'Flag for follow-up - no revenue in expected phase'
        else:
            thresholds['recommendation'] = 'On track - revenue activity detected'
            
    else:
        thresholds['stage'] = 'established'
        thresholds['expected_activity'] = True
        
        if tier_cohort_data is not None and not tier_cohort_data.empty:
            tier_cohort_data['TransactionDate'] = pd.to_datetime(tier_cohort_data['TransactionDate'])
            
            cohort_recent = tier_cohort_data[
                tier_cohort_data['TransactionDate'] >= recent_start
            ].groupby('AccountId')['PaymentReceived'].sum()
            
            if len(cohort_recent) > 0:
                cohort_avg = cohort_recent.mean()
                cohort_median = cohort_recent.median()
                
                thresholds['cohort_avg'] = float(cohort_avg)
                thresholds['cohort_median'] = float(cohort_median)
                thresholds['cohort_size'] = len(cohort_recent)
                
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
                    if recent_revenue > 0:
                        thresholds['recommendation'] = 'Outperforming cohort (cohort median is zero)'
                    else:
                        thresholds['recommendation'] = 'In line with cohort (both zero revenue)'
            else:
                thresholds['recommendation'] = 'No cohort revenue data for comparison'
        else:
            thresholds['recommendation'] = 'No cohort data available for comparison'
    
    return thresholds


# ============= Quintile Calculation =============
def calculate_quintiles_for_group(revenues: np.ndarray, grouping_col: str, 
                                 industry: str, time_period: str,
                                 start_date: pd.Timestamp, end_date: pd.Timestamp) -> Dict[str, Any]:
    """Calculate quintile thresholds for a revenue array"""
    if len(revenues) < MIN_ACCOUNTS_FOR_QUINTILES:
        return None
        
    q_values = np.percentile(revenues, [20, 40, 60, 80])
    
    zero_count = (revenues == 0).sum()
    zero_pct = zero_count / len(revenues)
    
    quintile_key = f"{grouping_col}:{industry}"
    if time_period == 'seasonal':
        quintile_key = f"seasonal_{quintile_key}"
    
    return {
        'Q1': float(q_values[0]),
        'Q2': float(q_values[1]),
        'Q3': float(q_values[2]),
        'Q4': float(q_values[3]),
        'account_count': len(revenues),
        'zero_revenue_pct': zero_pct,
        'zero_revenue_count': zero_count,
        'median_revenue': float(np.median(revenues)),
        'mean_revenue': float(np.mean(revenues)),
        'grouping_type': grouping_col,
        'time_period': time_period,
        'period_start': start_date.isoformat(),
        'period_end': end_date.isoformat()
    }


def calculate_industry_quintiles(
    booking_df: pd.DataFrame, 
    accounts_df: Optional[pd.DataFrame] = None,
    time_period: str = 'current',
    min_accounts: int = 100
) -> Dict[str, Dict[str, float]]:
    """Calculate revenue quintiles for each industry/subindustry."""
    quintiles = {}
    
    logger.info(f"Calculating industry quintiles for time_period={time_period}")
    
    if 'TransactionDate' in booking_df.columns:
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    else:
        logger.error("TransactionDate column not found in booking_df")
        return quintiles
    
    # Determine time period for filtering
    current_date = pd.Timestamp.now()
    if time_period == 'current':
        start_date = current_date - timedelta(days=28)
        end_date = current_date
    elif time_period == 'seasonal':
        start_date = current_date - timedelta(days=365+28)
        end_date = current_date - timedelta(days=365)
    else:
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
    
    # Process by grouping type
    for grouping_col in ['SubIndustry', 'Industry']:
        if grouping_col not in df_filtered.columns:
            logger.info(f"Skipping {grouping_col} - column not found")
            continue
        
        # Get unique industries
        industries = df_filtered[grouping_col].dropna().unique()
        logger.info(f"Processing {len(industries)} unique {grouping_col} values")
        
        start_time = time.time()
        processed_count = 0
        
        for idx, industry in enumerate(industries):
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
                result = calculate_quintiles_for_group(
                    mature_account_revenue.values,
                    grouping_col,
                    industry,
                    time_period,
                    start_date,
                    end_date
                )
                if result:
                    quintile_key = f"{grouping_col}:{industry}"
                    if time_period == 'seasonal':
                        quintile_key = f"seasonal_{quintile_key}"
                    quintiles[quintile_key] = result
                    processed_count += 1
                    
                    # Log progress
                    if processed_count % 10 == 0 or idx == len(industries) - 1:
                        elapsed = time.time() - start_time
                        rate = processed_count / elapsed if elapsed > 0 else 0
                        logger.info(f"Processed {processed_count}/{len(industries)} {grouping_col} values "
                                  f"({processed_count/len(industries)*100:.1f}%) - {rate:.1f} industries/sec")
            else:
                if idx % 10 == 0:
                    logger.debug(f"{industry}: Only {len(mature_account_revenue)} mature accounts, "
                              f"need {min_accounts} for quintiles")
    
    logger.info(f"Calculated quintiles for {len(quintiles)} industry/subindustry groups")
    return quintiles


# ============= Strategic Revenue Score Calculation =============
def calculate_strategic_revenue_score(
    account_data: pd.DataFrame,
    industry_quintiles: Dict[str, Dict[str, float]],
    account_pattern: str = 'continuous',
    account_info: Optional[Dict[str, any]] = None
) -> Tuple[int, Dict[str, any]]:
    """Calculate strategic revenue score based on peer benchmarking and long-term trends."""
    details = {
        'method': None,
        'current_revenue': 0,
        'comparison_revenue': 0,
        'revenue_change_pct': 0,
        'quintile_movement': 0,
        'account_pattern': account_pattern,
        'peer_comparison': {}
    }
    
    if account_data.empty:
        logger.warning("Empty account data provided")
        return 0, details
    
    account_data['TransactionDate'] = pd.to_datetime(account_data['TransactionDate'])
    
    account_id = account_data['AccountId'].iloc[0] if 'AccountId' in account_data.columns else 'unknown'
    
    # Get account info
    acc_info = get_account_info_from_data(account_data, account_id)
    
    logger.debug(f"Processing account {account_id}, industry='{acc_info.industry}', subindustry='{acc_info.subindustry}'")
    
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
    
    # Get appropriate comparison period
    comparison_start, comparison_end, comparison_type = get_seasonal_comparison_period(
        current_date, account_pattern, months_active, acc_info.is_education
    )
    details['comparison_type'] = comparison_type
    details['comparison_start'] = comparison_start.isoformat()
    details['comparison_end'] = comparison_end.isoformat()
    
    # Check if we have industry quintiles
    quintile_key = None
    
    # For seasonal comparison, check seasonal quintiles first
    if comparison_type == 'year_over_year' and account_pattern in ['seasonal', 'annual']:
        if acc_info.subindustry and f"seasonal_SubIndustry:{acc_info.subindustry}" in industry_quintiles:
            quintile_key = f"seasonal_SubIndustry:{acc_info.subindustry}"
        elif acc_info.industry and f"seasonal_Industry:{acc_info.industry}" in industry_quintiles:
            quintile_key = f"seasonal_Industry:{acc_info.industry}"
    
    # Fall back to current period quintiles
    if not quintile_key:
        if acc_info.subindustry and f"SubIndustry:{acc_info.subindustry}" in industry_quintiles:
            quintile_key = f"SubIndustry:{acc_info.subindustry}"
        elif acc_info.industry and f"Industry:{acc_info.industry}" in industry_quintiles:
            quintile_key = f"Industry:{acc_info.industry}"
    
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
        
        quintile_movement = current_quintile - comparison_quintile  # Positive is improvement
        details['quintile_movement'] = quintile_movement
        details['current_quintile'] = current_quintile
        details['comparison_quintile'] = comparison_quintile
        
        # Strategic scoring based on sustained quintile position
        # Focus on long-term underperformance patterns, not short-term volatility
        
        # Score based on current position and trajectory
        if current_quintile == 1:  # Bottom 20% of industry
            if comparison_quintile >= 4:  # Was in top 40%
                score = 3  # High strategic risk - major decline
            elif comparison_quintile >= 3:  # Was in middle tier
                score = 2  # Medium strategic risk - significant decline
            elif months_active >= 12:  # Established but consistently poor
                score = 2  # Medium strategic risk - chronic underperformer
            else:
                score = 1  # Low strategic risk - may still be establishing
        elif current_quintile == 2:  # 20-40% percentile
            if comparison_quintile >= 4:  # Was in top 40%
                score = 2  # Medium strategic risk - notable decline
            elif months_active >= 12 and quintile_movement < 0:  # Established and declining
                score = 1  # Low strategic risk - gradual decline
            else:
                score = 0  # No strategic concern
        elif current_quintile >= 3 and quintile_movement <= -2:
            # Still performing OK but declining trajectory
            score = 1  # Low strategic risk - monitor trend
        else:
            score = 0  # No strategic concern - performing well or improving
        
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
        
        logger.debug(f"Account {account_id}: Strategic quintile analysis - current_q={current_quintile}, "
                    f"comparison_q={comparison_quintile}, movement={quintile_movement}, strategic_score={score}")
    
    else:
        # Fallback to activity-based thresholds
        details['method'] = 'activity_based'
        details['fallback_reason'] = f"No quintiles for industry='{acc_info.industry}', subindustry='{acc_info.subindustry}'"
        
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
        
        # Calculate revenue change for strategic assessment
        if comparison_revenue > 0:
            revenue_ratio = current_revenue / comparison_revenue
            details['revenue_ratio'] = revenue_ratio
            details['revenue_change_pct'] = (revenue_ratio - 1) * 100  # Positive for growth, negative for decline
            
            # Strategic scoring based on sustained performance decline
            # More lenient thresholds than rapid detection - focus on trends
            if months_active >= 12:  # Established accounts
                if revenue_ratio < 0.25:  # Lost 75%+ of historical revenue
                    score = 3  # High strategic risk
                elif revenue_ratio < 0.5:  # Lost 50%+ of historical revenue
                    score = 2  # Medium strategic risk
                elif revenue_ratio < 0.75:  # Lost 25%+ of historical revenue
                    score = 1  # Low strategic risk
                else:
                    score = 0  # No strategic concern
            else:  # Newer accounts - more lenient
                if revenue_ratio < 0.1 and current_revenue == 0:  # Complete loss
                    score = 2  # Medium strategic risk
                elif revenue_ratio < 0.25:  # Major decline
                    score = 1  # Low strategic risk
                else:
                    score = 0  # No strategic concern
        else:
            # No historical revenue to compare
            details['revenue_ratio'] = 0
            details['revenue_change_pct'] = 0
            
            if current_revenue == 0 and months_active >= 6:
                # Established account with no current or historical revenue
                score = 1  # Low strategic risk
                details['zero_revenue_flag'] = True
            else:
                score = 0
        
        logger.debug(f"Account {account_id}: Activity method - ratio={details.get('revenue_ratio', 0):.2f}, "
                    f"score={score}")
    
    return score, details


# ============= Main Entry Point =============
def get_revenue_factor(
    current_revenue: float,
    historical_revenue: pd.DataFrame,
    industry_data: Optional[pd.DataFrame] = None,
    account_type: str = 'continuous',
    account_info: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Main function to calculate revenue factor and risk score.
    
    Args:
        current_revenue: Current period revenue
        historical_revenue: DataFrame with historical revenue data
        industry_data: Optional DataFrame with industry peer data
        account_type: Account pattern type ('continuous', 'seasonal', 'annual')
        account_info: Optional account metadata
    
    Returns:
        Dict with revenue factor score and supporting details
    """
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
        logger.info(f"Calculating industry quintiles for revenue analysis ({len(industry_data):,} records)")
        
        # Get accounts master data if available
        accounts_df = None
        if account_info and 'accounts_df' in account_info:
            accounts_df = account_info['accounts_df']
        
        # Calculate quintiles for current period
        current_quintiles = calculate_industry_quintiles(
            booking_df=industry_data,
            accounts_df=accounts_df,
            time_period='current',
            min_accounts=MIN_ACCOUNTS_FOR_QUINTILES
        )
        industry_quintiles.update(current_quintiles)
        
        # For seasonal accounts, also calculate seasonal quintiles
        if account_type in ['seasonal', 'annual']:
            seasonal_quintiles = calculate_industry_quintiles(
                booking_df=industry_data,
                accounts_df=accounts_df,
                time_period='seasonal',
                min_accounts=MIN_ACCOUNTS_FOR_QUINTILES
            )
            industry_quintiles.update(seasonal_quintiles)
    
    # Apply seasonal handling if needed
    acc_info = get_account_info_from_data(historical_revenue, account_id)
    if account_type in ['seasonal', 'annual'] or acc_info.is_education:
        processed_data = handle_seasonal_comparison(historical_revenue, account_type)
    else:
        processed_data = historical_revenue
    
    # Calculate strategic revenue score
    if not processed_data.empty:
        # Calculate with full context
        score, details = calculate_strategic_revenue_score(
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


# ============= Batch Processing =============
def batch_process_revenue_factors(accounts_df: pd.DataFrame, booking_df: pd.DataFrame, 
                                 batch_size: int = 5000) -> pd.DataFrame:
    """
    Process revenue factor calculations in batches with progress logging.
    
    Args:
        accounts_df: DataFrame with account information
        booking_df: DataFrame with booking/transaction data
        batch_size: Number of accounts to process per batch
        
    Returns:
        DataFrame with revenue factor scores added
    """
    total_accounts = len(accounts_df)
    logger.info(f"Starting revenue factor calculation for {total_accounts:,} accounts")
    start_time = time.time()
    
    # Pre-calculate industry quintiles once
    logger.info("Pre-calculating industry quintiles...")
    quintile_start = time.time()
    
    industry_quintiles = calculate_industry_quintiles(
        booking_df=booking_df,
        accounts_df=accounts_df,
        time_period='current',
        min_accounts=MIN_ACCOUNTS_FOR_QUINTILES
    )
    
    # Add seasonal quintiles
    seasonal_quintiles = calculate_industry_quintiles(
        booking_df=booking_df,
        accounts_df=accounts_df,
        time_period='seasonal',
        min_accounts=MIN_ACCOUNTS_FOR_QUINTILES
    )
    industry_quintiles.update(seasonal_quintiles)
    
    quintile_time = time.time() - quintile_start
    logger.info(f"Calculated {len(industry_quintiles)} industry quintile sets in {quintile_time:.1f}s")
    
    # Process accounts in batches
    results = []
    
    for i in range(0, total_accounts, batch_size):
        batch_start_time = time.time()
        batch_end = min(i + batch_size, total_accounts)
        batch_accounts = accounts_df.iloc[i:batch_end]
        
        batch_results = []
        
        # Process each account in the batch
        for _, account in batch_accounts.iterrows():
            account_id = str(account['AccountId'])
            
            # Get account's transaction history
            account_history = get_account_revenue_history(account_id, booking_df)
            
            # Determine account type
            account_type = account.get('Event_Frequency_Current', 'continuous').lower()
            if account_type in ['annual', 'seasonal']:
                pattern_type = account_type
            else:
                pattern_type = 'continuous'
            
            # Calculate current revenue
            current_revenue = account.get('_revenue_current', 0)
            
            # Get revenue factor
            revenue_result = get_revenue_factor(
                current_revenue=current_revenue,
                historical_revenue=account_history,
                industry_data=booking_df,
                account_type=pattern_type,
                account_info={'accounts_df': accounts_df}
            )
            
            batch_results.append({
                'AccountId': account_id,
                'Revenue_Factor_Score': revenue_result['score'],
                'Revenue_Factor_Severity': revenue_result['severity'],
                'Revenue_Factor_Details': revenue_result['details']
            })
        
        results.extend(batch_results)
        
        # Log progress with timing
        batch_time = time.time() - batch_start_time
        progress_pct = (batch_end / total_accounts) * 100
        accounts_per_sec = len(batch_accounts) / batch_time if batch_time > 0 else 0
        
        logger.info(f"Processed {batch_end:,} of {total_accounts:,} accounts ({progress_pct:.1f}%) - "
                   f"{accounts_per_sec:,.0f} accounts/sec")
    
    # Create results DataFrame
    results_df = pd.DataFrame(results)
    
    # Log summary statistics
    total_time = time.time() - start_time
    score_distribution = results_df['Revenue_Factor_Score'].value_counts().sort_index()
    
    logger.info(f"Revenue factor calculation complete in {total_time:.1f}s ({total_accounts/total_time:,.0f} accounts/sec)")
    logger.info("Revenue factor score distribution:")
    for score, count in score_distribution.items():
        pct = (count / total_accounts) * 100
        logger.info(f"  Score {score}: {count:,} accounts ({pct:.1f}%)")
    
    return results_df