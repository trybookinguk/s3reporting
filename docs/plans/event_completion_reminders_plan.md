# Event Completion Reminder Script Implementation Plan

## Overview
This document outlines the implementation plan for an automated ETL script that sends reminders to event organisers after their events complete. The script integrates with GetVero to send targeted behavioural emails based on event type, payment status, and account configuration.

**Important Note**: All dates and times in the system are stored and processed in UTC.

## 1. Script Architecture

### Main Components
- **Main script**: `event_completion_reminders.py` in root directory
- **New module**: `modules/utils/vero_api.py` for GetVero API integration
- **Extensions**: Updates to `modules/utils/data_loaders.py` for new report types
- **Output**: CSV file with all events emitted for tracking/auditing

### 2. Required New Modules

#### A. `modules/utils/vero_api.py`
- GetVero API client with retry logic following zoho_api.py pattern
- Track event function using the Vero Track API endpoint
- Authentication using VERO_AUTH_TOKEN
- Batch processing support for efficiency

#### B. Extend `modules/utils/data_loaders.py`
Add functions to load:
- `load_account_balance_data()` - for account balance report
- `load_account_movement_daily_data()` - for pending transfers
- `load_users_data()` - for user roles

## 3. Main Script Logic

### Phase 1: Data Loading
```python
# Load comprehensive booking data
booking_all_df = load_booking_data(s3_client, target_date, 'BookingDataAll')
booking_current_df = load_booking_data(s3_client, target_date, 'BookingData')

# Combine for complete picture
booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
booking_df = booking_df.drop_duplicates(subset=['BookingId'])

# Load other reports
accounts_df = load_accounts_data(s3_client, target_date)
balance_df = load_account_balance_data(s3_client, target_date)
movement_df = load_account_movement_daily_data(s3_client, target_date)
users_df = load_users_data(s3_client, target_date)

# Clean users data
users_df = users_df[users_df['UserId'].notna()]
```

### Phase 2: Event Processing

```python
# Identify ALL last sessions
all_last_sessions = booking_df.groupby('EventId')['EventDate'].agg(['min', 'max'])
all_last_sessions.columns = ['first_session', 'last_session']

# Find events where yesterday was the last session
# Note: EventDate is already in UTC from data_loaders
yesterday = target_date - timedelta(days=1)
yesterday_date = yesterday.date()
completed_event_ids = all_last_sessions[
    all_last_sessions['last_session'].dt.date == yesterday_date
].index

# Get detailed metrics for completed events
completed_events = booking_df[booking_df['EventId'].isin(completed_event_ids)]
event_metrics = completed_events.groupby('EventId').agg({
    'PaymentReceived': 'sum',
    'BookingFee': 'sum',
    'CardFee': 'sum',
    'ProcessingFee': 'sum',
    'TicketFee': 'sum',
    'TicketQuantity': 'sum',
    'AccountId': 'first',
    'EventName': 'first'
}).reset_index()

# Calculate total fees
fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
event_metrics['TotalFees'] = event_metrics[fee_columns].sum(axis=1)
event_metrics['net_amount'] = event_metrics['PaymentReceived'] - event_metrics['TotalFees']

# Determine event type
event_metrics['event_type'] = np.where(
    event_metrics['PaymentReceived'] == 0, 'free', 'paid'
)

# Find first events per account (chronologically by EventDate)
# First, get the earliest event date per account
first_event_dates = booking_df.groupby('AccountId')['EventDate'].min().reset_index()
first_event_dates.columns = ['AccountId', 'FirstEventDate']

# Then find the EventId for each account's first event
# (handling ties by taking the min EventId if multiple events on same date)
first_events = booking_df.merge(first_event_dates, on=['AccountId'])
first_events = first_events[first_events['EventDate'] == first_events['FirstEventDate']]
first_events_per_account = first_events.groupby('AccountId')['EventId'].min().to_dict()

# Mark first events in our metrics
event_metrics['is_first_event'] = event_metrics.apply(
    lambda x: x['EventId'] == first_events_per_account.get(x['AccountId'], None), 
    axis=1
)

# Merge with account data
event_metrics = event_metrics.merge(
    accounts_df[['AccountId', 'GatewayGroup', 'IsVerified']], 
    on='AccountId', 
    how='left'
)
```

