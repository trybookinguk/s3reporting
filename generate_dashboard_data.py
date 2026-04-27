#!/usr/bin/env python3
"""
Generate pre-computed dashboard data files and upload to SharePoint.

Produces daily/monthly aggregate JSON files from S3 data (Accounts, BookingData,
BookingDataAll, Users) for consumption by the reporting-dashboard app.

Output files (uploaded to SharePoint `Dashboard Data/` folder):
  - accounts.json              — domain → account names mapping (delegate checker)
  - account_metrics.json       — one row per account with all dimensions & metrics
                                 (enables arbitrary client-side cross-tabulation)
  - ppc_report.json            — full PPC conversion report (matches ppc_reporting.py output)
  - daily_metrics.json         — per-day KPIs (new accounts, fees, revenue, etc.)
  - daily_by_gateway.json      — per-day per-gateway breakdown
  - daily_by_industry.json     — per-day per-industry breakdown
  - daily_by_region.json       — per-day per-region (postcode area) breakdown
  - daily_by_channel.json      — per-day sales channel breakdown (Box Office vs Online)
  - monthly_metrics.json       — per-month activation, cohort quality, averages
  - dormancy.json              — account dormancy snapshot by industry and age cohort
  - price_bands.json           — price band distribution by year
  - cohort_curves.json         — revenue trajectory by signup cohort and month-of-life
  - expansion_revenue.json     — revenue by account lifecycle stage (monthly)
  - concentration.json         — revenue/fee concentration by tier
  - account_daily.json         — compact per-account daily aggregates (for day-exact tier calc)
  - account_targets.json       — monthly acquisition targets
  - metadata.json              — generation timestamp and record counts

Usage:
    python3 generate_dashboard_data.py              # Generate and upload
    python3 generate_dashboard_data.py --dry-run    # Generate locally only
    python3 generate_dashboard_data.py --local-dir ./output  # Save to local dir
"""

import argparse
import calendar
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta
import msal
import numpy as np
import pandas as pd
import requests

from modules.utils.config import UK_TZ, MIN_TICKETS_FOR_ACTIVE, TIER_PERCENTILES
from modules.utils.data_loader import (
    load_accounts,
    load_booking_data,
    load_users,
    filter_successful_transactions,
)
from modules.utils.date_utils import get_latest_data_date
from modules.utils.industry_utils import filter_valid_industries
from modules.tier_calculator import determine_tier_from_percentiles
from modules.uk_regional_segmentation import POSTCODE_TO_REGION
from mailshake_acquisition import build_acquisition_report, records_to_csv_bytes

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
SHAREPOINT_FOLDER = "Platform Data/Dashboard Data"

# Graph API
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# No lookback limit — all available data is included

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

# Price band boundaries (GBP)
PRICE_BANDS = [
    (0, 0, "Free"),
    (0.01, 9.99, "£1-£9.99"),
    (10, 24.99, "£10-£24.99"),
    (25, 49.99, "£25-£49.99"),
    (50, float("inf"), "£50+"),
]

# Activity rating thresholds — simplified version of the full rating system in
# modules/activity_rating.py. We lack LastLogIn/AccountStatus in the dashboard
# data pipeline, so we approximate using transaction history only.
# The full 10-level system (with Outreach, Re-Activated, etc.) runs via zoho_tiers.py.
ACTIVITY_THRESHOLDS_DAYS = {
    "Active Paid": 180,       # Paid revenue within 180 days
    "Active Free": 180,       # Bookings within 180 days but £0 revenue
    "At Risk": 365,           # No bookings for 180-365 days
    "Churned": float("inf"),  # No bookings for 365+ days
}

# Account lifecycle stages (months since account creation)
LIFECYCLE_STAGES = [
    (0, 0, "New Account (Month 0)"),
    (1, 3, "Ramping (Months 1-3)"),
    (4, 12, "First Year (Months 4-12)"),
    (13, float("inf"), "Mature (Year 2+)"),
]


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

    log.info("Uploading %s to SharePoint path: %s", filename, path)
    response = _request_with_retry(requests.put, url, headers=headers, data=data_bytes)

    if response.status_code in (200, 201):
        resp_data = response.json()
        web_url = resp_data.get("webUrl", "unknown")
        parent_path = resp_data.get("parentReference", {}).get("path", "unknown")
        log.info("Uploaded %s (%d bytes) → %s (parent: %s)", filename, len(data_bytes), web_url, parent_path)
        return True

    log.error("Upload failed for %s: %d - %s", filename, response.status_code, response.text[:500])
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


def normalise_gateway_series(series):
    """Vectorised gateway normalisation for a pandas Series."""
    s = pd.Series(series.astype(str).values, index=series.index).fillna("Unknown")
    mask_default = s.str.contains("Default", case=False, na=False)
    mask_stripe = s.str.contains("Stripe Connect", case=False, na=False)
    s.loc[mask_default] = "TryBooking Gateway"
    s.loc[mask_stripe] = "Stripe"
    return s


def extract_postcode_area(postcode):
    """Extract the letter prefix from a UK postcode (e.g. 'SW1A 1AA' → 'SW')."""
    if pd.isna(postcode):
        return None
    m = POSTCODE_AREA_RE.match(str(postcode).strip())
    return m.group(1).upper() if m else None


def extract_postcode_area_series(series):
    """Vectorised postcode area extraction for a pandas Series."""
    s = pd.Series(series.values, index=series.index, dtype="object").fillna("").astype(str).str.strip().str.upper()
    extracted = s.str.extract(r"^([A-Z]{1,2})\d", expand=False)
    return extracted


