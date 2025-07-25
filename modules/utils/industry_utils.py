"""
Industry analysis utilities for TryBooking reporting.
"""
import pandas as pd
from .metrics_calculator import calculate_percentage


def filter_valid_industries(df, industry_column='Industry'):
    """
    Filter out invalid industries (Ticket Purchaser) and handle nulls.
    
    Args:
        df: DataFrame with industry column
        industry_column: Name of the industry column
        
    Returns:
        DataFrame with valid industries only
    """
    if industry_column not in df.columns:
        return df
    
    # Filter out Ticket Purchaser and null values
    valid_industries = df[
        df[industry_column].notna() & 
        (df[industry_column] != 'Ticket Purchaser')
    ].copy()
    
    return valid_industries


def calculate_industry_breakdown(bookings_df, metrics_to_calculate):
    """
    Calculate industry breakdown for given metrics.
    
    Args:
        bookings_df: DataFrame with booking data including Industry column
        metrics_to_calculate: List of metric columns to aggregate
                            e.g. ['EventId', 'TicketQuantity', 'PaymentReceived']
    
    Returns:
        DataFrame with industry breakdown
    """
    # Filter valid industries
    valid_bookings = filter_valid_industries(bookings_df)
    
    if len(valid_bookings) == 0 or 'Industry' not in valid_bookings.columns:
        return pd.DataFrame()
    
    # Group by industry
    industry_groups = valid_bookings.groupby('Industry')
    
    # Calculate metrics
    industry_metrics = pd.DataFrame()
    industry_metrics.index.name = 'Industry'
    
    # Calculate each metric
    if 'EventId' in metrics_to_calculate:
        industry_metrics['events'] = industry_groups['EventId'].nunique()
        
    if 'TicketQuantity' in metrics_to_calculate:
        industry_metrics['tickets'] = industry_groups['TicketQuantity'].sum()
        
    if 'PaymentReceived' in metrics_to_calculate:
        industry_metrics['revenue'] = industry_groups['PaymentReceived'].sum()
    
    # Calculate percentages
    totals = {
        'events': industry_metrics['events'].sum() if 'events' in industry_metrics else 0,
        'tickets': industry_metrics['tickets'].sum() if 'tickets' in industry_metrics else 0,
        'revenue': industry_metrics['revenue'].sum() if 'revenue' in industry_metrics else 0
    }
    
    # Add percentage columns
    if 'events' in industry_metrics:
        industry_metrics['events_pct'] = (industry_metrics['events'] / totals['events'] * 100).round(1)
        
    if 'tickets' in industry_metrics:
        industry_metrics['tickets_pct'] = (industry_metrics['tickets'] / totals['tickets'] * 100).round(1)
        
    if 'revenue' in industry_metrics:
        industry_metrics['revenue_pct'] = (industry_metrics['revenue'] / totals['revenue'] * 100).round(1)
    
    # Sort by revenue descending (or events if no revenue)
    sort_by = 'revenue' if 'revenue' in industry_metrics else 'events'
    industry_metrics = industry_metrics.sort_values(sort_by, ascending=False)
    
    # Reset index to make Industry a column
    industry_metrics = industry_metrics.reset_index()
    
    return industry_metrics


def prepare_booking_data_with_industry(booking_df, accounts_df):
    """
    Merge industry from accounts into booking data.
    
    Args:
        booking_df: DataFrame with booking data
        accounts_df: DataFrame with account data including Industry
        
    Returns:
        DataFrame with booking data including Industry column
    """
    # Prepare account industry data
    account_industry = accounts_df[['Id', 'Industry']].copy()
    account_industry['Id'] = account_industry['Id'].astype(str)
    
    # Ensure AccountId is string for consistent merging
    booking_df = booking_df.copy()
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    
    # Merge industry into bookings
    booking_with_industry = booking_df.merge(
        account_industry,
        left_on='AccountId',
        right_on='Id',
        how='left'
    )
    
    # Drop the duplicate Id column
    if 'Id' in booking_with_industry.columns:
        booking_with_industry = booking_with_industry.drop(columns=['Id'])
    
    return booking_with_industry


def format_industry_metrics_table(metrics_df, title, include_tickets=True):
    """
    Format industry metrics as HTML table for email.
    
    Args:
        metrics_df: DataFrame with industry metrics
        title: Title for the table
        include_tickets: Whether to include ticket metrics
        
    Returns:
        HTML string for the table
    """
    if metrics_df.empty:
        return ""
    
    html = f"""
    <h3>{title}</h3>
    <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
        <tr>
            <th>Industry</th>
            <th>Events</th>
            <th>% of Events</th>"""
    
    if include_tickets and 'tickets' in metrics_df.columns:
        html += """
            <th>Tickets Sold</th>
            <th>% of Tickets</th>"""
    
    html += """
            <th>Revenue</th>
            <th>% of Revenue</th>
        </tr>"""
    
    # Add rows for each industry (limit to top 10 for email)
    for _, row in metrics_df.head(10).iterrows():
        html += f"""
        <tr>
            <td>{row['Industry']}</td>
            <td>{row['events']:,}</td>
            <td>{row['events_pct']:.1f}%</td>"""
        
        if include_tickets and 'tickets' in metrics_df.columns:
            html += f"""
            <td>{row['tickets']:,}</td>
            <td>{row['tickets_pct']:.1f}%</td>"""
        
        html += f"""
            <td>£{row['revenue']:,.2f}</td>
            <td>{row['revenue_pct']:.1f}%</td>
        </tr>"""
    
    # Add totals row
    total_events = metrics_df['events'].sum()
    total_revenue = metrics_df['revenue'].sum()
    
    html += f"""
        <tr style="font-weight: bold;">
            <td>Total</td>
            <td>{total_events:,}</td>
            <td>100.0%</td>"""
    
    if include_tickets and 'tickets' in metrics_df.columns:
        total_tickets = metrics_df['tickets'].sum()
        html += f"""
            <td>{total_tickets:,}</td>
            <td>100.0%</td>"""
    
    html += f"""
            <td>£{total_revenue:,.2f}</td>
            <td>100.0%</td>
        </tr>
    </table>"""
    
    # Add note if more than 10 industries
    if len(metrics_df) > 10:
        html += f"<p><i>Showing top 10 of {len(metrics_df)} industries</i></p>"
    
    return html


def calculate_period_metrics(df, start_date, end_date):
    """
    Calculate metrics for a specific period.
    
    Args:
        df: DataFrame with TransactionDate column
        start_date: Period start date
        end_date: Period end date
        
    Returns:
        DataFrame filtered to period
    """
    # Ensure TransactionDate is datetime
    df = df.copy()
    if 'TransactionDate' in df.columns:
        df['TransactionDate'] = pd.to_datetime(df['TransactionDate'])
        
        # Filter to period
        mask = (df['TransactionDate'] >= start_date) & (df['TransactionDate'] <= end_date)
        return df[mask]
    
    return df