### Phase 3: Event Classification

```python
# Initialize
event_metrics['vero_event'] = None

# Free events
free_events_mask = (
    (event_metrics['event_type'] == 'free') & 
    (event_metrics['is_first_event'] == True) & 
    (event_metrics['TicketQuantity'] > 10)
)
event_metrics.loc[free_events_mask, 'vero_event'] = 'event_completed_free'

# Stripe events
stripe_mask = (
    (event_metrics['event_type'] == 'paid') & 
    (event_metrics['GatewayGroup'] == 'Stripe Connect') &
    (event_metrics['is_first_event'] == True) & 
    (event_metrics['TicketQuantity'] > 10)
)
event_metrics.loc[stripe_mask, 'vero_event'] = 'event_completed_paid_stripe'

# Not verified
not_verified_mask = (
    (event_metrics['event_type'] == 'paid') & 
    (event_metrics['GatewayGroup'] == 'Default (All)') &
    (event_metrics['IsVerified'] != 1)
)
event_metrics.loc[not_verified_mask, 'vero_event'] = 'event_completed_paid_notverified'

# Verified accounts
verified_mask = (
    (event_metrics['event_type'] == 'paid') & 
    (event_metrics['GatewayGroup'] == 'Default (All)') &
    (event_metrics['IsVerified'] == 1) &
    (event_metrics['vero_event'].isna())
)

if verified_mask.any():
    verified_events = event_metrics[verified_mask].copy()
    
    # Add account balance
    verified_events = verified_events.merge(
        balance_df[['AccountId', 'AccountBalance']], 
        on='AccountId', 
        how='left'
    )
    verified_events['AccountBalance'] = pd.to_numeric(
        verified_events['AccountBalance'], errors='coerce'
    ).fillna(0)
    
    # Calculate future revenue
    future_bookings = booking_df[booking_df['EventDate'].dt.date > yesterday_date]
    future_revenue = future_bookings.groupby('AccountId').agg({
        'PaymentReceived': 'sum',
        'BookingFee': 'sum',
        'CardFee': 'sum', 
        'ProcessingFee': 'sum',
        'TicketFee': 'sum'
    })
    future_revenue['net_future'] = (
        future_revenue['PaymentReceived'] - 
        future_revenue[['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']].sum(axis=1)
    )
    
    verified_events = verified_events.merge(
        future_revenue[['net_future']], 
        left_on='AccountId',
        right_index=True,
        how='left'
    )
    verified_events['net_future'] = verified_events['net_future'].fillna(0)
    
    # Check exposure
    verified_events['is_exposed'] = (
        verified_events['AccountBalance'] < (verified_events['net_future'] / 2)
    )
    
    # Process only non-exposed accounts
    non_exposed_mask = ~verified_events['is_exposed']
    if non_exposed_mask.any():
        non_exposed = verified_events[non_exposed_mask].copy()
        
        # Get pending amount from AccountMovementDaily (skip diagnostic row)
        movement_clean = movement_df.iloc[1:].copy()
        movement_clean['Pending'] = pd.to_numeric(
            movement_clean['Pending'], errors='coerce'
        ).fillna(0)
        
        # Get the latest pending amount per account (snapshot value)
        latest_pending = movement_clean.groupby('AccountId')['Pending'].last()
        
        non_exposed = non_exposed.merge(
            latest_pending,
            on='AccountId',
            how='left'
        )
        non_exposed['Pending'] = non_exposed['Pending'].fillna(0)
        
        # Check if pending amount >= net amount from event
        non_exposed['vero_event'] = np.where(
            non_exposed['Pending'] >= non_exposed['net_amount'],
            'event_completed_paid_requested',
            'event_completed_paid_notrequested'
        )
        
        event_metrics.loc[non_exposed.index, 'vero_event'] = non_exposed['vero_event']
```

### Phase 4: Account-Level Deduplication

