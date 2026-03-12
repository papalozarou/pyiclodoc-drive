# ------------------------------------------------------------------------------
# This module provides lightweight structured logging helpers for console and
# file output.
# ------------------------------------------------------------------------------

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
# This function produces a configured-timezone timestamp string.
#
# Returns: Display timestamp including timezone abbreviation.
# ------------------------------------------------------------------------------
def get_timestamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")


# ------------------------------------------------------------------------------
# This function returns the configured log threshold from environment.
#
# Returns: Normalised log level token.
# ------------------------------------------------------------------------------
def get_log_level() -> str:
    RAW_VALUE = os.getenv("LOG_LEVEL", "info").strip().lower()

    if RAW_VALUE in LOG_LEVELS:
        return RAW_VALUE

    return "info"


# ------------------------------------------------------------------------------
# This function checks whether a log line should be emitted.
#
# 1. "LEVEL" is message severity token.
#
# Returns: True when line should be written and printed.
# ------------------------------------------------------------------------------
def should_log(LEVEL: str) -> bool:
    CURRENT_LEVEL = get_log_level()
    CURRENT_WEIGHT = LOG_LEVELS.get(CURRENT_LEVEL, LOG_LEVELS["info"])
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
    if not should_log(LEVEL):
        return

    rotate_log_if_needed(LOG_FILE)

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
def rotate_log_if_needed(LOG_FILE: Path) -> None:
    if not LOG_FILE.exists():
        return

    SHOULD_ROTATE = should_rotate_for_size(LOG_FILE) or should_rotate_for_daily_rollover(LOG_FILE)
    if not SHOULD_ROTATE:
        return

    rotate_log_file(LOG_FILE)
    prune_rotated_logs(LOG_FILE)


# ------------------------------------------------------------------------------
# This function checks size-based log rotation trigger.
#
# 1. "LOG_FILE" is the destination log file.
#
# Returns: True when file size meets or exceeds configured threshold.
# ------------------------------------------------------------------------------
def should_rotate_for_size(LOG_FILE: Path) -> bool:
    MAX_BYTES = get_log_rotate_max_bytes()
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
def should_rotate_for_daily_rollover(LOG_FILE: Path) -> bool:
    if not get_log_rotate_daily():
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
def prune_rotated_logs(LOG_FILE: Path) -> None:
    KEEP_DAYS = get_log_rotate_keep_days()
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
    DEFAULT_BYTES = 100 * 1024 * 1024
    RAW_VALUE = os.getenv("LOG_ROTATE_MAX_MIB", "100").strip()

    if not RAW_VALUE.isdigit():
        return DEFAULT_BYTES

    VALUE_MIB = int(RAW_VALUE)
    if VALUE_MIB < 1:
        return DEFAULT_BYTES

    return VALUE_MIB * 1024 * 1024


# ------------------------------------------------------------------------------
# This function reads configured daily rollover toggle.
#
# Returns: True when daily rollover is enabled, defaulting to true.
# ------------------------------------------------------------------------------
def get_log_rotate_daily() -> bool:
    RAW_VALUE = os.getenv("LOG_ROTATE_DAILY", "true").strip().lower()

    if RAW_VALUE in {"1", "true", "yes", "on"}:
        return True

    if RAW_VALUE in {"0", "false", "no", "off"}:
        return False

    return True


# ------------------------------------------------------------------------------
# This function reads configured rotated-log retention period in days.
#
# Returns: Positive day count, defaulting to 14 days.
# ------------------------------------------------------------------------------
def get_log_rotate_keep_days() -> int:
    RAW_VALUE = os.getenv("LOG_ROTATE_KEEP_DAYS", "14").strip()

    if not RAW_VALUE.isdigit():
        return 14

    VALUE = int(RAW_VALUE)
    if VALUE < 1:
        return 14

    return VALUE
