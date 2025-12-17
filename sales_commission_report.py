#!/usr/bin/env python3
"""
Sales Commission Report

Monthly commission calculation for sales team members based on claimed accounts
in Zoho CRM whose first paid event has completed.

Commission Rules:
- £15 flat fee per qualifying account (configurable per sales person)
- Default gateway: 10% of (ProcessingFee + CardFee)
- Stripe Connect: 10% of (TicketFee + BookingFee)
- Commission rates are configurable per sales person via sales_commission_config.json

Usage:
    python3 sales_commission_report.py

Environment Variables:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY - S3 access
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN - Zoho CRM access
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN - Email sending
    TEST_MODE - If 'true', sends emails only to alex@trybooking.co.uk
    REPORT_MONTH - Optional, format 'YYYY-MM' to specify report month (defaults to previous month)
"""
import os
import sys
import json
import pandas as pd
import requests
from datetime import datetime
from dateutil.relativedelta import relativedelta

from modules.utils.config import (
    TEST_MODE, UK_TZ, ZOHO_DOMAIN,
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN
)
from modules.utils.zoho_api import get_access_token, get_session
from modules.utils.data_loader import load_booking_data, load_accounts_data
from modules.utils.email_utils import send_html_email
from modules.utils.date_utils import get_latest_data_date

# Constants
COMMISSION_HISTORY_FILE = 'commission_paid_accounts.json'
COMMISSION_CONFIG_FILE = 'sales_commission_config.json'
MD_EMAIL = 'alex@trybooking.co.uk'


def validate_environment():
    """Validate required environment variables are set."""
    required_vars = [
        ('AWS_ACCESS_KEY_ID', AWS_ACCESS_KEY_ID),
        ('AWS_SECRET_ACCESS_KEY', AWS_SECRET_ACCESS_KEY),
        ('ZOHO_CLIENT_ID', ZOHO_CLIENT_ID),
        ('ZOHO_CLIENT_SECRET', ZOHO_CLIENT_SECRET),
        ('ZOHO_REFRESH_TOKEN', ZOHO_REFRESH_TOKEN),
        ('MAILGUN_SMTP_LOGIN', MAILGUN_SMTP_LOGIN),
        ('MAILGUN_SMTP_PASSWORD', MAILGUN_SMTP_PASSWORD),
        ('MAILGUN_DOMAIN', MAILGUN_DOMAIN),
    ]

    missing = [name for name, value in required_vars if not value]

    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def get_report_period():
    """
    Get the report period (start and end dates).

    Modes:
    - SCHEDULED_RUN=true: Previous month only (automatic runs on 2nd of month)
    - REPORT_MONTH=YYYY-MM: Specific month only
    - No env vars / empty: All time (no date filtering)

    Returns:
        Tuple of (report_period_start, report_period_end, period_description)
        - For "all time", returns (None, None, "All Time")
        - For specific periods, returns (start_timestamp, end_timestamp, "Month YYYY")
    """
    report_month_str = os.environ.get('REPORT_MONTH', '').strip()
    is_scheduled = os.environ.get('SCHEDULED_RUN', 'false').lower() == 'true'

    if is_scheduled:
        # Scheduled run: previous month only
        now = datetime.now(UK_TZ)
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        report_start = pd.Timestamp(first_of_this_month - relativedelta(months=1))
        report_end = pd.Timestamp(first_of_this_month)
        period_desc = report_start.strftime('%B %Y')
        return report_start, report_end, period_desc

    if report_month_str:
        # Specific month requested
        try:
            year, month = map(int, report_month_str.split('-'))
            report_start = pd.Timestamp(year=year, month=month, day=1, tz=UK_TZ)
            report_end = report_start + relativedelta(months=1)
            period_desc = report_start.strftime('%B %Y')
            return report_start, report_end, period_desc
        except ValueError:
            print(f"Error: Invalid REPORT_MONTH format '{report_month_str}'. Expected 'YYYY-MM'.")
            sys.exit(1)

    # Default: all time (no date filtering)
    return None, None, "All Time"


