#!/usr/bin/env python3
"""
Check whether a list of client/school names already exist as accounts
in the TryBooking UK Accounts S3 report.

Uses normalised exact matching first, then fuzzy matching (difflib) for
the remainder. Results are categorised as:

  - matched : exact match after normalisation (case, punctuation, whitespace,
              and common suffixes like "School", "Ltd", "GDST" stripped)
  - review  : fuzzy match with confidence >= REVIEW_THRESHOLD but < STRONG_THRESHOLD
              (or a strong fuzzy match — still worth a human glance)
  - none    : no candidate above REVIEW_THRESHOLD

Usage:
    python3 check_account_names.py                       # reads schools.txt
    python3 check_account_names.py --input names.txt
    python3 check_account_names.py --output results.csv

Output CSV columns:
    input_name, match_status, confidence,
    AccountId, AccountName, AccountStatus, AccountPostcode,
    Industry, SubIndustry, LastAccountTransactionDate, LastLogIn

Rows are sorted by match_status (matched, matched_multiple, review, none)
then by confidence descending.
"""

import argparse
import csv
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from datetime import datetime, timedelta, timezone

import pandas as pd

from modules.utils.data_loader import (
    filter_successful_transactions,
    load_accounts,
    load_booking_data,
)


STRONG_THRESHOLD = 90  # >= this and we call it a confident fuzzy match
REVIEW_THRESHOLD = 75  # >= this but < STRONG we flag as "review"

# Noise words/suffixes stripped during normalisation for more robust matching.
# We keep distinctive tokens (Prep, High, Girls) since they disambiguate schools.
NOISE_TOKENS = {
    "the", "school", "schools", "college", "academy",
    "ltd", "limited", "enterprises", "enterprise",
    "trust", "foundation", "facilities",
    "gdst",
}


