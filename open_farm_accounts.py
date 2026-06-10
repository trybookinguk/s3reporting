#!/usr/bin/env python3
"""Find accounts running Open Farm Sunday-type events this year that sold tickets.

Two independent lists are produced, written to separate CSVs so they can be
cross-referenced:

1. Name match (open_farm_accounts.csv): events whose name matches Open Farm
   Sunday / OFS variants (see NAME_PATTERNS / OFS_TOKEN below), case-insensitive.

2. Industry match (open_farm_industry_accounts.csv): events from accounts whose
   Industry is "Agriculture" and SubIndustry is "Farming".

Both lists are restricted to events whose EventDate falls in the current
calendar year and that actually sold tickets (successful transactions with a
positive ticket quantity).

Run via: python3 open_farm_accounts.py
"""

import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.utils.config import UK_TZ
from modules.utils.data_loader import (
    filter_successful_transactions,
    load_booking_data,
    load_users,
)

# Case-insensitive substring patterns covering Open Farm Sunday naming variants:
# the national LEAF campaign, the Northern Ireland "Open Farm Weekend" sibling,
# and common shortenings event organisers use.
NAME_PATTERNS = [
    "open farm sunday",
    "open farm weekend",
    "open farm day",
    "open farm",
    "farm sunday",
]
# "OFS" matched only as a standalone token to avoid false hits ("playoffs" etc.).
OFS_TOKEN = re.compile(r"\bOFS\b", re.IGNORECASE)

INDUSTRY = "Agriculture"
SUBINDUSTRY = "Farming"


def matches_open_farm(name: str) -> bool:
    """Return True if an event name refers to an Open Farm Sunday-type event."""
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if any(pattern in lowered for pattern in NAME_PATTERNS):
        return True
    return bool(OFS_TOKEN.search(name))


def load_ticketed_events_this_year() -> pd.DataFrame:
    """Load all this-year, ticket-selling events (successful transactions)."""
    current_year = pd.Timestamp.now(tz=UK_TZ).year

    # BookingDataAll holds history to the 1st of the month; BookingData is the
    # current month, updated daily. Union and de-duplicate on transaction id.
    history = load_booking_data(data_type="BookingDataAll")
    current = load_booking_data(data_type="BookingData")
    df = pd.concat([history, current], ignore_index=True)
    if "BookingTransactionId" in df.columns:
        df = df.drop_duplicates(subset=["BookingTransactionId"], keep="last")

    # Only successful transactions count as a sale.
    df = filter_successful_transactions(df)

    # Restrict to events that ran this calendar year — Open Farm Sunday is a
    # dated, seasonal event, so we filter on EventDate, not transaction date.
    df["EventDate"] = pd.to_datetime(df["EventDate"], errors="coerce", utc=True)
    df = df[df["EventDate"].dt.tz_convert(UK_TZ).dt.year == current_year]

    # Require tickets actually sold.
    df["TicketQuantity"] = pd.to_numeric(df["TicketQuantity"], errors="coerce").fillna(0)
    df = df[df["TicketQuantity"] > 0]

    return df


def load_account_owners() -> pd.DataFrame:
    """Return one account-owner row per AccountId, with a uk_{UserId} Vero ID.

    Owner = non-deleted users row whose RoleName is "AccountOwner" (matching the
    convention used by mailshake_acquisition.py / event_completion_reminders.py).
    The uk_{UserId} format mirrors the Vero identifier built elsewhere.
    """
    users = load_users()
    users["IsDeleted"] = users["IsDeleted"].astype(str).str.strip()
    owners = users[
        (users["RoleName"] == "AccountOwner") & (~users["IsDeleted"].isin(["1", "True"]))
    ].copy()

    owners["AccountId"] = pd.to_numeric(owners["AccountId"], errors="coerce").astype("Int64")
    owners = owners[owners["AccountId"].notna()]

    owners["OwnerVeroId"] = "uk_" + owners["UserId"].astype(str)
    owners["OwnerEmail"] = owners["Username"].astype(str).str.strip()

    cols = ["AccountId", "OwnerVeroId", "OwnerEmail"]
    # Include a name column if the users file carries one (not all exports do).
    for name_col in ("FullName", "FirstName", "LastName", "Name"):
        if name_col in owners.columns:
            owners[f"Owner{name_col}"] = owners[name_col].astype(str).str.strip()
            cols.append(f"Owner{name_col}")

    # One owner per account; keep the first if a duplicate slips through.
    return owners[cols].drop_duplicates(subset=["AccountId"], keep="first")


def summarise(df: pd.DataFrame, owners: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per account + event and attach the account owner."""
    summary = (
        df.groupby(["AccountId", "AccountName", "EventName"])
        .agg(
            EventDate=("EventDate", "max"),
            TicketsSold=("TicketQuantity", "sum"),
            Transactions=("BookingTransactionId", "count"),
            TicketRevenue=("PaymentReceived", "sum"),
        )
        .reset_index()
        .sort_values(["AccountName", "EventDate"])
    )
    summary["EventDate"] = summary["EventDate"].dt.tz_convert(UK_TZ).dt.date

    summary["AccountId"] = pd.to_numeric(summary["AccountId"], errors="coerce").astype("Int64")
    summary = summary.merge(owners, on="AccountId", how="left")
    return summary


def write_list(df: pd.DataFrame, owners: pd.DataFrame, filename: str, label: str) -> None:
    """Print a summary and write it to CSV, or report that nothing matched."""
    if df.empty:
        print(f"[{label}] No matching ticketed events found.\n")
        return
    summary = summarise(df, owners)
    n_accounts = summary["AccountId"].nunique()
    n_events = len(summary)
    print(f"[{label}] {n_accounts} account(s), {n_events} event(s):\n")
    print(summary.to_string(index=False))
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    summary.to_csv(out_path, index=False)
    print(f"\n[{label}] Written to {out_path}\n")


def main() -> None:
    current_year = pd.Timestamp.now(tz=UK_TZ).year
    print(f"Open Farm Sunday analysis for {current_year}\n{'=' * 48}\n")

    df = load_ticketed_events_this_year()
    owners = load_account_owners()

    # List 1 — events matched by name.
    name_match = df[df["EventName"].apply(matches_open_farm)]
    write_list(name_match, owners, "open_farm_accounts.csv", "Name match")

    # List 2 — events from Agriculture / Farming accounts.
    industry_match = df[
        (df["Industry"].astype(str).str.strip().str.casefold() == INDUSTRY.casefold())
        & (df["SubIndustry"].astype(str).str.strip().str.casefold() == SUBINDUSTRY.casefold())
    ]
    write_list(industry_match, owners, "open_farm_industry_accounts.csv", "Agriculture/Farming")


if __name__ == "__main__":
    main()
