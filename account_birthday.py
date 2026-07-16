#!/usr/bin/env python3
"""
Account Birthday Script
Sends a lifetime-stats "account birthday" reminder via GetVero to accounts
whose sign-up anniversary falls on the target date.
"""
import os
import sys
import pandas as pd
from datetime import datetime
from modules.utils.data_loader import (
    load_accounts_data, load_combined_booking_data, load_users_data,
    filter_successful_transactions
)
from modules.utils.date_utils import get_latest_data_date, UK_TZ
from modules.utils.vero_api import VeroClient
from modules.utils.config import TEST_MODE

# Environment variables
VERO_API_KEY = os.environ.get('VERO_API_KEY')
TEST_DATE = os.environ.get('TEST_DATE')

# Vero event name (tag used to trigger the Account_Birthday campaign)
VERO_EVENT = 'Account_Birthday'

# Recipient role - account birthdays are a relationship touchpoint, not a
# funds/finance action, so only the account owner is notified.
REQUIRED_ROLES = ['AccountOwner']


def main():
    """Main function to process account birthday notifications."""
    print("=" * 50)
    print("Account Birthday Script")
    print("=" * 50)

    if not VERO_API_KEY and not TEST_MODE:
        print("Error: VERO_API_KEY environment variable not set")
        sys.exit(1)

    # Get target date (defaults to yesterday, matching the other Vero scripts)
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

    # Phase 1: Data Loading
    print("Phase 1: Loading data...")
    print("-" * 30)

    try:
        accounts_df = load_accounts_data(None, target_date)
        print(f"  Accounts loaded: {len(accounts_df):,}")

        # Lifetime dataset: de-duped union of BookingDataAll + current month
        booking_df = load_combined_booking_data(target_date)
        print(f"  Lifetime bookings loaded: {len(booking_df):,}")

        users_df = load_users_data(None, target_date)
        print(f"  Users loaded: {len(users_df):,}")

    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)

    # Phase 2: Find accounts with an anniversary on the target date
    print("\nPhase 2: Finding today's account anniversaries...")
    print("-" * 30)

    if 'DateTimeCreated' not in accounts_df.columns:
        print("Error: DateTimeCreated column not found in Accounts data")
        sys.exit(1)

    accounts_df['AccountId'] = pd.to_numeric(accounts_df['AccountId'], errors='coerce')
    accounts_df['DateTimeCreated'] = pd.to_datetime(
        accounts_df['DateTimeCreated'], errors='coerce', utc=True
    )
    accounts_df = accounts_df[
        accounts_df['AccountId'].notna() & accounts_df['DateTimeCreated'].notna()
    ].copy()

    # Anniversary = same month/day as target date, at least one full year old
    # (accounts created this calendar year haven't had a first anniversary yet)
    anniversary_mask = (
        (accounts_df['DateTimeCreated'].dt.month == target_date.month) &
        (accounts_df['DateTimeCreated'].dt.day == target_date.day) &
        (accounts_df['DateTimeCreated'].dt.year < target_date.year)
    )
    birthday_accounts = accounts_df[anniversary_mask].copy()
    birthday_accounts['years_active'] = (
        target_date.year - birthday_accounts['DateTimeCreated'].dt.year
    )

    print(f"  Accounts created on {target_date.strftime('%d %b')}: {len(birthday_accounts):,}")

    if len(birthday_accounts) == 0:
        print("\n  No account anniversaries today. Exiting.")
        sys.exit(0)

    # Phase 3: Calculate lifetime stats
    print("\nPhase 3: Calculating lifetime stats...")
    print("-" * 30)

    successful_bookings = filter_successful_transactions(booking_df)
    successful_bookings['AccountId'] = pd.to_numeric(
        successful_bookings['AccountId'], errors='coerce'
    )
    successful_bookings['TicketQuantity'] = pd.to_numeric(
        successful_bookings['TicketQuantity'], errors='coerce'
    ).fillna(0)

    # Total tickets sold: lifetime sum per account
    tickets_by_account = successful_bookings.groupby('AccountId')['TicketQuantity'].sum()

    # Total events run: distinct events whose last session has already completed
    event_last_session = successful_bookings.groupby('EventId').agg(
        AccountId=('AccountId', 'first'),
        last_session=('EventDate', 'max')
    )
    event_last_session = event_last_session[event_last_session['last_session'].notna()]
    completed_events = event_last_session[
        event_last_session['last_session'].dt.date < target_date.date()
    ]
    events_by_account = completed_events.groupby('AccountId').size()

    birthday_accounts['total_tickets_sold'] = (
        birthday_accounts['AccountId'].map(tickets_by_account).fillna(0).astype(int)
    )
    birthday_accounts['total_events_run'] = (
        birthday_accounts['AccountId'].map(events_by_account).fillna(0).astype(int)
    )

    print(f"  Total tickets sold (all anniversary accounts): "
          f"{birthday_accounts['total_tickets_sold'].sum():,}")
    print(f"  Total events run (all anniversary accounts): "
          f"{birthday_accounts['total_events_run'].sum():,}")

    # Phase 4: Resolve users
    print("\nPhase 4: Resolving users...")
    print("-" * 30)

    users_df['UserId'] = users_df['UserId'].astype(str)
    users_df['vero_id'] = 'uk_' + users_df['UserId']
    users_df['AccountId'] = pd.to_numeric(users_df['AccountId'], errors='coerce')

    # Same "valid user" filtering as the other Vero scripts
    valid_users = users_df[
        users_df['AccountId'].notna() &
        users_df['RoleName'].notna() &
        (users_df['IsDeleted'] != '1') &
        (~users_df['Username'].str.match(r'.*-\d{8}-\d{6}$', na=False))
    ]

    print(f"  Total users: {len(users_df):,}")
    print(f"  Valid users after filtering: {len(valid_users):,}")

    vero_events = []
    for _, account in birthday_accounts.iterrows():
        relevant_users = valid_users[
            (valid_users['AccountId'] == account['AccountId']) &
            (valid_users['RoleName'].isin(REQUIRED_ROLES))
        ]

        for _, user in relevant_users.iterrows():
            vero_events.append({
                'account_id': int(account['AccountId']),
                'account_name': account.get('AccountName', ''),
                'years_active': int(account['years_active']),
                'total_events_run': int(account['total_events_run']),
                'total_tickets_sold': int(account['total_tickets_sold']),
                'vero_user_id': user['vero_id'],
                'user_email': user['Username'],
            })

    print(f"  Users to notify: {len(vero_events):,}")

    if len(vero_events) == 0:
        print("\n  No users found for anniversary accounts. Exiting.")
        sys.exit(0)

    # Phase 5: Send Account_Birthday events to Vero
    print("\nPhase 5: Sending Account_Birthday events to Vero...")
    print("-" * 30)

    vero_df = pd.DataFrame(vero_events)
    vero_df['status'] = 'Pending'
    vero_df['error_message'] = None
    vero_df['timestamp'] = datetime.now(UK_TZ).replace(tzinfo=None)

    if TEST_MODE:
        print("  (Running in TEST MODE - events will have testmode=true flag)")

    vero_client = VeroClient(VERO_API_KEY)

    for idx, row in vero_df.iterrows():
        try:
            if TEST_MODE:
                # Mirror VeroClient's own TEST_MODE short-circuit (see batch_add_tags) -
                # no live call, just record what would have been sent.
                pass
            else:
                vero_client.track_event(
                    user_id=row['vero_user_id'],
                    email=row['user_email'],
                    event_name=VERO_EVENT,
                    data={
                        'account_id': row['account_id'],
                        'total_events_run': row['total_events_run'],
                        'total_tickets_sold': row['total_tickets_sold'],
                        'years_active': row['years_active'],
                    },
                    extras={
                        'source': 'TryBooking Account Birthday Script',
                        'testmode': TEST_MODE
                    }
                )
            vero_df.loc[idx, 'status'] = 'Success'

        except Exception as e:
            vero_df.loc[idx, 'status'] = 'Failed'
            vero_df.loc[idx, 'error_message'] = str(e)
            print(f"  Error sending event for {row['vero_user_id']}: {e}")

    # Save output
    output_filename = f"account_birthday_{target_date.strftime('%Y%m%d')}.csv"
    vero_df.to_csv(output_filename, index=False)
    print(f"\n  Output saved to: {output_filename}")

    # Print summary
    print(f"\nAccount Birthday Summary - {target_date.strftime('%Y-%m-%d')}")
    print("=" * 50)
    print(f"Accounts with anniversary today: {len(birthday_accounts):,}")
    print(f"Events sent: {len(vero_df):,}")

    if not TEST_MODE:
        print(f"\nProcessing results:")
        print(f"  Success: {(vero_df['status'] == 'Success').sum()}")
        print(f"  Failed: {(vero_df['status'] == 'Failed').sum()}")

        failed = vero_df[vero_df['status'] == 'Failed']
        if len(failed) > 0:
            print("\nFailed events:")
            for _, row in failed.head(10).iterrows():
                print(f"  - {row['vero_user_id']}: {row['error_message']}")
            if len(failed) > 10:
                print(f"  ... and {len(failed) - 10} more")

    print("\nScript completed successfully!")


if __name__ == "__main__":
    main()
