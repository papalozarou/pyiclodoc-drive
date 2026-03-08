"""This module provides lightweight structured logging helpers for console and file output."""

from pathlib import Path

from app.time_utils import now_local


# ------------------------------------------------------------------------------
# This function produces a configured-timezone timestamp string.
#
# Returns: Display timestamp including timezone abbreviation.
# ------------------------------------------------------------------------------
def get_timestamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")


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
    LINE = f"[{get_timestamp()}] [{LEVEL.upper()}] {MESSAGE}"
    print(LINE, flush=True)

    with LOG_FILE.open("a", encoding="utf-8") as HANDLE:
        HANDLE.write(f"{LINE}\n")
