#!/usr/bin/env python3
"""
Monthly reporting script for TryBooking UK.
Runs on the first of each month to report on the previous month's performance.
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime

# Import shared modules
from modules.utils.config import TEST_MODE, UK_TZ
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached
from modules.utils.date_utils import get_last_month_dates, get_ytd_dates
from modules.utils.data_loaders import load_accounts_data, load_booking_data, filter_successful_transactions
from modules.utils.email_utils import send_html_email
from modules.utils.metrics_calculator import (
    calculate_yoy_change, calculate_percentage, calculate_transaction_metrics,
    calculate_fee_metrics, filter_date_range
)
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator


@timer_decorator
def calculate_metrics(accounts_df, booking_df, booking_all_df, dates):
    """Calculate all required metrics."""
    metrics = {}
    
    # 1. Total new accounts for last month
    last_month_accounts = filter_date_range(
        accounts_df, 'DateTimeCreated', 
        dates['last_month_start'], dates['last_month_end']
    )
    metrics['total_new_accounts'] = len(last_month_accounts)
    
    # 2. Accounts that created events last month
    accounts_with_events = last_month_accounts[last_month_accounts['FirstEventCreation'].notna()]
    metrics['accounts_with_events'] = len(accounts_with_events)
    metrics['accounts_with_events_pct'] = calculate_percentage(
        metrics['accounts_with_events'], metrics['total_new_accounts']
    )
    
    # 3. Accounts that have sold tickets (present in BookingData)
    # Get unique account IDs from last month's bookings
    last_month_bookings = filter_successful_transactions(
        filter_date_range(booking_df, 'TransactionDate', 
                         dates['last_month_start'], dates['last_month_end'])
    )
    
    # Find which new accounts sold tickets
    accounts_with_sales = last_month_accounts[
        last_month_accounts['Id'].isin(last_month_bookings['AccountId'].unique())
    ]
    metrics['accounts_with_sales'] = len(accounts_with_sales)
    metrics['accounts_with_sales_pct'] = calculate_percentage(
        metrics['accounts_with_sales'], metrics['total_new_accounts']
    )
    
    # 4 & 5. Transaction metrics
    transaction_metrics = calculate_transaction_metrics(last_month_bookings)
    metrics['avg_transaction_value'] = transaction_metrics['avg_amount']
    metrics['total_transactions'] = transaction_metrics['count']
    metrics['avg_tickets_per_transaction'] = transaction_metrics['avg_quantity']
    
    # 6. Total fees for last month
    metrics['total_fees_last_month'] = last_month_bookings['TotalFees'].sum()
    
    # Last year same month fees (from BookingDataAll)
    last_year_month_bookings = filter_successful_transactions(
        filter_date_range(booking_all_df, 'TransactionDate',
                         dates['last_year_month_start'], dates['last_year_month_end'])
    )
    metrics['total_fees_last_year_month'] = last_year_month_bookings['TotalFees'].sum()
    
    # Calculate YoY change
    metrics['fees_yoy_change'] = calculate_yoy_change(
        metrics['total_fees_last_month'], 
        metrics['total_fees_last_year_month']
    )
    
    # 7. YTD fees
    ytd_dates = get_ytd_dates()
    
    # Current YTD (from BookingDataAll for previous months + BookingData for current month)
    ytd_bookings_historical = filter_successful_transactions(
        filter_date_range(booking_all_df, 'TransactionDate',
                         ytd_dates['ytd_start'], ytd_dates['ytd_end'])
    )
    metrics['total_fees_ytd'] = ytd_bookings_historical['TotalFees'].sum()
    
    # Last year YTD
    ytd_bookings_ly = filter_successful_transactions(
        filter_date_range(booking_all_df, 'TransactionDate',
                         ytd_dates['ytd_start_ly'], ytd_dates['ytd_end_ly'])
    )
    metrics['total_fees_ytd_ly'] = ytd_bookings_ly['TotalFees'].sum()
    
    # YTD YoY change
    metrics['fees_ytd_yoy_change'] = calculate_yoy_change(
        metrics['total_fees_ytd'],
        metrics['total_fees_ytd_ly']
    )
    
    return metrics


def create_email_content(metrics, dates):
    """Create HTML email content."""
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>TryBooking UK - Monthly Report for {dates['month_name']}</h2>
        
        <h3>New Account Summary</h3>
        <ul>
            <li>Total new accounts: <strong>{metrics['total_new_accounts']:,}</strong></li>
            <li>Accounts that created events: <strong>{metrics['accounts_with_events']:,}</strong> ({metrics['accounts_with_events_pct']:.1f}%)</li>
            <li>Accounts that sold tickets: <strong>{metrics['accounts_with_sales']:,}</strong> ({metrics['accounts_with_sales_pct']:.1f}%)</li>
        </ul>
        
        <h3>Transaction Metrics</h3>
        <ul>
            <li>Total transactions: <strong>{metrics['total_transactions']:,}</strong></li>
            <li>Average transaction value: <strong>£{metrics['avg_transaction_value']:.2f}</strong></li>
            <li>Average tickets per transaction: <strong>{metrics['avg_tickets_per_transaction']:.1f}</strong></li>
        </ul>
        
        <h3>Revenue Performance</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Metric</th>
                <th>This Year</th>
                <th>Last Year</th>
                <th>Change</th>
            </tr>
            <tr>
                <td>{dates['month_name']} Fees</td>
                <td>£{metrics['total_fees_last_month']:,.2f}</td>
                <td>£{metrics['total_fees_last_year_month']:,.2f}</td>
                <td>{'+' if metrics['fees_yoy_change'] > 0 else ''}{metrics['fees_yoy_change']:.1f}%</td>
            </tr>
            <tr>
                <td>YTD Fees</td>
                <td>£{metrics['total_fees_ytd']:,.2f}</td>
                <td>£{metrics['total_fees_ytd_ly']:,.2f}</td>
                <td>{'+' if metrics['fees_ytd_yoy_change'] > 0 else ''}{metrics['fees_ytd_yoy_change']:.1f}%</td>
            </tr>
        </table>
        
        <br>
        <p style="color: #666; font-size: 12px;">This is an automated monthly report generated by TryBooking UK reporting system.</p>
    </body>
    </html>
    """
    
    return html_content



