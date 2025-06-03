# -*- coding: utf-8 -*-
import os
import boto3
import pandas as pd
from datetime import datetime, timedelta
from calendar import monthrange

# === Secrets ===
AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]

# === Time Windows ===
today = datetime.today()
last_week_date = today - timedelta(days=today.weekday() + 7)
week_start = pd.Timestamp(last_week_date.date(), tz='Europe/London')
week_end = week_start + pd.Timedelta(days=6, hours=23, minutes=59, seconds=59)

print(f"Reporting week: {week_start.strftime('%d %B %Y')} to {week_end.strftime('%d %B %Y')}")
print(f"Week start month: {week_start.month}, Week end month: {week_end.month}")

# Calculate ISO week info for last year comparison
iso_year, iso_week, _ = last_week_date.isocalendar()
last_year_week_start = datetime.strptime(f'{iso_year - 1}-W{iso_week}-1', '%G-W%V-%u')
last_year_week_end = last_year_week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
last_year_week_start = pd.Timestamp(last_year_week_start, tz='Europe/London')
last_year_week_end = pd.Timestamp(last_year_week_end, tz='Europe/London')

print(f"Last year comparison week: {last_year_week_start.strftime('%d %B %Y')} to {last_year_week_end.strftime('%d %B %Y')}")

# === S3 Fetch - Handle cross-month weeks ===
bucket_name = "produk-rdsextracts-438255373632"

def get_file_from_s3(year, month):
    """Fetch and return dataframe for a specific year/month file"""
    folder_year = str(year)
    folder_month = f"{month:02d}"
    file_prefix = f"{year}{month:02d}"
    filename = f"{file_prefix}-Accounts-TBUK.csv"
    s3_key = f"{folder_year}/{folder_month}/{filename}"
    
    print(f"Attempting to fetch: {s3_key}")
    
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )
        obj = s3.get_object(Bucket=bucket_name, Key=s3_key)
        df = pd.read_csv(obj['Body'])
        df['DateTimeCreated'] = pd.to_datetime(df['DateTimeCreated'], errors='coerce', utc=True).dt.tz_convert('Europe/London')
        print(f"Successfully loaded {len(df)} records from {s3_key}")
        return df
    except Exception as e:
        print(f"Failed to fetch {s3_key}: {e}")
        return None

# Determine which files we need
files_needed = set()
files_needed.add((week_start.year, week_start.month))
files_needed.add((week_end.year, week_end.month))
files_needed.add((last_year_week_start.year, last_year_week_start.month))
files_needed.add((last_year_week_end.year, last_year_week_end.month))

print(f"Files needed: {files_needed}")

# Fetch all required files
all_dataframes = []
for year, month in files_needed:
    df = get_file_from_s3(year, month)
    if df is not None:
        all_dataframes.append(df)

if not all_dataframes:
    print("ERROR: No data files could be loaded!")
    exit(1)

# Combine all dataframes
df = pd.concat(all_dataframes, ignore_index=True)
print(f"Combined dataset has {len(df)} total records")

# Remove duplicates if any (in case of overlapping data)
df = df.drop_duplicates()
print(f"After deduplication: {len(df)} records")

# === Filtering ===
current_week = df[(df['DateTimeCreated'] >= week_start) & (df['DateTimeCreated'] <= week_end)]
last_year_week = df[(df['DateTimeCreated'] >= last_year_week_start) & (df['DateTimeCreated'] <= last_year_week_end)]

print(f"Current week data: {len(current_week)} records")
print(f"Last year week data: {len(last_year_week)} records")

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

# === Print Email Content Instead of Sending ===

print("\n" + "="*50)
print("EMAIL A (INTERNAL) CONTENT:")
print("="*50)

print(f"To: jules@trybooking.co.uk")
print(f"CC: alex@trybooking.co.uk, louise@trybooking.co.uk")
print(f"Subject: New Accounts w/c {week_start.strftime('%d %B %Y')}")
print()
print(f"Total accounts last week: {total_accounts}")
print(f"Percentage YoY change compared to the same week last year: {yoy_change:.0f}%")
print(f"Ticket Purchasers: {ticket_purchasers} ({ticket_purchaser_pct:.0f}%)")
print("Top 5 industries (excluding Ticket Purchasers):")
for industry, count in top_5_industries.items():
    print(f"  - {industry}: {count} ({(count / total_accounts) * 100:.0f}%)")
print(f"% of accounts without an industry assigned: {without_industry_pct:.0f}%")

print("\n" + "="*50)
print("EMAIL B (EXTERNAL) CONTENT:")
print("="*50)

print(f"To: gareth@dgtlonline.co.uk, clients@dgtlonline.co.uk")
print(f"CC: alex@trybooking.co.uk, joan@trybooking.co.uk")
print(f"Subject: TryBooking New Accounts w/c {week_start.strftime('%d %B %Y')}")
print()
print("Hi Gareth and Abi,")
print()
print(f"Please find actual new account numbers below for the week commencing {week_start.strftime('%d %B %Y')}.")
print()
print(f"Percentage change YoY compared to last year: {yoy_change:.0f}%")
print(f"% of accounts who are ticket purchasers: {ticket_purchaser_pct:.0f}%")
print()
print("Top 3 industries (excluding Ticket Purchasers):")
for entry in top_3_named:
    print(f"  - {entry}")
print()
print("Daily Breakdown:")
print(f"{'Day':<12} {'Total':<8} {'Day (0900–1730)':<16} {'Evening':<8}")
print("-" * 50)
for _, row in daily.iterrows():
    print(f"{row['DayName']:<12} {row['Total']:<8} {row.get('Day', 0):<16} {row.get('Evening', 0):<8}")
print()
print("Do hope this helps.")
print()
print("Kindest regards,")

print("\n" + "="*50)
print("DEBUG INFO:")
print("="*50)
print(f"Current week date range in data:")
if len(current_week) > 0:
    print(f"  Min: {current_week['DateTimeCreated'].min()}")
    print(f"  Max: {current_week['DateTimeCreated'].max()}")
else:
    print("  No data found for current week!")
    
print(f"Last year week date range in data:")
if len(last_year_week) > 0:
    print(f"  Min: {last_year_week['DateTimeCreated'].min()}")
    print(f"  Max: {last_year_week['DateTimeCreated'].max()}")
else:
    print("  No data found for last year week!")