# Owner's Handover — TryBooking UK Reporting Stack

> **Read this first.** It is the owner-level overview of the reporting stack you are
> inheriting: what it is, what it costs to keep alive, what breaks it, and the one
> decision to make early. It sits *above* the engineer-level docs — when you need to
> actually operate the machine, drop down to the **Pi / Dashboard Handover** prompt and
> the repos' `deploy/README.md`.
>
> **How to use this with an AI assistant.** This document is written so you can work
> through it with Claude Code (or similar) at your side. Throughout, you'll see boxes
> like this:
>
> > 🔎 **Find out:** `ssh root@<Pi-address> 'pm2 list'`
> > *Ask your AI to run this and explain the output.*
>
> Each one is a question this handover can't answer from the code alone (because the
> answer lives on the running machine or in someone's head). Paste the command to your
> AI, let it run and interpret the result, and write the answer into this file as you go.
> By the time you reach the end, the **‼️** gaps that remain are the few things only the
> outgoing operator can tell you — chase those directly.
>
> Written on handover by the outgoing operator, 2026-06.

---

## 0. The one decision to make first

**There is no designated hands-on operator. That is currently undecided, and it is the
most important open question in this handover.**

The entire stack runs on a *single Raspberry Pi* (with an external **SSD** for storage)
and is operated hands-on over SSH — historically by one person. It is well-built and
self-healing in the small (it emails you when a nightly job fails), but it has a **bus
factor of one** and **no redundancy**. Decide which you're doing:

1. **Operate it yourself.** Viable — you're technical, and an AI assistant can drive the
   SSH commands for you — but budget the time (§4). It's not zero-touch; it's "watch the
   failure emails, occasionally SSH in."
2. **Delegate to an engineer/contractor.** The engineer-level docs are good; a competent
   person could pick this up in a day. Lowest-risk if your time is scarce.
3. **Migrate off the Pi to managed hosting** (§5). Removes the single point of failure
   but is a project, not a config change.

Until this is decided the stack keeps running — but a hardware failure or an expired
credential has **no second person to recover it.**

---

## 1. What this system actually does (in business terms)

It automates reporting and CRM-sync that TryBooking UK used to do by hand:

- **A live internal dashboard** (`https://trybooking.internal`) — the Customer Success
  and Marketing teams' day-to-day tool (retention worklists, revenue tiers, PPC). Login
  is via Microsoft (Entra) accounts.
- **Nightly CRM sync to Zoho** — account industry, value tiers, and a retention-priority
  score, computed from booking data and pushed into Zoho CRM each weeknight.
- **Weekly email reports** to stakeholders.
- **A SharePoint data feed** for the wider business.

If this stack stops, the symptoms are: the dashboard goes stale or down, Zoho stops
updating, the weekly emails stop. **None of it touches live booking or payment
processing** — TryBooking's core platform is separate. So an outage is an
internal-visibility / CRM-freshness problem, not customer-facing or revenue-affecting.

---

## 2. Where it physically lives (the bus-factor section — read carefully)

Everything is on **one Raspberry Pi** ("TrybookingPi", on the office network, also
reachable via Tailscale) with an **external SSD** holding the data warehouses. Two
GitHub repos under the `trybookinguk` org hold the code; the Pi runs it.

**The critical fact:** the *secrets* that make it all work are **not in GitHub.** They
live only on the Pi, in two gitignored files:

| File on the Pi | Holds |
| --- | --- |
| `/root/s3reporting/.env` | AWS, Azure/SharePoint, GA4, Zoho, Mailshake keys |
| `/root/reporting-dashboard/ecosystem.config.cjs` | Azure, Zoho, Mailgun, DB paths for the dashboard |

