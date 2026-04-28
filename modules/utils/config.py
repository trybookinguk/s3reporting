"""
Configuration and constants for TryBooking tier calculation system.
"""
import os
from datetime import datetime, timedelta
import pytz

# === ENVIRONMENT VARIABLES ===
# AWS Credentials - support both naming conventions
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")

# Zoho CRM Credentials
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN")
ZOHO_PORTAL_NAME = os.environ.get("ZOHO_PORTAL_NAME")

ZOHO_DOMAIN = "https://www.zohoapis.com"

# Microsoft 365 / Azure (Graph API) Credentials
# Reuses the existing app registration that powers s3_to_sharepoint.py.
# The app must have Microsoft Graph "Mail.Send" application permission, scoped
# to the shared mailbox via an Exchange Application Access Policy.
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")
AZURE_SENDER_MAILBOX = os.environ.get("AZURE_SENDER_MAILBOX")

# Vero Credentials
VERO_API_KEY = os.environ.get("VERO_API_KEY")

# === S3 CONFIGURATION ===
S3_BUCKET = "produk-rdsextracts-438255373632"

# === OPERATIONAL SETTINGS ===
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# === DATE AND TIMEZONE SETTINGS ===
UK_TZ = pytz.timezone('Europe/London')
TODAY = datetime.now(UK_TZ).date()

# Tier calculations use rolling 365-day windows
CUTOFF_365 = TODAY - timedelta(days=365)
CUTOFF_730 = CUTOFF_365 - timedelta(days=365)

# Event frequency uses month boundaries for stability
# Include up to start of assessment month
EVENT_FREQ_CUTOFF_CURRENT = TODAY.replace(day=1) - timedelta(days=365)
EVENT_FREQ_CUTOFF_PREVIOUS = EVENT_FREQ_CUTOFF_CURRENT - timedelta(days=365)

# === TIER THRESHOLDS ===
# Percentile thresholds for tier classification
TIER_PERCENTILES = {
    "Key Account": 99,    # Top 1%
    "High Value": 95,     # Top 5%
    "Tier 4": 75,         # Top 25%
    "Tier 3": 50,         # Top 50%
    "Tier 2": 25,         # Top 75%
}

# Minimum years loyalty for C+D+E path
MIN_YEARS_BY_TIER = {
    "Key Account": 8,
    "High Value": 7,
    "Tier 4": 5,
    "Tier 3": 3,
    "Tier 2": 2,
    "Tier 1": 1
}

# === ACTIVITY THRESHOLDS ===
MIN_TICKETS_FOR_ACTIVE = 10  # Minimum tickets to be considered active

# Years of loyalty are capped at this value before percentile-ranking, so
# tenure beyond the cap doesn't keep stacking. Lower the cap to make
# "established" easier to reach; raise it to weight long-standing accounts more.
YEARS_LOYALTY_CAP = 5

# === EVENT FREQUENCY THRESHOLDS ===
EVENT_FREQUENCY_THRESHOLDS = {
    "Regular": 4,     # 4+ events
    "Occasional": 2,  # 2-3 events
    "Annual": 1,      # 1 event
    "Inactive": 0     # 0 events
}

# === EMAIL SETTINGS ===
DEFAULT_RECIPIENT = "alex@trybooking.co.uk"
CC_RECIPIENT = ""

# === REPORT SETTINGS ===
ANNUAL_EVENT_MIN_REVENUE = 100  # Minimum revenue for annual event filtering
ANNUAL_EVENT_OUTREACH_DAYS = 30  # Days before typical creation to reach out

# === REVENUE FACTOR SETTINGS ===
# Minimum accounts required for valid industry quintiles
MIN_ACCOUNTS_FOR_QUINTILES = 100

# Account maturity threshold (days)
MATURE_ACCOUNT_AGE_DAYS = 180

# Revenue drop thresholds (unified for all account types)
REVENUE_DROP_THRESHOLDS = {
    "severe": 0.25,      # <25% of comparison period = 75%+ drop
    "significant": 0.5,  # <50% of comparison period = 50%+ drop
    "moderate": 0.80     # <80% of comparison period = 20%+ drop
}

# Minimum revenue threshold for rapid drop detection (£)
MIN_REVENUE_FOR_RAPID_DROP = 200

# Quintile drop thresholds
QUINTILE_DROP_SCORING = {
    "severe": 4,         # Drop of 4+ quintiles
    "significant": 3,    # Drop of 3 quintiles
    "moderate": 2        # Drop of 2 quintiles
}

# Zero revenue tolerance threshold
ZERO_REVENUE_COMMON_THRESHOLD = 0.3  # If >30% of industry has zero revenue

# New account lifecycle stages (in weeks)
ACCOUNT_LIFECYCLE_STAGES = {
    "new_building": 4,      # Weeks 1-4
    "new_expected": 8,      # Weeks 5-8
    "establishing": 26,     # Up to 6 months
    "maturing": 52,         # Up to 12 months
    "established": 52       # 12+ months
}

# Comparison period types
COMPARISON_PERIOD_DAYS = {
    "current": 28,          # 4 weeks - current period for sudden drop detection
    "rolling_average": 84,  # 12 weeks - 3 months rolling average
    "yoy": 365             # Year over year for seasonal accounts
}