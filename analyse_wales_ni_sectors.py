#!/usr/bin/env python3
"""
Regional Industry Analysis Report for TryBooking UK.

Analyses industry sectors for clients in specific regions (Wales, Northern Ireland, etc.).
Outputs CSV files with account counts, fees, and revenue by industry for each region.

Filters to 2025 transactions only and uses both account postcodes and event postcodes
to determine regional assignment.

Usage:
    python3 analyse_wales_ni_sectors.py
"""

import os
from datetime import datetime

import pandas as pd

from modules.uk_regional_segmentation import (
    VALID_UK_POSTCODE_AREAS,
    extract_postcode_areas_vectorized,
    get_regions_vectorized,
)
from modules.utils.config import UK_TZ
from modules.utils.data_loader import (
    filter_successful_transactions,
    load_accounts,
    load_booking_data,
)

# Year to filter transactions for
TARGET_YEAR = 2025


def main():
    print("Loading data...")

    # Load accounts
    accounts_df = load_accounts()
    print(f"  Accounts loaded: {len(accounts_df):,}")

    # Load booking data for revenue/fees context
    booking_all_df = load_booking_data(data_type="BookingDataAll")
    booking_current_df = load_booking_data(data_type="BookingData")
    booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
    if "BookingTransactionId" in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=["BookingTransactionId"])
    booking_df = filter_successful_transactions(booking_df)
    print(f"  Booking records loaded (all time): {len(booking_df):,}")

    # Filter to TARGET_YEAR transactions only
    if "TransactionDate" in booking_df.columns:
        booking_df["TransactionDate"] = pd.to_datetime(
            booking_df["TransactionDate"], errors="coerce", utc=True
        )
        booking_df = booking_df[
            booking_df["TransactionDate"].dt.year == TARGET_YEAR
        ].copy()
        print(f"  Booking records ({TARGET_YEAR} only): {len(booking_df):,}")

    # === REGIONAL ASSIGNMENT ===
    # We identify Wales/NI accounts using BOTH:
    # 1. Account postcode (where the organiser is based)
    # 2. Event postcode (where they run events)
    # An account is included if EITHER postcode is in Wales/NI

    # Get account ID column
    account_id_col = "AccountId" if "AccountId" in accounts_df.columns else "Id"

    # Step 1: Identify accounts with Wales/NI account postcodes
    postcode_col = (
        "Postcode" if "Postcode" in accounts_df.columns else "AccountPostcode"
    )
    if postcode_col not in accounts_df.columns:
        print("  Warning: No postcode column found in accounts")
        return

    accounts_df["PostcodeArea"] = extract_postcode_areas_vectorized(
        accounts_df[postcode_col]
    )
    accounts_df["Region_Account"] = get_regions_vectorized(accounts_df["PostcodeArea"])

    # Filter invalid UK postcodes
    accounts_df.loc[
        ~accounts_df["PostcodeArea"].isin(VALID_UK_POSTCODE_AREAS), "Region_Account"
    ] = None

    wales_ni_by_account = set(
        accounts_df[accounts_df["Region_Account"].isin(["Wales", "Northern Ireland"])][
            account_id_col
        ].unique()
    )
    print(f"\n  Accounts with Wales/NI account postcode: {len(wales_ni_by_account):,}")

    # Step 2: Identify accounts with Wales/NI event postcodes
    wales_ni_by_event = set()
    if "EventPostcode" in booking_df.columns:
        booking_df["EventPostcodeArea"] = extract_postcode_areas_vectorized(
            booking_df["EventPostcode"]
        )
        booking_df["Region_Event"] = get_regions_vectorized(
            booking_df["EventPostcodeArea"]
        )

        # Filter invalid UK postcodes
        booking_df.loc[
            ~booking_df["EventPostcodeArea"].isin(VALID_UK_POSTCODE_AREAS),
            "Region_Event",
        ] = None

        # Get accounts that have run events in Wales/NI
        wales_ni_events = booking_df[
            booking_df["Region_Event"].isin(["Wales", "Northern Ireland"])
        ]
        wales_ni_by_event = set(wales_ni_events["AccountId"].unique())
        print(f"  Accounts with Wales/NI event postcodes: {len(wales_ni_by_event):,}")

    # Step 3: Combine both sources
    all_wales_ni_accounts = wales_ni_by_account | wales_ni_by_event
    print(f"  Combined unique accounts: {len(all_wales_ni_accounts):,}")

    # Filter accounts to Wales/NI
    wales_ni = accounts_df[
        accounts_df[account_id_col].isin(all_wales_ni_accounts)
    ].copy()

    # Determine the primary region for each account
    # Priority: Account postcode > Event postcode (most common event region)
    def get_primary_region(account_id, account_region):
        """Get primary region, preferring account postcode over event postcode."""
        if pd.notna(account_region) and account_region in ["Wales", "Northern Ireland"]:
            return account_region
        # Fall back to most common event region
        if "Region_Event" in booking_df.columns:
            account_events = booking_df[
                (booking_df["AccountId"] == account_id)
                & (booking_df["Region_Event"].isin(["Wales", "Northern Ireland"]))
            ]
            if len(account_events) > 0:
                return account_events["Region_Event"].mode().iloc[0]
        return "Unknown"

    wales_ni["Region"] = wales_ni.apply(
        lambda row: get_primary_region(row[account_id_col], row.get("Region_Account")),
        axis=1,
    )

    # Filter out any that ended up with Unknown region (shouldn't happen but safety check)
    wales_ni = wales_ni[wales_ni["Region"].isin(["Wales", "Northern Ireland"])].copy()
    print(f"\nAccounts in Wales & NI (final): {len(wales_ni):,}")

    # Calculate fees per account from booking data
    fee_columns = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    if all(col in booking_df.columns for col in fee_columns):
        booking_df["TotalFees"] = (
            booking_df["BookingFee"].fillna(0)
            + booking_df["CardFee"].fillna(0)
            + booking_df["ProcessingFee"].fillna(0)
            + booking_df["TicketFee"].fillna(0)
        )
    else:
        booking_df["TotalFees"] = 0

    account_fees = (
        booking_df.groupby("AccountId")
        .agg({"TotalFees": "sum", "PaymentReceived": "sum", "TicketQuantity": "sum"})
        .reset_index()
    )
    account_fees.columns = ["AccountId", "Total_Fees", "Total_Revenue", "Total_Tickets"]

    # Merge fees to accounts
    wales_ni = wales_ni.merge(
        account_fees.rename(columns={"AccountId": account_id_col}),
        on=account_id_col,
        how="left",
    )
    wales_ni["Total_Fees"] = wales_ni["Total_Fees"].fillna(0)
    wales_ni["Total_Revenue"] = wales_ni["Total_Revenue"].fillna(0)
    wales_ni["Total_Tickets"] = wales_ni["Total_Tickets"].fillna(0)

    # === ANALYSIS BY REGION AND INDUSTRY ===
    print("\n" + "=" * 70)
    print("INDUSTRY BREAKDOWN BY REGION")
    print("=" * 70)

    for region in ["Wales", "Northern Ireland"]:
        region_accounts = wales_ni[wales_ni["Region"] == region]
        print(f"\n{'=' * 50}")
        print(f"{region.upper()}")
        print(f"{'=' * 50}")
        print(f"Total accounts: {len(region_accounts):,}")
        print(f"Total fees: £{region_accounts['Total_Fees'].sum():,.2f}")
        print(f"Total revenue: £{region_accounts['Total_Revenue'].sum():,.2f}")
        print(f"Total tickets: {region_accounts['Total_Tickets'].sum():,.0f}")

        if "Industry" in region_accounts.columns:
            print(f"\nBy Industry (sorted by fees):")
            print("-" * 50)

            industry_summary = (
                region_accounts.groupby("Industry", dropna=False, observed=False)
                .agg(
                    {
                        account_id_col: "count",
                        "Total_Fees": "sum",
                        "Total_Revenue": "sum",
                        "Total_Tickets": "sum",
                    }
                )
                .reset_index()
            )
            industry_summary.columns = [
                "Industry",
                "Accounts",
                "Fees",
                "Revenue",
                "Tickets",
            ]
            industry_summary["Industry"] = (
                industry_summary["Industry"].astype(object).fillna("Unspecified")
            )
            industry_summary = industry_summary.sort_values("Fees", ascending=False)

            total_fees = industry_summary["Fees"].sum()

            for _, row in industry_summary.iterrows():
                pct = (row["Fees"] / total_fees * 100) if total_fees > 0 else 0
                print(
                    f"  {row['Industry']:<35} {row['Accounts']:>5} accounts  £{row['Fees']:>10,.2f} fees ({pct:>5.1f}%)"
                )

            # Save to CSV
            csv_file = f"wales_ni_{region.lower().replace(' ', '_')}_industries.csv"
            industry_summary.to_csv(csv_file, index=False, float_format="%.2f")
            print(f"\n  Saved: {csv_file}")

    # === DETAILED ACCOUNT LIST ===
    print("\n" + "=" * 70)
    print("DETAILED ACCOUNT LIST")
    print("=" * 70)

    # Determine which columns to include in the detailed export
    detail_columns = [account_id_col]

    # Add account name if available
    name_col = "AccountName" if "AccountName" in wales_ni.columns else "Name"
    if name_col in wales_ni.columns:
        detail_columns.append(name_col)

    # Add industry columns
    if "Industry" in wales_ni.columns:
        detail_columns.append("Industry")
    if "SubIndustry" in wales_ni.columns:
        detail_columns.append("SubIndustry")

    # Add region and postcode
    detail_columns.append("Region")
    detail_columns.append(postcode_col)
    detail_columns.append("PostcodeArea")

    # Add metrics
    detail_columns.extend(["Total_Fees", "Total_Revenue", "Total_Tickets"])

    # Create the detailed accounts DataFrame
    detailed_accounts = wales_ni[detail_columns].copy()

    # Rename columns for clarity
    column_renames = {
        account_id_col: "AccountId",
        name_col: "AccountName",
        postcode_col: "Postcode",
    }
    detailed_accounts = detailed_accounts.rename(
        columns={
            k: v for k, v in column_renames.items() if k in detailed_accounts.columns
        }
    )

    # Sort by region then by fees (highest first)
    detailed_accounts = detailed_accounts.sort_values(
        ["Region", "Total_Fees"], ascending=[True, False]
    )

    # Save detailed account list
    detailed_accounts.to_csv(
        "wales_ni_account_list.csv", index=False, float_format="%.2f"
    )
    print(f"Saved: wales_ni_account_list.csv ({len(detailed_accounts):,} accounts)")

    # Also save per-region detailed lists
    for region in ["Wales", "Northern Ireland"]:
        region_detail = detailed_accounts[detailed_accounts["Region"] == region].copy()
        region_filename = (
            f"wales_ni_{region.lower().replace(' ', '_')}_account_list.csv"
        )
        region_detail.to_csv(region_filename, index=False, float_format="%.2f")
        print(f"Saved: {region_filename} ({len(region_detail):,} accounts)")

    # === COMBINED SUMMARY ===
    print("\n" + "=" * 70)
    print("COMBINED WALES & NORTHERN IRELAND SUMMARY")
    print("=" * 70)

    if "Industry" in wales_ni.columns:
        combined_summary = (
            wales_ni.groupby(["Region", "Industry"], dropna=False, observed=False)
            .agg(
                {
                    account_id_col: "count",
                    "Total_Fees": "sum",
                    "Total_Revenue": "sum",
                    "Total_Tickets": "sum",
                }
            )
            .reset_index()
        )
        combined_summary.columns = [
            "Region",
            "Industry",
            "Accounts",
            "Fees",
            "Revenue",
            "Tickets",
        ]
        combined_summary["Industry"] = (
            combined_summary["Industry"].astype(object).fillna("Unspecified")
        )
        combined_summary = combined_summary.sort_values(
            ["Region", "Fees"], ascending=[True, False]
        )

        # Save combined
        combined_summary.to_csv(
            "wales_ni_combined_industries.csv", index=False, float_format="%.2f"
        )
        print("Saved: wales_ni_combined_industries.csv")

        # Print top 10 overall
        overall = (
            wales_ni.groupby("Industry", dropna=False, observed=False)
            .agg(
                {
                    account_id_col: "count",
                    "Total_Fees": "sum",
                }
            )
            .reset_index()
        )
        overall.columns = ["Industry", "Accounts", "Fees"]
        overall["Industry"] = overall["Industry"].astype(object).fillna("Unspecified")
        overall = overall.sort_values("Fees", ascending=False).head(10)

        print("\nTop 10 Industries (Wales + NI combined, by fees):")
        print("-" * 50)
        for _, row in overall.iterrows():
            print(
                f"  {row['Industry']:<35} {row['Accounts']:>5} accounts  £{row['Fees']:>10,.2f}"
            )


if __name__ == "__main__":
    main()
