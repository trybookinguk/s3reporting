"""
Account processing orchestration for tier calculations, event frequency, and activity ratings.
This module coordinates the various calculations needed for account analysis.
"""
import pandas as pd
import logging
import time
from .utils.config import (
    CUTOFF_365, CUTOFF_730, TODAY, MIN_TICKETS_FOR_ACTIVE,
    EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
)
from .tier_calculator import determine_tier_from_percentiles, batch_determine_tiers
from .event_frequency import classify_event_frequency, get_months_active_fingerprint, format_months_active_for_zoho, batch_classify_frequencies
from .retention_priority import calculate_revenue_drop_category, get_revenue_drop_score
from .revenue_factor import get_revenue_factor, calculate_industry_quintiles
from .rapid_drop_detector import detect_rapid_drop

logger = logging.getLogger(__name__)


def calculate_percentiles(metrics_df):
    """Calculate percentile rankings for metrics.
    
    Args:
        metrics_df: DataFrame with raw metrics
        
    Returns:
        DataFrame with percentile columns added
    """
    # Current period percentiles
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
    
    return metrics_df


def prepare_metrics_dataframe(account_metrics):
    """Convert aggregated account metrics to DataFrame format.
    
    Args:
        account_metrics: Dictionary of aggregated account metrics
        
    Returns:
        DataFrame with structured metrics
    """
    logger.info(f"Processing {len(account_metrics):,} accounts into metrics dataframe")
    all_metrics = []
    processed = 0
    
    for account_id, data in account_metrics.items():
        # Skip if no transactions
        if data.get('tickets_lifetime', 0) == 0:
            continue
        
        # Use pre-aggregated metrics directly
        years_loyalty = data.get('years_loyalty', 0)
        lifetime_revenue = data.get('revenue_lifetime', 0)
        avg_revenue_per_year = data.get('avg_revenue_per_year', 0)
        tickets_current = data.get('tickets_current', 0)
        revenue_current = data.get('revenue_current', 0)
        
        years_loyalty_prev = data.get('years_loyalty_prev', 0)
        revenue_prev = lifetime_revenue - revenue_current  # Revenue up to previous period
        avg_rev_prev = data.get('avg_revenue_prev', 0)
        tickets_prev = data.get('tickets_prev', 0)
        revenue_window_prev = data.get('revenue_prev', 0)  # Revenue in previous window
        
        # Include both month tracking and event creation data
        event_data = {
            'event_months_current': data.get('event_months_current', set()),
            'event_months_previous': data.get('event_months_previous', set()),
            'event_months_freq_current': data.get('event_months_freq_current', set()),
            'event_months_freq_previous': data.get('event_months_freq_previous', set()),
            'event_creation_info': data.get('event_creation_info', {}),
            'last_booking_date': data.get('last_booking_date')
        }
        
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
            'has_activity': tickets_current >= MIN_TICKETS_FOR_ACTIVE,
            '_event_data': event_data  # Store for later use
        })
        
        processed += 1
        if processed % 10000 == 0:
            logger.info(f"Processed {processed:,} of {len(account_metrics):,} accounts ({processed/len(account_metrics)*100:.1f}%)")
        
        # Clear transaction data to free memory
        data['transactions'] = None
    
    logger.info(f"Completed processing {processed:,} accounts")
    
    return pd.DataFrame(all_metrics)


