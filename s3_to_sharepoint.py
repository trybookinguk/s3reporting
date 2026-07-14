#!/usr/bin/env python3
"""
S3 to SharePoint Sync

Mirrors the contents of an S3 bucket to a SharePoint folder via Microsoft Graph API.
Only uploads files whose content has changed (detected via S3 ETags).

Usage:
    python3 s3_to_sharepoint.py              # Run sync
    python3 s3_to_sharepoint.py --setup      # Discover SharePoint site/drive IDs
    python3 s3_to_sharepoint.py --dry-run    # Preview changes without uploading
"""

import argparse
import json
import logging
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("s3-sharepoint-sync")

# === Configuration ===

# AWS - match the credential lookup pattern from modules/utils/config.py
# without importing it (which pulls in pytz, pandas, etc.)
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")
# Optional — set only for temporary/MFA credentials (aws sts get-session-token).
AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN") or os.environ.get("AWS_SECURITY_TOKEN")
S3_BUCKET = "produk-rdsextracts-438255373632"

# Azure / Microsoft Graph
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

# SharePoint
SHAREPOINT_SITE_ID = os.environ.get("SHAREPOINT_SITE_ID")
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")
SHAREPOINT_FOLDER = "Platform Data/S3 Exports"

# Graph API
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Upload thresholds
SMALL_FILE_LIMIT = 4 * 1024 * 1024  # 4 MB
UPLOAD_CHUNK_SIZE = 32 * 320 * 1024  # 10 MiB — optimal per Graph API docs (multiple of 320 KiB)

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubled each retry

# Concurrency - Graph API allows up to 20 concurrent requests per app
MAX_WORKERS = int(os.environ.get("SYNC_MAX_WORKERS", "10"))

# Manifest — persistent local path. On the Pi this lives at
# /root/reporting/.sync_manifest; override with SYNC_MANIFEST_DIR if the script
# is run from somewhere other than the repo root.
MANIFEST_DIR = Path(os.environ.get("SYNC_MANIFEST_DIR", ".sync_manifest"))
MANIFEST_FILE = MANIFEST_DIR / "s3_etags.json"


def _fmt_size(size_bytes):
    """Format a byte count as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _fmt_duration(seconds):
    """Format seconds as a human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


# === Authentication ===

def authenticate_graph():
    """Authenticate to Microsoft Graph using client credentials and return an access token."""
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        log.error("Azure credentials not set. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET")
        sys.exit(1)

    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        log.error("Failed to authenticate: %s", result.get('error_description', result.get('error', 'Unknown error')))
        sys.exit(1)

    log.info("Authenticated to Microsoft Graph.")
    return result["access_token"]


def graph_headers(token):
    """Return standard headers for Graph API requests."""
    return {"Authorization": f"Bearer {token}"}


# === S3 Operations ===

