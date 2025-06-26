import os
import pandas as pd
from pandas.tseries.offsets import MonthBegin
import requests
from datetime import datetime

# Import shared modules
from modules.utils.config import S3_BUCKET, ZOHO_DOMAIN
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached
from modules.utils.zoho_api import get_access_token, upsert_to_zoho

# === S3 Download ===
def fetch_s3_report():
    report_date = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=1)
    
    # Fallback if first day of month: use previous month
    if report_date.day == 1:
        report_date -= MonthBegin(1)
    
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")
    prefix = report_date.strftime("%Y%m")
    filename = f"{prefix}-Accounts-TBUK.csv"
    s3_key = f"{year}/{month}/{filename}"

    # Use shared S3 client with caching
    s3 = get_s3_client()
    return download_s3_file_cached(s3, s3_key)

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
    s3_df = fetch_s3_report()
    zoho_accounts = fetch_zoho_accounts(token)
    upserts = prepare_upserts(s3_df, zoho_accounts)

    if not upserts:
        print("No updates needed.")
        return

    print(f"Prepared {len(upserts)} updates...")
    # Use shared upsert function with debug=True and return_results=True
    result = upsert_to_zoho(token, upserts, debug=True, return_results=True)
    success = [r for r in result if r.get("status") == "success"]
    print(f"Successfully synced: {len(success)} of {len(upserts)}")

if __name__ == "__main__":
    main()
