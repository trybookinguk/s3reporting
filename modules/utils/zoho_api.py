"""
Zoho CRM API integration for TryBooking tier updates.
"""
import requests
import time
from typing import List, Dict, Optional, Union, Tuple
from .config import ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_DOMAIN, TEST_MODE

# Module-level session for connection reuse
_session = None

def get_session() -> requests.Session:
    """Get or create a session with connection pooling."""
    global _session
    if _session is None:
        _session = requests.Session()
        # Configure connection pooling
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0  # We handle retries manually
        )
        _session.mount('http://', adapter)
        _session.mount('https://', adapter)
    return _session


def retry_with_backoff(func, max_retries: int = 3, backoff_base: float = 1.0):
    """
    Decorator function to retry with exponential backoff.
    
    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        backoff_base: Base time in seconds for exponential backoff
    """
    def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except requests.exceptions.RequestException as e:
                last_exception = e
                if attempt < max_retries:
                    # Check if it's a rate limit error
                    if hasattr(e, 'response') and e.response is not None:
                        if e.response.status_code == 429:
                            # For rate limits, use longer backoff
                            wait_time = backoff_base * (2 ** attempt) * 2
                            print(f"Rate limit hit, waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                        else:
                            wait_time = backoff_base * (2 ** attempt)
                            print(f"Request failed ({e}), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                    else:
                        wait_time = backoff_base * (2 ** attempt)
                        print(f"Request failed ({e}), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                    
                    time.sleep(wait_time)
                else:
                    raise last_exception
        
        raise last_exception
    return wrapper


def get_access_token():
    """Get OAuth access token using refresh token."""
    # Check credentials when actually needed
    if not all([ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN]):
        raise ValueError("Zoho credentials not found in environment variables")
    
    session = get_session()
    
    @retry_with_backoff
    def _make_request():
        resp = session.post(
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
    
    return _make_request()


def _clean_record_for_json(record: Dict) -> Dict:
    """
    Clean a record to ensure all values are JSON-serializable.
    Replaces NaN, infinity values with None.
    """
    import math
    import numpy as np
    
    cleaned = {}
    for key, value in record.items():
        # Handle pandas/numpy numeric types
        if isinstance(value, (np.integer, np.floating)):
            if np.isnan(value) or np.isinf(value):
                cleaned[key] = None
            else:
                cleaned[key] = float(value)
        # Handle regular Python floats
        elif isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                cleaned[key] = None
            else:
                cleaned[key] = value
        # Convert any other numpy types to Python types
        elif hasattr(value, 'item'):
            # This handles numpy scalars
            try:
                python_value = value.item()
                if isinstance(python_value, float) and (math.isnan(python_value) or math.isinf(python_value)):
                    cleaned[key] = None
                else:
                    cleaned[key] = python_value
            except:
                cleaned[key] = str(value)
        else:
            cleaned[key] = value
    
    return cleaned


def _process_batch_with_retry(session: requests.Session, url: str, headers: Dict, 
                            batch: List[Dict], batch_num: int, max_retries: int = 3) -> Tuple[List[Dict], List[Dict]]:
    """
    Process a single batch with retry logic.
    
    Returns:
        Tuple of (successful_records, failed_records)
    """
    # Clean all records in the batch to ensure JSON compatibility
    cleaned_batch = [_clean_record_for_json(record) for record in batch]
    
    payload = {
        "data": cleaned_batch,
        "duplicate_check_fields": ["Account_Name"]
    }
    
    successful_records = []
    failed_records = []
    
    for attempt in range(max_retries + 1):
        try:
            resp = session.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            
            # Process response
            response_data = resp.json()
            batch_results = response_data.get("data", [])
            
            # Separate successful and failed records
            for idx, result in enumerate(batch_results):
                record = cleaned_batch[idx] if idx < len(cleaned_batch) else {}
                if result.get("status") == "success":
                    successful_records.append({
                        "record": record,
                        "result": result
                    })
                else:
                    acct_name = record.get("Account_Name", "UNKNOWN")
                    msg = result.get("message", "No message")
                    print(f"Failed record: {acct_name} → {msg}")
                    failed_records.append({
                        "record": record,
                        "error": msg,
                        "result": result
                    })
            
            # Success - break out of retry loop
            success_count = len(successful_records)
            print(f"Batch {batch_num} completed ({success_count}/{len(batch)} records successful)")
            break
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                # Determine wait time based on error type
                if hasattr(e, 'response') and e.response is not None:
                    if e.response.status_code == 429:
                        wait_time = (2 ** attempt) * 2  # Longer wait for rate limits
                        print(f"Batch {batch_num}: Rate limit hit, waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    else:
                        wait_time = (2 ** attempt)
                        print(f"Batch {batch_num}: HTTP {e.response.status_code} error, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                else:
                    wait_time = (2 ** attempt)
                    print(f"Batch {batch_num}: Request error ({e}), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                
                time.sleep(wait_time)
            else:
                # Final attempt failed
                error_msg = f"HTTP {e.response.status_code}" if hasattr(e, 'response') and e.response else str(e)
                print(f"Batch {batch_num}: Failed after {max_retries} retries: {error_msg}")
                
                # Mark all records in batch as failed
                for record in cleaned_batch:
                    failed_records.append({
                        "record": record,
                        "error": f"Failed after retries: {error_msg}",
                        "result": {"status": "error", "message": error_msg}
                    })
    
    return successful_records, failed_records


def upsert_to_zoho(token, records, debug=False, return_results=False):
    """
    Upsert account records to Zoho CRM with retry logic and better error handling.
    
    Args:
        token: OAuth access token
        records: DataFrame or list of dicts with account updates
        debug: If True, print debug information
        return_results: If True, return detailed results from Zoho API
    
    Returns:
        None by default, or dict with detailed results if return_results=True:
        {
            "total": int,
            "successful": int,
            "failed": int,
            "results": list,  # Original format for backward compatibility
            "successful_records": list,
            "failed_records": list
        }
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
            if return_results:
                return {
                    "total": len(records),
                    "successful": 0,
                    "failed": 0,
                    "results": [],
                    "successful_records": [],
                    "failed_records": []
                }
            return None
        
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
            if return_results:
                return {
                    "total": len(records),
                    "successful": 0,
                    "failed": 0,
                    "results": [],
                    "successful_records": [],
                    "failed_records": []
                }
            return None
        records_list = records
    
    # Get session for connection reuse
    session = get_session()
    
    # Process in batches of 100 (Zoho max)
    batch_size = 100
    all_results = []  # For backward compatibility
    all_successful = []
    all_failed = []
    
    print(f"\nStarting Zoho upsert for {len(records_list)} records...")
    
    for i in range(0, len(records_list), batch_size):
        batch = records_list[i:i+batch_size]
        batch_num = i//batch_size + 1
        
        successful, failed = _process_batch_with_retry(
            session, url, headers, batch, batch_num
        )
        
        all_successful.extend(successful)
        all_failed.extend(failed)
        
        # For backward compatibility, maintain the original results format
        if return_results:
            for record in successful:
                all_results.append(record["result"])
            for record in failed:
                all_results.append(record["result"])
    
    # Print summary
    total_successful = len(all_successful)
    total_failed = len(all_failed)
    print(f"\n=== Zoho Update Summary ===")
    print(f"Total records: {len(records_list)}")
    print(f"Successful: {total_successful}")
    print(f"Failed: {total_failed}")
    
    if total_failed > 0 and debug:
        print("\nFailed records summary:")
        for failed in all_failed[:10]:  # Show first 10 failures
            acct = failed["record"].get("Account_Name", "UNKNOWN")
            error = failed["error"]
            print(f"  - {acct}: {error}")
        if total_failed > 10:
            print(f"  ... and {total_failed - 10} more failures")
    
    if return_results:
        # For backward compatibility, return list if that's what the caller expects
        # The zoho_industry.py file expects a list and checks r.get("status")
        return all_results
    
    return None


def upsert_to_zoho_with_details(token, records, debug=False):
    """
    Enhanced version of upsert_to_zoho that always returns detailed results.
    
    This is the recommended function for new code that needs detailed error tracking.
    
    Args:
        token: OAuth access token
        records: DataFrame or list of dicts with account updates
        debug: If True, print debug information
    
    Returns:
        Dict with detailed results:
        {
            "total": int,
            "successful": int,
            "failed": int,
            "successful_records": list of {"record": original_data, "result": api_response},
            "failed_records": list of {"record": original_data, "error": error_msg, "result": api_response}
        }
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
            return {
                "total": len(records),
                "successful": 0,
                "failed": 0,
                "successful_records": [],
                "failed_records": []
            }
        
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
            return {
                "total": len(records),
                "successful": 0,
                "failed": 0,
                "successful_records": [],
                "failed_records": []
            }
        records_list = records
    
    # Get session for connection reuse
    session = get_session()
    
    # Process in batches of 100 (Zoho max)
    batch_size = 100
    all_successful = []
    all_failed = []
    
    print(f"\nStarting Zoho upsert for {len(records_list)} records...")
    
    for i in range(0, len(records_list), batch_size):
        batch = records_list[i:i+batch_size]
        batch_num = i//batch_size + 1
        
        successful, failed = _process_batch_with_retry(
            session, url, headers, batch, batch_num
        )
        
        all_successful.extend(successful)
        all_failed.extend(failed)
    
    # Print summary
    total_successful = len(all_successful)
    total_failed = len(all_failed)
    print(f"\n=== Zoho Update Summary ===")
    print(f"Total records: {len(records_list)}")
    print(f"Successful: {total_successful}")
    print(f"Failed: {total_failed}")
    
    if total_failed > 0 and debug:
        print("\nFailed records summary:")
        for failed in all_failed[:10]:  # Show first 10 failures
            acct = failed["record"].get("Account_Name", "UNKNOWN")
            error = failed["error"]
            print(f"  - {acct}: {error}")
        if total_failed > 10:
            print(f"  ... and {total_failed - 10} more failures")
    
    return {
        "total": len(records_list),
        "successful": total_successful,
        "failed": total_failed,
        "successful_records": all_successful,
        "failed_records": all_failed
    }


def delete_from_zoho(token: str, account_ids: List[str]) -> Dict[str, Union[int, List]]:
    """
    Delete accounts from Zoho CRM.
    
    Args:
        token: Zoho access token
        account_ids: List of Account IDs to delete
        
    Returns:
        Dictionary with deletion results
    """
    if not account_ids:
        return {
            "total": 0,
            "successful": 0,
            "failed": 0,
            "results": []
        }
    
    # Check test mode
    if TEST_MODE:
        print(f"TEST MODE: Would delete {len(account_ids)} accounts from Zoho:")
        print(f"Account IDs: {account_ids[:5]}..." if len(account_ids) > 5 else f"Account IDs: {account_ids}")
        return {
            "total": len(account_ids),
            "successful": 0,
            "failed": 0,
            "results": []
        }
    
    url = f"{ZOHO_DOMAIN}/crm/v5/Accounts"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    
    # Get session for connection reuse
    session = get_session()
    
    # Zoho allows deleting up to 100 records at once
    batch_size = 100
    all_successful = []
    all_failed = []
    
    print(f"\nDeleting {len(account_ids)} accounts from Zoho...")
    
    for i in range(0, len(account_ids), batch_size):
        batch_ids = account_ids[i:i+batch_size]
        batch_num = i//batch_size + 1
        
        # Zoho delete API expects comma-separated IDs in the URL
        ids_param = ",".join(batch_ids)
        delete_url = f"{url}?ids={ids_param}"
        
        try:
            response = retry_with_backoff(session.delete)(delete_url, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            if result.get("data"):
                for item in result["data"]:
                    if item.get("status") == "success":
                        all_successful.append(item.get("details", {}).get("id"))
                    else:
                        all_failed.append({
                            "id": item.get("details", {}).get("id"),
                            "error": item.get("message", "Unknown error")
                        })
            
            print(f"  Batch {batch_num}: Deleted {len([item for item in result.get('data', []) if item.get('status') == 'success'])} accounts")
            
        except requests.exceptions.RequestException as e:
            print(f"  Batch {batch_num}: Failed to delete - {str(e)}")
            # Add all IDs in this batch as failed
            for account_id in batch_ids:
                all_failed.append({
                    "id": account_id,
                    "error": str(e)
                })
        
        # Rate limiting
        if i + batch_size < len(account_ids):
            time.sleep(0.5)
    
    # Print summary
    total_successful = len(all_successful)
    total_failed = len(all_failed)
    print(f"\n=== Zoho Delete Summary ===")
    print(f"Total accounts: {len(account_ids)}")
    print(f"Successfully deleted: {total_successful}")
    print(f"Failed to delete: {total_failed}")
    
    if total_failed > 0:
        print("\nFailed deletions:")
        for failed in all_failed[:5]:  # Show first 5 failures
            print(f"  - ID {failed['id']}: {failed['error']}")
        if total_failed > 5:
            print(f"  ... and {total_failed - 5} more failures")
    
    return {
        "total": len(account_ids),
        "successful": total_successful,
        "failed": total_failed,
        "results": {"successful": all_successful, "failed": all_failed}
    }