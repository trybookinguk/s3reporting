# Utility Scripts

These scripts are run manually on an ad-hoc basis. They don't send emails or update any systems unless you explicitly run the live version.

---

## zoho_tiers_v2.py — Tier dry run

Runs the tier calculation and shows what the results would be, without making any changes to Zoho CRM. Always run this before `zoho_tiers.py` to check for unexpected changes.

```bash
python3 zoho_tiers_v2.py
```

**Output:** `tier_output.csv` + summary printed to console

---

## compare_tiers.py — Tier comparison

Compares two sets of tier calculation results side by side to show what has changed. Run after `zoho_tiers_v2.py`.

```bash
python3 compare_tiers.py
```

**Output:** Comparison CSV

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

Restores the Pi's databases and config from a SharePoint backup. Use this if the Pi fails or data is lost.

```bash
# See available backups first
python3 restore_from_sharepoint.py --list

# Restore from a specific date
python3 restore_from_sharepoint.py --date 2026-06-10
```

Backups are created nightly by `backup_to_sharepoint.py` and stored in SharePoint under `Backups/pi/`.
