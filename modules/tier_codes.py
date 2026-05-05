"""
Numeric tier codes for compact storage in the historical tier file.

Lower codes = better tiers. Free and Nil are off the ranked scale —
shown as separate bands in the email chart, but neither has owners.
"""

TIER_TO_INT = {
    "Tier 1": 1,
    "Tier 2": 2,
    "Tier 3": 3,
    "Tier 4": 4,
    "Tier 5": 5,
    "Free":   6,
    "Nil":    7,
}

INT_TO_TIER = {v: k for k, v in TIER_TO_INT.items()}

# Best-to-worst ordering (used for direction calc and chart y-position).
TIER_ORDER_BEST_TO_WORST = ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5", "Free", "Nil"]


def tier_to_int(tier: str) -> int:
    """Map a tier name to its compact integer code. Raises on unknown tiers."""
    if tier not in TIER_TO_INT:
        raise KeyError(f"Unknown tier: {tier!r}")
    return TIER_TO_INT[tier]


def int_to_tier(code: int) -> str:
    """Map a compact integer code back to its tier name. Raises on unknown codes."""
    if code not in INT_TO_TIER:
        raise KeyError(f"Unknown tier code: {code!r}")
    return INT_TO_TIER[code]
