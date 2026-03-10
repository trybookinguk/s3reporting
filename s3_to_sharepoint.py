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
import os
import sys
import tempfile
import time
from pathlib import Path

import msal
import requests

# === Configuration ===

# AWS — match the credential lookup pattern from modules/utils/config.py
# without importing it (which pulls in pytz, pandas, etc.)
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")
S3_BUCKET = "produk-rdsextracts-438255373632"

# Azure / Microsoft Graph
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

# SharePoint
SHAREPOINT_SITE_ID = os.environ.get("SHAREPOINT_SITE_ID")
SHAREPOINT_DRIVE_ID = os.environ.get("SHAREPOINT_DRIVE_ID")
SHAREPOINT_FOLDER = os.environ.get("SHAREPOINT_FOLDER", "S3 Data")

# Graph API
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Upload thresholds
SMALL_FILE_LIMIT = 4 * 1024 * 1024  # 4 MB
UPLOAD_CHUNK_SIZE = 3932160  # 3.75 MB (must be multiple of 320 KiB)

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubled each retry

# Manifest
MANIFEST_DIR = Path(".sync_manifest")
MANIFEST_FILE = MANIFEST_DIR / "s3_etags.json"


# === Authentication ===

def authenticate_graph():
    """Authenticate to Microsoft Graph using client credentials and return an access token."""
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        print("ERROR: Azure credentials not set. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET")
        sys.exit(1)

    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        print(f"ERROR: Failed to authenticate: {result.get('error_description', result.get('error', 'Unknown error'))}")
        sys.exit(1)

    print("Authenticated to Microsoft Graph successfully.")
    return result["access_token"]


def graph_headers(token):
    """Return standard headers for Graph API requests."""
    return {"Authorization": f"Bearer {token}"}


# === S3 Operations ===

def get_s3_client():
    """Create and return a boto3 S3 client."""
    import boto3

    if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]):
        print("ERROR: AWS credentials not set. Required: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        sys.exit(1)

    return boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )


def list_s3_objects(s3_client, bucket):
    """List all objects in the S3 bucket, returning a dict of {key: etag}."""
    objects = {}
    paginator = s3_client.get_paginator("list_objects_v2")

    print(f"Listing objects in s3://{bucket}/ ...")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = obj["ETag"]

    print(f"  Found {len(objects)} objects in S3.")
    return objects


def download_s3_file(s3_client, bucket, key, dest_path):
    """Download a file from S3 to a local path with retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            s3_client.download_file(bucket, key, dest_path)
            return
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"  Retry {attempt + 1}/{MAX_RETRIES} for S3 download of {key} (waiting {wait}s): {e}")
                time.sleep(wait)
            else:
                raise


# === Manifest Operations ===

def load_manifest():
    """Load the ETag manifest from local cache."""
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, "r") as f:
            manifest = json.load(f)
        print(f"Loaded manifest with {len(manifest)} entries.")
        return manifest

    print("No existing manifest found — all files will be uploaded.")
    return {}


def save_manifest(manifest):
    """Save the ETag manifest to local cache."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Saved manifest with {len(manifest)} entries.")


# === SharePoint Upload Operations ===

