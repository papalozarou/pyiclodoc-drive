# ------------------------------------------------------------------------------
# This module validates parsed runtime configuration before worker startup.
# ------------------------------------------------------------------------------

from __future__ import annotations

from app.config import AppConfig
from app.scheduler import MONTHLY_WEEK_MAP, parse_daily, parse_weekday_list


# ------------------------------------------------------------------------------
# This function validates required runtime configuration.
#
# 1. "CONFIG" is the loaded runtime configuration model.
#
# Returns: Validation error list; empty list means configuration is usable.
# ------------------------------------------------------------------------------
def validate_config(CONFIG: AppConfig) -> list[str]:
    ERRORS = list(CONFIG.config_parse_errors)

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

    if CONFIG.reauth_interval_days < 1:
        ERRORS.append("REAUTH_INTERVAL_DAYS must be an integer of at least 1.")

    if CONFIG.safety_net_sample_size < 1:
        ERRORS.append("SAFETY_NET_SAMPLE_SIZE must be an integer of at least 1.")

    return ERRORS
