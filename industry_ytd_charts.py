#!/usr/bin/env python3
"""
Industry YTD comparison charts (2025 vs 2026 YTD).

Produces four grouped-bar PNG charts plus a backing CSV, all broken down by
account industry and comparing the full prior calendar year (2025) against the
current year to date (2026 YTD, i.e. 1 Jan -> today):

    1. Accounts that have taken a booking  (any Successful transaction)
    2. Accounts that have taken a PAID booking  (Successful + revenue > 0)
    3. High-value accounts  (Tier 1 + Tier 2, v2 tiers recomputed per year)
    4. New accounts  (created in the year, by DateTimeCreated)

Industry is taken from the authoritative Accounts report, not BookingData.

This is a read-only reporting script: it loads from S3, writes charts/CSV to
REPORTS_DIR, and updates nothing. Run via GitHub Actions or:

    python3 industry_ytd_charts.py

Honours NO_CACHE / TEST_DATE / REPORTS_DIR as per the rest of the codebase.
"""
import logging
import os
import time
from datetime import date, datetime

import pandas as pd

# Headless matplotlib backend must be selected before pyplot is imported. The
# mbr_charts module already does this; importing it first guarantees ordering.
from modules import mbr_charts
import matplotlib.pyplot as plt

from modules.utils.config import UK_TZ, REPORTS_DIR

# All output lands in its own subfolder under REPORTS_DIR. The combined
# side-by-side donuts go in combined/, and each period's standalone donuts go in
# their own period-named folder (e.g. "2025", "2026 YTD"); the backing CSV sits
# at the top level.
OUTPUT_DIR = os.path.join(REPORTS_DIR, "industry_ytd")
COMBINED_DIR = os.path.join(OUTPUT_DIR, "combined")


def _period_dir(period_label):
    """Folder for a period's standalone donuts, named after the period."""
    return os.path.join(OUTPUT_DIR, period_label)
from modules.utils.data_loader import (
    load_accounts,
    load_booking_data,
    get_s3_client,
)
from modules.utils.industry_utils import filter_valid_industries
from modules.utils.validation import validate_environment_variables
from modules.booking_aggregator import BookingAggregator
from modules.tier_calculator_v2 import calculate_composite_tiers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# The two comparison periods. PRIOR_YEAR is shown in full (Jan-Dec); CURRENT_YEAR
# is year-to-date. Derived from "today" so the script ages forward without edits.
PRIOR_LABEL = "2025"
CURRENT_LABEL = "2026 YTD"


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _report_date():
    """The data date we load for. On the 1st, S3's monthly files for the new
    month aren't published yet, so fall back to the previous day (matching
    zoho_tiers_v2.py). TEST_DATE overrides for local testing."""
    test_date = os.environ.get("TEST_DATE")
    if test_date:
        return pd.Timestamp(test_date, tz=UK_TZ).normalize()
    today = pd.Timestamp.now(UK_TZ).normalize()
    return today - pd.Timedelta(days=1) if today.day == 1 else today


def _load_combined_bookings(s3_client, report_date):
    """Load BookingDataAll + current-month BookingData, deduplicated. Mirrors
    the tested combine logic in zoho_tiers_v2.py."""
    logger.info("Loading BookingDataAll ...")
    booking_all = load_booking_data(s3_client, report_date, data_type="BookingDataAll")
    logger.info("Loading current-month BookingData ...")
    booking_month = load_booking_data(s3_client, report_date, data_type="BookingData")

    combined = pd.concat([booking_all, booking_month], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset="BookingTransactionId")
    logger.info(
        "Combined bookings: %s rows (%s duplicates removed)",
        f"{len(combined):,}", f"{before - len(combined):,}",
    )
    return combined


