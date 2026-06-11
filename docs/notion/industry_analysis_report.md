# Industry Analysis
`industry_analysis.py`

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Breaks down accounts and revenue by industry sector — useful for presentations, sector-level reporting, or understanding where growth is coming from.

## Who receives it

CSVs saved to `reports/`. No email is sent.

## How to run manually

```bash
python3 industry_analysis.py
```

## Inputs

- S3: BookingDataAll, BookingData, Accounts

## Outputs

- `industry_analysis_*.csv` — summary by industry
- `industry_period_*.csv` — industry breakdown over time
- `industry_account_*.csv` — account-level industry detail

## Technical notes

- Produces three levels of detail: summary, period-over-period, and per-account
- Uses BookingDataAll + BookingData combined for full history
