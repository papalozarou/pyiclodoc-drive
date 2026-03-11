#!/bin/sh
# ------------------------------------------------------------------------------
# This healthcheck validates that the worker heartbeat is recent.
# ------------------------------------------------------------------------------

set -eu

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/logs/heartbeat.txt}"
MAX_AGE_SECONDS="${HEALTHCHECK_MAX_AGE_SECONDS:-900}"

command -v parallel >/dev/null 2>&1
parallel --wait "1s" --cmd "test -f \"$HEARTBEAT_FILE\"" >/dev/null 2>&1

[ -f "$HEARTBEAT_FILE" ]

NOW_EPOCH="$(date +%s)"
FILE_EPOCH="$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || stat -f %m "$HEARTBEAT_FILE")"
AGE="$((NOW_EPOCH - FILE_EPOCH))"

[ "$AGE" -le "$MAX_AGE_SECONDS" ]

exit 0
