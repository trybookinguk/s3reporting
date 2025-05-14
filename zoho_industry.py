import os
import boto3
import pandas as pd
from pandas.tseries.offsets import MonthBegin
import requests
from datetime import datetime

# === ENV VARS ===
AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_KEY"]
ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_DOMAIN = "https://www.zohoapis.com"

# === Zoho Auth ===
def get_access_token():
    resp = requests.post(
        "https://accounts.zoho.com/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        }
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

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

    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY
    )
    obj = s3.get_object(Bucket="produk-rdsextracts-438255373632", Key=s3_key)
    return pd.read_csv(obj["Body"])

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
def send_upserts(token, records):
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/upsert"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    results = []
    for i in range(0, len(records), 100):
        batch = records[i:i+100]
        payload = {
            "data": batch,
            "duplicate_check_fields": ["Account_Name"]
        }
        # DEBUG: Print first record of batch
        print("First record in batch:")
        print(batch[0])
        resp = requests.post(url, headers=headers, json=payload)
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            print(f"\nBatch error {i}–{i+len(batch)}:")
            print(f"Status {resp.status_code}: {resp.text}")
            continue

        for r in resp.json().get("data", []):
            if r.get("status") != "success":
                acct = r.get("details", {}).get("Account_Name", "UNKNOWN")
                msg = r.get("message", "No message")
                print(f"Failed record: {acct} → {msg}")
                print("Response content:")
                print(resp.text)
        results.extend(resp.json().get("data", []))
        
    return results

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
    result = send_upserts(token, upserts)
    success = [r for r in result if r.get("status") == "success"]
    print(f"Successfully synced: {len(success)} of {len(upserts)}")

if __name__ == "__main__":
    main()
