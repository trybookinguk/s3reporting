"""
Rapid revenue drop detection for operational alerts.
Focuses on detecting sudden revenue drops within 4-8 week windows for Tier 3+ accounts.
"""
import pandas as pd
from datetime import datetime, timedelta
import logging
from .utils.config import REVENUE_DROP_THRESHOLDS, MIN_REVENUE_FOR_RAPID_DROP

logger = logging.getLogger(__name__)

# Tier hierarchy for filtering (only Tier 3 and above)
HIGH_VALUE_TIERS = ["Key Account", "High Value", "Tier 4", "Tier 3"]

# Period definitions for drop detection (in weeks)
CURRENT_PERIOD_WEEKS = 4  # Last 4 weeks
COMPARISON_PERIOD_WEEKS = 8  # Previous 8 weeks before current period


def detect_rapid_drop(account_data, account_info):
    """
    Main entry point for rapid revenue drop detection.
    
    Args:
        account_data: DataFrame with booking data for the account
        account_info: Dict with account metadata including:
            - tier: Account tier classification
            - event_frequency: Event frequency pattern (Continuous/Regular/Seasonal/Annual)
            - months_active: List of months (1-12) when account typically has events
            
    Returns:
        dict: {
            'score': 0-3 (none, moderate, significant, severe),
            'severity': severity label,
            'current_revenue': revenue in current period,
            'comparison_revenue': revenue in comparison period,
            'drop_percentage': percentage drop (0-100),
            'detection_method': method used for detection,
            'details': additional context
        }
    """
    # Check if account qualifies for rapid drop detection
    if account_info.get('tier', 'Tier 1') not in HIGH_VALUE_TIERS:
        return {
            'score': 0,
            'severity': 'none',
            'details': 'Account tier too low for rapid drop detection'
        }
    
    # Get event frequency and months active
    event_frequency = account_info.get('event_frequency', 'Unknown')
    months_active = account_info.get('months_active', [])
    
    # Route to appropriate detection method based on event frequency
    if event_frequency == 'Continuous':
        return check_continuous_drop(account_data)
    elif event_frequency == 'Regular':
        return check_regular_drop(account_data, months_active)
    elif event_frequency == 'Seasonal':
        return check_seasonal_drop(account_data, months_active)
    elif event_frequency == 'Annual':
        return check_annual_drop(account_data, months_active)
    else:
        return {
            'score': 0,
            'severity': 'none',
            'details': f'No drop detection for {event_frequency} accounts'
        }


def check_continuous_drop(account_data):
    """
    Check for revenue drops in continuous accounts (events every month).
    
    Args:
        account_data: DataFrame with booking data
        
    Returns:
        dict: Drop detection results
    """
    # Calculate current and comparison period revenue
    current_revenue = calculate_period_revenue(account_data, CURRENT_PERIOD_WEEKS)
    comparison_revenue = calculate_period_revenue(account_data, COMPARISON_PERIOD_WEEKS, offset=CURRENT_PERIOD_WEEKS)
    
    # Calculate drop severity
    result = calculate_drop_severity(current_revenue, comparison_revenue)
    result['detection_method'] = 'continuous'
    
    return result


def check_regular_drop(account_data, months_active):
    """
    Check for revenue drops in regular accounts (events most months).
    Only checks if we're in or near an active month.
    
    Args:
        account_data: DataFrame with booking data
        months_active: List of months (1-12) when account typically has events
        
    Returns:
        dict: Drop detection results
    """
    # Check if we should expect activity in the current period
    current_month = datetime.now().month
    previous_month = (current_month - 1) if current_month > 1 else 12
    
    # Check if current or previous month is typically active
    if current_month not in months_active and previous_month not in months_active:
        return {
            'score': 0,
            'severity': 'none',
            'details': f'Not in active period (active months: {months_active})'
        }
    
    # Calculate current and comparison period revenue
    current_revenue = calculate_period_revenue(account_data, CURRENT_PERIOD_WEEKS)
    comparison_revenue = calculate_period_revenue(account_data, COMPARISON_PERIOD_WEEKS, offset=CURRENT_PERIOD_WEEKS)
    
    # Calculate drop severity
    result = calculate_drop_severity(current_revenue, comparison_revenue)
    result['detection_method'] = 'regular'
    result['active_months'] = months_active
    
    return result


