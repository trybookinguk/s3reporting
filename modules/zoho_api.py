"""
Zoho CRM API integration for TryBooking tier updates.
"""
import requests
from .config import ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_DOMAIN, TEST_MODE


def get_access_token():
    """Get OAuth access token using refresh token."""
    # Check credentials when actually needed
    if not all([ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN]):
        raise ValueError("Zoho credentials not found in environment variables")
    
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


def upsert_to_zoho(token, records_df):
    """
    Upsert account records to Zoho CRM.
    
    Args:
        token: OAuth access token
        records_df: DataFrame with account updates
    """
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/upsert"
    
    # Check test mode
    if TEST_MODE:
        print("TEST MODE: Would update the following accounts:")
        # Show all the fields including new ones
        display_cols = ['Account_Name', 'Current_Tier', 'Previous_Tier', 'Event_Frequency_Current', 
                       'Event_Frequency_Previous', 'Rating', 'Ticket_Quantity']
        print(records_df[display_cols].head(10))
        print(f"\nTotal accounts to update: {len(records_df)}")
        print(f"Columns being sent to Zoho: {list(records_df.columns)}")
        return
    
    # Process in batches of 100 (Zoho max)
    batch_size = 100
    for i in range(0, len(records_df), batch_size):
        batch = records_df.iloc[i:i+batch_size]
        
        # Ensure all Account_Name values are strings
        records = batch.to_dict(orient="records")
        for record in records:
            record['Account_Name'] = str(record['Account_Name'])
        
        payload = {
            "data": records,
            "duplicate_check_fields": ["Account_Name"]
        }
        
        try:
            resp = requests.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                print(f"Batch {i//batch_size + 1} failed: {resp.status_code} - {resp.text}")
            else:
                print(f"Batch {i//batch_size + 1} success ({len(batch)} records)")
        except Exception as e:
            print(f"Batch {i//batch_size + 1} error: {str(e)}")