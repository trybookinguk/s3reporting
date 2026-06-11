# Monthly Commission Report
`sales_commission_report.py`

**Category:** Monthly report
**Schedule:** Run manually when needed (not currently on the Pi cron)

## What it does

Calculates monthly sales commissions for each member of the sales team, based on accounts they claimed in Zoho whose first paid event has completed, and emails each person their own report.

## Who receives it

- **Each salesperson** receives their own commission report (PDF), sent automatically to their **Zoho login email** — the address on their Zoho user account. There is no separate email list to maintain.
- **Whoever manages commissions** receives an overall copy (a CSV of everyone's commission). This recipient is managed in SharePoint.

**To change who receives a salesperson's report:** update that person's email on their **Zoho user account** — the report follows their Zoho login email.

**To change who gets the overall summary copy:** edit **`report_recipients.json`** in SharePoint (Platform Data folder), block `monthly_commission_summary` — see [Managing Report Emails](managing_report_emails.md).

## How to run manually

For the previous month:
```bash
export SCHEDULED_RUN=true
python3 sales_commission_report.py
```

For a specific month:
```bash
export REPORT_MONTH=2026-05
python3 sales_commission_report.py
```

Test without sending live emails (everything goes to the test address):
```bash
export TEST_MODE=1
python3 sales_commission_report.py
```

## Inputs

- S3: BookingData, Accounts
- Zoho CRM: claimed accounts (who owns each account) and user emails

## Outputs

- Email: PDF to each salesperson, CSV summary to whoever manages commissions
- CSV: saved to `reports/`

## Technical notes

- **Commission rates** (flat fee + percentage) are set per person in `sales_commission_config.json`. Names there must match the Zoho "Claimed" user name. This file holds rates only — **no email addresses**.
- **Salesperson emails** are resolved from Zoho: the account's "Claimed" user → that user's Zoho login email. Requires the `ZohoCRM.users.READ` scope on the refresh token.
- If a salesperson has no email in Zoho, their CSV is sent to the summary recipient instead and a note is printed — so no commission is silently lost.
- Email via Azure Graph API.
