"""
Long-form per-account tier history — daily samples, columnar layout.

Each daily run appends a column to a single SharePoint-resident JSON file.
Schema:

    {
      "schema_version": 1,
      "generated_at": "ISO timestamp",
      "tier_codes": {"1": "Tier 1", ..., "7": "Nil"},
      "accounts": [12345, 67890, ...],
      "days": ["2014-05-05", ..., "2026-05-05"],
      "tiers": [[1, 2, 2, ...], [null, 5, 5, ...], ...],
      "composite_scores": [[12.5, 14.0, ...], [null, 80.0, ...], ...],
    }

Rows of `tiers` and `composite_scores` align with `accounts` (row index = account
position). Columns align with `days` (column index = day position). `null`
in either matrix means "the account didn't exist in the dataset on that date"
— the chart renders these as gaps in the line.

This file gets large (estimated ~330 MB raw / ~80 MB gzipped at 12 years of
daily data over 15k unique accounts). Reads and writes go through the
resumable upload helper in modules.utils.sharepoint.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .tier_codes import INT_TO_TIER, TIER_TO_INT
from .utils.sharepoint import download_file, upload

log = logging.getLogger(__name__)

HISTORY_FILE = "tier_history.json"
SCHEMA_VERSION = 1


def _empty_history() -> Dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tier_codes": {str(k): v for k, v in INT_TO_TIER.items()},
        "accounts": [],
        "days": [],
        "tiers": [],
        "composite_scores": [],
    }


def load_history(token: str, drive_id: str) -> Dict:
    """Fetch tier_history.json. Returns an empty skeleton on 404."""
    raw = download_file(token, drive_id, HISTORY_FILE)
    if raw is None:
        log.info("No tier_history.json — starting fresh.")
        return _empty_history()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("tier_history.json is not valid JSON (%s) — refusing to overwrite. "
                  "Manual intervention required.", e)
        raise
    log.info("Loaded tier history: %d accounts × %d days",
             len(data.get("accounts", [])), len(data.get("days", [])))
    return data


def save_history(token: str, drive_id: str, history: Dict) -> bool:
    """Serialise and upload the full history file (uses resumable upload for >4 MiB)."""
    history["generated_at"] = datetime.now(timezone.utc).isoformat()
    data = json.dumps(history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    log.info("Uploading tier history: %.1f MiB", len(data) / (1024 * 1024))
    return upload(token, drive_id, HISTORY_FILE, data)


def append_day(history: Dict, day: date, current_df: pd.DataFrame) -> Dict:
    """Append today's column to the history (in place; returns the same dict).

    `current_df` must have columns AccountId, Current_Tier, Composite_Score.
    Accounts present in the day's data but not yet in `history["accounts"]`
    are appended (with their prior columns padded with null). Accounts in
    `history["accounts"]` but absent today get null in the new column.

    If `day` already exists in `history["days"]`, that column is overwritten
    in place — supports re-running for the same date without bloating the file.
    """
    day_str = day.isoformat()

    accounts: List[int] = list(history.get("accounts", []))
    days: List[str] = list(history.get("days", []))
    tiers: List[List] = list(history.get("tiers", []))
    scores: List[List] = list(history.get("composite_scores", []))

    account_index: Dict[int, int] = {aid: i for i, aid in enumerate(accounts)}

    # Pull today's data into account-keyed dicts for O(1) lookup
    today_tiers: Dict[int, Optional[int]] = {}
    today_scores: Dict[int, Optional[float]] = {}
    for _, row in current_df.iterrows():
        aid = int(row["AccountId"])
        tier_name = row["Current_Tier"]
        today_tiers[aid] = TIER_TO_INT.get(tier_name)
        score = row.get("Composite_Score")
        today_scores[aid] = float(score) if pd.notna(score) else None

    # Add any new accounts seen today, padding their historical columns with null
    pad_length = len(days)
    for aid in today_tiers:
        if aid not in account_index:
            account_index[aid] = len(accounts)
            accounts.append(aid)
            tiers.append([None] * pad_length)
            scores.append([None] * pad_length)

    # Determine the column position — overwrite existing day or append new
    if day_str in days:
        col = days.index(day_str)
        log.info("Overwriting existing column for %s (column %d).", day_str, col)
        for i, aid in enumerate(accounts):
            tiers[i][col] = today_tiers.get(aid)
            scores[i][col] = today_scores.get(aid)
    else:
        days.append(day_str)
        for i, aid in enumerate(accounts):
            tiers[i].append(today_tiers.get(aid))
            scores[i].append(today_scores.get(aid))
        log.info("Appended column for %s (now %d days × %d accounts).",
                 day_str, len(days), len(accounts))

    history["accounts"] = accounts
    history["days"] = days
    history["tiers"] = tiers
    history["composite_scores"] = scores
    return history


def find_most_recent_relevant_move(history: Dict, owned_tiers: Iterable[str]) -> Optional[Dict]:
    """Walk the history file backwards looking for the most recent T1/T2-touching
    tier change, for use as a TEST_MODE preview when today has no real moves.

    A move is "relevant" if either side of the transition (the held tier
    immediately before, or the held tier immediately at/after) is in
    `owned_tiers`. We pick the latest such move across all accounts.

    Returns a dict shaped like one row of detect_changes' output:
        {AccountId, Account_Name, previous_tier, current_tier, direction, day}
    or None if no relevant move is found.

    "day" is the ISO date the move was first observed (i.e. the day the
    history column shows the new tier).
    """
    from .tier_codes import INT_TO_TIER, TIER_ORDER_BEST_TO_WORST

    accounts = history.get("accounts", [])
    days = history.get("days", [])
    tiers_matrix = history.get("tiers", [])
    if not accounts or len(days) < 2:
        return None

    owned_set = set(owned_tiers)
    rank = {t: i for i, t in enumerate(TIER_ORDER_BEST_TO_WORST)}

    best: Optional[Dict] = None
    best_day_idx = -1

    for row_idx, account_id in enumerate(accounts):
        row = tiers_matrix[row_idx]
        # Walk backwards through the row tracking the new (most-recent) tier;
        # the transition is at the first column where the older code differs.
        # transition_day_idx is the column where the new tier *first* appeared.
        new_tier_code = None
        transition_day_idx = None  # earliest column showing the new tier
        for col_idx in range(len(row) - 1, -1, -1):
            code = row[col_idx]
            if code is None:
                continue
            if new_tier_code is None:
                new_tier_code = code
                transition_day_idx = col_idx
                continue
            if code == new_tier_code:
                # Same tier as the new run; push transition_day back further
                transition_day_idx = col_idx
                continue
            # Different code — this is the older (previous) tier. Record the
            # transition and stop scanning this account.
            prev_tier = INT_TO_TIER.get(code)
            curr_tier = INT_TO_TIER.get(new_tier_code)
            if (prev_tier in owned_set) or (curr_tier in owned_set):
                if transition_day_idx > best_day_idx:
                    prev_rank = rank.get(prev_tier)
                    curr_rank = rank.get(curr_tier)
                    direction = "up" if curr_rank < prev_rank else "down"
                    best = {
                        "AccountId": int(account_id),
                        "Account_Name": "",
                        "previous_tier": prev_tier,
                        "current_tier": curr_tier,
                        "direction": direction,
                        "day": days[transition_day_idx],
                    }
                    best_day_idx = transition_day_idx
            break

    return best


def extract_account_history(history: Dict, account_id: int) -> List[Tuple[str, Optional[str], Optional[float]]]:
    """Slice one account's full timeline out of the history.

    Returns a list of (day_iso, tier_name, composite_score) tuples ordered
    oldest-first. Days where the account wasn't in the dataset have
    tier_name=None and composite_score=None — the chart renderer can use
    these to draw gaps.
    """
    accounts = history.get("accounts", [])
    days = history.get("days", [])
    tiers = history.get("tiers", [])
    scores = history.get("composite_scores", [])

    try:
        idx = accounts.index(account_id)
    except ValueError:
        return []

    account_tiers = tiers[idx]
    account_scores = scores[idx]
    out: List[Tuple[str, Optional[str], Optional[float]]] = []
    for d, t, s in zip(days, account_tiers, account_scores):
        tier_name = INT_TO_TIER.get(t) if t is not None else None
        out.append((d, tier_name, s))
    return out
