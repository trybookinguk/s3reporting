# Weekly SalesIQ Report
`salesiq_weekly.py`

**Category:** Weekly report
**Schedule:** Every Tuesday at 08:00 UTC (09:00 BST)

## What it does

Sends a summary of the week's Zoho SalesIQ chat activity to the team — volume, response times, and key metrics.

## Who receives it

| | Recipients |
|---|---|
| **To** | jules@trybooking.co.uk, kathryn@trybooking.co.uk |
| **CC** | (none) |

**To add or remove recipients:** edit **`report_recipients.json`** in SharePoint (Platform Data folder), block `weekly_salesiq`. No code needed — see [Managing Report Emails](managing_report_emails.md).

## How to run manually

```bash
python3 salesiq_weekly.py
```

Test without sending:
```bash
export TEST_MODE=1
python3 salesiq_weekly.py
```

## Inputs

- Zoho SalesIQ API

## Outputs

- Email: HTML report to stakeholders

## Technical notes

- Runs 5 minutes before `weekly_reporting_unified.py` so both reports arrive close together
- Email sent via Azure Graph API
- Requires Zoho credentials (`ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`)
