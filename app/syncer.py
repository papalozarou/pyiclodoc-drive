# ------------------------------------------------------------------------------
# This module performs incremental iCloud Drive synchronisation with manifest
# and safety-net logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import time

from app.icloud_client import ICloudDriveClient, RemoteEntry
from app.logger import log_line

TRANSFER_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
TRANSFER_RETRY_ATTEMPTS = 3
TRANSFER_RETRY_BASE_DELAY_SECONDS = 1.0
TRANSFER_RETRY_MAX_DELAY_SECONDS = 8.0
TRANSFER_RETRY_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "throttled",
    "timed out",
    "timeout",
    "connection reset",
)


# ------------------------------------------------------------------------------
# This data class records safety-net findings used to block unsafe sync runs.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class SafetyNetResult:
    should_block: bool
    expected_uid: int
    expected_gid: int
    mismatched_samples: list[str]


# ------------------------------------------------------------------------------
# This data class captures per-run transfer summary metrics.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class SyncResult:
    total_files: int
    transferred_files: int
    transferred_bytes: int
    skipped_files: int
    error_files: int


# ------------------------------------------------------------------------------
# This function derives automatic transfer worker count from host CPU capacity.
#
# Returns: Bounded worker count for concurrent file download tasks.
# ------------------------------------------------------------------------------
def get_auto_worker_count() -> int:
    CPU_COUNT = os.cpu_count() or 1
    return min(max(CPU_COUNT, 1), 8)


# ------------------------------------------------------------------------------
# This function resolves effective transfer worker count.
#
# 1. "SYNC_DOWNLOAD_WORKERS" uses 0 for auto mode and positive values for overrides.
#
# Returns: Bounded worker count for concurrent file download tasks.
# ------------------------------------------------------------------------------
def get_transfer_worker_count(SYNC_DOWNLOAD_WORKERS: int) -> int:
    if SYNC_DOWNLOAD_WORKERS > 0:
        return min(max(SYNC_DOWNLOAD_WORKERS, 1), 16)

    return get_auto_worker_count()


# ------------------------------------------------------------------------------
# This function runs a first-time permission safety check.
#
# 1. "OUTPUT_DIR" is the backup destination root.
# 2. "SAMPLE_SIZE" is the max number of files to inspect.
#
# Returns: "SafetyNetResult" describing whether sync should be blocked and why.
#
# Notes: Ownership values are read from "stat" values:
# https://docs.python.org/3/library/os.html#os.stat_result
# ------------------------------------------------------------------------------
def run_first_time_safety_net(OUTPUT_DIR: Path, SAMPLE_SIZE: int) -> SafetyNetResult:
    LOCAL_FILES = collect_local_files(OUTPUT_DIR, SAMPLE_SIZE)
    EXPECTED_UID = os.getuid()
    EXPECTED_GID = os.getgid()

    if not LOCAL_FILES:
        return SafetyNetResult(False, EXPECTED_UID, EXPECTED_GID, [])

    MISMATCHES = collect_mismatches(LOCAL_FILES, EXPECTED_UID, EXPECTED_GID)
    SHOULD_BLOCK = len(MISMATCHES) > 0

    return SafetyNetResult(
        SHOULD_BLOCK,
        EXPECTED_UID,
        EXPECTED_GID,
        MISMATCHES,
    )


