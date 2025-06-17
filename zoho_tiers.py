import os
import boto3
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
from pandas.tseries.offsets import MonthBegin

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
                        'seen_tx_ids': set()
                    }
                
                # Filter out already seen transactions
                new_transactions = group[~group['BookingTransactionId'].isin(account_metrics[account_id]['seen_tx_ids'])]
                
                if len(new_transactions) > 0:
                    # Store only essential columns to save memory
                    essential_data = new_transactions[['TransactionDate', 'Revenue', 'TicketQuantity', 'Year', 'BookingTransactionId']].copy()
                    account_metrics[account_id]['transactions'].append(essential_data)
                    account_metrics[account_id]['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
            
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
        
        results.append({
            "Account_Name": int(row['Account_Name']),
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(row['tickets_current']),
            "Last_Year_Ticket_Quantity": int(row['tickets_prev']),
            "Years_Loyalty": row['years_loyalty']
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
    test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
    if test_mode:
        print("TEST MODE: Would update the following accounts:")
        print(records_df[['Account_Name', 'Current_Tier', 'Previous_Tier', 'Ticket_Quantity']].head(20))
        print(f"\nTotal accounts to update: {len(records_df)}")
        return
    
    # Process in batches of 200 (Zoho max)
    batch_size = 200
    for i in range(0, len(records_df), batch_size):
        batch = records_df.iloc[i:i+batch_size]
        payload = {
            "data": batch.to_dict(orient="records"),
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
    
    if not updates.empty:
        # Get Zoho token and update
        try:
            print("\nAuthenticating with Zoho...")
            token = get_access_token()
            
            print("Updating Zoho CRM...")
            upsert_to_zoho(token, updates)
            
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