def _norm_account_id(series):
    """Normalise an AccountId column to a canonical integer-string key.

    The loader downcasts BookingData's AccountId to float32 (via
    optimize_dtypes), so str() gives '6230.0', while the Accounts report keeps
    it as int64 -> '6230'. A naive .astype(str) on each side therefore never
    matches. Coerce to a nullable integer first, then to string, so both sides
    land on '6230'. Non-numeric / missing ids become <NA> (won't match, which
    is what we want)."""
    return (
        pd.to_numeric(series, errors="coerce")
        .astype("Int64")
        .astype(str)
        .replace("<NA>", pd.NA)
    )


def _account_industry_lookup(accounts_df):
    """Series mapping canonical AccountId -> Industry, from the authoritative
    Accounts report."""
    if "Industry" not in accounts_df.columns:
        raise ValueError("Accounts report has no Industry column")
    accts = accounts_df.copy()
    accts["AccountId"] = _norm_account_id(accts["AccountId"])
    accts = accts[accts["AccountId"].notna()]
    return accts.set_index("AccountId")["Industry"]


def _add_industry(df, industry_lookup):
    """Attach authoritative Industry to a frame keyed by AccountId, then drop
    rows whose industry is missing/invalid (e.g. Ticket Purchaser)."""
    out = df.copy()
    out["AccountId"] = _norm_account_id(out["AccountId"])
    out["Industry"] = out["AccountId"].map(industry_lookup)
    return filter_valid_industries(out)


# ---------------------------------------------------------------------------
# Metric 1 & 2: accounts that took a (paid) booking, by industry
# ---------------------------------------------------------------------------

def _active_accounts_by_industry(bookings, industry_lookup):
    """Count distinct accounts with at least one Successful transaction in each
    year, split into 'any booking' and 'paid booking' (revenue > 0).

    Returns (any_df, paid_df) — each a DataFrame indexed by Industry with
    PRIOR_LABEL and CURRENT_LABEL columns (distinct account counts)."""
    b = bookings.copy()
    b["TransactionDate"] = pd.to_datetime(b["TransactionDate"], errors="coerce", utc=True)
    b = b[b["TransactionDate"].notna()]

    # Successful only (matches the aggregator's own filter).
    if "Status" in b.columns:
        b = b[b["Status"] == "Successful"]

    b["txn_year"] = b["TransactionDate"].dt.year
    b = b[b["txn_year"].isin([2025, 2026])]

    # Revenue components: same definition the tier engine uses.
    fee_cols = ["BookingFee", "CardFee", "ProcessingFee", "TicketFee"]
    for col in fee_cols:
        b[col] = pd.to_numeric(b.get(col), errors="coerce").fillna(0.0)
    b["Revenue"] = b[fee_cols].sum(axis=1)

    b = _add_industry(b, industry_lookup)

    def _count(frame):
        per_year = {}
        for yr, label in ((2025, PRIOR_LABEL), (2026, CURRENT_LABEL)):
            sub = frame[frame["txn_year"] == yr]
            per_year[label] = sub.groupby("Industry")["AccountId"].nunique()
        out = pd.DataFrame(per_year).fillna(0).astype(int)
        out.index.name = "Industry"
        return out

    any_df = _count(b)
    paid_df = _count(b[b["Revenue"] > 0])
    return any_df, paid_df


# ---------------------------------------------------------------------------
# Metric 3: high-value (Tier 1 + Tier 2 v2) by industry, recomputed per year
# ---------------------------------------------------------------------------

