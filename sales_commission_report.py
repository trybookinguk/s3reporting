#!/usr/bin/env python3
"""
Sales Commission Report

Monthly commission calculation for sales team members based on claimed accounts
in Zoho CRM whose first paid event has completed.

Commission Rules:
- £15 flat fee per qualifying account (rate configurable per sales person)
- Default gateway: 10% of (ProcessingFee + CardFee)
- Stripe Connect: 10% of (TicketFee + BookingFee)
- Commission RATES are configurable per sales person via sales_commission_config.json.
  Salesperson EMAIL addresses are no longer configured here — each report is sent
  to the person's Zoho login email, resolved from the account's "Claimed" user.
  (Requires the ZohoCRM.users.READ scope on the refresh token.)

Usage:
    python3 sales_commission_report.py

Environment Variables:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY - S3 access
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN - Zoho CRM access
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX - Email sending via Microsoft Graph
    TEST_MODE - If 'true', sends emails only to henry@trybooking.co.uk
    REPORT_MONTH - Optional, format 'YYYY-MM' to specify report month (defaults to previous month)
"""
import os
import sys
import json
import io
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from weasyprint import HTML

from modules.utils.config import (
    TEST_MODE, UK_TZ, ZOHO_DOMAIN,
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX,
    get_recipients,
)
from modules.utils.zoho_api import get_access_token, get_session
from modules.utils.data_loader import load_booking_data, load_accounts_data
from modules.utils.email_utils import send_html_email
from modules.utils.date_utils import get_latest_data_date

# Constants
COMMISSION_CONFIG_FILE = 'sales_commission_config.json'
TEST_MODE_RECIPIENT = 'henry@trybooking.co.uk'
# The summary recipient — whoever manages commissions — is set in SharePoint →
# Platform Data/report_recipients.json (key "monthly_commission_summary").
# See docs/notion/managing_report_emails.md.
# get_recipients() already returns the test recipient when TEST_MODE is on, so
# SUMMARY_EMAIL becomes the catch-all that per-team-member emails also redirect to.
_summary_to, _ = get_recipients("monthly_commission_summary")
SUMMARY_EMAIL = _summary_to or TEST_MODE_RECIPIENT
# Individual team members are emailed their own report at their Zoho login email,
# resolved from the account's "Claimed" user via fetch_user_emails().


