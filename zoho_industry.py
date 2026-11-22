import os
import sys
import logging
import pandas as pd
from pandas.tseries.offsets import MonthBegin
import requests
from datetime import datetime, timezone, timedelta

# Import shared modules
from modules.utils.config import S3_BUCKET, ZOHO_DOMAIN
from modules.utils.data_loader import get_s3_client, download_s3_file_cached
from modules.utils.zoho_api import get_access_token, upsert_to_zoho, delete_from_zoho
from modules.deleted_account_handler import process_deleted_accounts

logger = logging.getLogger(__name__)

# Freshness guard: the Accounts CSV is written to S3 ~01:00 daily; this job runs
# ~02:30. If the upstream export stalls, the file silently goes stale and new
# accounts never reach Zoho (observed Nov 2026).
#
# We judge freshness by the file's S3 LastModified (when it was WRITTEN), NOT by
# its content age (max DateTimeCreated). The content inherently lags: a file
# written at 01:00 on day D holds data through ~end of D-1, and is read by the
# 02:30 job, so a brand-new account legitimately reaches Zoho up to ~2 days after
# it was created. Content age therefore always looks 1–2 days behind even when
# healthy and is a useless trigger; write-age is clean.
#
# Healthy LastModified age at job time ≈ 1.5h (01:00 write, 02:30 run). A single
# MISSED daily export shows up as ≈ 25.6h (previous day's 01:00 file, read at
# 02:30). The default threshold sits between the two so a single skipped export
# fires the alert, while a merely-late write (a few hours) does not. Overridable.
ACCOUNTS_FILE_MAX_AGE_HOURS = float(os.environ.get("ACCOUNTS_FILE_MAX_AGE_HOURS", "24"))
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "henry@trybooking.co.uk")


def resolve_accounts_s3_key():
    """Resolve the S3 key for the Accounts CSV the job reads (yesterday's month)."""
    report_date = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=1)
    # Fallback if first day of month: use previous month
    if report_date.day == 1:
        report_date -= MonthBegin(1)
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")
    prefix = report_date.strftime("%Y%m")
    return f"{year}/{month}/{prefix}-Accounts-TBUK.csv"


# === S3 Download ===
def fetch_s3_report():
    # Use shared S3 client with caching
    s3 = get_s3_client()
    return download_s3_file_cached(s3, resolve_accounts_s3_key())


def check_accounts_file_freshness(s3, s3_key):
    """Return freshness info for the Accounts file via its S3 LastModified.

    Returns a dict: {key, last_modified, age_hours, stale, max_age_hours}.
    On a head_object failure we return stale=True so a missing/unreadable file
    is treated as a problem, not silently ignored.
    """
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
        last_modified = head["LastModified"]
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - last_modified).total_seconds() / 3600.0
        return {
            "key": s3_key,
            "last_modified": last_modified,
            "age_hours": age_hours,
            "stale": age_hours > ACCOUNTS_FILE_MAX_AGE_HOURS,
            "max_age_hours": ACCOUNTS_FILE_MAX_AGE_HOURS,
            "error": None,
        }
    except Exception as e:
        logger.warning("Could not head_object the Accounts file %s: %s", s3_key, e)
        return {
            "key": s3_key,
            "last_modified": None,
            "age_hours": None,
            "stale": True,
            "max_age_hours": ACCOUNTS_FILE_MAX_AGE_HOURS,
            "error": str(e),
        }


def alert_stale_accounts_file(freshness, max_created=None):
    """Email an alert that the S3 Accounts export is stale. Best-effort."""
    key = freshness["key"]
    if freshness["error"]:
        detail = f"the file could not be read from S3 ({freshness['error']})"
        age_txt = "unknown"
        lm_txt = "unknown"
    else:
        age_txt = f"{freshness['age_hours']:.1f} h"
        lm_txt = freshness["last_modified"].isoformat()
        detail = (f"it was last written to S3 at {lm_txt} "
                  f"({age_txt} ago), exceeding the {freshness['max_age_hours']:.0f} h threshold")
    created_txt = (f"<li>Newest account in the file (max DateTimeCreated): "
                   f"<b>{max_created}</b></li>") if max_created is not None else ""
    subject = f"[s3reporting] ALERT: Accounts export stale — new accounts not syncing to Zoho"
    body = (
        f"<p>FYI - the daily TryBooking Accounts export to S3 looks stale, so "
        f"<b>new accounts are not reaching Zoho</b>. zoho_industry.py still ran, "
        f"but it only had old data to import.</p>"
        f"<ul>"
        f"<li>S3 object: <code>s3://{S3_BUCKET}/{key}</code></li>"
        f"<li>Problem: {detail}</li>"
        f"{created_txt}"
        f"</ul>"
        f"<p>This is an <b>upstream</b> issue: the export that writes this file to "
        f"S3 (normally ~01:00 UTC daily) has not refreshed. Once it does, the next "
        f"zoho_industry run will create the backlogged accounts automatically; to "
        f"catch up sooner, re-run <code>python3 zoho_industry.py</code> after the "
        f"file refreshes.</p>"
        f"<p>(Threshold is ACCOUNTS_FILE_MAX_AGE_HOURS={freshness['max_age_hours']:.0f}h.)</p>"
    )
    try:
        from modules.utils.email_utils import send_html_email
        send_html_email(to=ALERT_EMAIL, subject=subject, html_content=body)
        print(f"⚠ Stale-file alert emailed to {ALERT_EMAIL}")
        logger.warning("Stale Accounts-file alert emailed to %s (%s)", ALERT_EMAIL, key)
    except Exception as e:
        print(f"⚠ Could not send stale-file alert email: {e}")
        logger.error("Failed to send stale-file alert email: %s", e)

