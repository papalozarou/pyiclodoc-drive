# ------------------------------------------------------------------------------
# This module runs the backup worker loop and coordinates auth and sync.
# ------------------------------------------------------------------------------

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
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

WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
WEEKDAY_NAME_BY_INDEX = {VALUE: KEY for KEY, VALUE in WEEKDAY_MAP.items()}

MONTHLY_WEEK_MAP = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "last": -1,
}
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
# This function parses a daily schedule time in 24-hour "HH:MM" format.
#
# 1. "VALUE" is time text to parse.
#
# Returns: Tuple "(hour, minute)" when valid; otherwise None.
# ------------------------------------------------------------------------------
def parse_daily(VALUE: str) -> tuple[int, int] | None:
    PARTS = VALUE.strip().split(":")

    if len(PARTS) != 2:
        return None

    HOUR_TEXT, MINUTE_TEXT = PARTS

    if not HOUR_TEXT.isdigit() or not MINUTE_TEXT.isdigit():
        return None

    HOUR = int(HOUR_TEXT)
    MINUTE = int(MINUTE_TEXT)

    if HOUR < 0 or HOUR > 23:
        return None

    if MINUTE < 0 or MINUTE > 59:
        return None

    return HOUR, MINUTE


# ------------------------------------------------------------------------------
# This function parses weekday text to Python weekday index.
#
# 1. "VALUE" is weekday text.
#
# Returns: Integer weekday index where Monday is "0"; otherwise None.
# ------------------------------------------------------------------------------
def parse_weekday(VALUE: str) -> int | None:
    return WEEKDAY_MAP.get(VALUE.strip().lower())


# ------------------------------------------------------------------------------
# This function parses a strict weekday list for weekly schedule modes.
#
# 1. "VALUE" is comma-separated weekday text list.
# 2. "EXPECTED_COUNT" is required number of distinct weekdays.
#
# Returns: Weekday index list; otherwise None.
# ------------------------------------------------------------------------------
def parse_weekday_list(VALUE: str, EXPECTED_COUNT: int) -> list[int] | None:
    PARTS = [ITEM.strip().lower() for ITEM in VALUE.split(",") if ITEM.strip()]

    if len(PARTS) != EXPECTED_COUNT:
        return None

    INDICES = [parse_weekday(PART) for PART in PARTS]

    if any(INDEX is None for INDEX in INDICES):
        return None

    DISTINCT = {INDEX for INDEX in INDICES if INDEX is not None}

    if len(DISTINCT) != EXPECTED_COUNT:
        return None

    return [INDEX for INDEX in INDICES if INDEX is not None]


# ------------------------------------------------------------------------------
# This function calculates next daily run epoch for a configured local time.
#
# 1. "NOW_LOCAL" is current configured-timezone datetime.
# 2. "DAILY_TIME" is local schedule in "HH:MM" format.
#
# Returns: Epoch seconds for next scheduled run time.
# ------------------------------------------------------------------------------
def calculate_next_daily_run_epoch(NOW_LOCAL: datetime, DAILY_TIME: str) -> int:
    PARSED = parse_daily(DAILY_TIME)

    if PARSED is None:
        return int(NOW_LOCAL.timestamp())

    HOUR, MINUTE = PARSED
    TARGET = NOW_LOCAL.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)

    if TARGET <= NOW_LOCAL:
        TARGET = TARGET + timedelta(days=1)

    return int(TARGET.timestamp())


# ------------------------------------------------------------------------------
# This function calculates next weekly run epoch for weekday and time settings.
#
# 1. "NOW_LOCAL" is current configured-timezone datetime.
# 2. "WEEKDAY_TEXT" is weekday name.
# 3. "DAILY_TIME" is local schedule in "HH:MM" format.
#
# Returns: Epoch seconds for next scheduled run time.
# ------------------------------------------------------------------------------
def calculate_next_weekly_run_epoch(
    NOW_LOCAL: datetime,
    WEEKDAY_TEXT: str,
    DAILY_TIME: str,
) -> int:
    TIME_PARTS = parse_daily(DAILY_TIME)
    WEEKDAY = parse_weekday(WEEKDAY_TEXT)

    if TIME_PARTS is None or WEEKDAY is None:
        return int(NOW_LOCAL.timestamp())

    HOUR, MINUTE = TIME_PARTS
    DAYS_AHEAD = (WEEKDAY - NOW_LOCAL.weekday()) % 7
    TARGET = (NOW_LOCAL + timedelta(days=DAYS_AHEAD)).replace(
        hour=HOUR,
        minute=MINUTE,
        second=0,
        microsecond=0,
    )

    if TARGET <= NOW_LOCAL:
        TARGET = TARGET + timedelta(days=7)

    return int(TARGET.timestamp())


