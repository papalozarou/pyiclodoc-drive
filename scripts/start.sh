#!/bin/sh
# ------------------------------------------------------------------------------
# This launcher starts the Python worker for a named container user.
# ------------------------------------------------------------------------------

set -eu

CONTAINER_USERNAME="${1:-icloudbot}"
export CONTAINER_USERNAME

exec python3 -m app.main