# ------------------------------------------------------------------------------
# This function collects a bounded local-file sample for permission checks.
#
# 1. "OUTPUT_DIR" is the backup destination root.
# 2. "SAMPLE_SIZE" is the sample cap.
#
# Returns: Ordered file list up to "SAMPLE_SIZE" for ownership analysis.
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
# ------------------------------------------------------------------------------
# This function returns sampled files with non-matching ownership.
#
# 1. "FILES" is the sampled file list.
# 2. "EXPECTED_UID" is the runtime user ID expected to own files.
# 3. "EXPECTED_GID" is the runtime group ID expected to own files.
# 4. "LIMIT" caps mismatch output.
#
# Returns: Human-readable mismatch list for logs and Telegram alerts.
# ------------------------------------------------------------------------------
def collect_mismatches(
    FILES: list[Path],
    EXPECTED_UID: int,
    EXPECTED_GID: int,
    LIMIT: int = 20,
) -> list[str]:
    MISMATCHES: list[str] = []

    for PATH in FILES:
        FILE_STAT = PATH.stat()
        UID = FILE_STAT.st_uid
        GID = FILE_STAT.st_gid

        if UID == EXPECTED_UID and GID == EXPECTED_GID:
            continue

        MISMATCHES.append(
            f"{PATH}: uid={UID}, gid={GID} "
            f"(expected uid={EXPECTED_UID}, gid={EXPECTED_GID})",
        )

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
    SYNC_DOWNLOAD_WORKERS: int = 0,
    LOG_FILE: Path | None = None,
) -> tuple[SyncResult, dict[str, dict[str, Any]]]:
    TRAVERSAL_STARTED_EPOCH = time.monotonic()
    ENTRIES = list_entries_with_progress(
        CLIENT,
        LOG_FILE,
        TRAVERSAL_STARTED_EPOCH,
    )
    TRAVERSAL_DURATION_SECONDS = time.monotonic() - TRAVERSAL_STARTED_EPOCH
    FILES = [ENTRY for ENTRY in ENTRIES if not ENTRY.is_dir]
    DIRECTORIES = [ENTRY for ENTRY in ENTRIES if ENTRY.is_dir]
    if LOG_FILE is not None:
        log_line(
            LOG_FILE,
            "debug",
            "Traversal timing detail: "
            f"list_entries_seconds={TRAVERSAL_DURATION_SECONDS:.3f}",
        )
        log_line(
            LOG_FILE,
            "debug",
            "Remote listing detail: "
            f"entries={len(ENTRIES)}, files={len(FILES)}, directories={len(DIRECTORIES)}",
        )

    ensure_directories(OUTPUT_DIR, DIRECTORIES, LOG_FILE)
    NEW_MANIFEST: dict[str, dict[str, Any]] = {}
    TRANSFER_CANDIDATES: list[RemoteEntry] = []
    TRANSFER_CANDIDATE_METADATA: dict[str, dict[str, Any]] = {}

    TRANSFERRED = 0
    TRANSFERRED_BYTES = 0
    SKIPPED = 0
    ERRORS = 0

    for ENTRY in FILES:
        SHOULD_TRANSFER = needs_transfer(ENTRY, MANIFEST)
        ENTRY_METADATA = entry_metadata(ENTRY)

        if SHOULD_TRANSFER:
            TRANSFER_CANDIDATES.append(ENTRY)
            TRANSFER_CANDIDATE_METADATA[ENTRY.path] = ENTRY_METADATA
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"File queued for transfer: {ENTRY.path} ({max(ENTRY.size, 0)} bytes)",
                )
        else:
            SKIPPED += 1
            NEW_MANIFEST[ENTRY.path] = ENTRY_METADATA
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"File skipped unchanged: {ENTRY.path}",
                )

    if LOG_FILE is not None:
        log_line(
            LOG_FILE,
            "debug",
            "Transfer planning detail: "
            f"candidates={len(TRANSFER_CANDIDATES)}, skipped_unchanged={SKIPPED}",
        )

    if TRANSFER_CANDIDATES:
        WORKER_COUNT = get_transfer_worker_count(SYNC_DOWNLOAD_WORKERS)
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "debug",
                f"Transfer execution detail: workers={WORKER_COUNT}, sync_workers={SYNC_DOWNLOAD_WORKERS}",
            )

        with ThreadPoolExecutor(max_workers=WORKER_COUNT) as EXECUTOR:
            FUTURES = {
                EXECUTOR.submit(transfer_if_required, CLIENT, OUTPUT_DIR, ENTRY, True): ENTRY
                for ENTRY in TRANSFER_CANDIDATES
            }
            PENDING = set(FUTURES.keys())
            COMPLETED = 0
            TRANSFER_STARTED_EPOCH = time.monotonic()
            LAST_PROGRESS_LOG_EPOCH = TRANSFER_STARTED_EPOCH

            while PENDING:
                DONE, PENDING = wait(
                    PENDING,
                    timeout=TRANSFER_PROGRESS_LOG_INTERVAL_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                for FUTURE in DONE:
                    ENTRY = FUTURES[FUTURE]
                    COMPLETED += 1
                    try:
                        SUCCESS, ATTEMPT_COUNT = FUTURE.result()
                    except Exception as ERROR:
                        if LOG_FILE is not None:
                            log_line(
                                LOG_FILE,
                                "debug",
                                f"File transfer exception: {ENTRY.path} "
                                f"({type(ERROR).__name__}: {ERROR})",
                            )
                        print(
                            "File transfer worker failed: "
                            f"{type(ERROR).__name__}: {ERROR}",
                            flush=True,
                        )
                        ERRORS += 1
                        EXISTING_METADATA = MANIFEST.get(ENTRY.path)

                        if EXISTING_METADATA is not None:
                            NEW_MANIFEST[ENTRY.path] = EXISTING_METADATA
                        continue

                    if SUCCESS:
                        TRANSFERRED += 1
                        TRANSFERRED_BYTES += max(ENTRY.size, 0)
                        NEW_MANIFEST[ENTRY.path] = TRANSFER_CANDIDATE_METADATA[ENTRY.path]
                        if LOG_FILE is not None:
                            if ATTEMPT_COUNT > 1:
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    f"File transferred after retries: {ENTRY.path} "
                                    f"(attempts={ATTEMPT_COUNT}, "
                                    f"{max(ENTRY.size, 0)} bytes)",
                                )
                                continue

                            log_line(
                                LOG_FILE,
                                "debug",
                                f"File transferred: {ENTRY.path} "
                                f"({max(ENTRY.size, 0)} bytes)",
                            )
                        continue

                    ERRORS += 1
                    EXISTING_METADATA = MANIFEST.get(ENTRY.path)

                    if EXISTING_METADATA is not None:
                        NEW_MANIFEST[ENTRY.path] = EXISTING_METADATA
                    if LOG_FILE is not None:
                        log_line(
                            LOG_FILE,
                            "debug",
                            f"File transfer failed: {ENTRY.path}",
                        )

                NOW_EPOCH = time.monotonic()
                SHOULD_LOG_PROGRESS = (
                    LOG_FILE is not None
                    and NOW_EPOCH - LAST_PROGRESS_LOG_EPOCH
                    >= TRANSFER_PROGRESS_LOG_INTERVAL_SECONDS
                )
                if SHOULD_LOG_PROGRESS:
                    ELAPSED_SECONDS = NOW_EPOCH - TRANSFER_STARTED_EPOCH
                    log_line(
                        LOG_FILE,
                        "debug",
                        "Transfer progress detail: "
                        f"completed={COMPLETED}/{len(TRANSFER_CANDIDATES)}, "
                        f"active={len(PENDING)}, "
                        f"transferred={TRANSFERRED}, "
                        f"bytes={TRANSFERRED_BYTES}, "
                        f"skipped={SKIPPED}, "
                        f"errors={ERRORS}, "
                        f"elapsed_seconds={ELAPSED_SECONDS:.1f}",
                    )
                    LAST_PROGRESS_LOG_EPOCH = NOW_EPOCH

    for ENTRY in DIRECTORIES:
        NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)

    return SyncResult(
        len(FILES),
        TRANSFERRED,
        TRANSFERRED_BYTES,
        SKIPPED,
        ERRORS,
    ), NEW_MANIFEST


