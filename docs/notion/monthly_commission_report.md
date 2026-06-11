# Monthly Commission Report
`sales_commission_report.py`

**Category:** Monthly report
**Schedule:** 2nd of each month at 09:00 UTC

## What it does

Calculates monthly sales commissions for each member of the sales team based on new accounts they brought in during the previous month, and sends the report.

## Who receives it

- **Each salesperson** receives their own commission report (PDF), sent to the address held in `sales_commission_config.json`.
- **The MD** receives an overall copy (CSV of each person's commission). MD recipient: joan@trybooking.co.uk.

**To add or remove the MD copy recipient:** edit **`report_recipients.json`** in SharePoint (Platform Data folder), block `monthly_commission_md` — see [Managing Report Emails](managing_report_emails.md).

**To change a salesperson's own address:** that lives in `sales_commission_config.json` and needs an engineer to update.

## How to run manually

```bash
python3 sales_commission_report.py
```

Run for a specific month:
```bash
export REPORT_MONTH=2026-05
python3 sales_commission_report.py
```

Test without sending a live email:
```bash
export TEST_MODE=1
python3 sales_commission_report.py
```

## Inputs

- S3: BookingData, Accounts
- Zoho CRM: account ownership data

## Outputs

- Email: PDF + HTML report to stakeholders
- CSV: saved to `reports/`

## Technical notes

- Commission rates and structure configured in `sales_commission_config.json` (flat fee + percentage per person)
- Runs on the 2nd so the 1st's monthly report has already run and data is settled
- Email via Azure Graph API
