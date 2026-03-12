# ------------------------------------------------------------------------------
# This test module validates authentication reminder windows and
# reauthentication state transitions.
#
# Notes:
# https://docs.python.org/3/library/datetime.html#aware-and-naive-objects
# ------------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.config import AppConfig
from app.main import (
    calculate_next_daily_run_epoch,
    calculate_next_monthly_run_epoch,
    calculate_next_twice_weekly_run_epoch,
    calculate_next_weekly_run_epoch,
    get_monthly_weekday_day,
    parse_daily,
    parse_weekday,
    parse_weekday_list,
    process_reauth_reminders,
    reauth_days_left,
    validate_config,
)
from app.state import AuthState
from app.telegram_bot import TelegramConfig


# ------------------------------------------------------------------------------
# This function returns an ISO-8601 UTC string for days in the past.
# ------------------------------------------------------------------------------
def iso_days_ago(DAYS: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()


# ------------------------------------------------------------------------------
# This function creates an "AppConfig" test fixture with override support.
# ------------------------------------------------------------------------------
def build_config(**OVERRIDES: object) -> AppConfig:
    BASE_CONFIG = AppConfig(
        container_username="alice",
        icloud_email="alice@example.com",
        icloud_password="password",
        telegram_bot_token="token",
        telegram_chat_id="12345",
        keychain_service_name="icloud-drive-backup",
        run_once=False,
        schedule_mode="interval",
        schedule_backup_time="02:00",
        schedule_weekdays="monday,thursday",
        schedule_monthly_week="first",
        schedule_interval_minutes=1440,
        traversal_workers=1,
        sync_workers=0,
        download_chunk_mib=4,
        reauth_interval_days=30,
        output_dir=Path("/tmp/output"),
        config_dir=Path("/tmp/config"),
        logs_dir=Path("/tmp/logs"),
        manifest_path=Path("/tmp/config/manifest.json"),
        auth_state_path=Path("/tmp/config/auth_state.json"),
        heartbeat_path=Path("/tmp/logs/heartbeat.txt"),
        cookie_dir=Path("/tmp/config/cookies"),
        session_dir=Path("/tmp/config/session"),
        icloudpd_compat_dir=Path("/tmp/config/icloudpd"),
        safety_net_sample_size=200,
    )

    CONFIG_VALUES = BASE_CONFIG.__dict__.copy()
    CONFIG_VALUES.update(OVERRIDES)
    return AppConfig(**CONFIG_VALUES)


# ------------------------------------------------------------------------------
# These tests verify reminder behaviour for five-day and two-day thresholds.
# ------------------------------------------------------------------------------
class TestMainReminderLogic(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms the remaining-day calculation tracks elapsed days
# within expected bounds.
# --------------------------------------------------------------------------
    def test_reauth_days_left(self) -> None:
        REMAINING = reauth_days_left(iso_days_ago(25), 30)

        self.assertGreaterEqual(REMAINING, 4)
        self.assertLessEqual(REMAINING, 5)

# --------------------------------------------------------------------------
# This test confirms the five-day alert stage is recorded when the
# threshold is reached.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_sets_alert5(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            STATE_PATH = Path(TMPDIR) / "auth_state.json"
            TELEGRAM = TelegramConfig(bot_token="", chat_id="")
            STATE = AuthState(
                last_auth_utc=iso_days_ago(25),
                auth_pending=False,
                reauth_pending=False,
                reminder_stage="none",
            )

            UPDATED = process_reauth_reminders(STATE, STATE_PATH, TELEGRAM, "alice", 30)

            self.assertEqual(UPDATED.reminder_stage, "alert5")
            self.assertFalse(UPDATED.reauth_pending)

# --------------------------------------------------------------------------
# This test confirms the two-day prompt stage enables reauthentication
# pending state.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_sets_prompt2(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            STATE_PATH = Path(TMPDIR) / "auth_state.json"
            TELEGRAM = TelegramConfig(bot_token="", chat_id="")
            STATE = AuthState(
                last_auth_utc=iso_days_ago(29),
                auth_pending=False,
                reauth_pending=False,
                reminder_stage="alert5",
            )

            UPDATED = process_reauth_reminders(STATE, STATE_PATH, TELEGRAM, "alice", 30)

            self.assertEqual(UPDATED.reminder_stage, "prompt2")
            self.assertTrue(UPDATED.reauth_pending)


# ------------------------------------------------------------------------------
# These tests verify configuration validation for one-shot and interval modes.
# ------------------------------------------------------------------------------
class TestMainValidation(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms regular mode requires interval values of at least one.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_zero_interval_when_not_run_once(self) -> None:
        CONFIG = build_config(run_once=False, schedule_interval_minutes=0)

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_INTERVAL_MINUTES must be at least 1 when RUN_ONCE is false.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms one-shot mode permits zero-interval values.
# --------------------------------------------------------------------------
    def test_validate_config_allows_zero_interval_when_run_once(self) -> None:
        CONFIG = build_config(run_once=True, schedule_interval_minutes=0)

        ERRORS = validate_config(CONFIG)

        self.assertEqual(ERRORS, [])

# --------------------------------------------------------------------------
# This test confirms invalid schedule mode values are rejected.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_schedule_mode(self) -> None:
        CONFIG = build_config(schedule_mode="yearly")

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_MODE must be one of: interval, daily, weekly, twice_weekly, monthly.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms invalid daily-time values are rejected in daily mode.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_daily(self) -> None:
        CONFIG = build_config(schedule_mode="daily", schedule_backup_time="25:61")

        ERRORS = validate_config(CONFIG)

        self.assertIn("SCHEDULE_BACKUP_TIME must use 24-hour HH:MM format.", ERRORS)

# --------------------------------------------------------------------------
# This test confirms daily mode does not require interval minimum.
# --------------------------------------------------------------------------
    def test_validate_config_allows_zero_interval_in_daily_mode(self) -> None:
        CONFIG = build_config(
            run_once=False,
            schedule_mode="daily",
            schedule_backup_time="02:00",
            schedule_interval_minutes=0,
        )

        ERRORS = validate_config(CONFIG)

        self.assertEqual(ERRORS, [])

# --------------------------------------------------------------------------
# This test confirms worker override validation rejects out-of-range values.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_out_of_range_sync_workers(self) -> None:
        CONFIG = build_config(sync_workers=99)

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SYNC_DOWNLOAD_WORKERS must be auto or an integer between 1 and 16.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms traversal worker validation rejects out-of-range
# values.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_out_of_range_traversal_workers(self) -> None:
        CONFIG = build_config(traversal_workers=0)

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SYNC_TRAVERSAL_WORKERS must be an integer between 1 and 8.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms download chunk size validation rejects out-of-range
# values.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_out_of_range_download_chunk(self) -> None:
        CONFIG = build_config(download_chunk_mib=99)

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SYNC_DOWNLOAD_CHUNK_MIB must be an integer between 1 and 16.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms weekly mode requires exactly one valid weekday.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_weekday(self) -> None:
        CONFIG = build_config(schedule_mode="weekly", schedule_weekdays="monday,thursday")

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_WEEKDAYS must contain exactly one valid weekday name for weekly mode.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms twice-weekly mode requires two distinct weekdays.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_twice_weekly_days(self) -> None:
        CONFIG = build_config(schedule_mode="twice_weekly", schedule_weekdays="monday,monday")

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_WEEKDAYS must contain exactly two distinct weekday names.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms monthly mode rejects invalid monthly week tokens.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_monthly_week(self) -> None:
        CONFIG = build_config(
            schedule_mode="monthly",
            schedule_weekdays="monday",
            schedule_monthly_week="fifth",
        )

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_MONTHLY_WEEK must be one of: first, second, third, fourth, last.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms monthly mode requires exactly one valid weekday.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_invalid_monthly_weekdays(self) -> None:
        CONFIG = build_config(
            schedule_mode="monthly",
            schedule_weekdays="monday,thursday",
            schedule_monthly_week="first",
        )

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "SCHEDULE_WEEKDAYS must contain exactly one valid weekday name for monthly mode.",
            ERRORS,
        )


# ------------------------------------------------------------------------------
# These tests verify daily schedule parsing and next-run timestamp logic.
# ------------------------------------------------------------------------------
class TestMainDailySchedule(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms "HH:MM" parsing accepts valid 24-hour values.
# --------------------------------------------------------------------------
    def test_parse_daily_valid(self) -> None:
        self.assertEqual(parse_daily("02:30"), (2, 30))

# --------------------------------------------------------------------------
# This test confirms invalid daily-time text is rejected.
# --------------------------------------------------------------------------
    def test_parse_daily_invalid(self) -> None:
        self.assertIsNone(parse_daily("2pm"))

# --------------------------------------------------------------------------
# This test confirms weekday parsing handles valid and invalid values.
# --------------------------------------------------------------------------
    def test_parse_weekday(self) -> None:
        self.assertEqual(parse_weekday("monday"), 0)
        self.assertIsNone(parse_weekday("funday"))

# --------------------------------------------------------------------------
# This test confirms weekday list parsing enforces two distinct entries.
# --------------------------------------------------------------------------
    def test_parse_weekday_list(self) -> None:
        self.assertEqual(parse_weekday_list("monday", 1), [0])
        self.assertEqual(parse_weekday_list("monday,thursday", 2), [0, 3])
        self.assertIsNone(parse_weekday_list("monday,monday", 2))

# --------------------------------------------------------------------------
# This test confirms next daily run uses same-day target when still ahead.
# --------------------------------------------------------------------------
    def test_calculate_next_daily_run_same_day(self) -> None:
        NOW = datetime(2026, 3, 10, 1, 30, 0, tzinfo=timezone.utc)

        NEXT_EPOCH = calculate_next_daily_run_epoch(NOW, "02:00")

        EXPECTED = int(datetime(2026, 3, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(NEXT_EPOCH, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms next daily run rolls to tomorrow when time has passed.
# --------------------------------------------------------------------------
    def test_calculate_next_daily_run_next_day(self) -> None:
        NOW = datetime(2026, 3, 10, 3, 0, 0, tzinfo=timezone.utc)

        NEXT_EPOCH = calculate_next_daily_run_epoch(NOW, "02:00")

        EXPECTED = int(datetime(2026, 3, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(NEXT_EPOCH, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms weekly schedules target the requested weekday/time.
# --------------------------------------------------------------------------
    def test_calculate_next_weekly_run(self) -> None:
        NOW = datetime(2026, 3, 10, 1, 30, 0, tzinfo=timezone.utc)  # Tuesday

        NEXT_EPOCH = calculate_next_weekly_run_epoch(NOW, "thursday", "02:00")

        EXPECTED = int(datetime(2026, 3, 12, 2, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(NEXT_EPOCH, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms twice-weekly schedules choose the nearest configured day.
# --------------------------------------------------------------------------
    def test_calculate_next_twice_weekly_run(self) -> None:
        NOW = datetime(2026, 3, 10, 1, 30, 0, tzinfo=timezone.utc)  # Tuesday

        NEXT_EPOCH = calculate_next_twice_weekly_run_epoch(NOW, "monday,thursday", "02:00")

        EXPECTED = int(datetime(2026, 3, 12, 2, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(NEXT_EPOCH, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms monthly helper resolves first Monday correctly.
# --------------------------------------------------------------------------
    def test_get_monthly_weekday_day(self) -> None:
        self.assertEqual(get_monthly_weekday_day(2026, 3, 0, "first"), 2)

# --------------------------------------------------------------------------
# This test confirms monthly schedules move to next month after passing target.
# --------------------------------------------------------------------------
    def test_calculate_next_monthly_run(self) -> None:
        NOW = datetime(2026, 3, 3, 3, 0, 0, tzinfo=timezone.utc)

        NEXT_EPOCH = calculate_next_monthly_run_epoch(NOW, "monday", "first", "02:00")

        EXPECTED = int(datetime(2026, 4, 6, 2, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(NEXT_EPOCH, EXPECTED)


if __name__ == "__main__":
    unittest.main()