```python
# Filter to events that need sending
events_to_send = event_metrics[event_metrics['vero_event'].notna()].copy()

# DEDUPLICATION: For accounts with multiple events completing on same day
# Keep only the highest priority event per account
priority_map = {
    'event_completed_paid_notrequested': 1,  # Highest priority - needs action
    'event_completed_paid_notverified': 2,   
    'event_completed_paid_requested': 3,     
    'event_completed_paid_stripe': 4,        
    'event_completed_free': 5                # Lowest priority
}

events_to_send['priority'] = events_to_send['vero_event'].map(priority_map)

# Sort by AccountId and priority, keep first (highest priority) per account
events_to_send = events_to_send.sort_values(['AccountId', 'priority'])
events_to_send = events_to_send.groupby('AccountId').first().reset_index()

# Log deduplication
original_count = len(event_metrics[event_metrics['vero_event'].notna()])
deduped_count = len(events_to_send)
if original_count > deduped_count:
    print(f"Deduplication: Reduced {original_count} events to {deduped_count}")
```

### Phase 5: User Resolution and Event Emission

```python
# Prepare users
users_df['UserId'] = users_df['UserId'].astype(str)
users_df['vero_id'] = 'uk_' + users_df['UserId']
users_df['AccountId'] = pd.to_numeric(users_df['AccountId'], errors='coerce')

# Remove invalid users
valid_users = users_df[
    users_df['AccountId'].notna() & 
    users_df['RoleName'].notna() &
    (users_df['IsDeleted'] != '1')
]

# Build events list
vero_events = []

for _, event in events_to_send.iterrows():
    # Determine required roles
    if event['vero_event'] in ['event_completed_free', 'event_completed_paid_stripe']:
        required_roles = ['AccountOwner']
    else:
        required_roles = ['AccountOwner', 'Finance']
    
    # Find relevant users
    relevant_users = valid_users[
        (valid_users['AccountId'] == event['AccountId']) & 
        (valid_users['RoleName'].isin(required_roles))
    ]
    
    # Create event for each user
    for _, user in relevant_users.iterrows():
        vero_events.append({
            'event_id': int(event['EventId']),
            'event_name': str(event['EventName']),
            'account_id': int(event['AccountId']),
            'event_type': event['event_type'],
            'vero_event': event['vero_event'],
            'vero_user_id': user['vero_id'],
            'user_email': user['Username'],
            'payment_received': float(event['PaymentReceived']),
            'net_amount': float(event['net_amount']),
            'ticket_quantity': int(event['TicketQuantity'])
        })

# Convert to DataFrame
vero_df = pd.DataFrame(vero_events)

# Initialize results tracking
vero_df['status'] = 'Pending'
vero_df['error_message'] = None
vero_df['timestamp'] = datetime.now(UK_TZ).replace(tzinfo=None)  # Store as naive datetime for CSV

# Send to Vero
if not TEST_MODE:
    vero_client = VeroClient(VERO_AUTH_TOKEN)
    
    # Process in batches
    batch_size = 100
    for i in range(0, len(vero_df), batch_size):
        batch = vero_df.iloc[i:i+batch_size]
        try:
            results = vero_client.batch_track_events(batch)
            vero_df.loc[batch.index, 'status'] = 'Success'
        except Exception as e:
            vero_df.loc[batch.index, 'status'] = 'Failed'
            vero_df.loc[batch.index, 'error_message'] = str(e)
else:
    vero_df['status'] = 'Test Mode - Not Sent'

# Save output
output_filename = f"event_completion_reminders_{target_date.strftime('%Y%m%d')}.csv"
vero_df.to_csv(output_filename, index=False)

# Print summary
print(f"\nEvent Completion Reminders Summary - {target_date.strftime('%Y-%m-%d')}")
print(f"=" * 50)
print(f"Events with completed sessions: {len(event_metrics)}")
print(f"Events requiring notification: {original_count}")
print(f"After deduplication: {deduped_count}")
print(f"Vero events created: {len(vero_df)}")
print(f"Success: {(vero_df['status'] == 'Success').sum()}")
print(f"Failed: {(vero_df['status'] == 'Failed').sum()}")
```

## 4. Event Types and Business Logic

### Free Events
- **Trigger**: First event for account with >10 tickets
- **Event**: `event_completed_free`
- **Recipients**: AccountOwner only

