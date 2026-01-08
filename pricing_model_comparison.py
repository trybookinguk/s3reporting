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
from datetime import datetime
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.utils.data_loader import load_booking_data, filter_successful_transactions
from modules.utils.date_utils import get_latest_data_date


def classify_sales_channel(payment_type) -> str:
    """Classify payment type as Box Office or Online."""
    if pd.isna(payment_type):
        return 'Online'
    payment_type_upper = str(payment_type).upper().strip()
    if 'CARD PRESENT' in payment_type_upper or payment_type_upper == 'CASH':
        return 'Box Office'
    return 'Online'


def classify_box_office_type(payment_type) -> str:
    """Classify Box Office transactions as Cash or Card."""
    if pd.isna(payment_type):
        return None
    payment_type_upper = str(payment_type).upper().strip()
    if payment_type_upper == 'CASH':
        return 'Cash'
    elif 'CARD PRESENT' in payment_type_upper:
        return 'Card'
    return None


def normalise_gateway(gateway_value) -> str:
    """Normalise gateway names."""
    if pd.isna(gateway_value):
        return 'Unknown'
    gateway_str = str(gateway_value).strip()
    if 'stripe connect' in gateway_str.lower():
        return 'Stripe Connect'
    elif 'stripe' in gateway_str.lower():
        return 'Stripe'
    elif 'paypal' in gateway_str.lower():
        return 'PayPal'
    else:
        return 'Default'


def calculate_current_fees_ex_vat(row: pd.Series) -> dict:
    """
    Calculate current fees converted to ex VAT.

    Current fees in data are inc VAT, so divide by 1.2.
    """
    booking_fee = row.get('BookingFee', 0) or 0
    card_fee = row.get('CardFee', 0) or 0
    processing_fee = row.get('ProcessingFee', 0) or 0
    ticket_fee = row.get('TicketFee', 0) or 0

    total_fee_inc_vat = booking_fee + card_fee + processing_fee + ticket_fee
    total_fee_ex_vat = total_fee_inc_vat / 1.2

    return {
        'current_fee_inc_vat': total_fee_inc_vat,
        'current_fee_ex_vat': total_fee_ex_vat
    }


def calculate_proposed_fees(row: pd.Series) -> dict:
    """
    Calculate proposed fees (all ex VAT).

    Proposed Structure:
    - Online (Default/PayPal gateway):
        - Processing fee: 4% of PaymentReceived
        - Ticket fee: 20p per ticket
    - Online (Stripe gateway):
        - Processing fee: 0 (they pay Stripe directly)
        - Ticket fee: 75p per ticket
    - Box Office Cash:
        - Free (no fees)
    - Box Office Card:
        - Processing fee: 3% of PaymentReceived
        - Ticket fee: 20p per ticket
    """
    payment_received = row.get('PaymentReceived', 0) or 0
    ticket_quantity = row.get('TicketQuantity', 0) or 0
    sales_channel = row.get('Sales_Channel', 'Online')
    bo_type = row.get('BO_Type')
    gateway = row.get('Gateway_Normalised', 'Default')

    # Free tickets have no fees
    if payment_received == 0:
        return {
            'proposed_processing_fee': 0,
            'proposed_ticket_fee': 0,
            'proposed_total_fee': 0
        }

    # Box Office transactions
    if sales_channel == 'Box Office':
        if bo_type == 'Cash':
            # Box Office Cash = Free
            return {
                'proposed_processing_fee': 0,
                'proposed_ticket_fee': 0,
                'proposed_total_fee': 0
            }
        else:
            # Box Office Card = 3% + 20p per ticket
            processing_fee = payment_received * 0.03
            ticket_fee = ticket_quantity * 0.20
            return {
                'proposed_processing_fee': processing_fee,
                'proposed_ticket_fee': ticket_fee,
                'proposed_total_fee': processing_fee + ticket_fee
            }

    # Online transactions
    if gateway in ['Stripe Connect', 'Stripe']:
        # Stripe = 75p per ticket only (no processing fee)
        ticket_fee = ticket_quantity * 0.75
        return {
            'proposed_processing_fee': 0,
            'proposed_ticket_fee': ticket_fee,
            'proposed_total_fee': ticket_fee
        }
    else:
        # Default/PayPal = 4% + 20p per ticket
        processing_fee = payment_received * 0.04
        ticket_fee = ticket_quantity * 0.20
        return {
            'proposed_processing_fee': processing_fee,
            'proposed_ticket_fee': ticket_fee,
            'proposed_total_fee': processing_fee + ticket_fee
        }


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

    # Get latest data date
    target_date = get_latest_data_date()
    print(f"Target date: {target_date}")

    # Load booking data for the analysis year
    print(f"Loading {analysis_year} booking data...")
    booking_df = load_booking_data(target_date)

    if booking_df is None or len(booking_df) == 0:
        print("ERROR: No booking data loaded")
        return

    # Filter to successful transactions only
    booking_df = filter_successful_transactions(booking_df)

    # Filter to the analysis year
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'])
    booking_df = booking_df[booking_df['TransactionDate'].dt.year == analysis_year]

    if len(booking_df) == 0:
        print(f"ERROR: No transactions found for {analysis_year}")
        return

    print(f"  Loaded {len(booking_df):,} transactions for {analysis_year}")

    # Ensure required columns exist
    required_cols = ['PaymentReceived', 'TicketQuantity', 'TransactionDate']
    for col in required_cols:
        if col not in booking_df.columns:
            print(f"ERROR: Missing required column: {col}")
            return

    # Add classification columns
    print("Classifying transactions...")
    booking_df['Sales_Channel'] = booking_df['PaymentType'].apply(classify_sales_channel)
    booking_df['BO_Type'] = booking_df['PaymentType'].apply(classify_box_office_type)

    # Normalise gateway
    gateway_col = None
    for col in ['GatewayName', 'Gateway Group', 'GatewayGroup']:
        if col in booking_df.columns:
            gateway_col = col
            break

    if gateway_col:
        booking_df['Gateway_Normalised'] = booking_df[gateway_col].apply(normalise_gateway)
    else:
        booking_df['Gateway_Normalised'] = 'Default'

    # Add month column (TransactionDate already converted to datetime above)
    booking_df['Month'] = booking_df['TransactionDate'].dt.to_period('M')

    # Calculate fees for each transaction
    print("Calculating fees...")

    # Current fees (convert to ex VAT)
    current_fees = booking_df.apply(calculate_current_fees_ex_vat, axis=1, result_type='expand')
    booking_df = pd.concat([booking_df, current_fees], axis=1)

    # Proposed fees
    proposed_fees = booking_df.apply(calculate_proposed_fees, axis=1, result_type='expand')
    booking_df = pd.concat([booking_df, proposed_fees], axis=1)

    # Calculate difference
    booking_df['fee_difference'] = booking_df['proposed_total_fee'] - booking_df['current_fee_ex_vat']

    # === SUMMARY BY CATEGORY ===
    print("\n" + "=" * 60)
    print(f"SUMMARY BY CATEGORY ({analysis_year} Full Year)")
    print("=" * 60)

    # Create category column
    def get_category(row):
        if row['Sales_Channel'] == 'Box Office':
            return f"Box Office - {row['BO_Type'] or 'Unknown'}"
        else:
            return f"Online - {row['Gateway_Normalised']}"

    booking_df['Category'] = booking_df.apply(get_category, axis=1)

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
