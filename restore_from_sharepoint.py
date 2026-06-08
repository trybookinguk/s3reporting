#!/usr/bin/env python3
"""Restore the Pi state from a SharePoint backup made by backup_to_sharepoint.py.

This is the disaster-recovery counterpart: stand up a fresh Pi, clone the two
repos, then run this to pull the secrets and warehouses back into place.

The chicken-and-egg problem: the backup lives in SharePoint, but reaching
SharePoint needs Azure credentials — which live in `.env`, which is one of the
things you're restoring. So this script prompts for the three Azure values at
the terminal to bootstrap, authenticates, then restores everything (including
the real `.env`, which contains those same values going forward).

Usage:
  python3 restore_from_sharepoint.py            # interactive: prompts, lists backups, restores
  python3 restore_from_sharepoint.py --list     # just list available backups and exit
  python3 restore_from_sharepoint.py --date 2026-06-08   # restore a specific dated backup

You will be prompted for:
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, SHAREPOINT_DRIVE_ID
(get these from your password manager / the outgoing operator).
"""

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

import msal
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pi-restore")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
BACKUP_FOLDER = os.environ.get("BACKUP_FOLDER", "Backups/pi")

# Where each backed-up filename is restored to. Mirrors backup_to_sharepoint.py.
S3REPORTING_DIR = os.environ.get("S3REPORTING_DIR", "/root/s3reporting")
DASHBOARD_DIR = os.environ.get("DASHBOARD_DIR", "/root/reporting-dashboard")
PREPARED_DIR = os.environ.get("PREPARED_DIR", f"{S3REPORTING_DIR}/.cache/prepared")

RESTORE_MAP = {
    ".env": f"{S3REPORTING_DIR}/.env",
    "ecosystem.config.cjs": f"{DASHBOARD_DIR}/ecosystem.config.cjs",
    "retention_state.db": f"{PREPARED_DIR}/retention_state.db",
    "box_office.db": f"{PREPARED_DIR}/box_office.db",
    "database_builder.db": f"{PREPARED_DIR}/database_builder.db",
    "zoho_cache.db": f"{PREPARED_DIR}/zoho_cache.db",
    "warehouse_duck.db": f"{PREPARED_DIR}/warehouse_duck.db",
    "warehouse.db": f"{PREPARED_DIR}/warehouse.db",
}


def prompt_credentials():
    """Collect the bootstrap Azure creds interactively."""
    log.info("Enter the Azure credentials to reach SharePoint (from your secret store):")
    tenant = os.environ.get("AZURE_TENANT_ID") or input("  AZURE_TENANT_ID: ").strip()
    client = os.environ.get("AZURE_CLIENT_ID") or input("  AZURE_CLIENT_ID: ").strip()
    secret = os.environ.get("AZURE_CLIENT_SECRET") or getpass.getpass("  AZURE_CLIENT_SECRET (hidden): ").strip()
    drive = os.environ.get("SHAREPOINT_DRIVE_ID") or input("  SHAREPOINT_DRIVE_ID: ").strip()
    if not all([tenant, client, secret, drive]):
        log.error("All four values are required.")
        sys.exit(1)
    return tenant, client, secret, drive


def authenticate(tenant, client, secret):
    app = msal.ConfidentialClientApplication(
        client,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        log.error("Authentication failed: %s",
                  result.get("error_description", result.get("error", "unknown")))
        sys.exit(1)
    log.info("Authenticated to Microsoft Graph.")
    return result["access_token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def list_backups(token, drive_id):
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{BACKUP_FOLDER}:/children"
    resp = requests.get(url, headers=headers(token))
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    dates = [i["name"] for i in resp.json().get("value", []) if "folder" in i]
    return sorted(dates, reverse=True)


def list_files_in_backup(token, drive_id, date):
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{BACKUP_FOLDER}/{date}:/children"
    resp = requests.get(url, headers=headers(token))
    resp.raise_for_status()
    return [i["name"] for i in resp.json().get("value", []) if "file" in i]


def download_file(token, drive_id, date, name, dest):
    """Stream a backed-up file to its destination path."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{BACKUP_FOLDER}/{date}/{name}:/content"
    resp = requests.get(url, headers=headers(token), stream=True)
    resp.raise_for_status()

    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # If a live file already exists, keep a .bak rather than clobbering silently.
    if dest_path.exists():
        backup_copy = dest_path.with_suffix(dest_path.suffix + ".pre-restore.bak")
        dest_path.replace(backup_copy)
        log.info("  existing %s moved to %s", dest_path.name, backup_copy.name)

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    # Lock down secret files.
    if dest_path.name in (".env", "ecosystem.config.cjs"):
        os.chmod(dest_path, 0o600)
    log.info("  restored %s -> %s", name, dest)


def run(args):
    tenant, client, secret, drive = prompt_credentials()
    token = authenticate(tenant, client, secret)

    dates = list_backups(token, drive)
    if not dates:
        log.error("No backups found under '%s' in this drive.", BACKUP_FOLDER)
        sys.exit(1)

    if args.list:
        log.info("Available backups (newest first):")
        for d in dates:
            log.info("  %s", d)
        return 0

    date = args.date or dates[0]
    if date not in dates:
        log.error("Backup '%s' not found. Available: %s", date, ", ".join(dates))
        sys.exit(1)
    log.info("Restoring from backup dated %s", date)

    files = list_files_in_backup(token, drive, date)
    if not files:
        log.error("Backup %s is empty.", date)
        sys.exit(1)

    for name in files:
        dest = RESTORE_MAP.get(name)
        if not dest:
            log.warning("  unknown file in backup, skipping: %s", name)
            continue
        download_file(token, drive, date, name, dest)

    log.info("Restore complete from %s. Restored %d file(s).", date, len(files))
    log.info("Next: review the restored .env, then start the pipeline / dashboard.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Restore Pi state from a SharePoint backup.")
    parser.add_argument("--list", action="store_true", help="List available backups and exit.")
    parser.add_argument("--date", help="Restore a specific dated backup (YYYY-MM-DD). Default: newest.")
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
