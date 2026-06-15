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

By default the CSV is also uploaded to SharePoint at
"Platform Data/Users and Accounts/users_accounts.csv", overwriting the
previous day's file in place (so the folder only ever holds the latest).

Usage:
    python3 users_accounts_report.py                 # build CSV + upload to SharePoint
    python3 users_accounts_report.py --out path.csv  # write CSV to a specific path
    python3 users_accounts_report.py --no-upload     # build CSV only, skip SharePoint
"""

import argparse
import logging
import os
import sys

import pandas as pd

from modules.utils.config import REPORTS_DIR, SHAREPOINT_DRIVE_ID
from modules.utils.data_loader import load_accounts, load_users
from modules.utils.sharepoint import authenticate_graph, upload

# SharePoint destination. Fixed filename inside a dedicated folder; each daily
# upload overwrites the previous file in place, so the folder always holds
# exactly the latest snapshot.
SHAREPOINT_FOLDER = "Platform Data/Users and Accounts"
SHAREPOINT_FILENAME = "users_accounts.csv"

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


def upload_to_sharepoint(csv_bytes: bytes) -> bool:
    """Upload the CSV to SharePoint, overwriting the previous day's file in place.

    Returns True on success. Raises on missing config so a misconfigured cron
    run fails loudly (the wrapper emails on non-zero exit) rather than silently
    skipping the publish.
    """
    if not SHAREPOINT_DRIVE_ID:
        raise RuntimeError(
            "SHAREPOINT_DRIVE_ID not set — cannot upload (source the .env first)."
        )
    token = authenticate_graph()
    if not token:
        raise RuntimeError("Microsoft Graph authentication failed.")

    ok = upload(
        token,
        SHAREPOINT_DRIVE_ID,
        SHAREPOINT_FILENAME,
        csv_bytes,
        folder=SHAREPOINT_FOLDER,
    )
    if ok:
        log.info(
            "Uploaded to SharePoint: %s/%s (overwrote previous)",
            SHAREPOINT_FOLDER,
            SHAREPOINT_FILENAME,
        )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path (default: REPORTS_DIR/users_accounts.csv)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Build the CSV locally only; skip the SharePoint upload.",
    )
    args = parser.parse_args()

    out_path = args.out or os.path.join(REPORTS_DIR, "users_accounts.csv")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    report = build_report()
    csv_bytes = report.to_csv(index=False).encode("utf-8")

    with open(out_path, "wb") as f:
        f.write(csv_bytes)
    log.info("Wrote %d rows to %s", len(report), out_path)

    if args.no_upload:
        log.info("--no-upload set; skipping SharePoint publish.")
    elif not upload_to_sharepoint(csv_bytes):
        log.error("SharePoint upload failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
