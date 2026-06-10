#!/usr/bin/env python3
"""Email a failure report when a cron job exits non-zero.

Invoked by cron_wrapper.sh. Uses the existing send_html_email helper so it
inherits the Azure Graph credentials already in place for zoho_tiers.py.
"""
import argparse
import html
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root is one level up from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.utils.email_utils import send_html_email

FAILURE_RECIPIENT = ["henry@trybooking.co.uk"]
LOG_TAIL_LINES = 80


def tail_log(log_file: Path, lines: int) -> str:
    if not log_file.exists():
        return f"(log file {log_file} does not exist)"
    try:
        with log_file.open("r", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"(error reading log: {e})"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="Job name for subject/body")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log-file", required=True, help="Path to the log file to tail")
    args = parser.parse_args()

    log_path = Path(args.log_file)
    log_tail = tail_log(log_path, LOG_TAIL_LINES)
    host = socket.gethostname()
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    subject = f"[cron-fail] {args.job} on {host} (exit {args.exit_code})"

    html_body = f"""
    <p>A scheduled job exited with a non-zero status.</p>
    <table style="border-collapse: collapse;">
      <tr><td><b>Job</b></td><td>{html.escape(args.job)}</td></tr>
      <tr><td><b>Host</b></td><td>{html.escape(host)}</td></tr>
      <tr><td><b>Exit code</b></td><td>{args.exit_code}</td></tr>
      <tr><td><b>Failed at</b></td><td>{when}</td></tr>
      <tr><td><b>Log file</b></td><td><code>{html.escape(str(log_path))}</code></td></tr>
    </table>
    <p>Last {LOG_TAIL_LINES} lines of the log:</p>
    <pre style="background:#f4f4f4; padding:10px; border:1px solid #ddd; font-size:12px; white-space:pre-wrap;">
{html.escape(log_tail)}
    </pre>
    """

    send_html_email(
        to=FAILURE_RECIPIENT,
        subject=subject,
        html_content=html_body,
    )
    print(f"Failure notification sent to {', '.join(FAILURE_RECIPIENT)}")


if __name__ == "__main__":
    main()
