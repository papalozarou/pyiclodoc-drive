# ------------------------------------------------------------------------------
# This module provides lightweight structured logging helpers for console and
# file output.
# ------------------------------------------------------------------------------

from pathlib import Path
import os

from app.time_utils import now_local

LOG_LEVELS = {
    "debug": 10,
    "info": 20,
    "error": 30,
}
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


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
