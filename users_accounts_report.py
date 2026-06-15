#!/usr/bin/env python3
"""
Combined users + accounts report.

Joins the TryBooking Users and Accounts S3 snapshots into a single
spreadsheet — one row per user, carrying that user's account-level
details alongside their identity and role.

Output columns:
    User ID            — Users.UserId
    Vero ID            — "uk_" + UserId
    User Name          — Users.FirstName + " " + Users.LastName
    Account Role       — Users.RoleName (the user's role in the account)
    Account ID         — Users.AccountId (= Accounts.Id)
    Account Name       — Accounts.AccountName
    Account Industry   — Accounts.Industry
    Account Sub Industry — Accounts.SubIndustry

Idempotent: each run regenerates the full report from the latest monthly
snapshots. Both load_users() and load_accounts() fall back to the previous
month's file on the 1st (the new month's file isn't published until the 2nd).

Usage:
    python3 users_accounts_report.py                 # write CSV to REPORTS_DIR
    python3 users_accounts_report.py --out path.csv  # write to a specific path
"""

import argparse
import logging
import os
import sys

import pandas as pd

from modules.utils.config import REPORTS_DIR
from modules.utils.data_loader import load_accounts, load_users

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("users-accounts-report")

# Final column order for the spreadsheet.
OUTPUT_COLUMNS = [
    "User ID",
    "Vero ID",
    "User Name",
    "Account Role",
    "Account ID",
    "Account Name",
    "Account Industry",
    "Account Sub Industry",
]


def build_report() -> pd.DataFrame:
    """Build the combined users + accounts DataFrame."""
    users = load_users()
    accounts = load_accounts()
    log.info("Loaded %d users, %d accounts", len(users), len(accounts))

    # --- Users side ---
    # Drop deleted users — they shouldn't appear in the export.
    if "IsDeleted" in users.columns:
        deleted = pd.to_numeric(users["IsDeleted"], errors="coerce").fillna(0)
        before = len(users)
        users = users[deleted == 0].copy()
        log.info("Dropped %d deleted users", before - len(users))

    # load_users() standardises AccountId, but UserId/RoleName/names are raw.
    u = pd.DataFrame()
    u["_user_id"] = pd.to_numeric(users["UserId"], errors="coerce").astype("Int64")
    u["_account_id"] = pd.to_numeric(users["AccountId"], errors="coerce").astype("Int64")
    u["Account Role"] = users["RoleName"].astype("string").str.strip()

    first = users["FirstName"].astype("string").fillna("").str.strip()
    last = users["LastName"].astype("string").fillna("").str.strip()
    u["User Name"] = (first + " " + last).str.strip()

    # Drop rows with no usable user id — they can't be keyed or exported.
    u = u[u["_user_id"].notna()].copy()

    # --- Accounts side ---
    # load_accounts() copies Id -> AccountId, so AccountId is reliable here.
    a = pd.DataFrame()
    a["_account_id"] = pd.to_numeric(accounts["AccountId"], errors="coerce").astype("Int64")
    a["Account Name"] = accounts["AccountName"].astype("string")
    a["Account Industry"] = accounts["Industry"].astype("string")
    a["Account Sub Industry"] = accounts["SubIndustry"].astype("string")
    a = a[a["_account_id"].notna()].drop_duplicates(subset="_account_id")

    # --- Join (many users -> one account) ---
    merged = u.merge(a, on="_account_id", how="left")

    n_unmatched = int(merged["Account Name"].isna().sum())
    if n_unmatched:
        log.warning(
            "%d users have no matching account row (account fields left blank)",
            n_unmatched,
        )

    # --- Derived + final shaping ---
    merged["User ID"] = merged["_user_id"].astype("Int64")
    merged["Vero ID"] = "uk_" + merged["_user_id"].astype("string")
    merged["Account ID"] = merged["_account_id"].astype("Int64")

    report = merged[OUTPUT_COLUMNS].sort_values(
        ["Account ID", "User ID"], na_position="last"
    ).reset_index(drop=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path (default: REPORTS_DIR/users_accounts.csv)",
    )
    args = parser.parse_args()

    out_path = args.out or os.path.join(REPORTS_DIR, "users_accounts.csv")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    report = build_report()
    report.to_csv(out_path, index=False)
    log.info("Wrote %d rows to %s", len(report), out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
