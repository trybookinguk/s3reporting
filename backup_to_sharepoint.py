#!/usr/bin/env python3
"""Nightly backup of the irreplaceable Pi state to SharePoint.

The reporting stack lives on a single Pi/SSD. The code is on GitHub, but the
secrets and the data warehouses are not — if the SSD dies they are gone. This
script copies them to SharePoint each night so they can be restored with
`restore_from_sharepoint.py`.

It reuses the Microsoft Graph auth and chunked-upload machinery already proven
in `s3_to_sharepoint.py` — same Azure credentials, same trust boundary.

What it backs up (override paths via the env vars in CONFIG):
  - .env                         (s3reporting secrets)         — irreplaceable
  - ecosystem.config.cjs         (dashboard secrets)           — irreplaceable
  - retention_state.db, box_office.db, database_builder.db,
    zoho_cache.db                (dashboard-owned state)       — irreplaceable
  - warehouse_duck.db            (~370 MB, dashboard reads it)  — rebuildable, slow
  - warehouse.db                 (~3.7 GB, source of truth)     — rebuildable, very slow

Layout in SharePoint (folder set by BACKUP_FOLDER):
  Backups/pi/<YYYY-MM-DD>/<filename>

Retention: the BACKUP_KEEP most recent dated folders are kept; older ones are
deleted after a successful upload.

Usage (source the .env first so Azure + SharePoint vars are present):
  set -a && source /root/s3reporting/.env && set +a
  python3 backup_to_sharepoint.py                # full nightly backup
  python3 backup_to_sharepoint.py --no-warehouse # secrets + state only (small/fast)
  python3 backup_to_sharepoint.py --dry-run      # show what would happen, upload nothing
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import requests

import tempfile

from modules.utils.backup_crypto import (
    encrypt_stream,
    resolve_passphrase,
    BackupCryptoError,
)

# Reuse the auth, upload and retry helpers from the existing sync — importing is
# side-effect-free (its main() is guarded by __name__ == "__main__").
from s3_to_sharepoint import (
    GRAPH_BASE,
    SMALL_FILE_LIMIT,
    authenticate_graph,
    graph_headers,
    upload_small_file,
    upload_large_file,
    _fmt_size,
    _request_with_retry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pi-backup")

# --- What to back up. Override any path via env var if the Pi layout changes. ---
S3REPORTING_DIR = os.environ.get("S3REPORTING_DIR", "/root/s3reporting")
DASHBOARD_DIR = os.environ.get("DASHBOARD_DIR", "/root/reporting-dashboard")
PREPARED_DIR = os.environ.get(
    "PREPARED_DIR", f"{S3REPORTING_DIR}/.cache/prepared"
)

# Irreplaceable: secrets + dashboard-owned state. Always backed up.
CRITICAL_FILES = [
    f"{S3REPORTING_DIR}/.env",
    f"{DASHBOARD_DIR}/ecosystem.config.cjs",
    f"{PREPARED_DIR}/retention_state.db",
    f"{PREPARED_DIR}/box_office.db",
    f"{PREPARED_DIR}/database_builder.db",
    f"{PREPARED_DIR}/zoho_cache.db",
    # User-edited overrides migrated off SharePoint (ppc/mailshake manual matches,
    # exclusions, decisions, account_targets) + the mailshake pipeline output.
    f"{PREPARED_DIR}/app_state.db",
    # Tier history/snapshot state migrated off SharePoint (read-write by zoho_tiers).
    f"{PREPARED_DIR}/tier_state.db",
    # Mailgun validation cache — rebuildable but small, and re-validating costs
    # Mailgun quota, so keep a copy.
    f"{PREPARED_DIR}/mailgun_cache.db",
]

# Rebuildable-from-S3 but slow to rebuild. Skipped with --no-warehouse.
WAREHOUSE_FILES = [
    f"{PREPARED_DIR}/warehouse_duck.db",
    f"{PREPARED_DIR}/warehouse.db",
]

# Every backed-up file is encrypted before upload, per the "all client data
# encrypted at rest" commitment — secrets, state DBs, AND the warehouses (which
# hold the booking data itself). Uploaded as "<name>.enc"; restore decrypts by
# the .enc suffix + magic header. Encryption streams to a temp file so even the
# multi-GB warehouse never lands in memory (see upload_one).

# Where backups go in the SharePoint drive, and how many dated copies to keep.
BACKUP_FOLDER = os.environ.get("BACKUP_FOLDER", "Backups/pi")
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7"))

# The drive to upload into (reuses the SharePoint drive already configured for
# the S3 export sync).
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")

# Backup date — passed in so the script is deterministic and testable. Defaults
# to today in UTC if not supplied.
def _backup_date():
    override = os.environ.get("BACKUP_DATE")
    if override:
        return override
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def upload_one(token, drive_id, folder, src_path, passphrase):
    """Encrypt a file and upload it as "<name>.enc". Returns True/False/None.

    The file is stream-encrypted to a temp file on the same filesystem (bounded
    memory — the 3.5 GB warehouse never sits in RAM), then uploaded via the
    existing size-based path so the encrypted temp still goes chunked when large.
    The temp file is always cleaned up.
    """
    p = Path(src_path)
    if not p.exists():
        log.warning("Skipping (not found): %s", src_path)
        return None  # distinguish "missing" from "failed"

    if not passphrase:
        # Never silently upload anything in the clear.
        log.error("No passphrase available to encrypt %s; refusing to upload.", p.name)
        return False

    key = p.name + ".enc"
    log.info("Backing up %s (%s, encrypting -> %s)...", p.name, _fmt_size(p.stat().st_size), key)

    # Temp file beside the source so it's on the same disk (atomic-ish, no /tmp
    # tmpfs surprises) and gets cleaned up even on failure.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".bkpenc-", dir=str(p.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as dst, open(p, "rb") as src:
            encrypt_stream(src, dst, passphrase)
        enc_size = os.path.getsize(tmp_path)
        if enc_size <= SMALL_FILE_LIMIT:
            with open(tmp_path, "rb") as f:
                return upload_small_file(token, drive_id, folder, key, f.read())
        return upload_large_file(token, drive_id, folder, key, tmp_path, enc_size)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def delete_folder(token, drive_id, folder):
    """Delete a backup subfolder (and its contents) if it exists. Used to fully
    replace a same-day re-run rather than letting old files accumulate alongside
    new ones — which previously left stale PLAINTEXT files next to the new .enc
    ones in a re-used dated folder."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{folder}"
    resp = _request_with_retry(requests.delete, url, headers=graph_headers(token))
    if resp.status_code in (204, 200):
        log.info("Cleared existing folder %s before re-upload.", folder)
    elif resp.status_code == 404:
        pass  # nothing there — fine
    else:
        log.warning("Could not clear existing folder %s: %d", folder, resp.status_code)