def get_s3_client():
    """Create and return a boto3 S3 client."""
    import boto3
    from botocore.config import Config

    if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]):
        log.error("AWS credentials not set. Required: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        sys.exit(1)

    client_kwargs = {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
        "config": Config(max_pool_connections=MAX_WORKERS),
    }
    # Only pass the session token when set (temporary/MFA credentials).
    if AWS_SESSION_TOKEN:
        client_kwargs["aws_session_token"] = AWS_SESSION_TOKEN
    return boto3.client("s3", **client_kwargs)


def list_s3_objects(s3_client, bucket):
    """List all objects in the S3 bucket, returning a dict of {key: (etag, size)}.

    For BookingDataAll files, only the latest one is included since each
    contains all historical data up to that point (older ones are redundant).
    """
    objects = {}
    booking_data_all = {}
    total_size = 0
    paginator = s3_client.get_paginator("list_objects_v2")

    log.info("Listing objects in s3://%s/ ...", bucket)
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            size = obj["Size"]
            if "BookingDataAll-TBUK.csv" in key:
                booking_data_all[key] = (obj["ETag"], size)
            else:
                objects[key] = (obj["ETag"], size)
                total_size += size

    # Keep only the latest BookingDataAll (filename sorts chronologically)
    if booking_data_all:
        latest_key = sorted(booking_data_all.keys())[-1]
        etag, size = booking_data_all[latest_key]
        objects[latest_key] = (etag, size)
        total_size += size
        skipped = len(booking_data_all) - 1
        log.info("BookingDataAll: keeping latest (%s, %s), skipping %d older files.",
                 latest_key, _fmt_size(size), skipped)

    log.info("Found %d objects to sync (%s total).", len(objects), _fmt_size(total_size))
    return objects


def download_s3_bytes(s3_client, bucket, key):
    """Download a (small) file from S3 fully into memory, with retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Retry %d/%d for S3 download of %s (waiting %ds): %s",
                            attempt + 1, MAX_RETRIES, key, wait, e)
                time.sleep(wait)
            else:
                raise


def get_s3_object_stream(s3_client, bucket, key):
    """Open a streaming S3 object body, with retry logic for the initial request.

    Used for large files so they can be piped straight into the SharePoint
    upload session without ever being buffered to local disk - downloading
    a multi-GB file to a temp file on a constrained device (e.g. the Pi)
    can exhaust local disk space, which is what caused repeated
    "No space left on device" failures.
    """
    for attempt in range(MAX_RETRIES):
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            return obj["Body"], obj["ContentLength"]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Retry %d/%d for S3 download of %s (waiting %ds): %s",
                            attempt + 1, MAX_RETRIES, key, wait, e)
                time.sleep(wait)
            else:
                raise


# === Manifest Operations ===

def load_manifest():
    """Load the ETag manifest from local cache."""
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, "r") as f:
            manifest = json.load(f)
        log.info("Loaded manifest with %d entries.", len(manifest))
        return manifest

    log.info("No existing manifest found - all files will be uploaded.")
    return {}


def save_manifest(manifest):
    """Save the ETag manifest to local cache."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info("Saved manifest with %d entries.", len(manifest))


# === SharePoint Upload Operations ===

def _request_with_retry(method, url, **kwargs):
    """Make an HTTP request with retry logic for transient failures."""
    for attempt in range(MAX_RETRIES):
        try:
            response = method(url, **kwargs)

            # Retry on throttling (429) or server errors (5xx)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
                log.warning("Throttled by Graph API, waiting %ds...", retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    log.warning("Server error %d, retrying in %ds...", response.status_code, wait)
                    time.sleep(wait)
                    continue

            return response

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Request failed, retrying in %ds: %s", wait, e)
                time.sleep(wait)
            else:
                raise

    return response


def upload_small_file(token, drive_id, folder, key, data):
    """Upload a file <= 4MB using a simple PUT request."""
    path = f"{folder}/{key}" if folder else key
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/content"

    headers = graph_headers(token)
    headers["Content-Type"] = "application/octet-stream"

    response = _request_with_retry(requests.put, url, headers=headers, data=data)

    if response.status_code in (200, 201):
        return True

    log.error("Upload failed for %s: %d - %s", key, response.status_code, response.text[:200])
    return False


def upload_large_file(token, drive_id, folder, key, stream, file_size):
    """Upload a file > 4MB using a resumable upload session.

    `stream` is any file-like object supporting sequential .read(n) calls
    (a local file handle or, more commonly here, a boto3 StreamingBody read
    directly from S3) - chunks are read and uploaded in order, so no local
    buffering of the whole file is required.
    """
    path = f"{folder}/{key}" if folder else key
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/createUploadSession"

    headers = graph_headers(token)
    headers["Content-Type"] = "application/json"

    body = {
        "item": {
            "@microsoft.graph.conflictBehavior": "replace",
        }
    }

    response = _request_with_retry(requests.post, url, headers=headers, json=body)

    # 409 = stale upload session still active from a previous run; delete the file and retry
    if response.status_code == 409:
        log.warning("Stale upload session for %s, deleting existing file and retrying...", key)
        delete_url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}"
        _request_with_retry(requests.delete, delete_url, headers=graph_headers(token))
        time.sleep(2)
        response = _request_with_retry(requests.post, url, headers=headers, json=body)

    if response.status_code not in (200, 201):
        log.error("Upload session failed for %s: %d - %s", key, response.status_code, response.text[:200])
        return False

    upload_url = response.json()["uploadUrl"]

    # Upload in chunks — log progress at INFO level for large files (>100 MB)
    is_large = file_size > 100 * 1024 * 1024
    offset = 0
    chunk_num = 0
    total_chunks = (file_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
    chunk_start_time = time.time()
    while offset < file_size:
        chunk_end = min(offset + UPLOAD_CHUNK_SIZE, file_size) - 1
        chunk_data = stream.read(UPLOAD_CHUNK_SIZE)
        chunk_length = len(chunk_data)
        chunk_num += 1

        chunk_headers = {
            "Content-Length": str(chunk_length),
            "Content-Range": f"bytes {offset}-{chunk_end}/{file_size}",
        }

        chunk_response = _request_with_retry(
            requests.put, upload_url, headers=chunk_headers, data=chunk_data
        )

        if chunk_response.status_code not in (200, 201, 202):
            log.error("Chunk upload failed for %s (chunk %d/%d): %d - %s",
                      key, chunk_num, total_chunks, chunk_response.status_code, chunk_response.text[:200])
            return False

        pct = int((chunk_end + 1) / file_size * 100)
        if is_large and (chunk_num % 10 == 0 or pct == 100):
            elapsed = time.time() - chunk_start_time
            rate = (offset + chunk_length) / elapsed if elapsed > 0 else 0
            log.info("  %s: %d%% (%s/%s, %s/s)",
                     key, pct, _fmt_size(offset + chunk_length), _fmt_size(file_size), _fmt_size(rate))
        else:
            log.debug("  %s: chunk %d/%d (%d%%)", key, chunk_num, total_chunks, pct)

        offset += chunk_length

    return True


def delete_sharepoint_file(token, drive_id, folder, key):
    """Delete a file from SharePoint."""
    path = f"{folder}/{key}" if folder else key
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}"

    response = _request_with_retry(requests.delete, url, headers=graph_headers(token))

    if response.status_code in (200, 204):
        return True

    # 404 means already gone - not an error
    if response.status_code == 404:
        return True

    log.error("Delete failed for %s: %d - %s", key, response.status_code, response.text[:200])
    return False


# === Sync Orchestration ===

def sync(dry_run=False):
    """Main sync: compare S3 ETags to manifest, upload changed files to SharePoint."""
    sync_start = time.time()

    if not all([SHAREPOINT_SITE_ID, SHAREPOINT_DRIVE_ID]):
        log.error("SHAREPOINT_SITE_ID and SHAREPOINT_DRIVE_ID must be set.")
        log.error("Run with --setup to discover these values.")
        sys.exit(1)

    # Authenticate
    token = authenticate_graph()
    s3_client = get_s3_client()

    # Get current state
    s3_objects = list_s3_objects(s3_client, S3_BUCKET)
    manifest = load_manifest()

    # Determine what's changed
    to_upload = []
    to_delete = []
    unchanged = 0
    upload_size = 0

    for key, (etag, size) in s3_objects.items():
        if manifest.get(key) != etag:
            to_upload.append((key, size))
            upload_size += size
        else:
            unchanged += 1

    for key in manifest:
        if key not in s3_objects:
            to_delete.append(key)

    log.info("Sync summary: %d unchanged, %d to upload (%s), %d to delete.",
             unchanged, len(to_upload), _fmt_size(upload_size), len(to_delete))

    if dry_run:
        if to_upload:
            log.info("Files to upload:")
            for key, size in sorted(to_upload):
                log.info("  + %s (%s)", key, _fmt_size(size))
        if to_delete:
            log.info("Files to delete:")
            for key in sorted(to_delete):
                log.info("  - %s", key)
        log.info("Dry run complete - no changes made.")
        return

    if not to_upload and not to_delete:
        log.info("Nothing to do.")
        return

    # Thread-safe state
    lock = threading.Lock()
    new_manifest = dict(manifest)
    uploaded = 0
    uploaded_bytes = 0
    failed = 0
    deleted = 0

    def _upload_one(key, expected_size):
        """Stream from S3 straight into a SharePoint upload. Returns (key, success, file_size).

        Files are never buffered to local disk: small files are read fully
        into memory (cheap, <= SMALL_FILE_LIMIT) and large files are piped
        chunk-by-chunk from the S3 response body into the SharePoint upload
        session. This avoids exhausting local disk space on constrained
        devices (e.g. the Pi) when syncing large files like BookingDataAll.
        """
        try:
            t0 = time.time()
            if expected_size <= SMALL_FILE_LIMIT:
                data = download_s3_bytes(s3_client, S3_BUCKET, key)
                file_size = len(data)
                dl_time = time.time() - t0

                t1 = time.time()
                success = upload_small_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key, data)
            else:
                stream, file_size = get_s3_object_stream(s3_client, S3_BUCKET, key)
                dl_time = time.time() - t0

                t1 = time.time()
                success = upload_large_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key, stream, file_size)
            ul_time = time.time() - t1

            if success:
                log.debug("  %s: S3 download %.1fs, SharePoint upload %.1fs", key, dl_time, ul_time)

            return key, success, file_size

        except Exception as e:
            log.error("Failed %s: %s", key, e)
            return key, False, 0

    def _delete_one(key):
        """Delete a file from SharePoint. Returns (key, success)."""
        return key, delete_sharepoint_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key)

    # Process uploads in parallel
    log.info("Uploading %d files (%s) with %d workers...", len(to_upload), _fmt_size(upload_size), MAX_WORKERS)
    upload_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_upload_one, key, size): key for key, size in to_upload}
        for i, future in enumerate(as_completed(futures), 1):
            key, success, file_size = future.result()
            if success:
                with lock:
                    new_manifest[key] = s3_objects[key][0]  # store etag only
                    uploaded += 1
                    uploaded_bytes += file_size
                log.info("[%d/%d] OK %s (%s)", i, len(to_upload), key, _fmt_size(file_size))
            else:
                with lock:
                    failed += 1
                log.error("[%d/%d] FAILED %s", i, len(to_upload), key)

            # Progress summary every 50 files
            if i % 50 == 0:
                elapsed = time.time() - upload_start
                rate = uploaded_bytes / elapsed if elapsed > 0 else 0
                log.info("Progress: %d/%d done, %s uploaded, %s/s",
                         i, len(to_upload), _fmt_size(uploaded_bytes), _fmt_size(rate))

    upload_elapsed = time.time() - upload_start

    # Process deletions in parallel
    if to_delete:
        log.info("Deleting %d files with %d workers...", len(to_delete), MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_delete_one, key): key for key in to_delete}
            for i, future in enumerate(as_completed(futures), 1):
                key, success = future.result()
                if success:
                    with lock:
                        new_manifest.pop(key, None)
                        deleted += 1
                    log.info("[%d/%d] Deleted %s", i, len(to_delete), key)
                else:
                    with lock:
                        failed += 1
                    log.error("[%d/%d] Delete FAILED %s", i, len(to_delete), key)

    # Save updated manifest
    save_manifest(new_manifest)

    # Final summary
    total_elapsed = time.time() - sync_start
    avg_rate = uploaded_bytes / upload_elapsed if upload_elapsed > 0 else 0

    log.info("=" * 50)
    log.info("Sync complete in %s", _fmt_duration(total_elapsed))
    log.info("  Uploaded: %d files (%s at %s/s)", uploaded, _fmt_size(uploaded_bytes), _fmt_size(avg_rate))
    log.info("  Deleted:  %d files", deleted)
    log.info("  Failed:   %d files", failed)
    log.info("  Skipped:  %d files (unchanged)", unchanged)
    log.info("=" * 50)

    if failed > 0:
        log.warning("%d operation(s) failed. These files will be retried on the next run.", failed)


