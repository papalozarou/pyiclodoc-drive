# ------------------------------------------------------------------------------
# This test module verifies runtime helper behaviour in "app.main".
# ------------------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.config import AppConfig
from app.main import (
    attempt_auth,
    enforce_safety_net,
    get_monthly_weekday_day,
    get_next_run_epoch,
    handle_command,
    notify,
    parse_iso,
    process_commands,
    run_backup,
    start_heartbeat_updater,
    update_heartbeat,
    wait_for_one_shot_auth,
)
from app.state import AuthState
from app.telegram_bot import CommandEvent, TelegramConfig


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
        keychain_service_name="icloud-drive-backup",
        run_once=False,
        schedule_mode="interval",
        schedule_backup_time="02:00",
        schedule_weekdays="monday",
        schedule_monthly_week="first",
        schedule_interval_minutes=60,
        reauth_interval_days=30,
        output_dir=OUTPUT_DIR,
        config_dir=CONFIG_DIR,
        logs_dir=LOGS_DIR,
        manifest_path=CONFIG_DIR / "manifest.json",
        auth_state_path=CONFIG_DIR / "auth_state.json",
        heartbeat_path=LOGS_DIR / "heartbeat.txt",
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
            HEARTBEAT_PATH = Path(TMPDIR) / "logs" / "heartbeat.txt"
            update_heartbeat(HEARTBEAT_PATH)
            self.assertTrue(HEARTBEAT_PATH.exists())

# --------------------------------------------------------------------------
# This test confirms heartbeat updater starts a daemon thread and returns
# a stop event.
# --------------------------------------------------------------------------
    def test_start_heartbeat_updater_starts_daemon_thread(self) -> None:
        HEARTBEAT_PATH = Path("/tmp/heartbeat.txt")

        with patch("app.main.threading.Thread") as THREAD:
            THREAD_INSTANCE = Mock()
            THREAD.return_value = THREAD_INSTANCE

            STOP_EVENT = start_heartbeat_updater(HEARTBEAT_PATH)

        THREAD.assert_called_once()
        self.assertEqual(THREAD.call_args.kwargs.get("daemon"), True)
        THREAD_INSTANCE.start.assert_called_once()
        self.assertFalse(STOP_EVENT.is_set())

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait returns immediately when ready.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_returns_immediately_when_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            TELEGRAM = TelegramConfig("token", "12345")

            RESULT_STATE, RESULT_AUTH = wait_for_one_shot_auth(
                CONFIG,
                Mock(),
                STATE,
                True,
                TELEGRAM,
            )

        self.assertEqual(RESULT_STATE, STATE)
        self.assertTrue(RESULT_AUTH)