def list_backup_dates(token, drive_id):
    """Return the existing dated subfolders under BACKUP_FOLDER, newest first."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{BACKUP_FOLDER}:/children"
    resp = _request_with_retry(requests.get, url, headers=graph_headers(token))
    if resp.status_code == 404:
        return []  # folder doesn't exist yet — first run
    if resp.status_code != 200:
        log.warning("Could not list existing backups: %d", resp.status_code)
        return []
    folders = [
        item["name"]
        for item in resp.json().get("value", [])
        if "folder" in item
    ]
    return sorted(folders, reverse=True)


def prune_old_backups(token, drive_id, keep):
    """Delete dated backup folders beyond the `keep` most recent."""
    dates = list_backup_dates(token, drive_id)
    stale = dates[keep:]
    for name in stale:
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{BACKUP_FOLDER}/{name}"
        resp = _request_with_retry(requests.delete, url, headers=graph_headers(token))
        if resp.status_code in (200, 204):
            log.info("Pruned old backup: %s", name)
        else:
            log.warning("Failed to prune %s: %d", name, resp.status_code)


def run(no_warehouse=False, dry_run=False):
    if not SHAREPOINT_DRIVE_ID:
        log.error("SHAREPOINT_DRIVE_ID must be set (source the .env first).")
        sys.exit(1)

    targets = list(CRITICAL_FILES)
    if not no_warehouse:
        targets += WAREHOUSE_FILES

    date = _backup_date()
    folder = f"{BACKUP_FOLDER}/{date}"
    log.info("Backup destination: %s (keep last %d)", folder, BACKUP_KEEP)

    if dry_run:
        for src in targets:
            p = Path(src)
            state = _fmt_size(p.stat().st_size) if p.exists() else "MISSING"
            log.info("[dry-run] would encrypt + upload %s (%s) -> %s/%s.enc",
                     src, state, folder, p.name)
        log.info("[dry-run] would then keep the %d most recent dated folders.", BACKUP_KEEP)
        return 0

    # Resolve the encryption passphrase up front so a misconfiguration fails the
    # whole run before anything is uploaded, rather than half-uploading and then
    # erroring on the first sensitive file.
    try:
        passphrase = resolve_passphrase(prompt_if_missing=False)
    except BackupCryptoError as e:
        log.error("%s. Set %s in .env so the secret/state files can be encrypted.",
                  e, "BACKUP_SECRET_PASSPHRASE")
        return 1

    token = authenticate_graph()

    # Replace any existing same-day folder so a re-run can't leave stale files
    # (notably plaintext from an older code version) alongside the new uploads.
    delete_folder(token, SHAREPOINT_DRIVE_ID, folder)

    failures, uploaded, missing = [], 0, []
    for src in targets:
        result = upload_one(token, SHAREPOINT_DRIVE_ID, folder, src, passphrase=passphrase)
        if result is True:
            uploaded += 1
        elif result is None:
            missing.append(src)
        else:
            failures.append(src)

    # Only prune once the new backup is safely up — never delete old copies if
    # this run failed to produce a good new one.
    critical_failed = [f for f in failures if f in CRITICAL_FILES]
    if not critical_failed:
        prune_old_backups(token, SHAREPOINT_DRIVE_ID, BACKUP_KEEP)
    else:
        log.error("Critical file(s) failed to upload; skipping prune to keep older backups.")

    log.info("Backup complete: %d uploaded, %d missing, %d failed.",
             uploaded, len(missing), len(failures))
    if missing:
        log.warning("Missing files (not on this Pi?): %s", ", ".join(missing))

    # Non-zero exit on any failure so the cron wrapper emails the operator.
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Back up Pi secrets + warehouses to SharePoint.")
    parser.add_argument("--no-warehouse", action="store_true",
                        help="Back up secrets + dashboard state only; skip the large warehouses.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be backed up without uploading.")
    args = parser.parse_args()
    sys.exit(run(no_warehouse=args.no_warehouse, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