# === Setup Mode ===

def _prompt_choice(items, label="item"):
    """Prompt the user to pick from a numbered list. Returns the chosen index."""
    print(f"\nChoose a {label} (1-{len(items)}): ", end="", flush=True)
    try:
        choice = int(input()) - 1
    except (ValueError, EOFError):
        print("Invalid input.")
        sys.exit(1)

    if choice < 0 or choice >= len(items):
        print("Invalid choice.")
        sys.exit(1)

    return choice


def _list_folders(headers, drive_id, path="root"):
    """List child folders at a given path in a drive. Returns list of folder items."""
    if path == "root":
        url = f"{GRAPH_BASE}/drives/{drive_id}/root/children?$filter=folder ne null&$select=name,id,folder,webUrl"
    else:
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children?$filter=folder ne null&$select=name,id,folder,webUrl"

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        # Filter may not be supported on all tenants - fall back to unfiltered
        if path == "root":
            url = f"{GRAPH_BASE}/drives/{drive_id}/root/children?$select=name,id,folder,webUrl"
        else:
            url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children?$select=name,id,folder,webUrl"
        response = requests.get(url, headers=headers)

    if response.status_code != 200:
        log.error("Listing folders failed: %d - %s", response.status_code, response.text[:300])
        return []

    items = response.json().get("value", [])
    # Keep only folders
    return [item for item in items if "folder" in item]


