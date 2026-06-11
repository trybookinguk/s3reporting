#!/usr/bin/env python3
"""
Box Office (Card Present) usage by account — rolling last 365 days.

Ranks every account by how much it has taken on Box Office *Card Present*
transactions over the rolling year, written to a CSV. Card Present only — Cash
is excluded.

Definitions (consistent with the rest of the codebase):
  - Box Office Card Present = PaymentType (uppercased, spaces stripped) contains
    "CARDPRESENT" (any spacing variant). Cash is NOT included.
  - Revenue   = sum of PaymentReceived (ticket value, excluding fees).
  - Fees      = sum of (BookingFee + CardFee + ProcessingFee + TicketFee),
    inc VAT — the total the organiser is charged.
  - Period    = last 365 days, Europe/London, relative to today.

Output: box_office_cardpresent_accounts.csv, ranked by fees (inc VAT) descending.

Usage:
    python3 account_box_office_cardpresent.py              # rank all accounts
    python3 account_box_office_cardpresent.py 19815        # just one account
    ACCOUNT_ID=19815 python3 account_box_office_cardpresent.py
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
OUTPUT_CSV = "box_office_cardpresent_accounts.csv"


def is_card_present(payment_type: pd.Series) -> pd.Series:
    """True where PaymentType is a Card Present variant (excludes Cash)."""
    # Cast to str first: PaymentType is often loaded as a categorical, and
    # fillna("") on a categorical raises unless "" is already a category.
    pt = payment_type.astype(str).fillna("").str.upper().str.replace(" ", "", regex=False)
    return pt.str.contains("CARDPRESENT", na=False)


def main() -> None:
    # Optional single-account filter (CLI arg or ACCOUNT_ID env var).
    account_filter = os.environ.get("ACCOUNT_ID")
    if len(sys.argv) > 1:
        account_filter = sys.argv[1]
    account_filter = int(account_filter) if account_filter else None

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
    today = datetime.now(UK_TZ).date()
    cutoff = (datetime.now(UK_TZ) - timedelta(days=365)).date()
    df = df[df["txn_date"] >= cutoff]

    # Card Present only.
    bo = df[is_card_present(df["PaymentType"])].copy()
    if account_filter is not None:
        bo["AccountId"] = pd.to_numeric(bo["AccountId"], errors="coerce")
        bo = bo[bo["AccountId"] == account_filter]

    if bo.empty:
        scope = f"account {account_filter}" if account_filter is not None else "any account"
        sys.exit(f"No Card Present transactions found for {scope} "
                 f"between {cutoff.isoformat()} and {today.isoformat()}.")

    bo["AccountId"] = pd.to_numeric(bo["AccountId"], errors="coerce")
    bo["PaymentReceived"] = pd.to_numeric(bo["PaymentReceived"], errors="coerce").fillna(0)
    bo["TicketQuantity"] = pd.to_numeric(bo["TicketQuantity"], errors="coerce").fillna(0)

    fees_present = [c for c in FEE_COLS if c in bo.columns]
    bo["_fees_inc_vat"] = bo[fees_present].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)

    # Most-recent non-null account name per account.
    name_lookup = {}
    if "AccountName" in bo.columns:
        names = bo.dropna(subset=["AccountName"]).sort_values("TransactionDate")
        name_lookup = names.groupby("AccountId")["AccountName"].last().to_dict()

    grouped = bo.groupby("AccountId").agg(
        fees_inc_vat=("_fees_inc_vat", "sum"),
        revenue=("PaymentReceived", "sum"),
        tickets=("TicketQuantity", "sum"),
        transactions=("_fees_inc_vat", "count"),
        last_txn=("TransactionDate", "max"),
    ).reset_index()

    grouped["account_name"] = grouped["AccountId"].map(name_lookup).fillna("")
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["last_txn"] = grouped["last_txn"].dt.tz_convert(UK_TZ).dt.date.astype(str)
    for col in ["fees_inc_vat", "revenue"]:
        grouped[col] = grouped[col].round(2)

    grouped = grouped.sort_values("fees_inc_vat", ascending=False)

    out = grouped[[
        "AccountId", "account_name", "fees_inc_vat", "revenue",
        "tickets", "transactions", "last_txn",
    ]].rename(columns={"AccountId": "account_id"})

    out.to_csv(OUTPUT_CSV, index=False)

    # Console summary.
    print(f"\nBox Office (Card Present), {cutoff.isoformat()} to {today.isoformat()} (rolling 365 days)")
    print(f"  Accounts: {len(out):,}")
    print(f"  Total fees (inc VAT): £{out['fees_inc_vat'].sum():,.2f}")
    print(f"  Total revenue:        £{out['revenue'].sum():,.2f}")
    print(f"  Total tickets:        {int(out['tickets'].sum()):,}")
    print(f"  Ranked by fees (inc VAT), descending. Written to {OUTPUT_CSV}\n")

    # Show the top of the table inline (or the single account, if filtered).
    head = out.head(15)
    print(f"  {'Account':<40} {'Fees inc VAT':>13} {'Tickets':>9}")
    print(f"  {'-' * 40} {'-' * 13} {'-' * 9}")
    for _, row in head.iterrows():
        label = f"{int(row['account_id'])} {row['account_name']}"[:40]
        print(f"  {label:<40} £{row['fees_inc_vat']:>11,.2f} {row['tickets']:>9,}")
    if len(out) > len(head):
        print(f"  ... and {len(out) - len(head):,} more in {OUTPUT_CSV}")
    print()


if __name__ == "__main__":
    main()
