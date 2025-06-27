# Retention Priority Technical Specification

## Executive Summary

The Retention Priority system scores accounts from 0-20 based on their value, activity health, and revenue trends. Higher scores indicate accounts needing urgent intervention. The system runs daily (Monday-Friday at 4 AM UTC) to ensure Customer Success teams always work with current priorities.

## Core Formula

```
Base Score = (Tier Weight × 2) + Activity Rating Severity + Revenue Drop Score
Final Score = Base Score + Boosts + Adjustments
```

### Component Weights

| Component | Weight/Score |
|-----------|-------------|
| **Tier Weights** | |
| Key Account | 5 |
| High Value | 4 |
| Tier 4 | 3 |
| Tier 3 | 2 |
| Tier 2/1/NIL | 1 |
| **Activity Rating Severity** | |
| At Risk | 5 |
| Outreach | 3 |
| New | 2 |
| Returned | 1 |
| Active/Inactive | 0 |
| Churned | -1 (excluded) |
| **Revenue Drop Score** | |
| Severe (>75% drop) | 3 |
| Significant (50-75%) | 2 |
| Moderate (25-50%) | 1 |
| Stable (<25% drop) | 0 |

## Boost Mechanisms

### 1. Tier Drop Detection
Accounts that drop tiers receive priority boosts:
- Major drop (3+ tiers): +6 points
- Significant drop (2 tiers): +4 points  
- Minor drop (1 tier): +2 points

**Example**: High Value → Tier 2 = 2 tier drop = +4 boost

### 2. Annual Account Reachout
Annual accounts get boosted when approaching their typical event time:
- Triggers 60 days before they typically start selling tickets
- Uses account's average lead time (e.g., 96 days before event)
- Sets minimum score of 11 (High priority)
- Boost persists until event date passes

**Example**: September event with 96-day lead:
- April: Boost starts (60 days before June sales)
- June-September: Boost continues through selling season
- October: Boost ends

### 3. Rapid Drop Alerts
Detects sudden revenue drops in 4-week windows:
- Only applies to Continuous/Regular accounts in high tiers
- Compares last 4 weeks vs previous 8 weeks
- Level 2 (moderate): Sets minimum score to 13
- Level 3 (severe): Sets minimum score to 17-19

**Requirements for Level 3 boost**:
- Must be Key Account, High Value, or Tier 4
- Revenue must have declined (not just tickets)
- Previous revenue must be ≥ £100
- Must have "Severe" or "Significant" revenue drop category

## Special Adjustments

### 1. Education Industry (Schools)
During summer holidays, schools receive reduced priority:
- **Scottish schools** (June 15 - August 15): Priority capped at 5
- **English/Welsh schools** (July 1 - September 7): Priority capped at 5
- Rapid drop alerts disabled during holidays
- Scottish schools detected by postcode (AB, DD, DG, EH, FK, G, etc.)

### 2. New Account Protection
Accounts with Years_Loyalty ≤ 1:
- Rapid drop alerts cleared (no year-over-year comparison possible)
- Cannot be marked as "Returned"
- Tier upgrades from NIL don't trigger drops

### 3. Growing Account Protection
Accounts with revenue/ticket growth:
- 10%+ growth clears rapid drop alerts
- Prevents false positives from timing issues

### 4. Free Event Handling
Accounts running only free events:
- Still tracked if ticket volume is significant (100+)
- Rapid drop severity reduced if maintaining ticket volume

## Priority Categories

Scores map to actionable categories:

| Score Range | Priority | Action Required |
|-------------|----------|-----------------|
| 16-20 | Very High | Contact within 24-48 hours |
| 11-15 | High | Personalised outreach within 1 week |
| 6-10 | Medium | Include in regular check-ins |
| 0-5 | Low | Standard communications only |
| <0 | Excluded | Churned - separate win-back process |

## Calculation Examples

| Example | Account Details | Calculation | Result |
|---------|----------------|-------------|---------|
| At-Risk Key Account | Key Account, At Risk, Moderate drop | (5×2) + 5 + 1 = 16 | Very High |
| Tier Drop | High Value→Tier 3, Active, Stable | (2×2) + 0 + 0 + 2 = 6 | Medium |
| Annual Boost | Tier 3, Active, Event in 2 months | Base 4, boost to 11 | High |

## Business Rationale

### Why This Approach?

1. **Value-Weighted**: Higher tier accounts get more attention when at risk
2. **Early Warning**: Annual accounts flagged before they choose platforms
3. **Context-Aware**: Schools deprioritized during holidays
4. **Action-Oriented**: Clear thresholds drive specific CS actions
5. **Balanced**: Multiple factors prevent over-reliance on single metric

### Key Design Decisions

1. **Daily Updates**: Rapid drops detected within 24 hours vs weekly/monthly
2. **60-Day Annual Lead**: Based on analysis showing platform decisions made early
3. **£100 Revenue Threshold**: Focuses effort on commercially meaningful accounts
4. **Tier Drop Boosts**: Declining accounts need intervention even if still "Active"
5. **20-Point Cap**: Prevents too many "Very High" priorities diluting focus

## Integration Points

- **Zoho CRM**: Retention_Priority and Retention_Priority_Score fields updated daily
- **CS Workflows**: Priority drives task creation and follow-up cadence
- **Reporting**: Weekly summaries show priority distributions and changes
- **Alerts**: Very High priorities can trigger immediate notifications

## Future Enhancements

1. **Industry-Specific Scoring**: Adjust weights by industry patterns
2. **Predictive Elements**: ML-based churn probability integration
3. **Outcome Tracking**: Measure intervention success by priority level
4. **Dynamic Thresholds**: Adjust categories based on CS team capacity
5. **Multi-Channel Integration**: Include support ticket sentiment

## Technical Implementation

The system is implemented in Python using vectorized pandas operations for performance:
- Processes 50,000+ accounts in <3 minutes
- Handles missing data gracefully
- Validates all inputs to prevent errors
- Logs detailed metrics for monitoring

Code is modular with separate components for:
- Tier calculation
- Activity rating
- Revenue analysis
- Rapid drop detection
- Priority scoring

This separation allows easy updates to individual components without affecting the overall system.