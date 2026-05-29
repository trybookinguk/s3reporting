#!/bin/bash
# cron_wrapper.sh — run a command, capture output to a log, email on failure.
#
# Usage from crontab:
#   cron_wrapper.sh <job-name> <log-file> -- <command...>
#
# Example:
#   /root/s3reporting/scripts/cron_wrapper.sh prepare-data /root/logs/prepare-data.log -- \
#       bash -c 'set -a && source /root/s3reporting/.env && set +a && cd /root/s3reporting && python3 prepare_data.py'
#
# On non-zero exit, runs cron_notify.py to email a failure report. The notifier
# itself logs to <log-file>.notify so a broken notifier is debuggable.

set -u

if [ "$#" -lt 4 ] || [ "$3" != "--" ]; then
    echo "Usage: $0 <job-name> <log-file> -- <command...>" >&2
    exit 64
fi

JOB_NAME="$1"
LOG_FILE="$2"
shift 3  # drop job-name, log-file, --

mkdir -p "$(dirname "$LOG_FILE")"

START_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "===== $JOB_NAME started at $START_TS =====" >> "$LOG_FILE"
"$@" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
END_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "===== $JOB_NAME finished at $END_TS (exit $EXIT_CODE) =====" >> "$LOG_FILE"

if [ "$EXIT_CODE" -ne 0 ]; then
    # Source the .env so the notifier inherits Azure credentials.
    set -a
    # shellcheck disable=SC1091
    [ -f /root/s3reporting/.env ] && . /root/s3reporting/.env
    set +a

    /usr/bin/python3 /root/s3reporting/scripts/cron_notify.py \
        --job "$JOB_NAME" \
        --exit-code "$EXIT_CODE" \
        --log-file "$LOG_FILE" \
        >> "${LOG_FILE}.notify" 2>&1 || true
fi

exit "$EXIT_CODE"
