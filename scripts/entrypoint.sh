#!/bin/sh
# ------------------------------------------------------------------------------
# This entrypoint resolves secrets and starts the worker.
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

readSecretVar ICLOUD_EMAIL
readSecretVar ICLOUD_PASSWORD
readSecretVar TELEGRAM_BOT_TOKEN
readSecretVar TELEGRAM_CHAT_ID

CONTAINER_USERNAME="${CONTAINER_USERNAME:-icloudbot}"
TARGET_UID="${C_UID:-1000}"
TARGET_GID="${C_GID:-1000}"

if [ "$(id -u)" -ne 0 ]; then
  exec /app/scripts/start.sh "$CONTAINER_USERNAME"
fi

if ! command -v su-exec >/dev/null 2>&1; then
  echo "su-exec is required but not installed in the image." >&2
  exit 1
fi

exec su-exec "${TARGET_UID}:${TARGET_GID}" /app/scripts/start.sh "$CONTAINER_USERNAME"
