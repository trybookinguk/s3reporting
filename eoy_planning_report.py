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


def generate_geographic_breakdown_csv(accounts_df, booking_df, months, output_file):
    """
    Generate geographic breakdown CSV with metrics per region (based on postcodes).

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

    # Use EventPostcode or AccountPostcode
    postcode_col = None
    if 'EventPostcode' in period_bookings.columns:
        postcode_col = 'EventPostcode'
    elif 'AccountPostcode' in period_bookings.columns:
        postcode_col = 'AccountPostcode'

    if postcode_col is None:
        print("  Warning: No postcode data available")
        return None

    # Extract postcode area (first 1-2 letters)
    period_bookings['PostcodeArea'] = period_bookings[postcode_col].astype(str).str.extract(r'^([A-Za-z]{1,2})', expand=False)
    period_bookings['PostcodeArea'] = period_bookings['PostcodeArea'].str.upper()

    # Filter new accounts created in period
    new_accounts = accounts_df[
        (accounts_df['DateTimeCreated'] >= period_start) &
        (accounts_df['DateTimeCreated'] <= period_end)
    ].copy()

    # Extract postcode area from accounts if available
    account_postcode_col = 'Postcode' if 'Postcode' in new_accounts.columns else None
    if account_postcode_col:
        new_accounts['PostcodeArea'] = new_accounts[account_postcode_col].astype(str).str.extract(r'^([A-Za-z]{1,2})', expand=False)
        new_accounts['PostcodeArea'] = new_accounts['PostcodeArea'].str.upper()

    # Aggregate by postcode area
    geo_metrics = []

    for area in period_bookings['PostcodeArea'].dropna().unique():
        if not area or area == 'NAN' or len(area) == 0:
            continue

        area_bookings = period_bookings[period_bookings['PostcodeArea'] == area]
        area_new_accounts = new_accounts[new_accounts['PostcodeArea'] == area] if 'PostcodeArea' in new_accounts.columns else pd.DataFrame()

        metrics = {
            'Postcode Area': area,
            'New Accounts': len(area_new_accounts),
            'Active Accounts': area_bookings['AccountId'].nunique(),
            'Events With Sales': area_bookings['EventId'].nunique() if 'EventId' in area_bookings.columns else 0,
            'Total Tickets': int(area_bookings['TicketQuantity'].sum()) if 'TicketQuantity' in area_bookings.columns else 0,
            'Total Transactions': len(area_bookings),
            'Total Ticket Revenue': round(area_bookings['PaymentReceived'].sum(), 2) if 'PaymentReceived' in area_bookings.columns else 0,
            'Total Fees': round(area_bookings['TotalFees'].sum(), 2) if 'TotalFees' in area_bookings.columns else 0,
        }

        # Calculate averages
        if metrics['Active Accounts'] > 0:
            metrics['Avg Revenue Per Account'] = round(metrics['Total Ticket Revenue'] / metrics['Active Accounts'], 2)
        else:
            metrics['Avg Revenue Per Account'] = 0

        if metrics['Events With Sales'] > 0:
            metrics['Avg Revenue Per Event'] = round(metrics['Total Ticket Revenue'] / metrics['Events With Sales'], 2)
        else:
            metrics['Avg Revenue Per Event'] = 0

        geo_metrics.append(metrics)

    # Sort by total revenue descending
    geo_df = pd.DataFrame(geo_metrics)
    geo_df = geo_df.sort_values('Total Ticket Revenue', ascending=False)

    # Generate filename
    base_name = output_file.rsplit('.', 1)[0]
    geo_file = f"{base_name}_by_geography.csv"

    geo_df.to_csv(geo_file, index=False, float_format='%.2f')
    return geo_file


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

    # Generate geographic breakdown CSV
    geo_file = generate_geographic_breakdown_csv(accounts_df, booking_df, months, output_file)
    if geo_file:
        print(f"✓ Geographic breakdown saved to: {geo_file}")

    print(f"\n=== Report Complete ===")


if __name__ == "__main__":
    main()
