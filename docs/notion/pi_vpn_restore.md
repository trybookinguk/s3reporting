# Restoring the Pi's VPN / Remote Access After a Reset

**Category:** Infrastructure
**When to use:** the Pi has rebooted or reset itself and a VPN client connects but gets **no internet**, or you've lost remote access to the Pi entirely.

> ⚠️ This is the network counterpart to [Disaster Recovery Restore](disaster_recovery_restore.md) (which restores *data & secrets*). This page restores *connectivity* — Tailscale access, the exit-node routing that gives clients internet, and the `/root/s3reporting` working directory if it was wiped.

The Pi runs two independent pieces of remote-access plumbing. Diagnose them separately:

- **Tailscale** — the admin/SSH path onto the Pi **and** the exit node that routes client internet traffic. Pi is `TrybookingPi`, login `root`, tailnet `trybooking.co.uk`, MagicDNS `trybookingpi.tail1fb257.ts.net`, tailnet IP `100.79.35.128`, tagged `tag:server`.
- **cloudflared** — the Cloudflare Tunnel fronting the reporting dashboard.

## The one symptom this page exists for

**A client connects to the VPN but has no internet.** Almost always this is one thing: a reset resets `net.ipv4.ip_forward` back to `0`, so the Pi stops routing client traffic out. See Fix 1 — it's the first thing to check every time.

## Fast triage

Run on the **client** while connected (Windows uses `-n`, Linux/macOS use `-c`):

```cmd
ping -n 3 1.1.1.1        :: raw IP  — tests routing / forwarding
ping -n 3 google.com     :: name    — tests DNS
```

| Result | Cause | Go to |
|---|---|---|
| Both fail | Routing / forwarding on the Pi | Fix 1, then Fix 2 |
| `1.1.1.1` works, name fails | DNS | Fix 3 |
| Both work, browser doesn't | App / proxy layer, not the tunnel | — |

## Fix 1 — IP forwarding (the #1 reset casualty)

On the Pi, check both stacks:

