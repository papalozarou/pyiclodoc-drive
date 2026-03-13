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
        manifest_path=CONFIG_DIR / "iclouddd-manifest.json",
        auth_state_path=CONFIG_DIR / "iclouddd-auth_state.json",
        heartbeat_path=LOGS_DIR / "iclouddd-heartbeat.txt",
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
# This fake drive node emulates pyicloud child node attributes.
# ------------------------------------------------------------------------------
class FakeDriveChild:
    def __init__(self, NODE_TYPE: str, SIZE: int = 0, MODIFIED: str = ""):
        self.type = NODE_TYPE
        self.size = SIZE
        self.date_modified = MODIFIED

    def dir(self):
        if self.type == "folder":
            return []

        raise AttributeError("file node has no dir()")


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
    def test_create_service_uses_cookie_directory_only(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock()

            with patch("app.icloud_client.PyiCloudService", return_value=API) as SERVICE:
                RESULT = CLIENT._create_service()

            self.assertIs(RESULT, API)
            SERVICE.assert_called_once_with(
                CONFIG.icloud_email,
                CONFIG.icloud_password,
                cookie_directory=str(CONFIG.cookie_dir),
            )

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

    def test_complete_authentication_paths(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            CLIENT.api = None
            self.assertEqual(
                CLIENT.complete_authentication(""),
                (False, "Authentication session is not initialised."),
            )

            API = Mock()
            API.requires_2fa = True
            API.validate_2fa_code.return_value = False
            CLIENT.api = API
            self.assertEqual(
                CLIENT.complete_authentication("123456"),
                (False, "Two-factor code was rejected by Apple."),
            )

            API.validate_2fa_code.return_value = True
            API.is_trusted_session = True
            self.assertEqual(
                CLIENT.complete_authentication("123456"),
                (True, "Authenticated successfully with 2FA."),
            )

            API.is_trusted_session = False
            API.trust_session.return_value = True
            self.assertEqual(
                CLIENT.complete_authentication("123456"),
                (True, "Authenticated successfully with trusted 2FA session."),
            )
            API.trust_session.assert_called()

            API.trust_session.reset_mock()
            API.trust_session.return_value = False
            self.assertEqual(
                CLIENT.complete_authentication("123456"),
                (
                    False,
                    "Two-factor code was accepted, but Apple did not trust this session.",
                ),
            )
            API.trust_session.assert_called_once()


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

            CHILD_NODE = FakeNode(
                ["inner.txt"],
                {"inner.txt": FakeDriveChild("file", SIZE=3, MODIFIED="d2")},
            )
            ROOT_NODE = FakeNode(
                ["docs", "root.txt"],
                {
                    "docs": CHILD_NODE,
                    "root.txt": FakeDriveChild("file", SIZE=2, MODIFIED="d0"),
                },
            )
            CLIENT.api = Mock(drive=ROOT_NODE)

            ENTRIES = CLIENT.list_entries()
            PATHS = sorted(ENTRY.path for ENTRY in ENTRIES)
            self.assertEqual(PATHS, ["docs", "docs/inner.txt", "root.txt"])
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertGreater(STATS.get("directories_completed", 0), 0)
            self.assertEqual(STATS.get("directories_pending", 0), 0)
            self.assertEqual(STATS.get("workers_active", 0), 0)

    def test_list_entries_supports_name_list_payload(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            DOCS_NODE = FakeNode(
                ["inner.txt"],
                {"inner.txt": FakeDriveChild("file", SIZE=3, MODIFIED="d2")},
            )
            ROOT_NODE = FakeNode(
                ["docs", "root.txt"],
                {
                    "docs": DOCS_NODE,
                    "root.txt": FakeDriveChild("file", SIZE=2, MODIFIED="d0"),
                },
            )
            CLIENT.api = Mock(drive=ROOT_NODE)

            ENTRIES = CLIENT.list_entries()
            PATHS = sorted(ENTRY.path for ENTRY in ENTRIES)
            self.assertEqual(PATHS, ["docs", "docs/inner.txt", "root.txt"])

    def test_list_entries_uses_parallel_traversal_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CONFIG = AppConfig(**(CONFIG.__dict__ | {"traversal_workers": 3}))
            CLIENT = ICloudDriveClient(CONFIG)
            CLIENT.api = Mock(drive=FakeNode([]))

            with patch.object(CLIENT, "_walk_node_parallel", return_value=[]) as PARALLEL_WALK:
                CLIENT.list_entries()

            PARALLEL_WALK.assert_called_once()

    def test_list_entries_uses_serial_traversal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            CLIENT.api = Mock(drive=FakeNode([]))

            with patch.object(CLIENT, "_walk_node", return_value=[]) as SERIAL_WALK:
                CLIENT.list_entries()

            SERIAL_WALK.assert_called_once()

    def test_node_dir_and_child_node_error_paths(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            BROKEN_NODE = Mock()
            BROKEN_NODE.dir.side_effect = ValueError("bad")
            self.assertEqual(CLIENT._node_dir(BROKEN_NODE), {"dirs": [], "files": [], "names": []})
            self.assertIsNone(CLIENT._child_node({}, "missing"))

            FILE_CHILD = Mock()
            FILE_CHILD.dir.side_effect = NotADirectoryError("file.bin")
            self.assertFalse(CLIENT._child_is_dir(FILE_CHILD))

    def test_child_is_dir_prefers_directory_probe_for_open_capable_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            FOLDER_LIKE = Mock()
            FOLDER_LIKE.open = Mock()
            FOLDER_LIKE.dir.return_value = ["nested.bin"]

            self.assertTrue(CLIENT._child_is_dir(FOLDER_LIKE))

    def test_child_is_dir_records_non_directory_metric(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_LIKE = Mock()
            FILE_LIKE.dir.side_effect = NotADirectoryError("file.bin")

            self.assertFalse(CLIENT._child_is_dir(FILE_LIKE))
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS.get("dir_non_directory", 0), 1)
            self.assertEqual(STATS.get("dir_hard_failures", 0), 0)

    def test_child_is_dir_uses_explicit_false_folder_flags(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            FILE_LIKE = Mock(is_folder=False)
            FILE_LIKE.dir.side_effect = RuntimeError("should not run")

            self.assertFalse(CLIENT._child_is_dir(FILE_LIKE))

    def test_node_dir_returns_names_for_list_payload(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            NODE = FakeNode(["docs", "a.txt"])

            RESULT = CLIENT._node_dir(NODE)

            self.assertEqual(RESULT["names"], ["docs", "a.txt"])
            self.assertEqual(RESULT["dirs"], [])
            self.assertEqual(RESULT["files"], [])

    def test_node_dir_supports_items_payload(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            NODE = FakeNode(
                {
                    "items": [
                        {"name": "docs", "type": "folder", "dateModified": "m1"},
                        {"name": "a.txt", "size": 2, "modified": "m2"},
                    ]
                }
            )

            RESULT = CLIENT._node_dir(NODE)

            self.assertEqual(RESULT["names"], [])
            self.assertEqual(len(RESULT["dirs"]), 1)
            self.assertEqual(len(RESULT["files"]), 1)

    def test_node_dir_retries_transient_dir_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            NODE = Mock()
            NODE.dir.side_effect = [
                RuntimeError("transient"),
                RuntimeError("transient"),
                ["docs", "a.txt"],
            ]

            with patch("app.icloud_client.time.sleep") as SLEEP:
                RESULT = CLIENT._node_dir(NODE)

            self.assertEqual(RESULT["names"], ["docs", "a.txt"])
            self.assertEqual(NODE.dir.call_count, 3)
            self.assertEqual(SLEEP.call_count, 2)

    def test_node_dir_does_not_retry_non_retryable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            NODE = Mock()
            NODE.dir.side_effect = ValueError("bad payload")

            with patch("app.icloud_client.time.sleep") as SLEEP:
                RESULT = CLIENT._node_dir(NODE)

            self.assertEqual(RESULT, {"dirs": [], "files": [], "names": []})
            self.assertEqual(NODE.dir.call_count, 1)
            self.assertEqual(SLEEP.call_count, 0)

    def test_entries_from_files_supports_filename_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESULT = CLIENT._entries_from_files(
                "",
                [{"filename": "x.bin", "bytes": "9", "modified": "m"}],
            )

            self.assertEqual(len(RESULT), 1)
            self.assertEqual(RESULT[0].path, "x.bin")
            self.assertEqual(RESULT[0].size, 9)
            self.assertEqual(RESULT[0].modified, "m")


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
            self.assertEqual(CLIENT.get_last_download_failure_reason(), "not_authenticated")

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
            FILE_NODE = SimpleNamespace()
            RESPONSE = Mock()
            RESPONSE.iter_content.return_value = [b"abc", b"", b"def"]
            FILE_NODE.open = Mock(return_value=RESPONSE)

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "file.bin"
                RESULT = CLIENT.download_file("docs/file.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"abcdef")
            RESPONSE.iter_content.assert_called_once_with(chunk_size=4 * 1024 * 1024)

    def test_download_file_success_with_raw_stream(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
            RESPONSE = SimpleNamespace(raw=BytesIO(b"raw-data"))
            FILE_NODE.open = Mock(return_value=RESPONSE)

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "raw.bin"
                RESULT = CLIENT.download_file("docs/raw.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"raw-data")

    def test_download_file_handles_open_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
            FILE_NODE.open = Mock(side_effect=RuntimeError("boom"))
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                RESULT = CLIENT.download_file("docs/file.bin", Path(TMPDIR) / "file.bin")

            self.assertFalse(RESULT)
            self.assertEqual(CLIENT.get_last_download_failure_reason(), "open_failed")

    def test_download_file_rejects_directory_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            DIRECTORY_NODE = FakeNode([])
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=DIRECTORY_NODE):
                RESULT = CLIENT.download_file("docs/pkg.bundle", Path(TMPDIR) / "pkg.bundle")

            self.assertFalse(RESULT)
            self.assertEqual(CLIENT.get_last_download_failure_reason(), "directory_node")

    def test_download_package_tree_downloads_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_RESPONSE = Mock()
            FILE_RESPONSE.iter_content.return_value = [b"abc"]
            FILE_NODE = SimpleNamespace(open=Mock(return_value=FILE_RESPONSE))
            SUBDIR_NODE = FakeNode(["inner.txt"], {"inner.txt": FILE_NODE})
            ROOT_NODE = FakeNode(["data"], {"data": SUBDIR_NODE})
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=ROOT_NODE):
                RESULT = CLIENT.download_package_tree(
                    "docs/pkg.bundle",
                    Path(TMPDIR) / "pkg.bundle",
                )

            self.assertTrue(RESULT)
            self.assertEqual(
                (Path(TMPDIR) / "pkg.bundle" / "data" / "inner.txt").read_bytes(),
                b"abc",
            )

    def test_download_package_tree_fails_for_missing_child(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            ROOT_NODE = FakeNode(["missing.bin"], {})
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=ROOT_NODE):
                RESULT = CLIENT.download_package_tree(
                    "docs/pkg.bundle",
                    Path(TMPDIR) / "pkg.bundle",
                )

            self.assertFalse(RESULT)
            self.assertEqual(CLIENT.get_last_download_failure_reason(), "package_child_missing")

    def test_download_package_tree_uses_parent_metadata_for_non_directory_root(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            ROOT_NODE = SimpleNamespace()
            PARENT_NODE = FakeNode(
                {
                    "dirs": [],
                    "files": [
                        {
                            "name": "pkg.bundle",
                            "items": [
                                {
                                    "name": "inner.txt",
                                    "size": 3,
                                    "modified": "2026-03-12T10:15:30Z",
                                }
                            ],
                        }
                    ],
                },
                {"pkg.bundle": ROOT_NODE},
            )
            FILE_RESPONSE = Mock()
            FILE_RESPONSE.iter_content.return_value = [b"abc"]
            FILE_NODE = SimpleNamespace(open=Mock(return_value=FILE_RESPONSE))
            CLIENT.api = Mock()

            def resolve_side_effect(REMOTE_PATH: str):
                if REMOTE_PATH == "docs/pkg.bundle":
                    return ROOT_NODE
                if REMOTE_PATH == "docs":
                    return PARENT_NODE
                if REMOTE_PATH == "docs/pkg.bundle/inner.txt":
                    return FILE_NODE
                return None

            with patch.object(CLIENT, "_resolve_file_object", side_effect=resolve_side_effect):
                RESULT = CLIENT.download_package_tree(
                    "docs/pkg.bundle",
                    Path(TMPDIR) / "pkg.bundle",
                )

            self.assertTrue(RESULT)
            self.assertEqual(
                (Path(TMPDIR) / "pkg.bundle" / "inner.txt").read_bytes(),
                b"abc",
            )

    def test_download_file_falls_back_when_stream_keyword_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESPONSE = Mock()
            RESPONSE.iter_content.return_value = [b"x", b"y"]

            def open_without_stream(*args, **kwargs):
                _ = args
                if "stream" in kwargs:
                    raise TypeError("unexpected keyword argument")
                return RESPONSE

            FILE_NODE = SimpleNamespace(open=open_without_stream)
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "nostream.bin"
                RESULT = CLIENT.download_file("docs/nostream.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"xy")

    def test_download_file_success_with_open_stream_context_manager(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            RESPONSE = Mock()
            RESPONSE.iter_content.return_value = [b"a", b"b"]
            CONTEXT = Mock()
            CONTEXT.__enter__ = Mock(return_value=RESPONSE)
            CONTEXT.__exit__ = Mock(return_value=None)

            FILE_NODE = SimpleNamespace(open=Mock(return_value=CONTEXT))

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "ctx.bin"
                RESULT = CLIENT.download_file("docs/ctx.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"ab")
            FILE_NODE.open.assert_called_once_with(stream=True)

    def test_download_file_success_with_open_stream_closes_response(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            RESPONSE = SimpleNamespace(raw=BytesIO(b"from-open"), close=Mock())
            FILE_NODE = SimpleNamespace(open=Mock(return_value=RESPONSE))

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "open.bin"
                RESULT = CLIENT.download_file("docs/open.bin", LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"from-open")
            RESPONSE.close.assert_called_once()

    def test_download_file_fails_when_no_open_api_exists(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
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

    def test_write_downloaded_content_supports_response_content_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESPONSE = SimpleNamespace(content=b"payload")
            LOCAL_PATH = Path(TMPDIR) / "content.bin"

            RESULT = CLIENT._write_downloaded_content(RESPONSE, LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"payload")

    def test_write_downloaded_content_supports_readable_stream_objects(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESPONSE = BytesIO(b"streamed")
            LOCAL_PATH = Path(TMPDIR) / "streamed.bin"

            RESULT = CLIENT._write_downloaded_content(RESPONSE, LOCAL_PATH)

            self.assertTrue(RESULT)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"streamed")

    def test_write_downloaded_content_rejects_http_error_responses(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESPONSE = SimpleNamespace(status_code=503, iter_content=Mock(return_value=[b"x"]))
            LOCAL_PATH = Path(TMPDIR) / "error.bin"

            RESULT = CLIENT._write_downloaded_content(RESPONSE, LOCAL_PATH)

            self.assertFalse(RESULT)
            self.assertFalse(LOCAL_PATH.exists())

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
