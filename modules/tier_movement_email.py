"""
Per-account tier-movement email composition and dispatch.

Renders one email per account whose tier moved into or out of the owned
tiers (Tier 1 / Tier 2). Recipients are derived from TIER_OWNERS by tier;
when both the previous and current tier have owners (e.g. T2 -> T1), both
get a separate email — they have different operational interest in the move.

The email body is built by filling placeholder tokens in
templates/tier_movement_email.html with per-account data, including an
inline SVG chart of the account's tier history rendered from
tier_history.json.
"""

import html
import logging
import os
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .tier_codes import TIER_ORDER_BEST_TO_WORST
from .tier_history import extract_account_history
from .utils.config import DEFAULT_RECIPIENT, TEST_MODE, TIER_OWNERS
from .utils.email_utils import send_html_email

log = logging.getLogger(__name__)

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "tier_movement_email.html",
)
TRYBOOKING_IMPERSONATE_URL = (
    "https://portal.trybooking.com/uk/admin/impersonate"
    ";redirectTo=%2Fdashboard"
    ";accountId={account_id}"
    ";userId=undefined"
)

# Y positions for the seven tier bands in the chart (matches preview file).
_BAND_Y = {
    "Tier 1": 10,
    "Tier 2": 24,
    "Tier 3": 38,
    "Tier 4": 52,
    "Tier 5": 66,
    "Free":   80,
    "Nil":    94,
}
_CHART_X_START = 40
_CHART_X_END = 550
_CHART_VIEW_HEIGHT = 114
_CORNER_RADIUS = 5  # px of curve at each tier-change corner


def _direction_label(previous_tier: Optional[str], current_tier: Optional[str]) -> str:
    """Map (previous, current) to one of: up, down, new."""
    if previous_tier is None:
        return "new"
    prev_idx = TIER_ORDER_BEST_TO_WORST.index(previous_tier)
    curr_idx = TIER_ORDER_BEST_TO_WORST.index(current_tier)
    return "up" if curr_idx < prev_idx else "down"


def _headline(account_name: str, previous_tier: Optional[str], current_tier: str) -> str:
    """Verb-led headline that conveys the movement at a glance."""
    if previous_tier is None:
        return f"{account_name} entered {current_tier}"
    direction = _direction_label(previous_tier, current_tier)
    if direction == "up":
        return f"{account_name} moved up to {current_tier}"
    return f"{account_name} dropped to {current_tier}"


