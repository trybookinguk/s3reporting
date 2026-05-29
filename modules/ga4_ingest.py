"""
GA4 → warehouse PPC ingestion.

Fetches the last N days of PPC conversions from Google Analytics 4 and writes
them into the `ppc_attribution` table of the SQLite warehouse. One row per
(date, event_id, campaign, source, medium) — sessions and users summed by
GA4 over each day.

The table is rebuilt for the last `lookback_days` only — older rows are
preserved so the table accumulates a growing window without re-fetching the
full history daily. Set lookback_days large on first run (e.g. 700) to seed
back to mid-2024; daily runs use the default 30 to absorb GA4's late
attribution updates without thrashing.

Failures (auth, network, rate limit, missing package) are logged and the
function returns 0 — `prepare_data.py` continues with the rest of the
warehouse build so PPC outage doesn't block the daily run.

Account/event enrichment (which account owns each event, event name, fees
per event) lives in the read-side SQL — the warehouse already has bookings,
so we don't need to denormalise it into ppc_attribution.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


_EVENT_ID_RE = re.compile(r"/uk/event/(\d+)/success", re.IGNORECASE)


def _load_campaign_filter() -> Optional[dict]:
    """Load the tracked-campaigns whitelist from config/ppc_campaigns.json.

    Returns a dict keyed by campaign_name with optional source/medium filters,
    or None if the file is missing/invalid (in which case all campaigns are
    tracked).
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "ppc_campaigns.json",
    )
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
        return {
            c["campaign_name"]: c
            for c in cfg.get("campaigns", [])
            if c.get("active", True)
        }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("ppc_campaigns.json not found or invalid (%s) — tracking all campaigns", e)
        return None


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create ppc_attribution if it doesn't already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ppc_attribution (
            conversion_date TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            campaign TEXT NOT NULL,
            source TEXT NOT NULL,
            medium TEXT NOT NULL,
            sessions INTEGER NOT NULL,
            users INTEGER NOT NULL,
            PRIMARY KEY (conversion_date, event_id, campaign, source, medium)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ppc_event ON ppc_attribution(event_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ppc_date ON ppc_attribution(conversion_date)"
    )


def refresh_ppc(conn: sqlite3.Connection, *, lookback_days: int = 30) -> int:
    """Refresh the last `lookback_days` of PPC attribution from GA4.

    Replaces all rows where conversion_date >= today - lookback_days; older
    rows are preserved. Returns the number of rows written for the window,
    or 0 if GA4 is unavailable / returned nothing.
    """
    ga4_key = os.environ.get("GA4_SERVICE_ACCOUNT_KEY")
    ga4_property = os.environ.get("GA4_PROPERTY_ID")
    if not ga4_key or not ga4_property:
        logger.info("GA4 credentials not configured — skipping PPC refresh")
        return 0

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric,
            FilterExpression, Filter, FilterExpressionList,
        )
        from google.oauth2 import service_account as sa
    except ImportError:
        logger.warning("google-analytics-data not installed — skipping PPC refresh")
        return 0

    tracked = _load_campaign_filter()
    start_date = (date.today() - timedelta(days=lookback_days)).isoformat()

    # Authenticate
    try:
        key_data = json.loads(ga4_key)
        creds = sa.Credentials.from_service_account_info(
            key_data, scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=creds)
    except Exception as e:
        logger.error("GA4 auth failed: %s", e)
        return 0

    logger.info("Querying GA4 PPC conversions from %s to today...", start_date)
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
            metrics=[Metric(name="sessions"), Metric(name="totalUsers")],
            date_ranges=[DateRange(start_date=start_date, end_date="today")],
            dimension_filter=FilterExpression(
                and_group=FilterExpressionList(expressions=[
                    FilterExpression(filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.CONTAINS,
                            value="/uk/event/", case_sensitive=False,
                        ),
                    )),
                    FilterExpression(filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.CONTAINS,
                            value="/success", case_sensitive=False,
                        ),
                    )),
                ]),
            ),
            limit=100000,
        )
        response = client.run_report(request)
    except Exception as e:
        logger.error("GA4 query failed: %s", e)
        return 0

    # Parse GA4 response into rows. (date, event_id, campaign, source, medium)
    # may appear multiple times in GA4's output (different pagePaths to the same
    # event); accumulate sessions/users on the composite key.
    accum: dict[tuple, dict] = {}
    parsed = 0
    skipped_nopath = 0
    skipped_filter = 0
    for row in response.rows:
        parsed += 1
        page_path = row.dimension_values[0].value
        campaign = row.dimension_values[1].value or "(not set)"
        source = row.dimension_values[2].value or "(not set)"
        medium = row.dimension_values[3].value or "(not set)"
        ga4_date = row.dimension_values[4].value  # YYYYMMDD

        m = _EVENT_ID_RE.search(page_path)
        if not m:
            skipped_nopath += 1
            continue

        if tracked is not None:
            if campaign not in tracked:
                skipped_filter += 1
                continue
            cfg = tracked[campaign]
            if cfg.get("source") and cfg["source"] != source:
                skipped_filter += 1
                continue
            if cfg.get("medium") and cfg["medium"] != medium:
                skipped_filter += 1
                continue

        event_id = int(m.group(1))
        conv_date = f"{ga4_date[:4]}-{ga4_date[4:6]}-{ga4_date[6:8]}"
        sessions = int(row.metric_values[0].value)
        users = int(row.metric_values[1].value)

        key = (conv_date, event_id, campaign, source, medium)
        cur = accum.get(key)
        if cur:
            cur["sessions"] += sessions
            cur["users"] += users
        else:
            accum[key] = {"sessions": sessions, "users": users}

    logger.info(
        "GA4 parsed=%d rows; tracked_events=%d (skipped %d non-event paths, "
        "%d off-filter)", parsed, len(accum), skipped_nopath, skipped_filter,
    )

    # Replace the window in one transaction.
    _ensure_table(conn)
    with conn:
        conn.execute(
            "DELETE FROM ppc_attribution WHERE conversion_date >= ?", (start_date,)
        )
        if accum:
            conn.executemany(
                """INSERT OR REPLACE INTO ppc_attribution
                   (conversion_date, event_id, campaign, source, medium, sessions, users)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (k[0], k[1], k[2], k[3], k[4], v["sessions"], v["users"])
                    for k, v in accum.items()
                ],
            )

    logger.info("ppc_attribution refreshed: %d rows in [%s, today]", len(accum), start_date)
    return len(accum)
