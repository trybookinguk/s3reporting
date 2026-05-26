"""
Daily tier snapshot — what tier each owned-population account had on the
last run. Used purely for change detection: today's run compares its v2
output against this file to decide which accounts moved.

The file is small (~150 KB at 5,000 accounts) and overwritten each run.
For long-form per-account tier *history* see modules/tier_history.py.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd

from .tier_codes import TIER_ORDER_BEST_TO_WORST
from .utils.config import MOVEMENT_COOLDOWN_DAYS, TIER_OWNERS
from .utils.sharepoint import download_file, upload

log = logging.getLogger(__name__)

SNAPSHOT_FILE = "tier_snapshot.json"

# Tier-rank lookup for direction calculation. Lower index = better tier.
_TIER_RANK = {tier: idx for idx, tier in enumerate(TIER_ORDER_BEST_TO_WORST)}


def load_previous_snapshot(token: str, drive_id: str) -> Dict[str, Dict]:
    """Load the previous run's snapshot. Returns empty dict if file is missing."""
    raw = download_file(token, drive_id, SNAPSHOT_FILE)
    if raw is None:
        log.info("No previous tier snapshot found — treating as first run.")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("tier_snapshot.json is not valid JSON (%s) — treating as first run.", e)
        return {}
    tiers = data.get("tiers", {})
    log.info("Loaded previous tier snapshot: %d accounts, generated %s",
             len(tiers), data.get("generated_at", "?"))
    return tiers


