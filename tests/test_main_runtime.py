# ------------------------------------------------------------------------------
# This test module verifies runtime helper behaviour in "app.main".
# ------------------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.auth_runtime import AuthAttemptResult, parse_iso
from app.backup_runtime import BackupRunResult, format_deleted_summary
from app.command_runtime import CommandHandleResult
from app.config import AppConfig
from app.main import (
    attempt_auth,
    enforce_safety_net,
    get_next_run_epoch,
    handle_command,
    HeartbeatTelemetry,
    HeartbeatUpdater,
    notify,
    notify_container_stopped,
    poll_command_batch,
    process_reauth_reminders,
    run_backup,
    start_heartbeat_updater,
    update_heartbeat,
)
from app.scheduler import get_monthly_weekday_day
from app.worker_runtime import CommandPollingState, WorkerAuthState, wait_for_one_shot_auth
from app.state import AuthState, load_auth_state, save_auth_state
from app.syncer import SyncResult
from app.telegram_bot import TelegramConfig


# ------------------------------------------------------------------------------
# This function creates an "AppConfig" fixture for runtime helper tests.
# ------------------------------------------------------------------------------
def build_config_for_runtime(TMPDIR: str) -> AppConfig:
    ROOT_DIR = Path(TMPDIR)
    CONFIG_DIR = ROOT_DIR / "config"
    OUTPUT_DIR = ROOT_DIR / "output"
    LOGS_DIR = ROOT_DIR / "logs"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        container_username="alice",
        icloud_email="alice@example.com",
        icloud_password="password",
        telegram_bot_token="token",
        telegram_chat_id="12345",
        keychain_service_name="pyiclodoc-drive",
        run_once=False,
        schedule_mode="interval",
        schedule_backup_time="02:00",
        schedule_weekdays="monday",
        schedule_monthly_week="first",
        schedule_interval_minutes=60,
        backup_delete_removed=False,
        traversal_workers=1,
        sync_workers=0,
        download_chunk_mib=4,
        reauth_interval_days=30,
        output_dir=OUTPUT_DIR,
        config_dir=CONFIG_DIR,
        logs_dir=LOGS_DIR,
        manifest_path=CONFIG_DIR / "pyiclodoc-drive-manifest.json",
        auth_state_path=CONFIG_DIR / "pyiclodoc-drive-auth_state.json",
        heartbeat_path=LOGS_DIR / "pyiclodoc-drive-heartbeat.txt",
        safety_net_done_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_done.flag",
        safety_net_blocked_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_blocked.flag",
        cookie_dir=CONFIG_DIR / "cookies",
        session_dir=CONFIG_DIR / "session",
        icloudpd_compat_dir=CONFIG_DIR / "icloudpd",
        safety_net_sample_size=200,
    )


