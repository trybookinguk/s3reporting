"""
Activity rating determination logic for TryBooking accounts.
Analyzes account behavior patterns to classify activity status.
"""
import pandas as pd


def determine_activity_rating(current_freq, previous_freq, days_since_last, has_historical, 
                            avg_lead_days=60, last_event_date=None, months_active_list=None,
                            revenue_previous=0, industry=None, current_tier=None, 
                            account_postcode=None):
    """
    Determine activity rating based on event patterns and creation lead times.
    
    Args:
        current_freq: Event frequency for current period (Continuous/Regular/Seasonal/Annual/New/Inactive)
        previous_freq: Event frequency for previous period
        days_since_last: Days since last booking/transaction
        has_historical: Whether account has historical activity
        avg_lead_days: Average days between event creation and event date
        last_event_date: Date of the last event (for annual predictions)
        months_active_list: List of months (1-12) where account is typically active
        revenue_previous: Revenue from previous period (kept for compatibility)
        industry: Industry classification from Account report
        current_tier: Current tier classification (Key Account, High Value, Tier 4, Tier 3, etc.)
        account_postcode: Account postcode for Scottish school detection
    
    Returns:
        str: Activity rating (Active/Outreach/At Risk/Churned/Returned/New/Inactive)
    """
    # Active: Any current activity (except New)
    if current_freq not in ["Inactive", "New"]:
        return "Active"
    
    # New accounts - special handling with strict thresholds
    if current_freq == "New":
        # New accounts need quick activation - much stricter thresholds
        if days_since_last > 28:
            return "Churned"  # New account that didn't activate within 4 weeks
        elif days_since_last > 14:
            return "At Risk"  # New account with no activity after 2 weeks
        else:
            return "New"  # New account still in activation window
    
    # Returned: Was inactive, now active
    if current_freq != "Inactive" and previous_freq == "Inactive":
        return "Returned"
    
    # At Risk/Churned logic for accounts that were active but now inactive
    if previous_freq != "Inactive" and current_freq == "Inactive":
        
        # HIGH-TIER ANNUAL/SEASONAL EVENTS - Three stage approach
        # Tier 3 and above get proactive outreach
        high_tier = current_tier in ["Key Account", "High Value", "Tier 4", "Tier 3"]
        if previous_freq in ["Annual", "Seasonal"] and high_tier and last_event_date:
            today = pd.Timestamp.now()
            
            # Calculate expected timeline with some flexibility
            # Allow 30-day window for annual events (might not be exactly 365 days)
            expected_next_event = last_event_date + pd.Timedelta(days=365)
            expected_sale_start = expected_next_event - pd.Timedelta(days=avg_lead_days)
            outreach_date = expected_sale_start - pd.Timedelta(days=30)
            
            # Add grace period for natural variation
            grace_period = pd.Timedelta(days=30)
            
            # Determine stage with grace period
            if today.date() >= (expected_next_event + grace_period).date():
                return "Churned"  # Event should have happened (with grace period)
            elif today.date() >= expected_sale_start.date():
                return "At Risk"  # Should be selling tickets
            elif today.date() >= outreach_date.date():
                return "Outreach"  # Time for proactive contact
            else:
                return "Active"  # Too early to worry
        
        # REGULAR/CONTINUOUS EVENTS - Check for schools
        elif previous_freq in ["Regular", "Continuous"]:
            # Education industry - smart handling based on their pattern
            if industry == "Education" or (months_active_list and is_education_pattern(months_active_list)):
                current_date = pd.Timestamp.now()
                current_month = current_date.month
                
                # Check if this is a Scottish school
                is_scottish = account_postcode and is_scottish_postcode(account_postcode)
                
                # Check if this school runs summer programs
                runs_summer_programs = months_active_list and (7 in months_active_list or 8 in months_active_list)
                
                # Check if they typically run August events (Scottish schools return mid-August)
                runs_august_events = months_active_list and 8 in months_active_list
                
                # Check if they typically run September events
                runs_september_events = months_active_list and 9 in months_active_list
                
                if runs_summer_programs:
                    # Summer-active schools get no special treatment
                    days_to_check = days_since_last
                elif is_scottish and runs_august_events and current_month >= 8:
                    # Scottish schools that normally run August events need immediate attention
                    days_to_check = days_since_last
                elif not is_scottish and runs_september_events and current_month >= 9:
                    # English schools that normally run September events need immediate attention
                    days_to_check = days_since_last
                else:
                    # Traditional term-time only schools - exclude holidays
                    last_booking_date = pd.Timestamp.now() - pd.Timedelta(days=days_since_last)
                    
                    if is_scottish:
                        # Scottish schools: exclude July only (back mid-August)
                        if last_booking_date.month <= 6 and current_month in [7, 8]:
                            # Count days from last activity to start of July
                            days_before_summer = (pd.Timestamp(last_booking_date.year, 7, 1) - last_booking_date).days
                            # Add days since August 15th (typical return) if applicable
                            if current_month >= 8 and current_date.day >= 15:
                                days_after_summer = (current_date - pd.Timestamp(current_date.year, 8, 15)).days
                            else:
                                days_after_summer = 0
                            days_to_check = days_before_summer + days_after_summer
                        else:
                            days_to_check = days_since_last
                    else:
                        # English schools: exclude July and August
                        if last_booking_date.month <= 6 and current_month in [7, 8, 9]:
                            # Count days from last activity to start of July
                            days_before_summer = (pd.Timestamp(last_booking_date.year, 7, 1) - last_booking_date).days
                            # Add days since September started (if applicable)
                            days_after_summer = max(0, (current_date - pd.Timestamp(current_date.year, 9, 1)).days) if current_month >= 9 else 0
                            days_to_check = days_before_summer + days_after_summer
                        else:
                            days_to_check = days_since_last
            else:
                # Non-education accounts use actual days
                days_to_check = days_since_last
            
            # Apply thresholds using appropriate day count
            if previous_freq == "Continuous":
                if days_to_check > 90:
                    return "Churned"
                elif days_to_check > 45:
                    return "At Risk"
            else:  # Regular
                if days_to_check > 180:
                    return "Churned"
                elif days_to_check > 90:
                    return "At Risk"
        
        # Default fallback for other patterns
        else:
            if days_since_last > 365:
                return "Churned"
            elif days_since_last > 180:
                return "At Risk"
    
    return "Inactive"


