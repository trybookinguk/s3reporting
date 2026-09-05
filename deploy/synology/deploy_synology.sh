#!/usr/bin/env bash
#
# deploy_synology.sh — build/refresh the s3reporting container on a Synology NAS.
#
# Run over SSH on the NAS (Control Panel -> Terminal & SNMP -> Enable SSH),
# as a user in the administrators group, from this directory:
#   cd /volume1/docker/s3reporting/deploy/synology && sudo ./deploy_synology.sh
#
# It checks arch/Docker, ensures the data dir + .env exist, builds the image,
# and (re)starts the container. Idempotent — safe to re-run after a `git pull`.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/volume1/docker/s3reporting}"
DATA_DIR_HOST="${DATA_DIR_HOST:-/volume1/docker/s3reporting-data}"
COMPOSE_DIR="$REPO_DIR/deploy/synology"

say() { echo ">> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# --- 1. Sanity checks -------------------------------------------------------
arch="$(uname -m)"
say "NAS architecture: $arch"
case "$arch" in
  x86_64|amd64) : ;;
  *) die "This deployment targets Intel/x86-64 Synology models (Docker/Container Manager).
        Your NAS reports '$arch' — Docker isn't available on ARM value models.
        See the README for the alternatives." ;;
esac

# Locate docker + a compose command (DSM ships 'docker compose' or 'docker-compose').
command -v docker >/dev/null || die "docker not found. Install 'Container Manager' (DSM 7.2) or 'Docker' (DSM 7.0/7.1) from Package Center."
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "docker compose not found — update Container Manager, or run the docker build/run by hand (see README)."
fi
say "Using compose: ${COMPOSE[*]}"

# --- 2. Ensure paths + .env -------------------------------------------------
[ -d "$REPO_DIR" ] || die "$REPO_DIR not found — clone the repo there first (see README step 2)."
mkdir -p "$DATA_DIR_HOST/.cache/prepared" "$DATA_DIR_HOST/reports" "$DATA_DIR_HOST/.sync_manifest" "$DATA_DIR_HOST/logs"

if [ ! -f "$REPO_DIR/.env" ]; then
  die "$REPO_DIR/.env is missing. Create it (chmod 600) with the credentials AND the
        container data paths:
          S3_CACHE_DIR=/data/.cache
          DATA_DIR=/data/.cache/prepared
          REPORTS_DIR=/data/reports
          SYNC_MANIFEST_DIR=/data/.sync_manifest
          DUCKDB_BIN=/usr/local/bin/duckdb
        See the repo's blank .env template and deploy/synology/README.md."
fi
chmod 600 "$REPO_DIR/.env" || true

# --- 3. Build + (re)start ---------------------------------------------------
cd "$COMPOSE_DIR"
say "Building image (this pulls Python deps + duckdb; first run takes a few minutes) ..."
"${COMPOSE[@]}" build
say "Starting container ..."
"${COMPOSE[@]}" up -d

# --- 4. Smoke test ----------------------------------------------------------
say "Verifying tools inside the container:"
docker exec s3reporting bash -lc 'python3 --version && duckdb --version && echo "env paths:" && env | grep -E "^(S3_CACHE_DIR|DATA_DIR|DUCKDB_BIN)=" || true'

cat <<EOF

Done. Next:
  1. Verify credentials:   docker exec s3reporting python3 test_secrets.py
  2. Safe pipeline preview: docker exec s3reporting ./deploy/run_daily.sh --test
  3. Schedule the live run in DSM Task Scheduler (see deploy/synology/README.md):
       docker exec s3reporting ./deploy/run_daily.sh
EOF
