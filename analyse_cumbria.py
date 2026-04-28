#!/usr/bin/env python3
"""
Cumbria Client Analysis for TryBooking UK.

Lists all clients in Cumbria with industry, sub-industry and basic stats.
Uses account postcodes and event postcodes to identify Cumbrian accounts.

Cumbria postcode coverage:
  - All CA postcodes (Carlisle and north Cumbria)
  - LA7-LA23 (south Cumbria: Kendal, Barrow, Windermere, etc.)
  - Excludes LA1-LA6 (Lancaster, Lancashire)

Usage:
    python3 analyse_cumbria.py
"""

import re
from datetime import datetime

import pandas as pd

from modules.uk_regional_segmentation import (
    VALID_UK_POSTCODE_AREAS,
    extract_postcode_areas_vectorized,
)
from modules.utils.data_loader import (
    filter_successful_transactions,
    load_accounts,
    load_booking_data,
)

# Cumbria: all CA postcodes + LA7-LA23 (excluding LA1-LA6 which are Lancashire)
CUMBRIA_LA_DISTRICTS = frozenset(
    f"LA{n}" for n in range(7, 24)
)


def extract_postcode_district(postcodes: pd.Series) -> pd.Series:
    """Extract the full postcode district (e.g. 'LA7', 'CA1') from postcodes."""
    result = pd.Series(None, index=postcodes.index, dtype="object")
    valid_mask = postcodes.notna() & (postcodes != "")
    if valid_mask.any():
        clean = postcodes[valid_mask].str.strip().str.upper()
        extracted = clean.str.extract(r"^([A-Z]{1,2}\d{1,2})", expand=False)
        result.loc[valid_mask] = extracted
    return result


def is_cumbria_postcode(postcodes: pd.Series) -> pd.Series:
    """Check whether postcodes fall within Cumbria.

    Returns a boolean Series.
    """
    areas = extract_postcode_areas_vectorized(postcodes)
    districts = extract_postcode_district(postcodes)

    # CA postcodes are all Cumbria
    is_ca = areas == "CA"

    # LA postcodes only if district is LA7+
    is_cumbria_la = districts.isin(CUMBRIA_LA_DISTRICTS)

    return is_ca | is_cumbria_la


