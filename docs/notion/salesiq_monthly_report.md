# salesiq_monthly_report.py

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Pulls a full year of monthly SalesIQ chat statistics — volume by month, response times, and agent performance. Useful for annual reviews or when you need a longer historical view than the weekly report provides.

## Who receives it

CSV saved locally. No email is sent.

## How to run manually

```bash
python3 salesiq_monthly_report.py
```

For a specific year:
```bash
export YEAR=2025
python3 salesiq_monthly_report.py
```

## Inputs

- Zoho SalesIQ API

## Outputs

- CSV: `salesiq_monthly_YYYY.csv`

## Technical notes

- Separate from `salesiq_weekly.py` — this pulls a full year of monthly aggregates rather than last week's data
- Requires Zoho credentials
