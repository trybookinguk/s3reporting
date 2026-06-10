#!/usr/bin/env python3
"""
Industry analysis report for TryBooking UK.
Provides comprehensive analysis of account and revenue distribution by industry.
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta

# Import shared modules
from modules.utils.config import TEST_MODE, UK_TZ, CUTOFF_365, CUTOFF_730
from modules.utils.data_loader import get_s3_client, download_s3_file_cached
from modules.utils.email_utils import send_html_email_with_attachments
from modules.utils.industry_utils import (
    filter_valid_industries, prepare_booking_data_with_industry,
    calculate_industry_breakdown, format_industry_metrics_table
)
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator


@timer_decorator
def load_all_booking_data(s3_client, report_date):
    """Load and combine BookingDataAll and BookingData."""
    # Use the shared load_booking_data function which has fallback logic
    from modules.utils.data_loader import load_booking_data
    
    print("Loading BookingDataAll...")
    booking_all_df = load_booking_data(s3_client, report_date, data_type='BookingDataAll')
    print(f"  Loaded {len(booking_all_df):,} historical booking records")
    
    print("Loading current month BookingData...")
    booking_month_df = load_booking_data(s3_client, report_date, data_type='BookingData')
    print(f"  Loaded {len(booking_month_df):,} current month booking records")
    
    # Combine and deduplicate
    print("Combining and deduplicating booking data...")
    combined_df = pd.concat([booking_all_df, booking_month_df], ignore_index=True)
    initial_count = len(combined_df)
    
    if 'BookingTransactionId' in combined_df.columns:
        combined_df = combined_df.drop_duplicates(subset=['BookingTransactionId'])
        duplicates_removed = initial_count - len(combined_df)
        print(f"  Removed {duplicates_removed:,} duplicate transactions")
    
    # Filter successful transactions only
    if 'Status' in combined_df.columns:
        combined_df = combined_df[combined_df['Status'] == 'Successful']
        print(f"  Filtered to {len(combined_df):,} successful transactions")
    
    # TransactionDate and TotalFees are already handled by load_booking_data
    
    return combined_df


@timer_decorator
def calculate_all_time_metrics(accounts_df, booking_df):
    """Calculate all-time industry metrics."""
    print("\nCalculating all-time industry metrics...")
    
    # Get valid industries from accounts
    valid_accounts = filter_valid_industries(accounts_df)
    
    # Group accounts by industry
    industry_accounts = valid_accounts.groupby('Industry', observed=True).agg({
        'Id': 'count',
        'DateTimeCreated': ['min', 'max']
    }).round(2)
    
    industry_accounts.columns = ['total_accounts', 'first_account_date', 'latest_account_date']
    
    # Calculate revenue by industry from bookings
    if not booking_df.empty and 'Industry' in booking_df.columns:
        # Filter valid industries for revenue calculations
        valid_booking_df = filter_valid_industries(booking_df)
        
        if valid_booking_df.empty:
            print("  WARNING: No valid industry data found in bookings")
            return all_time_metrics
            
        industry_revenue = valid_booking_df.groupby('Industry', observed=True).agg({
            'PaymentReceived': 'sum',
            'TotalFees': 'sum',
            'EventId': 'nunique',
            'BookingTransactionId': 'count'
        }).round(2)
        
        industry_revenue.columns = ['total_revenue', 'total_fees', 'unique_events', 'total_transactions']
        
        # Merge with account counts
        all_time_metrics = industry_accounts.merge(
            industry_revenue,
            left_index=True,
            right_index=True,
            how='outer'
        ).fillna(0)
        
        # Calculate average revenue per account
        all_time_metrics['avg_revenue_per_account'] = (
            all_time_metrics['total_revenue'] / all_time_metrics['total_accounts']
        ).round(2)
        
        # Convert date objects to strings for better display
        all_time_metrics['first_account_date'] = pd.to_datetime(
            all_time_metrics['first_account_date']
        ).dt.strftime('%Y-%m-%d')
        all_time_metrics['latest_account_date'] = pd.to_datetime(
            all_time_metrics['latest_account_date']
        ).dt.strftime('%Y-%m-%d')
        
    else:
        all_time_metrics = industry_accounts
        # Add empty revenue columns if no booking data
        all_time_metrics['total_revenue'] = 0
        all_time_metrics['total_fees'] = 0
        all_time_metrics['unique_events'] = 0
        all_time_metrics['total_transactions'] = 0
        all_time_metrics['avg_revenue_per_account'] = 0
    
    # Sort by total revenue descending
    all_time_metrics = all_time_metrics.sort_values('total_revenue', ascending=False)
    
    return all_time_metrics


@timer_decorator
def calculate_period_metrics(booking_df, accounts_df, start_date, end_date, period_name):
    """Calculate metrics for a specific period."""
    # Handle both date and datetime objects
    start_str = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
    end_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
    print(f"\nCalculating {period_name} metrics ({start_str} to {end_str})...")
    
    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= start_date) &
        (booking_df['TransactionDate'] <= end_date)
    ]
    
    print(f"  Found {len(period_bookings):,} transactions in period")
    
    if period_bookings.empty:
        return pd.DataFrame()
    
    # Check if Industry column exists
    if 'Industry' not in period_bookings.columns:
        print(f"  WARNING: No Industry column in period bookings")
        return pd.DataFrame()
    
    # Filter valid industries
    period_bookings = filter_valid_industries(period_bookings)
    if period_bookings.empty:
        print(f"  WARNING: No valid industries found in period")
        return pd.DataFrame()
    
    # Get unique accounts with sales in period
    accounts_with_sales = set(period_bookings['AccountId'].unique())
    
    # Calculate metrics by industry
    industry_metrics = period_bookings.groupby('Industry', observed=True).agg({
        'AccountId': 'nunique',
        'EventId': 'nunique',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum',
        'TicketQuantity': 'sum',
        'BookingTransactionId': 'count'
    }).round(2)
    
    industry_metrics.columns = [
        f'{period_name}_active_accounts',
        f'{period_name}_events_with_sales',
        f'{period_name}_revenue',
        f'{period_name}_fees',
        f'{period_name}_tickets',
        f'{period_name}_transactions'
    ]
    
    return industry_metrics


@timer_decorator
def analyze_account_movements(accounts_df, booking_df, current_start, current_end, previous_start, previous_end):
    """Analyze account movements between periods."""
    print("\nAnalyzing account movements between periods...")
    
    # Check if we have Industry data
    if 'Industry' not in booking_df.columns or 'Industry' not in accounts_df.columns:
        print("  WARNING: No Industry data available for movement analysis")
        return pd.DataFrame()
    
    # Get accounts active in each period
    current_bookings = booking_df[
        (booking_df['TransactionDate'] >= current_start) &
        (booking_df['TransactionDate'] <= current_end)
    ]
    
    previous_bookings = booking_df[
        (booking_df['TransactionDate'] >= previous_start) &
        (booking_df['TransactionDate'] <= previous_end)
    ]
    
    # Create sets of active accounts by industry
    movements = []
    
    for industry in accounts_df['Industry'].dropna().unique():
        if industry == 'Ticket Purchaser':
            continue
            
        # Get all accounts in this industry
        industry_accounts = set(
            accounts_df[accounts_df['Industry'] == industry]['Id'].astype(str)
        )
        
        # Active in each period
        current_active = set(
            current_bookings[current_bookings['Industry'] == industry]['AccountId'].astype(str)
        )
        previous_active = set(
            previous_bookings[previous_bookings['Industry'] == industry]['AccountId'].astype(str)
        )
        
        # Account created dates
        industry_account_dates = accounts_df[
            accounts_df['Industry'] == industry
        ][['Id', 'DateTimeCreated']].copy()
        industry_account_dates['Id'] = industry_account_dates['Id'].astype(str)
        industry_account_dates['DateTimeCreated'] = pd.to_datetime(industry_account_dates['DateTimeCreated'])
        
        # New accounts (created in current period)
        new_accounts = set(
            industry_account_dates[
                industry_account_dates['DateTimeCreated'] >= current_start
            ]['Id']
        )
        
        # Calculate movements
        retained = current_active & previous_active
        new_active = current_active & new_accounts
        reactivated = current_active - previous_active - new_accounts
        churned = previous_active - current_active
        never_active = industry_accounts - current_active - previous_active
        
        movements.append({
            'Industry': industry,
            'Total_Accounts': len(industry_accounts),
            'Retained': len(retained),
            'New_Active': len(new_active),
            'Reactivated': len(reactivated),
            'Churned': len(churned),
            'Never_Active': len(never_active),
            'Current_Active_Total': len(current_active),
            'Previous_Active_Total': len(previous_active)
        })
    
    movements_df = pd.DataFrame(movements)
    
    # Calculate percentages
    movements_df['Retention_Rate'] = (
        movements_df['Retained'] / movements_df['Previous_Active_Total'] * 100
    ).round(1).fillna(0)
    
    movements_df['Churn_Rate'] = (
        movements_df['Churned'] / movements_df['Previous_Active_Total'] * 100
    ).round(1).fillna(0)
    
    movements_df['Activation_Rate'] = (
        movements_df['New_Active'] / movements_df['Total_Accounts'] * 100
    ).round(1)
    
    # Sort by current active accounts
    movements_df = movements_df.sort_values('Current_Active_Total', ascending=False)
    
    return movements_df


def create_summary_email(all_time_metrics, period_comparison, movements_df):
    """Create HTML email summary."""
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>Industry Performance Analysis - {datetime.now(UK_TZ).strftime('%B %Y')}</h2>
        
        <h3>Executive Summary</h3>
        <ul>
            <li>Total industries analysed: <strong>{len(all_time_metrics)}</strong></li>
            <li>Total accounts across all industries: <strong>{all_time_metrics['total_accounts'].sum():,}</strong></li>
            <li>Total all-time revenue: <strong>£{all_time_metrics['total_revenue'].sum():,.2f}</strong></li>
        </ul>
        
        <h3>Top 5 Industries by Current Period Revenue</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Industry</th>
                <th>Active Accounts</th>
                <th>Revenue</th>
                <th>YoY Change</th>
            </tr>"""
    
    # Get top 5 by current revenue
    top_5 = period_comparison.nlargest(5, 'current_revenue')
    
    for industry, row in top_5.iterrows():
        yoy_change = ((row['current_revenue'] / row['previous_revenue'] - 1) * 100) if row['previous_revenue'] > 0 else 0
        
        html_content += f"""
            <tr>
                <td>{industry}</td>
                <td>{int(row['current_active_accounts']):,}</td>
                <td>£{row['current_revenue']:,.2f}</td>
                <td>{'+' if yoy_change > 0 else ''}{yoy_change:.1f}%</td>
            </tr>"""
    
    html_content += """
        </table>
        
        <h3>Biggest Growth Industries (YoY Revenue)</h3>
        <ul>"""
    
    # Calculate YoY changes
    period_comparison['yoy_change'] = (
        (period_comparison['current_revenue'] / period_comparison['previous_revenue'] - 1) * 100
    ).fillna(0)
    
    # Get top growth industries (with minimum revenue threshold)
    growth_industries = period_comparison[
        period_comparison['current_revenue'] > 1000
    ].nlargest(3, 'yoy_change')
    
    for industry, row in growth_industries.iterrows():
        html_content += f"""
            <li>{industry}: <strong>+{row['yoy_change']:.1f}%</strong> 
                (£{row['previous_revenue']:,.2f} → £{row['current_revenue']:,.2f})</li>"""
    
    html_content += """
        </ul>
        
        <h3>Account Activity Summary</h3>
        <ul>"""
    
    # Overall activity metrics
    total_current_active = movements_df['Current_Active_Total'].sum()
    total_previous_active = movements_df['Previous_Active_Total'].sum()
    total_retained = movements_df['Retained'].sum()
    total_churned = movements_df['Churned'].sum()
    total_new_active = movements_df['New_Active'].sum()
    
    overall_retention = (total_retained / total_previous_active * 100) if total_previous_active > 0 else 0
    
    html_content += f"""
            <li>Currently active accounts: <strong>{total_current_active:,}</strong></li>
            <li>Overall retention rate: <strong>{overall_retention:.1f}%</strong></li>
            <li>New active accounts: <strong>{total_new_active:,}</strong></li>
            <li>Churned accounts: <strong>{total_churned:,}</strong></li>
        </ul>
        
        <p><em>Detailed analysis attached as CSV files.</em></p>
    </body>
    </html>"""
    
    return html_content


