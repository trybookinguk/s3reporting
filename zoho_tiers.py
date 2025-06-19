import os
import boto3
import pandas as pd
import numpy as np
from scipy import stats
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

def load_booking_data_for_analysis(s3_client, key_month):
    """Load booking data for churn risk analysis"""
    print("\nLoading current month booking data for enhanced analysis...")
    try:
        obj = s3_client.get_object(Bucket=BUCKET, Key=key_month)
        
        # Define dtypes for columns we need
        dtypes = {
            'BookingTransactionId': 'int64',
            'AccountId': 'int32',
            'EventId': 'Int64',
            'TicketQuantity': 'int16',
            'BookingFee': 'float32',
            'CardFee': 'float32',
            'ProcessingFee': 'float32',
            'TicketFee': 'float32'
        }
        
        # Load with specific columns and dtypes
        booking_df = pd.read_csv(obj['Body'], dtype=dtypes, parse_dates=['TransactionDate', 'EventDate'], low_memory=False)
        
        # Add timezone info
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'], utc=True).dt.tz_convert(UK_TZ)
        if 'EventDate' in booking_df.columns:
            booking_df['EventDate'] = pd.to_datetime(booking_df['EventDate'], utc=True).dt.tz_convert(UK_TZ)
            
        print(f"  Loaded {len(booking_df):,} transactions for {booking_df['AccountId'].nunique():,} accounts")
        return booking_df
        
    except Exception as e:
        print(f"  WARNING: Could not load booking data for analysis: {e}")
        return pd.DataFrame()

