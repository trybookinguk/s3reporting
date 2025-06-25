"""
Account processing orchestration for tier calculations, event frequency, and activity ratings.
This module coordinates the various calculations needed for account analysis.
"""
import pandas as pd
from .config import (
    CUTOFF_365, CUTOFF_730, TODAY, MIN_TICKETS_FOR_ACTIVE,
    EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
)
from .tier_calculator import determine_tier_from_percentiles
from .event_frequency import classify_event_frequency, get_months_active_fingerprint, format_months_active_for_zoho
from .activity_rating import determine_activity_rating
from .retention_priority import calculate_retention_priority, calculate_revenue_drop_category, categorize_priority, get_revenue_drop_score
from .revenue_factor import get_revenue_factor, calculate_industry_quintiles


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
        if processed % 1000 == 0:
            print(f"  Processed {processed:,} accounts...")
        
        # Clear transaction data to free memory
        data['transactions'] = None
    
    print(f"  Total accounts processed: {processed:,}")
    
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
    print("\nCalculating metrics for accounts...")
    
    # Convert to DataFrame
    metrics_df = prepare_metrics_dataframe(account_metrics)
    
    # Calculate percentiles
    print("\nCalculating percentiles...")
    metrics_df = calculate_percentiles(metrics_df)
    
    # Apply tier logic and other calculations
    print("\nAssigning tiers and calculating new metrics...")
    print(f"Total accounts to process: {len(metrics_df)}")
    
    results = []
    event_freq_summary = {'Continuous': 0, 'Regular': 0, 'Seasonal': 0, 'Annual': 0, 'Inactive': 0}
    
    for _, row in metrics_df.iterrows():
        # Calculate current tier
        tier_current = determine_tier_from_percentiles(
            row['tickets_current_pct'],
            row['revenue_current_pct'],
            row['years_loyalty'],
            row['lifetime_revenue_pct'],
            row['avg_revenue_per_year_pct'],
            row['has_activity']
        )
        
        # Calculate previous tier
        tier_prev = determine_tier_from_percentiles(
            row['tickets_prev_pct'],
            row['revenue_prev_pct'],
            row['years_loyalty_prev'],
            row['lifetime_revenue_prev_pct'],
            row['avg_revenue_prev_pct'],
            row['tickets_prev'] >= MIN_TICKETS_FOR_ACTIVE
        )
        
        # Handle Account_Name conversion safely
        try:
            if pd.notna(row['Account_Name']) and str(row['Account_Name']).strip():
                account_name = str(int(float(row['Account_Name'])))
            else:
                continue
        except (ValueError, TypeError):
            print(f"Warning: Skipping account with invalid Account_Name: {row['Account_Name']}")
            continue
        
        # Calculate new metrics from stored event data
        event_data = row.get('_event_data', {})
        
        # Get month counts for frequency calculation (month boundary)
        freq_month_count_current = len(event_data.get('event_months_freq_current', set()))
        freq_month_count_previous = len(event_data.get('event_months_freq_previous', set()))
        event_creation_info = event_data.get('event_creation_info', {})
        
        # Check if account has event creation in period for 'New' status
        has_event_creation_current = False
        has_event_creation_previous = False
        industry = None
        account_postcode = None
        account_created_date = None
        if account_lookup and account_name in account_lookup:
            account_info = account_lookup[account_name]
            last_creation = account_info.get('LastEventCreation')
            industry = account_info.get('Industry')
            account_postcode = account_info.get('Postcode')
            
            # Get account creation date
            date_time_created = account_info.get('DateTimeCreated')
            if date_time_created and pd.notna(date_time_created):
                account_created_date = pd.to_datetime(date_time_created).date()
            
            if last_creation and pd.notna(last_creation):
                last_creation_date = pd.to_datetime(last_creation).date()
                if last_creation_date >= EVENT_FREQ_CUTOFF_CURRENT:
                    has_event_creation_current = True
                elif last_creation_date >= EVENT_FREQ_CUTOFF_PREVIOUS:
                    has_event_creation_previous = True
        
        # Calculate event frequency based on month boundary counts
        event_freq_current = classify_event_frequency(freq_month_count_current, has_event_creation_current)
        event_freq_previous = classify_event_frequency(freq_month_count_previous, has_event_creation_previous)
        event_freq_summary[event_freq_current] += 1
        
        # Calculate lead times for annual event predictions
        lead_times = [info['lead_days'] for info in event_creation_info.values() if info['lead_days'] > 0]
        avg_lead_days = int(sum(lead_times) / len(lead_times)) if lead_times else 60
        
        # Calculate days since last activity
        last_booking = event_data.get('last_booking_date')
        days_since_last = (TODAY - last_booking.date()).days if last_booking else 999
        
        # Get last event date for annual predictions
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        last_event_date = max(event_dates).date() if event_dates else None
        
        # Calculate Months Active fingerprint for current period only (for Zoho field)
        current_freq_months = event_data.get('event_months_freq_current', set())
        months_active_current = get_months_active_fingerprint(current_freq_months)
        months_active_zoho = format_months_active_for_zoho(months_active_current)
        
        # For activity rating education pattern detection, we need historical patterns
        all_freq_months = current_freq_months | event_data.get('event_months_freq_previous', set())
        months_active_historical = get_months_active_fingerprint(all_freq_months)
        
        # Determine activity rating using full logic
        has_historical = row['years_loyalty'] > 0 or len(event_data.get('event_months_previous', set())) > 0
        
        activity_rating = determine_activity_rating(
            current_freq=event_freq_current,
            previous_freq=event_freq_previous,
            days_since_last=days_since_last,
            has_historical=has_historical,
            avg_lead_days=avg_lead_days,
            last_event_date=last_event_date,
            months_active_list=months_active_historical,
            revenue_previous=row.get('revenue_prev', 0),
            industry=industry,
            current_tier=tier_current,
            account_postcode=account_postcode,
            account_created_date=account_created_date
        )
        
        # Calculate revenue drop using sophisticated analysis if booking data available
        if booking_data_df is not None and not booking_data_df.empty and industry:
            # Get account's transaction history
            account_history = booking_data_df[booking_data_df['AccountId'] == account_name].copy()
            
            # Determine account pattern for revenue analysis
            account_pattern = 'continuous'  # Default
            if event_freq_current == 'Annual' or event_freq_previous == 'Annual':
                account_pattern = 'annual'
            elif event_freq_current == 'Seasonal' or event_freq_previous == 'Seasonal':
                account_pattern = 'seasonal'
            
            # Get industry data for quintile calculation
            industry_data = booking_data_df[booking_data_df['Industry'] == industry]
            
            # Prepare account info with accounts_df for quintile calculation
            account_info = {}
            if account_lookup:
                # Convert account_lookup to DataFrame format for quintile calculation
                accounts_df = pd.DataFrame.from_dict(account_lookup, orient='index')
                accounts_df.reset_index(inplace=True)
                accounts_df.rename(columns={'index': 'AccountId'}, inplace=True)
                account_info['accounts_df'] = accounts_df
            
            # Calculate revenue factor with all advanced features
            try:
                revenue_result = get_revenue_factor(
                    current_revenue=row.get('revenue_current', 0),
                    historical_revenue=account_history,
                    industry_data=industry_data,
                    account_type=account_pattern,
                    account_info=account_info
                )
                
                # Map score to category for compatibility
                revenue_drop_score = revenue_result['score']
                revenue_drop_category = revenue_result['severity'].capitalize()
                revenue_drop_details = revenue_result.get('details', {})
            except Exception as e:
                print(f"Warning: Error calculating revenue factor for {account_name}: {str(e)}")
                # Fallback to simple calculation
                revenue_drop_category = calculate_revenue_drop_category(
                    row.get('revenue_current', 0),
                    row.get('revenue_prev', 0)
                )
                revenue_drop_score = get_revenue_drop_score(revenue_drop_category)
                revenue_drop_details = {}
        else:
            # Fallback to simple calculation
            revenue_drop_category = calculate_revenue_drop_category(
                row.get('revenue_current', 0),
                row.get('revenue_prev', 0)
            )
            revenue_drop_score = get_revenue_drop_score(revenue_drop_category)
            revenue_drop_details = {}
        
        # Calculate retention priority using either score or category
        retention_priority_score = calculate_retention_priority(
            tier_current,
            activity_rating,
            revenue_drop_score
        )
        
        retention_priority = categorize_priority(retention_priority_score)
        
        # If account is churned (excluded), set retention priority to empty
        if activity_rating == "Churned":
            retention_priority = ""
            
        results.append({
            "Account_Name": account_name,
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(row['tickets_current']),
            "Last_Year_Ticket_Quantity": int(row['tickets_prev']),
            "Years_Loyalty": row['years_loyalty'],
            "Event_Frequency_Current": event_freq_current,
            "Event_Frequency_Previous": event_freq_previous,
            "Rating": activity_rating,
            "Months_Active": months_active_zoho,
            "Retention_Priority": retention_priority,
            # Hidden fields for report generation (prefix with _)
            "_retention_priority_score": retention_priority_score,
            "_avg_lead_days": avg_lead_days,
            "_last_event_date": last_event_date,
            "_month_count_current": len(event_data.get('event_months_current', set())),
            "_months_active_list": months_active_current,
            "_revenue_current": row.get('revenue_current', 0),
            "_revenue_prev": row.get('revenue_prev', 0),
            "_revenue_drop_category": revenue_drop_category,
            "_revenue_drop_details": revenue_drop_details
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
    
    return results_df