# ------------------------------------------------------------------------------
# This function calculates next twice-weekly run epoch from weekday pair.
#
# 1. "NOW_LOCAL" is current configured-timezone datetime.
# 2. "WEEKDAYS_TEXT" is comma-separated weekday list.
# 3. "DAILY_TIME" is local schedule in "HH:MM" format.
#
# Returns: Epoch seconds for next scheduled run time.
# ------------------------------------------------------------------------------
def calculate_next_twice_weekly_run_epoch(
    NOW_LOCAL: datetime,
    WEEKDAYS_TEXT: str,
    DAILY_TIME: str,
) -> int:
    WEEKDAYS = parse_weekday_list(WEEKDAYS_TEXT, 2)

    if WEEKDAYS is None:
        return int(NOW_LOCAL.timestamp())

    CANDIDATES = [
        calculate_next_weekly_run_epoch(
            NOW_LOCAL,
            WEEKDAY_NAME_BY_INDEX[WEEKDAY],
            DAILY_TIME,
        )
        for WEEKDAY in WEEKDAYS
    ]
    return min(CANDIDATES)


# ------------------------------------------------------------------------------
# This function returns calendar day number for nth weekday in a month.
#
# 1. "YEAR" and "MONTH" identify the month.
# 2. "WEEKDAY" is weekday index where Monday is "0".
# 3. "MONTHLY_WEEK_TEXT" is one of first/second/third/fourth/last.
#
# Returns: Day number in month; otherwise None.
# ------------------------------------------------------------------------------
def get_monthly_weekday_day(
    YEAR: int,
    MONTH: int,
    WEEKDAY: int,
    MONTHLY_WEEK_TEXT: str,
) -> int | None:
    WEEK_ORDER = MONTHLY_WEEK_MAP.get(MONTHLY_WEEK_TEXT.lower())

    if WEEK_ORDER is None:
        return None

    _, DAYS_IN_MONTH = calendar.monthrange(YEAR, MONTH)

    if WEEK_ORDER == -1:
        DAY = DAYS_IN_MONTH

        while DAY > 0:
            if datetime(YEAR, MONTH, DAY).weekday() == WEEKDAY:
                return DAY

            DAY -= 1

        return None

    COUNT = 0
    DAY = 1

    while DAY <= DAYS_IN_MONTH:
        if datetime(YEAR, MONTH, DAY).weekday() == WEEKDAY:
            COUNT += 1

            if COUNT == WEEK_ORDER:
                return DAY

        DAY += 1

    return None


