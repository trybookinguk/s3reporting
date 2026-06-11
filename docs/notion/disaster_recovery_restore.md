# Disaster Recovery Restore
`restore_from_sharepoint.py`

**Category:** Infrastructure
**When to use:** the Pi has failed, been rebuilt, or lost data and needs its databases and secrets put back.

> ⚠️ **This is the recovery procedure for a broken or fresh Pi.** It is the counterpart to the nightly `backup_to_sharepoint.py` job. You should only need it in an emergency. Read this whole page before running anything.

## What it restores

The script pulls the most recent (or a chosen) nightly backup from SharePoint and puts each file back in its place on the Pi:

| File | Restored to | What it is |
|---|---|---|
| `.env` | `/root/s3reporting/.env` | All the secrets/credentials |
| `ecosystem.config.cjs` | `/root/reporting-dashboard/` | Dashboard config + secrets |
| `warehouse.db` | `.cache/prepared/` | Main SQLite data warehouse |
| `warehouse_duck.db` | `.cache/prepared/` | DuckDB file the dashboard reads |
| `retention_state.db` | `.cache/prepared/` | Retention priority state |
| `box_office.db` | `.cache/prepared/` | Box office state |
| `database_builder.db` | `.cache/prepared/` | Lead pipeline state |
| `zoho_cache.db` | `.cache/prepared/` | Cached Zoho lookups |

If a file already exists, it is **not** overwritten silently — the old one is moved aside to `<name>.pre-restore.bak` first, so a mistaken restore can be undone.

## The catch-22 you need to know about

The backup lives in SharePoint. Reaching SharePoint needs Azure credentials. But those credentials live in `.env` — which is one of the things you're restoring. So the script can't read them from a file that doesn't exist yet.

To break the loop, the script **asks you to type four Azure values at the start**, straight from your password manager:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `SHAREPOINT_DRIVE_ID`

> 🔑 **These four values are the master key to recovery.** They must be kept in the company password manager. Without them, the backup in SharePoint is unreachable. (This is also called out in the owner handover doc.)

## Step-by-step recovery

**1. Stand up the Pi and get the code.** On the fresh/repaired Pi, clone both repos:
```bash
git clone <s3reporting repo> /root/s3reporting
git clone <reporting-dashboard repo> /root/reporting-dashboard
cd /root/s3reporting
```

**2. Install the Python dependencies** (see `deploy/README.md` for the full list):
```bash
pip install --break-system-packages msal requests
```

**3. See what backups are available:**
```bash
python3 restore_from_sharepoint.py --list
```
You'll be prompted for the four Azure values, then it prints the available backup dates (newest first).

**4. Restore.** To restore the most recent backup:
```bash
python3 restore_from_sharepoint.py
```
Or a specific date:
```bash
python3 restore_from_sharepoint.py --date 2026-06-08
```

**5. Check the restored `.env`,** then start the pipeline and dashboard as normal (see `deploy/README.md` — `crontab deploy/pi-crontab`, and start the dashboard with pm2).

## Good to know

- Secret files (`.env`, `ecosystem.config.cjs`) are automatically locked down to owner-only (`chmod 600`) after restore.
- Backups are kept for the **last 7 days** only — so recover sooner rather than later, and don't rely on an old backup still being there weeks later.
- If a file in the backup isn't recognised, it's skipped with a warning rather than restored to the wrong place.
- The four Azure values you typed are also inside the restored `.env`, so once recovery is done the Pi has them for normal operation — you only type them manually for this one bootstrap step.

## If it goes wrong

- **"No backups found"** — the `SHAREPOINT_DRIVE_ID` is probably wrong, or the Azure credentials don't have access to that drive. Double-check the four values.
- **Authentication failed** — the `AZURE_CLIENT_SECRET` may have expired (it has a hard expiry date — see the owner handover doc). A lapsed secret breaks restore, the dashboard login, and the nightly backup all at once.
- **A restore put the wrong data back** — every file it replaced was saved as `<name>.pre-restore.bak` in the same folder; move that back to undo.
