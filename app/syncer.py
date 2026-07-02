# ------------------------------------------------------------------------------
# This module performs incremental iCloud Drive synchronisation with manifest
# and safety-net logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol
import errno
import os
import shutil
import time

from app.icloud_client import (
    DownloadResult,
    ICloudDriveClient,
    RemoteEntry,
    TraversalWorkerTimeoutError,
    TraversalStatsSnapshot,
)
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
KNOWN_PACKAGE_SUFFIXES = (
    ".app",
    ".band",
    ".bundle",
    ".graffle",
    ".key",
    ".linea",
    ".numbers",
    ".pages",
    ".playground",
    ".playgroundbook",
    ".pxm",
    ".rtfd",
    ".sketch",
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
    deleted_files: int
    deleted_directories: int
    delete_errors: int
    skipped_files: int
    error_files: int
    traversal_complete: bool = True
    traversal_hard_failures: int = 0
    delete_phase_skipped: bool = False


# ------------------------------------------------------------------------------
# This protocol describes the transfer methods required by sync execution.
# ------------------------------------------------------------------------------
class TransferClient(Protocol):
    def download_file(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        ...

    def download_package_tree(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        ...


# ------------------------------------------------------------------------------
# This protocol describes traversal telemetry required by sync reporting.
# ------------------------------------------------------------------------------
class TraversalStatsClient(Protocol):
    def get_traversal_stats_snapshot(self) -> TraversalStatsSnapshot:
        ...


# ------------------------------------------------------------------------------
# This protocol describes the client capabilities required by sync execution.
# ------------------------------------------------------------------------------
class SyncClient(TransferClient, TraversalStatsClient, Protocol):
    def list_entries(self) -> list[RemoteEntry]:
        ...


# ------------------------------------------------------------------------------
# This data class captures one terminal transfer result for a file entry.
#
# N.B.
# This is the stable boundary consumed by worker threads, summary reporting,
# and manifest updates. It keeps retry state and outcome semantics explicit.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class TransferResult:
    is_success: bool
    attempt_count: int
    outcome: str


# ------------------------------------------------------------------------------
# This data class models one transfer attempt before retry orchestration decides
# whether to stop, retry, or switch strategy.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class TransferAttemptResult:
    outcome: str
    transfer_mode: str = ""
    failure_reason: str = ""


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
# 1. "SYNC_DOWNLOAD_WORKERS" uses 0 for auto mode and positive values for
# overrides.
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
    ENTRY_KIND = "dir" if ENTRY.is_dir else "file"
    return {
        "is_dir": ENTRY.is_dir,
        "entry_kind": ENTRY_KIND,
        "size": ENTRY.size,
        "modified": ENTRY.modified,
    }


# ------------------------------------------------------------------------------
# This function returns manifest metadata for package-like entries.
#
# 1. "ENTRY" is a remote file metadata record represented as a package.
# 2. "PACKAGE_STATE" is package transfer state token.
#
# Returns: Dictionary payload persisted in the incremental manifest.
# ------------------------------------------------------------------------------
def package_entry_metadata(ENTRY: RemoteEntry, PACKAGE_STATE: str) -> dict[str, Any]:
    METADATA = entry_metadata(ENTRY)
    METADATA["entry_kind"] = "package"
    METADATA["package_state"] = PACKAGE_STATE
    METADATA["package_signature"] = package_signature(ENTRY)
    return METADATA


# ------------------------------------------------------------------------------
# This function computes a stable package signature from remote entry data.
#
# 1. "ENTRY" is the remote package-like entry.
#
# Returns: Stable signature string for package transfer decisions.
# ------------------------------------------------------------------------------
def package_signature(ENTRY: RemoteEntry) -> str:
    return f"{ENTRY.modified}|{ENTRY.size}"


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

    if str(EXISTING.get("entry_kind", "file")) == "package":
        EXISTING_SIGNATURE = str(EXISTING.get("package_signature", ""))
        return EXISTING_SIGNATURE != package_signature(ENTRY)

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
    CLIENT: SyncClient,
    OUTPUT_DIR: Path,
    MANIFEST: dict[str, dict[str, Any]],
    SYNC_DOWNLOAD_WORKERS: int = 0,
    LOG_FILE: Path | None = None,
    BACKUP_DELETE_REMOVED: bool = False,
) -> tuple[SyncResult, dict[str, dict[str, Any]]]:
    TRAVERSAL_STARTED_EPOCH = time.monotonic()
    TRAVERSAL_ERROR_DETAIL = ""
    if LOG_FILE is not None:
        log_line(LOG_FILE, "info", "Traversal started.")

    try:
        ENTRIES = list_entries_with_progress(
            CLIENT,
            LOG_FILE,
            TRAVERSAL_STARTED_EPOCH,
        )
    except TraversalWorkerTimeoutError as ERROR:
        ENTRIES = []
        TRAVERSAL_ERROR_DETAIL = str(ERROR)

    TRAVERSAL_HARD_FAILURES = get_traversal_hard_failure_count(CLIENT)
    TRAVERSAL_COMPLETE = TRAVERSAL_HARD_FAILURES == 0
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
            f"duration_seconds={TRAVERSAL_DURATION_SECONDS:.3f}, "
            f"complete={TRAVERSAL_COMPLETE}, "
            f"hard_failures={TRAVERSAL_HARD_FAILURES}.",
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
        if not TRAVERSAL_COMPLETE:
            log_line(
                LOG_FILE,
                "error",
                "Traversal incomplete. Delete phase and manifest save will be skipped "
                "for this run.",
            )
            if TRAVERSAL_ERROR_DETAIL:
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal incomplete detail: "
                    f"reason={TRAVERSAL_ERROR_DETAIL}",
                )

    ensure_directories(OUTPUT_DIR, DIRECTORIES, LOG_FILE)
    NEW_MANIFEST: dict[str, dict[str, Any]] = {}
    TRANSFER_CANDIDATES: list[RemoteEntry] = []
    USE_LOCAL_RECONCILIATION = len(MANIFEST) == 0
    LOCAL_FILE_INDEX: dict[str, tuple[int, float]] = {}

    TRANSFERRED = 0
    TRANSFERRED_BYTES = 0
    SKIPPED = 0
    ERRORS = 0
    FAILURE_REASON_COUNTS: dict[str, int] = {}

    if USE_LOCAL_RECONCILIATION:
        if LOG_FILE is not None:
            log_line(LOG_FILE, "info", "Reconciliation started for first run.")
            log_line(
                LOG_FILE,
                "debug",
                "Reconciliation detail: "
                f"manifest_entries={len(MANIFEST)}, "
                f"output_dir={OUTPUT_DIR.as_posix()}, "
                "reason=empty_manifest",
            )

        LOCAL_FILE_INDEX = build_local_file_index(OUTPUT_DIR)

        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "info",
                f"Reconciliation finished. local_files={len(LOCAL_FILE_INDEX)}.",
            )

    for ENTRY in FILES:
        SHOULD_TRANSFER = needs_transfer(ENTRY, MANIFEST)
        EXISTING_METADATA = MANIFEST.get(ENTRY.path)
        ENTRY_METADATA = entry_metadata(ENTRY)

        if SHOULD_TRANSFER and USE_LOCAL_RECONCILIATION:
            LOCAL_METADATA = LOCAL_FILE_INDEX.get(ENTRY.path)
            SHOULD_TRANSFER = not is_local_file_aligned_with_remote(ENTRY, LOCAL_METADATA)

        if SHOULD_TRANSFER:
            TRANSFER_CANDIDATES.append(ENTRY)
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"File queued for transfer: {ENTRY.path} ({max(ENTRY.size, 0)} bytes)",
                )
        else:
            SKIPPED += 1
            if (
                EXISTING_METADATA is not None
                and str(EXISTING_METADATA.get("entry_kind", "file")) == "package"
            ):
                PACKAGE_STATE = str(EXISTING_METADATA.get("package_state", "package_reconciled"))
                NEW_MANIFEST[ENTRY.path] = package_entry_metadata(ENTRY, PACKAGE_STATE)
            else:
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
            f"files={len(FILES)}, "
            f"directories={len(DIRECTORIES)}, "
            f"manifest_entries={len(MANIFEST)}, "
            f"local_reconciliation={USE_LOCAL_RECONCILIATION}, "
            f"candidates={len(TRANSFER_CANDIDATES)}, "
            f"skipped_unchanged={SKIPPED}, "
            f"delete_removed={BACKUP_DELETE_REMOVED}, "
            f"traversal_complete={TRAVERSAL_COMPLETE}",
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
                "Transfer execution detail: "
                f"workers={WORKER_COUNT}, "
                f"sync_workers={SYNC_DOWNLOAD_WORKERS}",
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
                        TRANSFER_RESULT = FUTURE.result()
                    except Exception as ERROR:
                        if LOG_FILE is not None:
                            log_line(
                                LOG_FILE,
                                "debug",
                                f"File transfer exception: {ENTRY.path} "
                                f"({type(ERROR).__name__}: {ERROR})",
                            )
                        log_line(
                            LOG_FILE,
                            "error",
                            "File transfer worker failed: "
                            f"{type(ERROR).__name__}: {ERROR}",
                        )
                        ERRORS += 1
                        ERROR_REASON = "worker_exception"
                        FAILURE_REASON_COUNTS[ERROR_REASON] = (
                            FAILURE_REASON_COUNTS.get(ERROR_REASON, 0) + 1
                        )
                        EXISTING_METADATA = MANIFEST.get(ENTRY.path)

                        if EXISTING_METADATA is not None:
                            NEW_MANIFEST[ENTRY.path] = EXISTING_METADATA
                        continue

                    if TRANSFER_RESULT.is_success:
                        LOCAL_PATH = OUTPUT_DIR / ENTRY.path
                        apply_remote_modified_time(LOCAL_PATH, ENTRY.modified, LOG_FILE)
                        TRANSFERRED += 1
                        TRANSFERRED_BYTES += max(ENTRY.size, 0)
                        if TRANSFER_RESULT.outcome in {"package", "package_reconciled"}:
                            NEW_MANIFEST[ENTRY.path] = package_entry_metadata(
                                ENTRY,
                                TRANSFER_RESULT.outcome,
                            )
                        else:
                            NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)
                        if LOG_FILE is not None:
                            if TRANSFER_RESULT.attempt_count > 1:
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    f"File transferred after retries: {ENTRY.path} "
                                    f"(attempts={TRANSFER_RESULT.attempt_count}, "
                                    f"{max(ENTRY.size, 0)} bytes)",
                                )
                            elif TRANSFER_RESULT.outcome == "package":
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    f"Package transferred: {ENTRY.path} "
                                    f"({max(ENTRY.size, 0)} bytes)",
                                )
                            elif TRANSFER_RESULT.outcome == "package_reconciled":
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    "Package reconciled from existing local "
                                    f"directory: {ENTRY.path}",
                                )
                            else:
                                log_line(
                                    LOG_FILE,
                                    "debug",
                                    f"File transferred: {ENTRY.path} "
                                    f"({max(ENTRY.size, 0)} bytes)",
                                )
                        continue

                    ERRORS += 1
                    FAILURE_REASON = normalise_transfer_reason(TRANSFER_RESULT.outcome)
                    FAILURE_REASON_COUNTS[FAILURE_REASON] = (
                        FAILURE_REASON_COUNTS.get(FAILURE_REASON, 0) + 1
                    )
                    EXISTING_METADATA = MANIFEST.get(ENTRY.path)

                    if EXISTING_METADATA is not None:
                        NEW_MANIFEST[ENTRY.path] = EXISTING_METADATA
                    if LOG_FILE is not None:
                        log_line(
                            LOG_FILE,
                            "debug",
                            "File transfer failed: "
                            f"{ENTRY.path} (reason={TRANSFER_RESULT.outcome})",
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
        if FAILURE_REASON_COUNTS:
            DETAIL_TEXT = ", ".join(
                f"{REASON}={COUNT}"
                for REASON, COUNT in sorted(FAILURE_REASON_COUNTS.items())
            )
            log_line(
                LOG_FILE,
                "debug",
                f"Transfer failure reason detail: {DETAIL_TEXT}",
            )

    for ENTRY in DIRECTORIES:
        NEW_MANIFEST[ENTRY.path] = entry_metadata(ENTRY)

    DELETE_PHASE_SKIPPED = BACKUP_DELETE_REMOVED and not TRAVERSAL_COMPLETE
    DELETED_FILES = 0
    DELETED_DIRS = 0
    DELETE_ERRORS = 0

    if DELETE_PHASE_SKIPPED and LOG_FILE is not None:
        log_line(
            LOG_FILE,
            "debug",
            "Delete phase decision: "
            "enabled=True, will_run=False, reason=traversal_incomplete.",
        )
        log_line(
            LOG_FILE,
            "info",
            "Delete phase skipped because traversal was incomplete.",
        )

    if (
        not BACKUP_DELETE_REMOVED
        and LOG_FILE is not None
    ):
        log_line(
            LOG_FILE,
            "debug",
            "Delete phase decision: enabled=False, will_run=False, reason=disabled.",
        )

    if BACKUP_DELETE_REMOVED and TRAVERSAL_COMPLETE:
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "debug",
                "Delete phase decision: enabled=True, will_run=True, "
                "reason=traversal_complete.",
            )
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
        DELETED_FILES,
        DELETED_DIRS,
        DELETE_ERRORS,
        SKIPPED,
        ERRORS,
        traversal_complete=TRAVERSAL_COMPLETE,
        traversal_hard_failures=TRAVERSAL_HARD_FAILURES,
        delete_phase_skipped=DELETE_PHASE_SKIPPED,
    ), NEW_MANIFEST