def _browse_folders(headers, drive_id):
    """Interactively browse folders in a document library. Returns the selected path string."""
    current_path = "root"
    path_parts = []

    while True:
        display_path = "/" + "/".join(path_parts) if path_parts else "/ (root)"
        print(f"\nCurrent location: {display_path}")

        folders = _list_folders(headers, drive_id, "/".join(path_parts) if path_parts else "root")

        print("\n  0. ** Use this folder **")
        if path_parts:
            print("  b. Go back up")

        if folders:
            for i, folder in enumerate(folders, 1):
                child_count = folder.get("folder", {}).get("childCount", "?")
                print(f"  {i}. {folder['name']}/  ({child_count} items)")
        else:
            print("  (no subfolders)")

        print(f"\nEnter choice: ", end="", flush=True)
        try:
            raw = input().strip().lower()
        except EOFError:
            sys.exit(1)

        if raw == "0":
            return "/".join(path_parts) if path_parts else ""
        elif raw == "b" and path_parts:
            path_parts.pop()
        elif raw == "n":
            print("Enter new folder name: ", end="", flush=True)
            new_name = input().strip()
            if new_name:
                path_parts.append(new_name)
                return "/".join(path_parts)
        else:
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(folders):
                    path_parts.append(folders[idx]["name"])
                else:
                    print("Invalid choice.")
            except ValueError:
                print("Invalid input.")


