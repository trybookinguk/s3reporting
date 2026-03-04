"""
Simplified tier calculation using a single weighted composite score.

Replaces the multi-path qualification system in the original tier calculator
with a straightforward percentile-based approach:
    composite = (0.55 x revenue_current) + (0.35 x revenue_lifetime) +
                (0.10 x years_loyalty)

Tickets are used solely as a qualifier (>= MIN_TICKETS_FOR_ACTIVE to activate)
rather than as a scoring component, avoiding double-penalising low-volume
high-value accounts. Free accounts with significant ticket volumes are better
assessed separately.

Lower scores indicate stronger accounts (matching the tier numbering where
Tier 1 is the best). Tiers are assigned from the composite percentile rank
among activated accounts (those with >= MIN_TICKETS_FOR_ACTIVE tickets in
the relevant period).
"""
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any

from modules.utils.config import MIN_TICKETS_FOR_ACTIVE

logger = logging.getLogger(__name__)

# Composite score weights (tickets used as qualifier only, not scored)
WEIGHT_A_REVENUE = 0.55
WEIGHT_B_LIFETIME = 0.35
WEIGHT_C_LOYALTY = 0.10

# VAT rate — revenue from the aggregator is VAT-inclusive; we strip it
# so figures reflect actual value to the business.
VAT_RATE = 1.20

# Tier bands: maximum composite percentile rank (lower score = better)
# Accounts at or below the threshold qualify for the tier.
TIER_BANDS = {
    'Tier 1': 2,    # Top 2%
    'Tier 2': 10,   # 3-10%
    'Tier 3': 25,   # 11-25%
    'Tier 4': 50,   # 26-50%
    'Tier 5': 100,  # 51-100%
}

# Numeric mapping for YoY movement calculation
TIER_NUMERIC = {
    'Tier 1': 1,
    'Tier 2': 2,
    'Tier 3': 3,
    'Tier 4': 4,
    'Tier 5': 5,
    'Nil': 6,
}


