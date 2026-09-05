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

## Can't reach the Pi at all?

Normal access **is** Tailscale, so if Tailscale is the thing that's down you can't SSH by MagicDNS. Recovery paths, in order:

1. Tailscale SSH — `ssh root@trybookingpi.tail1fb257.ts.net`
2. Local LAN SSH — `ssh root@192.168.0.55` (same network only)
3. **Console access** — keyboard + monitor on the Pi. On a full re-image this is the only way in; reinstall Tailscale (`curl -fsSL https://tailscale.com/install.sh | sh`), then `tailscale up --ssh --hostname=TrybookingPi` and re-apply the `tag:server` tag + `ssh` ACL rule in the admin console.

## Prevention checklist

- [ ] `/etc/sysctl.d/99-tailscale.conf` exists with both forwarding lines (survives reboots — the fix above creates it).
- [ ] Exit node is **approved** in the admin console (approval persists; re-advertising after a re-register does not auto-approve).
- [ ] The four Azure values **and** `BACKUP_SECRET_PASSPHRASE` are in the company password manager — without them the SharePoint backup is unreachable.

## Good to know

- "Offers exit node" in `tailscale status` means *advertised*, not *approved* — the console tick is separate.
- A reset commonly hits **three** things at once: forwarding (Fix 1), exit-node state (Fix 2), and the working directory (Fix 4). Work them in that order.
- This page is connectivity only. For data recovery see [Disaster Recovery Restore](disaster_recovery_restore.md); for git auth see [Re-authenticating the Pi to GitHub](pi_github_reauth.md).
