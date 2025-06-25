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
  - 30 days before expected ticket sales should begin
- **Action**: Proactive contact to support upcoming event

### 4. **At Risk**
- **What it means**: They've gone quiet and that's worrying
- **How we know (depends on account type)**:
  - **New accounts**: No activity 14-27 days after account creation
  - **Annual/Seasonal**: Should be selling tickets based on historical patterns
  - **Regular**: No activity for 90-179 days
  - **Continuous**: No activity for 30-89 days
  - **Education industry**: July/August excluded from day count
- **Action**: Immediate intervention required

### 5. **Churned**
- **What it means**: They've probably stopped using us
- **How we know (depends on account type)**:
  - **New accounts**: No activity 28+ days after account creation
  - **Annual/Seasonal**: Missed expected event date (plus 30-day grace period)
  - **Regular**: No activity for 180+ days
  - **Continuous**: No activity for 90+ days
- **Action**: Win-back campaign or account closure

### 6. **Returned**
- **What it means**: They stopped using us but have come back
- **How we know**: No activity last year, but active now
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

**Schools that always start in September**:
- Flagged immediately if they haven't started by September
- Important for catching issues after summer staff changes

**Schools closed all summer (including September)**:
- July and August don't count against them
- Example: If a school was last active on June 30th and we check on September 15th, they've only been inactive for 15 days (not 77 days)

The system identifies education accounts by:
- Industry field marked as "Education"
- OR activity pattern showing 60%+ activity during term months (September-June) with summer gap

**Scottish Schools**:
- Identified automatically by their postcode
- Have different holiday patterns (only July is excluded, not August)
- Scottish schools return mid-August
- If a Scottish school normally runs August events, they're flagged immediately if inactive

### Annual Events
For accounts that run yearly or seasonal events, we predict when they should be active by looking at:
- When they ran events before (with 30-day flexibility for events that shift dates)
- How far in advance they usually set up events
- When they normally start selling tickets

The system allows annual events to vary by ±30 days from the expected 365-day cycle to accommodate real-world variations like "last Saturday in June" events.

### Tier-Based Prioritization
Only our most important accounts (Tier 3 and above) get the "Outreach" rating. This helps us focus on the accounts that matter most.

### Annual Events Report
A separate report identifies upcoming annual events for Tier 3+ accounts that need proactive outreach in the next 30 days. This report is automatically emailed to stakeholders.

## Data Sources

To work out the rating, we look at:
- **How often they run events**: This year vs last year
- **When they last sold tickets**: How many days ago
- **When their events happen**: To predict future activity
- **What type of account**: Industry classification
- **When they joined**: DateTimeCreated field from Accounts report
- **How important they are**: Their tier level

## Business Value

This rating system helps us:
- **Spot problems early** before accounts leave us
- **Focus on what matters** by prioritizing important accounts
- **Get warnings** when accounts start struggling
- **Know what to do** with clear next steps for each rating

## Update Frequency

Ratings are updated automatically every week, so we always have current information.