def save_snapshot(token: str, drive_id: str, current_df: pd.DataFrame) -> bool:
    """Write today's tier snapshot. Overwrites the existing file.

    `current_df` is expected to have columns AccountId, Current_Tier,
    Composite_Score (optional), and Account_Name (optional).
    """
    tiers: Dict[str, Dict] = {}
    has_score = "Composite_Score" in current_df.columns
    has_name = "Account_Name" in current_df.columns

    for _, row in current_df.iterrows():
        entry: Dict = {"tier": row["Current_Tier"]}
        if has_score and pd.notna(row["Composite_Score"]):
            entry["composite_score"] = float(row["Composite_Score"])
        if has_name and pd.notna(row.get("Account_Name")):
            entry["account_name"] = str(row["Account_Name"])
        tiers[str(int(row["AccountId"]))] = entry

    payload = {
        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tiers": tiers,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return upload(token, drive_id, SNAPSHOT_FILE, data)


def detect_changes(previous: Dict[str, Dict], current_df: pd.DataFrame) -> pd.DataFrame:
    """Identify accounts whose tier changed since the previous snapshot.

    Returns a DataFrame with columns:
        AccountId, Account_Name, previous_tier, current_tier, direction.

    `direction` is one of: 'up', 'down', 'new', 'departed'. (Per the agreed
    semantics, "departed = dropped out of T1/T2" is rendered as 'down', not
    a separate direction. 'departed' here only means "account vanished from
    the dataset entirely" — included for completeness; the email layer can
    decide whether to surface it.)
    """
    rows = []
    current_ids = set()

    for _, row in current_df.iterrows():
        aid = str(int(row["AccountId"]))
        current_ids.add(aid)
        current_tier = row["Current_Tier"]

        prev_entry = previous.get(aid)
        if prev_entry is None:
            rows.append({
                "AccountId": int(aid),
                "Account_Name": row.get("Account_Name", ""),
                "previous_tier": None,
                "current_tier": current_tier,
                "direction": "new",
            })
            continue

        prev_tier = prev_entry.get("tier")
        if prev_tier == current_tier:
            continue  # No movement

        prev_rank = _TIER_RANK.get(prev_tier)
        curr_rank = _TIER_RANK.get(current_tier)
        if prev_rank is None or curr_rank is None:
            log.warning("Unknown tier in change detection: prev=%r curr=%r (account %s)",
                        prev_tier, current_tier, aid)
            continue

        # Lower rank = better tier, so curr_rank < prev_rank means improvement.
        direction = "up" if curr_rank < prev_rank else "down"
        rows.append({
            "AccountId": int(aid),
            "Account_Name": row.get("Account_Name", ""),
            "previous_tier": prev_tier,
            "current_tier": current_tier,
            "direction": direction,
        })

    # Departed accounts: in previous snapshot, absent from current run
    for aid, prev_entry in previous.items():
        if aid in current_ids:
            continue
        rows.append({
            "AccountId": int(aid),
            "Account_Name": prev_entry.get("account_name", ""),
            "previous_tier": prev_entry.get("tier"),
            "current_tier": None,
            "direction": "departed",
        })

    return pd.DataFrame(rows)


def filter_email_relevant_moves(changes_df: pd.DataFrame) -> pd.DataFrame:
    """Keep only movements that touch an owned tier (Tier 1 or Tier 2).

    A move is email-relevant if either the previous or current tier is in
    TIER_OWNERS. Movements entirely within or between unowned tiers
    (e.g. Tier 4 -> Tier 5) are silently dropped.

    Drops:
      - "departed" rows (an account vanishing is more likely a data glitch
        than a real loss).
      - "new" rows / any row with no previous_tier — without a previous tier
        the email has nothing meaningful to say about the change. Genuinely
        new accounts will surface naturally on the day they actually move
        between tiers.
    """
    if changes_df.empty:
        return changes_df

    owned = set(TIER_OWNERS.keys())
    has_previous = changes_df["previous_tier"].notna()
    mask = (
        changes_df["previous_tier"].isin(owned)
        | changes_df["current_tier"].isin(owned)
    ) & (changes_df["direction"] != "departed") & has_previous
    return changes_df[mask].copy()


def suppress_repetitive_moves(
    relevant_df: pd.DataFrame,
    history: Dict,
    today: date,
    window_days: int = MOVEMENT_COOLDOWN_DAYS,
) -> pd.DataFrame:
    """Drop boundary flip-flops while always letting sustained climbs through.

    A move survives if EITHER:
      * it is a climb ('up') into a tier the account has NOT held anywhere in
        the trailing `window_days` (genuine new ground — a sustained
        progression), OR
      * the account had no owned-tier change within the trailing
        `window_days` (this is its first move in the window — normal signal).

    A move is suppressed when the account already changed owned tier inside
    the window and the move is not new-ground climb — i.e. it is a reversion
    or repeat oscillation around a boundary, which produces the repetitive
    emails we want to mute.

    Stateless: "recent activity" is inferred from each account's own daily
    tier series in `history` (tier_history.json), so no sent-email log is
    needed. Accounts absent from the history (or with no in-window samples)
    are treated as having no recent activity and pass through.
    """
    if relevant_df.empty:
        return relevant_df

    # Local import avoids a module-level cycle (tier_history imports nothing
    # from here, but keep the dependency one-directional and lazy).
    from . import tier_history

    owned = set(TIER_OWNERS.keys())
    cutoff = today - timedelta(days=window_days)

    keep_idx = []
    suppressed = 0
    for idx, row in relevant_df.iterrows():
        aid = int(row["AccountId"])
        current_tier = row["current_tier"]
        direction = row["direction"]

        # Trailing window of this account's tier series, oldest-first.
        # extract_account_history returns (day_iso, tier_name, score); we only
        # need days strictly within the window, and only days the account
        # actually existed (tier_name is not None).
        timeline = tier_history.extract_account_history(history, aid)
        window = [
            (d, t) for (d, t, _s) in timeline
            if t is not None and date.fromisoformat(d) >= cutoff
        ]
        tiers_held = {t for _d, t in window}

        # Did an owned-tier change happen within the window? Count transitions
        # between consecutive observed samples where either side is owned.
        recent_owned_change = False
        prev_t = None
        for _d, t in window:
            if prev_t is not None and t != prev_t and (t in owned or prev_t in owned):
                recent_owned_change = True
                break
            prev_t = t

        # Sustained-climb carve-out: a promotion into a tier not held during
        # the window is genuine new ground — always surface it. Note the
        # account's *current* tier is today's value (today's column may not
        # yet be in the loaded history slice), so `tiers_held` reflects the
        # window *before* this move, which is exactly what we want to test.
        new_ground_climb = direction == "up" and current_tier not in tiers_held

        if new_ground_climb or not recent_owned_change:
            keep_idx.append(idx)
        else:
            suppressed += 1
            log.info(
                "Suppressing repetitive move for account %s (%s -> %s, %s): "
                "owned-tier change already seen within %d days.",
                aid, row["previous_tier"], current_tier, direction, window_days,
            )

    if suppressed:
        log.info("Cooldown suppression: %d of %d email-relevant moves muted "
                 "(window=%d days).", suppressed, len(relevant_df), window_days)
    return relevant_df.loc[keep_idx].copy()
