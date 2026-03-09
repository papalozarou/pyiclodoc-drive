# ------------------------------------------------------------------------------
# This module runs the backup worker loop and coordinates auth and sync.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import time

from dateutil import parser as date_parser

from app.config import AppConfig, load_config
from app.credential_store import configure_keyring, load_credentials, save_credentials
from app.icloud_client import ICloudDriveClient
from app.logger import log_line
from app.state import AuthState, load_auth_state, load_manifest, now_iso, save_auth_state, save_manifest
from app.syncer import perform_incremental_sync, run_first_time_safety_net
from app.telegram_bot import TelegramConfig, fetch_updates, parse_command, send_message
from app.time_utils import now_local


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

    if not CONFIG.run_once and CONFIG.backup_interval_minutes < 1:
        ERRORS.append(
            "BACKUP_INTERVAL_MINUTES must be at least 1 when RUN_ONCE is false."
        )

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
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.touch()


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
    PROVIDED_CODE: str,
) -> tuple[AuthState, bool, str]:
    CODE = PROVIDED_CODE.strip()
    IS_SUCCESS, DETAILS = CLIENT.authenticate(lambda: CODE)

    if IS_SUCCESS:
        NEW_STATE = AuthState(
            last_auth_utc=now_iso(),
            auth_pending=False,
            reauth_pending=False,
            reminder_stage="none",
        )
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        notify(TELEGRAM, "Authentication successful.")
        return NEW_STATE, True, DETAILS

    if "Two-factor code is required" in DETAILS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        notify(
            TELEGRAM,
            f"MFA required. Send '{USERNAME} auth 123456' or "
            f"'{USERNAME} reauth 123456'.",
        )
        return NEW_STATE, False, DETAILS

    NEW_STATE = replace(AUTH_STATE, auth_pending=True)
    save_auth_state(AUTH_STATE_PATH, NEW_STATE)
    notify(TELEGRAM, f"Authentication failed: {DETAILS}")
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
    DONE_MARKER = CONFIG.config_dir / "safety_net_done.flag"
    BLOCKED_MARKER = CONFIG.config_dir / "safety_net_blocked.flag"

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
    notify(
        TELEGRAM,
        "Safety net blocked backup. Expected permissions "
        f"{RESULT.expected_mode}. Mismatches:\n{MISMATCH_TEXT}",
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
        notify(TELEGRAM, f"Reauth required in two days. Send '{USERNAME} reauth'.")
        NEW_STATE = replace(AUTH_STATE, reminder_stage="prompt2", reauth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 5 and AUTH_STATE.reminder_stage == "none":
        notify(TELEGRAM, "Reauthentication will be required within five days.")
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
) -> None:
    MANIFEST = load_manifest(CONFIG.manifest_path)
    notify(TELEGRAM, "Backup starting.")

    SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, CONFIG.output_dir, MANIFEST)
    save_manifest(CONFIG.manifest_path, NEW_MANIFEST)

    MESSAGE = (
        "Backup complete. "
        f"Transferred {SUMMARY.transferred_files}/{SUMMARY.total_files}, "
        f"skipped {SUMMARY.skipped_files}, errors {SUMMARY.error_files}."
    )
    notify(TELEGRAM, MESSAGE)
    log_line(LOG_FILE, "info", MESSAGE)


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
    if COMMAND == "backup":
        notify(TELEGRAM, "Backup requested.")
        return AUTH_STATE, IS_AUTHENTICATED, True

    if COMMAND == "auth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            f"Send '{CONFIG.container_username} auth 123456' "
            "with your current MFA code.",
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    if COMMAND == "reauth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, reauth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            f"Send '{CONFIG.container_username} reauth 123456' "
            "to complete reauthentication.",
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    NEW_STATE, NEW_AUTH, DETAILS = attempt_auth(
        CLIENT,
        AUTH_STATE,
        CONFIG.auth_state_path,
        TELEGRAM,
        CONFIG.container_username,
        ARGS,
    )
    log_line(CONFIG.logs_dir / "worker.log", "info", f"Auth command result: {DETAILS}")
    return NEW_STATE, NEW_AUTH, False


# ------------------------------------------------------------------------------
# This function is the worker entrypoint used by the container launcher.
#
# Returns: Non-zero on startup validation/runtime failure.
# ------------------------------------------------------------------------------
def main() -> int:
    CONFIG = load_config()
    LOG_FILE = CONFIG.logs_dir / "worker.log"
    TELEGRAM = TelegramConfig(CONFIG.telegram_bot_token, CONFIG.telegram_chat_id)

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

    save_credentials(
        CONFIG.keychain_service_name,
        CONFIG.container_username,
        CONFIG.icloud_email,
        CONFIG.icloud_password,
    )

    CLIENT = ICloudDriveClient(CONFIG)
    AUTH_STATE = load_auth_state(CONFIG.auth_state_path)
    AUTH_STATE, IS_AUTHENTICATED, DETAILS = attempt_auth(
        CLIENT,
        AUTH_STATE,
        CONFIG.auth_state_path,
        TELEGRAM,
        CONFIG.container_username,
        "",
    )
    log_line(LOG_FILE, "info", DETAILS)

    if CONFIG.run_once:
        if not IS_AUTHENTICATED:
            notify(TELEGRAM, "One-shot backup skipped because authentication is incomplete.")
            return 2

        if AUTH_STATE.reauth_pending:
            notify(TELEGRAM, "One-shot backup skipped because reauthentication is pending.")
            return 3

        if not enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE):
            return 4

        run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE)
        return 0

    BACKUP_REQUESTED = False
    NEXT_UPDATE_OFFSET: int | None = None
    NEXT_RUN_EPOCH = int(time.time())

    while True:
        update_heartbeat(CONFIG.heartbeat_path)
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

        NEXT_RUN_EPOCH = NOW_EPOCH + (CONFIG.backup_interval_minutes * 60)

        if not IS_AUTHENTICATED:
            notify(TELEGRAM, "Backup skipped because authentication is incomplete.")
            time.sleep(5)
            continue

        if AUTH_STATE.reauth_pending:
            notify(TELEGRAM, "Backup skipped because reauthentication is pending.")
            time.sleep(5)
            continue

        if not enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE):
            time.sleep(30)
            continue

        run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE)
        BACKUP_REQUESTED = False
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
