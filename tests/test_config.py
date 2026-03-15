# ------------------------------------------------------------------------------
# This test module validates environment-driven config loading behaviour.
# ------------------------------------------------------------------------------

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.config import load_config
from app.config_validation import validate_config


# ------------------------------------------------------------------------------
# This function builds baseline test environment values for writable paths.
# ------------------------------------------------------------------------------
def build_base_env(TMPDIR: str) -> dict[str, str]:
    ROOT_DIR = Path(TMPDIR)
    return {
        "CONFIG_DIR": str(ROOT_DIR / "config"),
        "OUTPUT_DIR": str(ROOT_DIR / "output"),
        "LOGS_DIR": str(ROOT_DIR / "logs"),
        "COOKIE_DIR": str(ROOT_DIR / "config" / "cookies"),
        "SESSION_DIR": str(ROOT_DIR / "config" / "session"),
        "ICLOUDPD_COMPAT_DIR": str(ROOT_DIR / "config" / "icloudpd"),
    }


# ------------------------------------------------------------------------------
# This test class validates full config loading defaults and parsing rules.
# ------------------------------------------------------------------------------
class TestConfigLoad(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms defaults are applied for unset optional values.
# --------------------------------------------------------------------------
    def test_load_config_uses_expected_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(os.environ, BASE_ENV, clear=True):
                CONFIG = load_config()

        self.assertEqual(CONFIG.container_username, "icloudbot")
        self.assertEqual(CONFIG.keychain_service_name, "icloud-drive-backup")
        self.assertEqual(CONFIG.schedule_mode, "interval")
        self.assertEqual(CONFIG.schedule_backup_time, "02:00")
        self.assertEqual(CONFIG.schedule_weekdays, "monday")
        self.assertEqual(CONFIG.schedule_monthly_week, "first")
        self.assertEqual(CONFIG.schedule_interval_minutes, 1440)
        self.assertFalse(CONFIG.backup_delete_removed)
        self.assertEqual(CONFIG.traversal_workers, 1)
        self.assertEqual(CONFIG.sync_workers, 0)
        self.assertEqual(CONFIG.download_chunk_mib, 4)
        self.assertEqual(CONFIG.reauth_interval_days, 30)
        self.assertEqual(CONFIG.safety_net_sample_size, 200)
        self.assertFalse(CONFIG.run_once)
        self.assertEqual(CONFIG.icloud_email, "")
        self.assertEqual(CONFIG.icloud_password, "")
        self.assertEqual(CONFIG.telegram_bot_token, "")
        self.assertEqual(CONFIG.telegram_chat_id, "")

# --------------------------------------------------------------------------
# This test confirms environment values override defaults as expected.
# --------------------------------------------------------------------------
    def test_load_config_applies_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            OVERRIDES = {
                "CONTAINER_USERNAME": "alice",
                "ICLOUD_EMAIL": "alice@example.com",
                "ICLOUD_PASSWORD": "secret",
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "12345",
                "KEYCHAIN_SERVICE_NAME": "custom-service",
                "RUN_ONCE": "true",
                "SCHEDULE_MODE": "WEEKLY",
                "SCHEDULE_BACKUP_TIME": "06:30",
                "SCHEDULE_WEEKDAYS": "Thursday",
                "SCHEDULE_MONTHLY_WEEK": "LAST",
                "SCHEDULE_INTERVAL_MINUTES": "90",
                "BACKUP_DELETE_REMOVED": "true",
                "SYNC_TRAVERSAL_WORKERS": "4",
                "SYNC_DOWNLOAD_WORKERS": "12",
                "SYNC_DOWNLOAD_CHUNK_MIB": "8",
                "REAUTH_INTERVAL_DAYS": "45",
                "SAFETY_NET_SAMPLE_SIZE": "300",
            }
            with patch.dict(os.environ, BASE_ENV | OVERRIDES, clear=True):
                CONFIG = load_config()

        self.assertEqual(CONFIG.container_username, "alice")
        self.assertEqual(CONFIG.icloud_email, "alice@example.com")
        self.assertEqual(CONFIG.icloud_password, "secret")
        self.assertEqual(CONFIG.telegram_bot_token, "token")
        self.assertEqual(CONFIG.telegram_chat_id, "12345")
        self.assertEqual(CONFIG.keychain_service_name, "custom-service")
        self.assertTrue(CONFIG.run_once)
        self.assertEqual(CONFIG.schedule_mode, "weekly")
        self.assertEqual(CONFIG.schedule_backup_time, "06:30")
        self.assertEqual(CONFIG.schedule_weekdays, "thursday")
        self.assertEqual(CONFIG.schedule_monthly_week, "last")
        self.assertEqual(CONFIG.schedule_interval_minutes, 90)
        self.assertTrue(CONFIG.backup_delete_removed)
        self.assertEqual(CONFIG.traversal_workers, 4)
        self.assertEqual(CONFIG.sync_workers, 12)
        self.assertEqual(CONFIG.download_chunk_mib, 8)
        self.assertEqual(CONFIG.reauth_interval_days, 45)
        self.assertEqual(CONFIG.safety_net_sample_size, 300)

# --------------------------------------------------------------------------
# This test confirms invalid integer values fall back to defaults.
# --------------------------------------------------------------------------
    def test_load_config_falls_back_for_invalid_int_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            INVALIDS = {
                "SCHEDULE_INTERVAL_MINUTES": "abc",
                "SYNC_TRAVERSAL_WORKERS": "zero",
                "SYNC_DOWNLOAD_WORKERS": "many",
                "SYNC_DOWNLOAD_CHUNK_MIB": "huge",
                "SAFETY_NET_SAMPLE_SIZE": "10.5",
            }
            with patch.dict(os.environ, BASE_ENV | INVALIDS, clear=True):
                CONFIG = load_config()

        self.assertEqual(CONFIG.schedule_interval_minutes, 1440)
        self.assertEqual(CONFIG.traversal_workers, 1)
        self.assertEqual(CONFIG.sync_workers, 0)
        self.assertEqual(CONFIG.download_chunk_mib, 4)
        self.assertEqual(CONFIG.reauth_interval_days, 30)
        self.assertEqual(CONFIG.safety_net_sample_size, 200)
        self.assertEqual(
            CONFIG.config_parse_errors,
            (
                'SCHEDULE_INTERVAL_MINUTES must be an integer. Received "abc".',
                'SYNC_TRAVERSAL_WORKERS must be an integer. Received "zero".',
                'SYNC_DOWNLOAD_WORKERS must be "auto" or a positive integer. Received "many".',
                'SYNC_DOWNLOAD_CHUNK_MIB must be an integer. Received "huge".',
                'SAFETY_NET_SAMPLE_SIZE must be an integer. Received "10.5".',
            ),
        )

# --------------------------------------------------------------------------
# This test confirms signed integer env values are parsed before range checks.
# --------------------------------------------------------------------------
    def test_load_config_parses_signed_integer_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(
                os.environ,
                BASE_ENV | {"REAUTH_INTERVAL_DAYS": "-1", "SYNC_DOWNLOAD_WORKERS": "-2"},
                clear=True,
            ):
                CONFIG = load_config()

        self.assertEqual(CONFIG.reauth_interval_days, -1)
        self.assertEqual(CONFIG.sync_workers, 0)
        self.assertEqual(
            CONFIG.config_parse_errors,
            ('SYNC_DOWNLOAD_WORKERS must be "auto" or a positive integer. Received "-2".',),
        )

# --------------------------------------------------------------------------
# This test confirms invalid integer parse messages are surfaced by runtime
# config validation.
# --------------------------------------------------------------------------
    def test_validate_config_includes_parse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(
                os.environ,
                BASE_ENV | {"SCHEDULE_INTERVAL_MINUTES": "abc"},
                clear=True,
            ):
                CONFIG = load_config()

        ERRORS = validate_config(CONFIG)
        self.assertIn('SCHEDULE_INTERVAL_MINUTES must be an integer. Received "abc".', ERRORS)

# --------------------------------------------------------------------------
# This test confirms signed values are range-checked after successful parse.
# --------------------------------------------------------------------------
    def test_validate_config_rejects_out_of_range_signed_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(
                os.environ,
                BASE_ENV | {"REAUTH_INTERVAL_DAYS": "-1", "SAFETY_NET_SAMPLE_SIZE": "0"},
                clear=True,
            ):
                CONFIG = load_config()

        ERRORS = validate_config(CONFIG)
        self.assertIn("REAUTH_INTERVAL_DAYS must be an integer of at least 1.", ERRORS)
        self.assertIn("SAFETY_NET_SAMPLE_SIZE must be an integer of at least 1.", ERRORS)

# --------------------------------------------------------------------------
# This test confirms sync worker auto mode parses to zero override.
# --------------------------------------------------------------------------
    def test_load_config_sync_workers_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(os.environ, BASE_ENV | {"SYNC_DOWNLOAD_WORKERS": "auto"}, clear=True):
                CONFIG = load_config()

        self.assertEqual(CONFIG.sync_workers, 0)

# --------------------------------------------------------------------------
# This test confirms unrecognised booleans fall back to default behaviour.
# --------------------------------------------------------------------------
    def test_load_config_falls_back_for_invalid_bool_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(
                os.environ,
                BASE_ENV | {"RUN_ONCE": "maybe", "BACKUP_DELETE_REMOVED": "sometimes"},
                clear=True,
            ):
                CONFIG = load_config()

        self.assertFalse(CONFIG.run_once)
        self.assertFalse(CONFIG.backup_delete_removed)

# --------------------------------------------------------------------------
# This test confirms config-derived paths are created and wired correctly.
# --------------------------------------------------------------------------
    def test_load_config_builds_expected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE_ENV = build_base_env(TMPDIR)
            with patch.dict(os.environ, BASE_ENV, clear=True):
                CONFIG = load_config()

                self.assertEqual(CONFIG.config_dir, Path(BASE_ENV["CONFIG_DIR"]))
                self.assertEqual(CONFIG.output_dir, Path(BASE_ENV["OUTPUT_DIR"]))
                self.assertEqual(CONFIG.logs_dir, Path(BASE_ENV["LOGS_DIR"]))
                self.assertEqual(CONFIG.cookie_dir, Path(BASE_ENV["COOKIE_DIR"]))
                self.assertEqual(CONFIG.session_dir, Path(BASE_ENV["SESSION_DIR"]))
                self.assertEqual(CONFIG.icloudpd_compat_dir, Path(BASE_ENV["ICLOUDPD_COMPAT_DIR"]))
                self.assertEqual(CONFIG.manifest_path, Path(BASE_ENV["CONFIG_DIR"]) / "pyiclodoc-drive-manifest.json")
                self.assertEqual(CONFIG.auth_state_path, Path(BASE_ENV["CONFIG_DIR"]) / "pyiclodoc-drive-auth_state.json")
                self.assertEqual(CONFIG.heartbeat_path, Path(BASE_ENV["LOGS_DIR"]) / "pyiclodoc-drive-heartbeat.txt")
                self.assertEqual(CONFIG.worker_log_path, Path(BASE_ENV["LOGS_DIR"]) / "pyiclodoc-drive-worker.log")
                self.assertTrue(CONFIG.config_dir.exists())
                self.assertTrue(CONFIG.output_dir.exists())
                self.assertTrue(CONFIG.logs_dir.exists())
                self.assertTrue(CONFIG.cookie_dir.exists())
                self.assertTrue(CONFIG.session_dir.exists())
                self.assertTrue(CONFIG.icloudpd_compat_dir.exists())


if __name__ == "__main__":
    unittest.main()
