# ------------------------------------------------------------------------------
# This module centralises environment-driven settings for the iCloud backup
# worker.
# ------------------------------------------------------------------------------

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Optional


# ------------------------------------------------------------------------------
# This data class holds validated configuration values used across worker code.
#
# N.B.
# Reused runtime paths live here so the rest of the worker refers to canonical
# locations rather than rebuilding path strings inline.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    container_username: str
    icloud_email: str
    icloud_password: str
    telegram_bot_token: str
    telegram_chat_id: str
    keychain_service_name: str
    run_once: bool
    schedule_mode: str
    schedule_backup_time: str
    schedule_weekdays: str
    schedule_monthly_week: str
    schedule_interval_minutes: int
    backup_delete_removed: bool
    traversal_workers: int
    sync_workers: int
    download_chunk_mib: int
    reauth_interval_days: int
    output_dir: Path
    config_dir: Path
    logs_dir: Path
    manifest_path: Path
    auth_state_path: Path
    heartbeat_path: Path
    safety_net_done_path: Path
    safety_net_blocked_path: Path
    cookie_dir: Path
    session_dir: Path
    icloudpd_compat_dir: Path
    safety_net_sample_size: int
    config_parse_errors: tuple[str, ...] = field(default_factory=tuple)

    # --------------------------------------------------------------------------
    # This property returns the canonical worker log file path.
    #
    # Returns: Worker log file path derived from "logs_dir".
    #
    # N.B.
    # This keeps worker-log path ownership alongside the other generated runtime
    # paths such as manifest, auth-state, heartbeat, and safety-net markers.
    # --------------------------------------------------------------------------
    @property
    def worker_log_path(self) -> Path:
        return self.logs_dir / "pyiclodoc-drive-worker.log"


# ------------------------------------------------------------------------------
# This function reads an environment variable with default fallback.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is returned when the key is unset.
#
# The function returns a stripped string suitable for configuration.
# ------------------------------------------------------------------------------
def env_value(NAME: str, DEFAULT: str = "") -> str:
    return os.getenv(NAME, DEFAULT).strip()


# ------------------------------------------------------------------------------
# This function parses an environment variable as an integer.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is used when parsing fails.
#
# The function returns the parsed integer or fallback default.
# ------------------------------------------------------------------------------
def env_int(NAME: str, DEFAULT: int) -> int:
    VALUE, _ = parse_env_int(NAME, DEFAULT)
    return VALUE


# ------------------------------------------------------------------------------
# This function parses an environment variable as an integer with error detail.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is used when parsing fails.
#
# The function returns the parsed integer and an optional validation message.
# ------------------------------------------------------------------------------
def parse_env_int(NAME: str, DEFAULT: int) -> tuple[int, Optional[str]]:
    RAW_VALUE = env_value(NAME, str(DEFAULT))

    try:
        return int(RAW_VALUE), None
    except ValueError:
        return DEFAULT, f'{NAME} must be an integer. Received "{RAW_VALUE}".'


# ------------------------------------------------------------------------------
# This function parses transfer worker count with "auto" fallback support.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is used when the value is unset or invalid.
#
# The function returns 0 for "auto" mode, otherwise a positive integer and an
# optional validation message.
# ------------------------------------------------------------------------------
def parse_env_workers(NAME: str, DEFAULT: int = 0) -> tuple[int, Optional[str]]:
    RAW_VALUE = env_value(NAME, "auto")
    NORMALISED = RAW_VALUE.lower()

    if NORMALISED in {"", "auto"}:
        return DEFAULT, None

    VALUE, ERROR = parse_env_int(NAME, DEFAULT)
    if ERROR is not None:
        return DEFAULT, f'{NAME} must be "auto" or a positive integer. Received "{RAW_VALUE}".'

    if VALUE > 0:
        return VALUE, None

    return DEFAULT, f'{NAME} must be "auto" or a positive integer. Received "{RAW_VALUE}".'


# ------------------------------------------------------------------------------
# This function parses transfer worker count with "auto" fallback support.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is used when the value is unset or invalid.
#
# The function returns 0 for "auto" mode, otherwise a positive integer.
# ------------------------------------------------------------------------------
def env_workers(NAME: str, DEFAULT: int = 0) -> int:
    VALUE, _ = parse_env_workers(NAME, DEFAULT)
    return VALUE


# ------------------------------------------------------------------------------
# This function parses an environment variable as a boolean.
#
# 1. "NAME" is the environment key.
# 2. "DEFAULT" is used when the value is unset or unrecognised.
#
# The function returns parsed boolean intent from common true/false tokens.
# ------------------------------------------------------------------------------
def env_bool(NAME: str, DEFAULT: bool) -> bool:
    RAW_VALUE = env_value(NAME).lower()

    if RAW_VALUE in {"1", "true", "yes", "on"}:
        return True

    if RAW_VALUE in {"0", "false", "no", "off"}:
        return False

    return DEFAULT


