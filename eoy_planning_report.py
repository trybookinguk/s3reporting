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
from modules.utils.config import UK_TZ, MIN_TICKETS_FOR_ACTIVE
from modules.utils.data_loader import (
    get_loader, load_accounts, load_booking_data,
    filter_successful_transactions
)
from modules.utils.date_utils import get_latest_data_date
from modules.utils.performance import timer_decorator


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

    # Calculate average days to first event
    avg_days_to_first_event = None
    if activated_with_events > 0 and 'FirstEventCreation' in accounts_with_events.columns:
        # Ensure both columns are timezone-aware for comparison
        created = pd.to_datetime(accounts_with_events['DateTimeCreated'], utc=True)
        first_event = pd.to_datetime(accounts_with_events['FirstEventCreation'], utc=True)
        days_to_event = (first_event - created).dt.total_seconds() / 86400
        # Filter out negative values (data issues) and extreme outliers
        valid_days = days_to_event[(days_to_event >= 0) & (days_to_event <= 365)]
        if len(valid_days) > 0:
            avg_days_to_first_event = valid_days.mean()

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

    # 4. Activated accounts - sold 10+ tickets (ever)
    accounts_with_10plus_tickets = account_tickets[
        account_tickets['TotalTickets'] >= MIN_TICKETS_FOR_ACTIVE
    ]
    activated_sold_10_tickets = len(accounts_with_10plus_tickets)

    # 5. New accounts that sold any tickets (tier qualified = sold 10+ tickets)
    accounts_with_any_sales = account_tickets[account_tickets['TotalTickets'] > 0]
    new_accounts_sold_tickets = len(accounts_with_any_sales)

    # Tier qualified = those with 10+ tickets
    new_accounts_tier_qualified = activated_sold_10_tickets

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
    pct_sold_tickets = (new_accounts_sold_tickets / total_new_accounts * 100) if total_new_accounts > 0 else 0
    pct_tier_qualified = (new_accounts_tier_qualified / total_new_accounts * 100) if total_new_accounts > 0 else 0

    return {
        'Year': year,
        'Month': month,
        'Month Name': month_name,
        'Total New Accounts': total_new_accounts,
        'Activated (Created Events)': activated_with_events,
        'Activated (Sold 10+ Tickets)': activated_sold_10_tickets,
        'New Accounts With Events': activated_with_events,
        'New Accounts Sold Tickets': new_accounts_sold_tickets,
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
        '% Sold Tickets': round(pct_sold_tickets, 1),
        '% Tier Qualified': round(pct_tier_qualified, 1),
        'Avg Days to First Event': round(avg_days_to_first_event, 1) if avg_days_to_first_event is not None else None,
        'Avg Days to First Sale': round(avg_days_to_first_sale, 1) if avg_days_to_first_sale is not None else None,
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
    current_metrics['PY Events With Sales'] = previous_year_metrics['Events With Sales']
    current_metrics['PY Ticket Revenue'] = previous_year_metrics['Total Ticket Revenue']
    current_metrics['PY Fees'] = previous_year_metrics['Total Fees']

    return current_metrics


def print_summary(results_df):
    """Print a formatted summary of results."""
    print("\n" + "=" * 100)
    print("END OF YEAR PLANNING REPORT")
    print("=" * 100)

    # Print column headers
    print(f"\n{'Month':<12} {'New':>8} {'Events':>8} {'10+ Tix':>8} {'Sold':>8} "
          f"{'Tier Q':>8} {'Events':>8} {'Revenue':>12} {'Fees':>12}")
    print(f"{'':12} {'Accts':>8} {'Created':>8} {'Actv':>8} {'Tickets':>8} "
          f"{'':>8} {'w/Sales':>8} {'(Tickets)':>12} {'':>12}")
    print("-" * 100)

    # Print each month
    for _, row in results_df.iterrows():
        month_label = f"{row['Month Name'][:3]} {row['Year']}"
        print(f"{month_label:<12} {row['Total New Accounts']:>8,} "
              f"{row['Activated (Created Events)']:>8,} "
              f"{row['Activated (Sold 10+ Tickets)']:>8,} "
              f"{row['New Accounts Sold Tickets']:>8,} "
              f"{row['New Accounts Tier Qualified']:>8,} "
              f"{row['Events With Sales']:>8,} "
              f"£{row['Total Ticket Revenue']:>10,.2f} "
              f"£{row['Total Fees']:>10,.2f}")

    # Print totals
    print("-" * 100)
    print(f"{'TOTAL':<12} {results_df['Total New Accounts'].sum():>8,} "
          f"{results_df['Activated (Created Events)'].sum():>8,} "
          f"{results_df['Activated (Sold 10+ Tickets)'].sum():>8,} "
          f"{results_df['New Accounts Sold Tickets'].sum():>8,} "
          f"{results_df['New Accounts Tier Qualified'].sum():>8,} "
          f"{results_df['Events With Sales'].sum():>8,} "
          f"£{results_df['Total Ticket Revenue'].sum():>10,.2f} "
          f"£{results_df['Total Fees'].sum():>10,.2f}")

    # Print averages
    print(f"{'AVERAGE':<12} {results_df['Total New Accounts'].mean():>8,.0f} "
          f"{results_df['Activated (Created Events)'].mean():>8,.0f} "
          f"{results_df['Activated (Sold 10+ Tickets)'].mean():>8,.0f} "
          f"{results_df['New Accounts Sold Tickets'].mean():>8,.0f} "
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
    print(f"  New Accounts → Sold Any Tickets:   {results_df['% Sold Tickets'].mean():.1f}%")
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

    # Print averages
    print("-" * 85)
    print(f"{'AVERAGE':<12} "
          f"{results_df['Accounts Selling In Month'].mean():>8,.0f} "
          f"£{results_df['Avg Price Per Ticket'].mean():>8,.2f} "
          f"£{results_df['Avg Transaction Value'].mean():>10,.2f} "
          f"{results_df['Avg Tickets Per Booking'].mean():>10,.2f} "
          f"£{results_df['Avg Account Ticket Sales'].mean():>12,.2f} "
          f"£{results_df['Avg Event Ticket Sales'].mean():>12,.2f}")

    # Additional insights
    print("\n" + "=" * 100)
    print("PERIOD TOTALS & INSIGHTS")
    print("=" * 100)
    total_tickets = results_df['Total Tickets Sold'].sum()
    total_transactions = results_df['Total Transactions'].sum()
    total_revenue = results_df['Total Ticket Revenue'].sum()
    total_fees = results_df['Total Fees'].sum()
    total_accounts_selling = results_df['Accounts Selling In Month'].sum()
    total_events = results_df['Events With Sales'].sum()

    print(f"  Total Tickets Sold:                {total_tickets:,}")
    print(f"  Total Transactions:                {total_transactions:,}")
    print(f"  Total Accounts Selling:            {total_accounts_selling:,} (sum of monthly, not unique)")
    print(f"  Total Events With Sales:           {total_events:,} (sum of monthly, not unique)")
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

    # Calculate metrics for each month
    print("\nCalculating monthly metrics...")
    results = []
    previous_year_cache = {}  # Cache previous year metrics to avoid recalculation

    for year, month in months:
        print(f"  Processing {calendar.month_name[month]} {year}...", end=" ")
        metrics = calculate_monthly_metrics(accounts_df, booking_df, year, month)

        # Calculate YoY comparison
        prev_year = year - 1
        prev_key = (prev_year, month)

        # Get or calculate previous year metrics
        if prev_key not in previous_year_cache:
            print(f"(calculating {prev_year} baseline)...", end=" ")
            prev_metrics = calculate_monthly_metrics(accounts_df, booking_df, prev_year, month)
            previous_year_cache[prev_key] = prev_metrics
        else:
            prev_metrics = previous_year_cache[prev_key]

        # Add YoY metrics
        metrics = calculate_yoy_metrics(metrics, prev_metrics)

        results.append(metrics)
        print(f"✓ ({metrics['Total New Accounts']:,} new accounts)")

    # Create results DataFrame
    results_df = pd.DataFrame(results)

    # Print summary
    print_summary(results_df)

    # Save to CSV - ensure no scientific notation for large numbers
    output_file = args.output
    results_df.to_csv(output_file, index=False, float_format='%.2f')
    print(f"\n✓ Results saved to: {output_file}")

    print(f"\n=== Report Complete ===")


if __name__ == "__main__":
    main()
