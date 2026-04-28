# CLAUDE.md



This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Style Guidelines

- Use British spellings always.

## Commands

**IMPORTANT:** You should NEVER run code or read non code files directly. Instead, you should give the command required to the user. This is to ensure compliance with data protection legislation.

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
- Optional: `NO_CACHE=1` to disable S3 file caching

### Common Operations
- **Fetching S3 files**: Use boto3 to download from `produk-rdsextracts-438255373632` bucket
  - Files are automatically cached in `.cache/` directory to avoid repeated downloads
  - Cache validates S3 file timestamps - stale cache is automatically refreshed
  - Cache expires after 7 days to ensure fresh data
  - GitHub Actions workflows use `actions/cache` to persist cache between runs
  - Cache key includes date to ensure fresh data each day in production
  - Set `NO_CACHE=1` environment variable to disable caching
  - Run `python3 -c "from modules.s3_data_loader import clear_cache; clear_cache()"` to clear cache locally
- **Processing dates**: Always use timezone-aware datetime with Europe/London
- **Zoho API**: OAuth2 with refresh token flow, batch operations of 200 records max
- **Email sending**: SMTP via Mailgun with HTML/plain text multipart messages
- **Error handling**: Critical operations wrapped in try-except, failures logged to console

## Reporting Fields

Our platform provides several reports via S3 that can be used for data analysis. These are:

- BookingDataAll: provides all booking data up until the 1st of the month.
- BookingData: provides all booking data for the current month, updated daily at midnight.
- Accounts: provides account related data.

### Booking Data Reports
| **Field** | **Description** |
| BookingId | **Internal platform use only** |
| BookingTransactionId | **Internal platform use only** |
| AccountId | The ID of the account on TryBooking. |
| AccountName | The name of the account on TryBooking. |
| DateTimeCreated | The date and time the account was created on TryBooking. |
| EventId | The ID of the event within TryBooking |
| EventName | The name of the event within TryBooking |
| DonationCampaignId | **Not used in UK** |
| DonationCampaignName | **Not used in UK** |
| TransactionDate | The date and time the transaction took place. |
| BookingUrlId | External Booking ID |
| PaymentReceived | The price of all tickets within the transaction (excluding fees) |
| TicketQuantity | The quantity of tickets contained within the transaction. |
| BookingFee | The ticket fee which has been paid by the event organiser. |
| CardFee | The processing fee which has been paid by the event organiser. |
| ProcessingFee | The processing fee which has been paid by the ticket purchaser. |
| Surcharge | **No longer used in UK.** Payment surcharges (e.g. AU charge extra for PP) |
| ProcessingFeeSurcharge | **No longer used in UK.** Payment surcharges (e.g. AU charge extra for PP) |
| TicketFee | The ticket fee which has been paid by the ticket purchaser. |
| TransactionType | Appears to always be Payment. |
| PaymentType | **Paid tickets only**. The payment type used for the transaction. |
| EventPostcode | The postcode added to the event as venue details. |
| AccountPostcode | The postcode added to the account under Account Settings. |
| EventDate | The session date and time of the event for which tickets were booked. |
| Industry | The industry assigned to the account within the TryBooking platform. |
| SubIndustry | The sub industry assigned to the account within the TryBooking platform. |
| Gateway Group | The gateway assigned to the account within the TryBooking platform. |
| DGRStatus | **Not used in UK** |
| CustomerId | Internal customer ID. |
| IPCountry | The country that the transaction was made within based on IP. |
| Status | The status of the transaction, either Successful, Failed or Unknown. |
| Wallet | **Stripe only**. The type of mobile wallet (Apple Pay/Google Pay) used. |
| GatewayName | The payment gateway used for the transaction. |
| GatewayId | The payment reference used within Stripe/PayPal. |
| GatewayReference | **Not used in UK** |
| GiftCertificateTypeName | The name of the gift certificate used for the transaction. |
| GiftCertificateId | The unique ID for the gift certificate used for the transaction (if applicable) |
| BookingCountryCode | Always GBR - internal region code. |
