# ------------------------------------------------------------------------------
# This module provides lightweight structured logging helpers for console and
# file output.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import gzip
import os
import shutil

from app.time_utils import now_local

LOG_LEVELS = {
    "debug": 10,
    "info": 20,
    "error": 30,
}
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ROTATED_FILE_PATTERN = "{name}.{stamp}.log"


# ------------------------------------------------------------------------------
# This data class stores the effective logger policy for the current process.
#
# N.B.
# The policy is intentionally resolved in one place so log emission, rotation,
# and tests all use the same interpretation of environment values. This keeps
# behaviour centralised without introducing pre-emptive global caching.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class LoggerConfig:
    level: str
    rotate_max_bytes: int
    rotate_daily: bool
    rotate_keep_days: int


# ------------------------------------------------------------------------------
# This function produces a configured-timezone timestamp string.
#
# Returns: Display timestamp including timezone abbreviation.
# ------------------------------------------------------------------------------
def get_timestamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")


# ------------------------------------------------------------------------------
# This function reads all logger settings from environment in one pass.
#
# Returns: Immutable logger policy used by console and file logging.
#
# N.B.
# The returned object is cheap to build and is passed down through log helpers
# during a single logging decision so repeated env parsing stays localised to
# one canonical seam.
# ------------------------------------------------------------------------------
def load_logger_config() -> LoggerConfig:
    DEFAULT_MAX_BYTES = 100 * 1024 * 1024
    DEFAULT_KEEP_DAYS = 14

    RAW_LEVEL = os.getenv("LOG_LEVEL", "info").strip().lower()
    LEVEL = RAW_LEVEL if RAW_LEVEL in LOG_LEVELS else "info"

    RAW_MAX_MIB = os.getenv("LOG_ROTATE_MAX_MIB", "100").strip()
    if RAW_MAX_MIB.isdigit() and int(RAW_MAX_MIB) >= 1:
        ROTATE_MAX_BYTES = int(RAW_MAX_MIB) * 1024 * 1024
    else:
        ROTATE_MAX_BYTES = DEFAULT_MAX_BYTES

    RAW_ROTATE_DAILY = os.getenv("LOG_ROTATE_DAILY", "true").strip().lower()
    if RAW_ROTATE_DAILY in {"0", "false", "no", "off"}:
        ROTATE_DAILY = False
    else:
        ROTATE_DAILY = True

    RAW_KEEP_DAYS = os.getenv("LOG_ROTATE_KEEP_DAYS", "14").strip()
    if RAW_KEEP_DAYS.isdigit() and int(RAW_KEEP_DAYS) >= 1:
        ROTATE_KEEP_DAYS = int(RAW_KEEP_DAYS)
    else:
        ROTATE_KEEP_DAYS = DEFAULT_KEEP_DAYS

    return LoggerConfig(
        level=LEVEL,
        rotate_max_bytes=ROTATE_MAX_BYTES,
        rotate_daily=ROTATE_DAILY,
        rotate_keep_days=ROTATE_KEEP_DAYS,
    )


# ------------------------------------------------------------------------------
# This function returns the configured log threshold from environment.
#
# Returns: Normalised log level token.
# ------------------------------------------------------------------------------
def get_log_level() -> str:
    return load_logger_config().level


# ------------------------------------------------------------------------------
# This function checks whether a log line should be emitted.
#
# 1. "LEVEL" is message severity token.
#
# Returns: True when line should be written and printed.
# ------------------------------------------------------------------------------
def should_log(LEVEL: str, CONFIG: LoggerConfig | None = None) -> bool:
    ACTIVE_CONFIG = CONFIG or load_logger_config()
    CURRENT_WEIGHT = LOG_LEVELS.get(ACTIVE_CONFIG.level, LOG_LEVELS["info"])
    MESSAGE_WEIGHT = LOG_LEVELS.get(LEVEL.lower(), LOG_LEVELS["info"])
    return MESSAGE_WEIGHT >= CURRENT_WEIGHT


# ------------------------------------------------------------------------------
# This function prints a log line and appends it to the worker log.
#
# 1. "LOG_FILE" is the destination log file.
# 2. "LEVEL" is severity.
# 3. "MESSAGE" is log content.
#
# Returns: None.
# ------------------------------------------------------------------------------
def log_line(LOG_FILE: Path, LEVEL: str, MESSAGE: str) -> None:
    LOGGER_CONFIG = load_logger_config()

    if not should_log(LEVEL, LOGGER_CONFIG):
        return

    rotate_log_if_needed(LOG_FILE, LOGGER_CONFIG)

    LEVEL_UPPER = LEVEL.upper()
    LINE = f"[{get_timestamp()}] [{LEVEL_UPPER}] {MESSAGE}"
    CONSOLE_LINE = format_console_line(LINE, LEVEL_UPPER)
    print(CONSOLE_LINE, flush=True)

    with LOG_FILE.open("a", encoding="utf-8") as HANDLE:
        HANDLE.write(f"{LINE}\n")


# ------------------------------------------------------------------------------
# This function applies console-only formatting for selected log levels.
#
# 1. "LINE" is the plain log line.
# 2. "LEVEL_UPPER" is uppercase severity token.
#
# Returns: Console display string.
# ------------------------------------------------------------------------------
def format_console_line(LINE: str, LEVEL_UPPER: str) -> str:
    if LEVEL_UPPER != "ERROR":
        return LINE

    return f"{ANSI_RED}{LINE}{ANSI_RESET}"


