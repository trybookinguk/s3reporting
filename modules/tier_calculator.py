"""
Tier calculation logic for TryBooking accounts.
This module focuses solely on determining account tiers based on percentile rankings.
"""
import logging
from .utils.config import TIER_PERCENTILES, MIN_YEARS_BY_TIER

logger = logging.getLogger(__name__)


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


def batch_determine_tiers(accounts_data, batch_size=10000):
    """
    Process tier calculations in batches with progress logging.
    
    Args:
        accounts_data: List of tuples containing (a_pct, b_pct, c_years, d_pct, e_pct, has_activity)
        batch_size: Number of accounts to process per batch
        
    Returns:
        List of tier classifications
    """
    import time
    
    total_accounts = len(accounts_data)
    tiers = []
    
    logger.info(f"Starting tier calculation for {total_accounts:,} accounts")
    start_time = time.time()
    
    for i in range(0, total_accounts, batch_size):
        batch_start_time = time.time()
        batch_end = min(i + batch_size, total_accounts)
        batch = accounts_data[i:batch_end]
        
        # Process batch
        batch_tiers = [determine_tier_from_percentiles(*account) for account in batch]
        tiers.extend(batch_tiers)
        
        # Log progress with timing
        batch_time = time.time() - batch_start_time
        progress_pct = (batch_end / total_accounts) * 100
        accounts_per_sec = len(batch) / batch_time if batch_time > 0 else 0
        
        logger.info(f"Processed {batch_end:,} of {total_accounts:,} accounts ({progress_pct:.1f}%) - "
                   f"{accounts_per_sec:,.0f} accounts/sec")
    
    # Log tier distribution summary
    tier_counts = {}
    for tier in tiers:
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    
    total_time = time.time() - start_time
    logger.info(f"Tier calculation complete in {total_time:.1f}s ({total_accounts/total_time:,.0f} accounts/sec)")
    logger.info("Tier distribution:")
    
    for tier in ['Key Account', 'High Value', 'Tier 4', 'Tier 3', 'Tier 2', 'Tier 1', 'NIL']:
        if tier in tier_counts:
            count = tier_counts[tier]
            pct = (count / total_accounts) * 100
            logger.info(f"  {tier}: {count:,} accounts ({pct:.1f}%)")
    
    return tiers