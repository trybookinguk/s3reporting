# Revenue Factor Module Implementation

## Overview
The `modules/revenue_factor.py` module implements the strategic revenue analysis system for long-term trends and peer comparisons. It provides sophisticated revenue analysis with industry quintile calculations, seasonality handling, and special accommodations for education accounts and new businesses.

**Note**: This module focuses on year-over-year and rolling average comparisons for strategic risk assessment. For rapid revenue drop detection (4-week trends), see the rapid drop detection logic in `account_processor.py`.

## Key Functions

### 1. `calculate_industry_quintiles(industry_data, period_type='current')`
Calculates revenue quintiles for peer comparison within industries.

**Features:**
- Requires 10+ accounts for valid quintiles (MIN_ACCOUNTS_FOR_QUINTILES)
- Works at Industry level (SubIndustry no longer used)
- Supports different period types: 'current' (84 days), 'yoy' (365 days ago), 'rolling_average' (84 days)
- Tracks zero revenue percentage for normalization

**Returns:**
Dictionary mapping industry keys to quintile thresholds and statistics.

### 2. `get_comparison_period_for_pattern(account_pattern)`
Determines the appropriate comparison period based on account pattern.

**Logic:**
- Annual/Seasonal accounts: Year-over-year comparison
- Continuous/Regular accounts: Rolling average (84 days)
- Returns comparison period type string

### 3. `get_account_lifecycle_stage(account_age_days)`
Determines account lifecycle stage based on age.

**Stages:**
- new_building: 0-4 weeks
- new_expected: 5-8 weeks
- establishing: 9-12 weeks
- maturing: 13-52 weeks
- established: >52 weeks

### 4. `assess_revenue_trend(current_revenue, comparison_revenue, account_pattern='continuous')`
Assesses revenue trend severity and score.

**Scoring (Based on Ratio):**
- Severe (3): <25% of comparison revenue
- Significant (2): 25-50% of comparison revenue
- Moderate (1): 50-75% of comparison revenue
- Stable (0): ≥75% of comparison revenue

**Industry Quintile Adjustments:**
When quintiles are available, the score can be increased based on quintile drops:
- Drop 3+ quintiles: Upgrade to Severe (3)
- Drop 2 quintiles: Upgrade to Significant (2)
- Drop 1 quintile: Upgrade to Moderate (1)

### 5. `get_revenue_factor(current_revenue, historical_revenue, industry_data, account_type, account_info)`
Entry point for revenue analysis that orchestrates all the other functions.

**Returns:**
```python
{
    'severity': 'stable/moderate/significant/severe',
    'score': 0-3,              # Severity score
    'details': {
        'current_revenue': float,
        'comparison_revenue': float,
        'current_quintile': int,        # 1-5, if quintiles available
        'comparison_quintile': int,     # 1-5, if quintiles available
        'quintile_drop': int,           # If using quintiles
        'lifecycle_stage': str,         # Account age category
        'comparison_method': str,       # 'yoy' or 'rolling_average'
        'account_pattern': str,         # 'continuous', 'seasonal', 'annual'
        'industry_context': {           # If quintiles available
            'account_count': int,
            'zero_revenue_pct': float,
            'median_revenue': float
        }
    }
}
```

## Special Features

### New Account Handling
- Accounts <12 weeks old: Simple trend analysis without quintiles
- Accounts 12+ weeks old: Full quintile comparison with industry peers
- Lifecycle stages tracked for context but don't affect scoring directly

### Zero Revenue Normalization
When >30% of industry peers have zero revenue (ZERO_REVENUE_COMMON_THRESHOLD), accounts with zero revenue receive reduced severity scores (-1 level). This prevents false positives in industries with seasonal patterns or high dormancy rates.

### Education Account Handling
Education accounts are not given special treatment in this strategic revenue analysis. School holiday periods are handled separately in the retention priority module which can suppress alerts during expected quiet periods.

## Integration with Retention Priority

The revenue factor module is designed to integrate with the retention priority system:

1. **Standalone Usage:** Can be called independently to analyze any account's revenue patterns
2. **Retention Scoring:** The returned score (0-3) feeds directly into the retention priority calculation
3. **Industry Context:** Provides rich details about peer comparison for account managers

## Usage Example

```python
from modules.revenue_factor import get_revenue_factor

# Prepare data
current_revenue = 5000  # Current period revenue
historical_df = pd.DataFrame(...)  # Transaction history with dates
industry_df = pd.DataFrame(...)    # Industry peer data for quintiles

# Calculate revenue factor
result = get_revenue_factor(
    current_revenue=current_revenue,
    historical_revenue=historical_df,
    industry_data=industry_df,
    account_type='seasonal',  # or 'continuous', 'annual'
    account_info={
        'account_age_days': 180,  # Optional: for lifecycle stage
        'accounts_df': accounts_df  # Optional: can extract age from here
    }
)

# Result contains:
print(result['severity'])  # 'stable', 'moderate', 'significant', or 'severe'
print(result['score'])     # 0, 1, 2, or 3
print(result['details'])   # Rich context about the analysis
```

## Testing

The module includes comprehensive test coverage through `test_revenue_factor.py` which validates:
- Quintile calculations with diverse revenue distributions
- Various revenue drop scenarios (severe, moderate, stable)
- Seasonal handling for education accounts
- Edge cases like zero revenue and new accounts