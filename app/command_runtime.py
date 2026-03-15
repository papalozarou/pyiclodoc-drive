# ------------------------------------------------------------------------------
# This module encapsulates Telegram command polling and command handling logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from app.state import AuthState, save_auth_state
from app.telegram_bot import TelegramConfig, fetch_updates, parse_command
from app.telegram_messages import (
    build_authentication_required_message,
    build_backup_requested_message,
    build_reauthentication_required_for_apple_id_message,
)


# ------------------------------------------------------------------------------
# This protocol describes the config values required for command handling.
# ------------------------------------------------------------------------------
class CommandConfig(Protocol):
    auth_state_path: Path
    container_username: str
    icloud_email: str


# ------------------------------------------------------------------------------
# This protocol is a marker for the command handler's iCloud client argument.
# ------------------------------------------------------------------------------
class CommandClient(Protocol):
    ...


# ------------------------------------------------------------------------------
# This data class groups Telegram polling callbacks used by command polling.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class CommandPollingDeps:
    fetch_updates_fn: Callable = fetch_updates
    parse_command_fn: Callable = parse_command


# ------------------------------------------------------------------------------
# This data class groups runtime callbacks used by command handling.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class CommandRuntimeDeps:
    attempt_auth_fn: Callable
    notify_fn: Callable[[TelegramConfig, str], None]
    save_auth_state_fn: Callable[[Path, AuthState], None] = save_auth_state
    log_line_fn: Callable | None = None
    log_file_path: Path | None = None


# ------------------------------------------------------------------------------
# This function polls Telegram and returns parsed command intents.
#
# 1. "TELEGRAM" is Telegram configuration.
# 2. "USERNAME" is command prefix.
# 3. "UPDATE_OFFSET" is update offset cursor.
#
# Returns: Tuple "(commands, next_offset)" for command execution.
# ------------------------------------------------------------------------------
def process_commands(
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    UPDATE_OFFSET: int | None,
    DEPS: CommandPollingDeps | None = None,
) -> tuple[list[tuple[str, str]], int | None]:
    RUNTIME_DEPS = DEPS or CommandPollingDeps()
    UPDATES = RUNTIME_DEPS.fetch_updates_fn(TELEGRAM, UPDATE_OFFSET)

    if not UPDATES:
        return [], UPDATE_OFFSET

    COMMANDS: list[tuple[str, str]] = []
    MAX_UPDATE = UPDATE_OFFSET or 0

    for UPDATE in UPDATES:
        EVENT = RUNTIME_DEPS.parse_command_fn(UPDATE, USERNAME, TELEGRAM.chat_id)
        UPDATE_ID = int(UPDATE.get("update_id", 0))
        MAX_UPDATE = max(MAX_UPDATE, UPDATE_ID + 1)

        if EVENT is None:
            continue

        COMMANDS.append((EVENT.command, EVENT.args))

    return COMMANDS, MAX_UPDATE


# ------------------------------------------------------------------------------
# This function handles a single Telegram command.
#
# 1. "COMMAND" is parsed command keyword.
# 2. "ARGS" is optional command payload.
# 3. "CONFIG" is runtime configuration.
# 4. "CLIENT" is iCloud client wrapper.
# 5. "AUTH_STATE" is current auth state.
# 6. "IS_AUTHENTICATED" tracks current auth validity.
# 7. "TELEGRAM" is Telegram integration configuration.
# 8. "APPLE_ID_LABEL" is the formatted Apple ID label.
# 9. "DEPS" groups runtime callbacks used by command handling.
#
# Returns: Tuple "(auth_state, is_authenticated, backup_requested)".
# ------------------------------------------------------------------------------
def handle_command(
    COMMAND: str,
    ARGS: str,
    CONFIG: CommandConfig,
    CLIENT: CommandClient,
    AUTH_STATE: AuthState,
    IS_AUTHENTICATED: bool,
    TELEGRAM: TelegramConfig,
    APPLE_ID_LABEL: str,
    DEPS: CommandRuntimeDeps,
) -> tuple[AuthState, bool, bool]:
    if COMMAND == "backup":
        DEPS.notify_fn(
            TELEGRAM,
            build_backup_requested_message(APPLE_ID_LABEL),
        )
        return AUTH_STATE, IS_AUTHENTICATED, True

    if COMMAND == "auth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        DEPS.save_auth_state_fn(CONFIG.auth_state_path, NEW_STATE)
        DEPS.notify_fn(
            TELEGRAM,
            build_authentication_required_message(
                APPLE_ID_LABEL, CONFIG.container_username
            ),
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    if COMMAND == "reauth" and not ARGS:
        NEW_STATE = replace(AUTH_STATE, reauth_pending=True)
        DEPS.save_auth_state_fn(CONFIG.auth_state_path, NEW_STATE)
        DEPS.notify_fn(
            TELEGRAM,
            build_reauthentication_required_for_apple_id_message(
                APPLE_ID_LABEL, CONFIG.container_username
            ),
        )
        return NEW_STATE, IS_AUTHENTICATED, False

    NEW_STATE, NEW_AUTH, DETAILS = DEPS.attempt_auth_fn(
        CLIENT,
        AUTH_STATE,
        CONFIG.auth_state_path,
        TELEGRAM,
        CONFIG.container_username,
        CONFIG.icloud_email,
        ARGS,
    )

    if DEPS.log_line_fn is not None and DEPS.log_file_path is not None:
        DEPS.log_line_fn(DEPS.log_file_path, "info", f"Auth command result: {DETAILS}")

    return NEW_STATE, NEW_AUTH, False
