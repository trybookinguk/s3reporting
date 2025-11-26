"""
Vero API integration for TryBooking event completion reminders.
"""
import json
import requests
import time
import pandas as pd
from typing import List, Dict, Optional, Union
from .config import TEST_MODE

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


class VeroClient:
    """Client for interacting with Vero API."""
    
    def __init__(self, auth_token: str):
        """
        Initialize Vero client.
        
        Args:
            auth_token: Vero API authentication token
        """
        self.auth_token = auth_token
        self.base_url = "https://api.getvero.com"
        self.session = get_session()
    
    def track_event(self, user_id: str, email: str, event_name: str, data: Dict, extras: Optional[Dict] = None) -> Dict:
        """
        Track a single event in Vero.
        
        Args:
            user_id: Unique user identifier
            email: User email address
            event_name: Name of the event to track
            data: Event properties/data
            extras: Optional dict containing 'source' and/or 'created_at'
            
        Returns:
            API response
        """
        @retry_with_backoff
        def _make_request():
            # Auth token goes in query parameter
            url = f"{self.base_url}/api/v2/events/track?auth_token={self.auth_token}"
            
            # Request body contains identity, event_name, and data
            payload = {
                "identity": {
                    "id": user_id,
                    "email": email
                },
                "event_name": event_name,
                "data": data
            }
            
            # Add extras if provided
            if extras:
                payload["extras"] = extras
            
            response = self.session.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
            )
            
            # Log request details on error
            if response.status_code >= 400:
                print(f"    API Error {response.status_code}: {response.text}")
                print(f"    Request payload: {json.dumps(payload, indent=2)}")
            
            response.raise_for_status()
            return response.json()
        
        return _make_request()
    
    def batch_track_events(self, events_df) -> List[Dict]:
        """
        Track multiple events in batches.
        
        Args:
            events_df: DataFrame containing event data with columns:
                - vero_user_id: User identifier
                - user_email: User email
                - vero_event: Event name
                - Other columns will be included as event data
                
        Returns:
            List of results for each event
        """
        results = []
        
        # Process each event
        for idx, event in events_df.iterrows():
            try:
                # Extract core fields
                user_id = event['vero_user_id']
                email = event['user_email']
                event_name = event['vero_event']
                
                # Build event data from remaining columns
                # Note: Financial amounts removed as they're not displayed in emails
                data_fields = [
                    'event_id', 'event_name', 'account_id', 'event_type',
                    'ticket_quantity'
                ]
                event_data = {}
                for field in data_fields:
                    if field in event and pd.notna(event[field]):
                        event_data[field] = event[field]

                # Add context fields that should be available in email templates
                event_data['event_name_tb'] = event.get('event_name', '')  # The TryBooking event name
                event_data['isMultiple'] = event.get('has_multiple_events', False)  # True if account has multiple events completing

                # Add extras to identify source
                extras = {
                    'source': 'TryBooking Event Completion Script',
                    'testmode': TEST_MODE  # True if running in test mode
                }
                
                # Track the event (testmode flag in extras allows filtering in Vero)
                result = self.track_event(user_id, email, event_name, event_data, extras)
                result['status'] = 'success'
                
                results.append(result)
                
            except Exception as e:
                # Log error but continue processing other events
                error_result = {
                    'status': 'error',
                    'error': str(e),
                    'user_id': event.get('vero_user_id', 'unknown'),
                    'event_name': event.get('vero_event', 'unknown')
                }
                results.append(error_result)
                print(f"Error tracking event: {e}")

        return results

    def add_tags(self, user_id: str, tags: List[str]) -> Dict:
        """
        Add tags to a user in Vero.

        Args:
            user_id: Unique user identifier (e.g., 'uk_12345')
            tags: List of tags to add

        Returns:
            API response
        """
        @retry_with_backoff
        def _make_request():
            url = f"{self.base_url}/api/v2/users/tags/edit?auth_token={self.auth_token}"

            payload = {
                "id": user_id,
                "add": tags
            }

            response = self.session.put(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
            )

            if response.status_code >= 400:
                print(f"    API Error {response.status_code}: {response.text}")
                print(f"    Request payload: {json.dumps(payload, indent=2)}")

            response.raise_for_status()
            return response.json()

        return _make_request()

    def batch_add_tags(self, users_df) -> List[Dict]:
        """
        Add tags to multiple users in batches.

        Args:
            users_df: DataFrame containing user data with columns:
                - vero_user_id: User identifier
                - vero_tag: Tag to add

        Returns:
            List of results for each user
        """
        results = []

        for _, user in users_df.iterrows():
            try:
                user_id = user['vero_user_id']
                tag = user['vero_tag']

                if TEST_MODE:
                    # In test mode, don't actually call API
                    result = {
                        'status': 'success',
                        'test_mode': True,
                        'user_id': user_id,
                        'tag': tag
                    }
                else:
                    result = self.add_tags(user_id, [tag])
                    result['status'] = 'success'

                results.append(result)

            except Exception as e:
                error_result = {
                    'status': 'error',
                    'error': str(e),
                    'user_id': user.get('vero_user_id', 'unknown'),
                    'tag': user.get('vero_tag', 'unknown')
                }
                results.append(error_result)
                print(f"Error adding tag: {e}")

        return results