def main():
    print("Loading data...")

    # Load accounts
    accounts_df = load_accounts()
    print(f"  Accounts loaded: {len(accounts_df):,}")

    # Load booking data
    booking_all_df = load_booking_data(data_type="BookingDataAll")
    booking_current_df = load_booking_data(data_type="BookingData")
    booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
    if "BookingTransactionId" in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=["BookingTransactionId"])
    booking_df = filter_successful_transactions(booking_df)
    print(f"  Booking records loaded (all time): {len(booking_df):,}")

    # Filter to 2025 transactions only
    if "TransactionDate" in booking_df.columns:
        booking_df["TransactionDate"] = pd.to_datetime(
            booking_df["TransactionDate"], errors="coerce", utc=True
        )
        booking_df = booking_df[booking_df["TransactionDate"].dt.year == 2025].copy()
        print(f"  Booking records (2025 only): {len(booking_df):,}")

    # === IDENTIFY CUMBRIAN ACCOUNTS ===

    account_id_col = "AccountId" if "AccountId" in accounts_df.columns else "Id"
    postcode_col = "Postcode" if "Postcode" in accounts_df.columns else "AccountPostcode"

    # Method 1: Account postcode is in Cumbria
    accounts_df["is_cumbria_account"] = is_cumbria_postcode(accounts_df[postcode_col])
    cumbria_by_account = set(
        accounts_df[accounts_df["is_cumbria_account"]][account_id_col].unique()
    )
    print(f"\n  Accounts with Cumbria account postcode: {len(cumbria_by_account):,}")

    # Method 2: Account has run events in Cumbria
    cumbria_by_event = set()
    if "EventPostcode" in booking_df.columns:
        booking_df["is_cumbria_event"] = is_cumbria_postcode(booking_df["EventPostcode"])
        cumbria_events = booking_df[booking_df["is_cumbria_event"]]
        cumbria_by_event = set(cumbria_events["AccountId"].unique())
        print(f"  Accounts with Cumbria event postcodes: {len(cumbria_by_event):,}")

    # Combine both sources
    all_cumbria_accounts = cumbria_by_account | cumbria_by_event
    print(f"  Combined unique Cumbria accounts: {len(all_cumbria_accounts):,}")

    # Filter to Cumbria accounts
    cumbria = accounts_df[accounts_df[account_id_col].isin(all_cumbria_accounts)].copy()

    # Tag how we identified them
    cumbria["Source"] = cumbria[account_id_col].apply(
        lambda aid: "Account & Events" if aid in cumbria_by_account and aid in cumbria_by_event
        else "Account Postcode" if aid in cumbria_by_account
        else "Event Postcode"
    )

    # === CALCULATE STATS FROM BOOKING DATA ===

    # Fee columns
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

    # All-time stats per account
    account_stats = (
        booking_df.groupby("AccountId")
        .agg(
            Total_Fees=("TotalFees", "sum"),
            Total_Revenue=("PaymentReceived", "sum"),
            Total_Tickets=("TicketQuantity", "sum"),
            Total_Transactions=("BookingTransactionId", "nunique"),
            Total_Events=("EventId", "nunique"),
            First_Transaction=("TransactionDate", "min"),
            Latest_Transaction=("TransactionDate", "max"),
        )
        .reset_index()
    )

    # Merge stats
    cumbria = cumbria.merge(
        account_stats.rename(columns={"AccountId": account_id_col}),
        on=account_id_col,
        how="left",
    )
    for col in ["Total_Fees", "Total_Revenue", "Total_Tickets", "Total_Transactions", "Total_Events"]:
        cumbria[col] = cumbria[col].fillna(0)
    for col in ["First_Transaction", "Latest_Transaction"]:
        if col in cumbria.columns:
            cumbria[col] = pd.to_datetime(cumbria[col], errors="coerce").dt.tz_localize(None)

    # === OUTPUT ===

    # Select and rename columns
    output_cols = [account_id_col]

    name_col = "AccountName" if "AccountName" in cumbria.columns else "Name"
    if name_col in cumbria.columns:
        output_cols.append(name_col)

    if "Industry" in cumbria.columns:
        output_cols.append("Industry")
    if "SubIndustry" in cumbria.columns:
        output_cols.append("SubIndustry")
    if "AccountStatus" in cumbria.columns:
        output_cols.append("AccountStatus")

    output_cols.append(postcode_col)
    output_cols.append("Source")
    output_cols.extend([
        "Total_Fees", "Total_Revenue", "Total_Tickets",
        "Total_Transactions", "Total_Events",
        "First_Transaction", "Latest_Transaction",
    ])

    result = cumbria[output_cols].copy()

    rename_map = {
        account_id_col: "AccountId",
        name_col: "AccountName",
        postcode_col: "Postcode",
    }
    result = result.rename(columns={k: v for k, v in rename_map.items() if k in result.columns})
    result = result.sort_values("Total_Revenue", ascending=False)

    # Console summary
    print(f"\n{'=' * 70}")
    print(f"CUMBRIA CLIENT SUMMARY (2025)")
    print(f"{'=' * 70}")
    print(f"Total accounts: {len(result):,}")
    if "AccountStatus" in result.columns:
        for status, count in result["AccountStatus"].value_counts().items():
            print(f"  {status}: {count:,}")
    print(f"\nTotal fees (2025): £{result['Total_Fees'].sum():,.2f}")
    print(f"Total revenue (2025): £{result['Total_Revenue'].sum():,.2f}")
    print(f"Total tickets (2025): {result['Total_Tickets'].sum():,.0f}")

    if "Industry" in result.columns:
        # Classify Active vs Inactive
        # Active = "Activated" status; everything else is Inactive
        result["Status_Group"] = result["AccountStatus"].apply(
            lambda s: "Active" if s == "Activated" else "Inactive"
        )

        print(f"\nBy Industry and Status (sorted by revenue):")
        print("-" * 90)

        sector_status = (
            result.groupby(["Industry", "Status_Group"], dropna=False, observed=False)
            .agg(
                Accounts=("AccountId", "count"),
                Fees=("Total_Fees", "sum"),
                Revenue=("Total_Revenue", "sum"),
                Tickets=("Total_Tickets", "sum"),
            )
            .reset_index()
        )
        sector_status["Industry"] = sector_status["Industry"].astype(object).fillna("Unspecified")

        # Pivot so we get Active/Inactive columns side by side
        industries = sector_status.groupby("Industry")["Revenue"].sum().sort_values(ascending=False).index

        print(f"  {'Industry':<28} {'Active':>7} {'Revenue':>12}  {'Inactive':>8} {'Revenue':>12}  {'Total':>12}")
        print(f"  {'-'*28} {'-'*7} {'-'*12}  {'-'*8} {'-'*12}  {'-'*12}")

        for industry in industries:
            ind_data = sector_status[sector_status["Industry"] == industry]
            active = ind_data[ind_data["Status_Group"] == "Active"]
            inactive = ind_data[ind_data["Status_Group"] == "Inactive"]

            a_count = int(active["Accounts"].sum()) if len(active) else 0
            a_rev = active["Revenue"].sum() if len(active) else 0
            i_count = int(inactive["Accounts"].sum()) if len(inactive) else 0
            i_rev = inactive["Revenue"].sum() if len(inactive) else 0
            total_rev = a_rev + i_rev

            print(f"  {industry:<28} {a_count:>7} £{a_rev:>10,.2f}  {i_count:>8} £{i_rev:>10,.2f}  £{total_rev:>10,.2f}")

    # Save to Excel with tabs
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"cumbria_clients_{timestamp}.xlsx"

    # Build sector summary pivot
    sector_pivot = None
    if "Industry" in result.columns:
        sector_pivot = sector_status.pivot_table(
            index="Industry",
            columns="Status_Group",
            values=["Accounts", "Revenue", "Fees", "Tickets"],
            aggfunc="sum",
            fill_value=0,
        )
        sector_pivot.columns = [f"{val}_{status}" for val, status in sector_pivot.columns]
        sector_pivot = sector_pivot.reset_index()
        for metric in ["Accounts", "Revenue", "Fees", "Tickets"]:
            active_col = f"{metric}_Active"
            inactive_col = f"{metric}_Inactive"
            if active_col not in sector_pivot.columns:
                sector_pivot[active_col] = 0
            if inactive_col not in sector_pivot.columns:
                sector_pivot[inactive_col] = 0
            sector_pivot[f"{metric}_Total"] = sector_pivot[active_col] + sector_pivot[inactive_col]
        sector_pivot = sector_pivot.sort_values("Revenue_Total", ascending=False)

        # Reorder columns for readability
        ordered_cols = [
            "Industry",
            "Accounts_Active", "Accounts_Inactive", "Accounts_Total",
            "Revenue_Active", "Revenue_Inactive", "Revenue_Total",
            "Fees_Active", "Fees_Inactive", "Fees_Total",
            "Tickets_Active", "Tickets_Inactive", "Tickets_Total",
        ]
        sector_pivot = sector_pivot[[c for c in ordered_cols if c in sector_pivot.columns]]

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="All Clients", index=False)
        if sector_pivot is not None:
            sector_pivot.to_excel(writer, sheet_name="Sector Summary", index=False)

        # Auto-fit column widths
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_length = max(
                    len(str(col[0].value or "")),
                    *(len(str(cell.value or "")) for cell in col[1:])
                )
                ws.column_dimensions[col[0].column_letter].width = min(max_length + 2, 50)

    print(f"\nSaved: {output_file}")
    print(f"  Tab 'All Clients': {len(result):,} accounts (sorted by revenue)")
    if sector_pivot is not None:
        print(f"  Tab 'Sector Summary': {len(sector_pivot)} industries (Active vs Inactive)")


if __name__ == "__main__":
    main()