### Paid Events - Stripe Connect
- **Trigger**: First event for account with >10 tickets using Stripe
- **Event**: `event_completed_paid_stripe`
- **Recipients**: AccountOwner only

### Paid Events - Default Gateway (Not Verified)
- **Trigger**: Account bank details not verified
- **Event**: `event_completed_paid_notverified`
- **Recipients**: AccountOwner and Finance roles

### Paid Events - Default Gateway (Verified)
Two possible outcomes based on exposure and pending transfers:

1. **Funds Already Requested**
   - **Trigger**: Pending amount >= event net amount
   - **Event**: `event_completed_paid_requested`
   - **Recipients**: AccountOwner and Finance roles

2. **Funds Not Yet Requested**
   - **Trigger**: Pending amount < event net amount
   - **Event**: `event_completed_paid_notrequested`
   - **Recipients**: AccountOwner and Finance roles

## 5. Key Technical Considerations

### Data Processing
- Uses vectorized pandas operations throughout for performance
- Loads both BookingDataAll (historical) and BookingData (current month)
- Handles deduplication at account level (one event per account per day)
- Prioritizes events that require action
- First event detection based on chronological EventDate, not EventId

### Exposure Calculation
- Account is exposed if: `AccountBalance < (FutureEventRevenue / 2)`
- Exposed accounts are skipped (no reminder sent)
- Future revenue includes all events after yesterday

### Pending Amount Logic
- Uses the latest snapshot value from AccountMovementDaily
- Compares pending amount to net amount available from the event
- Skips first diagnostic row in the report

### User Identification
- Uses format `uk_{UserId}` for Vero identification
- Filters users by role based on event type
- Excludes deleted users

## 6. CSV Output Format

```csv
event_id,event_name,account_id,event_type,vero_event,vero_user_id,user_email,payment_received,net_amount,ticket_quantity,status,error_message,timestamp
12345,"Summer Festival",1001,"paid","event_completed_paid_notrequested","uk_2","owner@example.com",500.00,450.00,25,"Success",,2025-01-07 10:30:00
```

## 7. GitHub Actions Workflow

```yaml
name: Event Completion Reminders

on:
  workflow_dispatch:
    inputs:
      test_mode:
        description: 'Test mode - log events without sending'
        type: boolean
        default: false
      test_date:
        description: 'Process events from specific date (YYYY-MM-DD)'
        type: string

jobs:
  event-reminders:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install boto3 pandas numpy requests pytz
      - name: Run Event Completion Reminders
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_KEY }}
          VERO_AUTH_TOKEN: ${{ secrets.VERO_AUTH_TOKEN }}
          TEST_MODE: ${{ inputs.test_mode }}
          TEST_DATE: ${{ inputs.test_date }}
        run: python event_completion_reminders.py
```

## 8. Testing Strategy

### Test Mode
- Set `TEST_MODE=1` to log events without sending to Vero
- All events marked as "Test Mode - Not Sent" in CSV output

### Historical Processing
- Use `TEST_DATE` to process events from a specific date
- Useful for testing logic with known data

### Output Verification
- CSV file provides complete audit trail
- Includes status, error messages, and timestamps
- Summary printed to console

## 9. Error Handling

- Continues processing if individual events fail
- Logs errors to CSV with detailed messages
- Processes events in batches to handle API limits
- Uses exponential backoff for retries
- Provides comprehensive summary at completion

## 10. Environment Variables

### Required
- `AWS_ACCESS_KEY_ID`: AWS credentials for S3 access
- `AWS_SECRET_ACCESS_KEY`: AWS credentials for S3 access
- `VERO_AUTH_TOKEN`: GetVero API authentication token

### Optional
- `TEST_MODE`: Set to 1 to enable test mode
- `TEST_DATE`: Override date for processing (YYYY-MM-DD format)

## 11. Implementation Timeline

1. Create `vero_api.py` module with API client
2. Extend `data_loaders.py` with new report loaders
3. Implement main script with phased logic
4. Add comprehensive logging and error handling
5. Create GitHub Actions workflow
6. Test with historical data in test mode
7. Deploy to production

This implementation follows established patterns in the codebase while adding the new GetVero integration capability.