#!/usr/bin/env python3
"""
List all clients that look theatre-related, to size the opportunity in the base.

Scans every account in the booking data and flags those whose account name
contains a theatre-related keyword, OR whose Industry/SubIndustry points at
theatre / performing arts. This is a candidate list for outreach, not a
definitive segmentation — name matching is deliberately broad.

Definitions:
  - Theatre by name = AccountName (uppercased) contains any theatre keyword
    (see THEATRE_NAME_KEYWORDS), e.g. "THEATRE", "PLAYHOUSE", "DRAMA".
  - Theatre by industry = Industry or SubIndustry (uppercased) contains any
    of THEATRE_INDUSTRY_KEYWORDS, e.g. "THEATRE", "PERFORMING ARTS".
  - Revenue = sum of PaymentReceived (ticket value, excluding fees).
  - Fees    = sum of (BookingFee + CardFee + ProcessingFee + TicketFee), inc VAT.
  - Period  = last 365 days, Europe/London.

Output: theatre_clients.csv, ranked by fees_inc_vat descending.

Usage:
    python3 theatre_clients.py
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
OUTPUT_CSV = "theatre_clients.csv"

# Broad on purpose: better to surface a false positive for a human to dismiss
# than to miss a genuine theatre. British spellings included.
THEATRE_NAME_KEYWORDS = [
    "THEATRE", "THEATER", "PLAYHOUSE", "DRAMA", "DRAMATIC", "PANTOMIME",
    "PANTO", "STAGE", "AMDRAM", "AM DRAM", "REPERTORY", "REP THEATRE",
    "OPERATIC", "OPERA", "MUSICAL", "PRODUCTIONS", "PERFORMING ARTS",
    "PLAYERS", "STAGECRAFT", "AUDITORIUM", "ARTS CENTRE", "ARTS CENTER",
]

THEATRE_INDUSTRY_KEYWORDS = [
    "THEATRE", "THEATER", "PERFORMING ARTS", "DRAMA", "OPERA",
]


def _contains_any(series: pd.Series, keywords: list) -> pd.Series:
    """True where the (uppercased) string contains any of the keywords."""
    text = series.astype(str).fillna("").str.upper()
    mask = pd.Series(False, index=series.index)
    for kw in keywords:
        mask |= text.str.contains(kw, na=False, regex=False)
    return mask


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

    # Theatre candidates: matched on name OR industry/sub-industry.
    name_match = _contains_any(df["AccountName"], THEATRE_NAME_KEYWORDS)
    industry_match = pd.Series(False, index=df.index)
    for col in ["Industry", "SubIndustry"]:
        if col in df.columns:
            industry_match |= _contains_any(df[col], THEATRE_INDUSTRY_KEYWORDS)

    theatre = df[name_match | industry_match].copy()
    if theatre.empty:
        sys.exit("No theatre-related accounts found in the last 365 days.")

    # Record why each row matched, so the output is auditable.
    theatre["_matched_name"] = name_match[theatre.index]
    theatre["_matched_industry"] = industry_match[theatre.index]

    theatre["AccountId"] = pd.to_numeric(theatre["AccountId"], errors="coerce")
    theatre["PaymentReceived"] = pd.to_numeric(theatre["PaymentReceived"], errors="coerce").fillna(0)
    theatre["TicketQuantity"] = pd.to_numeric(theatre["TicketQuantity"], errors="coerce").fillna(0)

    fees_present = [c for c in FEE_COLS if c in theatre.columns]
    theatre["_fees_inc_vat"] = (
        theatre[fees_present].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
    )

    # Latest non-null value per account for descriptive fields.
    def _latest_lookup(col: str) -> dict:
        if col not in theatre.columns:
            return {}
        vals = theatre[theatre[col].astype(str).str.strip().replace("nan", "") != ""]
        vals = vals.dropna(subset=[col]).sort_values("TransactionDate")
        return vals.groupby("AccountId")[col].last().to_dict()

    name_lookup = _latest_lookup("AccountName")
    industry_lookup = _latest_lookup("Industry")
    subindustry_lookup = _latest_lookup("SubIndustry")

    grouped = theatre.groupby("AccountId").agg(
        fees_inc_vat=("_fees_inc_vat", "sum"),
        revenue=("PaymentReceived", "sum"),
        tickets=("TicketQuantity", "sum"),
        transactions=("_fees_inc_vat", "count"),
        matched_name=("_matched_name", "any"),
        matched_industry=("_matched_industry", "any"),
        last_txn=("TransactionDate", "max"),
    ).reset_index()

    grouped["account_name"] = grouped["AccountId"].map(name_lookup).fillna("")
    grouped["industry"] = grouped["AccountId"].map(industry_lookup).fillna("")
    grouped["sub_industry"] = grouped["AccountId"].map(subindustry_lookup).fillna("")
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["last_txn"] = grouped["last_txn"].dt.tz_convert(UK_TZ).dt.date.astype(str)

    grouped = grouped.sort_values("fees_inc_vat", ascending=False)
    for col in ["fees_inc_vat", "revenue"]:
        grouped[col] = grouped[col].round(2)

    out = grouped[[
        "AccountId", "account_name", "industry", "sub_industry",
        "matched_name", "matched_industry", "fees_inc_vat", "revenue",
        "tickets", "transactions", "last_txn",
    ]].rename(columns={"AccountId": "account_id"})

    out.to_csv(OUTPUT_CSV, index=False)
    print(f"Wrote {len(out):,} theatre-related accounts to {OUTPUT_CSV}")
    print(f"  Period: {cutoff.isoformat()} to {datetime.now(UK_TZ).date().isoformat()} (rolling 365 days)")
    print(f"  Matched by name: {int(grouped['matched_name'].sum()):,}; "
          f"by industry: {int(grouped['matched_industry'].sum()):,}.")
    print(f"  Ranked by fees (inc VAT), descending.")


if __name__ == "__main__":
    main()
