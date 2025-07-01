"""
Common metrics calculations for TryBooking reports.
"""
import pandas as pd


def calculate_yoy_change(current_value, previous_value):
    """
    Calculate year-over-year percentage change.
    
    Args:
        current_value: Current period value
        previous_value: Previous period value
    
    Returns:
        float: Percentage change (0 if previous_value is 0)
    """
    if previous_value == 0:
        return 0
    return ((current_value - previous_value) / previous_value) * 100


def calculate_percentage(part, whole):
    """
    Calculate percentage with safe division.
    
    Args:
        part: Numerator
        whole: Denominator
    
    Returns:
        float: Percentage (0 if whole is 0)
    """
    if whole == 0:
        return 0
    return (part / whole) * 100


def summarize_by_time_category(df, datetime_col, categories):
    """
    Summarize DataFrame by time categories (e.g., day/evening).
    
    Args:
        df: DataFrame to summarize
        datetime_col: Name of datetime column
        categories: Dict mapping category names to classifier functions
    
    Returns:
        DataFrame with counts by category
    """
    results = {}
    for category_name, classifier_func in categories.items():
        mask = df[datetime_col].apply(classifier_func)
        results[category_name] = mask.sum()
    
    return pd.Series(results)


def aggregate_by_day_of_week(df, datetime_col, value_col=None):
    """
    Aggregate data by day of week.
    
    Args:
        df: DataFrame to aggregate
        datetime_col: Name of datetime column
        value_col: Optional column to sum (if None, counts rows)
    
    Returns:
        DataFrame with aggregations by day of week
    """
    df = df.copy()
    df['DayOfWeek'] = df[datetime_col].dt.strftime('%A')
    
    if value_col:
        result = df.groupby('DayOfWeek')[value_col].sum()
    else:
        result = df.groupby('DayOfWeek').size()
    
    # Reorder by weekday
    weekday_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    result = result.reindex(weekday_order, fill_value=0)
    
    return result


def calculate_transaction_metrics(df, amount_col='PaymentReceived', quantity_col='TicketQuantity'):
    """
    Calculate common transaction metrics.
    
    Args:
        df: DataFrame with transaction data
        amount_col: Column name for transaction amounts
        quantity_col: Column name for quantities
    
    Returns:
        dict: Metrics including count, total, average amount, average quantity
    """
    return {
        'count': len(df),
        'total_amount': df[amount_col].sum(),
        'avg_amount': df[amount_col].mean() if len(df) > 0 else 0,
        'avg_quantity': df[quantity_col].mean() if len(df) > 0 else 0,
        'total_quantity': df[quantity_col].sum()
    }


def calculate_fee_metrics(df, fee_columns=None):
    """
    Calculate fee-related metrics with automatic column detection.
    
    Args:
        df: DataFrame with fee data
        fee_columns: Optional list of fee column names
    
    Returns:
        dict: Fee totals by type and grand total
    """
    if fee_columns is None:
        # Auto-detect fee columns
        fee_columns = [col for col in df.columns if 'Fee' in col and col != 'TotalFees']
    
    metrics = {}
    for col in fee_columns:
        if col in df.columns:
            metrics[col] = df[col].sum()
    
    # Calculate total if not already present
    if 'TotalFees' in df.columns:
        metrics['TotalFees'] = df['TotalFees'].sum()
    else:
        metrics['TotalFees'] = sum(metrics.values())
    
    return metrics


def filter_date_range(df, date_col, start_date, end_date):
    """
    Filter DataFrame to date range with consistent timezone handling.
    
    Args:
        df: DataFrame to filter
        date_col: Name of date column
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
    
    Returns:
        Filtered DataFrame
    """
    return df[(df[date_col] >= start_date) & (df[date_col] <= end_date)].copy()