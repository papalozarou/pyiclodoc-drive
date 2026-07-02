# ------------------------------------------------------------------------------
# This test module verifies worker-loop control flow in "app.worker_runtime".
# ------------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.auth_runtime import AuthAttemptResult
from app.command_runtime import (
    CommandHandleResult,
    CommandPollBatch,
    STARTUP_CUTOVER_OFFSET,
)
from app.backup_runtime import BackupRunResult, BackupRuntimeDeps, run_backup as run_backup_once
from app.config import AppConfig
from app.icloud_client import DownloadResult, TraversalWorkerTimeoutError
from app.runtime_context import WorkerRuntimeContext
from app.state import AuthState
from app.syncer import build_empty_traversal_stats_snapshot
from app.telegram_bot import CommandEvent, TelegramConfig
from app.worker_runtime import (
    capture_startup_command_polling_state,
    CommandBatchReadResult,
    CommandPollingState,
    WorkerAuthState,
    WorkerRunResult,
    read_command_batch,
    run_one_shot_worker,
    run_scheduled_worker_loop,
    run_worker_runtime,
    wait_for_one_shot_auth,
)


class StopWorkerLoop(Exception):
    pass


# ------------------------------------------------------------------------------
# This function creates an "AppConfig" fixture for worker runtime tests.
# ------------------------------------------------------------------------------
def build_config_for_worker_runtime(TMPDIR: str) -> AppConfig:
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
        schedule_mode="daily",
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
# These tests verify skipped manual requests do not stay latched in the loop.
# ------------------------------------------------------------------------------
class TestWorkerRuntime(unittest.TestCase):
# --------------------------------------------------------------------------
# This function builds a worker runtime context plus shared dependency values
# for worker-loop tests.
#
# Returns: Tuple "(runtime_context, telegram, auth_state)".
# --------------------------------------------------------------------------
    def build_runtime_context(
        self,
        TMPDIR: str,
        *,
        reauth_pending: bool = False,
    ) -> tuple[WorkerRuntimeContext, TelegramConfig, AuthState]:
        CONFIG = build_config_for_worker_runtime(TMPDIR)
        TELEGRAM = TelegramConfig("token", "12345")
        AUTH_STATE = AuthState(
            "1970-01-01T00:00:00+00:00",
            False,
            reauth_pending,
            "none",
        )
        RUNTIME_CONTEXT = WorkerRuntimeContext(
            config=CONFIG,
            telegram=TELEGRAM,
            log_file=CONFIG.logs_dir / "pyiclodoc-drive-worker.log",
            apple_id_label="alice@example.com",
        )
        return RUNTIME_CONTEXT, TELEGRAM, AUTH_STATE

# --------------------------------------------------------------------------
# This function builds one command batch fixture for polling tests.
#
# 1. "COMMANDS" is a list of "(command, args, message_epoch)" tuples.
# 2. "NEXT_UPDATE_OFFSET" is the next polling cursor.
#
# Returns: "CommandPollBatch" fixture.
# --------------------------------------------------------------------------
    def build_command_batch(
        self,
        COMMANDS: list[tuple[str, str, int]],
        NEXT_UPDATE_OFFSET: int | None,
    ) -> CommandPollBatch:
        EVENTS = [
            CommandEvent(
                command=COMMAND,
                args=ARGS,
                update_id=INDEX + 1,
                message_epoch=MESSAGE_EPOCH,
            )
            for INDEX, (COMMAND, ARGS, MESSAGE_EPOCH) in enumerate(COMMANDS)
        ]
        return CommandPollBatch(EVENTS, NEXT_UPDATE_OFFSET)

