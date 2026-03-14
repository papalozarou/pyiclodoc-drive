# ------------------------------------------------------------------------------
# This module runs the backup worker loop and coordinates auth and sync.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import metadata as importlib_metadata
import os
from pathlib import Path
import threading
import time

from dateutil import parser as date_parser

from app.config import AppConfig, load_config
from app.credential_store import configure_keyring, load_credentials, save_credentials
from app.icloud_client import ICloudDriveClient
from app.logger import log_line
from app.scheduler import (
    MONTHLY_WEEK_MAP,
    calculate_next_daily_run_epoch,
    calculate_next_monthly_run_epoch,
    calculate_next_twice_weekly_run_epoch,
    calculate_next_weekly_run_epoch,
    format_schedule_description,
    format_schedule_line,
    get_monthly_weekday_day,
    get_next_run_epoch,
    parse_daily,
    parse_weekday,
    parse_weekday_list,
)
from app.state import AuthState, load_auth_state, load_manifest, now_iso, save_auth_state, save_manifest
from app.syncer import get_transfer_worker_count, perform_incremental_sync, run_first_time_safety_net
from app.telegram_bot import TelegramConfig, fetch_updates, parse_command, send_message
from app.telegram_messages import (
    build_authentication_complete_message,
    build_authentication_failed_message,
    build_authentication_required_message,
    build_backup_complete_message,
    build_backup_requested_message,
    build_backup_skipped_auth_incomplete_message,
    build_backup_skipped_reauth_pending_message,
    build_backup_started_message,
    build_container_started_message,
    build_container_stopped_message,
    build_one_shot_waiting_for_auth_message,
    build_reauth_reminder_message,
    build_reauthentication_required_for_apple_id_message,
    build_reauthentication_required_message,
    build_safety_net_blocked_message,
)
from app.time_utils import now_local
RUN_ONCE_AUTH_WAIT_SECONDS = 900
RUN_ONCE_AUTH_POLL_SECONDS = 5
HEARTBEAT_TOUCH_INTERVAL_SECONDS = 30


# ------------------------------------------------------------------------------
# This function validates required runtime configuration.
#
# 1. "CONFIG" is the loaded runtime configuration model.
#
# Returns: Validation error list; empty list means configuration is usable.
# ------------------------------------------------------------------------------
def validate_config(CONFIG: AppConfig) -> list[str]:
    ERRORS: list[str] = []

    if not CONFIG.icloud_email:
        ERRORS.append("ICLOUD_EMAIL is required.")

    if not CONFIG.icloud_password:
        ERRORS.append("ICLOUD_PASSWORD is required.")

    if CONFIG.schedule_mode not in {"interval", "daily", "weekly", "twice_weekly", "monthly"}:
        ERRORS.append(
            "SCHEDULE_MODE must be one of: interval, daily, weekly, twice_weekly, monthly."
        )

    if CONFIG.schedule_mode == "daily" and parse_daily(CONFIG.schedule_backup_time) is None:
        ERRORS.append("SCHEDULE_BACKUP_TIME must use 24-hour HH:MM format.")

    if (
        CONFIG.schedule_mode == "weekly"
        and parse_weekday_list(CONFIG.schedule_weekdays, 1) is None
    ):
        ERRORS.append(
            "SCHEDULE_WEEKDAYS must contain exactly one valid weekday name for weekly mode."
        )

    if (
        CONFIG.schedule_mode == "twice_weekly"
        and parse_weekday_list(CONFIG.schedule_weekdays, 2) is None
    ):
        ERRORS.append("SCHEDULE_WEEKDAYS must contain exactly two distinct weekday names.")

    if CONFIG.schedule_mode == "monthly":
        if parse_weekday_list(CONFIG.schedule_weekdays, 1) is None:
            ERRORS.append(
                "SCHEDULE_WEEKDAYS must contain exactly one valid weekday name for monthly mode."
            )

        if CONFIG.schedule_monthly_week not in MONTHLY_WEEK_MAP:
            ERRORS.append("SCHEDULE_MONTHLY_WEEK must be one of: first, second, third, fourth, last.")

        if parse_daily(CONFIG.schedule_backup_time) is None:
            ERRORS.append("SCHEDULE_BACKUP_TIME must use 24-hour HH:MM format.")

    if (
        CONFIG.schedule_mode == "interval"
        and not CONFIG.run_once
        and CONFIG.schedule_interval_minutes < 1
    ):
        ERRORS.append(
            "SCHEDULE_INTERVAL_MINUTES must be at least 1 when RUN_ONCE is false."
        )

    if CONFIG.traversal_workers < 1 or CONFIG.traversal_workers > 8:
        ERRORS.append("SYNC_TRAVERSAL_WORKERS must be an integer between 1 and 8.")

    if CONFIG.sync_workers < 0 or CONFIG.sync_workers > 16:
        ERRORS.append("SYNC_DOWNLOAD_WORKERS must be auto or an integer between 1 and 16.")

    if CONFIG.download_chunk_mib < 1 or CONFIG.download_chunk_mib > 16:
        ERRORS.append("SYNC_DOWNLOAD_CHUNK_MIB must be an integer between 1 and 16.")

    return ERRORS


