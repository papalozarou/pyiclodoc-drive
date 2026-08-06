# ------------------------------------------------------------------------------
# This module encapsulates backup execution and backup-run diagnostics logging.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
import os
import time
from typing import Any, Protocol
from app.config import AppConfig
from app.icloud_client import ICloudDriveClient
from app.syncer import SyncResult, get_transfer_worker_count, perform_incremental_sync
from app.telegram_bot import TelegramConfig
from app.telegram_messages import build_backup_complete_message, build_backup_started_message

ManifestDict = dict[str, dict[str, Any]]
BuildDetail = dict[str, str]


# ------------------------------------------------------------------------------
# This protocol loads manifest state from disk for backup execution.
# ------------------------------------------------------------------------------
class LoadManifestFn(Protocol):
    def __call__(self, PATH: Path) -> ManifestDict:
        ...


# ------------------------------------------------------------------------------
# This protocol saves manifest state to disk after a successful run.
# ------------------------------------------------------------------------------
class SaveManifestFn(Protocol):
    def __call__(self, PATH: Path, MANIFEST: ManifestDict) -> bool:
        ...


# ------------------------------------------------------------------------------
# This protocol writes worker log lines during backup execution.
# ------------------------------------------------------------------------------
class LogLineFn(Protocol):
    def __call__(self, PATH: Path, LEVEL: str, MESSAGE: str) -> None:
        ...


# ------------------------------------------------------------------------------
# This protocol sends operator notifications through Telegram.
# ------------------------------------------------------------------------------
class NotifyFn(Protocol):
    def __call__(self, TELEGRAM: TelegramConfig, MESSAGE: str) -> None:
        ...


# ------------------------------------------------------------------------------
# This protocol returns runtime build metadata for diagnostics logging.
# ------------------------------------------------------------------------------
class GetBuildDetailFn(Protocol):
    def __call__(self) -> BuildDetail:
        ...


# ------------------------------------------------------------------------------
# This protocol formats elapsed duration for completion reporting.
# ------------------------------------------------------------------------------
class FormatDurationFn(Protocol):
    def __call__(self, TOTAL_SECONDS: int) -> str:
        ...


# ------------------------------------------------------------------------------
# This protocol formats transfer speed for completion reporting.
# ------------------------------------------------------------------------------
class FormatSpeedFn(Protocol):
    def __call__(self, TRANSFERRED_BYTES: int, DURATION_SECONDS: int) -> str:
        ...


# ------------------------------------------------------------------------------
# This protocol performs the incremental sync and returns summary plus manifest.
# ------------------------------------------------------------------------------
class PerformSyncFn(Protocol):
    def __call__(
        self,
        CLIENT: ICloudDriveClient,
        OUTPUT_DIR: Path,
        MANIFEST: ManifestDict,
        SYNC_WORKERS: int,
        LOG_FILE: Path,
        *,
        BACKUP_DELETE_REMOVED: bool,
    ) -> tuple[SyncResult, ManifestDict]:
        ...


# ------------------------------------------------------------------------------
# This function formats elapsed seconds as "HH:MM:SS".
#
# 1. "TOTAL_SECONDS" is elapsed duration in seconds.
#
# Returns: Zero-padded duration string.
# ------------------------------------------------------------------------------
def format_duration_clock(TOTAL_SECONDS: int) -> str:
    SAFE_SECONDS = max(TOTAL_SECONDS, 0)
    HOURS = SAFE_SECONDS // 3600
    MINUTES = (SAFE_SECONDS % 3600) // 60
    SECONDS = SAFE_SECONDS % 60
    return f"{HOURS:02d}:{MINUTES:02d}:{SECONDS:02d}"


# ------------------------------------------------------------------------------
# This function formats average transfer speed using binary megabytes per
# second.
#
# 1. "TRANSFERRED_BYTES" is successful download byte total.
# 2. "DURATION_SECONDS" is elapsed run duration in seconds.
#
# Returns: Human-readable transfer speed string.
# ------------------------------------------------------------------------------
def format_average_speed(TRANSFERRED_BYTES: int, DURATION_SECONDS: int) -> str:
    SAFE_BYTES = max(TRANSFERRED_BYTES, 0)
    SAFE_DURATION_SECONDS = max(DURATION_SECONDS, 1)
    MEBIBYTES_PER_SECOND = SAFE_BYTES / SAFE_DURATION_SECONDS / (1024 * 1024)
    return f"{MEBIBYTES_PER_SECOND:.2f} MiB/s"


