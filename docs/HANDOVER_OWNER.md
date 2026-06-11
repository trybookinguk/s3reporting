# TryBooking UK Reporting Stack — Owner's Handover

This document details the internal data handling for TryBooking UK, which runs on a single
Raspberry Pi.

The Pi currently runs:

- Daily and weekly email reporting
- Syncs enriched account data to Zoho CRM
- Processes the data for the reporting dashboard
- Hosts the reporting dashboard
- Cloudflared for Cloudflare Access, plus Tailscale and Caddy for access and routing

It is reporting and CRM only — it does not touch live bookings or payments. An outage is an
internal-visibility problem, not customer-facing.

---

## 1. Decide first: who operates this?

No designated operator yet. Everything runs on that one Pi, historically run by one
person — so it's a single point of failure. Options:

1. **Operate it yourself** — low effort in normal weeks, but you own incidents (§4).
2. **Delegate to an engineer** — lowest risk; a competent person picks it up in a day.
3. **Migrate to managed hosting** (§5) — removes the single point of failure; it's a project.

Decide this and write it at the top of this doc.

---

## 2. Bus factor — secrets & access

One Raspberry Pi ("TrybookingPi"), external SSD for data. Code is on GitHub
(`trybookinguk` org). **Secrets are not** — they live only on the Pi:

| File | Holds |
| --- | --- |
| `/root/s3reporting/.env` | AWS, Azure/SharePoint, GA4, Zoho, Mailshake |
| `/root/reporting-dashboard/ecosystem.config.cjs` | Azure, Zoho, Mailgun, DB paths |

**Backup:** a nightly 04:00 job copies both secret files, the dashboard state DBs, and the
warehouses to SharePoint (`Backups/pi/<date>/`, last 7 kept).

> 🔎 Confirm it ran: `ssh root@<Pi> 'tail -5 /root/logs/backup-sharepoint.log'` — healthy
> ends `Backup complete: N uploaded`.

**Restore** onto a fresh Pi: clone both repos, run `restore_from_sharepoint.py`, enter the
Azure values when prompted.

**Access:**
- **Dashboard** — published via **Cloudflared** (Cloudflare Access); users sign in through
  Cloudflare. Caddy reverse-proxies to the app locally with a TLS cert.
- **Pi admin** — a normal **SSH key** on the LAN, and **Tailscale SSH** off-network (granted
  by company Tailnet membership). You're not locked out if one is lost: a new local SSH key
  can be generated and Tailnet access re-granted. Safeguard **Tailnet admin control** and the
  **Cloudflare account**. ‼️ *operator: confirm who admins the Tailnet and Cloudflare.*

**Do now — the disaster-recovery key:** put these four in the company password manager.
Without them you cannot reach the backup that holds everything else:
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `SHAREPOINT_DRIVE_ID`.

---

## 3. Vendors

> 🔎 List configured secret names (values hidden): `ssh root@<Pi> 'sed -E "s/=.*/=<set>/" /root/s3reporting/.env'`

All vendor accounts sit under **one company account** (not personal logins).

| Service | Used for | Renewal |
| --- | --- | --- |
| AWS | S3 booking data | Access keys, manual rotate |
| Zoho CRM | Tier/industry sync | OAuth refresh token — re-mint if its account is removed ⚠️ |
| Azure (Entra) | Dashboard login + SharePoint | Client secret — **expires Mar 2028** ⚠️ |
| Mailgun | Report emails | SMTP password |
| GA4 | PPC data | Service-account JSON |
| Tailscale | Pi admin access (SSH) | Tailnet membership |
| Cloudflare | Dashboard access (Cloudflared) | Cloudflare account |
| Domain/TLS | Dashboard URL | Caddy local cert, auto |

> ⚠️ **Diarise now: Azure client secret expires March 2028.** When it lapses, dashboard
> login *and* SharePoint sync (incl. the nightly backup) break together — put it in the
> calendar with a renewal reminder a month before. **Zoho token** is bound to the user who
> authorised it — **if that Zoho account is removed, all Zoho sync stops.** Re-mint a refresh
> token (ideally under a shared/service account) before removing the account; steps + scopes
> are in the Pi handover doc ("Re-authenticating Zoho").

---

## 4. Cost

- **Time:** historically **under 1 hour/month** — cron self-heals and emails only on
  failure; most incidents are a 5-min restart (§7 of the engineer doc). Rare ones (expired
  secret, dead SSD) need a rebuild.
- **Money:** ‼️ AWS bill + any paid Zoho/Mailgun/Mailshake/Tailscale tiers (all on the one
  company account).

---

## 5. Migrating off the Pi

- Code is portable (2 repos, Python + SvelteKit).
- Hard part: the 3.7 GB SQLite warehouse + staggered cron. Cloud VM = lift-and-shift;
  serverless = re-architecture.
- It ran on GitHub Actions before; reversing is a known path (git history).
- **Cheapest de-risk short of migrating:** a second box running the same cron + an off-Pi
  backup. Keeps the architecture, kills the single-SSD risk.

---

## 6. Doc drift — trust the live machine

- The dashboard reads DuckDB directly. Trust `deploy/pi-crontab` as the authoritative schedule.
- A cache-warm token sits in plaintext in `deploy/pi-crontab` (localhost-only, low risk).
- GitHub Actions workflows have been removed. The Pi cron is the only scheduler.

---

## 7. First hour

1. **Save the DR key** (§2) — Azure four-value card → password manager; confirm Tailnet access.
2. **Get in:** `ssh root@<Pi> 'pm2 list && crontab -l'`.
3. **Health:** dashboard URL loads (a Microsoft-login redirect = healthy);
   `ssh root@<Pi> 'tail -5 /root/logs/prepare-data.log'` shows success.
4. **Diarise** the Azure secret expiry (§3).
5. **Decide §1** (who operates this) and record it.

---

## 8. Deeper

- **Pi / Dashboard Handover** — engineer's manual: topology, deploy, break-glass runbook.
- `s3reporting/deploy/README.md` — pipeline scripts + flags.
- `s3reporting/README.md` — full architecture, cron schedule, environment variables, data sources.
- `s3reporting/docs/scripts_and_reports_inventory.csv` — every script: what it does, when it runs, how to run it manually.
- Repos: `trybookinguk/reporting-dashboard` (UI), `trybookinguk/s3reporting` (ETL).
