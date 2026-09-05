#!/usr/bin/env python3
"""Test every credential in .env with a lightweight, read-only call per service.

After rebuilding .env (or rotating a secret), run this to confirm each key
actually authenticates — before waiting for the nightly cron to fail. Every
check is NON-DESTRUCTIVE: it authenticates and/or reads, never writes, never
sends email, never tags a user.

Usage:
    python3 test_secrets.py
    python3 test_secrets.py --env-file /path/to/.env
    python3 test_secrets.py --only aws,zoho,graph        # subset

Exit code: 0 if every REQUIRED check passed, 1 otherwise (SKIP/optional don't
fail the run). Secret values are never printed.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

S3_BUCKET = "produk-rdsextracts-438255373632"
HTTP_TIMEOUT = 20

# ANSI (only if stdout is a tty)
_tty = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _tty else s
GREEN = lambda s: _c("32", s)
RED = lambda s: _c("31", s)
YELLOW = lambda s: _c("33", s)
DIM = lambda s: _c("2", s)

# status constants
PASS, FAIL, MISSING, SKIP = "PASS", "FAIL", "MISSING", "SKIP"


def load_env_file(path: Path):
    """Load .env by sourcing it in bash (matches cron_wrapper.sh:
    `set -a && source .env && set +a`) so quoting/exports/refs are honoured."""
    if not path.exists():
        print(RED(f"✗ .env file not found: {path}"))
        sys.exit(2)
    marker = "__ENV_AFTER_SOURCE__"
    script = f"set -a && source {path} && set +a && echo {marker} && env -0"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=False)
    if result.returncode != 0:
        print(RED(f"✗ Failed to source {path}: {result.stderr.decode(errors='replace')}"))
        sys.exit(2)
    output = result.stdout.decode(errors="replace")
    pos = output.find(marker)
    if pos == -1:
        print(RED(f"✗ Could not parse environment after sourcing {path}"))
        sys.exit(2)
    for entry in output[pos + len(marker):].lstrip("\n").split("\0"):
        if "=" in entry:
            k, _, v = entry.partition("=")
            os.environ[k] = v
    print(DIM(f"Loaded environment from {path}"))


def _have(*names):
    """Return the first non-empty env var among names, else ''."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


# --- Per-service checks: each returns (status, detail) -----------------------

def check_aws():
    key = _have("AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY")
    sec = _have("AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY")
    if not key or not sec:
        return MISSING, "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set"
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return SKIP, "boto3 not installed (pip install boto3)"
    region = _have("AWS_REGION", "AWS_DEFAULT_REGION") or "eu-west-2"
    token = _have("AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN") or None
    try:
        s3 = boto3.client(
            "s3", region_name=region,
            aws_access_key_id=key, aws_secret_access_key=sec,
            aws_session_token=token,
            config=Config(connect_timeout=HTTP_TIMEOUT, read_timeout=HTTP_TIMEOUT, retries={"max_attempts": 1}),
        )
        s3.list_objects_v2(Bucket=S3_BUCKET, MaxKeys=1)
        return PASS, f"authenticated; can list s3://{S3_BUCKET} ({region})"
    except (ClientError, NoCredentialsError) as e:
        return FAIL, f"{type(e).__name__}: {getattr(e, 'response', {}).get('Error', {}).get('Code', str(e))}"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


