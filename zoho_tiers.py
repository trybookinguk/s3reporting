import os
import boto3
import pandas as pd
import requests
from datetime import datetime, timedelta
from pandas.tseries.offsets import MonthBegin

# === ENV VARS ===
AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_KEY"]
ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_DOMAIN = "https://www.zohoapis.com"
BUCKET = "produk-rdsextracts-438255373632"

# === DATE WINDOWS ===
TODAY = datetime.utcnow().date()
CUTOFF_365 = TODAY - timedelta(days=365)
CUTOFF_730 = CUTOFF_365 - timedelta(days=365)

# === AUTH ===
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

# === S3 FETCH ===
def fetch_s3_file(key):
    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return pd.read_csv(obj["Body"])

# === TIER LOGIC ===
def determine_tier(a, b, c, d, e):
    if a > 7000 or b > 10000 or (c > 8 and d > 27000 and e > 6000 and a >= 10):
        return "Key Account"
    elif a >= 3700 or b >= 5000 or (c >= 7 and d >= 13000 and e >= 3000 and a >= 10):
        return "High Value"
    elif a >= 800 or b >= 1000 or (c >= 5 and d >= 1600 and e >= 650 and a >= 10):
        return "Tier 4"
    elif a >= 150 or b >= 200 or (c >= 3 and d >= 220 and e >= 150 and a >= 10):
        return "Tier 3"
    elif a >= 45 or b >= 50 or (c >= 2 and d >= 50 and e >= 40 and a >= 10):
        return "Tier 2"
    elif a < 45 and 0 < b <= 49 and c == 1 and 0 < d <= 49 and 0 < e < 40:
        return "Tier 1"
    else:
        return "NIL"

# === METRICS CALC ===
def calculate_metrics(df):
    df["TransactionDate"] = pd.to_datetime(df["TransactionDate"])
    df["Revenue"] = df["BookingFee"] + df["CardFee"] + df["ProcessingFee"] + df["TicketFee"]
    df["Year"] = df["TransactionDate"].dt.year

    results = []
    for account_id, group in df.groupby("AccountId"):
        group = group.sort_values("TransactionDate")

        # Define windows
        current_period = group[group["TransactionDate"] >= CUTOFF_365]
        previous_period = group[
            (group["TransactionDate"] >= CUTOFF_730) &
            (group["TransactionDate"] < CUTOFF_365)
        ]
        lifetime = group
        lifetime_pre_cutoff = group[group["TransactionDate"] < CUTOFF_365]

        # Shared metrics
        years_loyalty = lifetime["Year"].nunique()
        lifetime_revenue = lifetime["Revenue"].sum()
        avg_revenue_per_year = lifetime_revenue / years_loyalty if years_loyalty else 0

        years_loyalty_prev = lifetime_pre_cutoff["Year"].nunique()
        revenue_prev = lifetime_pre_cutoff["Revenue"].sum()
        avg_rev_prev = revenue_prev / years_loyalty_prev if years_loyalty_prev else 0

        # Current Tier
        tickets_current = current_period["TicketQuantity"].sum()
        revenue_current = current_period["Revenue"].sum()
        tier_current = determine_tier(tickets_current, revenue_current, years_loyalty, lifetime_revenue, avg_revenue_per_year)

        # Previous Tier
        tickets_prev = previous_period["TicketQuantity"].sum()
        revenue_window_prev = previous_period["Revenue"].sum()
        tier_prev = determine_tier(tickets_prev, revenue_window_prev, years_loyalty_prev, revenue_prev, avg_rev_prev)

        results.append({
            "Account_Name": account_id,
            "Current_Tier": tier_current,
            "Previous_Tier": tier_prev,
            "Ticket_Quantity": int(tickets_current),
            "Last_Year_Ticket_Quantity": int(tickets_prev),
            "Years_Loyalty": years_loyalty
        })

    return pd.DataFrame(results)

# === ZOHO UPSERT ===
def upsert_to_zoho(token, records_df):
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/upsert"
    for i in range(0, len(records_df), 100):
        batch = records_df.iloc[i:i+100]
        payload = {
            "data": batch.to_dict(orient="records"),
            "duplicate_check_fields": ["Account_Name"]
        }
        resp = requests.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            print(f"Batch {i} failed: {resp.status_code} - {resp.text}")
        else:
            print(f"Batch {i} success")

# === MAIN ===
def main():
    from pandas.tseries.offsets import MonthBegin

    report_date = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=1)
    if report_date.day == 1:
        report_date -= MonthBegin(1)

    prefix = report_date.strftime("%Y%m")
    year = report_date.strftime("%Y")
    month = report_date.strftime("%m")

    key_all = f"{year}/{month}/{prefix}-BookingDataAll.csv"
    key_month = f"{year}/{month}/{prefix}-BookingData.csv"

    df_all = fetch_s3_file(key_all)
    df_month = fetch_s3_file(key_month)
    df = pd.concat([df_all, df_month], ignore_index=True)
    df = df.drop_duplicates(subset="BookingTransactionID")

    token = get_access_token()
    updates = calculate_metrics(df)

    if not updates.empty:
        upsert_to_zoho(token, updates)
    else:
        print("No updates required.")


if __name__ == "__main__":
    main()
