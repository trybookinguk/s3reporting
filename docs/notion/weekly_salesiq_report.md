# Weekly SalesIQ Report
`salesiq_weekly.py`

**Category:** Weekly report
**Schedule:** Every Tuesday at 08:00 UTC (09:00 BST)

## What it does

Sends a summary of the week's Zoho SalesIQ chat activity to the team — volume, response times, and key metrics.

## Who receives it

Email to stakeholders via the standard distribution list.

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
