#!/usr/bin/env python3
"""
Rank clients by Box Office usage over the last 365 days.

Lists every account with any Box Office activity in the rolling year,
ranked by fees (inc VAT). Includes Card Present (incl. TTPi/TTPa) and Cash.

Definitions (consistent with account_box_office_cardpresent.py):
  - Box Office = PaymentType (uppercased, spaces stripped) contains
    "CARDPRESENT" or "CASH".
  - Revenue = sum of PaymentReceived (ticket value, excluding fees).
  - Fees    = sum of (BookingFee + CardFee + ProcessingFee + TicketFee), inc VAT.
  - Period  = last 365 days, Europe/London.

Output: cardpresent_top_clients.csv, ranked by fees_inc_vat descending.

Usage:
    python3 cardpresent_top_clients.py
"""

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.utils.data_loader import load_booking_data, filter_successful_transactions

UK_TZ = pytz.timezone("Europe/London")
FEE_COLS = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
OUTPUT_CSV = "cardpresent_top_clients.csv"


def is_box_office(payment_type: pd.Series) -> pd.Series:
    """True where PaymentType is a Box Office variant (Card Present or Cash)."""
    # Cast to str first: PaymentType is often loaded as a categorical.
    pt = payment_type.astype(str).fillna("").str.upper().str.replace(" ", "", regex=False)
    return pt.str.contains("CARDPRESENT", na=False) | pt.str.contains("CASH", na=False)


def main() -> None:
    # Load BookingDataAll (history to 1st of month) + BookingData (current month).
    all_df = load_booking_data(data_type="BookingDataAll")
    current_df = load_booking_data(data_type="BookingData")
    df = pd.concat([all_df, current_df], ignore_index=True)
    if "BookingTransactionId" in df.columns:
        df = df.drop_duplicates(subset=["BookingTransactionId"])

    df = filter_successful_transactions(df)

    # Last 365 days, Europe/London.
    df["TransactionDate"] = pd.to_datetime(df["TransactionDate"], errors="coerce", utc=True)
    df = df[df["TransactionDate"].notna()]
    df["txn_date"] = df["TransactionDate"].dt.tz_convert(UK_TZ).dt.date
    cutoff = (datetime.now(UK_TZ) - timedelta(days=365)).date()
    df = df[df["txn_date"] >= cutoff]

    # Box Office: Card Present (incl. TTPi/TTPa) and Cash.
    bo = df[is_box_office(df["PaymentType"])].copy()
    if bo.empty:
        sys.exit("No Box Office transactions found in the last 365 days.")

    bo["AccountId"] = pd.to_numeric(bo["AccountId"], errors="coerce")
    bo["PaymentReceived"] = pd.to_numeric(bo["PaymentReceived"], errors="coerce").fillna(0)
    bo["TicketQuantity"] = pd.to_numeric(bo["TicketQuantity"], errors="coerce").fillna(0)

    fees_present = [c for c in FEE_COLS if c in bo.columns]
    bo["_fees_inc_vat"] = bo[fees_present].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)

    # Account name: take the most recent non-null name per account.
    name_lookup = {}
    if "AccountName" in bo.columns:
        names = bo.dropna(subset=["AccountName"]).sort_values("TransactionDate")
        name_lookup = names.groupby("AccountId")["AccountName"].last().to_dict()

    # Industry / sub-industry: most recent non-null value per account.
    def _latest_lookup(col: str) -> dict:
        if col not in bo.columns:
            return {}
        vals = bo[bo[col].astype(str).str.strip().replace("nan", "") != ""]
        vals = vals.dropna(subset=[col]).sort_values("TransactionDate")
        return vals.groupby("AccountId")[col].last().to_dict()

    industry_lookup = _latest_lookup("Industry")
    subindustry_lookup = _latest_lookup("SubIndustry")

    grouped = bo.groupby("AccountId").agg(
        fees_inc_vat=("_fees_inc_vat", "sum"),
        revenue=("PaymentReceived", "sum"),
        tickets=("TicketQuantity", "sum"),
        transactions=("_fees_inc_vat", "count"),
        last_boxoffice_txn=("TransactionDate", "max"),
    ).reset_index()

    grouped["account_name"] = grouped["AccountId"].map(name_lookup).fillna("")
    grouped["industry"] = grouped["AccountId"].map(industry_lookup).fillna("")
    grouped["sub_industry"] = grouped["AccountId"].map(subindustry_lookup).fillna("")
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["last_boxoffice_txn"] = (
        grouped["last_boxoffice_txn"].dt.tz_convert(UK_TZ).dt.date.astype(str)
    )

    grouped = grouped.sort_values("fees_inc_vat", ascending=False)
    for col in ["fees_inc_vat", "revenue"]:
        grouped[col] = grouped[col].round(2)

    out = grouped[[
        "AccountId", "account_name", "industry", "sub_industry", "fees_inc_vat",
        "revenue", "tickets", "transactions", "last_boxoffice_txn",
    ]].rename(columns={"AccountId": "account_id"})

    out.to_csv(OUTPUT_CSV, index=False)
    print(f"Wrote {len(out):,} accounts to {OUTPUT_CSV}")
    print(f"  Period: {cutoff.isoformat()} to {datetime.now(UK_TZ).date().isoformat()} (rolling 365 days)")
    print(f"  Ranked by fees (inc VAT), descending.")


if __name__ == "__main__":
    main()
