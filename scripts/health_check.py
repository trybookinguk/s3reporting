#!/usr/bin/env python3
"""Nightly health check — email a status summary of the reporting Pi.

Complements cron_notify.py: that emails when a job *fails*; this emails a
positive daily summary so silence means the Pi (or cron) is dead — a
dead-man's switch. It also catches problems that don't fail a job outright:
a stale warehouse, a filling disk, missing dependencies, or errors buried in
an otherwise exit-0 run.

Checks:
  - each scheduled job's latest run (exit code + errors in that run's slice,
    parsed from cron_wrapper.sh's "===== <job> started/finished =====" markers)
  - warehouse_duck.db freshness (the dashboard's source)
  - free disk on the cache/warehouse volume
  - Python deps + DuckDB CLI (via deploy/check_deps.py)

Usage:
  python3 scripts/health_check.py                 # email the summary (always)
  python3 scripts/health_check.py --only-on-failure   # email only if issues
  python3 scripts/health_check.py --stdout            # print, don't email (testing)

Recipient: HEALTHCHECK_RECIPIENT (comma-separated) or henry@ + kathryn@.
"""
import argparse
import html
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Make `modules...` importable regardless of cwd (Python puts scripts/ on the
# path when run as `python3 scripts/health_check.py`, not the repo root).
sys.path.insert(0, str(REPO))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/root/logs"))

# job-name (as passed to cron_wrapper) -> (log filename, max age hours or None).
# Age is only checked for daily jobs; Mon–Fri jobs are informational (no fail on
# weekends). None = report age but never fail on it.
JOBS = [
    ("prepare-data",      "prepare-data.log",      26),
    ("s3-sharepoint",     "s3-sharepoint.log",     None),
    ("zoho-industry",     "zoho-industry.log",     None),
    ("zoho-tiers",        "zoho-tiers.log",        None),
    ("users-accounts",    "users-accounts.log",    26),
    ("backup-sharepoint", "backup-sharepoint.log", 26),
]

_MARK = re.compile(r"=====\s+(\S+)\s+(started|finished)\s+at\s+(\S+)(?:\s+\(exit\s+(\d+)\))?")
_ERR = re.compile(r"error|exception|traceback|no module|denied|forbidden", re.I)
# benign lines that contain "error/failed" but are not problems
_BENIGN = re.compile(r"failed:\s*0|0 failed|no errors|error rate", re.I)

OK, WARN, FAIL = "ok", "warn", "fail"


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def check_job(job, logname, max_age_h):
    """Return (status, detail) for a job's most recent run."""
    log = LOG_DIR / logname
    if not log.exists():
        return WARN, "no log file yet"
    lines = log.read_text(errors="replace").splitlines()

    # find the last "started" marker for this job
    start_idx = None
    for i in range(len(lines) - 1, -1, -1):
        m = _MARK.search(lines[i])
        if m and m.group(2) == "started":
            start_idx = i
            break
    if start_idx is None:
        return WARN, "no run markers found"

    # find the matching "finished" after it (if any)
    exit_code, fin_ts = None, None
    for j in range(start_idx + 1, len(lines)):
        m = _MARK.search(lines[j])
        if m and m.group(2) == "finished":
            fin_ts = _parse_ts(m.group(3))
            exit_code = int(m.group(4)) if m.group(4) is not None else None
            body = lines[start_idx + 1:j]
            break
    else:
        return WARN, "started but no 'finished' marker (still running or killed?)"

    # errors within this run's slice
    errs = [ln for ln in body if _ERR.search(ln) and not _BENIGN.search(ln)]
    age_h = (( _now() - fin_ts).total_seconds() / 3600) if fin_ts else None
    age_str = f"{age_h:.1f}h ago" if age_h is not None else "unknown time"

    if exit_code not in (0, None):
        return FAIL, f"last run exit {exit_code} ({age_str})"
    if errs:
        return FAIL, f"exit 0 but {len(errs)} error line(s) in last run ({age_str}): {errs[0][:80]}"
    if max_age_h is not None and age_h is not None and age_h > max_age_h:
        return FAIL, f"last successful run was {age_str} (> {max_age_h}h — did cron run?)"
    return OK, f"clean, {age_str}"


def check_warehouse():
    data_dir = os.environ.get("DATA_DIR") or os.path.join(
        os.environ.get("S3_CACHE_DIR", "/root/s3reporting/.cache"), "prepared")
    f = Path(data_dir) / "warehouse_duck.db"
    if not f.exists():
        return FAIL, f"{f} missing — dashboard has no data"
    age_h = (_now().timestamp() - f.stat().st_mtime) / 3600
    size_mb = f.stat().st_size / (1024 * 1024)
    if age_h > 26:
        return FAIL, f"stale: last built {age_h:.1f}h ago ({size_mb:.0f} MB)"
    return OK, f"fresh: built {age_h:.1f}h ago ({size_mb:.0f} MB)"


def check_disk():
    path = os.environ.get("S3_CACHE_DIR", "/root/s3reporting/.cache")
    try:
        du = shutil.disk_usage(path if os.path.exists(path) else "/")
    except Exception as e:
        return WARN, f"could not stat disk: {e}"
    free_gb = du.free / (1024 ** 3)
    pct = du.used / du.total * 100
    detail = f"{free_gb:.1f} GB free ({pct:.0f}% used) on {path}"
    if free_gb < 1.5 or pct > 92:
        return FAIL, "LOW: " + detail
    if free_gb < 3 or pct > 85:
        return WARN, detail
    return OK, detail


def check_deps():
    script = REPO / "deploy" / "check_deps.py"
    if not script.exists():
        return WARN, "deploy/check_deps.py not present"
    try:
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return WARN, f"could not run check_deps.py: {e}"
    if r.returncode == 0:
        return OK, "all required deps + duckdb CLI present"
    # pull the concise missing/broken summary lines
    bad = [ln.strip() for ln in r.stdout.splitlines() if re.search(r"MISSING|BROKEN|Missing REQUIRED", ln)]
    return FAIL, "; ".join(bad[:4]) or "check_deps reported problems"


def build(results):
    worst = FAIL if any(s == FAIL for _, s, _ in results) else (
        WARN if any(s == WARN for _, s, _ in results) else OK)
    host = socket.gethostname()
    when = _now().strftime("%Y-%m-%d %H:%M UTC")
    colour = {OK: "#2e7d32", WARN: "#f9a825", FAIL: "#c62828"}
    icon = {OK: "✓", WARN: "•", FAIL: "✗"}
    rows = "".join(
        f'<tr><td style="padding:4px 10px;color:{colour[s]};font-weight:bold;">{icon[s]} {s.upper()}</td>'
        f'<td style="padding:4px 10px;"><b>{html.escape(n)}</b></td>'
        f'<td style="padding:4px 10px;color:#555;">{html.escape(d)}</td></tr>'
        for n, s, d in results
    )
    body = f"""
    <p style="font-size:15px;">Reporting Pi health: <b style="color:{colour[worst]};">{worst.upper()}</b>
       — {host}, {when}</p>
    <table style="border-collapse:collapse;font-size:13px;">{rows}</table>
    <p style="color:#888;font-size:12px;">Automated nightly check (scripts/health_check.py).
       Per-job failures are also emailed live by cron_notify.py.</p>
    """
    return worst, body


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only-on-failure", action="store_true", help="email only if status is WARN/FAIL")
    ap.add_argument("--stdout", action="store_true", help="print summary, don't email (testing)")
    args = ap.parse_args()

    results = []
    results.append(("dependencies", *check_deps()))
    results.append(("warehouse (duckdb)", *check_warehouse()))
    results.append(("disk space", *check_disk()))
    for job, logname, max_age in JOBS:
        results.append((job, *check_job(job, logname, max_age)))

    worst, body = build(results)

    if args.stdout:
        for n, s, d in results:
            print(f"  {s.upper():5} {n:22} {d}")
        print(f"\n  overall: {worst.upper()}")
        return 1 if worst == FAIL else 0

    if args.only_on_failure and worst == OK:
        return 0

    recipient = os.environ.get("HEALTHCHECK_RECIPIENT")
    to = [a.strip() for a in recipient.split(",")] if recipient else \
        ["henry@trybooking.co.uk", "kathryn@trybooking.co.uk"]
    host = socket.gethostname()
    tag = {OK: "OK", WARN: "check", FAIL: "ISSUES"}[worst]
    subject = f"[Pi health {tag}] {host} — {_now().strftime('%Y-%m-%d')}"
    try:
        from modules.utils.email_utils import send_html_email
        send_html_email(to, subject, body)
        print(f"health email sent ({worst}) to {to}")
    except Exception as e:
        print(f"ERROR sending health email: {e}", file=sys.stderr)
        return 2
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
