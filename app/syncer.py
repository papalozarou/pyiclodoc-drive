# ------------------------------------------------------------------------------
# This module performs incremental iCloud Drive synchronisation with manifest
# and safety-net logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
import time

from app.icloud_client import ICloudDriveClient, RemoteEntry
from app.logger import log_line

TRANSFER_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
PROGRESS_LOG_SEPARATOR = "------------------------------------------------------------"
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
RECONCILE_MTIME_TOLERANCE_SECONDS = 2.0


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
    BACKUP_DELETE_REMOVED: bool = False,
) -> tuple[SyncResult, dict[str, dict[str, Any]]]:
    TRAVERSAL_STARTED_EPOCH = time.monotonic()
    if LOG_FILE is not None:
        log_line(LOG_FILE, "info", "Traversal started.")

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
            "info",
            "Traversal finished. "
            f"entries={len(ENTRIES)}, files={len(FILES)}, "
            f"directories={len(DIRECTORIES)}, "
            f"duration_seconds={TRAVERSAL_DURATION_SECONDS:.3f}.",
        )

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
    USE_LOCAL_RECONCILIATION = len(MANIFEST) == 0
    LOCAL_FILE_INDEX: dict[str, tuple[int, float]] = {}

    TRANSFERRED = 0
    TRANSFERRED_BYTES = 0
    SKIPPED = 0
    ERRORS = 0

    if USE_LOCAL_RECONCILIATION:
        if LOG_FILE is not None:
            log_line(LOG_FILE, "info", "Reconciliation started for first run.")

        LOCAL_FILE_INDEX = build_local_file_index(OUTPUT_DIR)

        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "info",
                f"Reconciliation finished. local_files={len(LOCAL_FILE_INDEX)}.",
            )

    for ENTRY in FILES:
        SHOULD_TRANSFER = needs_transfer(ENTRY, MANIFEST)
        ENTRY_METADATA = entry_metadata(ENTRY)

        if SHOULD_TRANSFER and USE_LOCAL_RECONCILIATION:
            LOCAL_METADATA = LOCAL_FILE_INDEX.get(ENTRY.path)
            SHOULD_TRANSFER = not is_local_file_aligned_with_remote(ENTRY, LOCAL_METADATA)

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
                if USE_LOCAL_RECONCILIATION and ENTRY.path in LOCAL_FILE_INDEX:
                    log_line(
                        LOG_FILE,
                        "debug",
                        f"File skipped reconciled: {ENTRY.path}",
                    )
                else:
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
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "info",
                f"Transfer started. candidates={len(TRANSFER_CANDIDATES)}.",
            )

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
                        SUCCESS, ATTEMPT_COUNT, TRANSFER_MODE = FUTURE.result()
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
                        LOCAL_PATH = OUTPUT_DIR / ENTRY.path
                        apply_remote_modified_time(LOCAL_PATH, ENTRY.modified, LOG_FILE)
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

                            if TRANSFER_MODE == "package":
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    f"Package transferred: {ENTRY.path} "
                                    f"({max(ENTRY.size, 0)} bytes)",
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
                            f"File transfer failed: {ENTRY.path} (reason={TRANSFER_MODE})",
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
                        PROGRESS_LOG_SEPARATOR,
                    )
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
                    log_line(
                        LOG_FILE,
                        "debug",
                        PROGRESS_LOG_SEPARATOR,
                    )
                    LAST_PROGRESS_LOG_EPOCH = NOW_EPOCH
    elif LOG_FILE is not None:
        log_line(LOG_FILE, "info", "Transfer skipped. candidates=0.")

    if LOG_FILE is not None:
        log_line(
            LOG_FILE,
            "info",
            "Transfer finished. "
            f"transferred={TRANSFERRED}, skipped={SKIPPED}, errors={ERRORS}.",
        )

    for ENTRY in DIRECTORIES:
        NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)

    if BACKUP_DELETE_REMOVED:
        if LOG_FILE is not None:
            log_line(LOG_FILE, "info", "Delete phase started.")

        DELETED_FILES, DELETED_DIRS, DELETE_ERRORS = delete_removed_local_paths(
            OUTPUT_DIR,
            FILES,
            DIRECTORIES,
            LOG_FILE,
        )

        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "info",
                "Delete phase finished. "
                f"deleted_files={DELETED_FILES}, "
                f"deleted_directories={DELETED_DIRS}, "
                f"errors={DELETE_ERRORS}.",
            )

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
                    PROGRESS_LOG_SEPARATOR,
                )
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal progress detail: "
                    f"elapsed_seconds={ELAPSED_SECONDS:.1f}",
                )
                log_line(
                    LOG_FILE,
                    "debug",
                    PROGRESS_LOG_SEPARATOR,
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
# This function removes local items that are no longer present in iCloud.
#
# 1. "OUTPUT_DIR" is local backup root.
# 2. "FILES" are current remote file entries.
# 3. "DIRECTORIES" are current remote directory entries.
# 4. "LOG_FILE" is optional log file path.
#
# Returns: Tuple of "(deleted_files, deleted_directories, errors)".
# ------------------------------------------------------------------------------
def delete_removed_local_paths(
    OUTPUT_DIR: Path,
    FILES: list[RemoteEntry],
    DIRECTORIES: list[RemoteEntry],
    LOG_FILE: Path | None = None,
) -> tuple[int, int, int]:
    REMOTE_FILE_PATHS = {ENTRY.path for ENTRY in FILES}
    REMOTE_DIR_PATHS = {ENTRY.path for ENTRY in DIRECTORIES}
    DELETED_FILES = 0
    DELETED_DIRS = 0
    ERRORS = 0

    LOCAL_FILES = list(iter_local_files(OUTPUT_DIR))
    for FILE_PATH in LOCAL_FILES:
        RELATIVE_PATH = FILE_PATH.relative_to(OUTPUT_DIR).as_posix()

        if RELATIVE_PATH in REMOTE_FILE_PATHS:
            continue

        try:
            FILE_PATH.unlink()
            DELETED_FILES += 1
            if LOG_FILE is not None:
                log_line(LOG_FILE, "debug", f"File deleted removed: {RELATIVE_PATH}")
        except Exception as ERROR:
            ERRORS += 1
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"File delete error: {RELATIVE_PATH} ({type(ERROR).__name__}: {ERROR})",
                )

    LOCAL_DIRS = list(iter_local_directories(OUTPUT_DIR))
    for DIR_PATH in LOCAL_DIRS:
        RELATIVE_PATH = DIR_PATH.relative_to(OUTPUT_DIR).as_posix()

        if RELATIVE_PATH in REMOTE_DIR_PATHS:
            continue

        try:
            DIR_PATH.rmdir()
            DELETED_DIRS += 1
            if LOG_FILE is not None:
                log_line(LOG_FILE, "debug", f"Directory deleted removed: {RELATIVE_PATH}")
        except OSError:
            continue
        except Exception as ERROR:
            ERRORS += 1
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"Directory delete error: {RELATIVE_PATH} "
                    f"({type(ERROR).__name__}: {ERROR})",
                )

    return DELETED_FILES, DELETED_DIRS, ERRORS