def classify_sales_channel_series(series):
    """Vectorised sales channel classification for a pandas Series."""
    s = pd.Series(series.values, index=series.index, dtype="object").fillna("").astype(str).str.upper().str.strip()
    is_box_office = s.str.contains("CARD PRESENT", na=False) | (s == "CASH")
    return pd.Series(
        np.where(is_box_office, "Box Office", "Online"),
        index=series.index,
    )


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
        bookings_df["TotalFees"] = bookings_df[existing_fee_cols].sum(axis=1) / 1.20  # Ex-VAT

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
    booking_agg = {
        "total_fees": ("TotalFees", "sum"),
        "total_revenue": ("PaymentReceived", "sum"),
        "total_tickets": ("TicketQuantity", "sum"),
        "total_transactions": ("TotalFees", "count"),
        "accounts_selling": ("AccountId", "nunique"),
    }
    if "EventId" in bookings_df.columns:
        booking_agg["events_with_sales"] = ("EventId", "nunique")

    booking_daily = (
        bookings_df
        .groupby("txn_date")
        .agg(**booking_agg)
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
        "accounts_selling",
    ]
    if "events_with_sales" in result_df.columns:
        fill_cols.append("events_with_sales")
    for col in fill_cols:
        if col in result_df.columns:
            result_df[col] = result_df[col].fillna(0)

    # Round financial columns
    result_df["total_fees"] = result_df["total_fees"].round(2)
    result_df["total_revenue"] = result_df["total_revenue"].round(2)

    # Convert int-like columns
    int_cols = ["new_accounts", "new_accounts_with_events", "new_accounts_with_sales",
                "total_tickets", "total_transactions", "accounts_selling"]
    if "events_with_sales" in result_df.columns:
        int_cols.append("events_with_sales")
    for col in int_cols:
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
    bookings_df["gateway"] = normalise_gateway_series(bookings_df[gateway_col])

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
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1) / 1.20  # Ex-VAT

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
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1) / 1.20  # Ex-VAT

    grouped = (
        bookings_df
        .groupby(["txn_date", "Industry"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            events=("EventId", "nunique"),
            tickets=("TicketQuantity", "sum"),
            transactions=("TotalFees", "count"),
            accounts=("AccountId", "nunique"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date", "Industry": "industry"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    for col in ["tickets", "events", "transactions", "accounts"]:
        grouped[col] = grouped[col].astype(int)
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

    bookings_df["region"] = extract_postcode_area_series(bookings_df["EventPostcode"])

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
        bookings_df["TotalFees"] = bookings_df[existing].sum(axis=1) / 1.20  # Ex-VAT

    # Map postcode area to named region
    bookings_df["named_region"] = bookings_df["region"].str.upper().map(POSTCODE_TO_REGION).fillna("Unknown")

    grouped = (
        bookings_df
        .groupby(["txn_date", "region", "named_region"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            events=("EventId", "nunique"),
            tickets=("TicketQuantity", "sum"),
            transactions=("TotalFees", "count"),
            accounts=("AccountId", "nunique"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    for col in ["events", "tickets", "transactions", "accounts"]:
        grouped[col] = grouped[col].astype(int)
    grouped["date"] = grouped["date"].astype(str)

    records = grouped.to_dict("records")
    log.info("  %d records", len(records))
    return records


def classify_sales_channel(payment_type) -> str:
    """Classify a payment type as Box Office or Online."""
    if pd.isna(payment_type):
        return "Online"
    pt = str(payment_type).upper().strip()
    if "CARD PRESENT" in pt or pt == "CASH":
        return "Box Office"
    return "Online"


def classify_price_band(avg_ticket_price: float) -> str:
    """Classify an average ticket price into a price band."""
    for low, high, label in PRICE_BANDS:
        if low <= avg_ticket_price <= high:
            return label
    return "Unknown"


def classify_activity_rating(days_since_txn, has_paid_revenue_recent: bool) -> str:
    """
    Classify an account's activity rating based on days since last transaction
    and whether they have recent paid revenue.

    Simplified version of the full 10-level system in modules/activity_rating.py.
    We lack LastLogIn/AccountStatus here, so Unactivated, Never Logged In,
    Suspended or Closed, Outreach, and Re-Activated cannot be determined.
    """
    if pd.isna(days_since_txn):
        return "Never Transacted"
    days = int(days_since_txn)
    if days <= ACTIVITY_THRESHOLDS_DAYS["Active Paid"]:
        return "Active Paid" if has_paid_revenue_recent else "Active Free"
    if days <= ACTIVITY_THRESHOLDS_DAYS["At Risk"]:
        return "At Risk"
    return "Churned"


def classify_lifecycle_stage(months_since_creation: int) -> str:
    """Classify a transaction's revenue type based on account age at transaction time."""
    for low, high, label in LIFECYCLE_STAGES:
        if low <= months_since_creation <= high:
            return label
    return "Mature (Year 2+)"


def postcode_area_to_region(area: str) -> str:
    """Map a postcode area prefix to its UK region using the shared mapping."""
    if not area:
        return "Unknown"
    return POSTCODE_TO_REGION.get(area.upper(), "Unknown")


def _prepare_bookings(bookings_df):
    """Shared preparation for bookings: dates, status filter, numeric cols, fees."""
    df = bookings_df.copy()
    df["TransactionDate"] = pd.to_datetime(df["TransactionDate"], errors="coerce", utc=True)
    if df["TransactionDate"].dt.tz is None:
        df["TransactionDate"] = df["TransactionDate"].dt.tz_localize("UTC")
    df["txn_date"] = df["TransactionDate"].dt.tz_convert("Europe/London").dt.date

    if "Status" in df.columns:
        df = df[df["Status"] == "Successful"]

    for col in ["PaymentReceived", "TicketQuantity"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    existing = [c for c in fee_cols if c in df.columns]
    if existing:
        df["TotalFees"] = df[existing].sum(axis=1) / 1.20  # Strip VAT — all fees ex-VAT

    return df


def build_daily_by_channel(bookings_df, start_date, end_date):
    """Build per-day sales channel breakdown (Box Office vs Online)."""
    log.info("Building daily_by_channel.json...")

    df = _prepare_bookings(bookings_df)

    if "PaymentType" not in df.columns:
        log.warning("PaymentType column not found — skipping channel breakdown")
        return []

    if df["PaymentType"].dtype.name == "category":
        df["PaymentType"] = df["PaymentType"].astype(str)
    df["channel"] = classify_sales_channel_series(df["PaymentType"])

    grouped = (
        df
        .groupby(["txn_date", "channel"])
        .agg(
            fees=("TotalFees", "sum"),
            revenue=("PaymentReceived", "sum"),
            tickets=("TicketQuantity", "sum"),
            transactions=("TotalFees", "count"),
            accounts=("AccountId", "nunique"),
            events=("EventId", "nunique"),
        )
        .reset_index()
        .rename(columns={"txn_date": "date"})
    )

    grouped["fees"] = grouped["fees"].round(2)
    grouped["revenue"] = grouped["revenue"].round(2)
    for col in ["tickets", "transactions", "accounts", "events"]:
        grouped[col] = grouped[col].astype(int)
    grouped["date"] = grouped["date"].astype(str)

    records = grouped.to_dict("records")
    log.info("  %d records", len(records))
    return records


def build_monthly_metrics(accounts_df, bookings_df, start_date, end_date):
    """
    Build per-month metrics that require full-month context.

    Includes activation timing, tier qualification, cohort quality, averages,
    free/paid event split — everything from calculate_monthly_metrics in the
    EOY planning report.
    """
    log.info("Building monthly_metrics.json...")

    # Prepare accounts
    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)
    if accts["DateTimeCreated"].dt.tz is None:
        accts["DateTimeCreated"] = accts["DateTimeCreated"].dt.tz_localize("UTC")
    if "FirstEventCreation" in accts.columns:
        accts["FirstEventCreation"] = pd.to_datetime(accts["FirstEventCreation"], errors="coerce", utc=True)
    accts["_id"] = _normalise_id(accts[id_col])

    # Prepare bookings
    bk = _prepare_bookings(bookings_df)
    bk["_acct_id"] = _normalise_id(bk["AccountId"])

    # Build set of account IDs that have any bookings (for sales detection)
    booking_acct_ids = set(bk["_acct_id"].unique())

    # Generate months in range
    current = start_date.replace(day=1)
    end_month = end_date.replace(day=1)
    results = []

    while current <= end_month:
        year, month = current.year, current.month
        month_start = pd.Timestamp(year=year, month=month, day=1, tz="Europe/London")
        last_day = calendar.monthrange(year, month)[1]
        month_end = pd.Timestamp(year=year, month=month, day=last_day, hour=23, minute=59, second=59, tz="Europe/London")

        # --- New accounts this month ---
        new_accts = accts[
            (accts["DateTimeCreated"] >= month_start) &
            (accts["DateTimeCreated"] <= month_end)
        ]
        total_new = len(new_accts)

        # Activated (created events)
        activated_with_events = 0
        avg_days_to_first_event = None
        activated_7d = activated_30d = activated_90d = 0
        if "FirstEventCreation" in new_accts.columns:
            has_event = new_accts[new_accts["FirstEventCreation"].notna()]
            activated_with_events = len(has_event)
            if activated_with_events > 0:
                created = pd.to_datetime(has_event["DateTimeCreated"], utc=True)
                first_event = pd.to_datetime(has_event["FirstEventCreation"], utc=True)
                days = (first_event - created).dt.total_seconds() / 86400
                valid = days[(days >= 0) & (days <= 365)]
                if len(valid) > 0:
                    avg_days_to_first_event = round(valid.mean(), 1)
                    activated_7d = int((valid <= 7).sum())
                    activated_30d = int((valid <= 30).sum())
                    activated_90d = int((valid <= 90).sum())

        # New accounts with sales
        new_acct_ids = set(new_accts["_id"].dropna().unique())
        new_with_sales = len(new_acct_ids & booking_acct_ids)

        # Tier qualified (10+ tickets ever)
        new_acct_bookings = bk[bk["_acct_id"].isin(new_acct_ids)]
        tier_qualified = 0
        avg_days_to_first_sale = None
        if len(new_acct_bookings) > 0:
            tix_per_acct = new_acct_bookings.groupby("_acct_id")["TicketQuantity"].sum()
            tier_qualified = int((tix_per_acct >= MIN_TICKETS_FOR_ACTIVE).sum())

            # Days to first sale
            first_sale = new_acct_bookings.groupby("_acct_id")["TransactionDate"].min().reset_index()
            first_sale.columns = ["_acct_id", "first_sale_dt"]
            merged = first_sale.merge(
                new_accts[["_id", "DateTimeCreated"]].rename(columns={"_id": "_acct_id"}),
                on="_acct_id", how="left"
            )
            if len(merged) > 0:
                days_to_sale = (merged["first_sale_dt"] - merged["DateTimeCreated"]).dt.total_seconds() / 86400
                valid_ds = days_to_sale[(days_to_sale >= 0) & (days_to_sale <= 365)]
                if len(valid_ds) > 0:
                    avg_days_to_first_sale = round(valid_ds.mean(), 1)

        # --- Month's bookings ---
        month_bk = bk[
            (bk["txn_date"] >= month_start.date()) &
            (bk["txn_date"] <= month_end.date())
        ]
        total_tickets = int(month_bk["TicketQuantity"].sum()) if "TicketQuantity" in month_bk.columns else 0
        total_revenue = round(float(month_bk["PaymentReceived"].sum()), 2) if "PaymentReceived" in month_bk.columns else 0
        total_fees = round(float(month_bk["TotalFees"].sum()), 2) if "TotalFees" in month_bk.columns else 0
        total_txns = len(month_bk)

        events_with_sales = int(month_bk["EventId"].nunique()) if "EventId" in month_bk.columns else 0
        accounts_selling = int(month_bk["AccountId"].nunique()) if "AccountId" in month_bk.columns else 0

        # Averages
        avg_price_per_ticket = round(total_revenue / total_tickets, 2) if total_tickets > 0 else 0
        avg_txn_value = round(total_revenue / total_txns, 2) if total_txns > 0 else 0
        avg_tickets_per_booking = round(total_tickets / total_txns, 2) if total_txns > 0 else 0
        avg_account_fees = round(total_fees / accounts_selling, 2) if accounts_selling > 0 else 0
        avg_event_fees = round(total_fees / events_with_sales, 2) if events_with_sales > 0 else 0

        # Free vs Paid events
        free_events = paid_events = 0
        if "EventId" in month_bk.columns and "PaymentReceived" in month_bk.columns:
            event_rev = month_bk.groupby("EventId")["PaymentReceived"].sum()
            free_events = int((event_rev == 0).sum())
            paid_events = int((event_rev > 0).sum())

        # New accounts with paid events (at least one event where PaymentReceived > 0)
        new_accounts_with_paid_events = 0
        new_accounts_free_only = 0
        if len(new_acct_bookings) > 0 and "EventId" in new_acct_bookings.columns and "PaymentReceived" in new_acct_bookings.columns:
            acct_event_rev = new_acct_bookings.groupby(["_acct_id", "EventId"])["PaymentReceived"].sum()
            acct_has_paid = acct_event_rev.reset_index().groupby("_acct_id")["PaymentReceived"].max()
            new_accounts_with_paid_events = int((acct_has_paid > 0).sum())
            new_accounts_free_only = new_with_sales - new_accounts_with_paid_events

        # Repeat event accounts
        repeat_event_accounts = 0
        avg_events_per_active = 0
        if len(new_acct_bookings) > 0 and "EventId" in new_acct_bookings.columns:
            events_per_acct = new_acct_bookings.groupby("_acct_id")["EventId"].nunique()
            repeat_event_accounts = int((events_per_acct >= 2).sum())
            avg_events_per_active = round(events_per_acct.mean(), 2) if len(events_per_acct) > 0 else 0

        # Percentages
        pct_with_events = round(activated_with_events / total_new * 100, 1) if total_new > 0 else 0
        pct_tier_qualified = round(tier_qualified / total_new * 100, 1) if total_new > 0 else 0
        pct_free_events = round(free_events / events_with_sales * 100, 1) if events_with_sales > 0 else 0
        pct_repeat_events = round(repeat_event_accounts / activated_with_events * 100, 1) if activated_with_events > 0 else 0
        pct_with_paid_events = round(new_accounts_with_paid_events / total_new * 100, 1) if total_new > 0 else 0
        revenue_per_new_account = round(total_revenue / total_new, 2) if total_new > 0 else 0

        results.append({
            "year": year,
            "month": month,
            "month_name": calendar.month_name[month],
            # Account metrics
            "new_accounts": total_new,
            "activated_with_events": activated_with_events,
            "new_accounts_with_sales": new_with_sales,
            "new_accounts_with_paid_events": new_accounts_with_paid_events,
            "new_accounts_free_only": new_accounts_free_only,
            "tier_qualified": tier_qualified,
            "accounts_selling": accounts_selling,
            # Activation timing
            "avg_days_to_first_event": avg_days_to_first_event,
            "avg_days_to_first_sale": avg_days_to_first_sale,
            "activated_within_7d": activated_7d,
            "activated_within_30d": activated_30d,
            "activated_within_90d": activated_90d,
            # Transaction metrics
            "events_with_sales": events_with_sales,
            "total_tickets": total_tickets,
            "total_revenue": total_revenue,
            "total_fees": total_fees,
            "total_transactions": total_txns,
            # Averages
            "avg_price_per_ticket": avg_price_per_ticket,
            "avg_transaction_value": avg_txn_value,
            "avg_tickets_per_booking": avg_tickets_per_booking,
            "avg_account_fees": avg_account_fees,
            "avg_event_fees": avg_event_fees,
            # Event split
            "free_events": free_events,
            "paid_events": paid_events,
            "pct_free_events": pct_free_events,
            # Cohort quality
            "pct_with_events": pct_with_events,
            "pct_tier_qualified": pct_tier_qualified,
            "pct_with_paid_events": pct_with_paid_events,
            "pct_repeat_events": pct_repeat_events,
            "revenue_per_new_account": revenue_per_new_account,
            "repeat_event_accounts": repeat_event_accounts,
            "avg_events_per_active_account": avg_events_per_active,
        })

        current += relativedelta(months=1)

    log.info("  %d monthly records", len(results))
    return results


def build_dormancy(accounts_df, bookings_df):
    """
    Build activity rating snapshot — current status of all accounts by industry and age cohort.

    Uses the same rating categories as zoho_tiers.py (Active Paid, Active Free,
    At Risk, Churned, Never Transacted). We lack LastLogIn/AccountStatus data
    so Unactivated, Never Logged In, Suspended or Closed, Outreach, and
    Re-Activated cannot be determined in this pipeline.

    Returns dict with:
      - by_industry: [{industry, rating, count, pct}, ...]
      - by_age_cohort: [{age_cohort, rating, count, pct}, ...]
      - summary: [{rating, count, pct}, ...]
    """
    log.info("Building dormancy.json...")

    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)
    if accts["DateTimeCreated"].dt.tz is None:
        accts["DateTimeCreated"] = accts["DateTimeCreated"].dt.tz_localize("UTC")
    accts["_id"] = _normalise_id(accts[id_col])

    bk = _prepare_bookings(bookings_df)
    bk["_acct_id"] = _normalise_id(bk["AccountId"])

    today = pd.Timestamp.now(tz="Europe/London")
    cutoff_180 = today - pd.Timedelta(days=180)

    # Last transaction per account + recent paid revenue check
    last_txn = bk.groupby("_acct_id").agg(
        last_txn=("TransactionDate", "max"),
    ).reset_index()
    last_txn.columns = ["_id", "last_txn"]
    last_txn["days_since"] = (today - last_txn["last_txn"]).dt.total_seconds() / 86400

    # Check for paid revenue in last 180 days
    recent_bk = bk[bk["TransactionDate"] >= cutoff_180]
    recent_paid = recent_bk.groupby("_acct_id")["PaymentReceived"].sum().reset_index()
    recent_paid.columns = ["_id", "recent_revenue"]
    recent_paid["has_paid_recent"] = recent_paid["recent_revenue"] > 0

    # Merge with accounts
    dorm = accts[["_id", "DateTimeCreated", "Industry"]].copy()
    dorm["account_age_months"] = ((today - dorm["DateTimeCreated"]).dt.total_seconds() / (86400 * 30.44)).round(0)
    dorm = dorm.merge(last_txn, on="_id", how="left")
    dorm = dorm.merge(recent_paid[["_id", "has_paid_recent"]], on="_id", how="left")
    dorm["has_paid_recent"] = dorm["has_paid_recent"].fillna(False)

    # Classify using activity rating system
    dorm["rating"] = dorm.apply(
        lambda r: classify_activity_rating(r["days_since"], r["has_paid_recent"]),
        axis=1,
    )

    # Age cohort
    def age_cohort(months):
        if pd.isna(months) or months < 12:
            return "0-12m"
        elif months < 24:
            return "12-24m"
        elif months < 36:
            return "24-36m"
        elif months < 48:
            return "36-48m"
        return "48m+"
    dorm["age_cohort"] = dorm["account_age_months"].apply(age_cohort)

    total = len(dorm)

    # Summary
    summary_counts = dorm["rating"].value_counts()
    summary = [
        {"rating": s, "count": int(c), "pct": round(c / total * 100, 1)}
        for s, c in summary_counts.items()
    ]

    # By industry
    by_ind = dorm.groupby(["Industry", "rating"]).size().reset_index(name="count")
    ind_totals = dorm.groupby("Industry").size().reset_index(name="total")
    by_ind = by_ind.merge(ind_totals, on="Industry")
    by_ind["pct"] = (by_ind["count"] / by_ind["total"] * 100).round(1)
    by_ind = by_ind.rename(columns={"Industry": "industry"})
    by_ind_records = by_ind[["industry", "rating", "count", "pct"]].to_dict("records")

    # By age cohort
    by_age = dorm.groupby(["age_cohort", "rating"]).size().reset_index(name="count")
    age_totals = dorm.groupby("age_cohort").size().reset_index(name="total")
    by_age = by_age.merge(age_totals, on="age_cohort")
    by_age["pct"] = (by_age["count"] / by_age["total"] * 100).round(1)
    by_age_records = by_age[["age_cohort", "rating", "count", "pct"]].to_dict("records")

    result = {
        "summary": summary,
        "by_industry": by_ind_records,
        "by_age_cohort": by_age_records,
        "rating_system": "simplified_activity_rating",
        "rating_categories": [
            "Active Paid", "Active Free", "At Risk", "Churned", "Never Transacted",
        ],
        "note": "Simplified version — full 10-level ratings (incl. Outreach, Re-Activated, "
                "Unactivated, Never Logged In, Suspended or Closed) run via zoho_tiers.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("  %d accounts classified", total)
    return result


def build_price_bands(bookings_df, accounts_df):
    """
    Build price band distribution by year.

    Returns list of dicts: [{year, price_band, events, tickets, revenue, fees, accounts}, ...]
    """
    log.info("Building price_bands.json...")

    bk = _prepare_bookings(bookings_df)

    if "EventId" not in bk.columns:
        log.warning("EventId column not found — skipping price bands")
        return []

    bk["year"] = pd.to_datetime(bk["txn_date"]).dt.year

    # Merge industry from accounts
    id_col = "Id" if "Id" in accounts_df.columns else "AccountId"
    acct_industry = accounts_df[[id_col, "Industry"]].copy()
    acct_industry["_acct_id"] = _normalise_id(acct_industry[id_col])
    if "Industry" in bk.columns:
        bk = bk.drop(columns=["Industry"])
    bk["_acct_id"] = _normalise_id(bk["AccountId"])
    bk = bk.merge(acct_industry[["_acct_id", "Industry"]], on="_acct_id", how="left")

    # Aggregate to event level per year
    events = bk.groupby(["EventId", "year"]).agg(
        revenue=("PaymentReceived", "sum"),
        tickets=("TicketQuantity", "sum"),
        fees=("TotalFees", "sum"),
        account_id=("AccountId", "first"),
        industry=("Industry", "first"),
    ).reset_index()

    events["avg_ticket_price"] = events["revenue"] / events["tickets"].replace(0, 1)
    events["price_band"] = events["avg_ticket_price"].apply(classify_price_band)

    # Summary by year and price band
    grouped = events.groupby(["year", "price_band"]).agg(
        events=("EventId", "count"),
        tickets=("tickets", "sum"),
        revenue=("revenue", "sum"),
        fees=("fees", "sum"),
        accounts=("account_id", "nunique"),
    ).reset_index()

    grouped["events"] = grouped["events"].astype(int)
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["fees"] = grouped["fees"].round(2)
    grouped["accounts"] = grouped["accounts"].astype(int)
    grouped["year"] = grouped["year"].astype(int)

    # Also add by_industry breakdown
    by_industry = events.groupby(["year", "price_band", "industry"]).agg(
        events=("EventId", "count"),
        fees=("fees", "sum"),
    ).reset_index()
    by_industry["events"] = by_industry["events"].astype(int)
    by_industry["fees"] = by_industry["fees"].round(2)
    by_industry["year"] = by_industry["year"].astype(int)

    result = {
        "summary": grouped.to_dict("records"),
        "by_industry": by_industry.to_dict("records"),
    }
    log.info("  %d summary records, %d by-industry records",
             len(result["summary"]), len(result["by_industry"]))
    return result


def build_expansion_revenue(bookings_df, accounts_df):
    """
    Build revenue breakdown by account lifecycle stage (monthly).

    Lifecycle stages based on months since account creation at transaction time:
      - New Account (Month 0)
      - Ramping (Months 1-3)
      - First Year (Months 4-12)
      - Mature (Year 2+)

    Returns list of dicts: [{year_month, lifecycle_stage, revenue, fees, tickets, accounts}, ...]
    """
    log.info("Building expansion_revenue.json...")

    bk = _prepare_bookings(bookings_df)
    bk["year_month"] = pd.to_datetime(bk["txn_date"]).dt.to_period("M").astype(str)

    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)
    accts["_id"] = _normalise_id(accts[id_col])

    # Build account creation lookup — vectorised
    bk["_acct_id"] = _normalise_id(bk["AccountId"])
    acct_created_series = accts.set_index("_id")["DateTimeCreated"]

    # Map creation date to each booking row
    bk["_created"] = bk["_acct_id"].map(acct_created_series)

    # Vectorised month difference
    txn_months = pd.to_datetime(bk["TransactionDate"]).dt.to_period("M")
    created_months = pd.to_datetime(bk["_created"]).dt.to_period("M")
    bk["_months_since"] = (
        (txn_months.dt.year - created_months.dt.year) * 12 +
        (txn_months.dt.month - created_months.dt.month)
    ).clip(lower=0)

    # Vectorised lifecycle stage classification
    conditions = [
        bk["_created"].isna(),
        bk["_months_since"] == 0,
        bk["_months_since"] <= 3,
        bk["_months_since"] <= 12,
    ]
    choices = [
        "Unknown",
        "New Account (Month 0)",
        "Ramping (Months 1-3)",
        "First Year (Months 4-12)",
    ]
    bk["lifecycle_stage"] = np.select(conditions, choices, default="Mature (Year 2+)")
    bk = bk.drop(columns=["_created", "_months_since"])

    # Also classify sales channel if available
    has_channel = "PaymentType" in bk.columns
    if has_channel:
        bk["channel"] = classify_sales_channel_series(bk["PaymentType"])

    # Aggregate
    agg_cols = {
        "PaymentReceived": "sum",
        "TotalFees": "sum",
        "TicketQuantity": "sum",
        "AccountId": "nunique",
    }

    grouped = bk.groupby(["year_month", "lifecycle_stage"]).agg(agg_cols).reset_index()
    grouped.columns = ["year_month", "lifecycle_stage", "revenue", "fees", "tickets", "accounts"]
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["fees"] = grouped["fees"].round(2)
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["accounts"] = grouped["accounts"].astype(int)

    result = {"monthly": grouped.to_dict("records")}

    # Add channel breakdown if available
    if has_channel:
        channel_grouped = bk.groupby(["year_month", "lifecycle_stage", "channel"]).agg(agg_cols).reset_index()
        channel_grouped.columns = ["year_month", "lifecycle_stage", "channel", "revenue", "fees", "tickets", "accounts"]
        channel_grouped["revenue"] = channel_grouped["revenue"].round(2)
        channel_grouped["fees"] = channel_grouped["fees"].round(2)
        channel_grouped["tickets"] = channel_grouped["tickets"].astype(int)
        channel_grouped["accounts"] = channel_grouped["accounts"].astype(int)
        result["monthly_by_channel"] = channel_grouped.to_dict("records")

    total_records = len(result["monthly"]) + len(result.get("monthly_by_channel", []))
    log.info("  %d total records", total_records)
    return result


def build_cohort_curves(bookings_df, accounts_df):
    """
    Build cohort revenue curves: revenue trajectory by signup cohort and month-of-life.

    Cohorts are grouped by signup quarter. Tracks cumulative revenue, activation rate,
    and per-account metrics for the first 24 months of each cohort.

    Returns list of dicts: [{cohort, month_of_life, revenue, tickets, active_accounts,
                             revenue_per_account, cumulative_revenue}, ...]
    """
    log.info("Building cohort_curves.json...")

    bk = _prepare_bookings(bookings_df)
    bk["_acct_id"] = _normalise_id(bk["AccountId"])

    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)
    accts["_id"] = _normalise_id(accts[id_col])

    # Assign cohort quarter and creation date — vectorised
    accts["cohort_q"] = pd.to_datetime(accts["DateTimeCreated"]).dt.to_period("Q").astype(str)
    cohort_series = accts.set_index("_id")["cohort_q"]
    created_series = accts.set_index("_id")["DateTimeCreated"]

    # Total accounts per cohort (for activation rate)
    cohort_sizes = accts.groupby("cohort_q")["_id"].nunique().to_dict()

    bk["cohort"] = bk["_acct_id"].map(cohort_series)
    bk = bk[bk["cohort"].notna()]

    # Vectorised month-of-life calculation
    bk["_created"] = bk["_acct_id"].map(created_series)
    txn_periods = pd.to_datetime(bk["TransactionDate"]).dt.to_period("M")
    created_periods = pd.to_datetime(bk["_created"]).dt.to_period("M")
    bk["month_of_life"] = (
        (txn_periods.dt.year - created_periods.dt.year) * 12 +
        (txn_periods.dt.month - created_periods.dt.month)
    )
    bk = bk[bk["month_of_life"].notna() & (bk["month_of_life"] >= 0) & (bk["month_of_life"] <= 24)]
    bk["month_of_life"] = bk["month_of_life"].astype(int)
    bk = bk.drop(columns=["_created"])

    if len(bk) == 0:
        log.warning("No valid cohort data — skipping")
        return []

    grouped = bk.groupby(["cohort", "month_of_life"]).agg(
        revenue=("PaymentReceived", "sum"),
        fees=("TotalFees", "sum"),
        tickets=("TicketQuantity", "sum"),
        active_accounts=("_acct_id", "nunique"),
    ).reset_index()

    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["fees"] = grouped["fees"].round(2)
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["active_accounts"] = grouped["active_accounts"].astype(int)
    grouped["revenue_per_account"] = (grouped["revenue"] / grouped["active_accounts"].replace(0, 1)).round(2)
    grouped["cohort_size"] = grouped["cohort"].map(cohort_sizes).fillna(0).astype(int)
    grouped["activation_rate"] = (grouped["active_accounts"] / grouped["cohort_size"].replace(0, 1) * 100).round(1)

    # Cumulative revenue per cohort
    grouped = grouped.sort_values(["cohort", "month_of_life"])
    grouped["cumulative_revenue"] = grouped.groupby("cohort")["revenue"].cumsum().round(2)
    grouped["cumulative_fees"] = grouped.groupby("cohort")["fees"].cumsum().round(2)

    records = grouped.to_dict("records")
    log.info("  %d records across %d cohorts", len(records), grouped["cohort"].nunique())
    return records


def build_concentration(bookings_df, accounts_df):
    """
    Build revenue/fee concentration by tier.

    Uses the v2 composite tier system: Tiers 1-5, Free, Nil.
    Composite = 0.55×revenue_current_pct + 0.35×revenue_lifetime_pct + 0.10×years_loyalty_pct
    Tier 1 = top 2%, Tier 2 = top 10%, Tier 3 = top 25%, Tier 4 = top 50%, Tier 5 = bottom 50%.
    Tickets used as activation qualifier only (≥10).

    Returns dict with:
      - by_tier: [{tier, accounts, fees, revenue, pct_accounts, pct_fees, pct_revenue}, ...]
      - by_year_tier: [{year, tier, accounts, fees, revenue}, ...]
    """
    log.info("Building concentration.json...")

    bk = _prepare_bookings(bookings_df)
    bk["AccountId_num"] = pd.to_numeric(bk["AccountId"], errors="coerce")
    bk = bk[bk["AccountId_num"].notna()]
    bk["AccountId_int"] = bk["AccountId_num"].astype(int)
    bk["year"] = pd.to_datetime(bk["txn_date"]).dt.year

    today = datetime.now(UK_TZ).date()
    cutoff_365 = today - timedelta(days=365)

    # --- Calculate tier assignments using v2 logic ---
    # Lifetime metrics
    lifetime = bk.groupby("AccountId_int").agg(
        revenue_lifetime=("TotalFees", "sum"),
        years_loyalty=("year", "nunique"),
        tickets_lifetime=("TicketQuantity", "sum"),
    )

    # Current period metrics (last 365 days)
    current_bk = bk[bk["txn_date"] >= cutoff_365]
    if len(current_bk) > 0:
        current = current_bk.groupby("AccountId_int").agg(
            revenue_current=("TotalFees", "sum"),
            tickets_current=("TicketQuantity", "sum"),
        )
    else:
        current = pd.DataFrame(columns=["revenue_current", "tickets_current"])

    metrics = lifetime.join(current, how="left").fillna(0)
    metrics = metrics[metrics["tickets_lifetime"] > 0]

    # Fees are already ex-VAT from _prepare_bookings — no further stripping needed

    # Activation mask
    activated = (metrics["tickets_current"] >= MIN_TICKETS_FOR_ACTIVE)
    paid_activated = activated & (metrics["revenue_current"] > 0)
    free_activated = activated & (metrics["revenue_current"] == 0)

    # Percentile ranks (inverted: lower = better) among paid activated
    for col in ["revenue_current", "revenue_lifetime", "years_loyalty"]:
        metrics[f"{col}_pct"] = pd.Series(dtype="float64", index=metrics.index)
        if paid_activated.sum() > 0:
            subset = metrics.loc[paid_activated, col]
            metrics.loc[paid_activated, f"{col}_pct"] = (1 - subset.rank(pct=True, method="average")) * 100

    # Composite score
    metrics["composite"] = (
        0.55 * metrics["revenue_current_pct"].fillna(100) +
        0.35 * metrics["revenue_lifetime_pct"].fillna(100) +
        0.10 * metrics["years_loyalty_pct"].fillna(100)
    ).round(2)

    # Assign tiers
    TIER_BANDS = {"Tier 1": 2, "Tier 2": 10, "Tier 3": 25, "Tier 4": 50, "Tier 5": 100}
    metrics["tier"] = "Nil"

    if paid_activated.sum() > 0:
        active_scores = metrics.loc[paid_activated, "composite"]
        active_rank = active_scores.rank(pct=True, method="average") * 100
        for tier, threshold in sorted(TIER_BANDS.items(), key=lambda x: x[1], reverse=True):
            metrics.loc[active_rank.index[active_rank <= threshold], "tier"] = tier

    metrics.loc[free_activated, "tier"] = "Free"

    # --- Build concentration outputs ---
    # Map tier back to bookings
    tier_map = metrics["tier"].to_dict()
    bk["tier"] = bk["AccountId_int"].map(tier_map).fillna("Nil")

    # Overall concentration
    tier_summary = bk.groupby("tier").agg(
        accounts=("AccountId_int", "nunique"),
        fees=("TotalFees", "sum"),
        revenue=("PaymentReceived", "sum"),
    ).reset_index()

    total_accts = tier_summary["accounts"].sum()
    total_fees = tier_summary["fees"].sum()
    total_rev = tier_summary["revenue"].sum()

    tier_summary["pct_accounts"] = (tier_summary["accounts"] / total_accts * 100).round(1) if total_accts > 0 else 0
    tier_summary["pct_fees"] = (tier_summary["fees"] / total_fees * 100).round(1) if total_fees > 0 else 0
    tier_summary["pct_revenue"] = (tier_summary["revenue"] / total_rev * 100).round(1) if total_rev > 0 else 0
    tier_summary["fees"] = tier_summary["fees"].round(2)
    tier_summary["revenue"] = tier_summary["revenue"].round(2)
    tier_summary["accounts"] = tier_summary["accounts"].astype(int)

    # By year and tier
    year_tier = bk.groupby(["year", "tier"]).agg(
        accounts=("AccountId_int", "nunique"),
        fees=("TotalFees", "sum"),
        revenue=("PaymentReceived", "sum"),
    ).reset_index()
    year_tier["fees"] = year_tier["fees"].round(2)
    year_tier["revenue"] = year_tier["revenue"].round(2)
    year_tier["accounts"] = year_tier["accounts"].astype(int)
    year_tier["year"] = year_tier["year"].astype(int)

    # Tier distribution snapshot
    tier_dist = metrics["tier"].value_counts().to_dict()

    result = {
        "by_tier": tier_summary.to_dict("records"),
        "by_year_tier": year_tier.to_dict("records"),
        "tier_distribution": tier_dist,
        "tier_system": "v2_composite",
        "tier_bands": TIER_BANDS,
    }
    log.info("  %d accounts tiered, %d year-tier records",
             len(metrics), len(year_tier))
    return result


def build_account_daily(bookings_df):
    """
    Build compact per-account daily aggregates for day-exact tier calculation.

    Uses a sparse columnar format to minimise file size:
      {
        "epoch": "2014-07-18",
        "12345": {"d": [0, 15, 42], "f": [45.20, 120.50, 89.00], "t": [12, 45, 30],
                  "r": [5000.00, 8000.00, 2000.00], "e": [2, 3, 1]}
      }

    Where:
      - epoch: the earliest transaction date (day 0)
      - d: array of integer day offsets from epoch (only days with transactions)
      - f: fees ex-VAT for that day (matching index in d)
      - t: tickets sold for that day (matching index in d)
      - r: revenue (total ticket sales including VAT) for that day (matching index in d)
      - e: distinct events with transactions for that day (matching index in d)

    The browser picks a reference date, calculates the 365-day window,
    filters each account's d array to that range, and sums f, t, r, e.

    Returns a dict (not a list) for direct JSON serialisation.
    """
    log.info("Building account_daily.json...")

    bk = _prepare_bookings(bookings_df)
    bk["AccountId_int"] = pd.to_numeric(bk["AccountId"], errors="coerce")
    bk = bk[bk["AccountId_int"].notna()]
    bk["AccountId_int"] = bk["AccountId_int"].astype(int)

    # Aggregate to account-day level
    grouped = bk.groupby(["AccountId_int", "txn_date"]).agg(
        fees=("TotalFees", "sum"),
        tickets=("TicketQuantity", "sum"),
        revenue=("PaymentReceived", "sum"),
        events=("EventId", "nunique"),
    ).reset_index()

    grouped["fees"] = grouped["fees"].round(2)
    grouped["tickets"] = grouped["tickets"].astype(int)
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["events"] = grouped["events"].astype(int)

    # Determine epoch (earliest transaction date)
    epoch = grouped["txn_date"].min()
    epoch_str = str(epoch)

    # Build compact structure
    result = {"epoch": epoch_str}

    for aid, grp in grouped.groupby("AccountId_int"):
        grp_sorted = grp.sort_values("txn_date")
        days = [(d - epoch).days for d in grp_sorted["txn_date"]]
        fees = [round(f, 2) for f in grp_sorted["fees"]]
        tickets = [int(t) for t in grp_sorted["tickets"]]
        revenue = [round(r, 2) for r in grp_sorted["revenue"]]
        events = [int(e) for e in grp_sorted["events"]]
        result[str(aid)] = {"d": days, "f": fees, "t": tickets, "r": revenue, "e": events}

    n_accounts = len(result) - 1  # Exclude the epoch key
    n_entries = len(grouped)
    log.info("  %d account-day entries across %d accounts (epoch: %s)",
             n_entries, n_accounts, epoch_str)
    return result


def _fetch_ppc_ga4_data(bookings_df):
    """
    Fetch PPC conversion data from GA4, match to accounts via booking data.

    Returns a tuple of:
      - acct_summary: dict of AccountId → {ppc_campaign, ppc_source, ...} for account_metrics
      - ga4_matched: DataFrame with full matched GA4 data for ppc_report
      - ga4_unmatched: DataFrame with unmatched GA4 events

    If GA4 credentials are not configured, returns ({}, empty DataFrame, empty DataFrame).
    """
    empty = ({}, pd.DataFrame(), pd.DataFrame())

    ga4_key = os.environ.get("GA4_SERVICE_ACCOUNT_KEY")
    ga4_property = os.environ.get("GA4_PROPERTY_ID")

    if not ga4_key or not ga4_property:
        log.info("GA4 credentials not configured — skipping PPC data")
        return empty

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric,
            FilterExpression, Filter, FilterExpressionList,
        )
        from google.oauth2 import service_account as sa
    except ImportError:
        log.warning("google-analytics-data package not installed — skipping PPC data")
        return empty

    # Load campaign config
    campaigns_file = os.path.join(os.path.dirname(__file__), "config", "ppc_campaigns.json")
    try:
        with open(campaigns_file, "r") as f:
            campaign_config = json.load(f)
        tracked_campaigns = {
            c["campaign_name"]: c
            for c in campaign_config.get("campaigns", [])
            if c.get("active", True)
        }
    except (FileNotFoundError, json.JSONDecodeError):
        log.warning("ppc_campaigns.json not found or invalid — tracking all campaigns")
        tracked_campaigns = None

    # Authenticate
    try:
        key_data = json.loads(ga4_key)
        credentials = sa.Credentials.from_service_account_info(
            key_data,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=credentials)
    except Exception as e:
        log.error("GA4 authentication failed: %s", e)
        return empty

    # Query GA4 for success page conversions (with date dimension for the report)
    log.info("Querying GA4 for PPC conversion data...")
    try:
        request = RunReportRequest(
            property=f"properties/{ga4_property}",
            dimensions=[
                Dimension(name="pagePath"),
                Dimension(name="firstUserCampaignName"),
                Dimension(name="firstUserSource"),
                Dimension(name="firstUserMedium"),
                Dimension(name="date"),
            ],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            date_ranges=[DateRange(start_date="2024-06-01", end_date="today")],
            dimension_filter=FilterExpression(
                and_group=FilterExpressionList(
                    expressions=[
                        FilterExpression(
                            filter=Filter(
                                field_name="pagePath",
                                string_filter=Filter.StringFilter(
                                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                                    value="/uk/event/",
                                    case_sensitive=False,
                                ),
                            )
                        ),
                        FilterExpression(
                            filter=Filter(
                                field_name="pagePath",
                                string_filter=Filter.StringFilter(
                                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                                    value="/success",
                                    case_sensitive=False,
                                ),
                            )
                        ),
                    ]
                )
            ),
            limit=50000,
        )
        response = client.run_report(request)
    except Exception as e:
        log.error("GA4 query failed: %s", e)
        return empty

    # Parse GA4 response
    event_id_re = re.compile(r"/uk/event/(\d+)/success", re.IGNORECASE)
    ga4_rows = []
    for row in response.rows:
        page_path = row.dimension_values[0].value
        campaign = row.dimension_values[1].value or "(not set)"
        source = row.dimension_values[2].value or "(not set)"
        medium = row.dimension_values[3].value or "(not set)"
        date_str = row.dimension_values[4].value  # YYYYMMDD format
        sessions = int(row.metric_values[0].value)
        users = int(row.metric_values[1].value)

        m = event_id_re.search(page_path)
        if not m:
            continue

        # Filter to tracked PPC campaigns only
        if tracked_campaigns is not None:
            if campaign not in tracked_campaigns:
                continue
            cfg = tracked_campaigns[campaign]
            if cfg.get("source") and cfg["source"] != source:
                continue
            if cfg.get("medium") and cfg["medium"] != medium:
                continue

        ga4_rows.append({
            "event_id": int(m.group(1)),
            "campaign": campaign,
            "source": source,
            "medium": medium,
            "conversion_date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
            "sessions": sessions,
            "users": users,
        })

    if not ga4_rows:
        log.info("No PPC conversions found in GA4 data")
        return empty

    ga4_df = pd.DataFrame(ga4_rows)
    log.info("  %d PPC conversion rows from GA4 across %d unique events",
             len(ga4_df), ga4_df["event_id"].nunique())

    # Map EventId → AccountId via booking data
    bk = bookings_df.copy()
    bk["EventId_int"] = pd.to_numeric(bk["EventId"], errors="coerce")
    bk["AccountId_int"] = pd.to_numeric(bk["AccountId"], errors="coerce")
    bk = bk[bk["EventId_int"].notna() & bk["AccountId_int"].notna()]

    # Build event → account + event name + revenue lookups
    event_to_account = bk.groupby("EventId_int")["AccountId_int"].first().astype(int).to_dict()

    event_names = {}
    if "EventName" in bk.columns:
        event_names = bk.groupby("EventId_int")["EventName"].first().to_dict()

    # Per-event revenue (fees)
    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    existing_fees = [c for c in fee_cols if c in bk.columns]
    if existing_fees:
        for c in existing_fees:
            bk[c] = pd.to_numeric(bk[c], errors="coerce").fillna(0)
        bk["_fee_total"] = bk[existing_fees].sum(axis=1)
    else:
        bk["_fee_total"] = 0
    if "TicketQuantity" in bk.columns:
        bk["TicketQuantity"] = pd.to_numeric(bk["TicketQuantity"], errors="coerce").fillna(0)

    event_revenue = bk.groupby("EventId_int").agg(
        total_revenue=("_fee_total", "sum"),
        tickets_sold=("TicketQuantity", "sum"),
    ).to_dict("index")

    # Enrich GA4 data with booking info
    ga4_df["account_id"] = ga4_df["event_id"].map(event_to_account)
    ga4_df["event_name"] = ga4_df["event_id"].map(event_names).fillna("")
    ga4_df["total_revenue"] = ga4_df["event_id"].map(
        lambda eid: event_revenue.get(eid, {}).get("total_revenue", 0)
    )
    ga4_df["tickets_sold"] = ga4_df["event_id"].map(
        lambda eid: int(event_revenue.get(eid, {}).get("tickets_sold", 0))
    )

    matched = ga4_df[ga4_df["account_id"].notna()].copy()
    unmatched = ga4_df[ga4_df["account_id"].isna()].copy()

    if not matched.empty:
        matched["account_id"] = matched["account_id"].astype(int)

    log.info("  %d matched, %d unmatched events", len(matched), len(unmatched))

    # Build account summary for account_metrics.json
    acct_summary = {}
    if not matched.empty:
        acct_summary = (
            matched
            .groupby("account_id")
            .agg(
                ppc_campaign=("campaign", "first"),
                ppc_source=("source", "first"),
                ppc_medium=("medium", "first"),
                ppc_sessions=("sessions", "sum"),
                ppc_users=("users", "sum"),
                ppc_events=("event_id", "nunique"),
            )
            .to_dict("index")
        )
        log.info("  %d accounts matched to PPC campaigns", len(acct_summary))

    return acct_summary, matched, unmatched


def build_ppc_report(ga4_matched, ga4_unmatched, accounts_df, bookings_df):
    """
    Build the full PPC report matching the output of ppc_reporting.py.

    One row per account with campaign attribution, revenue, eligibility,
    and all fields needed to identify and action the account.

    Returns a list of dicts for ppc_report.json.
    """
    log.info("Building ppc_report.json...")

    if ga4_matched.empty and ga4_unmatched.empty:
        log.info("  No PPC data — skipping")
        return []

    # Prepare accounts lookup
    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["_aid"] = pd.to_numeric(accts[id_col], errors="coerce")
    accts = accts[accts["_aid"].notna()]
    accts["_aid"] = accts["_aid"].astype(int)
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)

    acct_lookup = accts.set_index("_aid")[
        [c for c in ["AccountName", "Industry", "SubIndustry", "DateTimeCreated"]
         if c in accts.columns]
    ].to_dict("index")

    # Count total events per account from booking data (for eligibility)
    bk = bookings_df.copy()
    bk["AccountId_int"] = pd.to_numeric(bk["AccountId"], errors="coerce")
    bk["EventId_int"] = pd.to_numeric(bk["EventId"], errors="coerce")
    bk = bk[bk["AccountId_int"].notna() & bk["EventId_int"].notna()]
    total_events_per_acct = bk.groupby("AccountId_int")["EventId_int"].nunique().to_dict()

    records = []

    # --- Matched accounts ---
    if not ga4_matched.empty:
        # Aggregate by unique events first (prevent revenue double-counting)
        unique_events = (
            ga4_matched
            .groupby(["account_id", "event_id"])
            .agg(
                event_name=("event_name", "first"),
                campaign=("campaign", "first"),
                source=("source", "first"),
                medium=("medium", "first"),
                conversion_date=("conversion_date", "min"),
                total_revenue=("total_revenue", "first"),
                tickets_sold=("tickets_sold", "first"),
            )
            .reset_index()
        )

        # GA4 metrics aggregated separately (sum across all sessions)
        ga_metrics = (
            ga4_matched
            .groupby("account_id")
            .agg(ga_sessions=("sessions", "sum"), ga_users=("users", "sum"))
        ).to_dict("index")

        # Account-level aggregation from unique events
        acct_agg = (
            unique_events
            .groupby("account_id")
            .agg(
                event_id=("event_id", "first"),
                event_name=("event_name", "first"),
                campaign=("campaign", "first"),
                source=("source", "first"),
                medium=("medium", "first"),
                conversion_date=("conversion_date", "min"),
                total_revenue=("total_revenue", "sum"),
                tickets_sold=("tickets_sold", "sum"),
                events_with_tickets=("event_id", "nunique"),
            )
            .reset_index()
        )

        for _, row in acct_agg.iterrows():
            aid = int(row["account_id"])
            acct = acct_lookup.get(aid, {})
            created = acct.get("DateTimeCreated")
            conv_date = row["conversion_date"]
            gam = ga_metrics.get(aid, {})

            # Eligibility: <90 days old at conversion OR only 1 event total
            total_events = total_events_per_acct.get(aid, 0)
            account_age_days = None
            is_eligible = False
            eligibility_reason = "Not evaluated"

            if created is not None and pd.notna(created) and conv_date:
                try:
                    conv_dt = pd.Timestamp(conv_date, tz="UTC")
                    age = (conv_dt - created).days
                    account_age_days = age
                    if age < 90:
                        is_eligible = True
                        eligibility_reason = f"New account ({age} days old)"
                    elif total_events <= 1:
                        is_eligible = True
                        eligibility_reason = "First event for account"
                    else:
                        eligibility_reason = f"Established account with {total_events} events"
                except Exception:
                    eligibility_reason = "Could not determine age"

            records.append({
                "account_id": aid,
                "account_name": acct.get("AccountName", ""),
                "industry": acct.get("Industry", "") if pd.notna(acct.get("Industry")) else "",
                "sub_industry": acct.get("SubIndustry", "") if pd.notna(acct.get("SubIndustry")) else "",
                "created_date": str(created.date()) if created is not None and pd.notna(created) else "",
                "event_id": str(row["event_id"]),
                "event_name": str(row["event_name"]),
                "campaign": row["campaign"],
                "source": row["source"],
                "medium": row["medium"],
                "conversion_date": row["conversion_date"],
                "events_with_tickets": int(row["events_with_tickets"]),
                "total_events": total_events,
                "total_revenue": round(float(row["total_revenue"]), 2),
                "tickets_sold": int(row["tickets_sold"]),
                "matched_status": True,
                "is_eligible": is_eligible,
                "eligibility_reason": eligibility_reason,
                "account_age_days": account_age_days,
                "ga_sessions": gam.get("ga_sessions", 0),
                "ga_users": gam.get("ga_users", 0),
            })

    # --- Unmatched events ---
    for _, row in ga4_unmatched.iterrows():
        records.append({
            "account_id": None,
            "account_name": "Manual Match Required",
            "industry": "",
            "sub_industry": "",
            "created_date": "",
            "event_id": str(row["event_id"]),
            "event_name": row.get("event_name", f"Event {row['event_id']}"),
            "campaign": row["campaign"],
            "source": row["source"],
            "medium": row["medium"],
            "conversion_date": row["conversion_date"],
            "events_with_tickets": 0,
            "total_events": 0,
            "total_revenue": 0,
            "tickets_sold": 0,
            "matched_status": False,
            "is_eligible": True,
            "eligibility_reason": "Manual match required — no booking data found",
            "account_age_days": None,
            "ga_sessions": int(row["sessions"]),
            "ga_users": int(row["users"]),
        })

    # Sort by conversion date descending
    records.sort(key=lambda r: r.get("conversion_date", ""), reverse=True)

    matched_count = sum(1 for r in records if r["matched_status"])
    log.info("  %d PPC accounts (%d matched, %d manual match required)",
             len(records), matched_count, len(records) - matched_count)
    return records


def build_account_metrics(accounts_df, bookings_df, ppc_data):
    """
    Build a single row per account with all dimensions and metrics.

    This enables arbitrary client-side cross-tabulation on any combination of
    dimensions (industry, region, tier, activity_rating, price_band, gateway,
    ppc_source, etc.) against any metrics (revenue, fees, tickets, etc.).

    Returns a list of dicts, one per account.
    """
    log.info("Building account_metrics.json...")

    today = pd.Timestamp.now(tz="Europe/London")
    today_date = today.date()
    cutoff_365 = today - pd.Timedelta(days=365)
    cutoff_180 = today - pd.Timedelta(days=180)

    # --- Prepare accounts ---
    accts = accounts_df.copy()
    id_col = "Id" if "Id" in accts.columns else "AccountId"
    accts["DateTimeCreated"] = pd.to_datetime(accts["DateTimeCreated"], errors="coerce", utc=True)
    if accts["DateTimeCreated"].dt.tz is None:
        accts["DateTimeCreated"] = accts["DateTimeCreated"].dt.tz_localize("UTC")
    accts["_id"] = pd.to_numeric(accts[id_col], errors="coerce")
    accts = accts[accts["_id"].notna()]
    accts["_id"] = accts["_id"].astype(int)

    # --- Prepare bookings ---
    bk = _prepare_bookings(bookings_df)
    bk["AccountId_int"] = pd.to_numeric(bk["AccountId"], errors="coerce")
    bk = bk[bk["AccountId_int"].notna()]
    bk["AccountId_int"] = bk["AccountId_int"].astype(int)
    bk["txn_dt"] = pd.to_datetime(bk["txn_date"])
    bk["_txn_year"] = bk["txn_dt"].dt.year

    # --- Lifetime aggregates per account ---
    lifetime = bk.groupby("AccountId_int").agg(
        fees_lifetime=("TotalFees", "sum"),
        revenue_lifetime=("PaymentReceived", "sum"),
        tickets_lifetime=("TicketQuantity", "sum"),
        txns_lifetime=("TotalFees", "count"),
        events_lifetime=("EventId", "nunique"),
        first_txn=("TransactionDate", "min"),
        last_txn=("TransactionDate", "max"),
        years_active=("_txn_year", "nunique"),
    )

    # --- Current period (last 365 days) ---
    bk_current = bk[bk["txn_date"] >= cutoff_365.date()]
    if len(bk_current) > 0:
        current = bk_current.groupby("AccountId_int").agg(
            fees_current=("TotalFees", "sum"),
            revenue_current=("PaymentReceived", "sum"),
            tickets_current=("TicketQuantity", "sum"),
            txns_current=("TotalFees", "count"),
            events_current=("EventId", "nunique"),
        )
    else:
        current = pd.DataFrame(
            columns=["fees_current", "revenue_current", "tickets_current",
                     "txns_current", "events_current"]
        )

    # --- Previous period (365-730 days ago) ---
    cutoff_730 = today - pd.Timedelta(days=730)
    bk_prev = bk[(bk["txn_date"] >= cutoff_730.date()) & (bk["txn_date"] < cutoff_365.date())]
    if len(bk_prev) > 0:
        previous = bk_prev.groupby("AccountId_int").agg(
            fees_previous=("TotalFees", "sum"),
            revenue_previous=("PaymentReceived", "sum"),
            tickets_previous=("TicketQuantity", "sum"),
        )
    else:
        previous = pd.DataFrame(
            columns=["fees_previous", "revenue_previous", "tickets_previous"]
        )

    # --- Dominant gateway per account ---
    gateway_col = None
    for candidate in ["GatewayGroup", "Gateway Group"]:
        if candidate in bk.columns:
            gateway_col = candidate
            break
    if gateway_col:
        if bk[gateway_col].dtype.name == "category":
            bk[gateway_col] = bk[gateway_col].astype(str)
        bk["_gw"] = normalise_gateway_series(bk[gateway_col])
        gw_counts = bk.groupby(["AccountId_int", "_gw"]).size().reset_index(name="n")
        dominant_gw = gw_counts.loc[gw_counts.groupby("AccountId_int")["n"].idxmax()]
        gw_lookup = dominant_gw.set_index("AccountId_int")["_gw"].to_dict()
    else:
        gw_lookup = {}

    # --- Box Office percentage per account ---
    if "PaymentType" in bk.columns:
        if bk["PaymentType"].dtype.name == "category":
            bk["PaymentType"] = bk["PaymentType"].astype(str)
        bk["_channel"] = classify_sales_channel_series(bk["PaymentType"])
        channel_counts = bk.groupby(["AccountId_int", "_channel"]).size().unstack(fill_value=0)
        if "Box Office" in channel_counts.columns:
            total_txns = channel_counts.sum(axis=1)
            pct_boxoffice = (channel_counts.get("Box Office", 0) / total_txns * 100).round(1)
            boxoffice_lookup = pct_boxoffice.to_dict()
        else:
            boxoffice_lookup = {}
    else:
        boxoffice_lookup = {}

    # --- Dominant price band per account ---
    if "EventId" in bk.columns:
        event_atp = bk.groupby(["AccountId_int", "EventId"]).agg(
            rev=("PaymentReceived", "sum"),
            tix=("TicketQuantity", "sum"),
        )
        event_atp["avg_ticket"] = event_atp["rev"] / event_atp["tix"].replace(0, 1)
        event_atp["band"] = event_atp["avg_ticket"].apply(classify_price_band)
        # Most common band per account
        band_mode = (
            event_atp.reset_index()
            .groupby(["AccountId_int", "band"])
            .size()
            .reset_index(name="n")
        )
        dominant_band = band_mode.loc[band_mode.groupby("AccountId_int")["n"].idxmax()]
        band_lookup = dominant_band.set_index("AccountId_int")["band"].to_dict()
    else:
        band_lookup = {}

    # --- Region from account postcode ---
    region_lookup = {}
    postcode_col = "AccountPostcode" if "AccountPostcode" in accts.columns else None
    if postcode_col is None:
        # Try from bookings
        if "AccountPostcode" in bk.columns:
            acct_pc = bk.groupby("AccountId_int")["AccountPostcode"].first()
            for aid, pc in acct_pc.items():
                area = extract_postcode_area(pc)
                if area:
                    region_lookup[aid] = postcode_area_to_region(area)
    else:
        for _, row in accts[["_id", postcode_col]].iterrows():
            area = extract_postcode_area(row[postcode_col])
            if area:
                region_lookup[int(row["_id"])] = postcode_area_to_region(area)

    # Also extract postcode area for fine-grained analysis
    area_lookup = {}
    if postcode_col and postcode_col in accts.columns:
        for _, row in accts[["_id", postcode_col]].iterrows():
            area = extract_postcode_area(row[postcode_col])
            if area:
                area_lookup[int(row["_id"])] = area
    elif "AccountPostcode" in bk.columns:
        acct_pc = bk.groupby("AccountId_int")["AccountPostcode"].first()
        for aid, pc in acct_pc.items():
            area = extract_postcode_area(pc)
            if area:
                area_lookup[aid] = area

    # --- Merge everything into account-level metrics ---
    metrics = lifetime.join(current, how="left").join(previous, how="left")
    # Fill numeric columns only (preserve NaT in datetime columns)
    numeric_cols = metrics.select_dtypes(include="number").columns
    metrics[numeric_cols] = metrics[numeric_cols].fillna(0)

    # Fees are already ex-VAT from _prepare_bookings — use directly for tier scoring
    fees_ex_vat_current = metrics["fees_current"]
    fees_ex_vat_lifetime = metrics["fees_lifetime"]

    # --- Composite tier scoring (v2) ---
    activated = metrics["tickets_current"] >= MIN_TICKETS_FOR_ACTIVE
    paid_activated = activated & (fees_ex_vat_current > 0)
    free_activated = activated & (fees_ex_vat_current == 0)

    for col_name, col_data in [
        ("fees_current_pct", fees_ex_vat_current),
        ("fees_lifetime_pct", fees_ex_vat_lifetime),
        ("years_active_pct", metrics["years_active"]),
    ]:
        metrics[col_name] = pd.Series(dtype="float64", index=metrics.index)
        if paid_activated.sum() > 0:
            subset = col_data[paid_activated]
            metrics.loc[paid_activated, col_name] = (
                (1 - subset.rank(pct=True, method="average")) * 100
            )

    metrics["composite_score"] = (
        0.55 * metrics["fees_current_pct"].fillna(100) +
        0.35 * metrics["fees_lifetime_pct"].fillna(100) +
        0.10 * metrics["years_active_pct"].fillna(100)
    ).round(2)

    # Assign tiers
    TIER_BANDS = {"Tier 1": 2, "Tier 2": 10, "Tier 3": 25, "Tier 4": 50, "Tier 5": 100}
    metrics["tier"] = "Nil"
    if paid_activated.sum() > 0:
        active_scores = metrics.loc[paid_activated, "composite_score"]
        active_rank = active_scores.rank(pct=True, method="average") * 100
        for tier, threshold in sorted(TIER_BANDS.items(), key=lambda x: x[1], reverse=True):
            metrics.loc[active_rank.index[active_rank <= threshold], "tier"] = tier
    metrics.loc[free_activated, "tier"] = "Free"

    # --- Activity rating ---
    days_since_txn = (today - metrics["last_txn"]).dt.total_seconds() / 86400

    # Check for paid revenue in recent 180 days
    bk_recent = bk[bk["txn_date"] >= cutoff_180.date()]
    recent_paid = set()
    if len(bk_recent) > 0:
        rp = bk_recent.groupby("AccountId_int")["PaymentReceived"].sum()
        recent_paid = set(rp[rp > 0].index)

    metrics["activity_rating"] = [
        classify_activity_rating(
            days_since_txn.get(aid),
            aid in recent_paid,
        )
        for aid in metrics.index
    ]

    # --- Build output rows ---
    # Account-level dimensions from accounts_df
    acct_dims = accts.set_index("_id")[
        [c for c in ["AccountName", "Industry", "SubIndustry", "DateTimeCreated"] if c in accts.columns]
    ]
    if "FirstEventCreation" in accts.columns:
        acct_dims["FirstEventCreation"] = accts.set_index("_id")["FirstEventCreation"]

    records = []
    for aid in metrics.index:
        row = {"account_id": int(aid)}

        # Dimensions from accounts
        if aid in acct_dims.index:
            ad = acct_dims.loc[aid]
            row["account_name"] = str(ad.get("AccountName", ""))
            row["industry"] = str(ad.get("Industry", "")) if pd.notna(ad.get("Industry")) else ""
            row["sub_industry"] = str(ad.get("SubIndustry", "")) if pd.notna(ad.get("SubIndustry")) else ""
            created = ad.get("DateTimeCreated")
            row["created_date"] = str(created.date()) if pd.notna(created) else ""
            first_event = ad.get("FirstEventCreation")
            if first_event is not None and pd.notna(first_event):
                fe_ts = pd.Timestamp(first_event)
                if fe_ts.tzinfo is None:
                    fe_ts = fe_ts.tz_localize("UTC")
                row["first_event_date"] = str(fe_ts.date())
                created_ts = pd.Timestamp(created) if not isinstance(created, pd.Timestamp) else created
                if created_ts.tzinfo is None:
                    created_ts = created_ts.tz_localize("UTC")
                days_to_event = (fe_ts - created_ts).total_seconds() / 86400
                row["days_to_first_event"] = round(days_to_event, 1) if days_to_event >= 0 else None
            else:
                row["first_event_date"] = None
                row["days_to_first_event"] = None
        else:
            row["account_name"] = ""
            row["industry"] = ""
            row["sub_industry"] = ""
            row["created_date"] = ""
            row["first_event_date"] = None
            row["days_to_first_event"] = None

        # Computed dimensions
        row["region"] = region_lookup.get(aid, "")
        row["postcode_area"] = area_lookup.get(aid, "")
        row["gateway"] = gw_lookup.get(aid, "")
        row["tier"] = metrics.at[aid, "tier"]
        row["activity_rating"] = metrics.at[aid, "activity_rating"]
        row["price_band"] = band_lookup.get(aid, "")
        row["pct_box_office"] = boxoffice_lookup.get(aid, 0)
        row["composite_score"] = float(metrics.at[aid, "composite_score"])

        # PPC dimensions
        ppc = ppc_data.get(aid, {})
        row["ppc_campaign"] = ppc.get("ppc_campaign", "")
        row["ppc_source"] = ppc.get("ppc_source", "")
        row["ppc_medium"] = ppc.get("ppc_medium", "")
        row["is_ppc"] = bool(ppc)

        # Lifecycle stage
        if row["created_date"]:
            created_d = pd.Timestamp(row["created_date"]).date()
            months_age = ((today_date - created_d).days / 30.44)
            row["lifecycle_stage"] = classify_lifecycle_stage(int(months_age))
            row["account_age_months"] = int(months_age)
        else:
            row["lifecycle_stage"] = ""
            row["account_age_months"] = None

        # Signup cohort (quarter)
        if row["created_date"]:
            row["signup_cohort"] = pd.Timestamp(row["created_date"]).to_period("Q").strftime("%YQ%q")
            row["signup_year"] = pd.Timestamp(row["created_date"]).year
        else:
            row["signup_cohort"] = ""
            row["signup_year"] = None

        # Metrics — current period (last 365 days), fees already ex-VAT from _prepare_bookings
        row["fees_current"] = round(float(metrics.at[aid, "fees_current"]), 2)
        row["revenue_current"] = round(float(metrics.at[aid, "revenue_current"]), 2)
        row["tickets_current"] = int(metrics.at[aid, "tickets_current"])
        row["txns_current"] = int(metrics.at[aid, "txns_current"])
        row["events_current"] = int(metrics.at[aid, "events_current"])

        # Metrics — previous period (365-730 days ago)
        row["fees_previous"] = round(float(metrics.at[aid, "fees_previous"]), 2)
        row["revenue_previous"] = round(float(metrics.at[aid, "revenue_previous"]), 2)
        row["tickets_previous"] = int(metrics.at[aid, "tickets_previous"])

        # Metrics — lifetime
        row["fees_lifetime"] = round(float(metrics.at[aid, "fees_lifetime"]), 2)
        row["revenue_lifetime"] = round(float(metrics.at[aid, "revenue_lifetime"]), 2)
        row["tickets_lifetime"] = int(metrics.at[aid, "tickets_lifetime"])
        row["txns_lifetime"] = int(metrics.at[aid, "txns_lifetime"])
        row["events_lifetime"] = int(metrics.at[aid, "events_lifetime"])
        row["years_active"] = int(metrics.at[aid, "years_active"])

        # PPC metrics
        row["ppc_sessions"] = ppc.get("ppc_sessions", 0)
        row["ppc_users"] = ppc.get("ppc_users", 0)
        row["ppc_events"] = ppc.get("ppc_events", 0)

        # Derived metrics
        row["avg_ticket_price"] = (
            round(row["revenue_lifetime"] / row["tickets_lifetime"], 2)
            if row["tickets_lifetime"] > 0 else 0
        )
        row["avg_fees_per_event"] = (
            round(row["fees_lifetime"] / row["events_lifetime"], 2)
            if row["events_lifetime"] > 0 else 0
        )
        row["fees_growth_pct"] = (
            round((row["fees_current"] - row["fees_previous"]) / row["fees_previous"] * 100, 1)
            if row["fees_previous"] > 0 else None
        )

        records.append(row)

    # Also include accounts with zero bookings (from accounts_df)
    booked_ids = set(metrics.index)
    for _, acct in accts.iterrows():
        aid = int(acct["_id"])
        if aid in booked_ids:
            continue

        created = acct["DateTimeCreated"]
        created_str = str(created.date()) if pd.notna(created) else ""

        ppc = ppc_data.get(aid, {})

        row = {
            "account_id": aid,
            "account_name": str(acct.get("AccountName", "")),
            "industry": str(acct.get("Industry", "")) if pd.notna(acct.get("Industry")) else "",
            "sub_industry": str(acct.get("SubIndustry", "")) if pd.notna(acct.get("SubIndustry")) else "",
            "created_date": created_str,
            "first_event_date": None,
            "days_to_first_event": None,
            "region": region_lookup.get(aid, ""),
            "postcode_area": area_lookup.get(aid, ""),
            "gateway": "",
            "tier": "Nil",
            "activity_rating": "Never Transacted",
            "price_band": "",
            "pct_box_office": 0,
            "composite_score": 100.0,
            "ppc_campaign": ppc.get("ppc_campaign", ""),
            "ppc_source": ppc.get("ppc_source", ""),
            "ppc_medium": ppc.get("ppc_medium", ""),
            "is_ppc": bool(ppc),
            "lifecycle_stage": "",
            "account_age_months": None,
            "signup_cohort": "",
            "signup_year": None,
            # All metrics zero
            "fees_current": 0, "revenue_current": 0, "tickets_current": 0,
            "txns_current": 0, "events_current": 0,
            "fees_previous": 0, "revenue_previous": 0, "tickets_previous": 0,
            "fees_lifetime": 0, "revenue_lifetime": 0, "tickets_lifetime": 0,
            "txns_lifetime": 0, "events_lifetime": 0, "years_active": 0,
            "ppc_sessions": ppc.get("ppc_sessions", 0),
            "ppc_users": ppc.get("ppc_users", 0),
            "ppc_events": ppc.get("ppc_events", 0),
            "avg_ticket_price": 0, "avg_fees_per_event": 0, "fees_growth_pct": None,
        }

        if created_str:
            months_age = (today_date - pd.Timestamp(created_str).date()).days / 30.44
            row["lifecycle_stage"] = classify_lifecycle_stage(int(months_age))
            row["account_age_months"] = int(months_age)
            row["signup_cohort"] = pd.Timestamp(created_str).to_period("Q").strftime("%YQ%q")
            row["signup_year"] = pd.Timestamp(created_str).year

        # Check if first event exists
        if "FirstEventCreation" in acct.index and pd.notna(acct.get("FirstEventCreation")):
            fe = pd.Timestamp(acct["FirstEventCreation"])
            if fe.tzinfo is None:
                fe = fe.tz_localize("UTC")
            row["first_event_date"] = str(fe.date())
            if pd.notna(created):
                cr = pd.Timestamp(created)
                if cr.tzinfo is None:
                    cr = cr.tz_localize("UTC")
                days = (fe - cr).total_seconds() / 86400
                row["days_to_first_event"] = round(days, 1) if days >= 0 else None

        records.append(row)

    log.info("  %d account records (%d with bookings, %d without)",
             len(records), len(booked_ids), len(records) - len(booked_ids))
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

    # Determine date range — use all available data (no lookback limit)
    target_date = get_latest_data_date()
    today = datetime.now(UK_TZ).date()

    log.info("Generating dashboard data...")
    log.info("  Data date: %s", target_date.strftime("%Y-%m-%d"))

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

    # Derive date range from actual data (no artificial lookback limit)
    acct_dates = pd.to_datetime(accounts_df["DateTimeCreated"], errors="coerce", utc=True)
    txn_dates = pd.to_datetime(combined_bookings["TransactionDate"], errors="coerce", utc=True)
    earliest_acct = acct_dates.min()
    earliest_txn = txn_dates.min()
    data_start = min(earliest_acct, earliest_txn).date() if pd.notna(earliest_acct) and pd.notna(earliest_txn) else today.replace(year=today.year - 3, month=1, day=1)
    log.info("  Data range: %s to %s", data_start, today)

    # Build each data file
    outputs = {}

    outputs["accounts.json"] = build_accounts_json(accounts_df, users_df)

    # Fetch PPC data from GA4 (skips gracefully if credentials unavailable)
    ppc_data, ppc_matched, ppc_unmatched = _fetch_ppc_ga4_data(combined_bookings)

    outputs["account_metrics.json"] = build_account_metrics(
        accounts_df, combined_bookings, ppc_data
    )

    outputs["ppc_report.json"] = build_ppc_report(
        ppc_matched, ppc_unmatched, accounts_df, combined_bookings
    )

    # Mailshake acquisitions — reuses the just-built account_metrics in memory.
    # Skipped gracefully if MAILSHAKE_API_KEY is not set.
    # Graph token (when available) is passed so the report can overlay the
    # frontend-recorded mailshake_match_decisions.json / mailshake_exclusions.json.
    mailshake_token = None if dry_run else authenticate_graph()
    mailshake_records, _ = build_acquisition_report(
        users_df, accounts_df, outputs["account_metrics.json"],
        graph_token=mailshake_token,
    )
    outputs["mailshake_acquisition.json"] = mailshake_records
    mailshake_csv_bytes = records_to_csv_bytes(mailshake_records) if mailshake_records else b""

    outputs["daily_metrics.json"] = build_daily_metrics(
        accounts_df, combined_bookings, data_start, today
    )

    outputs["daily_by_gateway.json"] = build_daily_by_gateway(
        combined_bookings, data_start, today
    )

    outputs["daily_by_industry.json"] = build_daily_by_industry(
        combined_bookings, accounts_df, data_start, today
    )

    outputs["daily_by_region.json"] = build_daily_by_region(
        combined_bookings, data_start, today
    )

    outputs["daily_by_channel.json"] = build_daily_by_channel(
        combined_bookings, data_start, today
    )

    outputs["monthly_metrics.json"] = build_monthly_metrics(
        accounts_df, combined_bookings, data_start, today
    )

    outputs["dormancy.json"] = build_dormancy(accounts_df, combined_bookings)

    outputs["price_bands.json"] = build_price_bands(combined_bookings, accounts_df)

    outputs["expansion_revenue.json"] = build_expansion_revenue(
        combined_bookings, accounts_df
    )

    outputs["cohort_curves.json"] = build_cohort_curves(
        combined_bookings, accounts_df
    )

    outputs["concentration.json"] = build_concentration(
        combined_bookings, accounts_df
    )

    outputs["account_daily.json"] = build_account_daily(combined_bookings)

    outputs["account_targets.json"] = load_account_targets()

    generation_duration = time.time() - start_time

    # Record counts — handle both list and dict outputs
    def _count(v):
        if isinstance(v, list):
            return len(v)
        if isinstance(v, dict):
            # Sum list lengths for nested structures, or count top-level keys
            total = 0
            for sub in v.values():
                if isinstance(sub, list):
                    total += len(sub)
            return total if total > 0 else len(v)
        return 0

    outputs["metadata.json"] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "data_from": str(data_start),
        "data_to": str(today),
        "generation_duration_seconds": round(generation_duration, 1),
        "record_counts": {
            name.replace(".json", ""): _count(data)
            for name, data in outputs.items()
            if name != "metadata.json"
        },
    }

    # Serialise to JSON
    json_outputs = {}
    for filename, data in outputs.items():
        json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_outputs[filename] = json_bytes
        size_kb = len(json_bytes) / 1024
        log.info("  %s: %.1f KB", filename, size_kb)

    # Mailshake CSV is a non-JSON companion to mailshake_acquisition.json.
    if mailshake_csv_bytes:
        json_outputs["mailshake_acquisition.csv"] = mailshake_csv_bytes
        log.info("  mailshake_acquisition.csv: %.1f KB", len(mailshake_csv_bytes) / 1024)

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

        # Verify uploads by listing the SharePoint folder contents
        verify_url = f"{GRAPH_BASE}/drives/{SHAREPOINT_DRIVE_ID}/root:/{SHAREPOINT_FOLDER}:/children?$select=name,size,lastModifiedDateTime"
        verify_resp = requests.get(verify_url, headers={"Authorization": f"Bearer {token}"})
        if verify_resp.status_code == 200:
            items = verify_resp.json().get("value", [])
            log.info("SharePoint folder '%s' contents after upload:", SHAREPOINT_FOLDER)
            for item in items:
                log.info("  %s (%s bytes, modified %s)", item["name"], item.get("size", "?"), item.get("lastModifiedDateTime", "?"))
        else:
            log.warning("Could not list SharePoint folder: %d - %s", verify_resp.status_code, verify_resp.text[:200])

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
