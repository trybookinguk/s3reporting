# Activity Rating & Revenue Analysis Technical Specification

## Overview

This document details three interconnected systems that analyze account health:
1. **Activity Rating**: Classifies accounts based on engagement patterns
2. **Revenue Drop Analysis**: Year-over-year revenue comparison
3. **Rapid Drop Detection**: Short-term revenue monitoring for urgent issues

## Part 1: Activity Rating System

### Purpose
Classifies each account into behavioral categories that indicate their current relationship status with the platform.

### Rating Categories

| Rating | Description | Key Indicators |
|--------|-------------|----------------|
| **New** | Recently created accounts | Created ≤14 days ago, no bookings yet |
| **Active** | Currently engaged | Regular bookings, meeting expected patterns |
| **Outreach** | Needs proactive contact | Annual/seasonal account, 60 days before typical sales start |
| **At Risk** | Concerning inactivity | No activity within expected timeframe |
| **Churned** | Stopped using platform | Extended inactivity beyond grace periods |
| **Returned** | Reactivated after absence | Was inactive, now active again |
| **Inactive** | No current activity | Catch-all for unclassified inactive accounts |

### Classification Logic

#### 1. New Account Detection
```
IF account_created_date ≤ 14 days ago 
AND no bookings yet
THEN Rating = "New"
```

#### 2. Recently Created but Inactive
For accounts created 15+ days ago with no activity:
- 15-28 days old → "At Risk"
- 29+ days old → "Churned"

#### 3. Returned Accounts
```
IF Event_Frequency_Current != "Inactive"
AND Event_Frequency_Previous == "Inactive"  
AND Years_Loyalty > 1
AND had Previous_Tier
THEN Rating = "Returned"
```

#### 4. High-Tier Annual/Seasonal Accounts
For Key Account, High Value, Tier 4, or Tier 3:

**Timing Calculations**:
- Expected event date = Last event date + 365 days
- Expected sales start = Event date - Average lead days
- Outreach date = Sales start - 60 days

**Classifications**:
- Today ≥ Outreach date → "Outreach"
- Today ≥ Sales start date → "At Risk"
- Today ≥ Event date + 30 days → "Churned"

#### 5. Regular/Continuous Accounts
Based on days since last activity:

**Continuous** (monthly activity expected):
- 30-89 days inactive → "At Risk"
- 90+ days inactive → "Churned"

**Regular** (every few months):
- 90-179 days inactive → "At Risk"
- 180+ days inactive → "Churned"

#### 6. Special Cases

**Tier Loss Detection**:
```
IF Current_Tier is NIL/empty
AND Previous_Tier exists
AND Event_Frequency_Current == "Inactive"
THEN Rating = "Churned"
```

**Education Industry**:
- Accounts with education patterns get extended grace periods
- Scottish schools identified by postcode for different term dates

**Activity Override**:
If marked "Churned" but has significant current activity (≥10 tickets or ≥£100 revenue), override to "Active" or "At Risk" based on revenue trends.

## Part 2: Revenue Drop Analysis (Year-over-Year)

### Purpose
Compares current period revenue to same period last year to identify concerning trends.

### Drop Categories

| Category | Drop Range | Score | Description |
|----------|------------|-------|-------------|
| **Stable** | <25% drop or growth | 0 | Healthy or growing |
| **Moderate** | 25-50% drop | 1 | Notable decline |
| **Significant** | 50-75% drop | 2 | Serious concern |
| **Severe** | >75% drop | 3 | Critical situation |

### Calculation Method

```python
drop_percentage = ((previous_revenue - current_revenue) / previous_revenue) * 100
```

### Edge Cases

1. **Zero Previous Revenue**: 
   - If current > 0 → "Stable" (growth from zero)
   - If current = 0 → "Stable" (no change)

2. **Negative Values**: Not applicable (revenue ≥ 0)

3. **Industry Comparison**: For mature accounts (2+ years), compare against industry quintiles:
   - Below 20th percentile → Elevated concern
   - Above 80th percentile → Reduced concern

## Part 3: Rapid Drop Detection

### Purpose
Identifies sudden revenue stops in recent weeks, enabling intervention before year-end metrics show problems. The system uses different detection strategies based on account behavior patterns.

### Applicability Criteria

**Required Conditions**:
1. Account tier must be: Key Account, High Value, Tier 4, or Tier 3
2. Event frequency must be: Continuous or Regular
3. Comparison period revenue ≥ £100

**Excluded Accounts**:
- Annual/Seasonal accounts (natural gaps expected)
- New accounts (Years_Loyalty ≤ 1)
- Accounts with tier upgrades from NIL
- Education accounts during summer holidays
- Growing accounts (10%+ revenue/ticket increase)

### Detection Methods

