# Revenue Factor Module Implementation

## Overview
The `modules/revenue_factor.py` module implements the revenue drop monitoring system as specified in the retention priority plan. It provides sophisticated revenue analysis with industry quintile calculations, seasonality handling, and special accommodations for education accounts and new businesses.

## Key Functions

### 1. `calculate_industry_quintiles(booking_df, accounts_df, time_period='current')`
Calculates revenue quintiles for peer comparison within industries.

**Features:**
- Requires 100+ accounts with 6+ months history for valid quintiles
- Tries SubIndustry first, then falls back to Industry level
- Supports current period and seasonal (YoY) comparisons
- Tracks zero revenue percentage for normalization

**Returns:**
Dictionary mapping industry keys to quintile thresholds and statistics.

### 2. `get_seasonal_comparison_period(current_date, account_type, months_active, is_education, is_scottish)`
Determines the appropriate comparison period based on account characteristics.

**Logic:**
- 12+ months history: YoY for seasonal, 12-week rolling average for continuous
- 3-12 months: Average since account creation
- Returns start date, end date, and comparison type

### 3. `handle_education_seasonality(revenue_data, is_scottish=False)`
Special handling for education accounts to exclude holiday periods.

**Exclusions:**
- English/Welsh schools: July and August
- Scottish schools: June 25 - August 15
- Christmas break: December 15-31

### 4. `calculate_revenue_drop_score(account_data, industry_quintiles, account_pattern, account_info)`
Main scoring function that calculates the revenue drop severity.

**Scoring (Industry Quintiles Available):**
- Severe (3): Dropped 4 quintiles OR dropped to Q1 from Q3+
- Significant (2): Dropped 3 quintiles
- Moderate (1): Dropped 2 quintiles
- Stable (0): Dropped 1 or no drop

**Scoring (Activity-Based Fallback):**
- Year-over-Year (seasonal accounts):
  - Severe (3): <30% of last year OR zero when active before
  - Significant (2): 30-60% of last year
  - Moderate (1): 60-80% of last year
  
- Rolling Average (continuous accounts):
  - Severe (3): <25% of 12-week average OR zero for 6+ weeks
  - Significant (2): 25-50% of 12-week average
  - Moderate (1): 50-75% of 12-week average

### 5. `get_revenue_factor(current_revenue, historical_revenue, industry_data, account_type, account_info)`
Entry point for revenue analysis that orchestrates all the other functions.

**Returns:**
```python
{
    'score': 0-3,              # Severity score
    'factor': 'revenue_drop',
    'severity': 'none/moderate/significant/severe',
    'details': {
        'method': 'industry_quintiles' or 'activity_based',
        'current_revenue': float,
        'comparison_revenue': float,
        'quintile_drop': int,  # If using quintiles
        'drop_percentage': float,  # If using activity-based
        ...
    }
}
```

## Special Features

### New Account Handling
- Week 1-4: Building phase (no scoring)
- Week 5-8: Expected revenue phase (flag if zero)
- Week 9+: Compare to tier cohort average

### Zero Revenue Normalization
When >30% of industry peers have zero revenue, accounts with zero revenue receive no penalty. This prevents false positives in industries with seasonal patterns or high dormancy rates.

### Scottish School Detection
Uses postcode prefixes to identify Scottish schools:
AB, DD, DG, EH, FK, G, HS, IV, KA, KW, KY, ML, PA, PH, TD, ZE

## Integration with Retention Priority

The revenue factor module is designed to integrate with the retention priority system:

1. **Standalone Usage:** Can be called independently to analyze any account's revenue patterns
2. **Retention Scoring:** The returned score (0-3) feeds directly into the retention priority calculation
3. **Industry Context:** Provides rich details about peer comparison for account managers

## Usage Example

```python
from modules.revenue_factor import get_revenue_factor

# Prepare data
current_revenue = 5000  # Last 4 weeks
historical_df = pd.DataFrame(...)  # Transaction history
industry_df = pd.DataFrame(...)    # Optional peer data

# Calculate revenue factor
result = get_revenue_factor(
    current_revenue=current_revenue,
    historical_revenue=historical_df,
    industry_data=industry_df,
    account_type='seasonal'  # or 'continuous', 'annual'
)

# Use the score in retention priority
retention_score = calculate_retention_priority(
    tier="High Value",
    activity_rating="At Risk",
    revenue_drop_score=result['score']  # 0-3
)
```

## Testing

The module includes comprehensive test coverage through `test_revenue_factor.py` which validates:
- Quintile calculations with diverse revenue distributions
- Various revenue drop scenarios (severe, moderate, stable)
- Seasonal handling for education accounts
- Edge cases like zero revenue and new accounts