#!/usr/bin/env python3
"""
Generate pre-computed dashboard data files and upload to SharePoint.

Produces daily aggregate JSON files from S3 data (Accounts, BookingData,
BookingDataAll, Users) for consumption by the reporting-dashboard app.

Output files (uploaded to SharePoint `Dashboard Data/` folder):
  - accounts.json        — domain → account names mapping (delegate checker)
  - daily_metrics.json   — per-day KPIs (new accounts, fees, revenue, etc.)
  - daily_by_gateway.json — per-day per-gateway breakdown
  - daily_by_industry.json — per-day per-industry breakdown
  - daily_by_region.json  — per-day per-region (postcode area) breakdown
  - account_targets.json  — monthly acquisition targets
  - metadata.json         — generation timestamp and record counts

Usage:
    python3 generate_dashboard_data.py              # Generate and upload
    python3 generate_dashboard_data.py --dry-run    # Generate locally only
    python3 generate_dashboard_data.py --local-dir ./output  # Save to local dir
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import msal
import pandas as pd
import requests

from modules.utils.config import UK_TZ
from modules.utils.data_loader import (
    load_accounts,
    load_booking_data,
    load_users,
    filter_successful_transactions,
)
from modules.utils.date_utils import get_latest_data_date
from modules.utils.industry_utils import filter_valid_industries

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard-data")

# === Configuration ===

# Azure / Microsoft Graph
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

# SharePoint
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")
SHAREPOINT_FOLDER = os.environ.get("DASHBOARD_SHAREPOINT_FOLDER", "Platform Data/Dashboard Data")

# Graph API
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# How far back to compute daily data (years)
LOOKBACK_YEARS = 2

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 2

# Gateway normalisation rules
GATEWAY_RULES = [
    (re.compile(r"Default", re.IGNORECASE), "TryBooking Gateway"),
    (re.compile(r"Stripe Connect", re.IGNORECASE), "Stripe"),
]

# UK postcode area regex — extracts the letter prefix (1 or 2 chars)
POSTCODE_AREA_RE = re.compile(r"^([A-Z]{1,2})\d", re.IGNORECASE)


# === Graph API Helpers ===

def authenticate_graph():
    """Authenticate to Microsoft Graph and return an access token."""
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        log.error("Azure credentials not set. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET")
        return None

    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        log.error("Graph auth failed: %s", result.get("error_description", result.get("error", "Unknown")))
        return None

    log.info("Authenticated to Microsoft Graph.")
    return result["access_token"]


def _request_with_retry(method, url, **kwargs):
    """HTTP request with retry logic for transient failures."""
    response = None
    for attempt in range(MAX_RETRIES):
        try:
            response = method(url, **kwargs)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
                log.warning("Throttled, waiting %ds...", retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    log.warning("Server error %d, retrying in %ds...", response.status_code, wait)
                    time.sleep(wait)
                    continue
            return response
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Request failed, retrying in %ds: %s", wait, e)
                time.sleep(wait)
            else:
                raise
    return response


def upload_to_sharepoint(token, filename, data_bytes):
    """Upload a file to SharePoint via Graph API (small file PUT)."""
    path = f"{SHAREPOINT_FOLDER}/{filename}" if SHAREPOINT_FOLDER else filename
    url = f"{GRAPH_BASE}/drives/{SHAREPOINT_DRIVE_ID}/root:/{path}:/content"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = _request_with_retry(requests.put, url, headers=headers, data=data_bytes)

    if response.status_code in (200, 201):
        log.info("Uploaded %s (%d bytes)", filename, len(data_bytes))
        return True

    log.error("Upload failed for %s: %d - %s", filename, response.status_code, response.text[:200])
    return False


# === Data Processing ===

def normalise_gateway(value):
    """Normalise a gateway group name using the standard rules."""
    if pd.isna(value):
        return "Unknown"
    s = str(value)
    for pattern, replacement in GATEWAY_RULES:
        if pattern.search(s):
            return replacement
    return s


def extract_postcode_area(postcode):
    """Extract the letter prefix from a UK postcode (e.g. 'SW1A 1AA' → 'SW')."""
    if pd.isna(postcode):
        return None
    m = POSTCODE_AREA_RE.match(str(postcode).strip())
    return m.group(1).upper() if m else None


def _normalise_id(series):
    """Convert an ID column to clean string (handles float IDs like 1.0 → '1')."""
    return pd.to_numeric(series, errors="coerce").astype("Int64").astype(str).str.replace("<NA>", "", regex=False)


def build_accounts_json(accounts_df, users_df):
    """
    Build domain → account names mapping.

    Uses the Users data to extract email domains, then maps each domain
    to the account name(s) from the Accounts data.
    """
    log.info("Building accounts.json...")

    # Get AccountId → AccountName lookup from accounts
    id_col = "Id" if "Id" in accounts_df.columns else "AccountId"
    accounts_df = accounts_df.copy()
    accounts_df["_id_str"] = _normalise_id(accounts_df[id_col])
    account_names = accounts_df.set_index("_id_str")["AccountName"].dropna().to_dict()

    # Extract domains from Users
    if "Username" not in users_df.columns:
        log.warning("Username column not found in users data, trying column index 3")
        if len(users_df.columns) > 3:
            users_df = users_df.copy()
            users_df["Username"] = users_df.iloc[:, 3]
        else:
            log.error("Cannot find email column in users data")
            return {}

    # Only rows with valid emails
    valid = users_df[users_df["Username"].str.contains("@", na=False)].copy()
    valid["domain"] = valid["Username"].str.split("@").str[-1].str.lower().str.strip()

    # Map AccountId to match accounts lookup
    acct_col = "AccountId" if "AccountId" in valid.columns else "AccountID"
    if acct_col not in valid.columns:
        log.error("No AccountId column in users data")
        return {}

    valid["_acct_str"] = _normalise_id(valid[acct_col])

    # Build domain → set of account names
    domain_map = {}
    for _, row in valid[["domain", "_acct_str"]].drop_duplicates().iterrows():
        domain = row["domain"]
        acct_id = row["_acct_str"]
        name = account_names.get(acct_id)
        if not domain or not name:
            continue
        if domain not in domain_map:
            domain_map[domain] = set()
        domain_map[domain].add(name)

    # Convert sets to sorted lists for JSON serialisation
    result = {d: sorted(names) for d, names in sorted(domain_map.items())}
    log.info("  %d unique domains mapped to accounts", len(result))
    return result


def build_daily_metrics(accounts_df, bookings_df, start_date, end_date):
    """
    Build per-day top-level metrics.

    Returns a list of dicts with keys:
      date, new_accounts, new_accounts_with_events, new_accounts_with_sales,
      total_fees, total_revenue, total_tickets, total_transactions
    """
    log.info("Building daily_metrics.json...")

    # Prepare accounts dates
    accounts_df = accounts_df.copy()
    accounts_df["DateTimeCreated"] = pd.to_datetime(accounts_df["DateTimeCreated"], errors="coerce", utc=True)
    if accounts_df["DateTimeCreated"].dt.tz is None:
        accounts_df["DateTimeCreated"] = accounts_df["DateTimeCreated"].dt.tz_localize("UTC")
    accounts_df["created_date"] = accounts_df["DateTimeCreated"].dt.tz_convert("Europe/London").dt.date

    # Prepare first event creation
    if "FirstEventCreation" in accounts_df.columns:
        accounts_df["FirstEventCreation"] = pd.to_datetime(
            accounts_df["FirstEventCreation"], errors="coerce", utc=True
        )

    # Prepare bookings
    bookings_df = bookings_df.copy()
    bookings_df["TransactionDate"] = pd.to_datetime(bookings_df["TransactionDate"], errors="coerce", utc=True)
    if bookings_df["TransactionDate"].dt.tz is None:
        bookings_df["TransactionDate"] = bookings_df["TransactionDate"].dt.tz_localize("UTC")
    bookings_df["txn_date"] = bookings_df["TransactionDate"].dt.tz_convert("Europe/London").dt.date

    # Filter to successful transactions only
    if "Status" in bookings_df.columns:
        bookings_df = bookings_df[bookings_df["Status"] == "Successful"]

    # Ensure numeric columns
    for col in ["PaymentReceived", "TicketQuantity"]:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)

    # Calculate TotalFees if not present
    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)
    existing_fee_cols = [c for c in fee_cols if c in bookings_df.columns]
    if existing_fee_cols:
        bookings_df["TotalFees"] = bookings_df[existing_fee_cols].sum(axis=1)

    # Daily account counts
    id_col = "Id" if "Id" in accounts_df.columns else "AccountId"
    acct_daily = (
        accounts_df
        .groupby("created_date")
        .agg(
            new_accounts=(id_col, "count"),
            new_accounts_with_events=("FirstEventCreation", lambda x: x.notna().sum()),
        )
        .reset_index()
        .rename(columns={"created_date": "date"})
    )

    # Determine which new accounts sold tickets (per day)
    # Build a set of account IDs that appear in bookings
    booking_account_ids = set(_normalise_id(bookings_df["AccountId"]).unique())

    # For each day's new accounts, count how many have sales
    accounts_df["_id_str"] = _normalise_id(accounts_df[id_col])
    acct_sales = (
        accounts_df
        .groupby("created_date")
        .apply(lambda g: (g["_id_str"].isin(booking_account_ids)).sum(), include_groups=False)
        .reset_index(name="new_accounts_with_sales")
        .rename(columns={"created_date": "date"})
    )

    # Daily booking aggregates
    booking_daily = (
        bookings_df
        .groupby("txn_date")
        .agg(
            total_fees=("TotalFees", "sum"),
            total_revenue=("PaymentReceived", "sum"),
            total_tickets=("TicketQuantity", "sum"),
            total_transactions=("TotalFees", "count"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date"})
    )

    # Generate a complete date range
    date_range = pd.date_range(start=start_date, end=end_date, freq="D")
    result_df = pd.DataFrame({"date": date_range.date})

    # Merge all
    result_df = result_df.merge(acct_daily, on="date", how="left")
    result_df = result_df.merge(acct_sales, on="date", how="left")
    result_df = result_df.merge(booking_daily, on="date", how="left")

    # Fill NaN with 0 for count/sum columns
    fill_cols = [
        "new_accounts", "new_accounts_with_events", "new_accounts_with_sales",
        "total_fees", "total_revenue", "total_tickets", "total_transactions",
    ]
    for col in fill_cols:
        if col in result_df.columns:
            result_df[col] = result_df[col].fillna(0)

    # Round financial columns
    result_df["total_fees"] = result_df["total_fees"].round(2)
    result_df["total_revenue"] = result_df["total_revenue"].round(2)

    # Convert int-like columns
    for col in ["new_accounts", "new_accounts_with_events", "new_accounts_with_sales",
                 "total_tickets", "total_transactions"]:
        if col in result_df.columns:
            result_df[col] = result_df[col].astype(int)

    # Convert date to string
    result_df["date"] = result_df["date"].astype(str)

    records = result_df.to_dict("records")
    log.info("  %d daily records", len(records))
    return records


def build_daily_by_gateway(bookings_df, start_date, end_date):
    """Build per-day per-gateway breakdown."""
    log.info("Building daily_by_gateway.json...")

    bookings_df = bookings_df.copy()
    bookings_df["TransactionDate"] = pd.to_datetime(bookings_df["TransactionDate"], errors="coerce", utc=True)
    if bookings_df["TransactionDate"].dt.tz is None:
        bookings_df["TransactionDate"] = bookings_df["TransactionDate"].dt.tz_localize("UTC")
    bookings_df["txn_date"] = bookings_df["TransactionDate"].dt.tz_convert("Europe/London").dt.date

    if "Status" in bookings_df.columns:
        bookings_df = bookings_df[bookings_df["Status"] == "Successful"]

    # Normalise gateway
    gateway_col = None
    for candidate in ["GatewayGroup", "Gateway Group"]:
        if candidate in bookings_df.columns:
            gateway_col = candidate
            break

    if gateway_col is None:
        log.warning("No gateway column found — skipping gateway breakdown")
        return []

    # Handle categorical
    if bookings_df[gateway_col].dtype.name == "category":
        bookings_df[gateway_col] = bookings_df[gateway_col].astype(str)
    bookings_df["gateway"] = bookings_df[gateway_col].apply(normalise_gateway)

    # Ensure numeric
    for col in ["PaymentReceived"]:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)

    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)
    existing = [c for c in fee_cols if c in bookings_df.columns]
    if existing:
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1)

    grouped = (
        bookings_df
        .groupby(["txn_date", "gateway"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            transactions=("TotalFees", "count"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["transactions"] = grouped["transactions"].astype(int)
    grouped["date"] = grouped["date"].astype(str)

    records = grouped.to_dict("records")
    log.info("  %d records", len(records))
    return records


def build_daily_by_industry(bookings_df, accounts_df, start_date, end_date):
    """Build per-day per-industry breakdown."""
    log.info("Building daily_by_industry.json...")

    bookings_df = bookings_df.copy()
    bookings_df["TransactionDate"] = pd.to_datetime(bookings_df["TransactionDate"], errors="coerce", utc=True)
    if bookings_df["TransactionDate"].dt.tz is None:
        bookings_df["TransactionDate"] = bookings_df["TransactionDate"].dt.tz_localize("UTC")
    bookings_df["txn_date"] = bookings_df["TransactionDate"].dt.tz_convert("Europe/London").dt.date

    if "Status" in bookings_df.columns:
        bookings_df = bookings_df[bookings_df["Status"] == "Successful"]

    # Merge industry from accounts (authoritative source)
    id_col = "Id" if "Id" in accounts_df.columns else "AccountId"
    acct_industry = accounts_df[[id_col, "Industry"]].copy()
    acct_industry["_acct_id"] = _normalise_id(acct_industry[id_col])
    acct_industry = acct_industry.drop(columns=[id_col])

    # Drop any existing Industry column from bookings
    if "Industry" in bookings_df.columns:
        bookings_df = bookings_df.drop(columns=["Industry"])

    bookings_df["_booking_acct_id"] = _normalise_id(bookings_df["AccountId"])
    bookings_df = bookings_df.merge(acct_industry, left_on="_booking_acct_id", right_on="_acct_id", how="left")
    bookings_df = bookings_df.drop(columns=["_acct_id", "_booking_acct_id"], errors="ignore")

    # Filter valid industries
    bookings_df = filter_valid_industries(bookings_df)

    # Ensure numeric
    for col in ["PaymentReceived", "TicketQuantity"]:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)

    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)
    existing = [c for c in fee_cols if c in bookings_df.columns]
    if existing:
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1)

    grouped = (
        bookings_df
        .groupby(["txn_date", "Industry"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            events=("EventId", "nunique"),
            tickets=("TicketQuantity", "sum"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date", "Industry": "industry"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["events"] = grouped["events"].astype(int)
    grouped["date"] = grouped["date"].astype(str)

    records = grouped.to_dict("records")
    log.info("  %d records", len(records))
    return records


def build_daily_by_region(bookings_df, start_date, end_date):
    """Build per-day per-region (postcode area) breakdown."""
    log.info("Building daily_by_region.json...")

    bookings_df = bookings_df.copy()
    bookings_df["TransactionDate"] = pd.to_datetime(bookings_df["TransactionDate"], errors="coerce", utc=True)
    if bookings_df["TransactionDate"].dt.tz is None:
        bookings_df["TransactionDate"] = bookings_df["TransactionDate"].dt.tz_localize("UTC")
    bookings_df["txn_date"] = bookings_df["TransactionDate"].dt.tz_convert("Europe/London").dt.date

    if "Status" in bookings_df.columns:
        bookings_df = bookings_df[bookings_df["Status"] == "Successful"]

    # Extract postcode area from EventPostcode
    if "EventPostcode" not in bookings_df.columns:
        log.warning("EventPostcode column not found — skipping regional breakdown")
        return []

    # Handle categorical
    if bookings_df["EventPostcode"].dtype.name == "category":
        bookings_df["EventPostcode"] = bookings_df["EventPostcode"].astype(str)

    bookings_df["region"] = bookings_df["EventPostcode"].apply(extract_postcode_area)

    # Drop rows with no valid region
    bookings_df = bookings_df[bookings_df["region"].notna()]

    # Ensure numeric
    for col in ["PaymentReceived"]:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)

    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        if col in bookings_df.columns:
            bookings_df[col] = pd.to_numeric(bookings_df[col], errors="coerce").fillna(0)
    existing = [c for c in fee_cols if c in bookings_df.columns]
    if existing:
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1)

    grouped = (
        bookings_df
        .groupby(["txn_date", "region"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            events=("EventId", "nunique"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["events"] = grouped["events"].astype(int)
    grouped["date"] = grouped["date"].astype(str)

    records = grouped.to_dict("records")
    log.info("  %d records", len(records))
    return records


def load_account_targets():
    """Load account targets from the local JSON file."""
    targets_file = os.path.join(os.path.dirname(__file__), "account_targets.json")
    try:
        with open(targets_file, "r") as f:
            data = json.load(f)
        targets = data.get("targets", {})
        # Convert string values to integers
        cleaned = {}
        for year, year_data in targets.items():
            cleaned[year] = {}
            if "monthly" in year_data:
                cleaned[year]["monthly"] = {}
                for month, val in year_data["monthly"].items():
                    try:
                        cleaned[year]["monthly"][month] = int(val)
                    except (ValueError, TypeError):
                        cleaned[year]["monthly"][month] = val
        log.info("Loaded account targets for years: %s", ", ".join(cleaned.keys()))
        return cleaned
    except FileNotFoundError:
        log.warning("account_targets.json not found")
        return {}
    except json.JSONDecodeError:
        log.warning("account_targets.json is invalid JSON")
        return {}


# === Main ===

def generate(dry_run=False, local_dir=None):
    """Generate all dashboard data files and optionally upload to SharePoint."""
    start_time = time.time()

    # Determine date range
    target_date = get_latest_data_date()
    today = datetime.now(UK_TZ).date()
    lookback_start = today.replace(year=today.year - LOOKBACK_YEARS, month=1, day=1)

    log.info("Generating dashboard data...")
    log.info("  Data date: %s", target_date.strftime("%Y-%m-%d"))
    log.info("  Lookback: %s to %s (%d years)", lookback_start, today, LOOKBACK_YEARS)

    # Load data from S3
    log.info("Loading data from S3...")

    accounts_df = load_accounts(target_date)
    log.info("  Accounts: %d records", len(accounts_df))

    users_df = load_users(target_date)
    log.info("  Users: %d records", len(users_df))

    booking_all_df = load_booking_data(target_date=target_date, data_type="BookingDataAll")
    log.info("  BookingDataAll: %d records", len(booking_all_df))

    booking_df = load_booking_data(target_date=target_date, data_type="BookingData")
    log.info("  BookingData: %d records", len(booking_df))

    # Combine bookings and deduplicate
    combined_bookings = pd.concat([booking_all_df, booking_df], ignore_index=True)
    if "BookingTransactionId" in combined_bookings.columns:
        before = len(combined_bookings)
        combined_bookings = combined_bookings.drop_duplicates(subset=["BookingTransactionId"], keep="last")
        log.info("  Combined bookings: %d records (%d duplicates removed)",
                 len(combined_bookings), before - len(combined_bookings))

    # Build each data file
    outputs = {}

    outputs["accounts.json"] = build_accounts_json(accounts_df, users_df)

    outputs["daily_metrics.json"] = build_daily_metrics(
        accounts_df, combined_bookings, lookback_start, today
    )

    outputs["daily_by_gateway.json"] = build_daily_by_gateway(
        combined_bookings, lookback_start, today
    )

    outputs["daily_by_industry.json"] = build_daily_by_industry(
        combined_bookings, accounts_df, lookback_start, today
    )

    outputs["daily_by_region.json"] = build_daily_by_region(
        combined_bookings, lookback_start, today
    )

    outputs["account_targets.json"] = load_account_targets()

    generation_duration = time.time() - start_time
    outputs["metadata.json"] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "data_from": str(lookback_start),
        "data_to": str(today),
        "generation_duration_seconds": round(generation_duration, 1),
        "record_counts": {
            "daily_metrics": len(outputs["daily_metrics.json"]),
            "daily_by_gateway": len(outputs["daily_by_gateway.json"]),
            "daily_by_industry": len(outputs["daily_by_industry.json"]),
            "daily_by_region": len(outputs["daily_by_region.json"]),
            "account_domains": len(outputs["accounts.json"]),
        },
    }

    # Serialise to JSON
    json_outputs = {}
    for filename, data in outputs.items():
        json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_outputs[filename] = json_bytes
        size_kb = len(json_bytes) / 1024
        log.info("  %s: %.1f KB", filename, size_kb)

    # Save locally if requested
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        for filename, data_bytes in json_outputs.items():
            path = os.path.join(local_dir, filename)
            with open(path, "wb") as f:
                f.write(data_bytes)
            log.info("Saved %s to %s", filename, path)

    # Upload to SharePoint
    if dry_run:
        log.info("Dry run — skipping SharePoint upload")
    else:
        if not SHAREPOINT_DRIVE_ID:
            log.error("SHAREPOINT_DRIVE_ID not set — cannot upload to SharePoint")
            sys.exit(1)

        token = authenticate_graph()
        if not token:
            log.error("Authentication failed — cannot upload to SharePoint")
            sys.exit(1)

        failed = 0
        for filename, data_bytes in json_outputs.items():
            if not upload_to_sharepoint(token, filename, data_bytes):
                failed += 1

        if failed:
            log.error("%d file(s) failed to upload", failed)
            sys.exit(1)

    total_time = time.time() - start_time
    log.info("Dashboard data generation complete in %.1fs", total_time)


def main():
    parser = argparse.ArgumentParser(description="Generate pre-computed dashboard data and upload to SharePoint")
    parser.add_argument("--dry-run", action="store_true", help="Generate locally only, don't upload")
    parser.add_argument("--local-dir", type=str, help="Save output files to a local directory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # If local-dir is set, also treat as dry-run unless explicitly uploading
    if args.local_dir and not args.dry_run:
        # Still upload, but also save locally
        pass

    generate(dry_run=args.dry_run, local_dir=args.local_dir)


if __name__ == "__main__":
    main()
