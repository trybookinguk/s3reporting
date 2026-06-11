# TryBooking UK — Reporting & Data Pipeline

This repository contains the scripts that power TryBooking UK's automated reports, CRM updates, and data pipeline. Everything runs on a Raspberry Pi — no manual intervention is needed for day-to-day operation.

For a full list of every script, see **`docs/scripts_and_reports_inventory.csv`** — it covers what each one does, when it runs, where the output goes, and how to run it manually.

---

## What happens automatically

### Every day
- **02:00** — pulls the latest data from TryBooking into the local warehouse and updates the dashboard
- **04:00** — backs up the Pi's databases and config to SharePoint

### Every weekday
- **02:15** — copies TryBooking's raw data files to SharePoint
- **02:30** — syncs account industry data to Zoho CRM
- **02:45** — recalculates account tiers and updates Zoho CRM
- **03:30** — refreshes the dashboard with the updated tier and retention data

### Every week
- **Tuesday 08:00** — weekly SalesIQ chat summary emailed to the team
- **Tuesday 08:05** — weekly new accounts report emailed to the team

---

## On-demand reports

These are run manually when needed — typically from the Pi or a machine with credentials set up.

| Report | What it's for |
|---|---|
| `eoy_planning_report.py` | Planning metrics — targets, seasonality, cohorts, geography, industry |
| `industry_analysis.py` | Revenue and account breakdown by industry sector |
| `salesiq_monthly_report.py` | Full year of monthly SalesIQ chat stats |
| `ppc_reporting.py` | PPC campaign conversion and revenue attribution |
| `regional_segmentation.py` | UK regional distribution of accounts and events |
| `keyword_analysis_report.py` | Analysis of event types by keyword (balls, concerts, etc.) |

---

## Managing report email recipients

Who receives each emailed report is controlled by a single file in SharePoint:
**Platform Data → `report_recipients.json`**. Non-technical staff can add or remove
people by editing that file directly — no code or git required. See
`docs/notion/managing_report_emails.md` for the step-by-step guide.

If the file is ever missing or broken, the scripts fall back to a built-in default
list (in `modules/utils/config.py`) so reports always go out.

---

## CRM actions

These tag users in GetVero to trigger automated follow-up messages. **Always run with `TEST_MODE=1` first** to check the output before applying live tags.

| Script | What it does |
|---|---|
| `event_completion_reminders.py` | Tags users after their events complete |
| `funds_remaining_reminder.py` | Tags users who have unspent funds from past events |

---

## How to run a script

Scripts normally run on the Pi via cron, but any of them can be run by hand. They read their credentials from `/root/s3reporting/.env` on the Pi, or from environment variables on a local machine.

```bash
# Standard run
python3 script_name.py

# Test mode — redirects emails, skips live CRM writes
export TEST_MODE=1
python3 script_name.py

# Override the date the script thinks it is
export TEST_DATE=2026-05-01
python3 script_name.py
```

### Running locally

You can run these on your own machine for development or one-off analysis, as long as you have the credentials and dependencies. **Booking and account data is regulated** — only run data-reading scripts on an approved machine, and prefer the read-only / test paths (e.g. `zoho_tiers.py --preview`, `TEST_MODE=1`) over anything that writes to Zoho, SharePoint, GetVero, or sends email.

1. **Get the credentials.** Copy the Pi's `.env` (from the password manager / a secure transfer — never commit it) into the repo root, or export the variables in your shell. See the credentials table below for what each one is for.

2. **Install dependencies:**
   ```bash
   pip install --break-system-packages boto3 msal requests pandas pytz numpy scipy \
       python-dateutil google-analytics-data matplotlib weasyprint
   ```
   (`weasyprint` is only needed for the commission report's PDFs; `google-analytics-data` only for PPC/planning.)

3. **Load the credentials and run:**
   ```bash
   set -a && source .env && set +a     # load .env into the shell
   export TEST_MODE=1                   # safety: redirect emails, skip live writes
   python3 zoho_tiers.py --preview      # example: read-only tier preview
   ```

4. **To force fresh data** (bypass the local cache):
   ```bash
   NO_CACHE=1 python3 script_name.py
   ```

> If in doubt about whether a script writes anywhere, run it with `TEST_MODE=1` first and read its output — every emailing/CRM script honours it.

---

## If something goes wrong

**Dashboard is stale** — the nightly data pull may have failed. Check `/root/logs/prepare-data.log` on the Pi. Run `python3 prepare_data.py` to refresh manually.

**A report didn't send** — check the relevant log in `/root/logs/`. Run the script manually to resend.

**Pi needs rebuilding** — use `restore_from_sharepoint.py` to restore from the nightly SharePoint backup:
```bash
python3 restore_from_sharepoint.py --list        # see available backups
python3 restore_from_sharepoint.py --date YYYY-MM-DD
```

**Tiers look wrong** — run `zoho_tiers.py --preview` first (read-only, makes no changes) to check the output before running the live `zoho_tiers.py`.

Full handover notes are in `docs/HANDOVER_OWNER.md`.

---

## Credentials needed

| Service | What it's used for |
|---|---|
| AWS | Reading data from TryBooking's S3 storage |
| Zoho CRM | Updating account tiers, industry, and retention fields |
| Microsoft 365 / Azure | Sending emails and syncing files to SharePoint |
| Google Analytics 4 | PPC reporting and planning reports |
| GetVero | Tagging users for automated CRM messages |

All credentials are stored in `/root/s3reporting/.env` on the Pi. See `docs/HANDOVER_OWNER.md` for how to obtain or rotate them.

---

## Data sources (S3)

TryBooking exports these files daily to S3 bucket `produk-rdsextracts-438255373632`:

| File | What it contains |
|---|---|
| BookingData | This month's transactions, updated daily |
| BookingDataAll | All transactions up to the 1st of the current month |
| Accounts | Account details and status |
| Users | User accounts and roles |
| RiskReport | Account risk flags |

File path format: `YYYY/MM/YYYYMM-reportname-TBUK.csv`

> **Month-end note:** The current month's BookingData file stops updating when the month ends. The new month's files don't appear until the 2nd. Scripts running on the 1st will read the previous month's data — this is intentional.
