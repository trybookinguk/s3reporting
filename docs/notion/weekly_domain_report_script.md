# Weekly Domain Report
`weekly_domain_report.py`

**Category:** Weekly report
**Schedule:** Every Monday at 09:00 UTC

## What it does

Extracts the email domains of new users and counts them — useful for understanding which organisations and email providers new sign-ups are coming from, and spotting acquisition trends.

## Who receives it

| | Recipients |
|---|---|
| **To** | louise@trybooking.co.uk |
| **CC** | (none) |

The report is emailed with the CSV attached. **To add or remove recipients:** edit **`report_recipients.json`** in SharePoint (Platform Data folder), block `weekly_domain`. No code needed — see [Managing Report Emails](managing_report_emails.md).

## How to run manually

```bash
python3 weekly_domain_report.py
```

## Inputs

- S3: Users

## Outputs

- CSV: `email_domains_*.csv` saved to `reports/`

## Technical notes

- Reads from the Users S3 report (not booking data)
- Output is a simple domain frequency count — useful as an input to acquisition analysis