# ------------------------------------------------------------------------------
# This function parses an ISO timestamp with a strict epoch fallback.
#
# 1. "VALUE" is an ISO-formatted timestamp string.
#
# Returns: Offset-aware datetime; Unix epoch when parsing fails.
#
# Notes: dateutil parsing reference:
# https://dateutil.readthedocs.io/en/stable/parser.html
# ------------------------------------------------------------------------------
def parse_iso(VALUE: str) -> datetime:
    try:
        return date_parser.isoparse(VALUE)
    except (TypeError, ValueError, OverflowError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------
# This function calculates remaining whole days before reauthentication.
#
# 1. "LAST_AUTH_UTC" is stored offset-aware auth timestamp.
# 2. "INTERVAL_DAYS" is the reauthentication interval in days.
#
# Returns: Remaining whole days before reauthentication should complete.
# ------------------------------------------------------------------------------
def reauth_days_left(LAST_AUTH_UTC: str, INTERVAL_DAYS: int) -> int:
    LAST_AUTH = parse_iso(LAST_AUTH_UTC)
    ELAPSED = now_local() - LAST_AUTH
    ELAPSED_DAYS = max(int(ELAPSED.total_seconds() // 86400), 0)
    return INTERVAL_DAYS - ELAPSED_DAYS


# ------------------------------------------------------------------------------
# This function updates the healthcheck heartbeat file timestamp.
#
# 1. "PATH" is the heartbeat file path.
#
# Returns: None.
# ------------------------------------------------------------------------------
def update_heartbeat(PATH: Path) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.touch()
    except OSError:
        return


# ------------------------------------------------------------------------------
# This function starts a daemon heartbeat updater thread.
#
# 1. "PATH" is the heartbeat file path.
#
# Returns: Stop-event used to end the updater loop on process exit.
# ------------------------------------------------------------------------------
def start_heartbeat_updater(PATH: Path) -> threading.Event:
    STOP_EVENT = threading.Event()

    def run_heartbeat_loop() -> None:
        update_heartbeat(PATH)

        while not STOP_EVENT.wait(HEARTBEAT_TOUCH_INTERVAL_SECONDS):
            update_heartbeat(PATH)

    THREAD = threading.Thread(target=run_heartbeat_loop, daemon=True)
    THREAD.start()
    return STOP_EVENT


# ------------------------------------------------------------------------------
# This function sends a Telegram message when integration is configured.
#
# 1. "TELEGRAM" is Telegram integration configuration.
# 2. "MESSAGE" is outgoing message content.
#
# Returns: None.
# ------------------------------------------------------------------------------
def notify(TELEGRAM: TelegramConfig, MESSAGE: str) -> None:
    send_message(TELEGRAM, MESSAGE)


# ------------------------------------------------------------------------------
# This function formats a fallback-safe Apple ID label for Telegram messages.
#
# 1. "APPLE_ID" is the configured iCloud email value.
#
# Returns: Non-empty Apple ID label.
# ------------------------------------------------------------------------------
def format_apple_id_label(APPLE_ID: str) -> str:
    CLEAN_VALUE = APPLE_ID.strip()

    if CLEAN_VALUE:
        return CLEAN_VALUE

    return "<unknown>"


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
# This function executes authentication and persists updated auth state.
#
# 1. "CLIENT" is iCloud client wrapper.
# 2. "AUTH_STATE" is current auth state.
# 3. "AUTH_STATE_PATH" is auth state file path.
# 4. "TELEGRAM" is Telegram integration configuration.
# 5. "USERNAME" is command prefix used by Telegram control.
# 6. "PROVIDED_CODE" is optional MFA code.
#
# Returns: Tuple "(new_state, is_authenticated, details_message)".
# ------------------------------------------------------------------------------
def attempt_auth(
    CLIENT: ICloudDriveClient,
    AUTH_STATE: AuthState,
    AUTH_STATE_PATH: Path,
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    APPLE_ID: str,
    PROVIDED_CODE: str,
) -> tuple[AuthState, bool, str]:
    CODE = PROVIDED_CODE.strip()
    APPLE_ID_LABEL = format_apple_id_label(APPLE_ID)

    if CODE:
        IS_SUCCESS, DETAILS = CLIENT.complete_authentication(CODE)
    else:
        IS_SUCCESS, DETAILS = CLIENT.start_authentication()

    if IS_SUCCESS:
        NEW_STATE = AuthState(
            last_auth_utc=now_iso(),
            auth_pending=False,
            reauth_pending=False,
            reminder_stage="none",
        )
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        notify(
            TELEGRAM,
            build_authentication_complete_message(APPLE_ID_LABEL, DETAILS),
        )
        return NEW_STATE, True, DETAILS

    if "Two-factor code is required" in DETAILS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        notify(
            TELEGRAM,
            build_authentication_required_message(APPLE_ID_LABEL, USERNAME),
        )
        return NEW_STATE, False, DETAILS

    NEW_STATE = replace(AUTH_STATE, auth_pending=True)
    save_auth_state(AUTH_STATE_PATH, NEW_STATE)
    notify(
        TELEGRAM,
        build_authentication_failed_message(APPLE_ID_LABEL, DETAILS),
    )
    return NEW_STATE, False, DETAILS


# ------------------------------------------------------------------------------
# This function enforces first-run safety checks before backups are allowed.
#
# 1. "CONFIG" is runtime configuration.
# 2. "TELEGRAM" is Telegram integration configuration.
# 3. "LOG_FILE" is worker log path.
#
# Returns: True when backup can proceed; otherwise False.
# ------------------------------------------------------------------------------
def enforce_safety_net(CONFIG: AppConfig, TELEGRAM: TelegramConfig, LOG_FILE: Path) -> bool:
    DONE_MARKER = CONFIG.config_dir / "pyiclodoc-drive-safety_net_done.flag"
    BLOCKED_MARKER = CONFIG.config_dir / "pyiclodoc-drive-safety_net_blocked.flag"

    if DONE_MARKER.exists():
        return True

    RESULT = run_first_time_safety_net(CONFIG.output_dir, CONFIG.safety_net_sample_size)

    if not RESULT.should_block and BLOCKED_MARKER.exists():
        BLOCKED_MARKER.unlink()

    if not RESULT.should_block:
        DONE_MARKER.write_text("ok\n", encoding="utf-8")
        log_line(LOG_FILE, "info", "First-run safety net passed.")
        return True

    if BLOCKED_MARKER.exists():
        return False

    MISMATCH_TEXT = "\n".join(RESULT.mismatched_samples)
    log_line(LOG_FILE, "error", "Safety net blocked backup due to permissions.")
    log_line(LOG_FILE, "error", MISMATCH_TEXT)
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
    SAMPLE_TEXT = ", ".join(RESULT.mismatched_samples[:2]) or "<none>"
    notify(
        TELEGRAM,
        build_safety_net_blocked_message(
            APPLE_ID_LABEL, RESULT.expected_uid, RESULT.expected_gid, SAMPLE_TEXT
        ),
    )
    BLOCKED_MARKER.write_text("blocked\n", encoding="utf-8")
    return False


# ------------------------------------------------------------------------------
# This function applies 5-day and 2-day reauthentication reminder stages.
#
# 1. "AUTH_STATE" is current auth state.
# 2. "AUTH_STATE_PATH" is persistence file path.
# 3. "TELEGRAM" is Telegram integration configuration.
# 4. "USERNAME" is Telegram command prefix.
# 5. "INTERVAL_DAYS" is reauthentication interval in days.
#
# Returns: Updated authentication state.
# ------------------------------------------------------------------------------
def process_reauth_reminders(
    AUTH_STATE: AuthState,
    AUTH_STATE_PATH: Path,
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    INTERVAL_DAYS: int,
) -> AuthState:
    DAYS_LEFT = reauth_days_left(AUTH_STATE.last_auth_utc, INTERVAL_DAYS)

    if DAYS_LEFT > 5:
        NEW_STATE = replace(AUTH_STATE, reminder_stage="none", reauth_pending=False)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 2 and AUTH_STATE.reminder_stage != "prompt2":
        notify(
            TELEGRAM,
            build_reauthentication_required_message(USERNAME),
        )
        NEW_STATE = replace(AUTH_STATE, reminder_stage="prompt2", reauth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 5 and AUTH_STATE.reminder_stage == "none":
        notify(
            TELEGRAM,
            build_reauth_reminder_message(USERNAME),
        )
        NEW_STATE = replace(AUTH_STATE, reminder_stage="alert5")
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    return AUTH_STATE


# ------------------------------------------------------------------------------
# This function polls Telegram and returns parsed command intents.
#
# 1. "TELEGRAM" is Telegram configuration.
# 2. "USERNAME" is command prefix.
# 3. "UPDATE_OFFSET" is update offset cursor.
#
# Returns: Tuple "(commands, next_offset)" for command execution.
# ------------------------------------------------------------------------------
def process_commands(
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    UPDATE_OFFSET: int | None,
) -> tuple[list[tuple[str, str]], int | None]:
    UPDATES = fetch_updates(TELEGRAM, UPDATE_OFFSET)

    if not UPDATES:
        return [], UPDATE_OFFSET

    COMMANDS: list[tuple[str, str]] = []
    MAX_UPDATE = UPDATE_OFFSET or 0

    for UPDATE in UPDATES:
        EVENT = parse_command(UPDATE, USERNAME, TELEGRAM.chat_id)
        UPDATE_ID = int(UPDATE.get("update_id", 0))
        MAX_UPDATE = max(MAX_UPDATE, UPDATE_ID + 1)

        if EVENT is None:
            continue

        COMMANDS.append((EVENT.command, EVENT.args))

    return COMMANDS, MAX_UPDATE


# ------------------------------------------------------------------------------
# This function executes one backup pass and persists refreshed manifest data.
#
# 1. "CLIENT" is iCloud client wrapper.
# 2. "CONFIG" is runtime configuration.
# 3. "TELEGRAM" is Telegram integration configuration.
# 4. "LOG_FILE" is worker log destination.
#
# Returns: None.
# ------------------------------------------------------------------------------
def run_backup(
    CLIENT: ICloudDriveClient,
    CONFIG: AppConfig,
    TELEGRAM: TelegramConfig,
    LOG_FILE: Path,
    TRIGGER: str,
) -> None:
    log_effective_backup_settings(CONFIG, LOG_FILE)
    MANIFEST = load_manifest(CONFIG.manifest_path)
    log_line(LOG_FILE, "debug", f"Loaded manifest entries: {len(MANIFEST)}")
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
    RUN_START_EPOCH = int(time.time())
    SCHEDULE_LINE = format_schedule_line(CONFIG, TRIGGER)
    notify(
        TELEGRAM,
        build_backup_started_message(APPLE_ID_LABEL, SCHEDULE_LINE),
    )

    SUMMARY, NEW_MANIFEST = perform_incremental_sync(
        CLIENT,
        CONFIG.output_dir,
        MANIFEST,
        CONFIG.sync_workers,
        LOG_FILE,
        BACKUP_DELETE_REMOVED=CONFIG.backup_delete_removed,
    )
    log_line(
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
    save_manifest(CONFIG.manifest_path, NEW_MANIFEST)

    DURATION_SECONDS = int(time.time()) - RUN_START_EPOCH
    AVERAGE_SPEED = format_average_speed(SUMMARY.transferred_bytes, DURATION_SECONDS)
    STATUS_LINES = [
        f"Transferred: {SUMMARY.transferred_files}/{SUMMARY.total_files}",
        f"Skipped: {SUMMARY.skipped_files}",
        f"Errors: {SUMMARY.error_files}",
        f"Duration: {format_duration_clock(DURATION_SECONDS)}",
    ]

    if SUMMARY.transferred_files > 0:
        STATUS_LINES.append(f"Average speed: {AVERAGE_SPEED}")

    COMPLETION_MESSAGE = build_backup_complete_message(APPLE_ID_LABEL, STATUS_LINES)
    notify(TELEGRAM, COMPLETION_MESSAGE)
    log_line(
        LOG_FILE,
        "info",
        "Backup complete. "
        f"Transferred {SUMMARY.transferred_files}/{SUMMARY.total_files}, "
        f"skipped {SUMMARY.skipped_files}, errors {SUMMARY.error_files}.",
    )


# ------------------------------------------------------------------------------
# This function logs effective non-secret backup settings for debug runs.
#
# 1. "CONFIG" is runtime configuration.
# 2. "LOG_FILE" is worker log destination.
#
# Returns: None.
# ------------------------------------------------------------------------------
def log_effective_backup_settings(CONFIG: AppConfig, LOG_FILE: Path) -> None:
    SYNC_WORKERS_LABEL = "auto" if CONFIG.sync_workers == 0 else str(CONFIG.sync_workers)
    EFFECTIVE_WORKERS = get_transfer_worker_count(CONFIG.sync_workers)
    BUILD_DETAIL = get_build_detail()
    log_line(
        LOG_FILE,
        "debug",
        "Build detail: "
        f"app_build_ref={BUILD_DETAIL['app_build_ref']}, "
        f"pyicloud_version={BUILD_DETAIL['pyicloud_version']}",
    )
    log_line(
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
# This function handles a single Telegram command.
#
# 1. "COMMAND" is parsed command keyword.
# 2. "ARGS" is optional command payload.
# 3. "CONFIG" is runtime configuration.
# 4. "CLIENT" is iCloud client wrapper.
# 5. "AUTH_STATE" is current auth state.
# 6. "IS_AUTHENTICATED" tracks current auth validity.
# 7. "TELEGRAM" is Telegram integration configuration.
#
# Returns: Tuple "(auth_state, is_authenticated, backup_requested)".
# ------------------------------------------------------------------------------
def handle_command(
    COMMAND: str,
    ARGS: str,
    CONFIG: AppConfig,
    CLIENT: ICloudDriveClient,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    TELEGRAM: TelegramConfig,
) -> tuple[AuthState, bool, bool]:
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)

    if COMMAND == "backup":
        notify(
            TELEGRAM,
            build_backup_requested_message(APPLE_ID_LABEL),
        )
        return AUTH_STATE, IS_AUTHENTICATED, True

    if COMMAND == "auth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            build_authentication_required_message(
                APPLE_ID_LABEL, CONFIG.container_username
            ),
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    if COMMAND == "reauth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, reauth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            build_reauthentication_required_for_apple_id_message(
                APPLE_ID_LABEL, CONFIG.container_username
            ),
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    NEW_STATE, NEW_AUTH, DETAILS = attempt_auth(
        CLIENT,
        AUTH_STATE,
        CONFIG.auth_state_path,
        TELEGRAM,
        CONFIG.container_username,
        CONFIG.icloud_email,
        ARGS,
    )
    log_line(CONFIG.logs_dir / "pyiclodoc-drive-worker.log", "info", f"Auth command result: {DETAILS}")
    return NEW_STATE, NEW_AUTH, False


# ------------------------------------------------------------------------------
# This function waits for one-shot authentication commands before exit.
#
# 1. "CONFIG" is runtime configuration.
# 2. "CLIENT" is iCloud client wrapper.
# 3. "AUTH_STATE" is current auth state.
# 4. "IS_AUTHENTICATED" tracks current auth validity.
# 5. "TELEGRAM" is Telegram integration configuration.
#
# Returns: Tuple "(auth_state, is_authenticated)".
# ------------------------------------------------------------------------------
def wait_for_one_shot_auth(
    CONFIG: AppConfig,
    CLIENT: ICloudDriveClient,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    TELEGRAM: TelegramConfig,
) -> tuple[AuthState, bool]:
    START_EPOCH = int(time.time())
    UPDATE_OFFSET: int | None = None

    while True:
        if IS_AUTHENTICATED and not AUTH_STATE.reauth_pending:
            return AUTH_STATE, IS_AUTHENTICATED

        NOW_EPOCH = int(time.time())
        ELAPSED_SECONDS = NOW_EPOCH - START_EPOCH

        if ELAPSED_SECONDS >= RUN_ONCE_AUTH_WAIT_SECONDS:
            return AUTH_STATE, IS_AUTHENTICATED

        COMMANDS, UPDATE_OFFSET = process_commands(
            TELEGRAM,
            CONFIG.container_username,
            UPDATE_OFFSET,
        )

        for COMMAND, ARGS in COMMANDS:
            AUTH_STATE, IS_AUTHENTICATED, _ = handle_command(
                COMMAND,
                ARGS,
                CONFIG,
                CLIENT,
                AUTH_STATE,
                IS_AUTHENTICATED,
                TELEGRAM,
            )

        time.sleep(RUN_ONCE_AUTH_POLL_SECONDS)


# ------------------------------------------------------------------------------
# This function is the worker entrypoint used by the container launcher.
#
# Returns: Non-zero on startup validation/runtime failure.
# ------------------------------------------------------------------------------
def main() -> int:
    CONFIG = load_config()
    LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
    TELEGRAM = TelegramConfig(CONFIG.telegram_bot_token, CONFIG.telegram_chat_id)
    HEARTBEAT_STOP_EVENT: threading.Event | None = None
    STOP_STATUS = "Worker process exited."

    try:
        configure_keyring(CONFIG.config_dir)
        STORED_EMAIL, STORED_PASSWORD = load_credentials(
            CONFIG.keychain_service_name,
            CONFIG.container_username,
        )
        CONFIG = replace(
            CONFIG,
            icloud_email=CONFIG.icloud_email or STORED_EMAIL,
            icloud_password=CONFIG.icloud_password or STORED_PASSWORD,
        )

        ERRORS = validate_config(CONFIG)

        if ERRORS:
            for LINE in ERRORS:
                log_line(LOG_FILE, "error", LINE)

            return 1

        HEARTBEAT_STOP_EVENT = start_heartbeat_updater(CONFIG.heartbeat_path)

        save_credentials(
            CONFIG.keychain_service_name,
            CONFIG.container_username,
            CONFIG.icloud_email,
            CONFIG.icloud_password,
        )
        APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
        notify(
            TELEGRAM,
            build_container_started_message(APPLE_ID_LABEL),
        )

        CLIENT = ICloudDriveClient(CONFIG)
        AUTH_STATE = load_auth_state(CONFIG.auth_state_path)
        AUTH_STATE, IS_AUTHENTICATED, DETAILS = attempt_auth(
            CLIENT,
            AUTH_STATE,
            CONFIG.auth_state_path,
            TELEGRAM,
            CONFIG.container_username,
            CONFIG.icloud_email,
            "",
        )
        log_line(LOG_FILE, "info", DETAILS)
        log_line(
            LOG_FILE,
            "debug",
            "Auth state after startup attempt: "
            f"is_authenticated={IS_AUTHENTICATED}, "
            f"auth_pending={AUTH_STATE.auth_pending}, "
            f"reauth_pending={AUTH_STATE.reauth_pending}",
        )

        if CONFIG.run_once:
            if not IS_AUTHENTICATED or AUTH_STATE.reauth_pending:
                notify(
                    TELEGRAM,
                    build_one_shot_waiting_for_auth_message(
                        APPLE_ID_LABEL,
                        max(1, RUN_ONCE_AUTH_WAIT_SECONDS // 60),
                    ),
                )
                AUTH_STATE, IS_AUTHENTICATED = wait_for_one_shot_auth(
                    CONFIG,
                    CLIENT,
                    AUTH_STATE,
                    IS_AUTHENTICATED,
                    TELEGRAM,
                )

            if not IS_AUTHENTICATED:
                notify(
                    TELEGRAM,
                    build_backup_skipped_auth_incomplete_message(APPLE_ID_LABEL),
                )
                STOP_STATUS = "One-shot backup skipped due to incomplete authentication."
                return 2

            if AUTH_STATE.reauth_pending:
                notify(
                    TELEGRAM,
                    build_backup_skipped_reauth_pending_message(APPLE_ID_LABEL),
                )
                STOP_STATUS = "One-shot backup skipped due to pending reauthentication."
                return 3

            if not enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE):
                STOP_STATUS = "One-shot backup blocked by safety net."
                return 4

            run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "one-shot")
            STOP_STATUS = "Run completed and container exited."
            return 0

        BACKUP_REQUESTED = False
        NEXT_UPDATE_OFFSET: int | None = None
        INITIAL_EPOCH = int(time.time())

        if CONFIG.schedule_mode == "interval":
            NEXT_RUN_EPOCH = INITIAL_EPOCH
        else:
            NEXT_RUN_EPOCH = get_next_run_epoch(CONFIG, INITIAL_EPOCH)

        while True:
            AUTH_STATE = process_reauth_reminders(
                AUTH_STATE,
                CONFIG.auth_state_path,
                TELEGRAM,
                CONFIG.container_username,
                CONFIG.reauth_interval_days,
            )
            COMMANDS, NEXT_UPDATE_OFFSET = process_commands(
                TELEGRAM,
                CONFIG.container_username,
                NEXT_UPDATE_OFFSET,
            )

            for COMMAND, ARGS in COMMANDS:
                AUTH_STATE, IS_AUTHENTICATED, REQUESTED = handle_command(
                    COMMAND,
                    ARGS,
                    CONFIG,
                    CLIENT,
                    AUTH_STATE,
                    IS_AUTHENTICATED,
                    TELEGRAM,
                )
                BACKUP_REQUESTED = BACKUP_REQUESTED or REQUESTED

            NOW_EPOCH = int(time.time())
            SCHEDULE_DUE = NOW_EPOCH >= NEXT_RUN_EPOCH

            if not SCHEDULE_DUE and not BACKUP_REQUESTED:
                time.sleep(5)
                continue

            NEXT_RUN_EPOCH = get_next_run_epoch(CONFIG, NOW_EPOCH)

            if not IS_AUTHENTICATED:
                notify(
                    TELEGRAM,
                    build_backup_skipped_auth_incomplete_message(APPLE_ID_LABEL),
                )
                time.sleep(5)
                continue

            if AUTH_STATE.reauth_pending:
                notify(
                    TELEGRAM,
                    build_backup_skipped_reauth_pending_message(APPLE_ID_LABEL),
                )
                time.sleep(5)
                continue

            if not enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE):
                time.sleep(30)
                continue

            BACKUP_TRIGGER = "manual" if BACKUP_REQUESTED else "scheduled"
            run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, BACKUP_TRIGGER)
            BACKUP_REQUESTED = False
            time.sleep(5)
    finally:
        notify(
            TELEGRAM,
            build_container_stopped_message(
                format_apple_id_label(CONFIG.icloud_email), STOP_STATUS
            ),
        )
        if HEARTBEAT_STOP_EVENT is not None:
            HEARTBEAT_STOP_EVENT.set()


if __name__ == "__main__":
    raise SystemExit(main())
