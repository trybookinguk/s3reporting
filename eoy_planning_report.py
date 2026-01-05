#!/usr/bin/env python3
"""
End of Year Planning Report for TryBooking UK.
Generates monthly metrics for account, event, and revenue target modelling.

Metrics calculated per month:
- Total new accounts
- Activated accounts (created events)
- Activated accounts (sold 10+ tickets ever)
- New accounts that created events
- New accounts that sold tickets (tier qualified)
- Total events with tickets sold
- Total ticket sales (PaymentReceived)
- Total fees
- Activation timing (avg days to first event, avg days to first sale)
- YoY comparison for all metrics

Usage:
    # Full year 2025 (Jan-Dec or YTD)
    python3 eoy_planning_report.py --year 2025

    # Rolling 12 months (previous 12 complete months)
    python3 eoy_planning_report.py --rolling

    # Custom date range
    python3 eoy_planning_report.py --start 2024-01 --end 2025-11

Output Structure:
    Reports are organised into folders by type:
    - planning/     - Target models, growth recommendations, BHAG tracking
    - seasonality/  - Industry and event type seasonality analysis
    - industry/     - Industry breakdowns and cross-tabs
    - cohorts/      - Expansion revenue and cohort curves
    - geography/    - Regional analysis
    - keywords/     - Event keyword analysis
    - (root)        - Main monthly report and summary
"""
import os
import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import calendar

# Import shared modules
from modules.utils.config import UK_TZ, MIN_TICKETS_FOR_ACTIVE, TIER_PERCENTILES
from modules.utils.data_loader import (
    get_loader, load_accounts, load_booking_data,
    filter_successful_transactions
)
from modules.utils.date_utils import get_latest_data_date
from modules.utils.performance import timer_decorator
from modules.tier_calculator import determine_tier_from_percentiles
from modules.uk_regional_segmentation import (
    extract_postcode_areas_vectorized,
    get_regions_vectorized,
    VALID_UK_POSTCODE_AREAS
)
from modules.event_keyword_analysis import generate_keyword_analysis_csvs


# Output folder structure
OUTPUT_FOLDERS = {
    'planning': 'planning',
    'seasonality': 'seasonality',
    'industry': 'industry',
    'ppc': 'ppc',
    'boxoffice': 'boxoffice',
    'cohorts': 'cohorts',
    'geography': 'geography',
    'keywords': 'keywords',
}


def get_output_path(base_name: str, folder: str, filename: str) -> str:
    """
    Get the full output path for a file, creating the folder if needed.

    Args:
        base_name: Base path/name from output_file (e.g., 'eoy_planning_report_20260102')
        folder: Folder category (e.g., 'planning', 'seasonality') or None for root
        filename: The filename suffix (e.g., '_2026_targets.csv')

    Returns:
        Full path to the output file
    """
    # Extract directory and base filename
    base_dir = os.path.dirname(base_name) or '.'
    base_file = os.path.basename(base_name)

    if folder and folder in OUTPUT_FOLDERS:
        # Create subfolder
        folder_path = os.path.join(base_dir, OUTPUT_FOLDERS[folder])
        os.makedirs(folder_path, exist_ok=True)
        return os.path.join(folder_path, f"{base_file}{filename}")
    else:
        # Root level
        return f"{base_name}{filename}"


def classify_sales_channel(payment_type) -> str:
    """
    Classify a payment type as Box Office or Online.

    Box Office = Card Present (any variant) or Cash
    Online = Everything else (including null for free tickets)

    Args:
        payment_type: The PaymentType value from booking data

    Returns:
        'Box Office' or 'Online'
    """
    if pd.isna(payment_type):
        return 'Online'
    payment_type_upper = str(payment_type).upper().strip()
    if 'CARD PRESENT' in payment_type_upper or payment_type_upper == 'CASH':
        return 'Box Office'
    return 'Online'


