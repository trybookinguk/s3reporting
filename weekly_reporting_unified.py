#!/usr/bin/env python3
"""
Weekly reporting script for TryBooking UK.
Analyzes new accounts created in the past week and optionally sends email report.
"""
import os
import sys
import time
import boto3
import pandas as pd
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta

# Import shared modules
from modules.utils.config import (
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET,
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN,
    SMTP_HOST, SMTP_PORT, TEST_MODE, UK_TZ
)
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached


def calculate_time_windows():
    """Calculate reporting time windows for current and last year."""
    today = datetime.today()
    last_week_date = today - timedelta(days=today.weekday() + 7)
    week_start = pd.Timestamp(last_week_date.date(), tz='Europe/London')
    week_end = week_start + pd.Timedelta(days=6, hours=23, minutes=59, seconds=59)
    
    # Calculate ISO week info for last year comparison
    iso_year, iso_week, _ = last_week_date.isocalendar()
    last_year_week_start = datetime.strptime(f'{iso_year - 1}-W{iso_week}-1', '%G-W%V-%u')
    last_year_week_end = last_year_week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    last_year_week_start = pd.Timestamp(last_year_week_start, tz='Europe/London')
    last_year_week_end = pd.Timestamp(last_year_week_end, tz='Europe/London')
    
    return week_start, week_end, last_year_week_start, last_year_week_end


def fetch_and_process_data(s3_client):
    """Fetch account data from S3 and process timestamps."""
    # Use yesterday's date for file location
    yesterday = datetime.today() - timedelta(days=1)
    folder_year = yesterday.strftime('%Y')
    folder_month = yesterday.strftime('%m')
    file_prefix = yesterday.strftime('%Y%m')
    filename = f"{file_prefix}-Accounts-TBUK.csv"
    s3_key = f"{folder_year}/{folder_month}/{filename}"
    
    print(f"Fetching data from S3: {s3_key}")
    df = download_s3_file_cached(s3_client, s3_key)
    
    # Convert datetime columns
    df['DateTimeCreated'] = pd.to_datetime(df['DateTimeCreated'], errors='coerce', utc=True).dt.tz_convert('Europe/London')
    
    # Convert FirstEventCreation to datetime, handling empty values
    df['FirstEventCreation'] = pd.to_datetime(df['FirstEventCreation'], errors='coerce', utc=True)
    df.loc[df['FirstEventCreation'].notna(), 'FirstEventCreation'] = df.loc[df['FirstEventCreation'].notna(), 'FirstEventCreation'].dt.tz_convert('Europe/London')
    
    return df


def analyze_accounts(df, week_start, week_end, last_year_week_start, last_year_week_end):
    """Analyze account data for current and previous year."""
    # Filter data
    current_week = df[(df['DateTimeCreated'] >= week_start) & (df['DateTimeCreated'] <= week_end)].copy()
    last_year_week = df[(df['DateTimeCreated'] >= last_year_week_start) & (df['DateTimeCreated'] <= last_year_week_end)].copy()
    
    # Basic stats
    total_accounts = len(current_week)
    total_accounts_ly = len(last_year_week)
    
    # Determine if accounts have events based on FirstEventCreation
    current_week['HasEvents'] = current_week['FirstEventCreation'].notna()
    last_year_week['HasEvents'] = last_year_week['FirstEventCreation'].notna()
    
    # Accounts with events
    with_events = current_week[current_week['HasEvents']]
    without_events = current_week[~current_week['HasEvents']]
    ly_with_events = last_year_week[last_year_week['HasEvents']]
    
    # Event creation analysis - only for accounts that have events
    if not with_events.empty:
        with_events['FirstEventCreation'] = pd.to_datetime(with_events['FirstEventCreation'], errors='coerce', utc=True).dt.tz_convert('Europe/London')
        week_1 = with_events[with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=1)]
        week_2 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=1)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=2))]
        week_3 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=2)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=3))]
        week_4 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=3)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=4))]
        more_than_month = with_events[with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=4)]
    else:
        week_1 = week_2 = week_3 = week_4 = more_than_month = pd.DataFrame()
    
    # Days to create stats
    if not with_events.empty:
        with_events = with_events.copy()
        with_events['DaysToCreate'] = (with_events['FirstEventCreation'] - with_events['DateTimeCreated']).dt.days
    else:
        with_events['DaysToCreate'] = pd.Series(dtype='float64')
    
    return {
        'week_start': week_start,
        'week_end': week_end,
        'total_accounts': total_accounts,
        'total_accounts_ly': total_accounts_ly,
        'with_events': with_events,
        'without_events': without_events,
        'ly_with_events': ly_with_events,
        'week_1': week_1,
        'week_2': week_2,
        'week_3': week_3,
        'week_4': week_4,
        'more_than_month': more_than_month,
        'avg_days': with_events['DaysToCreate'].mean() if not with_events.empty else 0,
        'median_days': with_events['DaysToCreate'].median() if not with_events.empty else 0
    }


