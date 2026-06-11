#!/usr/bin/env python3
"""
Weekly reporting script for TryBooking UK.
Analyzes new accounts created in the past week and optionally sends email report.
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime

# Import shared modules
from modules.utils.config import TEST_MODE, UK_TZ, get_recipients
from modules.utils.data_loader import get_s3_client
from modules.utils.date_utils import get_week_dates, get_latest_data_date
from modules.utils.data_loader import load_accounts_data
from modules.utils.email_utils import send_html_email
from modules.utils.metrics_calculator import calculate_percentage, aggregate_by_day_of_week, filter_date_range, calculate_yoy_change
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator




@timer_decorator
def analyze_accounts(df, week_start, week_end, last_year_week_start, last_year_week_end):
    """Analyze account data for current and previous year."""
    # Filter data using optimized function
    current_week = filter_date_range(df, 'DateTimeCreated', week_start, week_end)
    last_year_week = filter_date_range(df, 'DateTimeCreated', last_year_week_start, last_year_week_end)
    
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
        
        # Convert DateTimeCreated to Europe/London timezone
        # It comes from load_accounts_data as UTC timezone-aware
        if with_events['DateTimeCreated'].dt.tz is None:
            # Timezone-naive, assume UTC and convert
            with_events['DateTimeCreated'] = pd.to_datetime(with_events['DateTimeCreated'], errors='coerce', utc=True).dt.tz_convert('Europe/London')
        else:
            # Already has timezone, just convert to London
            with_events['DateTimeCreated'] = with_events['DateTimeCreated'].dt.tz_convert('Europe/London')
        
        # Convert FirstEventCreation to Europe/London timezone
        # Parse FirstEventCreation - it might be string or already datetime
        first_event_parsed = pd.to_datetime(with_events['FirstEventCreation'], errors='coerce', utc=True)
        # Only convert non-null values to avoid issues with NaT
        with_events['FirstEventCreation'] = first_event_parsed.dt.tz_convert('Europe/London')
        
        # Filter comparisons
        week_1 = with_events[with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=1)]
        week_2 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=1)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=2))]
        week_3 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=2)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=3))]
        week_4 = with_events[(with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=3)) & 
                             (with_events['FirstEventCreation'] <= with_events['DateTimeCreated'] + pd.Timedelta(weeks=4))]
        more_than_month = with_events[with_events['FirstEventCreation'] > with_events['DateTimeCreated'] + pd.Timedelta(weeks=4)]
        
        # Days to create stats - calculate only for non-null FirstEventCreation
        valid_events = with_events['FirstEventCreation'].notna()
        with_events.loc[valid_events, 'DaysToCreate'] = (
            with_events.loc[valid_events, 'FirstEventCreation'] - 
            with_events.loc[valid_events, 'DateTimeCreated']
        ).dt.days
        # Fill NaN for rows where FirstEventCreation is null
        with_events['DaysToCreate'] = with_events['DaysToCreate'].fillna(pd.NA)
    else:
        week_1 = week_2 = week_3 = week_4 = more_than_month = pd.DataFrame()
        with_events = pd.DataFrame()
    
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
        'avg_days': with_events['DaysToCreate'].mean() if not with_events.empty and 'DaysToCreate' in with_events.columns else 0,
        'median_days': with_events['DaysToCreate'].median() if not with_events.empty and 'DaysToCreate' in with_events.columns else 0,
        'current_week': current_week  # Add this for email generation
    }


def create_internal_email_content(stats, df_current):
    """Create internal email content (Email A from original)."""
    # Calculate industry stats
    total_accounts = stats['total_accounts']
    total_accounts_ly = stats['total_accounts_ly']
    yoy_change = calculate_yoy_change(total_accounts, total_accounts_ly)
    without_industry_pct = calculate_percentage(df_current['Industry'].isna().sum(), total_accounts)
    
    # Industry analysis
    ticket_purchasers = df_current['Industry'].eq("Ticket Purchaser").sum()
    ticket_purchaser_pct = calculate_percentage(ticket_purchasers, total_accounts)
    
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
    ticket_purchaser_pct = calculate_percentage(ticket_purchasers, total_accounts)
    
    filtered_industries = df_current['Industry'][
        df_current['Industry'].notna() & (df_current['Industry'] != "Ticket Purchaser")
    ]
    top_3_counts = filtered_industries.value_counts().head(3)
    top_3_named = [f"{industry} ({(count / total_accounts) * 100:.0f}%)" for industry, count in top_3_counts.items()]
    
    # Event creation stats
    with_events_count = len(stats['with_events'])
    with_events_pct = calculate_percentage(with_events_count, total_accounts)
    
    # Daily breakdown - ensure DateTimeCreated is in London timezone for correct time classification
    df_for_daily = df_current.copy()
    if df_for_daily['DateTimeCreated'].dt.tz is None:
        df_for_daily['DateTimeCreated'] = df_for_daily['DateTimeCreated'].dt.tz_localize('UTC').dt.tz_convert('Europe/London')
    else:
        df_for_daily['DateTimeCreated'] = df_for_daily['DateTimeCreated'].dt.tz_convert('Europe/London')
    
    def classify_time(dt):
        return 'Day' if (dt.hour == 17 and dt.minute < 30) or (9 <= dt.hour < 17) else 'Evening'
    
    daily = (
        df_for_daily
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





def main(send_email_report=True):
    """Main execution function."""
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    print(f"\n=== Weekly Reporting Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Email sending: {'ENABLED' if send_email_report else 'DISABLED'}")
    if send_email_report and TEST_MODE:
        print("TEST MODE: Email will be sent to henry@trybooking.co.uk only")
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Calculate time windows
        dates = get_week_dates(weeks_back=1)
        week_start = dates['week_start']
        week_end = dates['week_end']
        last_year_week_start = dates['last_year_week_start']
        last_year_week_end = dates['last_year_week_end']
        
        print(f"\nReporting week: {week_start.strftime('%d %B %Y')} to {week_end.strftime('%d %B %Y')}")
        print(f"Last year comparison: {last_year_week_start.strftime('%d %B %Y')} to {last_year_week_end.strftime('%d %B %Y')}")
        
        # Fetch and process data
        df = load_accounts_data(s3_client, get_latest_data_date())
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
            # Recipients are managed in modules/utils/config.py → DISTRIBUTION_LISTS["weekly_new_accounts"]
            to_recipients, cc_recipients = get_recipients("weekly_new_accounts")
            send_html_email(
                to=to_recipients,
                cc=cc_recipients,
                subject=f"New Accounts w/c {stats['week_start'].strftime('%d %B %Y')}",
                html_content=internal_html
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
