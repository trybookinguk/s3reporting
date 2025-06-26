# Event Frequency Logic Documentation

## Overview

The event frequency classification system analyzes account activity patterns to understand how often event organisers run events throughout the year. This helps identify account behavior changes and potential risks.

## How It Works

### 1. Data Collection
- The system tracks which months have events with ticket sales
- Uses **EventDate** field (when the event happens, not when tickets are sold)
- Also requires **EventId** field to group events properly
- Tracks two 12-month periods:
  - **Current Period**: From start of current month going back 12 months
  - **Previous Period**: The 12 months before that

### 2. Month Counting
For each period, the system counts **unique months** where events occurred:
- If an account had events in January, March, and January again, it counts as 2 unique months
- Multiple events in the same month still count as 1 month
- Only events with actual ticket sales are counted

### 3. Classification Rules

Based on the number of unique months with events:

| Months Active | Classification | Description |
|--------------|----------------|-------------|
| 0 | Inactive | No events with ticket sales in the period |
| 1 | Annual | Events in only one month of the year |
| 2-4 | Seasonal | Events in specific seasons/quarters |
| 5-9 | Regular | Events throughout most of the year |
| 10-12 | Continuous | Year-round event activity |

### 4. Important Note About "New" Classification
"New" is NOT an event frequency classification. It is only used in the Activity Rating field:
- Activity Rating "New" = Account created in last 14 days with no bookings
- Event Frequency for new accounts = "Inactive" (0 months of activity)
- This distinction is important: Event Frequency measures historical patterns, while Activity Rating considers account lifecycle

## Key Design Decisions

### Why Month Boundaries?
- The system uses the **1st of each month** as the cutoff date
- This provides stable classifications when the system runs weekly
- Example: If today is November 15, 2024, the current period is November 1, 2023 to November 1, 2024

### Why Track Two Periods?
- Enables comparison: "Was Regular, now Seasonal" indicates declining activity
- Provides context for understanding account trends
- Helps identify at-risk accounts early

### Months Active Export
The system exports which specific months have activity to Zoho CRM:
- Format: Array of full month names (e.g., ["January", "March", "July", "December"])
- Shows the seasonal pattern at a glance
- Only includes months from the current period (last 12 months)
- Sent as a multi-select field to Zoho CRM

## Examples

### Example 1: Summer Festival Organiser
- Events in June, July, August for past 3 years
- **Current Period**: 3 unique months
- **Classification**: Seasonal
- **Months Active**: ["June", "July", "August"]

### Example 2: Monthly Quiz Night
- Events every month except December
- **Current Period**: 11 unique months
- **Classification**: Continuous
- **Months Active**: ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November"]

### Example 3: New Theatre Company
- Account created 2 months ago
- Created their first event last week
- No ticket sales yet
- **Current Period**: 0 unique months
- **Event Frequency Classification**: Inactive
- **Activity Rating**: Active (because account is >14 days old and has created an event)

### Example 4: Annual Charity Gala
- One big event each November
- **Current Period**: 1 unique month
- **Classification**: Annual
- **Months Active**: ["November"]

## Technical Implementation

The logic is split across several files:

1. **config.py**: Defines the cutoff dates
   - `EVENT_FREQ_CUTOFF_CURRENT`: Start of month 12 months ago
   - `EVENT_FREQ_CUTOFF_PREVIOUS`: Start of month 24 months ago

2. **s3_data_loader.py**: Extracts event months from booking data
   - Groups transactions by EventId and EventDate
   - Tracks unique (year, month) combinations
   - Maintains separate sets for tier calculation and frequency windows

3. **event_frequency.py**: Contains classification logic
   - `classify_event_frequency()`: Converts month count to classification
   - `format_months_active_for_zoho()`: Returns list of month names for Zoho multi-select
   - `get_months_active_fingerprint()`: Extracts unique months from event data

4. **account_processor.py**: Orchestrates the classification
   - Combines all logic to assign final classifications
   - Separates current period months for Zoho export
   - Note: "New" classification only exists in Activity Rating, not Event Frequency

## Edge Cases Handled

1. **Missing EventDate**: Some transactions may not have EventDate populated. These are excluded from frequency calculation.

2. **Multi-session Events**: Events with multiple sessions in the same month are counted as one month.

3. **Account Creation Timing**: New accounts need time to establish patterns, so recent accounts with single-month activity aren't immediately classified as "Annual".

4. **Data Caching**: The system caches S3 files for performance. After updating field parsing logic (e.g., EventId/EventDate), the cache must be cleared for changes to take effect.