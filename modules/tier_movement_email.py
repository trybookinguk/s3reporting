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
from .utils.config import TIER_OWNERS
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
    """Build the inline tier-history SVG.

    history_points is a list of (day_iso, tier_name, composite_score) ordered
    oldest-first (as returned by tier_history.extract_account_history).

    Strategy:
      - Skip points where tier_name is None (account didn't exist on that day).
        These render as gaps in the line.
      - Compress consecutive identical tiers into "held segments" so the path
        is small — (start_x, end_x, y) tuples.
      - Draw rounded-step transitions between segments via cubic Beziers.
    """
    visible = [(d, t) for d, t, _s in history_points if t is not None]
    if not visible:
        return '<p class="history-empty">No tier history recorded yet.</p>'

    # Map dates to x positions across [_CHART_X_START, _CHART_X_END].
    first_d = date.fromisoformat(visible[0][0])
    last_d = date.fromisoformat(visible[-1][0])
    span_days = max(1, (last_d - first_d).days)
    width = _CHART_X_END - _CHART_X_START

    def x_for(day_iso: str) -> float:
        d = date.fromisoformat(day_iso)
        frac = (d - first_d).days / span_days
        return _CHART_X_START + frac * width

    # Compress to held segments: list of (x_start, x_end, y).
    segments: List[Tuple[float, float, int]] = []
    cur_tier = visible[0][1]
    cur_x_start = x_for(visible[0][0])
    last_x = cur_x_start
    for day_iso, tier in visible[1:]:
        x = x_for(day_iso)
        if tier != cur_tier:
            segments.append((cur_x_start, last_x, _BAND_Y[cur_tier]))
            cur_tier = tier
            cur_x_start = last_x  # transition pinned to the previous sample
        last_x = x
    segments.append((cur_x_start, last_x, _BAND_Y[cur_tier]))

    # Build the path. Each segment gets an L; transitions between segments
    # use a cubic Bezier with a small radius to round the corner.
    parts = [f"M {segments[0][0]:.1f},{segments[0][2]}"]
    for i, (sx, ex, y) in enumerate(segments):
        if i == 0:
            # Hold the first segment, leaving room before the next corner.
            next_y = segments[i + 1][2] if i + 1 < len(segments) else y
            if i + 1 < len(segments):
                hold_end = ex - _CORNER_RADIUS
                parts.append(f"L {hold_end:.1f},{y}")
                # Curve into the next segment's y at ex + r.
                parts.append(
                    f"C {ex:.1f},{y} {ex:.1f},{next_y} {ex + _CORNER_RADIUS:.1f},{next_y}"
                )
            else:
                parts.append(f"L {ex:.1f},{y}")
        else:
            next_y = segments[i + 1][2] if i + 1 < len(segments) else y
            if i + 1 < len(segments):
                hold_end = ex - _CORNER_RADIUS
                parts.append(f"L {hold_end:.1f},{y}")
                parts.append(
                    f"C {ex:.1f},{y} {ex:.1f},{next_y} {ex + _CORNER_RADIUS:.1f},{next_y}"
                )
            else:
                parts.append(f"L {ex:.1f},{y}")
    path_d = " ".join(parts)

    # Year ticks across the x-axis.
    year_first = first_d.year
    year_last = last_d.year
    year_ticks = []
    for yr in range(year_first, year_last + 1):
        anchor = max(first_d, date(yr, 1, 1))
        if anchor > last_d:
            break
        x = x_for(anchor.isoformat())
        align = "start" if yr == year_first else "end" if yr == year_last else "middle"
        year_ticks.append(f'<text x="{x:.1f}" y="110" text-anchor="{align}">{yr}</text>')
    year_ticks_svg = "\n          ".join(year_ticks)

    last_x_val, last_y = last_x, segments[-1][2]

    return f"""<svg viewBox="0 0 560 {_CHART_VIEW_HEIGHT}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none" role="img" aria-label="Tier history chart">
        <rect x="{_CHART_X_START}" y="73" width="{_CHART_X_END - _CHART_X_START}" height="14" fill="#f5efe6"/>
        <g>
          <line x1="{_CHART_X_START}" y1="10" x2="{_CHART_X_END}" y2="10" stroke="#e5e7eb" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="24" x2="{_CHART_X_END}" y2="24" stroke="#f3f4f6" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="38" x2="{_CHART_X_END}" y2="38" stroke="#f3f4f6" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="52" x2="{_CHART_X_END}" y2="52" stroke="#f3f4f6" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="66" x2="{_CHART_X_END}" y2="66" stroke="#f3f4f6" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="80" x2="{_CHART_X_END}" y2="80" stroke="#f3f4f6" stroke-width="1"/>
          <line x1="{_CHART_X_START}" y1="94" x2="{_CHART_X_END}" y2="94" stroke="#e5e7eb" stroke-width="1"/>
        </g>
        <g font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="9" fill="#9ca3af">
          <text x="34" y="13" text-anchor="end">T1</text>
          <text x="34" y="27" text-anchor="end">T2</text>
          <text x="34" y="41" text-anchor="end">T3</text>
          <text x="34" y="55" text-anchor="end">T4</text>
          <text x="34" y="69" text-anchor="end">T5</text>
          <text x="34" y="83" text-anchor="end" fill="#a08a5e">Free</text>
          <text x="34" y="97" text-anchor="end">Nil</text>
        </g>
        <path fill="none" stroke="#0589A3" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" d="{path_d}"/>
        <circle cx="{last_x_val:.1f}" cy="{last_y}" r="4" fill="#0589A3"/>
        <g font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="9" fill="#9ca3af">
          {year_ticks_svg}
        </g>
      </svg>"""


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
        return f"[Tier Movement] {account_name}: (new) → {current_tier}"
    return f"[Tier Movement] {account_name}: {previous_tier} → {current_tier} ({direction})"


def _recipients_for_move(previous_tier: Optional[str], current_tier: Optional[str]) -> List[str]:
    """Owners interested in this move. Both previous and current owners get notified
    when they differ."""
    addresses: List[str] = []
    seen = set()
    for tier in (previous_tier, current_tier):
        owner = TIER_OWNERS.get(tier) if tier else None
        if owner and owner not in seen:
            addresses.append(owner)
            seen.add(owner)
    return addresses


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

    recipients = _recipients_for_move(previous_tier, current_tier)
    if not recipients:
        log.info("No owner for movement %s -> %s on account %s; skipping.",
                 previous_tier, current_tier, account_id)
        return False

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
        send_html_email(to=recipients, subject=subject, html_content=body)
        log.info("Sent tier-movement email for account %s to %s", account_id, ", ".join(recipients))
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
