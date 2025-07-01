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
        # Use .loc to avoid SettingWithCopyWarning
        with_events = with_events.copy()
        with_events.loc[:, 'FirstEventCreation'] = pd.to_datetime(with_events['FirstEventCreation'], errors='coerce', utc=True).dt.tz_convert('Europe/London')
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
        'median_days': with_events['DaysToCreate'].median() if not with_events.empty else 0,
        'current_week': current_week  # Add this for email generation
    }


def create_internal_email_content(stats, df_current):
    """Create internal email content (Email A from original)."""
    # Calculate industry stats
    total_accounts = stats['total_accounts']
    total_accounts_ly = stats['total_accounts_ly']
    yoy_change = ((total_accounts - total_accounts_ly) / total_accounts_ly * 100) if total_accounts_ly else 0
    without_industry_pct = 100 * df_current['Industry'].isna().sum() / total_accounts if total_accounts else 0
    
    # Industry analysis
    ticket_purchasers = df_current['Industry'].eq("Ticket Purchaser").sum()
    ticket_purchaser_pct = 100 * ticket_purchasers / total_accounts if total_accounts else 0
    
    filtered_industries = df_current['Industry'][
        df_current['Industry'].notna() & (df_current['Industry'] != "Ticket Purchaser")
    ]
    top_5_industries = filtered_industries.value_counts().head(5)
    
    industry_lines = ''.join(
        f"<li>{industry}: {count} ({(count / total_accounts) * 100:.0f}%)</li>"
        for industry, count in top_5_industries.items()
    )
    
    # Event creation stats
    with_events_count = len(stats['with_events'])
    without_events_count = len(stats['without_events'])
    with_events_pct = (with_events_count / total_accounts * 100) if total_accounts else 0
    
    html_content = f"""
    <div style="font-family: Arial, sans-serif; font-size: 11pt;">
      <p>Total accounts last week: {total_accounts}</p>
      <p>Percentage YoY change compared to the same week last year: {yoy_change:.0f}%</p>
      <p>Accounts with events: {with_events_count} ({with_events_pct:.0f}%)</p>
      <p>Accounts without events: {without_events_count} ({100 - with_events_pct:.0f}%)</p>
      <p>Average days to create first event: {stats['avg_days']:.1f} days</p>
      <br>
      <p>Ticket Purchasers: {ticket_purchasers} ({ticket_purchaser_pct:.0f}%)</p>
      <p>Top 5 industries (excluding Ticket Purchasers):</p>
      <ul>{industry_lines}</ul>
      <p>% of accounts without an industry assigned: {without_industry_pct:.0f}%</p>
    </div>
    """
    
    return html_content


