# Retention Priority System Plan

> **Status**: ✅ IMPLEMENTED - See [Field Guide](retention_priority_field_guide.md)

## Overview

The Retention Priority System combines three key metrics to identify accounts that need customer success intervention:
1. **Tier** (already implemented) - Account value/potential based on historical revenue and growth
2. **Activity Rating** (ready for rollout) - Behavioral health signals based on booking patterns
3. **Revenue Drop** (planned) - Financial performance trends compared to industry peers

The system updates weekly using data from S3 booking reports, calculating a single Priority score that CS teams use to focus their efforts on accounts most likely to churn.

### Account Activity Patterns
Accounts are classified based on their historical event frequency:
- **Continuous**: Events weekly/monthly (e.g., cinemas, fitness classes)
- **Regular**: Events quarterly (e.g., comedy clubs, small venues)
- **Seasonal**: Events during specific seasons (e.g., summer festivals, Christmas markets)
- **Annual**: One major event per year (e.g., annual conference, festival)

This classification is automatically determined by analyzing the last 2 years of booking data.

## Activity Rating System

### Rating Categories

1. **Active**: Sold tickets in the last 12 months
2. **New**: Account created in last 14 days with no bookings
3. **Outreach**: Tier 3+ annual/seasonal accounts 30 days before expected sales
4. **At Risk**: 
   - New accounts: No activity 14-27 days after creation
   - Annual/Seasonal: Should be selling based on historical patterns (e.g., a summer festival that hasn't started selling by April when they usually start in March)
   - Regular: No activity for 90-179 days
   - Continuous: No activity for 30-89 days
5. **Churned**:
   - New accounts: No activity 28+ days after creation
   - Annual/Seasonal: Missed expected event date (+30 day grace)
   - Regular: No activity for 180+ days
   - Continuous: No activity for 90+ days
6. **Returned**: No activity last year, but active now
7. **Inactive**: No events in last 2 years

### Special Handling
- Education accounts: July/August excluded from inactivity counts
- Scottish schools: Only July excluded (different term patterns)
- Annual events: ±30 day flexibility for date variations

## Revenue Drop Monitoring (Planned)

### Seasonality-Aware Calculation

For accounts with 12+ months history:
```
Current Period = Last 4 weeks revenue
Comparison Period = Same 4 weeks last year (YoY)
Baseline = 12-week average from same season last year
```

For newer accounts (3-12 months):
```
Current Period = Last 4 weeks revenue  
Baseline = Average weekly revenue since account creation × 4
```

### Industry Quintile Calculation

Revenue is divided into quintiles (5 equal groups) for clearer transitions:
```
Q5 = Top 20% (highest revenue)
Q4 = 60-80th percentile  
Q3 = 40-60th percentile (middle)
Q2 = 20-40th percentile
Q1 = Bottom 20% (lowest revenue)
```

**Example**: In the Theatre industry with 500 accounts:
- Account A: £50k/month = Q5 (top 100 accounts)
- Account B: £10k/month = Q3 (middle 100 accounts)
- If Account A drops to £10k/month: Q5→Q3 = 2 quintile drop = Moderate concern

For seasonal comparison:
```
Summer Quintile = Account's June-Aug revenue vs. all accounts' June-Aug revenue
Winter Quintile = Account's Dec-Feb revenue vs. all accounts' Dec-Feb revenue
Current Quintile = Last 4 weeks vs. same 4 weeks for all industry peers
```

This ensures a festival company in winter isn't compared to summer festival revenues.

### Revenue Drop Scoring

#### Industry Quintile Comparison (Primary Method):

When sufficient industry data exists:
```
IF subindustry has 100+ accounts with 6+ months history:
    Use subindustry quintiles
ELIF industry has 100+ accounts with 6+ months history:
    Use industry quintiles
ELSE:
    Use activity-based thresholds (below)
```

**Quintile-Based Scoring:**
- **Severe Drop** (Score: 3)
  - Dropped 4 quintiles (e.g., Q5 → Q1)
  - OR dropped to Q1 from Q3+
  
- **Significant Drop** (Score: 2)
  - Dropped 3 quintiles (e.g., Q5 → Q2)
  
- **Moderate Drop** (Score: 1)
  - Dropped 2 quintiles (e.g., Q4 → Q2)

- **No Revenue Impact** (Score: 0)
  - Dropped 1 quintile or stable
  - OR zero/low revenue is common in industry (>30% of peers also zero)

**Special Cases:**
- If >30% of industry peers have zero revenue, no penalty for zero revenue
- New accounts compared only after 3 months of operation
- Industry comparison uses same seasonal period for fairness

#### Activity-Based Fallback (when industry data insufficient):

**Year-over-Year Comparison** (for seasonal accounts):
- **Severe Drop** (Score: 3)
  - Current < 30% of same period last year
  - OR zero revenue when active same time last year
  
- **Significant Drop** (Score: 2)
  - Current 30-60% of same period last year
  - OR declining for 3 consecutive periods
  
- **Moderate Drop** (Score: 1)
  - Current 60-80% of same period last year

**Rolling Average Comparison** (for continuous accounts):
- **Severe Drop** (Score: 3)
  - Current < 25% of 12-week average
  - OR zero revenue for 6+ weeks
  
- **Significant Drop** (Score: 2)
  - Current 25-50% of 12-week average
  
- **Moderate Drop** (Score: 1)
  - Current 50-75% of 12-week average

### Special Seasonal Handling

#### Education Accounts:
- Summer months (July/August) excluded from calculations
- Compare term-to-term rather than sequential weeks
- September revenue compared to previous September

#### Event Industry Patterns:
- December typically low (compare to previous December)
- Spring/Summer peaks recognized
- Bank holiday impacts considered

#### Annual/Biennial Events:
- Only calculate during ±90 days of historical event window
- Compare to same event last cycle
- No penalty for expected quiet periods

### New Account Thresholds:
- Week 1-4: Building phase (no scoring)
- Week 5-8: Expected revenue (flag if zero)
- Week 9+: Compare to account tier cohort average

## Retention Priority Calculation

### Formula
```
Priority = Tier Weight × (Rating Severity + Revenue Drop Score)
```

**Calculation Example**:
- Account: Key Account (Weight=5), At Risk (Score=7), Severe Revenue Drop (Score=3)
- Priority = 5 × (7 + 3) = 50
- This would rank higher than a Tier 3 account with same issues: 2 × (7 + 3) = 20

### Weights

#### Tier Weights:
- Key Account: 5
- High Value: 4
- Tier 4: 3
- Tier 3: 2
- Below Tier 3: 1

#### Rating Severity:
- Churned: 10
- At Risk: 7
- Outreach: 4
- New (with issues): 3
- Active: 0
- Returned: 1
- Inactive: 0

#### Revenue Drop Scores:
- Severe Drop: 3
- Significant Drop: 2
- Moderate Drop: 1
- Stable/No Impact: 0

When >30% of industry peers have zero revenue, revenue scoring is disabled and priority relies solely on Tier × Activity Rating.

### Example Priority Scenarios

1. **Critical**: Key Account + At Risk + Severe Revenue Drop
   - Priority Score: 5 × (7 + 3) = 50

2. **High**: High Value + At Risk + Moderate Revenue Drop  
   - Priority Score: 4 × (7 + 1) = 32

3. **Medium**: Tier 3 + At Risk + Stable Revenue
   - Priority Score: 2 × (7 + 0) = 14

4. **Monitor**: Key Account + Active + Significant Revenue Drop
   - Priority Score: 5 × (0 + 2) = 10

## Implementation Phases

### Phase 1: Activity Rating Rollout (Immediate)
- Deploy ratings to CS team alongside existing Tier data
- Manual cross-reference while building automation
- Gather feedback on accuracy and usefulness

### Phase 2: Revenue Drop Integration (Next)
- Implement weekly revenue calculations
- Test thresholds against historical data
- Add to manual CS workflow

### Phase 3: Automated Priority System
- Combine all three metrics automatically
- Generate weekly priority lists
- Build CS dashboards and alerts

## Success Metrics

- False positive rate (flagged accounts that don't churn)
- Save rate (At Risk → Active with intervention)
- Miss rate (Active → Churned without warning)
- CS team efficiency (accounts saved per hour)
- Revenue retained through intervention

## Next Steps

1. Validate Activity Rating thresholds with 6 months historical data
2. Build industry revenue quintile baselines:
   - Calculate weekly quintiles for each industry/subindustry
   - Only include accounts with 6+ months history
   - Store seasonal patterns for accurate comparisons
   - Identify industries with <100 established accounts for fallback logic
   - Flag industries where zero revenue is common (>30%)
3. Build prototype scoring on subset of accounts
4. Create CS playbooks for each priority level
5. Establish weekly review process

## Data Pipeline Requirements

### Industry Quintile Calculation:
- Weekly job to calculate revenue quintiles by industry/subindustry
- Store 52 weeks of historical quintiles for YoY comparison
- Track percentage of accounts with zero revenue per industry
- Filter to only accounts with 6+ months history for stable quintiles
- Separate quintile calculations for different seasonal periods
- Cache quintile boundaries for performance

### Quintile Calculation Logic:
```
For each industry/subindustry:
1. Filter accounts with 6+ months history
2. Calculate revenue for comparison period (last 4 weeks)
3. If 100+ accounts remain:
   - Sort by revenue
   - Divide into 5 equal groups (quintiles)
   - Store quintile boundaries
   - Flag if >30% have zero revenue
4. Else: Mark as "insufficient data for quintiles"
```

## System Robustness Summary

This retention priority system is designed to be highly robust through:

1. **Multi-Signal Approach**: Combines behavioral (Activity Rating), financial (Revenue Drop), and strategic (Tier) signals
2. **Smart Normalization**: Quintile-based comparison eliminates industry bias while maintaining sensitivity
3. **Graceful Degradation**: Falls back from subindustry → industry → activity patterns when data is insufficient
4. **Seasonal Intelligence**: YoY and same-season comparisons prevent false positives
5. **Zero Revenue Handling**: No penalties when zero revenue is normal for the industry
6. **Statistical Validity**: 100+ accounts with 6+ months history ensures stable baselines
7. **Clear Prioritization**: Simple formula (Tier × (Rating + Revenue)) produces actionable scores
8. **Continuous Learning**: Built-in metrics allow ongoing threshold optimization

The system will catch genuine churn risks while minimizing false positives through industry-aware, seasonally-adjusted monitoring.