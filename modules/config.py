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

ZOHO_DOMAIN = "https://www.zohoapis.com"

# Mailgun Credentials
MAILGUN_SMTP_LOGIN = os.environ.get("MAILGUN_SMTP_LOGIN")
MAILGUN_SMTP_PASSWORD = os.environ.get("MAILGUN_SMTP_PASSWORD")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")

# === S3 CONFIGURATION ===
S3_BUCKET = "produk-rdsextracts-438255373632"

# === OPERATIONAL SETTINGS ===
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# === DATE AND TIMEZONE SETTINGS ===
UK_TZ = pytz.timezone('Europe/London')
TODAY = datetime.now(UK_TZ).date()
CUTOFF_365 = TODAY - timedelta(days=365)
CUTOFF_730 = CUTOFF_365 - timedelta(days=365)

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

# === EVENT FREQUENCY THRESHOLDS ===
EVENT_FREQUENCY_THRESHOLDS = {
    "Regular": 4,     # 4+ events
    "Occasional": 2,  # 2-3 events
    "Annual": 1,      # 1 event
    "Inactive": 0     # 0 events
}

# === EMAIL SETTINGS ===
SMTP_HOST = "smtp.mailgun.org"
SMTP_PORT = 587
DEFAULT_RECIPIENT = "alex@trybooking.co.uk"
CC_RECIPIENT = "louise@trybooking.co.uk" if not TEST_MODE else ""

# === REPORT SETTINGS ===
ANNUAL_EVENT_MIN_REVENUE = 100  # Minimum revenue for annual event filtering
ANNUAL_EVENT_OUTREACH_DAYS = 30  # Days before typical creation to reach out