"""
Report generation and email functionality for TryBooking tier system.
"""
import pandas as pd
import smtplib
from datetime import datetime
from email.message import EmailMessage
from .config import (
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD,
    MAILGUN_DOMAIN, SMTP_HOST, SMTP_PORT, TEST_MODE, DEFAULT_RECIPIENT, CC_RECIPIENT
)


def generate_upcoming_annual_events_report(results_df):
    """Generate report for annual events needing outreach."""
    
    # Filter for annual pattern accounts that are Tier 3 or higher
    tier_3_plus = ['Key Account', 'High Value', 'Tier 4', 'Tier 3']
    annual_accounts = results_df[
        ((results_df['Event_Frequency_Current'] == 'Annual') | 
         (results_df['Event_Frequency_Previous'] == 'Annual')) &
        (results_df['Current_Tier'].isin(tier_3_plus))
    ].copy()
    
    upcoming = []
    for _, account in annual_accounts.iterrows():
        if pd.notna(account.get('_last_event_date')):
            # Predict next event (365 days from last)
            last_event = pd.to_datetime(account['_last_event_date'])
            predicted_event_date = last_event + pd.Timedelta(days=365)
            
            # Calculate when they'll likely create it
            lead_days = account.get('_avg_lead_days', 60)
            predicted_creation_date = predicted_event_date - pd.Timedelta(days=lead_days)
            
            # We want to reach out 30 days before creation
            outreach_date = predicted_creation_date - pd.Timedelta(days=30)
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
    
    return pd.DataFrame(upcoming).sort_values('Reach_Out_By') if upcoming else pd.DataFrame()


def email_upcoming_events_report(report_df, filename):
    """Email the upcoming annual events report."""
    
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
    
    # Create email message
    msg = EmailMessage()
    msg['Subject'] = f'{"[TEST] " if TEST_MODE else ""}Upcoming Annual Events - {datetime.now().strftime("%B %Y")}'
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    msg['To'] = DEFAULT_RECIPIENT
    
    if CC_RECIPIENT and not TEST_MODE:
        msg['Cc'] = CC_RECIPIENT
    
    # Set content
    msg.set_content(body_plain)
    msg.add_alternative(body_html, subtype='html')
    
    # Attach CSV file
    with open(filename, 'rb') as f:
        csv_data = f.read()
        msg.add_attachment(csv_data, maintype='text', subtype='csv', filename=filename)
    
    # Send email via Mailgun
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        smtp.send_message(msg)
    
    recipients = DEFAULT_RECIPIENT if TEST_MODE else f"{DEFAULT_RECIPIENT}, {CC_RECIPIENT}"
    print(f"Email sent to {recipients} with {len(report_df)} upcoming annual events")