# weekly_reporting_unified.py

**Category:** Weekly report
**Schedule:** Every Tuesday at 08:05 UTC (09:05 BST)

## What it does

Sends the weekly new accounts report — how many accounts signed up this week, compared to the same period last year, with a breakdown by industry and source.

## Who receives it

Email to stakeholders. CSV also saved to `reports/`.

## How to run manually

```bash
python3 weekly_reporting_unified.py
```

Test without sending a live email:
```bash
export TEST_MODE=1
python3 weekly_reporting_unified.py
```

Run as if it were a different date:
```bash
export TEST_DATE=2026-05-06
python3 weekly_reporting_unified.py
```

## Inputs

- S3: Accounts, BookingData

## Outputs

- Email: HTML report to stakeholders
- CSV: saved to `reports/`

## Technical notes

- `TEST_MODE=1` redirects the email to test recipients instead of the live list
- Email sent via Azure Graph API (Mailgun SMTP as fallback)
- Runs 5 minutes after `salesiq_weekly.py`