def normalise(name: str) -> str:
    """Lowercase, strip punctuation, drop noise tokens, collapse whitespace."""
    if not isinstance(name, str):
        return ""
    s = name.lower()
    # Replace punctuation with space
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace
    tokens = [t for t in s.split() if t and t not in NOISE_TOKENS]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """Return a 0-100 similarity score between two normalised strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100


def load_input_names(path: Path) -> list[str]:
    names = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and name.lower() != "school name":
                names.append(name)
    return names


def build_account_index(accounts: pd.DataFrame) -> pd.DataFrame:
    """Return a frame with original columns plus a normalised name column."""
    # Identify the account name column
    name_col = None
    for candidate in ("AccountName", "Name", "accountName"):
        if candidate in accounts.columns:
            name_col = candidate
            break
    if name_col is None:
        raise RuntimeError(
            f"Could not find an account name column. Available: {list(accounts.columns)}"
        )

    df = accounts.copy()
    df["_norm_name"] = df[name_col].astype(str).map(normalise)
    df.attrs["name_col"] = name_col
    return df


def match_one(
    input_name: str,
    accounts: pd.DataFrame,
    norm_lookup: dict[str, list[int]],
) -> dict:
    """Match a single input name against the accounts frame."""
    name_col = accounts.attrs["name_col"]
    norm = normalise(input_name)

    # 1. Exact match on normalised form
    if norm and norm in norm_lookup:
        idxs = norm_lookup[norm]
        row = accounts.iloc[idxs[0]]
        return {
            "input_name": input_name,
            "match_status": "matched" if len(idxs) == 1 else "matched_multiple",
            "confidence": 100,
            "AccountId": row.get("AccountId", ""),
            "AccountName": row.get(name_col, ""),
            "AccountStatus": row.get("AccountStatus", ""),
            "AccountPostcode": row.get("AccountPostcode", row.get("Postcode", "")),
            "Industry": row.get("Industry", ""),
            "SubIndustry": row.get("SubIndustry", ""),
            "LastAccountTransactionDate": row.get("LastAccountTransactionDate", ""),
            "LastLogIn": row.get("LastLogIn", ""),
            "candidate_count": len(idxs),
        }

    # 2. Fuzzy — score against every unique normalised account name.
    best_score = 0.0
    best_idx = None
    for cand_norm, idxs in norm_lookup.items():
        score = similarity(norm, cand_norm)
        if score > best_score:
            best_score = score
            best_idx = idxs[0]

    if best_idx is None or best_score < REVIEW_THRESHOLD:
        return {
            "input_name": input_name,
            "match_status": "none",
            "confidence": round(best_score, 1),
            "AccountId": "",
            "AccountName": "",
            "AccountStatus": "",
            "AccountPostcode": "",
            "Industry": "",
            "SubIndustry": "",
            "LastAccountTransactionDate": "",
            "LastLogIn": "",
            "candidate_count": 0,
        }

    row = accounts.iloc[best_idx]
    status = "matched" if best_score >= STRONG_THRESHOLD else "review"
    return {
        "input_name": input_name,
        "match_status": status,
        "confidence": round(best_score, 1),
        "AccountId": row.get("AccountId", ""),
        "AccountName": row.get(name_col, ""),
        "AccountStatus": row.get("AccountStatus", ""),
        "AccountPostcode": row.get("AccountPostcode", row.get("Postcode", "")),
        "Industry": row.get("Industry", ""),
        "SubIndustry": row.get("SubIndustry", ""),
        "LastAccountTransactionDate": row.get("LastAccountTransactionDate", ""),
        "LastLogIn": row.get("LastLogIn", ""),
        "candidate_count": 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="schools.txt", help="Input file, one name per line")
    parser.add_argument("--output", default="account_name_check.csv", help="Output CSV path")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    names = load_input_names(input_path)
    print(f"Loaded {len(names)} input names from {input_path}")

    print("Loading Accounts report from S3...")
    accounts = load_accounts()
    print(f"Loaded {len(accounts):,} accounts")

    accounts = build_account_index(accounts)

    # Group accounts by normalised name for O(1) exact lookup and
    # a smaller unique set to fuzzy-score against.
    norm_lookup: dict[str, list[int]] = {}
    for pos, norm in enumerate(accounts["_norm_name"].tolist()):
        if not norm:
            continue
        norm_lookup.setdefault(norm, []).append(pos)

    print(f"Unique normalised account names: {len(norm_lookup):,}")
    print(f"Matching {len(names)} input names...")

    results = [match_one(name, accounts, norm_lookup) for name in names]

    # Enrich confident matches with most-recent transaction date from BookingData(All)
    matched_account_ids = {
        r["AccountId"] for r in results
        if r["match_status"] in ("matched", "matched_multiple") and r["AccountId"] != ""
    }
    last_txn_by_account: dict = {}
    if matched_account_ids:
        print(f"\nLoading booking data to check activity for {len(matched_account_ids)} accounts...")
        try:
            ba = load_booking_data(data_type="BookingDataAll")
            ba = filter_successful_transactions(ba)
            ba = ba[ba["AccountId"].isin(matched_account_ids)]
            for acc_id, max_date in ba.groupby("AccountId")["TransactionDate"].max().items():
                last_txn_by_account[acc_id] = max_date
        except Exception as e:
            print(f"  Warning: BookingDataAll load failed: {e}")

        try:
            bd = load_booking_data(data_type="BookingData")
            bd = filter_successful_transactions(bd)
            bd = bd[bd["AccountId"].isin(matched_account_ids)]
            for acc_id, max_date in bd.groupby("AccountId")["TransactionDate"].max().items():
                prev = last_txn_by_account.get(acc_id)
                if prev is None or (pd.notna(max_date) and max_date > prev):
                    last_txn_by_account[acc_id] = max_date
        except Exception as e:
            print(f"  Warning: BookingData load failed: {e}")

        # Apply to results + derive activity flag
        now = datetime.now(timezone.utc)
        twelve_months_ago = now - timedelta(days=365)
        for r in results:
            acc_id = r["AccountId"]
            if not acc_id:
                r["MostRecentTransaction"] = ""
                r["Activity"] = ""
                continue
            last = last_txn_by_account.get(acc_id)
            if last is None or pd.isna(last):
                r["MostRecentTransaction"] = ""
                r["Activity"] = "no_bookings"
            else:
                r["MostRecentTransaction"] = last.strftime("%Y-%m-%d")
                r["Activity"] = "active_12m" if last >= twelve_months_ago else "dormant"
    else:
        for r in results:
            r["MostRecentTransaction"] = ""
            r["Activity"] = ""

    # Sort: match_status (matched → matched_multiple → review → none), then confidence desc
    status_order = {"matched": 0, "matched_multiple": 1, "review": 2, "none": 3}
    results.sort(key=lambda r: (status_order.get(r["match_status"], 99), -float(r["confidence"] or 0)))

    # Write CSV
    output_path = Path(args.output)
    fieldnames = [
        "input_name", "match_status", "confidence",
        "AccountId", "AccountName", "AccountStatus", "AccountPostcode",
        "Industry", "SubIndustry",
        "MostRecentTransaction", "Activity",
        "LastAccountTransactionDate", "LastLogIn",
        "candidate_count",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Summary
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["match_status"]] = by_status.get(r["match_status"], 0) + 1

    print("\n=== Summary ===")
    for status in ("matched", "matched_multiple", "review", "none"):
        if status in by_status:
            print(f"  {status:<18} {by_status[status]:>4}")

    # Activity breakdown for confident matches
    active_statuses = {"Activated"}
    confident = [r for r in results if r["match_status"] in ("matched", "matched_multiple")]
    active = [r for r in confident if r["AccountStatus"] in active_statuses]
    inactive = [r for r in confident if r["AccountStatus"] and r["AccountStatus"] not in active_statuses]
    print(f"\n  Of confident matches: {len(active)} Activated, {len(inactive)} non-Activated")

    print(f"\nResults written to {output_path}")

    # Show inactive confident matches — these are the ones likely to surprise
    if inactive:
        print("\n=== Confident matches that are NOT Activated ===")
        for r in inactive:
            print(f"  [{r['AccountStatus']:<11}] {r['input_name']!r} -> {r['AccountName']!r} "
                  f"(last txn: {r.get('MostRecentTransaction') or 'never'})")

    # Activity (booking-based) breakdown
    activity_counts: dict[str, int] = {}
    for r in confident:
        a = r.get("Activity") or "unknown"
        activity_counts[a] = activity_counts.get(a, 0) + 1
    if activity_counts:
        print("\n=== Booking activity (confident matches) ===")
        for label in ("active_12m", "dormant", "no_bookings", "unknown"):
            if label in activity_counts:
                print(f"  {label:<14} {activity_counts[label]:>4}")

    dormant_rows = [r for r in confident if r.get("Activity") == "dormant"]
    if dormant_rows:
        print("\n=== Confident matches with NO bookings in last 12 months ===")
        for r in dormant_rows:
            print(f"  [{r['AccountStatus']:<11}] {r['input_name']!r} -> {r['AccountName']!r} "
                  f"(last txn: {r['MostRecentTransaction']})")

    # Print unmatched + review rows inline for quick scanning
    print("\n=== Needs review ===")
    for r in results:
        if r["match_status"] == "review":
            print(f"  [{r['confidence']:>5.1f}] {r['input_name']!r} -> {r['AccountName']!r}")

    print("\n=== Not found ===")
    for r in results:
        if r["match_status"] == "none":
            print(f"  {r['input_name']} (best score {r['confidence']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
