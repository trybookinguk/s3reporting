# Event Completion Reminders - Vero Event Data Structure

## Overview
This document details exactly what data is sent to Vero when an event completion reminder is triggered.

---

## HTTP Request Structure

### Endpoint
```
POST https://api.getvero.com/api/v2/events/track?auth_token={VERO_API_KEY}
```

### Headers
```json
{
  "Content-Type": "application/json",
  "Accept": "application/json"
}
```

### Request Body
```json
{
  "identity": {
    "id": "uk_{UserId}",
    "email": "{user_email}"
  },
  "event_name": "{vero_event_type}",
  "data": {
    "event_id": 12345,
    "event_name": "Summer Festival 2025",
    "account_id": 67890,
    "event_type": "paid",
    "ticket_quantity": 250
  },
  "extras": {
    "source": "TryBooking Event Completion Script",
    "event_name_tb": "Summer Festival 2025",
    "isMultiple": false,
    "testmode": false
  }
}
```

---

## Field Breakdown

### Identity Section (Who gets the email)

| Field | Source | Example | Description |
|-------|--------|---------|-------------|
| **id** | `'uk_' + UserId` | `"uk_12345"` | Vero user identifier (UK region prefix + TryBooking UserId) |
| **email** | `Username` from AccountUserRelationship | `"organiser@example.com"` | User's email address |

**Notes**:
- Only users with matching AccountId and correct role (AccountOwner or Finance) receive events
- Deleted users (`IsDeleted = '1'`) are excluded
- Users without email addresses are excluded

---

### Event Name (Which Vero campaign triggers)

| Value | When It's Sent |
|-------|----------------|
| `event_completed_free` | First free event completed, >10 tickets |
| `event_completed_paid_stripe` | First Stripe Connect event completed, >10 tickets |
| `event_completed_paid_notverified` | Paid event, bank details not verified |
| `event_completed_paid_requested` | Verified account, funds already requested (Pending >= net_amount) |
| `event_completed_paid_notrequested` | Verified account, funds not yet requested (Pending < net_amount) |

**Note**: These event names must match campaigns configured in Vero (GetVero dashboard).

---

### Data Section (Event details for personalisation)

| Field | Type | Source | Example | Description |
|-------|------|--------|---------|-------------|
| **event_id** | int | `EventId` from BookingData | `12345` | TryBooking event identifier |
| **event_name** | string | `EventName` from BookingData | `"Summer Festival 2025"` | Name of the completed event |
| **account_id** | int | `AccountId` from BookingData | `67890` | TryBooking account identifier |
| **event_type** | string | Calculated | `"paid"` or `"free"` | Whether event had paid tickets |
| **ticket_quantity** | int | Sum of `TicketQuantity` for event | `250` | Total tickets sold |

**Note**: Financial amounts (`payment_received`, `net_amount`) are **not sent to Vero** as they're not displayed in emails. Users should log in to TryBooking to view financial details.

**Usage in Vero**:
These fields can be used in email templates for personalisation:
- `{{ event.event_name }}` - Show which event completed
- `{{ event.ticket_quantity }}` - Show how many tickets were sold
- `{{ event.event_type }}` - "paid" or "free" for conditional messaging

---

### Extras Section (Metadata for filtering/tracking)

| Field | Type | Value | Purpose |
|-------|------|-------|---------|
| **source** | string | `"TryBooking Event Completion Script"` | Identifies events came from automated script (not manual tracking) |
| **event_name_tb** | string | `{EventName}` from TryBooking | Duplicate of event_name for filtering/segmentation |
| **isMultiple** | boolean | `true` / `false` | `true` if account had multiple events completing yesterday (useful for different messaging) |
| **testmode** | boolean | `true` / `false` | `true` if running with TEST_MODE=1 (allows filtering test events in Vero) |

**Usage**:
- **source**: Filter automated vs manual events in Vero analytics
- **isMultiple**: Trigger different email copy: "Your event has completed" vs "Your events have completed"
- **testmode**: Filter out test events in Vero analytics, or show "TEST" banner in email templates

---

## Who Receives Each Event Type

### Free & Stripe Events
**Recipients**: AccountOwner only
- These are simpler events requiring less financial management
- Only the account owner needs to be notified

### Payment Events (Not Verified, Requested, Not Requested)
**Recipients**: AccountOwner + Finance users
- These involve financial transactions and bank details
- Both account owners and finance managers should be informed

---

## Example Payloads

### Example 1: Free Event Completion
```json
{
  "identity": {
    "id": "uk_12345",
    "email": "john@example.com"
  },
  "event_name": "event_completed_free",
  "data": {
    "event_id": 98765,
    "event_name": "Community Meetup",
    "account_id": 55555,
    "event_type": "free",
    "ticket_quantity": 50
  },
  "extras": {
    "source": "TryBooking Event Completion Script",
    "event_name_tb": "Community Meetup",
    "isMultiple": false,
    "testmode": false
  }
}
```

