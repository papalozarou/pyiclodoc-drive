# ------------------------------------------------------------------------------
# This test module verifies iCloud client auth, traversal, and download helpers.
# ------------------------------------------------------------------------------

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.config import AppConfig
from app.icloud_client import (
    ICLOUD_SESSION_CONNECT_TIMEOUT_SECONDS,
    ICLOUD_SESSION_READ_TIMEOUT_SECONDS,
    ICloudDriveClient,
    TraversalWorkerTimeoutError,
)


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
        keychain_service_name="pyiclodoc-drive",
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
        manifest_path=CONFIG_DIR / "pyiclodoc-drive-manifest.json",
        auth_state_path=CONFIG_DIR / "pyiclodoc-drive-auth_state.json",
        heartbeat_path=LOGS_DIR / "pyiclodoc-drive-heartbeat.txt",
        safety_net_done_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_done.flag",
        safety_net_blocked_path=CONFIG_DIR / "pyiclodoc-drive-safety_net_blocked.flag",
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

    def test_create_service_wraps_session_with_default_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock()
            ORIGINAL_REQUEST = Mock(return_value="request-response")
            ORIGINAL_REQUEST_RAW = Mock(return_value="request-raw-response")
            API.session.request = ORIGINAL_REQUEST
            API.session.request_raw = ORIGINAL_REQUEST_RAW

            with patch("app.icloud_client.PyiCloudService", return_value=API):
                CLIENT._create_service()

            self.assertIsNot(API.session.request, ORIGINAL_REQUEST)
            self.assertIsNot(API.session.request_raw, ORIGINAL_REQUEST_RAW)

            DEFAULT_TIMEOUT = (
                ICLOUD_SESSION_CONNECT_TIMEOUT_SECONDS,
                ICLOUD_SESSION_READ_TIMEOUT_SECONDS,
            )

            API.session.request("GET", "https://example.invalid")
            ORIGINAL_REQUEST.assert_called_once_with(
                "GET",
                "https://example.invalid",
                timeout=DEFAULT_TIMEOUT,
            )

            API.session.request_raw("POST", "https://example.invalid")
            ORIGINAL_REQUEST_RAW.assert_called_once_with(
                "POST",
                "https://example.invalid",
                timeout=DEFAULT_TIMEOUT,
            )

    def test_create_service_preserves_explicit_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock()
            ORIGINAL_REQUEST = Mock(return_value="request-response")
            API.session.request = ORIGINAL_REQUEST

            with patch("app.icloud_client.PyiCloudService", return_value=API):
                CLIENT._create_service()

            API.session.request("GET", "https://example.invalid", timeout=5)
            ORIGINAL_REQUEST.assert_called_once_with(
                "GET",
                "https://example.invalid",
                timeout=5,
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

    def test_authentication_emits_redacted_debug_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            API = Mock()
            API.requires_2fa = True
            API.validate_2fa_code.return_value = True
            API.is_trusted_session = False
            API.trust_session.return_value = True

            with patch("app.icloud_client.log_line") as LOG_LINE:
                with patch("app.icloud_client.PyiCloudService", return_value=API):
                    IS_AUTHENTICATED, DETAILS = CLIENT.start_authentication()
                CLIENT.complete_authentication("123456")

        self.assertFalse(IS_AUTHENTICATED)
        self.assertIn("Two-factor code is required", DETAILS)
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("iCloud service creation started:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("requires_2fa=True" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("iCloud MFA validation started:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertFalse(any("123456" in LINE for LINE in DEBUG_LINES))

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

    def test_list_entries_records_hard_failure_when_drive_root_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            API = Mock()
            type(API).drive = PropertyMock(side_effect=RuntimeError("connect timeout"))
            CLIENT.api = API

            ENTRIES = CLIENT.list_entries()

            self.assertEqual(ENTRIES, [])
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["dir_hard_failures"], 1)
            self.assertEqual(len(STATS["dir_failure_samples"]), 1)
            self.assertIn(
                "drive_root_unavailable", STATS["dir_failure_samples"][0]["reason"]
            )

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

    def test_list_entries_emits_debug_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            CLIENT.api = Mock(drive=FakeNode([]))

            with patch("app.icloud_client.log_line") as LOG_LINE:
                ENTRIES = CLIENT.list_entries()

        self.assertEqual(ENTRIES, [])
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("iCloud traversal started: mode=serial" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("iCloud traversal completed: mode=serial" in LINE for LINE in DEBUG_LINES)
        )

    def test_record_traversal_queue_state_clamps_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))

            CLIENT._record_traversal_queue_state(-1, -2, -3)

            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["directories_completed"], 0)
            self.assertEqual(STATS["directories_pending"], 0)
            self.assertEqual(STATS["workers_active"], 0)

    def test_record_serial_directory_enter_and_exit_updates_stats(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))

            CLIENT._record_serial_directory_enter()
            CLIENT._record_serial_directory_exit()

            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["directories_pending"], 0)
            self.assertEqual(STATS["directories_completed"], 1)
            self.assertEqual(STATS["workers_active"], 0)

    def test_record_directory_read_tracks_slow_paths_and_failure_samples(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))

            CLIENT._record_directory_read("/slow", 6.0, False, "ok")
            CLIENT._record_directory_read("/retry", 1.0, True, "retryable_error", "timeout")
            CLIENT._record_directory_read("/hard", 1.0, False, "hard_failure", "boom")

            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["dir_reads"], 3)
            self.assertEqual(STATS["dir_retries"], 1)
            self.assertEqual(STATS["dir_retryable_errors"], 1)
            self.assertEqual(STATS["dir_hard_failures"], 1)
            self.assertEqual(STATS["slow_dirs"][0]["path"], "/slow")
            self.assertEqual(len(STATS["dir_failure_samples"]), 2)

    def test_record_directory_failure_sample_respects_cap(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))

            for INDEX in range(7):
                CLIENT._record_directory_failure_sample(f"/p{INDEX}", "hard_failure", "boom")

            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(len(STATS["dir_failure_samples"]), 5)

    def test_directory_read_retry_emits_debug_without_raw_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            NODE = Mock()
            NODE.dir.side_effect = [RuntimeError("temporary secret detail"), []]

            with patch("app.icloud_client.time.sleep"):
                with patch("app.icloud_client.log_line") as LOG_LINE:
                    PAYLOAD = CLIENT._read_dir_payload_with_retry(NODE, "docs")

        self.assertEqual(PAYLOAD, [])
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("Traversal probe started:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("Traversal probe retry:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(any("reason=RuntimeError" in LINE for LINE in DEBUG_LINES))
        self.assertFalse(any("temporary secret detail" in LINE for LINE in DEBUG_LINES))

    def test_walk_node_parallel_collects_and_sorts_entries(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_icloud(TMPDIR).__dict__ | {"traversal_workers": 2}))
            CLIENT = ICloudDriveClient(CONFIG)
            ROOT_NODE = object()
            CHILD_NODE = object()

            with patch.object(
                CLIENT,
                "_walk_node_shallow",
                side_effect=[
                    (
                        [SimpleNamespace(path="b.txt"), SimpleNamespace(path="dir")],
                        [("dir", CHILD_NODE)],
                    ),
                    (
                        [SimpleNamespace(path="dir/a.txt")],
                        [],
                    ),
                ],
            ):
                RESULT = CLIENT._walk_node_parallel(ROOT_NODE, "")

            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["b.txt", "dir", "dir/a.txt"])
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["directories_completed"], 2)
            self.assertEqual(STATS["directories_pending"], 0)

    def test_walk_node_parallel_raises_controlled_failure_when_worker_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_icloud(TMPDIR).__dict__ | {"traversal_workers": 2}))
            CLIENT = ICloudDriveClient(CONFIG)

            def fake_wait(PENDING, timeout, return_when):
                _ = return_when
                self.assertEqual(timeout, 30.0)
                return set(), set(PENDING)

            with patch.object(
                CLIENT,
                "_walk_node_shallow",
                return_value=([], []),
            ):
                with patch("app.icloud_client.wait", side_effect=fake_wait):
                    with self.assertRaises(TraversalWorkerTimeoutError) as ERROR:
                        CLIENT._walk_node_parallel(object(), "")

            self.assertIn("Traversal worker stalled while reading / after 30.0s.", str(ERROR.exception))
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["dir_hard_failures"], 1)
            self.assertEqual(STATS["directories_completed"], 0)
            self.assertEqual(STATS["directories_pending"], 1)
            self.assertEqual(STATS["workers_active"], 1)
            self.assertEqual(
                STATS["dir_failure_samples"],
                [
                    {
                        "path": "/",
                        "status": "hard_failure",
                        "reason": "worker_timeout_after_30.0s",
                    }
                ],
            )

    def test_walk_node_parallel_keeps_waiting_while_progress_advances(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = AppConfig(**(build_config_for_icloud(TMPDIR).__dict__ | {"traversal_workers": 2}))
            CLIENT = ICloudDriveClient(CONFIG)
            WAIT_CALLS = {"count": 0}

            def fake_wait(PENDING, timeout, return_when):
                _ = return_when
                self.assertEqual(timeout, 30.0)
                WAIT_CALLS["count"] += 1

                if WAIT_CALLS["count"] == 1:
                    CLIENT._record_directory_read("docs/file.txt", 0.01, False, "non_directory")
                    return set(), set(PENDING)

                FUTURE = next(iter(PENDING))
                return {FUTURE}, set()

            with patch.object(
                CLIENT,
                "_walk_node_shallow",
                return_value=([SimpleNamespace(path="docs/file.txt")], []),
            ):
                with patch("app.icloud_client.wait", side_effect=fake_wait):
                    RESULT = CLIENT._walk_node_parallel(object(), "")

            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs/file.txt"])
            self.assertEqual(WAIT_CALLS["count"], 2)
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS["dir_hard_failures"], 0)
            self.assertEqual(STATS["directories_completed"], 1)
            self.assertEqual(STATS["dir_non_directory"], 1)

    def test_walk_node_shallow_prefers_name_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            NODE = Mock()

            with patch.object(
                CLIENT,
                "_node_dir",
                return_value={"dirs": [], "files": [], "names": ["docs"]},
            ):
                with patch.object(
                    CLIENT,
                    "_shallow_entries_from_names",
                    return_value=(["from-names"], []),
                ) as FROM_NAMES:
                    RESULT = CLIENT._walk_node_shallow(NODE, "")

            self.assertEqual(RESULT, (["from-names"], []))
            FROM_NAMES.assert_called_once_with(NODE, "", ["docs"])

    def test_walk_node_shallow_uses_payload_entries_when_names_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            NODE = Mock()

            with patch.object(
                CLIENT,
                "_node_dir",
                return_value={"dirs": ["d"], "files": ["f"], "names": []},
            ):
                with patch.object(
                    CLIENT,
                    "_shallow_entries_from_payload",
                    return_value=(["from-payload"], []),
                ) as FROM_PAYLOAD:
                    RESULT = CLIENT._walk_node_shallow(NODE, "")

            self.assertEqual(RESULT, (["from-payload"], []))
            FROM_PAYLOAD.assert_called_once_with(NODE, "", ["d"], ["f"])

    def test_shallow_entries_from_names_skips_blank_and_missing_children(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            DIRECTORY_CHILD = FakeDriveChild("folder", MODIFIED="d1")
            FILE_CHILD = FakeDriveChild("file", SIZE=2, MODIFIED="d2")

            with patch.object(
                CLIENT,
                "_child_node",
                side_effect=[DIRECTORY_CHILD, FILE_CHILD],
            ):
                RESULT, CHILD_DIRECTORIES = CLIENT._shallow_entries_from_names(
                    {},
                    "",
                    [" ", "docs", "root.txt"],
                )

            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs", "root.txt"])
            self.assertEqual(CHILD_DIRECTORIES, [("docs", DIRECTORY_CHILD)])

    def test_shallow_entries_from_names_emits_discovery_and_classification_debug(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            DIRECTORY_CHILD = FakeDriveChild("folder", MODIFIED="d1")
            FILE_CHILD = FakeDriveChild("file", SIZE=2, MODIFIED="d2")

            with patch.object(
                CLIENT,
                "_child_node",
                side_effect=[DIRECTORY_CHILD, FILE_CHILD],
            ):
                with patch("app.icloud_client.log_line") as LOG_LINE:
                    RESULT, CHILD_DIRECTORIES = CLIENT._shallow_entries_from_names(
                        {},
                        "",
                        ["docs", "root.txt"],
                    )

            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs", "root.txt"])
            self.assertEqual(CHILD_DIRECTORIES, [("docs", DIRECTORY_CHILD)])
            DEBUG_LINES = [
                CALL.args[2]
                for CALL in LOG_LINE.call_args_list
                if CALL.args[1] == "debug"
            ]
            self.assertTrue(
                any(
                    "Traversal child discovered: path=docs, kind=unknown, "
                    "source=name_list."
                    in LINE
                    for LINE in DEBUG_LINES
                )
            )
            self.assertTrue(
                any(
                    "Traversal child classified: path=docs, kind=directory."
                    in LINE
                    for LINE in DEBUG_LINES
                )
            )
            self.assertTrue(
                any(
                    "Traversal child classified: path=root.txt, kind=file."
                    in LINE
                    for LINE in DEBUG_LINES
                )
            )

    def test_shallow_entries_from_names_skips_root_markers_and_self_references(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            ROOT_NODE = object()
            DIRECTORY_CHILD = FakeDriveChild("folder", MODIFIED="d1")

            with patch.object(
                CLIENT,
                "_child_node",
                return_value=DIRECTORY_CHILD,
            ) as CHILD_NODE:
                RESULT, CHILD_DIRECTORIES = CLIENT._shallow_entries_from_names(
                    ROOT_NODE,
                    "",
                    ["/", ".", "docs"],
                )

            self.assertEqual(CHILD_NODE.call_count, 1)
            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs"])
            self.assertEqual(CHILD_DIRECTORIES, [("docs", DIRECTORY_CHILD)])

    def test_shallow_entries_from_payload_skips_blank_names_and_missing_children(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            DIR_ITEMS = [
                {"name": "", "dateModified": "d0"},
                {"name": "docs", "dateModified": "d1"},
            ]
            FILE_ITEMS = [{"name": "root.txt", "size": 2, "modified": "d2"}]
            CHILD_NODE = object()

            with patch.object(CLIENT, "_child_node", return_value=CHILD_NODE):
                RESULT, CHILD_DIRECTORIES = CLIENT._shallow_entries_from_payload(
                    {},
                    "",
                    DIR_ITEMS,
                    FILE_ITEMS,
                )

            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["root.txt", "docs"])
            self.assertEqual(CHILD_DIRECTORIES, [("docs", CHILD_NODE)])

    def test_shallow_entries_from_payload_skips_root_marker_names_and_self_references(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CLIENT = ICloudDriveClient(build_config_for_icloud(TMPDIR))
            ROOT_NODE = object()
            DIR_ITEMS = [
                {"name": "/", "dateModified": "d0"},
                {"name": "docs", "dateModified": "d1"},
            ]

            with patch.object(
                CLIENT,
                "_child_node",
                return_value=object(),
            ) as CHILD_NODE:
                RESULT, CHILD_DIRECTORIES = CLIENT._shallow_entries_from_payload(
                    ROOT_NODE,
                    "",
                    DIR_ITEMS,
                    [],
                )

            self.assertEqual(CHILD_NODE.call_count, 1)
            self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs"])
            self.assertEqual(len(CHILD_DIRECTORIES), 1)
            self.assertEqual(CHILD_DIRECTORIES[0][0], "docs")

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

            with patch("app.icloud_client.log_line") as LOG_LINE:
                self.assertFalse(CLIENT._child_is_dir(FILE_LIKE, "docs/file.bin"))

            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS.get("dir_non_directory", 0), 1)
            self.assertEqual(STATS.get("dir_hard_failures", 0), 0)
            DEBUG_LINES = [
                CALL.args[2]
                for CALL in LOG_LINE.call_args_list
                if CALL.args[1] == "debug"
            ]
            self.assertTrue(
                any(
                    "Traversal probe non-directory: path=docs/file.bin."
                    in LINE
                    for LINE in DEBUG_LINES
                )
            )

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
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS.get("dir_retryable_errors", 0), 2)
            self.assertEqual(STATS.get("dir_hard_failures", 0), 0)
            FAILURE_SAMPLES = STATS.get("dir_failure_samples", [])
            self.assertEqual(len(FAILURE_SAMPLES), 2)
            self.assertEqual(FAILURE_SAMPLES[0]["status"], "retryable_error")
            self.assertIn("RuntimeError", FAILURE_SAMPLES[0]["reason"])

    def test_child_is_dir_uses_real_child_path_for_retry_logs_and_failure_samples(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_LIKE = Mock()
            FILE_LIKE.dir.side_effect = [
                RuntimeError("transient"),
                RuntimeError("transient"),
                RuntimeError("transient"),
                RuntimeError("transient"),
            ]

            with patch("app.icloud_client.log_line") as LOG_LINE:
                self.assertFalse(CLIENT._child_is_dir(FILE_LIKE, "docs/file.key"))

            DEBUG_LINES = [
                CALL.args[2]
                for CALL in LOG_LINE.call_args_list
                if CALL.args[1] == "debug"
            ]
            self.assertTrue(
                any("path=docs/file.key" in LINE for LINE in DEBUG_LINES)
            )
            self.assertFalse(any("path=/, " in LINE for LINE in DEBUG_LINES))
            STATS = CLIENT.get_traversal_stats_snapshot()
            FAILURE_SAMPLES = STATS.get("dir_failure_samples", [])
            self.assertTrue(FAILURE_SAMPLES)
            self.assertEqual(FAILURE_SAMPLES[-1]["path"], "docs/file.key")

    def test_traversal_stats_snapshot_detaches_mutable_lists(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            CLIENT._record_directory_read("docs", 6.2, False, "ok")

            SNAPSHOT = CLIENT.get_traversal_stats_snapshot()
            SNAPSHOT["slow_dirs"].append({"path": "extra", "duration_seconds": 99.0})

            SECOND_SNAPSHOT = CLIENT.get_traversal_stats_snapshot()

        self.assertEqual(len(SECOND_SNAPSHOT["slow_dirs"]), 1)
        self.assertEqual(SECOND_SNAPSHOT["slow_dirs"][0]["path"], "docs")

    def test_build_child_entry_uses_child_metadata_for_files(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            CHILD = SimpleNamespace(
                date_modified="2026-03-22T10:00:00Z",
                size=42,
            )

            ENTRY = CLIENT._build_child_entry("docs/file.txt", CHILD, False)

        self.assertEqual(ENTRY.path, "docs/file.txt")
        self.assertFalse(ENTRY.is_dir)
        self.assertEqual(ENTRY.size, 42)
        self.assertEqual(ENTRY.modified, "2026-03-22T10:00:00Z")

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
            STATS = CLIENT.get_traversal_stats_snapshot()
            self.assertEqual(STATS.get("dir_hard_failures", 0), 1)
            FAILURE_SAMPLES = STATS.get("dir_failure_samples", [])
            self.assertEqual(len(FAILURE_SAMPLES), 1)
            self.assertEqual(FAILURE_SAMPLES[0]["status"], "hard_failure")

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

    def test_entries_from_files_emits_discovery_and_classification_debug(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)

            with patch("app.icloud_client.log_line") as LOG_LINE:
                RESULT = CLIENT._entries_from_files(
                    "docs",
                    [{"filename": "x.bin", "bytes": "9", "modified": "m"}],
                )

        self.assertEqual(len(RESULT), 1)
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any(
                "Traversal child discovered: path=docs/x.bin, kind=file, "
                "source=file_payload."
                in LINE
                for LINE in DEBUG_LINES
            )
        )
        self.assertTrue(
            any(
                "Traversal child classified: path=docs/x.bin, kind=file."
                in LINE
                for LINE in DEBUG_LINES
            )
        )


# ------------------------------------------------------------------------------
# These tests validate download-path resolution and local write helpers.
# ------------------------------------------------------------------------------
class TestICloudClientDownloads(unittest.TestCase):
    def test_download_file_requires_authenticated_api(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RESULT = CLIENT.download_file("docs/file.txt", Path(TMPDIR) / "out.txt")
            self.assertFalse(RESULT.is_success)
            self.assertEqual(RESULT.failure_reason, "not_authenticated")

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

            self.assertTrue(RESULT.is_success)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"abcdef")
            RESPONSE.iter_content.assert_called_once_with(chunk_size=4 * 1024 * 1024)

    def test_download_file_emits_debug_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
            RESPONSE = Mock()
            RESPONSE.iter_content.return_value = [b"abc"]
            FILE_NODE.open = Mock(return_value=RESPONSE)

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                with patch("app.icloud_client.log_line") as LOG_LINE:
                    LOCAL_PATH = Path(TMPDIR) / "downloads" / "file.bin"
                    RESULT = CLIENT.download_file("docs/file.bin", LOCAL_PATH)

        self.assertTrue(RESULT.is_success)
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(
            any("iCloud file download started:" in LINE for LINE in DEBUG_LINES)
        )
        self.assertTrue(
            any("iCloud file download completed:" in LINE for LINE in DEBUG_LINES)
        )

    def test_download_file_success_with_raw_stream(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            RAW_STREAM = BytesIO(b"raw-data")
            FILE_NODE = SimpleNamespace()
            RESPONSE = SimpleNamespace(raw=RAW_STREAM)
            FILE_NODE.open = Mock(return_value=RESPONSE)

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "raw.bin"
                RESULT = CLIENT.download_file("docs/raw.bin", LOCAL_PATH)

            self.assertTrue(RESULT.is_success)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"raw-data")
            self.assertTrue(RAW_STREAM.closed)

    def test_download_file_handles_open_errors(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
            FILE_NODE.open = Mock(side_effect=RuntimeError("boom"))
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                RESULT = CLIENT.download_file("docs/file.bin", Path(TMPDIR) / "file.bin")

            self.assertFalse(RESULT.is_success)
            self.assertEqual(RESULT.failure_reason, "open_failed")

    def test_download_file_rejects_directory_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            DIRECTORY_NODE = FakeNode([])
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=DIRECTORY_NODE):
                RESULT = CLIENT.download_file("docs/pkg.bundle", Path(TMPDIR) / "pkg.bundle")

            self.assertFalse(RESULT.is_success)
            self.assertEqual(RESULT.failure_reason, "directory_node")

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

            self.assertTrue(RESULT.is_success)
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

            self.assertFalse(RESULT.is_success)
            self.assertEqual(RESULT.failure_reason, "package_child_missing")

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

            self.assertTrue(RESULT.is_success)
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

            self.assertTrue(RESULT.is_success)
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

            self.assertTrue(RESULT.is_success)
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

            self.assertTrue(RESULT.is_success)
            self.assertEqual(LOCAL_PATH.read_bytes(), b"from-open")
            RESPONSE.close.assert_called_once()

    def test_download_file_closes_stream_and_cleans_temp_file_after_write_failure(self) -> None:
        class FailingReadableStream:
            def __init__(self) -> None:
                self._reads = 0
                self.close = Mock()

            def read(self, _SIZE: int) -> bytes:
                self._reads += 1
                if self._reads == 1:
                    return b"abc"
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            STREAM = FailingReadableStream()
            FILE_NODE = SimpleNamespace(open=Mock(return_value=STREAM))

            CLIENT.api = Mock()
            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                LOCAL_PATH = Path(TMPDIR) / "downloads" / "failure.bin"
                TEMP_PATH = CLIENT._temporary_download_path(LOCAL_PATH)
                RESULT = CLIENT.download_file("docs/failure.bin", LOCAL_PATH)

            self.assertFalse(RESULT.is_success)
            self.assertEqual(RESULT.failure_reason, "write_failed")
            self.assertFalse(LOCAL_PATH.exists())
            self.assertFalse(TEMP_PATH.exists())
            STREAM.close.assert_called_once()

    def test_download_file_fails_when_no_open_api_exists(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG = build_config_for_icloud(TMPDIR)
            CLIENT = ICloudDriveClient(CONFIG)
            FILE_NODE = SimpleNamespace()
            CLIENT.api = Mock()

            with patch.object(CLIENT, "_resolve_file_object", return_value=FILE_NODE):
                RESULT = CLIENT.download_file("docs/file.bin", Path(TMPDIR) / "file.bin")

            self.assertFalse(RESULT.is_success)

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
            self.assertTrue(RESPONSE.closed)

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