def load_commission_config():
    """Load commission rates configuration from file."""
    if os.path.exists(COMMISSION_CONFIG_FILE):
        with open(COMMISSION_CONFIG_FILE, 'r') as f:
            return json.load(f)
    else:
        # Default configuration
        return {
            "default_rates": {
                "flat_fee": 15.00,
                "commission_rate": 0.10
            },
            "sales_people": {}
        }


def get_rates_for_sales_person(config, sales_person_name):
    """
    Get commission rates for a specific sales person, with fallback to defaults.

    Args:
        config: Commission configuration dictionary
        sales_person_name: Name of the sales person

    Returns:
        Dict with 'flat_fee' and 'commission_rate'
    """
    default_rates = config.get('default_rates', {'flat_fee': 15.00, 'commission_rate': 0.10})
    person_rates = config.get('sales_people', {}).get(sales_person_name, {})

    return {
        'flat_fee': person_rates.get('flat_fee', default_rates['flat_fee']),
        'commission_rate': person_rates.get('commission_rate', default_rates['commission_rate'])
    }


def load_commission_history():
    """Load commission history from file."""
    if os.path.exists(COMMISSION_HISTORY_FILE):
        with open(COMMISSION_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {"paid_accounts": []}


def save_commission_history(history):
    """Save commission history to file."""
    with open(COMMISSION_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2, default=str)


def fetch_claimed_accounts(token):
    """
    Fetch all Zoho CRM accounts that have the 'Claimed' field populated.

    Args:
        token: Zoho OAuth access token

    Returns:
        DataFrame with columns: account_id, claimed_user_id, claimed_user_name
    """
    session = get_session()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    all_accounts = []
    page = 1

    print("Fetching claimed accounts from Zoho...")

    while True:
        params = {
            "page": page,
            "per_page": 200,
            "fields": "Account_Name,Claimed"
        }
        resp = session.get(f"{ZOHO_DOMAIN}/crm/v2/Accounts", headers=headers, params=params)
        resp.raise_for_status()

        data = resp.json().get("data", [])
        if not data:
            break

        # Filter to only accounts with Claimed field set
        for acc in data:
            claimed = acc.get("Claimed")
            if claimed and isinstance(claimed, dict):
                all_accounts.append({
                    "account_id": acc.get("Account_Name"),  # Account_Name is the TryBooking ID
                    "claimed_user_id": claimed.get("id"),
                    "claimed_user_name": claimed.get("name")
                })

        if not resp.json().get("info", {}).get("more_records"):
            break
        page += 1

    print(f"  Found {len(all_accounts)} claimed accounts")

    if not all_accounts:
        return pd.DataFrame(columns=["account_id", "claimed_user_id", "claimed_user_name"])

    return pd.DataFrame(all_accounts)


def find_first_paid_events(booking_df, report_start, report_end):
    """
    Find accounts whose first paid event has completed.

    This reuses the logic from event_completion_reminders.py for consistency.

    Args:
        booking_df: Booking data DataFrame (filtered to claimed accounts only)
        report_start: Start of reporting period (inclusive), or None for all time
        report_end: End of reporting period (exclusive), or None for all time

    Returns:
        DataFrame with qualifying events and their metrics
    """
    if booking_df.empty:
        return pd.DataFrame()

    # Filter to successful paid transactions
    paid_bookings = booking_df[
        (booking_df['Status'] == 'Successful') &
        (booking_df['PaymentReceived'] > 0)
    ].copy()

    if paid_bookings.empty:
        print("  No successful paid transactions found")
        return pd.DataFrame()

    # Find all last sessions per event (handles multi-session events)
    # max(EventDate) = when the event fully completes
    all_last_sessions = paid_bookings.groupby('EventId')['EventDate'].agg(['min', 'max'])
    all_last_sessions.columns = ['first_session', 'last_session']

    # Find completed events based on period
    now = datetime.now(UK_TZ)
    if report_start is not None and report_end is not None:
        # Specific period: events where final session falls within the reporting period
        completed_event_ids = all_last_sessions[
            (all_last_sessions['last_session'].dt.date >= report_start.date()) &
            (all_last_sessions['last_session'].dt.date < report_end.date())
        ].index
    else:
        # All time: any event that has fully completed (last session in the past)
        completed_event_ids = all_last_sessions[
            all_last_sessions['last_session'].dt.date < now.date()
        ].index

    print(f"  Events completed in reporting month: {len(completed_event_ids)}")

    if len(completed_event_ids) == 0:
        return pd.DataFrame()

    # Get event metrics for completed events
    completed_events = paid_bookings[paid_bookings['EventId'].isin(completed_event_ids)]
    event_metrics = completed_events.groupby('EventId').agg({
        'PaymentReceived': 'sum',
        'BookingFee': 'sum',
        'CardFee': 'sum',
        'ProcessingFee': 'sum',
        'TicketFee': 'sum',
        'TicketQuantity': 'sum',
        'AccountId': 'first',
        'EventName': 'first'
    }).reset_index()

    # Add completion date (last session date)
    event_metrics = event_metrics.merge(
        all_last_sessions[['last_session']],
        left_on='EventId',
        right_index=True
    )
    event_metrics.rename(columns={'last_session': 'event_completed_date'}, inplace=True)

    # Find first PAID event per account
    # First event = earliest EventId with paid transactions (EventId correlates with creation order)
    first_paid_events = paid_bookings.groupby('AccountId')['EventId'].min().reset_index()
    first_paid_events.columns = ['AccountId', 'FirstPaidEventId']

    # Mark which events are first paid events
    event_metrics = event_metrics.merge(first_paid_events, on='AccountId', how='left')
    event_metrics['is_first_paid_event'] = event_metrics['EventId'] == event_metrics['FirstPaidEventId']

    # Filter to only first paid events
    qualifying_events = event_metrics[event_metrics['is_first_paid_event']].copy()

    print(f"  First paid events completing in month: {len(qualifying_events)}")

    return qualifying_events


def calculate_commission(row, config):
    """
    Calculate commission for a single account/event.

    Args:
        row: DataFrame row with event metrics and sales person info
        config: Commission configuration

    Returns:
        Dict with commission details
    """
    rates = get_rates_for_sales_person(config, row['sales_person_name'])

    if row['GatewayGroup'] == 'Stripe Connect':
        # Stripe Connect: 10% of (TicketFee + BookingFee)
        fee_commission = (row['TicketFee'] + row['BookingFee']) * rates['commission_rate']
    else:
        # Default (All): 10% of (ProcessingFee + CardFee)
        fee_commission = (row['ProcessingFee'] + row['CardFee']) * rates['commission_rate']

    return {
        'flat_fee': rates['flat_fee'],
        'commission_rate': rates['commission_rate'],
        'commission_on_sales': round(fee_commission, 2),
        'total_commission': round(rates['flat_fee'] + fee_commission, 2)
    }


def generate_html_email_content(report_df, period_description, is_individual=False, sales_person_name=None):
    """
    Generate HTML email content for the commission report.

    Args:
        report_df: DataFrame with commission data
        period_description: Human-readable period (e.g., "November 2025" or "All Time")
        is_individual: If True, this is for an individual sales person
        sales_person_name: Name of sales person (for individual reports)

    Returns:
        HTML string
    """
    period_name = period_description

    if report_df.empty:
        return f"""
        <div style="font-family: Arial, sans-serif; font-size: 11pt;">
            <h2>Sales Commission Report - {period_name}</h2>
            <p>No qualifying commissions for this period.</p>
        </div>
        """

    # Calculate totals
    total_accounts = len(report_df)
    total_commission = report_df['total_commission'].sum()
    total_ticket_sales = report_df['ticket_sales'].sum()

    if is_individual:
        title = f"Your Sales Commission - {period_name}"
        intro = f"<p>Hi {sales_person_name.split()[0] if sales_person_name else 'there'},</p><p>Here is your commission report for {period_name}.</p>"
    else:
        title = f"Sales Commission Summary - {period_name}"
        intro = f"<p>Commission report for all sales team members for {period_name}.</p>"

        # Add per-person summary for MD report
        person_summary = report_df.groupby('sales_person_name').agg({
            'account_id': 'count',
            'total_commission': 'sum'
        }).reset_index()
        person_summary.columns = ['Sales Person', 'Accounts', 'Total Commission']
        person_summary['Total Commission'] = person_summary['Total Commission'].apply(lambda x: f"£{x:,.2f}")

        person_table = """
        <h3>Commission by Sales Person</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr style="background-color: #f0f0f0;">
                <th>Sales Person</th>
                <th>Accounts</th>
                <th>Total Commission</th>
            </tr>
        """
        for _, row in person_summary.iterrows():
            person_table += f"""
            <tr>
                <td>{row['Sales Person']}</td>
                <td>{row['Accounts']}</td>
                <td>{row['Total Commission']}</td>
            </tr>
            """
        person_table += "</table>"
        intro += person_table

    html = f"""
    <div style="font-family: Arial, sans-serif; font-size: 11pt;">
        <h2>{title}</h2>
        {intro}

        <h3>Summary</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr><th style="text-align: left;">Total Qualifying Accounts</th><td>{total_accounts}</td></tr>
            <tr><th style="text-align: left;">Total Ticket Sales</th><td>£{total_ticket_sales:,.2f}</td></tr>
            <tr><th style="text-align: left;">Total Commission Payable</th><td><strong>£{total_commission:,.2f}</strong></td></tr>
        </table>

        <p>Full details are attached as a CSV file.</p>

        <p style="color: #666; font-size: 10pt;">
            This report was generated automatically on {datetime.now(UK_TZ).strftime('%d %B %Y at %H:%M')}.
        </p>
    </div>
    """

    return html


def send_reports(report_df, period_description, commission_config):
    """
    Send commission reports via email.

    Args:
        report_df: DataFrame with all commission data
        period_description: Human-readable period (e.g., "November 2025" or "All Time")
        commission_config: Commission configuration dict (for email overrides)
    """
    period_name = period_description
    # Generate a file-safe code for filenames
    file_code = period_description.replace(' ', '_').lower()

    # Prepare CSV columns for output
    csv_columns = [
        'sales_person_name', 'account_id', 'account_name', 'account_created_date',
        'event_completed_date', 'event_name', 'event_id', 'gateway_type',
        'ticket_sales', 'commission_on_sales', 'total_commission'
    ]

    # In TEST_MODE, all emails go to MD_EMAIL only
    if TEST_MODE:
        print("\n*** TEST MODE: All emails will be sent to MD only ***")

    # Send MD summary report
    print("\nSending MD summary report...")
    summary_filename = f"commission_summary_{file_code}.csv"

    if not report_df.empty:
        summary_csv = report_df[csv_columns].to_csv(index=False)
    else:
        # Empty CSV with headers
        summary_csv = ','.join(csv_columns) + '\n'

    summary_html = generate_html_email_content(report_df, period_name, is_individual=False)

    send_html_email(
        to=MD_EMAIL,
        subject=f"Sales Commission Summary - {period_name}",
        html_content=summary_html,
        attachments=[(summary_filename, summary_csv.encode('utf-8'), 'text', 'csv')]
    )

    if report_df.empty:
        print("  No individual reports to send (no qualifying commissions)")
        return

    # Send individual reports to each sales person
    print("\nSending individual reports...")

    for sales_person_name in report_df['sales_person_name'].unique():
        person_df = report_df[report_df['sales_person_name'] == sales_person_name]

        # Get sales person email from config
        sales_person_email = None
        person_config = commission_config.get('sales_people', {}).get(sales_person_name, {})

        if person_config.get('email'):
            sales_person_email = person_config['email']

        # Generate individual report
        person_filename = f"commission_{sales_person_name.replace(' ', '_')}_{file_code}.csv"
        person_csv = person_df[csv_columns].to_csv(index=False)
        person_html = generate_html_email_content(
            person_df, period_name,
            is_individual=True,
            sales_person_name=sales_person_name
        )

        if TEST_MODE:
            # TEST MODE: Send all individual reports to MD only
            send_html_email(
                to=MD_EMAIL,
                subject=f"[TEST] Commission for {sales_person_name} - {period_name}",
                html_content=person_html,
                attachments=[(person_filename, person_csv.encode('utf-8'), 'text', 'csv')]
            )
            print(f"  Sent {sales_person_name}'s report to MD (TEST MODE)")
        elif sales_person_email:
            # PRODUCTION: Send to sales person with MD on CC
            send_html_email(
                to=sales_person_email,
                cc=MD_EMAIL,
                subject=f"Your Sales Commission - {period_name}",
                html_content=person_html,
                attachments=[(person_filename, person_csv.encode('utf-8'), 'text', 'csv')]
            )
            print(f"  Sent to {sales_person_name} ({sales_person_email})")
        else:
            # No email found - send to MD only
            print(f"  Warning: No email found for {sales_person_name}, sending to MD only")

            send_html_email(
                to=MD_EMAIL,
                subject=f"Sales Commission for {sales_person_name} - {period_name}",
                html_content=person_html,
                attachments=[(person_filename, person_csv.encode('utf-8'), 'text', 'csv')]
            )


def main():
    """Main function to run the sales commission report."""
    print("=" * 60)
    print("Sales Commission Report")
    print("=" * 60)

    # Validate environment
    validate_environment()

    # Determine report period
    report_start, report_end, period_description = get_report_period()
    print(f"\nReport Period: {period_description}")
    if report_start is not None:
        print(f"  Start: {report_start.strftime('%Y-%m-%d')}")
        print(f"  End: {report_end.strftime('%Y-%m-%d')} (exclusive)")
    else:
        print("  Processing all completed first events")
    print(f"Test mode: {'ON' if TEST_MODE else 'OFF'}")

    # Load commission configuration
    print("\nLoading commission configuration...")
    commission_config = load_commission_config()
    print(f"  Default flat fee: £{commission_config['default_rates']['flat_fee']:.2f}")
    print(f"  Default commission rate: {commission_config['default_rates']['commission_rate'] * 100:.0f}%")
    if commission_config.get('sales_people'):
        print(f"  Custom rates configured for: {', '.join(commission_config['sales_people'].keys())}")

    # Phase 1: Fetch Zoho data FIRST
    print("\n" + "=" * 60)
    print("Phase 1: Fetching Zoho CRM data")
    print("=" * 60)

    token = get_access_token()
    claimed_accounts = fetch_claimed_accounts(token)

    if claimed_accounts.empty:
        print("\nNo claimed accounts found in Zoho CRM. Exiting.")
        sys.exit(0)

    # Phase 2: Load commission history and filter
    print("\n" + "=" * 60)
    print("Phase 2: Loading commission history")
    print("=" * 60)

    history = load_commission_history()
    paid_account_ids = {str(h['account_id']) for h in history.get('paid_accounts', [])}
    print(f"  Previously paid accounts: {len(paid_account_ids)}")

    # Filter out already-paid accounts
    claimed_accounts['account_id'] = claimed_accounts['account_id'].astype(str)
    unpaid_claimed = claimed_accounts[~claimed_accounts['account_id'].isin(paid_account_ids)]
    print(f"  Claimed accounts not yet paid: {len(unpaid_claimed)}")

    if unpaid_claimed.empty:
        print("\nNo unpaid claimed accounts. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, commission_config)
        sys.exit(0)

    # Phase 3: Load S3 data (filtered to claimed accounts)
    print("\n" + "=" * 60)
    print("Phase 3: Loading S3 booking data")
    print("=" * 60)

    target_date = get_latest_data_date()
    print(f"  Data date: {target_date.strftime('%Y-%m-%d')}")

    booking_df = load_booking_data(target_date=target_date, data_type='BookingDataAll')
    accounts_df = load_accounts_data(target_date=target_date)

    # Filter booking data to claimed accounts only
    claimed_account_ids = set(unpaid_claimed['account_id'].astype(str))
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    booking_df = booking_df[booking_df['AccountId'].isin(claimed_account_ids)]
    print(f"  Bookings for claimed accounts: {len(booking_df):,}")

    if booking_df.empty:
        print("\nNo booking data for claimed accounts. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, commission_config)
        sys.exit(0)

    # Phase 4: Find qualifying events
    print("\n" + "=" * 60)
    print("Phase 4: Finding qualifying events")
    print("=" * 60)

    qualifying_events = find_first_paid_events(booking_df, report_start, report_end)

    if qualifying_events.empty:
        print("\nNo qualifying events found. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, commission_config)
        sys.exit(0)

    # Phase 5: Enrich with account and sales person data
    print("\n" + "=" * 60)
    print("Phase 5: Enriching data and calculating commissions")
    print("=" * 60)

    # Convert AccountId to string for joining
    qualifying_events['AccountId'] = qualifying_events['AccountId'].astype(str)

    # Merge with account data for gateway type and creation date
    accounts_df['AccountId'] = accounts_df['AccountId'].astype(str)
    qualifying_events = qualifying_events.merge(
        accounts_df[['AccountId', 'AccountName', 'DateTimeCreated', 'GatewayGroup']],
        on='AccountId',
        how='left'
    )

    # Merge with claimed accounts data for sales person info
    qualifying_events = qualifying_events.merge(
        unpaid_claimed[['account_id', 'claimed_user_id', 'claimed_user_name']],
        left_on='AccountId',
        right_on='account_id',
        how='inner'
    )

    print(f"  Qualifying accounts after joining: {len(qualifying_events)}")

    if qualifying_events.empty:
        print("\nNo accounts match after joining with Zoho data. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, commission_config)
        sys.exit(0)

    # Calculate commissions
    qualifying_events['sales_person_name'] = qualifying_events['claimed_user_name']

    commission_results = qualifying_events.apply(
        lambda row: calculate_commission(row, commission_config), axis=1, result_type='expand'
    )
    qualifying_events = pd.concat([qualifying_events, commission_results], axis=1)

    # Prepare final report DataFrame
    report_df = qualifying_events.rename(columns={
        'AccountId': 'account_id',
        'AccountName': 'account_name',
        'DateTimeCreated': 'account_created_date',
        'EventName': 'event_name',
        'EventId': 'event_id',
        'GatewayGroup': 'gateway_type',
        'PaymentReceived': 'ticket_sales'
    })

    # Format dates
    if 'account_created_date' in report_df.columns and report_df['account_created_date'].notna().any():
        report_df['account_created_date'] = pd.to_datetime(report_df['account_created_date']).dt.strftime('%Y-%m-%d')
    if 'event_completed_date' in report_df.columns and report_df['event_completed_date'].notna().any():
        report_df['event_completed_date'] = pd.to_datetime(report_df['event_completed_date']).dt.strftime('%Y-%m-%d')

    # Select and order columns
    final_columns = [
        'sales_person_name', 'account_id', 'account_name', 'account_created_date',
        'event_completed_date', 'event_name', 'event_id', 'gateway_type',
        'ticket_sales', 'commission_on_sales', 'total_commission', 'claimed_user_id'
    ]
    report_df = report_df[[col for col in final_columns if col in report_df.columns]]

    # Print summary
    print(f"\n  Total qualifying accounts: {len(report_df)}")
    print(f"  Total commission payable: £{report_df['total_commission'].sum():,.2f}")

    # Phase 6: Send reports
    print("\n" + "=" * 60)
    print("Phase 6: Sending reports")
    print("=" * 60)

    send_reports(report_df, period_description, commission_config)

    # Phase 7: Update commission history
    print("\n" + "=" * 60)
    print("Phase 7: Updating commission history")
    print("=" * 60)

    new_paid_accounts = []
    now = datetime.now(UK_TZ)
    for _, row in report_df.iterrows():
        new_paid_accounts.append({
            'account_id': str(row['account_id']),
            'paid_date': now.strftime('%Y-%m'),
            'event_id': str(row['event_id']),
            'amount': row['total_commission'],
            'sales_person': row['sales_person_name']
        })

    history['paid_accounts'].extend(new_paid_accounts)
    save_commission_history(history)
    print(f"  Added {len(new_paid_accounts)} accounts to commission history")

    print("\n" + "=" * 60)
    print("Sales Commission Report Complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