def is_scottish_postcode(postcode):
    """
    Check if a postcode is Scottish based on the area code (first part).
    
    Args:
        postcode: UK postcode string (full or just area code)
        
    Returns:
        bool: True if Scottish postcode
    """
    if not postcode or not isinstance(postcode, str):
        return False
    
    # Scottish postcode areas
    scottish_areas = {
        'AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 
        'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'
    }
    
    # Get the area code (first 1-2 letters before any digits or spaces)
    area = postcode.strip().upper()
    
    # Extract just the letter portion at the start
    letter_portion = ''
    for char in area:
        if char.isalpha():
            letter_portion += char
        else:
            break
    
    # Check against Scottish areas
    if len(letter_portion) >= 2 and letter_portion[:2] in scottish_areas:
        return True
    elif letter_portion == 'G':  # Glasgow is just 'G'
        return True
    
    return False


def is_education_pattern(months_list):
    """
    Detect education/school patterns based on summer gap.
    Schools typically active Sept-June, quiet July-August.
    
    Args:
        months_list: List of month numbers (1-12) with activity
        
    Returns:
        bool: True if pattern matches education industry
    """
    if not months_list or len(months_list) < 6:
        return False
    
    # Check for summer gap (no July/August activity)
    has_summer_gap = 7 not in months_list and 8 not in months_list
    
    # Check for term-time activity (Sept-June)
    term_months = [9, 10, 11, 12, 1, 2, 3, 4, 5, 6]
    active_term_months = sum(1 for m in months_list if m in term_months)
    
    # If 70%+ active during term time and summer gap, likely education
    return has_summer_gap and active_term_months >= 7


def get_rating_transition_summary(results_df):
    """
    Generate a summary of rating transitions.
    
    Args:
        results_df: DataFrame with current and previous ratings
        
    Returns:
        dict: Summary of rating transitions and counts
    """
    if 'Rating' not in results_df.columns:
        return {}
    
    rating_counts = results_df['Rating'].value_counts()
    summary = {
        'distribution': rating_counts.to_dict(),
        'total_outreach': rating_counts.get('Outreach', 0),
        'total_at_risk': rating_counts.get('At Risk', 0),
        'total_churned': rating_counts.get('Churned', 0),
        'total_active': rating_counts.get('Active', 0),
        'total_new': rating_counts.get('New', 0),
        'total_returned': rating_counts.get('Returned', 0)
    }
    
    return summary


def identify_priority_accounts(results_df):
    """
    Identify accounts that need immediate attention.
    
    Args:
        results_df: DataFrame with ratings and tiers
        
    Returns:
        DataFrame: Priority accounts requiring outreach
    """
    priority_conditions = (
        # High-value accounts at risk
        ((results_df['Rating'] == 'At Risk') & 
         (results_df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4']))) |
        
        # Recently churned high-value accounts
        ((results_df['Rating'] == 'Churned') & 
         (results_df['Current_Tier'].isin(['Key Account', 'High Value'])) &
         (results_df.get('_days_since_last', 999) < 365))
    )
    
    if priority_conditions.any():
        return results_df[priority_conditions].sort_values(
            ['Current_Tier', 'Rating'], 
            ascending=[True, True]
        )
    
    return pd.DataFrame()