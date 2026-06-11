# backup_to_sharepoint.py

**Category:** Daily automation
**Schedule:** Every day at 04:00 UTC

## What it does

Backs up the Pi's databases and config files to SharePoint every night. This is the safety net — if the Pi's hard drive fails, everything needed to rebuild it is in SharePoint.

## Who it affects

Saves files to SharePoint under **Backups/pi/YYYY-MM-DD/**. Keeps the last 7 daily copies.

## How to run manually

```bash
python3 backup_to_sharepoint.py
```

Skip the warehouse databases (faster, secrets only):
```bash
python3 backup_to_sharepoint.py --no-warehouse
```

Preview without uploading:
```bash
python3 backup_to_sharepoint.py --dry-run
```

## Inputs

- Local Pi files: `.env`, `ecosystem.config.cjs`, `warehouse.db`, `warehouse_duck.db`

## Outputs

- SharePoint: `Backups/pi/YYYY-MM-DD/`

## Technical notes

- Runs after the 03:30 materialise so the backup always includes same-day tier and retention data
- To restore from a backup, use `restore_from_sharepoint.py`
- The four Azure credentials (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `SHAREPOINT_DRIVE_ID`) must be stored in the company password manager separately — they are the key to everything else