def _high_value_by_industry(bookings, industry_lookup):
    """Recompute v2 tiers as at each year-end and count Tier 1 + Tier 2 accounts
    by industry.

    For a given year Y the 'current' tier window is calendar year Y: we feed the
    aggregator only transactions up to 31 Dec Y (so lifetime metrics reflect
    history as at that point) with cutoff_365 set to 1 Jan Y, making the
    aggregator's 'current' period exactly year Y. cutoff_730 = 1 Jan (Y-1) gives
    the prior-period figures the calculator needs for its internal ranking; we
    only consume the resulting Current_Tier."""
    b = bookings.copy()
    b["TransactionDate"] = pd.to_datetime(b["TransactionDate"], errors="coerce", utc=True)
    b = b[b["TransactionDate"].notna()]
    # The aggregator classifies periods on the UTC calendar date
    # (TransactionDate.dt.date on a UTC-aware series), so slice on the same
    # basis here to keep the year boundary consistent with cutoff_365.
    b["tx_year_utc"] = b["TransactionDate"].dt.year

    def _tier_counts_for_year(year):
        # Transactions up to the end of `year` (YTD for the current year — the
        # frame already only contains data up to the report date).
        upto = b[b["tx_year_utc"] <= year]
        if upto.empty:
            return pd.Series(dtype=int)

        aggregator = BookingAggregator(
            cutoff_365=date(year, 1, 1),
            cutoff_730=date(year - 1, 1, 1),
            event_freq_cutoff_current=date(year, 1, 1),
            event_freq_cutoff_previous=date(year - 1, 1, 1),
            skip_event_metrics=True,  # v2 tiers don't use event metrics
        )

        def _chunks(df, size=100_000):
            for i in range(0, len(df), size):
                yield df.iloc[i:i + size].copy()

        metrics = aggregator.aggregate_bookings(_chunks(upto))
        tiers = calculate_composite_tiers(metrics)
        if tiers.empty:
            return pd.Series(dtype=int)

        hv = tiers[tiers["Current_Tier"].isin(["Tier 1", "Tier 2"])].copy()
        hv["AccountId"] = _norm_account_id(hv["AccountId"])
        hv["Industry"] = hv["AccountId"].map(industry_lookup)
        hv = filter_valid_industries(hv)
        return hv.groupby("Industry")["AccountId"].nunique()

    logger.info("Recomputing v2 tiers for 2025 window ...")
    prior = _tier_counts_for_year(2025)
    logger.info("Recomputing v2 tiers for 2026 YTD window ...")
    current = _tier_counts_for_year(2026)

    out = pd.DataFrame({PRIOR_LABEL: prior, CURRENT_LABEL: current}).fillna(0).astype(int)
    out.index.name = "Industry"
    return out


# ---------------------------------------------------------------------------
# Metric 4: new accounts created in year, by industry
# ---------------------------------------------------------------------------

def _new_accounts_by_industry(accounts_df):
    """Count accounts created in each year (by DateTimeCreated), by industry."""
    a = accounts_df.copy()
    if "DateTimeCreated" not in a.columns:
        raise ValueError("Accounts report has no DateTimeCreated column")
    a["DateTimeCreated"] = pd.to_datetime(a["DateTimeCreated"], errors="coerce", utc=True)
    a = a[a["DateTimeCreated"].notna()]
    a["created_year"] = a["DateTimeCreated"].dt.year
    a = filter_valid_industries(a)

    per_year = {}
    for yr, label in ((2025, PRIOR_LABEL), (2026, CURRENT_LABEL)):
        sub = a[a["created_year"] == yr]
        per_year[label] = sub.groupby("Industry")["AccountId"].nunique()
    out = pd.DataFrame(per_year).fillna(0).astype(int)
    out.index.name = "Industry"
    return out


# ---------------------------------------------------------------------------
# Charting
# ---------------------------------------------------------------------------

# Slices beyond this many (by combined volume) are folded into "Other" so the
# donuts stay readable rather than shattering into unlabelled slivers.
DONUT_TOP_N = 8
_OTHER_COLOUR = "#BDBDBD"


