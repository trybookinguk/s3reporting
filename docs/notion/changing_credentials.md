# Changing Credentials on the Pi

**Category:** Operations / Maintenance
**Schedule:** Run manually when a credential is rotated or a new one is added

## What it does

Explains how to change API credentials (Zoho, Azure/Graph, AWS, etc.) for the
two services that run on the Raspberry Pi. There is no shared secret store and
no `.env` in git — each service reads its own file on the Pi, so a credential
usually has to be updated in **both** places.

## Reaching the Pi

Access is via **Tailscale SSH** — we no longer connect with local SSH keys
(`~/.ssh` keypairs / `authorized_keys`). Tailscale SSH authenticates by your
**Tailscale identity**, not a key file, so there is nothing to copy onto the Pi
or onto your laptop. The Pi is `TrybookingPi`, you log in as `root`.

To connect:

1. Be signed in to the **`trybooking.co.uk`** tailnet with Tailscale running on
   your machine.
2. SSH to the Pi by its MagicDNS name — Tailscale intercepts it:

   ```bash
   ssh root@trybookingpi.tail1fb257.ts.net
   ```

   (The tailnet IP `100.79.35.128` also works, but prefer the name — the IP can
   change.) You may be asked to re-authenticate in the browser ("check mode").

### Granting / removing access (admin console only)

Who can Tailscale-SSH into the Pi is controlled by the tailnet **SSH access
rules** (Admin console → **Access Controls** → the `ssh` section) — NOT by
anything on the Pi. The Pi is tagged **`tag:server`**, so access is granted by
an explicit rule targeting that tag:

```jsonc
{
  "action": "check",                  // browser re-auth; use "accept" to skip
  "src":   ["autogroup:member"],      // or specific users e.g. "henry@trybooking.co.uk"
  "dst":   ["tag:server"],            // the Pi (and any other tag:server node)
  "users": ["root"]
}
```

- **Add a person:** add their user to `src` (or rely on `autogroup:member`),
  save the policy. No Pi access needed — it takes effect immediately.
- **Remove a leaver:** drop them from `src` (and remove their device from the
  tailnet). Again, console-only.
- **Note:** because the Pi is tagged, it's owned by `tag:server`, not by a
  person — so the default `dst: autogroup:self` ("SSH into your own devices")
  rule does **not** apply to it. The explicit `tag:server` rule above is what
  grants access; don't rely on the self rule for the Pi.

> Direct key-based SSH (`ssh root@192.168.0.55` with an `~/.ssh` key) is no
> longer the access path. If Tailscale SSH is ever misconfigured and locks
> everyone out, recovery is via physical/console access to the Pi.

## GitHub access

Both repos are **private**, under the **`trybookinguk`** GitHub org:

- `trybookinguk/s3reporting`
- `trybookinguk/reporting-dashboard`

Code changes follow: edit → commit → push → `git pull` on the Pi (the Pi runs
git-tracked code; see the deploy steps in [PI Handover](PI_HANDOVER.md)). For
that you need GitHub auth that can read/write the org repos.

### Set up GitHub auth (on your workstation)

Use the GitHub CLI — it manages the credential and wires up git:

```bash
gh auth login          # choose GitHub.com → HTTPS → authenticate in browser
gh auth setup-git      # makes git use the gh credential for HTTPS pushes
```

Make sure the account you log in as is a **member of the `trybookinguk` org**
with access to both repos (ask an org owner to add them).

### "Repository not found" on push/pull

This usually means git is using the **wrong GitHub account**, not that the repo
is missing — GitHub returns 404 (not 403) for a private repo when the
authenticated user can't see it. Check and switch:

```bash
gh auth status               # shows which account(s) are logged in + active
gh auth switch               # switch the active account (e.g. to the trybookinguk-org one)
```

(During the June 2026 credential work a push failed exactly this way until
`gh auth switch` selected the org-member account.)

### On the Pi

The Pi already has working git credentials for both repos under `/root`. A new
operator normally doesn't touch GitHub auth on the Pi — they pull/push from
their own workstation and the Pi just `git pull`s. If the Pi's git auth ever
needs renewing, run `gh auth login` (or refresh the stored token) while
connected to the Pi via Tailscale SSH.

## Where credentials live

| Service | File on the Pi | Format | Reload needed? |
|---|---|---|---|
| **s3reporting** (Python ETL / cron jobs) | `/root/s3reporting/.env` | `export KEY=value` (shell) | No — each cron run re-sources it |
| **reporting-dashboard** (SvelteKit, pm2 app `trybooking`) | `/root/reporting-dashboard/ecosystem.config.cjs` | JS object: `KEY: 'value',` | **Yes** — pm2 caches env |

Both files are **gitignored / not in git** and contain live secrets in plaintext.
They are backed up nightly to SharePoint by the `backup-sharepoint` job (see
[Nightly Backup](nightly_backup.md)) — so don't delete a credential file without
a replacement ready.

> **The SharePoint backup is now encrypted at rest** (all files, not just the
> credential files). It is NOT a copy you can read by just downloading it — you
> need the backup key to decrypt it. See **The backup encryption key** below.

### Credentials stored in their own files (not inline)

A few secrets are too large or awkward to inline, so `.env` loads them from a
dedicated root-only file under `/root/secrets/` via `$(cat ...)`:

