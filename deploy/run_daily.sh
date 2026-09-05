#!/usr/bin/env bash
#
# run_daily.sh — run the daily reporting pipeline by hand, in the same order as
# cron (deploy/pi-crontab), as one command. Handy after a rebuild or to catch up
# a missed night.
#
# Usage:
#   ./deploy/run_daily.sh              # live run (writes to Zoho, emails the team)
#   ./deploy/run_daily.sh --test       # safe preview: TEST_MODE=1 + --dry-run on
#                                       # the steps that support it (no Zoho writes,
#                                       # emails redirected, no SharePoint upload)
#   ./deploy/run_daily.sh --no-backup  # skip the 04:00 backup step
#
# prepare_data.py runs first and MUST succeed — every downstream job reuses the
# cache/warehouse it builds, so if it fails the script aborts. The remaining
# steps run in order; a failure in one is recorded but doesn't stop the rest,
# and the script exits non-zero if anything failed.
set -uo pipefail

REPO="${S3REPORTING_DIR:-/root/s3reporting}"
ENV_FILE="$REPO/.env"
LOG_DIR="${LOG_DIR:-/root/logs}"

TEST=0
DO_BACKUP=1
for a in "$@"; do
  case "$a" in
    --test) TEST=1 ;;
    --no-backup) DO_BACKUP=0 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }
mkdir -p "$LOG_DIR"
cd "$REPO" || exit 1
set -a; . "$ENV_FILE"; set +a
if [ "$TEST" = "1" ]; then
  export TEST_MODE=1
  echo ">> TEST MODE: TEST_MODE=1, dry-run where supported — no live writes/emails."
fi

declare -a NAMES STATUSES
run() {           # run <name> <logfile> -- <cmd...>
  local name="$1" logf="$2"; shift 3   # drop name, logf, and the literal --
  echo ">> [$(date '+%H:%M:%S')] $name ..."
  local t0=$SECONDS
  if "$@" >>"$LOG_DIR/$logf" 2>&1; then
    local dt=$((SECONDS - t0))
    echo "   ✓ $name (${dt}s)"
    NAMES+=("$name"); STATUSES+=("ok")
    return 0
  else
    local rc=$? dt=$((SECONDS - t0))
    echo "   ✗ $name FAILED (rc=$rc, ${dt}s) — see $LOG_DIR/$logf"
    NAMES+=("$name"); STATUSES+=("FAIL(rc=$rc)")
    return $rc
  fi
}

# dry-run flags only in --test mode, and only for scripts that accept them.
DRY=(); [ "$TEST" = "1" ] && DRY=(--dry-run)

# 1) prepare_data — must succeed; everything else reuses its cache.
if ! run "prepare-data" "prepare-data.log" -- python3 prepare_data.py; then
  echo "ABORT: prepare_data.py failed — downstream jobs would use a stale/empty cache." >&2
  exit 1
fi
# warm the dashboard caches, like cron does (best-effort)
curl -fsS "http://127.0.0.1:3000/api/warm?token=xU2tGU1zJUmZya1IH2GiNmKrBIre8p" -o /dev/null 2>/dev/null || true

# 2) downstream jobs (order matches cron). Failures are recorded, not fatal.
run "s3-sharepoint" "s3-sharepoint.log" -- python3 s3_to_sharepoint.py "${DRY[@]}"
run "zoho-industry" "zoho-industry.log" -- python3 zoho_industry.py
run "zoho-tiers"    "zoho-tiers.log"    -- python3 zoho_tiers.py "${DRY[@]}"
run "users-accounts" "users-accounts.log" -- python3 users_accounts_report.py

# 3) re-materialise after zoho_tiers so retention priority is same-day.
run "prepare-data-mat" "prepare-data.log" -- python3 prepare_data.py --materialise-only
curl -fsS "http://127.0.0.1:3000/api/warm?token=xU2tGU1zJUmZya1IH2GiNmKrBIre8p" -o /dev/null 2>/dev/null || true

# 4) nightly backup (skip with --no-backup; skipped in --test to avoid churn).
if [ "$DO_BACKUP" = "1" ] && [ "$TEST" != "1" ]; then
  run "backup-sharepoint" "backup-sharepoint.log" -- python3 backup_to_sharepoint.py
fi

# --- summary ---------------------------------------------------------------
echo
echo "==== daily run summary ===="
fail=0
for i in "${!NAMES[@]}"; do
  printf "  %-18s %s\n" "${NAMES[$i]}" "${STATUSES[$i]}"
  [[ "${STATUSES[$i]}" == FAIL* ]] && fail=1
done
[ "$fail" = "0" ] && echo "  all steps OK" || echo "  one or more steps FAILED — check the logs above."
exit $fail