def _prepare_ordered(metrics_df):
    """Rank industries by combined volume, keep the top N and fold the rest into
    'Other'. Returns (ordered_df, industries, colours) with a shared colour/order
    used across every donut so a given industry stays the same colour and slot
    in all of them. Returns None if there's nothing to plot."""
    if metrics_df.empty:
        return None

    df = metrics_df.copy()
    df["_total"] = df[PRIOR_LABEL] + df[CURRENT_LABEL]
    df = df.sort_values("_total", ascending=False)

    ordered = df.head(DONUT_TOP_N).copy()
    rest = df.iloc[DONUT_TOP_N:]
    if not rest.empty:
        ordered.loc["Other", [PRIOR_LABEL, CURRENT_LABEL]] = [
            rest[PRIOR_LABEL].sum(), rest[CURRENT_LABEL].sum(),
        ]

    industries = ordered.index.tolist()
    colours = [
        _OTHER_COLOUR if ind == "Other" else mbr_charts._industry_colour(ind)
        for ind in industries
    ]
    return ordered, industries, colours


def _draw_donut(ax, ordered, period_label, colours):
    """Draw a single industry-mix donut for one period onto `ax`."""
    values = ordered[period_label].to_numpy(dtype=float)
    total = values.sum()
    if total <= 0:
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                fontsize=12, color="#999999", transform=ax.transAxes)
        ax.set_title(period_label, fontsize=13, fontweight="bold",
                     color=mbr_charts.BRAND_DARK, pad=12)
        ax.axis("off")
        return

    # Only label slices big enough to read (>=4%).
    def _autopct(pct):
        return f"{pct:.0f}%" if pct >= 4 else ""

    _wedges, _texts, autotexts = ax.pie(
        values,
        colors=colours,
        startangle=90,
        counterclock=False,
        autopct=_autopct,
        pctdistance=0.78,
        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=1.5),
    )
    for at in autotexts:
        at.set_fontsize(8.5)
        at.set_color("white")
        at.set_fontweight("bold")

    # Centre: period label + total count.
    ax.text(0, 0.12, period_label, ha="center", va="center",
            fontsize=12, fontweight="bold", color=mbr_charts.BRAND_DARK)
    ax.text(0, -0.12, f"{int(total):,}", ha="center", va="center",
            fontsize=18, fontweight="bold", color=mbr_charts.BRAND_ACCENT)
    ax.text(0, -0.30, "accounts", ha="center", va="center",
            fontsize=8.5, color="#777777")
    ax.set_aspect("equal")


def _legend(fig, industries, colours, **kwargs):
    """Attach a shared industry colour legend beneath the donut(s)."""
    handles = [plt.Rectangle((0, 0), 1, 1, color=col) for col in colours]
    fig.legend(handles, industries, loc="lower center", frameon=False,
               fontsize=9, **kwargs)


