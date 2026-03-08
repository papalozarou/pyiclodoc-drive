"""This module performs incremental iCloud Drive synchronisation with manifest and safety-net logic."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.icloud_client import ICloudDriveClient, RemoteEntry


@dataclass(frozen=True)
class SafetyNetResult:
    """This data class records first-run permission findings used to avoid destructive overwrites."""

    should_block: bool
    expected_mode: str
    mismatched_samples: list[str]


@dataclass(frozen=True)
class SyncResult:
    """This data class captures summary metrics for a backup run."""

    total_files: int
    transferred_files: int
    skipped_files: int
    error_files: int


# ------------------------------------------------------------------------------
# This function runs a first-time permission safety check.
#
# 1. "OUTPUT_DIR" is the backup destination root.
# 2. "SAMPLE_SIZE" is the max number of files to inspect.
#
# Returns: "SafetyNetResult" describing whether sync should be blocked and why.
#
# Notes: Permission mode bits are read from `stat` values:
# https://docs.python.org/3/library/os.html#os.stat_result
# ------------------------------------------------------------------------------
def run_first_time_safety_net(OUTPUT_DIR: Path, SAMPLE_SIZE: int) -> SafetyNetResult:
    LOCAL_FILES = collect_local_files(OUTPUT_DIR, SAMPLE_SIZE)

    if not LOCAL_FILES:
        return SafetyNetResult(False, "unknown", [])

    MODE_COUNTS = count_modes(LOCAL_FILES)
    EXPECTED_MODE = most_common_mode(MODE_COUNTS)
    MISMATCHES = collect_mismatches(LOCAL_FILES, EXPECTED_MODE)
    SHOULD_BLOCK = len(MISMATCHES) > 0

    return SafetyNetResult(SHOULD_BLOCK, EXPECTED_MODE, MISMATCHES)


# ------------------------------------------------------------------------------
# This function collects a bounded local-file sample for permission checks.
#
# 1. "OUTPUT_DIR" is the backup destination root.
# 2. "SAMPLE_SIZE" is the sample cap.
#
# Returns: Ordered file list up to "SAMPLE_SIZE" for mode analysis.
# ------------------------------------------------------------------------------
def collect_local_files(OUTPUT_DIR: Path, SAMPLE_SIZE: int) -> list[Path]:
    RESULT: list[Path] = []

    for PATH in OUTPUT_DIR.rglob("*"):
        if not PATH.is_file():
            continue

        RESULT.append(PATH)

        if len(RESULT) >= SAMPLE_SIZE:
            return RESULT

    return RESULT


# ------------------------------------------------------------------------------
# This function counts Unix mode values across sampled files.
#
# 1. "FILES" is the sampled file list.
#
# Returns: Frequency mapping keyed by octal mode string.
# ------------------------------------------------------------------------------
def count_modes(FILES: list[Path]) -> dict[str, int]:
    COUNTS: dict[str, int] = {}

    for PATH in FILES:
        MODE = oct(PATH.stat().st_mode & 0o777)
        COUNTS[MODE] = COUNTS.get(MODE, 0) + 1

    return COUNTS


# ------------------------------------------------------------------------------
# This function returns the most common mode or a safe default.
#
# 1. "MODE_COUNTS" is a mapping of mode strings to frequency counts.
#
# Returns: Most frequent mode string, defaulting to "0o644".
# ------------------------------------------------------------------------------
def most_common_mode(MODE_COUNTS: dict[str, int]) -> str:
    if not MODE_COUNTS:
        return "0o644"

    SORTED_MODES = sorted(MODE_COUNTS.items(), key=lambda ITEM: ITEM[1], reverse=True)
    return SORTED_MODES[0][0]


# ------------------------------------------------------------------------------
# This function returns sampled files with non-matching permissions.
#
# 1. "FILES" is the sampled file list.
# 2. "EXPECTED_MODE" is the baseline mode.
# 3. "LIMIT" caps mismatch output.
#
# Returns: Human-readable mismatch list for logs and Telegram alerts.
# ------------------------------------------------------------------------------
def collect_mismatches(FILES: list[Path], EXPECTED_MODE: str, LIMIT: int = 20) -> list[str]:
    MISMATCHES: list[str] = []

    for PATH in FILES:
        MODE = oct(PATH.stat().st_mode & 0o777)

        if MODE == EXPECTED_MODE:
            continue

        MISMATCHES.append(f"{PATH}: {MODE} (expected {EXPECTED_MODE})")

        if len(MISMATCHES) >= LIMIT:
            return MISMATCHES

    return MISMATCHES


# ------------------------------------------------------------------------------
# This function returns a deterministic metadata dictionary for a remote entry.
#
# 1. "ENTRY" is a remote file or directory record.
#
# Returns: Dictionary payload persisted in the incremental manifest.
# ------------------------------------------------------------------------------
def entry_metadata(ENTRY: RemoteEntry) -> dict[str, Any]:
    return {
        "is_dir": ENTRY.is_dir,
        "size": ENTRY.size,
        "modified": ENTRY.modified,
    }


# ------------------------------------------------------------------------------
# This function decides whether a file should be transferred.
#
# 1. "ENTRY" is current remote metadata.
# 2. "MANIFEST" is previous run metadata.
#
# Returns: True when transfer is required, otherwise False.
# ------------------------------------------------------------------------------
def needs_transfer(ENTRY: RemoteEntry, MANIFEST: dict[str, dict[str, Any]]) -> bool:
    EXISTING = MANIFEST.get(ENTRY.path)

    if EXISTING is None:
        return True

    if bool(EXISTING.get("is_dir", False)):
        return True

    if int(EXISTING.get("size", -1)) != ENTRY.size:
        return True

    if str(EXISTING.get("modified", "")) != ENTRY.modified:
        return True

    return False


# ------------------------------------------------------------------------------
# This function syncs drive contents incrementally and updates manifest data.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "OUTPUT_DIR" is local backup root.
# 3. "MANIFEST" is previous metadata.
#
# Returns: Tuple of sync summary metrics and a refreshed manifest mapping.
# ------------------------------------------------------------------------------
def perform_incremental_sync(
    CLIENT: ICloudDriveClient,
    OUTPUT_DIR: Path,
    MANIFEST: dict[str, dict[str, Any]],
) -> tuple[SyncResult, dict[str, dict[str, Any]]]:
    ENTRIES = CLIENT.list_entries()
    FILES = [ENTRY for ENTRY in ENTRIES if not ENTRY.is_dir]
    DIRECTORIES = [ENTRY for ENTRY in ENTRIES if ENTRY.is_dir]

    ensure_directories(OUTPUT_DIR, DIRECTORIES)
    NEW_MANIFEST: dict[str, dict[str, Any]] = {}

    TRANSFERRED = 0
    SKIPPED = 0
    ERRORS = 0

    for ENTRY in FILES:
        SHOULD_TRANSFER = needs_transfer(ENTRY, MANIFEST)
        SUCCESS = transfer_if_required(CLIENT, OUTPUT_DIR, ENTRY, SHOULD_TRANSFER)

        if SHOULD_TRANSFER and SUCCESS:
            TRANSFERRED += 1

        if SHOULD_TRANSFER and not SUCCESS:
            ERRORS += 1

        if not SHOULD_TRANSFER:
            SKIPPED += 1

        NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)

    for ENTRY in DIRECTORIES:
        NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)

    return SyncResult(len(FILES), TRANSFERRED, SKIPPED, ERRORS), NEW_MANIFEST


# ------------------------------------------------------------------------------
# This function ensures local directories exist before file downloads begin.
#
# 1. "OUTPUT_DIR" is local backup root.
# 2. "DIRECTORIES" are remote directory entries.
#
# Returns: None.
# ------------------------------------------------------------------------------
def ensure_directories(OUTPUT_DIR: Path, DIRECTORIES: list[RemoteEntry]) -> None:
    for ENTRY in DIRECTORIES:
        LOCAL_PATH = OUTPUT_DIR / ENTRY.path
        LOCAL_PATH.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------------------
# This function transfers a file only when required by manifest diffing.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "OUTPUT_DIR" is local backup root.
# 3. "ENTRY" is file metadata.
# 4. "SHOULD_TRANSFER" determines whether download should proceed.
#
# Returns: True when skipped or downloaded successfully, otherwise False.
# ------------------------------------------------------------------------------
def transfer_if_required(
    CLIENT: ICloudDriveClient,
    OUTPUT_DIR: Path,
    ENTRY: RemoteEntry,
    SHOULD_TRANSFER: bool,
) -> bool:
    if not SHOULD_TRANSFER:
        return True

    LOCAL_PATH = OUTPUT_DIR / ENTRY.path
    return CLIENT.download_file(ENTRY.path, LOCAL_PATH)
