"""
Box Office terminal hire — schema and consistency check.

Two SharePoint-resident state files own the live data, both written by the
dashboard frontend (no backend mutation):

  box_office_inventory.json — every terminal and cradle we own.
  box_office_hires.json     — active and historical hires.

This module defines the schema (as documentation), provides loaders for both
files via Microsoft Graph, and runs a read-only consistency check that the
daily generate_dashboard_data.py job surfaces as warnings on metadata.json.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# === Schema reference ===
#
# InventoryItem (one per physical device):
#   {
#     "id": "TB-T1042",                # hardware ID stamped on the device
#     "type": "terminal" | "cradle",
#     "model": "BBPOS WisePOS E" | "", # terminals: fixed list (frontend-managed);
#                                      # cradles: empty string (no model concept).
#     "notes": "...",                  # optional
#     "retired_at": "YYYY-MM-DD",      # optional; hides from active pool
#     "current_hire_id": "<uuid> | null",
#     "added_by": "...", "added_at": "ISO",
#     "changed_by": "...", "changed_at": "ISO"
#   }
#
# Hire (one per hire instance):
#   {
#     "id": "<uuid v4>",
#     "account_id": 12345,
#     "account_name": "Snapshot Co",
#     "contact_name": "...", "contact_email": "...", "contact_phone": "...",
#     "shipping_address": {"line1", "line2?", "city", "postcode", "country?"},
#                                      # required iff outbound_method == "ship"
#                                      # OR return_method == "ship"
#     "hire_from": "YYYY-MM-DD",
#     "hire_to": "YYYY-MM-DD | null",  # null = open-ended
#     "status": "draft" | "pending_payment" | "trial" | "confirmed"
#             | "shipped" | "in_use" | "returned" | "cancelled",
#                                      # "trial" replaces the old is_trial flag.
#     "terminal_ids": [...],
#     "cradle_ids": [...],
#     "outbound_method": "ship" | "drop_off" | "collect",
#     "return_method": "ship" | "drop_off" | "collect",
#     "box_office_web_enabled": bool,
#     "terminals_linked_to_account": bool,
#     "payment_received": bool,
#     "trybooking_booking_url_id": "...",  # reference only
#     "amount_due_pence": int | null,
#     "payment_reference": "...",
#     "outbound_tracking": "...", "return_tracking": "...",
#     "shipped_at": "ISO?", "returned_at": "ISO?",
#     "notes": "...",
#     "created_by": "...", "created_at": "ISO",
#     "changed_by": "...", "changed_at": "ISO"
#   }
#
# Validation rule (frontend enforces, this check warns on drift):
#   A hire cannot be in confirmed/shipped/in_use unless payment_received
#   is true. Trial hires sit in status="trial" and don't need payment.
#
# Conflict rule:
#   Two active-status hires (trial/confirmed/shipped/in_use) cannot hold
#   the same inventory item with overlapping date windows. A future-dated
#   hire for a currently-held item is fine as long as the windows don't
#   overlap. hire_to=null is treated as +infinity.

INVENTORY_FILE = "box_office_inventory.json"
HIRES_FILE = "box_office_hires.json"

# Statuses where the equipment is considered "active" — the hire holds a
# terminal/cradle and the inventory item should be unavailable. "trial" is
# included: a trial hire still has the kit out the door.
ACTIVE_STATUSES = frozenset({"trial", "confirmed", "shipped", "in_use"})

# Statuses requiring payment-or-trial before they're reachable. "trial"
# itself doesn't need to appear here — by definition trial = is_trial.
PAID_STATUSES = frozenset({"confirmed", "shipped", "in_use"})

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SHAREPOINT_FOLDER = "Platform Data/Dashboard Data"


def _graph_get(token: str, drive_id: str, filename: str) -> Optional[bytes]:
    path = f"{SHAREPOINT_FOLDER}/{filename}"
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/content"
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if response.status_code == 200:
        return response.content
    if response.status_code == 404:
        return None
    log.warning("Box Office: Graph fetch of %s failed: %d %s",
                filename, response.status_code, response.text[:200])
    return None


def _load_json_list(token: str, drive_id: str, filename: str) -> list:
    raw = _graph_get(token, drive_id, filename)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Box Office: %s is not valid JSON (%s) — treating as empty.", filename, e)
        return []
    if not isinstance(data, list):
        log.warning("Box Office: %s is not a list — treating as empty.", filename)
        return []
    return data


def load_inventory(token: str, drive_id: str) -> list:
    """Return the inventory list (empty if file is missing or unreadable)."""
    items = _load_json_list(token, drive_id, INVENTORY_FILE)
    log.info("Box Office: loaded %d inventory items.", len(items))
    return items


def load_hires(token: str, drive_id: str) -> list:
    """Return the hires list (empty if file is missing or unreadable)."""
    hires = _load_json_list(token, drive_id, HIRES_FILE)
    log.info("Box Office: loaded %d hires.", len(hires))
    return hires


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None


def _windows_overlap(a_from: Optional[date], a_to: Optional[date],
                     b_from: Optional[date], b_to: Optional[date]) -> bool:
    """True if [a_from, a_to] intersects [b_from, b_to].

    None on hire_to is +infinity (open-ended). None on hire_from is treated
    conservatively as -infinity so a missing date doesn't accidentally hide
    an overlap. Inclusive at both ends.
    """
    a_start = a_from or date.min
    b_start = b_from or date.min
    a_end = a_to or date.max
    b_end = b_to or date.max
    return a_start <= b_end and b_start <= a_end


def check_box_office_consistency(inventory: list, hires: list,
                                 today: Optional[date] = None) -> list:
    """Return a list of warning dicts. Read-only: never mutates inputs.

    Each warning has:
      {"severity": "info"|"warning"|"error", "kind": "...", "message": "...",
       "hire_id": "...", "item_id": "..."}  (last two optional)
    """
    today = today or datetime.now(timezone.utc).date()
    warnings = []

    inventory_by_id = {}
    for item in inventory:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        if item_id in inventory_by_id:
            warnings.append({
                "severity": "error",
                "kind": "duplicate_inventory_id",
                "item_id": item_id,
                "message": f"Inventory item id '{item_id}' appears multiple times.",
            })
        inventory_by_id[item_id] = item

    # Map inventory.id → list of (hire_id, hire_from, hire_to) for active hires
    # that hold this item. hire_to may be None (= open-ended).
    active_holdings: dict = {}

    for hire in hires:
        if not isinstance(hire, dict):
            continue
        hire_id = hire.get("id", "<unknown>")
        status = hire.get("status", "draft")

        # Payment guardrail: forbidden states.
        # Trial hires sit in status="trial" and bypass this; only
        # confirmed/shipped/in_use require payment_received.
        if status in PAID_STATUSES and not hire.get("payment_received"):
            warnings.append({
                "severity": "error",
                "kind": "payment_required",
                "hire_id": hire_id,
                "message": (f"Hire {hire_id} is in status '{status}' but "
                            "payment_received is false. Frontend should "
                            "have blocked this — investigate."),
            })

        # Equipment refs must exist in inventory.
        for kind, ids in (("terminal", hire.get("terminal_ids") or []),
                          ("cradle", hire.get("cradle_ids") or [])):
            for item_id in ids:
                item = inventory_by_id.get(item_id)
                if item is None:
                    warnings.append({
                        "severity": "error",
                        "kind": "missing_inventory",
                        "hire_id": hire_id,
                        "item_id": item_id,
                        "message": (f"Hire {hire_id} references "
                                    f"{kind} '{item_id}' which is not in inventory."),
                    })
                    continue
                if item.get("type") != kind:
                    warnings.append({
                        "severity": "error",
                        "kind": "wrong_type",
                        "hire_id": hire_id,
                        "item_id": item_id,
                        "message": (f"Hire {hire_id} lists '{item_id}' as a "
                                    f"{kind}, but inventory has it as "
                                    f"'{item.get('type')}'."),
                    })
                if item.get("retired_at"):
                    warnings.append({
                        "severity": "warning",
                        "kind": "retired_in_use",
                        "hire_id": hire_id,
                        "item_id": item_id,
                        "message": (f"Hire {hire_id} uses '{item_id}' which "
                                    f"was retired on {item['retired_at']}."),
                    })

                if status in ACTIVE_STATUSES:
                    active_holdings.setdefault(item_id, []).append((
                        hire_id,
                        _parse_date(hire.get("hire_from")),
                        _parse_date(hire.get("hire_to")),
                    ))

        # Overdue check — only when hire_to is set.
        hire_to = _parse_date(hire.get("hire_to"))
        if hire_to and hire_to < today and status in ACTIVE_STATUSES:
            warnings.append({
                "severity": "warning",
                "kind": "overdue",
                "hire_id": hire_id,
                "message": (f"Hire {hire_id} was due back {hire_to.isoformat()} "
                            f"but is still '{status}'."),
            })

    # Double-booking detection: two active hires whose [hire_from, hire_to]
    # windows overlap on the same item. hire_to=None is treated as +infinity.
    # A future-dated hire for a currently-held item is fine if the windows
    # don't overlap.
    for item_id, holdings in active_holdings.items():
        if len(holdings) < 2:
            continue
        # Pair-wise comparison; n is tiny in practice.
        flagged_pairs = set()
        for i, (h1_id, h1_from, h1_to) in enumerate(holdings):
            for j in range(i + 1, len(holdings)):
                h2_id, h2_from, h2_to = holdings[j]
                if not _windows_overlap(h1_from, h1_to, h2_from, h2_to):
                    continue
                pair = tuple(sorted([h1_id, h2_id]))
                if pair in flagged_pairs:
                    continue
                flagged_pairs.add(pair)
                warnings.append({
                    "severity": "error",
                    "kind": "double_booked",
                    "item_id": item_id,
                    "message": (f"Inventory item '{item_id}' is held by two "
                                f"active hires with overlapping windows: "
                                f"{pair[0]} and {pair[1]}."),
                })

    # current_hire_id sanity check on inventory.
    active_hire_ids = {h.get("id") for h in hires
                      if isinstance(h, dict)
                      and h.get("status") in ACTIVE_STATUSES}
    for item in inventory:
        if not isinstance(item, dict):
            continue
        chid = item.get("current_hire_id")
        if chid and chid not in active_hire_ids:
            warnings.append({
                "severity": "warning",
                "kind": "stale_current_hire_id",
                "item_id": item.get("id"),
                "message": (f"Inventory '{item.get('id')}' points at "
                            f"current_hire_id '{chid}' which is not an "
                            f"active hire."),
            })

    return warnings


def summarise(inventory: list, hires: list, warnings: list) -> dict:
    """One-line summary fields for metadata.json — counts only."""
    active_hires = sum(1 for h in hires
                       if isinstance(h, dict)
                       and h.get("status") in ACTIVE_STATUSES)
    return {
        "inventory_count": len(inventory),
        "hires_count": len(hires),
        "active_hires": active_hires,
        "warning_count": len(warnings),
        "error_count": sum(1 for w in warnings if w.get("severity") == "error"),
    }
