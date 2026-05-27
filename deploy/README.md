# Raspberry Pi deployment

The full daily pipeline runs from cron on the Pi instead of GitHub Actions.

| # | Script | Schedule (UTC, Mon–Fri) | Notes |
| --- | --- | --- | --- |
| 1 | `prepare_data.py` | 02:00 | Refreshes the S3 cache, builds the combined booking pickle, and updates the SQLite warehouse. |
| 2 | `s3_to_sharepoint.py` | 02:15 | S3 → SharePoint sync. Manifest persists locally. |
| 3 | `zoho_industry.py` | 02:30 | Zoho industry sync. Runs from env vars. |
| 4 | `zoho_tiers.py` | 02:45 | Zoho tiers + tier-movement pipeline. Reports saved locally. |
| 5 | `generate_dashboard_data.py` | 03:15 | Builds dashboard JSON (incl. PPC/GA4) and uploads to SharePoint. |

`prepare_data.py` runs first so the rest of the jobs reuse one cache refresh and
one combined-booking build instead of each re-downloading and re-combining from
S3. The later jobs trust that day's cache via `CACHE_TRUST_TODAY=1`.

The old GitHub Actions workflows (`daily_sync.yml`, `tier_movements.yml`) have
been removed — the Pi now owns all of this.

## SQLite warehouse

`prepare_data.py` also maintains a local SQLite warehouse (`warehouse.db`,
under `DATA_DIR` by default; override with `WAREHOUSE_DB`). Tables:

- `bookings` — transaction log keyed by `BookingTransactionId`. Seeded once
  from the full `BookingDataAll`, then kept current by upserting the daily
  `BookingData` (current-month-to-date) file. Rows are `INSERT OR REPLACE`d so
  a transaction whose status/fees are revised later is corrected in place;
  prior-month rows are never deleted. Indexed on `AccountId`, `TransactionDate`.
- `accounts`, `users` — current-state snapshots, full-replaced each run.

The first run does the one-time `BookingDataAll` seed (heavy); subsequent runs
just upsert the small daily delta. The combined pickle is still written too —
the tier and dashboard jobs read that for now; migrating them to query the
warehouse is separate follow-up work.

Inspect it any time:

```bash
sqlite3 /root/s3reporting/.cache/prepared/warehouse.db \
  "SELECT key,value FROM meta; SELECT COUNT(*) FROM bookings;"
```

To skip the warehouse on a manual run: `python3 prepare_data.py --no-warehouse`.

## One-time setup

```bash
mkdir -p /root/logs /root/s3reporting/reports
chmod 600 /root/s3reporting/.env
pip install --break-system-packages boto3 msal requests pandas pytz numpy scipy \
    python-dateutil google-analytics-data
crontab /root/s3reporting/deploy/pi-crontab
```

`python-dateutil` is required by the dashboard; `google-analytics-data` is
required for the PPC/GA4 section of the dashboard.

## .env additions

On top of the existing secrets, add:

```sh
# --- Local paths (replace Actions cache/artifacts) ---

# Persistent S3 cache shared across the staggered jobs.
S3_CACHE_DIR=/root/s3reporting/.cache

# Combined booking pickle location (defaults under S3_CACHE_DIR/prepared).
DATA_DIR=/root/s3reporting/.cache/prepared

# Tier report CSVs (tier_updates_*, upcoming_annual_events_*, industry_summary_*,
# *_current_*) — written here instead of uploaded as Actions artifacts.
REPORTS_DIR=/root/s3reporting/reports

# ETag manifest for the SharePoint sync.
SYNC_MANIFEST_DIR=/root/s3reporting/.sync_manifest

# After prepare_data.py runs first each morning, downstream jobs trust that
# day's cache and skip per-file head_object checks. Set on the shared .env so
# all jobs see it; prepare_data.py always refreshes regardless.
CACHE_TRUST_TODAY=1

# --- Dashboard secrets (generate_dashboard_data.py) ---
GA4_PROPERTY_ID=...
GA4_SERVICE_ACCOUNT_KEY=...   # service account JSON (single line, or a path the script reads)
MAILSHAKE_API_KEY=...
```

`TIER_SYSTEM=v1` is already in `.env` per the migration plan. `TEST_MODE`
defaults to off; set `TEST_MODE=true` for a manual preview run.

> `GA4_SERVICE_ACCOUNT_KEY` is read the same way it was on Actions. If the
> existing Actions secret held the JSON inline, keep that format in `.env`.

## Manual / ad-hoc runs

The old `workflow_dispatch` inputs are now CLI flags or env vars:

```bash
# Refresh cache only, skip the combined-booking build
python3 prepare_data.py --no-combined

# Preview the SharePoint sync without uploading
python3 s3_to_sharepoint.py --dry-run

# Tier run that skips SharePoint writes (history/snapshot untouched)
python3 zoho_tiers.py --dry-run

# Tier preview emails redirected to the test recipient, no Zoho upsert
TEST_MODE=true python3 zoho_tiers.py --dry-run

# Rebuild tier_history.json from scratch
python3 zoho_tiers.py --rebuild-history --rebuild-from-scratch

# Dashboard, generate locally without uploading
python3 generate_dashboard_data.py --dry-run --local-dir ./dashboard_output
```

For any manual run, source the env first. To force fresh S3 reads bypassing the
trusted cache, prefix with `NO_CACHE=1`:

```bash
set -a && source /root/s3reporting/.env && set +a
NO_CACHE=1 python3 prepare_data.py    # ignore cache, re-download everything
```
