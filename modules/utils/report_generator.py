"""
Report generation and email functionality for TryBooking tier system.
"""
import pandas as pd
import logging
from datetime import datetime
from .config import TEST_MODE, DEFAULT_RECIPIENT, CC_RECIPIENT, UK_TZ
from .email_utils import send_html_email

logger = logging.getLogger(__name__)


def generate_upcoming_annual_events_report(results_df):
    """Generate report for annual events needing outreach."""
    import time
    
    logger.info(f"Generating upcoming annual events report from {len(results_df):,} accounts")
    start_time = time.time()
    
    # Filter for annual pattern accounts that are Tier 3 or higher
    tier_3_plus = ['Key Account', 'High Value', 'Tier 4', 'Tier 3']
    annual_accounts = results_df[
        ((results_df['Event_Frequency_Current'] == 'Annual') | 
         (results_df['Event_Frequency_Previous'] == 'Annual')) &
        (results_df['Current_Tier'].isin(tier_3_plus))
    ].copy()
    
    logger.info(f"Found {len(annual_accounts):,} annual accounts in Tier 3+")
    
    upcoming = []
    processed = 0
    
    for idx, (_, account) in enumerate(annual_accounts.iterrows()):
        processed += 1
        
        # Log progress every 100 accounts
        if processed % 100 == 0:
            logger.info(f"Processing annual accounts: {processed}/{len(annual_accounts)} ({processed/len(annual_accounts)*100:.1f}%)")
        if pd.notna(account.get('_last_event_date')):
            # Predict next event (365 days from last)
            last_event = pd.to_datetime(account['_last_event_date'])
            predicted_event_date = last_event + pd.Timedelta(days=365)
            
            # Calculate when they'll likely create it
            lead_days = account.get('_avg_lead_days', 60)
            predicted_creation_date = predicted_event_date - pd.Timedelta(days=lead_days)
            
            # We want to reach out 60 days before creation (matching retention priority)
            outreach_date = predicted_creation_date - pd.Timedelta(days=60)
            days_until_outreach = (outreach_date - pd.Timestamp.now()).days
            
            # Include if outreach needed in next 30 days
            if 0 <= days_until_outreach <= 30:
                # Get revenue for context
                last_revenue = account.get('_revenue_prev', 0) if account['Event_Frequency_Previous'] == 'Annual' else account.get('_revenue_current', 0)
                
                upcoming.append({
                    'Account_Name': account['Account_Name'],
                    'Tier': account['Current_Tier'],
                    'Last_Event_Date': last_event.strftime('%d/%m/%Y'),
                    'Expected_Event_Date': predicted_event_date.strftime('%d/%m/%Y'),
                    'Typical_Creation_Lead_Days': lead_days,
                    'Reach_Out_By': outreach_date.strftime('%d/%m/%Y'),
                    'Last_Year_Tickets': int(account['Last_Year_Ticket_Quantity']),
                    'Last_Year_Revenue': f"£{last_revenue:.2f}",
                    'Status': account['Rating']
                })
    
    if upcoming:
        total_time = time.time() - start_time
        logger.info(f"Identified {len(upcoming)} accounts requiring outreach within 30 days (processed in {total_time:.1f}s)")
    else:
        logger.info("No annual accounts requiring immediate outreach")
    
    return pd.DataFrame(upcoming).sort_values('Reach_Out_By') if upcoming else pd.DataFrame()


def email_upcoming_events_report(report_df, filename):
    """Email the upcoming annual events report."""
    
    logger.info(f"Preparing upcoming events email with {len(report_df)} accounts")
    
    # Prepare email body
    body_plain = f"""Hi Alex,

Please find attached the upcoming annual events report.

This report identifies annual event organisers who typically create their events soon, 
allowing proactive outreach approximately 1 month before they usually set up their event.

Summary:
- Total accounts requiring outreach: {len(report_df)}
- Outreach needed within 7 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By'], format='%d/%m/%Y') <= pd.Timestamp.now() + pd.Timedelta(days=7)])}
- Outreach needed within 14 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By'], format='%d/%m/%Y') <= pd.Timestamp.now() + pd.Timedelta(days=14)])}

Best regards,
TryBooking Reporting System
"""
    
    # HTML version of the body
    body_html = f"""<div style="font-family: Arial, sans-serif; font-size: 11pt;">
<p>Hi Alex,</p>
<p>Please find attached the upcoming annual events report.</p>
<p>This report identifies annual event organisers who typically create their events soon, 
allowing proactive outreach approximately 1 month before they usually set up their event.</p>
<p><strong>Summary:</strong></p>
<ul>
<li>Total accounts requiring outreach: {len(report_df)}</li>
<li>Outreach needed within 7 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By'], format='%d/%m/%Y') <= pd.Timestamp.now() + pd.Timedelta(days=7)])}</li>
<li>Outreach needed within 14 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By'], format='%d/%m/%Y') <= pd.Timestamp.now() + pd.Timedelta(days=14)])}</li>
</ul>
<p>Best regards,<br>TryBooking Reporting System</p>
</div>"""
    
    with open(filename, 'rb') as f:
        csv_data = f.read()

    send_html_email(
        to=DEFAULT_RECIPIENT,
        cc=CC_RECIPIENT if (CC_RECIPIENT and not TEST_MODE) else None,
        subject=f'Upcoming Annual Events - {datetime.now(UK_TZ).strftime("%B %Y")}',
        html_content=body_html,
        plain_text=body_plain,
        attachments=[(filename, csv_data, 'text', 'csv')],
    )

    recipients = DEFAULT_RECIPIENT if TEST_MODE else f"{DEFAULT_RECIPIENT}, {CC_RECIPIENT}"
    logger.info(f"Email sent to {recipients} with {len(report_df)} upcoming annual events")
    print(f"Email sent to {recipients} with {len(report_df)} upcoming annual events")