# ------------------------------------------------------------------------------
# This function yields all local files under output root.
#
# 1. "OUTPUT_DIR" is local backup root.
#
# Returns: Iterable of local file paths.
# ------------------------------------------------------------------------------
def iter_local_files(OUTPUT_DIR: Path):
    for PATH in OUTPUT_DIR.rglob("*"):
        if PATH.is_file():
            yield PATH


# ------------------------------------------------------------------------------
# This function yields local directories in depth-first reverse order.
#
# 1. "OUTPUT_DIR" is local backup root.
#
# Returns: Iterable of local directory paths suitable for safe pruning.
# ------------------------------------------------------------------------------
def iter_local_directories(OUTPUT_DIR: Path):
    DIRECTORIES = [PATH for PATH in OUTPUT_DIR.rglob("*") if PATH.is_dir()]
    DIRECTORIES.sort(key=lambda ITEM: len(ITEM.parts), reverse=True)
    return DIRECTORIES


# ------------------------------------------------------------------------------
# This function builds a local file metadata index for first-run reconciliation.
#
# 1. "OUTPUT_DIR" is local backup root.
#
# Returns: Mapping of relative path to "(size, modified_epoch)" metadata.
# ------------------------------------------------------------------------------
def build_local_file_index(OUTPUT_DIR: Path) -> dict[str, tuple[int, float]]:
    INDEX: dict[str, tuple[int, float]] = {}

    for FILE_PATH in iter_local_files(OUTPUT_DIR):
        try:
            FILE_STAT = FILE_PATH.stat()
        except OSError:
            continue

        INDEX[FILE_PATH.relative_to(OUTPUT_DIR).as_posix()] = (
            FILE_STAT.st_size,
            FILE_STAT.st_mtime,
        )

    return INDEX


