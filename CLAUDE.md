# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Running Scripts Locally
Scripts are executed through GitHub Actions in production, but can be tested locally:
```bash
python3 <script_name>.py
```

### Test Mode
Enable test mode to redirect emails to test recipients:
```bash
export TEST_MODE=1
python3 weekly_reporting.py
```

### Debug Without Email
Use the no-email version for testing:
```bash
python3 weekly_reporting_noemail.py
```

## High-Level Architecture

This codebase implements automated reporting pipelines for TryBooking UK using GitHub Actions for scheduling and execution.

### GitHub Actions Workflows
1. **Weekly Reports** (`reports.yml`): Runs every Tuesday at 08:00 UTC
   - Executes `salesiq_weekly.py` and `weekly_reporting.py`
   - Sends email reports to stakeholders
   
2. **Zoho Industry Sync** (`zoho_industry.yml`): Runs daily at 01:00 UTC
   - Syncs account industry data from S3 to Zoho CRM
   
3. **Tier Updates** (`zoho_tiers.yml`): Manual trigger only
   - Calculates and updates account tiers in Zoho CRM
   
4. **Test Workflow** (`weekly_reporting_test.yml`): Manual trigger for testing

### Data Flow
1. **S3 Data Source**: TryBooking exports CSV files daily to S3 buckets
   - Production data: `produk-rdsextracts-438255373632`
   - Archive format: `YYYY/MM/YYYYMM-reportname-TBUK.csv`
   
2. **Python ETL Scripts**: Process data for different purposes:
   - `TBUK_Data_Pipeline.py`: Core pipeline merging account, balance, risk, and booking data
   - `weekly_reporting.py`: Weekly new account analysis with YoY comparisons
   - `salesiq_weekly.py`: Weekly chat summary from Zoho SalesIQ
   - `zoho_industry.py` & `zoho_tiers.py`: Sync account data to Zoho CRM
   
3. **Outputs**: 
   - Email reports via Mailgun SMTP (port 587)
   - Zoho CRM updates via API
   - Local CSV files for debugging

### Key Patterns
- All credentials stored as GitHub Secrets and passed as environment variables
- Date calculations use Europe/London timezone consistently
- S3 operations use boto3 with retry logic for reliability
- API rate limiting handled through batch operations (200 records for Zoho)
- Test mode redirects all emails to `alex@trybooking.co.uk`

### Required Environment Variables
- AWS: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- Zoho: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID`
- Mailgun: `MAILGUN_FROM`, `MAILGUN_PASSWORD`
- Optional: `TEST_MODE`, `TEST_DATE` for development

### Common Operations
- **Fetching S3 files**: Use boto3 to download from `produk-rdsextracts-438255373632` bucket
- **Processing dates**: Always use timezone-aware datetime with Europe/London
- **Zoho API**: OAuth2 with refresh token flow, batch operations of 200 records max
- **Email sending**: SMTP via Mailgun with HTML/plain text multipart messages
- **Error handling**: Critical operations wrapped in try-except, failures logged to console