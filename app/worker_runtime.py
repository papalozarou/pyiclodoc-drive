# ------------------------------------------------------------------------------
# This module encapsulates the worker runtime loop and one-shot orchestration.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from app.runtime_context import WorkerRuntimeContext
from app.state import AuthState
from app.telegram_messages import (
    build_backup_skipped_auth_incomplete_message,
    build_backup_skipped_reauth_pending_message,
    build_one_shot_waiting_for_auth_message,
)

RUN_ONCE_AUTH_WAIT_SECONDS = 900
RUN_ONCE_AUTH_POLL_SECONDS = 5


# ------------------------------------------------------------------------------
# This data class groups runtime callbacks used by worker orchestration.
#
# N.B.
# This is the orchestration boundary for the worker loop. The runtime module
# owns loop control, one-shot waiting, and backup-trigger decisions, while the
# concrete auth, command, backup, and scheduling behaviour is injected from the
# surrounding runtime modules.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkerRuntimeDeps:
    attempt_auth_fn: Callable[..., tuple[AuthState, bool, str]]
    process_reauth_reminders_fn: Callable[..., AuthState]
    process_commands_fn: Callable[..., tuple[list[tuple[str, str]], int | None]]
    handle_command_fn: Callable[..., tuple[AuthState, bool, bool]]
    enforce_safety_net_fn: Callable[..., bool]
    run_backup_fn: Callable[..., None]
    notify_fn: Callable[..., None]
    log_line_fn: Callable[..., None]
    get_next_run_epoch_fn: Callable[..., int]
    build_one_shot_waiting_for_auth_message_fn: Callable[..., str]
    build_backup_skipped_auth_incomplete_message_fn: Callable[..., str]
    build_backup_skipped_reauth_pending_message_fn: Callable[..., str]
    time_fn: Callable[[], float] = time.time
    sleep_fn: Callable[[float], None] = time.sleep


# ------------------------------------------------------------------------------
# This data class returns worker runtime exit outcome to the process entrypoint.
#
# N.B.
# This keeps the runtime module focused on orchestration decisions and leaves
# process-exit handling, container-stop notification, and heartbeat shutdown to
# the caller.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkerRunResult:
    exit_code: int
    stop_status: str


# ------------------------------------------------------------------------------
# This function waits for one-shot authentication commands before exit.
#
# 1. "RUNTIME_CONTEXT" is shared worker runtime state.
# 2. "CLIENT" is iCloud client wrapper.
# 3. "AUTH_STATE" is current auth state.
# 4. "IS_AUTHENTICATED" tracks current auth validity.
# 5. "DEPS" groups runtime callbacks used by worker orchestration.
#
# Returns: Tuple "(auth_state, is_authenticated)".
# ------------------------------------------------------------------------------
def wait_for_one_shot_auth(
    RUNTIME_CONTEXT: WorkerRuntimeContext,
    CLIENT: Any,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    DEPS: WorkerRuntimeDeps,
) -> tuple[AuthState, bool]:
    START_EPOCH = int(DEPS.time_fn())
    UPDATE_OFFSET: int | None = None

    while True:
        if IS_AUTHENTICATED and not AUTH_STATE.reauth_pending:
            return AUTH_STATE, IS_AUTHENTICATED

        NOW_EPOCH = int(DEPS.time_fn())
        ELAPSED_SECONDS = NOW_EPOCH - START_EPOCH

        if ELAPSED_SECONDS >= RUN_ONCE_AUTH_WAIT_SECONDS:
            return AUTH_STATE, IS_AUTHENTICATED

        COMMANDS, UPDATE_OFFSET = DEPS.process_commands_fn(
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.CONFIG.container_username,
            UPDATE_OFFSET,
        )

        for COMMAND, ARGS in COMMANDS:
            AUTH_STATE, IS_AUTHENTICATED, _ = DEPS.handle_command_fn(
                COMMAND,
                ARGS,
                RUNTIME_CONTEXT.CONFIG,
                CLIENT,
                AUTH_STATE,
                IS_AUTHENTICATED,
                RUNTIME_CONTEXT.TELEGRAM,
            )

        DEPS.sleep_fn(RUN_ONCE_AUTH_POLL_SECONDS)