If the SSD or the Pi dies, **these are gone** unless backed up elsewhere. The code
survives (it's on GitHub); the credentials do not.

**There is now an automated nightly backup to SharePoint.** A cron job (04:00 daily,
`backup_to_sharepoint.py`) copies the two secret files, the dashboard state DBs, and
the warehouses into SharePoint under `Backups/pi/<date>/`, keeping the last 7 dated
copies. It reuses the same Azure/SharePoint credentials as the existing export sync.

> 🔎 **Confirm the backup is running:**
> > `ssh root@<Pi-address> 'tail -5 /root/logs/backup-sharepoint.log'`
> A healthy run ends with `Backup complete: N uploaded`. If a nightly run fails you'll
> get the standard cron failure email. The backup folder is in the same SharePoint
> drive as the S3 exports — confirm you can see `Backups/pi/` there.

**To restore onto a fresh Pi/SSD** (`restore_from_sharepoint.py`): clone the two repos,
then run the script. It prompts for the three Azure values + the SharePoint drive ID
(get them from your secret store — see below), lists available backups, and pulls the
chosen one back into place. This solves the chicken-and-egg problem: the secrets are in
the backup, but you need *just the Azure credentials* in hand to reach it.

> **The secrets are no longer single-SSD.** They are in GitHub-adjacent SharePoint
> nightly. But the **Azure bootstrap credentials still need to live somewhere off the
> Pi** — without them you can't reach the backup. ‼️ Put `AZURE_TENANT_ID`,
> `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `SHAREPOINT_DRIVE_ID` in the company
> password manager. That four-value card is now your true disaster-recovery key.

> 🔎 **Find out — who can get in?**
> The SSH private key is what grants root on the Pi. ‼️ Ask the outgoing operator where
> a copy of that key lives and how you obtain it — without it you have no access. Also:
> > 🔎 which Tailscale account owns the Pi node, and who can add your device to it? ‼️

> **Recommended first action regardless of the §0 decision:** copy `.env`,
> `ecosystem.config.cjs`, and the SSH key into the company password manager / secret
> store today. Ten minutes; removes the single worst failure mode. An AI can fetch the
> file contents for you to paste in:
> > `ssh root@<Pi-address> 'cat /root/s3reporting/.env'`

---

## 3. Vendor & account ownership map

The stack depends on these external accounts. When one emails "card expired / token
revoked / cert expiring", this is who owns it and how it renews.

> 🔎 **Find out — what secrets exist and what they're named** (values redacted):
> > `ssh root@<Pi-address> 'sed -E "s/=.*/=<set>/" /root/s3reporting/.env'`
> This prints the variable *names* so you can see exactly what the stack depends on
> without exposing values. Cross-reference against the table below.

| Service | Used for | Account owner / login | Renewal / expiry |
| --- | --- | --- | --- |
| **AWS** | Source booking-data S3 bucket | ‼️ | Access keys; rotate manually |
| **Zoho CRM** | Tier/industry/retention sync | ‼️ | OAuth refresh token — see ⚠️ |
| **Microsoft / Azure (Entra)** | Dashboard login + SharePoint sync | ‼️ | Client secret — **expires** ⚠️ |
| **Mailgun** | Sending report emails | ‼️ | SMTP password |
| **Mailshake** | Lead/outreach | ‼️ | API key |
| **GA4 (Google)** | PPC reporting on dashboard | ‼️ | Service-account JSON key |
| **Tailscale** | Off-network access to the Pi | ‼️ | Device auth |
| **Domain / TLS** (`trybooking.internal`) | Dashboard URL | n/a | Local cert via Caddy (auto-renews) |

> ⚠️ **Two time-bombs to diarise now:**
> - **Azure client secret** — Entra app secrets expire on a fixed date (commonly 1–2
>   years). When it does, **dashboard login *and* the SharePoint sync break together**,
>   with little warning. ‼️ Get the current secret's expiry date from the outgoing
>   operator (or read it in the Azure portal: Entra → App registrations → your app →
>   Certificates & secrets) and put it in your calendar.
> - **Zoho refresh token** — long-lived but revocable; if revoked, Zoho sync fails and
>   you'll get the nightly failure email. The token is deliberately *narrow-scoped* (it
>   lacks `ZohoCRM.users.READ`), so don't be surprised it can't do everything in Zoho.

---

## 4. What "keeping it alive" actually costs you

**Time, if you operate it yourself:**
- **Normal weeks:** near zero. The cron pipeline self-heals and emails you *only* on
  failure. Reading those emails is the whole job.
- **When something breaks:** §7 of the engineer handover is a symptom→fix runbook; most
  incidents are a 5-minute "SSH in and restart" — which you can have an AI do for you.
  The real cost is the *rare* incident not in the runbook (expired secret, dead SSD) —
  minutes if you did §7's backup, a rebuild-from-scratch if you didn't.
- ‼️ **Honest hours/month** the outgoing operator actually spent — ask them. This single
  number is the best input to the keep-vs-delegate-vs-migrate call.

> 🔎 **Find out — running costs.** ‼️ The main money item is the AWS bill (S3 reads) plus
> any paid tiers of Zoho / Mailgun / Mailshake / Tailscale, and who they're billed to.
> Ask the outgoing operator or check each vendor's billing page.

---

## 5. If you decide to migrate off the Pi

The shape of the work, so you can scope it:

- The **code** is portable (two GitHub repos, standard Python + a SvelteKit app).
- The **hard part** isn't the app — it's the ~3.7 GB SQLite warehouse build and the
  staggered nightly cron, which rely on the Pi's local SSD and a one-time heavy data
  seed. A cloud VM is a straightforward lift-and-shift; going fully serverless is a
  re-architecture.
- This system **used to run on GitHub Actions** and was deliberately moved to the Pi
  (cost/runtime). Reversing it is a known path — the history is in git.
- **Lowest-effort de-risking short of a full migration:** a cheap cloud VM (or even
  another Pi) running the same cron, plus a nightly off-Pi backup of the two secret
  files and the warehouse. Keeps the architecture, removes the single-SSD risk.

---

## 6. Known documentation drift — verify against the live machine

The engineer docs are good but have **already drifted** in places, which is the reason
this section exists: trust the *running machine*, not every markdown file.

- **`s3reporting/deploy/README.md` is stale.** It lists a `generate_dashboard_data.py`
  job at 03:15 uploading "dashboard JSON to SharePoint." That job was **deliberately
  removed** (git commit `8e8b20f`, *"drop generate_dashboard_data.py from crontab"*); the
  dashboard now reads the DuckDB warehouse directly. Trust the live crontab
  (`deploy/pi-crontab`) over that README.
- **A cache-warm token is committed in plaintext** in `deploy/pi-crontab`
  (`WARMURL=...?token=...`). Low severity — it only triggers a localhost cache refresh —
  but it *is* a secret in git history, so don't treat the repo as secret-free.
- **GitHub Actions isn't fully retired.** There are still ~14 workflow files in
  `s3reporting/.github/workflows/` (monthly reports, PPC, regional analysis, etc.). Some
  reporting may *still* run on Actions in parallel with the Pi.
  > 🔎 **Find out:** in the GitHub repo → **Actions** tab, look at which workflows have
  > run recently. Anything with runs in the last month is live and is a *second* place
  > reporting happens (with its own secrets, stored in GitHub repo settings, not on the
  > Pi). Ask your AI to summarise `.github/workflows/*.yml` schedules for you.

---

## 7. Your first hour (whichever path you choose)

You can have an AI assistant run each of these and explain the output.

1. **Save the disaster-recovery key** (§2) — the four-value Azure bootstrap card
   (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `SHAREPOINT_DRIVE_ID`)
   plus the SSH private key → company secret store. The nightly job already backs up the
   *rest* to SharePoint; these are the keys that get you *into* that backup. Before
   anything else.
2. **Confirm you can get in:**
   > `ssh root@<Pi-address> 'pm2 list && crontab -l'`
   If you can't connect, you have an access problem to solve first (§2 SSH-key ‼️).
3. **Confirm it's healthy:** the dashboard URL loads (a Microsoft-login redirect =
   healthy, *not* an error), and the latest nightly log shows success:
   > `ssh root@<Pi-address> 'tail -5 /root/logs/prepare-data.log'`
4. **Diarise the Azure secret expiry** (§3) once you have the date.
5. **Check whether Actions is still doing work** (§6) so you know all the places
   reporting runs.
6. **Make the §0 decision** — operate / delegate / migrate — and write it at the top of
   this file so the next person isn't asking the same question.

---

## 8. Where to go deeper

- **Pi / Dashboard Handover prompt** — the engineer's manual: full machine topology,
  pm2 / Caddy / cron, the atomic deploy script, and a symptom→fix break-glass runbook.
  Excellent; use it for actual operations. (You can paste it to an AI assistant as
  context and it'll act on it.)
- **`s3reporting/deploy/README.md`** — pipeline scripts and manual-run flags (mind §6).
- **`s3reporting/CLAUDE.md`** — conventions (British spelling; the booking data is
  regulated, so the rule is *hand pipeline commands to a human, don't run them blind*).
- **The two repos**, both `trybookinguk` org, branch `main`: `reporting-dashboard` (UI)
  and `s3reporting` (ETL).