# --------------------------------------------------------------------------
# This function builds worker-loop dependencies with controllable callbacks.
#
# 1. "PROCESS_COMMANDS_FN" returns Telegram commands plus next offset.
# 2. "HANDLE_COMMAND_FN" handles one command and returns request state.
# 3. "ENFORCE_SAFETY_NET_FN" applies the safety gate before backup.
# 4. "NOTIFY_FN" captures operator notifications for assertions.
# 5. "SLEEP_FN" controls loop exit for tests.
#
# Returns: Namespace exposing worker dependency fields.
# --------------------------------------------------------------------------
    def build_deps(
        self,
        *,
        PROCESS_COMMANDS_FN,
        HANDLE_COMMAND_FN,
        ENFORCE_SAFETY_NET_FN,
        NOTIFY_FN,
        SLEEP_FN,
    ) -> SimpleNamespace:
        def poll_command_batch_fn(*ARGS):
            RESULT = PROCESS_COMMANDS_FN(*ARGS)

            if isinstance(RESULT, CommandPollBatch):
                return RESULT

            COMMANDS, NEXT_UPDATE_OFFSET = RESULT
            return self.build_command_batch(
                [(COMMAND, COMMAND_ARGS, 0) for COMMAND, COMMAND_ARGS in COMMANDS],
                NEXT_UPDATE_OFFSET,
            )

        return SimpleNamespace(
            process_reauth_reminders_fn=lambda AUTH_STATE, *_: AUTH_STATE,
            poll_command_batch_fn=poll_command_batch_fn,
            handle_command_fn=HANDLE_COMMAND_FN,
            enforce_safety_net_fn=ENFORCE_SAFETY_NET_FN,
            run_backup_fn=Mock(
                return_value=BackupRunResult(
                    summary=SimpleNamespace(
                        traversal_complete=True,
                        traversal_hard_failures=0,
                        delete_phase_skipped=False,
                    ),
                    manifest_updated=True,
                    total_errors=0,
                    completion_message="complete",
                    completion_log_message="Backup complete.",
                )
            ),
            notify_fn=NOTIFY_FN,
            log_line_fn=Mock(),
            get_next_run_epoch_fn=lambda *_: 200,
            build_backup_skipped_auth_incomplete_message_fn=lambda APPLE_ID_LABEL: (
                f"auth incomplete for {APPLE_ID_LABEL}"
            ),
            build_backup_skipped_reauth_pending_message_fn=lambda APPLE_ID_LABEL: (
                f"reauth pending for {APPLE_ID_LABEL}"
            ),
            check_runtime_liveness_fn=lambda: None,
            time_fn=lambda: 100,
            sleep_fn=SLEEP_FN,
        )

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait exits at the configured timeout
# when authentication never completes.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_returns_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(return_value=([], None))
            HANDLE_COMMAND = Mock()
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.time_fn = Mock(side_effect=[0, 900])

            RESULT = wait_for_one_shot_auth(
                RUNTIME_CONTEXT,
                Mock(),
                WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=False),
                CommandPollingState(phase="live_polling", next_update_offset=None),
                DEPS,
            )

        self.assertEqual(RESULT.auth_state, AUTH_STATE)
        self.assertFalse(RESULT.is_authenticated)
        PROCESS_COMMANDS.assert_not_called()
        HANDLE_COMMAND.assert_not_called()
        DEPS.sleep_fn.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait processes commands until auth
