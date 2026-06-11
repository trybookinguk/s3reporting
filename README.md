# TryBooking UK — Reporting & Data Pipeline

Automated reporting and CRM sync pipelines for TryBooking UK. All scheduled jobs run on a Raspberry Pi via cron. On-demand scripts are run manually from the Pi or a local machine with credentials loaded.

See `docs/scripts_and_reports_inventory.csv` for the full inventory of every script — schedule, data in/out, how to run, and technical notes.

---

## Architecture

### Data Sources (S3)

TryBooking exports CSV files daily to `produk-rdsextracts-438255373632`.

| Report | Description |
|---|---|
| BookingData | Current month's transactions, updated daily |
| BookingDataAll | All transactions up to the 1st of the current month |
| Accounts | Account-level data |
| Users | User and role data |
| RiskReport | Account risk flags |

File path format: `YYYY/MM/YYYYMM-reportname-TBUK.csv`

> **Month-end timing:** The current-month BookingData file stops updating when the month ends. New-month files appear on the 2nd. The 1st must therefore read the previous month's data.

### Local Warehouse (Pi)

`prepare_data.py` runs nightly and builds two local stores:

- **SQLite** (`warehouse.db`) — full booking, account, user history; used by analysis scripts
- **DuckDB** (`warehouse_duck.db`) — materialised aggregate tables; read directly by the SvelteKit dashboard

S3 files are cached in `.cache/` (7-day TTL, ETag-validated) to avoid repeated downloads.

### Pi Cron Schedule

All production jobs run via `deploy/pi-crontab`. Key timings:

| Time (UTC) | Job |
|---|---|
| 02:00 daily | prepare_data.py — S3 cache refresh + SQLite + DuckDB materialise |
| 02:15 weekdays | s3_to_sharepoint.py — mirror S3 files to SharePoint |
| 02:30 weekdays | zoho_industry.py — sync industry to Zoho CRM |
| 02:45 weekdays | zoho_tiers.py — recalculate tiers + update Zoho CRM |
| 03:30 weekdays | prepare_data.py --materialise-only — rebuild DuckDB with same-day retention data |
| 04:00 daily | backup_to_sharepoint.py — nightly Pi state backup |
| 08:00 Tuesdays | salesiq_weekly.py — weekly chat report email |
| 08:05 Tuesdays | weekly_reporting_unified.py — weekly new accounts report email |
| 09:00 1st of month | monthly_reporting.py — monthly performance report email |
| 09:00 2nd of month | sales_commission_report.py — monthly commission report email |
| 09:00 Mondays | weekly_domain_report.py — email domain extraction |

---

## Running Scripts

All scripts load credentials from `/root/s3reporting/.env` on the Pi (or a local `.env` / exported env vars).

```bash
# Standard run
python3 <script_name>.py

# Test mode — redirects emails to test recipients
export TEST_MODE=1
python3 <script_name>.py

# Override the processing date
export TEST_DATE=2026-05-01
python3 <script_name>.py

# Disable S3 cache (force fresh download)
export NO_CACHE=1
python3 <script_name>.py
```

> **Data protection:** Do not run scripts that read S3 data on machines outside the approved environment. Scripts that touch regulated data should be handed to the operator to run.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` | S3 access |
| `AWS_SECRET_ACCESS_KEY` | S3 access |
| `ZOHO_CLIENT_ID` | Zoho CRM OAuth2 |
| `ZOHO_CLIENT_SECRET` | Zoho CRM OAuth2 |
| `ZOHO_REFRESH_TOKEN` | Zoho CRM OAuth2 |
| `ZOHO_PORTAL_NAME` | Zoho CRM portal |
| `AZURE_TENANT_ID` | Microsoft Graph / email / SharePoint |
| `AZURE_CLIENT_ID` | Microsoft Graph / email / SharePoint |
| `AZURE_CLIENT_SECRET` | Microsoft Graph / email / SharePoint |
| `AZURE_SENDER_MAILBOX` | Email sender address |
| `VERO_API_KEY` | GetVero user tagging |
| `GA4_PROPERTY_ID` | Google Analytics 4 |
| `GA4_SERVICE_ACCOUNT_KEY` | GA4 service account JSON (path or inline) |
| `TEST_MODE` | Redirect emails to test recipients |
| `TEST_DATE` | Override processing date (YYYY-MM-DD) |
| `NO_CACHE` | Disable S3 file caching |
| `REPORTS_DIR` | Output directory for CSVs (default: ./reports) |

---

## Key Patterns

- **Timezones:** All date calculations use `Europe/London` consistently
- **Zoho API:** OAuth2 refresh token flow; batch operations capped at 200 records
- **Email:** Azure Graph API primary; Mailgun SMTP fallback
- **Tier algorithm (v2):** 55% current 12-month revenue + 35% lifetime revenue + 10% years of loyalty
- **Vero tagging:** `TEST_MODE=1` skips live writes; always test before running in production

---

## Deployment & Recovery

- **Deploy crontab to Pi:** `crontab deploy/pi-crontab`
- **Backup Pi state:** `python3 backup_to_sharepoint.py` (runs automatically at 04:00)
- **Restore from backup:** `python3 restore_from_sharepoint.py --list` then `--date YYYY-MM-DD`
- **Full handover notes:** `docs/HANDOVER_OWNER.md`

---

## S3 Booking Data Fields

| Field | Description |
|---|---|
| BookingId | Internal platform use only |
| BookingTransactionId | Internal platform use only |
| AccountId | Account ID on TryBooking |
| AccountName | Account name on TryBooking |
| DateTimeCreated | Date and time the account was created |
| EventId | Event ID within TryBooking |
| EventName | Event name within TryBooking |
| TransactionDate | Date and time of the transaction |
| BookingUrlId | External booking ID |
| PaymentReceived | Ticket value excluding fees |
| TicketQuantity | Number of tickets in the transaction |
| BookingFee | Ticket fee paid by the organiser |
| CardFee | Processing fee paid by the organiser |
| ProcessingFee | Processing fee paid by the purchaser |
| TicketFee | Ticket fee paid by the purchaser |
| Surcharge | Not used in UK |
| ProcessingFeeSurcharge | Not used in UK |
| TransactionType | Always "Payment" |
| PaymentType | Payment method (paid tickets only) |
| EventPostcode | Venue postcode on the event |
| AccountPostcode | Postcode in account settings |
| EventDate | Session date and time of the event |
| Industry | Industry assigned in TryBooking |
| SubIndustry | Sub-industry assigned in TryBooking |
| Gateway Group | Payment gateway assigned to the account |
| DGRStatus | Not used in UK |
| CustomerId | Internal customer ID |
| IPCountry | Country of transaction by IP |
| Status | Successful / Failed / Unknown |
| Wallet | Apple Pay / Google Pay (Stripe only) |
| GatewayName | Payment gateway used |
| GatewayId | Payment reference in Stripe/PayPal |
| GatewayReference | Not used in UK |
| GiftCertificateTypeName | Gift certificate name (if applicable) |
| GiftCertificateId | Gift certificate ID (if applicable) |
| BookingCountryCode | Always GBR |
