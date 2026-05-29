#!/usr/bin/env python3
"""One-off: clear stale tier labels from every Zoho account.

After flipping TIER_SYSTEM from v1 to v2, the daily zoho_tiers.py only
upserts accounts with successful bookings (~8,413 of ~20,728). The rest still
hold v1 labels (Key Account / High Value / NIL / Tier 1..4 in v1's inverted
meaning) which now collide with v2's label space.

This script clears EVERY account's tier fields to a known-empty state. Run
zoho_tiers.py immediately after to repopulate the active accounts; the rest
remain at Nil.

Defaults to dry-run. Pass --apply to write.
"""
import argparse
import sys
import requests

from modules.utils.config import ZOHO_DOMAIN
from modules.utils.zoho_api import get_access_token, upsert_to_zoho

CLEAR_VALUES = {
    "Current_Tier": "Nil",
    "Previous_Tier": "Nil",
    "Tier_Movement": "No Change",
}
TIER_FIELDS = list(CLEAR_VALUES.keys())


def fetch_all_accounts(token):
    """Page through every account, pulling only the tier fields we care about."""
    accounts = []
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    fields = ",".join(["Account_Name", *TIER_FIELDS])

    while True:
        resp = requests.get(
            f"{ZOHO_DOMAIN}/crm/v2/Accounts",
            headers=headers,
            params={"page": page, "per_page": 200, "fields": fields},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        if not data:
            break
        accounts.extend(data)
        print(f"  Fetched page {page}: {len(data)} accounts (running total: {len(accounts)})")
        if not body.get("info", {}).get("more_records"):
            break
        page += 1

    return accounts


def needs_clearing(account):
    """True if any tier field has a non-empty value."""
    for field in TIER_FIELDS:
        val = account.get(field)
        if val is not None and str(val).strip() != "":
            return True
    return False


def summarise(targets):
    """Print breakdown of current Current_Tier values among accounts to clear."""
    counts = {}
    for acc in targets:
        label = acc.get("Current_Tier") or "(empty)"
        counts[label] = counts.get(label, 0) + 1

    print("\nCurrent_Tier distribution among accounts to clear:")
    for label, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {label!r:20s} {n:>6,}")


def build_payload(targets):
    return [
        {"Account_Name": str(acc["Account_Name"]), **CLEAR_VALUES}
        for acc in targets
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually upsert the clearing payload to Zoho. Without this flag the "
             "script only previews what would change.",
    )
    args = parser.parse_args()

    print("Authenticating with Zoho...")
    token = get_access_token()

    print("Fetching all accounts from Zoho...")
    accounts = fetch_all_accounts(token)
    print(f"\nTotal accounts in Zoho: {len(accounts):,}")

    targets = [a for a in accounts if needs_clearing(a)]
    print(f"Accounts with any tier field set: {len(targets):,}")
    print(f"Accounts already empty (skipped): {len(accounts) - len(targets):,}")

    if not targets:
        print("\nNothing to clear. Exiting.")
        return

    summarise(targets)

    print("\nSample of records to clear (first 10):")
    for acc in targets[:10]:
        print(
            f"  Account_Name={acc.get('Account_Name')!s:>10}  "
            f"Current_Tier={acc.get('Current_Tier')!r:18}  "
            f"Previous_Tier={acc.get('Previous_Tier')!r:18}  "
            f"Tier_Movement={acc.get('Tier_Movement')!r}"
        )

    payload = build_payload(targets)

    if not args.apply:
        print(
            f"\n[DRY-RUN] Would upsert {len(payload):,} accounts to "
            f"Current_Tier='Nil', Previous_Tier='Nil', Tier_Movement='No Change'."
        )
        print("Re-run with --apply to write these changes to Zoho.")
        return

    print(
        f"\n--apply was passed. About to upsert {len(payload):,} accounts to clear "
        f"their tier fields."
    )
    confirm = input("Type 'CLEAR' to proceed: ").strip()
    if confirm != "CLEAR":
        print("Aborted.")
        sys.exit(1)

    print("\nUpserting...")
    upsert_to_zoho(token, payload, debug=True)
    print("\nDone. Next step: run `python3 zoho_tiers.py` to repopulate v2 tiers "
          "for active accounts.")


if __name__ == "__main__":
    main()
