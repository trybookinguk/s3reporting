"""
Microsoft Graph / SharePoint helpers.

Single source of truth for talking to the Platform Data drive on SharePoint —
authentication, small-file uploads, large-file resumable uploads, downloads.
Used by both the dashboard pipeline and the tier reporting pipeline.
"""

import logging
import time
from typing import Optional

import msal
import requests

from .config import AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
SHAREPOINT_FOLDER = "Platform Data/Dashboard Data"

# Graph's small-file PUT endpoint caps at 4 MiB. Anything larger needs the
# resumable upload session API.
SMALL_UPLOAD_LIMIT_BYTES = 4 * 1024 * 1024

# Resumable uploads must use multiples of 320 KiB per chunk; 10 MiB is a
# common sweet spot — large enough to amortise round-trips, small enough to
# survive transient network blips.
RESUMABLE_CHUNK_BYTES = 10 * 1024 * 1024

MAX_RETRIES = 3
RETRY_BACKOFF = 2


def authenticate_graph() -> Optional[str]:
    """Authenticate to Microsoft Graph and return an access token, or None on failure."""
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        log.error("Azure credentials not set. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET")
        return None

    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        log.error("Graph auth failed: %s", result.get("error_description", result.get("error", "Unknown")))
        return None

    return result["access_token"]


def _request_with_retry(method, url, **kwargs):
    """HTTP request with retry for 429 (throttling) and 5xx (transient server errors)."""
    response = None
    for attempt in range(MAX_RETRIES):
        try:
            response = method(url, **kwargs)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
                log.warning("Throttled, waiting %ds...", retry_after)
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


def _content_url(drive_id: str, filename: str, folder: str = SHAREPOINT_FOLDER) -> str:
    path = f"{folder}/{filename}" if folder else filename
    return f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/content"


def _session_url(drive_id: str, filename: str, folder: str = SHAREPOINT_FOLDER) -> str:
    path = f"{folder}/{filename}" if folder else filename
    return f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/createUploadSession"


def download_file(token: str, drive_id: str, filename: str,
                  folder: str = SHAREPOINT_FOLDER) -> Optional[bytes]:
    """Download a file from SharePoint. Returns None on 404, raises on other failures."""
    url = _content_url(drive_id, filename, folder)
    response = _request_with_retry(
        requests.get, url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    )
    if response.status_code == 200:
        return response.content
    if response.status_code == 404:
        return None
    log.warning("Graph fetch of %s failed: %d %s",
                filename, response.status_code, response.text[:200])
    response.raise_for_status()
    return None


def upload_small(token: str, drive_id: str, filename: str, data: bytes,
                 folder: str = SHAREPOINT_FOLDER) -> bool:
    """Upload via the small-file PUT endpoint. Caller must ensure data <= 4 MiB."""
    if len(data) > SMALL_UPLOAD_LIMIT_BYTES:
        raise ValueError(
            f"upload_small called with {len(data):,} bytes (>{SMALL_UPLOAD_LIMIT_BYTES:,}); "
            "use upload_large instead."
        )
    url = _content_url(drive_id, filename, folder)
    response = _request_with_retry(
        requests.put, url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=data,
    )
    if response.status_code in (200, 201):
        log.info("Uploaded %s (%d bytes)", filename, len(data))
        return True
    log.error("Upload failed for %s: %d - %s", filename, response.status_code, response.text[:500])
    return False


def upload_large(token: str, drive_id: str, filename: str, data: bytes,
                 folder: str = SHAREPOINT_FOLDER,
                 chunk_size: int = RESUMABLE_CHUNK_BYTES) -> bool:
    """Upload via Graph's resumable upload session. Required for files >4 MiB.

    Splits the payload into chunks (must be multiples of 320 KiB) and PUTs each with
    a Content-Range header. Graph confirms each chunk; the final chunk yields the
    DriveItem metadata response.
    """
    session_url = _session_url(drive_id, filename, folder)
    session_resp = _request_with_retry(
        requests.post, session_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
    )
    if session_resp.status_code not in (200, 201):
        log.error("Failed to create upload session for %s: %d - %s",
                  filename, session_resp.status_code, session_resp.text[:500])
        return False

    upload_url = session_resp.json().get("uploadUrl")
    if not upload_url:
        log.error("Upload session for %s missing uploadUrl in response", filename)
        return False

    total = len(data)
    sent = 0
    while sent < total:
        end = min(sent + chunk_size, total) - 1
        chunk = data[sent:end + 1]
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {sent}-{end}/{total}",
        }
        # Note: chunk PUTs to the session URL must NOT include the auth header —
        # the session URL is pre-signed.
        chunk_resp = _request_with_retry(requests.put, upload_url, headers=headers, data=chunk, timeout=300)
        if chunk_resp.status_code not in (200, 201, 202):
            log.error("Chunk upload failed for %s at bytes %d-%d/%d: %d - %s",
                      filename, sent, end, total, chunk_resp.status_code, chunk_resp.text[:500])
            return False
        sent = end + 1
        log.debug("  uploaded %d/%d bytes (%.0f%%)", sent, total, sent / total * 100)

    log.info("Uploaded %s (%d bytes, resumable)", filename, total)
    return True


def upload(token: str, drive_id: str, filename: str, data: bytes,
           folder: str = SHAREPOINT_FOLDER) -> bool:
    """Upload a file, picking small-file PUT or resumable session based on size."""
    if len(data) <= SMALL_UPLOAD_LIMIT_BYTES:
        return upload_small(token, drive_id, filename, data, folder)
    return upload_large(token, drive_id, filename, data, folder)


def list_files(token: str, drive_id: str, folder: str = SHAREPOINT_FOLDER) -> list:
    """Return the names of the items directly in a SharePoint folder (paged)."""
    path = folder
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children?$select=name&$top=200"
    names = []
    while url:
        resp = _request_with_retry(
            requests.get, url, headers={"Authorization": f"Bearer {token}"}, timeout=60
        )
        if resp.status_code != 200:
            log.warning("Listing %s failed: %d - %s", folder, resp.status_code, resp.text[:200])
            resp.raise_for_status()
            break
        body = resp.json()
        names.extend(item["name"] for item in body.get("value", []) if "name" in item)
        url = body.get("@odata.nextLink")
    return names


def get_web_url(token: str, drive_id: str, filename: str,
                folder: str = SHAREPOINT_FOLDER) -> Optional[str]:
    """Return the SharePoint webUrl for a file (opens it in the browser), or None."""
    path = f"{folder}/{filename}" if folder else filename
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}?$select=webUrl"
    resp = _request_with_retry(
        requests.get, url, headers={"Authorization": f"Bearer {token}"}, timeout=60
    )
    if resp.status_code == 200:
        return resp.json().get("webUrl")
    log.warning("webUrl fetch for %s failed: %d - %s", filename, resp.status_code, resp.text[:200])
    return None