```bash
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

If either is `0`, enable **and persist** it so the next reset doesn't undo it:

```bash
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
```

Then re-apply Tailscale's NAT/masquerade rules (they may not have installed if forwarding was off when `tailscaled` started):

```bash
systemctl restart tailscaled
sudo nft list ruleset | grep -i masquerade    # expect a ts- masquerade rule
```

Re-test the pings from the client. This resolves it the large majority of the time.

## Fix 2 — Exit node advertised, approved, and selected

Three separate things must all be true; a reset or re-register can drop any of them.

1. **Advertised (on the Pi):**
   ```bash
   tailscale status | grep -i "exit node"        # Pi line should read "offers exit node"
   ```
   If not, re-advertise:
   ```bash
   tailscale up --advertise-exit-node --ssh --hostname=TrybookingPi
   ```

2. **Approved (admin console — not on the Pi):** Admin console → **Machines → TrybookingPi → ⋯ → Edit route settings** → the exit node must have a green tick. Advertised-but-unapproved = client connects, no traffic routed.

3. **Selected (on the client):** Tailscale tray/menu → **Exit node → TrybookingPi** must be ticked. Connecting to the tailnet alone does **not** route internet through the Pi.

## Fix 3 — DNS (IP pings work, names don't)

```bash
# client
nslookup google.com        # Windows
resolvectl status          # Linux — check the Tailscale adapter's DNS
```

In the admin console under **DNS**, confirm a global nameserver (e.g. `1.1.1.1` / `8.8.8.8`) is set, and enable *Override local DNS* if clients depend on it.

## Fix 4 — `/root/s3reporting` directory was deleted

If the reset also wiped the working directory (the git code, `.env`, and `.cache/prepared/*.db` all live under it), restore it:

**1. Re-clone the code.** Deleting the folder also removed the Pi's git credentials, so re-auth first if the clone fails with an auth / "Repository not found" error — see [Re-authenticating the Pi to GitHub](pi_github_reauth.md).
```bash
git clone https://github.com/trybookinguk/s3reporting.git /root/s3reporting
cd /root/s3reporting
pip install --break-system-packages msal requests pytz pandas boto3 python-dateutil cryptography
```
> `restore_from_sharepoint.py` imports `modules.utils`, which pulls in the whole
> utils stack — `msal requests` alone fails with `ModuleNotFoundError: No module
> named 'pytz'` (then pandas/boto3/cryptography). Install the wider set above, or
> the full `deploy/README.md` list plus `cryptography`.

**2. Restore secrets + warehouses from SharePoint.** Have the **four Azure values** *and* the **backup passphrase** (`BACKUP_SECRET_PASSPHRASE`) ready from the password manager — the files are stored encrypted:
```bash
python3 restore_from_sharepoint.py --list      # type the 4 Azure values; shows backup dates
python3 restore_from_sharepoint.py             # restore newest (or --date YYYY-MM-DD)
```
Full detail: [Disaster Recovery Restore](disaster_recovery_restore.md). Backups are kept **7 days only**.

**3. Bring the pipeline + dashboard back:**
```bash
pip install --break-system-packages boto3 msal requests pandas pytz numpy scipy \
    python-dateutil google-analytics-data matplotlib
mkdir -p /root/logs /root/s3reporting/reports
crontab /root/s3reporting/deploy/pi-crontab    # re-arm the schedule
# restart the dashboard under pm2 from /root/reporting-dashboard
```

## Adding / replacing the data disk

The pipeline's heavy, high-churn data lives under **`/root/s3reporting/.cache/`** — the S3 cache pickles directly under it, and every `.db` warehouse under `.cache/prepared/` (`warehouse.db`, `warehouse_duck.db`, `retention_state.db`, `box_office.db`, `database_builder.db`, `zoho_cache.db`). Paths come from `S3_CACHE_DIR` / `DATA_DIR` in `.env`. Putting this on a dedicated disk spares the SD card and isolates a disk failure from the code + secrets.

Use the helper script to mount a disk there persistently (it refuses the SD card / root disk, preserves any existing `.cache` data, and adds a `nofail` fstab entry by UUID so a future disk failure won't block boot):

```bash
# check which device is the new disk first
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL

# brand-new disk (partitions + formats — ERASES it; re-type device to confirm)
sudo FORMAT=1 /root/s3reporting/deploy/mount_cache_disk.sh /dev/sda

# disk that already has a filesystem
sudo /root/s3reporting/deploy/mount_cache_disk.sh /dev/sda
```

After a **disk failure**, the warehouses died with the old disk — once the new disk is mounted, repopulate `.cache/prepared/` from the nightly backup:
```bash
cd /root/s3reporting && python3 restore_from_sharepoint.py
```

See `deploy/mount_cache_disk.sh` for options (`MOUNTPOINT`, `LABEL`, `OWNER`).

**Current setup (this Pi):** the data disk is `/dev/sda1` (≈931 GB, ext4), mounted at **`/root/s3reporting/.cache`**, UUID `e5ffd1e9-0090-4377-9589-48238ce33812`. The persistent entry in `/etc/fstab` is:
```
UUID=e5ffd1e9-0090-4377-9589-48238ce33812  /root/s3reporting/.cache  ext4  defaults,nofail,x-systemd.device-timeout=15  0  2
```
Verify it's actually mounted (a blank MOUNTPOINT for `sda1` in `lsblk` means it is **not** — the pipeline would then write to the SD card):
```bash
findmnt /root/s3reporting/.cache      # shows /dev/sda1 when mounted; blank if not
lsblk | grep sda1                     # should list the mountpoint
sudo mount /root/s3reporting/.cache   # remount via fstab if it dropped off (nofail auto-mounts on boot)
```
> `nofail` means the Pi boots even if the disk is missing — good for resilience, but it also means a dropped disk fails **silently** and the pipeline quietly falls back to the SD card. If you ever unmount it manually (e.g. to reformat), remember to remount before the next cron run. (The nightly `health_check.py` now FAILs if the cache path is in fstab but not mounted, so a dropped disk emails an alert.)

### Boot order — SD-only (`0xf1`)

The OS lives on the **SD card** (`/boot/firmware` + `/` on `mmcblk0`); the USB is data only. The bootloader must therefore **not** try the USB disk at boot — a USB-first order (`0xf14`) makes the Pi scan the data disk first, fall through to SD, and appear to "try to boot from USB" (slow boot). Set SD-only:

```bash
sudo /root/s3reporting/deploy/set_sd_boot.sh        # sets BOOT_ORDER=0xf1 (SD only)
sudo reboot
```
Or by hand (Pi 4/5, `rpi-eeprom`):
```bash
sudo rpi-eeprom-config --edit
#   BOOT_ORDER=0xf14  ->  BOOT_ORDER=0xf1     # nibbles read right-to-left: 1=SD, 4=USB, f=retry
sudo reboot
```
`0xf1` = try SD, retry SD, never USB. (`0xf41` keeps USB as a boot *fallback* if you'd prefer that.) Verify after reboot:
```bash
sudo rpi-eeprom-config | grep BOOT_ORDER      # 0xf1
findmnt /                                     # /dev/mmcblk0p2 (SD)
findmnt /root/s3reporting/.cache              # /dev/sda1 auto-mounted via nofail
```
> Pi 3 and earlier don't use `rpi-eeprom` — boot order is fixed in OTP; `set_sd_boot.sh` detects this and exits with guidance. Check the model with `cat /proc/device-tree/model`.

## Can't reach the Pi at all?

Normal access **is** Tailscale, so if Tailscale is the thing that's down you can't SSH by MagicDNS. Recovery paths, in order:

1. Tailscale SSH — `ssh root@trybookingpi.tail1fb257.ts.net`
2. Local LAN SSH — `ssh root@192.168.0.55` (same network only)
3. **Console access** — keyboard + monitor on the Pi. On a full re-image this is the only way in; reinstall Tailscale (`curl -fsSL https://tailscale.com/install.sh | sh`), then `tailscale up --ssh --hostname=TrybookingPi` and re-apply the `tag:server` tag + `ssh` ACL rule in the admin console.

## Check dependencies first

A rebuild often misses packages that only one job imports — they fail silently until that job runs (matplotlib broke the 02:45 tier-movement emails; the DuckDB CLI broke the materialise). Check them all up front:
```bash
cd /root/s3reporting
python3 deploy/check_deps.py          # add --all to treat optional (GA4/pricing) as required
```
It does a **real import** of every runtime dependency (so it catches "installed but broken", e.g. weasyprint missing its system libs), verifies the **DuckDB CLI** binary, prints the exact `pip install` line for anything missing, and exits non-zero if a required dep is absent. Install what it reports, then re-run until it's green. Required set (used by scheduled jobs): boto3, pandas, numpy, pytz, python-dateutil, requests, msal, cryptography, matplotlib, weasyprint (+ the duckdb CLI). Optional (manual reports): google-analytics-data, PyYAML.

```bash
# typical fix in one line (weasyprint also needs the pango/cairo apt libs — see deploy/README):
pip install --break-system-packages boto3 pandas numpy pytz python-dateutil requests msal cryptography matplotlib weasyprint
```

## Bringing the nightly pipeline back (DuckDB materialise)

After the S3 pull + SQLite warehouse rebuild, `prepare_data.py` **materialises a DuckDB file** (`.cache/prepared/warehouse_duck.db`) that the dashboard reads. Two things bite on a freshly-rebuilt Pi and make it exit non-zero with *"the dashboard warehouse is STALE"*:

**1. The `duckdb` CLI binary is missing.** It's a separate install (not a pip package), so a rebuilt Pi won't have it, and the run fails with *"duckdb binary not found"*. Install the CLI (Raspberry Pi = ARM64):
```bash
uname -m                                   # expect aarch64 (64-bit OS); armv7l has no official build
curl https://install.duckdb.org | sh       # lands in ~/.duckdb/cli/latest/duckdb
sudo ln -sf /root/.duckdb/cli/latest/duckdb /usr/local/bin/duckdb   # put it on PATH
```
Then pin it in `.env` so **cron** finds it too (cron's PATH is minimal — this is why the materialise can silently freeze):
```bash
echo 'DUCKDB_BIN=/root/.duckdb/cli/latest/duckdb' >> /root/s3reporting/.env
duckdb -c "INSTALL sqlite; LOAD sqlite; SELECT 'ok';"   # the materialise needs the sqlite extension (fetched once, needs network)
```
> Version note: the dashboard reads the file with `@duckdb/node-api`. DuckDB's file format is version-sensitive — if the dashboard later can't open `warehouse_duck.db`, match the CLI version to the dashboard's `@duckdb/node-api` (in `reporting-dashboard/package.json`).

**2. A missing GA4 table used to abort the whole materialise.** On a warehouse where the GA4 ingest has never run, `ppc_attribution` doesn't exist. As of the fix in `prepare_data.py`, the materialise **probes for it** (like it does for `retention_priority`) and builds an empty shell if absent — so it no longer aborts, and no-ops once GA4 data exists. If you're on an older checkout that predates this, either `git pull origin main` or create the table manually:
```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/root/s3reporting/.cache/prepared/warehouse.db")
c.execute("""CREATE TABLE IF NOT EXISTS ppc_attribution (
  conversion_date TEXT NOT NULL, event_id INTEGER NOT NULL, campaign TEXT NOT NULL,
  source TEXT NOT NULL, medium TEXT NOT NULL, sessions INTEGER NOT NULL, users INTEGER NOT NULL,
  PRIMARY KEY (conversion_date, event_id, campaign, source, medium))""")
c.commit(); c.close(); print("ppc_attribution ensured")
PY
```

Re-run just the materialise (fast — no S3 re-pull):
```bash
cd /root/s3reporting && set -a && source .env && set +a
python3 prepare_data.py --materialise-only
```

## Running the pipeline by hand

`deploy/run_daily.sh` runs the whole daily sequence in cron order as one command — useful to catch up after a rebuild or a missed night:
```bash
cd /root/s3reporting
./deploy/run_daily.sh --test     # safe preview: TEST_MODE=1 + --dry-run, no Zoho writes / emails
./deploy/run_daily.sh            # live run (writes to Zoho, emails the team, runs the backup)
./deploy/run_daily.sh --no-backup
```
`prepare_data.py` runs first and must succeed (everything reuses its cache); the rest run in order, and the script prints a pass/fail summary and exits non-zero if any step failed. Always do a `--test` pass first after a rebuild.

## Rebuilding credentials (Zoho, SharePoint IDs)

If the backup passphrase is lost, `env.enc` can't be decrypted and each secret must be rebuilt from its source (see `docs/notion/disaster_recovery_restore.md`). Two are fiddly enough to have helpers/notes:

- **Zoho refresh token + portal/org id** — `deploy/zoho_setup.py` mints a refresh token via the headless **Self Client** grant-code flow and looks up `ZOHO_PORTAL_NAME` / `ZOHO_ORG_ID`. Key gotchas: the `ZOHO_CLIENT_ID`/`SECRET` in `.env` must be the **same Self Client** that generates the code (the refresh token is bound to it); this tenant uses the **`.com`** data centre (an `invalid_code` error usually means a wrong DC or an expired/reused code — codes are single-use, ~10 min).
- **SharePoint `SHAREPOINT_SITE_ID` / `SHAREPOINT_DRIVE_ID`** — these are Microsoft **Graph** values, not in the Entra portal. The tenant's SharePoint host is **`trybookingcouk.sharepoint.com`**, site **`PlatformData`**, and the backups live in the default **Documents** ("Shared Documents") drive under `Backups/pi/`. With an app token:
  ```bash
  curl -s -H "Authorization: Bearer $TOKEN" \
    "https://graph.microsoft.com/v1.0/sites/trybookingcouk.sharepoint.com:/sites/PlatformData:/drives?\$select=id,name,webUrl" | python3 -m json.tool
  ```
  The `Documents` drive's `id` is `SHAREPOINT_DRIVE_ID`; the site's `id` (from `…/sites/PlatformData?$select=id`) is `SHAREPOINT_SITE_ID`.

## Restoring email sending (Exchange application access policy)

Reports email via Microsoft Graph `sendMail` as `AZURE_SENDER_MAILBOX`. If sends fail with **`403 ErrorAccessDenied … [RAOP] : Blocked by tenant configured AppOnly AccessPolicy settings`**, the token and `Mail.Send` permission are fine — the block is Exchange's **Application Access Policy** (the app is restricted to specific mailboxes). This is the mechanism that locks the app to the shared sender mailbox.

After any Azure app change (or a mismatch between `AZURE_SENDER_MAILBOX` and the policy's group), fix it in **Exchange Online PowerShell** — use **Azure Cloud Shell** (browser PowerShell, no install) if you have no local PowerShell:
```powershell
Connect-ExchangeOnline
# every policy touching the app — look for MORE THAN ONE for the same AppId (they conflict!):
Get-ApplicationAccessPolicy | Format-List Identity,AppId,AccessRight,ScopeName,Description
# does the app get access to the SENDER mailbox?
Test-ApplicationAccessPolicy -Identity <AZURE_SENDER_MAILBOX> -AppId <AZURE_CLIENT_ID>
```
Rules learned the hard way:
- There must be **exactly one** `RestrictAccess` policy per AppId. **Two policies for the same app conflict** (intersection) and block sending even though `Test-ApplicationAccessPolicy` reports `Granted`. Remove the redundant one with `Remove-ApplicationAccessPolicy -Identity "<full identity>"`.
- `AZURE_SENDER_MAILBOX` **must be a member** of the policy's scope group. The original sender is a mailbox in the pre-existing `ReportingAppRestrictedSenders` group — set `.env` to that, or add your address to the group.
- Changes take **up to ~30 min** to propagate; `Granted` from `Test-…` is immediate but enforcement lags.
- This tenant uses **classic Application Access Policies**, not RBAC-for-Applications.

Test a send directly (bypasses TEST_MODE redirection):
```bash
cd /root/s3reporting && set -a && source .env && set +a
python3 -c "from modules.utils.email_utils import send_html_email; send_html_email('henry@trybooking.co.uk','Pi email test','<p>test</p>'); print('accepted')"
```
To **re-send reports whose emails failed** (e.g. a night the policy was broken): `python3 zoho_tiers.py --dry-run` sends the real emails but skips SharePoint state re-writes (preview first with `TEST_MODE=1 python3 zoho_tiers.py --dry-run`).

## Verify the recovery

Work top-down until each is clean:
```bash
cd /root/s3reporting && set -a && source .env && set +a
python3 deploy/check_deps.py                 # deps + duckdb CLI
python3 test_secrets.py                      # AWS/Azure/SharePoint/Zoho/backup
./deploy/run_daily.sh --test                 # whole pipeline, no live writes/emails
# scan the logs for anything real (ignores benign "Failed: 0" etc.):
for f in /root/logs/*.log; do printf "%-28s %s\n" "$(basename "$f")" "$(grep -ciE 'error|exception|traceback' "$f")"; done
# logs are append-only, so pre-fix errors linger — check only the latest run of a job:
awk 'BEGIN{RS="=== Completed at"} {last=$0} END{print last}' /root/logs/zoho-tiers.log \
  | grep -iE "error|exception|traceback|denied|no module" || echo "latest run: no errors"
```
Then confirm the dashboard shows a current **"Data as of"** date (Caddy on `:80` reverse-proxies the SvelteKit app on `:3000`; historical `EADDRINUSE :80` lines are recovery-restart noise, not a fault).

## Monitoring (so you find out before the team does)

Two layers, both via the existing email path:
- **Per-job failure alerts** — `scripts/cron_wrapper.sh` runs every cron job and calls `scripts/cron_notify.py` to email a report the moment a job exits non-zero.
- **Nightly health summary** — `scripts/health_check.py` (cron at 05:00) emails a daily status of the **Tailscale & cloudflared services**, deps + duckdb CLI, warehouse freshness, disk space, and each job's latest run (weekend-aware: Mon–Fri jobs don't WARN on Sat/Sun). It's a **dead-man's switch**: no email at all means the Pi or cron is down. It also catches silent issues (a stale warehouse, a down VPN/tunnel, an exit-0 run with errors inside).

Both need email working (the Exchange access policy above). Test the health check without waiting for cron:
```bash
cd /root/s3reporting && set -a && source .env && set +a
python3 scripts/health_check.py --stdout          # print, no email
python3 scripts/health_check.py                    # send the summary now
```
Recipient defaults to henry@ + kathryn@; override with `HEALTHCHECK_RECIPIENT` in `.env`.

## Prevention checklist

- [ ] `/etc/sysctl.d/99-tailscale.conf` exists with both forwarding lines (survives reboots — the fix above creates it).
- [ ] Exit node is **approved** in the admin console (approval persists; re-advertising after a re-register does not auto-approve).
- [ ] The four Azure values **and** `BACKUP_SECRET_PASSPHRASE` are in the company password manager — without them the SharePoint backup is unreachable.
- [ ] `duckdb` CLI installed and `DUCKDB_BIN` set in `.env` (else the nightly DuckDB materialise silently fails and the dashboard goes stale).
- [ ] `python3 deploy/check_deps.py` is green (all required Python deps + the duckdb CLI present) — a missing per-job package (e.g. matplotlib, weasyprint) only surfaces when that job runs.
- [ ] Exactly **one** Exchange Application Access Policy per Azure AppId, and `AZURE_SENDER_MAILBOX` is a member of its scope group (else `sendMail` 403s with `[RAOP]`).
- [ ] DuckDB CLI version matches the dashboard's `@duckdb/node-api` (currently **1.5.3**) so the materialised file is readable.
- [ ] `python3 test_secrets.py` all green, and `BACKUP_SECRET_PASSPHRASE` is in the password manager.

## Good to know

- "Offers exit node" in `tailscale status` means *advertised*, not *approved* — the console tick is separate.
- A reset commonly hits **three** things at once: forwarding (Fix 1), exit-node state (Fix 2), and the working directory (Fix 4). Work them in that order.
- The `duckdb` materialise runs a CLI binary, not a Python lib — installing DuckDB via pip does **not** satisfy it; you need the CLI on PATH or `DUCKDB_BIN`.
- A rebuild often needs the pipeline pieces in this order: duckdb CLI, then a materialise (empty `ppc_attribution`/`retention_priority` are fine), then the secrets (`test_secrets.py`), then `deploy/run_daily.sh --test`.
- A full rebuild touches, in rough order: connectivity (Fixes 1–3) → directory + `.env` (Fix 4) → dependencies (`check_deps.py`) → duckdb + materialise → credentials (`test_secrets.py`, Zoho/SharePoint rebuild) → email access policy → `run_daily.sh --test` → verify logs. Each has a section above.
- Email 403 `[RAOP]` after an Azure app change is an **Exchange** problem (application access policy), not a Pi/code one — fix it in Exchange Online PowerShell / Cloud Shell.
- Logs under `/root/logs/` are append-only; pre-fix error lines persist in the current file until the 30-day cleanup deletes whole old files. Scope error scans to the latest run.
- This page is connectivity only for the *network* parts. For the standing Tailscale/cloudflared setup, dependencies and config files see [Pi Network Services](pi_network_services.md); for data recovery see [Disaster Recovery Restore](disaster_recovery_restore.md); for git auth see [Re-authenticating the Pi to GitHub](pi_github_reauth.md).