def check_seasonal_drop(account_data, months_active):
    """
    Check for revenue drops in seasonal accounts.
    Only checks during their active season.
    
    Args:
        account_data: DataFrame with booking data
        months_active: List of months (1-12) when account typically has events
        
    Returns:
        dict: Drop detection results
    """
    # Check if we're in the active season
    current_month = datetime.now().month
    
    # Allow for season boundaries (check adjacent months too)
    extended_active_months = set(months_active)
    for month in months_active:
        # Add month before
        prev_month = (month - 1) if month > 1 else 12
        extended_active_months.add(prev_month)
        # Add month after  
        next_month = (month + 1) if month < 12 else 1
        extended_active_months.add(next_month)
    
    if current_month not in extended_active_months:
        return {
            'score': 0,
            'severity': 'none',
            'details': f'Not in active season (active months: {months_active})'
        }
    
    # For seasonal accounts, compare to same period last year
    current_revenue = calculate_period_revenue(account_data, CURRENT_PERIOD_WEEKS)
    
    # Get revenue from same period last year (52 weeks ago)
    yoy_revenue = calculate_period_revenue(account_data, COMPARISON_PERIOD_WEEKS, offset=52)
    
    # Calculate drop severity
    result = calculate_drop_severity(current_revenue, yoy_revenue)
    result['detection_method'] = 'seasonal_yoy'
    result['active_months'] = months_active
    
    return result


def check_annual_drop(account_data, months_active):
    """
    Check for revenue drops in annual accounts.
    Only checks during their specific event month.
    
    Args:
        account_data: DataFrame with booking data
        months_active: List of months (1-12) when account typically has events
        
    Returns:
        dict: Drop detection results
    """
    # Annual accounts should have exactly one active month
    if not months_active or len(months_active) != 1:
        return {
            'score': 0,
            'severity': 'none',
            'details': 'Annual account without clear single month pattern'
        }
    
    event_month = months_active[0]
    current_month = datetime.now().month
    
    # Check if we're in or just after the event month
    if current_month != event_month and current_month != (event_month % 12) + 1:
        return {
            'score': 0,
            'severity': 'none',
            'details': f'Not in event period (event month: {event_month})'
        }
    
    # For annual accounts, compare current month to same month last year
    current_revenue = calculate_period_revenue(account_data, 8)  # 8 weeks to capture full event
    
    # Get revenue from same period last year
    yoy_revenue = calculate_period_revenue(account_data, 8, offset=52)
    
    # Calculate drop severity
    result = calculate_drop_severity(current_revenue, yoy_revenue)
    result['detection_method'] = 'annual_yoy'
    result['event_month'] = event_month
    
    return result


def calculate_period_revenue(account_data, weeks, offset=0):
    """
    Calculate total revenue for a specific period.
    
    Args:
        account_data: DataFrame with booking data (must have 'TransactionDate' and 'PaymentReceived')
        weeks: Number of weeks to include in the period
        offset: Number of weeks to offset from today (0 = current period)
        
    Returns:
        float: Total revenue in the period
    """
    if account_data.empty:
        return 0.0
    
    # Ensure TransactionDate is datetime
    if 'TransactionDate' not in account_data.columns:
        logger.warning("TransactionDate column not found in account data")
        return 0.0
    
    account_data = account_data.copy()
    account_data['TransactionDate'] = pd.to_datetime(account_data['TransactionDate'], errors='coerce')
    
    # Calculate period boundaries
    end_date = datetime.now() - timedelta(weeks=offset)
    start_date = end_date - timedelta(weeks=weeks)
    
    # Filter data to period
    period_data = account_data[
        (account_data['TransactionDate'] >= start_date) & 
        (account_data['TransactionDate'] < end_date)
    ]
    
    # Sum revenue
    if 'PaymentReceived' in period_data.columns:
        revenue = period_data['PaymentReceived'].sum()
        return float(revenue) if pd.notna(revenue) else 0.0
    else:
        logger.warning("PaymentReceived column not found in account data")
        return 0.0