# completes and then returns the updated state.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_processes_commands_until_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            UPDATED_STATE = AuthState(
                "2026-03-15T12:00:00+00:00",
                False,
                False,
                "none",
            )
            PROCESS_COMMANDS = Mock(
                side_effect=[
                    ([], None),
                    ([("auth", "123456")], 9),
                ]
            )
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=UPDATED_STATE,
                    is_authenticated=True,
                    backup_requested=False,
                    reason_code="authenticated",
                    operator_detail="ok",
                )
            )
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.time_fn = Mock(side_effect=[0, 1, 2])

            RESULT = wait_for_one_shot_auth(
                RUNTIME_CONTEXT,
                Mock(),
                WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=False),
                CommandPollingState(phase="live_polling", next_update_offset=None),
                DEPS,
            )

        self.assertEqual(RESULT.auth_state, UPDATED_STATE)
        self.assertTrue(RESULT.is_authenticated)
        self.assertEqual(PROCESS_COMMANDS.call_count, 2)
        HANDLE_COMMAND.assert_called_once()
        DEPS.sleep_fn.assert_called_once()
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("One-shot auth wait started:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("One-shot auth poll:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any(
                "Telegram command received during auth wait: "
                "command=auth, args_present=True."
                in LINE
                for LINE in DEBUG_LINES
            )
        )
        self.assertTrue(
            any("One-shot auth wait completed after command:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertFalse(any("123456" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait aborts when runtime liveness fails.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_aborts_on_runtime_liveness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=Mock(return_value=([], None)),
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.check_runtime_liveness_fn = Mock(
                return_value="heartbeat_stale: age_seconds=120, detail=touch_failed."
            )

            RESULT = run_worker_runtime(
                RUNTIME_CONTEXT,
                Mock(),
                AUTH_STATE,
                SimpleNamespace(
                    **DEPS.__dict__,
                    attempt_auth_fn=Mock(
                        return_value=AuthAttemptResult(
                            auth_state=AUTH_STATE,
                            is_authenticated=False,
                            reason_code="mfa_required",
                            operator_detail="mfa",
                        )
                    ),
                    build_one_shot_waiting_for_auth_message_fn=lambda *_: "wait",
                ),
            )

        self.assertEqual(RESULT.exit_code, 5)
        self.assertIn("runtime liveness failed", RESULT.stop_status)
        ERROR_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "error"
        ]
        self.assertTrue(
            any("Runtime liveness failure detected:" in LINE for LINE in ERROR_LINES)
        )

# --------------------------------------------------------------------------
# This test confirms startup heartbeat failure still uses structured runtime
# abort handling.
# --------------------------------------------------------------------------
    def test_run_worker_runtime_aborts_on_startup_heartbeat_failure(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=Mock(return_value=([], None)),
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.check_runtime_liveness_fn = Mock(
                return_value=(
                    "heartbeat_startup_failed: age_seconds=70, "
                    "detail=PermissionError: denied."
                )
            )

            RESULT = run_worker_runtime(
                RUNTIME_CONTEXT,
                Mock(),
                AUTH_STATE,
                SimpleNamespace(
                    **DEPS.__dict__,
                    attempt_auth_fn=Mock(
                        return_value=AuthAttemptResult(
                            auth_state=AUTH_STATE,
                            is_authenticated=False,
                            reason_code="mfa_required",
                            operator_detail="mfa",
                        )
                    ),
                    build_one_shot_waiting_for_auth_message_fn=lambda *_: "wait",
                ),
            )

        self.assertEqual(RESULT.exit_code, 5)
        self.assertIn("heartbeat_startup_failed", RESULT.stop_status)

# --------------------------------------------------------------------------
# This test confirms startup cutover captures one live polling cursor from the
# current Telegram update snapshot.
# --------------------------------------------------------------------------
    def test_capture_startup_command_polling_state_returns_next_offset(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, _AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(
                return_value=self.build_command_batch([("backup", "", 50)], 41)
            )
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )

            RESULT = capture_startup_command_polling_state(
                RUNTIME_CONTEXT,
                DEPS,
            )

        self.assertEqual(
            RESULT,
            CommandPollingState(
                phase="live_polling",
                next_update_offset=41,
            ),
        )
        self.assertEqual(PROCESS_COMMANDS.call_count, 1)
        self.assertEqual(
            PROCESS_COMMANDS.call_args_list[0].args[2],
            STARTUP_CUTOVER_OFFSET,
        )
        PROCESS_COMMANDS.assert_called_once_with(
            RUNTIME_CONTEXT.telegram,
            RUNTIME_CONTEXT.config.container_username,
            STARTUP_CUTOVER_OFFSET,
        )

# --------------------------------------------------------------------------
# This test confirms startup cutover still advances the cursor when the
# current snapshot contains no parsed command events.
# --------------------------------------------------------------------------
    def test_capture_startup_command_polling_state_handles_non_command_updates(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, _AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(return_value=self.build_command_batch([], 42))
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )

            RESULT = capture_startup_command_polling_state(
                RUNTIME_CONTEXT,
                DEPS,
            )

        self.assertEqual(RESULT, CommandPollingState(phase="live_polling", next_update_offset=42))
        self.assertEqual(PROCESS_COMMANDS.call_count, 1)

# --------------------------------------------------------------------------
# This test confirms startup cutover completes immediately when Telegram has
# no visible backlog to discard.
# --------------------------------------------------------------------------
    def test_capture_startup_command_polling_state_handles_empty_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, _AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(return_value=CommandPollBatch([], None))
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )

            RESULT = capture_startup_command_polling_state(
                RUNTIME_CONTEXT,
                DEPS,
            )

        self.assertEqual(
            RESULT,
            CommandPollingState(
                phase="live_polling",
                next_update_offset=None,
            ),
        )
        PROCESS_COMMANDS.assert_called_once_with(
            RUNTIME_CONTEXT.telegram,
            RUNTIME_CONTEXT.config.container_username,
            STARTUP_CUTOVER_OFFSET,
        )

# --------------------------------------------------------------------------
# This test confirms startup cutover discards the visible backlog and leaves
# later live commands available from the returned cursor.
# --------------------------------------------------------------------------
    def test_capture_startup_command_polling_state_discards_backlog_and_keeps_live_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, _AUTH_STATE = self.build_runtime_context(TMPDIR)
            BACKLOG_EVENT = CommandEvent(
                command="backup",
                args="",
                update_id=500,
                message_epoch=100,
            )
            LIVE_EVENT = CommandEvent(
                command="auth",
                args="123456",
                update_id=501,
                message_epoch=101,
            )
            PROCESS_COMMANDS = Mock(
                side_effect=[
                    CommandPollBatch([BACKLOG_EVENT], 501),
                    CommandPollBatch([LIVE_EVENT], 502),
                ]
            )
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )

            POLLING_STATE = capture_startup_command_polling_state(
                RUNTIME_CONTEXT,
                DEPS,
            )
            READ_RESULT = read_command_batch(
                RUNTIME_CONTEXT,
                POLLING_STATE,
                DEPS,
            )

        self.assertEqual(
            POLLING_STATE,
            CommandPollingState(
                phase="live_polling",
                next_update_offset=501,
            ),
        )
        self.assertEqual(READ_RESULT.commands, [("auth", "123456")])
        self.assertEqual(
            READ_RESULT.polling_state,
            CommandPollingState(
                phase="live_polling",
                next_update_offset=502,
            ),
        )
        self.assertEqual(PROCESS_COMMANDS.call_args_list[0].args[2], STARTUP_CUTOVER_OFFSET)
        self.assertEqual(PROCESS_COMMANDS.call_args_list[1].args[2], 501)

