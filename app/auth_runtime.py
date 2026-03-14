# ------------------------------------------------------------------------------
# This module encapsulates authentication and reauthentication runtime logic.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dateutil import parser as date_parser

from app.state import AuthState, now_iso, save_auth_state
from app.telegram_bot import TelegramConfig, send_message
from app.telegram_messages import (
    build_authentication_complete_message,
    build_authentication_failed_message,
    build_authentication_required_message,
    build_reauth_reminder_message,
    build_reauthentication_required_message,
)
from app.time_utils import now_local


# ------------------------------------------------------------------------------
# This function sends a Telegram message when integration is configured.
#
# 1. "TELEGRAM" is Telegram integration configuration.
# 2. "MESSAGE" is outgoing message content.
#
# Returns: None.
# ------------------------------------------------------------------------------
def notify(TELEGRAM: TelegramConfig, MESSAGE: str) -> None:
    send_message(TELEGRAM, MESSAGE)


# ------------------------------------------------------------------------------
# This function formats a fallback-safe Apple ID label for Telegram messages.
#
# 1. "APPLE_ID" is the configured iCloud email value.
#
# Returns: Non-empty Apple ID label.
# ------------------------------------------------------------------------------
def format_apple_id_label(APPLE_ID: str) -> str:
    CLEAN_VALUE = APPLE_ID.strip()

    if CLEAN_VALUE:
        return CLEAN_VALUE

    return "<unknown>"


# ------------------------------------------------------------------------------
# This function parses an ISO timestamp with a strict epoch fallback.
#
# 1. "VALUE" is an ISO-formatted timestamp string.
#
# Returns: Offset-aware datetime; Unix epoch when parsing fails.
#
# Notes: dateutil parsing reference:
# https://dateutil.readthedocs.io/en/stable/parser.html
# ------------------------------------------------------------------------------
def parse_iso(VALUE: str) -> datetime:
    try:
        return date_parser.isoparse(VALUE)
    except (TypeError, ValueError, OverflowError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------
# This function calculates remaining whole days before reauthentication.
#
# 1. "LAST_AUTH_UTC" is stored offset-aware auth timestamp.
# 2. "INTERVAL_DAYS" is the reauthentication interval in days.
#
# Returns: Remaining whole days before reauthentication should complete.
# ------------------------------------------------------------------------------
def reauth_days_left(LAST_AUTH_UTC: str, INTERVAL_DAYS: int) -> int:
    LAST_AUTH = parse_iso(LAST_AUTH_UTC).astimezone(now_local().tzinfo)
    ELAPSED = now_local() - LAST_AUTH
    REMAINING = timedelta(days=INTERVAL_DAYS) - ELAPSED
    return max(0, int(REMAINING.total_seconds() // 86400))


# ------------------------------------------------------------------------------
# This function executes authentication and persists updated auth state.
#
# 1. "CLIENT" is iCloud client wrapper.
# 2. "AUTH_STATE" is current auth state.
# 3. "AUTH_STATE_PATH" is auth state file path.
# 4. "TELEGRAM" is Telegram integration configuration.
# 5. "USERNAME" is command prefix used by Telegram control.
# 6. "PROVIDED_CODE" is optional MFA code.
#
# Returns: Tuple "(new_state, is_authenticated, details_message)".
# ------------------------------------------------------------------------------
def attempt_auth(
    CLIENT: object,
    AUTH_STATE: AuthState,
    AUTH_STATE_PATH: Path,
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    APPLE_ID: str,
    PROVIDED_CODE: str,
    NOW_ISO_FN=now_iso,
    SAVE_AUTH_STATE_FN=save_auth_state,
    NOTIFY_FN=notify,
) -> tuple[AuthState, bool, str]:
    CODE = PROVIDED_CODE.strip()
    APPLE_ID_LABEL = format_apple_id_label(APPLE_ID)

    if CODE:
        IS_SUCCESS, DETAILS = CLIENT.complete_authentication(CODE)
    else:
        IS_SUCCESS, DETAILS = CLIENT.start_authentication()

    if IS_SUCCESS:
        NEW_STATE = AuthState(
            last_auth_utc=NOW_ISO_FN(),
            auth_pending=False,
            reauth_pending=False,
            reminder_stage="none",
        )
        SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
        NOTIFY_FN(
            TELEGRAM,
            build_authentication_complete_message(APPLE_ID_LABEL, DETAILS),
        )
        return NEW_STATE, True, DETAILS

    if "Two-factor code is required" in DETAILS:
        NEW_STATE = replace(AUTH_STATE, auth_pending=True)
        SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
        NOTIFY_FN(
            TELEGRAM,
            build_authentication_required_message(APPLE_ID_LABEL, USERNAME),
        )
        return NEW_STATE, False, DETAILS

    NEW_STATE = replace(AUTH_STATE, auth_pending=True)
    SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
    NOTIFY_FN(
        TELEGRAM,
        build_authentication_failed_message(APPLE_ID_LABEL, DETAILS),
    )
    return NEW_STATE, False, DETAILS


# ------------------------------------------------------------------------------
# This function applies 5-day and 2-day reauthentication reminder stages.
#
# 1. "AUTH_STATE" is current auth state.
# 2. "AUTH_STATE_PATH" is persistence file path.
# 3. "TELEGRAM" is Telegram integration configuration.
# 4. "USERNAME" is command prefix.
# 5. "INTERVAL_DAYS" is reauthentication interval in days.
#
# Returns: Updated authentication state.
# ------------------------------------------------------------------------------
def process_reauth_reminders(
    AUTH_STATE: AuthState,
    AUTH_STATE_PATH: Path,
    TELEGRAM: TelegramConfig,
    USERNAME: str,
    INTERVAL_DAYS: int,
    REAUTH_DAYS_LEFT_FN=reauth_days_left,
    SAVE_AUTH_STATE_FN=save_auth_state,
    NOTIFY_FN=notify,
) -> AuthState:
    DAYS_LEFT = REAUTH_DAYS_LEFT_FN(AUTH_STATE.last_auth_utc, INTERVAL_DAYS)

    if DAYS_LEFT > 5:
        NEW_STATE = replace(AUTH_STATE, reminder_stage="none", reauth_pending=False)
        if NEW_STATE != AUTH_STATE:
            SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 2 and AUTH_STATE.reminder_stage != "prompt2":
        NOTIFY_FN(
            TELEGRAM,
            build_reauthentication_required_message(USERNAME),
        )
        NEW_STATE = replace(AUTH_STATE, reminder_stage="prompt2", reauth_pending=True)
        SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    if DAYS_LEFT <= 5 and AUTH_STATE.reminder_stage == "none":
        NOTIFY_FN(
            TELEGRAM,
            build_reauth_reminder_message(USERNAME),
        )
        NEW_STATE = replace(AUTH_STATE, reminder_stage="alert5")
        SAVE_AUTH_STATE_FN(AUTH_STATE_PATH, NEW_STATE)
        return NEW_STATE

    return AUTH_STATE
