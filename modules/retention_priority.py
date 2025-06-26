"""
Retention priority scoring for TryBooking accounts.
Combines tier, activity rating, revenue drop, and rapid drop alerts to prioritize retention efforts.

Priority Formula: Priority = Tier Weight × (Rating Severity + Revenue Drop Score)

Rapid Drop Alert Boosting:
- Rapid drop alerts (0-3) indicate sudden revenue declines requiring immediate attention
- Alert level 2+: Ensures minimum priority score of 70 (High priority)
- Alert level 3 for Key/High Value accounts: Sets priority to 90 (Very High priority)

Annual Reachout Boosting:
- Annual accounts approaching their typical booking window get High priority for proactive outreach
- Based on months_active pattern and event creation tracking

Note: Accounts with "Churned" rating are automatically excluded from standard CS workflows
regardless of their tier or revenue drop. These accounts receive a negative priority score
and are categorized as "Excluded" to prevent them from appearing in retention priority lists.
"""

from datetime import datetime
import calendar


def get_tier_weight(tier_name):
    """
    Get the weight for a given tier.
    
    Args:
        tier_name: Tier classification string
        
    Returns:
        int: Weight value (1-5)
    """
    tier_weights = {
        "Key Account": 5,
        "High Value": 4,
        "Tier 4": 3,
        "Tier 3": 2,
        "Tier 2": 1,
        "Tier 1": 1,
        "NIL": 1
    }
    
    # Default to 1 for unknown tiers
    return tier_weights.get(tier_name, 1)


def get_rating_severity(rating):
    """
    Get the severity score for an activity rating.
    
    Args:
        rating: Activity rating string
        
    Returns:
        int: Severity score (-1 to 7)
    """
    rating_severity = {
        "Churned": -1,  # Negative score to exclude from standard workflows
        "At Risk": 7,
        "Outreach": 4,
        "New": 3,
        "Returned": 1,
        "Active": 0,
        "Inactive": 0   # Truly inactive accounts shouldn't be high-tier anyway
    }
    
    # Default to 0 for unknown ratings
    return rating_severity.get(rating, 0)


def get_revenue_drop_score(revenue_drop_category):
    """
    Get the score for a revenue drop category.
    
    Args:
        revenue_drop_category: Revenue drop category string (Severe/Significant/Moderate/Stable)
        
    Returns:
        int: Revenue drop score (0-3)
    """
    revenue_scores = {
        "Severe": 3,       # >75% drop
        "Significant": 2,  # 50-75% drop
        "Moderate": 1,     # 25-50% drop
        "Stable": 0        # <25% drop or growth
    }
    
    # Default to 0 for unknown categories
    return revenue_scores.get(revenue_drop_category, 0)


def get_rapid_drop_alert_description(alert_level):
    """
    Get a description for a rapid drop alert level.
    
    Args:
        alert_level: Rapid drop alert level (0-3)
        
    Returns:
        str: Description of the alert level
    """
    alert_descriptions = {
        0: "No rapid drop detected",
        1: "Minor rapid drop - monitoring recommended",
        2: "Significant rapid drop - immediate attention required",
        3: "Severe rapid drop - critical intervention needed"
    }
    
    # Default to unknown for invalid levels
    return alert_descriptions.get(alert_level, "Unknown alert level")


def calculate_revenue_drop_category(current_revenue, previous_revenue):
    """
    Calculate the revenue drop category based on current vs previous revenue.
    
    Args:
        current_revenue: Revenue in current period
        previous_revenue: Revenue in previous period
        
    Returns:
        str: Revenue drop category (Severe/Significant/Moderate/Stable)
    """
    # Handle edge cases
    if previous_revenue <= 0:
        # No previous revenue - consider stable if they have current revenue
        return "Stable" if current_revenue > 0 else "Severe"
    
    # Calculate percentage drop
    drop_percentage = ((previous_revenue - current_revenue) / previous_revenue) * 100
    
    # Categorize based on drop percentage
    if drop_percentage > 75:
        return "Severe"
    elif drop_percentage > 50:
        return "Significant"
    elif drop_percentage > 25:
        return "Moderate"
    else:
        return "Stable"