def _request_with_retry(method, url, **kwargs):
    """Make an HTTP request with retry logic for transient failures."""
    for attempt in range(MAX_RETRIES):
        try:
            response = method(url, **kwargs)

            # Retry on throttling (429) or server errors (5xx)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
                print(f"  Throttled by Graph API, waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    print(f"  Server error {response.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue

            return response

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"  Request failed, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise

    return response


def upload_small_file(token, drive_id, folder, key, data):
    """Upload a file ≤ 4MB using a simple PUT request."""
    # Build the path: folder/key (preserving S3 directory structure)
    path = f"{folder}/{key}" if folder else key
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/content"

    headers = graph_headers(token)
    headers["Content-Type"] = "application/octet-stream"

    response = _request_with_retry(requests.put, url, headers=headers, data=data)

    if response.status_code in (200, 201):
        return True

    print(f"  ERROR uploading {key}: {response.status_code} — {response.text[:200]}")
    return False


def upload_large_file(token, drive_id, folder, key, file_path, file_size):
    """Upload a file > 4MB using a resumable upload session."""
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

    if response.status_code not in (200, 201):
        print(f"  ERROR creating upload session for {key}: {response.status_code} — {response.text[:200]}")
        return False

    upload_url = response.json()["uploadUrl"]

    # Upload in chunks
    with open(file_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk_end = min(offset + UPLOAD_CHUNK_SIZE, file_size) - 1
            chunk_data = f.read(UPLOAD_CHUNK_SIZE)
            chunk_length = len(chunk_data)

            chunk_headers = {
                "Content-Length": str(chunk_length),
                "Content-Range": f"bytes {offset}-{chunk_end}/{file_size}",
            }

            chunk_response = _request_with_retry(
                requests.put, upload_url, headers=chunk_headers, data=chunk_data
            )

            if chunk_response.status_code not in (200, 201, 202):
                print(f"  ERROR uploading chunk for {key}: {chunk_response.status_code} — {chunk_response.text[:200]}")
                return False

            offset += chunk_length

    return True


def delete_sharepoint_file(token, drive_id, folder, key):
    """Delete a file from SharePoint."""
    path = f"{folder}/{key}" if folder else key
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}"

    response = _request_with_retry(requests.delete, url, headers=graph_headers(token))

    if response.status_code in (200, 204):
        return True

    # 404 means already gone — not an error
    if response.status_code == 404:
        return True

    print(f"  ERROR deleting {key}: {response.status_code} — {response.text[:200]}")
    return False


# === Sync Orchestration ===

def sync(dry_run=False):
    """Main sync: compare S3 ETags to manifest, upload changed files to SharePoint."""
    if not all([SHAREPOINT_SITE_ID, SHAREPOINT_DRIVE_ID]):
        print("ERROR: SHAREPOINT_SITE_ID and SHAREPOINT_DRIVE_ID must be set.")
        print("Run with --setup to discover these values.")
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

    for key, etag in s3_objects.items():
        if manifest.get(key) != etag:
            to_upload.append(key)
        else:
            unchanged += 1

    for key in manifest:
        if key not in s3_objects:
            to_delete.append(key)

    print(f"\nSync summary:")
    print(f"  Unchanged: {unchanged}")
    print(f"  To upload:  {len(to_upload)}")
    print(f"  To delete:  {len(to_delete)}")

    if dry_run:
        if to_upload:
            print("\nFiles to upload:")
            for key in sorted(to_upload):
                print(f"  + {key}")
        if to_delete:
            print("\nFiles to delete:")
            for key in sorted(to_delete):
                print(f"  - {key}")
        print("\nDry run complete — no changes made.")
        return

    if not to_upload and not to_delete:
        print("Nothing to do.")
        return

    # Process uploads
    uploaded = 0
    failed = 0
    new_manifest = dict(manifest)

    for i, key in enumerate(to_upload, 1):
        print(f"[{i}/{len(to_upload)}] Uploading {key} ...")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
            tmp_path = tmp.name

        try:
            download_s3_file(s3_client, S3_BUCKET, key, tmp_path)
            file_size = os.path.getsize(tmp_path)

            if file_size <= SMALL_FILE_LIMIT:
                with open(tmp_path, "rb") as f:
                    data = f.read()
                success = upload_small_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key, data)
            else:
                success = upload_large_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key, tmp_path, file_size)

            if success:
                new_manifest[key] = s3_objects[key]
                uploaded += 1
            else:
                failed += 1

        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Process deletions
    deleted = 0
    for i, key in enumerate(to_delete, 1):
        print(f"[{i}/{len(to_delete)}] Deleting {key} from SharePoint ...")
        if delete_sharepoint_file(token, SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, key):
            new_manifest.pop(key, None)
            deleted += 1
        else:
            failed += 1

    # Save updated manifest
    save_manifest(new_manifest)

    # Final summary
    print(f"\n{'=' * 40}")
    print(f"Sync complete:")
    print(f"  Uploaded: {uploaded}")
    print(f"  Deleted:  {deleted}")
    print(f"  Failed:   {failed}")
    print(f"  Skipped:  {unchanged}")
    print(f"{'=' * 40}")

    if failed > 0:
        print(f"\nWARNING: {failed} operation(s) failed. These files will be retried on the next run.")


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
        # Filter may not be supported on all tenants — fall back to unfiltered
        if path == "root":
            url = f"{GRAPH_BASE}/drives/{drive_id}/root/children?$select=name,id,folder,webUrl"
        else:
            url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children?$select=name,id,folder,webUrl"
        response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"  ERROR listing folders: {response.status_code} — {response.text[:300]}")
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
    print("=== S3 to SharePoint Sync — Setup ===\n")

    token = authenticate_graph()
    headers = graph_headers(token)

    # Step 1: Choose a SharePoint site
    print("Fetching SharePoint sites...\n")
    response = requests.get(f"{GRAPH_BASE}/sites?search=*", headers=headers)

    if response.status_code != 200:
        print(f"ERROR fetching sites: {response.status_code} — {response.text[:300]}")
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
        print(f"ERROR fetching drives: {response.status_code} — {response.text[:300]}")
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
        print(f"  SHAREPOINT_FOLDER   = (leave empty or omit — files go to the library root)")
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
    args = parser.parse_args()

    if args.setup:
        setup()
    else:
        sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
