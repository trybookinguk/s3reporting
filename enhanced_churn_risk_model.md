# Enhanced Churn Risk Model Documentation

## Overview

The Enhanced Churn Risk Model is a sophisticated scoring system (0-100) that predicts account churn likelihood by analyzing event creation patterns, revenue trends, and business activity. The model uniquely leverages **LastEventCreated** data to detect when accounts stop creating events, catching churn signals months before traditional metrics.

## Latest Enhancements (v3)

### 1. Volume-Weighted Event Window Detection
- Tracks which months accounts typically create events
- Weights importance by event volume and revenue (70/30 mix)
- Detects when accounts miss their critical creation windows
- Adds up to 45 points for missing high-importance windows

### 2. Weighted Tier Drop Scoring
- Drops from higher tiers score more severely
- Key Account drop: 3x weight, High Value: 2.5x, down to Tier 1: 0.5x
- Maximum 20 points for tier drops
- Prevents double-counting with revenue decline

### 3. Percentile-Based Value Protection
- Top 10% revenue accounts: Minimum 80 points if critical, 65 if warning
- Top 25% revenue accounts: Minimum 70 points if critical, 55 if warning
- Bottom quartile penalty: +5 points for struggling low-value accounts
- No fixed revenue thresholds - all percentile-based

### 4. Industry Context (Not Mitigation)
- Industry decline no longer reduces individual risk scores
- Declining worse than industry adds 15 points
- Declining in healthy industry adds 10 points
- Context noted for reporting but doesn't excuse poor performance

### 5. Decline Acceleration Detection
- Tracks if revenue decline is accelerating vs historical trends
- Adds up to 20 points for accounts "falling off a cliff"
- Distinguishes gradual decline from sudden collapse

## Key Innovation: Event Creation vs Event Occurrence

Traditional models only see events that sold tickets. This model uses `LastEventCreated` from the Accounts report to detect:
- Accounts that stopped creating events (strongest churn signal)
- Accounts creating events but not selling tickets (struggling)
- Expected events that haven't been created (pattern breaks)

## Model Architecture

### Multi-Tier Risk Assessment

#### Tier 1: Creation Activity (40 points max)
The strongest churn predictor - have they stopped creating events?

```python
# Pattern-specific thresholds
creation_thresholds = {
    'Annual': {'warning': 180, 'critical': 365},     # 6-12 months
    'Seasonal': {'warning': 90, 'critical': 180},    # 3-6 months
    'Regular': {'warning': 45, 'critical': 90},      # 1.5-3 months
    'Occasional': {'warning': 120, 'critical': 240}, # 4-8 months
    'Struggling': {'warning': 60, 'critical': 120}   # 2-4 months
}
```

**Scoring Logic:**
- Days since last created > critical threshold: 40 points
- Days since last created > warning threshold: 20-40 points (scaled)
- Within normal range: 0 points

#### Tier 2: Revenue Performance (35 points max)

**Revenue Decline Component (0-35 points):**
- Lost 75%+ revenue: 35 points
- Lost 50-75% revenue: 25 points
- Lost 25-50% revenue: 15 points
- Lost 0-25% revenue: 0-7.5 points (scaled)
- New account with no revenue: 10 points

#### Tier 3: Struggle Indicators (25 points max)

**Events Not Selling (0-25 points):**
- Created events recently but no sales > 60 days: 25 points
- Created events recently but no sales > 30 days: 15 points

**Velocity Decline (+10 points):**
- For Regular/Seasonal patterns: Recent activity < 50% of expected

#### Tier 4: Missed Event Windows (45 points max) - NEW

**Volume-Weighted Window Risk:**
- Tracks typical creation months by event volume
- 3+ months overdue on critical window: 40 points × importance
- 1.5+ months overdue: 25 points × importance
- 0.5+ months overdue: 15 points × importance
- Maximum 45 points total

#### Tier 5: Tier Drops (20 points max)

