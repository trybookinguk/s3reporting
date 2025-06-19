# Industry-Aware Percentile-Based Churn Risk Model

## Overview

This document outlines a comprehensive plan for implementing an industry-aware, percentile-based churn risk scoring system for TryBooking UK accounts. The model produces a single score (0-100) that prioritizes accounts by their likelihood to churn, weighted by revenue impact.

## Core Principles

1. **Percentile-Based**: All comparisons are relative to the account population, automatically adapting as the business evolves
2. **Industry-Aware**: Accounts are compared to peers in their industry when sufficient data exists
3. **Revenue-Focused**: Naturally prioritizes high-revenue accounts through percentile rankings
4. **Dynamic**: All thresholds and factors are calculated from current data, not hardcoded

## Data Requirements

### Primary Data Sources

1. **Booking Data** (existing)
   - Files: `YYYYMM01-BookingDataAll-TBUK.csv` and `YYYYMM-BookingData-TBUK.csv`
   - Contains: Transaction history, revenue, event counts
   - Key field: `AccountId`

2. **Accounts Data** (new addition)
   - File: `YYYYMM-Accounts-TBUK.csv`
   - Contains: Industry classification, account metadata
   - Key field: `AccountId` (matches booking data)

### Data Integration

```python
# Fetch Accounts report for industry data
accounts_key = f"{year}/{month}/{prefix}-Accounts-TBUK.csv"
accounts_df = pd.read_csv(s3_client.get_object(Bucket=BUCKET, Key=accounts_key)['Body'])

# Create lookups for both industry and sub-industry
industry_lookup = dict(zip(accounts_df['AccountId'], accounts_df['Industry']))
sub_industry_lookup = dict(zip(accounts_df['AccountId'], accounts_df['SubIndustry']))
```

## Model Architecture

### Score Components

The churn risk score consists of five weighted components:

1. **Industry-Relative Momentum (35%)**
   - Measures percentile rank deterioration within most specific cohort available
   - Uses sub-industry → industry → global hierarchy
   - More specific comparisons = more accurate risk assessment

2. **Industry Position (25%)**
   - Current revenue percentile within industry
   - Inverted scale: lower percentile = higher risk

3. **Activity Pattern Deviation (20%)**
   - Compares inactivity to industry peers
   - Dynamically calculates industry-specific thresholds

4. **Industry Context Modifier (10%)**
   - Adjusts score based on overall industry health
   - Reduces risk if entire industry is declining
   - Increases risk if account declines while industry grows

5. **Tier Value (10%)**
   - Maintains business priority for high-value accounts
   - Key Accounts score higher for same risk indicators

### Dynamic Industry Metrics

For each cohort level (sub-industry or industry) with sufficient data (≥10 accounts), the system calculates:

- **Growth Rate**: Industry revenue trend
- **Average Events**: Typical event frequency
- **Pattern Distribution**: Common event patterns (Annual, Regular, etc.)
- **Activity Percentiles**: P25, P50, P75, P90 for days since last activity

### Score Calculation Formula

```
Base Score = (
    Momentum_Score * 0.35 +
    Position_Score * 0.25 +
    Activity_Score * 0.20 +
    Tier_Score * 0.10
)

Final Score = Base_Score * Context_Modifier * Critical_Event_Multipliers
```

## Implementation Details

### Enhanced Metrics Calculation

Add to existing metrics:
- Industry classification from Accounts report
- Days since last activity for percentile calculations
- Preserve event count data for pattern analysis

### Hierarchical Industry Cohort Logic

The model uses a three-tier hierarchy for peer comparison:

1. **Sub-Industry** (most specific)
2. **Industry** (broader category)
3. **Global** (all accounts)

```python
def get_hierarchical_cohort(account_id, all_accounts_df, industry_lookup, sub_industry_lookup):
    """
    Get the most specific cohort with sufficient data
    Returns: (cohort_df, cohort_level)
    """
    MIN_COHORT_SIZE = 10
    
    # Get account's classifications
    industry = industry_lookup.get(account_id, 'Unknown')
    sub_industry = sub_industry_lookup.get(account_id, 'Unknown')
    
    # Try sub-industry first (most specific)
    if sub_industry != 'Unknown':
        sub_industry_cohort = all_accounts_df[
            all_accounts_df['Account_Name'].apply(
                lambda x: sub_industry_lookup.get(int(x), '') == sub_industry
            )
        ]
        if len(sub_industry_cohort) >= MIN_COHORT_SIZE:
            return sub_industry_cohort, 'sub_industry'
    
    # Fall back to industry
    if industry != 'Unknown':
        industry_cohort = all_accounts_df[
            all_accounts_df['Account_Name'].apply(
                lambda x: industry_lookup.get(int(x), '') == industry
            )
        ]
        if len(industry_cohort) >= MIN_COHORT_SIZE:
            return industry_cohort, 'industry'
    
    # Fall back to global
    return all_accounts_df, 'global'
```