def check_zoho():
    cid = _have("ZOHO_CLIENT_ID"); sec = _have("ZOHO_CLIENT_SECRET"); rt = _have("ZOHO_REFRESH_TOKEN")
    if not (cid and sec and rt):
        return MISSING, "ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN not all set"
    try:
        import requests
    except ImportError:
        return SKIP, "requests not installed"
    try:
        r = requests.post(
            "https://accounts.zoho.com/oauth/v2/token",
            data={"refresh_token": rt, "client_id": cid, "client_secret": sec,
                  "grant_type": "refresh_token"},
            timeout=HTTP_TIMEOUT,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.ok and body.get("access_token"):
            return PASS, "refresh token exchanged for an access token"
        return FAIL, f"HTTP {r.status_code}: {body.get('error', r.text[:120])}"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


def _graph_token():
    """Return (token, err). Client-credentials flow, same as modules/utils/sharepoint.py."""
    tenant = _have("AZURE_TENANT_ID"); cid = _have("AZURE_CLIENT_ID"); sec = _have("AZURE_CLIENT_SECRET")
    if not (tenant and cid and sec):
        return None, "AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET not all set"
    try:
        import msal
    except ImportError:
        return None, "msal not installed (pip install msal)"
    try:
        app = msal.ConfidentialClientApplication(
            cid, authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential=sec,
        )
        res = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" in res:
            return res["access_token"], None
        return None, f"{res.get('error')}: {res.get('error_description', '')[:120]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# Cache the graph token across graph/sharepoint/mailbox checks.
_GRAPH_CACHE = {}
def _get_graph_token_cached():
    if "done" not in _GRAPH_CACHE:
        _GRAPH_CACHE["token"], _GRAPH_CACHE["err"] = _graph_token()
        _GRAPH_CACHE["done"] = True
    return _GRAPH_CACHE["token"], _GRAPH_CACHE["err"]


def check_graph():
    if not _have("AZURE_TENANT_ID", ) and not _have("AZURE_CLIENT_ID"):
        return MISSING, "Azure credentials not set"
    token, err = _get_graph_token_cached()
    if token:
        return PASS, "Azure app authenticated to Microsoft Graph"
    if err and ("not all set" in err or "not installed" in err):
        return (MISSING if "not all set" in err else SKIP), err
    return FAIL, err


def check_sharepoint():
    drive = _have("SHAREPOINT_DRIVE_ID")
    if not drive:
        return MISSING, "SHAREPOINT_DRIVE_ID not set"
    token, err = _get_graph_token_cached()
    if not token:
        return (SKIP if err and "not installed" in err else FAIL), f"no Graph token ({err})"
    try:
        import requests
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive}/root?$select=name,webUrl",
            headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT,
        )
        if r.ok:
            # bonus: check the Backups/pi folder exists
            b = requests.get(
                f"https://graph.microsoft.com/v1.0/drives/{drive}/root:/Backups/pi",
                headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT,
            )
            note = "Backups/pi present" if b.ok else "drive OK but Backups/pi not found"
            return PASS, f"drive reachable; {note}"
        return FAIL, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


def check_mailbox():
    mbx = _have("AZURE_SENDER_MAILBOX")
    if not mbx:
        return MISSING, "AZURE_SENDER_MAILBOX not set"
    token, err = _get_graph_token_cached()
    if not token:
        return (SKIP if err and "not installed" in err else FAIL), f"no Graph token ({err})"
    try:
        import requests
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{mbx}?$select=mail,userPrincipalName",
            headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT,
        )
        if r.ok:
            return PASS, f"sender mailbox resolves ({mbx})"
        return FAIL, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


def check_ga4():
    prop = _have("GA4_PROPERTY_ID")
    keyjson = _have("GA4_SERVICE_ACCOUNT_KEY")
    keyfile = _have("GOOGLE_APPLICATION_CREDENTIALS")
    if not prop or not (keyjson or keyfile):
        return MISSING, "GA4_PROPERTY_ID and GA4_SERVICE_ACCOUNT_KEY/GOOGLE_APPLICATION_CREDENTIALS not set"
    try:
        import json
        from google.oauth2 import service_account
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric
    except ImportError:
        return SKIP, "google-analytics-data not installed"
    try:
        if keyjson:
            info = json.loads(keyjson) if keyjson.lstrip().startswith("{") else json.load(open(keyjson))
            creds = service_account.Credentials.from_service_account_info(info)
        else:
            creds = service_account.Credentials.from_service_account_file(keyfile)
        client = BetaAnalyticsDataClient(credentials=creds)
        client.run_report(RunReportRequest(
            property=f"properties/{prop.lstrip('properties/')}",
            date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
            metrics=[Metric(name="sessions")],
        ))
        return PASS, f"GA4 property {prop} queried"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {str(e)[:140]}"


