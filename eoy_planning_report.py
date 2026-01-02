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

    # Rolling 12 months (Dec 2024 - Nov 2025)
    python3 eoy_planning_report.py --rolling

    # Custom date range
    python3 eoy_planning_report.py --start 2024-01 --end 2025-11
"""
import os
import sys
import argparse
import pandas as pd
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
        help='Rolling 12 months (Dec previous year to Nov current year)'
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
        # Rolling 12 months: Dec previous year to Nov current year
        # If we're in Dec, use Dec current year - 1 to Nov current year
        if today.month == 12:
            start_year = today.year - 1
            start_month = 12
        else:
            start_year = today.year - 1
            start_month = 12

        current = datetime(start_year, start_month, 1)
        end = datetime(today.year, today.month - 1 if today.month > 1 else 12, 1)
        if today.month == 1:
            end = datetime(today.year - 1, 12, 1)

        while current <= end:
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

    # Aggregate by industry
    industry_metrics = []

    for industry in period_bookings[industry_col].dropna().unique():
        ind_bookings = period_bookings[period_bookings[industry_col] == industry]
        ind_new_accounts = new_accounts[new_accounts['Industry'] == industry] if 'Industry' in new_accounts.columns else pd.DataFrame()

        metrics = {
            'Industry': industry,
            'New Accounts': len(ind_new_accounts),
            'Active Accounts': ind_bookings['AccountId'].nunique(),
            'Events With Sales': ind_bookings['EventId'].nunique() if 'EventId' in ind_bookings.columns else 0,
            'Total Tickets': int(ind_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in ind_bookings.columns else 0,
            'Total Transactions': len(ind_bookings),
            'Total Ticket Revenue': round(ind_bookings['PaymentReceived'].sum(), 2) if 'PaymentReceived' in ind_bookings.columns else 0,
            'Total Fees': round(ind_bookings['TotalFees'].sum(), 2) if 'TotalFees' in ind_bookings.columns else 0,
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

        industry_metrics.append(metrics)

    # Sort by total revenue descending
    industry_df = pd.DataFrame(industry_metrics)
    industry_df = industry_df.sort_values('Total Ticket Revenue', ascending=False)

    # Generate filename
    base_name = output_file.rsplit('.', 1)[0]
    industry_file = f"{base_name}_by_industry.csv"

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

    # Generate filename
    base_name = output_file.rsplit('.', 1)[0]
    industry_file = f"{base_name}_by_industry_subindustry.csv"

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

    # Generate filename
    base_name = output_file.rsplit('.', 1)[0]
    cross_file = f"{base_name}_industry_x_geography.csv"

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

        metrics = {
            'Region': region,
            'New Accounts': len(area_new_accounts),
            'Active Accounts': area_bookings['AccountId'].nunique(),
            'Events With Sales': area_bookings['EventId'].nunique() if 'EventId' in area_bookings.columns else 0,
            'Total Tickets': int(area_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in area_bookings.columns else 0,
            'Total Transactions': len(area_bookings),
            'Total Ticket Revenue': round(area_bookings['PaymentReceived'].sum(), 2) if 'PaymentReceived' in area_bookings.columns else 0,
            'Total Fees': round(area_bookings['TotalFees'].sum(), 2) if 'TotalFees' in area_bookings.columns else 0,
        }

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

        geo_rows.append(row)

    # Sort by total revenue descending
    geo_df = pd.DataFrame(geo_rows)
    geo_df = geo_df.sort_values('Total Ticket Revenue', ascending=False)

    # Generate filename
    base_name = output_file.rsplit('.', 1)[0]
    geo_file = f"{base_name}_by_geography.csv"

    geo_df.to_csv(geo_file, index=False, float_format='%.2f')
    return geo_file


def generate_seasonality_analysis_csv(booking_df, accounts_df, output_file):
    """
    Generate seasonality analysis showing monthly patterns by industry and event type.
    Identifies dips and peaks to help plan contra-seasonal strategies.

    Args:
        booking_df: Booking transactions DataFrame
        accounts_df: Accounts DataFrame
        output_file: Base output filename

    Returns:
        Tuple of (industry_seasonality_file, keyword_seasonality_file)
    """
    base_name = output_file.rsplit('.', 1)[0]

    # Get industry from accounts if not in bookings
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'
    if 'Industry' not in booking_df.columns and 'Industry' in accounts_df.columns:
        booking_df = booking_df.merge(
            accounts_df[[account_id_col, 'Industry']].rename(columns={account_id_col: 'AccountId'}),
            on='AccountId',
            how='left'
        )

    # Ensure we have TransactionDate as datetime
    if 'TransactionDate' not in booking_df.columns:
        return None, None

    booking_df = booking_df.copy()
    booking_df['Month'] = pd.to_datetime(booking_df['TransactionDate']).dt.month
    booking_df['Year'] = pd.to_datetime(booking_df['TransactionDate']).dt.year
    booking_df['YearMonth'] = booking_df['Year'].astype(str) + '-' + booking_df['Month'].astype(str).str.zfill(2)

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
        month_order = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        month_order = [m for m in month_order if m in industry_pivot.columns]
        industry_pivot = industry_pivot[month_order]

        # Add annual revenue for context
        industry_pivot = industry_pivot.merge(industry_annual.set_index('Industry'), left_index=True, right_index=True)

        # Calculate volatility (std dev of monthly %)
        monthly_cols = [c for c in industry_pivot.columns if c in month_order]
        industry_pivot['Volatility'] = industry_pivot[monthly_cols].std(axis=1).round(1)

        # Sort by annual revenue
        industry_pivot = industry_pivot.sort_values('Annual Revenue', ascending=False)

        industry_file = f"{base_name}_seasonality_by_industry.csv"
        industry_pivot.to_csv(industry_file, float_format='%.1f')
        print(f"  ✓ Industry seasonality saved to: {industry_file}")

        # Also save the detailed view with dip/peak flags
        detail_file = f"{base_name}_seasonality_industry_detail.csv"
        industry_monthly_out = industry_monthly[['Industry', 'Month Name', 'Revenue', 'Tickets', 'Events', '% of Annual', 'Variance %', 'Status']]
        industry_monthly_out = industry_monthly_out.sort_values(['Industry', 'Month'])
        industry_monthly_out.to_csv(detail_file, index=False, float_format='%.1f')
        print(f"  ✓ Industry seasonality detail saved to: {detail_file}")
    else:
        industry_file = None

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
        keyword_pivot = keyword_pivot[[m for m in month_order if m in keyword_pivot.columns]]
        keyword_pivot = keyword_pivot.merge(keyword_totals.set_index('Keyword'), left_index=True, right_index=True)
        keyword_pivot['Volatility'] = keyword_pivot[[c for c in keyword_pivot.columns if c in month_order]].std(axis=1).round(1)
        keyword_pivot = keyword_pivot.sort_values('Annual Revenue', ascending=False)

        keyword_file = f"{base_name}_seasonality_by_event_type.csv"
        keyword_pivot.to_csv(keyword_file, float_format='%.1f')
        print(f"  ✓ Event type seasonality saved to: {keyword_file}")
    else:
        keyword_file = None

    return industry_file, keyword_file


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

    # Aggregate by month and revenue type
    monthly_breakdown = booking_df.groupby(['YearMonth', 'Revenue Type']).agg({
        'PaymentReceived': 'sum',
        'AccountId': 'nunique'
    }).reset_index()
    monthly_breakdown.columns = ['YearMonth', 'Revenue Type', 'Revenue', 'Accounts']

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
    revenue_file = f"{base_name}_expansion_revenue.csv"
    revenue_pivot.to_csv(revenue_file, index=False, float_format='%.2f')
    print(f"  ✓ Expansion revenue analysis saved to: {revenue_file}")

    # Save accounts breakdown
    accounts_file = f"{base_name}_expansion_accounts.csv"
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

    summary_file = f"{base_name}_expansion_yearly_summary.csv"
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

    # Aggregate by cohort and month-of-life
    cohort_curves = booking_df.groupby(['CohortMonth', 'MonthOfLife']).agg({
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
        'AccountId': 'nunique'
    }).reset_index()
    cohort_curves.columns = ['Cohort', 'Month of Life', 'Revenue', 'Tickets', 'Active Accounts']

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
    curves_file = f"{base_name}_cohort_curves.csv"
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

    milestone_file = f"{base_name}_cohort_milestones.csv"
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

    yoy_file = f"{base_name}_cohort_yoy_comparison.csv"
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


def calculate_trend_based_growth(yearly_data: pd.DataFrame) -> dict:
    """
    Calculate recommended growth targets based on historical trend momentum.

    Uses weighted average of recent growth rates with more weight on recent years.

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

                # Also calculate trend direction (acceleration/deceleration)
                if len(growth_rates) >= 2:
                    trend = growth_rates[-1] - growth_rates[-2]  # Most recent change
                else:
                    trend = 0

                # Recommended = weighted average + small trend adjustment
                recommended = weighted_avg + (trend * 0.25)

                recommendations[metric] = {
                    'weighted_avg': round(weighted_avg, 1),
                    'trend': round(trend, 1),
                    'recommended': round(recommended, 1),
                    'conservative': round(recommended * 0.7, 1),  # 70% of recommended
                    'stretch': round(recommended * 1.5, 1),  # 150% of recommended
                }
            elif len(growth_rates) == 1:
                recommendations[metric] = {
                    'weighted_avg': round(growth_rates[0], 1),
                    'trend': 0,
                    'recommended': round(growth_rates[0], 1),
                    'conservative': round(growth_rates[0] * 0.7, 1),
                    'stretch': round(growth_rates[0] * 1.5, 1),
                }

    return recommendations