# ------------------------------------------------------------------------------
# This function returns traversal hard-failure count from client telemetry.
#
# 1. "CLIENT" exposes traversal stats through the sync client contract.
#
# Returns: Count of hard traversal failures recorded by the client.
# ------------------------------------------------------------------------------
def get_traversal_hard_failure_count(CLIENT: TraversalStatsClient) -> int:
    STATS = get_traversal_stats_snapshot(CLIENT)
    return max(int(STATS.get("dir_hard_failures", 0)), 0)


# ------------------------------------------------------------------------------
# This function returns a validated traversal-stats snapshot.
#
# 1. "CLIENT" exposes traversal stats through the sync client contract.
#
# Returns: Traversal stats snapshot, or an empty default snapshot.
# ------------------------------------------------------------------------------
def get_traversal_stats_snapshot(CLIENT: TraversalStatsClient) -> TraversalStatsSnapshot:
    STATS = CLIENT.get_traversal_stats_snapshot()
    if not isinstance(STATS, dict):
        return build_empty_traversal_stats_snapshot()

    return STATS


# ------------------------------------------------------------------------------
# This function returns an empty traversal-stats snapshot shape.
#
# Returns: Empty traversal stats snapshot with zeroed counters.
# ------------------------------------------------------------------------------
def build_empty_traversal_stats_snapshot() -> TraversalStatsSnapshot:
    return {
        "directories_completed": 0,
        "directories_pending": 0,
        "workers_active": 0,
        "entries_discovered": 0,
        "files_discovered": 0,
        "directories_discovered": 0,
        "dir_reads": 0,
        "dir_retries": 0,
        "dir_non_directory": 0,
        "dir_retryable_errors": 0,
        "dir_hard_failures": 0,
        "dir_failure_samples": [],
        "slow_dirs": [],
    }