### Example 2: Funds Not Requested
```json
{
  "identity": {
    "id": "uk_67890",
    "email": "finance@festival.com"
  },
  "event_name": "event_completed_paid_notrequested",
  "data": {
    "event_id": 12345,
    "event_name": "Summer Festival 2025",
    "account_id": 11111,
    "event_type": "paid",
    "ticket_quantity": 500
  },
  "extras": {
    "source": "TryBooking Event Completion Script",
    "event_name_tb": "Summer Festival 2025",
    "isMultiple": false,
    "testmode": false
  }
}
```

**Email Context**: This user should log in to TryBooking to request a payout for their completed event.

### Example 3: Multiple Events (isMultiple = true)
```json
{
  "identity": {
    "id": "uk_99999",
    "email": "owner@events.com"
  },
  "event_name": "event_completed_paid_notrequested",
  "data": {
    "event_id": 55555,
    "event_name": "Concert Series - Show 1",
    "account_id": 77777,
    "event_type": "paid",
    "ticket_quantity": 150
  },
  "extras": {
    "source": "TryBooking Event Completion Script",
    "event_name_tb": "Concert Series - Show 1",
    "isMultiple": true,
    "testmode": false
  }
}
```

**Email Context**: This account had multiple events complete yesterday. The `isMultiple` flag allows Vero to use different wording like "Your events have completed" or provide aggregated information.

---

## Vero Email Template Variables

When creating email templates in Vero, you can reference these variables:

### Identity Fields
- `{{ user.id }}` - Vero user ID (uk_{UserId})
- `{{ user.email }}` - User's email address

### Data Fields
- `{{ event.event_id }}` - TryBooking event ID
- `{{ event.event_name }}` - Event name
- `{{ event.account_id }}` - TryBooking account ID
- `{{ event.event_type }}` - "paid" or "free"
- `{{ event.ticket_quantity }}` - Number of tickets sold

### Extras Fields
- `{{ extras.source }}` - Data source identifier
- `{{ extras.event_name_tb }}` - TryBooking event name
- `{{ extras.isMultiple }}` - Boolean for multiple events
- `{{ extras.testmode }}` - Boolean indicating if event sent in test mode

### Example Email Template Usage
```liquid
{% if extras.testmode %}
<div style="background: #ff0; padding: 10px; text-align: center;">
  <strong>TEST MODE</strong> - This email was sent from a test run
</div>
{% endif %}

Hi there,

{% if extras.isMultiple %}
Your events have completed! Here are the details for {{ event.event_name }}:
{% else %}
Your event "{{ event.event_name }}" has completed!
{% endif %}

Tickets sold: {{ event.ticket_quantity }}

{% if event_name == "event_completed_paid_notrequested" %}
You can now log in to TryBooking to request your funds to be transferred to your bank account.
{% elsif event_name == "event_completed_paid_requested" %}
Your funds are being processed and will arrive soon.
{% elsif event_name == "event_completed_paid_notverified" %}
Please verify your bank details in TryBooking before you can receive payments.
{% elsif event_name == "event_completed_paid_stripe" %}
Your Stripe integration is working! View your payouts in your Stripe dashboard.
{% endif %}
```

---

## Processing Flow Summary

```
1. Event Completion Detected (yesterday was last session)
   ↓
2. Event Classified (one of 5 types)
   ↓
3. Account-Level Deduplication (one event per account)
   ↓
4. User Resolution (find AccountOwner + Finance users)
   ↓
5. Build Vero Event Payload
   ↓
6. POST to https://api.getvero.com/api/v2/events/track
   ↓
7. Vero Triggers Campaign (sends email based on event_name)
```

---

## Technical Details

### Batching
- Events are processed in batches of 100
- Each batch is sent sequentially (not parallel) to avoid rate limits
- Individual event failures don't stop batch processing

### Retry Logic
- Automatic retry with exponential backoff (1s, 2s, 4s)
- Rate limit errors (429) get longer backoff (2x)
- Maximum 3 retry attempts

### Error Handling
- Failed events are logged in the CSV with error messages
- Batch failures don't stop subsequent batches
- All results (success/failure) saved to `event_completion_reminders_YYYYMMDD.csv`

### Test Mode
When `TEST_MODE=1`:
- No actual HTTP requests made to Vero
- Events logged to console showing what would be sent
- All events marked as "Test Mode - Not Sent" in output CSV

---

## Summary

**What Vero Receives**:
1. **User Identity**: Who to send the email to (uk_{UserId} + email)
2. **Event Type**: Which campaign to trigger (one of 5 event names)
3. **Event Data**: 7 fields about the completed TryBooking event
4. **Metadata**: Source, isMultiple flag for context

**Purpose**: Enable Vero to send personalised, triggered emails reminding event organisers to:
- Request payouts for completed events
- Verify bank details if not verified
- Take action on their Stripe integration
- Get notified when their first free event completes

All data can be used in Vero email templates for personalisation and conditional logic.