def generate_planning_model_csv(results_df, accounts_df, booking_df, output_file, target_year=2026):
    """
    Generate comprehensive planning model for target setting.

    Features:
    - Year-aware seasonality (adjusts for Easter timing)
    - School holiday period flags
    - Scenario modelling (Conservative / Base / Stretch)
    - Trend-based growth recommendations
    - BHAG cumulative tracking
    - Side-by-side comparison (previous year actuals vs targets)

    Args:
        results_df: DataFrame with monthly metrics
        accounts_df: Accounts DataFrame
        booking_df: Booking transactions DataFrame
        output_file: Base output filename
        target_year: Year to generate targets for (default 2026)

    Returns:
        Path to generated CSV file
    """
    base_name = output_file.rsplit('.', 1)[0]

    if len(results_df) == 0:
        print("  Warning: No results data for planning model")
        return None

    planning_df = results_df.copy()

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

    # === CALCULATE EASTER-ADJUSTED SEASONALITY ===
    # Group by Easter position to get adjusted indices
    easter_adjusted = planning_df.groupby(['Month', 'Easter Position']).agg({
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
    base_year = target_year - 1
    base_year_data = planning_df[planning_df['Year'] == base_year].copy()

    if len(base_year_data) == 0:
        # Try to use most recent complete year
        available_years = planning_df['Year'].unique()
        base_year = max(available_years)
        base_year_data = planning_df[planning_df['Year'] == base_year].copy()
        print(f"  Note: Using {base_year} as base year (most recent available)")

    # === BUILD SCENARIO TARGETS ===
    # Get average seasonality indices (use Easter-adjusted where available)
    avg_indices = planning_df.groupby('Month').agg({
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

    # Calculate scenario targets
    for metric, base_val, idx_col in [
        ('Accounts', base_accounts, 'Accounts Index %'),
        ('Revenue', base_revenue, 'Revenue Index %'),
        ('Fees', base_fees, 'Fees Index %')
    ]:
        metric_key = f'Total New {metric}' if metric == 'Accounts' else f'Total Ticket {metric}' if metric == 'Revenue' else f'Total {metric}'

        if metric_key in recommendations:
            rec = recommendations[metric_key]
            conservative_growth = rec['conservative'] / 100
            base_growth = rec['recommended'] / 100
            stretch_growth = rec['stretch'] / 100
        else:
            # Default growth rates if no historical data
            conservative_growth = 0.10
            base_growth = 0.15
            stretch_growth = 0.25

        # Annual targets
        conservative_annual = base_val * (1 + conservative_growth)
        base_annual = base_val * (1 + base_growth)
        stretch_annual = base_val * (1 + stretch_growth)

        # Monthly targets using seasonality
        scenarios[f'{target_year} {metric} Conservative'] = round(conservative_annual * scenarios[idx_col] / 100, 0 if metric == 'Accounts' else 2)
        scenarios[f'{target_year} {metric} Base'] = round(base_annual * scenarios[idx_col] / 100, 0 if metric == 'Accounts' else 2)
        scenarios[f'{target_year} {metric} Stretch'] = round(stretch_annual * scenarios[idx_col] / 100, 0 if metric == 'Accounts' else 2)

        # YoY variance vs base year
        scenarios[f'{metric} Base vs {base_year} %'] = round(
            (scenarios[f'{target_year} {metric} Base'] - scenarios[f'{base_year} {metric}']) / scenarios[f'{base_year} {metric}'].replace(0, 1) * 100, 1
        )

    # === ADD CUMULATIVE TOTALS FOR BHAG TRACKING ===
    # Get total accounts ever created up to base year end
    total_accounts_to_date = len(accounts_df)  # Current total

    # Add cumulative columns
    scenarios[f'Cumulative Accounts (Base)'] = scenarios[f'{target_year} Accounts Base'].cumsum() + total_accounts_to_date
    scenarios[f'Cumulative Accounts (Stretch)'] = scenarios[f'{target_year} Accounts Stretch'].cumsum() + total_accounts_to_date

    # BHAG milestone tracking (25,000 accounts)
    bhag_target = 25000
    scenarios['BHAG Progress %'] = round(scenarios['Cumulative Accounts (Stretch)'] / bhag_target * 100, 1)
    scenarios['BHAG Gap'] = bhag_target - scenarios['Cumulative Accounts (Stretch)']

    # === SAVE OUTPUTS ===

    # 1. Main scenario model
    scenario_cols = [
        'Month', 'Month Name', 'Holiday Type',
        f'{base_year} Accounts', f'{target_year} Accounts Conservative', f'{target_year} Accounts Base', f'{target_year} Accounts Stretch', f'Accounts Base vs {base_year} %',
        f'{base_year} Revenue', f'{target_year} Revenue Conservative', f'{target_year} Revenue Base', f'{target_year} Revenue Stretch', f'Revenue Base vs {base_year} %',
        f'{base_year} Fees', f'{target_year} Fees Conservative', f'{target_year} Fees Base', f'{target_year} Fees Stretch', f'Fees Base vs {base_year} %',
        'Cumulative Accounts (Base)', 'Cumulative Accounts (Stretch)', 'BHAG Progress %', 'BHAG Gap',
    ]
    scenario_cols = [c for c in scenario_cols if c in scenarios.columns]

    scenario_file = f"{base_name}_{target_year}_targets.csv"
    scenarios[scenario_cols].to_csv(scenario_file, index=False, float_format='%.2f')
    print(f"  ✓ {target_year} targets saved to: {scenario_file}")

    # 2. Growth recommendations summary
    rec_rows = []
    for metric, rec in recommendations.items():
        rec_rows.append({
            'Metric': metric,
            'Historical Weighted Avg %': rec['weighted_avg'],
            'Recent Trend': rec['trend'],
            'Recommended Growth %': rec['recommended'],
            'Conservative %': rec['conservative'],
            'Stretch %': rec['stretch'],
        })

    if rec_rows:
        rec_df = pd.DataFrame(rec_rows)
        rec_file = f"{base_name}_growth_recommendations.csv"
        rec_df.to_csv(rec_file, index=False, float_format='%.1f')
        print(f"  ✓ Growth recommendations saved to: {rec_file}")

    # 3. Annual summary
    annual_summary = {
        'Metric': ['New Accounts', 'Ticket Revenue', 'Fees'],
        f'{base_year} Actual': [int(base_accounts), round(base_revenue, 2), round(base_fees, 2)],
        f'{target_year} Conservative': [
            int(scenarios[f'{target_year} Accounts Conservative'].sum()),
            round(scenarios[f'{target_year} Revenue Conservative'].sum(), 2),
            round(scenarios[f'{target_year} Fees Conservative'].sum(), 2),
        ],
        f'{target_year} Base': [
            int(scenarios[f'{target_year} Accounts Base'].sum()),
            round(scenarios[f'{target_year} Revenue Base'].sum(), 2),
            round(scenarios[f'{target_year} Fees Base'].sum(), 2),
        ],
        f'{target_year} Stretch': [
            int(scenarios[f'{target_year} Accounts Stretch'].sum()),
            round(scenarios[f'{target_year} Revenue Stretch'].sum(), 2),
            round(scenarios[f'{target_year} Fees Stretch'].sum(), 2),
        ],
    }

    # Add growth percentages
    for scenario in ['Conservative', 'Base', 'Stretch']:
        annual_summary[f'{scenario} Growth %'] = [
            round((annual_summary[f'{target_year} {scenario}'][i] - annual_summary[f'{base_year} Actual'][i]) / annual_summary[f'{base_year} Actual'][i] * 100, 1)
            for i in range(3)
        ]

    annual_df = pd.DataFrame(annual_summary)
    annual_file = f"{base_name}_{target_year}_annual_summary.csv"
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

    model_file = f"{base_name}_planning_model.csv"
    planning_output.to_csv(model_file, index=False, float_format='%.2f')
    print(f"  ✓ Historical planning model saved to: {model_file}")

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
    keyword_files = generate_keyword_analysis_csvs(booking_df, output_file)
    if keyword_files:
        print(f"✓ Keyword analysis reports generated:")
        for report_type, filepath in keyword_files.items():
            print(f"    - {report_type}: {filepath}")

    # Generate advanced analytics reports
    print("\nGenerating advanced analytics...")

    # Seasonality analysis (identify dips by industry and event type)
    print("  Analysing seasonality patterns...")
    generate_seasonality_analysis_csv(booking_df, accounts_df, output_file)

    # Expansion revenue analysis (new vs existing account growth)
    print("  Analysing expansion revenue...")
    generate_expansion_revenue_analysis_csv(booking_df, accounts_df, output_file)

    # Cohort revenue curves (YoY comparable)
    print("  Generating cohort revenue curves...")
    generate_cohort_revenue_curves_csv(booking_df, accounts_df, output_file)

    # Planning model for 2026 target setting
    print("  Generating planning model...")
    generate_planning_model_csv(results_df, accounts_df, booking_df, output_file)

    print(f"\n=== Report Complete ===")


if __name__ == "__main__":
    main()