def process_accounts(account_metrics, account_lookup=None, booking_data_df=None):
    """Main orchestration function for processing accounts.
    
    Args:
        account_metrics: Dictionary of aggregated account metrics
        account_lookup: Optional dictionary with Account report data
        booking_data_df: Optional DataFrame with all booking data for industry analysis
        
    Returns:
        DataFrame with tier assignments, event frequencies, and activity ratings
    """
    start_time = time.time()
    logger.info(f"Starting account processing for {len(account_metrics):,} accounts")
    print("\nCalculating metrics for accounts...")
    
    # Convert to DataFrame
    metrics_df = prepare_metrics_dataframe(account_metrics)
    
    # Calculate percentiles
    print("\nCalculating percentiles...")
    logger.info("Calculating percentile rankings for metrics")
    percentile_start = time.time()
    metrics_df = calculate_percentiles(metrics_df)
    logger.info(f"Percentile calculation completed in {time.time() - percentile_start:.1f}s")
    
    # Apply tier logic and other calculations
    print("\nAssigning tiers and calculating new metrics...")
    print(f"Total accounts to process: {len(metrics_df)}")
    
    # Use batch tier calculations for better logging
    print("  Calculating tiers...")
    tier_start = time.time()
    
    # Prepare data for batch processing
    current_tier_data = list(zip(
        metrics_df['tickets_current_pct'],
        metrics_df['revenue_current_pct'],
        metrics_df['years_loyalty'],
        metrics_df['lifetime_revenue_pct'],
        metrics_df['avg_revenue_per_year_pct'],
        metrics_df['has_activity']
    ))
    
    previous_tier_data = list(zip(
        metrics_df['tickets_prev_pct'],
        metrics_df['revenue_prev_pct'],
        metrics_df['years_loyalty_prev'],
        metrics_df['lifetime_revenue_prev_pct'],
        metrics_df['avg_revenue_prev_pct'],
        metrics_df['tickets_prev'] >= MIN_TICKETS_FOR_ACTIVE
    ))
    
    logger.info("Processing current period tiers...")
    metrics_df['Current_Tier'] = batch_determine_tiers(current_tier_data)
    
    logger.info("Processing previous period tiers...")
    metrics_df['Previous_Tier'] = batch_determine_tiers(previous_tier_data)
    
    logger.info(f"Tier calculation completed in {time.time() - tier_start:.1f}s")
    
    # Vectorized Account_Name conversion
    print("  Processing account names...")
    
    def convert_account_name(name):
        """Safely convert account name to string format."""
        try:
            if pd.notna(name) and str(name).strip():
                return str(int(float(name)))
            return None
        except (ValueError, TypeError):
            return None
    
    metrics_df['Account_Name_Clean'] = metrics_df['Account_Name'].apply(convert_account_name)
    
    # Filter out invalid account names
    valid_mask = metrics_df['Account_Name_Clean'].notna()
    invalid_count = (~valid_mask).sum()
    if invalid_count > 0:
        logger.warning(f"Filtering out {invalid_count:,} accounts with invalid names")
    
    metrics_df = metrics_df[valid_mask].copy()
    
    if len(metrics_df) == 0:
        logger.error("No valid accounts to process after name conversion.")
        print("No valid accounts to process after name conversion.")
        return pd.DataFrame()
    
    # Extract event data components into separate columns for vectorization
    print("  Extracting event data...")
    logger.info("Extracting event data components")
    event_start = time.time()
    
    metrics_df['event_months_current'] = metrics_df['_event_data'].apply(lambda x: x.get('event_months_current', set()))
    metrics_df['event_months_previous'] = metrics_df['_event_data'].apply(lambda x: x.get('event_months_previous', set()))
    metrics_df['event_months_freq_current'] = metrics_df['_event_data'].apply(lambda x: x.get('event_months_freq_current', set()))
    metrics_df['event_months_freq_previous'] = metrics_df['_event_data'].apply(lambda x: x.get('event_months_freq_previous', set()))
    metrics_df['event_creation_info'] = metrics_df['_event_data'].apply(lambda x: x.get('event_creation_info', {}))
    metrics_df['last_booking_date'] = metrics_df['_event_data'].apply(lambda x: x.get('last_booking_date'))
    
    logger.info(f"Event data extraction completed in {time.time() - event_start:.1f}s")
    
    # Calculate month counts
    metrics_df['freq_month_count_current'] = metrics_df['event_months_freq_current'].apply(len)
    metrics_df['freq_month_count_previous'] = metrics_df['event_months_freq_previous'].apply(len)
    
    # Process account lookup data if available
    if account_lookup:
        print("  Processing account lookup data...")
        # Create a DataFrame from account_lookup for efficient merging
        lookup_df = pd.DataFrame.from_dict(
            {k: v for k, v in account_lookup.items()}, 
            orient='index'
        )
        lookup_df.index = lookup_df.index.astype(str)
        lookup_df = lookup_df.reset_index().rename(columns={'index': 'Account_Name_Clean'})
        
        # Merge with metrics_df
        metrics_df = metrics_df.merge(
            lookup_df[['Account_Name_Clean', 'Industry', 'Postcode', 'DateTimeCreated', 'LastEventCreation']],
            on='Account_Name_Clean',
            how='left'
        )
        
        # Process dates
        metrics_df['account_created_date'] = pd.to_datetime(
            metrics_df['DateTimeCreated'], errors='coerce'
        ).dt.date
        
        metrics_df['last_creation_date'] = pd.to_datetime(
            metrics_df['LastEventCreation'], errors='coerce'
        ).dt.date
        
        # Check event creation periods
        metrics_df['has_event_creation_current'] = (
            metrics_df['last_creation_date'] >= EVENT_FREQ_CUTOFF_CURRENT
        ).fillna(False)
        
        metrics_df['has_event_creation_previous'] = (
            (metrics_df['last_creation_date'] >= EVENT_FREQ_CUTOFF_PREVIOUS) & 
            (metrics_df['last_creation_date'] < EVENT_FREQ_CUTOFF_CURRENT)
        ).fillna(False)
    else:
        # Set default values if no lookup available
        metrics_df['Industry'] = None
        metrics_df['Postcode'] = None
        metrics_df['account_created_date'] = None
        metrics_df['has_event_creation_current'] = False
        metrics_df['has_event_creation_previous'] = False
    
    # VECTORIZED event frequency classification - major speedup
    print("  Classifying event frequencies...")
    
    # Vectorized current frequency classification
    metrics_df['Event_Frequency_Current'] = pd.cut(
        metrics_df['freq_month_count_current'],
        bins=[-1, 0, 1, 4, 9, 12],
        labels=['Inactive', 'Annual', 'Seasonal', 'Regular', 'Continuous'],
        include_lowest=True
    ).astype(str)
    
    # Vectorized previous frequency classification  
    metrics_df['Event_Frequency_Previous'] = pd.cut(
        metrics_df['freq_month_count_previous'],
        bins=[-1, 0, 1, 4, 9, 12],
        labels=['Inactive', 'Annual', 'Seasonal', 'Regular', 'Continuous'],
        include_lowest=True
    ).astype(str)
    
    # Event frequency summary
    event_freq_summary = metrics_df['Event_Frequency_Current'].value_counts().to_dict()
    
    # Calculate lead times and event dates
    print("  Processing event timing data...")
    
    def calculate_event_metrics(event_creation_info):
        """Extract lead times and last event date from event creation info."""
        if not event_creation_info:
            return 60, None
        
        lead_times = [info['lead_days'] for info in event_creation_info.values() if info['lead_days'] > 0]
        avg_lead_days = int(sum(lead_times) / len(lead_times)) if lead_times else 60
        
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        last_event_date = max(event_dates).date() if event_dates else None
        
        return avg_lead_days, last_event_date
    
    event_metrics = metrics_df['event_creation_info'].apply(calculate_event_metrics)
    metrics_df['avg_lead_days'] = event_metrics.apply(lambda x: x[0])
    metrics_df['last_event_date'] = event_metrics.apply(lambda x: x[1])
    
    # Calculate days since last activity
    metrics_df['days_since_last'] = metrics_df['last_booking_date'].apply(
        lambda x: (TODAY - x.date()).days if x else 999
    )
    
    # VECTORIZED months active patterns processing
    print("  Processing months active patterns...")
    
    # Vectorized months active fingerprint extraction
    def get_months_vectorized(event_months_series):
        """Vectorized version of get_months_active_fingerprint"""
        return event_months_series.apply(
            lambda months_set: sorted(list({month for year, month in months_set})) if months_set else []
        )
    
    def format_months_vectorized(months_list_series):
        """Vectorized version of format_months_active_for_zoho"""
        month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                      'July', 'August', 'September', 'October', 'November', 'December']
        return months_list_series.apply(
            lambda months: [month_names[m] for m in months if 1 <= m <= 12] if isinstance(months, list) else []
        )
    
    # Apply vectorized functions
    metrics_df['months_active_current'] = get_months_vectorized(metrics_df['event_months_freq_current'])
    metrics_df['Months_Active'] = format_months_vectorized(metrics_df['months_active_current'])
    
    # Vectorized historical months combination
    metrics_df['all_freq_months'] = metrics_df['event_months_freq_current'].combine(
        metrics_df['event_months_freq_previous'], 
        lambda x, y: (x if x else set()) | (y if y else set())
    )
    metrics_df['months_active_historical'] = get_months_vectorized(metrics_df['all_freq_months'])
    
    # Check if has historical data
    metrics_df['has_historical'] = (
        (metrics_df['years_loyalty'] > 0) | 
        (metrics_df['event_months_previous'].apply(len) > 0)
    )
    
    # VECTORIZED activity rating calculation - massive performance improvement
    print(f"  Determining activity ratings for {len(metrics_df):,} accounts...")
    logger.info(f"Starting VECTORIZED activity rating calculation for {len(metrics_df):,} accounts")
    
    activity_start_time = time.time()
    
    # Import vectorized function
    from .activity_rating import calculate_activity_ratings
    
    # Apply vectorized calculation (replaces slow row-by-row processing)
    metrics_df['Rating'] = calculate_activity_ratings(metrics_df)
    
    elapsed = time.time() - activity_start_time
    rate = len(metrics_df) / elapsed if elapsed > 0 else 0
    logger.info(f"VECTORIZED activity rating completed in {elapsed:.1f}s ({rate:.0f} accounts/sec) - MAJOR speedup!")
    
    # Initialize revenue drop fields
    metrics_df['revenue_drop_category'] = 'None'
    metrics_df['revenue_drop_score'] = 0
    metrics_df['revenue_drop_details'] = None
    metrics_df['rapid_drop_alert'] = 0
    metrics_df['rapid_drop_details'] = None
    
    # Process revenue drops with booking data if available
    if booking_data_df is not None and not booking_data_df.empty and 'Industry' in metrics_df.columns:
        print("  Calculating revenue factors...")
        
        # Process accounts with industry data
        has_industry = metrics_df['Industry'].notna()
        
        if has_industry.any():
            # Prepare accounts DataFrame for quintile calculation
            accounts_df = None
            if account_lookup:
                accounts_df = pd.DataFrame.from_dict(account_lookup, orient='index')
                accounts_df.reset_index(inplace=True)
                accounts_df.rename(columns={'index': 'AccountId'}, inplace=True)
            
            # Process in batches for efficiency
            batch_size = 1000
            total_with_industry = has_industry.sum()
            processed = 0
            
            # VECTORIZED revenue factor calculation - major performance improvement
            print(f"    Processing {has_industry.sum()} accounts with industry data...")
            
            # Determine account patterns vectorized
            accounts_with_industry = metrics_df.loc[has_industry].copy()
            
            # Vectorized pattern classification
            is_annual = (
                (accounts_with_industry['Event_Frequency_Current'] == 'Annual') |
                (accounts_with_industry['Event_Frequency_Previous'] == 'Annual')
            )
            is_seasonal = (
                (accounts_with_industry['Event_Frequency_Current'] == 'Seasonal') |
                (accounts_with_industry['Event_Frequency_Previous'] == 'Seasonal')
            ) & (~is_annual)
            
            accounts_with_industry['account_pattern'] = 'continuous'  # Default
            accounts_with_industry.loc[is_annual, 'account_pattern'] = 'annual'
            accounts_with_industry.loc[is_seasonal, 'account_pattern'] = 'seasonal'
            
            # Process by industry groups for efficiency (batch processing)
            revenue_results_list = []
            
            for industry, industry_accounts in accounts_with_industry.groupby('Industry'):
                if pd.isna(industry):
                    continue
                
                # Get industry data once for all accounts in this industry
                industry_data = booking_data_df[booking_data_df['Industry'] == industry]
                
                # Process accounts in this industry batch
                for _, account in industry_accounts.iterrows():
                    try:
                        # Get account history
                        account_history = booking_data_df[
                            booking_data_df['AccountId'] == account['Account_Name_Clean']
                        ].copy()
                        
                        # Prepare account info
                        account_info = {}
                        if accounts_df is not None:
                            account_info['accounts_df'] = accounts_df
                        
                        # Calculate revenue factor
                        revenue_result = get_revenue_factor(
                            current_revenue=account.get('revenue_current', 0),
                            historical_revenue=account_history,
                            industry_data=industry_data,
                            account_type=account['account_pattern'],
                            account_info=account_info
                        )
                        
                        revenue_results_list.append((
                            account.name,  # DataFrame index
                            revenue_result['severity'].capitalize(),
                            revenue_result['score'],
                            revenue_result.get('details', {})
                        ))
                        
                    except Exception as e:
                        # Fallback to simple calculation
                        category = calculate_revenue_drop_category(
                            account.get('revenue_current', 0),
                            account.get('revenue_prev', 0)
                        )
                        score = get_revenue_drop_score(category)
                        revenue_results_list.append((
                            account.name,
                            category,
                            score,
                            {}
                        ))
            
            # Convert results to format expected by downstream code
            revenue_results = pd.Series(
                [(row[1], row[2], row[3]) for row in revenue_results_list],
                index=[row[0] for row in revenue_results_list]
            )
            
            # Unpack results
            metrics_df.loc[has_industry, 'revenue_drop_category'] = revenue_results.apply(lambda x: x[0])
            metrics_df.loc[has_industry, 'revenue_drop_score'] = revenue_results.apply(lambda x: x[1])
            metrics_df.loc[has_industry, 'revenue_drop_details'] = revenue_results.apply(lambda x: x[2])
        
        # Apply simple revenue drop calculation for accounts without industry data
        no_industry = ~has_industry
        if no_industry.any():
            print(f"    Processing {no_industry.sum()} accounts without industry data...")
            metrics_df.loc[no_industry, 'revenue_drop_category'] = metrics_df.loc[no_industry].apply(
                lambda row: calculate_revenue_drop_category(
                    row.get('revenue_current', 0),
                    row.get('revenue_prev', 0)
                ), axis=1
            )
            metrics_df.loc[no_industry, 'revenue_drop_score'] = metrics_df.loc[no_industry, 'revenue_drop_category'].apply(
                get_revenue_drop_score
            )
            metrics_df.loc[no_industry, 'revenue_drop_details'] = {}
    else:
        # No booking data or no Industry column - apply simple calculation to all
        print("  Applying simple revenue drop calculation to all accounts...")
        metrics_df['revenue_drop_category'] = metrics_df.apply(
            lambda row: calculate_revenue_drop_category(
                row.get('revenue_current', 0),
                row.get('revenue_prev', 0)
            ), axis=1
        )
        metrics_df['revenue_drop_score'] = metrics_df['revenue_drop_category'].apply(get_revenue_drop_score)
        metrics_df['revenue_drop_details'] = None
    
    # Calculate rapid drop alerts for Tier 3+ accounts
    print("  Detecting rapid revenue drops...")
    rapid_drop_start = time.time()
    
    # Define high-value tiers that qualify for rapid drop detection
    HIGH_VALUE_TIERS = ["Key Account", "High Value", "Tier 4", "Tier 3"]
    
    # Filter accounts eligible for rapid drop detection
    # Include accounts that are currently high-value OR were high-value previously
    # This prevents losing monitoring when an account drops tier due to the revenue issues
    current_high_value = metrics_df['Current_Tier'].isin(HIGH_VALUE_TIERS)
    previous_high_value = metrics_df['Previous_Tier'].fillna('').isin(HIGH_VALUE_TIERS)
    eligible_for_rapid_drop = current_high_value | previous_high_value
    logger.info(f"Checking {eligible_for_rapid_drop.sum():,} high-value accounts for rapid drops")
    
    if eligible_for_rapid_drop.any() and booking_data_df is not None and not booking_data_df.empty:
        # Process each eligible account
        rapid_drop_count = 0
        for idx in metrics_df[eligible_for_rapid_drop].index:
            row = metrics_df.loc[idx]
            account_id = row['Account_Name_Clean']
            
            # Get account's transaction history
            account_history = booking_data_df[
                booking_data_df['AccountId'] == account_id
            ].copy()
            
            if not account_history.empty:
                # Prepare account info for rapid drop detection
                account_info = {
                    'tier': row['Current_Tier'],
                    'event_frequency': row['Event_Frequency_Current'],
                    'months_active': row.get('months_active_current', [])
                }
                
                # Detect rapid drops with error handling
                try:
                    rapid_drop_result = detect_rapid_drop(account_history, account_info)
                    
                    # Store results
                    metrics_df.loc[idx, 'rapid_drop_alert'] = rapid_drop_result.get('score', 0)
                    metrics_df.loc[idx, 'rapid_drop_details'] = rapid_drop_result
                    
                    if rapid_drop_result.get('score', 0) > 0:
                        rapid_drop_count += 1
                        
                except Exception as e:
                    logger.warning(f"Error detecting rapid drop for account {account_id}: {e}")
                    # Set safe defaults
                    metrics_df.loc[idx, 'rapid_drop_alert'] = 0
                    metrics_df.loc[idx, 'rapid_drop_details'] = {'error': str(e)}
        
        logger.info(f"Detected {rapid_drop_count:,} accounts with rapid revenue drops")
    else:
        logger.info("No booking data available or no eligible accounts for rapid drop detection")
    
    logger.info(f"Rapid drop detection completed in {time.time() - rapid_drop_start:.1f}s")
    
    # Calculate retention priority
    print("  Calculating retention priorities...")
    
    # Add data quality logging before priority calculation
    missing_current_tier = metrics_df['Current_Tier'].isna().sum()
    missing_rating = metrics_df['Rating'].isna().sum()
    missing_revenue_score = metrics_df['revenue_drop_score'].isna().sum()
    
    if missing_current_tier > 0 or missing_rating > 0 or missing_revenue_score > 0:
        logger.warning(f"Data quality issues before priority calculation: "
                      f"Missing Current_Tier: {missing_current_tier}, "
                      f"Missing Rating: {missing_rating}, "
                      f"Missing revenue_drop_score: {missing_revenue_score}")
    
    # VECTORIZED retention priority calculation - major performance improvement  
    from .retention_priority import calculate_retention_priorities, categorize_priorities
    
    priority_scores = calculate_retention_priorities(metrics_df)
    priority_categories = categorize_priorities(priority_scores)
    metrics_df['retention_priority_score'] = priority_scores
    metrics_df['Retention_Priority'] = priority_categories
    
    # Clear retention priority for churned accounts
    metrics_df.loc[metrics_df['Rating'] == 'Churned', 'Retention_Priority'] = ''
    
    # Build final results DataFrame
    print("  Building final results...")
    results_df = pd.DataFrame({
        'Account_Name': metrics_df['Account_Name_Clean'],
        'Current_Tier': metrics_df['Current_Tier'],
        'Previous_Tier': metrics_df['Previous_Tier'],
        'Ticket_Quantity': metrics_df['tickets_current'].astype(int),
        'Last_Year_Ticket_Quantity': metrics_df['tickets_prev'].astype(int),
        'Years_Loyalty': metrics_df['years_loyalty'],
        'Event_Frequency_Current': metrics_df['Event_Frequency_Current'],
        'Event_Frequency_Previous': metrics_df['Event_Frequency_Previous'],
        'Rating': metrics_df['Rating'],
        'Months_Active': metrics_df['Months_Active'],
        'Retention_Priority': metrics_df['Retention_Priority'],
        # Hidden fields for report generation (prefix with _)
        '_retention_priority_score': metrics_df['retention_priority_score'],
        '_avg_lead_days': metrics_df['avg_lead_days'],
        '_last_event_date': metrics_df['last_event_date'],
        '_month_count_current': metrics_df['event_months_current'].apply(len),
        '_months_active_list': metrics_df['months_active_current'],
        '_revenue_current': metrics_df['revenue_current'],
        '_revenue_prev': metrics_df['revenue_prev'],
        '_revenue_drop_category': metrics_df['revenue_drop_category'],
        '_revenue_drop_details': metrics_df['revenue_drop_details'],
        '_rapid_drop_alert': metrics_df['rapid_drop_alert'],
        '_rapid_drop_details': metrics_df['rapid_drop_details']
    })
    
    # Print event frequency summary
    print("\nEvent Frequency Summary:")
    event_freq_counts = metrics_df['Event_Frequency_Current'].value_counts()
    for freq_type in ['Continuous', 'Regular', 'Seasonal', 'Annual', 'Inactive']:
        count = event_freq_counts.get(freq_type, 0)
        print(f"  {freq_type}: {count:,} accounts")
    
    # Activity rating summary
    if not results_df.empty:
        rating_counts = results_df['Rating'].value_counts()
        print("\nActivity Rating Summary:")
        for rating in ['Active', 'Outreach', 'At Risk', 'Churned', 'Returned', 'New', 'Inactive']:
            count = rating_counts.get(rating, 0)
            print(f"  {rating}: {count:,} accounts")
        
        # Retention Priority summary
        priority_counts = results_df['Retention_Priority'].value_counts()
        print("\nRetention Priority Summary:")
        for priority in ['Very High', 'High', 'Medium', 'Low']:
            count = priority_counts.get(priority, 0)
            pct = (count / len(results_df) * 100) if len(results_df) > 0 else 0
            print(f"  {priority}: {count:,} accounts ({pct:.1f}%)")
        
        # Show churned accounts (empty retention priority) separately
        churned_count = len(results_df[results_df['Rating'] == 'Churned'])
        if churned_count > 0:
            churned_pct = (churned_count / len(results_df) * 100) if len(results_df) > 0 else 0
            print(f"\n  Churned (No Priority): {churned_count:,} accounts ({churned_pct:.1f}%) - Excluded from standard CS workflows")
        
        # Months Active patterns summary
        print("\nMonths Active Patterns (Top 10):")
        # Convert lists to strings for value_counts
        month_patterns_str = results_df['Months_Active'].apply(
            lambda x: ','.join(x) if isinstance(x, list) else str(x)
        ).value_counts().head(10)
        for pattern, count in month_patterns_str.items():
            if pattern:  # Skip empty patterns
                print(f"  {pattern}: {count:,} accounts")
        
        # Rapid Drop Alert summary
        if '_rapid_drop_alert' in results_df.columns:
            rapid_alerts = results_df['_rapid_drop_alert']
            severe_alerts = (rapid_alerts == 3).sum()
            significant_alerts = (rapid_alerts == 2).sum()
            moderate_alerts = (rapid_alerts == 1).sum()
            
            if severe_alerts > 0 or significant_alerts > 0 or moderate_alerts > 0:
                print("\nRapid Drop Alerts (High-Value Accounts):")
                if severe_alerts > 0:
                    print(f"  Severe (Level 3): {severe_alerts:,} accounts - Critical intervention needed")
                if significant_alerts > 0:
                    print(f"  Significant (Level 2): {significant_alerts:,} accounts - Immediate attention required")
                if moderate_alerts > 0:
                    print(f"  Moderate (Level 1): {moderate_alerts:,} accounts - Monitor closely")
        
        # Annual Reachout summary
        annual_accounts = results_df[results_df['Event_Frequency_Current'] == 'Annual']
        if len(annual_accounts) > 0:
            # Check which annual accounts got priority boost (score 18+ but not from other sources)
            annual_high_priority = annual_accounts[
                (annual_accounts['_retention_priority_score'] >= 18) & 
                (annual_accounts['Rating'].isin(['Active', 'Inactive', 'New'])) &  # Not already flagged for other issues
                (annual_accounts['_rapid_drop_alert'] == 0)  # Not rapid drop
            ]
            
            if len(annual_high_priority) > 0:
                print(f"\nAnnual Event Reachouts:")
                print(f"  {len(annual_high_priority):,} Annual accounts boosted to High priority for proactive outreach")
                print(f"  Total Annual accounts: {len(annual_accounts):,}")
    
    return results_df