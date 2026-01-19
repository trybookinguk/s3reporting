#!/usr/bin/env python3
"""
Regional Industry Analysis Report for TryBooking UK.

Analyses industry sectors for clients in specific regions (Wales, Northern Ireland, etc.).
Outputs CSV files with account counts, fees, and revenue by industry for each region.

Usage:
    python3 analyse_wales_ni_sectors.py
"""
import os
import pandas as pd
from datetime import datetime
from modules.utils.config import UK_TZ
from modules.utils.data_loader import load_accounts, load_booking_data, filter_successful_transactions
from modules.uk_regional_segmentation import (
    extract_postcode_areas_vectorized,
    get_regions_vectorized,
    VALID_UK_POSTCODE_AREAS
)


def main():
    print("Loading data...")

    # Load accounts
    accounts_df = load_accounts()
    print(f"  Accounts loaded: {len(accounts_df):,}")

    # Load booking data for revenue/fees context
    booking_all_df = load_booking_data(data_type='BookingDataAll')
    booking_current_df = load_booking_data(data_type='BookingData')
    booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
    if 'BookingTransactionId' in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=['BookingTransactionId'])
    booking_df = filter_successful_transactions(booking_df)
    print(f"  Booking records loaded: {len(booking_df):,}")

    # Add region to accounts based on postcode
    postcode_col = 'Postcode' if 'Postcode' in accounts_df.columns else 'AccountPostcode'
    if postcode_col not in accounts_df.columns:
        print(f"  Warning: No postcode column found in accounts")
        return

    accounts_df['PostcodeArea'] = extract_postcode_areas_vectorized(accounts_df[postcode_col])
    accounts_df['Region'] = get_regions_vectorized(accounts_df['PostcodeArea'])

    # Filter invalid UK postcodes
    accounts_df.loc[~accounts_df['PostcodeArea'].isin(VALID_UK_POSTCODE_AREAS), 'Region'] = None

    # Filter to Wales and Northern Ireland
    wales_ni = accounts_df[accounts_df['Region'].isin(['Wales', 'Northern Ireland'])].copy()
    print(f"\nAccounts in Wales & NI: {len(wales_ni):,}")

    # Get account ID column
    account_id_col = 'AccountId' if 'AccountId' in accounts_df.columns else 'Id'

    # Calculate fees per account from booking data
    fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    if all(col in booking_df.columns for col in fee_columns):
        booking_df['TotalFees'] = (
            booking_df['BookingFee'].fillna(0) +
            booking_df['CardFee'].fillna(0) +
            booking_df['ProcessingFee'].fillna(0) +
            booking_df['TicketFee'].fillna(0)
        )
    else:
        booking_df['TotalFees'] = 0

    account_fees = booking_df.groupby('AccountId').agg({
        'TotalFees': 'sum',
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum'
    }).reset_index()
    account_fees.columns = ['AccountId', 'Total_Fees', 'Total_Revenue', 'Total_Tickets']

    # Merge fees to accounts
    wales_ni = wales_ni.merge(
        account_fees.rename(columns={'AccountId': account_id_col}),
        on=account_id_col,
        how='left'
    )
    wales_ni['Total_Fees'] = wales_ni['Total_Fees'].fillna(0)
    wales_ni['Total_Revenue'] = wales_ni['Total_Revenue'].fillna(0)
    wales_ni['Total_Tickets'] = wales_ni['Total_Tickets'].fillna(0)

    # === ANALYSIS BY REGION AND INDUSTRY ===
    print("\n" + "=" * 70)
    print("INDUSTRY BREAKDOWN BY REGION")
    print("=" * 70)

    for region in ['Wales', 'Northern Ireland']:
        region_accounts = wales_ni[wales_ni['Region'] == region]
        print(f"\n{'=' * 50}")
        print(f"{region.upper()}")
        print(f"{'=' * 50}")
        print(f"Total accounts: {len(region_accounts):,}")
        print(f"Total fees: £{region_accounts['Total_Fees'].sum():,.2f}")
        print(f"Total revenue: £{region_accounts['Total_Revenue'].sum():,.2f}")
        print(f"Total tickets: {region_accounts['Total_Tickets'].sum():,.0f}")

        if 'Industry' in region_accounts.columns:
            print(f"\nBy Industry (sorted by fees):")
            print("-" * 50)

            industry_summary = region_accounts.groupby('Industry', dropna=False).agg({
                account_id_col: 'count',
                'Total_Fees': 'sum',
                'Total_Revenue': 'sum',
                'Total_Tickets': 'sum'
            }).reset_index()
            industry_summary.columns = ['Industry', 'Accounts', 'Fees', 'Revenue', 'Tickets']
            industry_summary['Industry'] = industry_summary['Industry'].fillna('Unspecified')
            industry_summary = industry_summary.sort_values('Fees', ascending=False)

            total_fees = industry_summary['Fees'].sum()

            for _, row in industry_summary.iterrows():
                pct = (row['Fees'] / total_fees * 100) if total_fees > 0 else 0
                print(f"  {row['Industry']:<35} {row['Accounts']:>5} accounts  £{row['Fees']:>10,.2f} fees ({pct:>5.1f}%)")

            # Save to CSV
            csv_file = f"wales_ni_{region.lower().replace(' ', '_')}_industries.csv"
            industry_summary.to_csv(csv_file, index=False, float_format='%.2f')
            print(f"\n  Saved: {csv_file}")

    # === COMBINED SUMMARY ===
    print("\n" + "=" * 70)
    print("COMBINED WALES & NORTHERN IRELAND SUMMARY")
    print("=" * 70)

    if 'Industry' in wales_ni.columns:
        combined_summary = wales_ni.groupby(['Region', 'Industry'], dropna=False).agg({
            account_id_col: 'count',
            'Total_Fees': 'sum',
            'Total_Revenue': 'sum',
            'Total_Tickets': 'sum'
        }).reset_index()
        combined_summary.columns = ['Region', 'Industry', 'Accounts', 'Fees', 'Revenue', 'Tickets']
        combined_summary['Industry'] = combined_summary['Industry'].fillna('Unspecified')
        combined_summary = combined_summary.sort_values(['Region', 'Fees'], ascending=[True, False])

        # Save combined
        combined_summary.to_csv('wales_ni_combined_industries.csv', index=False, float_format='%.2f')
        print("Saved: wales_ni_combined_industries.csv")

        # Print top 10 overall
        overall = wales_ni.groupby('Industry', dropna=False).agg({
            account_id_col: 'count',
            'Total_Fees': 'sum',
        }).reset_index()
        overall.columns = ['Industry', 'Accounts', 'Fees']
        overall['Industry'] = overall['Industry'].fillna('Unspecified')
        overall = overall.sort_values('Fees', ascending=False).head(10)

        print("\nTop 10 Industries (Wales + NI combined, by fees):")
        print("-" * 50)
        for _, row in overall.iterrows():
            print(f"  {row['Industry']:<35} {row['Accounts']:>5} accounts  £{row['Fees']:>10,.2f}")


if __name__ == '__main__':
    main()