# ------------------------------------------------------------------------------
# This function formats the top slow-directory samples for traversal logs.
#
# 1. "STATS" is the traversal stats snapshot.
#
# Returns: Comma-separated slow-directory summary, or an empty string.
# ------------------------------------------------------------------------------
def format_slow_directory_summary(STATS: TraversalStatsSnapshot) -> str:
    TOP_PATHS = [
        f"{ITEM.get('path', '/')}: {ITEM.get('duration_seconds', 0)}s"
        for ITEM in STATS.get("slow_dirs", [])[:3]
        if isinstance(ITEM, dict)
    ]
    return ", ".join(TOP_PATHS)


# ------------------------------------------------------------------------------
# This function lists remote entries and emits traversal progress diagnostics.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "LOG_FILE" is optional log file path.
# 3. "STARTED_EPOCH" is traversal start timestamp.
#
# Returns: Flat list of discovered remote entries.
#
# N.B.
# This helper must preserve traversal timeout as an explicit failure signal.
# Returning "[]" here would make an incomplete traversal look identical to a
# genuinely empty remote drive.
# ------------------------------------------------------------------------------
def list_entries_with_progress(
    CLIENT: ICloudDriveClient,
    LOG_FILE: Path | None,
    STARTED_EPOCH: float,
) -> list[RemoteEntry]:
    TIMEOUT_SECONDS = max(TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS, 0.01)
    PREVIOUS_SNAPSHOT: dict[str, int | float] = {
        "entries_discovered": 0,
        "files_discovered": 0,
        "directories_discovered": 0,
        "directories_completed": 0,
        "directories_pending": 0,
    }
    PREVIOUS_LOG_EPOCH = STARTED_EPOCH

    with ThreadPoolExecutor(max_workers=1) as EXECUTOR:
        FUTURE = EXECUTOR.submit(CLIENT.list_entries)

        while True:
            try:
                RESULT = FUTURE.result(timeout=TIMEOUT_SECONDS)
                if LOG_FILE is not None:
                    log_traversal_completion_details(LOG_FILE, CLIENT)
                return RESULT
            except TraversalWorkerTimeoutError as ERROR:
                if LOG_FILE is not None:
                    log_line(
                        LOG_FILE,
                        "error",
                        f"Traversal failed before completion: {ERROR}",
                    )
                    log_traversal_completion_details(LOG_FILE, CLIENT)
                raise
            except TimeoutError:
                if LOG_FILE is None:
                    continue

                ELAPSED_SECONDS = time.monotonic() - STARTED_EPOCH
                NOW_EPOCH = time.monotonic()
                WINDOW_SECONDS = max(NOW_EPOCH - PREVIOUS_LOG_EPOCH, 0.001)
                STATS = get_traversal_stats_snapshot(CLIENT)
                CURRENT_ENTRIES = int(STATS.get("entries_discovered", 0))
                CURRENT_FILES = int(STATS.get("files_discovered", 0))
                CURRENT_DIRECTORIES = int(STATS.get("directories_discovered", 0))
                CURRENT_COMPLETED = int(STATS.get("directories_completed", 0))
                CURRENT_PENDING = int(STATS.get("directories_pending", 0))
                ENTRY_DELTA = max(CURRENT_ENTRIES - int(PREVIOUS_SNAPSHOT["entries_discovered"]), 0)
                FILE_DELTA = max(
                    CURRENT_FILES - int(PREVIOUS_SNAPSHOT["files_discovered"]),
                    0,
                )
                DIR_DELTA = max(
                    CURRENT_COMPLETED - int(PREVIOUS_SNAPSHOT["directories_completed"]),
                    0,
                )
                PENDING_DELTA = CURRENT_PENDING - int(PREVIOUS_SNAPSHOT["directories_pending"])
                ENTRIES_PER_SECOND = ENTRY_DELTA / WINDOW_SECONDS
                DIRS_PER_SECOND = DIR_DELTA / WINDOW_SECONDS
                SLOW_TOP = format_slow_directory_summary(STATS)
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
                    "Traversal delta detail: "
                    f"entries_added={ENTRY_DELTA}, "
                    f"files_added={FILE_DELTA}, "
                    f"completed_dirs_added={DIR_DELTA}, "
                    f"pending_delta={PENDING_DELTA:+d}",
                )
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal queue detail: "
                    f"pending={CURRENT_PENDING}, "
                    f"active={STATS.get('workers_active', 0)}, "
                    f"completed_dirs={CURRENT_COMPLETED}",
                )
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal throughput detail: "
                    f"entries={CURRENT_ENTRIES}, "
                    f"files={CURRENT_FILES}, "
                    f"directories={CURRENT_DIRECTORIES}, "
                    f"entries_per_second={ENTRIES_PER_SECOND:.2f}, "
                    f"directories_per_second={DIRS_PER_SECOND:.2f}",
                )
                log_line(
                    LOG_FILE,
                    "debug",
                    "Traversal read detail: "
                    f"dir_reads={STATS.get('dir_reads', 0)}, "
                    f"retries={STATS.get('dir_retries', 0)}, "
                    f"non_directory={STATS.get('dir_non_directory', 0)}, "
                    f"retryable_errors={STATS.get('dir_retryable_errors', 0)}, "
                    f"hard_failures={STATS.get('dir_hard_failures', 0)}",
                )
                if SLOW_TOP:
                    log_line(LOG_FILE, "debug", f"Traversal slow-path detail: {SLOW_TOP}")
                log_line(
                    LOG_FILE,
                    "debug",
                    PROGRESS_LOG_SEPARATOR,
                )
                PREVIOUS_SNAPSHOT = {
                    "entries_discovered": CURRENT_ENTRIES,
                    "files_discovered": CURRENT_FILES,
                    "directories_discovered": CURRENT_DIRECTORIES,
                    "directories_completed": CURRENT_COMPLETED,
                    "directories_pending": CURRENT_PENDING,
                }
                PREVIOUS_LOG_EPOCH = NOW_EPOCH


