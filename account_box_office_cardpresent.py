#!/usr/bin/env python3
"""
Box Office (Card Present) totals for a single account, last 365 days.

Reports how much one account has taken on Box Office *Card Present*
transactions over the rolling year. Card Present only — Cash is excluded.

Definitions (consistent with the rest of the codebase):
  - Box Office Card Present = PaymentType (uppercased, stripped) contains
    "CARD PRESENT" or "CARDPRESENT" (any spacing variant). Cash is NOT included.
  - Revenue   = sum of PaymentReceived (ticket value, excluding fees).
  - Fees      = sum of (BookingFee + CardFee + ProcessingFee + TicketFee) / 1.20,
    i.e. ex-VAT, matching generate_dashboard_data._prepare_bookings.
  - Period    = last 365 days, Europe/London, relative to today.

Usage:
    ACCOUNT_ID=19815 python3 account_box_office_cardpresent.py
    python3 account_box_office_cardpresent.py 19815
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


def is_card_present(payment_type: pd.Series) -> pd.Series:
    """True where PaymentType is a Card Present variant (excludes Cash)."""
    # Cast to str first: PaymentType is often loaded as a categorical, and
    # fillna("") on a categorical raises unless "" is already a category.
    pt = payment_type.astype(str).fillna("").str.upper().str.replace(" ", "", regex=False)
    return pt.str.contains("CARDPRESENT", na=False)


def main() -> None:
    account_id = os.environ.get("ACCOUNT_ID")
    if len(sys.argv) > 1:
        account_id = sys.argv[1]
    if not account_id:
        sys.exit("Provide an account ID: ACCOUNT_ID=19815 python3 account_box_office_cardpresent.py")
    account_id = int(account_id)

    # Load BookingDataAll (history to 1st of month) + BookingData (current month).
    all_df = load_booking_data(data_type="BookingDataAll")
    current_df = load_booking_data(data_type="BookingData")
    df = pd.concat([all_df, current_df], ignore_index=True)
    if "BookingTransactionId" in df.columns:
        df = df.drop_duplicates(subset=["BookingTransactionId"])

    df = filter_successful_transactions(df)

    # Filter to the account.
    df["AccountId"] = pd.to_numeric(df["AccountId"], errors="coerce")
    df = df[df["AccountId"] == account_id]
    if df.empty:
        sys.exit(f"No successful transactions found for account {account_id}.")

    # Since 6 May 2026 (inclusive), Europe/London.
    df["TransactionDate"] = pd.to_datetime(df["TransactionDate"], errors="coerce", utc=True)
    df = df[df["TransactionDate"].notna()]
    df["txn_date"] = df["TransactionDate"].dt.tz_convert(UK_TZ).dt.date
    cutoff = datetime(2026, 5, 6).date()
    df = df[df["txn_date"] >= cutoff]

    # Card Present only.
    bo = df[is_card_present(df["PaymentType"])].copy()

    account_name = ""
    if "AccountName" in df.columns and not df["AccountName"].dropna().empty:
        account_name = str(df["AccountName"].dropna().iloc[0])

    if bo.empty:
        print(f"Account {account_id} ({account_name}) — no Card Present transactions since {cutoff.isoformat()}.")
        return

    revenue = pd.to_numeric(bo["PaymentReceived"], errors="coerce").fillna(0).sum()
    tickets = pd.to_numeric(bo["TicketQuantity"], errors="coerce").fillna(0).sum()
    txns = len(bo)

    fees_present = [c for c in FEE_COLS if c in bo.columns]
    fees_inc_vat = bo[fees_present].apply(pd.to_numeric, errors="coerce").fillna(0).sum().sum()
    fees_ex_vat = fees_inc_vat / 1.20

    # Annualised projection: scale the observed window up to a full year.
    # Days elapsed = cutoff..today inclusive, so it stays correct whenever run.
    today = datetime.now(UK_TZ).date()
    days_elapsed = (today - cutoff).days + 1
    factor = 365 / days_elapsed

    print(f"\nAccount {account_id} ({account_name}) — Box Office (Card Present), since 6 May 2026")
    print(f"  (from {cutoff.isoformat()} to {today.isoformat()} — {days_elapsed} days)\n")
    print(f"  Revenue (PaymentReceived): £{revenue:,.2f}")
    print(f"  Fees (inc VAT):            £{fees_inc_vat:,.2f}")
    print(f"  Fees (ex-VAT):             £{fees_ex_vat:,.2f}")
    print(f"  Tickets:                   {int(tickets):,}")
    print(f"  Transactions:              {txns:,}\n")
    print(f"  Annualised (×{factor:.2f}, {days_elapsed} days → 365):")
    print(f"    Revenue (PaymentReceived): £{revenue * factor:,.2f}")
    print(f"    Fees (inc VAT):            £{fees_inc_vat * factor:,.2f}")
    print(f"    Fees (ex-VAT):             £{fees_ex_vat * factor:,.2f}\n")


if __name__ == "__main__":
    main()
