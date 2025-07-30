# PPC Reporting Guide

## Overview

The PPC reporting script (`ppc_reporting.py`) integrates Google Analytics 4 conversion data with TryBooking's S3 booking data to track and attribute revenue from PPC campaigns. It identifies eligible accounts (new or first-time event creators) and calculates their revenue contribution.

## Prerequisites

### 1. Google Analytics 4 Setup

You need:
- A Google Cloud Project with Analytics Data API enabled
- A service account with analytics viewer permissions
- The GA4 property ID for your website

### 2. Environment Variables

Required environment variables:
```bash
# AWS credentials for S3 access
export AWS_ACCESS_KEY_ID="your-key-id"
export AWS_SECRET_ACCESS_KEY="your-secret-key"

# Google Analytics credentials
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
export GA4_PROPERTY_ID="123456789"  # Optional - can be passed via command line
```

### 3. Python Dependencies

Install the required packages:
```bash
pip install -r requirements_ppc.txt
```

## Google Analytics 4 Configuration

### 1. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select existing
3. Enable the Google Analytics Data API
4. Create a service account:
   - Navigate to IAM & Admin > Service Accounts
   - Click "Create Service Account"
   - Name it (e.g., "ppc-reporting")
   - Grant no specific roles at project level
   - Create and download JSON key

### 2. Grant GA4 Access

1. Go to Google Analytics 4 Admin
2. Navigate to Property > Property Access Management
3. Add the service account email with "Viewer" role

### 3. Find Your Property ID

1. In GA4 Admin > Property Settings
2. Copy the numeric Property ID

## Usage

### Basic Usage

```bash
# Report for a specific date range
python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31

# With custom output file
python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31 --output-file january_ppc_report.csv

# Using command-line property ID (overrides environment variable)
python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31 --property-id 123456789

# Test mode (for development)
python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31 --test-mode
```

### Date Range Considerations

- Use UK timezone dates (script handles timezone conversion)
- For current month data, end date can be yesterday
- Historical data depends on S3 file availability

## How It Works

### 1. GA4 Data Collection

The script queries GA4 for:
- Success page views matching `/uk/event/{ID}/success`
- Campaign attribution data (source, medium, campaign name)
- Conversion dates and session counts

Tracked dimensions:
- `pagePath` and `unifiedScreenClass` - To identify success pages
- `firstUserCampaignName` - Campaign that acquired the user
- `firstUserSource` and `firstUserMedium` - Traffic source details
- `date` - When the conversion occurred

### 2. S3 Data Integration

The script loads:
- **Accounts data**: Account details, creation dates, industries
- **Booking data**: Transaction records with revenue information

Revenue calculation includes all fees:
- Payment received (ticket price)
- Booking fee
- Card fee
- Processing fee
- Ticket fee

### 3. Eligibility Rules

An account is eligible for PPC attribution if:
- Account is less than 90 days old at conversion time, OR
- This is the first event for the account (no previous events)

For accounts over 12 months old, revenue is capped to the last 12 months only.

### 4. Output Report

The CSV report includes:
- **Account details**: ID, name, industry, creation date
- **Event details**: ID, name, tickets sold
- **Campaign data**: Campaign name, source, medium
- **Revenue metrics**: Total revenue, 12-month capped revenue
- **Eligibility status**: Whether eligible and reason

## Campaign Configuration

Active campaigns are defined in `config/ppc_campaigns.json`. The script automatically filters for campaigns marked as `"active": true`.

## Troubleshooting

### Common Issues

1. **"GOOGLE_APPLICATION_CREDENTIALS environment variable not set"**
   - Set the path to your service account JSON file
   
2. **"Permission denied" from GA4 API**
   - Ensure service account has viewer access to the GA4 property
   
3. **No conversions found**
   - Check date range has conversion data
   - Verify success page URL pattern matches `/uk/event/{ID}/success`
   
4. **No booking data matches**
   - Event IDs in GA4 must match EventId in booking data
   - Check S3 data is available for the date range

### Debug Mode

Run with Python logging to see detailed information:
```bash
python -u ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31
```

## Integration with GitHub Actions

To run via GitHub Actions, add these secrets:
- `GA4_PROPERTY_ID`
- `GOOGLE_APPLICATION_CREDENTIALS_JSON` (base64 encoded JSON key)

Example workflow step:
```yaml
- name: Setup Google credentials
  run: |
    echo "${{ secrets.GOOGLE_APPLICATION_CREDENTIALS_JSON }}" | base64 -d > /tmp/ga-key.json
    echo "GOOGLE_APPLICATION_CREDENTIALS=/tmp/ga-key.json" >> $GITHUB_ENV

- name: Run PPC Report
  env:
    AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
    AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
    GA4_PROPERTY_ID: ${{ secrets.GA4_PROPERTY_ID }}
  run: |
    python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31
```

## Maintenance

### Adding New Campaigns

Edit `config/ppc_campaigns.json` to add new campaigns. The script automatically picks up active campaigns.

### Modifying Eligibility Rules

Eligibility logic is in the `apply_eligibility_rules` method. Current rules:
- 90-day threshold for new accounts
- First event detection logic

### Revenue Calculations

Revenue components are defined in the `match_conversions` method. Modify the formula to include/exclude specific fee types.