# --------------------------------------------------------------------------
# This test confirms command batch reads advance and reuse one polling state.
# --------------------------------------------------------------------------
    def test_read_command_batch_reuses_one_polling_state(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, _AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(
                side_effect=[
                    ([("backup", "")], 9),
                    ([("auth", "123456")], 10),
                ]
            )
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )

            FIRST_RESULT = read_command_batch(
                RUNTIME_CONTEXT,
                CommandPollingState(),
                DEPS,
            )
            SECOND_RESULT = read_command_batch(
                RUNTIME_CONTEXT,
                FIRST_RESULT.polling_state,
                DEPS,
            )

        self.assertEqual(
            FIRST_RESULT.polling_state,
            CommandPollingState(phase="live_polling", next_update_offset=9),
        )
        self.assertEqual(FIRST_RESULT.commands, [("backup", "")])
        self.assertEqual(SECOND_RESULT.commands, [("auth", "123456")])
        self.assertEqual(
            SECOND_RESULT.polling_state,
            CommandPollingState(phase="live_polling", next_update_offset=10),
        )
        self.assertEqual(PROCESS_COMMANDS.call_count, 2)
        self.assertEqual(PROCESS_COMMANDS.call_args_list[0].args[2], None)
        self.assertEqual(PROCESS_COMMANDS.call_args_list[1].args[2], 9)

# --------------------------------------------------------------------------
# This test confirms one-shot auth wait starts from the captured cutover
# cursor and only processes later commands.
# --------------------------------------------------------------------------
    def test_wait_for_one_shot_auth_uses_captured_startup_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            UPDATED_STATE = AuthState(
                "2026-03-15T12:00:00+00:00",
                False,
                False,
                "none",
            )
            PROCESS_COMMANDS = Mock(
                return_value=self.build_command_batch([("auth", "123456", 100)], 10)
            )
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=UPDATED_STATE,
                    is_authenticated=True,
                    backup_requested=False,
                    reason_code="authenticated",
                    operator_detail="ok",
                )
            )
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.time_fn = Mock(side_effect=[0, 1, 2])

            RESULT = wait_for_one_shot_auth(
                RUNTIME_CONTEXT,
                Mock(),
                WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=False),
                CommandPollingState(phase="live_polling", next_update_offset=9),
                DEPS,
            )

        self.assertEqual(RESULT.auth_state, UPDATED_STATE)
        self.assertTrue(RESULT.is_authenticated)
        self.assertEqual(PROCESS_COMMANDS.call_count, 1)
        HANDLE_COMMAND.assert_called_once_with(
            "auth",
            "123456",
            RUNTIME_CONTEXT.config,
            unittest.mock.ANY,
            AUTH_STATE,
            False,
            RUNTIME_CONTEXT.telegram,
        )

# --------------------------------------------------------------------------
# This test confirms one-shot worker returns the safety-net blocked exit
# result when authentication is complete but backup is not permitted.
# --------------------------------------------------------------------------
    def test_run_one_shot_worker_returns_safety_net_blocked_result(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            NOTIFY = Mock()
            ENFORCE_SAFETY_NET = Mock(return_value=False)
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=Mock(return_value=([], None)),
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=ENFORCE_SAFETY_NET,
                NOTIFY_FN=NOTIFY,
                SLEEP_FN=Mock(),
            )
            DEPS.build_one_shot_waiting_for_auth_message_fn = Mock(return_value="waiting")

            RESULT = run_one_shot_worker(
                RUNTIME_CONTEXT,
                Mock(),
                WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                CommandPollingState(phase="live_polling", next_update_offset=None),
                DEPS,
            )

        self.assertEqual(
            RESULT,
            WorkerRunResult(
                exit_code=4,
                stop_status="One-shot backup blocked by safety net.",
            ),
        )
        ENFORCE_SAFETY_NET.assert_called_once()
        NOTIFY.assert_not_called()
        DEPS.run_backup_fn.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms a skipped manual backup due to incomplete authentication
# is cleared after one notification.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_clears_manual_request_after_auth_skip(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            NOTIFY = Mock()
            PROCESS_COMMANDS = Mock(side_effect=[([], None), ([("backup", "")], 6), ([], 6)])
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=False,
                    backup_requested=True,
                    reason_code="backup_requested",
                )
            )
            ENFORCE_SAFETY_NET = Mock(return_value=True)
            SLEEP_CALLS = {"count": 0}

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=ENFORCE_SAFETY_NET,
                NOTIFY_FN=NOTIFY,
                SLEEP_FN=sleep_fn,
            )

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=False),
                    CommandPollingState(phase="live_polling", next_update_offset=None),
                    DEPS,
                )

        NOTIFY.assert_called_once_with(TELEGRAM, "auth incomplete for alice@example.com")
        ENFORCE_SAFETY_NET.assert_not_called()
        DEPS.run_backup_fn.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms a skipped manual backup due to pending reauthentication