def add_sales_channel_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add Sales_Channel column to a DataFrame if PaymentType exists.

    Args:
        df: DataFrame with PaymentType column

    Returns:
        DataFrame with Sales_Channel column added
    """
    if 'PaymentType' in df.columns:
        df = df.copy()
        df['Sales_Channel'] = df['PaymentType'].apply(classify_sales_channel)
    return df


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate End of Year Planning Report for TryBooking UK'
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--year', type=int,
        help='Full calendar year to report on (e.g., 2025)'
    )
    group.add_argument(
        '--rolling', action='store_true',
        help='Rolling 12 months (previous 12 complete months)'
    )
    group.add_argument(
        '--start', type=str,
        help='Start month in YYYY-MM format (requires --end)'
    )

    parser.add_argument(
        '--end', type=str,
        help='End month in YYYY-MM format (requires --start)'
    )

    parser.add_argument(
        '--output', type=str, default='eoy_planning_report.csv',
        help='Output CSV filename (default: eoy_planning_report.csv)'
    )

    args = parser.parse_args()

    # Validate custom range
    if args.start and not args.end:
        parser.error("--start requires --end")
    if args.end and not args.start:
        parser.error("--end requires --start")

    return args


def get_month_range(args):
    """
    Get list of (year, month) tuples based on arguments.

    Returns:
        List of (year, month) tuples to process
    """
    today = datetime.now(UK_TZ)
    months = []

    if args.year:
        # Full calendar year
        year = args.year
        # If current year, only include months up to previous month
        max_month = 12 if year < today.year else today.month - 1
        for month in range(1, max_month + 1):
            months.append((year, month))

    elif args.rolling:
        # Rolling 12 months: previous 12 complete months
        # e.g., in Jan 2026, this gives Jan 2025 - Dec 2025
        end_date = datetime(today.year, today.month, 1) - relativedelta(months=1)  # Last complete month
        start_date = end_date - relativedelta(months=11)  # 12 months total

        current = start_date
        while current <= end_date:
            months.append((current.year, current.month))
            current += relativedelta(months=1)

    else:
        # Custom range
        start = datetime.strptime(args.start, '%Y-%m')
        end = datetime.strptime(args.end, '%Y-%m')

        current = start
        while current <= end:
            months.append((current.year, current.month))
            current += relativedelta(months=1)

    return months


def get_month_boundaries(year, month):
    """
    Get start and end datetime for a month.

    Returns:
        Tuple of (start_dt, end_dt) as timezone-aware datetimes
    """
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=UK_TZ)

    # Last day of month
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=UK_TZ)

    return start, end


@timer_decorator
def load_all_data():
    """
    Load all required data from S3.

    Returns:
        Tuple of (accounts_df, booking_df)
    """
    print("Loading data from S3...")

    # Load accounts
    accounts_df = load_accounts()
    print(f"  Accounts loaded: {len(accounts_df):,}")

    # Load all booking data (BookingDataAll + BookingData combined)
    booking_all_df = load_booking_data(data_type='BookingDataAll')
    booking_current_df = load_booking_data(data_type='BookingData')

    # Combine and deduplicate
    booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
    if 'BookingTransactionId' in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=['BookingTransactionId'])

    # Filter to successful transactions only
    booking_df = filter_successful_transactions(booking_df)
    print(f"  Booking records loaded: {len(booking_df):,}")

    # Ensure dates are timezone-aware
    if 'DateTimeCreated' in accounts_df.columns:
        accounts_df['DateTimeCreated'] = pd.to_datetime(
            accounts_df['DateTimeCreated'], errors='coerce', utc=True
        )
        if accounts_df['DateTimeCreated'].dt.tz is not None:
            accounts_df['DateTimeCreated'] = accounts_df['DateTimeCreated'].dt.tz_convert('Europe/London')

    if 'FirstEventCreation' in accounts_df.columns:
        accounts_df['FirstEventCreation'] = pd.to_datetime(
            accounts_df['FirstEventCreation'], errors='coerce', utc=True
        )

    if 'TransactionDate' in booking_df.columns:
        booking_df['TransactionDate'] = pd.to_datetime(
            booking_df['TransactionDate'], errors='coerce', utc=True
        )

    return accounts_df, booking_df


def calculate_period_totals(accounts_df, booking_df, months):
    """
    Calculate unique totals across the entire period (not sum of monthly).

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        months: List of (year, month) tuples for the period

    Returns:
        Dictionary with period-wide unique totals
    """
    if not months:
        return {}

    # Determine period date range
    first_year, first_month = months[0]
    last_year, last_month = months[-1]

    period_start = pd.Timestamp(year=first_year, month=first_month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(last_year, last_month)[1]
    period_end = pd.Timestamp(year=last_year, month=last_month, day=last_day,
                              hour=23, minute=59, second=59, tz='Europe/London')

    # Filter accounts created in period
    account_id_col = 'Account Id' if 'Account Id' in accounts_df.columns else 'AccountId'
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ]
    total_new_accounts = len(new_accounts)

    # Get new account IDs
    new_account_ids = set(new_accounts[account_id_col].astype(float).dropna().unique())

    # Activated (Created Events) - unique new accounts with events
    activated_with_events = 0
    if 'FirstEventCreation' in new_accounts.columns:
        activated_with_events = new_accounts['FirstEventCreation'].notna().sum()

    # Tier qualified - new accounts with 10+ tickets (all time)
    new_accounts_tier_qualified = 0
    if 'AccountId' in booking_df.columns:
        booking_df_copy = booking_df.copy()
        booking_df_copy['AccountId'] = pd.to_numeric(booking_df_copy['AccountId'], errors='coerce')
        new_account_bookings = booking_df_copy[booking_df_copy['AccountId'].isin(new_account_ids)]

        if len(new_account_bookings) > 0:
            account_tickets = new_account_bookings.groupby('AccountId')['TicketQuantity'].sum()
            new_accounts_tier_qualified = (account_tickets >= MIN_TICKETS_FOR_ACTIVE).sum()

    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) &
        (booking_df['TransactionDate'] <= period_end)
    ]

    # Unique events with sales in period
    events_with_sales = 0
    if 'EventId' in period_bookings.columns:
        events_with_sales = period_bookings['EventId'].nunique()

    # Unique accounts selling in period
    accounts_selling = 0
    if 'AccountId' in period_bookings.columns:
        accounts_selling = period_bookings['AccountId'].nunique()

    # Totals (these can be summed)
    total_tickets = period_bookings['TicketQuantity'].sum() if 'TicketQuantity' in period_bookings.columns else 0
    total_transactions = len(period_bookings)
    total_revenue = period_bookings['PaymentReceived'].sum() if 'PaymentReceived' in period_bookings.columns else 0
    total_fees = period_bookings['TotalFees'].sum() if 'TotalFees' in period_bookings.columns else 0

    return {
        'Total New Accounts': total_new_accounts,
        'Activated (Created Events)': activated_with_events,
        'New Accounts Tier Qualified': new_accounts_tier_qualified,
        'Accounts Selling (Unique)': accounts_selling,
        'Events With Sales (Unique)': events_with_sales,
        'Total Tickets Sold': int(total_tickets),
        'Total Transactions': total_transactions,
        'Total Ticket Revenue': round(total_revenue, 2),
        'Total Fees': round(total_fees, 2),
    }


def calculate_monthly_metrics(accounts_df, booking_df, year, month):
    """
    Calculate all metrics for a specific month.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        year: Year to calculate for
        month: Month to calculate for

    Returns:
        Dictionary of metrics for the month
    """
    month_start, month_end = get_month_boundaries(year, month)
    month_name = calendar.month_name[month]

    # Standardise account ID column
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'

    # 1. New accounts created this month
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= month_start) &
        (accounts_df['DateTimeCreated'] <= month_end)
    ].copy()
    total_new_accounts = len(new_accounts)

    # 2. Activated accounts - created events (FirstEventCreation not null)
    accounts_with_events = new_accounts[
        new_accounts['FirstEventCreation'].notna()
    ].copy()
    activated_with_events = len(accounts_with_events)

    # Calculate average days to first event and activation buckets
    avg_days_to_first_event = None
    activated_within_7_days = 0
    activated_within_30_days = 0
    activated_within_90_days = 0
    if activated_with_events > 0 and 'FirstEventCreation' in accounts_with_events.columns:
        # Ensure both columns are timezone-aware for comparison
        created = pd.to_datetime(accounts_with_events['DateTimeCreated'], utc=True)
        first_event = pd.to_datetime(accounts_with_events['FirstEventCreation'], utc=True)
        days_to_event = (first_event - created).dt.total_seconds() / 86400
        # Filter out negative values (data issues) and extreme outliers
        valid_days = days_to_event[(days_to_event >= 0) & (days_to_event <= 365)]
        if len(valid_days) > 0:
            avg_days_to_first_event = valid_days.mean()
            # Activation day buckets
            activated_within_7_days = (valid_days <= 7).sum()
            activated_within_30_days = (valid_days <= 30).sum()
            activated_within_90_days = (valid_days <= 90).sum()

    # 3. Get ticket counts for new accounts (any time, not just this month)
    new_account_ids = set(new_accounts[account_id_col].astype(float).dropna().unique())

    # Calculate total tickets sold per account (all time) and first sale date
    avg_days_to_first_sale = None
    if 'AccountId' in booking_df.columns:
        booking_df_copy = booking_df.copy()
        booking_df_copy['AccountId'] = pd.to_numeric(booking_df_copy['AccountId'], errors='coerce')

        # Filter bookings to new accounts only
        new_account_bookings = booking_df_copy[
            booking_df_copy['AccountId'].isin(new_account_ids)
        ]

        # Aggregate tickets per account
        if len(new_account_bookings) > 0:
            account_tickets = new_account_bookings.groupby('AccountId').agg({
                'TicketQuantity': 'sum',
                'PaymentReceived': 'sum',
                'TotalFees': 'sum',
                'TransactionDate': 'min'  # First sale date
            }).reset_index()
            account_tickets.columns = ['AccountId', 'TotalTickets', 'TotalRevenue', 'TotalFees', 'FirstSaleDate']

            # Calculate avg days to first sale
            # Merge with new_accounts to get DateTimeCreated
            account_tickets_merged = account_tickets.merge(
                new_accounts[[account_id_col, 'DateTimeCreated']].rename(columns={account_id_col: 'AccountId'}),
                on='AccountId',
                how='left'
            )
            if len(account_tickets_merged) > 0:
                created = pd.to_datetime(account_tickets_merged['DateTimeCreated'], utc=True)
                first_sale = pd.to_datetime(account_tickets_merged['FirstSaleDate'], utc=True)
                days_to_sale = (first_sale - created).dt.total_seconds() / 86400
                valid_days = days_to_sale[(days_to_sale >= 0) & (days_to_sale <= 365)]
                if len(valid_days) > 0:
                    avg_days_to_first_sale = valid_days.mean()
        else:
            account_tickets = pd.DataFrame(columns=['AccountId', 'TotalTickets', 'TotalRevenue', 'TotalFees', 'FirstSaleDate'])
    else:
        account_tickets = pd.DataFrame(columns=['AccountId', 'TotalTickets', 'TotalRevenue', 'TotalFees', 'FirstSaleDate'])

    # 4. New accounts tier qualified (sold 10+ tickets ever)
    accounts_with_10plus_tickets = account_tickets[
        account_tickets['TotalTickets'] >= MIN_TICKETS_FOR_ACTIVE
    ]
    new_accounts_tier_qualified = len(accounts_with_10plus_tickets)

    # 5. Average days from account creation to event date
    # This shows how far in advance new accounts set up events before they happen
    avg_days_creation_to_event = None
    if len(new_account_bookings) > 0 and 'EventDate' in new_account_bookings.columns:
        # Rename the account creation date column to avoid conflict with booking's DateTimeCreated
        account_creation_df = new_accounts[[account_id_col, 'DateTimeCreated']].rename(
            columns={account_id_col: 'AccountId', 'DateTimeCreated': 'AccountCreatedDate'}
        )
        bookings_with_dates = new_account_bookings.merge(
            account_creation_df,
            on='AccountId',
            how='left'
        )
        if len(bookings_with_dates) > 0 and 'AccountCreatedDate' in bookings_with_dates.columns:
            created = pd.to_datetime(bookings_with_dates['AccountCreatedDate'], utc=True)
            event_date = pd.to_datetime(bookings_with_dates['EventDate'], utc=True)
            days_to_event = (event_date - created).dt.total_seconds() / 86400
            # Filter valid values (event after account creation, within 2 years)
            valid_days = days_to_event[(days_to_event >= 0) & (days_to_event <= 730)]
            if len(valid_days) > 0:
                avg_days_creation_to_event = valid_days.mean()

    # 6. Total events with tickets sold this month
    month_bookings = booking_df[
        (booking_df['TransactionDate'] >= month_start) &
        (booking_df['TransactionDate'] <= month_end)
    ]

    if 'EventId' in month_bookings.columns:
        events_with_sales = month_bookings['EventId'].nunique()
    else:
        events_with_sales = 0

    # 7. Total ticket sales (PaymentReceived) this month
    total_ticket_sales = month_bookings['PaymentReceived'].sum() if 'PaymentReceived' in month_bookings.columns else 0

    # 8. Total fees this month
    total_fees = month_bookings['TotalFees'].sum() if 'TotalFees' in month_bookings.columns else 0

    # Additional helpful metrics
    total_tickets_sold = month_bookings['TicketQuantity'].sum() if 'TicketQuantity' in month_bookings.columns else 0
    total_transactions = len(month_bookings)

    # 9. Accounts selling tickets in the month (unique accounts with sales this month)
    if 'AccountId' in month_bookings.columns:
        accounts_selling_in_month = month_bookings['AccountId'].nunique()
    else:
        accounts_selling_in_month = 0

    # 10. Average price per ticket
    avg_price_per_ticket = (total_ticket_sales / total_tickets_sold) if total_tickets_sold > 0 else 0

    # 11. Average transaction value
    avg_transaction_value = (total_ticket_sales / total_transactions) if total_transactions > 0 else 0

    # 12. Average tickets per booking
    avg_tickets_per_booking = (total_tickets_sold / total_transactions) if total_transactions > 0 else 0

    # 13. Average account ticket sales (ticket revenue per account selling in month)
    avg_account_ticket_sales = (total_ticket_sales / accounts_selling_in_month) if accounts_selling_in_month > 0 else 0

    # 14. Average event ticket sales (ticket revenue per event with sales in month)
    avg_event_ticket_sales = (total_ticket_sales / events_with_sales) if events_with_sales > 0 else 0

    # 15. Average account fees (fees per account selling in month)
    avg_account_fees = (total_fees / accounts_selling_in_month) if accounts_selling_in_month > 0 else 0

    # 16. Average event fees (fees per event with sales in month)
    avg_event_fees = (total_fees / events_with_sales) if events_with_sales > 0 else 0

    # Calculate percentages
    pct_with_events = (activated_with_events / total_new_accounts * 100) if total_new_accounts > 0 else 0
    pct_tier_qualified = (new_accounts_tier_qualified / total_new_accounts * 100) if total_new_accounts > 0 else 0

    # Revenue per new account (cohort quality indicator)
    revenue_per_new_account = (total_ticket_sales / total_new_accounts) if total_new_accounts > 0 else 0

    # Repeat event rate - accounts with 2+ events
    repeat_event_accounts = 0
    avg_events_per_active_account = 0
    if len(new_account_bookings) > 0 and 'EventId' in new_account_bookings.columns:
        events_per_account = new_account_bookings.groupby('AccountId')['EventId'].nunique()
        repeat_event_accounts = (events_per_account >= 2).sum()
        avg_events_per_active_account = events_per_account.mean() if len(events_per_account) > 0 else 0

    pct_repeat_events = (repeat_event_accounts / activated_with_events * 100) if activated_with_events > 0 else 0

    # Free vs Paid event split (based on PaymentReceived - free events have 0 revenue)
    free_events = 0
    paid_events = 0
    if 'EventId' in month_bookings.columns and 'PaymentReceived' in month_bookings.columns:
        event_revenue = month_bookings.groupby('EventId')['PaymentReceived'].sum()
        free_events = (event_revenue == 0).sum()
        paid_events = (event_revenue > 0).sum()

    pct_free_events = (free_events / events_with_sales * 100) if events_with_sales > 0 else 0

    return {
        'Year': year,
        'Month': month,
        'Month Name': month_name,
        'Total New Accounts': total_new_accounts,
        'Activated (Created Events)': activated_with_events,
        'New Accounts Tier Qualified': new_accounts_tier_qualified,
        'Accounts Selling In Month': accounts_selling_in_month,
        'Events With Sales': events_with_sales,
        'Total Tickets Sold': int(total_tickets_sold),
        'Total Ticket Revenue': round(total_ticket_sales, 2),
        'Total Fees': round(total_fees, 2),
        'Total Transactions': total_transactions,
        'Avg Price Per Ticket': round(avg_price_per_ticket, 2),
        'Avg Transaction Value': round(avg_transaction_value, 2),
        'Avg Tickets Per Booking': round(avg_tickets_per_booking, 2),
        'Avg Account Ticket Sales': round(avg_account_ticket_sales, 2),
        'Avg Event Ticket Sales': round(avg_event_ticket_sales, 2),
        'Avg Account Fees': round(avg_account_fees, 2),
        'Avg Event Fees': round(avg_event_fees, 2),
        '% With Events': round(pct_with_events, 1),
        '% Tier Qualified': round(pct_tier_qualified, 1),
        'Avg Days to First Event': round(avg_days_to_first_event, 1) if avg_days_to_first_event is not None else None,
        'Avg Days to First Sale': round(avg_days_to_first_sale, 1) if avg_days_to_first_sale is not None else None,
        'Avg Days Creation to Event': round(avg_days_creation_to_event, 1) if avg_days_creation_to_event is not None else None,
        # Activation day buckets
        'Activated Within 7 Days': activated_within_7_days,
        'Activated Within 30 Days': activated_within_30_days,
        'Activated Within 90 Days': activated_within_90_days,
        # Cohort quality metrics
        'Revenue Per New Account': round(revenue_per_new_account, 2),
        'Repeat Event Accounts': repeat_event_accounts,
        '% Repeat Events': round(pct_repeat_events, 1),
        'Avg Events Per Active Account': round(avg_events_per_active_account, 2),
        # Free vs Paid split
        'Free Events': free_events,
        'Paid Events': paid_events,
        '% Free Events': round(pct_free_events, 1),
    }


def calculate_yoy_metrics(current_metrics, previous_year_metrics):
    """
    Calculate year-over-year changes for metrics.

    Args:
        current_metrics: Dictionary of current period metrics
        previous_year_metrics: Dictionary of same month previous year metrics (or None)

    Returns:
        Dictionary with YoY change values added
    """
    if previous_year_metrics is None:
        # No previous year data available
        current_metrics['YoY New Accounts'] = None
        current_metrics['YoY Created Events'] = None
        current_metrics['YoY Tier Qualified'] = None
        current_metrics['YoY Tier 4+'] = None
        current_metrics['YoY Events With Sales'] = None
        current_metrics['YoY Ticket Revenue'] = None
        current_metrics['YoY Fees'] = None
        return current_metrics

    # Calculate YoY percentage changes
    def calc_yoy(current, previous):
        if previous == 0 or previous is None:
            return None
        return round(((current - previous) / previous) * 100, 1)

    current_metrics['YoY New Accounts'] = calc_yoy(
        current_metrics['Total New Accounts'],
        previous_year_metrics['Total New Accounts']
    )
    current_metrics['YoY Created Events'] = calc_yoy(
        current_metrics['Activated (Created Events)'],
        previous_year_metrics['Activated (Created Events)']
    )
    current_metrics['YoY Tier Qualified'] = calc_yoy(
        current_metrics['New Accounts Tier Qualified'],
        previous_year_metrics['New Accounts Tier Qualified']
    )
    current_metrics['YoY Tier 4+'] = calc_yoy(
        current_metrics.get('New Accounts Tier 4+', 0),
        previous_year_metrics.get('New Accounts Tier 4+', 0)
    )
    current_metrics['YoY Events With Sales'] = calc_yoy(
        current_metrics['Events With Sales'],
        previous_year_metrics['Events With Sales']
    )
    current_metrics['YoY Ticket Revenue'] = calc_yoy(
        current_metrics['Total Ticket Revenue'],
        previous_year_metrics['Total Ticket Revenue']
    )
    current_metrics['YoY Fees'] = calc_yoy(
        current_metrics['Total Fees'],
        previous_year_metrics['Total Fees']
    )

    # Also store previous year values for reference
    current_metrics['PY New Accounts'] = previous_year_metrics['Total New Accounts']
    current_metrics['PY Created Events'] = previous_year_metrics['Activated (Created Events)']
    current_metrics['PY Tier Qualified'] = previous_year_metrics['New Accounts Tier Qualified']
    current_metrics['PY Tier 4+'] = previous_year_metrics.get('New Accounts Tier 4+', 0)
    current_metrics['PY Events With Sales'] = previous_year_metrics['Events With Sales']
    current_metrics['PY Ticket Revenue'] = previous_year_metrics['Total Ticket Revenue']
    current_metrics['PY Fees'] = previous_year_metrics['Total Fees']

    return current_metrics


def print_summary(results_df, period_totals=None, py_period_totals=None):
    """Print a formatted summary of results.

    Args:
        results_df: DataFrame with monthly metrics
        period_totals: Dictionary with unique period-wide totals (optional)
        py_period_totals: Dictionary with previous year unique period-wide totals (optional)
    """
    print("\n" + "=" * 100)
    print("END OF YEAR PLANNING REPORT")
    print("=" * 100)

    # Print column headers
    print(f"\n{'Month':<12} {'New':>8} {'Created':>8} {'Tier':>8} {'Events':>8} {'Revenue':>12} {'Fees':>12}")
    print(f"{'':12} {'Accts':>8} {'Events':>8} {'Qualified':>8} {'w/Sales':>8} {'(Tickets)':>12} {'':>12}")
    print("-" * 80)

    # Print each month
    for _, row in results_df.iterrows():
        month_label = f"{row['Month Name'][:3]} {row['Year']}"
        print(f"{month_label:<12} {row['Total New Accounts']:>8,} "
              f"{row['Activated (Created Events)']:>8,} "
              f"{row['New Accounts Tier Qualified']:>8,} "
              f"{row['Events With Sales']:>8,} "
              f"£{row['Total Ticket Revenue']:>10,.2f} "
              f"£{row['Total Fees']:>10,.2f}")

    # Print totals - use period_totals for unique counts if available
    print("-" * 80)
    if period_totals:
        print(f"{'TOTAL':<12} {period_totals['Total New Accounts']:>8,} "
              f"{period_totals['Activated (Created Events)']:>8,} "
              f"{period_totals['New Accounts Tier Qualified']:>8,} "
              f"{period_totals['Events With Sales (Unique)']:>8,} "
              f"£{period_totals['Total Ticket Revenue']:>10,.2f} "
              f"£{period_totals['Total Fees']:>10,.2f}")
    else:
        print(f"{'TOTAL':<12} {results_df['Total New Accounts'].sum():>8,} "
              f"{results_df['Activated (Created Events)'].sum():>8,} "
              f"{results_df['New Accounts Tier Qualified'].sum():>8,} "
              f"{results_df['Events With Sales'].sum():>8,} "
              f"£{results_df['Total Ticket Revenue'].sum():>10,.2f} "
              f"£{results_df['Total Fees'].sum():>10,.2f}")

    # Print averages
    print(f"{'AVERAGE':<12} {results_df['Total New Accounts'].mean():>8,.0f} "
          f"{results_df['Activated (Created Events)'].mean():>8,.0f} "
          f"{results_df['New Accounts Tier Qualified'].mean():>8,.0f} "
          f"{results_df['Events With Sales'].mean():>8,.0f} "
          f"£{results_df['Total Ticket Revenue'].mean():>10,.2f} "
          f"£{results_df['Total Fees'].mean():>10,.2f}")

    # Print YoY comparison if available
    if 'YoY New Accounts' in results_df.columns and results_df['YoY New Accounts'].notna().any():
        print("\n" + "=" * 100)
        print("YEAR-OVER-YEAR COMPARISON")
        print("=" * 100)
        print(f"\n{'Month':<12} {'New Accts':>12} {'YoY %':>8} {'Events':>12} {'YoY %':>8} "
              f"{'Revenue':>14} {'YoY %':>8} {'Fees':>14} {'YoY %':>8}")
        print("-" * 110)

        for _, row in results_df.iterrows():
            month_label = f"{row['Month Name'][:3]} {row['Year']}"

            # Format YoY values
            def fmt_yoy(val):
                if val is None or pd.isna(val):
                    return "N/A"
                return f"{val:+.1f}%"

            def fmt_py(val):
                if val is None or pd.isna(val):
                    return "N/A"
                return f"{int(val):,}"

            def fmt_py_money(val):
                if val is None or pd.isna(val):
                    return "N/A"
                return f"£{val:,.0f}"

            py_accts = fmt_py(row.get('PY New Accounts'))
            py_events = fmt_py(row.get('PY Events With Sales'))
            py_rev = fmt_py_money(row.get('PY Ticket Revenue'))
            py_fees = fmt_py_money(row.get('PY Fees'))

            print(f"{month_label:<12} "
                  f"{row['Total New Accounts']:>6,} ({py_accts:>5}) "
                  f"{fmt_yoy(row.get('YoY New Accounts')):>8} "
                  f"{row['Events With Sales']:>6,} ({py_events:>5}) "
                  f"{fmt_yoy(row.get('YoY Events With Sales')):>8} "
                  f"£{row['Total Ticket Revenue']:>8,.0f} ({py_rev:>6}) "
                  f"{fmt_yoy(row.get('YoY Ticket Revenue')):>8} "
                  f"£{row['Total Fees']:>8,.0f} ({py_fees:>6}) "
                  f"{fmt_yoy(row.get('YoY Fees')):>8}")

        # Add summary row with period totals and averages
        print("-" * 110)

        # Use unique period totals if available, otherwise fall back to summing monthly values
        if period_totals:
            total_new_accts = period_totals['Total New Accounts']
            total_events = period_totals['Events With Sales (Unique)']
            total_revenue = period_totals['Total Ticket Revenue']
            total_fees = period_totals['Total Fees']
        else:
            total_new_accts = results_df['Total New Accounts'].sum()
            total_events = results_df['Events With Sales'].sum()
            total_revenue = results_df['Total Ticket Revenue'].sum()
            total_fees = results_df['Total Fees'].sum()

        if py_period_totals:
            total_py_accts = py_period_totals['Total New Accounts']
            total_py_events = py_period_totals['Events With Sales (Unique)']
            total_py_revenue = py_period_totals['Total Ticket Revenue']
            total_py_fees = py_period_totals['Total Fees']
        else:
            total_py_accts = results_df['PY New Accounts'].dropna().sum()
            total_py_events = results_df['PY Events With Sales'].dropna().sum()
            total_py_revenue = results_df['PY Ticket Revenue'].dropna().sum()
            total_py_fees = results_df['PY Fees'].dropna().sum()

        # Calculate overall YoY percentages
        yoy_accts = ((total_new_accts - total_py_accts) / total_py_accts * 100) if total_py_accts > 0 else None
        yoy_events = ((total_events - total_py_events) / total_py_events * 100) if total_py_events > 0 else None
        yoy_revenue = ((total_revenue - total_py_revenue) / total_py_revenue * 100) if total_py_revenue > 0 else None
        yoy_fees = ((total_fees - total_py_fees) / total_py_fees * 100) if total_py_fees > 0 else None

        print(f"{'TOTAL':<12} "
              f"{int(total_new_accts):>6,} ({int(total_py_accts):>5,}) "
              f"{fmt_yoy(yoy_accts):>8} "
              f"{int(total_events):>6,} ({int(total_py_events):>5,}) "
              f"{fmt_yoy(yoy_events):>8} "
              f"£{total_revenue:>8,.0f} (£{total_py_revenue:>5,.0f}) "
              f"{fmt_yoy(yoy_revenue):>8} "
              f"£{total_fees:>8,.0f} (£{total_py_fees:>5,.0f}) "
              f"{fmt_yoy(yoy_fees):>8}")

        # Calculate monthly averages
        num_months = len(results_df)
        avg_new_accts = total_new_accts / num_months
        avg_py_accts = total_py_accts / num_months if total_py_accts > 0 else 0
        avg_events = total_events / num_months
        avg_py_events = total_py_events / num_months if total_py_events > 0 else 0
        avg_revenue = total_revenue / num_months
        avg_py_revenue = total_py_revenue / num_months if total_py_revenue > 0 else 0
        avg_fees = total_fees / num_months
        avg_py_fees = total_py_fees / num_months if total_py_fees > 0 else 0

        # Average YoY is same as total YoY for the period
        print(f"{'AVERAGE':<12} "
              f"{avg_new_accts:>6,.0f} ({avg_py_accts:>5,.0f}) "
              f"{fmt_yoy(yoy_accts):>8} "
              f"{avg_events:>6,.0f} ({avg_py_events:>5,.0f}) "
              f"{fmt_yoy(yoy_events):>8} "
              f"£{avg_revenue:>8,.0f} (£{avg_py_revenue:>5,.0f}) "
              f"{fmt_yoy(yoy_revenue):>8} "
              f"£{avg_fees:>8,.0f} (£{avg_py_fees:>5,.0f}) "
              f"{fmt_yoy(yoy_fees):>8}")

    # Print activation timing metrics
    print("\n" + "=" * 100)
    print("ACTIVATION TIMING (Average days from account creation)")
    print("=" * 100)
    print(f"\n{'Month':<12} {'Days to First Event':>20} {'Days to First Sale':>20}")
    print("-" * 55)

    for _, row in results_df.iterrows():
        month_label = f"{row['Month Name'][:3]} {row['Year']}"
        days_event = row.get('Avg Days to First Event')
        days_sale = row.get('Avg Days to First Sale')

        days_event_str = f"{days_event:.1f}" if days_event is not None and not pd.isna(days_event) else "N/A"
        days_sale_str = f"{days_sale:.1f}" if days_sale is not None and not pd.isna(days_sale) else "N/A"

        print(f"{month_label:<12} {days_event_str:>20} {days_sale_str:>20}")

    # Print averages for timing
    avg_days_event = results_df['Avg Days to First Event'].dropna().mean()
    avg_days_sale = results_df['Avg Days to First Sale'].dropna().mean()
    print("-" * 55)
    print(f"{'AVERAGE':<12} {avg_days_event:>20.1f} {avg_days_sale:>20.1f}")

    # Print conversion rates
    print("\n" + "=" * 100)
    print("CONVERSION RATES (Averages)")
    print("=" * 100)
    print(f"  New Accounts → Created Events:     {results_df['% With Events'].mean():.1f}%")
    print(f"  New Accounts → Tier Qualified:     {results_df['% Tier Qualified'].mean():.1f}%")

    # Transaction & Pricing metrics
    print("\n" + "=" * 100)
    print("TRANSACTION & PRICING METRICS")
    print("=" * 100)
    print(f"\n{'Month':<12} {'Accts':>8} {'Avg Tix':>10} {'Avg Trans':>12} {'Avg Tix/':>10} {'Avg Acct':>14} {'Avg Event':>14}")
    print(f"{'':12} {'Selling':>8} {'Price':>10} {'Value':>12} {'Booking':>10} {'Tix Sales':>14} {'Tix Sales':>14}")
    print("-" * 85)

    for _, row in results_df.iterrows():
        month_label = f"{row['Month Name'][:3]} {row['Year']}"
        print(f"{month_label:<12} "
              f"{row['Accounts Selling In Month']:>8,} "
              f"£{row['Avg Price Per Ticket']:>8,.2f} "
              f"£{row['Avg Transaction Value']:>10,.2f} "
              f"{row['Avg Tickets Per Booking']:>10,.2f} "
              f"£{row['Avg Account Ticket Sales']:>12,.2f} "
              f"£{row['Avg Event Ticket Sales']:>12,.2f}")

    # Print totals and averages - use unique counts if available
    print("-" * 85)
    unique_accounts_selling = period_totals['Accounts Selling (Unique)'] if period_totals else results_df['Accounts Selling In Month'].sum()
    print(f"{'TOTAL':<12} "
          f"{unique_accounts_selling:>8,} "
          f"{'':>10} "
          f"{'':>12} "
          f"{'':>10} "
          f"{'':>14} "
          f"{'':>14}")
    print(f"{'AVERAGE':<12} "
          f"{results_df['Accounts Selling In Month'].mean():>8,.0f} "
          f"£{results_df['Avg Price Per Ticket'].mean():>8,.2f} "
          f"£{results_df['Avg Transaction Value'].mean():>10,.2f} "
          f"{results_df['Avg Tickets Per Booking'].mean():>10,.2f} "
          f"£{results_df['Avg Account Ticket Sales'].mean():>12,.2f} "
          f"£{results_df['Avg Event Ticket Sales'].mean():>12,.2f}")

    # Additional insights - use unique period totals if available
    print("\n" + "=" * 100)
    print("PERIOD TOTALS & INSIGHTS")
    print("=" * 100)

    if period_totals:
        total_tickets = period_totals['Total Tickets Sold']
        total_transactions = period_totals['Total Transactions']
        total_revenue = period_totals['Total Ticket Revenue']
        total_fees = period_totals['Total Fees']
        total_accounts_selling = period_totals['Accounts Selling (Unique)']
        total_events = period_totals['Events With Sales (Unique)']
    else:
        total_tickets = results_df['Total Tickets Sold'].sum()
        total_transactions = results_df['Total Transactions'].sum()
        total_revenue = results_df['Total Ticket Revenue'].sum()
        total_fees = results_df['Total Fees'].sum()
        total_accounts_selling = results_df['Accounts Selling In Month'].sum()
        total_events = results_df['Events With Sales'].sum()

    print(f"  Total Tickets Sold:                {total_tickets:,}")
    print(f"  Total Transactions:                {total_transactions:,}")
    print(f"  Total Accounts Selling:            {total_accounts_selling:,} (unique)")
    print(f"  Total Events With Sales:           {total_events:,} (unique)")
    print(f"  Total Ticket Revenue:              £{total_revenue:,.2f}")
    print(f"  Total Fees:                        £{total_fees:,.2f}")
    print("")
    print(f"  Avg Price Per Ticket (Period):     £{total_revenue / total_tickets:.2f}" if total_tickets > 0 else "  Avg Price Per Ticket (Period):     N/A")
    print(f"  Avg Transaction Value (Period):    £{total_revenue / total_transactions:.2f}" if total_transactions > 0 else "  Avg Transaction Value (Period):    N/A")
    print(f"  Avg Tickets Per Booking (Period):  {total_tickets / total_transactions:.2f}" if total_transactions > 0 else "  Avg Tickets Per Booking (Period):  N/A")
    print(f"  Avg Account Ticket Sales (Period): £{total_revenue / total_accounts_selling:.2f}" if total_accounts_selling > 0 else "  Avg Account Ticket Sales (Period): N/A")
    print(f"  Avg Event Ticket Sales (Period):   £{total_revenue / total_events:.2f}" if total_events > 0 else "  Avg Event Ticket Sales (Period):   N/A")
    print(f"  Avg Account Fees (Period):         £{total_fees / total_accounts_selling:.2f}" if total_accounts_selling > 0 else "  Avg Account Fees (Period):         N/A")
    print(f"  Avg Event Fees (Period):           £{total_fees / total_events:.2f}" if total_events > 0 else "  Avg Event Fees (Period):           N/A")
    print(f"  Fee Rate (Fees/Revenue):           {total_fees / total_revenue * 100:.2f}%" if total_revenue > 0 else "  Fee Rate (Fees/Revenue):           N/A")


def generate_summary_csv(results_df, output_file, period_totals=None, py_period_totals=None):
    """
    Generate a summary CSV with period totals and monthly breakdown.

    Args:
        results_df: DataFrame with monthly metrics
        output_file: Base output filename (will create _summary version)
        period_totals: Dictionary with unique period-wide totals (optional)
        py_period_totals: Dictionary with previous year unique period-wide totals (optional)
    """
    # Use unique period totals if available, otherwise sum monthly values
    if period_totals:
        total_tickets = period_totals['Total Tickets Sold']
        total_transactions = period_totals['Total Transactions']
        total_revenue = period_totals['Total Ticket Revenue']
        total_fees = period_totals['Total Fees']
        total_accounts_selling = period_totals['Accounts Selling (Unique)']
        total_events = period_totals['Events With Sales (Unique)']
        total_new_accounts = period_totals['Total New Accounts']
        total_activated = period_totals['Activated (Created Events)']
        total_tier_qualified = period_totals['New Accounts Tier Qualified']
    else:
        total_tickets = results_df['Total Tickets Sold'].sum()
        total_transactions = results_df['Total Transactions'].sum()
        total_revenue = results_df['Total Ticket Revenue'].sum()
        total_fees = results_df['Total Fees'].sum()
        total_accounts_selling = results_df['Accounts Selling In Month'].sum()
        total_events = results_df['Events With Sales'].sum()
        total_new_accounts = results_df['Total New Accounts'].sum()
        total_activated = results_df['Activated (Created Events)'].sum()
        total_tier_qualified = results_df['New Accounts Tier Qualified'].sum()

    # YoY totals if available
    has_yoy = 'PY New Accounts' in results_df.columns and results_df['PY New Accounts'].notna().any()

    if has_yoy:
        if py_period_totals:
            total_py_accts = py_period_totals['Total New Accounts']
            total_py_events = py_period_totals['Events With Sales (Unique)']
            total_py_revenue = py_period_totals['Total Ticket Revenue']
            total_py_fees = py_period_totals['Total Fees']
        else:
            total_py_accts = results_df['PY New Accounts'].dropna().sum()
            total_py_events = results_df['PY Events With Sales'].dropna().sum()
            total_py_revenue = results_df['PY Ticket Revenue'].dropna().sum()
            total_py_fees = results_df['PY Fees'].dropna().sum()

        yoy_accts = ((total_new_accounts - total_py_accts) / total_py_accts * 100) if total_py_accts > 0 else None
        yoy_events = ((total_events - total_py_events) / total_py_events * 100) if total_py_events > 0 else None
        yoy_revenue = ((total_revenue - total_py_revenue) / total_py_revenue * 100) if total_py_revenue > 0 else None
        yoy_fees = ((total_fees - total_py_fees) / total_py_fees * 100) if total_py_fees > 0 else None

    # Build summary rows
    summary_rows = []

    # Period summary row - use unique totals (same column names as monthly for consistency)
    period_row = {
        'Period': 'TOTAL',
        'Total New Accounts': total_new_accounts,
        'Activated (Created Events)': total_activated,
        'New Accounts Tier Qualified': total_tier_qualified,
        'Accounts Selling In Month': total_accounts_selling,  # Actually unique for TOTAL
        'Events With Sales': total_events,  # Actually unique for TOTAL
        'Total Tickets Sold': total_tickets,
        'Total Transactions': total_transactions,
        'Total Ticket Revenue': round(total_revenue, 2),
        'Total Fees': round(total_fees, 2),
        'Avg Price Per Ticket': round(total_revenue / total_tickets, 2) if total_tickets > 0 else 0,
        'Avg Transaction Value': round(total_revenue / total_transactions, 2) if total_transactions > 0 else 0,
        'Avg Tickets Per Booking': round(total_tickets / total_transactions, 2) if total_transactions > 0 else 0,
        'Avg Account Ticket Sales': round(total_revenue / total_accounts_selling, 2) if total_accounts_selling > 0 else 0,
        'Avg Event Ticket Sales': round(total_revenue / total_events, 2) if total_events > 0 else 0,
        'Avg Account Fees': round(total_fees / total_accounts_selling, 2) if total_accounts_selling > 0 else 0,
        'Avg Event Fees': round(total_fees / total_events, 2) if total_events > 0 else 0,
        '% With Events': round(results_df['% With Events'].mean(), 1),
        '% Tier Qualified': round(results_df['% Tier Qualified'].mean(), 1),
        'Avg Days to First Event': round(results_df['Avg Days to First Event'].dropna().mean(), 1) if results_df['Avg Days to First Event'].notna().any() else None,
        'Avg Days to First Sale': round(results_df['Avg Days to First Sale'].dropna().mean(), 1) if results_df['Avg Days to First Sale'].notna().any() else None,
    }

    if has_yoy:
        period_row['PY New Accounts'] = total_py_accts
        period_row['PY Events With Sales'] = total_py_events
        period_row['PY Ticket Revenue'] = round(total_py_revenue, 2)
        period_row['PY Fees'] = round(total_py_fees, 2)
        period_row['YoY New Accounts'] = round(yoy_accts, 1) if yoy_accts is not None else None
        period_row['YoY Events With Sales'] = round(yoy_events, 1) if yoy_events is not None else None
        period_row['YoY Ticket Revenue'] = round(yoy_revenue, 1) if yoy_revenue is not None else None
        period_row['YoY Fees'] = round(yoy_fees, 1) if yoy_fees is not None else None

    summary_rows.append(period_row)

    # Average row
    num_months = len(results_df)
    avg_row = {
        'Period': 'AVERAGE',
        'Total New Accounts': round(results_df['Total New Accounts'].mean(), 0),
        'Activated (Created Events)': round(results_df['Activated (Created Events)'].mean(), 0),
        'New Accounts Tier Qualified': round(results_df['New Accounts Tier Qualified'].mean(), 0),
        'Accounts Selling In Month': round(results_df['Accounts Selling In Month'].mean(), 0),
        'Events With Sales': round(results_df['Events With Sales'].mean(), 0),
        'Total Tickets Sold': round(results_df['Total Tickets Sold'].mean(), 0),
        'Total Transactions': round(results_df['Total Transactions'].mean(), 0),
        'Total Ticket Revenue': round(results_df['Total Ticket Revenue'].mean(), 2),
        'Total Fees': round(results_df['Total Fees'].mean(), 2),
        'Avg Price Per Ticket': round(results_df['Avg Price Per Ticket'].mean(), 2),
        'Avg Transaction Value': round(results_df['Avg Transaction Value'].mean(), 2),
        'Avg Tickets Per Booking': round(results_df['Avg Tickets Per Booking'].mean(), 2),
        'Avg Account Ticket Sales': round(results_df['Avg Account Ticket Sales'].mean(), 2),
        'Avg Event Ticket Sales': round(results_df['Avg Event Ticket Sales'].mean(), 2),
        'Avg Account Fees': round(results_df['Avg Account Fees'].mean(), 2),
        'Avg Event Fees': round(results_df['Avg Event Fees'].mean(), 2),
        '% With Events': round(results_df['% With Events'].mean(), 1),
        '% Tier Qualified': round(results_df['% Tier Qualified'].mean(), 1),
        'Avg Days to First Event': round(results_df['Avg Days to First Event'].dropna().mean(), 1) if results_df['Avg Days to First Event'].notna().any() else None,
        'Avg Days to First Sale': round(results_df['Avg Days to First Sale'].dropna().mean(), 1) if results_df['Avg Days to First Sale'].notna().any() else None,
    }

    if has_yoy:
        avg_row['PY New Accounts'] = round(total_py_accts / num_months, 0) if total_py_accts > 0 else 0
        avg_row['PY Events With Sales'] = round(total_py_events / num_months, 0) if total_py_events > 0 else 0
        avg_row['PY Ticket Revenue'] = round(total_py_revenue / num_months, 2) if total_py_revenue > 0 else 0
        avg_row['PY Fees'] = round(total_py_fees / num_months, 2) if total_py_fees > 0 else 0
        avg_row['YoY New Accounts'] = round(yoy_accts, 1) if yoy_accts is not None else None
        avg_row['YoY Events With Sales'] = round(yoy_events, 1) if yoy_events is not None else None
        avg_row['YoY Ticket Revenue'] = round(yoy_revenue, 1) if yoy_revenue is not None else None
        avg_row['YoY Fees'] = round(yoy_fees, 1) if yoy_fees is not None else None

    summary_rows.append(avg_row)

    # Add monthly rows with Period column
    for _, row in results_df.iterrows():
        month_row = {'Period': f"{row['Month Name'][:3]} {row['Year']}"}
        for col in results_df.columns:
            if col not in ['Year', 'Month', 'Month Name']:
                month_row[col] = row[col]
        summary_rows.append(month_row)

    # Create DataFrame and save
    summary_df = pd.DataFrame(summary_rows)

    # Generate summary filename
    base_name = output_file.rsplit('.', 1)[0]
    summary_file = f"{base_name}_summary.csv"

    summary_df.to_csv(summary_file, index=False, float_format='%.2f')
    return summary_file


def calculate_churn_rate(accounts_df, booking_df, current_months, previous_months):
    """
    Calculate churn/dormancy rate - accounts active last year but not this year.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        current_months: List of (year, month) tuples for current period
        previous_months: List of (year, month) tuples for previous period

    Returns:
        Dictionary with churn metrics
    """
    if not current_months or not previous_months:
        return {}

    # Get date ranges
    curr_first_year, curr_first_month = current_months[0]
    curr_last_year, curr_last_month = current_months[-1]
    prev_first_year, prev_first_month = previous_months[0]
    prev_last_year, prev_last_month = previous_months[-1]

    curr_start = pd.Timestamp(year=curr_first_year, month=curr_first_month, day=1, tz='Europe/London')
    curr_last_day = calendar.monthrange(curr_last_year, curr_last_month)[1]
    curr_end = pd.Timestamp(year=curr_last_year, month=curr_last_month, day=curr_last_day,
                            hour=23, minute=59, second=59, tz='Europe/London')

    prev_start = pd.Timestamp(year=prev_first_year, month=prev_first_month, day=1, tz='Europe/London')
    prev_last_day = calendar.monthrange(prev_last_year, prev_last_month)[1]
    prev_end = pd.Timestamp(year=prev_last_year, month=prev_last_month, day=prev_last_day,
                            hour=23, minute=59, second=59, tz='Europe/London')

    # Get accounts that had sales in previous period
    prev_bookings = booking_df[
        (booking_df['TransactionDate'] >= prev_start) &
        (booking_df['TransactionDate'] <= prev_end)
    ]
    prev_active_accounts = set(prev_bookings['AccountId'].dropna().unique())

    # Get accounts that had sales in current period
    curr_bookings = booking_df[
        (booking_df['TransactionDate'] >= curr_start) &
        (booking_df['TransactionDate'] <= curr_end)
    ]
    curr_active_accounts = set(curr_bookings['AccountId'].dropna().unique())

    # Churned = active last year but not this year
    churned_accounts = prev_active_accounts - curr_active_accounts
    retained_accounts = prev_active_accounts & curr_active_accounts
    new_active_accounts = curr_active_accounts - prev_active_accounts

    churn_rate = (len(churned_accounts) / len(prev_active_accounts) * 100) if len(prev_active_accounts) > 0 else 0
    retention_rate = (len(retained_accounts) / len(prev_active_accounts) * 100) if len(prev_active_accounts) > 0 else 0

    return {
        'Previous Period Active Accounts': len(prev_active_accounts),
        'Current Period Active Accounts': len(curr_active_accounts),
        'Retained Accounts': len(retained_accounts),
        'Churned Accounts': len(churned_accounts),
        'New Active Accounts': len(new_active_accounts),
        'Churn Rate (%)': round(churn_rate, 1),
        'Retention Rate (%)': round(retention_rate, 1),
    }


def generate_industry_breakdown_csv(accounts_df, booking_df, months, output_file):
    """
    Generate industry breakdown CSV with metrics per industry.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        months: List of (year, month) tuples
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    if not months:
        return None

    # Get date range
    first_year, first_month = months[0]
    last_year, last_month = months[-1]

    period_start = pd.Timestamp(year=first_year, month=first_month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(last_year, last_month)[1]
    period_end = pd.Timestamp(year=last_year, month=last_month, day=last_day,
                              hour=23, minute=59, second=59, tz='Europe/London')

    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) &
        (booking_df['TransactionDate'] <= period_end)
    ].copy()

    # Add sales channel classification
    period_bookings = add_sales_channel_column(period_bookings)

    # Filter new accounts created in period
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Account Id'
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ].copy()

    # Get industry from booking data or accounts
    industry_col = 'Industry' if 'Industry' in period_bookings.columns else None

    if industry_col is None and 'Industry' in accounts_df.columns:
        # Merge industry from accounts to bookings
        period_bookings = period_bookings.merge(
            accounts_df[[account_id_col, 'Industry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )
        industry_col = 'Industry'

    if industry_col is None:
        print("  Warning: No industry data available")
        return None

    # Check if sales channel is available
    has_sales_channel = 'Sales_Channel' in period_bookings.columns

    # Aggregate by industry
    industry_metrics = []

    for industry in period_bookings[industry_col].dropna().unique():
        ind_bookings = period_bookings[period_bookings[industry_col] == industry]
        ind_new_accounts = new_accounts[new_accounts['Industry'] == industry] if 'Industry' in new_accounts.columns else pd.DataFrame()

        total_fees = ind_bookings['TotalFees'].sum() if 'TotalFees' in ind_bookings.columns else 0
        total_tickets = int(ind_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in ind_bookings.columns else 0

        metrics = {
            'Industry': industry,
            'New Accounts': len(ind_new_accounts),
            'Active Accounts': ind_bookings['AccountId'].nunique(),
            'Events With Sales': ind_bookings['EventId'].nunique() if 'EventId' in ind_bookings.columns else 0,
            'Total Tickets': total_tickets,
            'Total Transactions': len(ind_bookings),
            'Total Ticket Revenue': round(ind_bookings['PaymentReceived'].sum(), 2) if 'PaymentReceived' in ind_bookings.columns else 0,
            'Total Fees': round(total_fees, 2),
        }

        # Calculate averages
        if metrics['Active Accounts'] > 0:
            metrics['Avg Revenue Per Account'] = round(metrics['Total Ticket Revenue'] / metrics['Active Accounts'], 2)
            metrics['Avg Fees Per Account'] = round(metrics['Total Fees'] / metrics['Active Accounts'], 2)
        else:
            metrics['Avg Revenue Per Account'] = 0
            metrics['Avg Fees Per Account'] = 0

        if metrics['Events With Sales'] > 0:
            metrics['Avg Revenue Per Event'] = round(metrics['Total Ticket Revenue'] / metrics['Events With Sales'], 2)
        else:
            metrics['Avg Revenue Per Event'] = 0

        # Add Box Office metrics
        if has_sales_channel:
            bo_bookings = ind_bookings[ind_bookings['Sales_Channel'] == 'Box Office']
            bo_fees = bo_bookings['TotalFees'].sum() if 'TotalFees' in bo_bookings.columns else 0
            bo_tickets = int(bo_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in bo_bookings.columns else 0

            metrics['Box Office Tickets'] = bo_tickets
            metrics['Box Office Fees'] = round(bo_fees, 2)
            metrics['Box Office Pct Tickets'] = round(bo_tickets / total_tickets * 100, 1) if total_tickets > 0 else 0
            metrics['Box Office Pct Fees'] = round(bo_fees / total_fees * 100, 1) if total_fees > 0 else 0

        industry_metrics.append(metrics)

    # Sort by total revenue descending
    industry_df = pd.DataFrame(industry_metrics)
    industry_df = industry_df.sort_values('Total Ticket Revenue', ascending=False)

    # Generate filename in industry folder
    base_name = output_file.rsplit('.', 1)[0]
    industry_file = get_output_path(base_name, 'industry', '_by_industry.csv')

    industry_df.to_csv(industry_file, index=False, float_format='%.2f')
    return industry_file


def _calculate_industry_metrics_for_period(booking_df, accounts_df, period_start, period_end, account_id_col):
    """
    Calculate industry/sub-industry metrics for a specific period.

    Args:
        booking_df: Full booking DataFrame
        accounts_df: Full accounts DataFrame
        period_start: Start timestamp
        period_end: End timestamp
        account_id_col: Column name for account ID

    Returns:
        Dictionary mapping (industry, sub_industry) tuple to metrics dict
    """
    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) &
        (booking_df['TransactionDate'] <= period_end)
    ].copy()

    if len(period_bookings) == 0:
        return {}

    # Get industry from booking data or merge from accounts
    if 'Industry' not in period_bookings.columns and 'Industry' in accounts_df.columns:
        period_bookings = period_bookings.merge(
            accounts_df[[account_id_col, 'Industry', 'SubIndustry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )
    elif 'SubIndustry' not in period_bookings.columns and 'SubIndustry' in accounts_df.columns:
        period_bookings = period_bookings.merge(
            accounts_df[[account_id_col, 'SubIndustry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )

    if 'Industry' not in period_bookings.columns:
        return {}

    # Filter new accounts created in period
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ].copy()

    # Aggregate by industry and sub-industry
    industry_metrics = {}

    # Handle SubIndustry column - might not exist
    has_sub_industry = 'SubIndustry' in period_bookings.columns

    if has_sub_industry:
        # Group by both Industry and SubIndustry
        for (industry, sub_industry), group in period_bookings.groupby(['Industry', 'SubIndustry'], dropna=False):
            if pd.isna(industry):
                continue

            # Handle NaN sub-industry
            sub_industry_str = sub_industry if pd.notna(sub_industry) else 'Unspecified'

            # Filter new accounts for this industry/sub-industry
            if 'Industry' in new_accounts.columns and 'SubIndustry' in new_accounts.columns:
                ind_new_accounts = new_accounts[
                    (new_accounts['Industry'] == industry) &
                    ((new_accounts['SubIndustry'] == sub_industry) | (pd.isna(new_accounts['SubIndustry']) & pd.isna(sub_industry)))
                ]
            else:
                ind_new_accounts = pd.DataFrame()

            metrics = {
                'New Accounts': len(ind_new_accounts),
                'Active Accounts': group['AccountId'].nunique(),
                'Events With Sales': group['EventId'].nunique() if 'EventId' in group.columns else 0,
                'Total Tickets': int(group['TicketQuantity'].sum()) if 'TicketQuantity' in group.columns else 0,
                'Total Transactions': len(group),
                'Total Ticket Revenue': round(group['PaymentReceived'].sum(), 2) if 'PaymentReceived' in group.columns else 0,
                'Total Fees': round(group['TotalFees'].sum(), 2) if 'TotalFees' in group.columns else 0,
            }

            industry_metrics[(industry, sub_industry_str)] = metrics
    else:
        # Group by Industry only
        for industry, group in period_bookings.groupby('Industry', dropna=False):
            if pd.isna(industry):
                continue

            # Filter new accounts for this industry
            if 'Industry' in new_accounts.columns:
                ind_new_accounts = new_accounts[new_accounts['Industry'] == industry]
            else:
                ind_new_accounts = pd.DataFrame()

            metrics = {
                'New Accounts': len(ind_new_accounts),
                'Active Accounts': group['AccountId'].nunique(),
                'Events With Sales': group['EventId'].nunique() if 'EventId' in group.columns else 0,
                'Total Tickets': int(group['TicketQuantity'].sum()) if 'TicketQuantity' in group.columns else 0,
                'Total Transactions': len(group),
                'Total Ticket Revenue': round(group['PaymentReceived'].sum(), 2) if 'PaymentReceived' in group.columns else 0,
                'Total Fees': round(group['TotalFees'].sum(), 2) if 'TotalFees' in group.columns else 0,
            }

            industry_metrics[(industry, 'Unspecified')] = metrics

    return industry_metrics


def generate_industry_subindustry_breakdown_csv(accounts_df, booking_df, months, output_file):
    """
    Generate industry/sub-industry breakdown CSV with metrics and YoY comparison.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        months: List of (year, month) tuples
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    if not months:
        return None

    # Get date range for current period
    first_year, first_month = months[0]
    last_year, last_month = months[-1]

    period_start = pd.Timestamp(year=first_year, month=first_month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(last_year, last_month)[1]
    period_end = pd.Timestamp(year=last_year, month=last_month, day=last_day,
                              hour=23, minute=59, second=59, tz='Europe/London')

    # Calculate previous year period
    py_start = pd.Timestamp(year=first_year - 1, month=first_month, day=1, tz='Europe/London')
    py_last_day = calendar.monthrange(last_year - 1, last_month)[1]
    py_end = pd.Timestamp(year=last_year - 1, month=last_month, day=py_last_day,
                          hour=23, minute=59, second=59, tz='Europe/London')

    # Determine account ID column
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'

    # Check if industry data is available
    if 'Industry' not in booking_df.columns and 'Industry' not in accounts_df.columns:
        print("  Warning: No industry data available")
        return None

    # Calculate metrics for both periods
    current_metrics = _calculate_industry_metrics_for_period(
        booking_df, accounts_df, period_start, period_end, account_id_col
    )
    py_metrics = _calculate_industry_metrics_for_period(
        booking_df, accounts_df, py_start, py_end, account_id_col
    )

    # Combine all industry/sub-industry pairs from both periods
    all_keys = set(current_metrics.keys()) | set(py_metrics.keys())

    # Build combined metrics with YoY comparison
    industry_rows = []
    for key in all_keys:
        industry, sub_industry = key
        curr = current_metrics.get(key, {})
        prev = py_metrics.get(key, {})

        row = {
            'Industry': industry,
            'Sub-Industry': sub_industry,
            # Current year metrics
            'New Accounts': curr.get('New Accounts', 0),
            'Active Accounts': curr.get('Active Accounts', 0),
            'Events With Sales': curr.get('Events With Sales', 0),
            'Total Tickets': curr.get('Total Tickets', 0),
            'Total Transactions': curr.get('Total Transactions', 0),
            'Total Ticket Revenue': curr.get('Total Ticket Revenue', 0),
            'Total Fees': curr.get('Total Fees', 0),
            # Previous year metrics
            'PY New Accounts': prev.get('New Accounts', 0),
            'PY Active Accounts': prev.get('Active Accounts', 0),
            'PY Events With Sales': prev.get('Events With Sales', 0),
            'PY Total Tickets': prev.get('Total Tickets', 0),
            'PY Total Transactions': prev.get('Total Transactions', 0),
            'PY Total Ticket Revenue': prev.get('Total Ticket Revenue', 0),
            'PY Total Fees': prev.get('Total Fees', 0),
        }

        # Calculate YoY changes
        curr_revenue = curr.get('Total Ticket Revenue', 0)
        prev_revenue = prev.get('Total Ticket Revenue', 0)
        if prev_revenue > 0:
            row['Revenue YoY %'] = round((curr_revenue - prev_revenue) / prev_revenue * 100, 1)
        else:
            row['Revenue YoY %'] = None

        curr_tickets = curr.get('Total Tickets', 0)
        prev_tickets = prev.get('Total Tickets', 0)
        if prev_tickets > 0:
            row['Tickets YoY %'] = round((curr_tickets - prev_tickets) / prev_tickets * 100, 1)
        else:
            row['Tickets YoY %'] = None

        curr_accounts = curr.get('Active Accounts', 0)
        prev_accounts = prev.get('Active Accounts', 0)
        if prev_accounts > 0:
            row['Active Accounts YoY %'] = round((curr_accounts - prev_accounts) / prev_accounts * 100, 1)
        else:
            row['Active Accounts YoY %'] = None

        curr_fees = curr.get('Total Fees', 0)
        prev_fees = prev.get('Total Fees', 0)
        if prev_fees > 0:
            row['Fees YoY %'] = round((curr_fees - prev_fees) / prev_fees * 100, 1)
        else:
            row['Fees YoY %'] = None

        # Calculate averages for current period
        if row['Active Accounts'] > 0:
            row['Avg Revenue Per Account'] = round(row['Total Ticket Revenue'] / row['Active Accounts'], 2)
            row['Avg Fees Per Account'] = round(row['Total Fees'] / row['Active Accounts'], 2)
        else:
            row['Avg Revenue Per Account'] = 0
            row['Avg Fees Per Account'] = 0

        if row['Events With Sales'] > 0:
            row['Avg Revenue Per Event'] = round(row['Total Ticket Revenue'] / row['Events With Sales'], 2)
        else:
            row['Avg Revenue Per Event'] = 0

        industry_rows.append(row)

    # Sort by industry, then sub-industry, then by total revenue descending
    industry_df = pd.DataFrame(industry_rows)
    industry_df = industry_df.sort_values(
        ['Industry', 'Total Ticket Revenue'],
        ascending=[True, False]
    )

    # Generate filename in industry folder
    base_name = output_file.rsplit('.', 1)[0]
    industry_file = get_output_path(base_name, 'industry', '_by_industry_subindustry.csv')

    industry_df.to_csv(industry_file, index=False, float_format='%.2f')
    return industry_file


def _calculate_industry_geo_metrics_for_period(booking_df, accounts_df, period_start, period_end, account_id_col):
    """
    Calculate industry x geography cross-tab metrics for a specific period.

    Args:
        booking_df: Full booking DataFrame
        accounts_df: Full accounts DataFrame
        period_start: Start timestamp
        period_end: End timestamp
        account_id_col: Column name for account ID

    Returns:
        Dictionary mapping (industry, region) tuple to metrics dict
    """
    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) &
        (booking_df['TransactionDate'] <= period_end)
    ].copy()

    if len(period_bookings) == 0:
        return {}

    # Determine postcode column and extract regions
    postcode_col = None
    if 'EventPostcode' in period_bookings.columns:
        postcode_col = 'EventPostcode'
    elif 'AccountPostcode' in period_bookings.columns:
        postcode_col = 'AccountPostcode'

    if postcode_col is None:
        return {}

    # Extract postcode areas and regions using shared module
    period_bookings['PostcodeArea'] = extract_postcode_areas_vectorized(period_bookings[postcode_col])
    period_bookings['Region'] = get_regions_vectorized(period_bookings['PostcodeArea'])

    # Filter to valid UK postcode areas only
    period_bookings = period_bookings[
        period_bookings['PostcodeArea'].isin(VALID_UK_POSTCODE_AREAS)
    ]

    # Get industry from booking data or merge from accounts
    if 'Industry' not in period_bookings.columns and 'Industry' in accounts_df.columns:
        period_bookings = period_bookings.merge(
            accounts_df[[account_id_col, 'Industry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )

    if 'Industry' not in period_bookings.columns:
        return {}

    # Filter new accounts created in period
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ].copy()

    # Extract region for new accounts
    account_postcode_col = 'Postcode' if 'Postcode' in new_accounts.columns else None
    if account_postcode_col:
        new_accounts['PostcodeArea'] = extract_postcode_areas_vectorized(new_accounts[account_postcode_col])
        new_accounts['Region'] = get_regions_vectorized(new_accounts['PostcodeArea'])

    # Aggregate by industry and region
    cross_metrics = {}

    for (industry, region), group in period_bookings.groupby(['Industry', 'Region'], dropna=False):
        if pd.isna(industry) or region == 'Unknown':
            continue

        # Filter new accounts for this industry/region
        if 'Industry' in new_accounts.columns and 'Region' in new_accounts.columns:
            ind_new_accounts = new_accounts[
                (new_accounts['Industry'] == industry) &
                (new_accounts['Region'] == region)
            ]
        else:
            ind_new_accounts = pd.DataFrame()

        metrics = {
            'New Accounts': len(ind_new_accounts),
            'Active Accounts': group['AccountId'].nunique(),
            'Events With Sales': group['EventId'].nunique() if 'EventId' in group.columns else 0,
            'Total Tickets': int(group['TicketQuantity'].sum()) if 'TicketQuantity' in group.columns else 0,
            'Total Transactions': len(group),
            'Total Ticket Revenue': round(group['PaymentReceived'].sum(), 2) if 'PaymentReceived' in group.columns else 0,
            'Total Fees': round(group['TotalFees'].sum(), 2) if 'TotalFees' in group.columns else 0,
        }

        cross_metrics[(industry, region)] = metrics

    return cross_metrics


def generate_industry_geography_crosstab_csv(accounts_df, booking_df, months, output_file):
    """
    Generate industry x geography cross-tab CSV with metrics and YoY comparison.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        months: List of (year, month) tuples
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    if not months:
        return None

    # Get date range for current period
    first_year, first_month = months[0]
    last_year, last_month = months[-1]

    period_start = pd.Timestamp(year=first_year, month=first_month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(last_year, last_month)[1]
    period_end = pd.Timestamp(year=last_year, month=last_month, day=last_day,
                              hour=23, minute=59, second=59, tz='Europe/London')

    # Calculate previous year period
    py_start = pd.Timestamp(year=first_year - 1, month=first_month, day=1, tz='Europe/London')
    py_last_day = calendar.monthrange(last_year - 1, last_month)[1]
    py_end = pd.Timestamp(year=last_year - 1, month=last_month, day=py_last_day,
                          hour=23, minute=59, second=59, tz='Europe/London')

    # Determine account ID column
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'

    # Calculate metrics for both periods
    current_metrics = _calculate_industry_geo_metrics_for_period(
        booking_df, accounts_df, period_start, period_end, account_id_col
    )
    py_metrics = _calculate_industry_geo_metrics_for_period(
        booking_df, accounts_df, py_start, py_end, account_id_col
    )

    if not current_metrics and not py_metrics:
        print("  Warning: No industry/geography data available")
        return None

    # Combine all industry/region pairs from both periods
    all_keys = set(current_metrics.keys()) | set(py_metrics.keys())

    # Build combined metrics with YoY comparison
    cross_rows = []
    for key in all_keys:
        industry, region = key
        curr = current_metrics.get(key, {})
        prev = py_metrics.get(key, {})

        row = {
            'Industry': industry,
            'Region': region,
            # Current year metrics
            'New Accounts': curr.get('New Accounts', 0),
            'Active Accounts': curr.get('Active Accounts', 0),
            'Events With Sales': curr.get('Events With Sales', 0),
            'Total Tickets': curr.get('Total Tickets', 0),
            'Total Transactions': curr.get('Total Transactions', 0),
            'Total Ticket Revenue': curr.get('Total Ticket Revenue', 0),
            'Total Fees': curr.get('Total Fees', 0),
            # Previous year metrics
            'PY New Accounts': prev.get('New Accounts', 0),
            'PY Active Accounts': prev.get('Active Accounts', 0),
            'PY Events With Sales': prev.get('Events With Sales', 0),
            'PY Total Tickets': prev.get('Total Tickets', 0),
            'PY Total Transactions': prev.get('Total Transactions', 0),
            'PY Total Ticket Revenue': prev.get('Total Ticket Revenue', 0),
            'PY Total Fees': prev.get('Total Fees', 0),
        }

        # Calculate YoY changes
        curr_revenue = curr.get('Total Ticket Revenue', 0)
        prev_revenue = prev.get('Total Ticket Revenue', 0)
        if prev_revenue > 0:
            row['Revenue YoY %'] = round((curr_revenue - prev_revenue) / prev_revenue * 100, 1)
        else:
            row['Revenue YoY %'] = None

        curr_tickets = curr.get('Total Tickets', 0)
        prev_tickets = prev.get('Total Tickets', 0)
        if prev_tickets > 0:
            row['Tickets YoY %'] = round((curr_tickets - prev_tickets) / prev_tickets * 100, 1)
        else:
            row['Tickets YoY %'] = None

        curr_accounts = curr.get('Active Accounts', 0)
        prev_accounts = prev.get('Active Accounts', 0)
        if prev_accounts > 0:
            row['Active Accounts YoY %'] = round((curr_accounts - prev_accounts) / prev_accounts * 100, 1)
        else:
            row['Active Accounts YoY %'] = None

        # Calculate averages for current period
        if row['Active Accounts'] > 0:
            row['Avg Revenue Per Account'] = round(row['Total Ticket Revenue'] / row['Active Accounts'], 2)
        else:
            row['Avg Revenue Per Account'] = 0

        if row['Events With Sales'] > 0:
            row['Avg Revenue Per Event'] = round(row['Total Ticket Revenue'] / row['Events With Sales'], 2)
        else:
            row['Avg Revenue Per Event'] = 0

        cross_rows.append(row)

    # Sort by industry, then region, then by total revenue descending
    cross_df = pd.DataFrame(cross_rows)
    cross_df = cross_df.sort_values(
        ['Industry', 'Total Ticket Revenue'],
        ascending=[True, False]
    )

    # Generate filename in industry folder
    base_name = output_file.rsplit('.', 1)[0]
    cross_file = get_output_path(base_name, 'industry', '_industry_x_geography.csv')

    cross_df.to_csv(cross_file, index=False, float_format='%.2f')
    return cross_file


def _calculate_geo_metrics_for_period(booking_df, accounts_df, period_start, period_end, postcode_col):
    """
    Calculate geographic metrics for a specific period.

    Args:
        booking_df: Full booking DataFrame
        accounts_df: Full accounts DataFrame
        period_start: Start timestamp
        period_end: End timestamp
        postcode_col: Column name for postcode data

    Returns:
        Dictionary mapping postcode area to metrics dict
    """
    # Filter bookings to period
    period_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) &
        (booking_df['TransactionDate'] <= period_end)
    ].copy()

    if len(period_bookings) == 0:
        return {}

    # Add sales channel classification
    period_bookings = add_sales_channel_column(period_bookings)
    has_sales_channel = 'Sales_Channel' in period_bookings.columns

    # Extract postcode areas and regions using shared module
    period_bookings['PostcodeArea'] = extract_postcode_areas_vectorized(period_bookings[postcode_col])
    period_bookings['Region'] = get_regions_vectorized(period_bookings['PostcodeArea'])

    # Filter to valid UK postcode areas only
    period_bookings = period_bookings[
        period_bookings['PostcodeArea'].isin(VALID_UK_POSTCODE_AREAS)
    ]

    # Filter new accounts created in period
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ].copy()

    # Extract postcode area from accounts if available
    account_postcode_col = 'Postcode' if 'Postcode' in new_accounts.columns else None
    if account_postcode_col:
        new_accounts['PostcodeArea'] = extract_postcode_areas_vectorized(new_accounts[account_postcode_col])

    # Aggregate by postcode area
    geo_metrics = {}

    for area in period_bookings['PostcodeArea'].dropna().unique():
        area_bookings = period_bookings[period_bookings['PostcodeArea'] == area]
        area_new_accounts = new_accounts[new_accounts['PostcodeArea'] == area] if 'PostcodeArea' in new_accounts.columns else pd.DataFrame()

        # Get the region for this postcode area
        region = area_bookings['Region'].iloc[0] if len(area_bookings) > 0 else 'Unknown'

        total_tickets = int(area_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in area_bookings.columns else 0
        total_fees = area_bookings['TotalFees'].sum() if 'TotalFees' in area_bookings.columns else 0

        metrics = {
            'Region': region,
            'New Accounts': len(area_new_accounts),
            'Active Accounts': area_bookings['AccountId'].nunique(),
            'Events With Sales': area_bookings['EventId'].nunique() if 'EventId' in area_bookings.columns else 0,
            'Total Tickets': total_tickets,
            'Total Transactions': len(area_bookings),
            'Total Ticket Revenue': round(area_bookings['PaymentReceived'].sum(), 2) if 'PaymentReceived' in area_bookings.columns else 0,
            'Total Fees': round(total_fees, 2),
        }

        # Add Box Office metrics
        if has_sales_channel:
            bo_bookings = area_bookings[area_bookings['Sales_Channel'] == 'Box Office']
            bo_fees = bo_bookings['TotalFees'].sum() if 'TotalFees' in bo_bookings.columns else 0
            bo_tickets = int(bo_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in bo_bookings.columns else 0

            metrics['Box Office Tickets'] = bo_tickets
            metrics['Box Office Fees'] = round(bo_fees, 2)
            metrics['Box Office Pct Tickets'] = round(bo_tickets / total_tickets * 100, 1) if total_tickets > 0 else 0
            metrics['Box Office Pct Fees'] = round(bo_fees / total_fees * 100, 1) if total_fees > 0 else 0

        geo_metrics[area] = metrics

    return geo_metrics


def generate_geographic_breakdown_csv(accounts_df, booking_df, months, output_file):
    """
    Generate geographic breakdown CSV with metrics per region (based on postcodes).

    Uses the shared uk_regional_segmentation module for consistent postcode handling,
    including BFPO support and validation against known UK postcode areas.
    Includes YoY comparison with previous year's same period.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        months: List of (year, month) tuples
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    if not months:
        return None

    # Get date range for current period
    first_year, first_month = months[0]
    last_year, last_month = months[-1]

    period_start = pd.Timestamp(year=first_year, month=first_month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(last_year, last_month)[1]
    period_end = pd.Timestamp(year=last_year, month=last_month, day=last_day,
                              hour=23, minute=59, second=59, tz='Europe/London')

    # Calculate previous year period
    py_start = pd.Timestamp(year=first_year - 1, month=first_month, day=1, tz='Europe/London')
    py_last_day = calendar.monthrange(last_year - 1, last_month)[1]
    py_end = pd.Timestamp(year=last_year - 1, month=last_month, day=py_last_day,
                          hour=23, minute=59, second=59, tz='Europe/London')

    # Determine postcode column
    postcode_col = None
    if 'EventPostcode' in booking_df.columns:
        postcode_col = 'EventPostcode'
    elif 'AccountPostcode' in booking_df.columns:
        postcode_col = 'AccountPostcode'

    if postcode_col is None:
        print("  Warning: No postcode data available")
        return None

    # Calculate metrics for both periods
    current_metrics = _calculate_geo_metrics_for_period(
        booking_df, accounts_df, period_start, period_end, postcode_col
    )
    py_metrics = _calculate_geo_metrics_for_period(
        booking_df, accounts_df, py_start, py_end, postcode_col
    )

    # Combine all postcode areas from both periods
    all_areas = set(current_metrics.keys()) | set(py_metrics.keys())

    # Build combined metrics with YoY comparison
    geo_rows = []
    for area in all_areas:
        curr = current_metrics.get(area, {})
        prev = py_metrics.get(area, {})

        # Get region from whichever period has data
        region = curr.get('Region') or prev.get('Region', 'Unknown')

        row = {
            'Postcode Area': area,
            'Region': region,
            # Current year metrics
            'New Accounts': curr.get('New Accounts', 0),
            'Active Accounts': curr.get('Active Accounts', 0),
            'Events With Sales': curr.get('Events With Sales', 0),
            'Total Tickets': curr.get('Total Tickets', 0),
            'Total Transactions': curr.get('Total Transactions', 0),
            'Total Ticket Revenue': curr.get('Total Ticket Revenue', 0),
            'Total Fees': curr.get('Total Fees', 0),
            # Previous year metrics
            'PY New Accounts': prev.get('New Accounts', 0),
            'PY Active Accounts': prev.get('Active Accounts', 0),
            'PY Events With Sales': prev.get('Events With Sales', 0),
            'PY Total Tickets': prev.get('Total Tickets', 0),
            'PY Total Transactions': prev.get('Total Transactions', 0),
            'PY Total Ticket Revenue': prev.get('Total Ticket Revenue', 0),
            'PY Total Fees': prev.get('Total Fees', 0),
        }

        # Calculate YoY changes
        curr_revenue = curr.get('Total Ticket Revenue', 0)
        prev_revenue = prev.get('Total Ticket Revenue', 0)
        if prev_revenue > 0:
            row['Revenue YoY %'] = round((curr_revenue - prev_revenue) / prev_revenue * 100, 1)
        else:
            row['Revenue YoY %'] = None

        curr_tickets = curr.get('Total Tickets', 0)
        prev_tickets = prev.get('Total Tickets', 0)
        if prev_tickets > 0:
            row['Tickets YoY %'] = round((curr_tickets - prev_tickets) / prev_tickets * 100, 1)
        else:
            row['Tickets YoY %'] = None

        curr_accounts = curr.get('Active Accounts', 0)
        prev_accounts = prev.get('Active Accounts', 0)
        if prev_accounts > 0:
            row['Active Accounts YoY %'] = round((curr_accounts - prev_accounts) / prev_accounts * 100, 1)
        else:
            row['Active Accounts YoY %'] = None

        # Calculate averages for current period
        if row['Active Accounts'] > 0:
            row['Avg Revenue Per Account'] = round(row['Total Ticket Revenue'] / row['Active Accounts'], 2)
        else:
            row['Avg Revenue Per Account'] = 0

        if row['Events With Sales'] > 0:
            row['Avg Revenue Per Event'] = round(row['Total Ticket Revenue'] / row['Events With Sales'], 2)
        else:
            row['Avg Revenue Per Event'] = 0

        # Add Box Office metrics (if available in current period)
        if 'Box Office Tickets' in curr:
            row['Box Office Tickets'] = curr.get('Box Office Tickets', 0)
            row['Box Office Fees'] = curr.get('Box Office Fees', 0)
            row['Box Office Pct Tickets'] = curr.get('Box Office Pct Tickets', 0)
            row['Box Office Pct Fees'] = curr.get('Box Office Pct Fees', 0)

        geo_rows.append(row)

    # Sort by total revenue descending
    geo_df = pd.DataFrame(geo_rows)
    geo_df = geo_df.sort_values('Total Ticket Revenue', ascending=False)

    # Generate filename in geography folder
    base_name = output_file.rsplit('.', 1)[0]
    geo_file = get_output_path(base_name, 'geography', '_by_geography.csv')

    geo_df.to_csv(geo_file, index=False, float_format='%.2f')
    return geo_file


def generate_seasonality_analysis_csv(booking_df, accounts_df, output_file):
    """
    Generate seasonality analysis showing monthly patterns by industry, sub-industry,
    region, and event type. Identifies dips and peaks to help plan contra-seasonal strategies.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dict of output files generated
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    # Get industry and sub-industry from accounts if not in bookings
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'
    merge_cols = [account_id_col]
    if 'Industry' in accounts_df.columns:
        merge_cols.append('Industry')
    if 'SubIndustry' in accounts_df.columns:
        merge_cols.append('SubIndustry')

    if len(merge_cols) > 1:
        accounts_subset = accounts_df[merge_cols].rename(columns={account_id_col: 'AccountId'})
        # Only merge columns we don't already have
        cols_to_merge = ['AccountId'] + [c for c in merge_cols[1:] if c not in booking_df.columns]
        if len(cols_to_merge) > 1:
            booking_df = booking_df.merge(
                accounts_subset[cols_to_merge],
                on='AccountId',
                how='left'
            )

    # Ensure we have TransactionDate as datetime
    if 'TransactionDate' not in booking_df.columns:
        return output_files

    booking_df = booking_df.copy()
    booking_df['Month'] = pd.to_datetime(booking_df['TransactionDate']).dt.month
    booking_df['Year'] = pd.to_datetime(booking_df['TransactionDate']).dt.year
    booking_df['YearMonth'] = booking_df['Year'].astype(str) + '-' + booking_df['Month'].astype(str).str.zfill(2)

    # Add region data from postcodes
    postcode_col = None
    if 'EventPostcode' in booking_df.columns:
        postcode_col = 'EventPostcode'
    elif 'AccountPostcode' in booking_df.columns:
        postcode_col = 'AccountPostcode'

    if postcode_col:
        booking_df['PostcodeArea'] = extract_postcode_areas_vectorized(booking_df[postcode_col])
        booking_df['Region'] = get_regions_vectorized(booking_df['PostcodeArea'])
        # Filter to valid UK postcodes only for region analysis
        booking_df.loc[~booking_df['PostcodeArea'].isin(VALID_UK_POSTCODE_AREAS), 'Region'] = None

    # Standard month order for all pivots
    month_order = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    # === DATA QUALITY SUMMARY ===
    total_transactions = len(booking_df)
    total_revenue = booking_df['PaymentReceived'].sum()

    print(f"\n  Data Quality Summary ({total_transactions:,} transactions, £{total_revenue:,.0f} revenue):")

    # Industry coverage
    if 'Industry' in booking_df.columns:
        ind_valid = booking_df['Industry'].notna() & (booking_df['Industry'] != '')
        ind_txns = ind_valid.sum()
        ind_rev = booking_df.loc[ind_valid, 'PaymentReceived'].sum()
        print(f"    Industry:     {ind_txns:,} txns ({ind_txns/total_transactions*100:.1f}%), £{ind_rev:,.0f} ({ind_rev/total_revenue*100:.1f}% of revenue)")

    # SubIndustry coverage
    if 'SubIndustry' in booking_df.columns:
        sub_valid = booking_df['SubIndustry'].notna() & (booking_df['SubIndustry'] != '')
        sub_txns = sub_valid.sum()
        sub_rev = booking_df.loc[sub_valid, 'PaymentReceived'].sum()
        print(f"    SubIndustry:  {sub_txns:,} txns ({sub_txns/total_transactions*100:.1f}%), £{sub_rev:,.0f} ({sub_rev/total_revenue*100:.1f}% of revenue)")

    # Region coverage
    if 'Region' in booking_df.columns:
        reg_valid = booking_df['Region'].notna() & (booking_df['Region'] != 'Unknown')
        reg_txns = reg_valid.sum()
        reg_rev = booking_df.loc[reg_valid, 'PaymentReceived'].sum()
        postcode_source = postcode_col if postcode_col else 'N/A'
        print(f"    Region:       {reg_txns:,} txns ({reg_txns/total_transactions*100:.1f}%), £{reg_rev:,.0f} ({reg_rev/total_revenue*100:.1f}% of revenue) [from {postcode_source}]")

    print()

    # === INDUSTRY SEASONALITY ===
    if 'Industry' in booking_df.columns:
        # Calculate monthly revenue by industry
        industry_monthly = booking_df.groupby(['Industry', 'Month']).agg({
            'PaymentReceived': 'sum',
            'TicketQuantity': 'sum',
            'EventId': 'nunique'
        }).reset_index()
        industry_monthly.columns = ['Industry', 'Month', 'Revenue', 'Tickets', 'Events']

        # Calculate each industry's annual total
        industry_annual = industry_monthly.groupby('Industry')['Revenue'].sum().reset_index()
        industry_annual.columns = ['Industry', 'Annual Revenue']

        # Merge to get percentage
        industry_monthly = industry_monthly.merge(industry_annual, on='Industry')
        industry_monthly['% of Annual'] = round(industry_monthly['Revenue'] / industry_monthly['Annual Revenue'] * 100, 1)

        # Calculate expected % if perfectly flat (8.33% per month)
        industry_monthly['Expected %'] = 8.33
        industry_monthly['Variance %'] = round(industry_monthly['% of Annual'] - 8.33, 1)

        # Flag dips (more than 2% below expected) and peaks (more than 2% above)
        industry_monthly['Status'] = 'Normal'
        industry_monthly.loc[industry_monthly['Variance %'] <= -2, 'Status'] = 'DIP'
        industry_monthly.loc[industry_monthly['Variance %'] >= 2, 'Status'] = 'PEAK'

        # Add month names
        industry_monthly['Month Name'] = industry_monthly['Month'].apply(lambda m: calendar.month_abbr[m])

        # Pivot to show months as columns for easier reading
        industry_pivot = industry_monthly.pivot(index='Industry', columns='Month Name', values='% of Annual')

        # Reorder columns to calendar order
        available_months = [m for m in month_order if m in industry_pivot.columns]
        industry_pivot = industry_pivot[available_months]

        # Add annual revenue for context
        industry_pivot = industry_pivot.merge(industry_annual.set_index('Industry'), left_index=True, right_index=True)

        # Calculate volatility (std dev of monthly %)
        monthly_cols = [c for c in industry_pivot.columns if c in month_order]
        industry_pivot['Volatility'] = industry_pivot[monthly_cols].std(axis=1).round(1)

        # Sort by annual revenue
        industry_pivot = industry_pivot.sort_values('Annual Revenue', ascending=False)

        industry_file = get_output_path(base_name, 'seasonality', '_seasonality_by_industry.csv')
        industry_pivot.to_csv(industry_file, float_format='%.1f')
        print(f"  ✓ Industry seasonality saved to: {industry_file}")
        output_files['industry'] = industry_file

        # Also save the detailed view with dip/peak flags
        detail_file = get_output_path(base_name, 'seasonality', '_seasonality_industry_detail.csv')
        industry_monthly_out = industry_monthly[['Industry', 'Month', 'Month Name', 'Revenue', 'Tickets', 'Events', '% of Annual', 'Variance %', 'Status']]
        industry_monthly_out = industry_monthly_out.sort_values(['Industry', 'Month'])
        industry_monthly_out = industry_monthly_out.drop(columns=['Month'])  # Remove numeric month from output
        industry_monthly_out.to_csv(detail_file, index=False, float_format='%.1f')
        print(f"  ✓ Industry seasonality detail saved to: {detail_file}")
        output_files['industry_detail'] = detail_file

    # === SUB-INDUSTRY SEASONALITY ===
    if 'SubIndustry' in booking_df.columns:
        # Filter to rows with valid sub-industry
        sub_df = booking_df[booking_df['SubIndustry'].notna() & (booking_df['SubIndustry'] != '')]

        if len(sub_df) > 0:
            # Calculate monthly revenue by sub-industry
            subind_monthly = sub_df.groupby(['SubIndustry', 'Month']).agg({
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'EventId': 'nunique'
            }).reset_index()
            subind_monthly.columns = ['SubIndustry', 'Month', 'Revenue', 'Tickets', 'Events']

            # Calculate each sub-industry's annual total
            subind_annual = subind_monthly.groupby('SubIndustry')['Revenue'].sum().reset_index()
            subind_annual.columns = ['SubIndustry', 'Annual Revenue']

            # Merge to get percentage
            subind_monthly = subind_monthly.merge(subind_annual, on='SubIndustry')
            subind_monthly['% of Annual'] = round(subind_monthly['Revenue'] / subind_monthly['Annual Revenue'] * 100, 1)
            subind_monthly['Variance %'] = round(subind_monthly['% of Annual'] - 8.33, 1)
            subind_monthly['Status'] = 'Normal'
            subind_monthly.loc[subind_monthly['Variance %'] <= -2, 'Status'] = 'DIP'
            subind_monthly.loc[subind_monthly['Variance %'] >= 2, 'Status'] = 'PEAK'
            subind_monthly['Month Name'] = subind_monthly['Month'].apply(lambda m: calendar.month_abbr[m])

            # Pivot for overview
            subind_pivot = subind_monthly.pivot(index='SubIndustry', columns='Month Name', values='% of Annual')
            available_months = [m for m in month_order if m in subind_pivot.columns]
            subind_pivot = subind_pivot[available_months]
            subind_pivot = subind_pivot.merge(subind_annual.set_index('SubIndustry'), left_index=True, right_index=True)
            subind_pivot['Volatility'] = subind_pivot[[c for c in subind_pivot.columns if c in month_order]].std(axis=1).round(1)
            subind_pivot = subind_pivot.sort_values('Annual Revenue', ascending=False)

            subind_file = get_output_path(base_name, 'seasonality', '_seasonality_by_subindustry.csv')
            subind_pivot.to_csv(subind_file, float_format='%.1f')
            print(f"  ✓ Sub-industry seasonality saved to: {subind_file}")
            output_files['subindustry'] = subind_file

    # === INDUSTRY + SUB-INDUSTRY HIERARCHY ===
    if 'Industry' in booking_df.columns and 'SubIndustry' in booking_df.columns:
        # Group by both industry and sub-industry
        hierarchy_df = booking_df.copy()
        hierarchy_df['SubIndustry'] = hierarchy_df['SubIndustry'].fillna('(No Sub-Industry)')

        hierarchy_monthly = hierarchy_df.groupby(['Industry', 'SubIndustry', 'Month']).agg({
            'PaymentReceived': 'sum',
            'TicketQuantity': 'sum',
            'EventId': 'nunique'
        }).reset_index()
        hierarchy_monthly.columns = ['Industry', 'SubIndustry', 'Month', 'Revenue', 'Tickets', 'Events']

        # Calculate annual totals per industry+subindustry combo
        hierarchy_annual = hierarchy_monthly.groupby(['Industry', 'SubIndustry'])['Revenue'].sum().reset_index()
        hierarchy_annual.columns = ['Industry', 'SubIndustry', 'Annual Revenue']

        hierarchy_monthly = hierarchy_monthly.merge(hierarchy_annual, on=['Industry', 'SubIndustry'])
        hierarchy_monthly['% of Annual'] = round(hierarchy_monthly['Revenue'] / hierarchy_monthly['Annual Revenue'] * 100, 1)
        hierarchy_monthly['Variance %'] = round(hierarchy_monthly['% of Annual'] - 8.33, 1)
        hierarchy_monthly['Month Name'] = hierarchy_monthly['Month'].apply(lambda m: calendar.month_abbr[m])

        # Pivot with multi-index
        hierarchy_pivot = hierarchy_monthly.pivot(
            index=['Industry', 'SubIndustry'],
            columns='Month Name',
            values='% of Annual'
        )
        available_months = [m for m in month_order if m in hierarchy_pivot.columns]
        hierarchy_pivot = hierarchy_pivot[available_months]
        hierarchy_pivot = hierarchy_pivot.merge(
            hierarchy_annual.set_index(['Industry', 'SubIndustry']),
            left_index=True, right_index=True
        )
        hierarchy_pivot['Volatility'] = hierarchy_pivot[[c for c in hierarchy_pivot.columns if c in month_order]].std(axis=1).round(1)
        hierarchy_pivot = hierarchy_pivot.sort_values(['Industry', 'Annual Revenue'], ascending=[True, False])

        hierarchy_file = get_output_path(base_name, 'seasonality', '_seasonality_by_industry_subindustry.csv')
        hierarchy_pivot.to_csv(hierarchy_file, float_format='%.1f')
        print(f"  ✓ Industry/Sub-industry seasonality saved to: {hierarchy_file}")
        output_files['industry_subindustry'] = hierarchy_file

    # === REGION SEASONALITY ===
    if 'Region' in booking_df.columns:
        # Filter to rows with valid region
        region_df = booking_df[booking_df['Region'].notna() & (booking_df['Region'] != 'Unknown')]

        if len(region_df) > 0:
            region_monthly = region_df.groupby(['Region', 'Month']).agg({
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'EventId': 'nunique'
            }).reset_index()
            region_monthly.columns = ['Region', 'Month', 'Revenue', 'Tickets', 'Events']

            # Calculate each region's annual total
            region_annual = region_monthly.groupby('Region')['Revenue'].sum().reset_index()
            region_annual.columns = ['Region', 'Annual Revenue']

            region_monthly = region_monthly.merge(region_annual, on='Region')
            region_monthly['% of Annual'] = round(region_monthly['Revenue'] / region_monthly['Annual Revenue'] * 100, 1)
            region_monthly['Variance %'] = round(region_monthly['% of Annual'] - 8.33, 1)
            region_monthly['Status'] = 'Normal'
            region_monthly.loc[region_monthly['Variance %'] <= -2, 'Status'] = 'DIP'
            region_monthly.loc[region_monthly['Variance %'] >= 2, 'Status'] = 'PEAK'
            region_monthly['Month Name'] = region_monthly['Month'].apply(lambda m: calendar.month_abbr[m])

            # Pivot for overview
            region_pivot = region_monthly.pivot(index='Region', columns='Month Name', values='% of Annual')
            available_months = [m for m in month_order if m in region_pivot.columns]
            region_pivot = region_pivot[available_months]
            region_pivot = region_pivot.merge(region_annual.set_index('Region'), left_index=True, right_index=True)
            region_pivot['Volatility'] = region_pivot[[c for c in region_pivot.columns if c in month_order]].std(axis=1).round(1)
            region_pivot = region_pivot.sort_values('Annual Revenue', ascending=False)

            region_file = get_output_path(base_name, 'seasonality', '_seasonality_by_region.csv')
            region_pivot.to_csv(region_file, float_format='%.1f')
            print(f"  ✓ Region seasonality saved to: {region_file}")
            output_files['region'] = region_file

            # Detailed view
            region_detail_file = get_output_path(base_name, 'seasonality', '_seasonality_region_detail.csv')
            region_monthly_out = region_monthly[['Region', 'Month', 'Month Name', 'Revenue', 'Tickets', 'Events', '% of Annual', 'Variance %', 'Status']]
            region_monthly_out = region_monthly_out.sort_values(['Region', 'Month'])
            region_monthly_out = region_monthly_out.drop(columns=['Month'])
            region_monthly_out.to_csv(region_detail_file, index=False, float_format='%.1f')
            print(f"  ✓ Region seasonality detail saved to: {region_detail_file}")
            output_files['region_detail'] = region_detail_file

    # === INDUSTRY x REGION MATRIX ===
    if 'Industry' in booking_df.columns and 'Region' in booking_df.columns:
        # Filter to valid data
        matrix_df = booking_df[
            booking_df['Industry'].notna() &
            booking_df['Region'].notna() &
            (booking_df['Region'] != 'Unknown')
        ]

        if len(matrix_df) > 0:
            # Aggregate by industry and region
            ind_region = matrix_df.groupby(['Industry', 'Region']).agg({
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'EventId': 'nunique',
                'AccountId': 'nunique'
            }).reset_index()
            ind_region.columns = ['Industry', 'Region', 'Revenue', 'Tickets', 'Events', 'Accounts']

            # Create revenue matrix (Industry rows, Region columns)
            revenue_matrix = ind_region.pivot(index='Industry', columns='Region', values='Revenue').fillna(0)

            # Add row totals
            revenue_matrix['Total'] = revenue_matrix.sum(axis=1)

            # Sort by total revenue
            revenue_matrix = revenue_matrix.sort_values('Total', ascending=False)

            # Add column totals as a row
            col_totals = revenue_matrix.sum(axis=0)
            col_totals.name = 'Total'
            revenue_matrix = pd.concat([revenue_matrix, col_totals.to_frame().T])

            matrix_file = get_output_path(base_name, 'industry', '_industry_region_revenue_matrix.csv')
            revenue_matrix.to_csv(matrix_file, float_format='%.0f')
            print(f"  ✓ Industry x Region revenue matrix saved to: {matrix_file}")
            output_files['industry_region_revenue'] = matrix_file

            # Also create a tickets matrix
            tickets_matrix = ind_region.pivot(index='Industry', columns='Region', values='Tickets').fillna(0)
            tickets_matrix['Total'] = tickets_matrix.sum(axis=1)
            tickets_matrix = tickets_matrix.sort_values('Total', ascending=False)
            col_totals = tickets_matrix.sum(axis=0)
            col_totals.name = 'Total'
            tickets_matrix = pd.concat([tickets_matrix, col_totals.to_frame().T])

            tickets_file = get_output_path(base_name, 'industry', '_industry_region_tickets_matrix.csv')
            tickets_matrix.to_csv(tickets_file, float_format='%.0f')
            print(f"  ✓ Industry x Region tickets matrix saved to: {tickets_file}")
            output_files['industry_region_tickets'] = tickets_file

            # Percentage matrix - what % of each industry's revenue comes from each region
            pct_matrix = ind_region.pivot(index='Industry', columns='Region', values='Revenue').fillna(0)
            row_totals = pct_matrix.sum(axis=1)
            pct_matrix = pct_matrix.div(row_totals, axis=0) * 100
            pct_matrix['Total Revenue'] = row_totals
            pct_matrix = pct_matrix.sort_values('Total Revenue', ascending=False)

            pct_file = get_output_path(base_name, 'industry', '_industry_region_pct_matrix.csv')
            pct_matrix.to_csv(pct_file, float_format='%.1f')
            print(f"  ✓ Industry x Region percentage matrix saved to: {pct_file}")
            output_files['industry_region_pct'] = pct_file

    # === EVENT TYPE (KEYWORD) SEASONALITY ===
    # Use keywords from event names to identify event types
    from modules.event_keyword_analysis import extract_keywords

    # Get unique events with their keywords
    events_df = booking_df.groupby('EventId').agg({
        'EventName': 'first',
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'Month': 'first'
    }).reset_index()

    events_df['keywords'] = events_df['EventName'].apply(extract_keywords)

    # Explode keywords and aggregate by keyword + month
    keyword_rows = []
    for _, row in events_df.iterrows():
        for kw in row['keywords']:
            keyword_rows.append({
                'Keyword': kw,
                'Month': row['Month'],
                'Revenue': row['PaymentReceived'],
                'Tickets': row['TicketQuantity']
            })

    if keyword_rows:
        keyword_df = pd.DataFrame(keyword_rows)
        keyword_monthly = keyword_df.groupby(['Keyword', 'Month']).agg({
            'Revenue': 'sum',
            'Tickets': 'sum'
        }).reset_index()

        # Get top 50 keywords by total revenue
        keyword_totals = keyword_monthly.groupby('Keyword')['Revenue'].sum().reset_index()
        keyword_totals.columns = ['Keyword', 'Annual Revenue']
        top_keywords = keyword_totals.nlargest(50, 'Annual Revenue')['Keyword'].tolist()

        keyword_monthly = keyword_monthly[keyword_monthly['Keyword'].isin(top_keywords)]
        keyword_monthly = keyword_monthly.merge(keyword_totals, on='Keyword')

        keyword_monthly['% of Annual'] = round(keyword_monthly['Revenue'] / keyword_monthly['Annual Revenue'] * 100, 1)
        keyword_monthly['Variance %'] = round(keyword_monthly['% of Annual'] - 8.33, 1)
        keyword_monthly['Status'] = 'Normal'
        keyword_monthly.loc[keyword_monthly['Variance %'] <= -2, 'Status'] = 'DIP'
        keyword_monthly.loc[keyword_monthly['Variance %'] >= 2, 'Status'] = 'PEAK'
        keyword_monthly['Month Name'] = keyword_monthly['Month'].apply(lambda m: calendar.month_abbr[m])

        # Pivot for overview
        keyword_pivot = keyword_monthly.pivot(index='Keyword', columns='Month Name', values='% of Annual')
        available_months = [m for m in month_order if m in keyword_pivot.columns]
        keyword_pivot = keyword_pivot[available_months]
        keyword_pivot = keyword_pivot.merge(keyword_totals.set_index('Keyword'), left_index=True, right_index=True)
        keyword_pivot['Volatility'] = keyword_pivot[[c for c in keyword_pivot.columns if c in month_order]].std(axis=1).round(1)
        keyword_pivot = keyword_pivot.sort_values('Annual Revenue', ascending=False)

        keyword_file = get_output_path(base_name, 'seasonality', '_seasonality_by_event_type.csv')
        keyword_pivot.to_csv(keyword_file, float_format='%.1f')
        print(f"  ✓ Event type seasonality saved to: {keyword_file}")
        output_files['keyword'] = keyword_file

    return output_files


def generate_gateway_split_analysis_csv(booking_df, accounts_df, output_file):
    """
    Generate gateway split analysis showing payment gateway usage by region, industry, and cohort.
    Helps understand Stripe Connect vs TryBooking Gateway adoption patterns.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dict of output files generated
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    # Check for required columns
    gateway_col = None
    if 'GatewayGroup' in booking_df.columns:
        gateway_col = 'GatewayGroup'
    elif 'Gateway Group' in booking_df.columns:
        gateway_col = 'Gateway Group'

    if gateway_col is None:
        print("  Warning: No gateway column found, skipping gateway analysis")
        return output_files

    # Prepare data
    booking_df = booking_df.copy()

    # Normalise gateway names
    booking_df['Gateway'] = booking_df[gateway_col].fillna('Unknown')
    booking_df.loc[booking_df['Gateway'].str.contains('Default', case=False, na=False), 'Gateway'] = 'TryBooking Gateway'
    booking_df.loc[booking_df['Gateway'].str.contains('Stripe Connect', case=False, na=False), 'Gateway'] = 'Stripe Connect'

    # Calculate total fees
    fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    if all(col in booking_df.columns for col in fee_columns):
        booking_df['TotalFees'] = (
            booking_df['BookingFee'].fillna(0) +
            booking_df['CardFee'].fillna(0) +
            booking_df['ProcessingFee'].fillna(0) +
            booking_df['TicketFee'].fillna(0)
        )
    else:
        booking_df['TotalFees'] = 0

    # Get industry from accounts if not in bookings
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'
    if 'Industry' not in booking_df.columns and 'Industry' in accounts_df.columns:
        booking_df = booking_df.merge(
            accounts_df[[account_id_col, 'Industry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )

    # Add region data from postcodes
    postcode_col = None
    if 'EventPostcode' in booking_df.columns:
        postcode_col = 'EventPostcode'
    elif 'AccountPostcode' in booking_df.columns:
        postcode_col = 'AccountPostcode'

    if postcode_col:
        booking_df['PostcodeArea'] = extract_postcode_areas_vectorized(booking_df[postcode_col])
        booking_df['Region'] = get_regions_vectorized(booking_df['PostcodeArea'])
        booking_df.loc[~booking_df['PostcodeArea'].isin(VALID_UK_POSTCODE_AREAS), 'Region'] = None

    # Add account creation year for cohort analysis
    if 'DateTimeCreated' in accounts_df.columns:
        accounts_df = accounts_df.copy()
        accounts_df['CohortYear'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.year
        booking_df = booking_df.merge(
            accounts_df[[account_id_col, 'CohortYear']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )

    # === GATEWAY BY REGION ===
    if 'Region' in booking_df.columns:
        region_df = booking_df[booking_df['Region'].notna() & (booking_df['Region'] != 'Unknown')]

        if len(region_df) > 0:
            # Aggregate by region and gateway
            region_gateway = region_df.groupby(['Region', 'Gateway']).agg({
                'TotalFees': 'sum',
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'AccountId': 'nunique'
            }).reset_index()
            region_gateway.columns = ['Region', 'Gateway', 'Fees', 'Revenue', 'Tickets', 'Accounts']

            # Create pivot table - fees by gateway per region
            fees_pivot = region_gateway.pivot(index='Region', columns='Gateway', values='Fees').fillna(0)
            fees_pivot['Total'] = fees_pivot.sum(axis=1)

            # Calculate percentages
            for col in fees_pivot.columns:
                if col != 'Total':
                    fees_pivot[f'{col} %'] = (fees_pivot[col] / fees_pivot['Total'] * 100).round(1)

            fees_pivot = fees_pivot.sort_values('Total', ascending=False)

            region_file = get_output_path(base_name, 'geography', '_gateway_by_region.csv')
            fees_pivot.to_csv(region_file, float_format='%.2f')
            print(f"  ✓ Gateway by region saved to: {region_file}")
            output_files['region'] = region_file

    # === GATEWAY BY INDUSTRY ===
    if 'Industry' in booking_df.columns:
        industry_df = booking_df[booking_df['Industry'].notna()]

        if len(industry_df) > 0:
            # Aggregate by industry and gateway
            industry_gateway = industry_df.groupby(['Industry', 'Gateway']).agg({
                'TotalFees': 'sum',
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'AccountId': 'nunique'
            }).reset_index()
            industry_gateway.columns = ['Industry', 'Gateway', 'Fees', 'Revenue', 'Tickets', 'Accounts']

            # Create pivot table - fees by gateway per industry
            fees_pivot = industry_gateway.pivot(index='Industry', columns='Gateway', values='Fees').fillna(0)
            fees_pivot['Total'] = fees_pivot.sum(axis=1)

            # Calculate percentages
            for col in fees_pivot.columns:
                if col != 'Total':
                    fees_pivot[f'{col} %'] = (fees_pivot[col] / fees_pivot['Total'] * 100).round(1)

            fees_pivot = fees_pivot.sort_values('Total', ascending=False)

            industry_file = get_output_path(base_name, 'industry', '_gateway_by_industry.csv')
            fees_pivot.to_csv(industry_file, float_format='%.2f')
            print(f"  ✓ Gateway by industry saved to: {industry_file}")
            output_files['industry'] = industry_file

            # Also create accounts pivot - shows adoption rate
            accounts_pivot = industry_gateway.pivot(index='Industry', columns='Gateway', values='Accounts').fillna(0)
            accounts_pivot['Total'] = accounts_pivot.sum(axis=1)

            for col in accounts_pivot.columns:
                if col != 'Total':
                    accounts_pivot[f'{col} %'] = (accounts_pivot[col] / accounts_pivot['Total'] * 100).round(1)

            accounts_pivot = accounts_pivot.sort_values('Total', ascending=False)

            accounts_file = get_output_path(base_name, 'industry', '_gateway_accounts_by_industry.csv')
            accounts_pivot.to_csv(accounts_file, float_format='%.0f')
            print(f"  ✓ Gateway accounts by industry saved to: {accounts_file}")
            output_files['industry_accounts'] = accounts_file

    # === GATEWAY BY COHORT YEAR ===
    if 'CohortYear' in booking_df.columns:
        cohort_df = booking_df[booking_df['CohortYear'].notna()]

        if len(cohort_df) > 0:
            # Aggregate by cohort year and gateway
            cohort_gateway = cohort_df.groupby(['CohortYear', 'Gateway']).agg({
                'TotalFees': 'sum',
                'PaymentReceived': 'sum',
                'TicketQuantity': 'sum',
                'AccountId': 'nunique'
            }).reset_index()
            cohort_gateway.columns = ['Cohort Year', 'Gateway', 'Fees', 'Revenue', 'Tickets', 'Accounts']
            cohort_gateway['Cohort Year'] = cohort_gateway['Cohort Year'].astype(int)

            # Create pivot table - fees by gateway per cohort
            fees_pivot = cohort_gateway.pivot(index='Cohort Year', columns='Gateway', values='Fees').fillna(0)
            fees_pivot['Total'] = fees_pivot.sum(axis=1)

            # Calculate percentages
            for col in fees_pivot.columns:
                if col != 'Total':
                    fees_pivot[f'{col} %'] = (fees_pivot[col] / fees_pivot['Total'] * 100).round(1)

            fees_pivot = fees_pivot.sort_index(ascending=False)

            cohort_file = get_output_path(base_name, 'cohorts', '_gateway_by_cohort.csv')
            fees_pivot.to_csv(cohort_file, float_format='%.2f')
            print(f"  ✓ Gateway by cohort saved to: {cohort_file}")
            output_files['cohort'] = cohort_file

            # Accounts pivot for cohort - shows when Stripe adoption increased
            accounts_pivot = cohort_gateway.pivot(index='Cohort Year', columns='Gateway', values='Accounts').fillna(0)
            accounts_pivot['Total'] = accounts_pivot.sum(axis=1)

            for col in accounts_pivot.columns:
                if col != 'Total':
                    accounts_pivot[f'{col} %'] = (accounts_pivot[col] / accounts_pivot['Total'] * 100).round(1)

            accounts_pivot = accounts_pivot.sort_index(ascending=False)

            cohort_accounts_file = get_output_path(base_name, 'cohorts', '_gateway_accounts_by_cohort.csv')
            accounts_pivot.to_csv(cohort_accounts_file, float_format='%.0f')
            print(f"  ✓ Gateway accounts by cohort saved to: {cohort_accounts_file}")
            output_files['cohort_accounts'] = cohort_accounts_file

    # === GATEWAY BY INDUSTRY x REGION (DETAILED) ===
    if 'Industry' in booking_df.columns and 'Region' in booking_df.columns:
        detail_df = booking_df[
            booking_df['Industry'].notna() &
            booking_df['Region'].notna() &
            (booking_df['Region'] != 'Unknown')
        ]

        if len(detail_df) > 0:
            # Aggregate by industry, region and gateway
            detail_agg = detail_df.groupby(['Industry', 'Region', 'Gateway']).agg({
                'TotalFees': 'sum',
                'AccountId': 'nunique'
            }).reset_index()
            detail_agg.columns = ['Industry', 'Region', 'Gateway', 'Fees', 'Accounts']

            # Calculate totals for percentage
            industry_region_totals = detail_agg.groupby(['Industry', 'Region'])['Fees'].sum().reset_index()
            industry_region_totals.columns = ['Industry', 'Region', 'Total Fees']

            detail_agg = detail_agg.merge(industry_region_totals, on=['Industry', 'Region'])
            detail_agg['Gateway %'] = (detail_agg['Fees'] / detail_agg['Total Fees'] * 100).round(1)

            # Sort by industry, region, then fees
            detail_agg = detail_agg.sort_values(['Industry', 'Region', 'Fees'], ascending=[True, True, False])

            detail_file = get_output_path(base_name, 'industry', '_gateway_by_industry_region.csv')
            detail_agg.to_csv(detail_file, index=False, float_format='%.2f')
            print(f"  ✓ Gateway by industry x region saved to: {detail_file}")
            output_files['industry_region'] = detail_file

    # === OVERALL SUMMARY ===
    overall = booking_df.groupby('Gateway').agg({
        'TotalFees': 'sum',
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'AccountId': 'nunique',
        'EventId': 'nunique'
    }).reset_index()
    overall.columns = ['Gateway', 'Total Fees', 'Total Revenue', 'Total Tickets', 'Unique Accounts', 'Unique Events']

    total_fees = overall['Total Fees'].sum()
    overall['Fees %'] = (overall['Total Fees'] / total_fees * 100).round(1)
    overall = overall.sort_values('Total Fees', ascending=False)

    summary_file = get_output_path(base_name, 'planning', '_gateway_summary.csv')
    overall.to_csv(summary_file, index=False, float_format='%.2f')
    print(f"  ✓ Gateway summary saved to: {summary_file}")
    output_files['summary'] = summary_file

    return output_files


def generate_expansion_revenue_analysis_csv(booking_df, accounts_df, output_file):
    """
    Analyse revenue growth breakdown: new accounts vs existing account expansion.
    Shows how much growth comes from acquisition vs growing existing customers.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    base_name = output_file.rsplit('.', 1)[0]

    # Ensure we have dates
    if 'TransactionDate' not in booking_df.columns or 'DateTimeCreated' not in accounts_df.columns:
        print("  Warning: Missing date columns for expansion analysis")
        return None

    booking_df = booking_df.copy()
    booking_df['YearMonth'] = pd.to_datetime(booking_df['TransactionDate']).dt.to_period('M')

    accounts_df = accounts_df.copy()
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'
    accounts_df['CreatedYearMonth'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.to_period('M')

    # Create account lookup for creation month
    account_created = accounts_df.set_index(account_id_col)['CreatedYearMonth'].to_dict()

    # Classify each transaction
    def classify_revenue(row):
        account_id = row['AccountId']
        txn_month = row['YearMonth']

        created_month = account_created.get(account_id)
        if created_month is None:
            return 'Unknown'

        months_since_creation = (txn_month.year - created_month.year) * 12 + (txn_month.month - created_month.month)

        if months_since_creation <= 0:
            return 'New Account (Month 0)'
        elif months_since_creation <= 3:
            return 'Ramping (Months 1-3)'
        elif months_since_creation <= 12:
            return 'First Year (Months 4-12)'
        else:
            return 'Mature (Year 2+)'

    booking_df['Revenue Type'] = booking_df.apply(classify_revenue, axis=1)

    # Add sales channel classification
    booking_df = add_sales_channel_column(booking_df)
    has_sales_channel = 'Sales_Channel' in booking_df.columns

    # Aggregate by month and revenue type
    agg_dict = {
        'PaymentReceived': 'sum',
        'AccountId': 'nunique'
    }
    if has_sales_channel and 'TotalFees' in booking_df.columns:
        agg_dict['TotalFees'] = 'sum'
        agg_dict['TicketQuantity'] = 'sum'

    monthly_breakdown = booking_df.groupby(['YearMonth', 'Revenue Type']).agg(agg_dict).reset_index()
    monthly_breakdown.columns = ['YearMonth', 'Revenue Type', 'Revenue', 'Accounts'] + \
                                 (['Fees', 'Tickets'] if has_sales_channel and 'TotalFees' in booking_df.columns else [])

    # Add Box Office breakdown if available
    if has_sales_channel:
        bo_breakdown = booking_df[booking_df['Sales_Channel'] == 'Box Office'].groupby(['YearMonth', 'Revenue Type']).agg({
            'TicketQuantity': 'sum',
            'TotalFees': 'sum'
        }).reset_index()
        bo_breakdown.columns = ['YearMonth', 'Revenue Type', 'Box Office Tickets', 'Box Office Fees']
        monthly_breakdown = monthly_breakdown.merge(bo_breakdown, on=['YearMonth', 'Revenue Type'], how='left')
        monthly_breakdown['Box Office Tickets'] = monthly_breakdown['Box Office Tickets'].fillna(0).astype(int)
        monthly_breakdown['Box Office Fees'] = monthly_breakdown['Box Office Fees'].fillna(0)

    # Pivot for easier reading
    revenue_pivot = monthly_breakdown.pivot(index='YearMonth', columns='Revenue Type', values='Revenue').fillna(0)

    # Calculate totals and percentages
    revenue_pivot['Total'] = revenue_pivot.sum(axis=1)

    for col in revenue_pivot.columns:
        if col != 'Total':
            revenue_pivot[f'{col} %'] = round(revenue_pivot[col] / revenue_pivot['Total'] * 100, 1)

    # Calculate YoY comparison
    revenue_pivot = revenue_pivot.reset_index()
    revenue_pivot['YearMonth'] = revenue_pivot['YearMonth'].astype(str)

    # Also create accounts pivot
    accounts_pivot = monthly_breakdown.pivot(index='YearMonth', columns='Revenue Type', values='Accounts').fillna(0)
    accounts_pivot = accounts_pivot.reset_index()
    accounts_pivot['YearMonth'] = accounts_pivot['YearMonth'].astype(str)

    # Save revenue breakdown
    revenue_file = get_output_path(base_name, 'cohorts', '_expansion_revenue.csv')
    revenue_pivot.to_csv(revenue_file, index=False, float_format='%.2f')
    print(f"  ✓ Expansion revenue analysis saved to: {revenue_file}")

    # Save accounts breakdown
    accounts_file = get_output_path(base_name, 'cohorts', '_expansion_accounts.csv')
    accounts_pivot.to_csv(accounts_file, index=False, float_format='%.0f')
    print(f"  ✓ Expansion accounts analysis saved to: {accounts_file}")

    # Create summary showing growth composition
    # Compare current year to previous year
    revenue_pivot['Year'] = revenue_pivot['YearMonth'].str[:4]
    yearly_summary = revenue_pivot.groupby('Year').agg({
        col: 'sum' for col in revenue_pivot.columns if col not in ['YearMonth', 'Year'] and '%' not in col
    }).reset_index()

    # Recalculate percentages for yearly
    for col in yearly_summary.columns:
        if col not in ['Year', 'Total'] and '%' not in col:
            yearly_summary[f'{col} %'] = round(yearly_summary[col] / yearly_summary['Total'] * 100, 1)

    summary_file = get_output_path(base_name, 'cohorts', '_expansion_yearly_summary.csv')
    yearly_summary.to_csv(summary_file, index=False, float_format='%.2f')
    print(f"  ✓ Expansion yearly summary saved to: {summary_file}")

    return revenue_file


def generate_cohort_revenue_curves_csv(booking_df, accounts_df, output_file):
    """
    Generate cohort revenue curves showing revenue trajectory by month-of-life.
    Enables YoY cohort comparison to see if newer cohorts perform better/worse.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Path to generated CSV file
    """
    base_name = output_file.rsplit('.', 1)[0]

    # Ensure we have dates
    if 'TransactionDate' not in booking_df.columns or 'DateTimeCreated' not in accounts_df.columns:
        print("  Warning: Missing date columns for cohort analysis")
        return None

    booking_df = booking_df.copy()
    accounts_df = accounts_df.copy()

    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'

    # Get account creation dates
    accounts_df['CohortMonth'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.to_period('M')
    account_cohorts = accounts_df.set_index(account_id_col)['CohortMonth'].to_dict()

    # Add cohort info to bookings
    booking_df['CohortMonth'] = booking_df['AccountId'].map(account_cohorts)
    booking_df['TransactionMonth'] = pd.to_datetime(booking_df['TransactionDate']).dt.to_period('M')

    # Filter out bookings without cohort info
    booking_df = booking_df[booking_df['CohortMonth'].notna()]

    # Calculate month-of-life for each transaction
    def calc_month_of_life(row):
        if pd.isna(row['CohortMonth']) or pd.isna(row['TransactionMonth']):
            return None
        cohort = row['CohortMonth']
        txn = row['TransactionMonth']
        return (txn.year - cohort.year) * 12 + (txn.month - cohort.month)

    booking_df['MonthOfLife'] = booking_df.apply(calc_month_of_life, axis=1)
    booking_df = booking_df[booking_df['MonthOfLife'].notna() & (booking_df['MonthOfLife'] >= 0)]
    booking_df['MonthOfLife'] = booking_df['MonthOfLife'].astype(int)

    # Cap at 24 months for cleaner analysis
    booking_df = booking_df[booking_df['MonthOfLife'] <= 24]

    # Add sales channel classification
    booking_df = add_sales_channel_column(booking_df)
    has_sales_channel = 'Sales_Channel' in booking_df.columns

    # Aggregate by cohort and month-of-life
    cohort_curves = booking_df.groupby(['CohortMonth', 'MonthOfLife']).agg({
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'AccountId': 'nunique'
    }).reset_index()
    cohort_curves.columns = ['Cohort', 'Month of Life', 'Revenue', 'Tickets', 'Active Accounts']

    # Add Box Office metrics if available
    if has_sales_channel:
        bo_curves = booking_df[booking_df['Sales_Channel'] == 'Box Office'].groupby(['CohortMonth', 'MonthOfLife']).agg({
            'TicketQuantity': 'sum',
            'TotalFees': 'sum'
        }).reset_index()
        bo_curves.columns = ['Cohort', 'Month of Life', 'Box Office Tickets', 'Box Office Fees']

        cohort_curves = cohort_curves.merge(bo_curves, on=['Cohort', 'Month of Life'], how='left')
        cohort_curves['Box Office Tickets'] = cohort_curves['Box Office Tickets'].fillna(0).astype(int)
        cohort_curves['Box Office Fees'] = cohort_curves['Box Office Fees'].fillna(0)
        cohort_curves['Box Office Pct Tickets'] = np.where(
            cohort_curves['Tickets'] > 0,
            round(cohort_curves['Box Office Tickets'] / cohort_curves['Tickets'] * 100, 1),
            0
        )

    # Get cohort sizes (total accounts in each cohort)
    cohort_sizes = accounts_df.groupby('CohortMonth').size().reset_index()
    cohort_sizes.columns = ['Cohort', 'Cohort Size']

    cohort_curves = cohort_curves.merge(cohort_sizes, on='Cohort')
    cohort_curves['Revenue per Account'] = round(cohort_curves['Revenue'] / cohort_curves['Cohort Size'], 2)
    cohort_curves['Activation Rate %'] = round(cohort_curves['Active Accounts'] / cohort_curves['Cohort Size'] * 100, 1)

    # Calculate cumulative revenue per account
    cohort_curves = cohort_curves.sort_values(['Cohort', 'Month of Life'])
    cohort_curves['Cumulative Revenue'] = cohort_curves.groupby('Cohort')['Revenue'].cumsum()
    cohort_curves['Cumulative Revenue per Account'] = round(cohort_curves['Cumulative Revenue'] / cohort_curves['Cohort Size'], 2)

    # Convert cohort to string for CSV
    cohort_curves['Cohort'] = cohort_curves['Cohort'].astype(str)

    # Save detailed curves
    curves_file = get_output_path(base_name, 'cohorts', '_cohort_curves.csv')
    cohort_curves.to_csv(curves_file, index=False, float_format='%.2f')
    print(f"  ✓ Cohort revenue curves saved to: {curves_file}")

    # Create pivot table showing cumulative revenue per account at key milestones
    milestones = [0, 1, 3, 6, 12, 24]
    milestone_data = cohort_curves[cohort_curves['Month of Life'].isin(milestones)]
    milestone_pivot = milestone_data.pivot(
        index='Cohort',
        columns='Month of Life',
        values='Cumulative Revenue per Account'
    )
    milestone_pivot.columns = [f'Month {m}' for m in milestone_pivot.columns]

    # Add cohort size
    milestone_pivot = milestone_pivot.merge(
        cohort_sizes.set_index(cohort_sizes['Cohort'].astype(str))['Cohort Size'],
        left_index=True,
        right_index=True
    )

    milestone_file = get_output_path(base_name, 'cohorts', '_cohort_milestones.csv')
    milestone_pivot.to_csv(milestone_file, float_format='%.2f')
    print(f"  ✓ Cohort milestones saved to: {milestone_file}")

    # Create YoY comparison (same month cohorts across years)
    cohort_curves['Cohort Year'] = cohort_curves['Cohort'].str[:4]
    cohort_curves['Cohort Month Num'] = cohort_curves['Cohort'].str[5:7]

    # Compare cohorts by their month (e.g., all January cohorts)
    yoy_comparison = cohort_curves.groupby(['Cohort Month Num', 'Cohort Year', 'Month of Life']).agg({
        'Revenue per Account': 'mean',
        'Cumulative Revenue per Account': 'mean',
        'Activation Rate %': 'mean'
    }).reset_index()

    yoy_file = get_output_path(base_name, 'cohorts', '_cohort_yoy_comparison.csv')
    yoy_comparison.to_csv(yoy_file, index=False, float_format='%.2f')
    print(f"  ✓ Cohort YoY comparison saved to: {yoy_file}")

    return curves_file


def calculate_easter_date(year: int) -> tuple:
    """
    Calculate Easter Sunday date using the Anonymous Gregorian algorithm.

    Returns:
        Tuple of (month, day) for Easter Sunday
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return (month, day)


def get_school_holiday_flags(year: int, month: int) -> dict:
    """
    Get school holiday flags for a given month.

    Returns:
        Dictionary with holiday flags
    """
    easter_month, easter_day = calculate_easter_date(year)

    # Easter affects 2 weeks around Easter Sunday
    easter_start_month = 3 if (easter_month == 3 and easter_day > 14) or easter_month == 4 else easter_month
    easter_end_month = 4 if easter_month == 4 or (easter_month == 3 and easter_day > 20) else 3

    flags = {
        'Is Easter Month': month in [easter_start_month, easter_end_month] and easter_month in [3, 4],
        'Easter Position': 'Early' if easter_month == 3 else 'Late' if easter_month == 4 and easter_day > 15 else 'Mid',
        'Is Summer Holiday': month in [7, 8],
        'Is Half Term': month in [2, 5, 10],  # Feb, May, Oct half terms
        'Is Christmas Period': month in [12, 1],
        'Holiday Type': ''
    }

    # Set holiday type
    if flags['Is Easter Month']:
        flags['Holiday Type'] = 'Easter'
    elif flags['Is Summer Holiday']:
        flags['Holiday Type'] = 'Summer'
    elif flags['Is Half Term']:
        flags['Holiday Type'] = 'Half Term'
    elif flags['Is Christmas Period']:
        flags['Holiday Type'] = 'Christmas'
    else:
        flags['Holiday Type'] = 'Term Time'

    return flags


def calculate_logarithmic_growth_projection(growth_rates: np.ndarray, years_forward: int = 1) -> float:
    """
    Project future growth using logarithmic decay model.

    As businesses mature, YoY growth typically decreases proportionally (not linearly).
    This models the pattern where big drops happen early, smaller drops as you mature.

    Args:
        growth_rates: Array of historical YoY growth rates (oldest to newest)
        years_forward: How many years to project forward

    Returns:
        Projected growth rate for target year
    """
    if len(growth_rates) < 2:
        return growth_rates[-1] if len(growth_rates) == 1 else 0

    # Calculate the decay ratio between consecutive years
    # e.g., if growth went 50% -> 35% -> 25%, ratios are 0.7, 0.71
    decay_ratios = []
    for i in range(1, len(growth_rates)):
        if growth_rates[i-1] > 0:
            ratio = growth_rates[i] / growth_rates[i-1]
            # Clamp to reasonable range (0.5 to 1.1) to avoid outliers
            ratio = max(0.5, min(1.1, ratio))
            decay_ratios.append(ratio)

    if not decay_ratios:
        return growth_rates[-1]

    # Weight recent decay ratios more heavily
    weights = list(range(1, len(decay_ratios) + 1))
    weighted_decay = sum(r * w for r, w in zip(decay_ratios, weights)) / sum(weights)

    # Project forward using the decay ratio
    projected = growth_rates[-1]
    for _ in range(years_forward):
        projected = projected * weighted_decay

    # Floor at a minimum growth rate (businesses don't typically go to 0%)
    projected = max(projected, 5.0)  # 5% minimum growth floor

    return round(projected, 1)


def calculate_trend_based_growth(yearly_data: pd.DataFrame) -> dict:
    """
    Calculate recommended growth targets based on historical trend momentum.

    Uses logarithmic decay model to project growth, recognising that growth
    typically slows proportionally as businesses scale.

    Returns:
        Dictionary with recommended growth rates for each metric
    """
    recommendations = {}

    for metric in ['Total New Accounts', 'Total Ticket Revenue', 'Total Fees']:
        yoy_col = f'{metric} YoY %'
        if yoy_col in yearly_data.columns:
            # Get non-null growth rates
            growth_rates = yearly_data[yoy_col].dropna().values

            if len(growth_rates) >= 2:
                # Weighted average - more recent years get higher weight
                weights = list(range(1, len(growth_rates) + 1))
                weighted_avg = sum(g * w for g, w in zip(growth_rates, weights)) / sum(weights)

                # Calculate trend direction (acceleration/deceleration)
                trend = growth_rates[-1] - growth_rates[-2]

                # Use logarithmic decay projection for Base recommendation
                projected = calculate_logarithmic_growth_projection(growth_rates, years_forward=1)

                recommendations[metric] = {
                    'weighted_avg': round(weighted_avg, 1),
                    'most_recent': round(growth_rates[-1], 1),
                    'trend': round(trend, 1),
                    'projected': projected,  # Logarithmic projection
                    'recommended': projected,  # Use projection as Base
                    'growth_rates': [round(g, 1) for g in growth_rates],  # Store history
                }
            elif len(growth_rates) == 1:
                recommendations[metric] = {
                    'weighted_avg': round(growth_rates[0], 1),
                    'most_recent': round(growth_rates[0], 1),
                    'trend': 0,
                    'projected': round(growth_rates[0], 1),
                    'recommended': round(growth_rates[0], 1),
                    'growth_rates': [round(growth_rates[0], 1)],
                }

    return recommendations


def calculate_monthly_yoy_patterns(planning_df: pd.DataFrame, complete_years: list) -> dict:
    """
    Calculate monthly YoY growth patterns with recency weighting.

    Some months historically grow faster than others - this captures those patterns
    to vary targets by month rather than applying uniform growth.

    Args:
        planning_df: DataFrame with monthly data including YoY columns
        complete_years: List of years with complete data

    Returns:
        Dictionary with monthly growth patterns for each metric
    """
    if len(complete_years) < 2:
        return None

    # Get YoY data for complete years only
    yoy_data = planning_df[planning_df['Year'].isin(complete_years)].copy()

    results = {}
    for month in range(1, 13):
        month_data = yoy_data[yoy_data['Month'] == month]
        results[month] = {}

        for metric in ['Total New Accounts', 'Total Ticket Revenue', 'Total Fees']:
            yoy_col = f'{metric} YoY %'
            if yoy_col in month_data.columns:
                # Get YoY values for this month across years
                yoy_values = month_data[[yoy_col, 'Year']].dropna()

                if len(yoy_values) >= 1:
                    # Weight recent years more heavily (exponential weighting)
                    years = yoy_values['Year'].values
                    values = yoy_values[yoy_col].values

                    # Create weights: most recent year gets highest weight
                    max_year = max(years)
                    weights = [2 ** (y - max_year + len(years)) for y in years]

                    weighted_avg = sum(v * w for v, w in zip(values, weights)) / sum(weights)
                    results[month][metric] = round(weighted_avg, 1)

    return results


def calculate_trailing_momentum(planning_df: pd.DataFrame, base_year: int, window_months: int = 6) -> dict:
    """
    Calculate trailing momentum from H2 of base year to influence early target year months.

    This captures recent trends like SEO relaunches that should carry forward.

    Args:
        planning_df: DataFrame with monthly data
        base_year: The year before target year (e.g., 2025)
        window_months: Number of months to look back (default 6 = H2)

    Returns:
        Dictionary with momentum multipliers for each metric
    """
    # Get H2 data from base year (Jul-Dec)
    h2_months = list(range(13 - window_months, 13))  # [7, 8, 9, 10, 11, 12] for 6-month window
    h2_data = planning_df[(planning_df['Year'] == base_year) & (planning_df['Month'].isin(h2_months))]

    if len(h2_data) == 0:
        return None

    momentum = {}
    for metric in ['Total New Accounts', 'Total Ticket Revenue', 'Total Fees']:
        yoy_col = f'{metric} YoY %'
        if yoy_col in h2_data.columns:
            # Calculate average YoY growth in the trailing window
            avg_yoy = h2_data[yoy_col].dropna().mean()
            momentum[metric] = round(avg_yoy, 1) if not pd.isna(avg_yoy) else 0
        else:
            momentum[metric] = 0

    return momentum


def round_to_multiple(value: float, multiple: int = 50) -> int:
    """Round a value to the nearest multiple (default 50 for accounts)."""
    return int(round(value / multiple) * multiple)


def generate_planning_model_csv(results_df, accounts_df, booking_df, output_file, target_year=2026):
    """
    Generate comprehensive planning model for target setting.

    Features:
    - Year-aware seasonality (adjusts for Easter timing)
    - School holiday period flags
    - Scenario modelling (Base + BHAG)
    - Trend-based growth recommendations
    - BHAG cumulative tracking
    - Side-by-side comparison (previous year actuals vs targets)

    Note: This function builds its own complete historical data from booking_df
    rather than relying on results_df, to ensure all years are available
    regardless of the report mode (rolling, year, custom range).

    Args:
        results_df: DataFrame with monthly metrics (used for fallback only)
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        output_file: Base output filename
        target_year: Year to generate targets for (default 2026)

    Returns:
        Path to generated CSV file
    """
    base_name = output_file.rsplit('.', 1)[0]

    # Build complete historical data from booking_df (2022 onwards)
    # This ensures we have all years regardless of the report mode
    print("  Building complete historical data for planning model...")

    booking_df = booking_df.copy()
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df['Year'] = booking_df['TransactionDate'].dt.year
    booking_df['Month'] = booking_df['TransactionDate'].dt.month

    # Filter to 2022 onwards - earlier data not representative (COVID impact)
    booking_df = booking_df[booking_df['Year'] >= 2022]

    # Also need account creation dates for new accounts
    accounts_df = accounts_df.copy()
    accounts_df['DateTimeCreated'] = pd.to_datetime(accounts_df['DateTimeCreated'])
    accounts_df['Created_Year'] = accounts_df['DateTimeCreated'].dt.year
    accounts_df['Created_Month'] = accounts_df['DateTimeCreated'].dt.month

    # Filter to only Activated and InReview accounts for targets
    # Excludes Closed, Suspended, etc.
    if 'AccountStatus' in accounts_df.columns:
        valid_statuses = ['Activated', 'InReview']
        original_count = len(accounts_df)
        accounts_df = accounts_df[accounts_df['AccountStatus'].isin(valid_statuses)]
        print(f"  Filtered to {len(accounts_df):,} accounts (Activated/InReview) from {original_count:,} total")

    # Build monthly metrics from scratch
    monthly_data = []

    # Get all year-month combinations from 2022 to target_year - 1
    years = sorted(booking_df['Year'].unique())
    years = [y for y in years if y < target_year]  # Exclude target year

    for year in years:
        for month in range(1, 13):
            # Filter data for this month
            month_bookings = booking_df[(booking_df['Year'] == year) & (booking_df['Month'] == month)]
            month_accounts = accounts_df[(accounts_df['Created_Year'] == year) & (accounts_df['Created_Month'] == month)]

            if len(month_bookings) == 0 and len(month_accounts) == 0:
                continue  # Skip months with no data

            # Calculate metrics
            fee_cols = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
            total_fees = sum(month_bookings[col].fillna(0).sum() for col in fee_cols if col in month_bookings.columns)

            monthly_data.append({
                'Year': year,
                'Month': month,
                'Month Name': calendar.month_name[month],
                'Total New Accounts': len(month_accounts),
                'Total Ticket Revenue': month_bookings['PaymentReceived'].sum() if 'PaymentReceived' in month_bookings.columns else 0,
                'Total Fees': total_fees,
                'Total Tickets Sold': month_bookings['TicketQuantity'].sum() if 'TicketQuantity' in month_bookings.columns else 0,
            })

    if len(monthly_data) == 0:
        print("  Warning: No historical data available for planning model")
        return None

    planning_df = pd.DataFrame(monthly_data)
    print(f"  Using data from {planning_df['Year'].min()}-{planning_df['Year'].max()} ({len(planning_df)} months)")

    # === CALCULATE YEARLY TOTALS AND SEASONALITY ===
    planning_df['YearMonth'] = planning_df['Year'].astype(str) + '-' + planning_df['Month'].astype(str).str.zfill(2)

    yearly_totals = planning_df.groupby('Year').agg({
        'Total New Accounts': 'sum',
        'Total Ticket Revenue': 'sum',
        'Total Fees': 'sum',
        'Total Tickets Sold': 'sum',
    }).reset_index()
    yearly_totals.columns = ['Year', 'Year_Accounts', 'Year_Revenue', 'Year_Fees', 'Year_Tickets']

    planning_df = planning_df.merge(yearly_totals, on='Year')

    # Seasonality indices
    planning_df['Accounts Index %'] = round(planning_df['Total New Accounts'] / planning_df['Year_Accounts'] * 100, 2)
    planning_df['Revenue Index %'] = round(planning_df['Total Ticket Revenue'] / planning_df['Year_Revenue'] * 100, 2)
    planning_df['Fees Index %'] = round(planning_df['Total Fees'] / planning_df['Year_Fees'] * 100, 2)

    # === ADD EASTER AND HOLIDAY FLAGS ===
    planning_df['Easter Month'] = planning_df['Year'].apply(lambda y: calculate_easter_date(y)[0])
    planning_df['Easter Day'] = planning_df['Year'].apply(lambda y: calculate_easter_date(y)[1])
    planning_df['Easter Position'] = planning_df.apply(
        lambda r: 'Early (Mar)' if r['Easter Month'] == 3 else 'Late (Apr)' if r['Easter Day'] > 15 else 'Mid (Apr)',
        axis=1
    )

    # Add holiday flags
    for _, row in planning_df.iterrows():
        flags = get_school_holiday_flags(row['Year'], row['Month'])
        for flag, value in flags.items():
            if flag not in planning_df.columns:
                planning_df[flag] = None
            planning_df.loc[(planning_df['Year'] == row['Year']) & (planning_df['Month'] == row['Month']), flag] = value

    # === IDENTIFY COMPLETE YEARS FOR SEASONALITY ===
    # Complete years (all 12 months) are needed for accurate seasonality indices
    months_per_year = planning_df.groupby('Year')['Month'].nunique()
    complete_years = months_per_year[months_per_year == 12].index.tolist()
    print(f"  Complete years available: {complete_years}")

    # Use complete years for seasonality calculations
    if len(complete_years) > 0:
        complete_year_data = planning_df[planning_df['Year'].isin(complete_years)]
    else:
        complete_year_data = planning_df
        print("  Warning: No complete years - seasonality indices may be skewed")

    # === CALCULATE EASTER-ADJUSTED SEASONALITY ===
    # Group by Easter position to get adjusted indices (using complete years only)
    easter_adjusted = complete_year_data.groupby(['Month', 'Easter Position']).agg({
        'Accounts Index %': 'mean',
        'Revenue Index %': 'mean',
        'Fees Index %': 'mean',
    }).reset_index()
    easter_adjusted.columns = ['Month', 'Easter Position', 'Easter Adj Accounts %', 'Easter Adj Revenue %', 'Easter Adj Fees %']

    # Get target year's Easter position
    target_easter_month, target_easter_day = calculate_easter_date(target_year)
    target_easter_pos = 'Early (Mar)' if target_easter_month == 3 else 'Late (Apr)' if target_easter_day > 15 else 'Mid (Apr)'

    print(f"  Target year {target_year}: Easter is {target_easter_pos} (April {target_easter_day})" if target_easter_month == 4 else f"  Target year {target_year}: Easter is {target_easter_pos} (March {target_easter_day})")

    # === CALCULATE YOY GROWTH AND TRENDS ===
    planning_df = planning_df.sort_values(['Month', 'Year'])

    for metric in ['Total New Accounts', 'Total Ticket Revenue', 'Total Fees']:
        col_name = f'{metric} YoY %'
        planning_df[col_name] = planning_df.groupby('Month')[metric].pct_change() * 100
        planning_df[col_name] = planning_df[col_name].round(1)

    # === GROWTH TRENDS AND RECOMMENDATIONS ===
    yearly_growth = planning_df.groupby('Year').agg({
        'Total New Accounts': 'sum',
        'Total Ticket Revenue': 'sum',
        'Total Fees': 'sum',
    }).reset_index()

    for col in ['Total New Accounts', 'Total Ticket Revenue', 'Total Fees']:
        yearly_growth[f'{col} YoY %'] = round(yearly_growth[col].pct_change() * 100, 1)

    recommendations = calculate_trend_based_growth(yearly_growth)

    # === GET BASE YEAR (previous year) ACTUALS ===
    # For targets, we need a COMPLETE year of data (complete_years calculated above)
    base_year = target_year - 1

    if base_year in complete_years:
        base_year_data = planning_df[planning_df['Year'] == base_year].copy()
    elif len(complete_years) > 0:
        # Use most recent complete year
        base_year = max(complete_years)
        base_year_data = planning_df[planning_df['Year'] == base_year].copy()
        print(f"  Note: Using {base_year} as base year (most recent complete year)")
    else:
        # No complete years - use most recent year but warn
        available_years = planning_df['Year'].unique()
        base_year = max(available_years)
        base_year_data = planning_df[planning_df['Year'] == base_year].copy()
        months_available = len(base_year_data)
        print(f"  Warning: No complete year data. Using {base_year} with {months_available} months.")
        print(f"           Targets may be inaccurate. Run with --year for full year data.")

    # === BUILD SCENARIO TARGETS ===
    # Get average seasonality indices from complete years (already computed above)
    avg_indices = complete_year_data.groupby('Month').agg({
        'Accounts Index %': 'mean',
        'Revenue Index %': 'mean',
        'Fees Index %': 'mean',
        'Month Name': 'first',
    }).reset_index()

    # Try to use Easter-adjusted indices for the target year's Easter position
    for month in [3, 4]:  # March and April
        easter_match = easter_adjusted[
            (easter_adjusted['Month'] == month) &
            (easter_adjusted['Easter Position'] == target_easter_pos)
        ]
        if len(easter_match) > 0:
            avg_indices.loc[avg_indices['Month'] == month, 'Accounts Index %'] = easter_match['Easter Adj Accounts %'].values[0]
            avg_indices.loc[avg_indices['Month'] == month, 'Revenue Index %'] = easter_match['Easter Adj Revenue %'].values[0]
            avg_indices.loc[avg_indices['Month'] == month, 'Fees Index %'] = easter_match['Easter Adj Fees %'].values[0]

    # Base year totals
    base_accounts = base_year_data['Total New Accounts'].sum()
    base_revenue = base_year_data['Total Ticket Revenue'].sum()
    base_fees = base_year_data['Total Fees'].sum()

    # === CREATE TARGET SCENARIOS ===
    scenarios = pd.DataFrame()
    scenarios['Month'] = range(1, 13)
    scenarios['Month Name'] = scenarios['Month'].apply(lambda m: calendar.month_name[m])

    # Add holiday flags for target year
    for month in range(1, 13):
        flags = get_school_holiday_flags(target_year, month)
        for flag, value in flags.items():
            if flag not in scenarios.columns:
                scenarios[flag] = None
            scenarios.loc[scenarios['Month'] == month, flag] = value

    # Merge indices
    scenarios = scenarios.merge(avg_indices[['Month', 'Accounts Index %', 'Revenue Index %', 'Fees Index %']], on='Month')

    # Add base year actuals
    base_monthly = base_year_data[['Month', 'Total New Accounts', 'Total Ticket Revenue', 'Total Fees']].copy()
    base_monthly.columns = ['Month', f'{base_year} Accounts', f'{base_year} Revenue', f'{base_year} Fees']
    scenarios = scenarios.merge(base_monthly, on='Month', how='left')
    scenarios = scenarios.fillna(0)

    # === CALCULATE MONTHLY YOY PATTERNS ===
    # Get historical monthly YoY patterns with recency weighting
    monthly_yoy_patterns = calculate_monthly_yoy_patterns(planning_df, complete_years)

    # === CALCULATE TRAILING MOMENTUM ===
    # H2 of base year influences early target year months
    trailing_momentum = calculate_trailing_momentum(planning_df, base_year, window_months=6)
    if trailing_momentum:
        print(f"  Trailing momentum (H2 {base_year}):")
        for metric, growth in trailing_momentum.items():
            print(f"    {metric}: {growth}% avg YoY")

    # === GROWTH CAPS ===
    # Conservative growth assumptions for realistic planning
    MAX_MONTHLY_GROWTH = 0.20  # 20% cap for Base targets (15-20% annual)
    HISTORICAL_YOY_CAP = 0.25  # Cap historical YoY at 25% to reduce outlier impact

    # === BHAG CALCULATION ===
    # BHAG: 25,000 cumulative accounts by end of target year
    bhag_accounts_target = 25000
    total_accounts_to_date = len(accounts_df)  # Current cumulative total

    # Calculate how many NEW accounts needed in target year to hit BHAG
    bhag_new_accounts_needed = max(0, bhag_accounts_target - total_accounts_to_date)
    bhag_accounts_growth_required = round((bhag_new_accounts_needed / base_accounts - 1) * 100, 1) if base_accounts > 0 else 0

    print(f"  BHAG Analysis:")
    print(f"    Current cumulative accounts: {total_accounts_to_date:,}")
    print(f"    BHAG target: {bhag_accounts_target:,}")
    print(f"    New accounts needed in {target_year}: {bhag_new_accounts_needed:,}")
    print(f"    Required YoY growth vs {base_year}: {bhag_accounts_growth_required}%")

    # Calculate fees-per-new-account to derive fees BHAG
    fees_per_new_account = base_fees / base_accounts if base_accounts > 0 else 0
    bhag_fees_target = bhag_new_accounts_needed * fees_per_new_account
    bhag_fees_growth_required = round((bhag_fees_target / base_fees - 1) * 100, 1) if base_fees > 0 else 0

    print(f"    Derived fees BHAG: £{bhag_fees_target:,.2f} ({bhag_fees_growth_required}% YoY)")

    # === CALCULATE SCENARIO TARGETS (BASE + BHAG) ===
    # Each metric modelled independently with its own trend
    for metric, base_val, idx_col in [
        ('Accounts', base_accounts, 'Accounts Index %'),
        ('Revenue', base_revenue, 'Revenue Index %'),
        ('Fees', base_fees, 'Fees Index %')
    ]:
        metric_key = f'Total New {metric}' if metric == 'Accounts' else f'Total Ticket {metric}' if metric == 'Revenue' else f'Total {metric}'

        # Get annual growth projection from logarithmic decay model
        if metric_key in recommendations:
            rec = recommendations[metric_key]
            annual_base_growth = rec['recommended'] / 100
            print(f"    {metric}: projected {rec['recommended']}% (from {rec.get('growth_rates', [])})")
        else:
            annual_base_growth = 0.15  # Default

        # BHAG growth - back-calculated for accounts, derived for fees
        if metric == 'Accounts':
            annual_bhag_growth = bhag_accounts_growth_required / 100
        elif metric == 'Fees':
            annual_bhag_growth = bhag_fees_growth_required / 100
        else:
            # Revenue uses its own trend-based projection
            annual_bhag_growth = bhag_accounts_growth_required / 100

        # === ENSURE BASE HAS HEADROOM BELOW BHAG ===
        # Base should always be below BHAG - recalculate if needed
        if annual_base_growth >= annual_bhag_growth:
            # Calculate a dynamic gap based on the BHAG stretch
            # Minimum 10% gap, scaling with how ambitious BHAG is
            min_gap_ratio = 0.85  # Base should be at most 85% of BHAG
            annual_base_growth = annual_bhag_growth * min_gap_ratio
            print(f"    {metric}: Base capped at {round(annual_base_growth * 100, 1)}% to maintain headroom below BHAG")

        # === CALCULATE MONTHLY TARGETS ===
        # Hybrid approach: combine monthly YoY patterns with trailing momentum
        base_targets = []
        bhag_targets = []

        for month in range(1, 13):
            base_year_monthly = scenarios.loc[scenarios['Month'] == month, f'{base_year} {metric}'].values[0]

            # Skip if no base year data
            if base_year_monthly == 0:
                base_targets.append(0)
                bhag_targets.append(0)
                continue

            # Step 1: Get historical monthly YoY pattern (capped to reduce outlier impact)
            historical_yoy = 0
            if monthly_yoy_patterns and month in monthly_yoy_patterns and metric_key in monthly_yoy_patterns[month]:
                raw_historical = monthly_yoy_patterns[month][metric_key] / 100
                # Cap historical YoY at 25% to prevent outlier months inflating targets
                historical_yoy = min(raw_historical, HISTORICAL_YOY_CAP)

            # Step 2: Get trailing momentum (for Q1, weight H2 momentum more heavily)
            momentum_yoy = 0
            if trailing_momentum and metric_key in trailing_momentum:
                momentum_yoy = trailing_momentum[metric_key] / 100
                # Q1 months (Jan-Mar) get stronger momentum influence
                if month <= 3:
                    momentum_weight = 0.6  # 60% momentum, 40% historical
                elif month <= 6:
                    momentum_weight = 0.4  # 40% momentum, 60% historical
                else:
                    momentum_weight = 0.2  # 20% momentum, 80% historical (H2 reverts to patterns)
            else:
                momentum_weight = 0

            # Step 3: Blend historical pattern with momentum
            blended_yoy = (historical_yoy * (1 - momentum_weight)) + (momentum_yoy * momentum_weight)

            # Step 4: Apply 20% cap and 0% floor to Base targets
            # Floor ensures no negative growth (targets never below previous year)
            capped_base_yoy = max(min(blended_yoy, MAX_MONTHLY_GROWTH), 0.0)

            # Step 5: Calculate Base target with cap applied
            base_monthly_target = base_year_monthly * (1 + capped_base_yoy)

            # Step 6: Calculate BHAG target (stretch goal, can reach ~30%)
            # BHAG uses the capped historical YoY scaled towards BHAG requirement
            bhag_scale = annual_bhag_growth / max(annual_base_growth, 0.01) if annual_base_growth > 0 else 1.5
            # BHAG can stretch to 30% (50% above the 20% Base cap)
            bhag_yoy = max(min(blended_yoy * bhag_scale, 0.30), capped_base_yoy)  # At least match Base
            bhag_monthly_target = base_year_monthly * (1 + bhag_yoy)

            base_targets.append(base_monthly_target)
            bhag_targets.append(bhag_monthly_target)

        # Apply rounding for accounts (to nearest 50)
        if metric == 'Accounts':
            scenarios[f'{target_year} {metric} Base'] = [round_to_multiple(t, 50) for t in base_targets]
            scenarios[f'{target_year} {metric} Base Low'] = [round_to_multiple(t * 0.9, 50) for t in base_targets]
            scenarios[f'{target_year} {metric} Base High'] = [round_to_multiple(t * 1.1, 50) for t in base_targets]
            scenarios[f'{target_year} {metric} BHAG'] = [round_to_multiple(t, 50) for t in bhag_targets]
        else:
            scenarios[f'{target_year} {metric} Base'] = [round(t, 2) for t in base_targets]
            scenarios[f'{target_year} {metric} Base Low'] = [round(t * 0.9, 2) for t in base_targets]
            scenarios[f'{target_year} {metric} Base High'] = [round(t * 1.1, 2) for t in base_targets]
            scenarios[f'{target_year} {metric} BHAG'] = [round(t, 2) for t in bhag_targets]

        # === FINAL CHECK: Ensure Base <= BHAG for every month ===
        for i in range(len(scenarios)):
            base_val_month = scenarios.loc[i, f'{target_year} {metric} Base']
            bhag_val_month = scenarios.loc[i, f'{target_year} {metric} BHAG']
            if base_val_month > bhag_val_month:
                # Cap Base at BHAG minus a small gap
                if metric == 'Accounts':
                    scenarios.loc[i, f'{target_year} {metric} Base'] = round_to_multiple(bhag_val_month * 0.9, 50)
                else:
                    scenarios.loc[i, f'{target_year} {metric} Base'] = round(bhag_val_month * 0.9, 2)

        # YoY variance vs base year
        scenarios[f'{metric} Base vs {base_year} %'] = round(
            (scenarios[f'{target_year} {metric} Base'] - scenarios[f'{base_year} {metric}']) / scenarios[f'{base_year} {metric}'].replace(0, 1) * 100, 1
        )
        scenarios[f'{metric} BHAG vs {base_year} %'] = round(
            (scenarios[f'{target_year} {metric} BHAG'] - scenarios[f'{base_year} {metric}']) / scenarios[f'{base_year} {metric}'].replace(0, 1) * 100, 1
        )

    # === ADD CUMULATIVE TOTALS FOR BHAG TRACKING ===
    scenarios['Cumulative Accounts (Base)'] = scenarios[f'{target_year} Accounts Base'].cumsum() + total_accounts_to_date
    scenarios['Cumulative Accounts (BHAG)'] = scenarios[f'{target_year} Accounts BHAG'].cumsum() + total_accounts_to_date

    # BHAG milestone tracking
    scenarios['BHAG Progress %'] = round(scenarios['Cumulative Accounts (BHAG)'] / bhag_accounts_target * 100, 1)
    scenarios['BHAG Gap'] = bhag_accounts_target - scenarios['Cumulative Accounts (BHAG)']

    # === SAVE OUTPUTS ===

    # 1. Main scenario model (Base with ±10% confidence range + BHAG)
    scenario_cols = [
        'Month', 'Month Name', 'Holiday Type',
        f'{base_year} Accounts', f'{target_year} Accounts Base Low', f'{target_year} Accounts Base', f'{target_year} Accounts Base High', f'{target_year} Accounts BHAG', f'Accounts Base vs {base_year} %', f'Accounts BHAG vs {base_year} %',
        f'{base_year} Revenue', f'{target_year} Revenue Base Low', f'{target_year} Revenue Base', f'{target_year} Revenue Base High', f'{target_year} Revenue BHAG', f'Revenue Base vs {base_year} %', f'Revenue BHAG vs {base_year} %',
        f'{base_year} Fees', f'{target_year} Fees Base Low', f'{target_year} Fees Base', f'{target_year} Fees Base High', f'{target_year} Fees BHAG', f'Fees Base vs {base_year} %', f'Fees BHAG vs {base_year} %',
        'Cumulative Accounts (Base)', 'Cumulative Accounts (BHAG)', 'BHAG Progress %', 'BHAG Gap',
    ]
    scenario_cols = [c for c in scenario_cols if c in scenarios.columns]

    scenario_file = get_output_path(base_name, 'planning', f'_{target_year}_targets.csv')
    scenarios[scenario_cols].to_csv(scenario_file, index=False, float_format='%.2f')
    print(f"  ✓ {target_year} targets saved to: {scenario_file}")

    # 2. Growth recommendations summary (Base from logarithmic decay, BHAG back-calculated)
    rec_rows = []
    for metric, rec in recommendations.items():
        # Determine BHAG growth for this metric
        if 'Accounts' in metric:
            bhag_pct = bhag_accounts_growth_required
        elif 'Fees' in metric:
            bhag_pct = bhag_fees_growth_required
        else:
            bhag_pct = bhag_accounts_growth_required  # Revenue follows accounts

        rec_rows.append({
            'Metric': metric,
            'Historical YoY Rates': ' → '.join(f"{g}%" for g in rec.get('growth_rates', [])),
            'Most Recent YoY %': rec.get('most_recent', rec['weighted_avg']),
            'Trend (acceleration)': rec['trend'],
            'Projected (log decay) %': rec.get('projected', rec['recommended']),
            'Base (Recommended) %': rec['recommended'],
            'BHAG Required %': bhag_pct,
            'BHAG vs Base Gap': round(bhag_pct - rec['recommended'], 1),
        })

    if rec_rows:
        rec_df = pd.DataFrame(rec_rows)
        rec_file = get_output_path(base_name, 'planning', '_growth_recommendations.csv')
        rec_df.to_csv(rec_file, index=False, float_format='%.1f')
        print(f"  ✓ Growth recommendations saved to: {rec_file}")

    # 3. Annual summary (Base + BHAG only)
    annual_summary = {
        'Metric': ['New Accounts', 'Ticket Revenue', 'Fees'],
        f'{base_year} Actual': [int(base_accounts), round(base_revenue, 2), round(base_fees, 2)],
        f'{target_year} Base': [
            int(scenarios[f'{target_year} Accounts Base'].sum()),
            round(scenarios[f'{target_year} Revenue Base'].sum(), 2),
            round(scenarios[f'{target_year} Fees Base'].sum(), 2),
        ],
        f'{target_year} BHAG': [
            int(scenarios[f'{target_year} Accounts BHAG'].sum()),
            round(scenarios[f'{target_year} Revenue BHAG'].sum(), 2),
            round(scenarios[f'{target_year} Fees BHAG'].sum(), 2),
        ],
    }

    # Add growth percentages
    for scenario in ['Base', 'BHAG']:
        annual_summary[f'{scenario} Growth %'] = [
            round((annual_summary[f'{target_year} {scenario}'][i] - annual_summary[f'{base_year} Actual'][i]) / annual_summary[f'{base_year} Actual'][i] * 100, 1)
            for i in range(3)
        ]

    # Add BHAG gap (difference between BHAG and Base)
    annual_summary['BHAG vs Base Gap'] = [
        annual_summary[f'{target_year} BHAG'][i] - annual_summary[f'{target_year} Base'][i]
        for i in range(3)
    ]

    annual_df = pd.DataFrame(annual_summary)
    annual_file = get_output_path(base_name, 'planning', f'_{target_year}_annual_summary.csv')
    annual_df.to_csv(annual_file, index=False, float_format='%.2f')
    print(f"  ✓ Annual summary saved to: {annual_file}")

    # 4. Historical planning model (unchanged from before)
    planning_output_cols = [
        'Year', 'Month', 'Month Name', 'Holiday Type', 'Easter Position',
        'Total New Accounts', 'Total Ticket Revenue', 'Total Fees',
        'Accounts Index %', 'Revenue Index %', 'Fees Index %',
        'Total New Accounts YoY %', 'Total Ticket Revenue YoY %', 'Total Fees YoY %',
    ]
    planning_output_cols = [c for c in planning_output_cols if c in planning_df.columns]
    planning_output = planning_df[planning_output_cols].sort_values(['Year', 'Month'])

    model_file = get_output_path(base_name, 'planning', '_planning_model.csv')
    planning_output.to_csv(model_file, index=False, float_format='%.2f')
    print(f"  ✓ Historical planning model saved to: {model_file}")

    # 5. Industry segmentation with relative growth rates and unit economics
    print("  Generating industry segmentation...")

    # Get industry data from booking_df (filter to base year for current state)
    booking_df_filtered = booking_df.copy()
    booking_df_filtered['TransactionDate'] = pd.to_datetime(booking_df_filtered['TransactionDate'])
    booking_df_filtered['Year'] = booking_df_filtered['TransactionDate'].dt.year

    # Calculate industry metrics for base year
    base_year_bookings = booking_df_filtered[booking_df_filtered['Year'] == base_year]

    if len(base_year_bookings) > 0 and 'Industry' in base_year_bookings.columns:
        # Aggregate by industry
        industry_metrics = base_year_bookings.groupby('Industry').agg({
            'AccountId': 'nunique',
            'TicketQuantity': 'sum',
            'PaymentReceived': 'sum',
            'BookingFee': 'sum',
            'CardFee': 'sum',
            'ProcessingFee': 'sum',
            'TicketFee': 'sum',
        }).reset_index()

        industry_metrics.columns = ['Industry', 'Active Accounts', 'Tickets', 'Ticket Revenue',
                                     'BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']

        # Calculate total fees
        fee_cols = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        industry_metrics['Fees'] = industry_metrics[fee_cols].sum(axis=1)

        # Unit economics
        industry_metrics['Fees per Account'] = round(industry_metrics['Fees'] / industry_metrics['Active Accounts'], 2)
        industry_metrics['Tickets per Account'] = round(industry_metrics['Tickets'] / industry_metrics['Active Accounts'], 0)
        industry_metrics['Revenue per Account'] = round(industry_metrics['Ticket Revenue'] / industry_metrics['Active Accounts'], 2)

        # Calculate share of total
        total_accounts = industry_metrics['Active Accounts'].sum()
        total_fees = industry_metrics['Fees'].sum()
        industry_metrics['Account Share %'] = round(industry_metrics['Active Accounts'] / total_accounts * 100, 1)
        industry_metrics['Fees Share %'] = round(industry_metrics['Fees'] / total_fees * 100, 1)

        # Value index: fees share / account share (>1 = high value, <1 = low value)
        industry_metrics['Value Index'] = round(industry_metrics['Fees Share %'] / industry_metrics['Account Share %'].replace(0, 0.1), 2)

        # Relative growth recommendation based on value index
        industry_metrics['Relative Growth Rate'] = industry_metrics['Value Index'].apply(
            lambda x: 'Grow 30% faster' if x >= 1.5 else
                      'Grow 15% faster' if x >= 1.2 else
                      'Grow at average' if x >= 0.8 else
                      'Maintain' if x >= 0.5 else
                      'Deprioritise'
        )

        # Sort by value index descending
        industry_metrics = industry_metrics.sort_values('Value Index', ascending=False)

        # Select output columns
        industry_output = industry_metrics[[
            'Industry', 'Active Accounts', 'Account Share %', 'Fees', 'Fees Share %',
            'Fees per Account', 'Tickets per Account', 'Value Index', 'Relative Growth Rate'
        ]]

        industry_file = get_output_path(base_name, 'planning', '_industry_segmentation.csv')
        industry_output.to_csv(industry_file, index=False, float_format='%.2f')
        print(f"  ✓ Industry segmentation saved to: {industry_file}")

        # Log top industries
        print("  Top industries by value index:")
        for _, row in industry_output.head(5).iterrows():
            print(f"    {row['Industry']}: Value Index {row['Value Index']} - {row['Relative Growth Rate']}")
    else:
        print("  Warning: No industry data available for segmentation")

    return scenario_file


def calculate_account_tiers(accounts_df, booking_df, as_of_date=None):
    """
    Calculate tiers for ALL accounts based on percentile rankings across entire population.

    This uses the same tier logic as zoho_tiers.py:
    - Path A: tickets_current percentile
    - Path B: revenue_current percentile
    - Path C+D+E: years_loyalty + lifetime_revenue_pct + avg_revenue_per_year_pct

    Tiers: Key Account (99th), High Value (95th), Tier 4 (75th), Tier 3 (50th),
           Tier 2 (25th), Tier 1 (below 25th), NIL (no activity)

    Args:
        accounts_df: Accounts DataFrame with DateTimeCreated
        booking_df: Booking transactions DataFrame
        as_of_date: Calculate tiers as of this date (default: today). Used for
                    historical comparisons - e.g. to calculate what tiers would
                    have been assigned 1 year ago.

    Returns:
        Dictionary mapping AccountId -> tier string
    """
    from datetime import date

    # Use provided date or default to today
    reference_date = as_of_date if as_of_date else date.today()
    cutoff_365 = reference_date - timedelta(days=365)

    date_label = reference_date.strftime('%Y-%m-%d') if as_of_date else "current"
    print(f"  Calculating tiers for all accounts (as of {date_label})...")

    # Convert booking AccountId to numeric for matching
    booking_df = booking_df.copy()
    booking_df['AccountId'] = pd.to_numeric(booking_df['AccountId'], errors='coerce')

    # Filter bookings to only those before the reference date
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    if as_of_date:
        # For historical calculation, only include transactions up to the reference date
        reference_ts = pd.Timestamp(reference_date)
        if booking_df['TransactionDate'].dt.tz is not None:
            reference_ts = reference_ts.tz_localize('UTC')
        booking_df = booking_df[booking_df['TransactionDate'].dt.date <= reference_date]

    # Aggregate metrics per account
    account_metrics = {}

    # Group bookings by account
    for account_id, group in booking_df.groupby('AccountId'):
        if pd.isna(account_id):
            continue

        account_id = int(account_id)

        # Current period (365 days before reference date)
        tx_dates = pd.to_datetime(group['TransactionDate']).dt.date
        current_mask = tx_dates >= cutoff_365

        tickets_current = group.loc[current_mask, 'TicketQuantity'].sum() if 'TicketQuantity' in group.columns else 0

        # Calculate revenue from fees
        fee_cols = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        revenue_current = 0
        revenue_lifetime = 0
        for col in fee_cols:
            if col in group.columns:
                revenue_current += group.loc[current_mask, col].fillna(0).sum()
                revenue_lifetime += group[col].fillna(0).sum()

        # Years loyalty (unique years with transactions)
        years_with_tx = pd.to_datetime(group['TransactionDate']).dt.year.nunique()

        # Average revenue per year
        avg_revenue_per_year = revenue_lifetime / years_with_tx if years_with_tx > 0 else 0

        account_metrics[account_id] = {
            'tickets_current': tickets_current,
            'revenue_current': revenue_current,
            'revenue_lifetime': revenue_lifetime,
            'years_loyalty': years_with_tx,
            'avg_revenue_per_year': avg_revenue_per_year,
        }

    if not account_metrics:
        print("    No accounts with transactions found")
        return {}

    # Convert to DataFrame for percentile calculations
    metrics_df = pd.DataFrame.from_dict(account_metrics, orient='index')
    metrics_df.index.name = 'AccountId'

    # Calculate percentile rankings (only for accounts with activity)
    for metric in ['tickets_current', 'revenue_current', 'revenue_lifetime', 'avg_revenue_per_year']:
        pct_col = f"{metric}_pct"
        mask = metrics_df[metric] > 0
        if mask.sum() > 0:
            metrics_df.loc[mask, pct_col] = metrics_df.loc[mask, metric].rank(pct=True, method='average') * 100
        metrics_df.loc[~mask, pct_col] = 0

    # Determine tier for each account
    account_tiers = {}

    for account_id, row in metrics_df.iterrows():
        has_activity = row['tickets_current'] >= MIN_TICKETS_FOR_ACTIVE

        tier = determine_tier_from_percentiles(
            a_pct=row.get('tickets_current_pct', 0),
            b_pct=row.get('revenue_current_pct', 0),
            c_years=row.get('years_loyalty', 0),
            d_pct=row.get('revenue_lifetime_pct', 0),
            e_pct=row.get('avg_revenue_per_year_pct', 0),
            has_activity=has_activity
        )
        account_tiers[account_id] = tier

    # Log tier distribution
    tier_counts = {}
    for tier in account_tiers.values():
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    print(f"    Calculated tiers for {len(account_tiers):,} accounts")
    tier_4_plus = sum(tier_counts.get(t, 0) for t in ['Key Account', 'High Value', 'Tier 4'])
    print(f"    Tier 4+ accounts: {tier_4_plus:,}")

    return account_tiers


def count_tier4_plus_new_accounts(accounts_df, account_tiers, year, month):
    """
    Count new accounts created in a specific month that achieved Tier 4+.

    Args:
        accounts_df: Accounts DataFrame with DateTimeCreated
        account_tiers: Dictionary mapping AccountId -> tier string (from calculate_account_tiers)
        year: Year to filter
        month: Month to filter

    Returns:
        Count of Tier 4+ new accounts for that month
    """
    # Get month boundaries
    month_start = pd.Timestamp(year=year, month=month, day=1, tz='Europe/London')
    last_day = calendar.monthrange(year, month)[1]
    month_end = pd.Timestamp(year=year, month=month, day=last_day,
                             hour=23, minute=59, second=59, tz='Europe/London')

    # Get account ID column
    account_id_col = 'Account Id' if 'Account Id' in accounts_df.columns else 'AccountId'

    # Filter new accounts created in this month
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= month_start) &
        (accounts_df['DateTimeCreated'] <= month_end)
    ]

    # Count how many achieved Tier 4+
    tier_4_plus_tiers = {'Key Account', 'High Value', 'Tier 4'}
    tier4_plus_count = 0

    for _, row in new_accounts.iterrows():
        account_id = row[account_id_col]
        if pd.notna(account_id):
            account_id = int(float(account_id))
            tier = account_tiers.get(account_id, 'NIL')
            if tier in tier_4_plus_tiers:
                tier4_plus_count += 1

    return tier4_plus_count


# === DDDM ANALYTICS MODULES ===

def generate_account_ltv_analysis_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                       output_file: str) -> dict:
    """
    Generate 24-month Account Lifetime Value analysis by segment.

    Calculates LTV for accounts based on their first 24 months of activity,
    segmented by industry, region, and acquisition cohort.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating Account LTV analysis...")

    # Prepare data
    booking_df = booking_df.copy()
    accounts_df = accounts_df.copy()

    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    accounts_df['DateTimeCreated'] = pd.to_datetime(accounts_df['DateTimeCreated'])

    # Get account ID column
    account_id_col = 'Account Id' if 'Account Id' in accounts_df.columns else 'AccountId'
    booking_account_col = 'AccountId'

    # Create account creation date lookup
    account_created = accounts_df.set_index(account_id_col)['DateTimeCreated'].to_dict()

    # Add account creation date to bookings
    booking_df['AccountCreated'] = booking_df[booking_account_col].map(account_created)
    booking_df['DaysSinceCreation'] = (booking_df['TransactionDate'] - booking_df['AccountCreated']).dt.days

    # Filter to first 24 months (730 days) of each account
    booking_df_24m = booking_df[
        (booking_df['DaysSinceCreation'] >= 0) &
        (booking_df['DaysSinceCreation'] <= 730)
    ].copy()

    # Calculate LTV per account
    ltv_by_account = booking_df_24m.groupby(booking_account_col).agg({
        'BookingFee': 'sum',
        'CardFee': 'sum',
        'TicketFee': 'sum',
        'ProcessingFee': 'sum',
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'EventId': 'nunique',
        'TransactionDate': ['min', 'max', 'count']
    }).reset_index()

    # Flatten column names
    ltv_by_account.columns = [
        booking_account_col, 'Booking_Fee_24m', 'Card_Fee_24m', 'Ticket_Fee_24m',
        'Processing_Fee_24m', 'Tickets_24m', 'Revenue_24m', 'Events_24m',
        'First_Transaction', 'Last_Transaction', 'Transaction_Count_24m'
    ]

    # Calculate total fees
    ltv_by_account['Total_Fees_24m'] = (
        ltv_by_account['Booking_Fee_24m'] + ltv_by_account['Card_Fee_24m'] +
        ltv_by_account['Ticket_Fee_24m'] + ltv_by_account['Processing_Fee_24m']
    )

    # Add account metadata
    account_meta = accounts_df[[account_id_col, 'DateTimeCreated', 'Industry', 'SubIndustry']].copy()

    # Add region from postcode
    if 'Postcode' in accounts_df.columns:
        postcode_areas = extract_postcode_areas_vectorized(accounts_df['Postcode'])
        account_meta['Region'] = get_regions_vectorized(postcode_areas)
    else:
        account_meta['Region'] = 'Unknown'

    # Add acquisition cohort (year-quarter)
    account_meta['Cohort'] = account_meta['DateTimeCreated'].dt.to_period('Q').astype(str)
    account_meta['Cohort_Year'] = account_meta['DateTimeCreated'].dt.year

    ltv_by_account = ltv_by_account.merge(
        account_meta.rename(columns={account_id_col: booking_account_col}),
        on=booking_account_col, how='left'
    )

    # === OUTPUT 1: LTV by Industry ===
    ltv_by_industry = ltv_by_account.groupby('Industry').agg({
        booking_account_col: 'count',
        'Total_Fees_24m': ['sum', 'mean', 'median'],
        'Revenue_24m': ['sum', 'mean'],
        'Tickets_24m': ['sum', 'mean'],
        'Events_24m': ['sum', 'mean'],
    }).reset_index()

    ltv_by_industry.columns = [
        'Industry', 'Accounts', 'Total_Fees', 'Avg_LTV_24m', 'Median_LTV_24m',
        'Total_Revenue', 'Avg_Revenue_24m', 'Total_Tickets', 'Avg_Tickets_24m',
        'Total_Events', 'Avg_Events_24m'
    ]

    # Calculate LTV per account for ranking
    ltv_by_industry['Value_Index'] = (
        ltv_by_industry['Total_Fees'] / ltv_by_industry['Total_Fees'].sum() * 100
    ) / (
        ltv_by_industry['Accounts'] / ltv_by_industry['Accounts'].sum() * 100
    )
    ltv_by_industry = ltv_by_industry.sort_values('Avg_LTV_24m', ascending=False)

    industry_file = get_output_path(base_name, 'cohorts', '_ltv_by_industry.csv')
    ltv_by_industry.to_csv(industry_file, index=False, float_format='%.2f')
    output_files['ltv_by_industry'] = industry_file
    print(f"    ✓ LTV by industry: {industry_file}")

    # === OUTPUT 2: LTV by Region ===
    ltv_by_region = ltv_by_account.groupby('Region').agg({
        booking_account_col: 'count',
        'Total_Fees_24m': ['sum', 'mean', 'median'],
        'Revenue_24m': ['sum', 'mean'],
    }).reset_index()

    ltv_by_region.columns = [
        'Region', 'Accounts', 'Total_Fees', 'Avg_LTV_24m', 'Median_LTV_24m',
        'Total_Revenue', 'Avg_Revenue_24m'
    ]
    ltv_by_region = ltv_by_region.sort_values('Avg_LTV_24m', ascending=False)

    region_file = get_output_path(base_name, 'geography', '_ltv_by_region.csv')
    ltv_by_region.to_csv(region_file, index=False, float_format='%.2f')
    output_files['ltv_by_region'] = region_file
    print(f"    ✓ LTV by region: {region_file}")

    # === OUTPUT 3: LTV by Acquisition Cohort ===
    # Only include cohorts with at least 24 months of history
    today = pd.Timestamp.now(tz='Europe/London')
    cutoff_date = today - pd.Timedelta(days=730)

    mature_cohorts = ltv_by_account[
        ltv_by_account['DateTimeCreated'] <= cutoff_date
    ]

    if len(mature_cohorts) > 0:
        ltv_by_cohort = mature_cohorts.groupby('Cohort').agg({
            booking_account_col: 'count',
            'Total_Fees_24m': ['sum', 'mean', 'median'],
            'Revenue_24m': 'sum',
            'Events_24m': 'mean',
        }).reset_index()

        ltv_by_cohort.columns = [
            'Cohort', 'Accounts', 'Total_Fees', 'Avg_LTV_24m', 'Median_LTV_24m',
            'Total_Revenue', 'Avg_Events_24m'
        ]
        ltv_by_cohort = ltv_by_cohort.sort_values('Cohort')

        cohort_file = get_output_path(base_name, 'cohorts', '_ltv_by_cohort.csv')
        ltv_by_cohort.to_csv(cohort_file, index=False, float_format='%.2f')
        output_files['ltv_by_cohort'] = cohort_file
        print(f"    ✓ LTV by cohort: {cohort_file}")

    # === OUTPUT 4: LTV Distribution Summary ===
    ltv_distribution = pd.DataFrame({
        'Metric': ['Total Accounts Analysed', 'Avg 24m LTV', 'Median 24m LTV',
                   'Top 10% LTV Threshold', 'Top 25% LTV Threshold',
                   'Bottom 25% LTV', 'Accounts with Zero LTV'],
        'Value': [
            len(ltv_by_account),
            ltv_by_account['Total_Fees_24m'].mean(),
            ltv_by_account['Total_Fees_24m'].median(),
            ltv_by_account['Total_Fees_24m'].quantile(0.9),
            ltv_by_account['Total_Fees_24m'].quantile(0.75),
            ltv_by_account['Total_Fees_24m'].quantile(0.25),
            (ltv_by_account['Total_Fees_24m'] == 0).sum()
        ]
    })

    dist_file = get_output_path(base_name, 'cohorts', '_ltv_distribution.csv')
    ltv_distribution.to_csv(dist_file, index=False, float_format='%.2f')
    output_files['ltv_distribution'] = dist_file
    print(f"    ✓ LTV distribution: {dist_file}")

    return output_files


def generate_dormancy_analysis_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                    output_file: str) -> dict:
    """
    Generate dormancy/churn analysis using tier transitions.

    Tracks accounts transitioning to NIL (no activity) status and identifies
    dormancy patterns by segment.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating dormancy analysis...")

    # Prepare data
    booking_df = booking_df.copy()
    accounts_df = accounts_df.copy()

    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    accounts_df['DateTimeCreated'] = pd.to_datetime(accounts_df['DateTimeCreated'])

    # Get account ID column
    account_id_col = 'Account Id' if 'Account Id' in accounts_df.columns else 'AccountId'
    booking_account_col = 'AccountId'

    today = pd.Timestamp.now(tz='Europe/London')

    # Calculate last transaction date per account
    last_txn = booking_df.groupby(booking_account_col)['TransactionDate'].max().reset_index()
    last_txn.columns = [booking_account_col, 'Last_Transaction']

    # Calculate days since last transaction
    last_txn['Days_Since_Transaction'] = (today - last_txn['Last_Transaction']).dt.days
    last_txn['Months_Since_Transaction'] = (last_txn['Days_Since_Transaction'] / 30.44).round(0).astype(int)

    # Merge with account data
    account_meta = accounts_df[[account_id_col, 'DateTimeCreated', 'Industry', 'SubIndustry']].copy()
    account_meta['Account_Age_Days'] = (today - account_meta['DateTimeCreated']).dt.days
    account_meta['Account_Age_Months'] = (account_meta['Account_Age_Days'] / 30.44).round(0).astype(int)

    # Add region
    if 'Postcode' in accounts_df.columns:
        postcode_areas = extract_postcode_areas_vectorized(accounts_df['Postcode'])
        account_meta['Region'] = get_regions_vectorized(postcode_areas)
    else:
        account_meta['Region'] = 'Unknown'

    dormancy_df = account_meta.merge(
        last_txn.rename(columns={booking_account_col: account_id_col}),
        on=account_id_col, how='left'
    )

    # Define dormancy status based on months since last transaction
    def classify_dormancy(months):
        if pd.isna(months):
            return 'Never Transacted'
        elif months <= 3:
            return 'Active (0-3m)'
        elif months <= 6:
            return 'Recent (3-6m)'
        elif months <= 12:
            return 'At Risk (6-12m)'
        elif months <= 24:
            return 'Dormant (12-24m)'
        else:
            return 'Churned (24m+)'

    dormancy_df['Dormancy_Status'] = dormancy_df['Months_Since_Transaction'].apply(classify_dormancy)

    # === OUTPUT 1: Dormancy by Industry ===
    dormancy_by_industry = dormancy_df.groupby(['Industry', 'Dormancy_Status']).size().unstack(fill_value=0)
    dormancy_by_industry['Total'] = dormancy_by_industry.sum(axis=1)

    # Calculate percentages
    for col in dormancy_by_industry.columns[:-1]:
        dormancy_by_industry[f'{col} %'] = round(dormancy_by_industry[col] / dormancy_by_industry['Total'] * 100, 1)

    # Calculate dormancy rate (At Risk + Dormant + Churned)
    at_risk_cols = ['At Risk (6-12m)', 'Dormant (12-24m)', 'Churned (24m+)']
    existing_cols = [c for c in at_risk_cols if c in dormancy_by_industry.columns]
    if existing_cols:
        dormancy_by_industry['Dormancy_Rate %'] = round(
            dormancy_by_industry[existing_cols].sum(axis=1) / dormancy_by_industry['Total'] * 100, 1
        )

    dormancy_by_industry = dormancy_by_industry.sort_values('Dormancy_Rate %', ascending=False)

    industry_file = get_output_path(base_name, 'cohorts', '_dormancy_by_industry.csv')
    dormancy_by_industry.to_csv(industry_file, float_format='%.1f')
    output_files['dormancy_by_industry'] = industry_file
    print(f"    ✓ Dormancy by industry: {industry_file}")

    # === OUTPUT 2: Dormancy by Account Age ===
    # Group accounts by age cohort
    def age_cohort(months):
        if months < 12:
            return '0-12m'
        elif months < 24:
            return '12-24m'
        elif months < 36:
            return '24-36m'
        elif months < 48:
            return '36-48m'
        else:
            return '48m+'

    dormancy_df['Age_Cohort'] = dormancy_df['Account_Age_Months'].apply(age_cohort)

    dormancy_by_age = dormancy_df.groupby(['Age_Cohort', 'Dormancy_Status']).size().unstack(fill_value=0)
    dormancy_by_age['Total'] = dormancy_by_age.sum(axis=1)

    existing_cols = [c for c in at_risk_cols if c in dormancy_by_age.columns]
    if existing_cols:
        dormancy_by_age['Dormancy_Rate %'] = round(
            dormancy_by_age[existing_cols].sum(axis=1) / dormancy_by_age['Total'] * 100, 1
        )

    age_file = get_output_path(base_name, 'cohorts', '_dormancy_by_account_age.csv')
    dormancy_by_age.to_csv(age_file, float_format='%.1f')
    output_files['dormancy_by_age'] = age_file
    print(f"    ✓ Dormancy by account age: {age_file}")

    # === OUTPUT 3: Dormancy Summary ===
    summary = dormancy_df['Dormancy_Status'].value_counts()
    summary_df = pd.DataFrame({
        'Status': summary.index,
        'Count': summary.values,
        'Percentage': np.round(summary.values / len(dormancy_df) * 100, 1)
    })

    summary_file = get_output_path(base_name, 'cohorts', '_dormancy_summary.csv')
    summary_df.to_csv(summary_file, index=False)
    output_files['dormancy_summary'] = summary_file
    print(f"    ✓ Dormancy summary: {summary_file}")

    # === OUTPUT 4: YoY Dormancy Comparison (2024 vs 2025 cohorts) ===
    # Compare dormancy status for accounts created in each year, measured at end of that year
    # This gives an apples-to-apples comparison of first-year performance

    def classify_dormancy_at_date(last_txn_date, reference_date):
        """Classify dormancy status relative to a specific reference date."""
        if pd.isna(last_txn_date):
            return 'Never Transacted'
        months = (reference_date - last_txn_date).days / 30.44
        if months <= 3:
            return 'Active (0-3m)'
        elif months <= 6:
            return 'Recent (3-6m)'
        elif months <= 12:
            return 'At Risk (6-12m)'
        elif months <= 24:
            return 'Dormant (12-24m)'
        else:
            return 'Churned (24m+)'

    # Reference dates for each cohort year
    end_of_2024 = pd.Timestamp('2024-12-31', tz='Europe/London')
    end_of_2025 = pd.Timestamp('2025-12-31', tz='Europe/London')

    # Get accounts created in each year
    accounts_2024 = accounts_df[
        (accounts_df['DateTimeCreated'].dt.year == 2024)
    ][account_id_col].unique()

    accounts_2025 = accounts_df[
        (accounts_df['DateTimeCreated'].dt.year == 2025)
    ][account_id_col].unique()

    # Get last transaction dates for bookings up to end of each year
    booking_df_ts = booking_df.copy()
    booking_df_ts['TransactionDate'] = pd.to_datetime(booking_df_ts['TransactionDate'])

    # 2024 cohort: transactions up to end of 2024
    bookings_2024 = booking_df_ts[booking_df_ts['TransactionDate'] <= end_of_2024]
    last_txn_2024 = bookings_2024.groupby(booking_account_col)['TransactionDate'].max()

    # 2025 cohort: transactions up to end of 2025
    bookings_2025 = booking_df_ts[booking_df_ts['TransactionDate'] <= end_of_2025]
    last_txn_2025 = bookings_2025.groupby(booking_account_col)['TransactionDate'].max()

    # Classify 2024 cohort at end of 2024
    cohort_2024_status = []
    for acc_id in accounts_2024:
        last_txn = last_txn_2024.get(acc_id)
        status = classify_dormancy_at_date(last_txn, end_of_2024)
        cohort_2024_status.append(status)

    # Classify 2025 cohort at end of 2025
    cohort_2025_status = []
    for acc_id in accounts_2025:
        last_txn = last_txn_2025.get(acc_id)
        status = classify_dormancy_at_date(last_txn, end_of_2025)
        cohort_2025_status.append(status)

    # Build comparison DataFrame
    status_order = ['Never Transacted', 'Active (0-3m)', 'Recent (3-6m)',
                    'At Risk (6-12m)', 'Dormant (12-24m)', 'Churned (24m+)']

    from collections import Counter
    counts_2024 = Counter(cohort_2024_status)
    counts_2025 = Counter(cohort_2025_status)

    total_2024 = len(accounts_2024)
    total_2025 = len(accounts_2025)

    yoy_comparison = []
    for status in status_order:
        count_2024 = counts_2024.get(status, 0)
        count_2025 = counts_2025.get(status, 0)
        pct_2024 = round(count_2024 / total_2024 * 100, 1) if total_2024 > 0 else 0
        pct_2025 = round(count_2025 / total_2025 * 100, 1) if total_2025 > 0 else 0

        yoy_comparison.append({
            'Status': status,
            '2024 Cohort Count': count_2024,
            '2024 Cohort %': pct_2024,
            '2025 Cohort Count': count_2025,
            '2025 Cohort %': pct_2025,
        })

    # Add totals row
    yoy_comparison.append({
        'Status': 'TOTAL',
        '2024 Cohort Count': total_2024,
        '2024 Cohort %': 100.0,
        '2025 Cohort Count': total_2025,
        '2025 Cohort %': 100.0,
    })

    yoy_df = pd.DataFrame(yoy_comparison)
    yoy_file = get_output_path(base_name, 'cohorts', '_dormancy_yoy_comparison.csv')
    yoy_df.to_csv(yoy_file, index=False)
    output_files['dormancy_yoy_comparison'] = yoy_file
    print(f"    ✓ Dormancy YoY comparison: {yoy_file}")

    return output_files


def generate_event_metrics_analysis_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                         output_file: str) -> dict:
    """
    Generate event success proxy metrics from booking data.

    Calculates:
    - Average tickets/revenue per event
    - Events per account per year
    - Advance booking window (days between transaction and event)
    - Repeat event rate

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame (unused, kept for API consistency)
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    _ = accounts_df  # Unused, kept for API consistency

    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating event metrics analysis...")

    # Prepare data
    booking_df = booking_df.copy()

    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df['EventDate'] = pd.to_datetime(booking_df['EventDate'], errors='coerce')

    # Calculate advance booking window
    booking_df['Advance_Days'] = (booking_df['EventDate'] - booking_df['TransactionDate']).dt.days
    # Filter out negative values (post-event transactions shouldn't exist but just in case)
    booking_df.loc[booking_df['Advance_Days'] < 0, 'Advance_Days'] = np.nan

    # Get transaction year
    booking_df['Year'] = booking_df['TransactionDate'].dt.year

    # === METRICS BY EVENT ===
    event_metrics = booking_df.groupby('EventId').agg({
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'BookingFee': 'sum',
        'CardFee': 'sum',
        'TicketFee': 'sum',
        'ProcessingFee': 'sum',
        'AccountId': 'first',
        'Industry': 'first',
        'Advance_Days': 'median',
        'TransactionDate': ['min', 'max', 'count']
    }).reset_index()

    event_metrics.columns = [
        'EventId', 'Tickets', 'Revenue', 'Booking_Fee', 'Card_Fee', 'Ticket_Fee',
        'Processing_Fee', 'AccountId', 'Industry', 'Median_Advance_Days',
        'First_Sale', 'Last_Sale', 'Transaction_Count'
    ]

    event_metrics['Total_Fees'] = (
        event_metrics['Booking_Fee'] + event_metrics['Card_Fee'] +
        event_metrics['Ticket_Fee'] + event_metrics['Processing_Fee']
    )

    # Sales window (days between first and last sale)
    event_metrics['Sales_Window_Days'] = (
        event_metrics['Last_Sale'] - event_metrics['First_Sale']
    ).dt.days

    # === OUTPUT 1: Event Metrics by Industry ===
    industry_event_metrics = event_metrics.groupby('Industry').agg({
        'EventId': 'count',
        'Tickets': ['sum', 'mean', 'median'],
        'Revenue': ['sum', 'mean', 'median'],
        'Total_Fees': ['sum', 'mean'],
        'Median_Advance_Days': 'median',
        'Sales_Window_Days': 'median',
    }).reset_index()

    industry_event_metrics.columns = [
        'Industry', 'Event_Count', 'Total_Tickets', 'Avg_Tickets_Per_Event',
        'Median_Tickets_Per_Event', 'Total_Revenue', 'Avg_Revenue_Per_Event',
        'Median_Revenue_Per_Event', 'Total_Fees', 'Avg_Fees_Per_Event',
        'Median_Advance_Booking_Days', 'Median_Sales_Window_Days'
    ]

    industry_event_metrics = industry_event_metrics.sort_values('Total_Fees', ascending=False)

    industry_file = get_output_path(base_name, 'industry', '_event_metrics_by_industry.csv')
    industry_event_metrics.to_csv(industry_file, index=False, float_format='%.2f')
    output_files['event_metrics_by_industry'] = industry_file
    print(f"    ✓ Event metrics by industry: {industry_file}")

    # === OUTPUT 2: Account Event Frequency ===
    # Events per account per year
    events_per_account_year = booking_df.groupby(['AccountId', 'Year'])['EventId'].nunique().reset_index()
    events_per_account_year.columns = ['AccountId', 'Year', 'Events_Count']

    # Calculate average events per account per year
    account_event_freq = events_per_account_year.groupby('AccountId').agg({
        'Events_Count': ['mean', 'max', 'sum'],
        'Year': 'nunique'
    }).reset_index()
    account_event_freq.columns = ['AccountId', 'Avg_Events_Per_Year', 'Max_Events_Year',
                                   'Total_Events', 'Active_Years']

    # Classify frequency
    def classify_frequency(avg_events):
        if avg_events >= 4:
            return 'Regular (4+/yr)'
        elif avg_events >= 2:
            return 'Occasional (2-3/yr)'
        elif avg_events >= 1:
            return 'Annual (1/yr)'
        else:
            return 'Sporadic (<1/yr)'

    account_event_freq['Frequency_Category'] = account_event_freq['Avg_Events_Per_Year'].apply(classify_frequency)

    # Summary by frequency category
    freq_summary = account_event_freq['Frequency_Category'].value_counts()
    freq_summary_df = pd.DataFrame({
        'Frequency_Category': freq_summary.index,
        'Account_Count': freq_summary.values,
        'Percentage': np.round(freq_summary.values / len(account_event_freq) * 100, 1)
    })

    freq_file = get_output_path(base_name, 'cohorts', '_account_event_frequency.csv')
    freq_summary_df.to_csv(freq_file, index=False)
    output_files['account_event_frequency'] = freq_file
    print(f"    ✓ Account event frequency: {freq_file}")

    # === OUTPUT 3: Repeat Event Rate ===
    # Accounts with more than one event ever
    repeat_rate = (account_event_freq['Total_Events'] > 1).sum() / len(account_event_freq) * 100

    repeat_stats = pd.DataFrame({
        'Metric': [
            'Total Accounts with Events',
            'Accounts with 1 Event Only',
            'Accounts with 2+ Events (Repeat)',
            'Accounts with 5+ Events',
            'Accounts with 10+ Events',
            'Repeat Event Rate %',
            'Avg Events Per Account',
            'Median Events Per Account'
        ],
        'Value': [
            len(account_event_freq),
            (account_event_freq['Total_Events'] == 1).sum(),
            (account_event_freq['Total_Events'] > 1).sum(),
            (account_event_freq['Total_Events'] >= 5).sum(),
            (account_event_freq['Total_Events'] >= 10).sum(),
            round(repeat_rate, 1),
            round(account_event_freq['Total_Events'].mean(), 2),
            account_event_freq['Total_Events'].median()
        ]
    })

    repeat_file = get_output_path(base_name, 'cohorts', '_repeat_event_stats.csv')
    repeat_stats.to_csv(repeat_file, index=False)
    output_files['repeat_event_stats'] = repeat_file
    print(f"    ✓ Repeat event stats: {repeat_file}")

    # === OUTPUT 4: Advance Booking Analysis ===
    # By industry - when do tickets typically sell?
    advance_by_industry = booking_df.groupby('Industry')['Advance_Days'].agg([
        'count', 'mean', 'median',
        lambda x: x.quantile(0.25),
        lambda x: x.quantile(0.75)
    ]).reset_index()
    advance_by_industry.columns = ['Industry', 'Transactions', 'Mean_Advance_Days',
                                    'Median_Advance_Days', 'Q1_Advance_Days', 'Q3_Advance_Days']
    advance_by_industry = advance_by_industry.sort_values('Median_Advance_Days', ascending=False)

    advance_file = get_output_path(base_name, 'industry', '_advance_booking_by_industry.csv')
    advance_by_industry.to_csv(advance_file, index=False, float_format='%.1f')
    output_files['advance_booking_by_industry'] = advance_file
    print(f"    ✓ Advance booking by industry: {advance_file}")

    return output_files


def generate_boxoffice_analysis_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                     output_file: str) -> dict:
    """
    Generate Box Office sales analysis (in-person vs online transactions).

    Box Office transactions are identified by PaymentType:
    - 'Card Present' - In-person card payments at the door
    - 'Cash' - Cash payments at the door
    - All other payment types are considered online sales

    Outputs:
    - Summary by channel (Box Office vs Online)
    - Industry breakdown by channel
    - Monthly trends by channel
    - Account reliance on Box Office

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating Box Office sales analysis...")

    # Check if PaymentType column exists
    if 'PaymentType' not in booking_df.columns:
        print("    ⚠ PaymentType column not found - skipping Box Office analysis")
        return output_files

    # Prepare data
    booking_df = booking_df.copy()
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df['Year'] = booking_df['TransactionDate'].dt.year
    booking_df['Month'] = booking_df['TransactionDate'].dt.month
    booking_df['YearMonth'] = booking_df['TransactionDate'].dt.to_period('M').astype(str)

    # Classify sales channel using shared helper
    booking_df = add_sales_channel_column(booking_df)

    # Calculate TotalFees if not present
    if 'TotalFees' not in booking_df.columns:
        fee_cols = ['BookingFee', 'CardFee', 'TicketFee', 'ProcessingFee']
        available_fees = [col for col in fee_cols if col in booking_df.columns]
        booking_df['TotalFees'] = booking_df[available_fees].fillna(0).sum(axis=1)

    # === OUTPUT 1: Summary by Channel ===
    channel_summary = booking_df.groupby('Sales_Channel').agg({
        'BookingTransactionId': 'count',
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum',
        'AccountId': 'nunique',
        'EventId': 'nunique'
    }).reset_index()

    channel_summary.columns = [
        'Sales_Channel', 'Transactions', 'Tickets', 'Revenue',
        'Fees', 'Unique_Accounts', 'Unique_Events'
    ]

    total_transactions = channel_summary['Transactions'].sum()
    total_tickets = channel_summary['Tickets'].sum()
    total_revenue = channel_summary['Revenue'].sum()
    total_fees = channel_summary['Fees'].sum()

    channel_summary['Pct_Transactions'] = round(channel_summary['Transactions'] / total_transactions * 100, 1)
    channel_summary['Pct_Tickets'] = round(channel_summary['Tickets'] / total_tickets * 100, 1)
    channel_summary['Pct_Revenue'] = round(channel_summary['Revenue'] / total_revenue * 100, 1) if total_revenue > 0 else 0
    channel_summary['Pct_Fees'] = round(channel_summary['Fees'] / total_fees * 100, 1) if total_fees > 0 else 0
    channel_summary['Avg_Tickets_Per_Transaction'] = round(channel_summary['Tickets'] / channel_summary['Transactions'], 2)
    channel_summary['Avg_Revenue_Per_Transaction'] = round(channel_summary['Revenue'] / channel_summary['Transactions'], 2)

    summary_file = get_output_path(base_name, 'boxoffice', '_channel_summary.csv')
    channel_summary.to_csv(summary_file, index=False, float_format='%.2f')
    output_files['channel_summary'] = summary_file
    print(f"    ✓ Channel summary: {summary_file}")

    # === OUTPUT 2: Industry Breakdown by Channel ===
    industry_channel = booking_df.groupby(['Industry', 'Sales_Channel']).agg({
        'BookingTransactionId': 'count',
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum'
    }).reset_index()

    industry_channel.columns = ['Industry', 'Sales_Channel', 'Transactions', 'Tickets', 'Revenue', 'Fees']

    # Pivot to show Box Office vs Online side by side
    industry_pivot = industry_channel.pivot(index='Industry', columns='Sales_Channel',
                                            values=['Transactions', 'Tickets', 'Revenue', 'Fees'])
    industry_pivot.columns = [f'{col[1]}_{col[0]}' for col in industry_pivot.columns]
    industry_pivot = industry_pivot.reset_index()
    # Fill NaN only in numeric columns (Industry column may be categorical)
    numeric_cols = industry_pivot.select_dtypes(include=[np.number]).columns
    industry_pivot[numeric_cols] = industry_pivot[numeric_cols].fillna(0)

    # Calculate Box Office percentage
    for metric in ['Transactions', 'Tickets', 'Revenue', 'Fees']:
        bo_col = f'Box Office_{metric}'
        online_col = f'Online_{metric}'
        if bo_col in industry_pivot.columns and online_col in industry_pivot.columns:
            total = industry_pivot[bo_col] + industry_pivot[online_col]
            industry_pivot[f'Box_Office_Pct_{metric}'] = np.where(
                total > 0,
                round(industry_pivot[bo_col] / total * 100, 1),
                0
            )

    # Sort by Box Office percentage of fees
    if 'Box_Office_Pct_Fees' in industry_pivot.columns:
        industry_pivot = industry_pivot.sort_values('Box_Office_Pct_Fees', ascending=False)

    industry_file = get_output_path(base_name, 'boxoffice', '_industry_channel_breakdown.csv')
    industry_pivot.to_csv(industry_file, index=False, float_format='%.2f')
    output_files['industry_channel_breakdown'] = industry_file
    print(f"    ✓ Industry channel breakdown: {industry_file}")

    # === OUTPUT 3: Monthly Trends by Channel ===
    monthly_channel = booking_df.groupby(['YearMonth', 'Sales_Channel']).agg({
        'BookingTransactionId': 'count',
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum'
    }).reset_index()

    monthly_channel.columns = ['YearMonth', 'Sales_Channel', 'Transactions', 'Tickets', 'Revenue', 'Fees']

    # Pivot for time series view
    monthly_pivot = monthly_channel.pivot(index='YearMonth', columns='Sales_Channel',
                                           values=['Transactions', 'Tickets', 'Revenue', 'Fees'])
    monthly_pivot.columns = [f'{col[1]}_{col[0]}' for col in monthly_pivot.columns]
    monthly_pivot = monthly_pivot.reset_index()
    # Fill NaN only in numeric columns
    numeric_cols = monthly_pivot.select_dtypes(include=[np.number]).columns
    monthly_pivot[numeric_cols] = monthly_pivot[numeric_cols].fillna(0)

    # Calculate Box Office percentage per month
    for metric in ['Transactions', 'Tickets', 'Revenue', 'Fees']:
        bo_col = f'Box Office_{metric}'
        online_col = f'Online_{metric}'
        if bo_col in monthly_pivot.columns and online_col in monthly_pivot.columns:
            total = monthly_pivot[bo_col] + monthly_pivot[online_col]
            monthly_pivot[f'Box_Office_Pct_{metric}'] = np.where(
                total > 0,
                round(monthly_pivot[bo_col] / total * 100, 1),
                0
            )

    monthly_file = get_output_path(base_name, 'boxoffice', '_monthly_trends.csv')
    monthly_pivot.to_csv(monthly_file, index=False, float_format='%.2f')
    output_files['monthly_trends'] = monthly_file
    print(f"    ✓ Monthly trends: {monthly_file}")

    # === OUTPUT 4: Account Reliance on Box Office ===
    account_channel = booking_df.groupby(['AccountId', 'Sales_Channel']).agg({
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum'
    }).reset_index()

    account_channel.columns = ['AccountId', 'Sales_Channel', 'Tickets', 'Revenue', 'Fees']

    # Pivot to get Box Office vs Online per account
    account_pivot = account_channel.pivot(index='AccountId', columns='Sales_Channel',
                                          values=['Tickets', 'Revenue', 'Fees'])
    account_pivot.columns = [f'{col[1]}_{col[0]}' for col in account_pivot.columns]
    account_pivot = account_pivot.reset_index()
    # Fill NaN only in numeric columns
    numeric_cols = account_pivot.select_dtypes(include=[np.number]).columns
    account_pivot[numeric_cols] = account_pivot[numeric_cols].fillna(0)

    # Calculate Box Office percentage per account
    for metric in ['Tickets', 'Revenue', 'Fees']:
        bo_col = f'Box Office_{metric}'
        online_col = f'Online_{metric}'
        if bo_col in account_pivot.columns and online_col in account_pivot.columns:
            total = account_pivot[bo_col] + account_pivot[online_col]
            account_pivot[f'Box_Office_Pct_{metric}'] = np.where(
                total > 0,
                round(account_pivot[bo_col] / total * 100, 1),
                0
            )

    # Classify accounts by Box Office reliance
    def classify_boxoffice_reliance(pct):
        if pct >= 80:
            return 'Heavy Box Office (80%+)'
        elif pct >= 50:
            return 'Majority Box Office (50-79%)'
        elif pct >= 20:
            return 'Mixed Channel (20-49%)'
        elif pct > 0:
            return 'Primarily Online (1-19%)'
        else:
            return 'Online Only (0%)'

    if 'Box_Office_Pct_Fees' in account_pivot.columns:
        account_pivot['Box_Office_Reliance'] = account_pivot['Box_Office_Pct_Fees'].apply(classify_boxoffice_reliance)

        # Merge with account info
        account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Account Id'
        account_info = accounts_df[[account_id_col, 'Industry']].copy()
        if account_id_col != 'AccountId':
            account_info = account_info.rename(columns={account_id_col: 'AccountId'})
        account_info['AccountId'] = pd.to_numeric(account_info['AccountId'], errors='coerce')

        account_pivot = account_pivot.merge(account_info, on='AccountId', how='left')

        # Summary by reliance category
        reliance_summary = account_pivot.groupby('Box_Office_Reliance').agg({
            'AccountId': 'count',
            'Box Office_Fees': 'sum' if 'Box Office_Fees' in account_pivot.columns else None,
            'Online_Fees': 'sum' if 'Online_Fees' in account_pivot.columns else None
        })

        # Clean up columns
        reliance_summary = reliance_summary.reset_index()
        if 'AccountId' in reliance_summary.columns:
            reliance_summary = reliance_summary.rename(columns={'AccountId': 'Account_Count'})

        reliance_summary['Pct_Accounts'] = round(
            reliance_summary['Account_Count'] / reliance_summary['Account_Count'].sum() * 100, 1
        )

        # Order categories logically
        category_order = [
            'Online Only (0%)',
            'Primarily Online (1-19%)',
            'Mixed Channel (20-49%)',
            'Majority Box Office (50-79%)',
            'Heavy Box Office (80%+)'
        ]
        reliance_summary['Order'] = reliance_summary['Box_Office_Reliance'].apply(
            lambda x: category_order.index(x) if x in category_order else 99
        )
        reliance_summary = reliance_summary.sort_values('Order').drop('Order', axis=1)

        reliance_file = get_output_path(base_name, 'boxoffice', '_account_reliance.csv')
        reliance_summary.to_csv(reliance_file, index=False, float_format='%.2f')
        output_files['account_reliance'] = reliance_file
        print(f"    ✓ Account reliance: {reliance_file}")

        # === OUTPUT 5: Box Office Reliance by Industry ===
        industry_reliance = account_pivot.groupby(['Industry', 'Box_Office_Reliance']).size().unstack(fill_value=0)

        # Calculate percentages within each industry
        industry_reliance_pct = industry_reliance.div(industry_reliance.sum(axis=1), axis=0) * 100

        # Reorder columns
        cols_order = [c for c in category_order if c in industry_reliance.columns]
        industry_reliance = industry_reliance[cols_order]
        industry_reliance_pct = industry_reliance_pct[cols_order]

        # Add percentage columns
        for col in cols_order:
            industry_reliance[f'{col}_Pct'] = round(industry_reliance_pct[col], 1)

        industry_reliance = industry_reliance.reset_index()

        industry_reliance_file = get_output_path(base_name, 'boxoffice', '_industry_reliance.csv')
        industry_reliance.to_csv(industry_reliance_file, index=False, float_format='%.2f')
        output_files['industry_reliance'] = industry_reliance_file
        print(f"    ✓ Industry reliance: {industry_reliance_file}")

    # === OUTPUT 6: Box Office Payment Type Details ===
    # Show breakdown of Card Present vs Cash
    boxoffice_only = booking_df[booking_df['Sales_Channel'] == 'Box Office']
    if len(boxoffice_only) > 0:
        payment_detail = boxoffice_only.groupby('PaymentType').agg({
            'BookingTransactionId': 'count',
            'TicketQuantity': 'sum',
            'PaymentReceived': 'sum',
            'TotalFees': 'sum'
        }).reset_index()

        payment_detail.columns = ['Payment_Type', 'Transactions', 'Tickets', 'Revenue', 'Fees']
        payment_detail['Avg_Tickets_Per_Transaction'] = round(
            payment_detail['Tickets'] / payment_detail['Transactions'], 2
        )
        payment_detail['Avg_Revenue_Per_Transaction'] = round(
            payment_detail['Revenue'] / payment_detail['Transactions'], 2
        )

        payment_file = get_output_path(base_name, 'boxoffice', '_payment_type_breakdown.csv')
        payment_detail.to_csv(payment_file, index=False, float_format='%.2f')
        output_files['payment_type_breakdown'] = payment_file
        print(f"    ✓ Payment type breakdown: {payment_file}")

    return output_files


def generate_ppc_cohort_analysis_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                      output_file: str) -> dict:
    """
    Generate PPC cohort analysis using GA4 conversion data.

    Identifies accounts acquired via PPC campaigns and analyses their:
    - LTV compared to organic accounts
    - Maturation curves (time to first event, tier progression)
    - Campaign ROI by industry segment

    Requires GA4_SERVICE_ACCOUNT_KEY and GA4_PROPERTY_ID environment variables.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    import os
    import json
    import re

    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    # Check for GA4 credentials
    ga4_key = os.environ.get('GA4_SERVICE_ACCOUNT_KEY')
    ga4_property = os.environ.get('GA4_PROPERTY_ID')

    if not ga4_key or not ga4_property:
        print("  ⚠ PPC analysis skipped: GA4 credentials not configured")
        print("    Set GA4_SERVICE_ACCOUNT_KEY and GA4_PROPERTY_ID to enable")
        return output_files

    print("  Generating PPC cohort analysis...")

    try:
        # Import GA4 libraries
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric,
            FilterExpression, FilterExpressionList, Filter
        )
        from google.oauth2 import service_account

        # Initialize GA4 client
        key_data = json.loads(ga4_key)
        credentials = service_account.Credentials.from_service_account_info(
            key_data,
            scopes=['https://www.googleapis.com/auth/analytics.readonly']
        )
        ga_client = BetaAnalyticsDataClient(credentials=credentials)

        # Load campaign configuration
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'ppc_campaigns.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
            campaigns = [c for c in config['campaigns'] if c.get('active', True)]

        campaign_names = {c['campaign_name'] for c in campaigns}
        print(f"    Tracking {len(campaign_names)} PPC campaigns")

        # Fetch GA4 conversion data (from June 2024 onwards)
        # Use firstUser dimensions for accurate acquisition attribution
        start_date = "2024-06-01"
        end_date = datetime.now(UK_TZ).strftime("%Y-%m-%d")

        # Build server-side filter for success pages (more efficient)
        page_filter = FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value="/uk/event/",
                    case_sensitive=False
                )
            )
        )
        success_filter = FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value="/success",
                    case_sensitive=False
                )
            )
        )
        dimension_filter = FilterExpression(
            and_group=FilterExpressionList(
                expressions=[page_filter, success_filter]
            )
        )

        request = RunReportRequest(
            property=f"properties/{ga4_property}",
            dimensions=[
                Dimension(name="pagePath"),
                Dimension(name="firstUserCampaignName"),
                Dimension(name="firstUserSource"),
                Dimension(name="firstUserMedium"),
                Dimension(name="date"),
            ],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            dimension_filter=dimension_filter,
        )

        response = ga_client.run_report(request)

        # Helper function to check if campaign is tracked (matches ppc_reporting.py logic)
        def is_tracked_campaign(campaign_name: str, source: str, medium: str) -> bool:
            """Check if a campaign should be tracked based on exact matching."""
            if not campaign_name or campaign_name == '(not set)':
                return False
            for campaign_config in campaigns:
                if campaign_config['campaign_name'] == campaign_name:
                    # If source/medium are specified in config, they must also match
                    if 'source' in campaign_config and source and campaign_config['source'] != source:
                        continue
                    if 'medium' in campaign_config and medium and campaign_config['medium'] != medium:
                        continue
                    return True
            return False

        # Parse response
        ga_data = []
        pattern = r'/uk/event/(\d+)/success'

        for row in response.rows:
            page_path = row.dimension_values[0].value
            campaign = row.dimension_values[1].value
            source = row.dimension_values[2].value
            medium = row.dimension_values[3].value
            date_str = row.dimension_values[4].value

            # Extract event ID from success page and validate campaign
            match = re.search(pattern, page_path, re.IGNORECASE)
            if match and is_tracked_campaign(campaign, source, medium):
                event_id = int(match.group(1))
                ga_data.append({
                    'EventId': event_id,
                    'campaign': campaign,
                    'source': source,
                    'medium': medium,
                    'conversion_date': pd.to_datetime(date_str, format='%Y%m%d'),
                    'sessions': int(row.metric_values[0].value),
                    'users': int(row.metric_values[1].value),
                })

        if not ga_data:
            print("    ⚠ No PPC conversions found in GA4 data")
            return output_files

        ga_df = pd.DataFrame(ga_data)
        print(f"    Found {len(ga_df)} PPC conversion records")

        # Get unique PPC event IDs
        ppc_event_ids = set(ga_df['EventId'].unique())

        # Match to booking data to get account IDs
        booking_df = booking_df.copy()
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])

        # Get accounts that have PPC events
        ppc_bookings = booking_df[booking_df['EventId'].isin(ppc_event_ids)].copy()

        if ppc_bookings.empty:
            print("    ⚠ No matching bookings found for PPC events")
            return output_files

        ppc_account_ids = set(ppc_bookings['AccountId'].unique())
        print(f"    Matched {len(ppc_account_ids)} accounts with PPC conversions")

        # Prepare account data
        accounts_df = accounts_df.copy()
        accounts_df['DateTimeCreated'] = pd.to_datetime(accounts_df['DateTimeCreated'])
        account_id_col = 'Account Id' if 'Account Id' in accounts_df.columns else 'AccountId'

        # Add PPC flag to accounts
        accounts_df['Is_PPC'] = accounts_df[account_id_col].isin(ppc_account_ids)

        # Add region
        if 'Postcode' in accounts_df.columns:
            postcode_areas = extract_postcode_areas_vectorized(accounts_df['Postcode'])
            accounts_df['Region'] = get_regions_vectorized(postcode_areas)
        else:
            accounts_df['Region'] = 'Unknown'

        # === ANALYSIS 1: PPC vs Organic LTV Comparison ===
        # Calculate 24-month LTV for both groups
        account_created = accounts_df.set_index(account_id_col)['DateTimeCreated'].to_dict()
        account_ppc = accounts_df.set_index(account_id_col)['Is_PPC'].to_dict()
        account_industry = accounts_df.set_index(account_id_col)['Industry'].to_dict()

        booking_df['AccountCreated'] = booking_df['AccountId'].map(account_created)
        booking_df['Is_PPC'] = booking_df['AccountId'].map(account_ppc)
        booking_df['DaysSinceCreation'] = (booking_df['TransactionDate'] - booking_df['AccountCreated']).dt.days

        # Filter to first 24 months
        booking_24m = booking_df[
            (booking_df['DaysSinceCreation'] >= 0) &
            (booking_df['DaysSinceCreation'] <= 730)
        ].copy()

        # Calculate LTV by acquisition channel
        ltv_by_channel = booking_24m.groupby(['AccountId', 'Is_PPC']).agg({
            'BookingFee': 'sum',
            'CardFee': 'sum',
            'TicketFee': 'sum',
            'ProcessingFee': 'sum',
            'TicketQuantity': 'sum',
            'PaymentReceived': 'sum',
            'EventId': 'nunique',
        }).reset_index()

        ltv_by_channel['Total_Fees_24m'] = (
            ltv_by_channel['BookingFee'] + ltv_by_channel['CardFee'] +
            ltv_by_channel['TicketFee'] + ltv_by_channel['ProcessingFee']
        )

        # Summarise by channel
        channel_summary = ltv_by_channel.groupby('Is_PPC').agg({
            'AccountId': 'count',
            'Total_Fees_24m': ['sum', 'mean', 'median'],
            'PaymentReceived': ['sum', 'mean'],
            'EventId': ['sum', 'mean'],
        }).reset_index()

        channel_summary.columns = [
            'Is_PPC', 'Accounts', 'Total_Fees', 'Avg_LTV_24m', 'Median_LTV_24m',
            'Total_Revenue', 'Avg_Revenue', 'Total_Events', 'Avg_Events'
        ]
        channel_summary['Channel'] = channel_summary['Is_PPC'].map({True: 'PPC', False: 'Organic'})
        channel_summary = channel_summary[['Channel'] + [c for c in channel_summary.columns if c not in ['Is_PPC', 'Channel']]]

        ltv_file = get_output_path(base_name, 'ppc', '_ppc_vs_organic_ltv.csv')
        channel_summary.to_csv(ltv_file, index=False, float_format='%.2f')
        output_files['ppc_vs_organic_ltv'] = ltv_file
        print(f"    ✓ PPC vs Organic LTV: {ltv_file}")

        # === ANALYSIS 2: PPC Account Maturation ===
        # Track time to first event for PPC accounts
        ppc_accounts_df = accounts_df[accounts_df['Is_PPC']].copy()

        # Get first event date for each PPC account
        first_event = ppc_bookings.groupby('AccountId')['TransactionDate'].min().reset_index()
        first_event.columns = ['AccountId', 'First_Transaction']

        ppc_maturation = ppc_accounts_df.merge(
            first_event.rename(columns={'AccountId': account_id_col}),
            on=account_id_col, how='left'
        )

        ppc_maturation['Days_To_First_Sale'] = (
            ppc_maturation['First_Transaction'] - ppc_maturation['DateTimeCreated']
        ).dt.days

        # Maturation summary
        maturation_stats = pd.DataFrame({
            'Metric': [
                'Total PPC Accounts',
                'Accounts with Sales',
                'Activation Rate %',
                'Avg Days to First Sale',
                'Median Days to First Sale',
                'Within 7 Days %',
                'Within 30 Days %',
                'Within 90 Days %',
            ],
            'Value': [
                len(ppc_maturation),
                ppc_maturation['First_Transaction'].notna().sum(),
                round(ppc_maturation['First_Transaction'].notna().sum() / len(ppc_maturation) * 100, 1),
                round(ppc_maturation['Days_To_First_Sale'].mean(), 1),
                ppc_maturation['Days_To_First_Sale'].median(),
                round((ppc_maturation['Days_To_First_Sale'] <= 7).sum() / len(ppc_maturation) * 100, 1),
                round((ppc_maturation['Days_To_First_Sale'] <= 30).sum() / len(ppc_maturation) * 100, 1),
                round((ppc_maturation['Days_To_First_Sale'] <= 90).sum() / len(ppc_maturation) * 100, 1),
            ]
        })

        maturation_file = get_output_path(base_name, 'ppc', '_ppc_maturation_stats.csv')
        maturation_stats.to_csv(maturation_file, index=False)
        output_files['ppc_maturation_stats'] = maturation_file
        print(f"    ✓ PPC maturation stats: {maturation_file}")

        # === ANALYSIS 3: Campaign ROI by Industry ===
        # Merge campaign data with account industry
        ga_with_account = ga_df.merge(
            ppc_bookings[['EventId', 'AccountId']].drop_duplicates(),
            on='EventId', how='left'
        )
        ga_with_account['Industry'] = ga_with_account['AccountId'].map(account_industry)

        # Get total fees by campaign and industry
        ppc_fees = ppc_bookings.groupby(['AccountId', 'EventId']).agg({
            'BookingFee': 'sum',
            'CardFee': 'sum',
            'TicketFee': 'sum',
            'ProcessingFee': 'sum',
        }).reset_index()
        ppc_fees['Total_Fees'] = (
            ppc_fees['BookingFee'] + ppc_fees['CardFee'] +
            ppc_fees['TicketFee'] + ppc_fees['ProcessingFee']
        )

        ga_with_fees = ga_with_account.merge(
            ppc_fees[['EventId', 'Total_Fees']],
            on='EventId', how='left'
        )

        # Aggregate by campaign and industry
        campaign_industry = ga_with_fees.groupby(['campaign', 'Industry']).agg({
            'EventId': 'nunique',
            'AccountId': 'nunique',
            'sessions': 'sum',
            'Total_Fees': 'sum',
        }).reset_index()

        campaign_industry.columns = ['Campaign', 'Industry', 'Events', 'Accounts', 'Sessions', 'Total_Fees']
        campaign_industry['Fees_Per_Account'] = round(
            campaign_industry['Total_Fees'] / campaign_industry['Accounts'].replace(0, 1), 2
        )
        campaign_industry = campaign_industry.sort_values(['Campaign', 'Total_Fees'], ascending=[True, False])

        roi_file = get_output_path(base_name, 'ppc', '_campaign_roi_by_industry.csv')
        campaign_industry.to_csv(roi_file, index=False, float_format='%.2f')
        output_files['campaign_roi_by_industry'] = roi_file
        print(f"    ✓ Campaign ROI by industry: {roi_file}")

        # === ANALYSIS 4: PPC Summary by Campaign ===
        campaign_summary = ga_with_fees.groupby('campaign').agg({
            'EventId': 'nunique',
            'AccountId': 'nunique',
            'sessions': 'sum',
            'users': 'sum',
            'Total_Fees': 'sum',
        }).reset_index()

        campaign_summary.columns = ['Campaign', 'Unique_Events', 'Unique_Accounts', 'Total_Sessions',
                                     'Total_Users', 'Total_Fees']
        campaign_summary['Fees_Per_Account'] = round(
            campaign_summary['Total_Fees'] / campaign_summary['Unique_Accounts'].replace(0, 1), 2
        )
        campaign_summary = campaign_summary.sort_values('Total_Fees', ascending=False)

        summary_file = get_output_path(base_name, 'ppc', '_campaign_summary.csv')
        campaign_summary.to_csv(summary_file, index=False, float_format='%.2f')
        output_files['campaign_summary'] = summary_file
        print(f"    ✓ Campaign summary: {summary_file}")

    except ImportError as e:
        print(f"    ⚠ PPC analysis skipped: Missing dependency - {e}")
        print("    Install: pip install google-analytics-data google-auth")
        return output_files
    except Exception as e:
        print(f"    ⚠ PPC analysis error: {e}")
        import traceback
        traceback.print_exc()
        return output_files

    return output_files


