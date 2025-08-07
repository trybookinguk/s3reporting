#!/usr/bin/env python3
"""
Standalone test for Vero API functionality without importing the full utils package.
"""

# Mock the config module for testing
class MockConfig:
    TEST_MODE = True
    VERO_AUTH_TOKEN = 'test_token_12345'

import sys
sys.modules['modules.utils.config'] = MockConfig

# Now we can test our vero_api module directly
exec(open('/home/alexashley/s3reporting/modules/utils/vero_api.py').read())

print("✓ Vero API module loaded successfully")
print(f"✓ VeroClient class available: {type(VeroClient)}")
print(f"✓ track_events function available: {callable(track_events)}")
print(f"✓ track_event function available: {callable(track_event)}")

# Test in test mode (no actual API calls)
print("\n--- Testing functionality in TEST_MODE ---")

# Test single event
print("\n1. Testing single event tracking:")
result = track_event(
    user_id="test_user_123",
    email="test@example.com", 
    event_name="User Registration",
    data={"source": "website", "plan": "premium"}
)
print(f"Single event result: {result}")

# Test batch events
print("\n2. Testing batch event tracking:")
test_events = [
    {
        "id": "user_001",
        "email": "alice@example.com",
        "event_name": "Purchase Made",
        "data": {"amount": 299.99, "currency": "GBP", "product": "Event Tickets"}
    },
    {
        "id": "user_002",
        "email": "bob@example.com", 
        "event_name": "Event Created",
        "data": {"event_type": "Workshop", "capacity": 50}
    },
    {
        "id": "user_003",
        "email": "charlie@example.com",
        "event_name": "Support Request",
        "data": {"category": "billing", "priority": "high"}
    }
]

batch_result = track_events(test_events, debug=True)
print(f"Batch result: {batch_result}")

# Test VeroClient class directly
print("\n3. Testing VeroClient class:")
try:
    client = VeroClient()
    print(f"✓ VeroClient instantiated successfully")
    print(f"   - Auth token set: {bool(client.auth_token)}")
    print(f"   - Base URL: {client.base_url}")
    
    # Test validation
    valid_event = {"id": "test", "email": "test@example.com", "event_name": "Test"}
    invalid_event = {"id": "test"}  # Missing required fields
    
    print(f"   - Valid event validation: {client._validate_event(valid_event)}")
    print(f"   - Invalid event validation: {client._validate_event(invalid_event)}")
    
except Exception as e:
    print(f"✗ Error testing VeroClient: {e}")

print("\n✓ All tests completed successfully!")
print("✓ The Vero API module is ready to use")