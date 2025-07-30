#!/usr/bin/env python3
"""
Test script to verify PPC reporting setup.

This script checks that all required components are properly configured:
- Environment variables
- Google Analytics API access
- S3 access
- Campaign configuration
"""

import os
import sys
import json
from datetime import datetime, timedelta

# Test environment variables
print("="*60)
print("CHECKING ENVIRONMENT SETUP")
print("="*60)

# Check AWS credentials
aws_vars = {
    'AWS_ACCESS_KEY_ID': os.environ.get('AWS_ACCESS_KEY_ID'),
    'AWS_SECRET_ACCESS_KEY': os.environ.get('AWS_SECRET_ACCESS_KEY')
}

print("\n1. AWS Credentials:")
for var, value in aws_vars.items():
    if value:
        print(f"   ✓ {var} is set ({len(value)} characters)")
    else:
        print(f"   ✗ {var} is NOT set")

# Check Google Analytics credentials
ga_vars = {
    'GOOGLE_APPLICATION_CREDENTIALS': os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'),
    'GA4_PROPERTY_ID': os.environ.get('GA4_PROPERTY_ID')
}

print("\n2. Google Analytics Configuration:")
for var, value in ga_vars.items():
    if value:
        if var == 'GOOGLE_APPLICATION_CREDENTIALS':
            if os.path.exists(value):
                print(f"   ✓ {var} points to existing file: {value}")
                # Try to load and validate JSON
                try:
                    with open(value, 'r') as f:
                        creds = json.load(f)
                        if 'client_email' in creds:
                            print(f"     Service account: {creds['client_email']}")
                except Exception as e:
                    print(f"     Warning: Could not parse credentials file: {e}")
            else:
                print(f"   ✗ {var} file not found: {value}")
        else:
            print(f"   ✓ {var} is set: {value}")
    else:
        print(f"   ✗ {var} is NOT set")

# Check campaign configuration
print("\n3. Campaign Configuration:")
config_path = os.path.join(os.path.dirname(__file__), 'config', 'ppc_campaigns.json')
if os.path.exists(config_path):
    print(f"   ✓ Campaign config found: {config_path}")
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            active_campaigns = [c for c in config['campaigns'] if c.get('active', True)]
            print(f"     Active campaigns: {len(active_campaigns)}")
            for campaign in active_campaigns[:5]:  # Show first 5
                print(f"       - {campaign['campaign_name']}")
            if len(active_campaigns) > 5:
                print(f"       ... and {len(active_campaigns) - 5} more")
    except Exception as e:
        print(f"   ✗ Error loading campaign config: {e}")
else:
    print(f"   ✗ Campaign config not found at: {config_path}")

# Test imports
print("\n4. Python Dependencies:")
dependencies = [
    ('pandas', 'Core data processing'),
    ('boto3', 'AWS S3 access'),
    ('google.analytics.data_v1beta', 'Google Analytics API'),
    ('google.oauth2', 'Google authentication'),
    ('pytz', 'Timezone handling')
]

for module_name, description in dependencies:
    try:
        __import__(module_name)
        print(f"   ✓ {module_name} ({description})")
    except ImportError:
        print(f"   ✗ {module_name} NOT installed ({description})")

# Test S3 access
print("\n5. S3 Connectivity Test:")
if all(aws_vars.values()):
    try:
        from modules.utils.s3_data_loader import get_s3_client
        s3_client = get_s3_client()
        # Try to list a known file
        response = s3_client.head_object(
            Bucket="produk-rdsextracts-438255373632",
            Key=f"{datetime.now().year}/Accounts-TBUK.csv"
        )
        print("   ✓ S3 connection successful")
        print(f"     Accounts file size: {response['ContentLength'] / 1024 / 1024:.1f} MB")
    except Exception as e:
        print(f"   ✗ S3 connection failed: {e}")
else:
    print("   ⚠ Skipping S3 test (AWS credentials not set)")

# Test GA4 API access
print("\n6. Google Analytics API Test:")
if ga_vars['GOOGLE_APPLICATION_CREDENTIALS'] and os.path.exists(ga_vars['GOOGLE_APPLICATION_CREDENTIALS']):
    if ga_vars['GA4_PROPERTY_ID']:
        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient
            from google.oauth2 import service_account
            
            credentials = service_account.Credentials.from_service_account_file(
                ga_vars['GOOGLE_APPLICATION_CREDENTIALS'],
                scopes=['https://www.googleapis.com/auth/analytics.readonly']
            )
            client = BetaAnalyticsDataClient(credentials=credentials)
            print("   ✓ GA4 client initialized successfully")
            print(f"     Property ID: {ga_vars['GA4_PROPERTY_ID']}")
            print("     Note: Actual API calls require proper permissions in GA4")
        except Exception as e:
            print(f"   ✗ GA4 client initialization failed: {e}")
    else:
        print("   ⚠ GA4_PROPERTY_ID not set - needed for API calls")
else:
    print("   ⚠ Skipping GA4 test (credentials not configured)")

# Summary
print("\n" + "="*60)
print("SETUP SUMMARY")
print("="*60)

all_checks = []
all_checks.append(('AWS credentials', all(aws_vars.values())))
all_checks.append(('GA4 credentials file', ga_vars['GOOGLE_APPLICATION_CREDENTIALS'] and 
                   os.path.exists(ga_vars['GOOGLE_APPLICATION_CREDENTIALS'] or '')))
all_checks.append(('GA4 property ID', bool(ga_vars['GA4_PROPERTY_ID'])))
all_checks.append(('Campaign config', os.path.exists(config_path)))

passed = sum(1 for _, status in all_checks if status)
total = len(all_checks)

if passed == total:
    print(f"✓ All checks passed ({passed}/{total})")
    print("\nYou're ready to run the PPC reporting script!")
    print(f"\nExample command:")
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    print(f"  python ppc_reporting.py --start-date {month_start} --end-date {yesterday}")
else:
    print(f"⚠ Some checks failed ({passed}/{total} passed)")
    print("\nPlease address the issues above before running the PPC reporting script.")
    for check, status in all_checks:
        if not status:
            print(f"  - Fix: {check}")

print("\nFor detailed setup instructions, see: docs/ppc_reporting_guide.md")
print("="*60)