def create_external_email_content(stats, df_current):
    """Create external email content (Email B from original)."""
    # Calculate stats
    total_accounts = stats['total_accounts']
    total_accounts_ly = stats['total_accounts_ly']
    yoy_change = ((total_accounts - total_accounts_ly) / total_accounts_ly * 100) if total_accounts_ly else 0
    
    # Industry analysis
    ticket_purchasers = df_current['Industry'].eq("Ticket Purchaser").sum()
    ticket_purchaser_pct = 100 * ticket_purchasers / total_accounts if total_accounts else 0
    
    filtered_industries = df_current['Industry'][
        df_current['Industry'].notna() & (df_current['Industry'] != "Ticket Purchaser")
    ]
    top_3_counts = filtered_industries.value_counts().head(3)
    top_3_named = [f"{industry} ({(count / total_accounts) * 100:.0f}%)" for industry, count in top_3_counts.items()]
    
    # Event creation stats
    with_events_count = len(stats['with_events'])
    with_events_pct = (with_events_count / total_accounts * 100) if total_accounts else 0
    
    # Daily breakdown
    def classify_time(dt):
        return 'Day' if (dt.hour == 17 and dt.minute < 30) or (9 <= dt.hour < 17) else 'Evening'
    
    daily = (
        df_current
        .assign(DayName=lambda df: df['DateTimeCreated'].dt.strftime('%A'))
        .assign(TimeCategory=lambda df: df['DateTimeCreated'].apply(classify_time))
        .groupby(['DayName', 'TimeCategory'])
        .size()
        .unstack(fill_value=0)
    )
    
    daily = daily.reindex(columns=['Day', 'Evening'], fill_value=0)
    daily['Total'] = daily.sum(axis=1)
    daily = daily.reset_index()
    
    weekday_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    daily['DayName'] = pd.Categorical(daily['DayName'], categories=weekday_order, ordered=True)
    daily = daily.sort_values('DayName')
    
    html_table_rows = ''.join(
        f"<tr><td>{row['DayName']}</td><td>{row['Total']}</td><td>{row.get('Day', 0)}</td><td>{row.get('Evening', 0)}</td></tr>"
        for _, row in daily.iterrows()
    )
    
    html_content = f"""
    <div style="font-family: Arial, sans-serif; font-size: 11pt;">
      <p>Hi Gareth and Abi,</p>
      <p>Please find actual new account numbers below for the week commencing {stats['week_start'].strftime('%d %B %Y')}.</p>
      <p>Percentage change YoY compared to last year: {yoy_change:.0f}%<br>
         % of accounts who are ticket purchasers: {ticket_purchaser_pct:.0f}%<br>
         % of accounts who created events: {with_events_pct:.0f}%<br>
         Average days to first event: {stats['avg_days']:.1f} days</p>
      <p>Top 3 industries (excluding Ticket Purchasers):</p>
      <ul>{''.join(f'<li>{entry}</li>' for entry in top_3_named)}</ul>
      <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
        <thead>
          <tr><th>Day</th><th>Total</th><th>Day (0900-1730)</th><th>Evening</th></tr>
        </thead>
        <tbody>{html_table_rows}</tbody>
      </table>
      <p>Do hope this helps.</p>
      <p>Kindest regards,</p>
    </div>
    """
    
    return html_content


def send_email(to, cc, subject, html_body):
    """Send email via Mailgun SMTP."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    msg['To'] = to
    if cc:
        msg['Cc'] = cc
    msg.set_content("This is an HTML report. Please view it in an HTML-compatible client.")
    msg.add_alternative(html_body, subtype='html')
    
    recipients = to
    if cc:
        recipients += f", {cc}"
    print(f"\nSending email to: {recipients}")
    
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        smtp.send_message(msg)
    print("Email sent successfully!")



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
        
        # Send emails if requested
        if send_email_report:
            # Email A - Internal
            internal_html = create_internal_email_content(stats, stats['current_week'])
            send_email(
                to="alex@trybooking.co.uk" if TEST_MODE else "jules@trybooking.co.uk",
                cc="alex@trybooking.co.uk" if TEST_MODE else "alex@trybooking.co.uk, louise@trybooking.co.uk",
                subject=f"{'[TEST] ' if TEST_MODE else ''}New Accounts w/c {stats['week_start'].strftime('%d %B %Y')}",
                html_body=internal_html
            )
            
            # Email B - External
            external_html = create_external_email_content(stats, stats['current_week'])
            send_email(
                to="alex@trybooking.co.uk" if TEST_MODE else "gareth@dgtlonline.co.uk, clients@dgtlonline.co.uk",
                cc="alex@trybooking.co.uk" if TEST_MODE else "alex@trybooking.co.uk, joan@trybooking.co.uk",
                subject=f"{'[TEST] ' if TEST_MODE else ''}TryBooking New Accounts w/c {stats['week_start'].strftime('%d %B %Y')}",
                html_body=external_html
            )
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
    send_email_flag = '--no-email' not in sys.argv
    
    # Also check environment variable
    if os.environ.get('SEND_EMAIL', '').lower() in ['0', 'false', 'no']:
        send_email_flag = False
    
    main(send_email_report=send_email_flag)