# is cleared after one notification.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_clears_manual_request_after_reauth_skip(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, TELEGRAM, AUTH_STATE = self.build_runtime_context(
                TMPDIR,
                reauth_pending=True,
            )
            NOTIFY = Mock()
            PROCESS_COMMANDS = Mock(side_effect=[([], None), ([("backup", "")], 6), ([], 6)])
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=True,
                    backup_requested=True,
                    reason_code="backup_requested",
                )
            )
            ENFORCE_SAFETY_NET = Mock(return_value=True)
            SLEEP_CALLS = {"count": 0}

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=ENFORCE_SAFETY_NET,
                NOTIFY_FN=NOTIFY,
                SLEEP_FN=sleep_fn,
            )

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                    CommandPollingState(phase="live_polling", next_update_offset=None),
                    DEPS,
                )

        NOTIFY.assert_called_once_with(TELEGRAM, "reauth pending for alice@example.com")
        ENFORCE_SAFETY_NET.assert_not_called()
        DEPS.run_backup_fn.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms scheduled runtime starts from the captured cutover cursor
# and processes only later commands.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_uses_captured_startup_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(
                return_value=self.build_command_batch([("backup", "", 100)], 10)
            )
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=True,
                    backup_requested=True,
                    reason_code="backup_requested",
                )
            )

            def sleep_fn(_SECONDS: float) -> None:
                raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=sleep_fn,
            )
            DEPS.time_fn = Mock(side_effect=[100, 100])

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                    CommandPollingState(phase="live_polling", next_update_offset=9),
                    DEPS,
                )

        self.assertEqual(PROCESS_COMMANDS.call_count, 1)
        HANDLE_COMMAND.assert_called_once()
        DEPS.run_backup_fn.assert_called_once()

# --------------------------------------------------------------------------
# This test confirms a skipped manual backup due to a safety-net block is
# cleared after one blocked run.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_clears_manual_request_after_safety_net_skip(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            NOTIFY = Mock()
            PROCESS_COMMANDS = Mock(side_effect=[([], None), ([("backup", "")], 6), ([], 6)])
            HANDLE_COMMAND = Mock(
                return_value=CommandHandleResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=True,
                    backup_requested=True,
                    reason_code="backup_requested",
                )
            )
            ENFORCE_SAFETY_NET = Mock(return_value=False)
            SLEEP_CALLS = {"count": 0}

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=ENFORCE_SAFETY_NET,
                NOTIFY_FN=NOTIFY,
                SLEEP_FN=sleep_fn,
            )

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                    CommandPollingState(phase="live_polling", next_update_offset=None),
                    DEPS,
                )

        NOTIFY.assert_not_called()
        self.assertEqual(ENFORCE_SAFETY_NET.call_count, 1)
        DEPS.run_backup_fn.assert_not_called()

# --------------------------------------------------------------------------
# This test confirms the scheduled loop sleeps and retries when neither the
# schedule nor a manual backup request is due.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_waits_when_no_backup_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            SLEEP = Mock(side_effect=StopWorkerLoop())
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=Mock(return_value=([], None)),
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=SLEEP,
            )
            DEPS.time_fn = Mock(side_effect=[100, 100])
            DEPS.get_next_run_epoch_fn = Mock(return_value=200)

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                    CommandPollingState(phase="live_polling", next_update_offset=None),
                    DEPS,
                )

        SLEEP.assert_called_once_with(5)
        DEPS.notify_fn.assert_not_called()
        DEPS.enforce_safety_net_fn.assert_not_called()
        DEPS.run_backup_fn.assert_not_called()
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("Scheduled loop decision:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("Scheduled loop sleeping: reason=no_due_backup" in LINE for LINE in DEBUG_LINES)
        )

