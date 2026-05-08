#!/usr/bin/env python3
"""
Pricing Model Comparison Script

Compares current fee structure with proposed pricing model for 2025 data.

Current Structure (inc VAT):
- Processing fee: 5% inc VAT
- Ticket fee: 15p inc VAT
- Stripe accounts: 75p inc VAT ticket fee (no processing fee)
- Box Office: Same as online

Proposed Structure (ex VAT):
- Processing fee: 4% ex VAT
- Ticket fee: 20p ex VAT
- Stripe accounts: 75p ex VAT ticket fee (no processing fee)
- Box Office Cash: Free
- Box Office Card: 3% processing + 20p ex VAT (no ticket fee beyond the 20p)

Output: CSV comparing actual vs proposed revenue by month and category.

Usage:
    python pricing_model_comparison.py

Environment Variables:
    ANALYSIS_YEAR: Year to analyse (default: 2025)
"""

import pandas as pd
import numpy as np
from datetime import datetime
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.utils.data_loader import load_booking_data, filter_successful_transactions


def classify_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify transactions.

    Adds columns:
    - Sales_Channel: 'Box Office' or 'Online'
    - BO_Type: 'Cash', 'Card', or None
    - Gateway_Normalised: 'Stripe Connect', 'Stripe', 'PayPal', 'Default', or 'Unknown'
    """
    # Normalise PaymentType to uppercase
    payment_upper = df['PaymentType'].fillna('').str.upper().str.strip()

    # Sales Channel classification
    is_box_office = payment_upper.str.contains('CARD PRESENT', na=False) | (payment_upper == 'CASH')
    df['Sales_Channel'] = np.where(is_box_office, 'Box Office', 'Online')

    # Box Office Type classification
    df['BO_Type'] = np.where(
        payment_upper == 'CASH', 'Cash',
        np.where(payment_upper.str.contains('CARD PRESENT', na=False), 'Card', None)
    )

    # Gateway normalisation - use Gateway Group (account-level), not GatewayName (transaction-level)
    gateway_col = None
    for col in ['Gateway Group', 'GatewayGroup']:
        if col in df.columns:
            gateway_col = col
            break

    if gateway_col:
        gateway_lower = df[gateway_col].fillna('').str.lower().str.strip()
        df['Gateway_Normalised'] = np.select(
            [
                gateway_lower.str.contains('stripe connect', na=False),
                gateway_lower.str.contains('paypal', na=False),
                gateway_lower.str.contains('default', na=False),  # Default (All) = TryBooking gateway
            ],
            ['Stripe Connect', 'PayPal', 'Default'],
            default='Unknown'
        )
    else:
        df['Gateway_Normalised'] = 'Default'

    return df


def calculate_fees_vectorised(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate current and proposed fees.

    Current Structure (inc VAT) - converted to ex VAT:
    - All fee columns summed and divided by 1.2

    Proposed Structure (ex VAT):
    - Online (Default/PayPal): 4% processing + 20p per ticket
    - Online (Stripe): 75p per ticket only
    - Box Office Cash: Free
    - Box Office Card: 3% processing + 20p per ticket
    """
    # Ensure numeric columns, fill NaN with 0
    payment_received = df['PaymentReceived'].fillna(0)
    ticket_quantity = df['TicketQuantity'].fillna(0)
    booking_fee = df['BookingFee'].fillna(0) if 'BookingFee' in df.columns else 0
    card_fee = df['CardFee'].fillna(0) if 'CardFee' in df.columns else 0
    processing_fee = df['ProcessingFee'].fillna(0) if 'ProcessingFee' in df.columns else 0
    ticket_fee = df['TicketFee'].fillna(0) if 'TicketFee' in df.columns else 0

    # Current fees (convert inc VAT to ex VAT)
    df['current_fee_inc_vat'] = booking_fee + card_fee + processing_fee + ticket_fee
    df['current_fee_ex_vat'] = df['current_fee_inc_vat'] / 1.2

    # Create condition masks (free tickets already filtered out)
    is_bo_cash = (df['Sales_Channel'] == 'Box Office') & (df['BO_Type'] == 'Cash')
    is_bo_card = (df['Sales_Channel'] == 'Box Office') & (df['BO_Type'] == 'Card')
    is_stripe = df['Gateway_Normalised'] == 'Stripe Connect'
    is_online_default = (df['Sales_Channel'] == 'Online') & ~is_stripe

    # Calculate proposed processing fee
    df['proposed_processing_fee'] = np.select(
        [is_bo_cash, is_bo_card, is_stripe, is_online_default],
        [0, payment_received * 0.03, 0, payment_received * 0.04],
        default=0
    )

    # Calculate proposed ticket fee
    df['proposed_ticket_fee'] = np.select(
        [is_bo_cash, is_bo_card, is_stripe, is_online_default],
        [0, ticket_quantity * 0.20, ticket_quantity * 0.75, ticket_quantity * 0.20],
        default=0
    )

    # Total proposed fee
    df['proposed_total_fee'] = df['proposed_processing_fee'] + df['proposed_ticket_fee']

    # Fee difference
    df['fee_difference'] = df['proposed_total_fee'] - df['current_fee_ex_vat']

    return df


