import os
import boto3
import pandas as pd
import requests
from datetime import datetime

# === ENV VARS ===
AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_DOMAIN = os.environ.get("ZOHO_DOMAIN", "https://www.zohoapis.com")

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
    today = datetime.utcnow()
    year = today.strftime("%Y")
    month = today.strftime("%m")
    prefix = today.strftime("%Y%m")
    filename = f"{prefix}-Accounts-TBUK.csv"
    key = f"{year}/{month}/{filename}"

    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY
    )
    obj = s3.get_object(Bucket="produk-rdsextracts-438255373632", Key=key)
    return pd.read_csv(obj["Body"])

# === Zoho CRM Fetch ===
def fetch_zoho_accounts(token):
    all_accounts = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        params = {"page": page, "per_page": 200}
        resp = requests.get(f"{ZOHO_DOMAIN}/crm/v2/Accounts", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break
        for acc in data:
            name = acc.get("Account_Name")
            if name:
                all_accounts[name] = acc
        if not resp.json().get("info", {}).get("more_records"):
            break
        page += 1
    return all_accounts

# === Compare + Prepare Upserts ===
def prepare_upserts(s3_df, zoho_map):
    upserts = []
    for _, row in s3_df.iterrows():
        account_id = str(row.get("AccountID")).strip()
        industry = str(row.get("Industry", "")).strip()
        subindustry = str(row.get("SubIndustry", "")).strip()
        existing = zoho_map.get(account_id)

        payload = {"Account_Name": account_id}
        if not existing:
            if industry:
                payload["Industry"] = industry
            if subindustry:
                payload["SubIndustry"] = subindustry
            upserts.append(payload)
        else:
            changes = {}
            if industry and industry != existing.get("Industry", ""):
                changes["Industry"] = industry
            if subindustry and subindustry != existing.get("SubIndustry", ""):
                changes["SubIndustry"] = subindustry
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
        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()
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
