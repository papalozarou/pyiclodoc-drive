# ------------------------------------------------------------------------------
# This test module verifies incremental sync decisions and first-run safety
# helper behaviour.
#
# Notes:
# https://docs.python.org/3/library/os.html#os.stat_result
# ------------------------------------------------------------------------------

from pathlib import Path
import errno
import os
import tempfile
import time
import unittest

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.syncer import (
    DownloadResult,
    TransferResult,
    apply_remote_modified_time,
    build_local_file_index,
    build_empty_traversal_stats_snapshot,
    change_conflicting_local_path,
    collect_local_files,
    collect_mismatches,
    delete_removed_local_paths,
    ensure_directories,
    entry_metadata,
    format_slow_directory_summary,
    get_transfer_failure_reason,
    get_traversal_hard_failure_count,
    get_traversal_session_invalid_flag,
    get_auto_worker_count,
    get_transfer_worker_count,
    is_known_package_path,
    list_entries_with_progress,
    package_entry_metadata,
    is_retryable_transfer_error,
    is_local_file_aligned_with_remote,
    needs_transfer,
    normalise_transfer_reason,
    parse_remote_modified_epoch,
    package_signature,
    perform_incremental_sync,
    PROGRESS_LOG_SEPARATOR,
    run_first_time_safety_net,
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
        self.traversal_stats = {"dir_hard_failures": 0}

    def list_entries(self) -> list[RemoteEntry]:
        return self.entries

    def download_file(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        self.download_calls += 1
        if REMOTE_PATH == "docs/explode.txt":
            raise RuntimeError("boom")
        if REMOTE_PATH == "docs/session_dead.txt":
            ERROR = RuntimeError("Authentication required for Account.")
            ERROR.code = 421
            raise ERROR
        RESULT = self.download_results.get(REMOTE_PATH, True)
        if RESULT:
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_PATH.write_bytes(b"data")
            return DownloadResult(True)

        return DownloadResult(False, "download_failed")

    def download_package_tree(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        _ = LOCAL_PATH
        self.package_calls += 1
        RESULT = self.package_results.get(REMOTE_PATH, False)
        if RESULT:
            return DownloadResult(True)

        return DownloadResult(
            False,
            self.package_failure_reasons.get(
                REMOTE_PATH,
                "package_download_failed",
            ),
        )

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
# This test confirms directory entries always trigger transfer planning so
# they can be preserved in the manifest.
# --------------------------------------------------------------------------
    def test_needs_transfer_for_directory_manifest_entry(self) -> None:
        ENTRY = RemoteEntry(
            path="docs",
            is_dir=False,
            size=10,
            modified="2026-03-07T12:00:00Z",
        )
        MANIFEST = {
            "docs": {
                "is_dir": True,
                "size": 0,
                "modified": "2026-03-01T00:00:00Z",
            }
        }

        self.assertTrue(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms package entries trigger transfer when signatures differ.
# --------------------------------------------------------------------------
    def test_needs_transfer_for_changed_package_signature(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=11,
            modified="2026-03-08T12:00:00Z",
        )
        MANIFEST = {
            "docs/archive.bundle": {
                "is_dir": False,
                "entry_kind": "package",
                "package_signature": "stale",
                "package_state": "package",
            }
        }

        self.assertTrue(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms normal file entries trigger transfer when size differs.
# --------------------------------------------------------------------------
    def test_needs_transfer_for_changed_file_size(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/a.txt",
            is_dir=False,
            size=11,
            modified="2026-03-07T12:00:00Z",
        )
        MANIFEST = {
            "docs/a.txt": {
                "is_dir": False,
                "size": 10,
                "modified": "2026-03-07T12:00:00Z",
            }
        }

        self.assertTrue(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms normal file entries trigger transfer when modified time
# differs.
# --------------------------------------------------------------------------
    def test_needs_transfer_for_changed_file_modified_time(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/a.txt",
            is_dir=False,
            size=10,
            modified="2026-03-08T12:00:00Z",
        )
        MANIFEST = {
            "docs/a.txt": {
                "is_dir": False,
                "size": 10,
                "modified": "2026-03-07T12:00:00Z",
            }
        }

        self.assertTrue(needs_transfer(ENTRY, MANIFEST))

# --------------------------------------------------------------------------
# This test confirms safety-net file collection skips directories and stops
# at the configured sample limit.
# --------------------------------------------------------------------------
    def test_collect_local_files_limits_results_to_sample_size(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            (ROOT_DIR / "docs").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "a.txt").write_text("a", encoding="utf-8")
            (ROOT_DIR / "docs" / "b.txt").write_text("b", encoding="utf-8")

            RESULT = collect_local_files(ROOT_DIR, 1)

        self.assertEqual(len(RESULT), 1)
        self.assertIn(RESULT[0].name, {"a.txt", "b.txt"})

# --------------------------------------------------------------------------
# This test confirms first-run safety net returns a non-blocking result for
# empty output trees.
# --------------------------------------------------------------------------
    def test_run_first_time_safety_net_returns_safe_result_for_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            RESULT = run_first_time_safety_net(Path(TMPDIR), 5)

        self.assertFalse(RESULT.should_block)
        self.assertEqual(RESULT.mismatched_samples, [])

# --------------------------------------------------------------------------
# This test confirms entry metadata labels files and directories correctly.
# --------------------------------------------------------------------------
    def test_entry_metadata_uses_expected_entry_kind(self) -> None:
        FILE_ENTRY = RemoteEntry("docs/a.txt", False, 10, "m1")
        DIR_ENTRY = RemoteEntry("docs", True, 0, "m2")

        self.assertEqual(entry_metadata(FILE_ENTRY)["entry_kind"], "file")
        self.assertEqual(entry_metadata(DIR_ENTRY)["entry_kind"], "dir")

# --------------------------------------------------------------------------
# This test confirms package metadata stores both package state and derived
# signature.
# --------------------------------------------------------------------------
    def test_package_entry_metadata_includes_package_fields(self) -> None:
        ENTRY = RemoteEntry("docs/archive.bundle", False, 10, "m1")

        RESULT = package_entry_metadata(ENTRY, "package")

        self.assertEqual(RESULT["entry_kind"], "package")
        self.assertEqual(RESULT["package_state"], "package")
        self.assertEqual(RESULT["package_signature"], package_signature(ENTRY))

# --------------------------------------------------------------------------
# This test confirms traversal hard-failure count falls back safely when the
# client returns a malformed stats payload.
# --------------------------------------------------------------------------
    def test_get_traversal_hard_failure_count_handles_malformed_snapshot(self) -> None:
        CLIENT = SimpleNamespace(get_traversal_stats_snapshot=lambda: None)
        self.assertEqual(get_traversal_hard_failure_count(CLIENT), 0)

# --------------------------------------------------------------------------
# This test confirms traversal hard-failure count ignores non-dictionary
# stats payloads.
# --------------------------------------------------------------------------
    def test_get_traversal_hard_failure_count_handles_non_dict_snapshot(self) -> None:
        CLIENT = SimpleNamespace(get_traversal_stats_snapshot=lambda: [])
        self.assertEqual(get_traversal_hard_failure_count(CLIENT), 0)

# --------------------------------------------------------------------------
# This test confirms traversal hard-failure count is clamped at zero for
# invalid negative values.
# --------------------------------------------------------------------------
    def test_get_traversal_hard_failure_count_clamps_negative_value(self) -> None:
        CLIENT = SimpleNamespace(get_traversal_stats_snapshot=lambda: {"dir_hard_failures": -2})
        self.assertEqual(get_traversal_hard_failure_count(CLIENT), 0)

# --------------------------------------------------------------------------
# This test confirms the session-invalid flag falls back safely when the
# client returns a malformed stats payload.
# --------------------------------------------------------------------------
    def test_get_traversal_session_invalid_flag_handles_malformed_snapshot(self) -> None:
        CLIENT = SimpleNamespace(get_traversal_stats_snapshot=lambda: None)
        self.assertFalse(get_traversal_session_invalid_flag(CLIENT))

# --------------------------------------------------------------------------
# This test confirms the session-invalid flag reads a true value from the
# traversal stats snapshot.
# --------------------------------------------------------------------------
    def test_get_traversal_session_invalid_flag_reads_true_value(self) -> None:
        CLIENT = SimpleNamespace(
            get_traversal_stats_snapshot=lambda: {"dir_session_invalid": True}
        )
        self.assertTrue(get_traversal_session_invalid_flag(CLIENT))

# --------------------------------------------------------------------------
# This test confirms ensure_directories creates nested paths and emits a
# debug log when logging is enabled.
# --------------------------------------------------------------------------
    def test_ensure_directories_creates_paths_and_logs(self) -> None:
        DIRECTORIES = [RemoteEntry("docs/nested", True, 0, "m1")]

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            LOG_FILE = ROOT_DIR / "worker.log"

            with patch("app.syncer.log_line") as LOG_LINE:
                ensure_directories(ROOT_DIR, DIRECTORIES, LOG_FILE)

            self.assertTrue((ROOT_DIR / "docs" / "nested").exists())
            self.assertTrue(any("Directory ensured: docs/nested" in CALL.args[2] for CALL in LOG_LINE.call_args_list))

# --------------------------------------------------------------------------
# This test confirms ensure_directories replaces a conflicting file when the
# remote path is now a directory.
# --------------------------------------------------------------------------
    def test_ensure_directories_replaces_conflicting_file(self) -> None:
        DIRECTORIES = [RemoteEntry("docs/nested", True, 0, "m1")]

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            CONFLICT_PATH = ROOT_DIR / "docs" / "nested"
            CONFLICT_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFLICT_PATH.write_text("file", encoding="utf-8")

            ensure_directories(ROOT_DIR, DIRECTORIES)

            self.assertTrue(CONFLICT_PATH.exists())
            self.assertTrue(CONFLICT_PATH.is_dir())

# --------------------------------------------------------------------------
# This test confirms conflicting local path cleanup removes directories when a
# file destination is expected.
# --------------------------------------------------------------------------
    def test_change_conflicting_local_path_removes_directory_for_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOCAL_PATH = Path(TMPDIR) / "docs" / "report.txt"
            LOCAL_PATH.mkdir(parents=True, exist_ok=True)
            (LOCAL_PATH / "old.txt").write_text("old", encoding="utf-8")

            RESULT = change_conflicting_local_path(LOCAL_PATH, False)

            self.assertTrue(RESULT)
            self.assertFalse(LOCAL_PATH.exists())

# --------------------------------------------------------------------------
# This test confirms local file index building skips files whose metadata
# cannot be read.
# --------------------------------------------------------------------------
    def test_build_local_file_index_skips_stat_failures(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            FILE_PATH = ROOT_DIR / "docs" / "keep.txt"
            FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            FILE_PATH.write_text("keep", encoding="utf-8")

            with patch("app.syncer.iter_local_files", return_value=[FILE_PATH]):
                with patch.object(Path, "stat", side_effect=OSError("stat failed")):
                    RESULT = build_local_file_index(ROOT_DIR)

        self.assertEqual(RESULT, {})

# --------------------------------------------------------------------------
# This test confirms local-file reconciliation returns false when metadata is
# missing or invalid.
# --------------------------------------------------------------------------
    def test_is_local_file_aligned_with_remote_handles_missing_or_invalid_time(self) -> None:
        ENTRY = RemoteEntry("docs/a.txt", False, 10, "bad")

        self.assertFalse(is_local_file_aligned_with_remote(ENTRY, None))
        self.assertFalse(is_local_file_aligned_with_remote(ENTRY, (10, 1.0)))

# --------------------------------------------------------------------------
# This test confirms remote modified parsing handles blank, UTC-suffixed,
# naive, and invalid timestamp forms.
# --------------------------------------------------------------------------
    def test_parse_remote_modified_epoch_handles_supported_and_invalid_formats(self) -> None:
        self.assertIsNone(parse_remote_modified_epoch(""))
        self.assertIsNone(parse_remote_modified_epoch("not-a-date"))
        self.assertIsNotNone(parse_remote_modified_epoch("2026-03-12T10:15:30Z"))
        self.assertIsNotNone(parse_remote_modified_epoch("2026-03-12T10:15:30"))

# --------------------------------------------------------------------------
# This test confirms timestamp application skips invalid remote timestamps
# and emits a debug reason when logging is enabled.
# --------------------------------------------------------------------------
    def test_apply_remote_modified_time_skips_invalid_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOCAL_PATH = Path(TMPDIR) / "docs" / "a.txt"
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_PATH.write_text("x", encoding="utf-8")

            with patch("app.syncer.log_line") as LOG_LINE:
                RESULT = apply_remote_modified_time(LOCAL_PATH, "bad", Path(TMPDIR) / "worker.log")

        self.assertFalse(RESULT)
        self.assertTrue(any("Timestamp skipped parse:" in CALL.args[2] for CALL in LOG_LINE.call_args_list))

# --------------------------------------------------------------------------
# This test confirms timestamp application logs and returns false when file
# metadata cannot be updated.
# --------------------------------------------------------------------------
    def test_apply_remote_modified_time_logs_utime_error(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOCAL_PATH = Path(TMPDIR) / "docs" / "a.txt"
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_PATH.write_text("x", encoding="utf-8")

            with patch("app.syncer.os.utime", side_effect=OSError("utime failed")):
                with patch("app.syncer.log_line") as LOG_LINE:
                    RESULT = apply_remote_modified_time(
                        LOCAL_PATH,
                        "2026-03-12T10:15:30Z",
                        Path(TMPDIR) / "worker.log",
                    )

        self.assertFalse(RESULT)
        self.assertTrue(any("Timestamp apply error:" in CALL.args[2] for CALL in LOG_LINE.call_args_list))

# --------------------------------------------------------------------------
# This test confirms transfer helper short-circuits when the caller already
# knows transfer is not required.
# --------------------------------------------------------------------------
    def test_transfer_if_required_returns_skipped_when_transfer_not_needed(self) -> None:
        ENTRY = RemoteEntry("docs/a.txt", False, 10, "m1")

        RESULT = transfer_if_required(FakeClient([], {}), Path("/tmp"), ENTRY, False)

        self.assertEqual(RESULT, TransferResult(True, 1, "skipped"))

# --------------------------------------------------------------------------
# This test confirms known local package directories return package success
# when package export succeeds.
# --------------------------------------------------------------------------
    def test_transfer_if_required_returns_package_for_existing_local_package_dir(self) -> None:
        ENTRY = RemoteEntry("docs/archive.bundle", False, 10, "m1")
        CLIENT = FakeClient([ENTRY], {})
        CLIENT.package_results["docs/archive.bundle"] = True

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            PACKAGE_DIR = ROOT_DIR / "docs" / "archive.bundle"
            PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

            RESULT = transfer_if_required(CLIENT, ROOT_DIR, ENTRY, True)

        self.assertEqual(RESULT, TransferResult(True, 1, "package"))

# --------------------------------------------------------------------------
# This test confirms known local package directories surface package-fallback
# failure reasons when export fails without reconciliation markers.
# --------------------------------------------------------------------------
    def test_transfer_if_required_returns_package_reason_for_existing_local_package_dir(self) -> None:
        ENTRY = RemoteEntry("docs/archive.bundle", False, 10, "m1")
        CLIENT = FakeClient([ENTRY], {})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_download_failed"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            PACKAGE_DIR = ROOT_DIR / "docs" / "archive.bundle"
            PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

            RESULT = transfer_if_required(CLIENT, ROOT_DIR, ENTRY, True)

        self.assertEqual(RESULT, TransferResult(False, 1, "package_download_failed"))

# --------------------------------------------------------------------------
# This test confirms known package paths return package success when package
# export succeeds without a pre-existing local directory.
# --------------------------------------------------------------------------
    def test_transfer_if_required_returns_package_for_known_package_path(self) -> None:
        ENTRY = RemoteEntry("docs/archive.bundle", False, 10, "m1")
        CLIENT = FakeClient([ENTRY], {})
        CLIENT.package_results["docs/archive.bundle"] = True

        with tempfile.TemporaryDirectory() as TMPDIR:
            RESULT = transfer_if_required(CLIENT, Path(TMPDIR), ENTRY, True)

        self.assertEqual(RESULT, TransferResult(True, 1, "package"))

# --------------------------------------------------------------------------
# This test confirms transfer failure reason helper handles equal and missing
# reason tokens predictably.
# --------------------------------------------------------------------------
    def test_get_transfer_failure_reason_handles_equal_and_missing_tokens(self) -> None:
        self.assertEqual(get_transfer_failure_reason("download_failed", "download_failed"), "download_failed")
        self.assertEqual(get_transfer_failure_reason("", "package_download_failed"), "package_download_failed")
        self.assertEqual(get_transfer_failure_reason("download_failed", ""), "download_failed")

# --------------------------------------------------------------------------
# This test confirms normalised transfer reasons fall back to "unknown"
# for blank or separator-only values.
# --------------------------------------------------------------------------
    def test_normalise_transfer_reason_returns_unknown_for_blank_values(self) -> None:
        self.assertEqual(normalise_transfer_reason(""), "unknown")
        self.assertEqual(normalise_transfer_reason(" ; fallback=package"), "unknown")

# --------------------------------------------------------------------------
# This test confirms known package-path detection rejects blank values.
# --------------------------------------------------------------------------
    def test_is_known_package_path_rejects_blank_value(self) -> None:
        self.assertFalse(is_known_package_path(" "))

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
# This test confirms slow-directory summaries use the top three entries only.
# --------------------------------------------------------------------------
    def test_format_slow_directory_summary_limits_output_to_top_three(self) -> None:
        STATS = build_empty_traversal_stats_snapshot()
        STATS["slow_dirs"] = [
            {"path": "a", "duration_seconds": 9.1},
            {"path": "b", "duration_seconds": 8.2},
            {"path": "c", "duration_seconds": 7.3},
            {"path": "d", "duration_seconds": 6.4},
        ]

        RESULT = format_slow_directory_summary(STATS)

        self.assertEqual(RESULT, "a: 9.1s, b: 8.2s, c: 7.3s")

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
            RESULT = transfer_if_required(
                CLIENT,
                Path(TMPDIR),
                ENTRY,
                True,
            )

        self.assertEqual(RESULT, TransferResult(False, 1, "download_failed"))

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

            RESULT = transfer_if_required(
                CLIENT,
                ROOT_DIR,
                ENTRY,
                True,
            )

        self.assertEqual(RESULT, TransferResult(True, 1, "package_reconciled"))
        self.assertEqual(CLIENT.download_calls, 0)
        self.assertEqual(CLIENT.package_calls, 1)

# --------------------------------------------------------------------------
# This test confirms a stray local directory does not cause a normal remote
# file to be treated as a package reconciliation success.
# --------------------------------------------------------------------------
    def test_transfer_if_required_replaces_local_directory_conflict_for_normal_file(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/report.txt",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {"docs/report.txt": True})

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            CONFLICT_DIR = ROOT_DIR / "docs" / "report.txt"
            CONFLICT_DIR.mkdir(parents=True, exist_ok=True)

            RESULT = transfer_if_required(
                CLIENT,
                ROOT_DIR,
                ENTRY,
                True,
            )

        self.assertEqual(RESULT, TransferResult(True, 1, "file"))
        self.assertEqual(CLIENT.download_calls, 1)
        self.assertEqual(CLIENT.package_calls, 0)

# --------------------------------------------------------------------------
# This test confirms known package paths replace conflicting local files
# before package export.
# --------------------------------------------------------------------------
    def test_transfer_if_required_replaces_local_file_conflict_for_package_path(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {})
        CLIENT.package_results["docs/archive.bundle"] = True

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            CONFLICT_FILE = ROOT_DIR / "docs" / "archive.bundle"
            CONFLICT_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFLICT_FILE.write_text("file", encoding="utf-8")

            RESULT = transfer_if_required(
                CLIENT,
                ROOT_DIR,
                ENTRY,
                True,
            )

            self.assertEqual(RESULT, TransferResult(True, 1, "package"))
            self.assertEqual(CLIENT.download_calls, 0)
            self.assertEqual(CLIENT.package_calls, 1)

# --------------------------------------------------------------------------
# This test confirms local-missing package paths fall back to direct file
# download when package metadata is unavailable.
# --------------------------------------------------------------------------
    def test_transfer_if_required_downloads_known_package_as_file_when_metadata_missing(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/archive.bundle",
            is_dir=False,
            size=4,
            modified="2026-03-07T12:00:00Z",
        )
        CLIENT = FakeClient([ENTRY], {"docs/archive.bundle": True})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_item_missing"

        with tempfile.TemporaryDirectory() as TMPDIR:
            RESULT = transfer_if_required(
                CLIENT,
                Path(TMPDIR),
                ENTRY,
                True,
            )

        self.assertEqual(RESULT, TransferResult(True, 1, "file"))
        self.assertEqual(CLIENT.download_calls, 1)
        self.assertEqual(CLIENT.package_calls, 1)

# --------------------------------------------------------------------------
# This test confirms local-missing package paths remain terminal failures
# when package metadata and direct file download both fail.
# --------------------------------------------------------------------------
    def test_transfer_if_required_fails_known_package_when_direct_fallback_fails(self) -> None:
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
            RESULT = transfer_if_required(
                CLIENT,
                Path(TMPDIR),
                ENTRY,
                True,
            )

        self.assertEqual(
            RESULT,
            TransferResult(False, 1, "download_failed; fallback=package_item_missing"),
        )
        self.assertEqual(CLIENT.download_calls, 1)
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
            with patch("app.syncer.log_line") as LOG_LINE:
                SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertEqual(SUMMARY.total_files, 1)
        self.assertEqual(SUMMARY.transferred_files, 0)
        self.assertEqual(SUMMARY.transferred_bytes, 0)
        self.assertEqual(SUMMARY.skipped_files, 0)
        self.assertEqual(SUMMARY.error_files, 1)
        self.assertFalse(SUMMARY.session_invalid)
        self.assertNotIn("docs/explode.txt", NEW_MANIFEST)
        self.assertTrue(
            any(
                CALL.args[1] == "error"
                and "File transfer worker failed:" in CALL.args[2]
                for CALL in LOG_LINE.call_args_list
            )
        )

# --------------------------------------------------------------------------
# This test confirms a session-invalid download failure at the worker
# boundary is classified and surfaced on the sync summary.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_flags_session_invalid_from_download_failure(
        self,
    ) -> None:
        ENTRIES = [
            RemoteEntry("docs/session_dead.txt", False, 1, "2026-03-09T00:00:00Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {})

        with tempfile.TemporaryDirectory() as TMPDIR:
            SUMMARY, _NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertEqual(SUMMARY.error_files, 1)
        self.assertTrue(SUMMARY.session_invalid)

# --------------------------------------------------------------------------
# This test confirms a session-invalid traversal failure is carried through
# to the sync summary even when no download fails.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_flags_session_invalid_from_traversal_stats(
        self,
    ) -> None:
        ENTRIES = [RemoteEntry("docs/file.txt", False, 1, "2026-03-09T00:00:00Z")]
        CLIENT = FakeClient(ENTRIES, {"docs/file.txt": True})
        CLIENT.traversal_stats["dir_session_invalid"] = True

        with tempfile.TemporaryDirectory() as TMPDIR:
            SUMMARY, _NEW_MANIFEST = perform_incremental_sync(CLIENT, Path(TMPDIR), {})

        self.assertTrue(SUMMARY.session_invalid)

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
        self.assertTrue(any("Delete phase decision: enabled=False" in LINE for LINE in DEBUG_LINES))

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
        self.assertTrue(any("Reconciliation detail:" in LINE for LINE in DEBUG_LINES))
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

            def get_traversal_stats_snapshot(self):
                return build_empty_traversal_stats_snapshot()

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

        CLIENT = SlowClient()

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS", 0.01):
                with patch("app.syncer.log_line") as LOG_LINE:
                    perform_incremental_sync(CLIENT, Path(TMPDIR), {}, 0, LOG_FILE)

        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(any("Traversal progress detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Traversal delta detail:" in LINE for LINE in DEBUG_LINES))
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
# This test confirms package metadata failures fall back to direct file
# download and persist normal file metadata.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_persists_file_metadata_for_package_direct_fallback(self) -> None:
        ENTRIES = [
            RemoteEntry("docs/archive.bundle", False, 4, "2026-03-12T10:15:30Z"),
        ]
        CLIENT = FakeClient(ENTRIES, {"docs/archive.bundle": True})
        CLIENT.package_results["docs/archive.bundle"] = False
        CLIENT.package_failure_reasons["docs/archive.bundle"] = "package_item_missing"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            SUMMARY, NEW_MANIFEST = perform_incremental_sync(CLIENT, ROOT_DIR, {})
            LOCAL_FILE = ROOT_DIR / "docs" / "archive.bundle"
            LOCAL_FILE_EXISTS = LOCAL_FILE.is_file()

        self.assertEqual(CLIENT.package_calls, 1)
        self.assertEqual(CLIENT.download_calls, 1)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertEqual(SUMMARY.error_files, 0)
        self.assertTrue(LOCAL_FILE_EXISTS)
        self.assertEqual(NEW_MANIFEST["docs/archive.bundle"]["entry_kind"], "file")
        self.assertNotIn("package_state", NEW_MANIFEST["docs/archive.bundle"])
        self.assertNotIn("package_signature", NEW_MANIFEST["docs/archive.bundle"])

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
            self.assertEqual(SUMMARY.delete_errors, 0)
            self.assertEqual(SUMMARY.deleted_files, 2)
            self.assertEqual(SUMMARY.deleted_directories, 1)
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
# This test confirms delete-phase errors are returned in the sync summary.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_reports_delete_phase_errors(self) -> None:
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

            with patch("app.syncer.delete_removed_local_paths", return_value=(1, 0, 2)):
                with patch("app.syncer.log_line") as LOG_LINE:
                    SUMMARY, _ = perform_incremental_sync(
                        CLIENT,
                        ROOT_DIR,
                        MANIFEST,
                        LOG_FILE=ROOT_DIR / "worker.log",
                        BACKUP_DELETE_REMOVED=True,
                    )

        self.assertEqual(SUMMARY.error_files, 0)
        self.assertEqual(SUMMARY.delete_errors, 2)
        DEBUG_LINES = [CALL.args[2] for CALL in LOG_LINE.call_args_list if CALL.args[1] == "debug"]
        self.assertTrue(
            any(
                "Delete phase decision: enabled=True, will_run=True" in LINE
                for LINE in DEBUG_LINES
            )
        )

# --------------------------------------------------------------------------
# This test confirms non-empty directory delete errors are treated as benign.
# --------------------------------------------------------------------------
    def test_delete_removed_local_paths_ignores_non_empty_directory_error(self) -> None:
        FILES: list[RemoteEntry] = []
        DIRECTORIES: list[RemoteEntry] = []

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            STALE_DIR = ROOT_DIR / "docs"
            STALE_DIR.mkdir(parents=True, exist_ok=True)
            (STALE_DIR / "keep.txt").write_text("keep", encoding="utf-8")

            with patch.object(Path, "rmdir", side_effect=OSError(errno.ENOTEMPTY, "not empty")):
                DELETED_FILES, DELETED_DIRS, ERRORS = delete_removed_local_paths(
                    ROOT_DIR,
                    FILES,
                    DIRECTORIES,
                )

        self.assertEqual(DELETED_FILES, 1)
        self.assertEqual(DELETED_DIRS, 0)
        self.assertEqual(ERRORS, 0)

# --------------------------------------------------------------------------
# This test confirms unexpected directory delete errors are counted and logged.
# --------------------------------------------------------------------------
    def test_delete_removed_local_paths_counts_unexpected_directory_error(self) -> None:
        FILES: list[RemoteEntry] = []
        DIRECTORIES: list[RemoteEntry] = []

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            STALE_DIR = ROOT_DIR / "docs"
            STALE_DIR.mkdir(parents=True, exist_ok=True)

            with patch.object(Path, "rmdir", side_effect=PermissionError(errno.EACCES, "denied")):
                with patch("app.syncer.log_line") as LOG_LINE:
                    DELETED_FILES, DELETED_DIRS, ERRORS = delete_removed_local_paths(
                        ROOT_DIR,
                        FILES,
                        DIRECTORIES,
                        ROOT_DIR / "worker.log",
                    )

        self.assertEqual(DELETED_FILES, 0)
        self.assertEqual(DELETED_DIRS, 0)
        self.assertEqual(ERRORS, 1)
        self.assertTrue(any("Directory delete error:" in CALL.args[2] for CALL in LOG_LINE.call_args_list))

# --------------------------------------------------------------------------
# This test confirms delete-removed mode preserves live package roots and
# descendants while still deleting stale siblings.
# --------------------------------------------------------------------------
    def test_delete_removed_local_paths_preserves_live_package_directories(self) -> None:
        FILES = [
            RemoteEntry("docs/archive.bundle", False, 10, "2026-03-11T00:00:00Z"),
            RemoteEntry("docs/notes.pages", False, 20, "2026-03-11T00:00:00Z"),
            RemoteEntry("docs/keep.txt", False, 4, "2026-03-11T00:00:00Z"),
        ]
        DIRECTORIES = [RemoteEntry("docs", True, 0, "2026-03-11T00:00:00Z")]

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            (ROOT_DIR / "docs" / "archive.bundle" / "nested").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "notes.pages").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "notes.pages" / "index.xml").write_text("live", encoding="utf-8")
            (ROOT_DIR / "docs" / "archive.bundle" / "nested" / "asset.bin").write_text("live", encoding="utf-8")
            (ROOT_DIR / "docs" / "keep.txt").write_text("keep", encoding="utf-8")
            (ROOT_DIR / "docs" / "stale.txt").write_text("stale", encoding="utf-8")
            (ROOT_DIR / "docs" / "stale_dir").mkdir(parents=True, exist_ok=True)
            (ROOT_DIR / "docs" / "stale_dir" / "old.txt").write_text("old", encoding="utf-8")

            DELETED_FILES, DELETED_DIRS, ERRORS = delete_removed_local_paths(
                ROOT_DIR,
                FILES,
                DIRECTORIES,
            )

            self.assertEqual(DELETED_FILES, 2)
            self.assertEqual(DELETED_DIRS, 1)
            self.assertEqual(ERRORS, 0)
            self.assertTrue((ROOT_DIR / "docs" / "archive.bundle").exists())
            self.assertTrue((ROOT_DIR / "docs" / "archive.bundle" / "nested" / "asset.bin").exists())
            self.assertTrue((ROOT_DIR / "docs" / "notes.pages" / "index.xml").exists())
            self.assertFalse((ROOT_DIR / "docs" / "stale.txt").exists())
            self.assertFalse((ROOT_DIR / "docs" / "stale_dir").exists())

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
# This test confirms a failed drive root fetch (which "list_entries" now
# catches and reports as a hard failure instead of raising) still marks the
# run incomplete and skips manifest save and delete phase. Without this,
# "BACKUP_DELETE_REMOVED" deployments could mistake a drive root failure for
# an empty iCloud Drive and delete the entire local backup tree.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_skips_delete_when_drive_root_unavailable(self) -> None:
        CLIENT = FakeClient([], {})
        CLIENT.traversal_stats["dir_hard_failures"] = 1
        CLIENT.traversal_stats["dir_failure_samples"] = [
            {
                "path": "/",
                "status": "hard_failure",
                "reason": "drive_root_unavailable: ConnectTimeout: connect timeout",
            }
        ]

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            EXISTING_PATH = ROOT_DIR / "docs" / "keep.txt"
            EXISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
            EXISTING_PATH.write_text("keep", encoding="utf-8")

            SUMMARY, NEW_MANIFEST = perform_incremental_sync(
                CLIENT,
                ROOT_DIR,
                {"docs/keep.txt": {"size": 4, "modified": "2026-03-07T12:00:00Z"}},
                BACKUP_DELETE_REMOVED=True,
            )

            self.assertTrue(EXISTING_PATH.exists())

        self.assertFalse(SUMMARY.traversal_complete)
        self.assertEqual(SUMMARY.traversal_hard_failures, 1)
        self.assertTrue(SUMMARY.delete_phase_skipped)
        self.assertEqual(NEW_MANIFEST, {})

# --------------------------------------------------------------------------
# This test confirms a traversal worker stall downgrades into an incomplete
# sync run, keeping whatever the client already discovered rather than
# discarding it.
# --------------------------------------------------------------------------
    def test_perform_incremental_sync_downgrades_traversal_stall_to_partial_run(self) -> None:
        class StalledClient:
            def list_entries(self):
                return [RemoteEntry("docs/found.txt", False, 4, "2026-03-09T00:00:00Z")]

            def get_traversal_stats_snapshot(self):
                return (
                    build_empty_traversal_stats_snapshot()
                    | {
                        "dir_hard_failures": 1,
                        "dir_failure_samples": [
                            {
                                "path": "docs/stuck",
                                "status": "hard_failure",
                                "reason": "worker_timeout_after_190.0s",
                            }
                        ],
                    }
                )

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
                LOCAL_PATH.write_bytes(b"data")
                return DownloadResult(True)

            def download_package_tree(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                return DownloadResult(True)

        CLIENT = StalledClient()

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            LOG_FILE = ROOT_DIR / "pyiclodoc-drive-worker.log"
            STALE_PATH = ROOT_DIR / "docs" / "stale.txt"
            STALE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STALE_PATH.write_text("stale", encoding="utf-8")

            with patch("app.syncer.log_line") as LOG_LINE:
                SUMMARY, NEW_MANIFEST = perform_incremental_sync(
                    CLIENT,
                    ROOT_DIR,
                    {},
                    LOG_FILE=LOG_FILE,
                    BACKUP_DELETE_REMOVED=True,
                )

            self.assertTrue(STALE_PATH.exists())
            self.assertTrue((ROOT_DIR / "docs" / "found.txt").exists())

        self.assertIn("docs/found.txt", NEW_MANIFEST)
        self.assertFalse(SUMMARY.traversal_complete)
        self.assertEqual(SUMMARY.traversal_hard_failures, 1)
        self.assertTrue(SUMMARY.delete_phase_skipped)
        self.assertEqual(SUMMARY.total_files, 1)
        self.assertEqual(SUMMARY.transferred_files, 1)
        self.assertTrue(
            any(
                CALL.args[1] == "error"
                and "Traversal incomplete. Delete phase and manifest save will be skipped"
                in CALL.args[2]
                for CALL in LOG_LINE.call_args_list
            )
        )

# --------------------------------------------------------------------------
# This test confirms traversal timeout stays explicit at the progress wrapper
# instead of being flattened into a clean empty listing.
# --------------------------------------------------------------------------
    def test_list_entries_with_progress_returns_after_advancing_stats(self) -> None:
        class AdvancingClient:
            def __init__(self):
                self.started_epoch = time.monotonic()

            def list_entries(self):
                time.sleep(0.05)
                return [RemoteEntry("docs/file.txt", False, 10, "m1")]

            def get_traversal_stats_snapshot(self):
                elapsed_seconds = time.monotonic() - self.started_epoch
                if elapsed_seconds < 0.015:
                    return build_empty_traversal_stats_snapshot()

                return (
                    build_empty_traversal_stats_snapshot()
                    | {
                        "entries_discovered": 1,
                        "files_discovered": 1,
                        "dir_reads": 1,
                    }
                )

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.TRAVERSAL_PROGRESS_LOG_INTERVAL_SECONDS", 0.01):
                with patch("app.syncer.log_line") as LOG_LINE:
                    RESULT = list_entries_with_progress(
                        AdvancingClient(),
                        LOG_FILE,
                        time.monotonic(),
                    )

        self.assertEqual([ENTRY.path for ENTRY in RESULT], ["docs/file.txt"])
        DEBUG_LINES = [
            CALL.args[2]
            for CALL in LOG_LINE.call_args_list
            if CALL.args[1] == "debug"
        ]
        self.assertTrue(any("Traversal progress detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("Traversal delta detail:" in LINE for LINE in DEBUG_LINES))
        self.assertTrue(any("dir_reads=1" in LINE for LINE in DEBUG_LINES))

# --------------------------------------------------------------------------
# This test confirms a successful empty traversal still returns a clean empty
# list without being mistaken for traversal failure.
# --------------------------------------------------------------------------
    def test_list_entries_with_progress_returns_empty_list_for_successful_empty_traversal(self) -> None:
        class EmptyClient:
            def list_entries(self):
                return []

            def get_traversal_stats_snapshot(self):
                return build_empty_traversal_stats_snapshot()

        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "pyiclodoc-drive-worker.log"
            with patch("app.syncer.log_line") as LOG_LINE:
                RESULT = list_entries_with_progress(
                    EmptyClient(),
                    LOG_FILE,
                    time.monotonic(),
                )

        self.assertEqual(RESULT, [])
        self.assertFalse(
            any(
                CALL.args[1] == "error"
                and "Traversal failed before completion:" in CALL.args[2]
                for CALL in LOG_LINE.call_args_list
            )
        )

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

            def get_traversal_stats_snapshot(self):
                return build_empty_traversal_stats_snapshot()

            def download_file(self, REMOTE_PATH, LOCAL_PATH):
                _ = REMOTE_PATH
                _ = LOCAL_PATH
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("Service Unavailable (503)")
                return DownloadResult(True)

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
