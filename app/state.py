"""This module manages persisted runtime state such as manifests and authentication metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any

from app.time_utils import now_local_iso


@dataclass(frozen=True)
class AuthState:
    """This data class stores the latest offset-aware authentication timestamp and pending authentication state."""

    last_auth_utc: str
    auth_pending: bool
    reauth_pending: bool
    reminder_stage: str


# ------------------------------------------------------------------------------
# This function loads JSON content from disk with empty defaults.
# 1. `path` is the JSON file path to read.
# Returns: Parsed dictionary payload, or an empty dictionary when absent.
# ------------------------------------------------------------------------------
def read_json(PATH: Path) -> dict[str, Any]:
    if not PATH.exists():
        return {}

    with PATH.open("r", encoding="utf-8") as HANDLE:
        return json.load(HANDLE)


# ------------------------------------------------------------------------------
# This function writes JSON content atomically with a temp file.
# 1. `path` is the destination JSON file.
# 2. `payload` is the dictionary to persist.
# Returns: `None`.
# Notes: Atomic replace avoids partial writes during interruption.
# ------------------------------------------------------------------------------
def write_json(PATH: Path, PAYLOAD: dict[str, Any]) -> None:
    TEMPORARY_PATH = PATH.with_suffix(PATH.suffix + ".tmp")

    with TEMPORARY_PATH.open("w", encoding="utf-8") as HANDLE:
        json.dump(PAYLOAD, HANDLE, indent=2, sort_keys=True)

    TEMPORARY_PATH.replace(PATH)


# ------------------------------------------------------------------------------
# This function returns a configured-timezone ISO-8601 timestamp.
# Returns: Offset-aware ISO-8601 timestamp string.
# ------------------------------------------------------------------------------
def now_iso() -> str:
    return now_local_iso()


# ------------------------------------------------------------------------------
# This function loads persisted authentication state with robust defaults.
# 1. `path` is the JSON state file location.
# Returns: `AuthState` with default values when fields are missing.
# ------------------------------------------------------------------------------
def load_auth_state(PATH: Path) -> AuthState:
    PAYLOAD = read_json(PATH)
    DEFAULT_TIME = "1970-01-01T00:00:00+00:00"

    return AuthState(
        last_auth_utc=str(PAYLOAD.get("last_auth_utc", DEFAULT_TIME)),
        auth_pending=bool(PAYLOAD.get("auth_pending", False)),
        reauth_pending=bool(PAYLOAD.get("reauth_pending", False)),
        reminder_stage=str(PAYLOAD.get("reminder_stage", "none")),
    )


# ------------------------------------------------------------------------------
# This function persists authentication state to disk.
# 1. `path` is the JSON state file location; `state` is the model to persist.
# Returns: `None`.
# ------------------------------------------------------------------------------
def save_auth_state(PATH: Path, STATE: AuthState) -> None:
    PAYLOAD = {
        "last_auth_utc": STATE.last_auth_utc,
        "auth_pending": STATE.auth_pending,
        "reauth_pending": STATE.reauth_pending,
        "reminder_stage": STATE.reminder_stage,
    }
    write_json(PATH, PAYLOAD)


# ------------------------------------------------------------------------------
# This function loads a manifest that tracks remote file metadata by path.
# 1. `path` is the manifest file location.
# Returns: Mapping keyed by remote path for incremental diff checks.
# ------------------------------------------------------------------------------
def load_manifest(PATH: Path) -> dict[str, dict[str, Any]]:
    PAYLOAD = read_json(PATH)

    if not isinstance(PAYLOAD, dict):
        return {}

    return {
        str(KEY): VALUE for KEY, VALUE in PAYLOAD.items() if isinstance(VALUE, dict)
    }


# ------------------------------------------------------------------------------
# This function saves the manifest in stable ordering.
# 1. `path` is the manifest file location.
# 2. `manifest` is the payload to persist.
# Returns: `None`.
# ------------------------------------------------------------------------------
def save_manifest(PATH: Path, MANIFEST: dict[str, dict[str, Any]]) -> None:
    write_json(PATH, MANIFEST)