### Example Hierarchy

- **Industry**: Arts & Entertainment
  - **Sub-Industry**: Theatre
  - **Sub-Industry**: Music Venues
  - **Sub-Industry**: Comedy Clubs
  
- **Industry**: Education
  - **Sub-Industry**: Universities
  - **Sub-Industry**: Secondary Schools
  - **Sub-Industry**: Training Providers

### Graceful Degradation

The model handles edge cases through progressive fallbacks:

1. **Ideal**: Compare within same sub-industry (≥10 accounts)
2. **Fallback 1**: Compare within broader industry (≥10 accounts)
3. **Fallback 2**: Use global population for percentiles
4. **Fallback 3**: Use pattern-based defaults (Annual, Regular, etc.)
5. **Safe Default**: Neutral risk score if insufficient data

## Output Fields

The enhanced model will add these fields to Zoho:

- `Churn_Risk`: 0-100 score (existing field, new calculation)
- `Event_Count_Current`: Actual number of events in current period
- `Event_Count_Previous`: Actual number of events in previous period
- Industry data used internally but not sent to Zoho

## Advantages

1. **Self-Calibrating**: No fixed thresholds to maintain
2. **Granular Intelligence**: Sub-industry comparisons provide more precise benchmarks
3. **Industry Patterns**: Reduces false positives from seasonal/cyclical patterns
4. **Statistically Robust**: Requires minimum cohort sizes with smart fallbacks
5. **Business Aligned**: Prioritizes by revenue impact
6. **Transparent**: Score components can be explained
7. **Hierarchical**: Uses most specific comparison available for each account

## Risk Score Interpretation

- **0-20**: Healthy account, minimal risk
- **20-40**: Normal risk, standard monitoring
- **40-60**: Elevated risk, proactive engagement recommended
- **60-80**: High risk, urgent intervention needed
- **80-100**: Critical risk, immediate action required

## Example Scenarios

### Theatre Company
- **Best case**: Compared against other theatres (sub-industry)
- **Fallback**: Compared against Arts & Entertainment (industry)
- Seasonal dark periods won't trigger high risk
- Declining while other theatres grow = higher risk

### University
- **Best case**: Compared against other universities (sub-industry)
- **Fallback**: Compared against Education sector (industry)
- Academic year patterns recognized
- Summer quiet periods expected

### Music Festival
- **Best case**: Compared against other music festivals
- **Fallback**: Compared against Festivals industry
- Annual events with long lead times understood
- Weather-related cancellations contextualized

### Corporate Training Provider
- **Best case**: Compared against other training providers
- **Fallback**: Compared against corporate events or education
- Quarterly training cycles recognized
- B2B seasonality patterns expected

## Testing Approach

1. Run in TEST_MODE to verify calculations
2. Compare risk scores with known churned accounts
3. Validate industry cohort sizes and fallback logic
4. Ensure score distribution follows expected pattern

## Future Enhancements

1. **Seasonal Adjustments**: Industry-specific seasonal factors
2. **Economic Indicators**: Adjust for broader economic trends
3. **Predictive Windows**: Forecast risk 30/60/90 days forward
4. **Retention Probability**: Inverse score for positive framing

## Summary

This model provides a sophisticated, data-driven approach to churn risk that:
- Adapts automatically to business changes
- Uses hierarchical comparisons (sub-industry → industry → global)
- Reduces false positives through granular peer benchmarking
- Prioritizes revenue impact for efficient resource allocation
- Maintains simplicity with a single 0-100 score

The implementation builds on existing infrastructure, requiring only the addition of industry and sub-industry data from the Accounts report. The hierarchical approach ensures the most relevant comparisons are used for each account while maintaining statistical validity through minimum cohort sizes.

### Benefits of Sub-Industry Data

Sub-industry classification enables more precise risk assessment by comparing:
- Regional theatres with regional theatres (not all entertainment venues)
- Universities with universities (not all education providers)
- Music festivals with music festivals (not all outdoor events)

This granularity significantly improves the accuracy of churn predictions and reduces noise from comparing fundamentally different business models within the same broad industry category.