"""This test module validates authentication reminder windows and reauthentication state transitions.

Reference for timezone-aware datetime handling:
https://docs.python.org/3/library/datetime.html#aware-and-naive-objects
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.main import process_reauth_reminders, reauth_days_left
from app.state import AuthState
from app.telegram_bot import TelegramConfig


# ------------------------------------------------------------------------------
# This function returns an ISO-8601 UTC string for days in the past.
# ------------------------------------------------------------------------------
def iso_days_ago(DAYS: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()


class TestMainReminderLogic(unittest.TestCase):
    """These tests verify reminder behaviour at the documented five-day and two-day thresholds."""

    # ------------------------------------------------------------------------------
    # This test confirms the remaining-day calculation tracks elapsed days within expected bounds.
    # ------------------------------------------------------------------------------
    def test_reauth_days_left(self) -> None:
        REMAINING = reauth_days_left(iso_days_ago(25), 30)

        self.assertGreaterEqual(REMAINING, 4)
        self.assertLessEqual(REMAINING, 5)

    # ------------------------------------------------------------------------------
    # This test confirms the five-day alert stage is recorded when the threshold is reached.
    # ------------------------------------------------------------------------------
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

    # ------------------------------------------------------------------------------
    # This test confirms the two-day prompt stage enables reauthentication pending state.
    # ------------------------------------------------------------------------------
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


if __name__ == "__main__":
    unittest.main()