def create_email_content(stats):
    """Create HTML and plain text email content."""
    # HTML Email
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>TryBooking UK - Weekly New Accounts Report</h2>
        <p>Week: {stats['week_start'].strftime('%d %B %Y')} to {stats['week_end'].strftime('%d %B %Y')}</p>
        
        <h3>Summary</h3>
        <table border="1" cellpadding="5" cellspacing="0">
            <tr><th>Metric</th><th>This Year</th><th>Last Year</th><th>Change</th></tr>
            <tr>
                <td>Total New Accounts</td>
                <td>{stats['total_accounts']}</td>
                <td>{stats['total_accounts_ly']}</td>
                <td>{'+' if stats['total_accounts'] > stats['total_accounts_ly'] else ''}{stats['total_accounts'] - stats['total_accounts_ly']} ({((stats['total_accounts'] / stats['total_accounts_ly'] - 1) * 100):.1f}%)</td>
            </tr>
            <tr>
                <td>Accounts with Events</td>
                <td>{len(stats['with_events'])} ({(len(stats['with_events']) / stats['total_accounts'] * 100):.1f}%)</td>
                <td>{len(stats['ly_with_events'])} ({(len(stats['ly_with_events']) / stats['total_accounts_ly'] * 100):.1f}%)</td>
                <td>{'+' if len(stats['with_events']) > len(stats['ly_with_events']) else ''}{len(stats['with_events']) - len(stats['ly_with_events'])}</td>
            </tr>
            <tr>
                <td>Accounts without Events</td>
                <td>{len(stats['without_events'])} ({(len(stats['without_events']) / stats['total_accounts'] * 100):.1f}%)</td>
                <td>{stats['total_accounts_ly'] - len(stats['ly_with_events'])} ({((stats['total_accounts_ly'] - len(stats['ly_with_events'])) / stats['total_accounts_ly'] * 100):.1f}%)</td>
                <td>{'+' if len(stats['without_events']) > (stats['total_accounts_ly'] - len(stats['ly_with_events'])) else ''}{len(stats['without_events']) - (stats['total_accounts_ly'] - len(stats['ly_with_events']))}</td>
            </tr>
        </table>
        
        <h3>Time to First Event Creation</h3>
        <table border="1" cellpadding="5" cellspacing="0">
            <tr><th>Period</th><th>Count</th><th>Percentage</th></tr>
            <tr><td>Within 1 week</td><td>{len(stats['week_1'])}</td><td>{(len(stats['week_1']) / len(stats['with_events']) * 100) if len(stats['with_events']) > 0 else 0:.1f}%</td></tr>
            <tr><td>Week 2</td><td>{len(stats['week_2'])}</td><td>{(len(stats['week_2']) / len(stats['with_events']) * 100) if len(stats['with_events']) > 0 else 0:.1f}%</td></tr>
            <tr><td>Week 3</td><td>{len(stats['week_3'])}</td><td>{(len(stats['week_3']) / len(stats['with_events']) * 100) if len(stats['with_events']) > 0 else 0:.1f}%</td></tr>
            <tr><td>Week 4</td><td>{len(stats['week_4'])}</td><td>{(len(stats['week_4']) / len(stats['with_events']) * 100) if len(stats['with_events']) > 0 else 0:.1f}%</td></tr>
            <tr><td>More than 1 month</td><td>{len(stats['more_than_month'])}</td><td>{(len(stats['more_than_month']) / len(stats['with_events']) * 100) if len(stats['with_events']) > 0 else 0:.1f}%</td></tr>
        </table>
        
        <p><strong>Average days to create first event:</strong> {stats['avg_days']:.1f} days</p>
        <p><strong>Median days to create first event:</strong> {stats['median_days']:.1f} days</p>
        
        <br>
        <p style="color: #666; font-size: 12px;">This is an automated report generated by TryBooking UK reporting system.</p>
    </body>
    </html>
    """
    
    # Plain text version
    plain_text = f"""
TryBooking UK - Weekly New Accounts Report
Week: {stats['week_start'].strftime('%d %B %Y')} to {stats['week_end'].strftime('%d %B %Y')}

Summary:
- Total New Accounts: {stats['total_accounts']} (Last Year: {stats['total_accounts_ly']}, Change: {stats['total_accounts'] - stats['total_accounts_ly']})
- With Events: {len(stats['with_events'])} ({(len(stats['with_events']) / stats['total_accounts'] * 100):.1f}%)
- Without Events: {len(stats['without_events'])} ({(len(stats['without_events']) / stats['total_accounts'] * 100):.1f}%)

Time to First Event:
- Within 1 week: {len(stats['week_1'])} ({(len(stats['week_1']) / len(stats['with_events']) * 100):.1f}%)
- Week 2: {len(stats['week_2'])} ({(len(stats['week_2']) / len(stats['with_events']) * 100):.1f}%)
- Week 3: {len(stats['week_3'])} ({(len(stats['week_3']) / len(stats['with_events']) * 100):.1f}%)
- Week 4: {len(stats['week_4'])} ({(len(stats['week_4']) / len(stats['with_events']) * 100):.1f}%)
- More than 1 month: {len(stats['more_than_month'])} ({(len(stats['more_than_month']) / len(stats['with_events']) * 100):.1f}%)

