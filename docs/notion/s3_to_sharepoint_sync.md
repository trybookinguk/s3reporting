# S3 to SharePoint Sync
`s3_to_sharepoint.py`

**Category:** Daily automation
**Schedule:** Weekdays at 02:15 UTC

## What it does

Copies TryBooking's raw data files from S3 into SharePoint so the team can access them directly without needing AWS credentials. Only files that have changed since the last run are uploaded.

## Who it affects

Files land in SharePoint under **Platform Data/Dashboard Data**.

## How to run manually

```bash
python3 s3_to_sharepoint.py
```

Preview what would be uploaded without actually uploading:
```bash
python3 s3_to_sharepoint.py --dry-run
```

First-time setup:
```bash
python3 s3_to_sharepoint.py --setup
```

## Inputs

- S3 bucket: `produk-rdsextracts-438255373632`

## Outputs

- SharePoint folder: Platform Data/Dashboard Data

## Technical notes

- ETag-based sync — compares S3 file checksums against the last known state, so only changed files are uploaded
- Sync manifest persists at `/root/s3reporting/.sync_manifest` — delete this to force a full re-upload
- Requires: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SENDER_MAILBOX`
- Runs weekdays only — weekend data is picked up on Monday
