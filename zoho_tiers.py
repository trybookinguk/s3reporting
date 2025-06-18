import os
import boto3
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
from pandas.tseries.offsets import MonthBegin
import smtplib
from email.message import EmailMessage

# === ENV VARS ===
# Support both naming conventions for AWS credentials
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")

if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    raise ValueError("AWS credentials not found in environment variables")

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_DOMAIN = "https://www.zohoapis.com"
BUCKET = "produk-rdsextracts-438255373632"

# Mailgun credentials
MAILGUN_SMTP_LOGIN = os.environ["MAILGUN_SMTP_LOGIN"]
MAILGUN_SMTP_PASSWORD = os.environ["MAILGUN_SMTP_PASSWORD"]
MAILGUN_DOMAIN = os.environ["MAILGUN_DOMAIN"]

# Check if running in test mode
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# === DATE WINDOWS ===
UK_TZ = pytz.timezone('Europe/London')
TODAY = datetime.now(UK_TZ).date()
CUTOFF_365 = TODAY - timedelta(days=365)
CUTOFF_730 = CUTOFF_365 - timedelta(days=365)

# === AUTH ===
def get_access_token():
    resp = requests.post(
        "https://accounts.zoho.com/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        }
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

# === S3 FETCH ===
def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

def fetch_s3_file_info(s3_client, key):
    """Get file size without downloading"""
    try:
        response = s3_client.head_object(Bucket=BUCKET, Key=key)
        return response['ContentLength']
    except:
        return 0

def process_booking_data_optimized(s3_client, key_all, key_month):
    """Process booking data using chunked reading and optimized memory usage"""
    print("\nOptimized processing for large files...")
    
    # Define data types to reduce memory usage
    dtypes = {
        'BookingTransactionId': 'int64',
        'AccountId': 'int32',
        'EventID': 'int64',  # Added for event tracking
        'TicketQuantity': 'int16',
        'BookingFee': 'float32',
        'CardFee': 'float32',
        'ProcessingFee': 'float32',
        'TicketFee': 'float32'
    }
    
    # Process in chunks and aggregate by account
    account_metrics = {}
    chunk_size = 100000  # Process 100k rows at a time
    
    for key in [key_all, key_month]:
        print(f"\nProcessing {key}...")
        file_size = fetch_s3_file_info(s3_client, key)
        print(f"  File size: {file_size / 1024 / 1024:.1f} MB")
        
        obj = s3_client.get_object(Bucket=BUCKET, Key=key)
        
        # First, peek at the columns to verify structure
        first_chunk = pd.read_csv(obj['Body'], nrows=5)
        available_columns = list(first_chunk.columns)
        print(f"  Sample columns: {available_columns[:10]}...")  # Show first 10 columns
        print(f"  Total columns: {len(available_columns)}")
        
        # Only use dtypes for columns that exist
        actual_dtypes = {col: dtype for col, dtype in dtypes.items() if col in available_columns}
        print(f"  Using dtypes for: {list(actual_dtypes.keys())}")
        
        # Re-fetch the object for actual processing
        obj = s3_client.get_object(Bucket=BUCKET, Key=key)
        
        total_rows = 0
        # Add low_memory=False to handle mixed types warning
        for chunk_num, chunk in enumerate(pd.read_csv(obj['Body'], chunksize=chunk_size, dtype=actual_dtypes, parse_dates=['TransactionDate'], low_memory=False)):
            # Add timezone info
            chunk['TransactionDate'] = pd.to_datetime(chunk['TransactionDate'], utc=True).dt.tz_convert(UK_TZ)
            chunk['Revenue'] = chunk['BookingFee'] + chunk['CardFee'] + chunk['ProcessingFee'] + chunk['TicketFee']
            chunk['Year'] = chunk['TransactionDate'].dt.year
            
            # Drop duplicates within chunk
            chunk = chunk.drop_duplicates(subset='BookingTransactionId')
            
            # Aggregate by account
            for account_id, group in chunk.groupby('AccountId'):
                if account_id not in account_metrics:
                    account_metrics[account_id] = {
                        'transactions': [],
                        'seen_tx_ids': set(),
                        'event_ids_current': set(),
                        'event_ids_previous': set(),
                        'event_creation_info': {},
                        'last_booking_date': None
                    }
                
                # Filter out already seen transactions
                new_transactions = group[~group['BookingTransactionId'].isin(account_metrics[account_id]['seen_tx_ids'])]
                
                if len(new_transactions) > 0:
                    # Store only essential columns to save memory
                    essential_cols = ['TransactionDate', 'Revenue', 'TicketQuantity', 'Year', 'BookingTransactionId']
                    if 'EventID' in new_transactions.columns:
                        essential_cols.append('EventID')
                    if 'EventDate' in new_transactions.columns:
                        essential_cols.append('EventDate')
                    
                    essential_data = new_transactions[essential_cols].copy()
                    account_metrics[account_id]['transactions'].append(essential_data)
                    account_metrics[account_id]['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
                    
                    # Track event information
                    for _, trans in new_transactions.iterrows():
                        trans_date = trans['TransactionDate']
                        
                        # Track last booking date
                        if account_metrics[account_id]['last_booking_date'] is None or trans_date > account_metrics[account_id]['last_booking_date']:
                            account_metrics[account_id]['last_booking_date'] = trans_date
                        
                        if 'EventID' in trans and pd.notna(trans['EventID']):
                            event_id = int(trans['EventID'])
                            
                            # Track which period the event belongs to
                            if trans_date.date() >= CUTOFF_365:
                                account_metrics[account_id]['event_ids_current'].add(event_id)
                            elif trans_date.date() >= CUTOFF_730:
                                account_metrics[account_id]['event_ids_previous'].add(event_id)
                            
                            # Track first booking per event (proxy for creation)
                            if event_id not in account_metrics[account_id]['event_creation_info']:
                                event_date = trans.get('EventDate')
                                if pd.notna(event_date):
                                    lead_days = (pd.to_datetime(event_date).date() - trans_date.date()).days
                                    account_metrics[account_id]['event_creation_info'][event_id] = {
                                        'first_booking': trans_date,
                                        'event_date': pd.to_datetime(event_date),
                                        'lead_days': max(lead_days, 0)
                                    }
            
            total_rows += len(chunk)
            if chunk_num % 10 == 0:
                print(f"  Processed {total_rows:,} rows...")
        
        print(f"  Total rows processed: {total_rows:,}")
    
    return account_metrics

# === TIER LOGIC ===
def determine_tier_from_percentiles(a_pct, b_pct, c_years, d_pct, e_pct, has_activity):
    """
    Determine tier based on percentile rankings.
    a_pct: percentile rank for tickets_current (0-100)
    b_pct: percentile rank for revenue_current (0-100)
    c_years: years_loyalty (actual value, not percentile)
    d_pct: percentile rank for lifetime_revenue (0-100)
    e_pct: percentile rank for avg_revenue_per_year (0-100)
    has_activity: whether account has any current period activity
    """
    if not has_activity:
        return "NIL"
    
    # Tier thresholds (percentiles)
    tier_thresholds = {
        "Key Account": 99,    # Top 1%
        "High Value": 95,     # Top 5%
        "Tier 4": 80,         # Top 20%
        "Tier 3": 50,         # Top 50%
        "Tier 2": 25,         # Top 75%
    }
    
    # Check each path: A alone, B alone, or C+D+E combination
    best_tier = "Tier 1"  # Default for qualified accounts
    
    # Path 1: A alone (tickets)
    for tier, threshold in tier_thresholds.items():
        if a_pct >= threshold:
            best_tier = tier
            break
    
    # Path 2: B alone (revenue)
    for tier, threshold in tier_thresholds.items():
        if b_pct >= threshold:
            # Upgrade tier if better than current best
            if list(tier_thresholds.keys()).index(tier) < list(tier_thresholds.keys()).index(best_tier) if best_tier in tier_thresholds else True:
                best_tier = tier
            break
    
    # Path 3: C+D+E combination (requires minimum years loyalty)
    # Define minimum years required for each tier
    min_years_by_tier = {
        "Key Account": 8,
        "High Value": 7,
        "Tier 4": 5,
        "Tier 3": 3,
        "Tier 2": 2,
        "Tier 1": 1
    }
    
    for tier, threshold in tier_thresholds.items():
        if c_years >= min_years_by_tier.get(tier, 1):
            # Both D and E must meet the threshold
            if d_pct >= threshold and e_pct >= threshold:
                # Upgrade tier if better than current best
                if list(tier_thresholds.keys()).index(tier) < list(tier_thresholds.keys()).index(best_tier) if best_tier in tier_thresholds else True:
                    best_tier = tier
                break
    
    return best_tier

# === HELPER FUNCTIONS FOR NEW METRICS ===
def classify_event_frequency(event_count):
    """Convert event count to pattern classification"""
    if event_count == 0:
        return "Inactive"
    elif event_count == 1:
        return "Annual"
    elif event_count <= 3:
        return "Occasional"
    else:  # 4+
        return "Regular"

def determine_activity_rating(current_freq, previous_freq, days_since_last, has_historical):
    """Determine activity rating based on event patterns"""
    if current_freq != "Inactive":
        return "Active"
    
    if previous_freq == "Inactive" and not has_historical:
        return "New" if days_since_last < 365 else "Inactive"
    
    if current_freq != "Inactive" and previous_freq == "Inactive" and has_historical:
        return "Returned"
    
    # At Risk logic accounting for event patterns
    if days_since_last < 365 and days_since_last > 90:
        if previous_freq == "Annual" and days_since_last < 275:  # More grace for annual
            return "Active"
        return "At Risk"
    
    if previous_freq != "Inactive" and current_freq == "Inactive":
        return "Churned"
    
    return "Inactive"

# === METRICS CALC ===
def calculate_metrics_from_aggregated(account_metrics):
    """Calculate metrics from pre-aggregated account data"""
    print("\nCalculating metrics for accounts...")
    
    all_metrics = []
    processed = 0
    
    for account_id, data in account_metrics.items():
        if not data['transactions']:
            continue
            
        # Combine all transactions for this account
        account_df = pd.concat(data['transactions'], ignore_index=True)
        account_df = account_df.sort_values('TransactionDate')
        
        # Define windows
        current_period = account_df[account_df['TransactionDate'].dt.date >= CUTOFF_365]
        previous_period = account_df[
            (account_df['TransactionDate'].dt.date >= CUTOFF_730) &
            (account_df['TransactionDate'].dt.date < CUTOFF_365)
        ]
        lifetime = account_df
        lifetime_pre_cutoff = account_df[account_df['TransactionDate'].dt.date < CUTOFF_365]
        
        # Calculate metrics
        years_loyalty = lifetime['Year'].nunique()
        lifetime_revenue = lifetime['Revenue'].sum()
        avg_revenue_per_year = lifetime_revenue / years_loyalty if years_loyalty else 0
        tickets_current = current_period['TicketQuantity'].sum()
        revenue_current = current_period['Revenue'].sum()
        
        # Previous period metrics
        years_loyalty_prev = lifetime_pre_cutoff['Year'].nunique()
        revenue_prev = lifetime_pre_cutoff['Revenue'].sum()
        avg_rev_prev = revenue_prev / years_loyalty_prev if years_loyalty_prev else 0
        tickets_prev = previous_period['TicketQuantity'].sum()
        revenue_window_prev = previous_period['Revenue'].sum()
        
        all_metrics.append({
            'Account_Name': account_id,
            'tickets_current': float(tickets_current),
            'revenue_current': float(revenue_current),
            'years_loyalty': years_loyalty,
            'lifetime_revenue': float(lifetime_revenue),
            'avg_revenue_per_year': float(avg_revenue_per_year),
            'tickets_prev': float(tickets_prev),
            'revenue_prev': float(revenue_window_prev),
            'years_loyalty_prev': years_loyalty_prev,
            'lifetime_revenue_prev': float(revenue_prev),
            'avg_revenue_prev': float(avg_rev_prev),
            'has_activity': tickets_current >= 10
        })
        
        processed += 1
        if processed % 1000 == 0:
            print(f"  Processed {processed:,} accounts...")
        
        # Clear transaction data to free memory
        data['transactions'] = None
    
    print(f"  Total accounts processed: {processed:,}")
    
    # Convert to DataFrame
    metrics_df = pd.DataFrame(all_metrics)
    
    # Calculate percentiles
    print("\nCalculating percentiles...")
    for metric in ['tickets_current', 'revenue_current', 'lifetime_revenue', 'avg_revenue_per_year']:
        pct_col = f"{metric}_pct"
        mask = metrics_df[metric] > 0
        if mask.sum() > 0:
            metrics_df.loc[mask, pct_col] = metrics_df.loc[mask, metric].rank(pct=True, method='average') * 100
        metrics_df.loc[~mask, pct_col] = 0
    
    # Previous period percentiles
    for metric, prev_metric in [('tickets_current', 'tickets_prev'), 
                                 ('revenue_current', 'revenue_prev'),
                                 ('lifetime_revenue', 'lifetime_revenue_prev'),
                                 ('avg_revenue_per_year', 'avg_revenue_prev')]:
        pct_col = f"{prev_metric}_pct"
        mask = metrics_df[prev_metric] > 0
        if mask.sum() > 0:
            metrics_df.loc[mask, pct_col] = metrics_df.loc[mask, prev_metric].rank(pct=True, method='average') * 100
        metrics_df.loc[~mask, pct_col] = 0
    
    # Apply tier logic
    print("\nAssigning tiers...")
    results = []
    for _, row in metrics_df.iterrows():
        tier_current = determine_tier_from_percentiles(
            row['tickets_current_pct'],
            row['revenue_current_pct'],
            row['years_loyalty'],
            row['lifetime_revenue_pct'],
            row['avg_revenue_per_year_pct'],
            row['has_activity']
        )
        
        tier_prev = determine_tier_from_percentiles(
            row['tickets_prev_pct'],
            row['revenue_prev_pct'],
            row['years_loyalty_prev'],
            row['lifetime_revenue_prev_pct'],
            row['avg_revenue_prev_pct'],
            row['tickets_prev'] >= 10
        )
        
        # Handle Account_Name conversion safely
        try:
            # Check if Account_Name is valid and numeric
            if pd.notna(row['Account_Name']) and str(row['Account_Name']).strip():
                account_name = str(int(float(row['Account_Name'])))
            else:
                # Skip this record if Account_Name is invalid
                continue
        except (ValueError, TypeError):
            # Skip this record if conversion fails
            print(f"Warning: Skipping account with invalid Account_Name: {row['Account_Name']}")
            continue
        
        # Calculate new metrics
        account_id = int(row['Account_Name'])
        
        # Get event counts
        event_count_current = len(account_metrics[account_id].get('event_ids_current', set()))
        event_count_previous = len(account_metrics[account_id].get('event_ids_previous', set()))
        event_creation_info = account_metrics[account_id].get('event_creation_info', {})
        has_historical = len(event_creation_info) > event_count_current + event_count_previous
        
        # Calculate event frequency
        event_freq_current = classify_event_frequency(event_count_current)
        event_freq_previous = classify_event_frequency(event_count_previous)
        
        # Calculate lead times
        lead_times = [info['lead_days'] for info in event_creation_info.values() if info['lead_days'] > 0]
        avg_lead_days = int(sum(lead_times) / len(lead_times)) if lead_times else 60
        
        # Calculate days since last activity
        last_booking = account_metrics[account_id].get('last_booking_date')
        days_since_last = (TODAY - last_booking.date()).days if last_booking else 999
        
        # Determine activity rating
        activity_rating = determine_activity_rating(
            event_freq_current, event_freq_previous, days_since_last, has_historical
        )
        
        # Get last event date
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        last_event_date = max(event_dates).date() if event_dates else None
            
        results.append({
            "Account_Name": account_name,
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(row['tickets_current']),
            "Last_Year_Ticket_Quantity": int(row['tickets_prev']),
            "Years_Loyalty": row['years_loyalty'],
            "Event_Frequency_Current": event_freq_current,
            "Event_Frequency_Previous": event_freq_previous,
            "Activity_Rating": activity_rating,
            # Hidden fields for report generation (prefix with _)
            "_avg_lead_days": avg_lead_days,
            "_last_event_date": last_event_date,
            "_event_count_current": event_count_current
        })
    
    return pd.DataFrame(results)

# === ZOHO UPSERT ===
def upsert_to_zoho(token, records_df):
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/upsert"
    
    # Check test mode
    if TEST_MODE:
        print("TEST MODE: Would update the following accounts:")
        print(records_df[['Account_Name', 'Current_Tier', 'Previous_Tier', 'Ticket_Quantity', 'Last_Year_Ticket_Quantity']].head(20))
        print(f"\nTotal accounts to update: {len(records_df)}")
        return
    
    # Process in batches of 100 (Zoho max)
    batch_size = 100
    for i in range(0, len(records_df), batch_size):
        batch = records_df.iloc[i:i+batch_size]
        
        # Ensure all Account_Name values are strings
        records = batch.to_dict(orient="records")
        for record in records:
            record['Account_Name'] = str(record['Account_Name'])
        
        payload = {
            "data": records,
            "duplicate_check_fields": ["Account_Name"]
        }
        
        try:
            resp = requests.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                print(f"Batch {i//batch_size + 1} failed: {resp.status_code} - {resp.text}")
            else:
                print(f"Batch {i//batch_size + 1} success ({len(batch)} records)")
        except Exception as e:
            print(f"Batch {i//batch_size + 1} error: {str(e)}")

# === ANNUAL EVENTS REPORT ===
def generate_upcoming_annual_events_report(results_df):
    """Generate report for annual events needing outreach"""
    
    # Filter for annual pattern accounts
    annual_accounts = results_df[
        (results_df['Event_Frequency_Current'] == 'Annual') | 
        (results_df['Event_Frequency_Previous'] == 'Annual')
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
                upcoming.append({
                    'Account_Name': account['Account_Name'],
                    'Tier': account['Current_Tier'],
                    'Last_Event_Date': last_event.strftime('%d/%m/%Y'),
                    'Expected_Event_Date': predicted_event_date.strftime('%d/%m/%Y'),
                    'Typical_Creation_Lead_Days': lead_days,
                    'Reach_Out_By': outreach_date.strftime('%d/%m/%Y'),
                    'Last_Year_Tickets': int(account['Last_Year_Ticket_Quantity']),
                    'Status': account['Activity_Rating']
                })
    
    return pd.DataFrame(upcoming).sort_values('Reach_Out_By') if upcoming else pd.DataFrame()

def email_upcoming_events_report(report_df, filename):
    """Email the upcoming annual events report"""
    
    # Prepare email body
    body_plain = f"""Hi Alex,

Please find attached the upcoming annual events report.

This report identifies annual event organisers who typically create their events soon, 
allowing proactive outreach approximately 1 month before they usually set up their event.

Summary:
- Total accounts requiring outreach: {len(report_df)}
- Outreach needed within 7 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By']) <= pd.Timestamp.now() + pd.Timedelta(days=7)])}
- Outreach needed within 14 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By']) <= pd.Timestamp.now() + pd.Timedelta(days=14)])}

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
<li>Outreach needed within 7 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By']) <= pd.Timestamp.now() + pd.Timedelta(days=7)])}</li>
<li>Outreach needed within 14 days: {len(report_df[pd.to_datetime(report_df['Reach_Out_By']) <= pd.Timestamp.now() + pd.Timedelta(days=14)])}</li>
</ul>
<p>Best regards,<br>TryBooking Reporting System</p>
</div>"""
    
    # Create email message
    msg = EmailMessage()
    msg['Subject'] = f'{"[TEST] " if TEST_MODE else ""}Upcoming Annual Events - {datetime.now().strftime("%B %Y")}'
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    msg['To'] = "alex@trybooking.co.uk" if TEST_MODE else "alex@trybooking.co.uk"
    msg['Cc'] = "" if TEST_MODE else "louise@trybooking.co.uk"
    
    # Set content
    msg.set_content(body_plain)
    msg.add_alternative(body_html, subtype='html')
    
    # Attach CSV file
    with open(filename, 'rb') as f:
        csv_data = f.read()
        msg.add_attachment(csv_data, maintype='text', subtype='csv', filename=filename)
    
    # Send email via Mailgun
    with smtplib.SMTP("smtp.mailgun.org", 587) as smtp:
        smtp.starttls()
        smtp.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        smtp.send_message(msg)
    
    recipients = "alex@trybooking.co.uk" if TEST_MODE else "alex@trybooking.co.uk, louise@trybooking.co.uk"
    print(f"Email sent to {recipients} with {len(report_df)} upcoming annual events")

# === MAIN ===
def main():
    import time
    start_time = time.time()
    
    print(f"\n=== Zoho Tier Update Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    
    # Determine report date
    report_date = pd.Timestamp.now(UK_TZ).normalize() - pd.Timedelta(days=1)
    if report_date.day == 1:
        report_date -= MonthBegin(1)

    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")

    print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
    
    # S3 keys
    key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
    key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Process data using optimized chunked approach
        account_metrics = process_booking_data_optimized(s3_client, key_all, key_month)
        
        print(f"\nTotal unique accounts found: {len(account_metrics):,}")
        
    except Exception as e:
        print(f"ERROR: Failed to process S3 files: {str(e)}")
        import traceback
        traceback.print_exc()
        return
    
    # Calculate metrics and tiers
    updates = calculate_metrics_from_aggregated(account_metrics)
    
    # Save results to CSV for audit
    csv_filename = f"tier_updates_{datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    updates.to_csv(csv_filename, index=False)
    print(f"\nSaved tier calculations to: {csv_filename}")
    
    # Summary statistics
    tier_counts = updates['Current_Tier'].value_counts()
    print("\nTier Distribution:")
    for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
        count = tier_counts.get(tier, 0)
        pct = (count / len(updates) * 100) if len(updates) > 0 else 0
        print(f"  {tier}: {count:,} ({pct:.1f}%)")
    
    # Tier changes
    tier_changes = updates[updates['Current_Tier'] != updates['Previous_Tier']]
    print(f"\nTier Changes: {len(tier_changes):,} accounts")
    
    # Show some tier change examples
    if len(tier_changes) > 0:
        print("\nExample tier changes (first 5):")
        for _, row in tier_changes.head(5).iterrows():
            print(f"  Account {row['Account_Name']}: {row['Previous_Tier']} → {row['Current_Tier']}")
    
    # Generate and email annual events report
    annual_report = generate_upcoming_annual_events_report(updates)
    if not annual_report.empty:
        report_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
        annual_report.to_csv(report_filename, index=False)
        
        try:
            email_upcoming_events_report(annual_report, report_filename)
            print(f"\n📧 Emailed upcoming annual events report: {len(annual_report)} accounts")
        except Exception as e:
            print(f"WARNING: Failed to email annual events report: {str(e)}")
    else:
        print("\nNo upcoming annual events requiring outreach.")
    
    # Clean up hidden fields before Zoho upload
    zoho_updates = updates.drop(columns=[col for col in updates.columns if col.startswith('_')], errors='ignore')
    
    if not zoho_updates.empty:
        # Get Zoho token and update
        try:
            print("\nAuthenticating with Zoho...")
            token = get_access_token()
            
            print("Updating Zoho CRM...")
            upsert_to_zoho(token, zoho_updates)
            
        except Exception as e:
            print(f"ERROR: Zoho update failed: {str(e)}")
            import traceback
            traceback.print_exc()
    else:
        print("No updates required.")
    
    # Performance stats
    elapsed_time = time.time() - start_time
    print(f"\n=== Completed at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Total execution time: {elapsed_time:.1f} seconds ({elapsed_time/60:.1f} minutes)")


if __name__ == "__main__":
    main()
