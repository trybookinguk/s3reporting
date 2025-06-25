"""
Retention priority scoring for TryBooking accounts.
Combines tier, activity rating, and revenue drop to prioritize retention efforts.

Priority Formula: Priority = Tier Weight × (Rating Severity + Revenue Drop Score)

Note: Accounts with "Churned" rating are automatically excluded from standard CS workflows
regardless of their tier or revenue drop. These accounts receive a negative priority score
and are categorized as "Excluded" to prevent them from appearing in retention priority lists.
"""


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
        "Inactive": 0
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


def calculate_retention_priority(tier, activity_rating, revenue_drop_score):
    """
    Calculate the retention priority score.
    
    Priority = Tier Weight × (Rating Severity + Revenue Drop Score)
    
    Args:
        tier: Tier classification string (e.g., "Key Account", "High Value")
        activity_rating: Activity rating string (e.g., "Churned", "At Risk")
        revenue_drop_score: Revenue drop score (0-3) or category string
        
    Returns:
        int: Priority score (higher = higher priority)
    """
    # Get tier weight
    tier_weight = get_tier_weight(tier)
    
    # Get rating severity
    rating_severity = get_rating_severity(activity_rating)
    
    # Handle revenue drop score - could be int or string
    if isinstance(revenue_drop_score, str):
        revenue_score = get_revenue_drop_score(revenue_drop_score)
    else:
        revenue_score = revenue_drop_score
    
    # Calculate priority
    priority = tier_weight * (rating_severity + revenue_score)
    
    return priority


def categorize_priority(priority_score):
    """
    Categorize the priority score into actionable categories.
    
    Args:
        priority_score: Numeric priority score
        
    Returns:
        str: Priority category (Very High/High/Medium/Low/Excluded)
    """
    # Negative scores indicate churned accounts - exclude from workflows
    if priority_score < 0:
        return "Excluded"
    elif priority_score >= 40:
        return "Very High"
    elif priority_score >= 25:
        return "High"
    elif priority_score >= 10:
        return "Medium"
    else:
        return "Low"


def get_priority_action(priority_category, activity_rating, tier):
    """
    Get recommended action based on priority category.
    
    Args:
        priority_category: Priority category (Very High/High/Medium/Low)
        activity_rating: Activity rating string
        tier: Tier classification string
        
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
    
    category_actions = actions.get(priority_category, actions["Low"])
    return category_actions.get(activity_rating, category_actions.get("default", "Monitor"))


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
            row.get('revenue_drop_category', 'Stable')
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
            row.get('tier', 'Tier 1')
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
        'churned_accounts': len(accounts_df[accounts_df['activity_rating'] == 'Churned'])
    }
    
    return summary