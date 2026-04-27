#!/usr/bin/env python3
"""
Mailshake acquisition report.

Pulls every Mailshake send via the bulk /campaigns/export endpoint, joins
against TryBooking Users + Accounts, and produces the acquisition report
(matched conversions where the send pre-dated the account) as JSON + CSV
in the SharePoint dashboard folder.

Idempotent: each run regenerates the full report from scratch, so new
accounts automatically pick up matches without any incremental state.

Usage:
    python3 mailshake_acquisition.py                  # Run, upload to SharePoint
    python3 mailshake_acquisition.py --dry-run        # Run, skip upload
    python3 mailshake_acquisition.py --local-dir out  # Also save to local dir
"""

import argparse
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterable

import msal
import pandas as pd
import requests

from modules.utils.data_loader import load_accounts, load_users

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mailshake-acquisition")

# === Configuration ===

MAILSHAKE_API_KEY = os.environ.get("MAILSHAKE_API_KEY")
MAILSHAKE_BASE = "https://api.mailshake.com/2017-04-01"

AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")
SHAREPOINT_FOLDER = "Platform Data/Dashboard Data"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

EXPORT_BATCH_SIZE = 20          # Mailshake max campaigns per export call
EXPORT_POLL_INTERVAL = 5        # seconds between export-status polls
EXPORT_POLL_TIMEOUT = 600       # seconds to wait for a single export

MAX_RETRIES = 3
RETRY_BACKOFF = 2

# Free-email domains excluded from partial (domain-level) matching.
# A partial match only means anything for corporate/org domains where the
# prospect email shares a domain with the account owner.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk",
    "outlook.com", "outlook.co.uk",
    "live.com", "live.co.uk",
    "msn.com", "aol.com", "aol.co.uk",
    "icloud.com", "me.com", "mac.com",
    "btinternet.com", "sky.com", "talktalk.net",
    "virginmedia.com", "ntlworld.com", "tiscali.co.uk",
    "protonmail.com", "proton.me", "pm.me",
    "gmx.com", "gmx.co.uk", "mail.com",
})


# === Mailshake API ===

