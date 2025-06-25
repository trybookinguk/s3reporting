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
- **What to do**: Personalized outreach within 1 week

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

### Revenue Comparison
The system compares each account's revenue to others in their industry:
- Accounts in the bottom 20% of their industry get higher priority
- If many similar accounts have low revenue (>30%), priority is based on activity only
- New accounts aren't compared until they're 3 months old

### Education Accounts
Schools get special consideration:
- Summer revenue drops don't increase priority
- Priority increases in August/September if they haven't returned
- Scottish schools tracked separately (different term dates)

### Annual Events
For yearly events, priority increases:
- 30 days before they usually start selling tickets
- Immediately if they miss their typical event window

## Data Sources

To calculate priority, we combine:
- **Account tier**: How valuable they are (Key Account, High Value, Tier 4, etc.)
- **Activity rating**: Their current health status (Active, At Risk, etc.)
- **Revenue trends**: How they compare to similar accounts
- **Event patterns**: When they normally run events

## Business Value

This priority system helps us:
- **Save more accounts** by intervening before they churn
- **Focus effort** on accounts we can actually help
- **Use time wisely** by prioritizing high-value at-risk accounts
- **Track success** by monitoring priority changes over time

## Update Frequency

Priorities are updated automatically every Tuesday morning, using the latest booking data.