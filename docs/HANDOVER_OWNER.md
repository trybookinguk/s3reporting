# Owner's Handover — TryBooking UK Reporting Stack

Owner-level overview. For hands-on operation, see the **Pi / Dashboard Handover** doc.
Items marked **‼️** need filling in by the outgoing operator. 🔎 boxes are commands you
(or an AI assistant) can run to find an answer. Snapshot 2026-06.

---

## 0. Decide first: who operates this?

No designated operator yet. The whole stack runs on **one Raspberry Pi + SSD over SSH**,
historically run by one person. Bus factor of one. Options:

1. **Operate it yourself** — low effort in normal weeks, but you own incidents (§4).
2. **Delegate to an engineer** — lowest risk; a competent person picks it up in a day.
3. **Migrate to managed hosting** (§5) — removes the single point of failure; it's a project.

Decide this and write it at the top of this doc.

---

## 1. What it does

| | |
| --- | --- |
| **Dashboard** | `https://trybooking.internal` — CS & Marketing tool (retention, tiers, PPC). Microsoft login. |
| **Zoho sync** | Nightly push of industry, tiers, retention priority to Zoho CRM. |
| **Email reports** | Weekly stakeholder reports. |
| **SharePoint feed** | Data export for the wider business. |

Reporting + CRM only. **Does not touch live bookings or payments** — an outage is an
internal-visibility problem, not customer-facing.

---

## 2. Bus factor — secrets & access

One Raspberry Pi ("TrybookingPi", office LAN + Tailscale), external SSD for data. Code is
on GitHub (`trybookinguk` org). **Secrets are not** — they live only on the Pi:

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

**Do now — the disaster-recovery key:** put these in the company password manager. Without
them you cannot reach the backup that holds everything else:
- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `SHAREPOINT_DRIVE_ID`
- the SSH private key for the Pi ‼️ *operator: where is a copy?*
- ‼️ *operator: which Tailscale account owns the Pi node?*

---

## 3. Vendors

> 🔎 List configured secret names (values hidden): `ssh root@<Pi> 'sed -E "s/=.*/=<set>/" /root/s3reporting/.env'`

| Service | Used for | Owner ‼️ | Renewal |
| --- | --- | --- | --- |
| AWS | S3 booking data | | Access keys, manual rotate |
| Zoho CRM | Tier/industry sync | | OAuth refresh token ⚠️ |
| Azure (Entra) | Dashboard login + SharePoint | | Client secret — **expires** ⚠️ |
| Mailgun | Report emails | | SMTP password |
| Mailshake | Lead/outreach | | API key |
| GA4 | PPC data | | Service-account JSON |
| Tailscale | Off-network Pi access | | Device auth |
| Domain/TLS | Dashboard URL | n/a | Caddy local cert, auto |

> ⚠️ **Diarise:** **Azure client secret** expires on a fixed date — when it lapses,
> dashboard login *and* SharePoint sync break together. ‼️ Get the expiry date (Azure
> portal → Entra → App registrations → Certificates & secrets) and put it in your calendar.
> **Zoho token** is revocable; failure surfaces via the nightly email.

---

## 4. Cost

- **Time:** near-zero in normal weeks (cron self-heals, emails only on failure). Most
  incidents are a 5-min restart (§7 of the engineer doc). Rare ones (expired secret, dead
  SSD) need a rebuild. ‼️ *operator: honest hours/month?*
- **Money:** ‼️ AWS bill + any paid Zoho/Mailgun/Mailshake/Tailscale tiers, and who's billed.

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

- `s3reporting/deploy/README.md` once listed a 03:15 dashboard-JSON job — **removed**
  (commit `8e8b20f`); the dashboard reads DuckDB directly now. Trust `deploy/pi-crontab`.
- A cache-warm token sits in plaintext in `deploy/pi-crontab` (localhost-only, low risk).
- ~14 GitHub Actions workflows still exist and may run in parallel with the Pi.
  > 🔎 GitHub repo → Actions tab → check which ran in the last month.

---

## 7. First hour

1. **Save the DR key** (§2) — Azure four-value card + SSH key → password manager.
2. **Get in:** `ssh root@<Pi> 'pm2 list && crontab -l'`.
3. **Health:** dashboard URL loads (a Microsoft-login redirect = healthy);
   `ssh root@<Pi> 'tail -5 /root/logs/prepare-data.log'` shows success.
4. **Diarise** the Azure secret expiry (§3).
5. **Check Actions** (§6).
6. **Decide §0** and record it.

---

## 8. Deeper

- **Pi / Dashboard Handover** — engineer's manual: topology, deploy, break-glass runbook.
- `s3reporting/deploy/README.md` — pipeline scripts + flags.
- `s3reporting/CLAUDE.md` — conventions (British spelling; booking data is regulated).
- Repos: `trybookinguk/reporting-dashboard` (UI), `trybookinguk/s3reporting` (ETL).