def email_tier_updates_report(updates_df, csv_filename):
    """Email the tier updates report with retention priority summary."""
    import time
    
    logger.info(f"Preparing tier updates email report for {len(updates_df):,} accounts")
    start_time = time.time()
    
    # Get summary statistics
    logger.info("Calculating tier distribution...")
    tier_counts = updates_df['Current_Tier'].value_counts()
    
    logger.info("Identifying tier changes...")
    tier_changes = updates_df[updates_df['Current_Tier'] != updates_df['Previous_Tier']]
    
    logger.info("Calculating retention priority distribution...")
    priority_counts = updates_df['Retention_Priority'].value_counts()
    
    logger.info(f"Found {len(tier_changes):,} tier changes")
    
    # Log tier change details
    if len(tier_changes) > 0:
        # Count upgrades vs downgrades
        tier_order = ['NIL', 'Tier 1', 'Tier 2', 'Tier 3', 'Tier 4', 'High Value', 'Key Account']
        upgrades = 0
        downgrades = 0
        
        for _, change in tier_changes.iterrows():
            prev_idx = tier_order.index(change['Previous_Tier']) if change['Previous_Tier'] in tier_order else -1
            curr_idx = tier_order.index(change['Current_Tier']) if change['Current_Tier'] in tier_order else -1
            
            if curr_idx > prev_idx:
                upgrades += 1
            elif curr_idx < prev_idx:
                downgrades += 1
        
        logger.info(f"  Upgrades: {upgrades:,} accounts")
        logger.info(f"  Downgrades: {downgrades:,} accounts")
    
    # Get top priority accounts (excluding inactive/churned accounts)
    # Excluded ratings: Churned, Suspended or Closed, Unactivated, Never Logged In, Never Transacted
    excluded_ratings = ['Churned', 'Suspended or Closed', 'Unactivated', 'Never Logged In', 'Never Transacted']
    logger.info("Identifying high-priority accounts...")
    very_high_accounts = updates_df[
        (updates_df['Retention_Priority'] == 'Very High') &
        (~updates_df['Rating'].isin(excluded_ratings))
    ].nlargest(10, '_retention_priority_score')
    high_accounts = updates_df[
        (updates_df['Retention_Priority'] == 'High') &
        (~updates_df['Rating'].isin(excluded_ratings))
    ].nlargest(10, '_retention_priority_score')

    # Count excluded accounts (they have empty retention priority)
    churned_count = len(updates_df[updates_df['Rating'].isin(excluded_ratings)])
    
    action_required = priority_counts.get('Very High', 0) + priority_counts.get('High', 0)
    logger.info(f"Action required for {action_required:,} accounts (Very High + High priority)")
    
    prep_time = time.time() - start_time
    logger.info(f"Report preparation complete in {prep_time:.1f}s")
    
    # Create HTML table for very high priority accounts
    very_high_table_html = ""
    if not very_high_accounts.empty:
        very_high_table_html = """
<table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; font-family: Arial, sans-serif; font-size: 10pt;">
<tr style="background-color: #f0f0f0;">
<th>Account Name</th>
<th>Tier</th>
<th>Activity Rating</th>
<th>Priority Score</th>
<th>Revenue Drop</th>
<th>Current Revenue</th>
</tr>"""
        for _, account in very_high_accounts.iterrows():
            revenue_drop = account.get('_revenue_drop_category', 'N/A')
            current_revenue = f"£{account.get('_revenue_current', 0):,.2f}"
            very_high_table_html += f"""
<tr>
<td>{account['Account_Name']}</td>
<td>{account['Current_Tier']}</td>
<td>{account['Rating']}</td>
<td>{account['_retention_priority_score']}</td>
<td>{revenue_drop}</td>
<td>{current_revenue}</td>
</tr>"""
        very_high_table_html += "</table>"
    
    # Prepare email body
    body_plain = f"""Hi Alex,

Please find attached the tier updates report for {datetime.now(UK_TZ).strftime('%B %Y')}.

SUMMARY
=======
Total accounts processed: {len(updates_df):,}
Tier changes: {len(tier_changes):,} accounts

TIER DISTRIBUTION
================"""
    
    for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
        count = tier_counts.get(tier, 0)
        pct = (count / len(updates_df) * 100) if len(updates_df) > 0 else 0
        body_plain += f"\n{tier}: {count:,} ({pct:.1f}%)"
    
    body_plain += f"""

RETENTION PRIORITY DISTRIBUTION
==============================
Very High: {priority_counts.get('Very High', 0):,} accounts
High: {priority_counts.get('High', 0):,} accounts
Medium: {priority_counts.get('Medium', 0):,} accounts
Low: {priority_counts.get('Low', 0):,} accounts

Excluded (No Priority): {churned_count:,} accounts - Churned/Suspended/Unactivated/Never Transacted

Action required for {priority_counts.get('Very High', 0) + priority_counts.get('High', 0):,} accounts (Very High + High priority)

Note: Excluded accounts (Churned, Suspended, Unactivated, Never Logged In, Never Transacted) are excluded from CS workflows

Best regards,
TryBooking Reporting System
"""
    
    # HTML version
    body_html = f"""<div style="font-family: Arial, sans-serif; font-size: 11pt;">
<p>Hi Alex,</p>
<p>Please find attached the tier updates report for {datetime.now(UK_TZ).strftime('%B %Y')}.</p>

<h3>Summary</h3>
<ul>
<li>Total accounts processed: <strong>{len(updates_df):,}</strong></li>
<li>Tier changes: <strong>{len(tier_changes):,}</strong> accounts</li>
</ul>

<h3>Tier Distribution</h3>
<table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
<tr style="background-color: #f0f0f0;">
<th>Tier</th>
<th>Count</th>
<th>Percentage</th>
</tr>"""
    
    for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
        count = tier_counts.get(tier, 0)
        pct = (count / len(updates_df) * 100) if len(updates_df) > 0 else 0
        body_html += f"""
<tr>
<td>{tier}</td>
<td>{count:,}</td>
<td>{pct:.1f}%</td>
</tr>"""
    
    body_html += f"""</table>

<h3>Retention Priority Distribution</h3>
<table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
<tr style="background-color: #f0f0f0;">
<th>Priority</th>
<th>Count</th>
<th>Action</th>
</tr>
<tr>
<td><span style="color: #d32f2f; font-weight: bold;">Very High</span></td>
<td>{priority_counts.get('Very High', 0):,}</td>
<td>Immediate intervention required</td>
</tr>
<tr>
<td><span style="color: #f57c00; font-weight: bold;">High</span></td>
<td>{priority_counts.get('High', 0):,}</td>
<td>Urgent outreach needed</td>
</tr>
<tr>
<td><span style="color: #388e3c;">Medium</span></td>
<td>{priority_counts.get('Medium', 0):,}</td>
<td>Standard monitoring</td>
</tr>
<tr>
<td><span style="color: #757575;">Low</span></td>
<td>{priority_counts.get('Low', 0):,}</td>
<td>Regular communications</td>
</tr>
</table>

<table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; margin-top: 10px;">
<tr style="background-color: #f0f0f0;">
<th>Excluded Accounts</th>
<th>Count</th>
<th>Note</th>
</tr>
<tr>
<td><span style="color: #9e9e9e;">Excluded (No Priority)</span></td>
<td>{churned_count:,}</td>
<td>Churned/Suspended/Unactivated/Never Transacted - Excluded from CS workflows</td>
</tr>
</table>

<p style="margin-top: 10px;"><em>Total Excluded Accounts: {churned_count:,}</em></p>

<p><strong>Action required for {priority_counts.get('Very High', 0) + priority_counts.get('High', 0):,} accounts</strong> (Very High + High priority)</p>
<p style="font-size: 10pt; color: #666;">Note: Excluded accounts (Churned, Suspended, Unactivated, Never Logged In, Never Transacted) are excluded from standard CS workflows.</p>
"""
    
    if very_high_table_html:
        body_html += f"""
<h3>Top Very High Priority Accounts</h3>
<p>These accounts require immediate attention (excluded accounts omitted):</p>
{very_high_table_html}
"""
    
    body_html += """
<p>Best regards,<br>TryBooking Reporting System</p>
</div>"""
    
    with open(csv_filename, 'rb') as f:
        csv_data = f.read()

    send_html_email(
        to=DEFAULT_RECIPIENT,
        cc=CC_RECIPIENT if (CC_RECIPIENT and not TEST_MODE) else None,
        subject=f'Tier Updates & Retention Priorities - {datetime.now(UK_TZ).strftime("%B %Y")}',
        html_content=body_html,
        plain_text=body_plain,
        attachments=[(csv_filename, csv_data, 'text', 'csv')],
    )

    recipients = DEFAULT_RECIPIENT if TEST_MODE else f"{DEFAULT_RECIPIENT}, {CC_RECIPIENT}"
    logger.info(f"Email sent to {recipients} with tier updates and retention priorities")
    print(f"Email sent to {recipients} with tier updates and retention priorities")