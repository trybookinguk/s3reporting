# Running s3reporting on a Synology NAS (DSM 7.1.1)

Yes — a Synology NAS can run the reporting pipeline, via **Docker / Container
Manager**, with two conditions:

- **CPU must be Intel/x86-64.** Docker (called *Container Manager* on DSM 7.2,
  *Docker* on DSM 7.0/7.1) only runs on x86-64 Synology models. ARM "value"
  models (e.g. many `j`/`play`/older DS`x`slim units) can't run it — check your
  model at synology.com, or run `uname -m` over SSH (`x86_64` = good). The
  deploy script refuses to continue on ARM.
- **RAM matters.** The pipeline builds a multi-GB warehouse in pandas. The Pi
  did it in 4 GB; give the NAS **≥4 GB free**, ideally 8 GB+, or the
  `prepare_data.py` step can OOM.

This runs the **pipeline only** (data pull, warehouse, DuckDB materialise,
Zoho/SharePoint/email jobs, backup). The Node dashboard (`reporting-dashboard`)
is a separate deployment and out of scope here.

Why Docker and not DSM's Python: DSM's Python3 package is sandboxed, and
building `pandas`/`numpy`/`scipy`/`weasyprint`/`duckdb` natively against it is
fragile and arch-specific. The container pins all of that and matches the Pi's
Linux environment exactly.

## What's here
- `Dockerfile` — Python 3.12 + weasyprint system libs + the DuckDB CLI + pip deps.
- `docker-compose.yml` — one long-running container; repo bind-mounted at `/app`,
  persistent data at `/data`, secrets from `.env`.
- `deploy_synology.sh` — checks arch/Docker, ensures paths/`.env`, builds, starts,
  smoke-tests.

## One-time setup

**1. Enable Docker + SSH.** Package Center → install **Container Manager**
(or **Docker**). Control Panel → **Terminal & SNMP** → enable SSH. SSH in as an
administrator user.

**2. Clone the repo and create the data folder** (a `docker` shared folder is
conventional):
```bash
sudo mkdir -p /volume1/docker
sudo git clone https://github.com/trybookinguk/s3reporting.git /volume1/docker/s3reporting
sudo mkdir -p /volume1/docker/s3reporting-data
```

**3. Create `.env`** at `/volume1/docker/s3reporting/.env` (chmod 600). Use the
blank template from the repo root, but point the **paths at the container's
`/data`** (not `/root/s3reporting`):
```sh
# credentials … (AWS / Azure / SharePoint / Zoho / BACKUP_SECRET_PASSPHRASE)
# container data paths:
S3_CACHE_DIR=/data/.cache
DATA_DIR=/data/.cache/prepared
REPORTS_DIR=/data/reports
SYNC_MANIFEST_DIR=/data/.sync_manifest
DUCKDB_BIN=/usr/local/bin/duckdb
CACHE_TRUST_TODAY=1
TIER_SYSTEM=v1
```
```bash
sudo chmod 600 /volume1/docker/s3reporting/.env
```

**4. Build + start:**
```bash
cd /volume1/docker/s3reporting/deploy/synology
sudo ./deploy_synology.sh
```

**5. Verify:**
```bash
sudo docker exec s3reporting python3 test_secrets.py
sudo docker exec s3reporting ./deploy/run_daily.sh --test    # safe preview
```

## Scheduling the daily run (DSM Task Scheduler — the crontab replacement)

DSM ignores the repo's `deploy/pi-crontab`; use **Control Panel → Task
Scheduler** instead. Create a **Scheduled Task → User-defined script**, run as
`root`, daily at your chosen time, with:
```bash
/usr/local/bin/docker exec s3reporting /app/deploy/run_daily.sh
```
(or run the individual scripts as separate tasks to mirror the staggered cron
times). `run_daily.sh` runs the whole sequence in order and exits non-zero on
failure — tick "send run details by email" in the task's settings for alerts.

To update the code later: `sudo git -C /volume1/docker/s3reporting pull`, then
re-run `deploy_synology.sh` only if the `Dockerfile`/deps changed (code changes
are picked up live via the bind mount).

## Notes / caveats
- **DuckDB version**: the `Dockerfile` pins `DUCKDB_VERSION`. If the dashboard
  later reads this warehouse, match it to the dashboard's `@duckdb/node-api`.
- **Timezone** is set to `Europe/London` in the image; the cron/Pi schedule was
  UTC, so pick Task Scheduler times accordingly.
- **Backups still go to SharePoint** exactly as before (nothing NAS-specific),
  so disaster recovery is unchanged.
- This is an alternative host to the Pi — don't run both against live Zoho/email
  on the same schedule, or reports/CRM writes will double up.
