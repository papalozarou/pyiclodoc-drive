# ------------------------------------------------------------------------------
# This module encapsulates backup execution and backup-run diagnostics logging.
# ------------------------------------------------------------------------------

from __future__ import annotations

from importlib import metadata as importlib_metadata
import os
import time

from app.syncer import get_transfer_worker_count, perform_incremental_sync
from app.telegram_messages import build_backup_complete_message, build_backup_started_message


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
# This function formats average transfer speed using binary megabytes per second.
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
    CONFIG,
    LOG_FILE,
    LOG_LINE_FN,
    GET_BUILD_DETAIL_FN=get_build_detail,
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
# 5. "TRIGGER" is backup trigger context.
# 6. "APPLE_ID_LABEL" is formatted Apple ID label.
# 7. "SCHEDULE_LINE" is formatted schedule line.
# 8. "LOAD_MANIFEST_FN" loads persisted manifest.
# 9. "SAVE_MANIFEST_FN" persists refreshed manifest.
# 10. "LOG_LINE_FN" writes worker logs.
# 11. "NOTIFY_FN" sends Telegram notifications.
# 12. "FORMAT_DURATION_FN" formats elapsed run time.
# 13. "FORMAT_SPEED_FN" formats average transfer speed.
# 14. "LOG_SETTINGS_FN" logs effective backup settings.
#
# Returns: None.
# ------------------------------------------------------------------------------
def run_backup(
    CLIENT,
    CONFIG,
    TELEGRAM,
    LOG_FILE,
    TRIGGER: str,
    APPLE_ID_LABEL: str,
    SCHEDULE_LINE: str,
    LOAD_MANIFEST_FN,
    SAVE_MANIFEST_FN,
    LOG_LINE_FN,
    NOTIFY_FN,
    FORMAT_DURATION_FN=format_duration_clock,
    FORMAT_SPEED_FN=format_average_speed,
    LOG_SETTINGS_FN=log_effective_backup_settings,
    PERFORM_SYNC_FN=perform_incremental_sync,
) -> None:
    LOG_SETTINGS_FN(CONFIG, LOG_FILE, LOG_LINE_FN)
    MANIFEST = LOAD_MANIFEST_FN(CONFIG.manifest_path)
    LOG_LINE_FN(LOG_FILE, "debug", f"Loaded manifest entries: {len(MANIFEST)}")
    RUN_START_EPOCH = int(time.time())
    NOTIFY_FN(
        TELEGRAM,
        build_backup_started_message(APPLE_ID_LABEL, SCHEDULE_LINE),
    )

    SUMMARY, NEW_MANIFEST = PERFORM_SYNC_FN(
        CLIENT,
        CONFIG.output_dir,
        MANIFEST,
        CONFIG.sync_workers,
        LOG_FILE,
        BACKUP_DELETE_REMOVED=CONFIG.backup_delete_removed,
    )
    LOG_LINE_FN(
        LOG_FILE,
        "debug",
        "Sync summary detail: "
        f"total={SUMMARY.total_files}, "
        f"transferred={SUMMARY.transferred_files}, "
        f"bytes={SUMMARY.transferred_bytes}, "
        f"skipped={SUMMARY.skipped_files}, "
        f"errors={SUMMARY.error_files}, "
        f"manifest_entries={len(NEW_MANIFEST)}",
    )
    TRAVERSAL_COMPLETE = bool(getattr(SUMMARY, "traversal_complete", True))
    TRAVERSAL_HARD_FAILURES = max(int(getattr(SUMMARY, "traversal_hard_failures", 0)), 0)
    DELETE_PHASE_SKIPPED = bool(getattr(SUMMARY, "delete_phase_skipped", False))

    if TRAVERSAL_COMPLETE:
        SAVE_MANIFEST_FN(CONFIG.manifest_path, NEW_MANIFEST)
    else:
        LOG_LINE_FN(
            LOG_FILE,
            "error",
            "Manifest save skipped because traversal was incomplete.",
        )

    DURATION_SECONDS = int(time.time()) - RUN_START_EPOCH
    AVERAGE_SPEED = FORMAT_SPEED_FN(SUMMARY.transferred_bytes, DURATION_SECONDS)
    STATUS_LINES: list[str] = []

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

    STATUS_LINES.extend(
        [
            f"Transferred: {SUMMARY.transferred_files}/{SUMMARY.total_files}",
            f"Skipped: {SUMMARY.skipped_files}",
            f"Errors: {SUMMARY.error_files}",
            f"Duration: {FORMAT_DURATION_FN(DURATION_SECONDS)}",
        ]
    )

    if SUMMARY.transferred_files > 0:
        STATUS_LINES.append(f"Average speed: {AVERAGE_SPEED}")

    COMPLETION_MESSAGE = build_backup_complete_message(APPLE_ID_LABEL, STATUS_LINES)
    NOTIFY_FN(TELEGRAM, COMPLETION_MESSAGE)
    LOG_LINE_FN(
        LOG_FILE,
        "info" if TRAVERSAL_COMPLETE else "error",
        "Backup complete. " if TRAVERSAL_COMPLETE else "Backup completed with incomplete traversal. "
        f"Transferred {SUMMARY.transferred_files}/{SUMMARY.total_files}, "
        f"skipped {SUMMARY.skipped_files}, errors {SUMMARY.error_files}.",
    )
