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

from modules.utils.config import MIN_TICKETS_FOR_ACTIVE, YEARS_LOYALTY_CAP

logger = logging.getLogger(__name__)

# Composite score weights (tickets used as qualifier only, not scored)
WEIGHT_A_REVENUE = 0.55
WEIGHT_B_LIFETIME = 0.35
WEIGHT_C_LOYALTY = 0.10

# Default VAT rate — revenue from the aggregator is VAT-inclusive; we strip
# it so figures reflect actual value to the business. Callers that already
# hand in ex-VAT figures should pass vat_rate=1.0 to avoid double-stripping.
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
    'Free': 6,
    'Nil': 7,
}


def _build_metrics_df(account_metrics: Dict[int, Dict[str, Any]],
                      vat_rate: float = VAT_RATE) -> pd.DataFrame:
    """
    Build a DataFrame from the BookingAggregator output dictionary.

    Extracts the four scoring metrics for current and previous periods,
    plus lifetime tickets for the activation filter.
    """
    if not account_metrics:
        return pd.DataFrame()

    # Construct via from_dict + orient='index' so we avoid a Python-level
    # row loop. Missing keys are tolerated by reindexing the columns we
    # care about and filling NaN with 0 before the numeric coercions.
    raw = pd.DataFrame.from_dict(account_metrics, orient='index')
    expected_cols = [
        'revenue_lifetime', 'revenue_current', 'revenue_prev',
        'tickets_lifetime', 'tickets_current', 'tickets_prev',
        'years_loyalty', 'years_loyalty_prev',
    ]
    raw = raw.reindex(columns=expected_cols).fillna(0)

    revenue_lifetime = pd.to_numeric(raw['revenue_lifetime'], errors='coerce').fillna(0).astype(float)
    revenue_current = pd.to_numeric(raw['revenue_current'], errors='coerce').fillna(0).astype(float)
    revenue_prev = pd.to_numeric(raw['revenue_prev'], errors='coerce').fillna(0).astype(float)
    # Previous-period lifetime revenue: clamp to avoid float imprecision negatives
    revenue_lifetime_prev = (revenue_lifetime - revenue_current).clip(lower=0.0)

    tickets_current = pd.to_numeric(raw['tickets_current'], errors='coerce').fillna(0).astype(int)
    tickets_prev = pd.to_numeric(raw['tickets_prev'], errors='coerce').fillna(0).astype(int)
    tickets_lifetime = pd.to_numeric(raw['tickets_lifetime'], errors='coerce').fillna(0).astype(int)

    # Cap years_loyalty so tenure beyond YEARS_LOYALTY_CAP doesn't keep
    # stacking. Accounts at or above the cap tie at the top of the loyalty
    # distribution; accounts below it rank against each other normally.
    years_loyalty = (
        pd.to_numeric(raw['years_loyalty'], errors='coerce').fillna(0).astype(int)
        .clip(lower=0, upper=YEARS_LOYALTY_CAP)
    )
    years_loyalty_prev = (
        pd.to_numeric(raw['years_loyalty_prev'], errors='coerce').fillna(0).astype(int)
        .clip(lower=0, upper=YEARS_LOYALTY_CAP)
    )

    df = pd.DataFrame({
        'AccountId': raw.index,
        'revenue_current': (revenue_current / vat_rate).clip(lower=0.0),
        'revenue_lifetime': (revenue_lifetime / vat_rate).clip(lower=0.0),
        'tickets_current': tickets_current.clip(lower=0),
        'years_loyalty': years_loyalty,
        'revenue_prev': (revenue_prev / vat_rate).clip(lower=0.0),
        'revenue_lifetime_prev': (revenue_lifetime_prev / vat_rate).clip(lower=0.0),
        'tickets_prev': tickets_prev.clip(lower=0),
        'years_loyalty_prev': years_loyalty_prev,
        'tickets_lifetime': tickets_lifetime.clip(lower=0),
    }).reset_index(drop=True)

    df = df[df['tickets_lifetime'] > 0].copy()
    logger.debug(f"Metrics DataFrame built: {len(df):,} accounts with lifetime tickets > 0")
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


