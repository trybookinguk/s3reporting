"""
Revenue Factor Module - Strategic Revenue Analysis and Risk Assessment.

This module provides strategic revenue analysis focused on long-term trends and peer comparisons:
- Industry quintile calculations for benchmarking against peers
- Year-over-year (YoY) and rolling average comparisons 
- Account lifecycle-aware assessment (new vs established accounts)
- Seasonal pattern recognition for education and annual events

This is designed for strategic risk assessment, NOT operational alerts.
For rapid revenue drop detection, use rapid_drop_detector.py instead.
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, Tuple
from dataclasses import dataclass
import logging
import pytz

from .utils.config import (
    MIN_ACCOUNTS_FOR_QUINTILES, MATURE_ACCOUNT_AGE_DAYS,
    REVENUE_DROP_THRESHOLDS, QUINTILE_DROP_SCORING,
    ZERO_REVENUE_COMMON_THRESHOLD, ACCOUNT_LIFECYCLE_STAGES,
    COMPARISON_PERIOD_DAYS
)

logger = logging.getLogger(__name__)


@dataclass
class RevenueMetrics:
    """Strategic revenue metrics for analysis"""
    current: float = 0.0
    comparison: float = 0.0
    ratio: float = 0.0
    change_percentage: float = 0.0
    quintile_current: int = 0
    quintile_comparison: int = 0
    lifecycle_stage: str = "unknown"


@dataclass
class IndustryQuintiles:
    """Industry quintile thresholds and metadata"""
    thresholds: Dict[int, float]
    account_count: int
    zero_revenue_pct: float
    median_revenue: float
    period_info: str


def get_account_lifecycle_stage(account_age_days: int) -> str:
    """Determine account lifecycle stage based on age."""
    age_weeks = account_age_days / 7
    
    if age_weeks <= ACCOUNT_LIFECYCLE_STAGES["new_building"]:
        return "new_building"
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES["new_expected"]:
        return "new_expected"
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES["establishing"]:
        return "establishing"
    elif age_weeks <= ACCOUNT_LIFECYCLE_STAGES["maturing"]:
        return "maturing"
    else:
        return "established"


def calculate_revenue_for_period(df: pd.DataFrame, days_back: int, 
                                revenue_col: str = 'PaymentReceived') -> float:
    """Calculate total revenue for a specific period with robust error handling."""
    if df.empty:
        return 0.0
    
    try:
        # Ensure TransactionDate is datetime
        if 'TransactionDate' not in df.columns:
            logger.warning("TransactionDate column missing from revenue data")
            return 0.0
        
        df_copy = df.copy()
        df_copy['TransactionDate'] = pd.to_datetime(df_copy['TransactionDate'], errors='coerce')
        
        # Filter out rows with invalid dates
        valid_dates = df_copy['TransactionDate'].notna()
        if not valid_dates.any():
            logger.warning("No valid transaction dates found")
            return 0.0
        
        df_copy = df_copy[valid_dates]
        
        # Calculate cutoff date
        cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=days_back)
        recent_data = df_copy[df_copy['TransactionDate'] >= cutoff_date]
        
        if recent_data.empty:
            return 0.0
        
        # Handle revenue column
        if revenue_col not in recent_data.columns:
            logger.warning(f"Revenue column '{revenue_col}' missing, trying alternatives")
            # Try alternative revenue columns
            for alt_col in ['Revenue', 'PaymentReceived', 'revenue_current']:
                if alt_col in recent_data.columns:
                    revenue_col = alt_col
                    break
            else:
                logger.error("No valid revenue column found")
                return 0.0
        
        # Sum revenue with null handling
        revenue_sum = recent_data[revenue_col].fillna(0).sum()
        return float(revenue_sum) if pd.notna(revenue_sum) else 0.0
        
    except Exception as e:
        logger.error(f"Error calculating revenue for period: {e}")
        return 0.0


def calculate_revenue_for_period_range(df: pd.DataFrame, start_days_back: int, 
                                      end_days_back: int, revenue_col: str = 'PaymentReceived') -> float:
    """Calculate revenue for a specific date range (e.g., for YoY comparisons)."""
    if df.empty:
        return 0.0
    
    try:
        # Ensure TransactionDate is datetime
        if 'TransactionDate' not in df.columns:
            return 0.0
        
        df_copy = df.copy()
        df_copy['TransactionDate'] = pd.to_datetime(df_copy['TransactionDate'], errors='coerce')
        
        # Filter out rows with invalid dates
        valid_dates = df_copy['TransactionDate'].notna()
        if not valid_dates.any():
            return 0.0
        
        df_copy = df_copy[valid_dates]
        
        # Calculate date range
        end_date = pd.Timestamp.now() - pd.Timedelta(days=end_days_back)
        start_date = pd.Timestamp.now() - pd.Timedelta(days=start_days_back)
        
        # Filter to date range
        range_data = df_copy[
            (df_copy['TransactionDate'] >= start_date) & 
            (df_copy['TransactionDate'] <= end_date)
        ]
        
        if range_data.empty:
            return 0.0
        
        # Handle revenue column
        if revenue_col not in range_data.columns:
            for alt_col in ['Revenue', 'PaymentReceived', 'revenue_current']:
                if alt_col in range_data.columns:
                    revenue_col = alt_col
                    break
            else:
                return 0.0
        
        # Sum revenue with null handling
        revenue_sum = range_data[revenue_col].fillna(0).sum()
        return float(revenue_sum) if pd.notna(revenue_sum) else 0.0
        
    except Exception as e:
        logger.error(f"Error calculating revenue for date range: {e}")
        return 0.0


def calculate_industry_quintiles(industry_data: pd.DataFrame, 
                                period_type: str = "current") -> IndustryQuintiles:
    """
    Calculate industry quintiles for peer benchmarking.
    
    Args:
        industry_data: DataFrame with revenue data for the industry
        period_type: Type of period analysis ("current", "yoy", "rolling")
        
    Returns:
        IndustryQuintiles object with thresholds and metadata
    """
    if industry_data.empty:
        logger.warning("Empty industry data provided for quintile calculation")
        return IndustryQuintiles(
            thresholds={}, account_count=0, zero_revenue_pct=100.0,
            median_revenue=0.0, period_info=f"Empty data for {period_type}"
        )
    
    # Get period-appropriate revenue data
    period_days = COMPARISON_PERIOD_DAYS.get(period_type, 84)
    
    # Revenue calculation by account for the period
    revenues = industry_data.groupby('AccountId').apply(
        lambda group: calculate_revenue_for_period(group, period_days)
    ).reset_index(drop=True)
    
    if len(revenues) < MIN_ACCOUNTS_FOR_QUINTILES:
        logger.warning(f"Insufficient accounts ({len(revenues)}) for reliable quintiles")
        return IndustryQuintiles(
            thresholds={}, account_count=len(revenues), zero_revenue_pct=100.0,
            median_revenue=0.0, period_info=f"Insufficient data for {period_type}"
        )
    
    # Calculate quintile thresholds (1=lowest, 5=highest)
    thresholds = {}
    for quintile in range(1, 6):
        percentile = quintile * 20
        threshold = revenues.quantile(percentile / 100)
        thresholds[quintile] = float(threshold)
    
    # Calculate metadata
    zero_revenue_count = (revenues == 0).sum()
    zero_revenue_pct = (zero_revenue_count / len(revenues)) * 100
    median_revenue = float(revenues.median())
    
    return IndustryQuintiles(
        thresholds=thresholds,
        account_count=len(revenues),
        zero_revenue_pct=zero_revenue_pct,
        median_revenue=median_revenue,
        period_info=f"{period_type.title()} period ({period_days} days)"
    )


def get_quintile_for_revenue(revenue: float, quintiles: IndustryQuintiles) -> int:
    """Determine which quintile a revenue amount falls into."""
    if not quintiles.thresholds:
        return 0
    
    for quintile in range(5, 0, -1):  # Check from highest to lowest
        if revenue >= quintiles.thresholds[quintile]:
            return quintile
    
    return 1  # Lowest quintile


def assess_revenue_trend(current_revenue: float, comparison_revenue: float,
                        account_pattern: str = "continuous") -> Tuple[str, int]:
    """
    Assess revenue trend severity and score.
    
    Args:
        current_revenue: Current period revenue
        comparison_revenue: Comparison period revenue  
        account_pattern: Account type (reserved for future pattern-specific logic)
        
    Returns:
        Tuple of (severity_label, severity_score)
    """
    # Note: account_pattern reserved for future pattern-specific thresholds
    if comparison_revenue <= 0:
        return "stable", 0  # No baseline for comparison
    
    ratio = current_revenue / comparison_revenue
    
    # Apply thresholds
    if ratio < REVENUE_DROP_THRESHOLDS["severe"]:
        return "severe", 3
    elif ratio < REVENUE_DROP_THRESHOLDS["significant"]:
        return "significant", 2  
    elif ratio < REVENUE_DROP_THRESHOLDS["moderate"]:
        return "moderate", 1
    else:
        return "stable", 0


def get_comparison_period_for_pattern(account_pattern: str) -> str:
    """Get appropriate comparison period based on account pattern."""
    if account_pattern in ["annual", "seasonal"]:
        return "yoy"  # Year-over-year for seasonal patterns
    else:
        return "rolling_average"  # Rolling average for continuous accounts


def get_revenue_factor(current_revenue: float, historical_revenue: pd.DataFrame,
                      industry_data: pd.DataFrame, account_type: str = "continuous",
                      account_info: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Main entry point for strategic revenue factor analysis.
    
    Args:
        current_revenue: Current period revenue
        historical_revenue: Historical transaction data for the account
        industry_data: Industry peer data for benchmarking
        account_type: Account pattern (continuous, seasonal, annual)
        account_info: Optional account metadata (may contain accounts_df or account_age_days)
        
    Returns:
        Dictionary with revenue analysis results
    """
    try:
        # Initialize account info
        account_info = account_info or {}
        
        # Calculate account age - handle both direct value and accounts_df lookup
        account_age_days = MATURE_ACCOUNT_AGE_DAYS + 1  # Default to mature account
        
        if 'account_age_days' in account_info:
            account_age_days = account_info['account_age_days']
        elif 'accounts_df' in account_info and not historical_revenue.empty:
            # Extract account ID and look up creation date
            try:
                accounts_df = account_info['accounts_df']
                if 'AccountId' in historical_revenue.columns:
                    account_id = str(historical_revenue['AccountId'].iloc[0])
                    account_row = accounts_df[accounts_df.index.astype(str) == account_id]
                    if not account_row.empty and 'DateTimeCreated' in account_row.columns:
                        created_date = pd.to_datetime(account_row['DateTimeCreated'].iloc[0], errors='coerce')
                        if pd.notna(created_date):
                            # Use UTC-aware datetime for consistency
                            now_utc = datetime.now(pytz.UTC) if hasattr(created_date, 'tz') and created_date.tz is not None else datetime.now()
                            account_age_days = (now_utc - created_date).days
            except Exception as e:
                logger.warning(f"Could not calculate account age from accounts_df: {e}")
        
        logger.debug(f"Account age calculated as {account_age_days} days")
        
        # Determine lifecycle stage
        lifecycle_stage = get_account_lifecycle_stage(account_age_days)
        
        # Get appropriate comparison period
        comparison_period = get_comparison_period_for_pattern(account_type)
        period_days = COMPARISON_PERIOD_DAYS[comparison_period]
        
        # Calculate comparison revenue using appropriate offset
        if comparison_period == "yoy":
            # For YoY, look at revenue from 365 days ago to (365-period_days) days ago
            comparison_start_days = 365 + period_days
            comparison_end_days = 365
            # Calculate revenue in the comparison window
            comparison_revenue = calculate_revenue_for_period_range(
                historical_revenue, comparison_start_days, comparison_end_days
            )
        else:
            # For rolling average, use the period directly
            comparison_revenue = calculate_revenue_for_period(historical_revenue, period_days)
        
        # For new accounts, use simpler analysis
        if lifecycle_stage in ["new_building", "new_expected"]:
            trend_severity, trend_score = assess_revenue_trend(
                current_revenue, comparison_revenue, account_type
            )
            
            return {
                "severity": trend_severity,
                "score": trend_score,
                "details": {
                    "current_revenue": current_revenue,
                    "comparison_revenue": comparison_revenue,
                    "lifecycle_stage": lifecycle_stage,
                    "comparison_method": "simple_trend",
                    "account_pattern": account_type
                }
            }
        
        # For established accounts, include industry quintile analysis
        current_quintiles = calculate_industry_quintiles(industry_data, "current")
        comparison_quintiles = calculate_industry_quintiles(industry_data, comparison_period)
        
        # Get quintile positions
        current_quintile = get_quintile_for_revenue(current_revenue, current_quintiles)
        comparison_quintile = get_quintile_for_revenue(comparison_revenue, comparison_quintiles)
        
        # Calculate basic trend
        trend_severity, trend_score = assess_revenue_trend(
            current_revenue, comparison_revenue, account_type
        )
        
        # Adjust score based on quintile movement
        quintile_drop = comparison_quintile - current_quintile
        if quintile_drop >= QUINTILE_DROP_SCORING["severe"]:
            trend_score = max(trend_score, 3)
            trend_severity = "severe"
        elif quintile_drop >= QUINTILE_DROP_SCORING["significant"]:
            trend_score = max(trend_score, 2)
            if trend_severity == "stable":
                trend_severity = "significant"
        elif quintile_drop >= QUINTILE_DROP_SCORING["moderate"]:
            trend_score = max(trend_score, 1)
            if trend_severity == "stable":
                trend_severity = "moderate"
        
        # Handle cases where industry has widespread zero revenue
        if (current_quintiles.zero_revenue_pct > ZERO_REVENUE_COMMON_THRESHOLD * 100 and
            current_revenue == 0):
            # If >30% of industry has zero revenue and account has zero, less concerning
            trend_score = max(0, trend_score - 1)
            if trend_score == 0:
                trend_severity = "stable"
        
        return {
            "severity": trend_severity,
            "score": trend_score,
            "details": {
                "current_revenue": current_revenue,
                "comparison_revenue": comparison_revenue,
                "current_quintile": current_quintile,
                "comparison_quintile": comparison_quintile,
                "quintile_drop": quintile_drop,
                "lifecycle_stage": lifecycle_stage,
                "comparison_method": comparison_period,
                "account_pattern": account_type,
                "industry_context": {
                    "account_count": current_quintiles.account_count,
                    "zero_revenue_pct": current_quintiles.zero_revenue_pct,
                    "median_revenue": current_quintiles.median_revenue
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Error in revenue factor calculation: {e}")
        # Fallback to simple trend analysis
        try:
            # Try to get comparison revenue even in error case
            fallback_comparison = 0
            if 'comparison_revenue' in locals():
                fallback_comparison = comparison_revenue
            elif not historical_revenue.empty:
                # Try simple period calculation as last resort
                fallback_comparison = calculate_revenue_for_period(historical_revenue, 84)  # 12 weeks
            
            trend_severity, trend_score = assess_revenue_trend(
                current_revenue, fallback_comparison, account_type
            )
            
            return {
                "severity": trend_severity,
                "score": trend_score,
                "details": {
                    "error": str(e),
                    "fallback_analysis": True,
                    "current_revenue": current_revenue,
                    "fallback_comparison_revenue": fallback_comparison,
                    "account_type": account_type
                }
            }
        except Exception as fallback_error:
            logger.error(f"Even fallback analysis failed: {fallback_error}")
            # Ultimate fallback - return safe defaults
            return {
                "severity": "stable",
                "score": 0,
                "details": {
                    "error": str(e),
                    "fallback_error": str(fallback_error),
                    "ultimate_fallback": True,
                    "current_revenue": current_revenue
                }
            }


def analyze_revenue_portfolio(accounts_df: pd.DataFrame, 
                             booking_data_df: pd.DataFrame) -> pd.DataFrame:
    """
    Analyze revenue factors for a portfolio of accounts.
    
    Args:
        accounts_df: DataFrame with account information
        booking_data_df: DataFrame with all booking transaction data
        
    Returns:
        DataFrame with revenue analysis results added
    """
    results = accounts_df.copy()
    results['revenue_severity'] = 'stable'
    results['revenue_score'] = 0
    results['revenue_details'] = None
    
    logger.info(f"Analyzing revenue factors for {len(accounts_df)} accounts")
    
    # Process by industry for better quintile calculations
    for industry, industry_accounts in accounts_df.groupby('Industry'):
        if pd.isna(industry):
            continue
            
        industry_booking_data = booking_data_df[booking_data_df['Industry'] == industry]
        
        for _, account in industry_accounts.iterrows():
            account_id = account['AccountId']
            account_bookings = booking_data_df[booking_data_df['AccountId'] == account_id]
            
            # Determine account pattern
            account_pattern = 'continuous'  # Default
            if account.get('Event_Frequency_Current') == 'Annual':
                account_pattern = 'annual'
            elif account.get('Event_Frequency_Current') == 'Seasonal':
                account_pattern = 'seasonal'
            
            # Get revenue factor
            revenue_result = get_revenue_factor(
                current_revenue=account.get('revenue_current', 0),
                historical_revenue=account_bookings,
                industry_data=industry_booking_data,
                account_type=account_pattern,
                account_info={
                    'account_age_days': account.get('account_age_days', MATURE_ACCOUNT_AGE_DAYS + 1)
                }
            )
            
            # Store results
            idx = results.index[results['AccountId'] == account_id].tolist()
            if idx:
                results.loc[idx[0], 'revenue_severity'] = revenue_result['severity']
                results.loc[idx[0], 'revenue_score'] = revenue_result['score']
                results.loc[idx[0], 'revenue_details'] = revenue_result['details']
    
    logger.info("Revenue factor analysis complete")
    return results


def get_revenue_summary(results_df: pd.DataFrame) -> Dict[str, Any]:
    """Generate summary statistics for revenue analysis results."""
    if 'revenue_severity' not in results_df.columns:
        return {"error": "No revenue analysis results found"}
    
    severity_counts = results_df['revenue_severity'].value_counts().to_dict()
    
    return {
        "total_accounts": len(results_df),
        "severity_distribution": severity_counts,
        "accounts_at_risk": len(results_df[
            results_df['revenue_severity'].isin(['moderate', 'significant', 'severe'])
        ]),
        "severe_risk_accounts": len(results_df[results_df['revenue_severity'] == 'severe']),
        "average_revenue_score": results_df['revenue_score'].mean(),
        "high_risk_industries": results_df[
            results_df['revenue_severity'] == 'severe'
        ]['Industry'].value_counts().head(5).to_dict()
    }