def calculate_retention_priority(tier, activity_rating, revenue_drop_score, rapid_drop_alert=0, previous_tier=None, 
                               event_frequency=None, months_active=None, has_created_event_this_period=False):
    """
    Calculate the retention priority score.
    
    Base Priority = Tier Weight × (Rating Severity + Revenue Drop Score)
    
    Minimum priority thresholds:
    - High-value accounts (Key Account, High Value, Tier 4, Tier 3): Minimum Medium priority
    - Unless they are Churned (excluded from workflows)
    
    Tier drop boost:
    - Accounts that dropped tiers get priority boost (indicates declining performance)
    
    Annual reachout boost:
    - Annual accounts approaching their typical booking window without event creation get High priority
    
    Additional logic for rapid drop alerts:
    - If rapid_drop_alert >= 2: Minimum score of 70 (High priority)  
    - If rapid_drop_alert == 3 and tier in ['Key Account', 'High Value']: Score set to 90 (Very High)
    
    Args:
        tier: Tier classification string (e.g., "Key Account", "High Value")
        activity_rating: Activity rating string (e.g., "Churned", "At Risk")
        revenue_drop_score: Revenue drop score (0-3) or category string
        rapid_drop_alert: Rapid drop alert level (0-3, where 0=no alert, 3=severe rapid drop)
        previous_tier: Previous tier classification (optional, for tier drop detection)
        event_frequency: Event frequency classification (for annual reachout detection)
        months_active: List of typical active months (for annual reachout detection)
        has_created_event_this_period: Whether account has created event in current period
        
    Returns:
        int: Priority score (higher = higher priority)
    """
    # Validate inputs and apply defaults for robustness
    try:
        # Get tier weight with validation
        tier_weight = get_tier_weight(tier) if tier else 1
        
        # Get rating severity with validation  
        rating_severity = get_rating_severity(activity_rating) if activity_rating else 0
        
        # Handle revenue drop score - could be int or string
        if isinstance(revenue_drop_score, str):
            revenue_score = get_revenue_drop_score(revenue_drop_score)
        elif isinstance(revenue_drop_score, (int, float)):
            revenue_score = max(0, min(3, int(revenue_drop_score)))  # Clamp to 0-3
        else:
            revenue_score = 0  # Default for invalid input
        
        # Validate rapid drop alert
        if not isinstance(rapid_drop_alert, (int, float)):
            rapid_drop_alert = 0
        else:
            rapid_drop_alert = max(0, min(3, int(rapid_drop_alert)))  # Clamp to 0-3
        
        # Calculate base priority
        priority = tier_weight * (rating_severity + revenue_score)
        
    except Exception as e:
        # Fallback calculation if anything fails
        logger.warning(f"Error in retention priority calculation for tier={tier}, rating={activity_rating}: {e}")
        priority = 10  # Safe default - Medium priority
    
    # High-value tiers (Tier 3 and above)
    HIGH_VALUE_TIERS = ['Key Account', 'High Value', 'Tier 4', 'Tier 3']
    
    # Apply minimum priority for high-value accounts (unless churned)
    if tier in HIGH_VALUE_TIERS and activity_rating != 'Churned':
        # Ensure minimum Medium priority (score of 10)
        priority = max(priority, 10)
    
    # Tier drop boost - indicates declining performance
    if previous_tier and previous_tier != tier:
        tier_hierarchy = ["NIL", "Tier 1", "Tier 2", "Tier 3", "Tier 4", "High Value", "Key Account"]
        
        try:
            prev_index = tier_hierarchy.index(previous_tier)
            current_index = tier_hierarchy.index(tier)
            
            # If current tier is lower in hierarchy (declined)
            if current_index < prev_index:
                tier_drop_severity = prev_index - current_index
                # Add boost based on severity of drop
                if tier_drop_severity >= 3:  # Major drop (e.g., Key Account -> Tier 3)
                    priority += 15
                elif tier_drop_severity >= 2:  # Significant drop (e.g., High Value -> Tier 3)
                    priority += 10
                else:  # Minor drop (e.g., Tier 4 -> Tier 3)
                    priority += 5
                    
        except ValueError:
            # Unknown tier names - skip boost
            pass
    
    # Annual reachout boost - proactive outreach for annual accounts approaching their window
    if (event_frequency == 'Annual' and 
        months_active and 
        not has_created_event_this_period and
        activity_rating != 'Churned'):
        
        # Check if we're approaching their typical booking month
        current_month = datetime.now().month
        current_month_name = calendar.month_name[current_month]
        
        # Convert months_active to month numbers for comparison
        month_name_to_number = {calendar.month_name[i]: i for i in range(1, 13)}
        
        for active_month_name in months_active:
            if active_month_name in month_name_to_number:
                active_month_num = month_name_to_number[active_month_name]
                
                # Check if we're within 1-2 months of their typical booking month
                # Handle year boundary (e.g., November event, checking in September/October)
                months_until_event = (active_month_num - current_month) % 12
                
                # If we're 1-2 months before their typical booking month
                if months_until_event in [1, 2]:
                    # Boost to High priority (minimum score 18 to reach "High" band)
                    priority = max(priority, 18)
                    break
    
    # Apply rapid drop alert boosting logic
    # Rapid drops indicate immediate attention needed regardless of other factors
    if rapid_drop_alert >= 2:
        # Ensure minimum score of 70 for significant rapid drops
        priority = max(priority, 70)
        
        # Critical accounts with severe rapid drops get maximum priority
        if rapid_drop_alert == 3 and tier in ['Key Account', 'High Value']:
            priority = 90
    
    return priority