# ------------------------------------------------------------------------------
# This function calculates next monthly run epoch for weekday-in-month schedules.
#
# 1. "NOW_LOCAL" is current configured-timezone datetime.
# 2. "WEEKDAY_TEXT" is weekday name.
# 3. "MONTHLY_WEEK_TEXT" is ordinal week in month.
# 4. "DAILY_TIME" is local schedule in "HH:MM" format.
#
# Returns: Epoch seconds for next scheduled run time.
# ------------------------------------------------------------------------------
def calculate_next_monthly_run_epoch(
    NOW_LOCAL: datetime,
    WEEKDAY_TEXT: str,
    MONTHLY_WEEK_TEXT: str,
    DAILY_TIME: str,
) -> int:
    TIME_PARTS = parse_daily(DAILY_TIME)
    WEEKDAY = parse_weekday(WEEKDAY_TEXT)

    if TIME_PARTS is None or WEEKDAY is None:
        return int(NOW_LOCAL.timestamp())

    HOUR, MINUTE = TIME_PARTS

    for MONTH_OFFSET in [0, 1, 2]:
        YEAR = NOW_LOCAL.year + ((NOW_LOCAL.month - 1 + MONTH_OFFSET) // 12)
        MONTH = ((NOW_LOCAL.month - 1 + MONTH_OFFSET) % 12) + 1
        DAY = get_monthly_weekday_day(YEAR, MONTH, WEEKDAY, MONTHLY_WEEK_TEXT)

        if DAY is None:
            continue

        TARGET = NOW_LOCAL.replace(
            year=YEAR,
            month=MONTH,
            day=DAY,
            hour=HOUR,
            minute=MINUTE,
            second=0,
            microsecond=0,
        )

        if TARGET > NOW_LOCAL:
            return int(TARGET.timestamp())

    return int(NOW_LOCAL.timestamp())


# ------------------------------------------------------------------------------
# This function returns next scheduled run epoch for active schedule mode.
#
# 1. "CONFIG" is runtime configuration.
# 2. "NOW_EPOCH" is current epoch timestamp.
#
# Returns: Epoch seconds for next scheduled backup execution.
# ------------------------------------------------------------------------------
def get_next_run_epoch(CONFIG: AppConfig, NOW_EPOCH: int) -> int:
    if CONFIG.schedule_mode == "daily":
        return calculate_next_daily_run_epoch(now_local(), CONFIG.schedule_backup_time)

    if CONFIG.schedule_mode == "weekly":
        WEEKDAYS = parse_weekday_list(CONFIG.schedule_weekdays, 1)

        if WEEKDAYS is None:
            return NOW_EPOCH

        return calculate_next_weekly_run_epoch(
            now_local(),
            WEEKDAY_NAME_BY_INDEX[WEEKDAYS[0]],
            CONFIG.schedule_backup_time,
        )

    if CONFIG.schedule_mode == "twice_weekly":
        return calculate_next_twice_weekly_run_epoch(
            now_local(),
            CONFIG.schedule_weekdays,
            CONFIG.schedule_backup_time,
        )

    if CONFIG.schedule_mode == "monthly":
        WEEKDAYS = parse_weekday_list(CONFIG.schedule_weekdays, 1)

        if WEEKDAYS is None:
            return NOW_EPOCH

        return calculate_next_monthly_run_epoch(
            now_local(),
            WEEKDAY_NAME_BY_INDEX[WEEKDAYS[0]],
            CONFIG.schedule_monthly_week,
            CONFIG.schedule_backup_time,
        )

    return NOW_EPOCH + (CONFIG.schedule_interval_minutes * 60)


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
# This function builds a compact multi-line Telegram event message.
#
# 1. "ICON" is the leading emoji marker.
# 2. "TITLE" is sentence-case message heading.
# 3. "DESCRIPTION" is one-line activity summary.
# 4. "STATUS_LINES" are optional detail lines.
#
# Returns: Formatted Telegram message text.
# ------------------------------------------------------------------------------
def format_telegram_event(
    ICON: str,
    TITLE: str,
    DESCRIPTION: str,
    STATUS_LINES: list[str] | None = None,
) -> str:
    LINES = [f"*{ICON} iCloudDD - {TITLE}*", DESCRIPTION]

    if STATUS_LINES:
        LINES.extend([LINE for LINE in STATUS_LINES if LINE.strip()])

    return "\n".join(LINES)


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
# This function formats schedule settings as plain-English backup wording.
#
# 1. "CONFIG" is runtime configuration.
# 2. "TRIGGER" is backup trigger context.
#
# Returns: Human-readable schedule description.
# ------------------------------------------------------------------------------
def format_schedule_description(CONFIG: AppConfig, TRIGGER: str) -> str:
    if TRIGGER == "one-shot":
        return "One-shot run – configured schedule is ignored."

    if CONFIG.schedule_mode == "interval":
        return f"Every {CONFIG.schedule_interval_minutes} minutes"

    if CONFIG.schedule_mode == "daily":
        return f"Daily at {CONFIG.schedule_backup_time}"

    if CONFIG.schedule_mode == "weekly":
        DAY_TEXT = CONFIG.schedule_weekdays.strip().title()
        return f"Weekly on {DAY_TEXT} at {CONFIG.schedule_backup_time}"

    if CONFIG.schedule_mode == "twice_weekly":
        RAW_DAYS = [PART.strip().title() for PART in CONFIG.schedule_weekdays.split(",") if PART.strip()]
        DAYS_TEXT = " and ".join(RAW_DAYS)
        return f"Twice weekly on {DAYS_TEXT} at {CONFIG.schedule_backup_time}"

    if CONFIG.schedule_mode == "monthly":
        DAY_TEXT = CONFIG.schedule_weekdays.strip().title()
        WEEK_TEXT = CONFIG.schedule_monthly_week.strip().lower()
        WEEK_LABEL = WEEK_TEXT.capitalize()
        return f"Monthly on the {WEEK_LABEL} {DAY_TEXT} at {CONFIG.schedule_backup_time}"

    return f"Configured mode {CONFIG.schedule_mode}"


# ------------------------------------------------------------------------------
# This function formats schedule status text for backup-start notifications.
#
# 1. "CONFIG" is runtime configuration.
# 2. "TRIGGER" is backup trigger context.
#
# Returns: One-line schedule status text.
# ------------------------------------------------------------------------------
def format_schedule_line(CONFIG: AppConfig, TRIGGER: str) -> str:
    SCHEDULE_DESCRIPTION = format_schedule_description(CONFIG, TRIGGER)

    if TRIGGER == "manual":
        return f"Schedule: Manual, then {SCHEDULE_DESCRIPTION}."

    return f"Schedule: {SCHEDULE_DESCRIPTION}."


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
            format_telegram_event(
                "🔒",
                "Authentication complete",
                f"Authenticated for Apple ID {APPLE_ID_LABEL}.",
                [DETAILS],
            ),
        )
        return NEW_STATE, True, DETAILS

    if "Two-factor code is required" in DETAILS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        notify(
            TELEGRAM,
            format_telegram_event(
                "🔑",
                "Authentication required",
                f"Authentication required for Apple ID {APPLE_ID_LABEL}.",
                [
                    f"Send: {USERNAME} auth 123456",
                    f"Or: {USERNAME} reauth 123456",
                ],
            ),
        )
        return NEW_STATE, False, DETAILS

    NEW_STATE = replace(AUTH_STATE, auth_pending=True)
    save_auth_state(AUTH_STATE_PATH, NEW_STATE)
    notify(
        TELEGRAM,
        format_telegram_event(
            "❌",
            "Authentication failed",
            f"Authentication failed for Apple ID {APPLE_ID_LABEL}.",
            [f"Reason: {DETAILS}"],
        ),
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
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
    SAMPLE_TEXT = ", ".join(RESULT.mismatched_samples[:2]) or "<none>"
    notify(
        TELEGRAM,
        format_telegram_event(
            "⚠️",
            "Safety net blocked",
            f"Backup blocked for Apple ID {APPLE_ID_LABEL}.",
            [
                "Permission mismatches detected in existing files.",
                "Expected: "
                f"uid {RESULT.expected_uid}, "
                f"gid {RESULT.expected_gid}",
                f"Sample mismatches: {SAMPLE_TEXT}",
            ],
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
            format_telegram_event(
                "🔑",
                "Reauthentication required",
                "Reauthentication is due within two days.",
                [f"Send: {USERNAME} reauth"],
            ),
        )
        NEW_STATE = replace(AUTH_STATE, reminder_stage="prompt2", reauth_pending=True)
        save_auth_state(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 5 and AUTH_STATE.reminder_stage == "none":
        notify(
            TELEGRAM,
            format_telegram_event(
                "📣",
                "Reauth reminder",
                "Reauthentication will be required within five days.",
            ),
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
    MANIFEST = load_manifest(CONFIG.manifest_path)
    log_line(LOG_FILE, "debug", f"Loaded manifest entries: {len(MANIFEST)}")
    APPLE_ID_LABEL = format_apple_id_label(CONFIG.icloud_email)
    RUN_START_EPOCH = int(time.time())
    SCHEDULE_LINE = format_schedule_line(CONFIG, TRIGGER)
    notify(
        TELEGRAM,
        format_telegram_event(
            "⬇️",
            "Backup started",
            f"Files downloading for Apple ID {APPLE_ID_LABEL}.",
            [
                SCHEDULE_LINE,
            ],
        ),
    )

    SUMMARY, NEW_MANIFEST = perform_incremental_sync(
        CLIENT,
        CONFIG.output_dir,
        MANIFEST,
        CONFIG.sync_workers,
        LOG_FILE,
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

    COMPLETION_MESSAGE = format_telegram_event(
        "📦",
        "Backup complete",
        f"Backup finished for Apple ID {APPLE_ID_LABEL}.",
        STATUS_LINES,
    )
    notify(TELEGRAM, COMPLETION_MESSAGE)
    log_line(
        LOG_FILE,
        "info",
        "Backup complete. "
        f"Transferred {SUMMARY.transferred_files}/{SUMMARY.total_files}, "
        f"skipped {SUMMARY.skipped_files}, errors {SUMMARY.error_files}.",
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
            format_telegram_event(
                "📥",
                "Backup requested",
                f"Manual backup requested for Apple ID {APPLE_ID_LABEL}.",
                ["Worker queued backup to run now."],
            ),
        )
        return AUTH_STATE, IS_AUTHENTICATED, True

    if COMMAND == "auth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            format_telegram_event(
                "🔑",
                "Authentication required",
                f"Authentication required for Apple ID {APPLE_ID_LABEL}.",
                [f"Send: {CONFIG.container_username} auth 123456"],
            ),
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    if COMMAND == "reauth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, reauth_pending=True)
        save_auth_state(CONFIG.auth_state_path, NEW_STATE)
        notify(
            TELEGRAM,
            format_telegram_event(
                "🔑",
                "Reauthentication required",
                f"Reauthentication required for Apple ID {APPLE_ID_LABEL}.",
                [f"Send: {CONFIG.container_username} reauth 123456"],
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
    log_line(CONFIG.logs_dir / "worker.log", "info", f"Auth command result: {DETAILS}")
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
    LOG_FILE = CONFIG.logs_dir / "worker.log"
    TELEGRAM = TelegramConfig(CONFIG.telegram_bot_token, CONFIG.telegram_chat_id)
    HEARTBEAT_STOP_EVENT: threading.Event | None = None
    STOP_STATUS = "Last status: worker process exited."

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
            format_telegram_event(
                "🟢",
                "Container started",
                f"Worker started for Apple ID {APPLE_ID_LABEL}.",
                ["Initialising authentication and backup checks."],
            ),
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
                    format_telegram_event(
                        "🔑",
                        "Authentication required",
                        f"Authentication required for Apple ID {APPLE_ID_LABEL}.",
                        [
                            "One-shot mode is waiting for an auth command before backup.",
                            "Wait window: "
                            f"{max(1, RUN_ONCE_AUTH_WAIT_SECONDS // 60)} mins.",
                        ],
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
                    format_telegram_event(
                        "⏭️",
                        "Backup skipped",
                        f"Backup skipped for Apple ID {APPLE_ID_LABEL}.",
                        ["Reason: Authentication incomplete."],
                    ),
                )
                STOP_STATUS = "Last status: one-shot backup skipped due to incomplete authentication."
                return 2

            if AUTH_STATE.reauth_pending:
                notify(
                    TELEGRAM,
                    format_telegram_event(
                        "⏭️",
                        "Backup skipped",
                        f"Backup skipped for Apple ID {APPLE_ID_LABEL}.",
                        ["Reason: Reauthentication pending."],
                    ),
                )
                STOP_STATUS = "Last status: one-shot backup skipped due to pending reauthentication."
                return 3

            if not enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE):
                STOP_STATUS = "Last status: one-shot backup blocked by safety net."
                return 4

            run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "one-shot")
            STOP_STATUS = "Last status: run completed and container exited."
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
                    format_telegram_event(
                        "⏭️",
                        "Backup skipped",
                        f"Backup skipped for Apple ID {APPLE_ID_LABEL}.",
                        ["Reason: Authentication incomplete."],
                    ),
                )
                time.sleep(5)
                continue

            if AUTH_STATE.reauth_pending:
                notify(
                    TELEGRAM,
                    format_telegram_event(
                        "⏭️",
                        "Backup skipped",
                        f"Backup skipped for Apple ID {APPLE_ID_LABEL}.",
                        ["Reason: Reauthentication pending."],
                    ),
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
            format_telegram_event(
                "🛑",
                "Container stopped",
                f"Worker stopped for Apple ID {format_apple_id_label(CONFIG.icloud_email)}.",
                [STOP_STATUS],
            ),
        )
        if HEARTBEAT_STOP_EVENT is not None:
            HEARTBEAT_STOP_EVENT.set()


if __name__ == "__main__":
    raise SystemExit(main())
