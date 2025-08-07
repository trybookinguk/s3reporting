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

### 2. `modules/utils/data_loaders.py` (Extended)
Added three new data loading functions:
- `load_account_balance_data()` - Loads account balance report
- `load_account_movement_daily_data()` - Loads pending transfers (handles diagnostic rows)
- `load_users_data()` - Loads user data for role-based targeting

### 3. `event_completion_reminders.py` (New)
Main script implementing 5-phase processing:
- **Phase 1**: Data loading from S3
- **Phase 2**: Event processing (finds yesterday's completed events)
- **Phase 3**: Event classification into 5 types
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
- First event detection based on chronological EventDate (not EventId)
- Exposure calculation: Skip if AccountBalance < (FutureRevenue / 2)
- Deduplication prioritizes events requiring action
- Role-based targeting (AccountOwner + Finance for payment-related events)

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
CSV file with columns:
- Event details (id, name, type)
- User details (vero_id, email)
- Financial metrics (payment_received, net_amount)
- Processing status and timestamps

The implementation follows all existing codebase patterns and is ready for testing via GitHub Actions.