# ------------------------------------------------------------------------------
# This function rotates and prunes worker logs based on configured policy.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: None.
# ------------------------------------------------------------------------------
def rotate_log_if_needed(
    LOG_FILE: Path,
    CONFIG: LoggerConfig | None = None,
) -> None:
    ACTIVE_CONFIG = CONFIG or load_logger_config()

    if not LOG_FILE.exists():
        return

    SHOULD_ROTATE = should_rotate_for_size(
        LOG_FILE,
        ACTIVE_CONFIG,
    ) or should_rotate_for_daily_rollover(
        LOG_FILE,
        ACTIVE_CONFIG,
    )
    if not SHOULD_ROTATE:
        return

    rotate_log_file(LOG_FILE)
    prune_rotated_logs(LOG_FILE, ACTIVE_CONFIG)


# ------------------------------------------------------------------------------
# This function checks size-based log rotation trigger.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: True when file size meets or exceeds configured threshold.
# ------------------------------------------------------------------------------
def should_rotate_for_size(
    LOG_FILE: Path,
    CONFIG: LoggerConfig | None = None,
) -> bool:
    ACTIVE_CONFIG = CONFIG or load_logger_config()
    MAX_BYTES = ACTIVE_CONFIG.rotate_max_bytes
    if MAX_BYTES < 1:
        return False

    try:
        return LOG_FILE.stat().st_size >= MAX_BYTES
    except OSError:
        return False


# ------------------------------------------------------------------------------
# This function checks date-based daily rollover trigger.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: True when file has entries from a previous local date.
# ------------------------------------------------------------------------------
def should_rotate_for_daily_rollover(
    LOG_FILE: Path,
    CONFIG: LoggerConfig | None = None,
) -> bool:
    ACTIVE_CONFIG = CONFIG or load_logger_config()

    if not ACTIVE_CONFIG.rotate_daily:
        return False

    try:
        MODIFIED_EPOCH = LOG_FILE.stat().st_mtime
    except OSError:
        return False

    FILE_DATE = datetime.fromtimestamp(MODIFIED_EPOCH, tz=now_local().tzinfo).date()
    NOW_DATE = now_local().date()
    return FILE_DATE != NOW_DATE


# ------------------------------------------------------------------------------
# This function rotates the active log file into a compressed archive.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: None.
# ------------------------------------------------------------------------------
def rotate_log_file(LOG_FILE: Path) -> None:
    STAMP = now_local().strftime("%Y%m%d-%H%M%S")
    ROTATED_NAME = ROTATED_FILE_PATTERN.format(name=LOG_FILE.stem, stamp=STAMP)
    ROTATED_PATH = LOG_FILE.with_name(ROTATED_NAME)
    COMPRESSED_PATH = ROTATED_PATH.with_suffix(f"{ROTATED_PATH.suffix}.gz")

    try:
        LOG_FILE.replace(ROTATED_PATH)
    except OSError:
        return

    try:
        with ROTATED_PATH.open("rb") as SOURCE:
            with gzip.open(COMPRESSED_PATH, "wb") as TARGET:
                shutil.copyfileobj(SOURCE, TARGET)
    except OSError:
        return

    try:
        ROTATED_PATH.unlink()
    except OSError:
        return


# ------------------------------------------------------------------------------
# This function removes old rotated log archives by retention age.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: None.
# ------------------------------------------------------------------------------
def prune_rotated_logs(
    LOG_FILE: Path,
    CONFIG: LoggerConfig | None = None,
) -> None:
    ACTIVE_CONFIG = CONFIG or load_logger_config()
    KEEP_DAYS = ACTIVE_CONFIG.rotate_keep_days
    if KEEP_DAYS < 1:
        return

    CUTOFF = now_local() - timedelta(days=KEEP_DAYS)
    PATTERN = f"{LOG_FILE.stem}.*.log.gz"

    for PATH in LOG_FILE.parent.glob(PATTERN):
        try:
            MODIFIED_DT = datetime.fromtimestamp(PATH.stat().st_mtime, tz=now_local().tzinfo)
        except OSError:
            continue

        if MODIFIED_DT >= CUTOFF:
            continue

        try:
            PATH.unlink()
        except OSError:
            continue


# ------------------------------------------------------------------------------
# This function reads configured maximum log size in bytes.
#
# Returns: Positive byte count, defaulting to 100 MiB.
# ------------------------------------------------------------------------------
def get_log_rotate_max_bytes() -> int:
    return load_logger_config().rotate_max_bytes


# ------------------------------------------------------------------------------
# This function reads configured daily rollover toggle.
#
# Returns: True when daily rollover is enabled, defaulting to true.
# ------------------------------------------------------------------------------
def get_log_rotate_daily() -> bool:
    return load_logger_config().rotate_daily


# ------------------------------------------------------------------------------
# This function reads configured rotated-log retention period in days.
#
# Returns: Positive day count, defaulting to 14 days.
# ------------------------------------------------------------------------------
def get_log_rotate_keep_days() -> int:
    return load_logger_config().rotate_keep_days
