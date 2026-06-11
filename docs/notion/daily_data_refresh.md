# Daily Data Refresh
`prepare_data.py`

**Category:** Daily automation
**Schedule:** Every day at 02:00 UTC (including weekends)

## What it does

Pulls the latest data from TryBooking's S3 storage and rebuilds the local data warehouse that the dashboard and all reports read from. This is the foundation everything else depends on — if this job fails, reports and the dashboard will show stale data.

## Who it affects

Updates the reporting dashboard automatically. No email is sent.

## How to run manually

```bash
python3 prepare_data.py
```

To rebuild the dashboard data only (faster, skips S3 download):
```bash
python3 prepare_data.py --materialise-only
```

## Inputs

- S3: Accounts, Users, BookingData, BookingDataAll

## Outputs

- `warehouse.db` — SQLite database with full booking history
- `warehouse_duck.db` — DuckDB file read directly by the dashboard

## Technical notes

- Runs daily including weekends — critical for capturing month-end data (the current-month BookingData file stops updating when the month ends)
- After rebuilding, curls `/api/warm` to rebuild dashboard in-process caches so the first user doesn't see a slow load
- `duckdb` CLI must be on PATH (`/usr/local/bin`) — without it the DuckDB materialise is silently skipped
- A second run at 03:30 weekdays (`--materialise-only`) picks up retention priority data written by `zoho_tiers.py`
- S3 files are cached in `.cache/` for 7 days to avoid repeated downloads; set `NO_CACHE=1` to force a fresh pull