# ------------------------------------------------------------------------------
# This function writes final traversal telemetry after completion or failure.
#
# 1. "LOG_FILE" is the worker log destination.
# 2. "CLIENT" exposes traversal stats through the sync client contract.
#
# Returns: None.
# ------------------------------------------------------------------------------
def log_traversal_completion_details(
    LOG_FILE: Path,
    CLIENT: TraversalStatsClient,
) -> None:
    STATS = get_traversal_stats_snapshot(CLIENT)
    SLOW_TOP = format_slow_directory_summary(STATS)

    log_line(
        LOG_FILE,
        "debug",
        "Traversal queue detail: "
        f"pending={STATS.get('directories_pending', 0)}, "
        f"active={STATS.get('workers_active', 0)}, "
        f"completed_dirs={STATS.get('directories_completed', 0)}",
    )
    log_line(
        LOG_FILE,
        "debug",
        "Traversal read detail: "
        f"dir_reads={STATS.get('dir_reads', 0)}, "
        f"retries={STATS.get('dir_retries', 0)}, "
        f"non_directory={STATS.get('dir_non_directory', 0)}, "
        f"retryable_errors={STATS.get('dir_retryable_errors', 0)}, "
        f"hard_failures={STATS.get('dir_hard_failures', 0)}",
    )

    for SAMPLE in STATS.get("dir_failure_samples", []):
        if not isinstance(SAMPLE, dict):
            continue

        log_line(
            LOG_FILE,
            "debug",
            "Traversal failure sample: "
            f"status={SAMPLE.get('status', 'unknown')}, "
            f"path={SAMPLE.get('path', '/')}, "
            f"reason={SAMPLE.get('reason', '<none>')}",
        )

    if SLOW_TOP:
        log_line(
            LOG_FILE,
            "debug",
            f"Traversal slow-path detail: {SLOW_TOP}",
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
        if not change_conflicting_local_path(LOCAL_PATH, True):
            continue
        LOCAL_PATH.mkdir(parents=True, exist_ok=True)
        if LOG_FILE is not None:
            log_line(
                LOG_FILE,
                "debug",
                f"Directory ensured: {ENTRY.path}",
            )


# ------------------------------------------------------------------------------
# This function reconciles a local path so its type matches the remote entry.
#
# 1. "LOCAL_PATH" is the destination path on disk.
# 2. "EXPECT_DIRECTORY" selects directory or file destination handling.
#
# Returns: True when the local path is ready for the expected type.
# ------------------------------------------------------------------------------
def change_conflicting_local_path(LOCAL_PATH: Path, EXPECT_DIRECTORY: bool) -> bool:
    if not LOCAL_PATH.exists():
        return True

    if EXPECT_DIRECTORY and LOCAL_PATH.is_dir():
        return True

    if not EXPECT_DIRECTORY and not LOCAL_PATH.is_dir():
        return True

    try:
        if LOCAL_PATH.is_dir():
            shutil.rmtree(LOCAL_PATH)
            return True

        LOCAL_PATH.unlink()
        return True
    except OSError:
        return False


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
    PROTECTED_PACKAGE_ROOTS = {
        ENTRY.path for ENTRY in FILES if is_known_package_path(ENTRY.path)
    }
    DELETED_FILES = 0
    DELETED_DIRS = 0
    ERRORS = 0

    LOCAL_FILES = list(iter_local_files(OUTPUT_DIR))
    for FILE_PATH in LOCAL_FILES:
        RELATIVE_PATH = FILE_PATH.relative_to(OUTPUT_DIR).as_posix()

        if is_delete_protected_file_path(
            RELATIVE_PATH,
            REMOTE_FILE_PATHS,
            PROTECTED_PACKAGE_ROOTS,
        ):
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

        if is_delete_protected_directory_path(
            RELATIVE_PATH,
            REMOTE_DIR_PATHS,
            PROTECTED_PACKAGE_ROOTS,
        ):
            continue

        try:
            DIR_PATH.rmdir()
            DELETED_DIRS += 1
            if LOG_FILE is not None:
                log_line(LOG_FILE, "debug", f"Directory deleted removed: {RELATIVE_PATH}")
        except OSError as ERROR:
            if is_non_empty_directory_error(ERROR):
                continue

            ERRORS += 1
            if LOG_FILE is not None:
                log_line(
                    LOG_FILE,
                    "debug",
                    f"Directory delete error: {RELATIVE_PATH} "
                    f"({type(ERROR).__name__}: {ERROR})",
                )
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
# This function reports whether one local file path must be preserved.
#
# 1. "RELATIVE_PATH" is local path relative to the output root.
# 2. "REMOTE_FILE_PATHS" is the exact current remote file path set.
# 3. "PROTECTED_PACKAGE_ROOTS" is the set of directory-backed package roots.
#
# Returns: True when delete-removed must preserve the file path.
# ------------------------------------------------------------------------------
def is_delete_protected_file_path(
    RELATIVE_PATH: str,
    REMOTE_FILE_PATHS: set[str],
    PROTECTED_PACKAGE_ROOTS: set[str],
) -> bool:
    if RELATIVE_PATH in REMOTE_FILE_PATHS:
        return True

    return is_protected_package_descendant(RELATIVE_PATH, PROTECTED_PACKAGE_ROOTS)


# ------------------------------------------------------------------------------
# This function reports whether one local directory path must be preserved.
#
# 1. "RELATIVE_PATH" is local path relative to the output root.
# 2. "REMOTE_DIR_PATHS" is the exact current remote directory path set.
# 3. "PROTECTED_PACKAGE_ROOTS" is the set of directory-backed package roots.
#
# Returns: True when delete-removed must preserve the directory path.
# ------------------------------------------------------------------------------
def is_delete_protected_directory_path(
    RELATIVE_PATH: str,
    REMOTE_DIR_PATHS: set[str],
    PROTECTED_PACKAGE_ROOTS: set[str],
) -> bool:
    if RELATIVE_PATH in REMOTE_DIR_PATHS:
        return True

    if RELATIVE_PATH in PROTECTED_PACKAGE_ROOTS:
        return True

    return is_protected_package_descendant(RELATIVE_PATH, PROTECTED_PACKAGE_ROOTS)


# ------------------------------------------------------------------------------
# This function reports whether one path is nested inside a live package root.
#
# 1. "RELATIVE_PATH" is local path relative to the output root.
# 2. "PROTECTED_PACKAGE_ROOTS" is the set of directory-backed package roots.
#
# Returns: True when the path is a descendant of a protected package root.
# ------------------------------------------------------------------------------
def is_protected_package_descendant(
    RELATIVE_PATH: str,
    PROTECTED_PACKAGE_ROOTS: set[str],
) -> bool:
    return any(
        RELATIVE_PATH.startswith(f"{PACKAGE_ROOT}/")
        for PACKAGE_ROOT in PROTECTED_PACKAGE_ROOTS
    )


# ------------------------------------------------------------------------------
# This function identifies the expected non-empty-directory delete error.
#
# 1. "ERROR" is the exception raised by "rmdir()".
#
# Returns: True only for the benign non-empty-directory case.
# ------------------------------------------------------------------------------
def is_non_empty_directory_error(ERROR: OSError) -> bool:
    return ERROR.errno in {errno.ENOTEMPTY, errno.EEXIST}


# ------------------------------------------------------------------------------
# This function yields all local files under output root.
#
# 1. "OUTPUT_DIR" is local backup root.
#
# Returns: Iterator of local file paths.
# ------------------------------------------------------------------------------
def iter_local_files(OUTPUT_DIR: Path) -> Iterator[Path]:
    for PATH in OUTPUT_DIR.rglob("*"):
        if PATH.is_file():
            yield PATH


# ------------------------------------------------------------------------------
# This function yields local directories in depth-first reverse order.
#
# 1. "OUTPUT_DIR" is local backup root.
#
# Returns: Iterator of local directory paths suitable for safe pruning.
# ------------------------------------------------------------------------------
def iter_local_directories(OUTPUT_DIR: Path) -> Iterator[Path]:
    DIRECTORIES = [PATH for PATH in OUTPUT_DIR.rglob("*") if PATH.is_dir()]
    DIRECTORIES.sort(key=lambda ITEM: len(ITEM.parts), reverse=True)
    yield from DIRECTORIES


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
# This function executes one transfer attempt without retry orchestration.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "LOCAL_PATH" is the resolved destination path for "ENTRY".
# 3. "ENTRY" is file metadata.
#
# Returns: "TransferAttemptResult" for retry orchestration.
# ------------------------------------------------------------------------------
def execute_transfer_attempt(
    CLIENT: TransferClient,
    LOCAL_PATH: Path,
    ENTRY: RemoteEntry,
) -> TransferAttemptResult:
    IS_KNOWN_PACKAGE_PATH = is_known_package_path(ENTRY.path)

    if not change_conflicting_local_path(LOCAL_PATH, IS_KNOWN_PACKAGE_PATH):
        return TransferAttemptResult(
            "terminal_failure",
            failure_reason="local_type_conflict_cleanup_failed",
        )

    if LOCAL_PATH.exists() and LOCAL_PATH.is_dir():
        PACKAGE_RESULT = CLIENT.download_package_tree(ENTRY.path, LOCAL_PATH)
        if PACKAGE_RESULT.is_success:
            return TransferAttemptResult("success", transfer_mode="package")

        if PACKAGE_RESULT.failure_reason in {
            "package_item_missing",
            "package_children_unavailable",
        }:
            return TransferAttemptResult("success", transfer_mode="package_reconciled")

        return TransferAttemptResult(
            "terminal_failure",
            failure_reason=PACKAGE_RESULT.failure_reason or "package_download_failed",
        )

    if IS_KNOWN_PACKAGE_PATH:
        PACKAGE_RESULT = CLIENT.download_package_tree(ENTRY.path, LOCAL_PATH)
        if PACKAGE_RESULT.is_success:
            return TransferAttemptResult("success", transfer_mode="package")

        if PACKAGE_RESULT.failure_reason in {
            "package_item_missing",
            "package_children_unavailable",
        }:
            FILE_RESULT = CLIENT.download_file(ENTRY.path, LOCAL_PATH)
            if FILE_RESULT.is_success:
                return TransferAttemptResult("success", transfer_mode="file")

            FAILURE_REASON = get_transfer_failure_reason(
                FILE_RESULT.failure_reason,
                PACKAGE_RESULT.failure_reason,
            )
            return TransferAttemptResult("terminal_failure", failure_reason=FAILURE_REASON)

        return TransferAttemptResult(
            "terminal_failure",
            failure_reason=PACKAGE_RESULT.failure_reason or "package_download_failed",
        )

    FILE_RESULT = CLIENT.download_file(ENTRY.path, LOCAL_PATH)
    if FILE_RESULT.is_success:
        return TransferAttemptResult("success", transfer_mode="file")

    PACKAGE_RESULT = CLIENT.download_package_tree(ENTRY.path, LOCAL_PATH)
    if PACKAGE_RESULT.is_success:
        return TransferAttemptResult("success", transfer_mode="package")

    FAILURE_REASON = get_transfer_failure_reason(
        FILE_RESULT.failure_reason,
        PACKAGE_RESULT.failure_reason,
    )
    return TransferAttemptResult("terminal_failure", failure_reason=FAILURE_REASON)


# ------------------------------------------------------------------------------
# This function executes a file transfer with explicit retry semantics.
#
# 1. "CLIENT" is the active iCloud API wrapper.
# 2. "OUTPUT_DIR" is local backup root.
# 3. "ENTRY" is file metadata.
# 4. "SHOULD_TRANSFER" determines whether download should proceed.
#
# Returns: "TransferResult" describing the final transfer outcome.
# ------------------------------------------------------------------------------
def transfer_if_required(
    CLIENT: TransferClient,
    OUTPUT_DIR: Path,
    ENTRY: RemoteEntry,
    SHOULD_TRANSFER: bool,
) -> TransferResult:
    if not SHOULD_TRANSFER:
        return TransferResult(True, 1, "skipped")

    LOCAL_PATH = OUTPUT_DIR / ENTRY.path
    ATTEMPT = 1

    while ATTEMPT <= TRANSFER_RETRY_ATTEMPTS:
        try:
            ATTEMPT_RESULT = execute_transfer_attempt(CLIENT, LOCAL_PATH, ENTRY)
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
            continue

        if ATTEMPT_RESULT.outcome == "success":
            return TransferResult(True, ATTEMPT, ATTEMPT_RESULT.transfer_mode)

        return TransferResult(False, ATTEMPT, ATTEMPT_RESULT.failure_reason)

    return TransferResult(False, ATTEMPT, "retry_exhausted")


# ------------------------------------------------------------------------------
# This function merges file and package fallback failure reasons into one
# stable reason token for logging and diagnostics.
#
# 1. "FILE_REASON" is reason token from direct file download attempt.
# 2. "PACKAGE_REASON" is reason token from package fallback attempt.
#
# Returns: Combined reason token.
# ------------------------------------------------------------------------------
def get_transfer_failure_reason(FILE_REASON: str, PACKAGE_REASON: str) -> str:
    FILE_TOKEN = FILE_REASON or "download_failed"
    PACKAGE_TOKEN = PACKAGE_REASON or "package_download_failed"

    if PACKAGE_TOKEN in {"not_directory_node", "package_children_unavailable"}:
        return FILE_TOKEN

    if PACKAGE_TOKEN == FILE_TOKEN:
        return FILE_TOKEN

    if not FILE_REASON:
        return PACKAGE_TOKEN

    if not PACKAGE_REASON:
        return FILE_TOKEN

    return f"{FILE_TOKEN}; fallback={PACKAGE_TOKEN}"


# ------------------------------------------------------------------------------
# This function normalises transfer failure reason strings for summary logging.
#
# 1. "RAW_REASON" is the reason returned by transfer execution.
#
# Returns: Canonical reason token used for aggregate diagnostics.
# ------------------------------------------------------------------------------
def normalise_transfer_reason(RAW_REASON: str) -> str:
    CLEAN_REASON = RAW_REASON.strip().lower()
    if not CLEAN_REASON:
        return "unknown"

    PRIMARY_REASON = CLEAN_REASON.split(";", 1)[0].strip()
    if PRIMARY_REASON:
        return PRIMARY_REASON

    return "unknown"


# ------------------------------------------------------------------------------
# This function checks whether a remote path uses a known package suffix.
#
# 1. "REMOTE_PATH" is slash-separated iCloud path.
#
# Returns: True when path suffix matches known package types.
# ------------------------------------------------------------------------------
def is_known_package_path(REMOTE_PATH: str) -> bool:
    PATH_LOWER = REMOTE_PATH.strip().lower()
    if not PATH_LOWER:
        return False

    return PATH_LOWER.endswith(KNOWN_PACKAGE_SUFFIXES)


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