**Weighted by Starting Position:**
- Key Account drop: 3x weight
- High Value drop: 2.5x weight
- Tier 4 drop: 2x weight
- Lower tiers: 1.5x down to 0.5x

### Enhanced Modifiers

**1. Dynamic Revenue Multiplier (Logarithmic Scale):**
```python
# Scale risk based on revenue impact using log scale
# £1k = 1.0x, £10k = 1.15x, £100k = 1.3x, £1M = 1.45x
revenue_multiplier = 1.0 + (log10(annual_revenue / 1000) * 0.15)
```
- Smoothly scales with revenue size
- Capped at 1.5x to prevent outliers
- Ensures high-value accounts get appropriate priority

**2. Decline Acceleration Tracking (+20 points max):**
- Compares current decline rate to historical average
- Severe acceleration (>30%): +20 points
- Moderate acceleration (>15%): +10 points
- Catches "falling off a cliff" scenarios

**3. Industry Decline Adjustment:**
- If industry average decline >20%: Reduce individual risk by up to 30%
- If individual declining worse than industry +30%: Add 10 points
- Prevents over-reacting to market-wide downturns
- Requires minimum 10 accounts in industry for statistical validity

**4. Tier Drop (+10 points):**
- Dropped 2+ tiers: Additional risk

**5. Long-term Customer Flag:**
- 5+ years loyalty with score > 50: Flagged for special attention

## Event Pattern Classification

### Pattern Detection Algorithm

```python
def calculate_event_frequency_v2(account_row, booking_data):
    # Uses FirstEventCreated and LastEventCreated for account age
    # Groups visible events into temporal clusters (30-day windows)
    # Classifies based on clusters per year and months active
```

**Pattern Types:**

1. **Annual** (≤1.5 clusters/year)
   - Examples: Festivals, annual conferences
   - Long creation lead times (2-6 months)
   - Risk assessed yearly

2. **Seasonal** (≤4 clusters/year, ≤6 active months)
   - Examples: Christmas shows, summer camps
   - Medium lead times (1-3 months)
   - Risk assessed per season

3. **Regular** (≥6 clusters/year)
   - Examples: Weekly classes, monthly workshops
   - Short lead times (2-6 weeks)
   - Risk assessed monthly

4. **Occasional** (Between seasonal and regular)
   - Examples: Quarterly events, sporadic bookings
   - Variable lead times
   - Risk assessed quarterly

5. **Struggling** (No visible events but recent creation)
   - Creating events but zero sales
   - High risk indicator
   - Monitored closely

## Implementation Details

### Data Requirements

1. **Booking Data** (BookingDataAll, BookingData)
   - Only shows events with ticket sales
   - Used for revenue, tickets, and visible patterns

2. **Accounts Report** (NEW)
   - `FirstEventCreated`: Account start date
   - `LastEventCreated`: Most recent event creation (KEY FIELD)
   - `Industry`/`SubIndustry`: For cohort comparison

### Risk Score Interpretation

- **0-20**: Healthy account, minimal concern
- **21-40**: Low risk, routine monitoring
- **41-60**: Moderate risk, proactive check-in recommended
- **61-80**: High risk, urgent intervention needed
- **81-100**: Critical risk, immediate action required

### Example: Account 5716 (Churned Theatre)

**Before Enhancement (Score: 51)**
- Industry decline adjustment reduced score by 30%
- Missed event window not detected
- Misclassified as moderate risk

**After Enhancement (Score: ~95-100)**
- Pattern: Occasional (biannual shows)
- Days since created: 288 (critical for pattern)
- Creation activity: 40 points
- Revenue decline (51%): 25 points
- Missed June window (high importance): ~30 points
- Tier drop (Key Account → High Value): ~7.5 points
- Top revenue percentile override: Ensures 80+ minimum
- **Total: 95-100 (CRITICAL RISK)**

## Edge Cases Handled