def main():
    """Run the pricing model comparison."""
    print("=" * 60)
    print("PRICING MODEL COMPARISON")
    print("=" * 60)
    print()

    # Get year from environment variable or default to 2025
    analysis_year = int(os.environ.get('ANALYSIS_YEAR', '2025'))
    print(f"Analysis Year: {analysis_year}")
    print()

    # Load booking data (all historical + current month)
    print("Loading booking data...")
    booking_all_df = load_booking_data(data_type='BookingDataAll')
    booking_current_df = load_booking_data(data_type='BookingData')

    if booking_all_df is None and booking_current_df is None:
        print("ERROR: No booking data loaded")
        return

    # Combine and deduplicate
    dfs_to_concat = []
    if booking_all_df is not None and len(booking_all_df) > 0:
        dfs_to_concat.append(booking_all_df)
    if booking_current_df is not None and len(booking_current_df) > 0:
        dfs_to_concat.append(booking_current_df)

    if not dfs_to_concat:
        print("ERROR: No booking data loaded")
        return

    booking_df = pd.concat(dfs_to_concat, ignore_index=True)

    # Deduplicate by BookingTransactionId
    if 'BookingTransactionId' in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=['BookingTransactionId'])

    print(f"  Loaded {len(booking_df):,} total transactions")

    # Filter to successful transactions only
    booking_df = filter_successful_transactions(booking_df)

    # Filter to the analysis year
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df = booking_df[booking_df['TransactionDate'].dt.year == analysis_year]

    if len(booking_df) == 0:
        print(f"ERROR: No transactions found for {analysis_year}")
        return

    print(f"  Filtered to {len(booking_df):,} transactions for {analysis_year}")

    # Ensure required columns exist
    required_cols = ['PaymentReceived', 'TicketQuantity', 'TransactionDate']
    for col in required_cols:
        if col not in booking_df.columns:
            print(f"ERROR: Missing required column: {col}")
            return

    # Filter out free tickets (PaymentReceived = 0) - they don't generate fees in either model
    free_count = (booking_df['PaymentReceived'].fillna(0) == 0).sum()
    booking_df = booking_df[booking_df['PaymentReceived'].fillna(0) > 0]
    print(f"  Excluded {free_count:,} free tickets, {len(booking_df):,} paid transactions remaining")

    # Classify transactions
    print("Classifying transactions...")
    booking_df = classify_transactions(booking_df)

    # Add month column (TransactionDate already converted to datetime above)
    booking_df['Month'] = booking_df['TransactionDate'].dt.to_period('M')

    # Calculate fees
    print("Calculating fees...")
    booking_df = calculate_fees_vectorised(booking_df)

    # === SUMMARY BY CATEGORY ===
    print("\n" + "=" * 60)
    print(f"SUMMARY BY CATEGORY ({analysis_year} Full Year)")
    print("=" * 60)

    # Create category column
    booking_df['Category'] = np.where(
        booking_df['Sales_Channel'] == 'Box Office',
        'Box Office - ' + booking_df['BO_Type'].fillna('Unknown'),
        'Online - ' + booking_df['Gateway_Normalised']
    )

    # Aggregate by category
    category_summary = booking_df.groupby('Category').agg({
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'current_fee_inc_vat': 'sum',
        'current_fee_ex_vat': 'sum',
        'proposed_total_fee': 'sum',
        'fee_difference': 'sum'
    }).round(2)

    category_summary['pct_change'] = (
        (category_summary['proposed_total_fee'] - category_summary['current_fee_ex_vat'])
        / category_summary['current_fee_ex_vat'] * 100
    ).round(1)

    print("\n" + category_summary.to_string())

    # === TOTALS ===
    print("\n" + "=" * 60)
    print("TOTALS")
    print("=" * 60)

    total_tickets = booking_df['TicketQuantity'].sum()
    total_revenue = booking_df['PaymentReceived'].sum()
    total_current_inc = booking_df['current_fee_inc_vat'].sum()
    total_current_ex = booking_df['current_fee_ex_vat'].sum()
    total_proposed = booking_df['proposed_total_fee'].sum()
    total_diff = total_proposed - total_current_ex
    pct_change = (total_diff / total_current_ex * 100) if total_current_ex > 0 else 0

    print(f"\nTotal Tickets:              {total_tickets:>15,}")
    print(f"Total Revenue:              £{total_revenue:>14,.2f}")
    print(f"\nCurrent Fees (inc VAT):     £{total_current_inc:>14,.2f}")
    print(f"Current Fees (ex VAT):      £{total_current_ex:>14,.2f}")
    print(f"Proposed Fees (ex VAT):     £{total_proposed:>14,.2f}")
    print(f"\nDifference (ex VAT):        £{total_diff:>14,.2f}")
    print(f"Percentage Change:          {pct_change:>14.1f}%")

    # === MONTHLY BREAKDOWN ===
    print("\n" + "=" * 60)
    print("MONTHLY BREAKDOWN")
    print("=" * 60)

    monthly_summary = booking_df.groupby('Month').agg({
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'current_fee_inc_vat': 'sum',
        'current_fee_ex_vat': 'sum',
        'proposed_total_fee': 'sum',
        'fee_difference': 'sum'
    }).round(2)

    monthly_summary['pct_change'] = (
        (monthly_summary['proposed_total_fee'] - monthly_summary['current_fee_ex_vat'])
        / monthly_summary['current_fee_ex_vat'] * 100
    ).round(1)

    print("\n" + monthly_summary.to_string())

    # === SAVE TO CSV ===
    output_dir = 'output'
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d')

    # Category summary
    category_file = os.path.join(output_dir, f'pricing_comparison_by_category_{timestamp}.csv')
    category_summary.to_csv(category_file)
    print(f"\n✓ Category summary saved: {category_file}")

    # Monthly summary
    monthly_file = os.path.join(output_dir, f'pricing_comparison_by_month_{timestamp}.csv')
    monthly_summary.to_csv(monthly_file)
    print(f"✓ Monthly summary saved: {monthly_file}")

    # Detailed breakdown by category and month
    detailed = booking_df.groupby(['Month', 'Category']).agg({
        'TicketQuantity': 'sum',
        'PaymentReceived': 'sum',
        'current_fee_inc_vat': 'sum',
        'current_fee_ex_vat': 'sum',
        'proposed_total_fee': 'sum',
        'fee_difference': 'sum'
    }).round(2)

    detailed_file = os.path.join(output_dir, f'pricing_comparison_detailed_{timestamp}.csv')
    detailed.to_csv(detailed_file)
    print(f"✓ Detailed breakdown saved: {detailed_file}")

    print("\n" + "=" * 60)
    print("COMPLETE")
    print("=" * 60)


if __name__ == '__main__':
    main()
