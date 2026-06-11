# eoy_planning_report.py

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Generates a comprehensive set of planning reports — useful for target-setting, year-end reviews, and monthly performance deep-dives. Covers revenue trends, seasonality, industry breakdowns, account cohorts, geographic distribution, event keywords, and PPC attribution.

## Who receives it

CSVs saved to subfolders: `planning/`, `seasonality/`, `industry/`, `cohorts/`, `geography/`, `keywords/`. No email is sent.

## How to run manually

Rolling 12-month view (most common):
```bash
python3 eoy_planning_report.py
```

Monthly review mode:
```bash
export REPORT_TYPE=monthly_review
export REVIEW_MONTH=2026-05
python3 eoy_planning_report.py
```

Custom date range:
```bash
export REPORT_TYPE=custom
export CUSTOM_START=2026-01-01
export CUSTOM_END=2026-05-31
python3 eoy_planning_report.py
```

Full year:
```bash
export REPORT_TYPE=year_2025
python3 eoy_planning_report.py
```

## Inputs

- S3: Accounts, BookingData, BookingDataAll
- Google Analytics 4 API

## Outputs

- CSVs across multiple subfolders (planning, seasonality, industry, cohorts, geography, keywords)

## Technical notes

- Requires `GA4_PROPERTY_ID` and `GA4_SERVICE_ACCOUNT_KEY` for PPC-related outputs
- See `docs/EOY_PLANNING_REPORT_GUIDE.md` for a full breakdown of every output file and what each metric means