# --------------------------------------------------------------------------
# This test confirms the scheduled loop survives the April 30, 2026 class
# of traversal failure when the failure happens inside the real backup path.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_survives_real_traversal_failure_backup_flow(self) -> None:
        class StalledClient:
            def list_entries(self):
                raise TraversalWorkerTimeoutError(
                    "Traversal worker stalled while reading / after 30.0s."
                )

            def get_traversal_stats_snapshot(self):
                return (
                    build_empty_traversal_stats_snapshot()
                    | {
                        "dir_hard_failures": 1,
                        "dir_failure_samples": [
                            {
                                "path": "/",
                                "status": "hard_failure",
                                "reason": "worker_timeout_after_30.0s",
                            }
                        ],
                    }
                )

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

            def download_package_tree(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            CONFIG = AppConfig(
                **(
                    RUNTIME_CONTEXT.config.__dict__
                    | {
                        "backup_delete_removed": True,
                    }
                )
            )
            RUNTIME_CONTEXT = WorkerRuntimeContext(
                config=CONFIG,
                telegram=RUNTIME_CONTEXT.telegram,
                log_file=RUNTIME_CONTEXT.log_file,
                apple_id_label=RUNTIME_CONTEXT.apple_id_label,
            )
            STALE_PATH = CONFIG.output_dir / "docs" / "stale.txt"
            STALE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STALE_PATH.write_text("stale", encoding="utf-8")
            PROCESS_COMMANDS = Mock(return_value=([], None))
            SAVE_MANIFEST = Mock(return_value=True)
            SLEEP_CALLS = {"count": 0}
            NEXT_RUN_EPOCHS: list[int] = []
            EXISTING_MANIFEST = {
                "docs/known.txt": {
                    "modified": "2026-03-01T00:00:00Z",
                    "size": 1,
                }
            }

            def get_next_run_epoch_fn(_CONFIG, NOW_EPOCH):
                NEXT_RUN_EPOCHS.append(NOW_EPOCH)
                return 200 if len(NEXT_RUN_EPOCHS) == 1 else 500

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=sleep_fn,
            )
            DEPS.time_fn = Mock(side_effect=[100, 200, 200, 200])
            DEPS.get_next_run_epoch_fn = Mock(side_effect=get_next_run_epoch_fn)

            def run_backup_fn(CLIENT, CONFIG, TELEGRAM, LOG_FILE, BACKUP_TRIGGER):
                _ = BACKUP_TRIGGER
                return run_backup_once(
                    CLIENT,
                    CONFIG,
                    TELEGRAM,
                    LOG_FILE,
                    "alice@example.com",
                    "Scheduled daily at 02:00.",
                    BackupRuntimeDeps(
                        load_manifest_fn=lambda _PATH: dict(EXISTING_MANIFEST),
                        save_manifest_fn=SAVE_MANIFEST,
                        log_line_fn=DEPS.log_line_fn,
                        notify_fn=DEPS.notify_fn,
                    ),
                )

            DEPS.run_backup_fn = Mock(side_effect=run_backup_fn)
            CLIENT = StalledClient()

            with patch("app.syncer.log_line", side_effect=DEPS.log_line_fn):
                with self.assertRaises(StopWorkerLoop):
                    run_scheduled_worker_loop(
                        RUNTIME_CONTEXT,
                        CLIENT,
                        WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                        CommandPollingState(phase="live_polling", next_update_offset=None),
                        DEPS,
                    )
            self.assertTrue(STALE_PATH.exists())

        SAVE_MANIFEST.assert_not_called()
        DEPS.run_backup_fn.assert_called_once_with(
            CLIENT,
            CONFIG,
            TELEGRAM,
            RUNTIME_CONTEXT.log_file,
            "scheduled",
        )
        self.assertEqual(NEXT_RUN_EPOCHS, [100, 200])
        ERROR_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "error"
        ]
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "debug"
        ]
        NOTIFY_MESSAGES = [
            CALL.args[1]
            for CALL in DEPS.notify_fn.call_args_list
        ]
        self.assertTrue(
            any("Traversal failed before completion:" in LINE for LINE in ERROR_LINES)
        )
        self.assertTrue(
            any(
                "Manifest save skipped because traversal was incomplete."
                in LINE
                for LINE in ERROR_LINES
            )
        )
        self.assertTrue(
            any(
                "Scheduled backup did not complete successfully." in LINE
                and "next scheduled run will retry" in LINE
                for LINE in ERROR_LINES
            )
        )
        self.assertTrue(
            any("Scheduled backup follow-up:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any(
                "Next scheduled backup calculated: next_run_epoch=500."
                in LINE
                for LINE in DEBUG_LINES
            )
        )
        self.assertTrue(
            any(
                "Status: Partial run due to incomplete traversal" in MESSAGE
                and "Delete removed: Skipped because traversal was incomplete" in MESSAGE
                and "Manifest: Not updated" in MESSAGE
                for MESSAGE in NOTIFY_MESSAGES
            )
        )

