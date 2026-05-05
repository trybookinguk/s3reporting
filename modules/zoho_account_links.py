"""
Build Zoho CRM record URLs for accounts referenced in tier-movement emails.

Zoho records are addressed by an internal record ID, not the TryBooking
AccountId. The TryBooking ID is stored in Zoho's `Account_Name` field, so we
look up the record ID via the search API and build the user-facing URL from it.

Lookups are batched (Zoho's search criteria supports OR'd predicates up to a
URL-length limit) and cached per call so the email loop doesn't pay the cost
multiple times for the same account.
"""

import logging
import os
from typing import Dict, Iterable, List

import requests

from .utils.config import ZOHO_DOMAIN
from .utils.zoho_api import get_session, retry_with_backoff

log = logging.getLogger(__name__)

# Zoho's web URL is region-specific. Override via ZOHO_CRM_WEB_BASE if needed
# (e.g. crm.zoho.com for US accounts). EU is the TryBooking default.
CRM_WEB_BASE = os.environ.get("ZOHO_CRM_WEB_BASE", "https://crm.zoho.eu")

# Number of Account_Name predicates to OR into a single search call. Zoho's
# criteria string has a practical length cap; 20 is comfortably safe.
SEARCH_BATCH_SIZE = 20


def _search_record_ids(token: str, account_ids: List[str]) -> Dict[str, str]:
    """Search Zoho for a batch of TryBooking AccountIds, return {account_id: zoho_id}."""
    if not account_ids:
        return {}

    session = get_session()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    criteria = "(" + " or ".join(f"(Account_Name:equals:{aid})" for aid in account_ids) + ")"
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/search"
    params = {"criteria": criteria, "fields": "id,Account_Name"}

    try:
        resp = retry_with_backoff(session.get)(url, headers=headers, params=params)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning("Zoho search for record IDs failed: %s", e)
        return {}

    if resp.status_code == 204 or not resp.content:
        return {}

    try:
        data = resp.json().get("data", [])
    except ValueError:
        log.warning("Zoho search returned non-JSON response.")
        return {}

    return {
        str(record["Account_Name"]): str(record["id"])
        for record in data
        if record.get("Account_Name") and record.get("id")
    }


def lookup_account_urls(token: str, org_id: str, account_ids: Iterable[int]) -> Dict[int, str]:
    """Resolve TryBooking AccountIds to Zoho CRM web URLs.

    Args:
        token: Zoho OAuth access token.
        org_id: Zoho org ID, used to build the user-facing URL.
        account_ids: TryBooking AccountIds to resolve.

    Returns:
        Dict of {account_id: url}. Accounts not found in Zoho are absent
        from the dict — callers should fall back gracefully (e.g. omit the
        Zoho button from the email).
    """
    if not org_id:
        log.warning("ZOHO_ORG_ID not set — cannot build Zoho record URLs.")
        return {}

    ids_str = [str(int(aid)) for aid in account_ids]
    if not ids_str:
        return {}

    record_ids: Dict[str, str] = {}
    for i in range(0, len(ids_str), SEARCH_BATCH_SIZE):
        batch = ids_str[i:i + SEARCH_BATCH_SIZE]
        record_ids.update(_search_record_ids(token, batch))

    return {
        int(aid): f"{CRM_WEB_BASE}/crm/{org_id}/tab/Accounts/{record_id}"
        for aid, record_id in record_ids.items()
    }