def generate_gateway_by_geography_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                       output_file: str) -> dict:
    """
    Generate gateway usage analysis by geographic region.

    Shows Default vs Stripe Connect adoption rates across UK regions.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating gateway by geography analysis...")

    # Check for gateway column
    gateway_col = None
    for col in ['GatewayName', 'Gateway Group', 'GatewayGroup']:
        if col in booking_df.columns:
            gateway_col = col
            break

    if gateway_col is None:
        print("    ⚠ No gateway column found - skipping")
        return output_files

    booking_df = booking_df.copy()

    # Standardise gateway names
    def standardise_gateway(gateway):
        if pd.isna(gateway):
            return 'Unknown'
        gateway_upper = str(gateway).upper()
        if 'STRIPE' in gateway_upper and 'CONNECT' in gateway_upper:
            return 'Stripe Connect'
        elif 'STRIPE' in gateway_upper:
            return 'Stripe'
        elif 'PAYPAL' in gateway_upper:
            return 'PayPal'
        elif 'DEFAULT' in gateway_upper or gateway_upper == 'TRYBOOKING':
            return 'Default'
        return str(gateway)

    booking_df['Gateway'] = booking_df[gateway_col].apply(standardise_gateway)

    # Add region from postcode
    postcode_col = 'EventPostcode' if 'EventPostcode' in booking_df.columns else 'AccountPostcode'
    if postcode_col in booking_df.columns:
        booking_df['PostcodeArea'] = extract_postcode_areas_vectorized(booking_df[postcode_col])
        booking_df['Region'] = get_regions_vectorized(booking_df['PostcodeArea'])
        booking_df = booking_df[booking_df['Region'] != 'Unknown']

        # Aggregate by region and gateway
        region_gateway = booking_df.groupby(['Region', 'Gateway']).agg({
            'TotalFees': 'sum' if 'TotalFees' in booking_df.columns else 'count',
            'AccountId': 'nunique'
        }).reset_index()

        if 'TotalFees' in booking_df.columns:
            region_gateway.columns = ['Region', 'Gateway', 'Fees', 'Accounts']
        else:
            region_gateway.columns = ['Region', 'Gateway', 'Transactions', 'Accounts']

        # Pivot for side-by-side view
        value_col = 'Fees' if 'Fees' in region_gateway.columns else 'Transactions'
        pivot = region_gateway.pivot(index='Region', columns='Gateway', values=value_col).fillna(0)
        pivot['Total'] = pivot.sum(axis=1)

        # Calculate percentages
        for col in pivot.columns:
            if col != 'Total':
                pivot[f'{col} %'] = round(pivot[col] / pivot['Total'] * 100, 1)

        pivot = pivot.sort_values('Total', ascending=False)

        geo_file = get_output_path(base_name, 'geography', '_gateway_by_region.csv')
        pivot.to_csv(geo_file, float_format='%.2f')
        output_files['gateway_by_region'] = geo_file
        print(f"    ✓ Gateway by region: {geo_file}")

    return output_files


def generate_organiser_concentration_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                          output_file: str, account_tiers: dict = None) -> dict:
    """
    Generate organiser concentration analysis by tier.

    Shows what percentage of fees come from each tier level.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename
        account_tiers: Optional dict mapping AccountId to tier

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating organiser concentration analysis...")

    booking_df = booking_df.copy()

    # Get account tiers if not provided
    if account_tiers is None:
        # Calculate tiers
        account_tiers = calculate_account_tiers(accounts_df, booking_df)

    # Map tiers to bookings
    booking_df['Tier'] = booking_df['AccountId'].map(account_tiers).fillna('Untiered')

    # Aggregate by tier
    tier_summary = booking_df.groupby('Tier').agg({
        'TotalFees': 'sum' if 'TotalFees' in booking_df.columns else 'count',
        'PaymentReceived': 'sum' if 'PaymentReceived' in booking_df.columns else 'count',
        'TicketQuantity': 'sum' if 'TicketQuantity' in booking_df.columns else 'count',
        'AccountId': 'nunique',
        'EventId': 'nunique'
    }).reset_index()

    tier_summary.columns = ['Tier', 'Total Fees', 'Total Revenue', 'Total Tickets', 'Accounts', 'Events']

    # Calculate totals for percentages
    total_fees = tier_summary['Total Fees'].sum()
    total_revenue = tier_summary['Total Revenue'].sum()
    total_accounts = tier_summary['Accounts'].sum()

    tier_summary['Fees %'] = round(tier_summary['Total Fees'] / total_fees * 100, 1)
    tier_summary['Revenue %'] = round(tier_summary['Total Revenue'] / total_revenue * 100, 1)
    tier_summary['Accounts %'] = round(tier_summary['Accounts'] / total_accounts * 100, 1)
    tier_summary['Avg Fees Per Account'] = round(tier_summary['Total Fees'] / tier_summary['Accounts'], 2)

    # Order tiers logically
    tier_order = ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'Untiered']
    tier_summary['Order'] = tier_summary['Tier'].apply(
        lambda x: tier_order.index(x) if x in tier_order else 99
    )
    tier_summary = tier_summary.sort_values('Order').drop('Order', axis=1)

    # Calculate cumulative concentration
    tier_summary['Cumulative Fees %'] = tier_summary['Fees %'].cumsum()
    tier_summary['Cumulative Accounts %'] = tier_summary['Accounts %'].cumsum()

    concentration_file = get_output_path(base_name, 'cohorts', '_tier_concentration.csv')
    tier_summary.to_csv(concentration_file, index=False, float_format='%.2f')
    output_files['tier_concentration'] = concentration_file
    print(f"    ✓ Tier concentration: {concentration_file}")

    # === OUTPUT 2: YoY Concentration Comparison (2024 vs 2025) ===
    # Compare concentration by tier for each calendar year

    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df['Year'] = booking_df['TransactionDate'].dt.year

    # Calculate TotalFees if not present
    if 'TotalFees' not in booking_df.columns:
        fee_cols = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        available_fees = [c for c in fee_cols if c in booking_df.columns]
        if available_fees:
            booking_df['TotalFees'] = booking_df[available_fees].fillna(0).sum(axis=1)
        else:
            booking_df['TotalFees'] = 0

    def calculate_year_concentration(year_bookings, year_label):
        """Calculate tier concentration for a specific year's bookings."""
        if len(year_bookings) == 0:
            return pd.DataFrame()

        # Calculate tiers based on this year's activity only
        year_account_fees = year_bookings.groupby('AccountId')['TotalFees'].sum().reset_index()
        year_account_fees.columns = ['AccountId', 'YearFees']
        year_account_fees = year_account_fees.sort_values('YearFees', ascending=False)

        # Assign tiers based on percentiles
        total_accounts = len(year_account_fees)
        year_account_fees['Rank'] = range(1, total_accounts + 1)
        year_account_fees['Percentile'] = year_account_fees['Rank'] / total_accounts * 100

        def assign_tier(row):
            if row['Rank'] <= 65:
                return 'Key Account'
            elif row['Percentile'] <= 3.5:
                return 'Tier 4+'
            elif row['Percentile'] <= 16:
                return 'Tier 3'
            elif row['Percentile'] <= 50:
                return 'Tier 2'
            elif row['YearFees'] > 0:
                return 'Tier 1'
            else:
                return 'NIL'

        year_account_fees['Tier'] = year_account_fees.apply(assign_tier, axis=1)

        # Aggregate by tier
        tier_agg = year_account_fees.groupby('Tier').agg({
            'AccountId': 'count',
            'YearFees': 'sum'
        }).reset_index()
        tier_agg.columns = ['Tier', f'{year_label} Accounts', f'{year_label} Fees']

        total_accounts = tier_agg[f'{year_label} Accounts'].sum()
        total_fees = tier_agg[f'{year_label} Fees'].sum()

        tier_agg[f'{year_label} Account %'] = round(tier_agg[f'{year_label} Accounts'] / total_accounts * 100, 1)
        tier_agg[f'{year_label} Fees %'] = round(tier_agg[f'{year_label} Fees'] / total_fees * 100, 1) if total_fees > 0 else 0

        return tier_agg, year_account_fees, total_accounts, total_fees

    # Calculate for 2024 and 2025
    bookings_2024 = booking_df[booking_df['Year'] == 2024]
    bookings_2025 = booking_df[booking_df['Year'] == 2025]

    result_2024, accounts_2024, total_acc_2024, total_fees_2024 = calculate_year_concentration(bookings_2024, '2024')
    result_2025, accounts_2025, total_acc_2025, total_fees_2025 = calculate_year_concentration(bookings_2025, '2025')

    # Merge results
    tier_order = ['Key Account', 'Tier 4+', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']

    yoy_data = []
    for tier in tier_order:
        row = {'Tier': tier}

        # 2024 data
        tier_2024 = result_2024[result_2024['Tier'] == tier] if len(result_2024) > 0 else pd.DataFrame()
        row['2024 Accounts'] = int(tier_2024['2024 Accounts'].values[0]) if len(tier_2024) > 0 else 0
        row['2024 Account %'] = float(tier_2024['2024 Account %'].values[0]) if len(tier_2024) > 0 else 0
        row['2024 Fees'] = float(tier_2024['2024 Fees'].values[0]) if len(tier_2024) > 0 else 0
        row['2024 Fees %'] = float(tier_2024['2024 Fees %'].values[0]) if len(tier_2024) > 0 else 0

        # 2025 data
        tier_2025 = result_2025[result_2025['Tier'] == tier] if len(result_2025) > 0 else pd.DataFrame()
        row['2025 Accounts'] = int(tier_2025['2025 Accounts'].values[0]) if len(tier_2025) > 0 else 0
        row['2025 Account %'] = float(tier_2025['2025 Account %'].values[0]) if len(tier_2025) > 0 else 0
        row['2025 Fees'] = float(tier_2025['2025 Fees'].values[0]) if len(tier_2025) > 0 else 0
        row['2025 Fees %'] = float(tier_2025['2025 Fees %'].values[0]) if len(tier_2025) > 0 else 0

        yoy_data.append(row)

    # Add concentration metrics rows
    # Top 65 accounts
    if len(accounts_2024) > 0:
        top65_2024 = accounts_2024.head(65)
        top65_fees_2024 = top65_2024['YearFees'].sum()
        top65_pct_2024 = round(top65_fees_2024 / total_fees_2024 * 100, 1) if total_fees_2024 > 0 else 0
    else:
        top65_fees_2024, top65_pct_2024 = 0, 0

    if len(accounts_2025) > 0:
        top65_2025 = accounts_2025.head(65)
        top65_fees_2025 = top65_2025['YearFees'].sum()
        top65_pct_2025 = round(top65_fees_2025 / total_fees_2025 * 100, 1) if total_fees_2025 > 0 else 0
    else:
        top65_fees_2025, top65_pct_2025 = 0, 0

    yoy_data.append({
        'Tier': 'Top 65 Accounts',
        '2024 Accounts': 65 if total_acc_2024 >= 65 else total_acc_2024,
        '2024 Account %': round(65 / total_acc_2024 * 100, 1) if total_acc_2024 >= 65 else 100,
        '2024 Fees': round(top65_fees_2024, 2),
        '2024 Fees %': top65_pct_2024,
        '2025 Accounts': 65 if total_acc_2025 >= 65 else total_acc_2025,
        '2025 Account %': round(65 / total_acc_2025 * 100, 1) if total_acc_2025 >= 65 else 100,
        '2025 Fees': round(top65_fees_2025, 2),
        '2025 Fees %': top65_pct_2025,
    })

    # Top 3.5% of accounts
    if len(accounts_2024) > 0:
        top_3_5_count_2024 = max(1, int(total_acc_2024 * 0.035))
        top_3_5_2024 = accounts_2024.head(top_3_5_count_2024)
        top_3_5_fees_2024 = top_3_5_2024['YearFees'].sum()
        top_3_5_pct_2024 = round(top_3_5_fees_2024 / total_fees_2024 * 100, 1) if total_fees_2024 > 0 else 0
    else:
        top_3_5_count_2024, top_3_5_fees_2024, top_3_5_pct_2024 = 0, 0, 0

    if len(accounts_2025) > 0:
        top_3_5_count_2025 = max(1, int(total_acc_2025 * 0.035))
        top_3_5_2025 = accounts_2025.head(top_3_5_count_2025)
        top_3_5_fees_2025 = top_3_5_2025['YearFees'].sum()
        top_3_5_pct_2025 = round(top_3_5_fees_2025 / total_fees_2025 * 100, 1) if total_fees_2025 > 0 else 0
    else:
        top_3_5_count_2025, top_3_5_fees_2025, top_3_5_pct_2025 = 0, 0, 0

    yoy_data.append({
        'Tier': 'Top 3.5%',
        '2024 Accounts': top_3_5_count_2024,
        '2024 Account %': 3.5,
        '2024 Fees': round(top_3_5_fees_2024, 2),
        '2024 Fees %': top_3_5_pct_2024,
        '2025 Accounts': top_3_5_count_2025,
        '2025 Account %': 3.5,
        '2025 Fees': round(top_3_5_fees_2025, 2),
        '2025 Fees %': top_3_5_pct_2025,
    })

    # Top 16% of accounts
    if len(accounts_2024) > 0:
        top_16_count_2024 = max(1, int(total_acc_2024 * 0.16))
        top_16_2024 = accounts_2024.head(top_16_count_2024)
        top_16_fees_2024 = top_16_2024['YearFees'].sum()
        top_16_pct_2024 = round(top_16_fees_2024 / total_fees_2024 * 100, 1) if total_fees_2024 > 0 else 0
    else:
        top_16_count_2024, top_16_fees_2024, top_16_pct_2024 = 0, 0, 0

    if len(accounts_2025) > 0:
        top_16_count_2025 = max(1, int(total_acc_2025 * 0.16))
        top_16_2025 = accounts_2025.head(top_16_count_2025)
        top_16_fees_2025 = top_16_2025['YearFees'].sum()
        top_16_pct_2025 = round(top_16_fees_2025 / total_fees_2025 * 100, 1) if total_fees_2025 > 0 else 0
    else:
        top_16_count_2025, top_16_fees_2025, top_16_pct_2025 = 0, 0, 0

    yoy_data.append({
        'Tier': 'Top 16%',
        '2024 Accounts': top_16_count_2024,
        '2024 Account %': 16.0,
        '2024 Fees': round(top_16_fees_2024, 2),
        '2024 Fees %': top_16_pct_2024,
        '2025 Accounts': top_16_count_2025,
        '2025 Account %': 16.0,
        '2025 Fees': round(top_16_fees_2025, 2),
        '2025 Fees %': top_16_pct_2025,
    })

    yoy_concentration_df = pd.DataFrame(yoy_data)
    yoy_concentration_file = get_output_path(base_name, 'cohorts', '_concentration_yoy_comparison.csv')
    yoy_concentration_df.to_csv(yoy_concentration_file, index=False, float_format='%.2f')
    output_files['concentration_yoy_comparison'] = yoy_concentration_file
    print(f"    ✓ Concentration YoY comparison: {yoy_concentration_file}")

    return output_files


def generate_cohort_quality_by_month_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                          output_file: str) -> dict:
    """
    Generate cohort quality analysis by signup month.

    Shows activation rates, avg revenue, etc. by month of account creation.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating cohort quality by signup month...")

    accounts_df = accounts_df.copy()
    booking_df = booking_df.copy()

    # Extract signup month
    accounts_df['SignupMonth'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.month
    accounts_df['SignupYear'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.year

    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Account Id'

    # Get accounts with events (activated)
    accounts_with_events = accounts_df[accounts_df['FirstEventCreation'].notna()][account_id_col].unique()

    # Get accounts with sales
    accounts_with_sales = booking_df['AccountId'].unique()

    # Calculate metrics per signup month
    monthly_quality = []

    for month in range(1, 13):
        month_accounts = accounts_df[accounts_df['SignupMonth'] == month]
        total_accounts = len(month_accounts)

        if total_accounts == 0:
            continue

        month_account_ids = set(month_accounts[account_id_col].astype(float).dropna())

        # Activation metrics
        activated = len([a for a in month_account_ids if a in accounts_with_events])
        with_sales = len([a for a in month_account_ids if a in accounts_with_sales])

        # Revenue from these accounts
        month_bookings = booking_df[booking_df['AccountId'].isin(month_account_ids)]
        total_fees = month_bookings['TotalFees'].sum() if 'TotalFees' in month_bookings.columns else 0
        total_revenue = month_bookings['PaymentReceived'].sum() if 'PaymentReceived' in month_bookings.columns else 0

        monthly_quality.append({
            'Signup Month': calendar.month_name[month],
            'Month Num': month,
            'Total Accounts': total_accounts,
            'Activated (Events)': activated,
            'With Sales': with_sales,
            'Activation Rate %': round(activated / total_accounts * 100, 1),
            'Sales Rate %': round(with_sales / total_accounts * 100, 1),
            'Total Fees': round(total_fees, 2),
            'Total Revenue': round(total_revenue, 2),
            'Avg Fees Per Account': round(total_fees / total_accounts, 2),
            'Avg Fees Per Active': round(total_fees / with_sales, 2) if with_sales > 0 else 0,
        })

    quality_df = pd.DataFrame(monthly_quality)
    quality_df = quality_df.sort_values('Month Num')

    quality_file = get_output_path(base_name, 'cohorts', '_cohort_quality_by_month.csv')
    quality_df.to_csv(quality_file, index=False, float_format='%.2f')
    output_files['cohort_quality_by_month'] = quality_file
    print(f"    ✓ Cohort quality by month: {quality_file}")

    return output_files


def generate_outreach_calendar_csv(booking_df: pd.DataFrame, output_file: str) -> dict:
    """
    Generate outreach calendar based on keyword timing analysis.

    Shows recommended outreach dates for different event types.

    Args:
        booking_df: Booking transactions DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating outreach calendar...")

    if 'EventName' not in booking_df.columns or 'EventDate' not in booking_df.columns:
        print("    ⚠ Missing EventName or EventDate - skipping")
        return output_files

    booking_df = booking_df.copy()
    booking_df['EventDate'] = pd.to_datetime(booking_df['EventDate'], errors='coerce')
    booking_df['EventMonth'] = booking_df['EventDate'].dt.month

    # Get top keywords by fees
    from modules.event_keyword_analysis import extract_keywords

    # Aggregate to event level
    events = booking_df.groupby('EventId').agg({
        'EventName': 'first',
        'EventMonth': 'first',
        'TotalFees': 'sum' if 'TotalFees' in booking_df.columns else 'count',
        'TransactionDate': 'min'
    }).reset_index()

    events['TransactionDate'] = pd.to_datetime(events['TransactionDate'])
    events['EventDate'] = pd.to_datetime(booking_df.groupby('EventId')['EventDate'].first())

    # Calculate lead time
    events['LeadDays'] = (events['EventDate'] - events['TransactionDate']).dt.days
    events = events[events['LeadDays'] >= 0]

    # Extract keywords and explode to one row per keyword
    events['Keywords'] = events['EventName'].apply(extract_keywords)
    events_exploded = events.explode('Keywords')
    events_exploded = events_exploded[events_exploded['Keywords'].notna()]
    events_exploded['Keywords'] = events_exploded['Keywords'].astype(str)

    # Aggregate by keyword using vectorised operations
    keyword_agg = events_exploded.groupby('Keywords').agg({
        'EventMonth': list,
        'LeadDays': lambda x: [v for v in x if pd.notna(v)],
        'TotalFees': 'sum'
    }).reset_index()
    keyword_agg.columns = ['Keyword', 'months', 'lead_days', 'fees']

    # Get top 50 keywords by fees
    keyword_agg = keyword_agg.nlargest(50, 'fees')
    calendar_data = []

    for _, row in keyword_agg.iterrows():
        keyword = row['Keyword']
        months = row['months']
        lead_days_list = row['lead_days']

        if not months or not lead_days_list:
            continue

        # Find peak month(s)
        month_counts = pd.Series(months).value_counts()
        peak_month = int(month_counts.index[0])

        # Calculate recommended lead time (75th percentile)
        lead_days_sorted = sorted(lead_days_list)
        recommended_lead = int(np.percentile(lead_days_sorted, 75)) if lead_days_sorted else 30

        # Calculate outreach month
        outreach_month = peak_month - (recommended_lead // 30)
        if outreach_month <= 0:
            outreach_month += 12

        calendar_data.append({
            'Keyword': keyword,
            'Peak Event Month': calendar.month_name[peak_month],
            'Peak Month Num': peak_month,
            'Recommended Lead (days)': recommended_lead,
            'Recommended Lead (weeks)': round(recommended_lead / 7),
            'Start Outreach Month': calendar.month_name[outreach_month],
            'Outreach Month Num': outreach_month,
            'Total Fees': round(row['fees'], 2),
            'Event Count': len(months)
        })

    calendar_df = pd.DataFrame(calendar_data)
    calendar_df = calendar_df.sort_values('Outreach Month Num')

    calendar_file = get_output_path(base_name, 'planning', '_outreach_calendar.csv')
    calendar_df.to_csv(calendar_file, index=False, float_format='%.2f')
    output_files['outreach_calendar'] = calendar_file
    print(f"    ✓ Outreach calendar: {calendar_file}")

    # Also create monthly summary
    monthly_outreach = calendar_df.groupby('Outreach Month Num').agg({
        'Keyword': lambda x: ', '.join(x[:5]),  # Top 5 keywords
        'Total Fees': 'sum'
    }).reset_index()
    monthly_outreach['Month'] = monthly_outreach['Outreach Month Num'].apply(lambda m: calendar.month_name[int(m)])
    monthly_outreach = monthly_outreach[['Month', 'Keyword', 'Total Fees']]
    monthly_outreach.columns = ['Month', 'Top Keywords to Target', 'Potential Fees']

    monthly_file = get_output_path(base_name, 'planning', '_monthly_outreach_focus.csv')
    monthly_outreach.to_csv(monthly_file, index=False, float_format='%.2f')
    output_files['monthly_outreach'] = monthly_file
    print(f"    ✓ Monthly outreach focus: {monthly_file}")

    return output_files


def generate_price_band_analysis_csv(booking_df: pd.DataFrame, output_file: str) -> dict:
    """
    Generate price band analysis.

    Price bands: Free, <£10, £10-25, £25-50, £50+

    Args:
        booking_df: Booking transactions DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating price band analysis...")

    if 'PaymentReceived' not in booking_df.columns or 'TicketQuantity' not in booking_df.columns:
        print("    ⚠ Missing price data - skipping")
        return output_files

    booking_df = booking_df.copy()

    # Calculate price per ticket at event level
    events = booking_df.groupby('EventId').agg({
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'TotalFees': 'sum' if 'TotalFees' in booking_df.columns else 'count',
        'Industry': 'first' if 'Industry' in booking_df.columns else 'count',
        'AccountId': 'first'
    }).reset_index()

    events['AvgTicketPrice'] = events['PaymentReceived'] / events['TicketQuantity'].replace(0, 1)

    # Classify into price bands
    def classify_price_band(price):
        if price == 0:
            return 'Free'
        elif price < 10:
            return '£1-£9.99'
        elif price < 25:
            return '£10-£24.99'
        elif price < 50:
            return '£25-£49.99'
        else:
            return '£50+'

    events['Price Band'] = events['AvgTicketPrice'].apply(classify_price_band)

    # Summary by price band
    band_summary = events.groupby('Price Band').agg({
        'EventId': 'count',
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'TotalFees': 'sum',
        'AccountId': 'nunique'
    }).reset_index()

    band_summary.columns = ['Price Band', 'Events', 'Tickets', 'Revenue', 'Fees', 'Accounts']

    # Order bands logically
    band_order = ['Free', '£1-£9.99', '£10-£24.99', '£25-£49.99', '£50+']
    band_summary['Order'] = band_summary['Price Band'].apply(
        lambda x: band_order.index(x) if x in band_order else 99
    )
    band_summary = band_summary.sort_values('Order').drop('Order', axis=1)

    # Calculate percentages
    total_events = band_summary['Events'].sum()
    total_fees = band_summary['Fees'].sum()
    band_summary['Events %'] = round(band_summary['Events'] / total_events * 100, 1)
    band_summary['Fees %'] = round(band_summary['Fees'] / total_fees * 100, 1)
    band_summary['Avg Fees Per Event'] = round(band_summary['Fees'] / band_summary['Events'], 2)

    band_file = get_output_path(base_name, 'industry', '_price_band_summary.csv')
    band_summary.to_csv(band_file, index=False, float_format='%.2f')
    output_files['price_band_summary'] = band_file
    print(f"    ✓ Price band summary: {band_file}")

    # Price band by industry
    if 'Industry' in events.columns and events['Industry'].dtype == 'object':
        industry_band = events.groupby(['Industry', 'Price Band']).agg({
            'EventId': 'count',
            'TotalFees': 'sum'
        }).reset_index()
        industry_band.columns = ['Industry', 'Price Band', 'Events', 'Fees']

        # Pivot
        pivot = industry_band.pivot(index='Industry', columns='Price Band', values='Fees').fillna(0)
        pivot = pivot[[c for c in band_order if c in pivot.columns]]
        pivot['Total'] = pivot.sum(axis=1)
        pivot = pivot.sort_values('Total', ascending=False)

        industry_file = get_output_path(base_name, 'industry', '_price_band_by_industry.csv')
        pivot.to_csv(industry_file, float_format='%.2f')
        output_files['price_band_by_industry'] = industry_file
        print(f"    ✓ Price band by industry: {industry_file}")

    return output_files


def generate_fee_structure_analysis_csv(booking_df: pd.DataFrame, output_file: str) -> dict:
    """
    Generate fee structure analysis (free vs paid events) by industry.

    Args:
        booking_df: Booking transactions DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating fee structure analysis...")

    if 'PaymentReceived' not in booking_df.columns:
        print("    ⚠ Missing PaymentReceived - skipping")
        return output_files

    booking_df = booking_df.copy()

    # Aggregate to event level
    events = booking_df.groupby('EventId').agg({
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'TotalFees': 'sum' if 'TotalFees' in booking_df.columns else 'count',
        'Industry': 'first' if 'Industry' in booking_df.columns else 'count',
        'AccountId': 'first'
    }).reset_index()

    # Classify as free or paid
    events['Event Type'] = events['PaymentReceived'].apply(lambda x: 'Free' if x == 0 else 'Paid')

    # Summary by industry
    if 'Industry' in events.columns and events['Industry'].dtype == 'object':
        industry_fee = events.groupby(['Industry', 'Event Type']).agg({
            'EventId': 'count',
            'TicketQuantity': 'sum',
            'TotalFees': 'sum'
        }).reset_index()
        industry_fee.columns = ['Industry', 'Event Type', 'Events', 'Tickets', 'Fees']

        # Pivot for side-by-side
        events_pivot = industry_fee.pivot(index='Industry', columns='Event Type', values='Events').fillna(0)
        fees_pivot = industry_fee.pivot(index='Industry', columns='Event Type', values='Fees').fillna(0)

        # Combine
        result = pd.DataFrame(index=events_pivot.index)
        result['Free Events'] = events_pivot.get('Free', 0)
        result['Paid Events'] = events_pivot.get('Paid', 0)
        result['Total Events'] = result['Free Events'] + result['Paid Events']
        result['Free %'] = round(result['Free Events'] / result['Total Events'] * 100, 1)
        result['Paid %'] = round(result['Paid Events'] / result['Total Events'] * 100, 1)
        result['Free Fees'] = fees_pivot.get('Free', 0)
        result['Paid Fees'] = fees_pivot.get('Paid', 0)
        result['Total Fees'] = result['Free Fees'] + result['Paid Fees']

        result = result.sort_values('Total Fees', ascending=False)

        fee_file = get_output_path(base_name, 'industry', '_free_vs_paid_by_industry.csv')
        result.to_csv(fee_file, float_format='%.2f')
        output_files['free_vs_paid_by_industry'] = fee_file
        print(f"    ✓ Free vs paid by industry: {fee_file}")

    return output_files


def generate_activation_by_month_csv(accounts_df: pd.DataFrame, booking_df: pd.DataFrame,
                                      output_file: str) -> dict:
    """
    Generate activation rate analysis by signup month.

    Shows how quickly accounts activate based on signup month.

    Args:
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating activation by signup month...")

    accounts_df = accounts_df.copy()

    accounts_df['SignupMonth'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.month
    accounts_df['SignupDate'] = pd.to_datetime(accounts_df['DateTimeCreated'])

    # Calculate days to first event
    if 'FirstEventCreation' in accounts_df.columns:
        accounts_df['FirstEventDate'] = pd.to_datetime(accounts_df['FirstEventCreation'], errors='coerce')
        accounts_df['DaysToFirstEvent'] = (accounts_df['FirstEventDate'] - accounts_df['SignupDate']).dt.days
        accounts_df.loc[accounts_df['DaysToFirstEvent'] < 0, 'DaysToFirstEvent'] = None

    # Aggregate by signup month
    monthly_activation = []

    for month in range(1, 13):
        month_accounts = accounts_df[accounts_df['SignupMonth'] == month]
        total = len(month_accounts)

        if total == 0:
            continue

        activated = month_accounts['FirstEventCreation'].notna().sum()
        within_7 = (month_accounts['DaysToFirstEvent'] <= 7).sum() if 'DaysToFirstEvent' in month_accounts.columns else 0
        within_30 = (month_accounts['DaysToFirstEvent'] <= 30).sum() if 'DaysToFirstEvent' in month_accounts.columns else 0
        within_90 = (month_accounts['DaysToFirstEvent'] <= 90).sum() if 'DaysToFirstEvent' in month_accounts.columns else 0

        avg_days = month_accounts['DaysToFirstEvent'].mean() if 'DaysToFirstEvent' in month_accounts.columns else None

        monthly_activation.append({
            'Signup Month': calendar.month_name[month],
            'Month Num': month,
            'Total Accounts': total,
            'Activated': activated,
            'Activation Rate %': round(activated / total * 100, 1),
            'Within 7 Days': within_7,
            'Within 7 Days %': round(within_7 / total * 100, 1),
            'Within 30 Days': within_30,
            'Within 30 Days %': round(within_30 / total * 100, 1),
            'Within 90 Days': within_90,
            'Within 90 Days %': round(within_90 / total * 100, 1),
            'Avg Days to Activate': round(avg_days, 1) if avg_days else None
        })

    activation_df = pd.DataFrame(monthly_activation)
    activation_df = activation_df.sort_values('Month Num')

    activation_file = get_output_path(base_name, 'cohorts', '_activation_by_signup_month.csv')
    activation_df.to_csv(activation_file, index=False, float_format='%.1f')
    output_files['activation_by_signup_month'] = activation_file
    print(f"    ✓ Activation by signup month: {activation_file}")

    return output_files


def generate_gateway_migration_csv(booking_df: pd.DataFrame, accounts_df: pd.DataFrame,
                                    output_file: str) -> dict:
    """
    Generate gateway migration analysis (Default to Stripe Connect).

    Shows:
    - Which gateway new accounts choose by cohort year
    - Migration patterns over time

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Dictionary of generated file paths
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("  Generating gateway migration analysis...")

    # Check for gateway column
    gateway_col = None
    for col in ['GatewayName', 'Gateway Group', 'GatewayGroup']:
        if col in booking_df.columns:
            gateway_col = col
            break

    if gateway_col is None:
        print("    ⚠ No gateway column found - skipping")
        return output_files

    booking_df = booking_df.copy()
    accounts_df = accounts_df.copy()

    # Standardise gateway names
    def standardise_gateway(gateway):
        if pd.isna(gateway):
            return 'Unknown'
        gateway_upper = str(gateway).upper()
        if 'STRIPE' in gateway_upper and 'CONNECT' in gateway_upper:
            return 'Stripe Connect'
        elif 'STRIPE' in gateway_upper:
            return 'Stripe'
        elif 'DEFAULT' in gateway_upper or gateway_upper == 'TRYBOOKING':
            return 'Default'
        return 'Other'

    booking_df['Gateway'] = booking_df[gateway_col].apply(standardise_gateway)

    # Add cohort year to accounts
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Account Id'
    accounts_df['CohortYear'] = pd.to_datetime(accounts_df['DateTimeCreated']).dt.year
    cohort_map = accounts_df.set_index(account_id_col)['CohortYear'].to_dict()

    booking_df['CohortYear'] = booking_df['AccountId'].map(cohort_map)

    # First gateway choice by cohort year
    first_gateway = booking_df.sort_values('TransactionDate').groupby('AccountId').first()[['Gateway', 'CohortYear']]

    cohort_gateway = first_gateway.groupby(['CohortYear', 'Gateway']).size().unstack(fill_value=0)
    cohort_gateway['Total'] = cohort_gateway.sum(axis=1)

    for col in cohort_gateway.columns:
        if col != 'Total':
            cohort_gateway[f'{col} %'] = round(cohort_gateway[col] / cohort_gateway['Total'] * 100, 1)

    cohort_gateway = cohort_gateway.sort_index()

    cohort_file = get_output_path(base_name, 'cohorts', '_gateway_choice_by_cohort.csv')
    cohort_gateway.to_csv(cohort_file, float_format='%.1f')
    output_files['gateway_choice_by_cohort'] = cohort_file
    print(f"    ✓ Gateway choice by cohort: {cohort_file}")

    # Migration analysis - accounts that switched
    account_gateways = booking_df.groupby('AccountId').agg({
        'Gateway': lambda x: list(x.unique()),
        'TransactionDate': ['min', 'max']
    })
    account_gateways.columns = ['Gateways', 'First Transaction', 'Last Transaction']

    # Find accounts that used both Default and Stripe Connect
    def detect_migration(gateways):
        if 'Default' in gateways and 'Stripe Connect' in gateways:
            return 'Default → Stripe Connect'
        elif 'Default' in gateways and 'Stripe' in gateways:
            return 'Default → Stripe'
        elif len(gateways) == 1:
            return f'{gateways[0]} Only'
        return 'Other'

    account_gateways['Migration'] = account_gateways['Gateways'].apply(detect_migration)

    migration_summary = account_gateways['Migration'].value_counts()
    migration_df = pd.DataFrame({
        'Migration Pattern': migration_summary.index,
        'Accounts': migration_summary.values,
        'Percentage': np.round(migration_summary.values / len(account_gateways) * 100, 1)
    })

    migration_file = get_output_path(base_name, 'cohorts', '_gateway_migration_patterns.csv')
    migration_df.to_csv(migration_file, index=False)
    output_files['gateway_migration_patterns'] = migration_file
    print(f"    ✓ Gateway migration patterns: {migration_file}")

    return output_files


def main():
    """Main execution function."""
    args = parse_args()

    print(f"\n=== End of Year Planning Report ===")
    print(f"Generated: {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Get months to process
    months = get_month_range(args)
    print(f"\nProcessing {len(months)} months: {months[0][0]}/{months[0][1]:02d} to {months[-1][0]}/{months[-1][1]:02d}")

    # Load all data
    accounts_df, booking_df = load_all_data()

    # Calculate tiers for ALL accounts - need both current and historical for fair YoY comparison
    # Current tiers: based on last 365 days from today
    # Historical tiers: based on last 365 days from 1 year ago (for PY comparison)
    print("\nCalculating account tiers across entire client base...")
    from datetime import date
    today = date.today()
    one_year_ago = today - timedelta(days=365)

    account_tiers_current = calculate_account_tiers(accounts_df, booking_df)
    print("  Calculating historical tiers for YoY comparison...")
    account_tiers_previous = calculate_account_tiers(accounts_df, booking_df, as_of_date=one_year_ago)

    # Calculate metrics for each month
    print("\nCalculating monthly metrics...")
    results = []
    previous_year_cache = {}  # Cache previous year metrics to avoid recalculation

    for year, month in months:
        print(f"  Processing {calendar.month_name[month]} {year}...", end=" ")
        metrics = calculate_monthly_metrics(accounts_df, booking_df, year, month)

        # Add Tier 4+ new accounts count (uses current tiers for current year)
        tier4_plus_count = count_tier4_plus_new_accounts(accounts_df, account_tiers_current, year, month)
        metrics['New Accounts Tier 4+'] = tier4_plus_count

        # Calculate YoY comparison
        prev_year = year - 1
        prev_key = (prev_year, month)

        # Get or calculate previous year metrics
        if prev_key not in previous_year_cache:
            print(f"(calculating {prev_year} baseline)...", end=" ")
            prev_metrics = calculate_monthly_metrics(accounts_df, booking_df, prev_year, month)
            # Add Tier 4+ for previous year using HISTORICAL tiers (fair comparison)
            prev_tier4_plus = count_tier4_plus_new_accounts(accounts_df, account_tiers_previous, prev_year, month)
            prev_metrics['New Accounts Tier 4+'] = prev_tier4_plus
            previous_year_cache[prev_key] = prev_metrics
        else:
            prev_metrics = previous_year_cache[prev_key]

        # Add YoY metrics
        metrics = calculate_yoy_metrics(metrics, prev_metrics)

        results.append(metrics)
        print(f"✓ ({metrics['Total New Accounts']:,} new accounts, {tier4_plus_count} T4+)")

    # Create results DataFrame
    results_df = pd.DataFrame(results)

    # Calculate unique period totals (for current year and previous year)
    print("\nCalculating unique period totals...")
    period_totals = calculate_period_totals(accounts_df, booking_df, months)

    # Calculate previous year period totals
    py_months = [(year - 1, month) for year, month in months]
    py_period_totals = calculate_period_totals(accounts_df, booking_df, py_months)

    print(f"  Current period: {period_totals['Events With Sales (Unique)']:,} unique events, "
          f"{period_totals['Accounts Selling (Unique)']:,} unique accounts selling")
    print(f"  Previous year:  {py_period_totals['Events With Sales (Unique)']:,} unique events, "
          f"{py_period_totals['Accounts Selling (Unique)']:,} unique accounts selling")

    # Calculate churn rate
    print("\nCalculating churn/retention metrics...")
    churn_metrics = calculate_churn_rate(accounts_df, booking_df, months, py_months)
    if churn_metrics:
        print(f"  Previous period active: {churn_metrics['Previous Period Active Accounts']:,}")
        print(f"  Current period active:  {churn_metrics['Current Period Active Accounts']:,}")
        print(f"  Retained: {churn_metrics['Retained Accounts']:,} ({churn_metrics['Retention Rate (%)']}%)")
        print(f"  Churned:  {churn_metrics['Churned Accounts']:,} ({churn_metrics['Churn Rate (%)']}%)")
        print(f"  New active: {churn_metrics['New Active Accounts']:,}")

    # Print summary
    print_summary(results_df, period_totals, py_period_totals)

    # Save to CSV - ensure no scientific notation for large numbers
    output_file = args.output
    results_df.to_csv(output_file, index=False, float_format='%.2f')
    print(f"\n✓ Monthly results saved to: {output_file}")

    # Generate summary CSV with totals and averages
    summary_file = generate_summary_csv(results_df, output_file, period_totals, py_period_totals)
    print(f"✓ Summary report saved to: {summary_file}")

    # Generate industry breakdown CSV
    print("\nGenerating breakdown reports...")
    industry_file = generate_industry_breakdown_csv(accounts_df, booking_df, months, output_file)
    if industry_file:
        print(f"✓ Industry breakdown saved to: {industry_file}")

    # Generate industry/sub-industry breakdown CSV with YoY
    industry_sub_file = generate_industry_subindustry_breakdown_csv(accounts_df, booking_df, months, output_file)
    if industry_sub_file:
        print(f"✓ Industry/Sub-Industry breakdown saved to: {industry_sub_file}")

    # Generate geographic breakdown CSV
    geo_file = generate_geographic_breakdown_csv(accounts_df, booking_df, months, output_file)
    if geo_file:
        print(f"✓ Geographic breakdown saved to: {geo_file}")

    # Generate industry x geography cross-tab CSV
    cross_file = generate_industry_geography_crosstab_csv(accounts_df, booking_df, months, output_file)
    if cross_file:
        print(f"✓ Industry x Geography cross-tab saved to: {cross_file}")

    # Generate keyword analysis reports
    keyword_files = generate_keyword_analysis_csvs(booking_df, output_file, output_folder='keywords')
    if keyword_files:
        print(f"✓ Keyword analysis reports generated:")
        for report_type, filepath in keyword_files.items():
            print(f"    - {report_type}: {filepath}")

    # Generate advanced analytics reports
    print("\nGenerating advanced analytics...")

    # Seasonality analysis (identify dips by industry and event type)
    print("  Analysing seasonality patterns...")
    generate_seasonality_analysis_csv(booking_df, accounts_df, output_file)

    # Gateway split analysis (Stripe Connect vs TryBooking Gateway)
    print("  Analysing gateway split...")
    gateway_files = generate_gateway_split_analysis_csv(booking_df, accounts_df, output_file)
    if gateway_files:
        print(f"  ✓ Gateway analysis: {len(gateway_files)} reports generated")

    # Expansion revenue analysis (new vs existing account growth)
    print("  Analysing expansion revenue...")
    generate_expansion_revenue_analysis_csv(booking_df, accounts_df, output_file)

    # Cohort revenue curves (YoY comparable)
    print("  Generating cohort revenue curves...")
    generate_cohort_revenue_curves_csv(booking_df, accounts_df, output_file)

    # Planning model for 2026 target setting
    print("  Generating planning model...")
    generate_planning_model_csv(results_df, accounts_df, booking_df, output_file)

    # === DDDM ANALYTICS ===
    print("\nGenerating DDDM analytics...")

    # Account LTV analysis (24-month)
    ltv_files = generate_account_ltv_analysis_csv(booking_df, accounts_df, output_file)
    if ltv_files:
        print(f"  ✓ Account LTV analysis: {len(ltv_files)} reports generated")

    # Dormancy/churn analysis
    dormancy_files = generate_dormancy_analysis_csv(booking_df, accounts_df, output_file)
    if dormancy_files:
        print(f"  ✓ Dormancy analysis: {len(dormancy_files)} reports generated")

    # Event metrics analysis
    event_files = generate_event_metrics_analysis_csv(booking_df, accounts_df, output_file)
    if event_files:
        print(f"  ✓ Event metrics analysis: {len(event_files)} reports generated")

    # PPC cohort analysis (requires GA4 credentials)
    ppc_files = generate_ppc_cohort_analysis_csv(booking_df, accounts_df, output_file)
    if ppc_files:
        print(f"  ✓ PPC cohort analysis: {len(ppc_files)} reports generated")

    # Box Office sales analysis (Card Present/Cash vs Online)
    boxoffice_files = generate_boxoffice_analysis_csv(booking_df, accounts_df, output_file)
    if boxoffice_files:
        print(f"  ✓ Box Office analysis: {len(boxoffice_files)} reports generated")

    # === ADDITIONAL ANALYTICS ===
    print("\nGenerating additional analytics...")

    # Gateway by geography analysis
    gateway_geo_files = generate_gateway_by_geography_csv(booking_df, accounts_df, output_file)
    if gateway_geo_files:
        print(f"  ✓ Gateway by geography: {len(gateway_geo_files)} reports generated")

    # Organiser concentration by tier
    concentration_files = generate_organiser_concentration_csv(booking_df, accounts_df, output_file)
    if concentration_files:
        print(f"  ✓ Organiser concentration: {len(concentration_files)} reports generated")

    # Cohort quality by signup month
    cohort_quality_files = generate_cohort_quality_by_month_csv(booking_df, accounts_df, output_file)
    if cohort_quality_files:
        print(f"  ✓ Cohort quality by month: {len(cohort_quality_files)} reports generated")

    # Outreach calendar (campaign calendar synthesis)
    outreach_files = generate_outreach_calendar_csv(booking_df, output_file)
    if outreach_files:
        print(f"  ✓ Outreach calendar: {len(outreach_files)} reports generated")

    # Price band analysis
    price_band_files = generate_price_band_analysis_csv(booking_df, output_file)
    if price_band_files:
        print(f"  ✓ Price band analysis: {len(price_band_files)} reports generated")

    # Fee structure analysis (free vs paid by industry)
    fee_structure_files = generate_fee_structure_analysis_csv(booking_df, output_file)
    if fee_structure_files:
        print(f"  ✓ Fee structure analysis: {len(fee_structure_files)} reports generated")

    # Activation by signup month
    activation_files = generate_activation_by_month_csv(accounts_df, booking_df, output_file)
    if activation_files:
        print(f"  ✓ Activation by month: {len(activation_files)} reports generated")

    # Gateway migration trends
    migration_files = generate_gateway_migration_csv(booking_df, accounts_df, output_file)
    if migration_files:
        print(f"  ✓ Gateway migration: {len(migration_files)} reports generated")

    print(f"\n=== Report Complete ===")


if __name__ == "__main__":
    main()