# ------------------------------------------------------------------------------
# This function lists remote entries and emits traversal progress diagnostics.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "LOG_FILE" is optional log file path.
# 3. "STARTED_EPOCH" is traversal start timestamp.
#
# Returns: Flat list of discovered remote entries.
# ------------------------------------------------------------------------------
def list_entries_with_progress(
    CLIENT: ICloudDriveClient,
    LOG_FILE: Path | None,
    STARTED_EPOCH: float,
) -> list[RemoteEntry]:
    TIMEOUT_SECONDS = max(TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS, 0.01)

    with ThreadPoolExecutor(max_workers=1) as EXECUTOR:
        FUTURE = EXECUTOR.submit(CLIENT.list_entries)

        while True:
            try:
                return FUTURE.result(timeout=TIMEOUT_SECONDS)
            except TimeoutError:
                if LOG_FILE is None:
                    continue

                ELAPSED_SECONDS = time.monotonic() - STARTED_EPOCH
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal progress detail: "
                    f"elapsed_seconds={ELAPSED_SECONDS:.1f}",
                )


# ------------------------------------------------------------------------------
# This function ensures local directories exist before file downloads begin.
#
# 1. "OUTPUT_DIR" is local backup root.
# 2. "DIRECTORIES" are remote directory entries.
#
# Returns: None.
# ------------------------------------------------------------------------------
def ensure_directories(
    OUTPUT_DIR: Path,
    DIRECTORIES: list[RemoteEntry],
    LOG_FILE: Path | None = None,
) -> None:
    for ENTRY in DIRECTORIES:
        LOCAL_PATH = OUTPUT_DIR / ENTRY.path
        LOCAL_PATH.mkdir(parents=True, exist_ok=True)
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "debug",
                f"Directory ensured: {ENTRY.path}",
            )


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
) -> tuple[bool, int]:
    if not SHOULD_TRANSFER:
        return True, 1

    LOCAL_PATH = OUTPUT_DIR / ENTRY.path
    ATTEMPT = 1

    while ATTEMPT <= TRANSFER_RETRY_ATTEMPTS:
        try:
            IS_SUCCESS = CLIENT.download_file(ENTRY.path, LOCAL_PATH)
            return IS_SUCCESS, ATTEMPT
        except Exception as ERROR:
            if ATTEMPT >= TRANSFER_RETRY_ATTEMPTS:
                raise

            if not is_retryable_transfer_error(ERROR):
                raise

            DELAY_SECONDS = min(
                TRANSFER_RETRY_BASE_DELAY_SECONDS * (2 ** (ATTEMPT - 1)),
                TRANSFER_RETRY_MAX_DELAY_SECONDS,
            )
            time.sleep(DELAY_SECONDS)
            ATTEMPT += 1

    return False, ATTEMPT


# ------------------------------------------------------------------------------
# This function identifies transient transfer errors that should be retried.
#
# 1. "ERROR" is a transfer exception from pyicloud or network layers.
#
# Returns: True for retryable errors; otherwise False.
# ------------------------------------------------------------------------------
def is_retryable_transfer_error(ERROR: Exception) -> bool:
    ERROR_TEXT = f"{type(ERROR).__name__}: {ERROR}".lower()
    return any(MARKER in ERROR_TEXT for MARKER in TRANSFER_RETRY_ERROR_MARKERS)
