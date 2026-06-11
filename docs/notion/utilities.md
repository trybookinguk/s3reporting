# Utilities
`various`

These scripts are run manually on an ad-hoc basis. They don't send emails or update any systems unless you explicitly run the live version.

---

## zoho_tiers_v2.py — Tier dry run

Runs the tier calculation and shows what the results would be, without making any changes to Zoho CRM. Always run this before `zoho_tiers.py` to check for unexpected changes.

```bash
python3 zoho_tiers_v2.py
```

**Output:** `tier_output.csv` + summary printed to console

---

## check_account_names.py — Account name lookup

Takes a list of account names and fuzzy-matches them against TryBooking data. Useful for identifying duplicates, looking up accounts by partial name, or cleaning up a list.

```bash
python3 check_account_names.py
```

Requires a `names.txt` file in the working directory — one name per line.

**Output:** CSV with matched / needs review / no match results

---

## account_box_office_cardpresent.py — Single account box office breakdown

Shows a breakdown of Box Office card-present revenue for one specific account over the last 365 days.

```bash
ACCOUNT_ID=12345 python3 account_box_office_cardpresent.py
```

**Output:** Console summary + CSV

---

## test_ppc_setup.py — PPC credential check

Checks that AWS and Google Analytics credentials are correctly configured before running `ppc_reporting.py`. Safe to run at any time — read-only.

```bash
python3 test_ppc_setup.py
```

---

## test_vero_api.py — Vero connectivity check

Checks that the Vero API connection is working before running CRM action scripts. Runs in test mode — no live writes.

```bash
python3 test_vero_api.py
```

---

## restore_from_sharepoint.py — Disaster recovery

Restores the Pi's databases and config from a SharePoint backup if the Pi fails or data is lost. This is a full recovery procedure with a few important steps (including a credential bootstrap) — it has its own page: **[Disaster Recovery Restore](disaster_recovery_restore.md)**.
