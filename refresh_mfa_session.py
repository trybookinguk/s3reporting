#!/usr/bin/env python3
"""
Refresh AWS temporary/MFA session credentials in .env.

Reads long-lived credentials from MFA_-prefixed environment variables, calls
STS GetSessionToken with a one-time MFA code, and writes (overwriting) the
resulting temporary credentials back into .env as AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN.

Why: the reporting jobs run under an IAM policy that denies S3 without an MFA
context (ForceMfaAndAllowUserManagedCredentials). Long-lived keys alone are
denied; temporary session credentials from `aws sts get-session-token` carry
the MFA context and are accepted, but expire (max 36h). This script lets an
operator mint a fresh set from a phone MFA code and drop them into .env in one
step, without pasting secrets by hand.

Required environment variables (typically the permanent IAM user's keys, kept
under MFA_ names so they are never used directly by the jobs):
    MFA_ACCESS_KEY_ID       - the long-lived AKIA... access key id
    MFA_SECRET_ACCESS_KEY   - its secret
    MFA_SERIAL_NUMBER       - the MFA device ARN, e.g.
                              arn:aws:iam::438255373632:mfa/henry
Optional:
    MFA_DURATION_SECONDS    - session lifetime (default 43200 = 12h, max 129600)

Usage (source .env first so the MFA_ vars are present):
    set -a && source /root/s3reporting/.env && set +a
    python3 refresh_mfa_session.py 123456
    python3 refresh_mfa_session.py 123456 --env-file /root/s3reporting/.env
"""
import argparse
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_ENV_FILE = "/root/s3reporting/.env"
# Keys this script writes into .env (overwriting any existing values).
AWS_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def update_env_file(path: Path, updates: dict) -> None:
    """Overwrite the given KEY=VALUE lines in an env file, preserving everything
    else (comments, order, other vars). Missing keys are appended. Values are
    single-quoted (session tokens contain '/', '+', '=') with embedded single
    quotes escaped, so the file stays safe to `source` in bash."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={_quote(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={_quote(val)}")
    path.write_text("\n".join(out) + "\n")


def _quote(value: str) -> str:
    """Single-quote a value for a bash-sourced env file, escaping any quotes."""
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint temporary AWS MFA session credentials and write them to .env."
    )
    parser.add_argument("mfa_code", help="The current 6-digit MFA code from your device.")
    parser.add_argument(
        "--env-file", default=DEFAULT_ENV_FILE,
        help=f"Path to the .env file to update (default: {DEFAULT_ENV_FILE}).",
    )
    args = parser.parse_args()

    code = args.mfa_code.strip()
    if not (code.isdigit() and len(code) == 6):
        print(f"✗ MFA code must be 6 digits (got '{code}').")
        return 1

    access_key = os.environ.get("MFA_ACCESS_KEY_ID")
    secret_key = os.environ.get("MFA_SECRET_ACCESS_KEY")
    serial = os.environ.get("MFA_SERIAL_NUMBER")
    duration = os.environ.get("MFA_DURATION_SECONDS", "43200")

    missing = [
        name for name, val in (
            ("MFA_ACCESS_KEY_ID", access_key),
            ("MFA_SECRET_ACCESS_KEY", secret_key),
            ("MFA_SERIAL_NUMBER", serial),
        ) if not val
    ]
    if missing:
        print("✗ Missing required environment variable(s): " + ", ".join(missing))
        print("  Source your .env first: set -a && source .env && set +a")
        return 1

    try:
        duration_int = int(duration)
    except ValueError:
        print(f"✗ MFA_DURATION_SECONDS must be an integer (got '{duration}').")
        return 1

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"✗ .env file not found: {env_path}")
        return 1

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("✗ boto3 is not installed (pip install boto3).")
        return 1

    print(f"Requesting session token for {access_key[:4]}...{access_key[-4:]} "
          f"via {serial} (duration {duration_int}s)...")
    sts = boto3.client(
        "sts",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    try:
        resp = sts.get_session_token(
            DurationSeconds=duration_int,
            SerialNumber=serial,
            TokenCode=code,
        )
    except ClientError as e:
        print(f"✗ STS GetSessionToken failed: {e}")
        return 1

    creds = resp["Credentials"]
    updates = {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
    }

    # Back up the .env once before rewriting, so a bad run is recoverable.
    backup = env_path.with_suffix(env_path.suffix + ".bak")
    shutil.copy2(env_path, backup)

    update_env_file(env_path, updates)

    expiry = creds["Expiration"]
    print("✓ Wrote temporary credentials to", env_path)
    print(f"  AWS_ACCESS_KEY_ID: {creds['AccessKeyId'][:4]}...{creds['AccessKeyId'][-4:]}")
    print(f"  Expires:           {expiry.isoformat()}")
    print(f"  Backup of previous .env: {backup}")
    print("  (secret key and session token written but not printed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