def categorize_priority(priority_score):
    """
    Categorize the priority score into actionable categories.
    
    Thresholds aligned to allow Tier 3/4 to reach Very High:
    - Tier 3 maximum: 2×(7+3) = 20
    - Tier 4 maximum: 3×(7+3) = 30
    
    Args:
        priority_score: Numeric priority score
        
    Returns:
        str: Priority category (Very High/High/Medium/Low/Excluded)
    """
    # Negative scores indicate churned accounts - exclude from workflows
    if priority_score < 0:
        return "Excluded"
    elif priority_score >= 20:
        return "Very High"
    elif priority_score >= 15:
        return "High"
    elif priority_score >= 10:
        return "Medium"
    else:
        return "Low"


def get_priority_action(priority_category, activity_rating, tier, rapid_drop_alert=0):
    """
    Get recommended action based on priority category.
    
    Args:
        priority_category: Priority category (Very High/High/Medium/Low)
        activity_rating: Activity rating string
        tier: Tier classification string
        rapid_drop_alert: Rapid drop alert level (0-3), optional
        
    Returns:
        str: Recommended action
    """
    actions = {
        "Very High": {
            "At Risk": "Urgent intervention - schedule call within 24-48 hours",
            "Outreach": "Priority outreach - personalized campaign within 1 week",
            "default": "High-priority monitoring and support"
        },
        "High": {
            "At Risk": "Proactive support - reach out within 1 week",
            "Outreach": "Scheduled outreach - include in next campaign batch",
            "default": "Regular check-ins and support"
        },
        "Medium": {
            "At Risk": "Monitor closely - automated alerts for further decline",
            "Outreach": "Standard outreach sequence",
            "default": "Standard support and monitoring"
        },
        "Low": {
            "default": "Standard communications and self-service support"
        },
        "Excluded": {
            "Churned": "Account excluded from standard CS workflows - consider specialized win-back campaigns",
            "default": "Account excluded from standard CS workflows"
        }
    }
    
    # Get base action
    category_actions = actions.get(priority_category, actions["Low"])
    base_action = category_actions.get(activity_rating, category_actions.get("default", "Monitor"))
    
    # Enhance action based on rapid drop alert
    if rapid_drop_alert >= 3:
        base_action = f"CRITICAL RAPID DROP - {base_action}. Immediate executive escalation required."
    elif rapid_drop_alert == 2:
        base_action = f"RAPID DROP ALERT - {base_action}. Prioritize within 24 hours."
    
    return base_action


