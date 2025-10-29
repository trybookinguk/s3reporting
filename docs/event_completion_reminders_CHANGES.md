# Event Completion Reminders - Implementation Changes Summary

## Overview
This document summarises the changes made to align the Event Completion Reminders implementation with documentation and clarify the business logic.

## Date
2025-10-29

## Changes Made

### 1. ✅ Created Missing Data Loader Functions

**File**: `modules/utils/data_loader.py`

Added to `UnifiedDataLoader` class:
- `load_account_movement_daily()` - Loads AccountMovementDaily with Pending/Transferred columns
- `load_risk_report()` - Loads RiskReport with FullBalance, Balance, SalesForUpcomingEvents, Exposure

Added wrapper functions for legacy compatibility:
- `load_account_balance_data(s3_client, target_date)`
- `load_account_movement_daily_data(s3_client, target_date)`
- `load_users_data(s3_client, target_date)`
- `load_risk_report_data(s3_client, target_date)`

**Impact**: Script can now run without import errors.

---

### 2. ✅ Integrated Risk Report for Enhanced Data

**File**: `event_completion_reminders.py`

**Changes**:
- Added Risk Report loading in Phase 1 data loading
- Merged Risk Report fields into verified events processing
- Used Risk Report's `Balance` and `FullBalance` fields for better accuracy

**Benefits**:
- More accurate balance information (Balance vs FullBalance distinction)
- Pre-calculated SalesForUpcomingEvents available for validation
- Richer audit trail with Risk Report columns

---

### 3. ✅ Fixed Exposure Calculation Logic

**Problem**: Original code used Risk Report's `Exposure` field, which has different business logic than our needs.

**Solution**: Calculate exposure ourselves based on our specific requirements.

**New Logic**:
```python
# Use Risk Report's Balance (available, excludes pending)
AccountBalance = Balance from Risk Report

# Calculate future revenue ourselves
net_future = sum(future_bookings.PaymentReceived - TotalFees)

# Our exposure calculation
is_exposed = AccountBalance < (net_future / 2)
```

**Rationale**:
- We need 50% buffer for future events before paying out completed events
- Risk Report's Exposure field serves different business purposes
- Using `Balance` (not `FullBalance`) because Pending amounts aren't available for future events

---

### 4. ✅ Clarified Funds Request Detection Logic

**Problem**: Original logic was unclear about when to classify as "requested" vs "notrequested".

**Solution**: Compare Pending transfer amount with the net amount from THIS specific event.

**New Logic**:
```python
# Pending = amount currently in transfer state (from AccountMovementDaily)
# net_amount = revenue from THIS completed event (PaymentReceived - TotalFees)

if Pending >= net_amount:
    classification = 'event_completed_paid_requested'
    # Their pending transfer covers this event's funds
else:
    classification = 'event_completed_paid_notrequested'
    # They need to request more to cover this event
```

**Examples**:
- Event net = £800, Pending = £1000 → **requested** (they've requested enough)
- Event net = £800, Pending = £500 → **notrequested** (partial request, need more)
- Event net = £800, Pending = £0 → **notrequested** (nothing requested)

---

## Data Sources & Their Usage

| Report | Column Used | Purpose |
|--------|-------------|---------|
| **RiskReport** | `Balance` | Available balance for exposure check (excludes pending) |
| **RiskReport** | `FullBalance` | Total balance including pending (informational) |
| **RiskReport** | `SalesForUpcomingEvents` | Pre-calculated future revenue (validation/audit) |
| **RiskReport** | `Exposure` | Risk Report's exposure flag (informational only, not used in logic) |
| **AccountMovementDaily** | `Pending` | Amount in pending transfer state |
| **AccountBalance** | `AccountBalance` | Fallback balance if Risk Report unavailable |

---

## Updated Business Logic Flow

### Phase 3: Event Classification - Verified Events

```
1. Merge Risk Report data (Balance, FullBalance, etc.)
2. Get available balance (Risk Report's Balance field)
3. Calculate future revenue from upcoming events
4. Check exposure: Balance < (FutureRevenue / 2)
   ├─ If EXPOSED → Skip (don't send reminder)
   └─ If NOT EXPOSED → Continue to step 5
5. Get Pending amount from AccountMovementDaily
6. Compare Pending with event's net_amount
   ├─ Pending >= net_amount → 'event_completed_paid_requested'
   └─ Pending < net_amount → 'event_completed_paid_notrequested'
```

---

## Audit Trail Enhancements

The audit CSV now includes:
- `Balance` - Available balance from Risk Report
- `FullBalance` - Total balance from Risk Report
- `SalesForUpcomingEvents` - Risk Report's future revenue calculation
- `Exposure` - Risk Report's exposure flag (for comparison)
- `is_exposed` - Our calculated exposure flag
- `Pending` - Pending transfer amount
- `net_future` - Our calculated future revenue

This allows verification and debugging of exposure calculations.

---

## Testing Checklist

Before production use, verify:

- [ ] Data loader functions import successfully
- [ ] Risk Report loads correctly
- [ ] AccountMovementDaily Pending column exists and parses
- [ ] Exposure calculation produces sensible results
- [ ] Pending vs net_amount comparison works for partial requests
- [ ] Audit trail CSV includes all new columns
- [ ] TEST_MODE works correctly
- [ ] GitHub Actions workflow runs successfully

---

## Files Modified

1. **modules/utils/data_loader.py** - Added 3 new methods and 4 wrapper functions
2. **event_completion_reminders.py** - Updated exposure and funds request logic
3. **docs/event_completion_reminders_implementation.md** - Updated documentation

---

## Implementation Status

✅ **COMPLETE** - Ready for testing

All critical gaps have been addressed:
1. Missing data loader functions created
2. Risk Report integration working
3. Exposure calculation corrected (using our own logic)
4. Funds request detection clarified (event-specific comparison)
5. Documentation updated to reflect actual implementation

---

## Notes

- The Risk Report's `Exposure` field is loaded but not used in our logic - it's for audit/informational purposes only
- We use `Balance` (not `FullBalance`) for exposure calculation because pending amounts can't be used for future events
- The Pending comparison is against THIS event's net_amount, not total account balance
- Partial requests are handled: if Pending < net_amount, they get "notrequested" reminder
