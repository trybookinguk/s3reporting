#!/usr/bin/env python3
"""
Simple test script for the Vero API module.
"""
import os
import sys

# Add the project root to the path
sys.path.insert(0, '/home/alexashley/s3reporting')

# Set test environment variables
os.environ['TEST_MODE'] = '1'
os.environ['VERO_AUTH_TOKEN'] = 'test_token'

try:
    from modules.utils.vero_api import VeroClient, track_events, track_event
    
    print("✓ Successfully imported Vero API module")
    print("✓ VeroClient class available")
    print("✓ track_events function available") 
    print("✓ track_event function available")
    
    # Test basic functionality in test mode
    print("\n--- Testing basic functionality (TEST_MODE=1) ---")
    
    # Test single event
    result = track_event(
        user_id="test_user_123",
        email="test@example.com",
        event_name="Test Event",
        data={"property1": "value1", "property2": 42}
    )
    print(f"Single event tracking result: {result}")
    
    # Test batch events
    test_events = [
        {
            "id": "user_1",
            "email": "user1@example.com",
            "event_name": "Event A",
            "data": {"value": 100}
        },
        {
            "id": "user_2", 
            "email": "user2@example.com",
            "event_name": "Event B",
            "data": {"value": 200}
        }
    ]
    
    batch_result = track_events(test_events, debug=True)
    print(f"Batch tracking result: {batch_result}")
    
    print("\n✓ All tests passed!")
    
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Unexpected error: {e}")
    sys.exit(1)