Average days to create first event: {stats['avg_days']:.1f} days
Median days to create first event: {stats['median_days']:.1f} days
"""
    
    return html_content, plain_text


def send_email(html_content, plain_text, stats):
    """Send email report via Mailgun SMTP."""
    msg = EmailMessage()
    msg['From'] = f"TryBooking UK Reports <reports@{MAILGUN_DOMAIN}>"
    
    # Recipients based on TEST_MODE
    if TEST_MODE:
        msg['To'] = 'alex@trybooking.co.uk'
        msg['Subject'] = f'[TEST] Weekly New Accounts Report - {stats["week_start"].strftime("%d %b %Y")}'
    else:
        msg['To'] = 'tbuk@trybooking.com'
        msg['Subject'] = f'Weekly New Accounts Report - {stats["week_start"].strftime("%d %b %Y")}'
    
    msg.set_content(plain_text)
    msg.add_alternative(html_content, subtype='html')
    
    # Send via SMTP
    print(f"\nSending email to: {msg['To']}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        server.send_message(msg)
    print("Email sent successfully!")


def save_csv_reports(stats):
    """Save detailed CSV reports for debugging."""
    # Accounts with events - top 20
    if not stats['with_events'].empty:
        top_accounts = stats['with_events'].nlargest(20, 'DaysToCreate')[
            ['Id', 'AccountName', 'DateTimeCreated', 'FirstEventCreation', 'DaysToCreate']
        ].copy()
        top_accounts['DateTimeCreated'] = top_accounts['DateTimeCreated'].dt.strftime('%Y-%m-%d %H:%M')
        top_accounts['FirstEventCreation'] = top_accounts['FirstEventCreation'].dt.strftime('%Y-%m-%d %H:%M')
        filename = f"weekly_accounts_with_events_{stats['week_start'].strftime('%Y%m%d')}.csv"
        top_accounts.to_csv(filename, index=False)
        print(f"Saved top 20 accounts to {filename}")
    
    # Accounts without events
    if not stats['without_events'].empty:
        no_events = stats['without_events'][['Id', 'AccountName', 'DateTimeCreated']].copy()
        no_events['DateTimeCreated'] = no_events['DateTimeCreated'].dt.strftime('%Y-%m-%d %H:%M')
        filename = f"weekly_accounts_without_events_{stats['week_start'].strftime('%Y%m%d')}.csv"
        no_events.to_csv(filename, index=False)
        print(f"Saved accounts without events to {filename}")


def main(send_email_report=True):
    """Main execution function."""
    start_time = time.time()
    
    print(f"\n=== Weekly Reporting Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Email sending: {'ENABLED' if send_email_report else 'DISABLED'}")
    if send_email_report and TEST_MODE:
        print("TEST MODE: Email will be sent to alex@trybooking.co.uk only")
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Calculate time windows
        week_start, week_end, last_year_week_start, last_year_week_end = calculate_time_windows()
        print(f"\nReporting week: {week_start.strftime('%d %B %Y')} to {week_end.strftime('%d %B %Y')}")
        print(f"Last year comparison: {last_year_week_start.strftime('%d %B %Y')} to {last_year_week_end.strftime('%d %B %Y')}")
        
        # Fetch and process data
        df = fetch_and_process_data(s3_client)
        print(f"Total accounts in dataset: {len(df):,}")
        
        # Analyze accounts
        stats = analyze_accounts(df, week_start, week_end, last_year_week_start, last_year_week_end)
        
        # Print summary
        print(f"\nSummary for week {week_start.strftime('%d %B')} - {week_end.strftime('%d %B %Y')}:")
        print(f"- Total new accounts: {stats['total_accounts']} (LY: {stats['total_accounts_ly']})")
        print(f"- With events: {len(stats['with_events'])} ({(len(stats['with_events']) / stats['total_accounts'] * 100):.1f}%)")
        print(f"- Without events: {len(stats['without_events'])} ({(len(stats['without_events']) / stats['total_accounts'] * 100):.1f}%)")
        print(f"- Average days to first event: {stats['avg_days']:.1f}")
        
        # Save CSV reports
        save_csv_reports(stats)
        
        # Send email if requested
        if send_email_report:
            html_content, plain_text = create_email_content(stats)
            send_email(html_content, plain_text, stats)
        else:
            print("\nEmail sending disabled - report generation complete")
        
        print(f"\n=== Weekly Reporting Completed in {time.time() - start_time:.1f} seconds ===")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    # Check command line arguments
    send_email = '--no-email' not in sys.argv
    
    # Also check environment variable
    if os.environ.get('SEND_EMAIL', '').lower() in ['0', 'false', 'no']:
        send_email = False
    
    main(send_email_report=send_email)