import os
import time
import boto3
import pandas as pd
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta

# === Secrets ===
AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
MAILGUN_SMTP_LOGIN = os.environ["MAILGUN_SMTP_LOGIN"]
MAILGUN_SMTP_PASSWORD = os.environ["MAILGUN_SMTP_PASSWORD"]
MAILGUN_DOMAIN = os.environ["MAILGUN_DOMAIN"]

# === Time Windows ===
today = datetime.today()
last_week_date = today - timedelta(days=today.weekday() + 7)
week_start = pd.Timestamp(last_week_date.date(), tz='Europe/London')
week_end = week_start + pd.Timedelta(days=6, hours=23, minutes=59, seconds=59)
iso_year, iso_week, _ = last_week_date.isocalendar()
last_year_week_start = datetime.strptime(f'{iso_year - 1}-W{iso_week}-1', '%G-W%V-%u')
last_year_week_end = last_year_week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
last_year_week_start = pd.Timestamp(last_year_week_start, tz='Europe/London')
last_year_week_end = pd.Timestamp(last_year_week_end, tz='Europe/London')

# === S3 Fetch ===
bucket_name = "produk-rdsextracts-438255373632"
folder_year = last_week_date.strftime('%Y')
folder_month = last_week_date.strftime('%m')
file_prefix = last_week_date.strftime('%Y%m')
filename = f"{file_prefix}-Accounts-TBUK.csv"
s3_key = f"{folder_year}/{folder_month}/{filename}"

s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

obj = s3.get_object(Bucket=bucket_name, Key=s3_key)
df = pd.read_csv(obj['Body'])
df['DateTimeCreated'] = pd.to_datetime(df['DateTimeCreated'], errors='coerce', utc=True).dt.tz_convert('Europe/London')

# === Filtering ===
current_week = df[(df['DateTimeCreated'] >= week_start) & (df['DateTimeCreated'] <= week_end)]
last_year_week = df[(df['DateTimeCreated'] >= last_year_week_start) & (df['DateTimeCreated'] <= last_year_week_end)]

total_accounts = len(current_week)
total_accounts_ly = len(last_year_week)
yoy_change = ((total_accounts - total_accounts_ly) / total_accounts_ly) * 100 if total_accounts_ly else 0
without_industry_pct = 100 * current_week['Industry'].isna().sum() / total_accounts if total_accounts else 0

# === Daily Breakdown ===
def classify_time(dt):
    return 'Day' if (dt.hour == 17 and dt.minute < 30) or (9 <= dt.hour < 17) else 'Evening'

daily = (
    current_week
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

# === Industry Analysis ===
ticket_purchasers = current_week['Industry'].eq("Ticket Purchaser").sum()
ticket_purchaser_pct = 100 * ticket_purchasers / total_accounts if total_accounts else 0

filtered_industries = current_week['Industry'][
    current_week['Industry'].notna() & (current_week['Industry'] != "Ticket Purchaser")
]
top_3_counts = filtered_industries.value_counts().head(3)
top_3_named = [f"{industry} ({(count / total_accounts) * 100:.0f}%)" for industry, count in top_3_counts.items()]
top_5_industries = filtered_industries.value_counts().head(5)

# === Mail sender ===
def send_mail(to, cc, subject, html_body):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    msg['To'] = to
    msg['Cc'] = cc
    msg.set_content("This is an HTML report. Please view it in an HTML-compatible client.")
    msg.add_alternative(html_body, subtype='html')

    with smtplib.SMTP("smtp.mailgun.org", 587) as smtp:
        smtp.starttls()
        smtp.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        smtp.send_message(msg)

# === Email A (Internal) ===
industry_lines = ''.join(
    f"<li>{industry}: {count} ({(count / total_accounts) * 100:.0f}%)</li>"
    for industry, count in top_5_industries.items()
)

html_a = f"""
<div style="font-family: Arial, sans-serif; font-size: 11pt;">
  <p>Total accounts last week: {total_accounts}</p>
  <p>Percentage YoY change compared to the same week last year: {yoy_change:.0f}%</p>
  <p>Ticket Purchasers: {ticket_purchasers} ({ticket_purchaser_pct:.0f}%)</p>
  <p>Top 5 industries (excluding Ticket Purchasers):</p>
  <ul>{industry_lines}</ul>
  <p>% of accounts without an industry assigned: {without_industry_pct:.0f}%</p>
</div>
"""

send_mail(
    to="jules@trybooking.co.uk",
    cc="alex@trybooking.co.uk, louise@trybooking.co.uk",
    subject=f"New Accounts w/c {week_start.strftime('%d %B %Y')}",
    html_body=html_a
)

# === Email B (External) ===
html_table_rows = ''.join(
    f"<tr><td>{row['DayName']}</td><td>{row['Total']}</td><td>{row.get('Day', 0)}</td><td>{row.get('Evening', 0)}</td></tr>"
    for _, row in daily.iterrows()
)

html_b = f"""
<div style="font-family: Arial, sans-serif; font-size: 11pt;">
  <p>Hi Gareth and Abi,</p>
  <p>Please find actual new account numbers below for the week commencing {week_start.strftime('%d %B %Y')}.</p>
  <p>Percentage change YoY compared to last year: {yoy_change:.0f}%<br>
     % of accounts who are ticket purchasers: {ticket_purchaser_pct:.0f}%</p>
  <p>Top 3 industries (excluding Ticket Purchasers):</p>
  <ul>{''.join(f'<li>{entry}</li>' for entry in top_3_named)}</ul>
  <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
    <thead>
      <tr><th>Day</th><th>Total</th><th>Day (0900–1730)</th><th>Evening</th></tr>
    </thead>
    <tbody>{html_table_rows}</tbody>
  </table>
  <p>Do hope this helps.</p>
  <p>Kindest regards,</p>
</div>
"""

send_mail(
    to="alex@trybooking.co.uk",
    subject=f"TryBooking New Accounts w/c {week_start.strftime('%d %B %Y')}",
    html_body=html_b
)
