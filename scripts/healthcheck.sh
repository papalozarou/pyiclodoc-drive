#!/bin/sh
# ------------------------------------------------------------------------------
# This healthcheck validates that the worker heartbeat is recent.
# ------------------------------------------------------------------------------

set -eu

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/logs/heartbeat.txt}"
MAX_AGE_SECONDS="${HEALTHCHECK_MAX_AGE_SECONDS:-900}"

if ! command -v microcheck >/dev/null 2>&1; then
  exit 1
fi

microcheck --insecure --wait "1s" --cmd "test -f $HEARTBEAT_FILE"

if [ ! -f "$HEARTBEAT_FILE" ]; then
  exit 1
fi

NOW_EPOCH="$(date +%s)"
FILE_EPOCH="$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || stat -f %m "$HEARTBEAT_FILE")"
AGE="$((NOW_EPOCH - FILE_EPOCH))"

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
  exit 1
fi

exit 0