def _render_donuts(metrics_df, name, subtitle):
    """Render the combined side-by-side donut (in COMBINED_DIR) plus a standalone
    donut per period (each in its own period-named folder).

    `name` is the human-readable metric name used for both the chart title and
    the filenames (e.g. "Accounts with bookings" ->
    "Accounts with bookings - combined.png" and
    "Accounts with bookings - 2026 YTD.png"). Colours/order are shared across
    all images."""
    mbr_charts._load_poppins()

    prepared = _prepare_ordered(metrics_df)
    if prepared is None:
        logger.warning("No data for chart '%s' — skipping", name)
        return
    ordered, industries, colours = prepared

    # --- Combined: two donuts side by side ---
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.5))
    _draw_donut(axes[0], ordered, PRIOR_LABEL, colours)
    _draw_donut(axes[1], ordered, CURRENT_LABEL, colours)
    _legend(fig, industries, colours,
            ncol=min(len(industries), 5), bbox_to_anchor=(0.5, -0.02))
    fig.suptitle(name, fontsize=17, fontweight="bold",
                 color=mbr_charts.BRAND_DARK, x=0.02, ha="left", y=0.98)
    fig.text(0.02, 0.92, subtitle, fontsize=10.5, color="#555555", ha="left")
    fig.subplots_adjust(bottom=0.16, top=0.88)
    combined_path = os.path.join(COMBINED_DIR, f"{name} - combined.png")
    mbr_charts._save(fig, combined_path)
    logger.info("Saved chart: %s", combined_path)

    # --- Separate: one donut per period, each in its own period-named folder ---
    for period_label in (PRIOR_LABEL, CURRENT_LABEL):
        fig, ax = plt.subplots(figsize=(7, 7))
        _draw_donut(ax, ordered, period_label, colours)
        _legend(fig, industries, colours,
                ncol=min(len(industries), 3), bbox_to_anchor=(0.5, -0.01))
        fig.suptitle(f"{name} — {period_label}", fontsize=14, fontweight="bold",
                     color=mbr_charts.BRAND_DARK, y=0.98)
        fig.subplots_adjust(bottom=0.18, top=0.90)
        sep_path = os.path.join(_period_dir(period_label),
                                f"{name} - {period_label}.png")
        mbr_charts._save(fig, sep_path)
        logger.info("Saved chart: %s", sep_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start = time.time()
    validate_environment_variables(["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])

    report_date = _report_date()
    logger.info("Industry YTD charts — data date %s", report_date.strftime("%Y-%m-%d"))
    for d in (OUTPUT_DIR, COMBINED_DIR,
              _period_dir(PRIOR_LABEL), _period_dir(CURRENT_LABEL)):
        os.makedirs(d, exist_ok=True)

    s3_client = get_s3_client()

    accounts_df = load_accounts(report_date)
    industry_lookup = _account_industry_lookup(accounts_df)

    bookings = _load_combined_bookings(s3_client, report_date)

    # --- Compute the four metrics ---
    any_df, paid_df = _active_accounts_by_industry(bookings, industry_lookup)
    high_value_df = _high_value_by_industry(bookings, industry_lookup)
    new_accounts_df = _new_accounts_by_industry(accounts_df)

    # --- Charts ---
    sub = f"Full {PRIOR_LABEL} vs {CURRENT_LABEL} (as at {report_date.strftime('%d %b %Y')}), by industry"
    _render_donuts(any_df, "Accounts with bookings", sub)
    _render_donuts(paid_df, "Accounts with paid bookings", sub)
    _render_donuts(
        high_value_df,
        "High-value accounts (Tier 1 + Tier 2)",
        f"v2 tiers recomputed per year — full {PRIOR_LABEL} vs {CURRENT_LABEL}, by industry",
    )
    _render_donuts(new_accounts_df, "New accounts created", sub)

    # --- Backing CSV (long form: one row per industry/metric/year) ---
    frames = []
    for metric, df in (
        ("Accounts with booking", any_df),
        ("Accounts with paid booking", paid_df),
        ("High value (Tier 1+2)", high_value_df),
        ("New accounts", new_accounts_df),
    ):
        tidy = df.reset_index().melt(
            id_vars="Industry", var_name="Period", value_name="Accounts"
        )
        tidy.insert(0, "Metric", metric)
        frames.append(tidy)
    csv_df = pd.concat(frames, ignore_index=True)
    csv_path = os.path.join(OUTPUT_DIR, "industry_ytd_breakdown.csv")
    csv_df.to_csv(csv_path, index=False)
    logger.info("Saved data: %s", csv_path)

    elapsed = time.time() - start
    logger.info(
        "Done in %.1fs. Charts + CSV written to %s/",
        elapsed, OUTPUT_DIR,
    )
    print(
        f"\nGenerated donut charts in {OUTPUT_DIR}/:"
        f"\n  combined/      — 4 side-by-side ({PRIOR_LABEL} | {CURRENT_LABEL}) images"
        f"\n  {PRIOR_LABEL}/          — 4 standalone {PRIOR_LABEL} images"
        f"\n  {CURRENT_LABEL}/      — 4 standalone {CURRENT_LABEL} images"
        f"\n  industry_ytd_breakdown.csv"
    )
    print(f"Data date: {report_date.strftime('%Y-%m-%d')}  |  {elapsed:.1f}s")


if __name__ == "__main__":
    main()