def calculate_composite_tiers(account_metrics: Dict[int, Dict[str, Any]],
                              vat_rate: float = VAT_RATE) -> pd.DataFrame:
    """
    Main entry point: compute composite tiers for all accounts.

    Args:
        account_metrics: Dictionary keyed by AccountId, as returned by
                         BookingAggregator.aggregate_bookings().
        vat_rate: Divisor applied to revenue figures to strip VAT. Defaults
                  to 1.20 because the aggregator emits VAT-inclusive revenue.
                  Callers feeding in already-ex-VAT figures must pass 1.0
                  to avoid double-stripping.

    Returns:
        DataFrame with one row per account containing tier assignments,
        composite scores, raw metrics and percentile ranks.
    """
    if not account_metrics:
        logger.warning("No account metrics provided")
        return pd.DataFrame()

    df = _build_metrics_df(account_metrics, vat_rate=vat_rate)
    if df.empty:
        return pd.DataFrame()

    # --- Determine activation masks (tickets as qualifier only) ---
    current_activated = df['tickets_current'] >= MIN_TICKETS_FOR_ACTIVE
    current_has_revenue = df['revenue_current'] > 0
    # Paid activated: enough tickets AND has revenue — these get scored/tiered
    current_paid_activated = current_activated & current_has_revenue
    # Free: enough tickets but zero revenue — separate "Free" tier
    current_free = current_activated & ~current_has_revenue
    logger.debug(
        f"Activated accounts (current): {current_activated.sum():,} of {len(df):,} "
        f"(paid: {current_paid_activated.sum():,}, free: {current_free.sum():,})"
    )

    # Rename previous-period columns to match the ranking helper's expectations
    df.rename(columns={
        'revenue_prev': 'revenue_current_prev',
        'revenue_lifetime_prev': 'revenue_lifetime_prev',  # already correct
        'tickets_prev': 'tickets_current_prev',
        'years_loyalty_prev': 'years_loyalty_prev',  # already correct
    }, inplace=True)

    previous_activated = df['tickets_current_prev'] >= MIN_TICKETS_FOR_ACTIVE
    previous_has_revenue = df['revenue_lifetime_prev'] > 0
    previous_paid_activated = previous_activated & previous_has_revenue
    previous_free = previous_activated & ~previous_has_revenue

    # --- Current period percentile ranks (among paid activated only) ---
    df = _rank_percentiles(df, [
        'revenue_current', 'revenue_lifetime', 'years_loyalty',
    ], mask=current_paid_activated)
    # Accounts at the loyalty cap are treated as fully loyal — force their
    # percentile to 0 (best) rather than the tied-group average.
    df.loc[current_paid_activated & (df['years_loyalty'] >= YEARS_LOYALTY_CAP),
           'years_loyalty_pct'] = 0.0
    df['Composite_Score'] = _compute_composite(df)

    # --- Previous period percentile ranks (among previously paid activated only) ---
    df = _rank_percentiles(df, [
        'revenue_current_prev', 'revenue_lifetime_prev',
        'years_loyalty_prev',
    ], mask=previous_paid_activated)
    df.loc[previous_paid_activated & (df['years_loyalty_prev'] >= YEARS_LOYALTY_CAP),
           'years_loyalty_prev_pct'] = 0.0
    df['Previous_Composite_Score'] = _compute_composite(df, suffix='_prev')

    # --- Tier assignment ---
    df['Current_Tier'] = _assign_tier(df['Composite_Score'], current_paid_activated)
    df.loc[current_free, 'Current_Tier'] = 'Free'
    df['Previous_Tier'] = _assign_tier(df['Previous_Composite_Score'], previous_paid_activated)
    df.loc[previous_free, 'Previous_Tier'] = 'Free'

    # --- YoY movement ---
    df['Tier_Movement'] = _calculate_movement(df['Current_Tier'], df['Previous_Tier'])

    # --- Build output DataFrame ---
    # Revenue_Current_Prev / Revenue_Lifetime_Prev are the same metrics 365 days
    # ago, surfaced for downstream rank-vs-year-ago comparisons in the
    # tier-movement email. Internally already computed for percentile ranking.
    result = pd.DataFrame({
        'AccountId': df['AccountId'],
        'Current_Tier': df['Current_Tier'],
        'Previous_Tier': df['Previous_Tier'],
        'Tier_Movement': df['Tier_Movement'],
        'Composite_Score': df['Composite_Score'],
        'Previous_Composite_Score': df['Previous_Composite_Score'],
        'Revenue_Current': df['revenue_current'].round(2),
        'Revenue_Lifetime': df['revenue_lifetime'].round(2),
        'Revenue_Current_Prev': df['revenue_current_prev'].round(2),
        'Revenue_Lifetime_Prev': df['revenue_lifetime_prev'].round(2),
        'Tickets_Current': df['tickets_current'],
        'Years_Loyalty': df['years_loyalty'],
        'A_Percentile': df['revenue_current_pct'].round(2),
        'B_Percentile': df['revenue_lifetime_pct'].round(2),
        'C_Percentile': df['years_loyalty_pct'].round(2),
    })

    logger.debug(f"Composite tier calculation complete: {len(result):,} accounts scored")
    return result
