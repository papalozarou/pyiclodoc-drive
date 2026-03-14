# ------------------------------------------------------------------------------
# This test module verifies incremental sync decisions and first-run safety
# helper behaviour.
#
# Notes:
# https://docs.python.org/3/library/os.html#os.stat_result
# ------------------------------------------------------------------------------

from pathlib import Path
import os
import tempfile
import time
import unittest

from dataclasses import dataclass
from unittest.mock import patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.syncer import (
    collect_mismatches,
    get_transfer_failure_reason,
    get_auto_worker_count,
    get_transfer_worker_count,
    is_known_package_path,
    is_retryable_transfer_error,
    needs_transfer,
    normalise_transfer_reason,
    package_signature,
    perform_incremental_sync,
    PROGRESS_LOG_SEPARATOR,
    transfer_if_required,
)


# ------------------------------------------------------------------------------
# This data class mirrors production remote-entry shape used by helpers.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class RemoteEntry:
    path: str
    is_dir: bool
    size: int
    modified: str


# ------------------------------------------------------------------------------
# This class provides a minimal client stub for incremental sync tests.
# ------------------------------------------------------------------------------
class FakeClient:
    def __init__(self, ENTRIES: list[RemoteEntry], DOWNLOAD_RESULTS: dict[str, bool]):
        self.entries = ENTRIES
        self.download_results = DOWNLOAD_RESULTS
        self.package_results: dict[str, bool] = {}
        self.package_failure_reasons: dict[str, str] = {}
        self.download_calls = 0
        self.package_calls = 0
        self.last_reason = ""
        self.traversal_stats = {"dir_hard_failures": 0}

    def list_entries(self) -> list[RemoteEntry]:
        return self.entries

    def download_file(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> bool:
        self.download_calls += 1
        if REMOTE_PATH == "docs/explode.txt":
            raise RuntimeError("boom")
        RESULT = self.download_results.get(REMOTE_PATH, True)
        if RESULT:
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_PATH.write_bytes(b"data")
            self.last_reason = ""
            return RESULT

        self.last_reason = "download_failed"
        return RESULT

    def download_package_tree(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> bool:
        _ = LOCAL_PATH
        self.package_calls += 1
        RESULT = self.package_results.get(REMOTE_PATH, False)
        if RESULT:
            self.last_reason = ""
            return True

        self.last_reason = self.package_failure_reasons.get(
            REMOTE_PATH,
            "package_download_failed",
        )
        return False

    def get_last_download_failure_reason(self) -> str:
        return self.last_reason

    def get_traversal_stats_snapshot(self) -> dict[str, int]:
        return dict(self.traversal_stats)


# ------------------------------------------------------------------------------
# These tests verify manifest diffing and permission helper behaviour.
# ------------------------------------------------------------------------------
class TestSyncerHelpers(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms a file transfer is requested when no manifest
# entry exists.
# --------------------------------------------------------------------------
    def test_needs_transfer_for_new_file(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/a.txt",
            is_dir=False,
            size=10,
            modified="2026-03-07T12:00:00Z",
        )

        self.assertTrue(needs_transfer(ENTRY, {}))

# --------------------------------------------------------------------------
# This test confirms unchanged file metadata does not trigger a transfer.
# --------------------------------------------------------------------------
    def test_no_transfer_for_unchanged_file(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/a.txt",
            is_dir=False,
            size=10,
            modified="2026-03-07T12:00:00Z",
        )
        MANIFEST = {
            "docs/a.txt": {
                "is_dir": False,
                "size": 10,
                "modified": "2026-03-07T12:00:00Z",
            }
        }

        self.assertFalse(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms package entries use package signatures for transfer
# decisions.
# --------------------------------------------------------------------------
    def test_no_transfer_for_unchanged_package_signature(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=10,
            modified="2026-03-07T12:00:00Z",
        )
        MANIFEST = {
            "docs/archive.bundle": {
                "is_dir": False,
                "entry_kind": "package",
                "size": 10,
                "modified": "2026-03-07T12:00:00Z",
                "package_signature": package_signature(ENTRY),
                "package_state": "package_reconciled",
            }
        }

        self.assertFalse(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms fallback reason merge preserves primary file failures
# when package fallback is not directory-capable.
# --------------------------------------------------------------------------
    def test_get_transfer_failure_reason_prefers_primary_failure(self) -> None:
        RESULT = get_transfer_failure_reason("open_failed", "not_directory_node")
        self.assertEqual(RESULT, "open_failed")

# --------------------------------------------------------------------------
# This test confirms fallback reason merge includes both reasons when they
# are independently meaningful diagnostics.
# --------------------------------------------------------------------------
    def test_get_transfer_failure_reason_combines_distinct_failures(self) -> None:
        RESULT = get_transfer_failure_reason("open_failed", "package_child_missing")
        self.assertEqual(RESULT, "open_failed; fallback=package_child_missing")

# --------------------------------------------------------------------------
# This test confirms transfer reason normalisation keeps the primary token
# when fallback detail is present.
# --------------------------------------------------------------------------
    def test_normalise_transfer_reason_uses_primary_token(self) -> None:
        RESULT = normalise_transfer_reason("write_failed; fallback=package_item_missing")
        self.assertEqual(RESULT, "write_failed")

# --------------------------------------------------------------------------
# This test confirms package suffix detection recognises known package types.
# --------------------------------------------------------------------------
    def test_is_known_package_path_identifies_supported_suffixes(self) -> None:
        self.assertTrue(is_known_package_path("docs/archive.bundle"))
        self.assertTrue(is_known_package_path("Swift Playground/My Playground.playgroundbook"))
        self.assertFalse(is_known_package_path("docs/file.txt"))

# --------------------------------------------------------------------------
# This test confirms transfer fallback keeps the initial file failure reason
# when package fallback only reports a non-directory marker.
# --------------------------------------------------------------------------
    def test_transfer_if_required_keeps_primary_reason_on_non_directory_fallback(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.pkg",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {"docs/archive.pkg": False})
        CLIENT.package_results["docs/archive.pkg"] = False
        CLIENT.package_failure_reasons["docs/archive.pkg"] = "not_directory_node"

        with tempfile.TemporaryDirectory() as TMPDIR:
            IS_SUCCESS, ATTEMPT, REASON = transfer_if_required(
                CLIENT,
                Path(TMPDIR),
                ENTRY,
                True,
            )

        self.assertFalse(IS_SUCCESS)
        self.assertEqual(ATTEMPT, 1)
        self.assertEqual(REASON, "download_failed")

# --------------------------------------------------------------------------
# This test confirms existing local package directories are reconciled when
# package fallback cannot resolve parent metadata for non-directory nodes.
# --------------------------------------------------------------------------
    def test_transfer_if_required_reconciles_existing_local_package_directory(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {"docs/archive.bundle": False})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_item_missing"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            PACKAGE_DIR = ROOT_DIR / "docs" / "archive.bundle"
            PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
            (PACKAGE_DIR / "child.dat").write_bytes(b"x")

            IS_SUCCESS, ATTEMPT, REASON = transfer_if_required(
                CLIENT,
                ROOT_DIR,
                ENTRY,
                True,
            )

        self.assertTrue(IS_SUCCESS)
        self.assertEqual(ATTEMPT, 1)
        self.assertEqual(REASON, "package_reconciled")
        self.assertEqual(CLIENT.download_calls, 0)
        self.assertEqual(CLIENT.package_calls, 1)

# --------------------------------------------------------------------------
# This test confirms known package paths use package-first handling and
# emit explicit metadata-unavailable diagnostics when needed.
# --------------------------------------------------------------------------
    def test_transfer_if_required_flags_known_package_metadata_unavailable(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {"docs/archive.bundle": False})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_item_missing"

        with tempfile.TemporaryDirectory() as TMPDIR:
            IS_SUCCESS, ATTEMPT, REASON = transfer_if_required(
                CLIENT,
                Path(TMPDIR),
                ENTRY,
                True,
            )

        self.assertFalse(IS_SUCCESS)
        self.assertEqual(ATTEMPT, 1)
        self.assertEqual(REASON, "known_package_metadata_unavailable")
        self.assertEqual(CLIENT.download_calls, 0)
        self.assertEqual(CLIENT.package_calls, 1)

# --------------------------------------------------------------------------
# This test confirms ownership mismatch detection identifies outlier files.
# --------------------------------------------------------------------------
    def test_ownership_mismatch_detection(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE = Path(TMPDIR)
            FILE_ONE = BASE / "one.txt"
            FILE_TWO = BASE / "two.txt"

            FILE_ONE.write_text("1", encoding="utf-8")
            FILE_TWO.write_text("2", encoding="utf-8")

            FILES = [FILE_ONE, FILE_TWO]
            EXPECTED_UID = FILE_ONE.stat().st_uid
            EXPECTED_GID = FILE_ONE.stat().st_gid
            MISMATCHES = collect_mismatches(
                FILES,
                EXPECTED_UID,
                EXPECTED_GID,
            )

            self.assertEqual(MISMATCHES, [])

# --------------------------------------------------------------------------
# This test confirms mismatch formatting includes expected ownership details.
# --------------------------------------------------------------------------
    def test_ownership_mismatch_message_includes_expected_values(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            FILE_PATH = Path(TMPDIR) / "file.txt"
            FILE_PATH.write_text("x", encoding="utf-8")

            EXPECTED_UID = FILE_PATH.stat().st_uid + 1
            EXPECTED_GID = FILE_PATH.stat().st_gid + 1
            MISMATCHES = collect_mismatches([FILE_PATH], EXPECTED_UID, EXPECTED_GID)

            self.assertEqual(len(MISMATCHES), 1)
            self.assertIn("expected uid=", MISMATCHES[0])

# --------------------------------------------------------------------------
# This test confirms automatic worker sizing falls back to one when CPU
# count is unavailable.
# --------------------------------------------------------------------------
    def test_auto_worker_count_defaults_to_one(self) -> None:
        with patch("app.syncer.os.cpu_count", return_value=None):
            self.assertEqual(get_auto_worker_count(), 1)

# --------------------------------------------------------------------------
# This test confirms automatic worker sizing is capped for high-core hosts.
# --------------------------------------------------------------------------
    def test_auto_worker_count_caps_at_eight(self) -> None:
        with patch("app.syncer.os.cpu_count", return_value=64):
            self.assertEqual(get_auto_worker_count(), 8)

# --------------------------------------------------------------------------
# This test confirms automatic worker sizing uses direct CPU count when
# within normal bounds.
# --------------------------------------------------------------------------
    def test_auto_worker_count_uses_cpu_count_within_bounds(self) -> None:
        with patch("app.syncer.os.cpu_count", return_value=4):
            self.assertEqual(get_auto_worker_count(), 4)

# --------------------------------------------------------------------------
# This test confirms transfer worker override is bounded when configured.
# --------------------------------------------------------------------------
    def test_transfer_worker_count_uses_bounded_override(self) -> None:
        self.assertEqual(get_transfer_worker_count(12), 12)
        self.assertEqual(get_transfer_worker_count(64), 16)

# --------------------------------------------------------------------------
# This test confirms transfer worker count falls back to auto mode.
# --------------------------------------------------------------------------
    def test_transfer_worker_count_falls_back_to_auto(self) -> None:
        with patch("app.syncer.get_auto_worker_count", return_value=5):
            self.assertEqual(get_transfer_worker_count(0), 5)

# --------------------------------------------------------------------------
# This test confirms incremental sync reports transfer, skip, and error
# counts correctly with mixed file outcomes.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_counts_results(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-09T00:00:00Z"),
            RemoteEntry("docs/new.txt", False, 11, "2026-03-09T00:00:00Z"),
            RemoteEntry("docs/unchanged.txt", False, 10, "2026-03-08T00:00:00Z"),
            RemoteEntry("docs/fail.txt", False, 12, "2026-03-09T00:00:00Z"),
        ]
        MANIFEST = {
            "docs/unchanged.txt": {
                "is_dir": False,
                "size": 10,
                "modified": "2026-03-08T00:00:00Z",
            }
        }
        CLIENT = FakeClient(
            ENTRIES,
            {
                "docs/new.txt": True,
                "docs/fail.txt": False,
            },
        )

        with tempfile.TemporaryDirectory() as TMPDIR:
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), MANIFEST)

        self.assertEqual(SUMMARY.total_files, 3)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(SUMMARY.transferred_bytes, 11)
        self.assertEqual(SUMMARY.skipped_files, 1)
        self.assertEqual(SUMMARY.error_files, 1)
        self.assertIn("docs", NEW_MANIFEST)
        self.assertIn("docs/new.txt", NEW_MANIFEST)
        self.assertIn("docs/unchanged.txt", NEW_MANIFEST)
        self.assertNotIn("docs/fail.txt", NEW_MANIFEST)

# --------------------------------------------------------------------------
# This test confirms worker exceptions are counted and logged.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_logs_worker_exception(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/explode.txt", False, 1, "2026-03-09T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {})

        with tempfile.TemporaryDirectory() as TMPDIR:
            with patch("builtins.print") as PRINT:
                SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertEqual(SUMMARY.total_files, 1)
        self.assertEqual(SUMMARY.transferred_files, 0)
        self.assertEqual(SUMMARY.transferred_bytes, 0)
        self.assertEqual(SUMMARY.skipped_files, 0)
        self.assertEqual(SUMMARY.error_files, 1)
        self.assertNotIn("docs/explode.txt", NEW_MANIFEST)
        self.assertTrue(any("File transfer worker failed:" in CALL.args[0] for CALL in PRINT.call_args_list))

# --------------------------------------------------------------------------
# This test confirms incremental sync emits debug diagnostics when a log
# file path is provided.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_emits_debug_diagnostics(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-09T00:00:00Z"),
            RemoteEntry("docs/new.txt", False, 11, "2026-03-09T00:00:00Z"),
            RemoteEntry("docs/unchanged.txt", False, 5, "2026-03-08T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/new.txt": True})
        MANIFEST = {
            "docs/unchanged.txt": {
                "is_dir": False,
                "size": 5,
                "modified": "2026-03-08T00:00:00Z",
            }
        }

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.log_line") as LOG_LINE:
                perform_incremental_sync(CLIENT, Path(TMPDIR), MANIFEST, 0, LOG_FILE)

        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(any("Traversal timing detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Remote listing detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Directory ensured: docs" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("File queued for transfer: docs/new.txt" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("File transferred: docs/new.txt" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("File skipped unchanged: docs/unchanged.txt" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Transfer planning detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Transfer execution detail:" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms failed transfers emit aggregated failure reason
# diagnostics for quick root-cause visibility.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_emits_failure_reason_summary(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/file.txt", False, 11, "2026-03-08T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/file.txt": False})
        CLIENT.package_results["docs/file.txt"] = False
        CLIENT.package_failure_reasons["docs/file.txt"] = "not_directory_node"

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.log_line") as LOG_LINE:
                perform_incremental_sync(CLIENT, Path(TMPDIR), {}, 0, LOG_FILE)

        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(
            any("Transfer failure reason detail: download_failed=1" in LINE for LINE in DEBUG_LINES)
        )

# --------------------------------------------------------------------------
# This test confirms info-level stage markers are emitted for traversal
# and transfer lifecycle visibility.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_emits_info_stage_markers(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/new.txt", False, 11, "2026-03-09T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/new.txt": True})

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.log_line") as LOG_LINE:
                perform_incremental_sync(CLIENT, Path(TMPDIR), {}, 0, LOG_FILE)

        INFO_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "info"]
        self.assertTrue(any("Traversal started." in LINE for LINE in INFO_LINES))
        self.assertTrue(any("Traversal finished." in LINE for LINE in INFO_LINES))
        self.assertTrue(any("Transfer started." in LINE for LINE in INFO_LINES))
        self.assertTrue(any("Transfer finished." in LINE for LINE in INFO_LINES))

# --------------------------------------------------------------------------
# This test confirms long-running transfer loops emit in-run progress logs.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_emits_periodic_progress_logs(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/file.txt", False, 11, "2026-03-09T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/file.txt": True})

        WAIT_CALLS = {"count": 0}

        def fake_wait(PENDING, timeout, return_when):
            _ = timeout
            _ = return_when
            WAIT_CALLS["count"] += 1
            if WAIT_CALLS["count"] == 1:
                return set(), set(PENDING)

            FUTURE = next(iter(PENDING))
            return {FUTURE}, set()

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.wait", side_effect=fake_wait):
                with patch("app.syncer.TRANSFER_PROGRESS_LOG_INTERVAL_SECONDS", 0.0):
                    with patch("app.syncer.log_line") as LOG_LINE:
                        perform_incremental_sync(CLIENT, Path(TMPDIR), {}, 1, LOG_FILE)

        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(any("Transfer progress detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any(PROGRESS_LOG_SEPARATOR == LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms long-running traversal emits in-run progress logs.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_emits_traversal_progress_logs(self) -> None:
        class SlowClient:
            def list_entries(self):
                time.sleep(0.05)
                return []

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return True

        CLIENT = SlowClient()

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS", 0.01):
                with patch("app.syncer.log_line") as LOG_LINE:
                    perform_incremental_sync(CLIENT, Path(TMPDIR), {}, 0, LOG_FILE)

        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(any("Traversal progress detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any(PROGRESS_LOG_SEPARATOR == LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms failed transfers preserve existing manifest metadata.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_preserves_existing_manifest_on_failure(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/file.txt", False, 22, "2026-03-10T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/file.txt": False})
        MANIFEST = {
            "docs/file.txt": {
                "is_dir": False,
                "size": 11,
                "modified": "2026-03-09T00:00:00Z",
            }
        }

        with tempfile.TemporaryDirectory() as TMPDIR:
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), MANIFEST)

        self.assertEqual(SUMMARY.error_files, 1)
        self.assertEqual(NEW_MANIFEST["docs/file.txt"]["size"], 11)
        self.assertEqual(NEW_MANIFEST["docs/file.txt"]["modified"], "2026-03-09T00:00:00Z")

# --------------------------------------------------------------------------
# This test confirms first-run reconciliation skips download when local
# file metadata already matches remote metadata.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_reconciles_first_run_existing_files(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-12T00:00:00Z"),
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-12T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/keep.txt": True})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            LOCAL_FILE = ROOT_DIR / "docs" / "keep.txt"
            LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_FILE.write_text("keep", encoding="utf-8")

            REMOTE_MTIME = time.mktime(time.strptime("2026-03-12T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
            os.utime(LOCAL_FILE, (REMOTE_MTIME, REMOTE_MTIME))

            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, ROOT_DIR, {})

        self.assertEqual(CLIENT.download_calls, 0)
        self.assertEqual(SUMMARY.total_files, 1)
        self.assertEqual(SUMMARY.transferred_files, 0)
        self.assertEqual(SUMMARY.skipped_files, 1)
        self.assertEqual(SUMMARY.error_files, 0)
        self.assertIn("docs/keep.txt", NEW_MANIFEST)

# --------------------------------------------------------------------------
# This test confirms first-run reconciliation still downloads when local
# metadata does not match remote metadata.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_reconciles_first_run_mismatch_downloads(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-12T00:00:00Z"),
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-12T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/keep.txt": True})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            LOCAL_FILE = ROOT_DIR / "docs" / "keep.txt"
            LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_FILE.write_text("old", encoding="utf-8")
            os.utime(LOCAL_FILE, None)

            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, ROOT_DIR, {})

        self.assertEqual(CLIENT.download_calls, 1)
        self.assertEqual(SUMMARY.total_files, 1)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(SUMMARY.skipped_files, 0)
        self.assertEqual(SUMMARY.error_files, 0)
        self.assertIn("docs/keep.txt", NEW_MANIFEST)

# --------------------------------------------------------------------------
# This test confirms successful transfers apply remote modified timestamps
# to downloaded local files.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_applies_remote_modified_timestamp(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/new.txt", False, 4, "2026-03-12T10:15:30Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/new.txt": True})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, ROOT_DIR, {})
            LOCAL_FILE = ROOT_DIR / "docs" / "new.txt"
            self.assertEqual(SUMMARY.transferred_files, 1)
            self.assertIn("docs/new.txt", NEW_MANIFEST)
            self.assertTrue(LOCAL_FILE.exists())
            EXPECTED_MTIME = time.mktime(time.strptime("2026-03-12T10:15:30Z", "%Y-%m-%dT%H:%M:%SZ"))
            self.assertAlmostEqual(LOCAL_FILE.stat().st_mtime, EXPECTED_MTIME, delta=2.0)

# --------------------------------------------------------------------------
# This test confirms package fallback succeeds when file download fails.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_uses_package_fallback(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/archive.bundle", False, 0, "2026-03-12T10:15:30Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/archive.bundle": False})
        CLIENT.package_results["docs/archive.bundle"] = True

        with tempfile.TemporaryDirectory() as TMPDIR:
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertEqual(CLIENT.download_calls, 0)
        self.assertEqual(CLIENT.package_calls, 1)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(SUMMARY.error_files, 0)
        self.assertIn("docs/archive.bundle", NEW_MANIFEST)
        self.assertEqual(NEW_MANIFEST["docs/archive.bundle"]["entry_kind"], "package")
        self.assertEqual(NEW_MANIFEST["docs/archive.bundle"]["package_state"], "package")

# --------------------------------------------------------------------------
# This test confirms delete-removed mode prunes stale local files and
# empty directories that no longer exist remotely.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_deletes_removed_local_paths_when_enabled(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-11T00:00:00Z"),
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-11T00:00:00Z"),
        ]
        MANIFEST = {
            "docs/keep.txt": {
                "is_dir": False,
                "size": 4,
                "modified": "2026-03-11T00:00:00Z",
            }
        }
        CLIENT = FakeClient(ENTRIES, {})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            (ROOT_DIR / "docs").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "archive").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "keep.txt").write_text("keep", encoding="utf-8")
            (ROOT_DIR / "docs" / "stale.txt").write_text("stale", encoding="utf-8")
            (ROOT_DIR / "docs" / "archive" / "old.txt").write_text("old", encoding="utf-8")

            SUMMARY, NEW_MANIFEST = perform_incremental_sync(
                CLIENT,
                ROOT_DIR,
                MANIFEST,
                BACKUP_DELETE_REMOVED=True,
            )

            self.assertEqual(SUMMARY.error_files, 0)
            self.assertTrue((ROOT_DIR / "docs" / "keep.txt").exists())
            self.assertFalse((ROOT_DIR / "docs" / "stale.txt").exists())
            self.assertFalse((ROOT_DIR / "docs" / "archive" / "old.txt").exists())
            self.assertFalse((ROOT_DIR / "docs" / "archive").exists())
            self.assertIn("docs/keep.txt", NEW_MANIFEST)

# --------------------------------------------------------------------------
# This test confirms stale local files remain untouched when delete-removed
# mode is disabled.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_keeps_removed_local_paths_when_disabled(self) -> None:
        ENTRIES = [
            RemoteEntry("docs", True, 0, "2026-03-11T00:00:00Z"),
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-11T00:00:00Z"),
        ]
        MANIFEST = {
            "docs/keep.txt": {
                "is_dir": False,
                "size": 4,
                "modified": "2026-03-11T00:00:00Z",
            }
        }
        CLIENT = FakeClient(ENTRIES, {})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            (ROOT_DIR / "docs").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "keep.txt").write_text("keep", encoding="utf-8")
            (ROOT_DIR / "docs" / "stale.txt").write_text("stale", encoding="utf-8")

            perform_incremental_sync(
                CLIENT,
                ROOT_DIR,
                MANIFEST,
                BACKUP_DELETE_REMOVED=False,
            )

            self.assertTrue((ROOT_DIR / "docs" / "stale.txt").exists())

# --------------------------------------------------------------------------
# This test confirms reconciled package entries persist package metadata in
# the manifest for future transfer decisions.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_persists_reconciled_package_metadata(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/archive.bundle", False, 4, "2026-03-07T12:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/archive.bundle": False})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_item_missing"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            PACKAGE_DIR = ROOT_DIR / "docs" / "archive.bundle"
            PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
            (PACKAGE_DIR / "child.dat").write_bytes(b"x")
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, ROOT_DIR, {})

        self.assertEqual(SUMMARY.error_files, 0)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(NEW_MANIFEST["docs/archive.bundle"]["entry_kind"], "package")
        self.assertEqual(
            NEW_MANIFEST["docs/archive.bundle"]["package_state"],
            "package_reconciled",
        )

# --------------------------------------------------------------------------
# This test confirms incomplete traversal blocks delete-removed behaviour
# and surfaces the partial-run state in the sync summary.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_skips_delete_when_traversal_incomplete(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-07T12:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/keep.txt": True})
        CLIENT.traversal_stats["dir_hard_failures"] = 1

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            STALE_PATH = ROOT_DIR / "docs" / "stale.txt"
            STALE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STALE_PATH.write_text("stale", encoding="utf-8")

            SUMMARY, _ = perform_incremental_sync(
                CLIENT,
                ROOT_DIR,
                {},
                BACKUP_DELETE_REMOVED=True,
            )

            self.assertTrue(STALE_PATH.exists())

        self.assertFalse(SUMMARY.traversal_complete)
        self.assertEqual(SUMMARY.traversal_hard_failures, 1)
        self.assertTrue(SUMMARY.delete_phase_skipped)

# --------------------------------------------------------------------------
# This test confirms transient exceptions are retried before succeeding.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_retries_transient_transfer_errors(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/retry.txt", False, 5, "2026-03-10T00:00:00Z"),
        ]

        class FlakyClient:
            def __init__(self):
                self.calls = 0

            def list_entries(self):
                return ENTRIES

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("Service Unavailable (503)")
                return True

        CLIENT = FlakyClient()

        with tempfile.TemporaryDirectory() as TMPDIR:
            with patch("app.syncer.time.sleep") as SLEEP:
                SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertEqual(CLIENT.calls, 3)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(SUMMARY.error_files, 0)
        self.assertIn("docs/retry.txt", NEW_MANIFEST)
        self.assertEqual(SLEEP.call_count, 2)

# --------------------------------------------------------------------------
# This test confirms retry filtering only includes transient transfer errors.
# --------------------------------------------------------------------------
    def test_is_retryable_transfer_error_classification(self) -> None:
        self.assertTrue(is_retryable_transfer_error(RuntimeError("Service Unavailable (503)")))
        self.assertTrue(is_retryable_transfer_error(RuntimeError("Bad Gateway (502)")))
        self.assertFalse(is_retryable_transfer_error(RuntimeError("Permission denied")))


if __name__ == "__main__":
    unittest.main()
