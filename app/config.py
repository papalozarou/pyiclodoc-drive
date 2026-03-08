"""This module centralises environment-driven settings for the iCloud backup worker."""

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AppConfig:
    """This data class holds validated configuration values used across worker code."""

    container_username: str
    icloud_email: str
    icloud_password: str
    telegram_bot_token: str
    telegram_chat_id: str
    keychain_service_name: str
    backup_interval_minutes: int
    startup_delay_seconds: int
    reauth_interval_days: int
    output_dir: Path
    config_dir: Path
    logs_dir: Path
    manifest_path: Path
    auth_state_path: Path
    heartbeat_path: Path
    cookie_dir: Path
    session_dir: Path
    icloudpd_compat_dir: Path
    safety_net_sample_size: int


# ------------------------------------------------------------------------------
# This function reads an environment variable with default fallback.
#
# 1. `NAME` is the environment key.
# 2. `DEFAULT` is returned when the key is unset.
#
# The function returns a stripped string suitable for configuration.
# ------------------------------------------------------------------------------
def env_value(NAME: str, DEFAULT: str = "") -> str:
    return os.getenv(NAME, DEFAULT).strip()


# ------------------------------------------------------------------------------
# This function parses an environment variable as an integer.
#
# 1. `NAME` is the environment key.
# 2. `DEFAULT` is used when parsing fails.
#
# The function returns the parsed integer or fallback default.
# ------------------------------------------------------------------------------
def env_int(NAME: str, DEFAULT: int) -> int:
    RAW_VALUE = env_value(NAME, str(DEFAULT))

    if RAW_VALUE.isdigit():
        return int(RAW_VALUE)

    return DEFAULT


# ------------------------------------------------------------------------------
# This function ensures a directory exists before the worker starts.
#
# 1. `PATH` is the directory path to create when missing.
#
# The function returns the same `Path` instance.
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

    return AppConfig(
        container_username=env_value("CONTAINER_USERNAME", "icloudbot"),
        icloud_email=env_value("ICLOUD_EMAIL"),
        icloud_password=env_value("ICLOUD_PASSWORD"),
        telegram_bot_token=env_value("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env_value("TELEGRAM_CHAT_ID"),
        keychain_service_name=env_value("KEYCHAIN_SERVICE_NAME", "icloud-drive-backup"),
        backup_interval_minutes=env_int("BACKUP_INTERVAL_MINUTES", 720),
        startup_delay_seconds=env_int("STARTUP_DELAY_SECONDS", 0),
        reauth_interval_days=env_int("REAUTH_INTERVAL_DAYS", 30),
        output_dir=OUTPUT_DIR,
        config_dir=CONFIG_DIR,
        logs_dir=LOGS_DIR,
        manifest_path=CONFIG_DIR / "manifest.json",
        auth_state_path=CONFIG_DIR / "auth_state.json",
        heartbeat_path=LOGS_DIR / "heartbeat.txt",
        cookie_dir=COOKIE_DIR,
        session_dir=SESSION_DIR,
        icloudpd_compat_dir=COMPAT_DIR,
        safety_net_sample_size=env_int("SAFETY_NET_SAMPLE_SIZE", 200),
    )
