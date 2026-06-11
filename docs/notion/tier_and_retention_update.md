# Tier & Retention Update
`zoho_tiers.py`

**Category:** Daily automation
**Schedule:** Weekdays at 02:45 UTC

## What it does

Recalculates which tier every account belongs to based on their revenue history, then updates Zoho CRM and the dashboard. Also calculates the retention priority score that flags which accounts the CS team should focus on.

## Who it affects

- Zoho CRM: Tier fields updated on each account
- Dashboard: Tier and retention priority data refreshed (via the 03:30 re-materialise)
- CSV saved to `reports/` for reference

## How to run manually

To preview changes without updating Zoho (read-only — no writes anywhere), run:
```bash
python3 zoho_tiers.py --preview
```

To apply live:
```bash
python3 zoho_tiers.py
```

## Inputs

- S3: Accounts, BookingData
- Zoho CRM (read — to carry over previous tier for movement tracking)

## Outputs

- Zoho CRM: tier fields updated
- SQLite warehouse: retention priority scores written
- CSV: tier output saved to `reports/`

## Technical notes

- **Tier v2 algorithm:** 55% current 12-month revenue + 35% lifetime revenue + 10% years of loyalty
- **Bands:** Tier 1 = top 2%, Tier 2 = 3–10%, Tier 3 = 11–25%, Tier 4 = 26–50%, Tier 5 = 51–100%
- Writing retention priority to SQLite is why a second `prepare_data.py --materialise-only` runs at 03:30 — otherwise the dashboard would show yesterday's retention scores
- Weekdays only
- See `docs/retention_priority_technical_spec.md` for the full scoring formula
