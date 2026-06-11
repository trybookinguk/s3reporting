# PPC Attribution Report
`ppc_reporting.py`

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Matches PPC ad clicks in Google Analytics with TryBooking sign-ups to show which campaigns are converting — and how much revenue those accounts have generated.

## Who receives it

CSV saved to `reports/`. No email is sent.

## How to run manually

```bash
python3 ppc_reporting.py
```

Test without writing output:
```bash
export TEST_MODE=1
python3 ppc_reporting.py
```

## Inputs

- Google Analytics 4 API
- S3: BookingData

## Outputs

- CSV: `ppc_report_2024-06-01_YYYY-MM-DD.csv` saved to `reports/`

## Technical notes

- Data starts from 2024-06-01 (when PPC tracking was set up) and runs to today
- Campaign configuration is in `config/ppc_campaigns.json` — update this when campaigns change
- Requires `GA4_PROPERTY_ID` and `GA4_SERVICE_ACCOUNT_KEY`
- Use `test_ppc_setup.py` to verify credentials are configured correctly before running
