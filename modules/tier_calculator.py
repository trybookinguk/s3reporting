"""
Tier calculation and business logic for TryBooking accounts.
"""
import pandas as pd
from .config import (
    CUTOFF_365, CUTOFF_730, TODAY, TIER_PERCENTILES, MIN_YEARS_BY_TIER,
    MIN_TICKETS_FOR_ACTIVE, EVENT_FREQUENCY_THRESHOLDS,
    EVENT_FREQ_CUTOFF_CURRENT, EVENT_FREQ_CUTOFF_PREVIOUS
)
from .event_frequency import classify_event_frequency, get_months_active_fingerprint, format_months_active_for_zoho
from .activity_rating import determine_activity_rating


def determine_tier_from_percentiles(a_pct, b_pct, c_years, d_pct, e_pct, has_activity):
    """
    Determine tier based on percentile rankings.
    
    Args:
        a_pct: percentile rank for tickets_current (0-100)
        b_pct: percentile rank for revenue_current (0-100)
        c_years: years_loyalty (actual value, not percentile)
        d_pct: percentile rank for lifetime_revenue (0-100)
        e_pct: percentile rank for avg_revenue_per_year (0-100)
        has_activity: whether account has any current period activity
    
    Returns:
        Tier classification string
    """
    if not has_activity:
        return "NIL"
    
    # Check each path: A alone, B alone, or C+D+E combination
    best_tier = "Tier 1"  # Default for qualified accounts
    
    # Path 1: A alone (tickets)
    for tier, threshold in TIER_PERCENTILES.items():
        if a_pct >= threshold:
            best_tier = tier
            break
    
    # Path 2: B alone (revenue)
    for tier, threshold in TIER_PERCENTILES.items():
        if b_pct >= threshold:
            # Upgrade tier if better than current best
            if list(TIER_PERCENTILES.keys()).index(tier) < list(TIER_PERCENTILES.keys()).index(best_tier) if best_tier in TIER_PERCENTILES else True:
                best_tier = tier
            break
    
    # Path 3: C+D+E combination (requires minimum years loyalty)
    for tier, threshold in TIER_PERCENTILES.items():
        if c_years >= MIN_YEARS_BY_TIER.get(tier, 1):
            # Both D and E must meet the threshold
            if d_pct >= threshold and e_pct >= threshold:
                # Upgrade tier if better than current best
                if list(TIER_PERCENTILES.keys()).index(tier) < list(TIER_PERCENTILES.keys()).index(best_tier) if best_tier in TIER_PERCENTILES else True:
                    best_tier = tier
                break
    
    return best_tier


# Rating functions moved to activity_rating.py module


def calculate_metrics_from_aggregated(account_metrics, account_lookup=None):
    """Calculate metrics from pre-aggregated account data.
    
    Args:
        account_metrics: Dictionary of aggregated account metrics
        account_lookup: Optional dictionary with Account report data (for LastEventCreation)
    """
    print("\nCalculating metrics for accounts...")
    
    all_metrics = []
    processed = 0
    
    for account_id, data in account_metrics.items():
        # Skip if no transactions (using new optimized check)
        if data.get('tickets_lifetime', 0) == 0:
            continue
        
        # Use pre-aggregated metrics directly (all data from optimized loader has these)
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
            print(f"  Active months current: {len(event_data_sample.get('event_months_current', set()))}")
            print(f"  Active months previous: {len(event_data_sample.get('event_months_previous', set()))}")
            print(f"  Event creation info entries: {len(event_data_sample.get('event_creation_info', {}))}")
    
    results = []
    event_freq_summary = {'Continuous': 0, 'Regular': 0, 'Seasonal': 0, 'Annual': 0, 'Inactive': 0}
    
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
        
        # Get month counts for tier calculation (rolling window)
        month_count_current = len(event_data.get('event_months_current', set()))
        month_count_previous = len(event_data.get('event_months_previous', set()))
        
        # Get month counts for frequency calculation (month boundary)
        freq_month_count_current = len(event_data.get('event_months_freq_current', set()))
        freq_month_count_previous = len(event_data.get('event_months_freq_previous', set()))
        event_creation_info = event_data.get('event_creation_info', {})
        
        # Check if account has event creation in period for 'New' status
        has_event_creation_current = False
        has_event_creation_previous = False
        industry = None
        account_postcode = None
        if account_lookup and account_name in account_lookup:
            account_info = account_lookup[account_name]
            last_creation = account_info.get('LastEventCreation')
            industry = account_info.get('Industry')
            account_postcode = account_info.get('Postcode')
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
        
        # Determine activity rating using full logic
        has_historical = years_loyalty > 0 or len(event_data.get('event_months_previous', set())) > 0
        
        activity_rating = determine_activity_rating(
            current_freq=event_freq_current,
            previous_freq=event_freq_previous,
            days_since_last=days_since_last,
            has_historical=has_historical,
            avg_lead_days=avg_lead_days,
            last_event_date=last_event_date,
            months_active_list=months_active_list,
            revenue_previous=row.get('revenue_prev', 0),
            industry=industry,
            current_tier=tier_current,
            account_postcode=account_postcode
        )
        
        # Calculate Months Active fingerprint using frequency months for consistency
        all_freq_months = event_data.get('event_months_freq_current', set()) | event_data.get('event_months_freq_previous', set())
        months_active_list = get_months_active_fingerprint(all_freq_months)
        months_active_zoho = format_months_active_for_zoho(months_active_list)
            
        results.append({
            "Account_Name": account_name,
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(row['tickets_current']),
            "Last_Year_Ticket_Quantity": int(row['tickets_prev']),
            "Years_Loyalty": row['years_loyalty'],
            "Event_Frequency_Current": event_freq_current,
            "Event_Frequency_Previous": event_freq_previous,
            "Rating": activity_rating,  # Changed from Activity_Rating to match Zoho field name
            "Months_Active": months_active_zoho,  # New field for Zoho multi-select
            # Hidden fields for report generation (prefix with _)
            "_avg_lead_days": avg_lead_days,
            "_last_event_date": last_event_date,
            "_month_count_current": month_count_current,
            "_months_active_list": months_active_list,  # Keep list format for reporting
            "_revenue_current": row.get('revenue_current', 0),
            "_revenue_prev": row.get('revenue_prev', 0)
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
        
        # Months Active patterns summary
        print("\nMonths Active Patterns (Top 10):")
        month_patterns = results_df['Months_Active'].value_counts().head(10)
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        for pattern, count in month_patterns.items():
            if pattern:  # Skip empty patterns
                months = [month_names[int(m)-1] for m in pattern.split(',')]
                print(f"  {', '.join(months)}: {count:,} accounts")
    
    return results_df