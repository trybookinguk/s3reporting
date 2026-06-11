# Monthly Performance Report
`monthly_reporting.py`

**Category:** Monthly report
**Schedule:** 1st of each month at 09:00 UTC

## What it does

Sends the monthly performance report covering the previous calendar month — new accounts, revenue, fees, and key trends. Two versions go out: a full-financials version for the MD, and a lighter version for general staff.

## Who receives it

| Version | To | CC |
|---|---|---|
| **MD** (full financials) | joan@trybooking.co.uk, henry@trybooking.co.uk | (none) |
| **Staff** (lighter) | louise@trybooking.co.uk, jules@trybooking.co.uk | (none) |

CSV also saved to `reports/`.

**To add or remove recipients:** edit **`report_recipients.json`** in SharePoint (Platform Data folder) — block `monthly_performance_md` for the MD version, `monthly_performance_staff` for the staff version. No code needed — see [Managing Report Emails](managing_report_emails.md).

## How to run manually

```bash
python3 monthly_reporting.py
```

Test without sending a live email:
```bash
export TEST_MODE=1
python3 monthly_reporting.py
```

## Inputs

- S3: Accounts, BookingData, BookingDataAll

## Outputs

- Email: HTML report to stakeholders
- CSV: saved to `reports/`

## Technical notes

- Runs on the 1st of the month — S3's monthly files land on the 1st, but new-month files don't appear until the 2nd, so the report always reads the previous month's complete data
- Email via Azure Graph API