def analyze_portfolio_priorities(accounts_df):
    """
    Analyze a portfolio of accounts and calculate priorities.
    
    Args:
        accounts_df: DataFrame with columns: tier, activity_rating, 
                    current_revenue, previous_revenue
                    
    Returns:
        DataFrame: Original data with priority scores and categories added
    """
    import pandas as pd
    
    # Create a copy to avoid modifying original
    results = accounts_df.copy()
    
    # Calculate revenue drop categories if not present
    if 'revenue_drop_category' not in results.columns:
        results['revenue_drop_category'] = results.apply(
            lambda row: calculate_revenue_drop_category(
                row.get('current_revenue', 0),
                row.get('previous_revenue', 0)
            ),
            axis=1
        )
    
    # Calculate priority scores
    results['priority_score'] = results.apply(
        lambda row: calculate_retention_priority(
            row.get('tier', 'Tier 1'),
            row.get('activity_rating', 'Active'),
            row.get('revenue_drop_category', 'Stable'),
            row.get('rapid_drop_alert', 0)  # Include rapid drop alert if present
        ),
        axis=1
    )
    
    # Categorize priorities
    results['priority_category'] = results['priority_score'].apply(categorize_priority)
    
    # Add recommended actions
    results['recommended_action'] = results.apply(
        lambda row: get_priority_action(
            row['priority_category'],
            row.get('activity_rating', 'Active'),
            row.get('tier', 'Tier 1'),
            row.get('rapid_drop_alert', 0)
        ),
        axis=1
    )
    
    # Sort by priority score descending
    results = results.sort_values('priority_score', ascending=False)
    
    return results


def get_priority_summary(accounts_df):
    """
    Generate a summary of retention priorities across the portfolio.
    
    Args:
        accounts_df: DataFrame with priority analysis results
        
    Returns:
        dict: Summary statistics
    """
    if 'priority_category' not in accounts_df.columns:
        accounts_df = analyze_portfolio_priorities(accounts_df)
    
    # Separate excluded accounts for clarity
    excluded_accounts = accounts_df[accounts_df['priority_category'] == 'Excluded']
    active_accounts = accounts_df[accounts_df['priority_category'] != 'Excluded']
    
    # Check for rapid drop alerts
    rapid_drop_accounts = accounts_df[accounts_df.get('rapid_drop_alert', 0) >= 2] if 'rapid_drop_alert' in accounts_df.columns else accounts_df[accounts_df['rapid_drop_alert'] >= 2] if 'rapid_drop_alert' in accounts_df else None
    
    summary = {
        'total_accounts': len(accounts_df),
        'active_accounts': len(active_accounts),
        'excluded_accounts': len(excluded_accounts),
        'priority_distribution': accounts_df['priority_category'].value_counts().to_dict(),
        'very_high_priority_accounts': accounts_df[
            (accounts_df['priority_category'] == 'Very High') & 
            (accounts_df['activity_rating'] != 'Churned')
        ][
            ['AccountId', 'AccountName', 'tier', 'activity_rating', 'priority_score']
        ].head(10).to_dict('records') if 'AccountId' in accounts_df.columns else [],
        'average_priority_score': active_accounts['priority_score'].mean() if len(active_accounts) > 0 else 0,
        'highest_priority_score': active_accounts['priority_score'].max() if len(active_accounts) > 0 else 0,
        'accounts_needing_action': len(accounts_df[
            accounts_df['priority_category'].isin(['Very High', 'High'])
        ]),
        'churned_accounts': len(accounts_df[accounts_df['activity_rating'] == 'Churned']),
        'rapid_drop_alerts': {
            'total': len(rapid_drop_accounts) if rapid_drop_accounts is not None else 0,
            'critical': len(accounts_df[accounts_df.get('rapid_drop_alert', 0) == 3]) if 'rapid_drop_alert' in accounts_df.columns else 0,
            'significant': len(accounts_df[accounts_df.get('rapid_drop_alert', 0) == 2]) if 'rapid_drop_alert' in accounts_df.columns else 0
        }
    }
    
    return summary