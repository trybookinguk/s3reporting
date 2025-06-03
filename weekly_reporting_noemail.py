# -*- coding: utf-8 -*-
import os
import boto3
import pandas as pd
from datetime import datetime, timedelta

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

print("\n=== RESULTS ===")
print(f"Total accounts this week: {total_accounts}")
print(f"Total accounts last year same week: {total_accounts_ly}")
print(f"Year-over-year change: {yoy_change:.1f}%")

if len(current_week) > 0:
    print(f"Current week date range: {current_week['DateTimeCreated'].min()} to {current_week['DateTimeCreated'].max()}")
else:
    print("No data found for current week!")
    
if len(last_year_week) > 0:
    print(f"Last year week date range: {last_year_week['DateTimeCreated'].min()} to {last_year_week['DateTimeCreated'].max()}")
else:
    print("No data found for last year week!")

print("\n=== SUCCESS: Cross-month week handling is working! ===")