def main():
    """Main execution function."""
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    print(f"\n=== Industry Analysis Report Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    if TEST_MODE:
        print("TEST MODE: email sending is disabled for this report (no recipient).")
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Determine report date (same logic as monthly report)
        today = pd.Timestamp.now(UK_TZ).normalize()
        if today.day == 1:
            report_date = today - pd.Timedelta(days=1)
        else:
            report_date = today
        
        # Load accounts data using the standardized loader
        from modules.utils.data_loader import load_accounts_data
        
        print("\nLoading accounts data...")
        accounts_df = load_accounts_data(s3_client, report_date)
        print(f"  Loaded {len(accounts_df):,} accounts")
        
        # Load all booking data
        booking_df = load_all_booking_data(s3_client, report_date)
        
        # Check for Industry in booking data (should already exist per CLAUDE.md)
        print("\nPreparing booking data with industry information...")
        booking_with_industry = prepare_booking_data_with_industry(booking_df, accounts_df)
        
        # Verify we have Industry data
        if 'Industry' not in booking_with_industry.columns:
            print("  ERROR: Industry column not found in booking data!")
            print("  Cannot proceed with industry analysis.")
            sys.exit(1)
        
        # Show industry data summary
        valid_industry_count = booking_with_industry['Industry'].notna().sum()
        null_industry_count = booking_with_industry['Industry'].isna().sum()
        print(f"  Total transactions: {len(booking_with_industry):,}")
        print(f"  Transactions with industry: {valid_industry_count:,} ({valid_industry_count/len(booking_with_industry)*100:.1f}%)")
        print(f"  Transactions without industry: {null_industry_count:,} ({null_industry_count/len(booking_with_industry)*100:.1f}%)")
        
        # Calculate all-time metrics
        all_time_metrics = calculate_all_time_metrics(accounts_df, booking_with_industry)
        
        # Calculate period metrics
        # Define period boundaries (using same as tier calculations)
        # Convert date objects to pandas Timestamps with UTC timezone for comparison
        current_end = pd.Timestamp.now('UTC').normalize()
        current_start = pd.Timestamp(CUTOFF_365, tz='UTC')
        previous_start = pd.Timestamp(CUTOFF_730, tz='UTC')
        previous_end = pd.Timestamp(CUTOFF_365, tz='UTC')
        
        current_metrics = calculate_period_metrics(
            booking_with_industry, accounts_df, 
            current_start, current_end, 'current'
        )
        
        previous_metrics = calculate_period_metrics(
            booking_with_industry, accounts_df,
            previous_start, previous_end, 'previous'
        )
        
        # Combine period metrics
        period_comparison = current_metrics.merge(
            previous_metrics,
            left_index=True,
            right_index=True,
            how='outer'
        ).fillna(0)
        
        # Analyze account movements
        movements_df = analyze_account_movements(
            accounts_df, booking_with_industry,
            current_start, current_end,
            previous_start, previous_end
        )
        
        # Save detailed reports
        timestamp = datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')
        
        # 1. All-time industry overview
        all_time_filename = f"industry_analysis_all_time_{timestamp}.csv"
        all_time_metrics.to_csv(all_time_filename)
        print(f"\nSaved all-time analysis to: {all_time_filename}")
        
        # 2. Period comparison
        period_filename = f"industry_period_comparison_{timestamp}.csv"
        period_comparison.to_csv(period_filename)
        print(f"Saved period comparison to: {period_filename}")
        
        # 3. Account movements
        movements_filename = f"industry_account_movements_{timestamp}.csv"
        movements_df.to_csv(movements_filename, index=False)
        print(f"Saved account movements to: {movements_filename}")
        
        # Print summary
        print("\n=== Summary ===")
        print(f"Industries analysed: {len(all_time_metrics)}")
        print(f"Total accounts: {all_time_metrics['total_accounts'].sum():,}")
        print(f"Total all-time revenue: £{all_time_metrics['total_revenue'].sum():,.2f}")
        print(f"\nTop 5 industries by all-time revenue:")
        for industry in all_time_metrics.head(5).index:
            row = all_time_metrics.loc[industry]
            print(f"  - {industry}: £{row['total_revenue']:,.2f} "
                  f"({row['total_accounts']:,} accounts)")
        
        # Email sending removed — this report previously went only to
        # alex@trybooking.co.uk, who has left. Reinstate send_html_email_with_attachments
        # with a real recipient if this report is wanted again.
        print("\nReport generated; email sending disabled (no recipient).")
        
        elapsed_time = time.time() - start_time
        print(f"\n=== Industry Analysis Completed in {elapsed_time:.1f} seconds ===")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()