# --------------------------------------------------------------------------
# This test confirms the scheduled loop survives the 2026-06-14 class of
# failure: an unguarded drive root fetch raising inside "list_entries". Real
# "ICloudDriveClient.list_entries" now catches that exception and returns an
# empty list with "dir_hard_failures" set instead of letting it escape, so
# this fake reproduces exactly that contract rather than a raised
# "TraversalWorkerTimeoutError". Before the fix, the real exception had no
# handler anywhere on this call stack and crashed the whole worker process.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_survives_drive_root_failure_backup_flow(self) -> None:
        class DriveRootFailureClient:
            def list_entries(self):
                return []

            def get_traversal_stats_snapshot(self):
                return (
                    build_empty_traversal_stats_snapshot()
                    | {
                        "dir_hard_failures": 1,
                        "dir_failure_samples": [
                            {
                                "path": "/",
                                "status": "hard_failure",
                                "reason": (
                                    "drive_root_unavailable: ConnectTimeout: "
                                    "connect timeout"
                                ),
                            }
                        ],
                    }
                )

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

            def download_package_tree(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            CONFIG = AppConfig(
                **(
                    RUNTIME_CONTEXT.config.__dict__
                    | {
                        "backup_delete_removed": True,
                    }
                )
            )
            RUNTIME_CONTEXT = WorkerRuntimeContext(
                config=CONFIG,
                telegram=RUNTIME_CONTEXT.telegram,
                log_file=RUNTIME_CONTEXT.log_file,
                apple_id_label=RUNTIME_CONTEXT.apple_id_label,
            )
            STALE_PATH = CONFIG.output_dir / "docs" / "stale.txt"
            STALE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STALE_PATH.write_text("stale", encoding="utf-8")
            PROCESS_COMMANDS = Mock(return_value=([], None))
            SAVE_MANIFEST = Mock(return_value=True)
            SLEEP_CALLS = {"count": 0}
            NEXT_RUN_EPOCHS: list[int] = []
            EXISTING_MANIFEST = {
                "docs/known.txt": {
                    "modified": "2026-03-01T00:00:00Z",
                    "size": 1,
                }
            }

            def get_next_run_epoch_fn(_CONFIG, NOW_EPOCH):
                NEXT_RUN_EPOCHS.append(NOW_EPOCH)
                return 200 if len(NEXT_RUN_EPOCHS) == 1 else 500

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=sleep_fn,
            )
            DEPS.time_fn = Mock(side_effect=[100, 200, 200, 200])
            DEPS.get_next_run_epoch_fn = Mock(side_effect=get_next_run_epoch_fn)

            def run_backup_fn(CLIENT, CONFIG, TELEGRAM, LOG_FILE, BACKUP_TRIGGER):
                _ = BACKUP_TRIGGER
                return run_backup_once(
                    CLIENT,
                    CONFIG,
                    TELEGRAM,
                    LOG_FILE,
                    "alice@example.com",
                    "Scheduled daily at 02:00.",
                    BackupRuntimeDeps(
                        load_manifest_fn=lambda _PATH: dict(EXISTING_MANIFEST),
                        save_manifest_fn=SAVE_MANIFEST,
                        log_line_fn=DEPS.log_line_fn,
                        notify_fn=DEPS.notify_fn,
                    ),
                )

            DEPS.run_backup_fn = Mock(side_effect=run_backup_fn)
            CLIENT = DriveRootFailureClient()

            with patch("app.syncer.log_line", side_effect=DEPS.log_line_fn):
                with self.assertRaises(StopWorkerLoop):
                    run_scheduled_worker_loop(
                        RUNTIME_CONTEXT,
                        CLIENT,
                        WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                        CommandPollingState(phase="live_polling", next_update_offset=None),
                        DEPS,
                    )
            self.assertTrue(STALE_PATH.exists())

        SAVE_MANIFEST.assert_not_called()
        DEPS.run_backup_fn.assert_called_once_with(
            CLIENT,
            CONFIG,
            TELEGRAM,
            RUNTIME_CONTEXT.log_file,
            "scheduled",
        )
        self.assertEqual(NEXT_RUN_EPOCHS, [100, 200])
        ERROR_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "error"
        ]
        self.assertTrue(
            any(
                "Manifest save skipped because traversal was incomplete."
                in LINE
                for LINE in ERROR_LINES
            )
        )
        self.assertTrue(
            any(
                "Scheduled backup did not complete successfully." in LINE
                and "next scheduled run will retry" in LINE
                for LINE in ERROR_LINES
            )
        )