# ------------------------------------------------------------------------------
# These tests verify auth, commands, and safety-net runtime helper behaviour.
# ------------------------------------------------------------------------------
class TestMainRuntimeHelpers(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms deleted-path summary text uses natural singular and
# plural wording.
# --------------------------------------------------------------------------
    def test_format_deleted_summary_uses_natural_wording(self) -> None:
        self.assertEqual(
            format_deleted_summary(0, 0),
            "Deleted: 0 files, 0 directories",
        )
        self.assertEqual(
            format_deleted_summary(1, 0),
            "Deleted: 1 file, 0 directories",
        )
        self.assertEqual(
            format_deleted_summary(2, 1),
            "Deleted: 2 files, 1 directory",
        )

# --------------------------------------------------------------------------
# This test confirms parse_iso falls back to epoch for invalid values.
# --------------------------------------------------------------------------
    def test_parse_iso_invalid_value_returns_epoch(self) -> None:
        RESULT = parse_iso("not-a-date")
        self.assertEqual(RESULT, datetime(1970, 1, 1, tzinfo=timezone.utc))

# --------------------------------------------------------------------------
# This test confirms monthly helper rejects unsupported week tokens.
# --------------------------------------------------------------------------
    def test_get_monthly_weekday_day_rejects_invalid_week_token(self) -> None:
        RESULT = get_monthly_weekday_day(2026, 3, 0, "fifth")
        self.assertIsNone(RESULT)

# --------------------------------------------------------------------------
# This test confirms get_next_run_epoch returns NOW for invalid weekly day.
# --------------------------------------------------------------------------
    def test_get_next_run_epoch_weekly_invalid_day_returns_now(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            CONFIG = AppConfig(**(CONFIG.__dict__ | {"schedule_mode": "weekly", "schedule_weekdays": "funday"}))

            RESULT = get_next_run_epoch(CONFIG, NOW_EPOCH=1234)

        self.assertEqual(RESULT, 1234)

# --------------------------------------------------------------------------
# This test confirms get_next_run_epoch returns NOW for invalid monthly day.
# --------------------------------------------------------------------------
    def test_get_next_run_epoch_monthly_invalid_day_returns_now(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            CONFIG = AppConfig(
                **(CONFIG.__dict__ | {"schedule_mode": "monthly", "schedule_weekdays": "monday,thursday"})
            )

            RESULT = get_next_run_epoch(CONFIG, NOW_EPOCH=999)

        self.assertEqual(RESULT, 999)

# --------------------------------------------------------------------------
# This test confirms update_heartbeat creates the heartbeat file.
# --------------------------------------------------------------------------
    def test_update_heartbeat_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            HEARTBEAT_PATH = Path(TMPDIR) / "logs" / "pyiclodoc-drive-heartbeat.txt"
            IS_SUCCESS, FAILURE_DETAIL = update_heartbeat(HEARTBEAT_PATH)
            self.assertTrue(HEARTBEAT_PATH.exists())
            self.assertTrue(IS_SUCCESS)
            self.assertEqual(FAILURE_DETAIL, "")

# --------------------------------------------------------------------------
# This test confirms heartbeat updater starts a daemon thread and returns a
# lifecycle controller.
# --------------------------------------------------------------------------
    def test_start_heartbeat_updater_starts_daemon_thread(self) -> None:
        HEARTBEAT_PATH = Path("/tmp/pyiclodoc-drive-heartbeat.txt")
        LOG_FILE = Path("/tmp/pyiclodoc-drive-worker.log")

        with patch("app.main.threading.Thread") as THREAD:
            THREAD_INSTANCE = Mock()
            THREAD.return_value = THREAD_INSTANCE

            UPDATER = start_heartbeat_updater(HEARTBEAT_PATH, LOG_FILE, 65)

        THREAD.assert_called_once()
        self.assertEqual(THREAD.call_args.kwargs.get("daemon"), True)
        THREAD_INSTANCE.start.assert_called_once()
        self.assertFalse(UPDATER.stop_event.is_set())
        self.assertIs(UPDATER.thread, THREAD_INSTANCE)
        self.assertIsInstance(UPDATER.telemetry, HeartbeatTelemetry)
        self.assertEqual(UPDATER.max_age_seconds, 65)

# --------------------------------------------------------------------------
# This test confirms heartbeat updater shutdown sets the stop signal and waits
# for the writer thread to finish.
# --------------------------------------------------------------------------
    def test_heartbeat_updater_stop_joins_thread(self) -> None:
        STOP_EVENT = Mock()
        THREAD = Mock()
        UPDATER = HeartbeatUpdater(
            stop_event=STOP_EVENT,
            thread=THREAD,
            telemetry=HeartbeatTelemetry(),
            max_age_seconds=65,
        )

        UPDATER.stop()

        STOP_EVENT.set.assert_called_once()
        THREAD.join.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms heartbeat liveness ignores the expected shutdown path.
# --------------------------------------------------------------------------
    def test_heartbeat_updater_ignores_stop_requested_exit(self) -> None:
        THREAD = Mock()
        THREAD.is_alive.return_value = False
        TELEMETRY = HeartbeatTelemetry(loop_exit_reason="stop_requested")
        UPDATER = HeartbeatUpdater(
            stop_event=Mock(),
            thread=THREAD,
            telemetry=TELEMETRY,
            max_age_seconds=65,
        )

        RESULT = UPDATER.get_liveness_failure(100)

        self.assertIsNone(RESULT)

# --------------------------------------------------------------------------
# This test confirms heartbeat liveness reports stale heartbeat age.
# --------------------------------------------------------------------------
    def test_heartbeat_updater_reports_stale_heartbeat(self) -> None:
        THREAD = Mock()
        THREAD.is_alive.return_value = True
        TELEMETRY = HeartbeatTelemetry(
            first_attempt_epoch=10,
            last_success_epoch=10,
            last_failure_detail="touch_failed",
        )
        UPDATER = HeartbeatUpdater(
            stop_event=Mock(),
            thread=THREAD,
            telemetry=TELEMETRY,
            max_age_seconds=65,
        )

        RESULT = UPDATER.get_liveness_failure(100)

        self.assertIn("heartbeat_stale", RESULT)
        self.assertIn("touch_failed", RESULT)

# --------------------------------------------------------------------------
# This test confirms heartbeat liveness fails after repeated startup write
# failures when no successful heartbeat was ever recorded.
# --------------------------------------------------------------------------
    def test_heartbeat_updater_reports_startup_failure_after_budget_expires(self) -> None:
        THREAD = Mock()
        THREAD.is_alive.return_value = True
        TELEMETRY = HeartbeatTelemetry(
            first_attempt_epoch=10,
            last_attempt_epoch=70,
            last_failure_detail="PermissionError: denied",
        )
        UPDATER = HeartbeatUpdater(
            stop_event=Mock(),
            thread=THREAD,
            telemetry=TELEMETRY,
            max_age_seconds=65,
        )

        RESULT = UPDATER.get_liveness_failure(100)

        self.assertIn("heartbeat_startup_failed", RESULT)
        self.assertIn("PermissionError: denied", RESULT)

# --------------------------------------------------------------------------
# This test confirms heartbeat liveness allows a short startup grace window
# before the first successful heartbeat is written.
# --------------------------------------------------------------------------
    def test_heartbeat_updater_allows_short_startup_failure_window(self) -> None:
        THREAD = Mock()
        THREAD.is_alive.return_value = True
        TELEMETRY = HeartbeatTelemetry(
            first_attempt_epoch=10,
            last_attempt_epoch=40,
            last_failure_detail="PermissionError: denied",
        )
        UPDATER = HeartbeatUpdater(
            stop_event=Mock(),
            thread=THREAD,
            telemetry=TELEMETRY,
            max_age_seconds=65,
        )

        RESULT = UPDATER.get_liveness_failure(60)

        self.assertIsNone(RESULT)

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait returns immediately when ready.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_returns_immediately_when_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            TELEGRAM = TelegramConfig("token", "12345")

            RESULT = wait_for_one_shot_auth(
                SimpleNamespace(CONFIG=CONFIG, TELEGRAM=TELEGRAM),
                Mock(),
                WorkerAuthState(auth_state=STATE, is_authenticated=True),
                CommandPollingState(phase="live_polling", next_update_offset=None),
                SimpleNamespace(
                    poll_command_batch_fn=Mock(
                        return_value=SimpleNamespace(
                            commands=[],
                            next_update_offset=None,
                        )
                    ),
                    handle_command_fn=Mock(),
                    time_fn=lambda: 0,
                    sleep_fn=Mock(),
                ),
            )

        self.assertEqual(RESULT.auth_state, STATE)
        self.assertTrue(RESULT.is_authenticated)

# --------------------------------------------------------------------------
# This test confirms notify delegates to send_message_result.
# --------------------------------------------------------------------------
    def test_notify_delegates_to_send_message(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")
        with patch("app.runtime_helpers.send_message_result") as SEND:
            SEND.return_value = SimpleNamespace(success=True, failure_detail="")
            notify(TELEGRAM, "hello")
        SEND.assert_called_once_with(TELEGRAM, "hello")

# --------------------------------------------------------------------------
# This test confirms notify logs Telegram failure detail when delivery fails.
# --------------------------------------------------------------------------
    def test_notify_logs_telegram_failure_detail(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")

        with patch("app.runtime_helpers.send_message_result") as SEND:
            with patch("app.runtime_helpers.log_line") as LOG_LINE:
                SEND.return_value = SimpleNamespace(
                    success=False,
                    disabled=False,
                    failure_detail="Telegram API rejected the request: Bad Request",
                )
                notify(TELEGRAM, "hello")

        LOG_LINE.assert_called_once()
        self.assertIsNone(LOG_LINE.call_args[0][0])
        self.assertEqual(LOG_LINE.call_args[0][1], "error")
        self.assertIn("Telegram notification failed", LOG_LINE.call_args[0][2])
        self.assertIn("Bad Request", LOG_LINE.call_args[0][2])

# --------------------------------------------------------------------------
# This test confirms notify stays quiet when Telegram integration is disabled.
# --------------------------------------------------------------------------
    def test_notify_is_quiet_when_telegram_is_disabled(self) -> None:
        TELEGRAM = TelegramConfig("", "12345")

        with patch("app.runtime_helpers.send_message_result") as SEND:
            with patch("app.runtime_helpers.log_line") as LOG_LINE:
                SEND.return_value = SimpleNamespace(
                    success=False,
                    disabled=True,
                    failure_detail="Telegram bot token is not configured.",
                )
                notify(TELEGRAM, "hello")

        LOG_LINE.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms stop notifications use the standard stopped template.
# --------------------------------------------------------------------------
    def test_notify_container_stopped_uses_standard_template(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")

        with patch("app.main.notify") as NOTIFY:
            notify_container_stopped(
                TELEGRAM,
                "alice@example.com",
                "Worker process exited.",
            )

        NOTIFY.assert_called_once()
        self.assertIn("🛑 PCD Drive - Container stopped", NOTIFY.call_args[0][1])
        self.assertIn("Worker process exited.", NOTIFY.call_args[0][1])
        self.assertIsNone(NOTIFY.call_args[0][2])

# --------------------------------------------------------------------------
# This test confirms attempt_auth success resets auth flags and notifies.
# --------------------------------------------------------------------------
    def test_attempt_auth_success_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", True, True, "prompt2")
            CLIENT = Mock()
            CLIENT.complete_authentication.return_value = (True, "ok")
            LOG_FILE = Path(TMPDIR) / "worker.log"
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="password",
                worker_log_path=LOG_FILE,
            )

            with patch("app.main.now_iso", return_value="2026-03-10T10:00:00+00:00"):
                with patch("app.main.notify") as NOTIFY:
                    with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                        with patch("app.main.log_line") as LOG_LINE:
                            RESULT = attempt_auth(
                                CLIENT,
                                AUTH_STATE,
                                AUTH_STATE_PATH,
                                TELEGRAM,
                                "alice",
                                "alice@example.com",
                                " 123456 ",
                            )

            self.assertTrue(RESULT.is_authenticated)
            self.assertEqual(RESULT.operator_detail, "ok")
            self.assertEqual(RESULT.reason_code, "authenticated")
            self.assertEqual(RESULT.auth_state.last_auth_utc, "2026-03-10T10:00:00+00:00")
            self.assertFalse(RESULT.auth_state.auth_pending)
            self.assertFalse(RESULT.auth_state.reauth_pending)
            CLIENT.complete_authentication.assert_called_once_with("123456")
            SAVE_CREDENTIALS.assert_called_once_with(
                "pyiclodoc-drive",
                "alice",
                "alice@example.com",
                "password",
            )
            self.assertIn("Authentication complete", NOTIFY.call_args[0][1])
            self.assertIn("🔒 PCD Drive - Authentication complete", NOTIFY.call_args[0][1])
            DEBUG_LINES = [
                CALL.args[2]
                for CALL in LOG_LINE.call_args_list
                if CALL.args[1] == "debug"
            ]
            self.assertTrue(
                any("Authentication attempt started:" in LINE for LINE in DEBUG_LINES)
            )
            self.assertTrue(
                any("reason=authenticated" in LINE for LINE in DEBUG_LINES)
            )
            self.assertTrue(any("code_present=True" in LINE for LINE in DEBUG_LINES))
            self.assertFalse(any("123456" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms successful authentication fails closed when auth-state
# persistence does not succeed.
# --------------------------------------------------------------------------
    def test_attempt_auth_success_path_fails_closed_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", True, True, "prompt2")
            CLIENT = Mock()
            CLIENT.complete_authentication.return_value = (True, "ok")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="password",
            )

            with patch("app.main.now_iso", return_value="2026-03-10T10:00:00+00:00"):
                with patch("app.main.notify") as NOTIFY:
                    with patch("app.main.save_auth_state", return_value=False):
                        with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                            RESULT = attempt_auth(
                                CLIENT,
                                AUTH_STATE,
                                AUTH_STATE_PATH,
                                TELEGRAM,
                                "alice",
                                "alice@example.com",
                                " 123456 ",
                            )

            self.assertFalse(RESULT.is_authenticated)
            self.assertEqual(RESULT.auth_state, AUTH_STATE)
            self.assertEqual(
                RESULT.reason_code,
                "state_save_failed_after_success",
            )
            self.assertEqual(
                RESULT.operator_detail,
                "Authentication succeeded but auth state could not be saved.",
            )
            SAVE_CREDENTIALS.assert_not_called()
            self.assertIn("Authentication failed", NOTIFY.call_args[0][1])
            self.assertIn("could not be saved", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms a failed auth-state save leaves the previously persisted
# auth state in place for the next startup.
# --------------------------------------------------------------------------
    def test_attempt_auth_failed_save_preserves_persisted_state_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            PERSISTED_STATE = AuthState(
                "2026-03-01T00:00:00+00:00",
                True,
                False,
                "alert5",
            )
            CLIENT = Mock()
            CLIENT.complete_authentication.return_value = (True, "ok")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="password",
            )

            self.assertTrue(save_auth_state(AUTH_STATE_PATH, PERSISTED_STATE))

            with patch("app.main.now_iso", return_value="2026-03-10T10:00:00+00:00"):
                with patch("app.main.notify"):
                    with patch("app.main.save_auth_state", return_value=False):
                        RESULT = attempt_auth(
                            CLIENT,
                            PERSISTED_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            "alice@example.com",
                            " 123456 ",
                        )

            RELOADED_STATE = load_auth_state(AUTH_STATE_PATH)
            self.assertEqual(RESULT.auth_state, PERSISTED_STATE)
            self.assertFalse(RESULT.is_authenticated)
            self.assertEqual(
                RESULT.reason_code,
                "state_save_failed_after_success",
            )
            self.assertEqual(RELOADED_STATE, PERSISTED_STATE)

# --------------------------------------------------------------------------
# This test confirms attempt_auth MFA-required branch sets auth pending.
# --------------------------------------------------------------------------
    def test_attempt_auth_mfa_required_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Two-factor code is required")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="password",
            )

            with patch("app.main.notify") as NOTIFY:
                with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                    RESULT = attempt_auth(
                        CLIENT,
                        AUTH_STATE,
                        AUTH_STATE_PATH,
                        TELEGRAM,
                        "alice",
                        "alice@example.com",
                        "",
                    )

            self.assertFalse(RESULT.is_authenticated)
            self.assertIn("Two-factor code is required", RESULT.operator_detail)
            self.assertEqual(RESULT.reason_code, "mfa_required")
            self.assertTrue(RESULT.auth_state.auth_pending)
            self.assertFalse(RESULT.auth_state.reauth_pending)
            CLIENT.start_authentication.assert_called_once()
            SAVE_CREDENTIALS.assert_not_called()
            self.assertIn("Authentication required", NOTIFY.call_args[0][1])
            self.assertIn('Send "alice auth 123456"', NOTIFY.call_args[0][1])
            self.assertIn('Or "alice reauth 123456"', NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms MFA-required authentication fails closed when auth-state
# persistence does not succeed.
# --------------------------------------------------------------------------
    def test_attempt_auth_mfa_required_path_fails_closed_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Two-factor code is required")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="password",
            )

            with patch("app.main.notify") as NOTIFY:
                with patch("app.main.save_auth_state", return_value=False):
                    with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                        RESULT = attempt_auth(
                            CLIENT,
                            AUTH_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            "alice@example.com",
                            "",
                        )

            self.assertFalse(RESULT.is_authenticated)
            self.assertEqual(RESULT.auth_state, AUTH_STATE)
            self.assertEqual(
                RESULT.reason_code,
                "state_save_failed_after_mfa_required",
            )
            self.assertEqual(
                RESULT.operator_detail,
                "Authentication requires MFA, but auth state could not be saved.",
            )
            SAVE_CREDENTIALS.assert_not_called()
            self.assertIn("Authentication failed", NOTIFY.call_args[0][1])
            self.assertNotIn("Authentication required", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms attempt_auth generic failure sends failure message.
# --------------------------------------------------------------------------
    def test_attempt_auth_failure_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Bad credentials")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="wrong-password",
            )

            with patch("app.main.notify") as NOTIFY:
                with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                    RESULT = attempt_auth(
                        CLIENT,
                        AUTH_STATE,
                        AUTH_STATE_PATH,
                        TELEGRAM,
                        "alice",
                        "alice@example.com",
                        "",
                    )

            self.assertFalse(RESULT.is_authenticated)
            self.assertEqual(RESULT.reason_code, "auth_failed")
            self.assertTrue(RESULT.auth_state.auth_pending)
            CLIENT.start_authentication.assert_called_once()
            SAVE_CREDENTIALS.assert_not_called()
            self.assertIn("Authentication failed", NOTIFY.call_args[0][1])
            self.assertIn("Bad credentials", NOTIFY.call_args[0][1])
            self.assertNotIn("Reason:", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms generic authentication failure also fails closed when
# auth-state persistence does not succeed.
# --------------------------------------------------------------------------
    def test_attempt_auth_failure_path_fails_closed_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Bad credentials")
            CLIENT.config = SimpleNamespace(
                keychain_service_name="pyiclodoc-drive",
                icloud_email="alice@example.com",
                icloud_password="wrong-password",
            )

            with patch("app.main.notify") as NOTIFY:
                with patch("app.main.save_auth_state", return_value=False):
                    with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                        RESULT = attempt_auth(
                            CLIENT,
                            AUTH_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            "alice@example.com",
                            "",
                        )

            self.assertFalse(RESULT.is_authenticated)
            self.assertEqual(RESULT.auth_state, AUTH_STATE)
            self.assertEqual(
                RESULT.reason_code,
                "state_save_failed_after_auth_failed",
            )
            self.assertEqual(
                RESULT.operator_detail,
                "Authentication failed, and auth state could not be saved.",
            )
            SAVE_CREDENTIALS.assert_not_called()
            self.assertIn("Authentication failed", NOTIFY.call_args[0][1])
            self.assertIn("could not be saved", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms failed startup auth does not overwrite stored credentials.
# --------------------------------------------------------------------------
    def test_main_does_not_save_unverified_env_credentials_on_failed_startup_auth(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            CONFIG = AppConfig(
                **(
                    CONFIG.__dict__
                    | {
                        "run_once": True,
                        "icloud_email": "env@example.com",
                        "icloud_password": "wrong-password",
                    }
                )
            )
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch(
                        "app.main.load_credentials",
                        return_value=("stored@example.com", "stored-password"),
                    ):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=False,
                                                reason_code="auth_failed",
                                                operator_detail="Bad credentials",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.wait_for_one_shot_auth",
                                                return_value=WorkerAuthState(
                                                    auth_state=STATE,
                                                    is_authenticated=False,
                                                ),
                                            ):
                                                RESULT = __import__(
                                                    "app.main", fromlist=["main"]
                                                ).main()

            self.assertEqual(RESULT, 2)
            SAVE_CREDENTIALS.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms verified env credentials are persisted after startup auth.
# --------------------------------------------------------------------------
    def test_main_saves_verified_env_credentials_after_successful_startup_auth(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            CONFIG = AppConfig(
                **(
                    CONFIG.__dict__
                    | {
                        "run_once": True,
                        "icloud_email": "env@example.com",
                        "icloud_password": "verified-password",
                    }
                )
            )
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch(
                        "app.main.load_credentials",
                        return_value=("stored@example.com", "stored-password"),
                    ):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials") as SAVE_CREDENTIALS:
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            side_effect=lambda *_ARGS, **_KWARGS: (
                                                SAVE_CREDENTIALS(
                                                    "pyiclodoc-drive",
                                                    "alice",
                                                    "env@example.com",
                                                    "verified-password",
                                                ),
                                                AuthAttemptResult(
                                                    auth_state=STATE,
                                                    is_authenticated=True,
                                                    reason_code="authenticated",
                                                    operator_detail="ok",
                                                ),
                                            )[1],
                                        ):
                                            with patch("app.worker_runtime.run_one_shot_worker") as RUN_ONE_SHOT:
                                                RUN_ONE_SHOT.return_value = SimpleNamespace(
                                                    exit_code=0,
                                                    stop_status="Run completed and container exited.",
                                                )
                                                RESULT = __import__(
                                                    "app.main", fromlist=["main"]
                                                ).main()

            self.assertEqual(RESULT, 0)
            SAVE_CREDENTIALS.assert_called_once_with(
                "pyiclodoc-drive",
                "alice",
                "env@example.com",
                "verified-password",
            )

# --------------------------------------------------------------------------
# This test confirms a done marker short-circuits safety-net checks.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_returns_true_when_done_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CONFIG.safety_net_done_path.write_text("ok\n", encoding="utf-8")

            with patch("app.main.run_first_time_safety_net") as RUN_NET:
                with patch("app.main.log_line") as LOG_LINE:
                    RESULT = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertTrue(RESULT)
            RUN_NET.assert_not_called()
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(any("Safety net check started:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("reason=done_marker_exists" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms passing safety-net creates done marker and unblocks.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_pass_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            BLOCKED = CONFIG.safety_net_blocked_path
            BLOCKED.write_text("blocked\n", encoding="utf-8")
            RESULT = SimpleNamespace(
                should_block=False,
                mismatched_samples=[],
                expected_uid=1000,
                expected_gid=1000,
            )

            with patch("app.main.run_first_time_safety_net", return_value=RESULT):
                with patch("app.main.log_line") as LOG_LINE:
                    RETURNED = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertTrue(RETURNED)
            self.assertTrue(CONFIG.safety_net_done_path.exists())
            self.assertFalse(BLOCKED.exists())
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(any("Safety net scan result:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Safety net stale blocked marker removed." in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Safety net done marker written:" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms blocked safety-net sends notification once.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_block_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            RESULT = SimpleNamespace(
                should_block=True,
                mismatched_samples=["/output/file1"],
                expected_uid=1000,
                expected_gid=1000,
            )

            with patch("app.main.run_first_time_safety_net", return_value=RESULT):
                with patch("app.main.notify") as NOTIFY:
                    with patch("app.main.log_line") as LOG_LINE:
                        RETURNED = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertFalse(RETURNED)
            self.assertTrue(CONFIG.safety_net_blocked_path.exists())
            self.assertIn("Safety net blocked", NOTIFY.call_args[0][1])
            self.assertIn(
                "Expected uid 1000, gid 1000",
                NOTIFY.call_args[0][1],
            )
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(any("Safety net scan result:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Safety net blocked marker written:" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms startup cutover polling uses short polling so worker
# startup does not block on long polling.
# --------------------------------------------------------------------------
    def test_poll_command_batch_uses_short_poll_for_startup_cutover(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")

        with patch("app.main.fetch_updates", return_value=[]) as FETCH_UPDATES:
            with patch("app.main.parse_command", return_value=None):
                RESULT = poll_command_batch(TELEGRAM, "alice", -1)

        self.assertEqual(RESULT.commands, [])
        self.assertIsNone(RESULT.next_update_offset)
        FETCH_UPDATES.assert_called_once_with(
            TELEGRAM,
            -1,
            TIMEOUT=0,
            LOG_LINE_FN=ANY,
            LOG_FILE=None,
        )

# --------------------------------------------------------------------------
# This test confirms run_backup sends start/end notifications and logs.
# --------------------------------------------------------------------------
    def test_run_backup_persists_manifest_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=2,
                transferred_bytes=2097152,
                deleted_files=0,
                deleted_directories=0,
                delete_errors=0,
                skipped_files=1,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/b": {"etag": "2"}})) as SYNC:
                    with patch("app.main.save_manifest") as SAVE_MANIFEST:
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line") as LOG_LINE:
                                RESULT = run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            SAVE_MANIFEST.assert_called_once()
            SYNC.assert_called_once_with(
                CLIENT,
                CONFIG.output_dir,
                {"/a": {"etag": "1"}},
                CONFIG.sync_workers,
                LOG_FILE,
                BACKUP_DELETE_REMOVED=CONFIG.backup_delete_removed,
            )
            self.assertEqual(NOTIFY.call_count, 2)
            self.assertGreaterEqual(LOG_LINE.call_count, 1)
            self.assertEqual(LOG_LINE.call_args_list[-1].args[1], "info")
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(any("Build detail:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Effective backup settings detail:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Backup run started:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Loaded manifest entries:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Sync summary detail:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Manifest save detail:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(any("Backup completion detail:" in LINE for LINE in DEBUG_LINES))
            self.assertTrue(
                any(
                    "transfer_errors=0, delete_errors=0, total_errors=0" in LINE
                    for LINE in DEBUG_LINES
                )
            )
            self.assertIn("⬇️ PCD Drive - Backup started", NOTIFY.call_args_list[0].args[1])
            self.assertIn("Files downloading for Apple ID alice@example.com.", NOTIFY.call_args_list[0].args[1])
            self.assertIn("Scheduled every 60 minutes.", NOTIFY.call_args_list[0].args[1])
            self.assertNotIn("Mode:", NOTIFY.call_args_list[0].args[1])
            self.assertNotIn("Trigger:", NOTIFY.call_args_list[0].args[1])
            self.assertIn("📦 PCD Drive - Backup complete", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Backup finished for Apple ID alice@example.com.", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Transferred: 2/3", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Deleted: 0 files, 0 directories", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Skipped: 1", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Errors: 0", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Duration:", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Average speed:", NOTIFY.call_args_list[1].args[1])
            self.assertNotIn(
                "Status: Failure looks like a dead iCloud session",
                NOTIFY.call_args_list[1].args[1],
            )
            self.assertTrue(RESULT.manifest_updated)
            self.assertEqual(
                LOG_LINE.call_args_list[-1].args[2],
                "Backup complete. Transferred 2/3, skipped 1, errors 0.",
            )

# --------------------------------------------------------------------------
# This test confirms a session-invalid sync summary adds a status line to
# the backup completion message without affecting manifest handling.
# --------------------------------------------------------------------------
    def test_run_backup_reports_session_invalid_status_line(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=1,
                transferred_files=0,
                transferred_bytes=0,
                deleted_files=0,
                deleted_directories=0,
                delete_errors=0,
                skipped_files=0,
                error_files=1,
                session_invalid=True,
            )

            with patch("app.main.load_manifest", return_value={}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {})):
                    with patch("app.main.save_manifest"):
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line"):
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            self.assertIn(
                "Status: Failure looks like a dead iCloud session, "
                "reauthentication may be required",
                NOTIFY.call_args_list[1].args[1],
            )

# --------------------------------------------------------------------------
# This test confirms backup completion omits speed when no files transfer.
# --------------------------------------------------------------------------
    def test_run_backup_omits_average_speed_when_nothing_downloaded(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=0,
                transferred_bytes=0,
                deleted_files=0,
                deleted_directories=0,
                delete_errors=0,
                skipped_files=3,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/a": {"etag": "1"}})):
                    with patch("app.main.save_manifest"):
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line"):
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            self.assertEqual(NOTIFY.call_count, 2)
            self.assertIn("Deleted: 0 files, 0 directories", NOTIFY.call_args_list[1].args[1])
            self.assertNotIn("Average speed:", NOTIFY.call_args_list[1].args[1])

# --------------------------------------------------------------------------
# This test confirms incomplete traversal suppresses manifest persistence and
# surfaces the partial-run outcome in logs and Telegram output.
# --------------------------------------------------------------------------
    def test_run_backup_skips_manifest_save_when_traversal_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=1,
                transferred_bytes=1024,
                deleted_files=0,
                deleted_directories=0,
                delete_errors=0,
                skipped_files=1,
                error_files=1,
                traversal_complete=False,
                traversal_hard_failures=2,
                delete_phase_skipped=True,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/b": {"etag": "2"}})):
                    with patch("app.main.save_manifest") as SAVE_MANIFEST:
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line") as LOG_LINE:
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            SAVE_MANIFEST.assert_not_called()
            self.assertEqual(NOTIFY.call_count, 2)
            self.assertIn("Status: Partial run due to incomplete traversal", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Traversal hard failures: 2", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Manifest: Not updated", NOTIFY.call_args_list[1].args[1])
            self.assertIn(
                "Delete removed: Skipped because traversal was incomplete",
                NOTIFY.call_args_list[1].args[1],
            )
            self.assertIn("Deleted: 0 files, 0 directories", NOTIFY.call_args_list[1].args[1])
            self.assertTrue(
                any(
                    CALL.args[1] == "error"
                    and "Manifest save skipped because traversal was incomplete." in CALL.args[2]
                    for CALL in LOG_LINE.call_args_list
                )
            )
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(
                any("reason=traversal_incomplete" in LINE for LINE in DEBUG_LINES)
            )
            self.assertEqual(
                LOG_LINE.call_args_list[-1].args[2],
                "Backup completed with incomplete traversal. Transferred 1/3, skipped 1, errors 1.",
            )

# --------------------------------------------------------------------------
# This test confirms manifest save failure stays visible in backup result,
# completion output, and error logging.
# --------------------------------------------------------------------------
    def test_run_backup_reports_manifest_save_failure_after_successful_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=2,
                transferred_bytes=2048,
                deleted_files=0,
                deleted_directories=0,
                delete_errors=0,
                skipped_files=1,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/b": {"etag": "2"}})):
                    with patch("app.main.save_manifest", return_value=False) as SAVE_MANIFEST:
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line") as LOG_LINE:
                                RESULT = run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

        SAVE_MANIFEST.assert_called_once()
        self.assertFalse(RESULT.manifest_updated)
        self.assertIn("Manifest: Save failed after traversal completed", NOTIFY.call_args_list[1].args[1])
        self.assertEqual(
            LOG_LINE.call_args_list[-1].args[2],
            "Backup completed but manifest save failed. Transferred 2/3, skipped 1, errors 0.",
        )
        self.assertTrue(
            any(
                CALL.args[1] == "error"
                and "Manifest save failed after traversal completed." in CALL.args[2]
                for CALL in LOG_LINE.call_args_list
            )
        )

# --------------------------------------------------------------------------
# This test confirms backup completion surfaces deleted file and directory
# counts in the Telegram summary when paths are removed locally.
# --------------------------------------------------------------------------
    def test_run_backup_reports_deleted_paths_in_completion_message(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=2,
                transferred_bytes=2048,
                deleted_files=3,
                deleted_directories=1,
                delete_errors=0,
                skipped_files=1,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/a": {"etag": "1"}})):
                    with patch("app.main.save_manifest"):
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line"):
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            self.assertEqual(NOTIFY.call_count, 2)
            self.assertIn("Transferred: 2/3", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Deleted: 3 files, 1 directory", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Skipped: 1", NOTIFY.call_args_list[1].args[1])

# --------------------------------------------------------------------------
# This test confirms backup completion includes delete-phase failures in the
# total error count and surfaces the delete-error detail explicitly.
# --------------------------------------------------------------------------
    def test_run_backup_reports_delete_phase_errors_in_completion_message(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SyncResult(
                total_files=3,
                transferred_files=1,
                transferred_bytes=1024,
                deleted_files=2,
                deleted_directories=0,
                delete_errors=3,
                skipped_files=1,
                error_files=1,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/a": {"etag": "1"}})):
                    with patch("app.main.save_manifest"):
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line") as LOG_LINE:
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

            self.assertIn("Errors: 4", NOTIFY.call_args_list[1].args[1])
            self.assertIn("Delete errors: 3", NOTIFY.call_args_list[1].args[1])
            DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertTrue(
                any(
                    "transfer_errors=1, delete_errors=3, total_errors=4" in LINE
                    for LINE in DEBUG_LINES
                )
            )
            self.assertEqual(
                LOG_LINE.call_args_list[-1].args[2],
                "Backup complete. Transferred 1/3, skipped 1, errors 4.",
            )

# --------------------------------------------------------------------------
# This test confirms malformed sync summary shapes fail loudly instead of
# being silently coerced through fallback field defaults.
# --------------------------------------------------------------------------
    def test_run_backup_raises_when_sync_summary_shape_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "pyiclodoc-drive-worker.log"
            CLIENT = Mock()
            SUMMARY = SimpleNamespace(
                total_files=3,
                transferred_files=1,
                transferred_bytes=1024,
                deleted_files=0,
                deleted_directories=0,
                skipped_files=1,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/a": {"etag": "1"}})):
                    with patch("app.main.save_manifest"):
                        with patch("app.main.notify"):
                            with patch("app.main.log_line"):
                                with self.assertRaises(AttributeError):
                                    run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE, "scheduled")

# --------------------------------------------------------------------------
# This test confirms two-day reauth reminder sends a required action prompt.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_sends_reauth_required_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("2026-03-01T00:00:00+00:00", False, False, "alert5")

            with patch("app.auth_runtime.reauth_days_left", return_value=2):
                with patch("app.main.notify") as NOTIFY:
                    NEW_STATE = process_reauth_reminders(
                        AUTH_STATE,
                        AUTH_STATE_PATH,
                        TELEGRAM,
                        "alice",
                        30,
                    )

            self.assertEqual(NEW_STATE.reminder_stage, "prompt2")
            self.assertTrue(NEW_STATE.reauth_pending)
            self.assertIn("Reauthentication required", NOTIFY.call_args[0][1])
            self.assertIn("Reauthentication is due within two days.", NOTIFY.call_args[0][1])
            self.assertIn('Send "alice auth 123456"', NOTIFY.call_args[0][1])
            self.assertIn('Or "alice reauth 123456"', NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms two-day reminders are suppressed when auth-state
# persistence does not succeed.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_skips_two_day_notify_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("2026-03-01T00:00:00+00:00", False, False, "alert5")

            with patch("app.auth_runtime.reauth_days_left", return_value=2):
                with patch("app.main.save_auth_state", return_value=False):
                    with patch("app.main.notify") as NOTIFY:
                        NEW_STATE = process_reauth_reminders(
                            AUTH_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            30,
                        )

        self.assertEqual(NEW_STATE, AUTH_STATE)
        NOTIFY.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms five-day reminder sends a reauth reminder message.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_sends_five_day_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("2026-03-01T00:00:00+00:00", False, False, "none")

            with patch("app.auth_runtime.reauth_days_left", return_value=5):
                with patch("app.main.notify") as NOTIFY:
                    NEW_STATE = process_reauth_reminders(
                        AUTH_STATE,
                        AUTH_STATE_PATH,
                        TELEGRAM,
                        "alice",
                        30,
                    )

            self.assertEqual(NEW_STATE.reminder_stage, "alert5")
            self.assertFalse(NEW_STATE.reauth_pending)
            self.assertIn("Reauth reminder", NOTIFY.call_args[0][1])
            self.assertIn("Reauthentication will be required within five days.", NOTIFY.call_args[0][1])
            self.assertIn('Send "alice auth 123456"', NOTIFY.call_args[0][1])
            self.assertIn('Or "alice reauth 123456"', NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms five-day reminders are suppressed when auth-state
# persistence does not succeed.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_skips_five_day_notify_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("2026-03-01T00:00:00+00:00", False, False, "none")

            with patch("app.auth_runtime.reauth_days_left", return_value=5):
                with patch("app.main.save_auth_state", return_value=False):
                    with patch("app.main.notify") as NOTIFY:
                        NEW_STATE = process_reauth_reminders(
                            AUTH_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            30,
                        )

        self.assertEqual(NEW_STATE, AUTH_STATE)
        NOTIFY.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms steady-state reminder processing does not rewrite auth
# state when no transition is required.
# --------------------------------------------------------------------------
    def test_process_reauth_reminders_does_not_save_when_state_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "pyiclodoc-drive-auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("2026-03-01T00:00:00+00:00", False, False, "none")

            with patch("app.auth_runtime.reauth_days_left", return_value=8):
                with patch("app.main.save_auth_state") as SAVE:
                    with patch("app.main.notify") as NOTIFY:
                        NEW_STATE = process_reauth_reminders(
                            AUTH_STATE,
                            AUTH_STATE_PATH,
                            TELEGRAM,
                            "alice",
                            30,
                        )

            self.assertEqual(NEW_STATE, AUTH_STATE)
            SAVE.assert_not_called()
            NOTIFY.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms handle_command backup path requests a backup.
# --------------------------------------------------------------------------
    def test_handle_command_backup_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.notify") as NOTIFY:
                RESULT = handle_command(
                    "backup",
                    "",
                    CONFIG,
                    Mock(),
                    AUTH_STATE,
                    True,
                    TELEGRAM,
                )

            self.assertEqual(RESULT.auth_state, AUTH_STATE)
            self.assertTrue(RESULT.is_authenticated)
            self.assertTrue(RESULT.backup_requested)
            self.assertEqual(RESULT.reason_code, "backup_requested")
            self.assertIn("Backup requested", NOTIFY.call_args[0][1])
            self.assertIn("Manual backup requested for Apple ID alice@example.com.", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms handle_command auth prompt path starts a fresh challenge.
# --------------------------------------------------------------------------
    def test_handle_command_auth_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            EXPECTED_STATE = AuthState(
                "1970-01-01T00:00:00+00:00",
                True,
                False,
                "none",
            )

            with patch(
                "app.main.attempt_auth",
                return_value=AuthAttemptResult(
                    auth_state=EXPECTED_STATE,
                    is_authenticated=False,
                    reason_code="mfa_required",
                    operator_detail="mfa",
                ),
            ) as ATTEMPT:
                with patch("app.main.log_line") as LOG:
                    RESULT = handle_command(
                        "auth",
                        "",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            self.assertTrue(RESULT.auth_state.auth_pending)
            self.assertFalse(RESULT.is_authenticated)
            self.assertFalse(RESULT.backup_requested)
            self.assertEqual(RESULT.reason_code, "mfa_required")
            ATTEMPT.assert_called_once()
            self.assertEqual(ATTEMPT.call_args[0][1], AUTH_STATE)
            self.assertEqual(ATTEMPT.call_args[0][6], "")
            LOG_MESSAGES = [CALL.args[2] for CALL in LOG.call_args_list]
            self.assertTrue(
                any("command=auth, args_present=False" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertTrue(
                any("requesting a fresh Apple challenge" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertTrue(
                any("reason_code=mfa_required" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )

# --------------------------------------------------------------------------
# This test confirms handle_command reauth prompt path starts a fresh
# challenge while preserving reauth state.
# --------------------------------------------------------------------------
    def test_handle_command_reauth_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            EXPECTED_STATE = AuthState(
                "1970-01-01T00:00:00+00:00",
                True,
                True,
                "none",
            )

            with patch(
                "app.main.attempt_auth",
                return_value=AuthAttemptResult(
                    auth_state=EXPECTED_STATE,
                    is_authenticated=False,
                    reason_code="mfa_required",
                    operator_detail="mfa",
                ),
            ) as ATTEMPT:
                with patch("app.main.log_line") as LOG:
                    RESULT = handle_command(
                        "reauth",
                        "",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            self.assertTrue(RESULT.auth_state.reauth_pending)
            self.assertFalse(RESULT.is_authenticated)
            self.assertFalse(RESULT.backup_requested)
            self.assertEqual(RESULT.reason_code, "mfa_required")
            ATTEMPT.assert_called_once()
            self.assertTrue(ATTEMPT.call_args[0][1].reauth_pending)
            self.assertEqual(ATTEMPT.call_args[0][6], "")
            LOG_MESSAGES = [CALL.args[2] for CALL in LOG.call_args_list]
            self.assertTrue(
                any("command=reauth, args_present=False" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertTrue(
                any("requesting a fresh Apple challenge" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertTrue(
                any("reauth_pending=True" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )

# --------------------------------------------------------------------------
# This test confirms handle_command auth flow delegates to attempt_auth.
# --------------------------------------------------------------------------
    def test_handle_command_auth_with_code_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            EXPECTED_STATE = AuthState("2026-03-09T12:00:00+00:00", False, False, "none")

            with patch(
                "app.main.attempt_auth",
                return_value=AuthAttemptResult(
                    auth_state=EXPECTED_STATE,
                    is_authenticated=True,
                    reason_code="authenticated",
                    operator_detail="ok",
                ),
            ) as ATTEMPT:
                with patch("app.main.log_line") as LOG:
                    RESULT = handle_command(
                        "auth",
                        "123456",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            ATTEMPT.assert_called_once()
            LOG_MESSAGES = [CALL.args[2] for CALL in LOG.call_args_list]
            self.assertTrue(
                any("command=auth, args_present=True" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertTrue(
                any("submitting redacted code to Apple" in MESSAGE for MESSAGE in LOG_MESSAGES)
            )
            self.assertFalse(any("123456" in MESSAGE for MESSAGE in LOG_MESSAGES))
            self.assertEqual(RESULT.auth_state, EXPECTED_STATE)
            self.assertTrue(RESULT.is_authenticated)
            self.assertFalse(RESULT.backup_requested)
            self.assertEqual(RESULT.reason_code, "authenticated")

# --------------------------------------------------------------------------
# This test confirms handle_command reauth with a code uses reauth state
# rather than the plain auth path.
# --------------------------------------------------------------------------
    def test_handle_command_reauth_with_code_uses_reauth_state(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            EXPECTED_STATE = AuthState("2026-03-09T12:00:00+00:00", False, False, "none")

            with patch(
                "app.main.attempt_auth",
                return_value=AuthAttemptResult(
                    auth_state=EXPECTED_STATE,
                    is_authenticated=True,
                    reason_code="authenticated",
                    operator_detail="ok",
                ),
            ) as ATTEMPT:
                with patch("app.main.log_line") as LOG:
                    RESULT = handle_command(
                        "reauth",
                        "123456",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        True,
                        TELEGRAM,
                    )

        self.assertTrue(ATTEMPT.call_args[0][1].reauth_pending)
        self.assertEqual(ATTEMPT.call_args[0][6], "123456")
        self.assertEqual(RESULT.auth_state, EXPECTED_STATE)
        self.assertTrue(RESULT.is_authenticated)
        self.assertFalse(RESULT.backup_requested)
        self.assertEqual(RESULT.reason_code, "authenticated")
        LOG_MESSAGES = [CALL.args[2] for CALL in LOG.call_args_list]
        self.assertTrue(
            any("command=reauth, args_present=True" in MESSAGE for MESSAGE in LOG_MESSAGES)
        )
        self.assertTrue(
            any("Reauth code command received" in MESSAGE for MESSAGE in LOG_MESSAGES)
        )
        self.assertFalse(any("123456" in MESSAGE for MESSAGE in LOG_MESSAGES))


# ------------------------------------------------------------------------------
# These tests verify "main()" startup and loop control-flow branches.
# ------------------------------------------------------------------------------
class TestMainEntrypoint(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms startup validation errors return non-zero status.
# --------------------------------------------------------------------------
    def test_main_returns_1_for_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=["bad config"]):
                            with patch("app.main.log_line") as LOG_LINE:
                                RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 1)
            LOG_LINE.assert_called()

# --------------------------------------------------------------------------
# This test confirms one-shot mode returns 2 when auth is incomplete.
# --------------------------------------------------------------------------
    def test_main_run_once_returns_2_when_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=False,
                                                reason_code="auth_failed",
                                                operator_detail="fail",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.wait_for_one_shot_auth",
                                                return_value=WorkerAuthState(
                                                    auth_state=STATE,
                                                    is_authenticated=False,
                                                ),
                                            ):
                                                with patch("app.main.notify") as NOTIFY:
                                                    RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 2)
            MESSAGES = [CALL.args[1] for CALL in NOTIFY.call_args_list]
            self.assertTrue(any("Backup skipped" in MESSAGE for MESSAGE in MESSAGES))
            self.assertTrue(any("Authentication incomplete" in MESSAGE for MESSAGE in MESSAGES))
            self.assertTrue(any("The wait window is 15 mins." in MESSAGE for MESSAGE in MESSAGES))
            self.assertFalse(any("Reason:" in MESSAGE for MESSAGE in MESSAGES))

# --------------------------------------------------------------------------
# This test confirms one-shot mode returns 3 when reauth is pending.
# --------------------------------------------------------------------------
    def test_main_run_once_returns_3_when_reauth_pending(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, True, "prompt2")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.wait_for_one_shot_auth",
                                                return_value=WorkerAuthState(
                                                    auth_state=STATE,
                                                    is_authenticated=True,
                                                ),
                                            ):
                                                RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 3)

# --------------------------------------------------------------------------
# This test confirms one-shot mode returns 4 when safety-net blocks.
# --------------------------------------------------------------------------
    def test_main_run_once_returns_4_when_safety_net_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.run_one_shot_worker",
                                                return_value=SimpleNamespace(
                                                    exit_code=4,
                                                    stop_status=(
                                                        "One-shot backup blocked by safety "
                                                        "net."
                                                    ),
                                                ),
                                            ):
                                                RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 4)

# --------------------------------------------------------------------------
# This test confirms one-shot mode returns 0 on successful backup run.
# --------------------------------------------------------------------------
    def test_main_run_once_returns_0_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch("app.worker_runtime.run_one_shot_worker") as RUN_ONE_SHOT:
                                                RUN_ONE_SHOT.return_value = SimpleNamespace(
                                                    exit_code=0,
                                                    stop_status="Run completed and container exited.",
                                                )
                                                with patch("app.main.run_backup") as RUN_BACKUP:
                                                    with patch("app.main.notify") as NOTIFY:
                                                        RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 0)
            RUN_ONE_SHOT.assert_called_once()
            RUN_BACKUP.assert_not_called()
            self.assertGreaterEqual(NOTIFY.call_count, 2)
            self.assertIn("🟢 PCD Drive - Container started", NOTIFY.call_args_list[0].args[1])
            self.assertIn("Worker started for Apple ID alice@example.com.", NOTIFY.call_args_list[0].args[1])
            self.assertIn("🛑 PCD Drive - Container stopped", NOTIFY.call_args_list[-1].args[1])
            self.assertIn("Run completed and container exited.", NOTIFY.call_args_list[-1].args[1])

# --------------------------------------------------------------------------
# This test confirms one-shot mode runs backup after auth wait succeeds.
# --------------------------------------------------------------------------
    def test_main_run_once_runs_backup_after_waited_auth(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            INITIAL_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            READY_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=INITIAL_STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=INITIAL_STATE,
                                                is_authenticated=False,
                                                reason_code="mfa_required",
                                                operator_detail="mfa",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.wait_for_one_shot_auth",
                                                return_value=WorkerAuthState(
                                                    auth_state=READY_STATE,
                                                    is_authenticated=True,
                                                ),
                                            ) as WAIT_AUTH:
                                                with patch("app.main.run_backup") as RUN_BACKUP:
                                                    with patch(
                                                        "app.worker_runtime.run_one_shot_worker",
                                                        wraps=__import__(
                                                            "app.worker_runtime",
                                                            fromlist=["run_one_shot_worker"],
                                                        ).run_one_shot_worker,
                                                    ):
                                                        RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 0)
            WAIT_AUTH.assert_called_once()
            RUN_BACKUP.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms startup emits auth-state debug diagnostics.
# --------------------------------------------------------------------------
    def test_main_logs_startup_auth_state_debug_line(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", True, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=False,
                                                reason_code="mfa_required",
                                                operator_detail="mfa",
                                            ),
                                        ):
                                            with patch(
                                                "app.worker_runtime.wait_for_one_shot_auth",
                                                return_value=WorkerAuthState(
                                                    auth_state=STATE,
                                                    is_authenticated=False,
                                                ),
                                            ):
                                                with patch("app.main.notify"):
                                                    with patch("app.main.log_line") as LOG_LINE:
                                                        __import__("app.main", fromlist=["main"]).main()

            DEBUG_LINES = [CALL for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertGreaterEqual(len(DEBUG_LINES), 1)
            self.assertTrue(
                any("Auth state after startup attempt:" in CALL.args[2] for CALL in DEBUG_LINES)
            )
            self.assertTrue(
                any("Worker bootstrap started:" in CALL.args[2] for CALL in DEBUG_LINES)
            )
            self.assertTrue(
                any("Stored credential lookup completed:" in CALL.args[2] for CALL in DEBUG_LINES)
            )
            self.assertTrue(
                any("Auth state loaded:" in CALL.args[2] for CALL in DEBUG_LINES)
            )
            self.assertFalse(any("alice_password_secret" in CALL.args[2] for CALL in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms main stops and joins the heartbeat updater before exit.
# --------------------------------------------------------------------------
    def test_main_stops_heartbeat_updater_before_exit(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(
                **(build_config_for_runtime(TMPDIR).__dict__ | {"run_once": True})
            )
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            HEARTBEAT_UPDATER = Mock()

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch(
                                "app.main.start_heartbeat_updater",
                                return_value=HEARTBEAT_UPDATER,
                            ):
                                with patch("app.main.save_credentials"):
                                    with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                        with patch("app.main.load_auth_state", return_value=STATE):
                                            with patch(
                                                "app.main.attempt_auth",
                                                return_value=AuthAttemptResult(
                                                    auth_state=STATE,
                                                    is_authenticated=False,
                                                    reason_code="auth_failed",
                                                    operator_detail="fail",
                                                ),
                                            ):
                                                with patch(
                                                    "app.worker_runtime.wait_for_one_shot_auth",
                                                    return_value=WorkerAuthState(
                                                        auth_state=STATE,
                                                        is_authenticated=False,
                                                    ),
                                                ):
                                                    with patch("app.main.notify"):
                                                        RESULT = __import__(
                                                            "app.main",
                                                            fromlist=["main"],
                                                        ).main()

            self.assertEqual(RESULT, 2)
            HEARTBEAT_UPDATER.stop.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms loop sleeps and continues when not due and no request.
# --------------------------------------------------------------------------
    def test_main_loop_sleeps_when_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"schedule_mode": "daily"}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            HEARTBEAT_UPDATER = Mock()
            HEARTBEAT_UPDATER.get_liveness_failure.return_value = None

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.main.start_heartbeat_updater",
                                                return_value=HEARTBEAT_UPDATER,
                                            ):
                                                with patch("app.main.get_next_run_epoch", return_value=200):
                                                    with patch(
                                                        "app.worker_runtime.time.time",
                                                        side_effect=[100, 100, 100],
                                                    ):
                                                        with patch(
                                                            "app.main.process_reauth_reminders",
                                                            return_value=STATE,
                                                        ):
                                                            with patch(
                                                                "app.main.poll_command_batch",
                                                                return_value=SimpleNamespace(
                                                                    commands=[],
                                                                    next_update_offset=None,
                                                                ),
                                                            ):
                                                                with patch(
                                                                    "app.worker_runtime.time.sleep",
                                                                    side_effect=SystemExit,
                                                                ):
                                                                    with self.assertRaises(SystemExit):
                                                                        __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path skips when auth becomes incomplete.
# --------------------------------------------------------------------------
    def test_main_loop_skips_when_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            HEARTBEAT_UPDATER = Mock()
            HEARTBEAT_UPDATER.get_liveness_failure.return_value = None

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=False,
                                                reason_code="auth_failed",
                                                operator_detail="fail",
                                            ),
                                        ):
                                            with patch(
                                                "app.main.start_heartbeat_updater",
                                                return_value=HEARTBEAT_UPDATER,
                                            ):
                                                with patch("app.worker_runtime.time.time", side_effect=[100, 100, 100]):
                                                    with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                        with patch(
                                                            "app.main.poll_command_batch",
                                                            return_value=SimpleNamespace(
                                                                commands=[],
                                                                next_update_offset=None,
                                                            ),
                                                        ):
                                                            with patch("app.main.get_next_run_epoch", return_value=160):
                                                                with patch(
                                                                    "app.worker_runtime.time.sleep",
                                                                    side_effect=SystemExit,
                                                                ):
                                                                    with self.assertRaises(SystemExit):
                                                                        __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path skips when reauth is pending.
# --------------------------------------------------------------------------
    def test_main_loop_skips_when_reauth_pending(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, True, "prompt2")
            HEARTBEAT_UPDATER = Mock()
            HEARTBEAT_UPDATER.get_liveness_failure.return_value = None

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.main.start_heartbeat_updater",
                                                return_value=HEARTBEAT_UPDATER,
                                            ):
                                                with patch("app.worker_runtime.time.time", side_effect=[100, 100, 100]):
                                                    with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                        with patch(
                                                            "app.main.poll_command_batch",
                                                            return_value=SimpleNamespace(
                                                                commands=[],
                                                                next_update_offset=None,
                                                            ),
                                                        ):
                                                            with patch("app.main.get_next_run_epoch", return_value=160):
                                                                with patch(
                                                                    "app.worker_runtime.time.sleep",
                                                                    side_effect=SystemExit,
                                                                ):
                                                                    with self.assertRaises(SystemExit):
                                                                        __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path sleeps when safety-net blocks.
# --------------------------------------------------------------------------
    def test_main_loop_sleeps_when_safety_net_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            HEARTBEAT_UPDATER = Mock()
            HEARTBEAT_UPDATER.get_liveness_failure.return_value = None

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.main.start_heartbeat_updater",
                                                return_value=HEARTBEAT_UPDATER,
                                            ):
                                                with patch("app.worker_runtime.time.time", side_effect=[100, 100, 100]):
                                                    with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                        with patch(
                                                            "app.main.poll_command_batch",
                                                            return_value=SimpleNamespace(
                                                                commands=[],
                                                                next_update_offset=None,
                                                            ),
                                                        ):
                                                            with patch("app.main.get_next_run_epoch", return_value=160):
                                                                with patch("app.main.enforce_safety_net", return_value=False):
                                                                    with patch(
                                                                        "app.worker_runtime.time.sleep",
                                                                        side_effect=SystemExit,
                                                                    ):
                                                                        with self.assertRaises(SystemExit):
                                                                            __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path runs backup when all checks pass.
# --------------------------------------------------------------------------
    def test_main_loop_runs_backup_when_due_and_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            HEARTBEAT_UPDATER = Mock()
            HEARTBEAT_UPDATER.get_liveness_failure.return_value = None

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch(
                                            "app.main.attempt_auth",
                                            return_value=AuthAttemptResult(
                                                auth_state=STATE,
                                                is_authenticated=True,
                                                reason_code="authenticated",
                                                operator_detail="ok",
                                            ),
                                        ):
                                            with patch(
                                                "app.main.start_heartbeat_updater",
                                                return_value=HEARTBEAT_UPDATER,
                                            ):
                                                with patch("app.worker_runtime.time.time", side_effect=[100, 100, 100]):
                                                    with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                        with patch(
                                                            "app.main.poll_command_batch",
                                                            return_value=SimpleNamespace(
                                                                commands=[],
                                                                next_update_offset=None,
                                                            ),
                                                        ):
                                                            with patch("app.main.get_next_run_epoch", return_value=160):
                                                                with patch("app.main.enforce_safety_net", return_value=True):
                                                                    with patch("app.main.run_backup") as RUN_BACKUP:
                                                                        with patch(
                                                                            "app.worker_runtime.time.sleep",
                                                                            side_effect=SystemExit,
                                                                        ):
                                                                            with self.assertRaises(SystemExit):
                                                                                __import__("app.main", fromlist=["main"]).main()

            RUN_BACKUP.assert_called_once()


if __name__ == "__main__":
    unittest.main()
