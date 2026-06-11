# zoho_industry.py

**Category:** Daily automation
**Schedule:** Weekdays at 02:30 UTC

## What it does

Keeps the Industry field on every account in Zoho CRM up to date with the latest data from TryBooking. Runs silently in the background — no email is sent.

## Who it affects

Updates Zoho CRM account records. No email is sent.

## How to run manually

```bash
python3 zoho_industry.py
```

## Inputs

- S3: Accounts

## Outputs

- Zoho CRM: Industry field updated on each account

## Technical notes

- Weekdays only (Mon–Fri)
- Uses OAuth2 refresh token flow — if the Zoho token expires or the authorising account is removed, this job will fail silently; check `/root/logs/zoho-industry.log`
- Batch operations capped at 200 records per API call
