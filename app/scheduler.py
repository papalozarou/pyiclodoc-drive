# ------------------------------------------------------------------------------
# This module encapsulates scheduling parsing, formatting, and next-run logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from app.config import AppConfig
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
# This function formats schedule settings as plain-English backup wording.
#
# 1. "CONFIG" is runtime configuration.
# 2. "TRIGGER" is backup trigger context.
#
# Returns: Human-readable schedule description.
# ------------------------------------------------------------------------------
def format_schedule_description(CONFIG: AppConfig, TRIGGER: str) -> str:
    if TRIGGER == "one-shot":
        return "One-shot run – configured schedule is ignored"

    if CONFIG.schedule_mode == "interval":
        return f"Every {CONFIG.schedule_interval_minutes} minutes"

    if CONFIG.schedule_mode == "daily":
        return f"Daily at {CONFIG.schedule_backup_time}"

    if CONFIG.schedule_mode == "weekly":
        DAY_TEXT = CONFIG.schedule_weekdays.strip().title()
        return f"Weekly on {DAY_TEXT} at {CONFIG.schedule_backup_time}"

    if CONFIG.schedule_mode == "twice_weekly":
        RAW_DAYS = [
            PART.strip().title()
            for PART in CONFIG.schedule_weekdays.split(",")
            if PART.strip()
        ]
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

    if TRIGGER == "scheduled":
        return f"Scheduled {SCHEDULE_DESCRIPTION.lower()}."

    if TRIGGER == "manual":
        return f"Manual, then {SCHEDULE_DESCRIPTION.lower()}."

    return f"{SCHEDULE_DESCRIPTION}."
