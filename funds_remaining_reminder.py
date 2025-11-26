#!/usr/bin/env python3
"""
Funds Remaining Reminder Script
Tags users in Vero for accounts with remaining funds from past events.

Tags applied:
- 2025-fundsremaining-verified: Account has verified bank details
- 2025-fundsremaining-notverified: Account has not verified bank details
"""
import os
import sys
import pandas as pd
from datetime import datetime
from modules.utils.data_loader import (
    load_accounts_data, load_users_data, load_risk_report_data
)
from modules.utils.date_utils import get_latest_data_date, UK_TZ
from modules.utils.vero_api import VeroClient
from modules.utils.config import TEST_MODE

# Environment variables
VERO_API_KEY = os.environ.get('VERO_API_KEY')

# Minimum past funds threshold (£10)
MIN_PAST_FUNDS = 10.0

# Tag names
TAG_VERIFIED = '2025-fundsremaining-verified'
TAG_NOT_VERIFIED = '2025-fundsremaining-notverified'


def main():
    """Main function to process funds remaining reminders."""
    print("=" * 50)
    print("Funds Remaining Reminder Script")
    print("=" * 50)

    if not VERO_API_KEY and not TEST_MODE:
        print("Error: VERO_API_KEY environment variable not set")
        sys.exit(1)

    target_date = get_latest_data_date()
    print(f"Processing date: {target_date.strftime('%Y-%m-%d')}")
    print(f"Test mode: {'ON' if TEST_MODE else 'OFF'}")
    print("")

    # Phase 1: Data Loading
    print("Phase 1: Loading data...")
    print("-" * 30)

    try:
        risk_df = load_risk_report_data(None, target_date)
        print(f"  Risk report loaded: {len(risk_df):,}")

        accounts_df = load_accounts_data(None, target_date)
        print(f"  Accounts loaded: {len(accounts_df):,}")

        users_df = load_users_data(None, target_date)
        print(f"  Users loaded: {len(users_df):,}")

    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)

    # Phase 2: Calculate Past Funds
    print("\nPhase 2: Calculating past funds...")
    print("-" * 30)

    # Ensure numeric columns
    risk_df['FullBalance'] = pd.to_numeric(risk_df['FullBalance'], errors='coerce').fillna(0)
    risk_df['SalesForUpcomingEvents'] = pd.to_numeric(
        risk_df['SalesForUpcomingEvents'], errors='coerce'
    ).fillna(0)

    # Calculate past funds: FullBalance - SalesForUpcomingEvents
    risk_df['past_funds'] = risk_df['FullBalance'] - risk_df['SalesForUpcomingEvents']

    print(f"  Total accounts in risk report: {len(risk_df):,}")
    print(f"  Accounts with positive past funds: {(risk_df['past_funds'] > 0).sum():,}")

    # Phase 3: Filter and Classify
    print("\nPhase 3: Filtering and classifying accounts...")
    print("-" * 30)

    # Merge with accounts to get GatewayGroup and IsVerified
    merged_df = risk_df.merge(
        accounts_df[['AccountId', 'GatewayGroup', 'IsVerified']],
        on='AccountId',
        how='left'
    )

    # Filter: past_funds >= £10 and not Stripe Connect
    filtered_df = merged_df[
        (merged_df['past_funds'] >= MIN_PAST_FUNDS) &
        (merged_df['GatewayGroup'] != 'Stripe Connect')
    ].copy()

    print(f"  Accounts with >= £{MIN_PAST_FUNDS:.0f} past funds: {len(filtered_df):,}")
    print(f"  (Stripe Connect accounts excluded)")

    if len(filtered_df) == 0:
        print("\n  No accounts meet the criteria. Exiting.")
        sys.exit(0)

    # Classify by verification status
    filtered_df['vero_tag'] = filtered_df['IsVerified'].apply(
        lambda x: TAG_VERIFIED if x == 1 else TAG_NOT_VERIFIED
    )

    verified_count = (filtered_df['vero_tag'] == TAG_VERIFIED).sum()
    not_verified_count = (filtered_df['vero_tag'] == TAG_NOT_VERIFIED).sum()

    print(f"  Verified accounts: {verified_count:,}")
    print(f"  Not verified accounts: {not_verified_count:,}")

    # Summary stats
    print(f"\n  Total past funds (verified): £{filtered_df[filtered_df['vero_tag'] == TAG_VERIFIED]['past_funds'].sum():,.2f}")
    print(f"  Total past funds (not verified): £{filtered_df[filtered_df['vero_tag'] == TAG_NOT_VERIFIED]['past_funds'].sum():,.2f}")

    # Phase 4: Resolve Users
    print("\nPhase 4: Resolving users...")
    print("-" * 30)

    # Prepare users
    users_df['UserId'] = users_df['UserId'].astype(str)
    users_df['vero_id'] = 'uk_' + users_df['UserId']
    users_df['AccountId'] = pd.to_numeric(users_df['AccountId'], errors='coerce')

    # Filter valid users (same logic as event_completion_reminders.py)
    valid_users = users_df[
        users_df['AccountId'].notna() &
        users_df['RoleName'].notna() &
        (users_df['IsDeleted'] != '1') &
        (~users_df['Username'].str.match(r'.*-\d{8}-\d{6}$', na=False))
    ]

    print(f"  Total users: {len(users_df):,}")
    print(f"  Valid users after filtering: {len(valid_users):,}")

    # Build user list for tagging (AccountOwner + Finance)
    required_roles = ['AccountOwner', 'Finance']
    vero_users = []

    for _, account in filtered_df.iterrows():
        relevant_users = valid_users[
            (valid_users['AccountId'] == account['AccountId']) &
            (valid_users['RoleName'].isin(required_roles))
        ]

        for _, user in relevant_users.iterrows():
            vero_users.append({
                'account_id': int(account['AccountId']),
                'account_name': account.get('AccountName', ''),
                'past_funds': float(account['past_funds']),
                'full_balance': float(account['FullBalance']),
                'sales_for_upcoming': float(account['SalesForUpcomingEvents']),
                'is_verified': account['IsVerified'] == 1,
                'vero_tag': account['vero_tag'],
                'vero_user_id': user['vero_id'],
                'user_email': user['Username'],
                'user_role': user['RoleName']
            })

    print(f"  Users to tag: {len(vero_users):,}")

    if len(vero_users) == 0:
        print("\n  No users found for accounts. Exiting.")
        sys.exit(0)

    # Convert to DataFrame
    vero_df = pd.DataFrame(vero_users)

    # Phase 5: Apply Vero Tags
    print("\nPhase 5: Applying Vero tags...")
    print("-" * 30)

    vero_df['status'] = 'Pending'
    vero_df['error_message'] = None
    vero_df['timestamp'] = datetime.now(UK_TZ).replace(tzinfo=None)

    if TEST_MODE:
        print("  (Running in TEST MODE - tags will not be applied)")

    vero_client = VeroClient(VERO_API_KEY)

    # Process in batches
    batch_size = 100
    for i in range(0, len(vero_df), batch_size):
        batch = vero_df.iloc[i:i + batch_size]
        print(f"  Processing batch {i // batch_size + 1} ({len(batch)} users)...")

        try:
            results = vero_client.batch_add_tags(batch)

            for j, result in enumerate(results):
                idx = batch.index[j]
                if result.get('status') == 'success':
                    vero_df.loc[idx, 'status'] = 'Success'
                else:
                    vero_df.loc[idx, 'status'] = 'Failed'
                    vero_df.loc[idx, 'error_message'] = result.get('error', 'Unknown error')

        except Exception as e:
            vero_df.loc[batch.index, 'status'] = 'Failed'
            vero_df.loc[batch.index, 'error_message'] = str(e)
            print(f"  Error processing batch: {e}")

    # Save output
    output_filename = f"funds_remaining_reminder_{target_date.strftime('%Y%m%d')}.csv"
    vero_df.to_csv(output_filename, index=False)
    print(f"\n  Output saved to: {output_filename}")

    # Print summary
    print(f"\nFunds Remaining Reminder Summary - {target_date.strftime('%Y-%m-%d')}")
    print("=" * 50)
    print(f"Accounts processed: {len(filtered_df):,}")
    print(f"  - Verified: {verified_count:,}")
    print(f"  - Not verified: {not_verified_count:,}")
    print(f"Users tagged: {len(vero_df):,}")

    if not TEST_MODE:
        print(f"\nProcessing results:")
        print(f"  Success: {(vero_df['status'] == 'Success').sum()}")
        print(f"  Failed: {(vero_df['status'] == 'Failed').sum()}")

        failed = vero_df[vero_df['status'] == 'Failed']
        if len(failed) > 0:
            print("\nFailed users:")
            for _, row in failed.head(10).iterrows():
                print(f"  - {row['vero_user_id']}: {row['error_message']}")
            if len(failed) > 10:
                print(f"  ... and {len(failed) - 10} more")

    print("\nScript completed successfully!")


if __name__ == "__main__":
    main()
