#!/bin/sh
# ------------------------------------------------------------------------------
# This entrypoint configures runtime identity and starts the worker.
# ------------------------------------------------------------------------------

set -eu

# ------------------------------------------------------------------------------
# This function reads a secret from a direct value or "_FILE" path.
#
# 1. "${1:?}" is the base variable name, for example "ICLOUD_EMAIL".
#
# N.B.
# "_FILE" convention aligns with container secret patterns:
# https://docs.docker.com/compose/how-tos/use-secrets/
#
# The function exits non-zero when a declared secret file is missing.
# ------------------------------------------------------------------------------
readSecretVar() {
  VAR_NAME="${1:?}"
  FILE_VAR_NAME="${VAR_NAME}_FILE"
  CURRENT_VALUE="$(eval "printf '%s' \"\${$VAR_NAME:-}\"")"
  FILE_PATH="$(eval "printf '%s' \"\${$FILE_VAR_NAME:-}\"")"

  if [ -n "$CURRENT_VALUE" ]; then
    return 0
  fi

  if [ -z "$FILE_PATH" ]; then
    return 0
  fi

  if [ ! -f "$FILE_PATH" ]; then
    echo "Secret file not found for $VAR_NAME: $FILE_PATH" >&2
    exit 1
  fi

  SECRET_VALUE="$(cat "$FILE_PATH")"
  export "$VAR_NAME=$SECRET_VALUE"
}

# ------------------------------------------------------------------------------
# This function creates or updates the runtime user and group.
#
# N.B.
# Runtime identity defaults to "1000:1000" and user "icloudbot".
# ------------------------------------------------------------------------------
configureUser() {
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  CONTAINER_USERNAME="${CONTAINER_USERNAME:-icloudbot}"

  addgroup -g "$PGID" -S "$CONTAINER_USERNAME" >/dev/null 2>&1 || true
  adduser -u "$PUID" -S -D -h "/home/$CONTAINER_USERNAME" -G "$CONTAINER_USERNAME" "$CONTAINER_USERNAME" >/dev/null 2>&1 || true

  mkdir -p /config /output /logs "/home/$CONTAINER_USERNAME"
  chown -R "$PUID:$PGID" /config /output /logs "/home/$CONTAINER_USERNAME"
}

readSecretVar ICLOUD_EMAIL
readSecretVar ICLOUD_PASSWORD
readSecretVar TELEGRAM_BOT_TOKEN
readSecretVar TELEGRAM_CHAT_ID

configureUser

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CONTAINER_USERNAME="${CONTAINER_USERNAME:-icloudbot}"

exec su-exec "$PUID:$PGID" /app/scripts/start.sh "$CONTAINER_USERNAME"