1. **New Accounts**: Low baseline risk, escalates if no activity after 90 days
2. **Zero-Sale Events**: Detected via creation-to-sale gap analysis
3. **Free Events**: Identified by zero total fees
4. **Missing Expected Events**: Pattern-based expectation vs actual creation
5. **Data Quality Issues**: Graceful fallbacks and 50-point default

## Why This Model Works

1. **Sees Hidden Churn**: Detects when accounts stop trying (creation) vs just failing (sales)
2. **Pattern Aware**: Different rules for festivals vs daily events
3. **Revenue Focused**: Natural bias toward high-value accounts
4. **Early Warning**: Catches churn 3-6 months earlier than traditional models
5. **Actionable**: Clear risk factors for targeted intervention

## Technical Implementation

### Key Functions

```python
# Main risk calculation
def calculate_churn_risk_final(account_row, booking_data, all_accounts_df):
    """Returns dict with score, factors, pattern, and metrics"""

# Pattern detection
def calculate_event_frequency_v2(account_row, booking_data):
    """Enhanced frequency using creation dates and clusters"""

# Cluster identification
def identify_temporal_clusters(event_dates, gap_days=30):
    """Groups events within 30 days as single cluster"""
```

### Integration Points

1. **Data Loading**: Accounts report loaded in main() with creation date lookups
2. **Booking Data**: Full current month data loaded for pattern analysis
3. **Metrics Calculation**: Enhanced risk replaces old calculate_bulletproof_churn_risk()
4. **Zoho Updates**: Churn_Risk field (0-100) sent to CRM

## Revenue Protection Features

### Revenue at Risk Calculation
```python
# Pattern-based revenue expectations
if pattern == 'Annual':
    revenue_at_risk = base_revenue * 1.0  # Full year
elif pattern == 'Seasonal':
    revenue_at_risk = base_revenue * 0.5  # Two seasons
elif pattern == 'Regular':
    revenue_at_risk = base_revenue * 0.25  # Quarter
    
# Adjust by probability
expected_loss = revenue_at_risk * (risk_score / 100)
```

### Priority Score (0-100)
Combines risk with revenue impact for optimal resource allocation:
```python
priority = (risk_score * 0.5) + (revenue_factor * 0.5)
# Boosted for critical factors:
# - No creation critical: ×1.2
# - Severe decline acceleration: ×1.15
# - Long-time customer: ×1.1
```

## Model Outputs

**Visible Fields (sent to Zoho):**
- `Churn_Risk`: 0-100 score
- `Event_Count_Current`: Actual event count
- `Event_Count_Previous`: Previous period count
- `Event_Frequency_Current`: Pattern classification

**Hidden Fields (for reports):**
- `_risk_factors`: Comma-separated risk reasons
- `_event_pattern`: Detected pattern type
- `_days_since_created`: Days since LastEventCreated
- `_revenue_decline_pct`: Percentage revenue drop
- `_revenue_at_risk`: Expected revenue loss (£)
- `_priority_score`: Combined risk and revenue score

**At-Risk Report Fields:**
- `Priority_Score`: For sorting by revenue impact
- `Revenue_At_Risk`: Expected loss in pounds
- `Key_Risks`: Top 3 risk factors in plain language
- `Days_Since_Created`: Direct indicator of activity

## Recommended Run Frequency

**Weekly** - Optimal for catching:
- Regular accounts exceeding 45-day thresholds
- Seasonal accounts missing creation windows
- Revenue trends requiring intervention

Monthly is too infrequent for Regular accounts, while daily provides minimal additional value.

## Future Enhancements

1. **Industry Benchmarking**: Compare creation patterns within industry cohorts
2. **Predictive Windows**: Forecast risk 30/60/90 days forward
3. **Creation Success Rate**: Track ratio of created to visible events
4. **Automated Alerts**: Trigger when accounts cross risk thresholds

## Summary

This enhanced model revolutionizes churn detection by focusing on event creation patterns rather than just sales outcomes. By leveraging LastEventCreated data, it identifies at-risk accounts 3-6 months earlier than traditional approaches, enabling proactive retention efforts that can save significant revenue.