# ------------------------------------------------------------------------------
# This function formats deleted-path summary text for completion messages.
#
# 1. "DELETED_FILES" is the count of deleted local files.
# 2. "DELETED_DIRECTORIES" is the count of deleted local directories.
#
# Returns: Human-readable delete summary with natural singular/plural wording.
# ------------------------------------------------------------------------------
def format_deleted_summary(DELETED_FILES: int, DELETED_DIRECTORIES: int) -> str:
    SAFE_DELETED_FILES = max(int(DELETED_FILES), 0)
    SAFE_DELETED_DIRECTORIES = max(int(DELETED_DIRECTORIES), 0)
    FILE_LABEL = "file" if SAFE_DELETED_FILES == 1 else "files"
    DIRECTORY_LABEL = "directory" if SAFE_DELETED_DIRECTORIES == 1 else "directories"
    return (
        f"Deleted: {SAFE_DELETED_FILES} {FILE_LABEL}, "
        f"{SAFE_DELETED_DIRECTORIES} {DIRECTORY_LABEL}"
    )


# ------------------------------------------------------------------------------
# This function returns runtime build metadata for startup diagnostics.
#
# Returns: Mapping with app build ref and pyicloud package version.
# ------------------------------------------------------------------------------
def get_build_detail() -> dict[str, str]:
    APP_BUILD_REF = os.getenv("C_APP_BUILD_REF", "unknown").strip() or "unknown"

    try:
        PYICLOUD_VERSION = importlib_metadata.version("pyicloud")
    except importlib_metadata.PackageNotFoundError:
        PYICLOUD_VERSION = "unknown"

    return {
        "app_build_ref": APP_BUILD_REF,
        "pyicloud_version": PYICLOUD_VERSION,
    }


# ------------------------------------------------------------------------------
# This data class groups runtime callbacks used by backup execution.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class BackupRuntimeDeps:
    load_manifest_fn: LoadManifestFn
    save_manifest_fn: SaveManifestFn
    log_line_fn: LogLineFn
    notify_fn: NotifyFn
    get_build_detail_fn: GetBuildDetailFn = get_build_detail
    format_duration_fn: FormatDurationFn = format_duration_clock
    format_speed_fn: FormatSpeedFn = format_average_speed
    perform_sync_fn: PerformSyncFn = perform_incremental_sync


# ------------------------------------------------------------------------------
# This data class models the completed backup run for caller inspection.
#
# 1. "summary" is the sync summary returned by incremental sync.
# 2. "manifest_updated" records whether the manifest was persisted.
# 3. "total_errors" is the combined transfer and delete error count.
# 4. "completion_message" is the Telegram completion message body.
# 5. "completion_log_message" is the final worker log line.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class BackupRunResult:
    summary: SyncResult
    manifest_updated: bool
    total_errors: int
    completion_message: str
    completion_log_message: str


# ------------------------------------------------------------------------------
# This function logs effective non-secret backup settings for debug runs.
#
# 1. "CONFIG" is runtime configuration.
# 2. "LOG_FILE" is worker log destination.
# 3. "LOG_LINE_FN" writes worker logs.
# 4. "GET_BUILD_DETAIL_FN" returns build metadata.
#
# Returns: None.
# ------------------------------------------------------------------------------
def log_effective_backup_settings(
    CONFIG: AppConfig,
    LOG_FILE: Path,
    LOG_LINE_FN: LogLineFn,
    GET_BUILD_DETAIL_FN: GetBuildDetailFn = get_build_detail,
) -> None:
    SYNC_WORKERS_LABEL = "auto" if CONFIG.sync_workers == 0 else str(CONFIG.sync_workers)
    EFFECTIVE_WORKERS = get_transfer_worker_count(CONFIG.sync_workers)
    BUILD_DETAIL = GET_BUILD_DETAIL_FN()
    LOG_LINE_FN(
        LOG_FILE,
        "debug",
        "Build detail: "
        f"app_build_ref={BUILD_DETAIL['app_build_ref']}, "
        f"pyicloud_version={BUILD_DETAIL['pyicloud_version']}",
    )
    LOG_LINE_FN(
        LOG_FILE,
        "debug",
        "Effective backup settings detail: "
        f"run_once={CONFIG.run_once}, "
        f"schedule_mode={CONFIG.schedule_mode}, "
        f"schedule_interval_minutes={CONFIG.schedule_interval_minutes}, "
        f"schedule_backup_time={CONFIG.schedule_backup_time}, "
        f"schedule_weekdays={CONFIG.schedule_weekdays}, "
        f"schedule_monthly_week={CONFIG.schedule_monthly_week}, "
        f"sync_traversal_workers={CONFIG.traversal_workers}, "
        f"sync_download_workers={SYNC_WORKERS_LABEL}, "
        f"effective_download_workers={EFFECTIVE_WORKERS}, "
        f"sync_download_chunk_mib={CONFIG.download_chunk_mib}, "
        f"backup_delete_removed={CONFIG.backup_delete_removed}",
    )