def calculate_drop_severity(current_revenue, comparison_revenue):
    """
    Calculate the severity of a revenue drop.
    
    Args:
        current_revenue: Revenue in current period
        comparison_revenue: Revenue in comparison period
        
    Returns:
        dict: Drop detection results with score and severity
    """
    # Check if revenue meets minimum threshold
    if comparison_revenue < MIN_REVENUE_FOR_RAPID_DROP:
        return {
            'score': 0,
            'severity': 'none',
            'current_revenue': round(current_revenue, 2),
            'comparison_revenue': round(comparison_revenue, 2),
            'drop_percentage': 0,
            'details': f'Revenue below threshold (£{MIN_REVENUE_FOR_RAPID_DROP})'
        }
    
    # Handle edge cases
    if comparison_revenue == 0:
        if current_revenue == 0:
            return {
                'score': 0,
                'severity': 'none',
                'current_revenue': 0,
                'comparison_revenue': 0,
                'drop_percentage': 0,
                'details': 'No revenue in either period'
            }
        else:
            return {
                'score': 0,
                'severity': 'none',
                'current_revenue': current_revenue,
                'comparison_revenue': 0,
                'drop_percentage': 0,
                'details': 'No comparison baseline (new revenue)'
            }
    
    # Calculate percentage of comparison period
    revenue_ratio = current_revenue / comparison_revenue
    drop_percentage = max(0, (1 - revenue_ratio) * 100)
    
    # Determine severity based on thresholds
    if revenue_ratio < REVENUE_DROP_THRESHOLDS['severe']:
        score = 3
        severity = 'severe'
    elif revenue_ratio < REVENUE_DROP_THRESHOLDS['significant']:
        score = 2
        severity = 'significant'
    elif revenue_ratio < REVENUE_DROP_THRESHOLDS['moderate']:
        score = 1
        severity = 'moderate'
    else:
        score = 0
        severity = 'none'
    
    return {
        'score': score,
        'severity': severity,
        'current_revenue': round(current_revenue, 2),
        'comparison_revenue': round(comparison_revenue, 2),
        'drop_percentage': round(drop_percentage, 1),
        'revenue_ratio': round(revenue_ratio, 3)
    }


def batch_detect_drops(accounts_df, booking_data_df, batch_size=1000):
    """
    Process drop detection for multiple accounts in batches.
    
    Args:
        accounts_df: DataFrame with account information (AccountId, Tier, event_frequency, months_active)
        booking_data_df: DataFrame with all booking data
        batch_size: Number of accounts to process per batch
        
    Returns:
        DataFrame with drop detection results
    """
    # Filter to only high-value accounts
    eligible_accounts = accounts_df[accounts_df['Tier'].isin(HIGH_VALUE_TIERS)].copy()
    
    logger.info(f"Processing rapid drop detection for {len(eligible_accounts):,} high-value accounts")
    
    results = []
    total_accounts = len(eligible_accounts)
    
    for i in range(0, total_accounts, batch_size):
        batch_end = min(i + batch_size, total_accounts)
        batch = eligible_accounts.iloc[i:batch_end]
        
        for _, account in batch.iterrows():
            account_id = account['AccountId']
            
            # Get booking data for this account
            account_bookings = booking_data_df[booking_data_df['AccountId'] == account_id]
            
            # Prepare account info
            account_info = {
                'tier': account.get('Tier', 'Unknown'),
                'event_frequency': account.get('event_frequency', 'Unknown'),
                'months_active': account.get('months_active', [])
            }
            
            # Detect drops
            drop_result = detect_rapid_drop(account_bookings, account_info)
            
            # Add account ID to result
            drop_result['AccountId'] = account_id
            drop_result['AccountName'] = account.get('AccountName', '')
            
            results.append(drop_result)
        
        # Log progress
        if (i + batch_size) % 5000 == 0 or batch_end == total_accounts:
            progress_pct = (batch_end / total_accounts) * 100
            logger.info(f"Processed {batch_end:,} of {total_accounts:,} accounts ({progress_pct:.1f}%)")
    
    # Convert to DataFrame
    results_df = pd.DataFrame(results)
    
    # Log summary
    severe_count = len(results_df[results_df['severity'] == 'severe'])
    significant_count = len(results_df[results_df['severity'] == 'significant'])
    moderate_count = len(results_df[results_df['severity'] == 'moderate'])
    
    logger.info(f"Drop detection complete:")
    logger.info(f"  Severe drops: {severe_count:,} accounts")
    logger.info(f"  Significant drops: {significant_count:,} accounts")
    logger.info(f"  Moderate drops: {moderate_count:,} accounts")
    
    return results_df