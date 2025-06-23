"""
Tier calculation and business logic for TryBooking accounts.
"""
import pandas as pd
from .config import (
    CUTOFF_365, CUTOFF_730, TODAY, TIER_PERCENTILES, MIN_YEARS_BY_TIER,
    MIN_TICKETS_FOR_ACTIVE, EVENT_FREQUENCY_THRESHOLDS
)


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


def classify_event_frequency(event_count):
    """Convert event count to pattern classification."""
    if event_count == 0:
        return "Inactive"
    elif event_count == 1:
        return "Annual"
    elif event_count <= 3:
        return "Occasional"
    else:  # 4+
        return "Regular"


def determine_activity_rating(current_freq, previous_freq, days_since_last, has_historical, 
                            avg_lead_days=60, last_event_date=None):
    """Determine activity rating based on event patterns and creation lead times."""
    if current_freq != "Inactive":
        return "Active"
    
    if previous_freq == "Inactive" and not has_historical:
        return "New" if days_since_last < 365 else "Inactive"
    
    if current_freq != "Inactive" and previous_freq == "Inactive" and has_historical:
        return "Returned"
    
    # At Risk logic using creation lead times
    if previous_freq != "Inactive" and current_freq == "Inactive":
        # For annual/occasional events, check if we're past expected creation time
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
        
        # For regular events or if no date info, use simpler logic
        if days_since_last < 180:
            return "At Risk"
        else:
            return "Churned"
    
    return "Inactive"


def calculate_metrics_from_aggregated(account_metrics):
    """Calculate metrics from pre-aggregated account data."""
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
        
        # Include event tracking data
        event_data = {
            'event_ids_current': data.get('event_ids_current', set()),
            'event_ids_previous': data.get('event_ids_previous', set()),
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
            print(f"  Event IDs current: {len(event_data_sample.get('event_ids_current', set()))}")
            print(f"  Event IDs previous: {len(event_data_sample.get('event_ids_previous', set()))}")
    
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
        
        # Get last event date
        event_dates = [info['event_date'] for info in event_creation_info.values() if info['event_date']]
        last_event_date = max(event_dates).date() if event_dates else None
        
        # Determine activity rating with lead time consideration
        activity_rating = determine_activity_rating(
            event_freq_current, event_freq_previous, days_since_last, has_historical,
            avg_lead_days=avg_lead_days, last_event_date=last_event_date
        )
            
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
            # Hidden fields for report generation (prefix with _)
            "_avg_lead_days": avg_lead_days,
            "_last_event_date": last_event_date,
            "_event_count_current": event_count_current,
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
        for rating in ['Active', 'At Risk', 'Churned', 'Returned', 'New', 'Inactive']:
            count = rating_counts.get(rating, 0)
            print(f"  {rating}: {count:,} accounts")
    
    return results_df