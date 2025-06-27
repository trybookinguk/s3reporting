# Activity Rating Field Guide

## Overview

The **Rating** field tells us how each account is doing and whether they need help. Every account gets one of seven ratings based on how often they run events and what type of business they are.

## Rating Values

### 1. **Active**
- **What it means**: They're running events and selling tickets
- **How we know**: They've sold tickets in the last 12 months
- **What to do**: Nothing - they're doing fine

### 2. **New**
- **What it means**: They just signed up and are getting started
- **How we know**: Account created in the last 14 days (using DateTimeCreated field) with no bookings
- **What to do**: Keep an eye on them

### 3. **Outreach**
- **What it means**: Important account that should be creating their next event soon
- **Criteria**: 
  - Tier 3 or above (Key Account, High Value, Tier 4, Tier 3)
  - Annual or Seasonal event pattern
  - 60 days before expected ticket sales should begin
- **Action**: Proactive contact to support upcoming event

### 4. **At Risk**
- **What it means**: They've gone quiet and that's worrying
- **How we know (depends on account type)**:
  - **New accounts**: No activity 15-28 days after account creation (changed from 14-27)
  - **Annual/Seasonal**: Past when they typically start selling tickets but event hasn't happened yet
  - **Regular**: No activity for 90-179 days
  - **Continuous**: No activity for 30-89 days
  - **Education industry**: Special handling based on term patterns
- **Action**: Immediate intervention required

### 5. **Churned**
- **What it means**: They've probably stopped using us
- **How we know (depends on account type)**:
  - **New accounts**: No activity 28+ days after account creation
  - **Annual/Seasonal**: Missed expected event date (plus 30-day grace period)
  - **Regular**: No activity for 180+ days AND minimal current activity (<10 tickets or <£100 revenue)
  - **Continuous**: No activity for 90+ days AND minimal current activity (<10 tickets or <£100 revenue)
  - **Tier Loss**: Lost their tier and became inactive
- **Action**: Win-back campaign or account closure

**Note**: Accounts with meaningful current activity (10+ tickets or £100+ revenue) are NOT marked as churned even if they haven't had recent bookings

### 6. **Returned**
- **What it means**: They stopped using us but have come back
- **How we know**: 
  - No activity last year, but active now
  - Only applies to accounts that existed before (Years_Loyalty > 1)
  - New accounts can't be "Returned"
- **What to do**: Welcome them back and help them succeed

### 7. **Inactive**
- **What it means**: Not enough activity to judge
- **How we know**: No events in the last 2 years
- **What to do**: Check occasionally but don't worry

## Special Handling

### Education Industry
Schools and education accounts are handled differently based on when they normally run events:

**Schools that run summer programs**:
- Treated like any other account
- No special allowances for July/August

**Schools with term-time pattern**:
- Identified by 60%+ activity during term months (September-June)
- Minimal summer activity (≤1 month in July/August)
- July and August treated differently for inactivity calculations

**Schools that run summer programs**:
- Any education account with July/August activity in their history
- Treated like regular accounts with no special summer allowances

The system identifies education accounts by:
- Industry field marked as "Education"
- OR activity pattern showing 60%+ activity during term months (September-June) with summer gap

**Scottish Schools**:
- Identified automatically by their postcode (AB, DD, DG, EH, FK, G, HS, IV, KA, KW, KY, ML, PA, PH, TD, ZE)
- Have different holiday patterns than English/Welsh schools
- Scottish schools typically return mid-August
- Special handling during their specific holiday periods

### Annual Events
For accounts that run yearly or seasonal events, we predict when they should be active by looking at:
- When they ran events before (with 30-day flexibility for events that shift dates)
- How far in advance they usually set up events
- When they normally start selling tickets

The system allows annual events to vary by ±30 days from the expected 365-day cycle to accommodate real-world variations like "last Saturday in June" events.

### Tier-Based Features

**Outreach Rating**:
- Only for Tier 3+ accounts (Key Account, High Value, Tier 4, Tier 3)
- Applied to annual/seasonal accounts 60 days before expected sales
- Aligns with retention priority boost timing
- Helps prioritize proactive support for valuable accounts

**Tier Loss Detection**:
- Accounts that had a tier but lost it (became NIL)
- If also inactive, immediately marked as "Churned"
- Critical indicator of account health issues

### Annual Events Report
A separate report identifies upcoming annual events for Tier 3+ accounts that need proactive outreach in the next 30 days. Since outreach happens 60 days before expected sales, this report captures accounts whose sales are expected to begin in the next 60-90 days. This report is automatically emailed to stakeholders.

## Data Sources

To work out the rating, we look at:
- **How often they run events**: This year vs last year
- **When they last sold tickets**: How many days ago
- **When their events happen**: To predict future activity
- **What type of account**: Industry classification
- **When they joined**: DateTimeCreated field from Accounts report
- **How important they are**: Their tier level (current and previous)
- **Current activity levels**: Tickets and revenue in current period
- **Revenue drops**: Severe drops can influence ratings
- **Postcode**: For identifying Scottish schools

## Business Value

This rating system helps us:
- **Spot problems early** before accounts leave us
- **Focus on what matters** by prioritizing important accounts
- **Get warnings** when accounts start struggling
- **Know what to do** with clear next steps for each rating

## Update Frequency

Ratings are updated automatically every week, so we always have current information.