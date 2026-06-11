# Monthly Performance Report
`monthly_reporting.py`

**Category:** Monthly report
**Schedule:** 1st of each month at 09:00 UTC

## What it does

Sends the monthly performance report covering the previous calendar month — new accounts, revenue, fees, and key trends.

## Who receives it

Email to stakeholders. CSV also saved to `reports/`.

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
