# Retention Priority Field Guide

## Overview

The **Retention_Priority** field tells us which accounts need immediate attention from Customer Success. It combines an account's value, activity health, and revenue trends into a single priority level.

## Priority Values

### 1. **Very High**
- **What it means**: Critical account needs immediate intervention
- **How we know**: High-value account with serious warning signs (At Risk rating + dropping revenue)
- **What to do**: Call within 24-48 hours

### 2. **High**
- **What it means**: Important account needs urgent attention
- **How we know**: Valuable account showing concerning patterns
- **What to do**: Personalised outreach within 1 week

### 3. **Medium**
- **What it means**: Account showing early warning signs
- **How we know**: Some risk factors but not critical yet
- **What to do**: Include in regular check-ins, monitor closely

### 4. **Low**
- **What it means**: Account is healthy or has minor issues only
- **How we know**: Active with stable revenue or lower-value account
- **What to do**: Standard communications only

### 5. **(Empty)**
- **What it means**: Churned account - excluded from workflows
- **How we know**: Activity rating is "Churned"
- **What to do**: Nothing - they're handled separately

## Special Handling

### High-Value Accounts
Key Accounts and High Value accounts get the highest priority scores when they show any warning signs. Even small issues become high priority for these accounts.

### Revenue Analysis
The system performs two types of revenue analysis:

#### 1. Strategic Revenue Drops (Year-over-Year)
- Compares current year revenue to same period last year
- Categories: Severe (>75% drop), Significant (50-75%), Moderate (25-50%), Stable (<25%)
- Industry quintile comparison for mature accounts
- New accounts (<12 weeks) use simple trend analysis

#### 2. Rapid Revenue Drops (4-week trends)
- **Only for high-value accounts**: Key Account, High Value, Tier 4, Tier 3
- **Only for continuous/regular patterns**: Accounts expected to have consistent revenue
- Compares last 4 weeks to previous 8 weeks
- Minimum £100 in comparison period to trigger alerts
- Creates urgent alerts for sudden drops (>50% = Level 2, >75% = Level 3)

### Education Accounts
Schools get special handling during holidays:
- **Scottish schools**: Mid-June to mid-August (15th June - 15th August)
- **English/Welsh schools**: July to early September (July - 7th September)  
- During holidays: Priority capped at "Low", rapid drop alerts disabled
- After holidays: Normal priority calculation resumes
- System detects Scottish schools by postcode (AB, DD, DG, EH, FK, G, etc.)

### Annual Events
For yearly events, priority increases when:
- They haven't created events yet this year
- We're within 60 days of when they typically start selling tickets
- Uses their average lead time (e.g., if they sell 96 days before events, we boost 60 days before that)
- Priority boosted to at least "High" (score 11+) during this critical period
- **Boost persists** from 60 days before typical sales start until the event date passes

Example: September event with 96-day lead:
- April: Boost starts (60 days before typical sales)
- September: Boost ends after event
- Ensures intervention before platform decision

## Key Benefits

- **Early intervention** before accounts churn
- **Focused effort** on high-value at-risk accounts
- **Clear actions** based on priority levels
- **Measurable success** through priority tracking

## Priority Scoring Details

The system uses a scoring formula to calculate priorities:
- **Base score**: (Tier Weight × 2) + Activity Rating Severity + Revenue Drop Score
- **Tier weights**: Key Account=5, High Value=4, Tier 4=3, Tier 3=2, Tier 2/1=1
- **Activity severity**: At Risk=5, Outreach=3, New=2, Active/Inactive=0
- **Revenue scores**: Severe=3, Significant=2, Moderate=1, Stable=0

### Additional Boosts
- **Tier drops**: +2 to +6 points based on severity
- **Annual accounts approaching events**: Minimum score of 11 (starts 60 days before typical sales, persists until event passes)
- **Rapid drops**: Level 2 sets minimum 13, Level 3 sets minimum 17-19
- **Maximum score**: Capped at 20 to prevent excessive "Very High" classifications

### Priority Thresholds
- **Very High**: Score 16-20
- **High**: Score 11-15  
- **Medium**: Score 6-10
- **Low**: Score 1-5
- **Empty**: Churned accounts (excluded from scoring)

## Update Frequency

Priorities are updated automatically Monday-Friday at 4:00 AM UTC, using the latest booking data. This daily update ensures:
- Rapid drop alerts catch issues within 24 hours
- Tier changes are reflected immediately
- Annual account boosts activate at the right time
- CS teams always work with current priorities