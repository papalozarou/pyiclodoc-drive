"""This module provides timezone-aware current time helpers based on the container TZ setting."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
import os

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ------------------------------------------------------------------------------
# This function returns timezone from `TZ`, falling back to UTC.
# Returns: A valid timezone object resolved from IANA zone data.
# Notes: ZoneInfo behaviour follows Python standard library guidance:
# https://docs.python.org/3/library/zoneinfo.html
# ------------------------------------------------------------------------------
def configured_timezone() -> tzinfo:
    TZ_NAME = os.getenv("TZ", "UTC").strip() or "UTC"

    try:
        return ZoneInfo(TZ_NAME)
    except ZoneInfoNotFoundError:
        return timezone.utc


# ------------------------------------------------------------------------------
# This function returns the current time in the configured timezone.
# Returns: Offset-aware `datetime` in the configured timezone.
# ------------------------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now(configured_timezone())


# ------------------------------------------------------------------------------
# This function returns a configured-timezone ISO-8601 timestamp.
# Returns: Offset-aware ISO-8601 timestamp string.
# ------------------------------------------------------------------------------
def now_local_iso() -> str:
    return now_local().isoformat()
