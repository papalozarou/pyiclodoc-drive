# ------------------------------------------------------------------------------
# This module runs the backup worker loop and coordinates auth and sync.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import app.auth_runtime as auth_runtime
import app.backup_runtime as backup_runtime
import app.command_runtime as command_runtime
from app.config import AppConfig, load_config
from app.config_validation import validate_config
from app.credential_store import configure_keyring, load_credentials, save_credentials
from app.icloud_client import ICloudDriveClient
from app.logger import log_line
from app.runtime_helpers import format_apple_id_label, notify
from app.scheduler import (
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
from app.runtime_context import WorkerRuntimeContext
from app.state import AuthState, load_auth_state, load_manifest, now_iso, save_auth_state, save_manifest
from app.syncer import perform_incremental_sync, run_first_time_safety_net
from app.telegram_bot import TelegramConfig, fetch_updates, parse_command
from app.telegram_messages import (
    build_backup_skipped_auth_incomplete_message,
    build_backup_skipped_reauth_pending_message,
    build_container_started_message,
    build_container_stopped_message,
    build_one_shot_waiting_for_auth_message,
    build_safety_net_blocked_message,
)
from app.time_utils import now_local
RUN_ONCE_AUTH_WAIT_SECONDS = 900
RUN_ONCE_AUTH_POLL_SECONDS = 5
HEARTBEAT_TOUCH_INTERVAL_SECONDS = 30


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
# This function parses an ISO timestamp with a strict epoch fallback.
#
# 1. "VALUE" is an ISO-formatted timestamp string.
#
# Returns: Offset-aware datetime; Unix epoch when parsing fails.
# ------------------------------------------------------------------------------
def parse_iso(VALUE: str) -> datetime:
    return auth_runtime.parse_iso(VALUE)


# ------------------------------------------------------------------------------
# This function calculates remaining whole days before reauthentication.
#
# 1. "LAST_AUTH_UTC" is stored offset-aware auth timestamp.
# 2. "INTERVAL_DAYS" is the reauthentication interval in days.
#
# Returns: Remaining whole days before reauthentication should complete.
# ------------------------------------------------------------------------------
def reauth_days_left(LAST_AUTH_UTC: str, INTERVAL_DAYS: int) -> int:
    return auth_runtime.reauth_days_left(LAST_AUTH_UTC, INTERVAL_DAYS)


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
    return auth_runtime.attempt_auth(
        CLIENT,
        AUTH_STATE,
        AUTH_STATE_PATH,
        TELEGRAM,
        USERNAME,
        APPLE_ID,
        PROVIDED_CODE,
        DEPS=auth_runtime.AuthRuntimeDeps(
            now_iso_fn=now_iso,
            save_auth_state_fn=save_auth_state,
            notify_fn=notify,
        ),
    )


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
    return auth_runtime.process_reauth_reminders(
        AUTH_STATE,
        AUTH_STATE_PATH,
        TELEGRAM,
        USERNAME,
        INTERVAL_DAYS,
        DEPS=auth_runtime.AuthRuntimeDeps(
            save_auth_state_fn=save_auth_state,
            notify_fn=notify,
        ),
        REAUTH_DAYS_LEFT_FN=reauth_days_left,
    )


# ------------------------------------------------------------------------------
# This function formats elapsed seconds as "HH:MM:SS".
#
# 1. "TOTAL_SECONDS" is elapsed duration in seconds.
#
# Returns: Zero-padded duration string.
# ------------------------------------------------------------------------------
def format_duration_clock(TOTAL_SECONDS: int) -> str:
    return backup_runtime.format_duration_clock(TOTAL_SECONDS)


# ------------------------------------------------------------------------------
# This function formats average transfer speed using binary megabytes per second.
#
# 1. "TRANSFERRED_BYTES" is successful download byte total.
# 2. "DURATION_SECONDS" is elapsed run duration in seconds.
#
# Returns: Human-readable transfer speed string.
# ------------------------------------------------------------------------------
def format_average_speed(TRANSFERRED_BYTES: int, DURATION_SECONDS: int) -> str:
    return backup_runtime.format_average_speed(TRANSFERRED_BYTES, DURATION_SECONDS)


# ------------------------------------------------------------------------------
# This function returns runtime build metadata for startup diagnostics.
#
# Returns: Mapping with app build ref and pyicloud package version.
# ------------------------------------------------------------------------------
def get_build_detail() -> dict[str, str]:
    return backup_runtime.get_build_detail()


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
    DONE_MARKER = CONFIG.safety_net_done_path
    BLOCKED_MARKER = CONFIG.safety_net_blocked_path

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
    return command_runtime.process_commands(
        TELEGRAM,
        USERNAME,
        UPDATE_OFFSET,
        DEPS=command_runtime.CommandPollingDeps(
            fetch_updates_fn=fetch_updates,
            parse_command_fn=parse_command,
        ),
    )


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
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
    SCHEDULE_LINE = format_schedule_line(CONFIG, TRIGGER)
    return backup_runtime.run_backup(
        CLIENT,
        CONFIG,
        TELEGRAM,
        LOG_FILE,
        APPLE_ID_LABEL,
        SCHEDULE_LINE,
        DEPS=backup_runtime.BackupRuntimeDeps(
            load_manifest_fn=load_manifest,
            save_manifest_fn=save_manifest,
            log_line_fn=log_line,
            notify_fn=notify,
            get_build_detail_fn=get_build_detail,
            format_duration_fn=format_duration_clock,
            format_speed_fn=format_average_speed,
            perform_sync_fn=perform_incremental_sync,
        ),
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
    return backup_runtime.log_effective_backup_settings(
        CONFIG,
        LOG_FILE,
        LOG_LINE_FN=log_line,
        GET_BUILD_DETAIL_FN=get_build_detail,
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
    return command_runtime.handle_command(
        COMMAND,
        ARGS,
        CONFIG,
        CLIENT,
        AUTH_STATE,
        IS_AUTHENTICATED,
        TELEGRAM,
        APPLE_ID_LABEL,
        DEPS=command_runtime.CommandRuntimeDeps(
            attempt_auth_fn=attempt_auth,
            notify_fn=notify,
            save_auth_state_fn=save_auth_state,
            log_line_fn=log_line,
            log_file_path=CONFIG.logs_dir / "pyiclodoc-drive-worker.log",
        ),
    )


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
    RUNTIME_CONTEXT: WorkerRuntimeContext | None = None
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
        RUNTIME_CONTEXT = WorkerRuntimeContext(
            CONFIG=CONFIG,
            TELEGRAM=TELEGRAM,
            LOG_FILE=LOG_FILE,
            APPLE_ID_LABEL=format_apple_id_label(CONFIG.icloud_email),
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
        notify(
            RUNTIME_CONTEXT.TELEGRAM,
            build_container_started_message(RUNTIME_CONTEXT.APPLE_ID_LABEL),
        )

        CLIENT = ICloudDriveClient(RUNTIME_CONTEXT.CONFIG)
        AUTH_STATE = load_auth_state(RUNTIME_CONTEXT.CONFIG.auth_state_path)
        AUTH_STATE, IS_AUTHENTICATED, DETAILS = attempt_auth(
            CLIENT,
            AUTH_STATE,
            RUNTIME_CONTEXT.CONFIG.auth_state_path,
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.CONFIG.container_username,
            RUNTIME_CONTEXT.CONFIG.icloud_email,
            "",
        )
        log_line(RUNTIME_CONTEXT.LOG_FILE, "info", DETAILS)
        log_line(
            RUNTIME_CONTEXT.LOG_FILE,
            "debug",
            "Auth state after startup attempt: "
            f"is_authenticated={IS_AUTHENTICATED}, "
            f"auth_pending={AUTH_STATE.auth_pending}, "
            f"reauth_pending={AUTH_STATE.reauth_pending}",
        )

        if RUNTIME_CONTEXT.CONFIG.run_once:
            if not IS_AUTHENTICATED or AUTH_STATE.reauth_pending:
                notify(
                    RUNTIME_CONTEXT.TELEGRAM,
                    build_one_shot_waiting_for_auth_message(
                        RUNTIME_CONTEXT.APPLE_ID_LABEL,
                        max(1, RUN_ONCE_AUTH_WAIT_SECONDS // 60),
                    ),
                )
                AUTH_STATE, IS_AUTHENTICATED = wait_for_one_shot_auth(
                    RUNTIME_CONTEXT.CONFIG,
                    CLIENT,
                    AUTH_STATE,
                    IS_AUTHENTICATED,
                    RUNTIME_CONTEXT.TELEGRAM,
                )

            if not IS_AUTHENTICATED:
                notify(
                    RUNTIME_CONTEXT.TELEGRAM,
                    build_backup_skipped_auth_incomplete_message(
                        RUNTIME_CONTEXT.APPLE_ID_LABEL
                    ),
                )
                STOP_STATUS = "One-shot backup skipped due to incomplete authentication."
                return 2

            if AUTH_STATE.reauth_pending:
                notify(
                    RUNTIME_CONTEXT.TELEGRAM,
                    build_backup_skipped_reauth_pending_message(
                        RUNTIME_CONTEXT.APPLE_ID_LABEL
                    ),
                )
                STOP_STATUS = "One-shot backup skipped due to pending reauthentication."
                return 3

            if not enforce_safety_net(
                RUNTIME_CONTEXT.CONFIG,
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.LOG_FILE,
            ):
                STOP_STATUS = "One-shot backup blocked by safety net."
                return 4

            run_backup(
                CLIENT,
                RUNTIME_CONTEXT.CONFIG,
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.LOG_FILE,
                "one-shot",
            )
            STOP_STATUS = "Run completed and container exited."
            return 0

        BACKUP_REQUESTED = False
        NEXT_UPDATE_OFFSET: int | None = None
        INITIAL_EPOCH = int(time.time())

        if RUNTIME_CONTEXT.CONFIG.schedule_mode == "interval":
            NEXT_RUN_EPOCH = INITIAL_EPOCH
        else:
            NEXT_RUN_EPOCH = get_next_run_epoch(RUNTIME_CONTEXT.CONFIG, INITIAL_EPOCH)

        while True:
            AUTH_STATE = process_reauth_reminders(
                AUTH_STATE,
                RUNTIME_CONTEXT.CONFIG.auth_state_path,
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.CONFIG.container_username,
                RUNTIME_CONTEXT.CONFIG.reauth_interval_days,
            )
            COMMANDS, NEXT_UPDATE_OFFSET = process_commands(
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.CONFIG.container_username,
                NEXT_UPDATE_OFFSET,
            )

            for COMMAND, ARGS in COMMANDS:
                AUTH_STATE, IS_AUTHENTICATED, REQUESTED = handle_command(
                    COMMAND,
                    ARGS,
                    RUNTIME_CONTEXT.CONFIG,
                    CLIENT,
                    AUTH_STATE,
                    IS_AUTHENTICATED,
                    RUNTIME_CONTEXT.TELEGRAM,
                )
                BACKUP_REQUESTED = BACKUP_REQUESTED or REQUESTED

            NOW_EPOCH = int(time.time())
            SCHEDULE_DUE = NOW_EPOCH >= NEXT_RUN_EPOCH

            if not SCHEDULE_DUE and not BACKUP_REQUESTED:
                time.sleep(5)
                continue

            NEXT_RUN_EPOCH = get_next_run_epoch(RUNTIME_CONTEXT.CONFIG, NOW_EPOCH)

            if not IS_AUTHENTICATED:
                notify(
                    RUNTIME_CONTEXT.TELEGRAM,
                    build_backup_skipped_auth_incomplete_message(
                        RUNTIME_CONTEXT.APPLE_ID_LABEL
                    ),
                )
                time.sleep(5)
                continue

            if AUTH_STATE.reauth_pending:
                notify(
                    RUNTIME_CONTEXT.TELEGRAM,
                    build_backup_skipped_reauth_pending_message(
                        RUNTIME_CONTEXT.APPLE_ID_LABEL
                    ),
                )
                time.sleep(5)
                continue

            if not enforce_safety_net(
                RUNTIME_CONTEXT.CONFIG,
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.LOG_FILE,
            ):
                time.sleep(30)
                continue

            BACKUP_TRIGGER = "manual" if BACKUP_REQUESTED else "scheduled"
            run_backup(
                CLIENT,
                RUNTIME_CONTEXT.CONFIG,
                RUNTIME_CONTEXT.TELEGRAM,
                RUNTIME_CONTEXT.LOG_FILE,
                BACKUP_TRIGGER,
            )
            BACKUP_REQUESTED = False
            time.sleep(5)
    finally:
        if RUNTIME_CONTEXT is None:
            APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
            notify(TELEGRAM, build_container_stopped_message(APPLE_ID_LABEL, STOP_STATUS))
        else:
            notify(
                RUNTIME_CONTEXT.TELEGRAM,
                build_container_stopped_message(
                    RUNTIME_CONTEXT.APPLE_ID_LABEL, STOP_STATUS
                ),
            )
        if HEARTBEAT_STOP_EVENT is not None:
            HEARTBEAT_STOP_EVENT.set()


if __name__ == "__main__":
    raise SystemExit(main())