def check_vero():
    tok = _have("VERO_AUTH_TOKEN", "VERO_API_KEY")
    if not tok:
        return MISSING, "VERO_AUTH_TOKEN / VERO_API_KEY not set"
    # Vero has no clean read endpoint and its write endpoints tag real users, so
    # we do a deliberately harmless call: the events API with a bad payload.
    # A 401 means the token is rejected; a 400/422 means auth was accepted but
    # the payload was refused (i.e. the credential works). No event is created.
    try:
        import requests
        r = requests.post(
            "https://api.getvero.com/api/v2/events/track.json",
            json={"auth_token": tok},  # missing required fields on purpose
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code in (400, 422):
            return PASS, "auth token accepted (payload rejected as expected, no event created)"
        if r.status_code in (401, 403):
            return FAIL, f"HTTP {r.status_code}: token rejected"
        if r.status_code == 200:
            return PASS, "HTTP 200 (token accepted)"
        return SKIP, f"HTTP {r.status_code}: could not classify — verify manually"
    except Exception as e:
        return SKIP, f"{type(e).__name__}: {e} (network?)"


def check_mailshake():
    key = _have("MAILSHAKE_API_KEY")
    if not key:
        return MISSING, "MAILSHAKE_API_KEY not set"
    try:
        import requests
        r = requests.get(
            "https://api.mailshake.com/2017-04-01/me",
            headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT,
        )
        if r.ok:
            return PASS, "API key accepted (/me)"
        if r.status_code in (401, 403):
            return FAIL, f"HTTP {r.status_code}: key rejected"
        return SKIP, f"HTTP {r.status_code}: verify manually"
    except Exception as e:
        return SKIP, f"{type(e).__name__}: {e} (network?)"


def check_backup_passphrase():
    pw = _have("BACKUP_SECRET_PASSPHRASE")
    if not pw:
        return MISSING, "BACKUP_SECRET_PASSPHRASE not set"
    # If an encrypted backup file is on disk, prove the passphrase decrypts it.
    candidates = [Path("env.enc"), Path(".env.enc"), Path("/root/s3reporting/env.enc")]
    enc = next((p for p in candidates if p.is_file()), None)
    if not enc:
        return SKIP, "set, but no env.enc found to verify it against"
    try:
        import importlib.util
        bc_path = Path(__file__).resolve().parent / "modules" / "utils" / "backup_crypto.py"
        spec = importlib.util.spec_from_file_location("backup_crypto", bc_path)
        bc = importlib.util.module_from_spec(spec); spec.loader.exec_module(bc)
    except Exception as e:
        return SKIP, f"cannot load backup_crypto ({e})"
    try:
        with enc.open("rb") as f:
            head = f.read(len(bc.MAGIC))
            if not bc.is_encrypted_header(head):
                return FAIL, f"{enc} is not an S3RBK backup"
            f.seek(len(bc.MAGIC))
            # Decrypt just the first frame to validate the key without loading it all.
            import struct
            salt = f.read(16)
            ln = f.read(4)
            if len(ln) == 4:
                (n,) = struct.unpack(">I", ln)
                token = f.read(n)
                key = bc._derive_key(pw, salt)
                from cryptography.fernet import Fernet
                Fernet(key).decrypt(token)   # raises on wrong passphrase
        return PASS, f"passphrase correctly decrypts {enc.name}"
    except Exception as e:
        return FAIL, f"wrong passphrase or corrupt file ({type(e).__name__})"


CHECKS = [
    ("aws",       "AWS S3",            check_aws),
    ("zoho",      "Zoho CRM",          check_zoho),
    ("graph",     "Azure / Graph",     check_graph),
    ("sharepoint","SharePoint drive",  check_sharepoint),
    ("mailbox",   "M365 sender mbox",  check_mailbox),
    ("ga4",       "Google Analytics 4",check_ga4),
    ("vero",      "GetVero",           check_vero),
    ("mailshake", "Mailshake",         check_mailshake),
    ("backup",    "Backup passphrase", check_backup_passphrase),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default="/root/s3reporting/.env", help="path to .env (default: /root/s3reporting/.env)")
    ap.add_argument("--only", help="comma-separated subset, e.g. aws,zoho,graph")
    args = ap.parse_args()

    load_env_file(Path(args.env_file))

    wanted = {s.strip() for s in args.only.split(",")} if args.only else None
    print()
    results = []
    for slug, label, fn in CHECKS:
        if wanted and slug not in wanted:
            continue
        try:
            status, detail = fn()
        except Exception as e:  # a check itself blew up — never abort the run
            status, detail = FAIL, f"check crashed: {type(e).__name__}: {e}"
        results.append((label, status, detail))
        mark = {PASS: GREEN("✓ PASS"), FAIL: RED("✗ FAIL"),
                MISSING: YELLOW("• MISSING"), SKIP: DIM("– SKIP")}[status]
        print(f"  {mark:<16} {label:<20} {DIM(detail)}")

    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_missing = sum(1 for _, s, _ in results if s == MISSING)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    print()
    print(f"  {n_pass} passed, {n_fail} failed, {n_missing} missing, {n_skip} skipped")
    if n_fail:
        print(RED("  Some credentials are present but INVALID — fix these."))
    elif n_missing:
        print(YELLOW("  No failures, but some credentials are blank — fill them in if that service is used."))
    else:
        print(GREEN("  All configured credentials authenticated."))
    # Fail the run only on an actual FAIL (a wrong/rejected secret).
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