def validate_environment():
    """Validate required environment variables are set."""
    required_vars = [
        ('AWS_ACCESS_KEY_ID', AWS_ACCESS_KEY_ID),
        ('AWS_SECRET_ACCESS_KEY', AWS_SECRET_ACCESS_KEY),
        ('ZOHO_CLIENT_ID', ZOHO_CLIENT_ID),
        ('ZOHO_CLIENT_SECRET', ZOHO_CLIENT_SECRET),
        ('ZOHO_REFRESH_TOKEN', ZOHO_REFRESH_TOKEN),
        ('AZURE_TENANT_ID', AZURE_TENANT_ID),
        ('AZURE_CLIENT_ID', AZURE_CLIENT_ID),
        ('AZURE_CLIENT_SECRET', AZURE_CLIENT_SECRET),
        ('AZURE_SENDER_MAILBOX', AZURE_SENDER_MAILBOX),
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


def fetch_user_emails(token):
    """
    Fetch every active Zoho CRM user's email, keyed by their Zoho user id.

    The salesperson on each account is the Zoho "Claimed" user, which carries
    a user id. We resolve that id to the person's real Zoho login email here,
    so commission reports go to the right inbox automatically — no per-person
    email config to maintain.

    Requires the ZohoCRM.users.READ scope on the refresh token. (COQL returns
    user references as a bare {id} with no email, so we use the REST Users API.)

    Args:
        token: Zoho OAuth access token

    Returns:
        Dict of {user_id (str): email (str)} for active users with an email.
    """
    session = get_session()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    emails = {}
    page = 1

    print("Fetching Zoho user emails...")

    while True:
        params = {"type": "ActiveUsers", "page": page, "per_page": 200}
        resp = session.get(f"{ZOHO_DOMAIN}/crm/v2/users", headers=headers, params=params)
        resp.raise_for_status()

        users = resp.json().get("users", [])
        if not users:
            break

        for user in users:
            uid = user.get("id")
            email = user.get("email")
            if uid and email:
                emails[str(uid)] = email

        if not resp.json().get("info", {}).get("more_records"):
            break
        page += 1

    print(f"  Found emails for {len(emails)} active users")
    return emails


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

    # Filter to events with at least 10 tickets sold (to exclude test/minimal events)
    MIN_TICKETS_FOR_COMMISSION = 10
    before_ticket_filter = len(qualifying_events)
    qualifying_events = qualifying_events[qualifying_events['TicketQuantity'] >= MIN_TICKETS_FOR_COMMISSION]
    filtered_out = before_ticket_filter - len(qualifying_events)
    if filtered_out > 0:
        print(f"  Events filtered out (< {MIN_TICKETS_FOR_COMMISSION} tickets): {filtered_out}")

    # Add total fees column (sum of all fee types)
    qualifying_events['TotalFees'] = (
        qualifying_events['BookingFee'] +
        qualifying_events['CardFee'] +
        qualifying_events['ProcessingFee'] +
        qualifying_events['TicketFee']
    )

    print(f"  Qualifying first paid events (>= {MIN_TICKETS_FOR_COMMISSION} tickets): {len(qualifying_events)}")

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
    total_fees = report_df['total_fees'].sum()
    total_flat_fees = report_df['flat_fee'].sum()
    total_commission_on_sales = report_df['commission_on_sales'].sum()

    if is_individual:
        title = f"Your Sales Commission - {period_name}"
        intro = f"<p>Hi {sales_person_name.split()[0] if sales_person_name else 'there'},</p><p>Here is your commission report for {period_name}.</p>"
    else:
        title = f"Sales Commission Summary - {period_name}"
        intro = f"<p>Commission report for all sales team members for {period_name}.</p>"

        # Add per-person summary for the overall summary report
        person_summary = report_df.groupby('team_member_name').agg(
            Accounts=('total_commission', 'count'),
            Total_Commission=('total_commission', 'sum')
        ).reset_index()
        person_summary.columns = ['Team Member', 'Accounts', 'Total Commission']
        person_summary['Total Commission'] = person_summary['Total Commission'].apply(lambda x: f"£{x:,.2f}")

        person_table = """
        <h3>Commission by Team Member</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr style="background-color: #f0f0f0;">
                <th>Team Member</th>
                <th>Accounts</th>
                <th>Total Commission</th>
            </tr>
        """
        for _, row in person_summary.iterrows():
            person_table += f"""
            <tr>
                <td>{row['Team Member']}</td>
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
            <tr><th style="text-align: left;">Total Fees</th><td>£{total_fees:,.2f}</td></tr>
            <tr><th style="text-align: left;">New Account Commission</th><td>£{total_flat_fees:,.2f}</td></tr>
            <tr><th style="text-align: left;">Event Commission</th><td>£{total_commission_on_sales:,.2f}</td></tr>
            <tr><th style="text-align: left;">Total Commission Payable</th><td><strong>£{total_commission:,.2f}</strong></td></tr>
        </table>

        <p>Full details are attached as a CSV file.</p>

        <p style="color: #666; font-size: 10pt;">
            This report was generated automatically on {datetime.now(UK_TZ).strftime('%d %B %Y at %H:%M')}.
        </p>
    </div>
    """

    return html


def generate_pdf_report(report_df, period_description, sales_person_name):
    """
    Generate a styled PDF commission report for a sales person.

    Args:
        report_df: DataFrame with commission data for this sales person
        period_description: Human-readable period (e.g., "November 2025")
        sales_person_name: Name of the sales person

    Returns:
        bytes: PDF content
    """
    if report_df.empty:
        # Empty report
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                h1 {{ color: #333; }}
            </style>
        </head>
        <body>
            <h1>Sales Commission Report - {period_description}</h1>
            <p>Hi {sales_person_name.split()[0] if sales_person_name else 'there'},</p>
            <p>No qualifying commissions for this period.</p>
        </body>
        </html>
        """
        pdf_buffer = io.BytesIO()
        HTML(string=html_content).write_pdf(pdf_buffer)
        return pdf_buffer.getvalue()

    # Calculate totals
    total_accounts = len(report_df)
    total_commission = report_df['total_commission'].sum()
    total_ticket_sales = report_df['ticket_sales'].sum()
    total_fees = report_df['total_fees'].sum()
    total_flat_fees = report_df['flat_fee'].sum()
    total_commission_on_sales = report_df['commission_on_sales'].sum()

    # Sort by event completed date
    sorted_df = report_df.copy()
    sorted_df['_sort_date'] = pd.to_datetime(sorted_df['event_completed_date'], errors='coerce')
    sorted_df = sorted_df.sort_values('_sort_date', ascending=True)

    # Build the detail rows
    detail_rows = ""
    for _, row in sorted_df.iterrows():
        detail_rows += f"""
        <tr>
            <td>{row.get('account_name', row.get('account_id', 'N/A'))}</td>
            <td>{row.get('account_created_date', 'N/A')}</td>
            <td>{row.get('event_name', 'N/A')}</td>
            <td>{row.get('event_completed_date', 'N/A')}</td>
            <td style="text-align: right;">£{row.get('ticket_sales', 0):,.2f}</td>
            <td style="text-align: right;">£{row.get('total_fees', 0):,.2f}</td>
            <td style="text-align: right;">£{row.get('flat_fee', 0):,.2f}</td>
            <td style="text-align: right;">£{row.get('commission_on_sales', 0):,.2f}</td>
            <td style="text-align: right;"><strong>£{row.get('total_commission', 0):,.2f}</strong></td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
        <style>
            @page {{
                size: A4 landscape;
                margin: 20mm;
            }}
            body {{
                font-family: 'Inter', sans-serif;
                font-size: 10pt;
                color: #333;
            }}
            .header {{
                border-bottom: 3px solid #01517f;
                padding-bottom: 15px;
                margin-bottom: 20px;
            }}
            .header h1 {{
                color: #01517f;
                margin: 0;
                font-size: 24pt;
                font-weight: 600;
            }}
            .header .period {{
                color: #666;
                font-size: 14pt;
                margin-top: 5px;
            }}
            .greeting {{
                margin-bottom: 20px;
            }}
            .summary-section {{
                background: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 5px;
                padding: 15px;
                margin-bottom: 25px;
            }}
            .summary-section h2 {{
                margin-top: 0;
                color: #01517f;
                font-size: 14pt;
                font-weight: 600;
            }}
            .summary-table {{
                width: 100%;
                border-collapse: collapse;
            }}
            .summary-table th {{
                text-align: left;
                padding: 8px 15px 8px 0;
                border-bottom: 1px solid #dee2e6;
                width: 70%;
                font-weight: 500;
            }}
            .summary-table td {{
                text-align: right;
                padding: 8px 0;
                border-bottom: 1px solid #dee2e6;
                font-weight: 600;
            }}
            .summary-table tr:last-child th,
            .summary-table tr:last-child td {{
                border-bottom: none;
                font-size: 12pt;
                color: #01517f;
            }}
            .detail-section {{
                page-break-before: always;
            }}
            .detail-section h2 {{
                color: #01517f;
                font-size: 14pt;
                font-weight: 600;
                margin-bottom: 10px;
            }}
            .detail-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 9pt;
            }}
            .detail-table th {{
                background: #01517f;
                color: white;
                padding: 10px 8px;
                text-align: left;
                font-weight: 600;
            }}
            .detail-table th:nth-child(n+5) {{
                text-align: right;
            }}
            .detail-table td {{
                padding: 8px;
                border-bottom: 1px solid #dee2e6;
            }}
            .detail-table tr:nth-child(even) {{
                background: #f8f9fa;
            }}
            .footer {{
                margin-top: 30px;
                padding-top: 15px;
                border-top: 1px solid #dee2e6;
                color: #666;
                font-size: 9pt;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Sales Commission Report</h1>
            <div class="period">{period_description}</div>
        </div>

        <div class="greeting">
            <p>Hi {sales_person_name.split()[0] if sales_person_name else 'there'},</p>
            <p>Here is your commission report for {period_description}.</p>
        </div>

        <div class="summary-section">
            <h2>Summary</h2>
            <table class="summary-table">
                <tr><th>Total Qualifying Accounts</th><td>{total_accounts}</td></tr>
                <tr><th>Total Ticket Sales</th><td>£{total_ticket_sales:,.2f}</td></tr>
                <tr><th>Total Fees</th><td>£{total_fees:,.2f}</td></tr>
                <tr><th>New Account Commission</th><td>£{total_flat_fees:,.2f}</td></tr>
                <tr><th>Event Commission</th><td>£{total_commission_on_sales:,.2f}</td></tr>
                <tr><th>Total Commission Payable</th><td>£{total_commission:,.2f}</td></tr>
            </table>
        </div>

        <div class="detail-section">
            <h2>Commission Details</h2>
            <table class="detail-table">
                <thead>
                    <tr>
                        <th>Account</th>
                        <th>Opened</th>
                        <th>Event</th>
                        <th>Completed</th>
                        <th>Ticket Sales</th>
                        <th>Fees</th>
                        <th>New Account</th>
                        <th>Event Comm.</th>
                        <th>Total</th>
                    </tr>
                </thead>
                <tbody>
                    {detail_rows}
                </tbody>
            </table>
        </div>

        <div class="footer">
            <p>This report was generated automatically on {datetime.now(UK_TZ).strftime('%d %B %Y at %H:%M')}.</p>
        </div>
    </body>
    </html>
    """

    pdf_buffer = io.BytesIO()
    HTML(string=html_content).write_pdf(pdf_buffer)
    return pdf_buffer.getvalue()


def send_reports(report_df, period_description, user_emails):
    """
    Send commission reports via email.

    Args:
        report_df: DataFrame with all commission data
        period_description: Human-readable period (e.g., "November 2025" or "All Time")
        user_emails: Dict of {zoho_user_id: email} used to address each
            salesperson's individual report (from fetch_user_emails).
    """
    period_name = period_description
    # Generate a file-safe code for filenames
    file_code = period_description.replace(' ', '_').lower()

    # Prepare CSV columns for output (internal names)
    csv_columns = [
        'team_member_name', 'account_id', 'account_name', 'account_created_date',
        'event_completed_date', 'event_name', 'event_id', 'gateway_type',
        'ticket_sales', 'total_fees', 'flat_fee', 'commission_on_sales', 'total_commission'
    ]

    # User-friendly column names for CSV export
    csv_column_names = {
        'team_member_name': 'Team Member',
        'account_id': 'Account ID',
        'account_name': 'Account Name',
        'account_created_date': 'Account Created',
        'event_completed_date': 'Event Completed',
        'event_name': 'Event Name',
        'event_id': 'Event ID',
        'gateway_type': 'Gateway',
        'ticket_sales': 'Ticket Sales (£)',
        'total_fees': 'Total Fees (£)',
        'flat_fee': 'New Account Commission (£)',
        'commission_on_sales': 'Event Commission (£)',
        'total_commission': 'Total Commission (£)'
    }

    # Money columns must render with exactly 2dp in CSVs — commission cheques
    # are cut from these numbers, so 15.0 / 15 / 15.000000001 are all
    # unacceptable. Round in-memory and let to_csv's float_format pin the
    # display precision belt-and-braces.
    MONEY_COLUMNS = ['ticket_sales', 'total_fees', 'flat_fee',
                     'commission_on_sales', 'total_commission']

    def to_csv_2dp(df):
        if df.empty:
            return ','.join(csv_column_names.values()) + '\n'
        out = df[csv_columns].copy()
        # Money columns: pre-format as strings so to_csv emits exactly 2dp and
        # float_format can't touch IDs. Anything that round-trips through float
        # loses precision (e.g. 17.07 → 17.069999... → "17.07" requires the
        # float_format step, which then also stringifies EventID as "97014.00").
        for col in MONEY_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(2).map(
                lambda v: '' if pd.isna(v) else f'{v:.2f}'
            )
        # IDs come in as floats from the warehouse-merged frame; render as int.
        for col in ('account_id', 'event_id'):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors='coerce').astype('Int64').astype(str)
                out[col] = out[col].replace('<NA>', '')
        return out.rename(columns=csv_column_names).to_csv(index=False)

    # In TEST_MODE, every email goes to the summary recipient only.
    if TEST_MODE:
        print("\n*** TEST MODE: All emails will be sent to the summary recipient only ***")

    # Send the overall summary to whoever manages commissions.
    print("\nSending commission summary...")
    summary_filename = f"commission_summary_{file_code}.csv"

    summary_csv = to_csv_2dp(report_df)

    summary_html = generate_html_email_content(report_df, period_name, is_individual=False)

    send_html_email(
        to=SUMMARY_EMAIL,
        subject=f"Sales Commission Summary - {period_name}",
        html_content=summary_html,
        attachments=[(summary_filename, summary_csv.encode('utf-8'), 'text', 'csv')]
    )

    if report_df.empty:
        print("  No individual reports to send (no qualifying commissions)")
        return

    # Send individual reports to each team member
    print("\nSending individual reports...")

    for team_member_name in report_df['team_member_name'].unique():
        person_df = report_df[report_df['team_member_name'] == team_member_name]

        # Resolve the salesperson's email from their Zoho user id. The id rides
        # along in the report frame (from the account's "Claimed" user), so we
        # just map it through the Zoho user emails — no per-person email config.
        team_member_email = None
        if 'claimed_user_id' in person_df.columns:
            user_id = str(person_df['claimed_user_id'].iloc[0])
            team_member_email = user_emails.get(user_id)

        # Generate individual report files
        person_filename_csv = f"commission_{team_member_name.replace(' ', '_')}_{file_code}.csv"
        person_filename_pdf = f"commission_{team_member_name.replace(' ', '_')}_{file_code}.pdf"
        person_csv = to_csv_2dp(person_df)
        person_pdf = generate_pdf_report(person_df, period_name, team_member_name)
        person_html = generate_html_email_content(
            person_df, period_name,
            is_individual=True,
            sales_person_name=team_member_name
        )

        # Summary recipient gets the CSV; the salesperson gets the PDF.
        pdf_attachment = [(person_filename_pdf, person_pdf, 'application', 'pdf')]
        csv_attachment = [(person_filename_csv, person_csv.encode('utf-8'), 'text', 'csv')]

        if not team_member_email:
            # No Zoho email found for this user - send the CSV to the summary
            # recipient so the commission isn't lost, and flag it for follow-up.
            send_html_email(
                to=SUMMARY_EMAIL,
                subject=f"Commission for {team_member_name} - {period_name}",
                html_content=person_html,
                attachments=csv_attachment
            )
            print(f"  Sent {team_member_name}'s CSV to the summary recipient (no Zoho email found for this user)")
            continue

        if TEST_MODE:
            # TEST MODE: send both PDF and CSV to the summary recipient only.
            send_html_email(
                to=SUMMARY_EMAIL,
                subject=f"[TEST] Commission for {team_member_name} - {period_name}",
                html_content=person_html,
                attachments=pdf_attachment + csv_attachment
            )
            print(f"  Sent {team_member_name}'s report to the summary recipient (TEST MODE)")
        else:
            # PRODUCTION: PDF to the salesperson, CSV to the summary recipient.
            send_html_email(
                to=team_member_email,
                subject=f"Your Sales Commission - {period_name}",
                html_content=person_html,
                attachments=pdf_attachment
            )
            send_html_email(
                to=SUMMARY_EMAIL,
                subject=f"Commission for {team_member_name} - {period_name}",
                html_content=person_html,
                attachments=csv_attachment
            )
            print(f"  Sent PDF to {team_member_name} ({team_member_email}), CSV to the summary recipient")


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
    # Resolve salesperson emails once, up front, so every report path can use them.
    user_emails = fetch_user_emails(token)

    if claimed_accounts.empty:
        print("\nNo claimed accounts found in Zoho CRM. Exiting.")
        sys.exit(0)

    # Ensure account_id is string for matching
    claimed_accounts['account_id'] = claimed_accounts['account_id'].astype(str)

    # Phase 2: Load S3 data (filtered to claimed accounts)
    print("\n" + "=" * 60)
    print("Phase 2: Loading S3 booking data")
    print("=" * 60)

    target_date = get_latest_data_date()
    print(f"  Data date: {target_date.strftime('%Y-%m-%d')}")

    # Load bookings only for the claimed accounts. The original implementation
    # loaded the full all-time BookingDataAll (>3 GB in memory) then filtered;
    # that OOMs the 4 GB Pi. The warehouse can apply the AccountId filter at
    # the source, which keeps the working set tiny (231 accounts' bookings,
    # typically a few hundred thousand rows).
    claimed_account_ids = set(claimed_accounts['account_id'].astype(str))
    claimed_account_ints = sorted({int(a) for a in claimed_account_ids if a.isdigit()})

    print(f"  Querying warehouse for bookings of {len(claimed_account_ints)} claimed accounts...")
    from modules import warehouse
    conn = warehouse.connect()
    try:
        # Chunk the IN clause — SQLite has a default 999-param limit.
        chunks = [claimed_account_ints[i:i + 500] for i in range(0, len(claimed_account_ints), 500)]
        booking_parts = []
        for chunk in chunks:
            placeholders = ",".join(["?"] * len(chunk))
            booking_parts.append(pd.read_sql_query(
                f"SELECT AccountId, EventId, EventName, EventDate, TransactionDate, "
                f"Status, PaymentReceived, BookingFee, CardFee, ProcessingFee, "
                f"TicketFee, TicketQuantity "
                f"FROM bookings WHERE AccountId IN ({placeholders})",
                conn, params=chunk,
            ))
        booking_df = pd.concat(booking_parts, ignore_index=True) if booking_parts else pd.DataFrame()
    finally:
        conn.close()

    accounts_df = load_accounts_data(target_date=target_date)

    # Parse EventDate back to datetime — warehouse stores ISO strings (see
    # modules/warehouse.py header) and find_first_paid_events uses .dt accessors.
    if not booking_df.empty:
        booking_df['EventDate'] = pd.to_datetime(booking_df['EventDate'], errors='coerce')
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'], errors='coerce')
        # Match the str-typed AccountId convention the rest of the script uses.
        booking_df['AccountId'] = booking_df['AccountId'].fillna(0).astype(int).astype(str)

    print(f"  Bookings for claimed accounts: {len(booking_df):,}")

    if booking_df.empty:
        print("\nNo booking data for claimed accounts. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, user_emails)
        sys.exit(0)

    # Phase 3: Find qualifying events
    print("\n" + "=" * 60)
    print("Phase 3: Finding qualifying events")
    print("=" * 60)

    qualifying_events = find_first_paid_events(booking_df, report_start, report_end)

    if qualifying_events.empty:
        print("\nNo qualifying events found. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, user_emails)
        sys.exit(0)

    # Phase 4: Enrich with account and sales person data
    print("\n" + "=" * 60)
    print("Phase 4: Enriching data and calculating commissions")
    print("=" * 60)

    # Convert AccountId to clean string for joining (handle float -> int -> str)
    qualifying_events['AccountId'] = qualifying_events['AccountId'].fillna(0).astype(int).astype(str)

    # Merge with account data for gateway type and creation date
    accounts_df['AccountId'] = accounts_df['AccountId'].fillna(0).astype(int).astype(str)
    qualifying_events = qualifying_events.merge(
        accounts_df[['AccountId', 'AccountName', 'DateTimeCreated', 'GatewayGroup']],
        on='AccountId',
        how='left'
    )

    # Merge with claimed accounts data for sales person info
    qualifying_events = qualifying_events.merge(
        claimed_accounts[['account_id', 'claimed_user_id', 'claimed_user_name']],
        left_on='AccountId',
        right_on='account_id',
        how='inner'
    )

    # Drop duplicate account_id column from claimed_accounts (keep AccountId)
    qualifying_events = qualifying_events.drop(columns=['account_id'])

    print(f"  Qualifying accounts after joining: {len(qualifying_events)}")

    if qualifying_events.empty:
        print("\nNo accounts match after joining with Zoho data. Sending empty report.")
        send_reports(pd.DataFrame(), period_description, user_emails)
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
        'PaymentReceived': 'ticket_sales',
        'TotalFees': 'total_fees',
        'sales_person_name': 'team_member_name'
    })

    # Format dates (UK format: DD/MM/YYYY)
    if 'account_created_date' in report_df.columns and report_df['account_created_date'].notna().any():
        report_df['account_created_date'] = pd.to_datetime(report_df['account_created_date']).dt.strftime('%d/%m/%Y')
    if 'event_completed_date' in report_df.columns and report_df['event_completed_date'].notna().any():
        report_df['event_completed_date'] = pd.to_datetime(report_df['event_completed_date']).dt.strftime('%d/%m/%Y')

    # Map gateway types to user-friendly names
    gateway_display_names = {
        'Stripe Connect': 'Stripe',
        'Default (All)': 'TryBooking'
    }
    if 'gateway_type' in report_df.columns:
        report_df['gateway_type'] = report_df['gateway_type'].map(
            lambda x: gateway_display_names.get(x, x) if pd.notna(x) else 'TryBooking'
        )

    # Select and order columns
    final_columns = [
        'team_member_name', 'account_id', 'account_name', 'account_created_date',
        'event_completed_date', 'event_name', 'event_id', 'gateway_type',
        'ticket_sales', 'total_fees', 'flat_fee', 'commission_on_sales', 'total_commission', 'claimed_user_id'
    ]
    report_df = report_df[[col for col in final_columns if col in report_df.columns]]

    # Print summary
    print(f"\n  Total qualifying accounts: {len(report_df)}")
    print(f"  Total commission payable: £{report_df['total_commission'].sum():,.2f}")

    # Phase 5: Send reports
    print("\n" + "=" * 60)
    print("Phase 5: Sending reports")
    print("=" * 60)

    send_reports(report_df, period_description, user_emails)

    print("\n" + "=" * 60)
    print("Sales Commission Report Complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
