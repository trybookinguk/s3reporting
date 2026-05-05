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

    Returns an <img> tag with a data: URI. The function is named *_svg for
    historical reasons — the original implementation rendered inline SVG, but
    classic Outlook on Windows strips SVG and the chart came through as a
    flat row of text. PNG renders identically across every email client at
    the cost of a matplotlib dependency.

    history_points is a list of (day_iso, tier_name, composite_score) ordered
    oldest-first (as returned by tier_history.extract_account_history). Days
    where tier_name is None are skipped (account didn't exist that day).
    """
    visible = [(d, t) for d, t, _s in history_points if t is not None]
    if not visible:
        return '<p class="history-empty">No tier history recorded yet.</p>'

    # Lazy-import matplotlib so the dependency is only required when an
    # email actually fires. Important: use the Agg backend before importing
    # pyplot so it works in headless CI.
    import io
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Tier -> y-axis position. Lower y = better tier (T1 at top).
    tier_y = {
        "Tier 1": 6, "Tier 2": 5, "Tier 3": 4,
        "Tier 4": 3, "Tier 5": 2, "Free": 1, "Nil": 0,
    }

    xs = [date.fromisoformat(d) for d, _ in visible]
    ys = [tier_y[t] for _, t in visible]

    # Build a step-style line by interleaving each transition with a hold
    # at the previous y-value.
    step_xs = [xs[0]]
    step_ys = [ys[0]]
    for i in range(1, len(xs)):
        if ys[i] != ys[i - 1]:
            step_xs.append(xs[i])
            step_ys.append(ys[i - 1])
        step_xs.append(xs[i])
        step_ys.append(ys[i])

    fig, ax = plt.subplots(figsize=(7.0, 1.8), dpi=140)
    fig.patch.set_facecolor("#fafbfc")
    ax.set_facecolor("#fafbfc")

    # Free band tint — y in [0.5, 1.5] covers the Free row.
    ax.axhspan(0.5, 1.5, facecolor="#f5efe6", zorder=0)

    # Gridlines at each band.
    for y in range(7):
        ax.axhline(y, color="#e5e7eb" if y in (0, 6) else "#f3f4f6",
                   linewidth=0.8, zorder=1)

    # The chart line — TryBooking accent colour, rounded joins.
    ax.plot(step_xs, step_ys, color="#0589A3", linewidth=2.0,
            solid_joinstyle="round", solid_capstyle="round", zorder=3)
    # Highlight the latest point.
    ax.plot([xs[-1]], [ys[-1]], marker="o", markersize=6,
            color="#0589A3", zorder=4)

    # Y-axis: tier labels at the right band positions.
    ax.set_yticks(list(tier_y.values()))
    ax.set_yticklabels(list(tier_y.keys()))
    ax.set_ylim(-0.5, 6.5)
    for label in ax.get_yticklabels():
        label.set_color("#6b7280")
        label.set_fontsize(8)
        if label.get_text() == "Free":
            label.set_color("#a08a5e")

    # X-axis: year ticks only, and only the years actually spanned.
    first_year = xs[0].year
    last_year = xs[-1].year
    year_ticks = [date(y, 1, 1) for y in range(first_year, last_year + 1)
                  if date(y, 1, 1) >= xs[0] and date(y, 1, 1) <= xs[-1]]
    if year_ticks:
        ax.set_xticks(year_ticks)
        ax.set_xticklabels([str(d.year) for d in year_ticks])
    for label in ax.get_xticklabels():
        label.set_color("#9ca3af")
        label.set_fontsize(8)

    # Strip frame and axis ticks for a cleaner look.
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
    if direction == "new":
        return f"[Tier Change] {account_name}: (new) → {current_tier}"
    return f"[Tier Change] {account_name}: {previous_tier} → {current_tier} ({direction})"


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

    replacements = {
        "headline": html.escape(_headline(account_name, previous_tier, current_tier)),
        "account_id": str(account_id),
        "account_name": html.escape(account_name),
        "previous_tier_label": html.escape(previous_tier or "(new)"),
        "current_tier_label": html.escape(current_tier),
        "direction": _direction_label(previous_tier, current_tier),
        "direction_label": _direction_label(previous_tier, current_tier),
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
