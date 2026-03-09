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
from app.main import process_reauth_reminders, reauth_days_left, validate_config
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
        backup_interval_minutes=1440,
        startup_delay_seconds=0,
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
        CONFIG = build_config(run_once=False, backup_interval_minutes=0)

        ERRORS = validate_config(CONFIG)

        self.assertIn(
            "BACKUP_INTERVAL_MINUTES must be at least 1 when RUN_ONCE is false.",
            ERRORS,
        )

# --------------------------------------------------------------------------
# This test confirms one-shot mode permits zero-interval values.
# --------------------------------------------------------------------------
    def test_validate_config_allows_zero_interval_when_run_once(self) -> None:
        CONFIG = build_config(run_once=True, backup_interval_minutes=0)

        ERRORS = validate_config(CONFIG)

        self.assertEqual(ERRORS, [])


if __name__ == "__main__":
    unittest.main()