| Env var | File | Notes |
|---|---|---|
| `GA4_SERVICE_ACCOUNT_KEY` | `/root/secrets/ga4_service_account_key.json` | Google service-account JSON. To rotate: replace the file, no `.env` edit needed. |
| `BACKUP_SECRET_PASSPHRASE` | `/root/secrets/backup.key` | The backup encryption key — see below. |

To change one of these, replace the **file** (keep it `chmod 600`); `.env`
re-reads it on the next run. The dashboard does not use these.

## Before you start

- You need SSH access to the Pi (`root@192.168.0.55`).
- Know which keys you're changing. The same credential often appears in both
  files under the **same key name** (e.g. `ZOHO_CLIENT_ID`,
  `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `AZURE_CLIENT_SECRET`).
- **Don't revoke the old credential until the new one is verified working**
  (see step 4). Keep the old one live during the cutover.

## How to change a credential

### 1. Back up the file first

```bash
ssh root@192.168.0.55
cp /root/s3reporting/.env /root/s3reporting/.env.bak.$(date +%Y%m%d-%H%M%S)
cp /root/reporting-dashboard/ecosystem.config.cjs \
   /root/reporting-dashboard/ecosystem.config.cjs.bak.$(date +%Y%m%d-%H%M%S)
```

### 2. Edit s3reporting `.env`

Edit `/root/s3reporting/.env` (e.g. `nano /root/s3reporting/.env`). Lines look
like:

```
  export ZOHO_REFRESH_TOKEN=1000.xxxxxxxx.yyyyyyyy
```

Change the value after the `=`. Keep the `export ` prefix and the indentation.
No quotes needed.

### 3. Edit the dashboard `ecosystem.config.cjs`

Edit `/root/reporting-dashboard/ecosystem.config.cjs`. Lines look like:

```
      ZOHO_REFRESH_TOKEN: '1000.xxxxxxxx.yyyyyyyy',
```

Change the value **inside the single quotes**. Keep the quotes and the trailing
comma. Then reload pm2 so the new value is actually picked up:

```bash
cd /root/reporting-dashboard && pm2 restart trybooking --update-env
```

> `--update-env` is required. A plain `pm2 restart` reuses the env from the last
> start and your change will appear to have no effect.

### 4. Verify before revoking the old credential

- **Dashboard:** `pm2 logs trybooking` and watch for auth errors
  (e.g. `OAUTH_SCOPE_MISMATCH`, `401`, `invalid`) after the restart. Old log
  lines persist — `pm2 flush trybooking` first so you only see fresh errors.
- **s3reporting:** run the relevant job in test mode where possible, or its
  dedicated credential checker (e.g. [PPC Credential Check](ppc_credential_check.md)
  for AWS/GA4). For Zoho specifically, any read-path job exercises the token.

### 5. Revoke the old credential

Only once both services are confirmed working, revoke/delete the old credential
at the provider (Zoho API console, Azure app registration, AWS IAM, etc.).

### 6. Clean up

Once you're confident nothing needs rolling back, delete the `.bak.*` files from
step 1 — they contain the **old** secrets in plaintext.

## The backup encryption key

The nightly SharePoint backup encrypts every file with a single key held at
`/root/secrets/backup.key` (loaded into `.env` as `BACKUP_SECRET_PASSPHRASE`).
This key is special — treat it differently from every other credential here:

- **It is the only thing that decrypts the backups.** Every other secret can be
  recovered from the (encrypted) backup; the backup key cannot — it's what
  unlocks that backup. So a copy MUST live off the Pi, in the password manager.
  If the SSD dies and the password-manager copy is gone, every backup is
  permanently unrecoverable.
- **It is deliberately independent of the Azure/SharePoint credentials.** That
  means rotating Azure does NOT break old backups — but it also means losing
  this key isn't covered by anything else.
- **Rotating it invalidates old backups.** If you generate a new backup key, all
  previously-written backups become undecryptable with the new key. So only
  rotate it deliberately, and run a fresh full backup immediately after, and
  keep the old key until those old backups have aged out of retention.

To rotate the backup key (rarely needed — only if it's believed compromised):

```bash
ssh root@192.168.0.55
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /root/secrets/backup.key
chmod 600 /root/secrets/backup.key
cat /root/secrets/backup.key      # copy into the password manager NOW
# then run a full backup so a recoverable copy exists under the new key:
cd /root/s3reporting && set -a && source .env && set +a && python3 backup_to_sharepoint.py
```

Restoring from an encrypted backup ([Disaster Recovery Restore](disaster_recovery_restore.md))
prompts for this key — keep it to hand before you start a restore.

## Technical notes

- **Two different services, often one credential.** A change is usually needed
  in both files. Search both for the key name before assuming you're done:
  `grep -n KEY_NAME /root/s3reporting/.env /root/reporting-dashboard/ecosystem.config.cjs`
- **s3reporting needs no restart** — cron jobs `source` the `.env` fresh on each
  run. The dashboard **always** needs `pm2 restart ... --update-env`.
- **Zoho tokens are app-bound.** A Zoho refresh token only works with the
  `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` it was issued under. If you change the
  client app, you must change all three together. See
  [Monthly Commission Report](monthly_commission_report.md) for the scopes the
  Zoho token must carry (notably `ZohoCRM.users.READ`).
- **Don't commit secrets.** Neither credential file belongs in git. The off-Pi
  copy is the nightly SharePoint backup — but it's **encrypted**, so recovering
  from it needs the backup key (above). The backup key itself lives ONLY on the
  Pi and in the password manager, nowhere else.