def setup():
    """Interactive setup to discover SharePoint site and drive IDs, and browse for the target folder."""
    print("=== S3 to SharePoint Sync - Setup ===\n")

    token = authenticate_graph()
    headers = graph_headers(token)

    # Step 1: Choose a SharePoint site
    print("Fetching SharePoint sites...\n")
    response = requests.get(f"{GRAPH_BASE}/sites?search=*", headers=headers)

    if response.status_code != 200:
        log.error("Fetching sites failed: %d - %s", response.status_code, response.text[:300])
        sys.exit(1)

    sites = response.json().get("value", [])

    if not sites:
        print("No SharePoint sites found. Check that the app has Sites.ReadWrite.All permission.")
        sys.exit(1)

    print("Available SharePoint sites:\n")
    for i, site in enumerate(sites, 1):
        print(f"  {i}. {site.get('displayName', 'Unnamed')}")
        print(f"     {site.get('webUrl', '')}")

    choice = _prompt_choice(sites, "site")
    site = sites[choice]
    site_id = site["id"]
    print(f"\nSelected: {site.get('displayName')}")

    # Step 2: Choose a document library
    print("\nFetching document libraries...\n")
    response = requests.get(f"{GRAPH_BASE}/sites/{site_id}/drives", headers=headers)

    if response.status_code != 200:
        log.error("Fetching drives failed: %d - %s", response.status_code, response.text[:300])
        sys.exit(1)

    drives = response.json().get("value", [])

    if not drives:
        print("No document libraries found for this site.")
        sys.exit(1)

    print("Available document libraries:\n")
    for i, drive in enumerate(drives, 1):
        print(f"  {i}. {drive.get('name', 'Unnamed')} (type: {drive.get('driveType', 'unknown')})")

    choice = _prompt_choice(drives, "library")
    drive = drives[choice]
    drive_id = drive["id"]
    print(f"\nSelected: {drive.get('name')}")

    # Step 3: Browse for the target folder
    print("\n--- Browse for the target folder ---")
    print("Navigate into the folder where S3 files should be synced.")
    print("Enter 'n' at any point to type a new folder name.\n")

    folder_path = _browse_folders(headers, drive_id)

    if folder_path:
        print(f"\nTarget folder: /{folder_path}")
    else:
        print("\nTarget folder: / (root of the document library)")

    # Output the values to add as secrets
    print("\n" + "=" * 60)
    print("Add these as GitHub Secrets:\n")
    print(f"  SHAREPOINT_SITE_ID  = {site_id}")
    print(f"  SHAREPOINT_DRIVE_ID = {drive_id}")
    if folder_path:
        print(f"  SHAREPOINT_FOLDER   = {folder_path}")
    else:
        print(f"  SHAREPOINT_FOLDER   = (leave empty or omit - files go to the library root)")
    print()
    print("The SHAREPOINT_FOLDER value is the exact path within the document")
    print("library. S3 file paths will be appended to it, e.g.:")
    if folder_path:
        print(f"  {folder_path}/2026/03/202603-Accounts-TBUK.csv")
    else:
        print(f"  2026/03/202603-Accounts-TBUK.csv")
    print("=" * 60)


# === CLI Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Sync S3 bucket to SharePoint via Microsoft Graph API")
    parser.add_argument("--setup", action="store_true", help="Discover SharePoint site and drive IDs")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without uploading")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.setup:
        setup()
    else:
        sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
