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


def upsert_to_zoho(token, records, debug=False, return_results=False):
    """
    Upsert account records to Zoho CRM.
    
    Args:
        token: OAuth access token
        records: DataFrame or list of dicts with account updates
        debug: If True, print debug information
        return_results: If True, return detailed results from Zoho API
    
    Returns:
        None by default, or list of API responses if return_results=True
    """
    import pandas as pd
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    url = f"{ZOHO_DOMAIN}/crm/v2/Accounts/upsert"
    
    # Convert DataFrame to list of dicts if needed
    if isinstance(records, pd.DataFrame):
        # Check test mode for DataFrame input
        if TEST_MODE:
            print("TEST MODE: Would update the following accounts:")
            display_cols = [col for col in ['Account_Name', 'Current_Tier', 'Previous_Tier', 
                          'Event_Frequency_Current', 'Event_Frequency_Previous', 'Rating', 
                          'Ticket_Quantity'] if col in records.columns]
            if display_cols:
                print(records[display_cols].head(10))
            print(f"\nTotal accounts to update: {len(records)}")
            print(f"Columns being sent to Zoho: {list(records.columns)}")
            return [] if return_results else None
        
        records_list = records.to_dict(orient="records")
        # Ensure all Account_Name values are strings
        for record in records_list:
            record['Account_Name'] = str(record['Account_Name'])
    else:
        # Already a list
        if TEST_MODE:
            print("TEST MODE: Would update the following accounts:")
            print(f"Total accounts to update: {len(records)}")
            if records:
                print(f"First record: {records[0]}")
            return [] if return_results else None
        records_list = records
    
    # Process in batches of 100 (Zoho max)
    batch_size = 100
    all_results = []
    
    for i in range(0, len(records_list), batch_size):
        batch = records_list[i:i+batch_size]
        
        payload = {
            "data": batch,
            "duplicate_check_fields": ["Account_Name"]
        }
        
        # Debug: Print first record of batch
        if debug and batch:
            print(f"\nBatch {i//batch_size + 1} - First record:")
            for key, value in batch[0].items():
                print(f"  {key}: {value} (type: {type(value).__name__})")
        
        try:
            resp = requests.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            
            # Process response
            response_data = resp.json()
            batch_results = response_data.get("data", [])
            
            # Check for failures and print them
            failed_count = 0
            for r in batch_results:
                if r.get("status") != "success":
                    failed_count += 1
                    acct = r.get("details", {}).get("Account_Name", "UNKNOWN")
                    msg = r.get("message", "No message")
                    print(f"Failed record: {acct} → {msg}")
            
            success_count = len(batch) - failed_count
            print(f"Batch {i//batch_size + 1} success ({success_count}/{len(batch)} records)")
            
            if return_results:
                all_results.extend(batch_results)
                
        except requests.HTTPError:
            print(f"\nBatch error {i}–{i+len(batch)}:")
            print(f"Status {resp.status_code}: {resp.text}")
            if not return_results:
                continue
            # For return_results mode, add error entries
            all_results.extend([{"status": "error", "message": f"HTTP {resp.status_code}"} 
                              for _ in batch])
        except Exception as e:
            print(f"Batch {i//batch_size + 1} error: {str(e)}")
            if return_results:
                all_results.extend([{"status": "error", "message": str(e)} 
                                  for _ in batch])
    
    return all_results if return_results else None