# Re-authenticating the Pi to GitHub

**Category:** Operations / Maintenance
**Schedule:** Run manually when the Pi can no longer pull/push (expired token, rotated credentials, "Repository not found")

## What it does

Restores the Pi's ability to `git pull` / `git push` the two private repos in the
**`trybookinguk`** GitHub org (`s3reporting`, `reporting-dashboard`). The Pi runs
git-tracked code, so if its GitHub auth lapses, deploys (pull-on-Pi) stop working.

This is separate from a workstation's GitHub setup — see
[Changing Credentials on the Pi](changing_credentials.md) for the workstation
side and for how to reach the Pi in the first place.

## Get onto the Pi

Access is via Tailscale SSH (no local key SSH):

```bash
ssh root@trybookingpi.tail1fb257.ts.net
cd /root/s3reporting
```

## Step 1 — Diagnose the auth method

This decides which path to follow. Check the remote URL scheme:

```bash
git remote -v | head -1
git config --get credential.helper
command -v gh && gh --version
```

- Remote starts with **`https://github.com/...`** -> token-based -> **Path A**.
- Remote starts with **`git@github.com:...`** -> SSH deploy key -> **Path B**.

## Path A - HTTPS (token-based)

### If `gh` (GitHub CLI) is installed (preferred)

```bash
gh auth login
#   -> GitHub.com  ->  HTTPS  ->  "Login with a web browser" (or paste a token)
gh auth setup-git        # make git use the gh credential for HTTPS
```

On a headless Pi the browser flow shows a one-time code to enter at
github.com/login/device from any device. The account you log in as must be a
member of the `trybookinguk` org with access to both repos.

### If `gh` is NOT installed - use a Personal Access Token

1. On github.com, as a `trybookinguk` org member:
   **Settings -> Developer settings -> Personal access tokens -> Fine-grained
   token**. Scope it to `trybookinguk/s3reporting` and
   `trybookinguk/reporting-dashboard`, permission **Contents: Read and write**.
2. On the Pi:
   ```bash
   git config --global credential.helper store   # persist to ~/.git-credentials
   git pull
   #   username: your GitHub username
   #   password: the TOKEN (not your account password)
   ```
   The token is cached for future pull/push.

## Path B - SSH deploy key

If the remote is `git@github.com:...`, the Pi authenticates with an SSH key whose
public half is registered as a deploy key on the repo. To re-key:

```bash
ssh-keygen -t ed25519 -C "trybookingpi-deploy" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub        # copy this
```

Then on github.com: **repo -> Settings -> Deploy keys -> Add deploy key**, paste
the public key, tick **Allow write access**. Point SSH at it in `~/.ssh/config`:

```
Host github.com
    IdentityFile ~/.ssh/github_deploy
```

A deploy key is per-repo, so repeat for `reporting-dashboard` (a separate key, or
reuse one key only if GitHub lets you — separate keys are cleaner).

## Step 2 — Verify both repos

Auth can differ per repo, so check both:

```bash
cd /root/s3reporting        && git fetch origin && echo "s3reporting OK"
cd /root/reporting-dashboard && git fetch origin && echo "dashboard OK"
```

## Technical notes

- **"Repository not found" almost never means the repo is missing.** Over HTTPS,
  GitHub returns 404 (not 403) for a private repo when the token/account can't
  see it. So a 404 = wrong/expired token or wrong account, not a deleted repo.
  Re-check with `gh auth status` or redo the token step.
- **The Pi pulls; it rarely pushes.** Normal flow is: edit + commit + push from a
  workstation, then `git pull` on the Pi. Read access is enough for that; only
  give the Pi write access if something genuinely commits from the Pi.
- **Don't store a token in a repo file or in `.env`.** Use the credential helper
  (`~/.git-credentials`, root-only) or `gh`'s own store. Either is gitignored by
  living outside the repo.