#### 1. Continuous Accounts (Monthly Activity)
Simple comparison between recent and previous periods:
- **Current Period**: Last 4 weeks
- **Comparison Period**: Previous 8 weeks (weeks 5-12)
- Always checks for drops since they should have constant activity

#### 2. Regular Accounts (Most Months)
Sophisticated "selling window" detection:

**Algorithm**:
1. Identifies which months the account typically has events
2. Uses average lead days to calculate when they sell tickets
3. Determines if we're currently in their selling window
4. Only triggers alerts if they SHOULD be selling now

**Example**:
- Account runs events in: March, June, September, December
- Average lead time: 60 days
- Current date: January 15
- Expected sales for March event: Yes (within 60-day window)
- Result: Check for drops

**If not in selling window**:
- No alert triggered
- Returns: "Not in selling window (active months: [3,6,9,12], lead time: 60 days)"

### Severity Calculation

Based on revenue ratio thresholds (configurable):

| Severity | Revenue Ratio | Score | Typical Threshold |
|----------|---------------|-------|-------------------|
| None | ≥50% of baseline | 0 | ratio ≥ 0.5 |
| Moderate | 25-50% of baseline | 1 | 0.25 ≤ ratio < 0.5 |
| Significant | 10-25% of baseline | 2 | 0.1 ≤ ratio < 0.25 |
| Severe | <10% of baseline | 3 | ratio < 0.1 |

### Calculation Details

```python
# Core calculation
revenue_ratio = current_revenue / comparison_revenue
drop_percentage = (1 - revenue_ratio) * 100

# Return comprehensive data
{
    'score': 0-3,
    'severity': 'none|moderate|significant|severe',
    'current_revenue': 123.45,
    'comparison_revenue': 1234.56,
    'drop_percentage': 90.0,
    'revenue_ratio': 0.1,
    'detection_method': 'continuous|regular',
    'details': 'Context about the detection'
}
```

### Adjustments & Refinements

**Ticket Activity Reduction**:
If account maintains significant ticket volume (≥100 tickets and ≥50% of previous), reduce severity by 1 level. This handles accounts running free events.

**Minimum Revenue Threshold**:
Comparison period must have ≥£100 revenue to avoid noise from very small accounts.

**Edge Case Handling**:
- Zero comparison revenue → "No comparison baseline"
- Below threshold → "Revenue below threshold (£100)"
- Not in selling window → "Not in selling window"

### Real-World Examples

#### Example 1: Continuous Theatre
- Always sells tickets monthly
- Weeks 1-8: £1,000/week average
- Weeks 9-12: £100 total
- Ratio: 0.0125 (1.25% of normal)
- Result: Severe alert (score 3)

#### Example 2: Regular Sports Club
- Events in: June, July, August, December
- Current: October
- Lead time: 30 days
- Selling window check: No December sales expected yet
- Result: No alert

#### Example 3: Regular Conference Organiser
- Annual conference in September
- Lead time: 90 days
- Current: July
- Selling window: Yes (within 90 days of September)
- Recent sales: £0
- Result: Severe alert if previously had sales

## Integration Between Systems

### Combined Risk Assessment

An account's overall risk combines all three systems:

1. **Activity Rating** provides behavioral classification
2. **Revenue Drop** shows year-over-year trends
3. **Rapid Drop** catches sudden changes

### Example Scenarios

| Scenario | Activity Rating | Revenue Drop | Rapid Drop | Interpretation |
|----------|----------------|--------------|------------|----------------|
| Gradual Decline | Active | Significant (60% YoY) | Level 1 | Long-term decline, needs strategic intervention |
| Sudden Stop | Active | Stable | Level 3 | Urgent issue, immediate contact required |
| Seasonal Pattern | Outreach | Stable | N/A | Normal pattern, proactive outreach scheduled |

## Business Value

### Early Warning System
- **Activity Rating**: 30-90 day warning for behavioral changes
- **Revenue Drop**: Annual trend analysis
- **Rapid Drop**: 24-hour detection of sudden stops

### Resource Optimisation
- Focus on accounts where intervention matters
- Exclude natural patterns (holidays, annual cycles)
- Prioritise by commercial impact (£100+ thresholds)

### Measurable Outcomes
- Faster response to account issues
- Better understanding of account lifecycles
- Data-driven customer success strategies

## Technical Implementation Notes

### Performance Optimisations
- Vectorised pandas operations throughout
- Batch processing for large datasets
- Efficient date calculations using pandas datetime

### Data Quality Handling
- Graceful handling of missing data
- Validation of all inputs
- Clear audit trail for classifications

### Maintenance Considerations
- Modular design allows independent updates
- Configuration-driven thresholds
- Comprehensive logging for debugging

---

*These three systems work together to provide a complete picture of account health, enabling proactive customer success interventions based on data-driven insights.*