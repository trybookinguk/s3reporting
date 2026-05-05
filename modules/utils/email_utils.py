"""
Generic email utilities for TryBooking reports.

Sends mail via Microsoft Graph using OAuth client credentials. The Azure app
registration is shared with s3_to_sharepoint.py and is restricted to the
shared mailbox via an Exchange Application Access Policy.
"""
import base64
import time
import requests
import msal

from .config import (
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    AZURE_SENDER_MAILBOX, TEST_MODE,
)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph rejects sendMail payloads above ~4 MB. Use 3 MB as the inline ceiling
# so headers + base64 overhead don't push the request past the limit.
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024

_token_cache = {"value": None, "expires_at": 0}


def _get_access_token():
    """Acquire (and cache) an application access token for Microsoft Graph."""
    now = time.time()
    if _token_cache["value"] and _token_cache["expires_at"] - now > 60:
        return _token_cache["value"]

    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX]):
        raise RuntimeError(
            "Missing Azure credentials. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, "
            "AZURE_CLIENT_SECRET, AZURE_SENDER_MAILBOX."
        )

    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Graph token acquisition failed: "
            f"{result.get('error_description', result.get('error', 'unknown'))}"
        )

    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
    return _token_cache["value"]


def _to_recipient_list(value):
    """Normalise a string or list of strings into Graph recipient objects.

    Accepts comma- or semicolon-separated addresses within a single string so
    callers carried over from the Mailgun era keep working.
    """
    if not value:
        return []
    raw_items = [value] if isinstance(value, str) else list(value)
    addresses = []
    for item in raw_items:
        if not item:
            continue
        for part in item.replace(";", ",").split(","):
            addr = part.strip()
            if addr:
                addresses.append(addr)
    return [{"emailAddress": {"address": addr}} for addr in addresses]


def _send_via_graph(message):
    """POST a Graph sendMail message and surface useful errors."""
    token = _get_access_token()
    url = f"{GRAPH_BASE}/users/{AZURE_SENDER_MAILBOX}/sendMail"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph sendMail failed ({resp.status_code}): {resp.text}")


def _send_via_draft(message, large_attachments):
    """For payloads above the inline limit: create a draft, upload large
    attachments via upload sessions, then send the draft."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    user_url = f"{GRAPH_BASE}/users/{AZURE_SENDER_MAILBOX}"

    create = requests.post(f"{user_url}/messages", headers=headers, json=message, timeout=60)
    if create.status_code >= 400:
        raise RuntimeError(f"Graph create draft failed ({create.status_code}): {create.text}")
    message_id = create.json()["id"]

    for filename, content, maintype, subtype in large_attachments:
        session = requests.post(
            f"{user_url}/messages/{message_id}/attachments/createUploadSession",
            headers=headers,
            json={"AttachmentItem": {
                "attachmentType": "file",
                "name": filename,
                "size": len(content),
                "contentType": f"{maintype}/{subtype}",
            }},
            timeout=60,
        )
        if session.status_code >= 400:
            raise RuntimeError(f"Upload session failed ({session.status_code}): {session.text}")
        upload_url = session.json()["uploadUrl"]

        # Upload in <=4 MiB chunks per the Graph spec.
        chunk_size = 4 * 1024 * 1024
        total = len(content)
        for start in range(0, total, chunk_size):
            chunk = content[start:start + chunk_size]
            end = start + len(chunk) - 1
            put = requests.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
                data=chunk,
                timeout=120,
            )
            if put.status_code >= 400:
                raise RuntimeError(f"Chunk upload failed ({put.status_code}): {put.text}")

    send = requests.post(
        f"{user_url}/messages/{message_id}/send", headers=headers, timeout=60
    )
    if send.status_code >= 400:
        raise RuntimeError(f"Graph send draft failed ({send.status_code}): {send.text}")


def send_html_email(to, subject, html_content, cc=None, bcc=None, plain_text=None, attachments=None):
    """
    Send an HTML email via Microsoft Graph.

    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject (will be prefixed with [TEST] in test mode)
        html_content: HTML body content
        cc: CC recipients - string or list (optional)
        bcc: BCC recipients - string or list (optional)
        plain_text: Plain text alternative (kept for signature compatibility,
                    not used by Graph - HTML body is sent as-is)
        attachments: List of tuples (filename, content, maintype, subtype) (optional)
    """
    to_recipients = _to_recipient_list(to)
    cc_recipients = _to_recipient_list(cc)
    bcc_recipients = _to_recipient_list(bcc)

    message = {
        "subject": f"{'[TEST] ' if TEST_MODE else ''}{subject}",
        "body": {"contentType": "HTML", "content": html_content},
        "toRecipients": to_recipients,
        "ccRecipients": cc_recipients,
        "bccRecipients": bcc_recipients,
    }

    inline_attachments = []
    large_attachments = []
    if attachments:
        for filename, content, maintype, subtype in attachments:
            if len(content) <= INLINE_ATTACHMENT_LIMIT:
                inline_attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": f"{maintype}/{subtype}",
                    "contentBytes": base64.b64encode(content).decode("ascii"),
                })
            else:
                large_attachments.append((filename, content, maintype, subtype))

    if inline_attachments:
        message["attachments"] = inline_attachments

    log_recipients = ", ".join(r["emailAddress"]["address"] for r in to_recipients + cc_recipients)
    print(f"\nSending email to: {log_recipients}")

    if large_attachments:
        _send_via_draft(message, large_attachments)
    else:
        _send_via_graph(message)

    print("Email sent successfully!")


def create_html_table(data, headers=None, style="border-collapse: collapse;"):
    """
    Create an HTML table from a list of dictionaries or pandas DataFrame.

    Args:
        data: List of dicts or pandas DataFrame
        headers: Optional list of headers (uses dict keys if not provided)
        style: CSS style for the table

    Returns:
        HTML string for the table
    """
    import pandas as pd

    if isinstance(data, pd.DataFrame):
        data = data.to_dict('records')

    if not data:
        return "<p>No data available</p>"

    if headers is None:
        headers = list(data[0].keys())

    html = f'<table border="1" cellpadding="5" cellspacing="0" style="{style}">\n'

    html += '<tr style="background-color: #f0f0f0;">\n'
    for header in headers:
        html += f'<th>{header}</th>\n'
    html += '</tr>\n'

    for row in data:
        html += '<tr>\n'
        for header in headers:
            value = row.get(header, '')
            html += f'<td>{value}</td>\n'
        html += '</tr>\n'

    html += '</table>'

    return html


def send_html_email_with_attachments(to, subject, html_content, attachments=None, cc=None, bcc=None):
    """
    Send an HTML email with CSV file attachments.

    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject
        html_content: HTML body content
        attachments: List of file paths to attach as CSV files
        cc: CC recipients (optional)
        bcc: BCC recipients (optional)
    """
    attachment_tuples = []

    if attachments:
        for filepath in attachments:
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()
                    filename = filepath.split('/')[-1]
                    attachment_tuples.append((filename, content, 'text', 'csv'))
                    print(f"  Attached: {filename}")
            except Exception as e:
                print(f"  Warning: Could not attach {filepath}: {str(e)}")

    send_html_email(
        to=to,
        subject=subject,
        html_content=html_content,
        cc=cc,
        bcc=bcc,
        attachments=attachment_tuples if attachment_tuples else None
    )
