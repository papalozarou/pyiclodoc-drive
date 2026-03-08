"""This test module verifies incremental sync decisions and first-run safety helper behaviour.

Reference for Python file permission bits:
https://docs.python.org/3/library/os.html#os.stat_result
"""

from pathlib import Path
import tempfile
import unittest

from dataclasses import dataclass

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.syncer import collect_mismatches, count_modes, most_common_mode, needs_transfer


@dataclass(frozen=True)
class RemoteEntry:
    """This test data class mirrors the production remote-entry shape used by sync helpers."""

    path: str
    is_dir: bool
    size: int
    modified: str


class TestSyncerHelpers(unittest.TestCase):
    """These tests ensure manifest diffing and permission checks behave predictably."""

    # ------------------------------------------------------------------------------
    # This test confirms a file transfer is requested when no manifest entry exists.
    # ------------------------------------------------------------------------------
    def test_needs_transfer_for_new_file(self) -> None:
        ENTRY = RemoteEntry(
            path="docs/a.txt",
            is_dir=False,
            size=10,
            modified="2026-03-07T12:00:00Z",
        )

        self.assertTrue(needs_transfer(ENTRY, {}))

    # ------------------------------------------------------------------------------
    # This test confirms unchanged file metadata does not trigger a transfer.
    # ------------------------------------------------------------------------------
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

    # ------------------------------------------------------------------------------
    # This test confirms mode counting and mismatch detection identify outlier permissions.
    # ------------------------------------------------------------------------------
    def test_mode_counting_and_mismatch_detection(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            BASE = Path(TMPDIR)
            FILE_ONE = BASE / "one.txt"
            FILE_TWO = BASE / "two.txt"
            FILE_THREE = BASE / "three.txt"

            FILE_ONE.write_text("1", encoding="utf-8")
            FILE_TWO.write_text("2", encoding="utf-8")
            FILE_THREE.write_text("3", encoding="utf-8")

            FILE_ONE.chmod(0o644)
            FILE_TWO.chmod(0o644)
            FILE_THREE.chmod(0o600)

            FILES = [FILE_ONE, FILE_TWO, FILE_THREE]
            COUNTS = count_modes(FILES)
            EXPECTED_MODE = most_common_mode(COUNTS)
            MISMATCHES = collect_mismatches(FILES, EXPECTED_MODE)

            self.assertEqual(EXPECTED_MODE, "0o644")
            self.assertEqual(COUNTS.get("0o644"), 2)
            self.assertTrue(any("three.txt" in LINE for LINE in MISMATCHES))


if __name__ == "__main__":
    unittest.main()
