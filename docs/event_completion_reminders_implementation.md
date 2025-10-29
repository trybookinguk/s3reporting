# Event Completion Reminders - Implementation Summary

## Overview
Implementation of automated event completion reminders that sends behavioural emails via GetVero to event organisers after their events complete.

## Files Created/Modified

### 1. `modules/utils/vero_api.py` (New)
- VeroClient class with retry logic and session management
- `track_event()` method for single event tracking
- `batch_track_events()` method for bulk processing
- Follows zoho_api.py patterns for consistency
- Handles TEST_MODE for safe testing

### 2. `modules/utils/data_loader.py` (Extended)
Added data loading functions for event completion reminders:
- `load_account_balance_data()` - Loads account balance report
- `load_account_movement_daily_data()` - Loads pending transfers (handles diagnostic rows)
- `load_users_data()` - Loads user data for role-based targeting
- `load_risk_report_data()` - Loads risk report with exposure calculations

The UnifiedDataLoader class includes methods:
- `load_account_movement_daily()` - Parses AccountMovementDaily CSV with numeric columns
- `load_risk_report()` - Parses RiskReport CSV with FullBalance, Exposure, and SalesForUpcomingEvents

### 3. `event_completion_reminders.py` (New)
Main script implementing 5-phase processing:
- **Phase 1**: Data loading from S3
  - Loads BookingDataAll + BookingData for complete history
  - Loads Accounts, Balance, AccountMovementDaily, RiskReport, and Users
- **Phase 2**: Event processing (finds yesterday's completed events)
- **Phase 3**: Event classification into 5 types
  - Uses RiskReport for enhanced exposure detection
  - Merges FullBalance, SalesForUpcomingEvents, and Exposure fields
- **Phase 4**: Account-level deduplication
- **Phase 5**: User resolution and Vero event emission

### 4. `.github/workflows/event_completion_reminders.yml` (New)
GitHub Actions workflow with:
- Manual trigger only (no schedule)
- Test mode and test date parameters
- S3 cache support
- Output file artifact upload

## Key Features

### Event Types
1. `event_completed_free` - First free event with >10 tickets
2. `event_completed_paid_stripe` - First Stripe event with >10 tickets
3. `event_completed_paid_notverified` - Paid events without verified bank details
4. `event_completed_paid_requested` - Verified accounts with funds already requested
5. `event_completed_paid_notrequested` - Verified accounts needing to request funds

### Business Logic

#### First Event Detection
- Based on chronological EventDate (not EventId)
- Identifies the very first event for each account across all time

#### Exposure Calculation (Our Own Logic)
- **We calculate exposure ourselves** - don't use Risk Report's pre-calculated Exposure field
- Uses Risk Report's `Balance` field (available balance, excludes pending)
- Calculates future revenue from all upcoming events (after yesterday)
- **Exposure rule**: `Balance < (FutureRevenue / 2)`
- Rationale: They need at least 50% buffer for future events before we pay them for completed events
- **Exposed accounts are skipped** - no reminders sent

#### Funds Request Detection
- Uses AccountMovementDaily's `Pending` field (amount currently in transfer)
- Compares `Pending >= net_amount` from THIS completed event
- **requested**: Pending transfer amount covers this event's net revenue
- **notrequested**: Pending is less than this event's net revenue (they need to request more)
- Handles partial requests: If they've requested £500 but event net is £800, they get "notrequested"

#### Deduplication
- Prioritises events requiring action: notrequested > notverified > requested > stripe > free
- One email per account per day (highest priority event only)

#### Role-Based Targeting
- **Free/Stripe events**: AccountOwner only
- **Payment-related events**: AccountOwner + Finance users

### Safety Features
- TEST_MODE support throughout
- Comprehensive error handling
- CSV output for audit trail
- Batch processing with retry logic
- No emails sent to exposed accounts

## Testing
Run via GitHub Actions with:
- `test_mode: true` - Logs events without sending to Vero
- `test_date: YYYY-MM-DD` - Process historical data

## Environment Variables Required
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `VERO_API_KEY`
- `TEST_MODE` (optional)
- `TEST_DATE` (optional)

## Output

### Event Reminders CSV
Main output file: `event_completion_reminders_YYYYMMDD.csv`
- Event details (id, name, type)
- User details (vero_id, email)
- Financial metrics (payment_received, net_amount)
- Processing status and timestamps

### Audit Trail CSV
Comprehensive audit file: `event_completion_audit_YYYYMMDD.csv`
- All completed events (not just ones we send reminders for)
- Classification flags for each event type
- Risk Report data: FullBalance, SalesForUpcomingEvents, Exposure
- Exposure calculation details
- Pending transfer amounts
- Helps verify logic and troubleshoot edge cases

## Data Sources

The script uses the following S3 reports:
- **BookingDataAll** - Complete historical booking data (up to 1st of previous month)
- **BookingData** - Current month booking data (updated daily)
- **Accounts** - Account details with GatewayGroup and IsVerified status
- **AccountBalance** - Account balance snapshot
- **AccountMovementDaily** - Pending transfer amounts and balance movements
- **RiskReport** - Pre-calculated exposure metrics with FullBalance and SalesForUpcomingEvents
- **AccountUserRelationship** - User roles for targeting emails

## Implementation Status

✅ **Complete and Ready for Testing**

All components implemented and aligned with documentation:
1. Data loading functions created in `data_loader.py`
2. Risk Report integration for enhanced exposure detection
3. Vero API client with retry logic and batch processing
4. Main processing script with 5-phase approach
5. GitHub Actions workflow configured
6. Comprehensive audit trail generation

The implementation follows all existing codebase patterns (British spellings, UK timezone, S3 caching, error handling) and is ready for testing via GitHub Actions.