# ------------------------------------------------------------------------------
# This function executes one backup pass and persists refreshed manifest data.
#
# 1. "CLIENT" is iCloud client wrapper.
# 2. "CONFIG" is runtime configuration.
# 3. "TELEGRAM" is Telegram integration configuration.
# 4. "LOG_FILE" is worker log destination.
# 5. "APPLE_ID_LABEL" is formatted Apple ID label.
# 6. "SCHEDULE_LINE" is formatted schedule line.
# 7. "DEPS" groups runtime callbacks used by backup execution.
#
# Returns: "BackupRunResult" for the completed backup pass.
# ------------------------------------------------------------------------------
def run_backup(
    CLIENT: ICloudDriveClient,
    CONFIG: AppConfig,
    TELEGRAM: TelegramConfig,
    LOG_FILE: Path,
    APPLE_ID_LABEL: str,
    SCHEDULE_LINE: str,
    DEPS: BackupRuntimeDeps,
) -> BackupRunResult:
    DEPS.log_line_fn(
        LOG_FILE,
        "debug",
        "Backup run started: "
        f"apple_id_label={APPLE_ID_LABEL}, "
        f"schedule_line={SCHEDULE_LINE}, "
        f"manifest_path={CONFIG.manifest_path.as_posix()}, "
        f"output_dir={CONFIG.output_dir.as_posix()}",
    )
    log_effective_backup_settings(
        CONFIG,
        LOG_FILE,
        DEPS.log_line_fn,
        DEPS.get_build_detail_fn,
    )
    MANIFEST = DEPS.load_manifest_fn(CONFIG.manifest_path)
    DEPS.log_line_fn(LOG_FILE, "debug", f"Loaded manifest entries: {len(MANIFEST)}")
    RUN_START_EPOCH = int(time.time())
    DEPS.notify_fn(
        TELEGRAM,
        build_backup_started_message(APPLE_ID_LABEL, SCHEDULE_LINE),
    )

    SUMMARY, NEW_MANIFEST = DEPS.perform_sync_fn(
        CLIENT,
        CONFIG.output_dir,
        MANIFEST,
        CONFIG.sync_workers,
        LOG_FILE,
        BACKUP_DELETE_REMOVED=CONFIG.backup_delete_removed,
    )
    DELETE_ERRORS = max(int(SUMMARY.delete_errors), 0)
    TOTAL_ERRORS = max(int(SUMMARY.error_files), 0) + DELETE_ERRORS
    DEPS.log_line_fn(
        LOG_FILE,
        "debug",
        "Sync summary detail: "
        f"total={SUMMARY.total_files}, "
        f"transferred={SUMMARY.transferred_files}, "
        f"bytes={SUMMARY.transferred_bytes}, "
        f"skipped={SUMMARY.skipped_files}, "
        f"transfer_errors={SUMMARY.error_files}, "
        f"delete_errors={DELETE_ERRORS}, "
        f"total_errors={TOTAL_ERRORS}, "
        f"manifest_entries={len(NEW_MANIFEST)}",
    )
    TRAVERSAL_COMPLETE = bool(SUMMARY.traversal_complete)
    TRAVERSAL_HARD_FAILURES = max(int(SUMMARY.traversal_hard_failures), 0)
    DELETE_PHASE_SKIPPED = bool(SUMMARY.delete_phase_skipped)
    SESSION_INVALID = bool(SUMMARY.session_invalid)
    MANIFEST_UPDATED = False

    if TRAVERSAL_COMPLETE:
        MANIFEST_UPDATED = DEPS.save_manifest_fn(CONFIG.manifest_path, NEW_MANIFEST)
        MANIFEST_REASON = "save_succeeded" if MANIFEST_UPDATED else "save_failed"
        DEPS.log_line_fn(
            LOG_FILE,
            "debug",
            "Manifest save detail: "
            f"path={CONFIG.manifest_path.as_posix()}, "
            f"entries={len(NEW_MANIFEST)}, "
            f"reason={MANIFEST_REASON}",
        )
        if not MANIFEST_UPDATED:
            DEPS.log_line_fn(
                LOG_FILE,
                "error",
                "Manifest save failed after traversal completed.",
            )
    else:
        DEPS.log_line_fn(
            LOG_FILE,
            "error",
            "Manifest save skipped because traversal was incomplete.",
        )
        DEPS.log_line_fn(
            LOG_FILE,
            "debug",
            "Manifest save detail: "
            f"path={CONFIG.manifest_path.as_posix()}, "
            f"candidate_entries={len(NEW_MANIFEST)}, "
            f"traversal_hard_failures={TRAVERSAL_HARD_FAILURES}, "
            "reason=traversal_incomplete",
        )

    DURATION_SECONDS = int(time.time()) - RUN_START_EPOCH
    AVERAGE_SPEED = DEPS.format_speed_fn(SUMMARY.transferred_bytes, DURATION_SECONDS)
    STATUS_LINES: list[str] = []

    if SESSION_INVALID:
        STATUS_LINES.append(
            "Status: Failure looks like a dead iCloud session, "
            "reauthentication may be required"
        )

    if not TRAVERSAL_COMPLETE:
        STATUS_LINES.extend(
            [
                "Status: Partial run due to incomplete traversal",
                f"Traversal hard failures: {TRAVERSAL_HARD_FAILURES}",
                "Manifest: Not updated",
            ]
        )

        if DELETE_PHASE_SKIPPED:
            STATUS_LINES.append("Delete removed: Skipped because traversal was incomplete")
    elif not MANIFEST_UPDATED:
        STATUS_LINES.append("Manifest: Save failed after traversal completed")

    STATUS_LINES.extend(
        [
            f"Transferred: {SUMMARY.transferred_files}/{SUMMARY.total_files}",
            format_deleted_summary(
                SUMMARY.deleted_files,
                SUMMARY.deleted_directories,
            ),
            f"Skipped: {SUMMARY.skipped_files}",
            f"Errors: {TOTAL_ERRORS}",
            f"Duration: {DEPS.format_duration_fn(DURATION_SECONDS)}",
        ]
    )

    if DELETE_ERRORS > 0:
        STATUS_LINES.append(f"Delete errors: {DELETE_ERRORS}")

    if SUMMARY.transferred_files > 0:
        STATUS_LINES.append(f"Average speed: {AVERAGE_SPEED}")

    DEPS.log_line_fn(
        LOG_FILE,
        "debug",
        "Backup completion detail: "
        f"duration_seconds={DURATION_SECONDS}, "
        f"average_speed={AVERAGE_SPEED}, "
        f"session_invalid={SESSION_INVALID}, "
        f"status_lines={len(STATUS_LINES)}",
    )
    COMPLETION_MESSAGE = build_backup_complete_message(APPLE_ID_LABEL, STATUS_LINES)
    COMPLETION_LOG_PREFIX = (
        "Backup complete."
        if TRAVERSAL_COMPLETE and MANIFEST_UPDATED
        else (
            "Backup completed with incomplete traversal."
            if not TRAVERSAL_COMPLETE
            else "Backup completed but manifest save failed."
        )
    )
    COMPLETION_LOG_MESSAGE = (
        f"{COMPLETION_LOG_PREFIX} "
        f"Transferred {SUMMARY.transferred_files}/{SUMMARY.total_files}, "
        f"skipped {SUMMARY.skipped_files}, errors {TOTAL_ERRORS}."
    )
    DEPS.notify_fn(TELEGRAM, COMPLETION_MESSAGE)
    DEPS.log_line_fn(
        LOG_FILE,
        "info" if TRAVERSAL_COMPLETE and MANIFEST_UPDATED else "error",
        COMPLETION_LOG_MESSAGE,
    )
    return BackupRunResult(
        summary=SUMMARY,
        manifest_updated=MANIFEST_UPDATED,
        total_errors=TOTAL_ERRORS,
        completion_message=COMPLETION_MESSAGE,
        completion_log_message=COMPLETION_LOG_MESSAGE,
    )