# --------------------------------------------------------------------------
# This test confirms notify delegates to send_message.
# --------------------------------------------------------------------------
    def test_notify_delegates_to_send_message(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")
        with patch("app.main.send_message") as SEND:
            notify(TELEGRAM, "hello")
        SEND.assert_called_once_with(TELEGRAM, "hello")

# --------------------------------------------------------------------------
# This test confirms attempt_auth success resets auth flags and notifies.
# --------------------------------------------------------------------------
    def test_attempt_auth_success_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", True, True, "prompt2")
            CLIENT = Mock()
            CLIENT.complete_authentication.return_value = (True, "ok")

            with patch("app.main.now_iso", return_value="2026-03-10T10:00:00+00:00"):
                with patch("app.main.notify") as NOTIFY:
                    NEW_STATE, IS_AUTHENTICATED, DETAILS = attempt_auth(
                        CLIENT,
                        AUTH_STATE,
                        AUTH_STATE_PATH,
                        TELEGRAM,
                        "alice",
                        " 123456 ",
                    )

            self.assertTrue(IS_AUTHENTICATED)
            self.assertEqual(DETAILS, "ok")
            self.assertEqual(NEW_STATE.last_auth_utc, "2026-03-10T10:00:00+00:00")
            self.assertFalse(NEW_STATE.auth_pending)
            self.assertFalse(NEW_STATE.reauth_pending)
            CLIENT.complete_authentication.assert_called_once_with("123456")
            NOTIFY.assert_called_once_with(TELEGRAM, "Authentication successful.")

# --------------------------------------------------------------------------
# This test confirms attempt_auth MFA-required branch sets auth pending.
# --------------------------------------------------------------------------
    def test_attempt_auth_mfa_required_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Two-factor code is required")

            with patch("app.main.notify") as NOTIFY:
                NEW_STATE, IS_AUTHENTICATED, DETAILS = attempt_auth(
                    CLIENT,
                    AUTH_STATE,
                    AUTH_STATE_PATH,
                    TELEGRAM,
                    "alice",
                    "",
                )

            self.assertFalse(IS_AUTHENTICATED)
            self.assertIn("Two-factor code is required", DETAILS)
            self.assertTrue(NEW_STATE.auth_pending)
            self.assertFalse(NEW_STATE.reauth_pending)
            CLIENT.start_authentication.assert_called_once()
            self.assertIn("MFA required", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms attempt_auth generic failure sends failure message.
# --------------------------------------------------------------------------
    def test_attempt_auth_failure_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            AUTH_STATE_PATH = Path(TMPDIR) / "auth_state.json"
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            CLIENT = Mock()
            CLIENT.start_authentication.return_value = (False, "Bad credentials")

            with patch("app.main.notify") as NOTIFY:
                NEW_STATE, IS_AUTHENTICATED, _ = attempt_auth(
                    CLIENT,
                    AUTH_STATE,
                    AUTH_STATE_PATH,
                    TELEGRAM,
                    "alice",
                    "",
                )

            self.assertFalse(IS_AUTHENTICATED)
            self.assertTrue(NEW_STATE.auth_pending)
            CLIENT.start_authentication.assert_called_once()
            self.assertIn("Authentication failed", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms a done marker short-circuits safety-net checks.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_returns_true_when_done_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "worker.log"
            (CONFIG.config_dir / "safety_net_done.flag").write_text("ok\n", encoding="utf-8")

            with patch("app.main.run_first_time_safety_net") as RUN_NET:
                RESULT = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertTrue(RESULT)
            RUN_NET.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms passing safety-net creates done marker and unblocks.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_pass_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "worker.log"
            BLOCKED = CONFIG.config_dir / "safety_net_blocked.flag"
            BLOCKED.write_text("blocked\n", encoding="utf-8")
            RESULT = SimpleNamespace(should_block=False, mismatched_samples=[], expected_mode="775")

            with patch("app.main.run_first_time_safety_net", return_value=RESULT):
                with patch("app.main.log_line"):
                    RETURNED = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertTrue(RETURNED)
            self.assertTrue((CONFIG.config_dir / "safety_net_done.flag").exists())
            self.assertFalse(BLOCKED.exists())

# --------------------------------------------------------------------------
# This test confirms blocked safety-net sends notification once.
# --------------------------------------------------------------------------
    def test_enforce_safety_net_block_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "worker.log"
            RESULT = SimpleNamespace(
                should_block=True,
                mismatched_samples=["/output/file1"],
                expected_mode="775",
            )

            with patch("app.main.run_first_time_safety_net", return_value=RESULT):
                with patch("app.main.notify") as NOTIFY:
                    with patch("app.main.log_line"):
                        RETURNED = enforce_safety_net(CONFIG, TELEGRAM, LOG_FILE)

            self.assertFalse(RETURNED)
            self.assertTrue((CONFIG.config_dir / "safety_net_blocked.flag").exists())
            self.assertIn("Safety net blocked backup.", NOTIFY.call_args[0][1])

# --------------------------------------------------------------------------
# This test confirms process_commands returns events and next offset.
# --------------------------------------------------------------------------
    def test_process_commands_with_updates(self) -> None:
        TELEGRAM = TelegramConfig("token", "12345")
        UPDATES = [{"update_id": 1}, {"update_id": 7}]
        EVENTS = [
            None,
            CommandEvent(command="backup", args="", update_id=7),
        ]

        with patch("app.main.fetch_updates", return_value=UPDATES):
            with patch("app.main.parse_command", side_effect=EVENTS):
                COMMANDS, OFFSET = process_commands(TELEGRAM, "alice", UPDATE_OFFSET=3)

        self.assertEqual(COMMANDS, [("backup", "")])
        self.assertEqual(OFFSET, 8)

# --------------------------------------------------------------------------
# This test confirms run_backup sends start/end notifications and logs.
# --------------------------------------------------------------------------
    def test_run_backup_persists_manifest_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            LOG_FILE = CONFIG.logs_dir / "worker.log"
            CLIENT = Mock()
            SUMMARY = SimpleNamespace(
                transferred_files=2,
                total_files=3,
                skipped_files=1,
                error_files=0,
            )

            with patch("app.main.load_manifest", return_value={"/a": {"etag": "1"}}):
                with patch("app.main.perform_incremental_sync", return_value=(SUMMARY, {"/b": {"etag": "2"}})):
                    with patch("app.main.save_manifest") as SAVE_MANIFEST:
                        with patch("app.main.notify") as NOTIFY:
                            with patch("app.main.log_line") as LOG_LINE:
                                run_backup(CLIENT, CONFIG, TELEGRAM, LOG_FILE)

            SAVE_MANIFEST.assert_called_once()
            self.assertEqual(NOTIFY.call_count, 2)
            self.assertGreaterEqual(LOG_LINE.call_count, 1)
            self.assertEqual(LOG_LINE.call_args_list[-1].args[1], "info")
            self.assertEqual(LOG_LINE.call_args_list[0].args[1], "debug")
            self.assertIn("Loaded manifest entries:", LOG_LINE.call_args_list[0].args[2])
            self.assertEqual(LOG_LINE.call_args_list[1].args[1], "debug")
            self.assertIn("Sync summary detail:", LOG_LINE.call_args_list[1].args[2])

# --------------------------------------------------------------------------
# This test confirms handle_command backup path requests a backup.
# --------------------------------------------------------------------------
    def test_handle_command_backup_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.notify") as NOTIFY:
                NEW_STATE, IS_AUTHENTICATED, REQUESTED = handle_command(
                    "backup",
                    "",
                    CONFIG,
                    Mock(),
                    AUTH_STATE,
                    True,
                    TELEGRAM,
                )

            self.assertEqual(NEW_STATE, AUTH_STATE)
            self.assertTrue(IS_AUTHENTICATED)
            self.assertTrue(REQUESTED)
            NOTIFY.assert_called_once_with(TELEGRAM, "Backup requested.")

# --------------------------------------------------------------------------
# This test confirms handle_command auth prompt path persists pending state.
# --------------------------------------------------------------------------
    def test_handle_command_auth_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.save_auth_state") as SAVE:
                with patch("app.main.notify"):
                    NEW_STATE, _, REQUESTED = handle_command(
                        "auth",
                        "",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            self.assertTrue(NEW_STATE.auth_pending)
            self.assertFalse(REQUESTED)
            SAVE.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms handle_command reauth prompt path persists pending state.
# --------------------------------------------------------------------------
    def test_handle_command_reauth_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.save_auth_state") as SAVE:
                with patch("app.main.notify"):
                    NEW_STATE, _, REQUESTED = handle_command(
                        "reauth",
                        "",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            self.assertTrue(NEW_STATE.reauth_pending)
            self.assertFalse(REQUESTED)
            SAVE.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms handle_command auth flow delegates to attempt_auth.
# --------------------------------------------------------------------------
    def test_handle_command_auth_with_code_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            TELEGRAM = TelegramConfig("token", "12345")
            AUTH_STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")
            EXPECTED_STATE = AuthState("2026-03-09T12:00:00+00:00", False, False, "none")

            with patch("app.main.attempt_auth", return_value=(EXPECTED_STATE, True, "ok")) as ATTEMPT:
                with patch("app.main.log_line") as LOG:
                    NEW_STATE, IS_AUTHENTICATED, REQUESTED = handle_command(
                        "auth",
                        "123456",
                        CONFIG,
                        Mock(),
                        AUTH_STATE,
                        False,
                        TELEGRAM,
                    )

            ATTEMPT.assert_called_once()
            LOG.assert_called_once()
            self.assertEqual(NEW_STATE, EXPECTED_STATE)
            self.assertTrue(IS_AUTHENTICATED)
            self.assertFalse(REQUESTED)


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
                                        with patch("app.main.attempt_auth", return_value=(STATE, False, "fail")):
                                            with patch(
                                                "app.main.wait_for_one_shot_auth",
                                                return_value=(STATE, False),
                                            ):
                                                with patch("app.main.notify") as NOTIFY:
                                                    RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 2)
            NOTIFY.assert_called_with(
                TelegramConfig(CONFIG.telegram_bot_token, CONFIG.telegram_chat_id),
                "One-shot backup skipped because authentication is incomplete.",
            )

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
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch(
                                                "app.main.wait_for_one_shot_auth",
                                                return_value=(STATE, True),
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
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.enforce_safety_net", return_value=False):
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
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.enforce_safety_net", return_value=True):
                                                with patch("app.main.run_backup") as RUN_BACKUP:
                                                    RESULT = __import__("app.main", fromlist=["main"]).main()

            self.assertEqual(RESULT, 0)
            RUN_BACKUP.assert_called_once()

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
                                        with patch("app.main.attempt_auth", return_value=(INITIAL_STATE, False, "mfa")):
                                            with patch(
                                                "app.main.wait_for_one_shot_auth",
                                                return_value=(READY_STATE, True),
                                            ) as WAIT_AUTH:
                                                with patch("app.main.enforce_safety_net", return_value=True):
                                                    with patch("app.main.run_backup") as RUN_BACKUP:
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
                                        with patch("app.main.attempt_auth", return_value=(STATE, False, "mfa")):
                                            with patch("app.main.wait_for_one_shot_auth", return_value=(STATE, False)):
                                                with patch("app.main.notify"):
                                                    with patch("app.main.log_line") as LOG_LINE:
                                                        __import__("app.main", fromlist=["main"]).main()

            DEBUG_LINES = [CALL for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
            self.assertGreaterEqual(len(DEBUG_LINES), 1)
            self.assertTrue(
                any("Auth state after startup attempt:" in CALL.args[2] for CALL in DEBUG_LINES)
            )

# --------------------------------------------------------------------------
# This test confirms loop sleeps and continues when not due and no request.
# --------------------------------------------------------------------------
    def test_main_loop_sleeps_when_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_runtime(TMPDIR).__dict__ | {"schedule_mode": "daily"}))
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.get_next_run_epoch", return_value=200):
                                                with patch("app.main.time.time", side_effect=[100, 100]):
                                                    with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                        with patch("app.main.process_commands", return_value=([], None)):
                                                            with patch("app.main.time.sleep", side_effect=SystemExit):
                                                                with self.assertRaises(SystemExit):
                                                                    __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path skips when auth becomes incomplete.
# --------------------------------------------------------------------------
    def test_main_loop_skips_when_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch("app.main.attempt_auth", return_value=(STATE, False, "fail")):
                                            with patch("app.main.time.time", side_effect=[100, 100]):
                                                with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                    with patch("app.main.process_commands", return_value=([], None)):
                                                        with patch("app.main.get_next_run_epoch", return_value=160):
                                                            with patch("app.main.time.sleep", side_effect=SystemExit):
                                                                with self.assertRaises(SystemExit):
                                                                    __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path skips when reauth is pending.
# --------------------------------------------------------------------------
    def test_main_loop_skips_when_reauth_pending(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, True, "prompt2")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.time.time", side_effect=[100, 100]):
                                                with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                    with patch("app.main.process_commands", return_value=([], None)):
                                                        with patch("app.main.get_next_run_epoch", return_value=160):
                                                            with patch("app.main.time.sleep", side_effect=SystemExit):
                                                                with self.assertRaises(SystemExit):
                                                                    __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path sleeps when safety-net blocks.
# --------------------------------------------------------------------------
    def test_main_loop_sleeps_when_safety_net_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.time.time", side_effect=[100, 100]):
                                                with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                    with patch("app.main.process_commands", return_value=([], None)):
                                                        with patch("app.main.get_next_run_epoch", return_value=160):
                                                            with patch("app.main.enforce_safety_net", return_value=False):
                                                                with patch("app.main.time.sleep", side_effect=SystemExit):
                                                                    with self.assertRaises(SystemExit):
                                                                        __import__("app.main", fromlist=["main"]).main()

# --------------------------------------------------------------------------
# This test confirms due loop path runs backup when all checks pass.
# --------------------------------------------------------------------------
    def test_main_loop_runs_backup_when_due_and_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_runtime(TMPDIR)
            STATE = AuthState("1970-01-01T00:00:00+00:00", False, False, "none")

            with patch("app.main.load_config", return_value=CONFIG):
                with patch("app.main.configure_keyring"):
                    with patch("app.main.load_credentials", return_value=("", "")):
                        with patch("app.main.validate_config", return_value=[]):
                            with patch("app.main.save_credentials"):
                                with patch("app.main.ICloudDriveClient", return_value=Mock()):
                                    with patch("app.main.load_auth_state", return_value=STATE):
                                        with patch("app.main.attempt_auth", return_value=(STATE, True, "ok")):
                                            with patch("app.main.time.time", side_effect=[100, 100]):
                                                with patch("app.main.process_reauth_reminders", return_value=STATE):
                                                    with patch("app.main.process_commands", return_value=([], None)):
                                                        with patch("app.main.get_next_run_epoch", return_value=160):
                                                            with patch("app.main.enforce_safety_net", return_value=True):
                                                                with patch("app.main.run_backup") as RUN_BACKUP:
                                                                    with patch("app.main.time.sleep", side_effect=SystemExit):
                                                                        with self.assertRaises(SystemExit):
                                                                            __import__("app.main", fromlist=["main"]).main()

            RUN_BACKUP.assert_called_once()


if __name__ == "__main__":
    unittest.main()