def _build_metrics_df(account_metrics: Dict[int, Dict[str, Any]]) -> pd.DataFrame:
    """
    Build a DataFrame from the BookingAggregator output dictionary.

    Extracts the four scoring metrics for current and previous periods,
    plus lifetime tickets for the activation filter.
    """
    rows = []
    for account_id, m in account_metrics.items():
        revenue_lifetime = float(m.get('revenue_lifetime', 0))
        revenue_current = float(m.get('revenue_current', 0))
        # Previous-period lifetime revenue: clamp to avoid float imprecision negatives
        revenue_lifetime_prev = max(0.0, revenue_lifetime - revenue_current)

        rows.append({
            'AccountId': account_id,
            'revenue_current': max(0.0, revenue_current / VAT_RATE),
            'revenue_lifetime': max(0.0, revenue_lifetime / VAT_RATE),
            'tickets_current': max(0, int(m.get('tickets_current', 0))),
            'years_loyalty': max(0, int(m.get('years_loyalty', 0))),
            'revenue_prev': max(0.0, float(m.get('revenue_prev', 0)) / VAT_RATE),
            'revenue_lifetime_prev': max(0.0, revenue_lifetime_prev / VAT_RATE),
            'tickets_prev': max(0, int(m.get('tickets_prev', 0))),
            'years_loyalty_prev': max(0, int(m.get('years_loyalty_prev', 0))),
            'tickets_lifetime': max(0, int(m.get('tickets_lifetime', 0))),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Filter to accounts that have ever transacted
    df = df[df['tickets_lifetime'] > 0].copy()
    logger.info(f"Metrics DataFrame built: {len(df):,} accounts with lifetime tickets > 0")
    return df


def _rank_percentiles(df: pd.DataFrame, columns: list,
                      mask: pd.Series = None) -> pd.DataFrame:
    """
    Calculate inverted percentile ranks (0-100) using average tie-breaking.

    Lower values indicate stronger performance (i.e. the best accounts score
    closest to 0).

    Args:
        df: DataFrame to rank.
        columns: Metric columns to rank.
        mask: Optional boolean mask. When provided, only rows where mask is
              True are ranked among each other. Rows where mask is False
              receive NaN.
    """
    for col in columns:
        if mask is not None:
            df[f'{col}_pct'] = pd.Series(dtype='float64', index=df.index)
            subset = df.loc[mask, col]
            df.loc[mask, f'{col}_pct'] = (
                (1 - subset.rank(pct=True, method='average')) * 100
            )
        else:
            df[f'{col}_pct'] = (1 - df[col].rank(pct=True, method='average')) * 100
    return df


def _compute_composite(df: pd.DataFrame, suffix: str = '') -> pd.Series:
    """
    Compute the weighted composite score from percentile columns.

    Args:
        df: DataFrame containing *_pct columns.
        suffix: Column name suffix to distinguish current vs previous period.

    Returns:
        Series of composite scores (0-100).
    """
    a = df[f'revenue_current{suffix}_pct']
    b = df[f'revenue_lifetime{suffix}_pct']
    c = df[f'years_loyalty{suffix}_pct']

    return (
        WEIGHT_A_REVENUE * a +
        WEIGHT_B_LIFETIME * b +
        WEIGHT_C_LOYALTY * c
    ).round(2)


def _assign_tier(composite_pct: pd.Series, activated_mask: pd.Series) -> pd.Series:
    """
    Assign tier labels from composite percentile ranks.

    Only activated accounts (meeting the minimum ticket threshold) receive a
    numbered tier. Others are labelled 'Nil'.

    Args:
        composite_pct: Percentile rank of the composite score among activated
                       accounts (0-100).
        activated_mask: Boolean mask — True for accounts meeting the activation
                        threshold in the relevant period.

    Returns:
        Series of tier labels.
    """
    tiers = pd.Series('Nil', index=composite_pct.index)

    # Only assign tiers to activated accounts
    active_scores = composite_pct[activated_mask]
    if active_scores.empty:
        return tiers

    # Percentiles are already ranked among activated accounts only, so
    # rank the composite scores directly to get the tier band position.
    active_rank = active_scores.rank(pct=True, method='average') * 100

    # Iterate from highest threshold to lowest so the most exclusive tier
    # overwrites broader ones (e.g. an account at p1 matches Tier 5 first,
    # then Tier 4, 3, 2, and finally Tier 1 which sticks).
    for tier, threshold in sorted(TIER_BANDS.items(), key=lambda x: x[1], reverse=True):
        tiers.loc[active_rank.index[active_rank <= threshold]] = tier

    return tiers


def _calculate_movement(current_tier: pd.Series, previous_tier: pd.Series) -> pd.Series:
    """
    Calculate YoY tier movement labels.

    Uses the numeric mapping (Tier 1=1 … Not Activated=6) so that an
    improvement yields a positive delta and a drop yields a negative delta.
    """
    current_num = current_tier.map(TIER_NUMERIC)
    previous_num = previous_tier.map(TIER_NUMERIC)
    delta = previous_num - current_num  # positive = improved

    movement = pd.Series('No Change', index=current_tier.index)
    movement[delta >= 2] = 'Improved 2+ tiers'
    movement[delta == 1] = 'Improved 1 tier'
    movement[delta == -1] = 'Dropped 1 tier'
    movement[delta <= -2] = 'Dropped 2+ tiers'
    return movement


def calculate_composite_tiers(account_metrics: Dict[int, Dict[str, Any]]) -> pd.DataFrame:
    """
    Main entry point: compute composite tiers for all accounts.

    Args:
        account_metrics: Dictionary keyed by AccountId, as returned by
                         BookingAggregator.aggregate_bookings().

    Returns:
        DataFrame with one row per account containing tier assignments,
        composite scores, raw metrics and percentile ranks.
    """
    if not account_metrics:
        logger.warning("No account metrics provided")
        return pd.DataFrame()

    df = _build_metrics_df(account_metrics)
    if df.empty:
        return pd.DataFrame()

    # --- Determine activation masks (tickets as qualifier only) ---
    current_activated = df['tickets_current'] >= MIN_TICKETS_FOR_ACTIVE
    logger.info(f"Activated accounts (current): {current_activated.sum():,} of {len(df):,}")

    # Rename previous-period columns to match the ranking helper's expectations
    df.rename(columns={
        'revenue_prev': 'revenue_current_prev',
        'revenue_lifetime_prev': 'revenue_lifetime_prev',  # already correct
        'tickets_prev': 'tickets_current_prev',
        'years_loyalty_prev': 'years_loyalty_prev',  # already correct
    }, inplace=True)

    previous_activated = df['tickets_current_prev'] >= MIN_TICKETS_FOR_ACTIVE

    # --- Current period percentile ranks (among activated only) ---
    df = _rank_percentiles(df, [
        'revenue_current', 'revenue_lifetime', 'years_loyalty',
    ], mask=current_activated)
    df['Composite_Score'] = _compute_composite(df)

    # --- Previous period percentile ranks (among previously activated only) ---
    df = _rank_percentiles(df, [
        'revenue_current_prev', 'revenue_lifetime_prev',
        'years_loyalty_prev',
    ], mask=previous_activated)
    df['Previous_Composite_Score'] = _compute_composite(df, suffix='_prev')

    # --- Tier assignment ---
    df['Current_Tier'] = _assign_tier(df['Composite_Score'], current_activated)
    df['Previous_Tier'] = _assign_tier(df['Previous_Composite_Score'], previous_activated)

    # --- YoY movement ---
    df['Tier_Movement'] = _calculate_movement(df['Current_Tier'], df['Previous_Tier'])

    # --- Build output DataFrame ---
    result = pd.DataFrame({
        'AccountId': df['AccountId'],
        'Current_Tier': df['Current_Tier'],
        'Previous_Tier': df['Previous_Tier'],
        'Tier_Movement': df['Tier_Movement'],
        'Composite_Score': df['Composite_Score'],
        'Previous_Composite_Score': df['Previous_Composite_Score'],
        'Revenue_Current': df['revenue_current'].round(2),
        'Revenue_Lifetime': df['revenue_lifetime'].round(2),
        'Tickets_Current': df['tickets_current'],
        'Years_Loyalty': df['years_loyalty'],
        'A_Percentile': df['revenue_current_pct'].round(2),
        'B_Percentile': df['revenue_lifetime_pct'].round(2),
        'C_Percentile': df['years_loyalty_pct'].round(2),
    })

    logger.info(f"Composite tier calculation complete: {len(result):,} accounts scored")
    return result
