# ------------------------------------------------------------------------------
# This test module verifies iCloud client auth, traversal, and download helpers.
# ------------------------------------------------------------------------------

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.config import AppConfig
from app.icloud_client import ICloudDriveClient


# ------------------------------------------------------------------------------
# This function creates an "AppConfig" fixture for iCloud client tests.
# ------------------------------------------------------------------------------
def build_config_for_icloud(TMPDIR: str) -> AppConfig:
    ROOT_DIR = Path(TMPDIR)
    CONFIG_DIR = ROOT_DIR / "config"
    OUTPUT_DIR = ROOT_DIR / "output"
    LOGS_DIR = ROOT_DIR / "logs"
    COOKIE_DIR = CONFIG_DIR / "cookies"
    SESSION_DIR = CONFIG_DIR / "session"
    COMPAT_DIR = CONFIG_DIR / "icloudpd"

    for DIR_PATH in [CONFIG_DIR, OUTPUT_DIR, LOGS_DIR, COOKIE_DIR, SESSION_DIR]:
        DIR_PATH.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        container_username="alice",
        icloud_email="alice@example.com",
        icloud_password="password",
        telegram_bot_token="token",
        telegram_chat_id="12345",
        keychain_service_name="icloud-drive-backup",
        run_once=False,
        schedule_mode="interval",
        backup_daily_time="02:00",
        schedule_weekdays="monday",
        schedule_monthly_week="first",
        backup_interval_minutes=60,
        reauth_interval_days=30,
        output_dir=OUTPUT_DIR,
        config_dir=CONFIG_DIR,
        logs_dir=LOGS_DIR,
        manifest_path=CONFIG_DIR / "manifest.json",
        auth_state_path=CONFIG_DIR / "auth_state.json",
        heartbeat_path=LOGS_DIR / "heartbeat.txt",
        cookie_dir=COOKIE_DIR,
        session_dir=SESSION_DIR,
        icloudpd_compat_dir=COMPAT_DIR,
        safety_net_sample_size=200,
    )


# ------------------------------------------------------------------------------
# This fake node supports "dir()" metadata and dict-style child lookup.
# ------------------------------------------------------------------------------
class FakeNode(dict):
    def __init__(self, PAYLOAD, CHILDREN=None):
        super().__init__(CHILDREN or {})
        self._payload = PAYLOAD

    def dir(self):
        return self._payload


# ------------------------------------------------------------------------------
# These tests validate iCloud client compatibility-path behaviour.
# ------------------------------------------------------------------------------
class TestICloudClientCompat(unittest.TestCase):
    def test_prepare_compat_paths_creates_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            CLIENT.prepare_compat_paths()

            COOKIE_LINK = CONFIG.icloudpd_compat_dir / "cookies"
            SESSION_LINK = CONFIG.icloudpd_compat_dir / "session"
            self.assertTrue(COOKIE_LINK.is_symlink())
            self.assertTrue(SESSION_LINK.is_symlink())

    def test_replace_path_with_symlink_handles_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            LINK_PATH = CONFIG.icloudpd_compat_dir / "cookies"
            LINK_PATH.mkdir(parents=True, exist_ok=True)

            CLIENT._replace_path_with_symlink(LINK_PATH, CONFIG.cookie_dir)

            self.assertTrue(LINK_PATH.is_symlink())