# --------------------------------------------------------------------------
# This test confirms a handled traversal failure during a due scheduled run
# leaves the worker alive, keeps the next schedule, and emits visible retry
# guidance instead of crashing the loop.
# --------------------------------------------------------------------------
    def test_run_scheduled_worker_loop_survives_incomplete_backup_and_retries_later(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            PROCESS_COMMANDS = Mock(return_value=([], None))
            NEXT_RUN_EPOCHS: list[int] = []
            SLEEP_CALLS = {"count": 0}
            INCOMPLETE_RESULT = BackupRunResult(
                summary=SimpleNamespace(
                    traversal_complete=False,
                    traversal_hard_failures=1,
                    delete_phase_skipped=True,
                ),
                manifest_updated=False,
                total_errors=1,
                completion_message="partial",
                completion_log_message="Backup completed with incomplete traversal.",
            )

            def get_next_run_epoch_fn(_CONFIG, NOW_EPOCH):
                NEXT_RUN_EPOCHS.append(NOW_EPOCH)
                return 200 if len(NEXT_RUN_EPOCHS) == 1 else 500

            def sleep_fn(_SECONDS: float) -> None:
                SLEEP_CALLS["count"] += 1
                if SLEEP_CALLS["count"] >= 2:
                    raise StopWorkerLoop()

            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=sleep_fn,
            )
            DEPS.time_fn = Mock(side_effect=[100, 200, 200, 200])
            DEPS.get_next_run_epoch_fn = Mock(side_effect=get_next_run_epoch_fn)
            DEPS.run_backup_fn = Mock(return_value=INCOMPLETE_RESULT)

            with self.assertRaises(StopWorkerLoop):
                run_scheduled_worker_loop(
                    RUNTIME_CONTEXT,
                    Mock(),
                    WorkerAuthState(auth_state=AUTH_STATE, is_authenticated=True),
                    CommandPollingState(phase="live_polling", next_update_offset=None),
                    DEPS,
                )

        DEPS.run_backup_fn.assert_called_once()
        self.assertEqual(NEXT_RUN_EPOCHS, [100, 200])
        ERROR_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "error"
        ]
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any(
                "Scheduled backup did not complete successfully." in LINE
                and "next scheduled run will retry" in LINE
                for LINE in ERROR_LINES
            )
        )
        self.assertTrue(
            any("Scheduled backup follow-up:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any(
                "Next scheduled backup calculated: next_run_epoch=500."
                in LINE
                for LINE in DEBUG_LINES
            )
        )
        self.assertTrue(
            any("Scheduled loop sleeping: reason=no_due_backup" in LINE for LINE in DEBUG_LINES)
        )

# --------------------------------------------------------------------------
# This test confirms run_worker_runtime delegates to the scheduled loop and
# returns the steady-state worker exit result.
# --------------------------------------------------------------------------
    def test_run_worker_runtime_returns_scheduled_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            ATTEMPT_AUTH = Mock(
                return_value=AuthAttemptResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=True,
                    reason_code="authenticated",
                    operator_detail="ok",
                )
            )
            PROCESS_COMMANDS = Mock(return_value=([], None))
            HANDLE_COMMAND = Mock()
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=HANDLE_COMMAND,
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.attempt_auth_fn = ATTEMPT_AUTH
            DEPS.log_line_fn = Mock()
            DEPS.build_one_shot_waiting_for_auth_message_fn = Mock(return_value="waiting")

            with patch("app.worker_runtime.run_scheduled_worker_loop") as RUN_LOOP:
                RESULT = run_worker_runtime(
                    RUNTIME_CONTEXT,
                    Mock(),
                    AUTH_STATE,
                    DEPS,
                )

        self.assertEqual(
            RESULT,
            WorkerRunResult(
                exit_code=0,
                stop_status="Worker process exited.",
            ),
        )
        ATTEMPT_AUTH.assert_called_once()
        RUN_LOOP.assert_called_once()
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in DEPS.log_line_fn.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("Capturing startup command cursor:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any(
                "Starting startup authentication attempt with Apple." in LINE
                for LINE in DEBUG_LINES
            )
        )
        self.assertTrue(
            any("Auth state after startup attempt:" in LINE for LINE in DEBUG_LINES)
        )

# --------------------------------------------------------------------------
# This test confirms runtime captures the startup cursor before auth and
# passes the same live polling state into the scheduled loop.
# --------------------------------------------------------------------------
    def test_run_worker_runtime_captures_startup_cursor_before_auth(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RUNTIME_CONTEXT, _TELEGRAM, AUTH_STATE = self.build_runtime_context(TMPDIR)
            STARTUP_BATCH = self.build_command_batch([("backup", "", 50)], 9)
            ATTEMPT_AUTH = Mock(
                return_value=AuthAttemptResult(
                    auth_state=AUTH_STATE,
                    is_authenticated=True,
                    reason_code="authenticated",
                    operator_detail="ok",
                )
            )
            PROCESS_COMMANDS = Mock(return_value=STARTUP_BATCH)
            DEPS = self.build_deps(
                PROCESS_COMMANDS_FN=PROCESS_COMMANDS,
                HANDLE_COMMAND_FN=Mock(),
                ENFORCE_SAFETY_NET_FN=Mock(return_value=True),
                NOTIFY_FN=Mock(),
                SLEEP_FN=Mock(),
            )
            DEPS.attempt_auth_fn = ATTEMPT_AUTH
            DEPS.log_line_fn = Mock()

            with patch("app.worker_runtime.run_scheduled_worker_loop") as RUN_LOOP:
                run_worker_runtime(
                    RUNTIME_CONTEXT,
                    Mock(),
                    AUTH_STATE,
                    DEPS,
                )

        PROCESS_COMMANDS.assert_called_once_with(
            RUNTIME_CONTEXT.telegram,
            RUNTIME_CONTEXT.config.container_username,
            STARTUP_CUTOVER_OFFSET,
        )
        ATTEMPT_AUTH.assert_called_once()
        self.assertEqual(
            RUN_LOOP.call_args.args[3],
            CommandPollingState(phase="live_polling", next_update_offset=9),
        )


if __name__ == "__main__":
    unittest.main()
