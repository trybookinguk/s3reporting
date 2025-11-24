#!/usr/bin/env python3
"""
Event Completion Reminders Script
Sends behavioural email reminders via GetVero to event organisers after their events complete.
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from modules.utils.data_loader import get_s3_client
from modules.utils.data_loader import (
    load_accounts_data, load_booking_data,
    load_account_balance_data, load_account_movement_daily_data,
    load_users_data, load_risk_report_data
)
from modules.utils.date_utils import get_latest_data_date, UK_TZ
from modules.utils.vero_api import VeroClient
from modules.utils.config import TEST_MODE

# Environment variables
VERO_API_KEY = os.environ.get('VERO_API_KEY')
TEST_DATE = os.environ.get('TEST_DATE')

if not VERO_API_KEY and not TEST_MODE:
    print("Error: VERO_API_KEY environment variable not set")
    sys.exit(1)


def main():
    """Main function to process event completion reminders."""
    print("=" * 50)
    print("Event Completion Reminders Script")
    print("=" * 50)
    
    # Get target date (defaults to yesterday)
    if TEST_DATE:
        try:
            target_date = pd.Timestamp(TEST_DATE, tz=UK_TZ)
            print(f"Using test date: {TEST_DATE}")
        except Exception as e:
            print(f"Error parsing TEST_DATE '{TEST_DATE}': {e}")
            sys.exit(1)
    else:
        target_date = get_latest_data_date()
    
    print(f"Processing date: {target_date.strftime('%Y-%m-%d')}")
    print(f"Test mode: {'ON' if TEST_MODE else 'OFF'}")
    print("")
    
    # Initialize S3 client
    s3_client = get_s3_client()
    
    # Phase 1: Data Loading
    print("Phase 1: Loading data...")
    print("-" * 30)
    
    try:
        # Load comprehensive booking data
        booking_all_df = load_booking_data(s3_client, target_date, 'BookingDataAll')
        booking_current_df = load_booking_data(s3_client, target_date, 'BookingData')

        # Combine for complete picture
        booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
        booking_df = booking_df.drop_duplicates(subset=['BookingId'])
        print(f"  Total bookings loaded: {len(booking_df):,}")

        # Load other reports
        accounts_df = load_accounts_data(s3_client, target_date)
        print(f"  Accounts loaded: {len(accounts_df):,}")

        balance_df = load_account_balance_data(s3_client, target_date)
        print(f"  Account balances loaded: {len(balance_df):,}")

        movement_df = load_account_movement_daily_data(s3_client, target_date)
        print(f"  Account movements loaded: {len(movement_df):,}")

        # Load Risk Report for exposure data
        risk_df = load_risk_report_data(s3_client, target_date)
        print(f"  Risk report loaded: {len(risk_df):,}")

        users_df = load_users_data(s3_client, target_date)
        print(f"  Users loaded: {len(users_df):,}")
        
        # Clean users data
        users_df = users_df[users_df['UserId'].notna()]
        print(f"  Valid users after cleaning: {len(users_df):,}")
        
    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)
    
    # Phase 2: Event Processing
    print("\nPhase 2: Processing events...")
    print("-" * 30)
    
    # Identify ALL last sessions
    all_last_sessions = booking_df.groupby('EventId')['EventDate'].agg(['min', 'max'])
    all_last_sessions.columns = ['first_session', 'last_session']
    
    # Find events where yesterday was the last session
    yesterday = target_date - timedelta(days=1)
    yesterday_date = yesterday.date()
    completed_event_ids = all_last_sessions[
        all_last_sessions['last_session'].dt.date == yesterday_date
    ].index
    
    print(f"  Events with completed sessions yesterday: {len(completed_event_ids):,}")
    
    if len(completed_event_ids) == 0:
        print("  No events completed yesterday. Exiting.")
        sys.exit(0)
    
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
    
    print(f"  Free events: {(event_metrics['event_type'] == 'free').sum()}")
    print(f"  Paid events: {(event_metrics['event_type'] == 'paid').sum()}")
    print(f"  First events: {event_metrics['is_first_event'].sum()}")
    
    # Phase 3: Event Classification
    print("\nPhase 3: Classifying events...")
    print("-" * 30)

    # Initialize
    event_metrics['vero_event'] = None

    # Mark events to skip (free events and Stripe Connect that don't meet criteria)
    # These will be explicitly excluded from further processing
    event_metrics['skip_further_processing'] = False

    # Free events - ONLY send if first event with >10 tickets
    free_events_mask = (
        (event_metrics['event_type'] == 'free') &
        (event_metrics['is_first_event'] == True) &
        (event_metrics['TicketQuantity'] > 10)
    )
    event_metrics.loc[free_events_mask, 'vero_event'] = 'event_completed_free'
    print(f"  Free events (>10 tickets): {free_events_mask.sum()}")

    # Mark ALL free events as processed (so they don't fall through to other logic)
    event_metrics.loc[event_metrics['event_type'] == 'free', 'skip_further_processing'] = True

    # Stripe events - ONLY send if first event with >10 tickets
    stripe_mask = (
        (event_metrics['event_type'] == 'paid') &
        (event_metrics['GatewayGroup'] == 'Stripe Connect') &
        (event_metrics['is_first_event'] == True) &
        (event_metrics['TicketQuantity'] > 10)
    )
    event_metrics.loc[stripe_mask, 'vero_event'] = 'event_completed_paid_stripe'
    print(f"  Stripe events (>10 tickets): {stripe_mask.sum()}")

    # Mark ALL Stripe Connect events as processed (so they don't fall through to verified logic)
    event_metrics.loc[event_metrics['GatewayGroup'] == 'Stripe Connect', 'skip_further_processing'] = True

    # Not verified - handle null IsVerified as not verified
    # Only process Default (All) gateway, paid events, that haven't been marked to skip
    not_verified_mask = (
        (event_metrics['event_type'] == 'paid') &
        (event_metrics['GatewayGroup'] == 'Default (All)') &
        (event_metrics['IsVerified'] != 1) &
        (~event_metrics['skip_further_processing'])
    )
    event_metrics.loc[not_verified_mask, 'vero_event'] = 'event_completed_paid_notverified'
    print(f"  Not verified events: {not_verified_mask.sum()}")

    # Initialize verified_events as empty DataFrame for audit trail
    verified_events = pd.DataFrame()

    # Verified accounts - only process those not already handled
    verified_mask = (
        (event_metrics['event_type'] == 'paid') &
        (event_metrics['GatewayGroup'] == 'Default (All)') &
        (event_metrics['IsVerified'] == 1) &
        (~event_metrics['skip_further_processing']) &
        (event_metrics['vero_event'].isna())
    )
    
    if verified_mask.any():
        print(f"  Processing {verified_mask.sum()} verified events...")
        verified_events = event_metrics[verified_mask].copy()

        # Merge with Risk Report for balance data (informational only)
        verified_events = verified_events.merge(
            risk_df[['AccountId', 'FullBalance', 'Balance', 'SalesForUpcomingEvents', 'Exposure']],
            on='AccountId',
            how='left'
        )

        # Get account balance - use Risk Report's Balance (available, excludes pending)
        # This is the amount available for future events
        verified_events = verified_events.merge(
            balance_df[['AccountId', 'AccountBalance']],
            on='AccountId',
            how='left',
            suffixes=('', '_fallback')
        )

        # Use Balance from Risk Report (available balance, excludes pending)
        # Fallback to AccountBalance, then FullBalance if neither available
        verified_events['AccountBalance'] = verified_events['Balance'].fillna(
            verified_events['AccountBalance']
        ).fillna(
            verified_events['FullBalance']
        ).fillna(0)

        # Calculate our own future revenue (this is what we'll actually owe them)
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

        # Calculate exposure ourselves - don't use Risk Report's definition
        # Exposure: Available balance should be at least half of what we owe for future events
        # This ensures they have enough buffer for future events before we pay them
        verified_events['is_exposed'] = (
            verified_events['AccountBalance'] < (verified_events['net_future'] / 2)
        )
        
        print(f"    Exposed accounts (skipped): {verified_events['is_exposed'].sum()}")
        
        # Process only non-exposed accounts
        non_exposed_mask = ~verified_events['is_exposed']
        print(f"    Non-exposed verified events: {non_exposed_mask.sum()}")
        
        if non_exposed_mask.any():
            non_exposed = verified_events[non_exposed_mask].copy()

            # Get pending and transferred amounts from AccountMovementDaily
            print(f"    Movement data columns: {list(movement_df.columns)[:10]}...")

            if 'Pending' in movement_df.columns:
                # Convert Pending to numeric
                movement_df['Pending'] = pd.to_numeric(
                    movement_df['Pending'], errors='coerce'
                ).fillna(0)

                # Get the latest pending amount per account (this is a snapshot, not cumulative)
                latest_pending = movement_df.groupby('AccountId')['Pending'].last()

                non_exposed = non_exposed.merge(
                    latest_pending,
                    on='AccountId',
                    how='left'
                )
                non_exposed['Pending'] = non_exposed['Pending'].fillna(0)

                # The net_amount field already has the amount from THIS completed event
                # (calculated as PaymentReceived - TotalFees in Phase 2)
                # Logic: Check if Pending >= net_amount from this event
                # If yes, they've already requested a payout that includes these funds
                # If no, they still need to request a payout for these funds
                non_exposed['vero_event'] = np.where(
                    non_exposed['Pending'] >= non_exposed['net_amount'],
                    'event_completed_paid_requested',  # Pending includes funds from this event
                    'event_completed_paid_notrequested'  # They need to request these event funds
                )

                # Only update events in event_metrics that don't already have a classification
                # and haven't been marked to skip (safety check)
                valid_indices = non_exposed.index[
                    event_metrics.loc[non_exposed.index, 'vero_event'].isna() &
                    (~event_metrics.loc[non_exposed.index, 'skip_further_processing'])
                ]

                if len(valid_indices) < len(non_exposed):
                    print(f"    WARNING: Filtered out {len(non_exposed) - len(valid_indices)} events that already had classifications")

                event_metrics.loc[valid_indices, 'vero_event'] = non_exposed.loc[valid_indices, 'vero_event']

                print(f"    Funds requested (Pending >= event net_amount): {(non_exposed.loc[valid_indices, 'vero_event'] == 'event_completed_paid_requested').sum()}")
                print(f"    Funds not requested (Pending < event net_amount): {(non_exposed.loc[valid_indices, 'vero_event'] == 'event_completed_paid_notrequested').sum()}")
                print(f"    Successfully classified {len(valid_indices)} verified events")
            else:
                print(f"    WARNING: 'Pending' column not found in AccountMovementDaily")
                print(f"    Defaulting all non-exposed verified events to 'not requested'")
                # Default to not requested if Pending column is missing
                non_exposed['vero_event'] = 'event_completed_paid_notrequested'

                # Only update events in event_metrics that don't already have a classification
                # and haven't been marked to skip (safety check)
                valid_indices = non_exposed.index[
                    event_metrics.loc[non_exposed.index, 'vero_event'].isna() &
                    (~event_metrics.loc[non_exposed.index, 'skip_further_processing'])
                ]
                event_metrics.loc[valid_indices, 'vero_event'] = non_exposed.loc[valid_indices, 'vero_event']
                print(f"    Events defaulted to not requested: {len(non_exposed)}")
    
    # Create audit trail CSV with all events processed
    print("\nCreating audit trail CSV...")
    audit_df = event_metrics.copy()
    
    # Add additional columns for audit
    audit_df['checked_free_first_event'] = (
        (audit_df['event_type'] == 'free') & 
        (audit_df['is_first_event'] == True) & 
        (audit_df['TicketQuantity'] > 10)
    )
    audit_df['checked_stripe_first_event'] = (
        (audit_df['event_type'] == 'paid') & 
        (audit_df['GatewayGroup'] == 'Stripe Connect') &
        (audit_df['is_first_event'] == True) & 
        (audit_df['TicketQuantity'] > 10)
    )
    audit_df['checked_not_verified'] = (
        (audit_df['event_type'] == 'paid') & 
        (audit_df['GatewayGroup'] == 'Default (All)') &
        (audit_df['IsVerified'] != 1)
    )
    audit_df['checked_verified'] = (
        (audit_df['event_type'] == 'paid') & 
        (audit_df['GatewayGroup'] == 'Default (All)') &
        (audit_df['IsVerified'] == 1)
    )
    
    # Add exposure and pending info for verified events
    if len(verified_events) > 0 and 'is_exposed' in verified_events.columns:
        # Merge verified event info including Risk Report data
        merge_columns = ['EventId', 'AccountBalance', 'net_future', 'is_exposed']
        # Add Risk Report columns if available
        risk_columns = ['FullBalance', 'SalesForUpcomingEvents', 'Exposure']
        for col in risk_columns:
            if col in verified_events.columns:
                merge_columns.append(col)

        audit_df = audit_df.merge(
            verified_events[merge_columns],
            on='EventId',
            how='left'
        )

        # If we processed non-exposed events, add Pending info
        if 'non_exposed' in locals() and 'Pending' in non_exposed.columns:
            audit_df = audit_df.merge(
                non_exposed[['EventId', 'Pending']],
                on='EventId',
                how='left'
            )
    
    # Save audit trail
    audit_filename = f"event_completion_audit_{target_date.strftime('%Y%m%d')}.csv"
    audit_df.to_csv(audit_filename, index=False)
    print(f"  Audit trail saved to: {audit_filename}")
    
    # Phase 4: Account-Level Aggregation
    print("\nPhase 4: Aggregating events per account...")
    print("-" * 30)

    # Filter to events that need sending
    events_to_send = event_metrics[event_metrics['vero_event'].notna()].copy()

    # AGGREGATION: For accounts with multiple events completing on same day
    # Aggregate metrics and choose highest priority vero_event type
    priority_map = {
        'event_completed_paid_notrequested': 1,  # Highest priority - needs action
        'event_completed_paid_notverified': 2,
        'event_completed_paid_requested': 3,
        'event_completed_paid_stripe': 4,
        'event_completed_free': 5                # Lowest priority
    }

    events_to_send['priority'] = events_to_send['vero_event'].map(priority_map)

    # Count events per account to identify multiple events
    events_per_account = events_to_send.groupby('AccountId').size()
    accounts_with_multiple = set(events_per_account[events_per_account > 1].index)

    # Aggregate by account
    aggregated = events_to_send.groupby('AccountId').agg({
        'EventId': 'first',  # Use first event ID as representative
        'EventName': lambda x: ', '.join(x[:3]) if len(x) <= 3 else f'{x.iloc[0]} and {len(x)-1} others',  # Combine event names
        'event_type': 'first',  # Use first event type
        'vero_event': lambda x: x.loc[x.map(priority_map).idxmin()],  # Highest priority vero_event
        'PaymentReceived': 'sum',  # Sum financial data
        'net_amount': 'sum',
        'TicketQuantity': 'sum',
        'priority': 'min'  # Keep highest priority (lowest number)
    }).reset_index()

    # Mark accounts that had multiple events
    aggregated['has_multiple_events'] = aggregated['AccountId'].isin(accounts_with_multiple)
    aggregated['event_count'] = aggregated['AccountId'].map(events_per_account)

    events_to_send = aggregated

    # Log aggregation
    original_count = len(event_metrics[event_metrics['vero_event'].notna()])
    aggregated_count = len(events_to_send)
    if original_count > aggregated_count:
        print(f"  Aggregation: Combined {original_count} events into {aggregated_count} notifications")
        print(f"  Accounts with multiple events: {len(accounts_with_multiple)}")
    else:
        print(f"  No aggregation needed: {aggregated_count} events")
    
    # Phase 5: User Resolution and Event Emission
    print("\nPhase 5: Resolving users and sending events...")
    print("-" * 30)
    
    # Prepare users
    users_df['UserId'] = users_df['UserId'].astype(str)
    users_df['vero_id'] = 'uk_' + users_df['UserId']
    users_df['AccountId'] = pd.to_numeric(users_df['AccountId'], errors='coerce')

    # Remove invalid users
    # Filter out:
    # - Deleted users (IsDeleted == '1')
    # - Closed users (email ends with -YYYYMMDD-HHMMSS format)
    # - Users with invalid AccountId or RoleName
    valid_users = users_df[
        users_df['AccountId'].notna() &
        users_df['RoleName'].notna() &
        (users_df['IsDeleted'] != '1') &
        (~users_df['Username'].str.match(r'.*-\d{8}-\d{6}$', na=False))
    ]

    print(f"  Total users: {len(users_df)}")
    print(f"  Valid users after filtering: {len(valid_users)}")
    print(f"  Filtered out {len(users_df) - len(valid_users)} invalid/closed users")
    
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
                'ticket_quantity': int(event['TicketQuantity']),
                'has_multiple_events': bool(event.get('has_multiple_events', False))
            })
    
    print(f"  Total Vero events to send: {len(vero_events)}")
    
    if len(vero_events) == 0:
        print("  No events to send.")
        # Still create an empty CSV for audit purposes
        vero_df = pd.DataFrame(columns=[
            'event_id', 'event_name', 'account_id', 'event_type', 'vero_event',
            'vero_user_id', 'user_email', 'payment_received', 'net_amount',
            'ticket_quantity', 'has_multiple_events', 'status', 'error_message', 'timestamp'
        ])
        output_filename = f"event_completion_reminders_{target_date.strftime('%Y%m%d')}.csv"
        vero_df.to_csv(output_filename, index=False)
        print(f"  Empty CSV saved to: {output_filename}")
        sys.exit(0)
    
    # Convert to DataFrame
    vero_df = pd.DataFrame(vero_events)
    
    # Initialize results tracking
    vero_df['status'] = 'Pending'
    vero_df['error_message'] = None
    vero_df['timestamp'] = datetime.now(UK_TZ).replace(tzinfo=None)  # Store as naive datetime for CSV
    
    # Send to Vero (includes testmode flag in extras for filtering)
    print("\n  Sending events to Vero...")
    if TEST_MODE:
        print("  (Running in TEST MODE - events will have testmode=true flag)")

    vero_client = VeroClient(VERO_API_KEY)

    # Process in batches
    batch_size = 100
    for i in range(0, len(vero_df), batch_size):
        batch = vero_df.iloc[i:i+batch_size]
        print(f"    Processing batch {i//batch_size + 1} ({len(batch)} events)...")

        try:
            results = vero_client.batch_track_events(batch)

            # Update status based on results
            for j, result in enumerate(results):
                idx = batch.index[j]
                if result.get('status') == 'success':
                    vero_df.loc[idx, 'status'] = 'Success'
                else:
                    vero_df.loc[idx, 'status'] = 'Failed'
                    vero_df.loc[idx, 'error_message'] = result.get('error', 'Unknown error')

        except Exception as e:
            # Mark entire batch as failed
            vero_df.loc[batch.index, 'status'] = 'Failed'
            vero_df.loc[batch.index, 'error_message'] = str(e)
            print(f"    Error processing batch: {e}")
    
    # Save output
    output_filename = f"event_completion_reminders_{target_date.strftime('%Y%m%d')}.csv"
    vero_df.to_csv(output_filename, index=False)
    print(f"\n  Output saved to: {output_filename}")
    
    # Print summary
    print(f"\nEvent Completion Reminders Summary - {target_date.strftime('%Y-%m-%d')}")
    print("=" * 50)
    print(f"Events with completed sessions: {len(event_metrics)}")
    print(f"Events requiring notification: {original_count}")
    print(f"After aggregation: {aggregated_count}")
    print(f"Vero events created: {len(vero_df)}")
    
    if not TEST_MODE:
        print(f"\nProcessing results:")
        print(f"  Success: {(vero_df['status'] == 'Success').sum()}")
        print(f"  Failed: {(vero_df['status'] == 'Failed').sum()}")
        
        # Show any errors
        failed_events = vero_df[vero_df['status'] == 'Failed']
        if len(failed_events) > 0:
            print("\nFailed events:")
            for _, failed in failed_events.iterrows():
                print(f"  - User {failed['vero_user_id']}: {failed['error_message']}")
    
    print("\nScript completed successfully!")


if __name__ == "__main__":
    main()