# === Zoho CRM Fetch ===
def fetch_zoho_accounts(token):
    all_accounts = []
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    while True:
        params = {"page": page, "per_page": 200}
        resp = requests.get(f"{ZOHO_DOMAIN}/crm/v2/Accounts", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break
        all_accounts.extend(data)
        if not resp.json().get("info", {}).get("more_records"):
            break
        page += 1

    zoho_accounts = {acc["Account_Name"]: acc for acc in all_accounts if "Account_Name" in acc}
    return zoho_accounts

# === Compare + Prepare Upserts ===
def prepare_upserts(s3_df, zoho_map):
    upserts = []

    for _, row in s3_df.iterrows():
        account_id = str(int(row["Id"])) if pd.notna(row.get("Id")) else None
        if not account_id:
            continue

        business_name = str(row.get("AccountName", "")).strip()
        industry = str(row.get("Industry")) if pd.notna(row.get("Industry")) else None
        subindustry = str(row.get("SubIndustry")) if pd.notna(row.get("SubIndustry")) else None
        status = str(row.get("AccountStatus")) if pd.notna(row.get("AccountStatus")) else None

        def as_date(val):
            try:
                if pd.notna(val):
                    return pd.to_datetime(val, format="%Y-%m-%d %H:%M:%S").date().isoformat()
            except:
                pass
            return None

        created      = as_date(row.get("DateTimeCreated"))     # Keep full ISO
        last_login   = as_date(row.get("LastLogIn"))           # Keep full ISO
        first_event  = as_date(row.get("FirstEventCreation")) # Convert to date only
        last_event   = as_date(row.get("LastEventCreation"))  # Convert to date only

        payload = {"Account_Name": account_id}
        existing = zoho_map.get(account_id)

        new_fields = {
            "Business_Name": business_name or None,
            "Industry": industry,
            "SubIndustry": subindustry,
            "Account_Status": status,
            "DateTimeCreated": created,
            "Last_Login": last_login,
            "First_Event_Creation_Date": first_event,
            "Last_Event_Creation_Date": last_event
        }

        if not existing:
            # Always insert full record if account is new
            payload.update({k: v for k, v in new_fields.items() if v is not None})
            upserts.append(payload)
        else:
            # Only include changed fields for existing accounts
            changes = {}
            for key, new_val in new_fields.items():
                existing_val = existing.get(key, None)

                if key in ["Industry", "SubIndustry"]:
                    if new_val != existing_val:
                        changes[key] = new_val
                    continue

                if isinstance(new_val, str):
                    if (existing_val or "").strip() != new_val.strip():
                        changes[key] = new_val
                elif new_val != existing_val:
                    changes[key] = new_val

            if changes:
                payload.update(changes)
                upserts.append(payload)

    return upserts


# === Zoho Upsert ===
# Now uses shared upsert_to_zoho from modules.utils.zoho_api

# === Main ===
def main():
    token = get_access_token()

    # --- Freshness guard: is the upstream S3 Accounts export actually fresh? ---
    s3 = get_s3_client()
    s3_key = resolve_accounts_s3_key()
    freshness = check_accounts_file_freshness(s3, s3_key)
    if freshness["error"] is None:
        print(f"Accounts file {s3_key}: last written {freshness['last_modified'].isoformat()} "
              f"({freshness['age_hours']:.1f}h ago; threshold {freshness['max_age_hours']:.0f}h)")

    s3_df = download_s3_file_cached(s3, s3_key)
    max_created = None
    if 'DateTimeCreated' in s3_df.columns:
        max_created = pd.to_datetime(s3_df['DateTimeCreated'], errors='coerce').max()

    if freshness["stale"]:
        print("⚠ Accounts export is STALE — new accounts are not reaching Zoho. Alerting.")
        logger.warning("Accounts file stale (key=%s, age=%s, max_created=%s)",
                       s3_key, freshness["age_hours"], max_created)
        alert_stale_accounts_file(freshness, max_created=max_created)
        # Proceed with the (harmless) sync of the data we do have so existing
        # accounts stay current, then exit non-zero so the run is flagged.

    zoho_accounts = fetch_zoho_accounts(token)

    # Handle deleted accounts using the dedicated module. Pass the set of
    # Account_Names already in Zoho so we only attempt to delete accounts that
    # actually exist there (the deleted set accumulates ~1000 historically-
    # deleted accounts, almost all long gone from Zoho — re-searching them each
    # night wastes Zoho API credits).
    deleted_account_ids, deletion_results = process_deleted_accounts(
        s3_df, token, delete_from_zoho, existing_zoho_ids=set(zoho_accounts.keys())
    )

    # Remove deleted accounts from the dataframe so they won't be upserted
    if deleted_account_ids:
        s3_df = s3_df[~s3_df['Id'].astype(str).isin(deleted_account_ids)]

    # Now prepare upserts for non-deleted accounts
    upserts = prepare_upserts(s3_df, zoho_accounts)

    if not upserts:
        print("No updates needed.")
    else:
        print(f"\nPrepared {len(upserts)} updates...")
        # Use shared upsert function with debug=True and return_results=True
        result = upsert_to_zoho(token, upserts, debug=True, return_results=True)
        success = [r for r in result if r.get("status") == "success"]
        print(f"Successfully synced: {len(success)} of {len(upserts)}")

    # Exit non-zero on a stale source so the cron wrapper / monitoring flags it
    # loudly instead of the failure being silent.
    if freshness["stale"]:
        print("Exiting non-zero: Accounts export was stale this run.")
        sys.exit(1)

if __name__ == "__main__":
    main()
