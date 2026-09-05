# s3reporting pipeline image for Synology DSM (Container Manager / Docker).
# Runs the reporting pipeline only (not the Node dashboard). x86-64 Synology.
FROM python:3.12-slim

# System libraries: weasyprint (commission PDF) needs pango/cairo/gdk-pixbuf;
# unzip/curl to fetch the duckdb CLI; tzdata so BST/UTC logic is correct.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libcairo2 libffi8 libjpeg62-turbo shared-mime-info fonts-dejavu-core \
        curl unzip tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/London

# DuckDB CLI — the materialise shells out to this binary (not the pip package).
# amd64 build for Intel Synology. Pin a version for reproducibility / to match
# the dashboard's @duckdb/node-api reader; bump deliberately.
ARG DUCKDB_VERSION=1.1.3
RUN curl -fsSL -o /tmp/duckdb.zip \
        "https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb_cli-linux-amd64.zip" \
    && unzip /tmp/duckdb.zip -d /usr/local/bin && rm /tmp/duckdb.zip \
    && chmod +x /usr/local/bin/duckdb && duckdb --version
ENV DUCKDB_BIN=/usr/local/bin/duckdb

# Python deps — same set the Pi uses, plus cryptography (backup) and weasyprint.
RUN pip install --no-cache-dir \
        boto3 msal requests pandas pytz numpy scipy python-dateutil \
        google-analytics-data matplotlib weasyprint cryptography

WORKDIR /app
# The repo is bind-mounted at /app by docker-compose (so `git pull` on the NAS
# updates it without rebuilding the image). Nothing is COPYied in.

# Persistent data lives on a mounted NAS volume at /data (see compose + .env):
#   S3_CACHE_DIR=/data/.cache  DATA_DIR=/data/.cache/prepared
#   REPORTS_DIR=/data/reports  SYNC_MANIFEST_DIR=/data/.sync_manifest
# Keep the container alive so DSM Task Scheduler can `docker exec` into it.
CMD ["sleep", "infinity"]
