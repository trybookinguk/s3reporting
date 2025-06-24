"""
Tier calculation logic for TryBooking accounts.
This module focuses solely on determining account tiers based on percentile rankings.
"""
from .config import TIER_PERCENTILES, MIN_YEARS_BY_TIER


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