# ------------------------------------------------------------------------------
# This function logs startup authentication status after the initial attempt.
#
# 1. "RUNTIME_CONTEXT" is shared worker runtime state.
# 2. "AUTH_STATE" is current auth state.
# 3. "IS_AUTHENTICATED" tracks current auth validity.
# 4. "DETAILS" is auth result detail text.
# 5. "DEPS" groups runtime callbacks used by worker orchestration.
#
# Returns: None.
# ------------------------------------------------------------------------------
def log_startup_auth_state(
    RUNTIME_CONTEXT: WorkerRuntimeContext,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    DETAILS: str,
    DEPS: WorkerRuntimeDeps,
) -> None:
    DEPS.log_line_fn(RUNTIME_CONTEXT.LOG_FILE, "info", DETAILS)
    DEPS.log_line_fn(
        RUNTIME_CONTEXT.LOG_FILE,
        "debug",
        "Auth state after startup attempt: "
        f"is_authenticated={IS_AUTHENTICATED}, "
        f"auth_pending={AUTH_STATE.auth_pending}, "
        f"reauth_pending={AUTH_STATE.reauth_pending}",
    )


# ------------------------------------------------------------------------------
# This function executes the one-shot worker path after startup auth.
#
# 1. "RUNTIME_CONTEXT" is shared worker runtime state.
# 2. "CLIENT" is iCloud client wrapper.
# 3. "AUTH_STATE" is current auth state.
# 4. "IS_AUTHENTICATED" tracks current auth validity.
# 5. "DEPS" groups runtime callbacks used by worker orchestration.
#
# Returns: Worker exit result for one-shot processing.
# ------------------------------------------------------------------------------
def run_one_shot_worker(
    RUNTIME_CONTEXT: WorkerRuntimeContext,
    CLIENT: Any,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    DEPS: WorkerRuntimeDeps,
) -> WorkerRunResult:
    if not IS_AUTHENTICATED or AUTH_STATE.reauth_pending:
        DEPS.notify_fn(
            RUNTIME_CONTEXT.TELEGRAM,
            DEPS.build_one_shot_waiting_for_auth_message_fn(
                RUNTIME_CONTEXT.APPLE_ID_LABEL,
                max(1, RUN_ONCE_AUTH_WAIT_SECONDS // 60),
            ),
        )
        AUTH_STATE, IS_AUTHENTICATED = wait_for_one_shot_auth(
            RUNTIME_CONTEXT,
            CLIENT,
            AUTH_STATE,
            IS_AUTHENTICATED,
            DEPS,
        )

    if not IS_AUTHENTICATED:
        DEPS.notify_fn(
            RUNTIME_CONTEXT.TELEGRAM,
            DEPS.build_backup_skipped_auth_incomplete_message_fn(
                RUNTIME_CONTEXT.APPLE_ID_LABEL
            ),
        )
        return WorkerRunResult(
            exit_code=2,
            stop_status="One-shot backup skipped due to incomplete authentication.",
        )

    if AUTH_STATE.reauth_pending:
        DEPS.notify_fn(
            RUNTIME_CONTEXT.TELEGRAM,
            DEPS.build_backup_skipped_reauth_pending_message_fn(
                RUNTIME_CONTEXT.APPLE_ID_LABEL
            ),
        )
        return WorkerRunResult(
            exit_code=3,
            stop_status="One-shot backup skipped due to pending reauthentication.",
        )

    if not DEPS.enforce_safety_net_fn(
        RUNTIME_CONTEXT.CONFIG,
        RUNTIME_CONTEXT.TELEGRAM,
        RUNTIME_CONTEXT.LOG_FILE,
    ):
        return WorkerRunResult(
            exit_code=4,
            stop_status="One-shot backup blocked by safety net.",
        )

    DEPS.run_backup_fn(
        CLIENT,
        RUNTIME_CONTEXT.CONFIG,
        RUNTIME_CONTEXT.TELEGRAM,
        RUNTIME_CONTEXT.LOG_FILE,
        "one-shot",
    )
    return WorkerRunResult(
        exit_code=0,
        stop_status="Run completed and container exited.",
    )


# ------------------------------------------------------------------------------
# This function executes the scheduled or manual worker loop indefinitely.
#
# 1. "RUNTIME_CONTEXT" is shared worker runtime state.
# 2. "CLIENT" is iCloud client wrapper.
# 3. "AUTH_STATE" is current auth state.
# 4. "IS_AUTHENTICATED" tracks current auth validity.
# 5. "DEPS" groups runtime callbacks used by worker orchestration.
#
# Returns: This function does not return during normal operation.
# ------------------------------------------------------------------------------
def run_scheduled_worker_loop(
    RUNTIME_CONTEXT: WorkerRuntimeContext,
    CLIENT: Any,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    DEPS: WorkerRuntimeDeps,
) -> None:
    BACKUP_REQUESTED = False
    NEXT_UPDATE_OFFSET: int | None = None
    INITIAL_EPOCH = int(DEPS.time_fn())

    if RUNTIME_CONTEXT.CONFIG.schedule_mode == "interval":
        NEXT_RUN_EPOCH = INITIAL_EPOCH
    else:
        NEXT_RUN_EPOCH = DEPS.get_next_run_epoch_fn(
            RUNTIME_CONTEXT.CONFIG,
            INITIAL_EPOCH,
        )

    while True:
        AUTH_STATE = DEPS.process_reauth_reminders_fn(
            AUTH_STATE,
            RUNTIME_CONTEXT.CONFIG.auth_state_path,
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.CONFIG.container_username,
            RUNTIME_CONTEXT.CONFIG.reauth_interval_days,
        )
        COMMANDS, NEXT_UPDATE_OFFSET = DEPS.process_commands_fn(
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.CONFIG.container_username,
            NEXT_UPDATE_OFFSET,
        )

        for COMMAND, ARGS in COMMANDS:
            AUTH_STATE, IS_AUTHENTICATED, REQUESTED = DEPS.handle_command_fn(
                COMMAND,
                ARGS,
                RUNTIME_CONTEXT.CONFIG,
                CLIENT,
                AUTH_STATE,
                IS_AUTHENTICATED,
                RUNTIME_CONTEXT.TELEGRAM,
            )
            BACKUP_REQUESTED = BACKUP_REQUESTED or REQUESTED

        NOW_EPOCH = int(DEPS.time_fn())
        SCHEDULE_DUE = NOW_EPOCH >= NEXT_RUN_EPOCH

        if not SCHEDULE_DUE and not BACKUP_REQUESTED:
            DEPS.sleep_fn(5)
            continue

        NEXT_RUN_EPOCH = DEPS.get_next_run_epoch_fn(
            RUNTIME_CONTEXT.CONFIG,
            NOW_EPOCH,
        )

        if not IS_AUTHENTICATED:
            DEPS.notify_fn(
                RUNTIME_CONTEXT.TELEGRAM,
                DEPS.build_backup_skipped_auth_incomplete_message_fn(
                    RUNTIME_CONTEXT.APPLE_ID_LABEL
                ),
            )
            DEPS.sleep_fn(5)
            continue

        if AUTH_STATE.reauth_pending:
            DEPS.notify_fn(
                RUNTIME_CONTEXT.TELEGRAM,
                DEPS.build_backup_skipped_reauth_pending_message_fn(
                    RUNTIME_CONTEXT.APPLE_ID_LABEL
                ),
            )
            DEPS.sleep_fn(5)
            continue

        if not DEPS.enforce_safety_net_fn(
            RUNTIME_CONTEXT.CONFIG,
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.LOG_FILE,
        ):
            DEPS.sleep_fn(30)
            continue

        BACKUP_TRIGGER = "manual" if BACKUP_REQUESTED else "scheduled"
        DEPS.run_backup_fn(
            CLIENT,
            RUNTIME_CONTEXT.CONFIG,
            RUNTIME_CONTEXT.TELEGRAM,
            RUNTIME_CONTEXT.LOG_FILE,
            BACKUP_TRIGGER,
        )
        BACKUP_REQUESTED = False
        DEPS.sleep_fn(5)


# ------------------------------------------------------------------------------
# This function executes the worker runtime after bootstrap has completed.
#
# 1. "RUNTIME_CONTEXT" is shared worker runtime state.
# 2. "CLIENT" is iCloud client wrapper.
# 3. "AUTH_STATE" is persisted auth state loaded during startup.
# 4. "DEPS" groups runtime callbacks used by worker orchestration.
#
# Returns: Worker exit result for the calling process entrypoint.
# ------------------------------------------------------------------------------
def run_worker_runtime(
    RUNTIME_CONTEXT: WorkerRuntimeContext,
    CLIENT: Any,
    AUTH_STATE: AuthState,
    DEPS: WorkerRuntimeDeps,
) -> WorkerRunResult:
    AUTH_STATE, IS_AUTHENTICATED, DETAILS = DEPS.attempt_auth_fn(
        CLIENT,
        AUTH_STATE,
        RUNTIME_CONTEXT.CONFIG.auth_state_path,
        RUNTIME_CONTEXT.TELEGRAM,
        RUNTIME_CONTEXT.CONFIG.container_username,
        RUNTIME_CONTEXT.CONFIG.icloud_email,
        "",
    )
    log_startup_auth_state(
        RUNTIME_CONTEXT,
        AUTH_STATE,
        IS_AUTHENTICATED,
        DETAILS,
        DEPS,
    )

    if RUNTIME_CONTEXT.CONFIG.run_once:
        return run_one_shot_worker(
            RUNTIME_CONTEXT,
            CLIENT,
            AUTH_STATE,
            IS_AUTHENTICATED,
            DEPS,
        )

    run_scheduled_worker_loop(
        RUNTIME_CONTEXT,
        CLIENT,
        AUTH_STATE,
        IS_AUTHENTICATED,
        DEPS,
    )
    return WorkerRunResult(
        exit_code=0,
        stop_status="Worker process exited.",
    )