# ------------------------------------------------------------------------------
# This function ensures a directory exists before the worker starts.
#
# 1. "PATH" is the directory path to create when missing.
#
# The function returns the same "Path" instance.
# ------------------------------------------------------------------------------
def ensure_dir(PATH: Path) -> Path:
    PATH.mkdir(parents=True, exist_ok=True)
    return PATH


# ------------------------------------------------------------------------------
# This function builds the immutable runtime configuration object.
#
# N.B.
# Docker env and secrets conventions are documented at:
# https://docs.docker.com/compose/how-tos/use-secrets/
# ------------------------------------------------------------------------------
def load_config() -> AppConfig:
    CONFIG_DIR = ensure_dir(Path(env_value("CONFIG_DIR", "/config")))
    OUTPUT_DIR = ensure_dir(Path(env_value("OUTPUT_DIR", "/output")))
    LOGS_DIR = ensure_dir(Path(env_value("LOGS_DIR", "/logs")))
    COOKIE_DIR = ensure_dir(Path(env_value("COOKIE_DIR", "/config/cookies")))
    SESSION_DIR = ensure_dir(Path(env_value("SESSION_DIR", "/config/session")))
    COMPAT_DIR = ensure_dir(Path(env_value("ICLOUDPD_COMPAT_DIR", "/config/icloudpd")))
    CONFIG_PARSE_ERRORS: list[str] = []

    SCHEDULE_INTERVAL_MINUTES, SCHEDULE_INTERVAL_ERROR = parse_env_int(
        "SCHEDULE_INTERVAL_MINUTES",
        1440,
    )
    TRAVERSAL_WORKERS, TRAVERSAL_WORKERS_ERROR = parse_env_int(
        "SYNC_TRAVERSAL_WORKERS",
        1,
    )
    SYNC_WORKERS, SYNC_WORKERS_ERROR = parse_env_workers("SYNC_DOWNLOAD_WORKERS", 0)
    DOWNLOAD_CHUNK_MIB, DOWNLOAD_CHUNK_ERROR = parse_env_int(
        "SYNC_DOWNLOAD_CHUNK_MIB",
        4,
    )
    REAUTH_INTERVAL_DAYS, REAUTH_INTERVAL_ERROR = parse_env_int(
        "REAUTH_INTERVAL_DAYS",
        30,
    )
    SAFETY_NET_SAMPLE_SIZE, SAFETY_NET_SAMPLE_ERROR = parse_env_int(
        "SAFETY_NET_SAMPLE_SIZE",
        200,
    )

    for ERROR in (
        SCHEDULE_INTERVAL_ERROR,
        TRAVERSAL_WORKERS_ERROR,
        SYNC_WORKERS_ERROR,
        DOWNLOAD_CHUNK_ERROR,
        REAUTH_INTERVAL_ERROR,
        SAFETY_NET_SAMPLE_ERROR,
    ):
        if ERROR is not None:
            CONFIG_PARSE_ERRORS.append(ERROR)

    return AppConfig(
        container_username=env_value("CONTAINER_USERNAME", "icloudbot"),
        icloud_email=env_value("ICLOUD_EMAIL"),
        icloud_password=env_value("ICLOUD_PASSWORD"),
        telegram_bot_token=env_value("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env_value("TELEGRAM_CHAT_ID"),
        keychain_service_name=env_value("KEYCHAIN_SERVICE_NAME", "icloud-drive-backup"),
        run_once=env_bool("RUN_ONCE", False),
        schedule_mode=env_value("SCHEDULE_MODE", "interval").lower(),
        schedule_backup_time=env_value("SCHEDULE_BACKUP_TIME", "02:00"),
        schedule_weekdays=env_value("SCHEDULE_WEEKDAYS", "monday").lower(),
        schedule_monthly_week=env_value("SCHEDULE_MONTHLY_WEEK", "first").lower(),
        schedule_interval_minutes=SCHEDULE_INTERVAL_MINUTES,
        backup_delete_removed=env_bool("BACKUP_DELETE_REMOVED", False),
        traversal_workers=TRAVERSAL_WORKERS,
        sync_workers=SYNC_WORKERS,
        download_chunk_mib=DOWNLOAD_CHUNK_MIB,
        reauth_interval_days=REAUTH_INTERVAL_DAYS,
        output_dir=OUTPUT_DIR,
        config_dir=CONFIG_DIR,
        logs_dir=LOGS_DIR,
        manifest_path=CONFIG_DIR / "pyiclodoc-drive-manifest.json",
        auth_state_path=CONFIG_DIR / "pyiclodoc-drive-auth_state.json",
        heartbeat_path=LOGS_DIR / "pyiclodoc-drive-heartbeat.txt",
        safety_net_done_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_done.flag",
        safety_net_blocked_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_blocked.flag",
        cookie_dir=COOKIE_DIR,
        session_dir=SESSION_DIR,
        icloudpd_compat_dir=COMPAT_DIR,
        safety_net_sample_size=SAFETY_NET_SAMPLE_SIZE,
        config_parse_errors=tuple(CONFIG_PARSE_ERRORS),
    )
