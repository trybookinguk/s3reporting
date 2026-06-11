"""
Configuration and constants for TryBooking tier calculation system.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import pytz

log = logging.getLogger(__name__)

# === ENVIRONMENT VARIABLES ===
# AWS Credentials - support both naming conventions
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
    "AWS_ACCESS_KEY"
)
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get(
    "AWS_SECRET_KEY"
)

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
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")

# Vero Credentials
VERO_API_KEY = os.environ.get("VERO_API_KEY")

# === S3 CONFIGURATION ===
S3_BUCKET = "produk-rdsextracts-438255373632"

# === OPERATIONAL SETTINGS ===
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# Directory for locally-saved report CSVs (tier updates, annual events,
# industry revenue). Defaults to ./reports relative to the working directory;
# on the Pi this is set to /root/reporting/reports via .env. Replaces the
# GitHub Actions artifact uploads that previously captured these files.
REPORTS_DIR = os.environ.get("REPORTS_DIR", "reports")

# === DATE AND TIMEZONE SETTINGS ===
UK_TZ = pytz.timezone("Europe/London")
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
    "Key Account": 99,  # Top 1%
    "High Value": 95,  # Top 5%
    "Tier 4": 75,  # Top 25%
    "Tier 3": 50,  # Top 50%
    "Tier 2": 25,  # Top 75%
}

# Minimum years loyalty for C+D+E path
MIN_YEARS_BY_TIER = {
    "Key Account": 8,
    "High Value": 7,
    "Tier 4": 5,
    "Tier 3": 3,
    "Tier 2": 2,
    "Tier 1": 1,
}

# === ACTIVITY THRESHOLDS ===
MIN_TICKETS_FOR_ACTIVE = 10  # Minimum tickets to be considered active

# Years of loyalty are capped at this value before percentile-ranking, so
# tenure beyond the cap doesn't keep stacking. Lower the cap to make
# "established" easier to reach; raise it to weight long-standing accounts more.
YEARS_LOYALTY_CAP = 5

# === EVENT FREQUENCY THRESHOLDS ===
EVENT_FREQUENCY_THRESHOLDS = {
    "Regular": 4,  # 4+ events
    "Occasional": 2,  # 2-3 events
    "Annual": 1,  # 1 event
    "Inactive": 0,  # 0 events
}

# === EMAIL SETTINGS ===
DEFAULT_RECIPIENT = "henry@trybooking.co.uk"
CC_RECIPIENT = ""

# === REPORT DISTRIBUTION LISTS ===
# Report email recipients are managed in a JSON file in SharePoint:
#
#     Platform Data / report_recipients.json
#
# Non-technical staff edit that file directly in SharePoint — no code, no git.
# See docs/notion/managing_report_emails.md for the step-by-step guide.
#
# The values below are the FALLBACK only. They are used if the SharePoint file
# is missing, unreadable, or malformed — so reports always go out even if the
# JSON gets broken. Keep them roughly in sync with SharePoint as a safety net,
# but SharePoint is the source of truth.
#
# When TEST_MODE is on, every report redirects to TEST_MODE_RECIPIENT, so a
# recipient change can be tested without reaching real inboxes.

TEST_MODE_RECIPIENT = "henry@trybooking.co.uk"

# Filename + folder of the SharePoint source of truth (root of Platform Data).
RECIPIENTS_FILENAME = "report_recipients.json"
RECIPIENTS_FOLDER = "Platform Data"

# Fallback lists — used only if the SharePoint file can't be loaded.
DISTRIBUTION_LISTS_FALLBACK = {
    "weekly_new_accounts": {
        "to": ["jules@trybooking.co.uk", "kathryn@trybooking.co.uk"],
        "cc": ["louise@trybooking.co.uk"],
    },
    "weekly_salesiq": {
        "to": ["jules@trybooking.co.uk", "kathryn@trybooking.co.uk"],
        "cc": [],
    },
    # Monthly Commission Report — overall summary copy, sent to whoever manages
    # commissions. Each salesperson is emailed their own report at their Zoho
    # login email (not via this list).
    "monthly_commission_summary": {
        "to": ["joan@trybooking.co.uk"],
        "cc": [],
    },
}

# Cached after the first SharePoint fetch so a single run hits Graph once.
_distribution_lists_cache = None


def _load_distribution_lists():
    """Fetch report_recipients.json from SharePoint, falling back to the
    hardcoded lists above if anything goes wrong.

    Cached for the lifetime of the process. Never raises — a failure to load
    must not stop reports going out, so it logs and returns the fallback.
    """
    global _distribution_lists_cache
    if _distribution_lists_cache is not None:
        return _distribution_lists_cache

    lists = DISTRIBUTION_LISTS_FALLBACK
    try:
        # Imported lazily so config.py has no hard dependency on the Graph stack.
        from . import sharepoint

        if not SHAREPOINT_DRIVE_ID:
            log.warning("SHAREPOINT_DRIVE_ID not set — using fallback recipient lists.")
        else:
            token = sharepoint.authenticate_graph()
            if not token:
                log.warning("Graph auth failed — using fallback recipient lists.")
            else:
                raw = sharepoint.download_file(
                    token, SHAREPOINT_DRIVE_ID, RECIPIENTS_FILENAME,
                    folder=RECIPIENTS_FOLDER,
                )
                if raw is None:
                    log.warning("%s not found in SharePoint — using fallback recipient lists.",
                                RECIPIENTS_FILENAME)
                else:
                    parsed = json.loads(raw)
                    # Basic shape check: every entry needs to/cc lists.
                    for key, entry in parsed.items():
                        if not isinstance(entry, dict) or "to" not in entry:
                            raise ValueError(f"entry '{key}' missing a 'to' list")
                        entry.setdefault("cc", [])
                    lists = parsed
                    log.info("Loaded report recipients from SharePoint (%d reports).", len(parsed))
    except Exception as e:
        log.warning("Could not load report recipients from SharePoint (%s) — using fallback.", e)
        lists = DISTRIBUTION_LISTS_FALLBACK

    _distribution_lists_cache = lists
    return lists


def get_recipients(report_key):
    """Return (to, cc) for a named report as comma-separated strings.

    Pulls the live lists from SharePoint (cached per run) and respects
    TEST_MODE, which redirects everything to TEST_MODE_RECIPIENT.
    """
    if TEST_MODE:
        return TEST_MODE_RECIPIENT, ""

    lists = _load_distribution_lists()
    entry = lists.get(report_key) or DISTRIBUTION_LISTS_FALLBACK.get(report_key, {"to": [], "cc": []})
    return ", ".join(entry.get("to", [])), ", ".join(entry.get("cc", []))

# Owner + CC list per v2 tier. Only Tier 1 / Tier 2 are owned — movements
# involving those tiers fire individual per-account notification emails.
# Tiers absent from this dict are deliberately unowned: no email.
# 'to' is the primary owner; 'cc' is a list of additional recipients copied
# on every email for that tier. Set 'cc' to [] for none.
# Update via PR — values intentionally left as TODO until CS team confirms.
TIER_OWNERS = {
    "Tier 1": {"to": ["joan@trybooking.co.uk", "kathryn@trybooking.co.uk"], "cc": []},
    "Tier 2": {"to": ["joan@trybooking.co.uk", "kathryn@trybooking.co.uk"], "cc": []},
}

# Accounts sitting on a tier boundary can flip-flop between T1/T2 on tiny
# daily ranking wiggles, firing a repetitive email each time. We suppress a
# movement if the account already changed owned tier within this many days —
# UNLESS the move is a genuine climb into a tier it has NOT held during the
# window (a sustained progression, which we always surface).
MOVEMENT_COOLDOWN_DAYS = 30

# Persistent bouncers earn a longer mute. When, over the trailing
# BOUNCER_WINDOW_DAYS, an account has completed at least BOUNCER_MIN_ROUNDTRIPS
# full round-trips of the SAME owned-tier pair (e.g. T1<->T2), moves *within
# that pair* are suppressed. The check is stateless and re-evaluated each run,
# so the mute self-clears once the round-trips age past the window. A downward
# breakout *out* of the pair (e.g. T2->T3) is always sent — it's a real
# demotion, not a bounce.
BOUNCER_WINDOW_DAYS = 90
BOUNCER_MIN_ROUNDTRIPS = 2

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
    "severe": 0.25,  # <25% of comparison period = 75%+ drop
    "significant": 0.5,  # <50% of comparison period = 50%+ drop
    "moderate": 0.80,  # <80% of comparison period = 20%+ drop
}

# Minimum revenue threshold for rapid drop detection (£)
MIN_REVENUE_FOR_RAPID_DROP = 200

# Quintile drop thresholds
QUINTILE_DROP_SCORING = {
    "severe": 4,  # Drop of 4+ quintiles
    "significant": 3,  # Drop of 3 quintiles
    "moderate": 2,  # Drop of 2 quintiles
}

# Zero revenue tolerance threshold
ZERO_REVENUE_COMMON_THRESHOLD = 0.3  # If >30% of industry has zero revenue

# New account lifecycle stages (in weeks)
ACCOUNT_LIFECYCLE_STAGES = {
    "new_building": 4,  # Weeks 1-4
    "new_expected": 8,  # Weeks 5-8
    "establishing": 26,  # Up to 6 months
    "maturing": 52,  # Up to 12 months
    "established": 52,  # 12+ months
}

# Comparison period types
COMPARISON_PERIOD_DAYS = {
    "current": 28,  # 4 weeks - current period for sudden drop detection
    "rolling_average": 84,  # 12 weeks - 3 months rolling average
    "yoy": 365,  # Year over year for seasonal accounts
}