def _mailshake_request(method, path, params=None, json_body=None):
    """Call the Mailshake API with retry and rate-limit handling."""
    url = f"{MAILSHAKE_BASE}{path}"
    params = dict(params or {})
    params["apiKey"] = MAILSHAKE_API_KEY

    for attempt in range(MAX_RETRIES):
        response = method(url, params=params, json=json_body, timeout=60)

        if response.status_code == 200:
            return response.json()

        # Rate limited — message carries an absolute UTC retry timestamp.
        if response.status_code == 429 or (
            response.status_code == 400 and b"limit_reached" in response.content
        ):
            try:
                body = response.json()
                msg = body.get("error", "")
                # Format: "Please wait and try again after: 2017-08-21T15:16:15.207Z"
                if "after:" in msg:
                    ts = msg.split("after:", 1)[1].strip()
                    retry_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    wait = max(1, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    log.warning("Rate limited — waiting %.0fs until %s", wait, retry_at.isoformat())
                    time.sleep(wait + 1)
                    continue
            except Exception:
                pass
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue

        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF * (2 ** attempt)
            log.warning("Server error %d, retrying in %ds", response.status_code, wait)
            time.sleep(wait)
            continue

        log.error("Mailshake %s %s failed: %d %s", method.__name__.upper(), path,
                  response.status_code, response.text[:300])
        response.raise_for_status()

    raise RuntimeError(f"Mailshake request exhausted retries: {path}")


def list_all_campaigns():
    """Paginate through /campaigns/list and return every campaign."""
    campaigns = []
    next_token = None
    while True:
        params = {"perPage": 100}
        if next_token:
            params["nextToken"] = next_token
        data = _mailshake_request(requests.get, "/campaigns/list", params=params)
        campaigns.extend(data.get("results", []))
        next_token = data.get("nextToken")
        if not next_token:
            break
    log.info("Fetched %d campaigns from Mailshake.", len(campaigns))
    return campaigns


def submit_export(campaign_ids):
    """Submit a bulk export for up to 20 campaigns. Returns the status ID."""
    body = {"campaignIDs": list(campaign_ids), "exportType": "simple"}
    data = _mailshake_request(requests.post, "/campaigns/export", json_body=body)
    if data.get("isEmpty"):
        return None
    return data.get("checkStatusID")


def wait_for_export(status_id):
    """Poll export-status until finished. Returns the CSV download URL."""
    deadline = time.time() + EXPORT_POLL_TIMEOUT
    while time.time() < deadline:
        data = _mailshake_request(
            requests.get, "/campaigns/export-status", params={"statusID": status_id}
        )
        if data.get("isFinished"):
            return data.get("csvDownloadUrl")
        time.sleep(EXPORT_POLL_INTERVAL)
    raise TimeoutError(f"Export {status_id} did not finish within {EXPORT_POLL_TIMEOUT}s")


def download_export_csv(url):
    """Download an export CSV (hosted on S3, no auth) and return a DataFrame."""
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return pd.read_csv(io.BytesIO(response.content), low_memory=False)


def fetch_all_sends(campaigns):
    """Run bulk exports in batches and concatenate the results."""
    all_ids = [c["id"] for c in campaigns]
    frames = []
    for i in range(0, len(all_ids), EXPORT_BATCH_SIZE):
        batch = all_ids[i : i + EXPORT_BATCH_SIZE]
        log.info("Submitting export batch %d (%d campaigns)...", i // EXPORT_BATCH_SIZE + 1, len(batch))
        status_id = submit_export(batch)
        if not status_id:
            log.info("  Batch was empty — skipping.")
            continue
        url = wait_for_export(status_id)
        if not url:
            log.warning("  Batch %d finished without a CSV URL — skipping.", i // EXPORT_BATCH_SIZE + 1)
            continue
        df = download_export_csv(url)
        log.info("  %d rows downloaded.", len(df))
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    log.info("Combined: %d recipient rows across %d campaigns.", len(combined), len(campaigns))
    return combined


# === Join: Mailshake ↔ Users ↔ Accounts ===

def _email_domain(email):
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()


def build_owners_lookup(users_df, accounts_df):
    """Return (by_email, by_domain) lookups keyed to account data.

    by_email: {lowercased_email: [account_dict, ...]}
    by_domain: {lowercased_domain: [(owner_email, account_dict), ...]}
    Domains in FREE_EMAIL_DOMAINS are excluded from by_domain.
    """
    owners = users_df[
        (users_df["RoleName"] == "AccountOwner")
        & (users_df["IsDeleted"] == 0)
    ].copy()
    owners["email_lc"] = owners["Username"].astype(str).str.strip().str.lower()
    owners = owners[owners["email_lc"].str.contains("@", na=False)]
    owners["AccountId"] = pd.to_numeric(owners["AccountId"], errors="coerce").astype("Int64")
    owners = owners[owners["AccountId"].notna()]

    accts = accounts_df.copy()
    id_col = "AccountId" if "AccountId" in accts.columns else "Id"
    accts["_aid"] = pd.to_numeric(accts[id_col], errors="coerce").astype("Int64")
    accts = accts[accts["_aid"].notna()].set_index("_aid")

    by_email = {}
    by_domain = {}

    for _, row in owners.iterrows():
        aid = int(row["AccountId"])
        if aid not in accts.index:
            continue
        acct_row = accts.loc[aid]
        # Duplicate AccountIds in accounts_df would produce a DataFrame — take the first.
        if isinstance(acct_row, pd.DataFrame):
            acct_row = acct_row.iloc[0]

        acct_info = {
            "account_id": aid,
            "account_name": _safe_str(acct_row.get("AccountName")),
            "account_created": _iso_date(acct_row.get("DateTimeCreated")),
            "account_industry": _safe_str(acct_row.get("Industry")),
            "owner_email": row["email_lc"],
        }

        by_email.setdefault(row["email_lc"], []).append(acct_info)

        domain = _email_domain(row["email_lc"])
        if domain and domain not in FREE_EMAIL_DOMAINS:
            by_domain.setdefault(domain, []).append((row["email_lc"], acct_info))

    log.info("Owner lookups: %d unique emails, %d unique non-free domains.",
             len(by_email), len(by_domain))
    return by_email, by_domain


def _safe_str(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _iso_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts.date().isoformat()


def _parse_send_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts


def match_recipient(email_lc, by_email, by_domain):
    """Return (match_type, account_info_or_none) for a Mailshake recipient email."""
    if email_lc in by_email:
        return "full", by_email[email_lc][0]

    domain = _email_domain(email_lc)
    if domain and domain in by_domain:
        return "partial", by_domain[domain][0][1]

    return "none", None


# Fields carried across from account_metrics.json into each acquisition row.
# Mirrors the PPC report shape: identity/segmentation dimensions + lifetime metrics.
ACCOUNT_METRIC_FIELDS = (
    "tier",
    "activity_rating",
    "sub_industry",
    "gateway",
    "years_active",
    "events_lifetime",
    "tickets_lifetime",
    "revenue_lifetime",
    "fees_lifetime",
)


def build_report(sends_df, by_email, by_domain, metrics_by_account=None):
    """Produce acquisition rows: matched recipients who signed up *after* being emailed.

    Excluded: unmatched recipients (no account); matched recipients whose account
    pre-dated the send (those are re-engagement, handled by a separate step).

    If ``metrics_by_account`` is provided (keyed by account_id), each matched row
    is enriched with tier, activity rating, lifetime revenue/fees/events, etc.
    """
    records = []
    if sends_df.empty:
        return records

    col = {c.lower().strip(): c for c in sends_df.columns}

    def get(row, key, default=""):
        real = col.get(key.lower())
        if real is None:
            return default
        val = row.get(real, default)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return val

    for _, row in sends_df.iterrows():
        email_raw = get(row, "Email")
        email_lc = str(email_raw).strip().lower() if email_raw else ""
        if not email_lc:
            continue

        first_send = _parse_send_date(get(row, "First send date", None))
        match_type, acct = match_recipient(email_lc, by_email, by_domain)

        account_id = acct["account_id"] if acct else None
        account_name = acct["account_name"] if acct else ""
        account_created = acct["account_created"] if acct else None
        account_industry = acct["account_industry"] if acct else ""
        owner_email = acct["owner_email"] if acct else ""

        # Only keep matched recipients whose account was created on/after the send.
        if not acct or first_send is None or not account_created:
            continue

        created_ts = pd.to_datetime(account_created, utc=True)
        days_send_to_signup = int((created_ts - first_send).total_seconds() // 86400)
        if days_send_to_signup < 0:
            # Account pre-dated the send → re-engagement, not acquisition.
            continue

        record = {
            "email": email_lc,
            "first_name": _safe_str(get(row, "First Name")),
            "last_name": _safe_str(get(row, "Last Name")),
            "campaign": _safe_str(get(row, "Campaign")),
            "first_send_date": first_send.date().isoformat(),
            "first_open_date": _iso_date(get(row, "First open date", None)),
            "first_reply_date": _iso_date(get(row, "First reply date", None)),
            "match_type": match_type,
            "account_id": account_id,
            "account_name": account_name,
            "account_created": account_created,
            "account_industry": account_industry,
            "owner_email": owner_email,
            "days_send_to_signup": days_send_to_signup,
        }

        metrics = (metrics_by_account or {}).get(account_id, {}) if account_id is not None else {}
        for field in ACCOUNT_METRIC_FIELDS:
            record[field] = metrics.get(field)

        records.append(record)

    # Full matches at the top; within each group, most recent send first.
    match_order = {"full": 0, "partial": 1}
    records.sort(key=lambda r: (
        match_order.get(r["match_type"], 99),
        -(pd.Timestamp(r["first_send_date"]).value if r.get("first_send_date") else 0),
    ))
    return records


# === Graph / SharePoint ===

def authenticate_graph():
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        log.error("Azure credentials not set (AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET).")
        return None
    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID, authority=authority, client_credential=AZURE_CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        log.error("Graph auth failed: %s", result.get("error_description", result.get("error")))
        return None
    log.info("Authenticated to Microsoft Graph.")
    return result["access_token"]


def download_from_sharepoint(token, filename):
    """Download a file from the dashboard SharePoint folder. Returns bytes or None."""
    path = f"{SHAREPOINT_FOLDER}/{filename}" if SHAREPOINT_FOLDER else filename
    url = f"{GRAPH_BASE}/drives/{SHAREPOINT_DRIVE_ID}/root:/{path}:/content"
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if response.status_code == 200:
        return response.content
    if response.status_code == 404:
        log.warning("SharePoint file not found: %s", filename)
        return None
    log.error("SharePoint download of %s failed: %d %s",
              filename, response.status_code, response.text[:300])
    return None


def load_account_metrics(token):
    """Fetch account_metrics.json from SharePoint and index by account_id.

    Returns {} if the file is missing — the report will still run but without
    tier/revenue enrichment.
    """
    data = download_from_sharepoint(token, "account_metrics.json")
    if not data:
        return {}
    try:
        rows = json.loads(data)
    except json.JSONDecodeError as e:
        log.error("account_metrics.json is not valid JSON: %s", e)
        return {}
    indexed = {int(r["account_id"]): r for r in rows if r.get("account_id") is not None}
    log.info("Loaded account_metrics.json: %d accounts.", len(indexed))
    return indexed


def upload_to_sharepoint(token, filename, data_bytes):
    path = f"{SHAREPOINT_FOLDER}/{filename}" if SHAREPOINT_FOLDER else filename
    url = f"{GRAPH_BASE}/drives/{SHAREPOINT_DRIVE_ID}/root:/{path}:/content"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}

    for attempt in range(MAX_RETRIES):
        response = requests.put(url, headers=headers, data=data_bytes, timeout=60)
        if response.status_code in (200, 201):
            log.info("Uploaded %s (%d bytes) to SharePoint.", filename, len(data_bytes))
            return True
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
            log.warning("Throttled, waiting %ds", wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue
        log.error("Upload of %s failed: %d %s", filename, response.status_code, response.text[:300])
        return False
    return False


# === Orchestration ===

def records_to_csv_bytes(records):
    if not records:
        return b""
    df = pd.DataFrame(records)
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def build_acquisition_report(users_df, accounts_df, account_metrics=None):
    """Public entry-point used by generate_dashboard_data.py.

    Pulls Mailshake sends, joins against the provided Users+Accounts frames,
    and enriches with the (optional) in-memory account_metrics list.

    Returns (records, stats_dict). Callers handle serialisation/upload.
    """
    if not MAILSHAKE_API_KEY:
        log.warning("MAILSHAKE_API_KEY not set — skipping Mailshake acquisition.")
        return [], {"skipped": True, "reason": "no_api_key"}

    campaigns = list_all_campaigns()
    if not campaigns:
        log.warning("No campaigns returned from Mailshake — skipping.")
        return [], {"skipped": True, "reason": "no_campaigns"}

    sends_df = fetch_all_sends(campaigns)
    by_email, by_domain = build_owners_lookup(users_df, accounts_df)

    metrics_by_account = {}
    if account_metrics:
        metrics_by_account = {
            int(r["account_id"]): r
            for r in account_metrics
            if r.get("account_id") is not None
        }

    records = build_report(sends_df, by_email, by_domain, metrics_by_account)

    counts = {"full": 0, "partial": 0}
    for r in records:
        counts[r["match_type"]] += 1
    enriched = sum(1 for r in records if r.get("tier") is not None)

    stats = {
        "campaigns_seen": len(campaigns),
        "recipients_processed": int(len(sends_df)),
        "acquisition_rows": len(records),
        "matched_full": counts["full"],
        "matched_partial": counts["partial"],
        "enriched_from_metrics": enriched,
    }
    log.info("Acquisition report: %d rows (full=%d, partial=%d, enriched=%d)",
             len(records), counts["full"], counts["partial"], enriched)
    return records, stats


def run(dry_run=False, local_dir=None):
    start = time.time()

    if not MAILSHAKE_API_KEY:
        log.error("MAILSHAKE_API_KEY not set.")
        sys.exit(1)

    # 1. Authenticate Graph once — used for both fetching account_metrics and uploading.
    graph_token = None
    if SHAREPOINT_DRIVE_ID:
        graph_token = authenticate_graph()
        if not graph_token and not dry_run:
            sys.exit(1)

    # 2. Pull all campaigns and export their recipients.
    campaigns = list_all_campaigns()
    if not campaigns:
        log.error("No campaigns returned from Mailshake.")
        sys.exit(1)
    sends_df = fetch_all_sends(campaigns)

    # 3. Load TryBooking Users + Accounts from S3.
    log.info("Loading Users and Accounts from S3...")
    users_df = load_users()
    accounts_df = load_accounts()
    by_email, by_domain = build_owners_lookup(users_df, accounts_df)

    # 4. Load pre-computed account metrics (tier, lifetime revenue, etc.) from SharePoint.
    # Generated by generate_dashboard_data.py — may be a day stale, which is fine.
    metrics_by_account = load_account_metrics(graph_token) if graph_token else {}

    # 5. Build the report.
    records = build_report(sends_df, by_email, by_domain, metrics_by_account)

    counts = {"full": 0, "partial": 0}
    enriched = 0
    for r in records:
        counts[r["match_type"]] += 1
        if r.get("tier") is not None:
            enriched += 1
    log.info("Acquisition report: %d rows (full=%d, partial=%d, enriched=%d)",
             len(records), counts["full"], counts["partial"], enriched)

    # 4. Serialise outputs.
    report_json = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    report_csv = records_to_csv_bytes(records)

    run_state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "campaigns_seen": len(campaigns),
        "recipients_processed": int(len(sends_df)),
        "acquisition_rows": len(records),
        "matched_full": counts["full"],
        "matched_partial": counts["partial"],
        "enriched_from_metrics": enriched,
    }
    run_state_json = json.dumps(run_state, ensure_ascii=False, indent=2).encode("utf-8")

    outputs = {
        "mailshake_acquisition.json": report_json,
        "mailshake_acquisition.csv": report_csv,
        "mailshake_run_state.json": run_state_json,
    }

    # 5. Save locally and/or upload.
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        for name, data in outputs.items():
            path = os.path.join(local_dir, name)
            with open(path, "wb") as f:
                f.write(data)
            log.info("Saved %s (%.1f KB)", path, len(data) / 1024)

    if dry_run:
        log.info("Dry run — skipping SharePoint upload.")
    else:
        if not SHAREPOINT_DRIVE_ID:
            log.error("SHAREPOINT_DRIVE_ID not set — cannot upload.")
            sys.exit(1)
        if not graph_token:
            log.error("Graph auth failed — cannot upload.")
            sys.exit(1)
        failed = 0
        for name, data in outputs.items():
            if not upload_to_sharepoint(graph_token, name, data):
                failed += 1
        if failed:
            log.error("%d file(s) failed to upload.", failed)
            sys.exit(1)

    log.info("Done in %.1fs.", time.time() - start)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Skip SharePoint upload")
    parser.add_argument("--local-dir", help="Also save outputs to this local directory")
    args = parser.parse_args()
    run(dry_run=args.dry_run, local_dir=args.local_dir)


if __name__ == "__main__":
    main()