# ------------------------------------------------------------------------------
# This function checks local-file metadata against remote entry metadata.
#
# 1. "ENTRY" is current remote file metadata.
# 2. "LOCAL_METADATA" is optional local metadata tuple.
#
# Returns: True when local file can be treated as already synced.
# ------------------------------------------------------------------------------
def is_local_file_aligned_with_remote(
    ENTRY: RemoteEntry,
    LOCAL_METADATA: tuple[int, float] | None,
) -> bool:
    if LOCAL_METADATA is None:
        return False

    LOCAL_SIZE, LOCAL_MTIME = LOCAL_METADATA
    if LOCAL_SIZE != ENTRY.size:
        return False

    REMOTE_MTIME = parse_remote_modified_epoch(ENTRY.modified)
    if REMOTE_MTIME is None:
        return False

    return abs(LOCAL_MTIME - REMOTE_MTIME) <= RECONCILE_MTIME_TOLERANCE_SECONDS


# ------------------------------------------------------------------------------
# This function parses remote modified timestamps to UTC epoch seconds.
#
# 1. "RAW_VALUE" is remote timestamp string from iCloud metadata.
#
# Returns: Parsed epoch seconds, or None when parsing fails.
# ------------------------------------------------------------------------------
def parse_remote_modified_epoch(RAW_VALUE: str) -> float | None:
    VALUE = RAW_VALUE.strip()
    if not VALUE:
        return None

    NORMALISED = VALUE
    if VALUE.endswith("Z"):
        NORMALISED = VALUE[:-1] + "+00:00"

    try:
        PARSED = datetime.fromisoformat(NORMALISED)
    except ValueError:
        return None

    if PARSED.tzinfo is None:
        PARSED = PARSED.replace(tzinfo=timezone.utc)

    return PARSED.timestamp()


# ------------------------------------------------------------------------------
# This function applies remote modified time to a local file after transfer.
#
# 1. "LOCAL_PATH" is transferred local file path.
# 2. "REMOTE_MODIFIED" is remote timestamp string from iCloud metadata.
# 3. "LOG_FILE" is optional log file path.
#
# Returns: True when timestamp is applied; otherwise False.
# ------------------------------------------------------------------------------
def apply_remote_modified_time(
    LOCAL_PATH: Path,
    REMOTE_MODIFIED: str,
    LOG_FILE: Path | None = None,
) -> bool:
    REMOTE_MTIME = parse_remote_modified_epoch(REMOTE_MODIFIED)
    if REMOTE_MTIME is None:
        if LOG_FILE is not None:
            log_line(LOG_FILE, "debug", f"Timestamp skipped parse: {LOCAL_PATH.as_posix()}")
        return False

    try:
        FILE_STAT = LOCAL_PATH.stat()
        os.utime(LOCAL_PATH, (FILE_STAT.st_atime, REMOTE_MTIME))
    except OSError as ERROR:
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "debug",
                "Timestamp apply error: "
                f"{LOCAL_PATH.as_posix()} ({type(ERROR).__name__}: {ERROR})",
            )
        return False

    if LOG_FILE is not None:
        log_line(
            LOG_FILE,
            "debug",
            f"Timestamp applied: {LOCAL_PATH.as_posix()} <- {REMOTE_MODIFIED}",
        )
    return True


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
) -> tuple[bool, int, str]:
    if not SHOULD_TRANSFER:
        return True, 1, "skipped"

    LOCAL_PATH = OUTPUT_DIR / ENTRY.path
    ATTEMPT = 1

    while ATTEMPT <= TRANSFER_RETRY_ATTEMPTS:
        try:
            IS_SUCCESS = CLIENT.download_file(ENTRY.path, LOCAL_PATH)
            if IS_SUCCESS:
                return True, ATTEMPT, "file"

            IS_PACKAGE_SUCCESS = CLIENT.download_package_tree(ENTRY.path, LOCAL_PATH)
            if IS_PACKAGE_SUCCESS:
                return True, ATTEMPT, "package"

            FAILURE_REASON = CLIENT.get_last_download_failure_reason() or "download_failed"
            return False, ATTEMPT, FAILURE_REASON
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

    return False, ATTEMPT, "retry_exhausted"


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