def main():
    """Main execution function."""
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'MAILGUN_SMTP_LOGIN', 'MAILGUN_SMTP_PASSWORD', 'MAILGUN_DOMAIN'
    ])
    
    print(f"\n=== Monthly Reporting Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    if TEST_MODE:
        print("TEST MODE: Email will be sent to alex@trybooking.co.uk only")
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Get date ranges
        dates = get_last_month_dates()
        print(f"\nReporting for: {dates['month_name']}")
        print(f"Comparison with: {dates['month_name_ly']}")
        
        # Load data
        print("\nLoading data from S3...")
        accounts_df = load_accounts_data(s3_client, dates['last_month_end'])
        print(f"Total accounts loaded: {len(accounts_df):,}")
        
        booking_df = load_booking_data(s3_client, dates['last_month_end'])
        print(f"Total booking records loaded: {len(booking_df):,}")
        
        # For BookingDataAll, we need the PREVIOUS month's file since this report runs on the 1st
        # but the current month's BookingDataAll isn't generated until the 2nd
        booking_all_df = load_booking_data(s3_client, dates['last_month_end'], data_type='BookingDataAll')
        print(f"Total historical booking records loaded: {len(booking_all_df):,}")
        
        # Calculate metrics
        print("\nCalculating metrics...")
        metrics = calculate_metrics(accounts_df, booking_df, booking_all_df, dates)
        
        # Print summary
        print(f"\nSummary for {dates['month_name']}:")
        print(f"- New accounts: {metrics['total_new_accounts']:,}")
        print(f"- Accounts with events: {metrics['accounts_with_events']:,} ({metrics['accounts_with_events_pct']:.1f}%)")
        print(f"- Accounts with sales: {metrics['accounts_with_sales']:,} ({metrics['accounts_with_sales_pct']:.1f}%)")
        print(f"- Total fees: £{metrics['total_fees_last_month']:,.2f} (YoY: {metrics['fees_yoy_change']:+.1f}%)")
        print(f"- YTD fees: £{metrics['total_fees_ytd']:,.2f} (YoY: {metrics['fees_ytd_yoy_change']:+.1f}%)")
        
        # Create and send email
        html_content = create_email_content(metrics, dates)
        send_html_email(
            to='alex@trybooking.co.uk',
            subject=f"Monthly Report - {dates['month_name']}",
            html_content=html_content
        )
        
        print(f"\n=== Monthly Reporting Completed in {time.time() - start_time:.1f} seconds ===")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()