def process_booking_data_optimized(s3_client, key_all, key_month):
    """Process booking data using chunked reading and optimized memory usage"""
    print("\nOptimized processing for large files...")
    
    # Define data types to reduce memory usage
    dtypes = {
        'BookingTransactionId': 'int64',
        'AccountId': 'int32',
        'EventId': 'Int64',  # Nullable integer type for EventId (note capital 'I')
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
                    if 'EventId' in new_transactions.columns:
                        essential_cols.append('EventId')
                    if 'EventDate' in new_transactions.columns:
                        essential_cols.append('EventDate')
                    
                    essential_data = new_transactions[essential_cols].copy()
                    account_metrics[account_id]['transactions'].append(essential_data)
                    account_metrics[account_id]['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
                    
                    # Track event information (vectorized approach)
                    # Update last booking date
                    if len(new_transactions) > 0:
                        last_booking = new_transactions['TransactionDate'].max()
                        if account_metrics[account_id]['last_booking_date'] is None or last_booking > account_metrics[account_id]['last_booking_date']:
                            account_metrics[account_id]['last_booking_date'] = last_booking
                    
                    # Process events if EventId column exists
                    if 'EventId' in new_transactions.columns and 'EventDate' in new_transactions.columns:
                        event_data = new_transactions[['EventId', 'TransactionDate', 'EventDate']].copy()
                        event_data = event_data[pd.notna(event_data['EventId'])]
                        
                        if len(event_data) > 0:
                            # Vectorized period classification (EventId is already Int64 nullable type)
                            # No need to convert, as Int64 handles nulls properly
                            current_mask = event_data['TransactionDate'].dt.date >= CUTOFF_365
                            previous_mask = (event_data['TransactionDate'].dt.date >= CUTOFF_730) & (~current_mask)
                            
                            # Update event sets (convert nullable Int64 to regular int)
                            current_events = event_data[current_mask]['EventId'].dropna().astype(int).unique()
                            previous_events = event_data[previous_mask]['EventId'].dropna().astype(int).unique()
                            account_metrics[account_id]['event_ids_current'].update(current_events)
                            account_metrics[account_id]['event_ids_previous'].update(previous_events)
                            
                            # Group by EventId to find first booking per event
                            event_groups = event_data[pd.notna(event_data['EventDate'])].groupby('EventId')
                            
                            for event_id, group in event_groups:
                                # Convert nullable Int64 to regular int for dictionary key
                                event_id_key = int(event_id) if pd.notna(event_id) else None
                                if event_id_key and event_id_key not in account_metrics[account_id]['event_creation_info']:
                                    first_booking = group['TransactionDate'].min()
                                    event_date = group['EventDate'].iloc[0]
                                    lead_days = (pd.to_datetime(event_date).date() - first_booking.date()).days
                                    account_metrics[account_id]['event_creation_info'][event_id_key] = {
                                        'first_booking': first_booking,
                                        'event_date': pd.to_datetime(event_date),
                                        'lead_days': max(lead_days, 0)
                                    }
            
            total_rows += len(chunk)
            if chunk_num % 10 == 0:
                print(f"  Processed {total_rows:,} rows...")
        
        print(f"  Total rows processed: {total_rows:,}")
        
        # Debug: sample event tracking
        if len(account_metrics) > 0:
            sample_accounts = list(account_metrics.keys())[:5]  # Show 5 samples
            print("\n  Sample event tracking:")
            for acc_id in sample_accounts:
                curr_events = len(account_metrics[acc_id].get('event_ids_current', set()))
                prev_events = len(account_metrics[acc_id].get('event_ids_previous', set()))
                curr_event_ids = list(account_metrics[acc_id].get('event_ids_current', set()))[:3]  # Show first 3 IDs
                print(f"    Account {acc_id}: {curr_events} current events, {prev_events} previous events")
                if curr_event_ids:
                    print(f"      Sample current event IDs: {curr_event_ids}")
    
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
        "Tier 4": 75,         # Top 25%
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

# === BULLETPROOF CHURN RISK MODEL ===

def calculate_absolute_decline(row):
    """
    Measures actual revenue loss, not relative position
    Component 1: 0-30 points
    """
    revenue_current = row.get('revenue_current', 0)
    revenue_previous = row.get('revenue_prev', 0)
    
    if revenue_previous == 0:
        return 0  # No baseline
    
    decline_pct = max(0, (revenue_previous - revenue_current) / revenue_previous)
    
    # Non-linear scoring
    if decline_pct >= 0.75:      # Lost 75%+ revenue
        return 30
    elif decline_pct >= 0.50:    # Lost 50-75%
        return 20 + (decline_pct - 0.5) * 40  # 20-30 points
    elif decline_pct >= 0.25:    # Lost 25-50%
        return 10 + (decline_pct - 0.25) * 40  # 10-20 points
    else:                        # Lost 0-25%
        return decline_pct * 40   # 0-10 points

def calculate_position_risk(row, cohort):
    """
    Low percentile = high risk, but capped to prevent volatility
    Component 2: 0-25 points
    """
    if len(cohort) < 100:
        # Use tier-based scoring instead
        return calculate_tier_based_risk(row)
    
    current_percentile = row.get('revenue_current_pct', 0)
    
    # Inverted scale with graduated risk
    if current_percentile == 0:      # No revenue
        return 25
    elif current_percentile < 10:    # Bottom 10%
        return 20 + (10 - current_percentile) * 0.5  # 20-25 points
    elif current_percentile < 25:    # Bottom quartile
        return 15 + (25 - current_percentile) * 0.33  # 15-20 points
    elif current_percentile < 50:    # Below median
        return 5 + (50 - current_percentile) * 0.4   # 5-15 points
    else:                            # Above median
        return max(0, (50 - current_percentile) * 0.1)  # 0-5 points

def calculate_tier_based_risk(row):
    """
    Fallback when cohort is too small
    Uses tier position as proxy for risk
    """
    tier = row.get('Current_Tier', 'NIL')
    tier_risk = {
        'NIL': 25,
        'Tier 1': 20,
        'Tier 2': 15,
        'Tier 3': 10,
        'Tier 4': 5,
        'High Value': 3,
        'Key Account': 0
    }
    return tier_risk.get(tier, 15)

def calculate_activity_risk(row, cohort):
    """
    Simple days-based risk with pattern adjustment
    Component 3: 0-15 points
    """
    days_inactive = row.get('_days_since_last', 999)
    pattern = row.get('Event_Frequency_Previous', 'Unknown')
    
    # Pattern-specific thresholds
    if pattern == 'Annual':
        threshold_low = 300   # ~10 months
        threshold_high = 450  # ~15 months
    elif pattern == 'Occasional':
        threshold_low = 120   # 4 months
        threshold_high = 240  # 8 months
    elif pattern == 'Regular':
        threshold_low = 45    # 1.5 months
        threshold_high = 90   # 3 months
    else:  # Unknown/Inactive
        threshold_low = 180
        threshold_high = 365
    
    if days_inactive <= threshold_low:
        return 0
    elif days_inactive <= threshold_high:
        progress = (days_inactive - threshold_low) / (threshold_high - threshold_low)
        return progress * 15  # 0-15 points
    else:
        return 15  # Max points

def calculate_momentum(row):
    """
    Rate of change matters - gradual vs sudden decline
    Component 4: 0-15 points
    """
    # Compare current to previous percentile, but bounded
    percentile_drop = max(0, row.get('revenue_prev_pct', 0) - row.get('revenue_current_pct', 0))
    
    # Only significant drops matter (>10 percentile points)
    if percentile_drop > 10:
        # Cap at 50 percentile drop to prevent volatility
        bounded_drop = min(percentile_drop - 10, 40) / 40
        return bounded_drop * 15  # 0-15 points
    return 0

def calculate_business_value(row):
    """
    Prioritize high-value accounts
    Component 5: 0-15 points
    """
    tier = row.get('Current_Tier', 'NIL')
    lifetime_revenue = row.get('lifetime_revenue', 0)
    
    # Tier-based component (0-10 points)
    tier_points = {
        'Key Account': 10,
        'High Value': 8,
        'Tier 4': 6,
        'Tier 3': 4,
        'Tier 2': 2,
        'Tier 1': 1,
        'NIL': 0
    }
    tier_score = tier_points.get(tier, 0)
    
    # Lifetime value component (0-5 points)
    if lifetime_revenue > 100000:
        ltv_score = 5
    elif lifetime_revenue > 50000:
        ltv_score = 4
    elif lifetime_revenue > 25000:
        ltv_score = 3
    elif lifetime_revenue > 10000:
        ltv_score = 2
    elif lifetime_revenue > 5000:
        ltv_score = 1
    else:
        ltv_score = 0
    
    return tier_score + ltv_score

def calculate_seasonality_multiplier(account_id, account_metrics, all_accounts_df=None, industry_lookup=None):
    """
    Smart seasonality adjustment based on event creation patterns
    Returns multiplier between 0.7-1.5
    
    Key insight: We care about when events are CREATED, not when they happen
    """
    if account_id not in account_metrics or not account_metrics[account_id].get('event_creation_info'):
        return 1.0
    
    event_info = account_metrics[account_id]['event_creation_info']
    if not event_info or len(event_info) < 2:  # Need minimum history
        return 1.0
    
    # Build creation pattern: When are events typically created for each month?
    creation_patterns = {}  # {event_month: [creation_months]}
    lead_times_by_month = {}  # {event_month: [lead_days]}
    
    for event_id, info in event_info.items():
        if info.get('event_date') and info.get('first_booking'):
            event_date = pd.to_datetime(info['event_date'])
            first_booking = pd.to_datetime(info['first_booking'])
            lead_days = info.get('lead_days', 0)
            
            event_month = event_date.month
            creation_month = first_booking.month
            
            if event_month not in creation_patterns:
                creation_patterns[event_month] = []
                lead_times_by_month[event_month] = []
            
            creation_patterns[event_month].append(creation_month)
            lead_times_by_month[event_month].append(lead_days)
    
    if not creation_patterns:
        return 1.0
    
    # Current date analysis
    today = datetime.now(UK_TZ)
    current_month = today.month
    current_day = today.day
    
    # Determine if we're in a critical creation window
    multiplier = 1.0
    
    # Check each future month to see if we should be creating events now
    for months_ahead in range(0, 6):  # Look 6 months ahead
        future_date = today + timedelta(days=months_ahead * 30)
        future_month = future_date.month
        
        if future_month in creation_patterns:
            # When do they typically create events for this month?
            typical_creation_months = creation_patterns[future_month]
            typical_lead_days = lead_times_by_month[future_month]
            
            if not typical_lead_days:
                continue
                
            # Calculate when they should be creating this event
            avg_lead = sum(typical_lead_days) / len(typical_lead_days)
            expected_creation_date = future_date - timedelta(days=avg_lead)
            
            # Are we in the creation window?
            days_until_creation = (expected_creation_date - today).days
            
            if -14 <= days_until_creation <= 30:  # Within creation window (+/- buffer)
                # CHECK: Do they have an event for this period?
                has_current_event = False
                
                # Look for events in current period for this month
                # Handle year boundary (e.g., checking December events in January)
                target_year = future_date.year
                current_year_events = [
                    info for info in event_info.values()
                    if info.get('event_date') 
                    and pd.to_datetime(info['event_date']).year == target_year
                    and pd.to_datetime(info['event_date']).month == future_month
                ]
                
                has_current_event = len(current_year_events) > 0
                
                if not has_current_event:
                    # They should be creating but haven't!
                    if days_until_creation < 0:  # Past expected date
                        overdue_days = abs(days_until_creation)
                        if overdue_days > 30:
                            multiplier = max(multiplier, 1.5)  # Very late
                        elif overdue_days > 14:
                            multiplier = max(multiplier, 1.3)  # Late
                        else:
                            multiplier = max(multiplier, 1.1)  # Slightly late
                    else:
                        # Approaching creation window
                        multiplier = max(multiplier, 1.05)
    
    # Check if we're in a genuine quiet period
    # This is when no events are typically created OR held
    creation_months_set = set()
    event_months_set = set()
    
    for event_month, creation_months in creation_patterns.items():
        event_months_set.add(event_month)
        creation_months_set.update(creation_months)
    
    # If this month typically has no creation or event activity
    if current_month not in creation_months_set and current_month not in event_months_set:
        # But only reduce if they're not overdue for any events
        if multiplier == 1.0:  # No overdue events
            # Calculate how "quiet" this month typically is
            total_activity = len(creation_patterns)
            if total_activity > 12:  # Active account
                multiplier = 0.9  # Small reduction
            else:
                multiplier = 0.8  # Larger reduction for less active accounts
    
    # Industry seasonality check
    if all_accounts_df is not None and industry_lookup and multiplier < 1.0:
        industry = industry_lookup.get(account_id)
        if industry and industry != 'Unknown':
            # Simple check: Is the whole industry quiet?
            industry_mask = all_accounts_df['Account_Name'].apply(
                lambda x: industry_lookup.get(int(x), '') == industry
            )
            industry_cohort = all_accounts_df[industry_mask]
            
            if len(industry_cohort) >= 20:
                # What % of industry has current activity?
                # Check if Event_Count_Current exists in the dataframe
                if 'Event_Count_Current' in industry_cohort.columns:
                    active_pct = (industry_cohort['Event_Count_Current'] > 0).mean()
                elif '_event_count_current' in industry_cohort.columns:
                    active_pct = (industry_cohort['_event_count_current'] > 0).mean()
                else:
                    # Fallback: check if they have revenue
                    active_pct = (industry_cohort['revenue_current'] > 0).mean()
                
                if active_pct < 0.2:  # Less than 20% of industry active
                    multiplier *= 0.9  # Additional reduction
    
    return round(multiplier, 2)

def calculate_industry_seasonality_pattern(industry_cohort, booking_data):
    """
    Calculate industry-wide seasonal patterns for comparison
    Returns dict with monthly activity levels
    """
    # Aggregate booking data by month for entire industry
    industry_monthly = {}
    
    for account_id in industry_cohort['Account_Name']:
        account_bookings = booking_data[booking_data['AccountId'] == account_id]
        monthly_revenue = account_bookings.groupby(
            pd.to_datetime(account_bookings['TransactionDate']).dt.month
        )['Revenue'].sum()
        
        for month, revenue in monthly_revenue.items():
            industry_monthly[month] = industry_monthly.get(month, 0) + revenue
    
    # Convert to normalized pattern (0-1 scale)
    total_revenue = sum(industry_monthly.values())
    if total_revenue > 0:
        pattern = {m: v/total_revenue for m, v in industry_monthly.items()}
    else:
        pattern = {m: 1/12 for m in range(1, 13)}  # Uniform if no data
        
    return pattern

def calculate_critical_flags(row):
    """
    Binary flags for critical situations
    Returns multiplier 1.0-1.4
    """
    multiplier = 1.0
    
    # Complete revenue cessation
    if row.get('revenue_current', 0) == 0 and row.get('revenue_prev', 0) > 1000:
        multiplier *= 1.2
    
    # Tier collapse (3+ tiers)
    tier_order = ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']
    try:
        prev_idx = tier_order.index(row.get('Previous_Tier', 'NIL'))
        curr_idx = tier_order.index(row.get('Current_Tier', 'NIL'))
        tier_drop = max(0, curr_idx - prev_idx)
        if tier_drop >= 3:
            multiplier *= 1.15
    except ValueError:
        pass  # Invalid tier name
    
    return multiplier

def get_comparison_cohort(account_id, all_accounts_df, industry_lookup, sub_industry_lookup):
    """
    Strict cohort selection with 100+ requirement
    Returns (cohort_df, cohort_level)
    """
    MIN_COHORT_SIZE = 100
    
    # Try sub-industry (most specific)
    sub_industry = sub_industry_lookup.get(account_id)
    if sub_industry and sub_industry != 'Unknown':
        sub_cohort_mask = all_accounts_df['Account_Name'].apply(
            lambda x: sub_industry_lookup.get(int(x), 'Unknown') == sub_industry
        )
        sub_cohort = all_accounts_df[sub_cohort_mask]
        if len(sub_cohort) >= MIN_COHORT_SIZE:
            return sub_cohort, 'sub_industry'
    
    # Try industry
    industry = industry_lookup.get(account_id)
    if industry and industry != 'Unknown':
        ind_cohort_mask = all_accounts_df['Account_Name'].apply(
            lambda x: industry_lookup.get(int(x), 'Unknown') == industry
        )
        ind_cohort = all_accounts_df[ind_cohort_mask]
        if len(ind_cohort) >= MIN_COHORT_SIZE:
            return ind_cohort, 'industry'
    
    # Fall back to global (always has enough)
    return all_accounts_df, 'global'

def calculate_new_account_risk(row):
    """
    Simplified scoring for accounts without history
    Returns 0-50
    """
    # Base risk is low (they're new)
    risk = 10
    
    # Add risk if no current activity
    if row.get('revenue_current', 0) == 0:
        risk += 20
    
    # Add risk based on days since last activity
    days_inactive = row.get('_days_since_last', 0)
    if days_inactive > 90 and row.get('revenue_current', 0) == 0:
        risk += 20
    
    return min(50, risk)  # Cap at 50 for new accounts

def identify_temporal_clusters(event_dates, gap_days=30):
    """
    Group event dates into clusters where events within gap_days are considered part of same cluster
    """
    if len(event_dates) == 0:
        return []
    
    # Sort dates
    sorted_dates = sorted(event_dates)
    
    clusters = []
    current_cluster = [sorted_dates[0]]
    
    for date in sorted_dates[1:]:
        if (date - current_cluster[-1]).days <= gap_days:
            current_cluster.append(date)
        else:
            clusters.append(current_cluster)
            current_cluster = [date]
    
    clusters.append(current_cluster)
    return clusters

def calculate_event_frequency_v2(account_row, booking_data, first_event_created_lookup=None, last_event_created_lookup=None):
    """
    Enhanced event frequency calculation using creation dates and visible sales
    """
    account_id = account_row['Account_Name']
    account_id_int = int(account_id) if isinstance(account_id, (int, float, str)) else account_id
    
    # Try to get from lookups if not in row
    first_created = account_row.get('FirstEventCreated')
    last_created = account_row.get('LastEventCreated')
    
    if (pd.isna(first_created) or pd.isna(last_created)) and first_event_created_lookup and last_event_created_lookup:
        first_created = first_event_created_lookup.get(account_id_int)
        last_created = last_event_created_lookup.get(account_id_int)
    
    if pd.isna(first_created) or pd.isna(last_created):
        # No creation data available - fall back to booking data patterns
        if len(booking_data) > 0 and account_id_int in booking_data['AccountId'].values:
            # Use booking data to estimate pattern
            account_bookings = booking_data[booking_data['AccountId'] == account_id_int]
            if 'EventDate' in account_bookings.columns:
                event_dates = pd.to_datetime(account_bookings['EventDate'].dropna()).unique()
            else:
                event_dates = pd.to_datetime(account_bookings['TransactionDate'].dropna()).unique()
            
            if len(event_dates) > 0:
                # Estimate pattern from visible data
                date_range = (event_dates.max() - event_dates.min()).days
                if date_range > 0:
                    clusters = identify_temporal_clusters(event_dates, gap_days=30)
                    clusters_per_year = len(clusters) / max(date_range / 365, 1)
                    months_with_events = len(pd.DatetimeIndex(event_dates).tz_localize(None).to_period('M').unique())
                    
                    if clusters_per_year <= 1.5:
                        pattern = 'Annual'
                    elif clusters_per_year <= 4:
                        pattern = 'Seasonal'
                    elif clusters_per_year >= 6:
                        pattern = 'Regular'
                    else:
                        pattern = 'Occasional'
                    
                    return {
                        'pattern': pattern,
                        'clusters_per_year': clusters_per_year,
                        'months_active': len(pd.DatetimeIndex(event_dates).tz_localize(None).to_period('M').unique()),
                        'typical_months': pd.DatetimeIndex(event_dates).tz_localize(None).month.value_counts().head(3).index.tolist(),
                        'days_since_created': 999,  # Unknown
                        'days_since_visible': (datetime.now(UK_TZ) - event_dates.max()).days
                    }
        
        return {
            'pattern': 'Unknown',
            'clusters_per_year': 0,
            'months_active': 0,
            'typical_months': [],
            'days_since_created': 999,
            'days_since_visible': 999
        }
    
    account_age_days = (last_created - first_created).days
    
    # Get visible events (with sales)
    visible_events = booking_data[booking_data['AccountId'] == int(account_id)] if len(booking_data) > 0 else pd.DataFrame()
    
    if len(visible_events) == 0:
        # They create events but sell nothing
        return {
            'pattern': 'Struggling',
            'visible_clusters': 0,
            'creation_active': (datetime.now(UK_TZ) - last_created).days < 90,
            'days_since_created': (datetime.now(UK_TZ) - last_created).days,
            'days_since_visible': 999
        }
    
    # Identify event clusters from sales data
    if 'EventDate' in visible_events.columns:
        event_dates = pd.to_datetime(visible_events['EventDate'].dropna()).unique()
        clusters = identify_temporal_clusters(event_dates, gap_days=30)
    else:
        # Fallback to transaction dates
        event_dates = pd.to_datetime(visible_events['TransactionDate'].dropna()).unique()
        clusters = identify_temporal_clusters(event_dates, gap_days=30)
    
    # Calculate patterns
    clusters_per_year = len(clusters) / max(account_age_days / 365, 1)
    # Remove timezone info before converting to periods to avoid warning
    months_with_events = len(pd.DatetimeIndex(event_dates).tz_localize(None).to_period('M').unique())
    
    # Pattern classification
    if clusters_per_year <= 1.5:
        pattern = 'Annual'
    elif clusters_per_year <= 4 and months_with_events <= 6:
        pattern = 'Seasonal'  
    elif clusters_per_year >= 6:
        pattern = 'Regular'
    else:
        pattern = 'Occasional'
    
    return {
        'pattern': pattern,
        'clusters_per_year': clusters_per_year,
        'months_active': months_with_events,
        'typical_months': pd.DatetimeIndex(event_dates).tz_localize(None).month.value_counts().head(3).index.tolist(),
        'days_since_created': (datetime.now(UK_TZ) - last_created).days,
        'days_since_visible': (datetime.now(UK_TZ) - event_dates.max()).days if len(event_dates) > 0 else 999
    }

def calculate_revenue_at_risk(row, risk_result):
    """
    Calculate expected revenue loss based on pattern and current performance
    """
    revenue_current = row.get('revenue_current', 0)
    revenue_previous = row.get('revenue_prev', 0)
    pattern = risk_result.get('pattern', 'Unknown')
    risk_score = risk_result.get('score', 0)
    
    # Base expectation on better of current or previous
    base_revenue = max(revenue_current, revenue_previous)
    
    if base_revenue == 0:
        return 0
    
    # Pattern-based time horizon
    if pattern == 'Annual':
        # Full year at risk
        revenue_at_risk = base_revenue
    elif pattern == 'Seasonal':
        # Next 2 seasons at risk
        revenue_at_risk = base_revenue * 0.5
    elif pattern == 'Regular':
        # Next quarter at risk
        revenue_at_risk = base_revenue * 0.25
    else:
        # Conservative estimate
        revenue_at_risk = base_revenue * 0.33
    
    # Adjust by probability (risk score)
    probability = risk_score / 100
    expected_loss = revenue_at_risk * probability
    
    return round(expected_loss)

def calculate_priority_score(row, risk_result):
    """
    Combine risk score with revenue impact for prioritization
    Higher score = higher priority for intervention
    """
    risk_score = risk_result.get('score', 0)
    revenue_at_risk = calculate_revenue_at_risk(row, risk_result)
    
    # Normalize revenue to 0-100 scale (£100k = 100)
    revenue_factor = min(revenue_at_risk / 1000, 100)
    
    # Weight risk and revenue equally
    priority = (risk_score * 0.5) + (revenue_factor * 0.5)
    
    # Boost for certain critical factors
    risk_factors = risk_result.get('factors', [])
    if 'no_creation_critical' in risk_factors:
        priority *= 1.2
    if 'severe_decline_acceleration' in risk_factors:
        priority *= 1.15
    if 'longtime_customer_at_risk' in risk_factors:
        priority *= 1.1
    
    return round(min(priority, 100))

def get_weighted_creation_months(account_id, booking_data):
    """
    Identify when accounts create events, weighted by event volume and revenue
    """
    import calendar
    
    if len(booking_data) == 0:
        return []
    
    account_events = booking_data[booking_data['AccountId'] == int(account_id)]
    if len(account_events) == 0:
        return []
    
    # Parse event dates
    if 'EventDate' in account_events.columns:
        account_events = account_events.copy()
        account_events['EventDate'] = pd.to_datetime(account_events['EventDate'], errors='coerce')
        account_events = account_events.dropna(subset=['EventDate'])
    else:
        return []
    
    if len(account_events) == 0:
        return []
    
    # Calculate total revenue per event
    fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    existing_fee_cols = [col for col in fee_columns if col in account_events.columns]
    if existing_fee_cols:
        account_events['TotalRevenue'] = account_events[existing_fee_cols].sum(axis=1)
    else:
        account_events['TotalRevenue'] = 0
    
    # Group by event month to get volume and revenue
    account_events['EventMonth'] = account_events['EventDate'].dt.to_period('M')
    monthly_stats = account_events.groupby(['EventMonth', 'EventId']).agg({
        'TicketQuantity': 'sum',
        'TotalRevenue': 'sum'
    }).reset_index()
    
    # Count unique events per month
    events_per_month = monthly_stats.groupby('EventMonth').agg({
        'EventId': 'nunique',
        'TicketQuantity': 'sum',
        'TotalRevenue': 'sum'
    })
    
    # Calculate when these were likely created (2-3 months prior)
    creation_months = {}
    for event_month, stats in events_per_month.iterrows():
        # Estimate creation month (2 months before event)
        creation_date = event_month.to_timestamp() - pd.DateOffset(months=2)
        creation_month = creation_date.month
        
        if creation_month not in creation_months:
            creation_months[creation_month] = {
                'event_count': 0,
                'total_tickets': 0,
                'total_revenue': 0
            }
        
        creation_months[creation_month]['event_count'] += stats['EventId']
        creation_months[creation_month]['total_tickets'] += stats['TicketQuantity']
        creation_months[creation_month]['total_revenue'] += stats['TotalRevenue']
    
    # Calculate importance weights
    total_events = sum(m['event_count'] for m in creation_months.values())
    total_revenue = sum(m['total_revenue'] for m in creation_months.values())
    
    # Calculate years of data
    date_range = account_events['EventDate'].max() - account_events['EventDate'].min()
    years_of_data = max(date_range.days / 365, 1)
    
    typical_months = []
    for month, stats in creation_months.items():
        event_weight = stats['event_count'] / total_events if total_events > 0 else 0
        revenue_weight = stats['total_revenue'] / total_revenue if total_revenue > 0 else 0
        
        # Combined importance (70% events, 30% revenue)
        importance = (event_weight * 0.7) + (revenue_weight * 0.3)
        
        # Only include months with >5% importance
        if importance > 0.05:
            typical_months.append({
                'month': month,
                'month_name': calendar.month_name[month],
                'event_count': stats['event_count'],
                'avg_events_per_year': stats['event_count'] / years_of_data,
                'importance': importance,
                'revenue_share': revenue_weight,
                'event_share': event_weight
            })
    
    return sorted(typical_months, key=lambda x: x['importance'], reverse=True)

def check_missed_creation_windows(typical_months, last_created_date, current_date, pattern='Unknown'):
    """
    Check if account has missed any typical creation windows
    """
    if not typical_months or pd.isna(last_created_date):
        return []
    
    missed_windows = []
    
    for month_info in typical_months:
        month = month_info['month']
        avg_events_per_year = month_info['avg_events_per_year']
        
        # Determine expected frequency
        if avg_events_per_year >= 0.8:  # Nearly annual
            check_years = 1
        elif avg_events_per_year >= 0.4:  # Every 2-3 years
            check_years = 2
        else:  # Less frequent
            check_years = 3
        
        # Check recent years
        for years_back in range(check_years):
            expected_year = current_date.year - years_back
            expected_date = datetime(expected_year, month, 15, tzinfo=UK_TZ)
            
            # Have we passed this date?
            if current_date > expected_date:
                # When should they have created? (2-3 months before)
                if pattern in ['Annual', 'Seasonal']:
                    lead_time_days = 90  # 3 months
                elif pattern == 'Regular':
                    lead_time_days = 30  # 1 month
                else:  # Occasional
                    lead_time_days = 60  # 2 months
                
                expected_creation = expected_date - timedelta(days=lead_time_days)
                
                # Did they create for this window?
                if last_created_date < expected_creation:
                    months_overdue = (current_date - expected_date).days / 30.44  # avg days per month
                    
                    # Only flag if significantly overdue
                    if months_overdue > 0.5:  # At least 2 weeks past event date
                        missed_windows.append({
                            'month': month,
                            'month_name': month_info['month_name'],
                            'months_overdue': months_overdue,
                            'expected_date': expected_date,
                            'importance': month_info['importance'],
                            'event_share': month_info['event_share']
                        })
                        break  # Only count once per month
    
    return missed_windows

def calculate_weighted_window_risk(missed_windows, typical_months):
    """
    Calculate risk based on importance of missed windows
    """
    if not missed_windows:
        return 0, []
    
    total_risk = 0
    risk_details = []
    
    for window in missed_windows:
        months_overdue = window['months_overdue']
        importance = window['importance']
        
        # Base risk from timing
        if months_overdue >= 3:
            base_risk = 40  # Very critical
        elif months_overdue >= 1.5:
            base_risk = 25  # Critical
        elif months_overdue >= 0.5:
            base_risk = 15  # High risk
        else:
            base_risk = 5   # Warning
        
        # Weight by importance (capped multiplier to avoid extreme scores)
        importance_multiplier = min(1.5, 0.5 + importance)
        weighted_risk = base_risk * importance_multiplier
        
        total_risk += weighted_risk
        
        risk_details.append({
            'month': window['month_name'],
            'importance': importance,
            'months_overdue': months_overdue,
            'risk_contribution': weighted_risk,
            'event_share': window['event_share']
        })
    
    # Sort by risk contribution
    risk_details.sort(key=lambda x: x['risk_contribution'], reverse=True)
    
    # Cap total risk but ensure significant windows score high
    return min(45, total_risk), risk_details

def validate_row_data(row):
    """
    Ensure data is valid before scoring
    Returns (is_valid, error_message)
    """
    required_fields = [
        'revenue_current', 'revenue_prev',
        'Event_Frequency_Current', 'Event_Frequency_Previous',
        'Current_Tier', 'Previous_Tier'
    ]
    
    for field in required_fields:
        if field not in row or pd.isna(row[field]):
            return False, f"Missing required field: {field}"
    
    # Validate numeric fields
    numeric_fields = ['revenue_current', 'revenue_prev', 'tickets_current', 'tickets_prev']
    for field in numeric_fields:
        if field in row and row[field] < 0:
            return False, f"Negative value for {field}: {row[field]}"
    
    return True, "Valid"

def calculate_churn_risk_final(account_row, booking_data, all_accounts_df, 
                              first_event_created_lookup=None, last_event_created_lookup=None):
    """
    Revenue-focused churn risk with creation insights
    Returns dict with score and details
    """
    try:
        account_id = account_row['Account_Name']
        
        # Get pattern analysis
        pattern_info = calculate_event_frequency_v2(account_row, booking_data, 
                                                   first_event_created_lookup, last_event_created_lookup)
        
        # Initialize risk components
        risk_score = 0
        risk_factors = []
        
        # TIER 1: Creation Activity (40 points max)
        # This is the strongest signal - have they stopped creating events?
        
        days_since_created = pattern_info['days_since_created']
        pattern = pattern_info['pattern']
        
        # Pattern-specific thresholds for creation gaps
        creation_thresholds = {
            'Annual': {'warning': 180, 'critical': 365},
            'Seasonal': {'warning': 90, 'critical': 180},
            'Regular': {'warning': 45, 'critical': 90},
            'Occasional': {'warning': 120, 'critical': 240},
            'Struggling': {'warning': 60, 'critical': 120},
            'Unknown': {'warning': 90, 'critical': 180}
        }
        
        thresholds = creation_thresholds.get(pattern, {'warning': 90, 'critical': 180})
        
        if days_since_created > thresholds['critical']:
            risk_score += 40
            risk_factors.append('no_creation_critical')
        elif days_since_created > thresholds['warning']:
            progress = (days_since_created - thresholds['warning']) / (thresholds['critical'] - thresholds['warning'])
            risk_score += 20 + (20 * progress)
            risk_factors.append('no_creation_warning')
        
        # TIER 2: Revenue Performance (35 points max)
        # Focus on decline rate, not absolute position
        
        revenue_current = account_row.get('revenue_current', 0)
        revenue_previous = account_row.get('revenue_prev', 0)
        
        # Absolute revenue decline (main component)
        if revenue_previous > 0:
            revenue_decline = (revenue_previous - revenue_current) / revenue_previous
            
            if revenue_decline >= 0.75:
                risk_score += 35
                risk_factors.append('severe_revenue_decline')
            elif revenue_decline >= 0.50:
                risk_score += 25
                risk_factors.append('major_revenue_decline')
            elif revenue_decline >= 0.25:
                risk_score += 15
                risk_factors.append('revenue_decline')
            elif revenue_decline > 0:
                risk_score += revenue_decline * 30  # Scale 0-25% decline to 0-7.5 points
        else:
            revenue_decline = 0
            # New account with no revenue history
            if revenue_current == 0:
                risk_score += 10
                risk_factors.append('no_revenue_activity')
        
        # TIER 3: Struggle Indicators (25 points max)
        # Are they trying but failing?
        
        creation_to_sale_gap = pattern_info['days_since_created'] - pattern_info['days_since_visible']
        
        if creation_to_sale_gap > 60:
            # They've created events recently but no sales in 60+ days
            risk_score += 25
            risk_factors.append('events_not_selling')
        elif creation_to_sale_gap > 30:
            risk_score += 15
            risk_factors.append('sales_struggling')
        
        # Free event detection - REMOVED as not a risk factor
        # Free events are a valid business model, not a churn indicator
        
        # Velocity decline
        if pattern in ['Regular', 'Seasonal']:
            expected_clusters = pattern_info['clusters_per_year']
            # Count recent clusters (last 90 days)
            recent_cutoff = datetime.now(UK_TZ) - timedelta(days=90)
            if len(booking_data) > 0 and int(account_id) in booking_data['AccountId'].values:
                recent_bookings = booking_data[
                    (booking_data['AccountId'] == int(account_id)) & 
                    (pd.to_datetime(booking_data['TransactionDate']) > recent_cutoff)
                ]
                if 'EventDate' in recent_bookings.columns:
                    recent_dates = pd.to_datetime(recent_bookings['EventDate'].dropna()).unique()
                else:
                    recent_dates = pd.to_datetime(recent_bookings['TransactionDate'].dropna()).unique()
                recent_clusters = len(identify_temporal_clusters(recent_dates, gap_days=30))
            else:
                recent_clusters = 0
            
            annualized_recent = recent_clusters * 4
            
            if expected_clusters > 0 and annualized_recent < expected_clusters * 0.5:
                risk_score += 10
                risk_factors.append('velocity_decline')
        
        # SIMPLIFIED MODIFIERS - Focus on behavior, not double-counting value
        
        # 2. DECLINE ACCELERATION TRACKING
        # Check if revenue decline is accelerating by comparing periods
        if account_row.get('revenue_prev', 0) > 0 and account_row.get('lifetime_revenue_prev', 0) > 0:
            # Calculate year-over-year decline rates
            current_decline_rate = revenue_decline  # Already calculated above
            
            # Need previous period's decline rate - estimate from lifetime data
            years_active = account_row.get('years_loyalty', 1)
            if years_active > 2:
                # Rough estimate of previous decline rate
                avg_historical_revenue = account_row.get('lifetime_revenue_prev', 0) / max(years_active - 1, 1)
                if avg_historical_revenue > 0:
                    previous_decline_rate = (avg_historical_revenue - revenue_previous) / avg_historical_revenue
                    
                    # Check if decline is accelerating
                    if current_decline_rate > 0 and previous_decline_rate < current_decline_rate:
                        acceleration = current_decline_rate - max(previous_decline_rate, 0)
                        
                        if acceleration > 0.3:  # 30%+ acceleration
                            risk_score += 20
                            risk_factors.append('severe_decline_acceleration')
                        elif acceleration > 0.15:  # 15%+ acceleration
                            risk_score += 10
                            risk_factors.append('decline_acceleration')
        
        # 3. INDUSTRY CONTEXT (NOT ADJUSTMENT)
        # Use industry trends to provide context, not to reduce risk
        industry = account_row.get('Industry', 'Unknown')
        if industry != 'Unknown' and len(all_accounts_df) > 20:
            # Get industry cohort
            industry_mask = all_accounts_df['Industry'] == industry
            industry_cohort = all_accounts_df[industry_mask]
            
            if len(industry_cohort) >= 10:  # Need minimum cohort size
                # Calculate industry-wide decline
                industry_avg_decline = 0
                decline_count = 0
                
                for _, ind_row in industry_cohort.iterrows():
                    if ind_row.get('revenue_prev', 0) > 0:
                        ind_decline = (ind_row['revenue_prev'] - ind_row['revenue_current']) / ind_row['revenue_prev']
                        if ind_decline > 0:
                            industry_avg_decline += ind_decline
                            decline_count += 1
                
                if decline_count > 0:
                    industry_avg_decline = industry_avg_decline / decline_count
                    
                    # CRITICAL CHANGE: Industry decline is contextual, not mitigating
                    # High-value accounts at risk remain at risk regardless of industry
                    
                    # If declining worse than industry average, that's additional risk
                    if revenue_decline > 0 and revenue_decline > industry_avg_decline + 0.2:
                        risk_score += 15
                        risk_factors.append(f'declining_worse_than_industry_{int(industry_avg_decline*100)}pct')
                    
                    # If industry is healthy but you're declining, that's concerning
                    elif industry_avg_decline < 0.1 and revenue_decline > 0.25:
                        risk_score += 10
                        risk_factors.append('declining_in_healthy_industry')
                    
                    # Note industry context for reporting (but don't reduce score)
                    if industry_avg_decline > 0.2:
                        risk_factors.append(f'industry_context_decline_{int(industry_avg_decline*100)}pct')
        
        # WEIGHTED TIER DROP - drops from higher tiers are more concerning
        tier_map = {'NIL': 0, 'Tier 1': 1, 'Tier 2': 2, 'Tier 3': 3, 'Tier 4': 4, 'High Value': 5, 'Key Account': 6}
        tier_drop_weights = {
            'Key Account': 3.0,      # Losing a Key Account is critical
            'High Value': 2.5,       # High Value drops are very concerning
            'Tier 4': 2.0,          # Upper tier drops matter
            'Tier 3': 1.5,          # Mid-tier drops are notable
            'Tier 2': 1.0,          # Lower tier drops less impactful
            'Tier 1': 0.5,          # Minimal accounts dropping
        }
        
        current_tier_val = tier_map.get(account_row.get('Current_Tier', 'NIL'), 0)
        previous_tier_val = tier_map.get(account_row.get('Previous_Tier', 'NIL'), 0)
        drop_distance = previous_tier_val - current_tier_val
        
        if drop_distance > 0:
            # Weight the drop based on starting position
            previous_tier = account_row.get('Previous_Tier', 'NIL')
            weight = tier_drop_weights.get(previous_tier, 1.0)
            
            # Calculate weighted risk (max 20 points for tier drops)
            tier_drop_score = min(20, drop_distance * weight * 3)
            
            # Only add if not already heavily penalized by revenue decline
            if revenue_decline < 0.5 or tier_drop_score > 10:
                risk_score += tier_drop_score
                
                if drop_distance >= 2:
                    risk_factors.append(f'major_tier_drop_from_{previous_tier}')
                else:
                    risk_factors.append(f'tier_drop_from_{previous_tier}')
        
        # VOLUME-WEIGHTED WINDOW DETECTION
        # Check if they've missed their typical event creation windows
        typical_months = get_weighted_creation_months(account_id, booking_data)
        
        if typical_months and pattern != 'Unknown':
            # Get last created date
            last_created = last_event_created_lookup.get(int(account_id)) if last_event_created_lookup else None
            if pd.isna(last_created) and 'LastEventCreated' in account_row:
                last_created = pd.to_datetime(account_row['LastEventCreated'], errors='coerce')
            
            if not pd.isna(last_created):
                # Check for missed windows
                missed_windows = check_missed_creation_windows(
                    typical_months, 
                    last_created,
                    datetime.now(UK_TZ),
                    pattern
                )
                
                if missed_windows:
                    # Calculate weighted risk based on importance
                    window_risk, risk_details = calculate_weighted_window_risk(missed_windows, typical_months)
                    
                    if window_risk > 0:
                        risk_score += window_risk
                        
                        # Add risk factors based on severity
                        if window_risk >= 30:
                            risk_factors.append('missed_critical_event_windows')
                        elif window_risk >= 20:
                            risk_factors.append('missed_important_event_windows')
                        else:
                            risk_factors.append('missed_event_windows')
                        
                        # Add details about the most critical missed window
                        if risk_details:
                            top_window = risk_details[0]
                            if top_window['importance'] > 0.5:
                                risk_factors.append(f"missed_{top_window['month']}_critical_{int(top_window['event_share']*100)}pct_events")
                            elif top_window['importance'] > 0.2:
                                risk_factors.append(f"missed_{top_window['month']}_{int(top_window['months_overdue'])}mo_overdue")
        
        # Long-term customer modifier (keep existing)
        if account_row.get('Years_Loyalty', 0) > 5 and risk_score > 50:
            risk_factors.append('longtime_customer_at_risk')
        
        # VALUE-BASED ADJUSTMENTS - Percentile-based protection
        annual_revenue = revenue_current + revenue_previous
        
        # Calculate revenue percentile across all accounts
        if len(all_accounts_df) > 100:
            # Use current revenue for percentile (what they're worth now)
            revenue_percentile = stats.percentileofscore(
                all_accounts_df['revenue_current'].fillna(0), 
                revenue_current
            )
            
            # Top percentile accounts get minimum risk scores when behavior is bad
            if revenue_percentile >= 90:  # Top 10%
                if days_since_created > thresholds['critical']:
                    if risk_score < 80:
                        risk_score = 80
                        risk_factors.append('top_10pct_revenue_critical')
                elif days_since_created > thresholds['warning']:
                    if risk_score < 65:
                        risk_score = 65
                        risk_factors.append('top_10pct_revenue_warning')
            
            elif revenue_percentile >= 75:  # Top 25%
                if days_since_created > thresholds['critical']:
                    if risk_score < 70:
                        risk_score = 70
                        risk_factors.append('top_25pct_revenue_critical')
                elif days_since_created > thresholds['warning']:
                    if risk_score < 55:
                        risk_score = 55
                        risk_factors.append('top_25pct_revenue_warning')
            
            # Bottom quartile penalty (existing behavior)
            if revenue_percentile < 25 and revenue_current > 0:
                risk_score += 5
                risk_factors.append('bottom_quartile_revenue')
        
        # Cap score at 100
        final_score = min(100, round(risk_score))
        
        return {
            'score': final_score,
            'factors': risk_factors,
            'pattern': pattern,
            'days_since_created': days_since_created,
            'revenue_decline_pct': revenue_decline if revenue_previous > 0 else 0
        }
        
    except Exception as e:
        account_name = account_row.get('Account_Name', 'Unknown')
        # Only show first error in detail to avoid spam
        if not hasattr(calculate_churn_risk_final, '_error_shown'):
            print(f"\n  ERROR calculating churn risk: {type(e).__name__}: {str(e)}")
            print(f"  First error was for account {account_name}")
            import traceback
            traceback.print_exc()
            calculate_churn_risk_final._error_shown = True
        return {
            'score': 50,  # Default middle risk on error
            'factors': ['calculation_error'],
            'pattern': 'Unknown',
            'days_since_created': 999,
            'revenue_decline_pct': 0
        }

def determine_activity_rating(current_freq, previous_freq, days_since_last, has_historical, avg_lead_days=60, last_event_date=None):
    """Determine activity rating based on event patterns and creation lead times"""
    # Active if any current activity
    if current_freq != "Inactive":
        return "Active"
    
    # New account (no previous activity ever)
    if previous_freq == "Inactive" and not has_historical:
        return "New"
    
    # Returned (was inactive in previous period but now active)
    if current_freq != "Inactive" and previous_freq == "Inactive" and has_historical:
        return "Returned"
    
    # No current activity - determine if At Risk, Churned, or New
    if current_freq == "Inactive":
        # For annual/occasional events, use creation lead time logic
        if previous_freq in ["Annual", "Occasional"] and last_event_date:
            # Calculate when they should have created their next event
            expected_next_event = last_event_date + pd.Timedelta(days=365)
            expected_creation_date = expected_next_event - pd.Timedelta(days=avg_lead_days)
            days_past_expected_creation = (pd.Timestamp.now().date() - expected_creation_date).days
            
            # At Risk if past expected creation but not too far past
            if 0 < days_past_expected_creation <= 90:
                return "At Risk"
            elif days_past_expected_creation > 90:
                return "Churned"
            else:
                # Not yet time to create next event
                return "Active"
        
        # For regular events or if no previous activity
        if previous_freq != "Inactive":
            # Had activity before, now inactive
            # Use their typical creation lead time + 14 days as the at-risk threshold
            at_risk_threshold = avg_lead_days + 14
            if days_since_last < at_risk_threshold:
                return "At Risk"
            else:
                return "Churned"
        else:
            # No activity in current or previous periods
            return "New"
    
    # Default - should never reach here but return New to avoid "Inactive"
    return "New"

# === METRICS CALC ===
def calculate_metrics_from_aggregated(account_metrics, industry_lookup=None, sub_industry_lookup=None, 
                                    first_event_created_lookup=None, last_event_created_lookup=None,
                                    all_booking_data=None):
    """Calculate metrics from pre-aggregated account data"""
    print("\nCalculating metrics for accounts...")
    
    # Initialize lookups if not provided
    if industry_lookup is None:
        industry_lookup = {}
    if sub_industry_lookup is None:
        sub_industry_lookup = {}
    if first_event_created_lookup is None:
        first_event_created_lookup = {}
    if last_event_created_lookup is None:
        last_event_created_lookup = {}
    if all_booking_data is None:
        all_booking_data = pd.DataFrame()
    
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
        
        # Include event tracking data
        event_data = {
            'event_ids_current': data.get('event_ids_current', set()),
            'event_ids_previous': data.get('event_ids_previous', set()),
            'event_creation_info': data.get('event_creation_info', {}),
            'last_booking_date': data.get('last_booking_date')
        }
        
        # Add industry data
        industry = industry_lookup.get(account_id, 'Unknown')
        sub_industry = sub_industry_lookup.get(account_id, 'Unknown')
        
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
            'has_activity': tickets_current >= 10,
            'Industry': industry,
            'SubIndustry': sub_industry,
            '_event_data': event_data  # Store for later use
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
    print("\nAssigning tiers and calculating new metrics...")
    print(f"Total accounts to process: {len(metrics_df)}")
    
    # Debug: Check if event data is present
    sample_row = metrics_df.iloc[0] if len(metrics_df) > 0 else None
    if sample_row is not None:
        print(f"Sample row has _event_data: {'_event_data' in sample_row}")
        if '_event_data' in sample_row:
            event_data_sample = sample_row['_event_data']
            print(f"  Event IDs current: {len(event_data_sample.get('event_ids_current', set()))}")
            print(f"  Event IDs previous: {len(event_data_sample.get('event_ids_previous', set()))}")
            # Show actual event IDs for verification
            curr_ids = list(event_data_sample.get('event_ids_current', set()))[:5]
            prev_ids = list(event_data_sample.get('event_ids_previous', set()))[:5]
            print(f"  Sample current event IDs: {curr_ids}")
            print(f"  Sample previous event IDs: {prev_ids}")
    
    results = []
    event_freq_summary = {'Regular': 0, 'Occasional': 0, 'Annual': 0, 'Inactive': 0}
    
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
        
        # Calculate new metrics from stored event data
        event_data = row.get('_event_data', {})
        
        # Get event counts
        event_count_current = len(event_data.get('event_ids_current', set()))
        event_count_previous = len(event_data.get('event_ids_previous', set()))
        event_creation_info = event_data.get('event_creation_info', {})
        has_historical = len(event_creation_info) > event_count_current + event_count_previous
        
        # Calculate event frequency
        event_freq_current = classify_event_frequency(event_count_current)
        event_freq_previous = classify_event_frequency(event_count_previous)
        event_freq_summary[event_freq_current] += 1
        
        # Calculate lead times
        lead_times = [info['lead_days'] for info in event_creation_info.values() if info['lead_days'] > 0]
        avg_lead_days = int(sum(lead_times) / len(lead_times)) if lead_times else 60
        
        # Calculate days since last activity
        last_booking = event_data.get('last_booking_date')
        days_since_last = (TODAY - last_booking.date()).days if last_booking else 999
        
        # Add required fields to row for bulletproof model
        row['_days_since_last'] = days_since_last
        row['Event_Frequency_Current'] = event_freq_current
        row['Event_Frequency_Previous'] = event_freq_previous
        row['Current_Tier'] = tier_current
        row['Previous_Tier'] = tier_prev
        
        # Add event creation dates from Accounts data
        account_id_int = int(account_name)
        row['FirstEventCreated'] = first_event_created_lookup.get(account_id_int)
        row['LastEventCreated'] = last_event_created_lookup.get(account_id_int)
        
        # Get last event date
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        last_event_date = max(event_dates).date() if event_dates else None
        
        # Determine activity rating with lead time consideration
        activity_rating = determine_activity_rating(
            event_freq_current, event_freq_previous, days_since_last, has_historical,
            avg_lead_days=avg_lead_days, last_event_date=last_event_date
        )
        
        # Calculate enhanced churn risk
        risk_result = calculate_churn_risk_final(
            row, all_booking_data, metrics_df,
            first_event_created_lookup, last_event_created_lookup
        )
        churn_prob = risk_result['score']
            
        results.append({
            "Account_Name": account_name,
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(row['tickets_current']),
            "Last_Year_Ticket_Quantity": int(row['tickets_prev']),
            "Years_Loyalty": row['years_loyalty'],
            "Event_Frequency_Current": event_freq_current,
            "Event_Frequency_Previous": event_freq_previous,
            "Event_Count_Current": event_count_current,  # Add actual count
            "Event_Count_Previous": event_count_previous,  # Add actual count
            "Rating": activity_rating,  # Changed from Activity_Rating to match Zoho field name
            "Churn_Risk": int(churn_prob),  # 0-100 score
            # Hidden fields for report generation (prefix with _)
            "_avg_lead_days": avg_lead_days,
            "_last_event_date": last_event_date,
            "_event_count_current": event_count_current,
            "_event_count_previous": event_count_previous,
            "_revenue_current": row.get('revenue_current', 0),
            "_revenue_previous": row.get('revenue_prev', 0),
            "_risk_factors": ','.join(risk_result.get('factors', [])),
            "_event_pattern": risk_result.get('pattern', 'Unknown'),
            "_days_since_created": risk_result.get('days_since_created', 999),
            "_revenue_at_risk": calculate_revenue_at_risk(row, risk_result),
            "_priority_score": calculate_priority_score(row, risk_result)
        })
    
    # Print event frequency summary
    print("\nEvent Frequency Summary:")
    for freq_type, count in event_freq_summary.items():
        print(f"  {freq_type}: {count:,} accounts")
    
    # Activity rating summary
    results_df = pd.DataFrame(results)
    if not results_df.empty:
        rating_counts = results_df['Rating'].value_counts()
        print("\nActivity Rating Summary:")
        for rating in ['Active', 'At Risk', 'Churned', 'Returned', 'New']:
            count = rating_counts.get(rating, 0)
            print(f"  {rating}: {count:,} accounts")
        
        # Cohort usage analysis
        print("\nCohort Analysis:")
        if industry_lookup:
            # Count accounts by industry
            industry_counts = pd.Series([industry_lookup.get(int(acc), 'Unknown') 
                                       for acc in results_df['Account_Name']]).value_counts()
            print(f"  Total industries: {len(industry_counts)}")
            print("  Largest industries:")
            for industry, count in industry_counts.head(5).items():
                print(f"    {industry}: {count:,} accounts")
            
            # Check which cohorts would be used
            cohort_usage = {'sub_industry': 0, 'industry': 0, 'global': 0}
            for _, row in results_df.iterrows():
                account_id = int(row['Account_Name'])
                cohort, level = get_comparison_cohort(account_id, results_df, industry_lookup, sub_industry_lookup)
                cohort_usage[level] += 1
            
            print("\n  Cohort level usage:")
            for level, count in cohort_usage.items():
                pct = count / len(results_df) * 100 if len(results_df) > 0 else 0
                print(f"    {level}: {count:,} accounts ({pct:.1f}%)")
        
        # Churn risk analysis
        print("\nChurn Risk Analysis:")
        risk_bands = {
            'Healthy (0-20)': results_df[results_df['Churn_Risk'] <= 20],
            'Low Risk (21-40)': results_df[(results_df['Churn_Risk'] > 20) & (results_df['Churn_Risk'] <= 40)],
            'Moderate Risk (41-60)': results_df[(results_df['Churn_Risk'] > 40) & (results_df['Churn_Risk'] <= 60)],
            'High Risk (61-80)': results_df[(results_df['Churn_Risk'] > 60) & (results_df['Churn_Risk'] <= 80)],
            'Critical (81-100)': results_df[results_df['Churn_Risk'] > 80]
        }
        
        total_revenue_at_risk = 0
        for band, accounts in risk_bands.items():
            count = len(accounts)
            current_revenue = accounts['_revenue_current'].sum() if '_revenue_current' in accounts.columns else 0
            expected_loss = accounts['_revenue_at_risk'].sum() if '_revenue_at_risk' in accounts.columns else 0
            total_revenue_at_risk += expected_loss
            print(f"  {band}: {count:,} accounts (£{current_revenue:,.0f} current, £{expected_loss:,.0f} at risk)")
        
        print(f"\n  Total Expected Revenue Loss: £{total_revenue_at_risk:,.0f}")
        
        # Top priority accounts
        print("\nTop 10 Priority Accounts (by revenue impact):")
        top_priority = results_df.nlargest(10, '_priority_score')
        for _, acc in top_priority.iterrows():
            factors = acc.get('_risk_factors', '').split(',')[:2]  # Top 2 factors
            print(f"  {acc['Account_Name']}: Score={acc['Churn_Risk']}, Priority={acc.get('_priority_score', 0)}, "
                  f"Revenue@Risk=£{acc.get('_revenue_at_risk', 0):,.0f}, Factors={', '.join(factors)}")
        
        # Score distribution statistics
        print("\nScore Distribution:")
        churn_scores = results_df['Churn_Risk']
        print(f"  Mean: {churn_scores.mean():.1f}")
        print(f"  Median: {churn_scores.median():.1f}")
        print(f"  Std Dev: {churn_scores.std():.1f}")
        print(f"  Min: {churn_scores.min()}")
        print(f"  Max: {churn_scores.max()}")
        
        # Validate score distribution
        if churn_scores.mean() < 15 or churn_scores.mean() > 45:
            print("  WARNING: Mean score outside expected range (15-45)")
        if churn_scores.std() < 10:
            print("  WARNING: Low standard deviation - scores may be too clustered")
    
    return results_df

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
        # Show all the fields including new ones
        display_cols = ['Account_Name', 'Current_Tier', 'Previous_Tier', 'Event_Frequency_Current', 
                       'Event_Frequency_Previous', 'Event_Count_Current', 'Event_Count_Previous',
                       'Rating', 'Churn_Risk', 'Ticket_Quantity']
        print(records_df[display_cols].head(10))
        print(f"\nTotal accounts to update: {len(records_df)}")
        print(f"Columns being sent to Zoho: {list(records_df.columns)}")
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

# === AT RISK ACCOUNTS REPORT ===
def generate_at_risk_accounts_report(results_df):
    """Generate report for at risk accounts needing immediate attention"""
    
    # Filter for high risk accounts (>50 churn risk) with meaningful revenue
    MIN_REVENUE = 50  # £50 minimum to focus on valuable accounts
    
    high_risk_accounts = results_df[
        (results_df['Churn_Risk'] > 50) &
        ((results_df.get('_revenue_current', 0) >= MIN_REVENUE) | 
         (results_df.get('_revenue_previous', 0) >= MIN_REVENUE))
    ].copy()
    
    # Sort by priority score (combines risk with revenue impact)
    high_risk_accounts = high_risk_accounts.sort_values('_priority_score', ascending=False)
    
    # Prepare report data
    report_data = []
    for _, account in high_risk_accounts.iterrows():
        # Parse risk factors for display
        risk_factors_str = account.get('_risk_factors', '')
        risk_factors = risk_factors_str.split(',') if risk_factors_str else []
        
        # Identify key risk reasons
        key_risks = []
        if 'no_creation_critical' in risk_factors:
            key_risks.append('No Event Creation')
        if 'severe_revenue_decline' in risk_factors:
            key_risks.append('Severe Revenue Decline')
        if 'severe_decline_acceleration' in risk_factors:
            key_risks.append('Accelerating Decline')
        if 'events_not_selling' in risk_factors:
            key_risks.append('Events Not Selling')
        if 'major_tier_drop' in risk_factors:
            key_risks.append('Major Tier Drop')
        
        report_data.append({
            'Account_Name': account['Account_Name'],
            'Priority_Score': account.get('_priority_score', 0),
            'Churn_Risk_Score': account['Churn_Risk'],
            'Revenue_At_Risk': round(account.get('_revenue_at_risk', 0)),
            'Risk_Level': 'Critical' if account['Churn_Risk'] > 75 else 'High',
            'Key_Risks': ', '.join(key_risks[:3]),  # Top 3 risks
            'Event_Pattern': account.get('_event_pattern', account['Event_Frequency_Current']),
            'Days_Since_Created': account.get('_days_since_created', 999),
            'Current_Tier': account['Current_Tier'],
            'Last_Year_Revenue': round(account.get('_revenue_previous', 0), 2),
            'Current_Revenue': round(account.get('_revenue_current', 0), 2),
            'Revenue_Change': round(((account.get('_revenue_current', 0) - account.get('_revenue_previous', 0)) / account.get('_revenue_previous', 0) * 100), 0) if account.get('_revenue_previous', 0) > 0 else 0,
            'Last_Year_Events': account.get('_event_count_previous', 0),
            'Current_Events': account.get('_event_count_current', 0)
        })
    
    return pd.DataFrame(report_data)

# === ANNUAL EVENTS REPORT ===
def generate_upcoming_annual_events_report(results_df):
    """Generate report for annual events needing outreach"""
    
    # Filter for annual pattern accounts with minimum revenue
    MIN_REVENUE = 100  # £100 minimum revenue threshold
    
    annual_accounts = results_df[
        ((results_df['Event_Frequency_Current'] == 'Annual') | 
         (results_df['Event_Frequency_Previous'] == 'Annual')) &
        ((results_df['_revenue_current'] >= MIN_REVENUE) | 
         (results_df['_revenue_previous'] >= MIN_REVENUE))
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
                last_revenue = account.get('_revenue_previous', 0) if account['Event_Frequency_Previous'] == 'Annual' else account.get('_revenue_current', 0)
                
                upcoming.append({
                    'Account_Name': account['Account_Name'],
                    'Tier': account['Current_Tier'],
                    'Last_Event_Date': last_event.strftime('%d/%m/%Y'),
                    'Expected_Event_Date': predicted_event_date.strftime('%d/%m/%Y'),
                    'Typical_Creation_Lead_Days': lead_days,
                    'Reach_Out_By': outreach_date.strftime('%d/%m/%Y'),
                    'Last_Year_Tickets': int(account['Last_Year_Ticket_Quantity']),
                    'Last_Year_Revenue': round(last_revenue, 2),
                    'Status': account['Rating']
                })
    
    return pd.DataFrame(upcoming).sort_values('Reach_Out_By') if upcoming else pd.DataFrame()

# === MAIN ===
def main():
    import time
    start_time = time.time()
    
    print(f"\n=== Zoho Tier Update Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    
    # Determine report date
    # If running on the 1st, use previous month's data
    # Otherwise, use current month's data
    today = pd.Timestamp.now(UK_TZ).normalize()
    if today.day == 1:
        # Use last day of previous month
        report_date = today - pd.Timedelta(days=1)
    else:
        # Use current month
        report_date = today

    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")

    print(f"Processing data for: {report_date.strftime('%Y-%m-%d')}")
    
    # S3 keys
    key_all = f"{year}/{month}/{prefix}01-BookingDataAll-TBUK.csv"
    key_month = f"{year}/{month}/{prefix}-BookingData-TBUK.csv"
    key_accounts = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Fetch Accounts data for industry information
        print("\nFetching Accounts data for industry classification...")
        try:
            accounts_obj = s3_client.get_object(Bucket=BUCKET, Key=key_accounts)
            accounts_df = pd.read_csv(accounts_obj['Body'])
            print(f"  Loaded {len(accounts_df)} accounts with industry data")
            
            # Debug: Check available columns
            print(f"  Account columns: {', '.join(accounts_df.columns[:20])}")
            
            # The columns are FirstEventCreation and LastEventCreation (no 'd')
            has_first = 'FirstEventCreation' in accounts_df.columns
            has_last = 'LastEventCreation' in accounts_df.columns
            print(f"  FirstEventCreation in columns: {has_first}")
            print(f"  LastEventCreation in columns: {has_last}")
            
            if has_first:
                print(f"  FirstEventCreation non-null: {accounts_df['FirstEventCreation'].notna().sum()}")
            if has_last:
                print(f"  LastEventCreation non-null: {accounts_df['LastEventCreation'].notna().sum()}")
            
            # Create lookup dictionaries for O(1) access
            # The Accounts report uses 'Id' not 'AccountId'
            industry_lookup = dict(zip(accounts_df['Id'].astype(int), accounts_df['Industry'].fillna('Unknown')))
            sub_industry_lookup = dict(zip(accounts_df['Id'].astype(int), accounts_df['SubIndustry'].fillna('Unknown')))
            
            # Add creation date lookups with proper timezone handling
            # The columns are FirstEventCreation and LastEventCreation (no 'd' at the end)
            print("\n  Parsing event creation dates...")
            
            # Parse dates - they are in the format YYYY-MM-DD HH:MM:SS
            if 'FirstEventCreation' in accounts_df.columns:
                first_dates = pd.to_datetime(accounts_df['FirstEventCreation'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
                print(f"    Parsed {first_dates.notna().sum()} FirstEventCreation dates")
            else:
                first_dates = pd.Series([pd.NaT] * len(accounts_df))
                print("    No FirstEventCreation column found")
            
            if 'LastEventCreation' in accounts_df.columns:
                last_dates = pd.to_datetime(accounts_df['LastEventCreation'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
                print(f"    Parsed {last_dates.notna().sum()} LastEventCreation dates")
            else:
                last_dates = pd.Series([pd.NaT] * len(accounts_df))
                print("    No LastEventCreation column found")
            
            # Localize to UK timezone if not already timezone-aware
            try:
                if not first_dates.empty and first_dates.dt.tz is None:
                    first_dates = first_dates.dt.tz_localize(UK_TZ)
                if not last_dates.empty and last_dates.dt.tz is None:
                    last_dates = last_dates.dt.tz_localize(UK_TZ)
            except Exception as e:
                print(f"  Warning: Could not localize dates: {e}")
                
            first_event_created_lookup = dict(zip(accounts_df['Id'].astype(int), first_dates))
            last_event_created_lookup = dict(zip(accounts_df['Id'].astype(int), last_dates))
            
            # Debug: Show industry distribution
            industry_counts = accounts_df['Industry'].value_counts().head(10)
            print("\n  Top industries:")
            for industry, count in industry_counts.items():
                print(f"    {industry}: {count} accounts")
            
            # Debug: Check if specific accounts have creation dates
            test_accounts = [5716, 100, 500, 1000]
            print("\n  Sample account creation dates:")
            for acc_id in test_accounts:
                first = first_event_created_lookup.get(acc_id, 'Not found')
                last = last_event_created_lookup.get(acc_id, 'Not found')
                if pd.notna(last) and last != 'Not found':
                    days_since = (datetime.now(UK_TZ) - last).days
                    print(f"    Account {acc_id}: Last={last}, Days since={days_since}")
                else:
                    print(f"    Account {acc_id}: First={first}, Last={last}")
            
            # Check overall parsing success
            valid_first = sum(1 for v in first_event_created_lookup.values() if pd.notna(v))
            valid_last = sum(1 for v in last_event_created_lookup.values() if pd.notna(v))
            print(f"\n  Date parsing success: First={valid_first}/{len(accounts_df)}, Last={valid_last}/{len(accounts_df)}")
                
        except Exception as e:
            print(f"WARNING: Could not load Accounts data: {e}")
            print(f"  Error details: {type(e).__name__}")
            import traceback
            traceback.print_exc()
            print("  Continuing without industry segmentation...")
            industry_lookup = {}
            sub_industry_lookup = {}
            first_event_created_lookup = {}
            last_event_created_lookup = {}
        
        # Process data using optimized chunked approach
        account_metrics = process_booking_data_optimized(s3_client, key_all, key_month)
        
        print(f"\nTotal unique accounts found: {len(account_metrics):,}")
        
        # Load booking data for enhanced churn risk analysis
        booking_data_for_analysis = load_booking_data_for_analysis(s3_client, key_month)
        
    except Exception as e:
        print(f"ERROR: Failed to process S3 files: {str(e)}")
        import traceback
        traceback.print_exc()
        return
    
    # Calculate metrics and tiers
    updates = calculate_metrics_from_aggregated(account_metrics, industry_lookup, sub_industry_lookup,
                                               first_event_created_lookup, last_event_created_lookup,
                                               booking_data_for_analysis)
    
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
    
    # Generate annual events report
    print("\n=== Annual Events Report ===")
    # First show how many annual accounts we have
    annual_count = len(updates[updates['Event_Frequency_Current'] == 'Annual'])
    annual_prev_count = len(updates[updates['Event_Frequency_Previous'] == 'Annual'])
    print(f"Annual accounts (current): {annual_count}")
    print(f"Annual accounts (previous): {annual_prev_count}")
    
    # Show revenue filter impact
    annual_with_revenue = len(updates[
        ((updates['Event_Frequency_Current'] == 'Annual') | 
         (updates['Event_Frequency_Previous'] == 'Annual')) &
        ((updates['_revenue_current'] >= 100) | 
         (updates['_revenue_previous'] >= 100))
    ])
    print(f"Annual accounts with £100+ revenue: {annual_with_revenue}")
    
    annual_report = generate_upcoming_annual_events_report(updates)
    if not annual_report.empty:
        report_filename = f"upcoming_annual_events_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
        annual_report.to_csv(report_filename, index=False)
        print(f"Upcoming annual events needing outreach: {len(annual_report)}")
    else:
        print("No upcoming annual events requiring outreach in next 30 days")
    
    # Generate at risk accounts report
    print("\n=== At Risk Accounts Report ===")
    at_risk_report = generate_at_risk_accounts_report(updates)
    if not at_risk_report.empty:
        risk_filename = f"at_risk_accounts_{datetime.now(UK_TZ).strftime('%Y%m%d')}.csv"
        at_risk_report.to_csv(risk_filename, index=False)
        print(f"At risk accounts identified: {len(at_risk_report)}")
        
        # Show risk breakdown
        critical = len(at_risk_report[at_risk_report['Risk_Level'] == 'Critical'])
        high = len(at_risk_report[at_risk_report['Risk_Level'] == 'High'])
        print(f"  Critical risk (76-100): {critical} accounts")
        print(f"  High risk (51-75): {high} accounts")
        
    else:
        print("No at risk accounts identified")
    
    # Clean up hidden fields before Zoho upload (including _event_data from metrics_df)
    hidden_cols = [col for col in updates.columns if col.startswith('_')]
    zoho_updates = updates.drop(columns=hidden_cols, errors='ignore')
    print(f"\nRemoving {len(hidden_cols)} hidden columns before Zoho upload")
    
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
