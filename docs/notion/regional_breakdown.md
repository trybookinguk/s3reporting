# Regional Breakdown
`regional_segmentation.py`

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Shows the regional distribution of accounts and events across the UK, based on postcodes. Useful for understanding geographic spread or preparing regional presentations.

## Who receives it

CSVs saved to `reports/`. No email is sent.

## How to run manually

```bash
python3 regional_segmentation.py
```

## Inputs

- S3: Accounts, BookingData

## Outputs

- `account_regional_report_*.csv` — accounts by UK region
- `event_regional_report_*.csv` — events by UK region

## Technical notes

- Assigns regions based on postcode areas
- Uses account postcode and event postcode separately, so an account in London can run events in Manchester