# ------------------------------------------------------------------------------
# These tests validate authentication and 2FA handling branches.
# ------------------------------------------------------------------------------
class TestICloudClientAuth(unittest.TestCase):
    def test_authenticate_success_without_2fa(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock(requires_2fa=False, requires_2sa=False)

            with patch("app.icloud_client.PyiCloudService", return_value=API) as SERVICE:
                IS_AUTHENTICATED, DETAILS = CLIENT.authenticate(lambda: "")

            self.assertTrue(IS_AUTHENTICATED)
            self.assertIn("Authenticated successfully", DETAILS)
            SERVICE.assert_called_once()

    def test_authenticate_two_step_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock(requires_2fa=False, requires_2sa=True)

            with patch("app.icloud_client.PyiCloudService", return_value=API):
                IS_AUTHENTICATED, DETAILS = CLIENT.authenticate(lambda: "")

            self.assertFalse(IS_AUTHENTICATED)
            self.assertIn("Two-step authentication is required", DETAILS)

    def test_handle_2fa_paths(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            CLIENT.api = None
            self.assertEqual(CLIENT._handle_2fa(lambda: ""), (False, "Authentication state unavailable."))

            API = Mock()
            API.validate_2fa_code.return_value = False
            CLIENT.api = API
            self.assertEqual(CLIENT._handle_2fa(lambda: "123456"), (False, "Two-factor code was rejected by Apple."))

            API.validate_2fa_code.return_value = True
            API.is_trusted_session = True
            self.assertEqual(CLIENT._handle_2fa(lambda: "123456"), (True, "Authenticated successfully with 2FA."))

            API.is_trusted_session = False
            self.assertEqual(
                CLIENT._handle_2fa(lambda: "123456"),
                (True, "Authenticated successfully with trusted 2FA session."),
            )
            API.trust_session.assert_called()


# ------------------------------------------------------------------------------
# These tests validate traversal, listing, and remote entry construction.
# ------------------------------------------------------------------------------
class TestICloudClientTraversal(unittest.TestCase):
    def test_list_entries_returns_empty_when_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            self.assertEqual(CLIENT.list_entries(), [])

    def test_list_entries_walks_directories_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            CHILD_NODE = FakeNode({"dirs": [], "files": [{"name": "inner.txt", "size": 3, "dateModified": "d2"}]})
            ROOT_NODE = FakeNode(
                {
                    "dirs": [{"name": "docs", "dateModified": "d1"}],
                    "files": [{"name": "root.txt", "size": 2, "dateModified": "d0"}],
                },
                {"docs": CHILD_NODE},
            )
            CLIENT.api = Mock(drive=ROOT_NODE)

            ENTRIES = CLIENT.list_entries()
            PATHS = sorted(ENTRY.path for ENTRY in ENTRIES)
            self.assertEqual(PATHS, ["docs", "docs/inner.txt", "root.txt"])

    def test_node_dir_and_child_node_error_paths(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            BROKEN_NODE = Mock()
            BROKEN_NODE.dir.side_effect = ValueError("bad")
            self.assertEqual(CLIENT._node_dir(BROKEN_NODE), {"dirs": [], "files": []})
            self.assertIsNone(CLIENT._child_node({}, "missing"))


# ------------------------------------------------------------------------------
# These tests validate download-path resolution and local write helpers.
# ------------------------------------------------------------------------------
class TestICloudClientDownloads(unittest.TestCase):
    def test_download_file_requires_authenticated_api(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESULT = CLIENT.download_file("docs/file.txt", Path(TMPDIR) / "out.txt")
            self.assertFalse(RESULT)

    def test_resolve_file_object_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            FILE_NODE = Mock()
            ROOT = {"docs": {"file.txt": FILE_NODE}}
            CLIENT.api = Mock(drive=ROOT)

            self.assertIs(CLIENT._resolve_file_object("docs/file.txt"), FILE_NODE)
            self.assertIsNone(CLIENT._resolve_file_object("docs/missing.txt"))

    def test_download_file_success_with_iter_content(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = Mock()
            RESPONSE = Mock()
            RESPONSE.iter_content.return_value = [b"abc", b"", b"def"]
            FILE_NODE.download.return_value = RESPONSE

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "file.bin"
                RESULT = CLIENT.download_file("docs/file.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"abcdef")

    def test_download_file_success_with_raw_stream(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = Mock()
            RESPONSE = SimpleNamespace(raw=BytesIO(b"raw-data"))
            FILE_NODE.download.return_value = RESPONSE

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "raw.bin"
                RESULT = CLIENT.download_file("docs/raw.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"raw-data")

    def test_download_file_handles_download_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = Mock()
            FILE_NODE.download.side_effect = RuntimeError("boom")
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                RESULT = CLIENT.download_file("docs/file.bin", Path(TMPDIR) / "file.bin")

            self.assertFalse(RESULT)

    def test_write_downloaded_content_rejects_missing_raw(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESULT = CLIENT._write_downloaded_content(object(), Path(TMPDIR) / "x.bin")
            self.assertFalse(RESULT)

    def test_cleanup_temporary_file_ignores_unlink_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            TEMP_PATH = Path(TMPDIR) / ".x.partial"
            TEMP_PATH.write_text("x", encoding="utf-8")

            with patch.object(Path, "unlink", side_effect=OSError("denied")):
                CLIENT._cleanup_temporary_file(TEMP_PATH)


if __name__ == "__main__":
    unittest.main()