def _format_date(value) -> Tuple[str, str]:
    """Return (display_string, css_class). Empty/missing values render as italic placeholder."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return ("No record", "empty")
    if isinstance(value, str):
        try:
            d = date.fromisoformat(value[:10])
        except ValueError:
            return (value, "")
    elif isinstance(value, (datetime, pd.Timestamp)):
        d = value.date() if hasattr(value, "date") else value
    elif isinstance(value, date):
        d = value
    else:
        return (str(value), "")

    today = datetime.now(timezone.utc).date()
    delta = (today - d).days
    if delta == 0:
        return (d.strftime("%-d %b %Y") + " (today)", "")
    if delta == 1:
        return (d.strftime("%-d %b %Y") + " (yesterday)", "")
    if delta > 0:
        return (d.strftime("%-d %b %Y") + f" ({delta:,} days ago)", "")
    return (d.strftime("%-d %b %Y") + f" (in {-delta:,} days)", "")


def _format_int(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _build_chart_svg(history_points: List[Tuple[str, Optional[str], Optional[float]]]) -> str:
    """Build a base64-embedded PNG chart of the tier history.

    Layout: tier bands (T1 -> Nil, top to bottom) as coloured horizontal
    regions. The line traces through them. Within each scored band (T1-T5)
    the line position is composite-score-driven — so trajectory inside a
    band is visible — but the y-axis labels only the tiers, not numeric
    scores. Free and Nil have no composite score and sit at fixed band
    centres.

    Y-axis transform: linear-then-log. Linear inside T1 (0-2) so the
    smallest band still has visible space; log-scaled from 2 onwards so
    T1/T2/T3 don't get crushed against the top. Free / Nil are appended
    below the score range as a fixed-width band each.

    Returns an <img> tag with a data: URI. PNG renders identically across
    every email client; the original SVG implementation got stripped by
    classic Outlook on Windows.
    """
    visible = [(d, t, s) for d, t, s in history_points if t is not None]
    if not visible:
        return '<p class="history-empty">No tier history recorded yet.</p>'

    import io
    import base64
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Visual coordinate system: y increases downward (worse). We render
    # bands top-to-bottom T1, T2, T3, T4, T5, Free, Nil. Each band gets
    # a y-range; within a scored band the line position is computed from
    # the composite score along a linear-then-log mapping that gives the
    # smaller (better) bands visibly more room than a pure linear scale.
    band_specs = [
        # (label, score_lo, score_hi, colour, y_lo, y_hi)
        # y axis: 0 (best) at top, ~7 (worst) at bottom.
        ("T1",   0,   2,   "#dcfce7", 0.0, 1.4),
        ("T2",   2,   10,  "#ecfccb", 1.4, 2.6),
        ("T3",   10,  25,  "#fef9c3", 2.6, 3.6),
        ("T4",   25,  50,  "#fed7aa", 3.6, 4.5),
        ("T5",   50,  100, "#fecaca", 4.5, 5.4),
        ("Free", None, None, "#e5e7eb", 5.4, 6.2),
        ("Nil",  None, None, "#d1d5db", 6.2, 7.0),
    ]
    # Lookup helpers
    band_by_label = {b[0]: b for b in band_specs}

    def score_to_y(label: str, score: Optional[float]) -> float:
        """Map (tier label, composite score) to a y-coordinate.

        For scored bands (T1-T5) the score is mapped to the band's
        [y_lo, y_hi] range via a log transform so the lower scores
        (better accounts) sit higher in the band. For Free/Nil the
        composite score is undefined; we just place the line at the
        band's centre.
        """
        spec = band_by_label.get(label)
        if spec is None:
            return 7.0
        _, lo, hi, _, y_lo, y_hi = spec
        if lo is None or score is None:
            return (y_lo + y_hi) / 2
        # Clamp to band bounds (calculator guarantees this but be safe)
        s = max(lo, min(hi, float(score)))
        # Log-style position within band: log1p so the start of the band
        # gets visible space even when lo is 0.
        if hi == lo:
            t = 0.5
        else:
            t = math.log1p(s - lo) / math.log1p(hi - lo)
        return y_lo + t * (y_hi - y_lo)

    xs = [date.fromisoformat(d) for d, _, _ in visible]
    ys = [score_to_y(t, s) for _, t, s in visible]

    fig, ax = plt.subplots(figsize=(7.0, 3.2), dpi=140)
    fig.patch.set_facecolor("#fafbfc")
    ax.set_facecolor("#fafbfc")

    # Render bands as solid horizontal regions.
    for label, _lo, _hi, colour, y_lo, y_hi in band_specs:
        ax.axhspan(y_lo, y_hi, facecolor=colour, alpha=0.7, zorder=0)

    # The line — TryBooking accent colour, slightly thicker so it reads
    # against the busy banded background.
    ax.plot(xs, ys, color="#0589A3", linewidth=2.2,
            solid_joinstyle="round", solid_capstyle="round", zorder=3)
    ax.plot([xs[-1]], [ys[-1]], marker="o", markersize=7,
            color="#0589A3", zorder=4)

    # Y-axis: only band labels, at band centres. Free uses the warm tint
    # we used elsewhere in the design.
    tick_positions = [(spec[4] + spec[5]) / 2 for spec in band_specs]
    tick_labels = [spec[0] for spec in band_specs]
    ax.set_yticks(tick_positions)
    ax.set_yticklabels(tick_labels)
    ax.set_ylim(7.0, 0.0)  # inverted: T1 at top
    for tick_label in ax.get_yticklabels():
        tick_label.set_color("#6b7280")
        tick_label.set_fontsize(8)
        if tick_label.get_text() == "Free":
            tick_label.set_color("#a08a5e")

    # X-axis: year ticks only, spanning actual data range.
    first_year = xs[0].year
    last_year = xs[-1].year
    year_ticks = [date(y, 1, 1) for y in range(first_year, last_year + 1)
                  if date(y, 1, 1) >= xs[0] and date(y, 1, 1) <= xs[-1]]
    if year_ticks:
        ax.set_xticks(year_ticks)
        ax.set_xticklabels([str(d.year) for d in year_ticks])
    for tick_label in ax.get_xticklabels():
        tick_label.set_color("#9ca3af")
        tick_label.set_fontsize(8)

    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False)

    fig.tight_layout(pad=0.4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return (
        f'<img src="data:image/png;base64,{encoded}" '
        f'alt="Tier history chart" '
        f'style="display:block;width:100%;height:auto;border-radius:4px;" />'
    )


def _load_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _render_email(template: str, replacements: Dict[str, str]) -> str:
    """Replace {{key}} tokens with values. Tokens missing from replacements are left as-is."""
    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _subject(account_name: str, previous_tier: Optional[str], current_tier: str) -> str:
    direction = _direction_label(previous_tier, current_tier)
    # Drops get their own prefix so a quick scan of the inbox surfaces
    # losses immediately. Upgrades and "new" entrants share [Tier Change].
    prefix = "[Tier Drop]" if direction == "down" else "[Tier Change]"
    return f"{prefix} {account_name}"


def _recipients_for_move(
    previous_tier: Optional[str], current_tier: Optional[str]
) -> Tuple[List[str], List[str]]:
    """Owners interested in this move and the per-tier CCs.

    Both previous and current owners get notified when they differ — they
    have different operational interest in the move. CC lists from each
    affected tier are unioned into a single Cc set, with deduplication and
    suppression of any address that's already in To:.

    Returns (to_list, cc_list).
    """
    to_list: List[str] = []
    cc_list: List[str] = []
    seen_to = set()
    seen_cc = set()
    for tier in (previous_tier, current_tier):
        if not tier:
            continue
        entry = TIER_OWNERS.get(tier)
        if not entry:
            continue
        if isinstance(entry, dict):
            primary = entry.get("to")
            ccs = entry.get("cc") or []
        else:
            # Backwards-compat for the old flat-string shape, just in case.
            primary = entry
            ccs = []
        if primary and primary not in seen_to:
            to_list.append(primary)
            seen_to.add(primary)
        for cc in ccs:
            if cc and cc not in seen_cc:
                cc_list.append(cc)
                seen_cc.add(cc)
    # Don't CC anyone who's already on the To: line
    cc_list = [c for c in cc_list if c not in seen_to]
    return to_list, cc_list


def compose_and_send(
    move_row: pd.Series,
    history: Dict,
    account_meta: Dict,
    zoho_url: Optional[str],
) -> bool:
    """Compose and send a single tier-movement email. Returns True on success."""
    account_id = int(move_row["AccountId"])
    account_name = str(move_row.get("Account_Name") or account_meta.get("account_name") or f"Account #{account_id}")
    previous_tier = move_row.get("previous_tier")
    current_tier = move_row.get("current_tier")

    if current_tier is None:
        # 'departed' rows shouldn't reach here — they're filtered out by
        # filter_email_relevant_moves — but defensive guard to avoid sending
        # a malformed email if the filter ever changes.
        log.warning("Skipping departed account %s: nothing to email about.", account_id)
        return False

    to_list, cc_list = _recipients_for_move(previous_tier, current_tier)
    if not to_list:
        log.info("No owner for movement %s -> %s on account %s; skipping.",
                 previous_tier, current_tier, account_id)
        return False

    # In test mode, redirect to the test recipient and drop CCs so a TEST_MODE=1
    # run can't spam real owners or copied team members. Subject already gets a
    # [TEST] prefix from send_html_email.
    if TEST_MODE:
        log.info("TEST_MODE: redirecting tier-movement email for account %s "
                 "(was to=%s cc=%s) to %s.",
                 account_id, to_list, cc_list, DEFAULT_RECIPIENT)
        to_list = [DEFAULT_RECIPIENT]
        cc_list = []

    history_points = extract_account_history(history, account_id)
    chart_svg = _build_chart_svg(history_points)

    last_sale_disp, last_sale_class = _format_date(account_meta.get("last_ticket_sale"))
    last_event_disp, last_event_class = _format_date(account_meta.get("last_event_created"))
    industry = account_meta.get("industry") or "—"
    sub_industry = account_meta.get("sub_industry") or "—"

    direction = _direction_label(previous_tier, current_tier)
    # Up arrow for upgrades (and new entrants), down arrow for drops. Plus
    # sign for "new" so first-time entrants don't read like they merely
    # held a tier — though current filters mean "new" never reaches here
    # in production, only in TEST_MODE previews of historical data.
    arrow = {"up": "↑", "down": "↓", "new": "+"}.get(direction, "•")
    replacements = {
        "headline": html.escape(_headline(account_name, previous_tier, current_tier)),
        "account_id": str(account_id),
        "account_name": html.escape(account_name),
        "previous_tier_label": html.escape(previous_tier or "(new)"),
        "current_tier_label": html.escape(current_tier),
        "direction": direction,
        "direction_label": arrow,
        "zoho_url": zoho_url or "#",
        "industry": html.escape(str(industry)),
        "sub_industry": html.escape(str(sub_industry)),
        "tickets_365d": _format_int(account_meta.get("tickets_365d")),
        "last_ticket_sale": html.escape(last_sale_disp),
        "last_event_created": html.escape(last_event_disp),
        "tier_history_svg": chart_svg,
    }

    template = _load_template()
    body = _render_email(template, replacements)
    subject = _subject(account_name, previous_tier, current_tier)

    # If Zoho URL lookup failed, swap the Zoho button for a disabled placeholder.
    if not zoho_url:
        body = body.replace(
            '<a class="btn" href="#">Open in Zoho</a>',
            '<span class="btn" style="opacity:0.5;cursor:not-allowed;">Open in Zoho (not linked)</span>',
        )

    try:
        send_html_email(to=to_list, cc=cc_list or None, subject=subject, html_content=body)
        log.info("Sent tier-movement email for account %s (to=%s cc=%s)",
                 account_id, ", ".join(to_list), ", ".join(cc_list) if cc_list else "—")
        return True
    except Exception as e:
        log.error("Failed to send tier-movement email for account %s: %s", account_id, e)
        return False


def send_movement_emails(
    moves_df: pd.DataFrame,
    history: Dict,
    account_meta_lookup: Dict[int, Dict],
    zoho_url_lookup: Dict[int, str],
) -> Tuple[int, int]:
    """Send one email per move. Returns (sent, failed) counts."""
    sent = 0
    failed = 0
    for _, row in moves_df.iterrows():
        aid = int(row["AccountId"])
        ok = compose_and_send(
            move_row=row,
            history=history,
            account_meta=account_meta_lookup.get(aid, {}),
            zoho_url=zoho_url_lookup.get(aid),
        )
        if ok:
            sent += 1
        else:
            failed += 1
    return sent, failed
