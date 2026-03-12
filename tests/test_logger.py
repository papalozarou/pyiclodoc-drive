# ------------------------------------------------------------------------------
# This test module verifies timestamped logging output and file writes.
# ------------------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import gzip
import os
from unittest.mock import patch

from app import logger


# ------------------------------------------------------------------------------
# These tests validate logger timestamp formatting and output behaviour.
# ------------------------------------------------------------------------------
class TestLogger(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms timestamp rendering includes date/time and timezone.
# --------------------------------------------------------------------------
    def test_get_timestamp_formats_expected_value(self) -> None:
        FIXED_NOW = datetime(2026, 3, 9, 12, 34, 56, tzinfo=timezone.utc)

        with patch.object(logger, "now_local", return_value=FIXED_NOW):
            RESULT = logger.get_timestamp()

        self.assertEqual(RESULT, "2026-03-09 12:34:56 UTC")

# --------------------------------------------------------------------------
# This test confirms log lines are printed and appended to the log file.
# --------------------------------------------------------------------------
    def test_log_line_writes_and_prints(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                with patch("builtins.print") as PRINT:
                    logger.log_line(LOG_FILE, "info", "Backup starting.")

            EXPECTED = "[2026-03-09 12:34:56 UTC] [INFO] Backup starting."
            PRINT.assert_called_once_with(EXPECTED, flush=True)
            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms debug lines are suppressed when LOG_LEVEL is info.
# --------------------------------------------------------------------------
    def test_log_line_suppresses_debug_when_info_level(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "info"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "debug", "Hidden debug.")

            PRINT.assert_not_called()
            self.assertFalse(LOG_FILE.exists())

# --------------------------------------------------------------------------
# This test confirms debug lines are emitted when LOG_LEVEL is debug.
# --------------------------------------------------------------------------
    def test_log_line_emits_debug_when_debug_level(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "debug"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "debug", "Visible debug.")

            EXPECTED = "[2026-03-09 12:34:56 UTC] [DEBUG] Visible debug."
            PRINT.assert_called_once_with(EXPECTED, flush=True)
            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms info lines are emitted when LOG_LEVEL is info.
# --------------------------------------------------------------------------
    def test_log_line_emits_info_when_info_level(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "info"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "info", "Visible info.")

            EXPECTED = "[2026-03-09 12:34:56 UTC] [INFO] Visible info."
            PRINT.assert_called_once_with(EXPECTED, flush=True)
            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms error lines are emitted at info level threshold.
# --------------------------------------------------------------------------
    def test_log_line_emits_error_when_info_level(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "info"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "error", "Visible error.")

            EXPECTED = "[2026-03-09 12:34:56 UTC] [ERROR] Visible error."
            PRINT.assert_called_once_with(
                f"{logger.ANSI_RED}{EXPECTED}{logger.ANSI_RESET}",
                flush=True,
            )
            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms console formatting leaves non-error lines unchanged.
# --------------------------------------------------------------------------
    def test_format_console_line_returns_plain_line_for_info(self) -> None:
        LINE = "[2026-03-09 12:34:56 UTC] [INFO] Visible info."
        self.assertEqual(logger.format_console_line(LINE, "INFO"), LINE)

# --------------------------------------------------------------------------
# This test confirms console formatting wraps error lines with ANSI red.
# --------------------------------------------------------------------------
    def test_format_console_line_wraps_error_line_in_red(self) -> None:
        LINE = "[2026-03-09 12:34:56 UTC] [ERROR] Visible error."
        RESULT = logger.format_console_line(LINE, "ERROR")
        self.assertEqual(RESULT, f"{logger.ANSI_RED}{LINE}{logger.ANSI_RESET}")

# --------------------------------------------------------------------------
# This test confirms invalid LOG_LEVEL values fall back to info threshold.
# --------------------------------------------------------------------------
    def test_log_line_invalid_log_level_falls_back_to_info(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "invalid"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "debug", "Hidden debug.")

            PRINT.assert_not_called()
            self.assertFalse(LOG_FILE.exists())

# --------------------------------------------------------------------------
# This test confirms get_log_level falls back to info on invalid values.
# --------------------------------------------------------------------------
    def test_get_log_level_fallbacks(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(logger.get_log_level(), "info")

        with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}):
            self.assertEqual(logger.get_log_level(), "debug")

        with patch.dict("os.environ", {"LOG_LEVEL": "garbage"}):
            self.assertEqual(logger.get_log_level(), "info")

# --------------------------------------------------------------------------
# This test confirms should_log threshold decisions across levels.
# --------------------------------------------------------------------------
    def test_should_log_threshold_behaviour(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "info"}):
            self.assertFalse(logger.should_log("debug"))
            self.assertTrue(logger.should_log("info"))
            self.assertTrue(logger.should_log("error"))

        with patch.dict("os.environ", {"LOG_LEVEL": "debug"}):
            self.assertTrue(logger.should_log("debug"))
            self.assertTrue(logger.should_log("info"))
            self.assertTrue(logger.should_log("error"))

# --------------------------------------------------------------------------
# This test confirms daily rollover defaults to enabled and parses toggles.
# --------------------------------------------------------------------------
    def test_get_log_rotate_daily_parsing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(logger.get_log_rotate_daily())

        with patch.dict("os.environ", {"LOG_ROTATE_DAILY": "false"}, clear=True):
            self.assertFalse(logger.get_log_rotate_daily())

        with patch.dict("os.environ", {"LOG_ROTATE_DAILY": "garbage"}, clear=True):
            self.assertTrue(logger.get_log_rotate_daily())

# --------------------------------------------------------------------------
# This test confirms size and retention settings parse with safe defaults.
# --------------------------------------------------------------------------
    def test_log_rotation_config_defaults_and_invalids(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(logger.get_log_rotate_max_bytes(), 100 * 1024 * 1024)
            self.assertEqual(logger.get_log_rotate_keep_days(), 14)

        with patch.dict(
            "os.environ",
            {"LOG_ROTATE_MAX_MIB": "2", "LOG_ROTATE_KEEP_DAYS": "7"},
            clear=True,
        ):
            self.assertEqual(logger.get_log_rotate_max_bytes(), 2 * 1024 * 1024)
            self.assertEqual(logger.get_log_rotate_keep_days(), 7)

        with patch.dict(
            "os.environ",
            {"LOG_ROTATE_MAX_MIB": "zero", "LOG_ROTATE_KEEP_DAYS": "-1"},
            clear=True,
        ):
            self.assertEqual(logger.get_log_rotate_max_bytes(), 100 * 1024 * 1024)
            self.assertEqual(logger.get_log_rotate_keep_days(), 14)

# --------------------------------------------------------------------------
# This test confirms log_line rotates oversized logs, writes new entries,
# and leaves a compressed archive.
# --------------------------------------------------------------------------
    def test_log_line_rotates_oversized_log_and_writes_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"
            LOG_FILE.write_text("old\n", encoding="utf-8")

            with patch.object(logger, "get_log_rotate_max_bytes", return_value=1):
                with patch.object(logger, "get_log_rotate_daily", return_value=False):
                    with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                        with patch("builtins.print"):
                            logger.log_line(LOG_FILE, "info", "Backup starting.")

            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, "[2026-03-09 12:34:56 UTC] [INFO] Backup starting.")
            ARCHIVES = list(Path(TMPDIR).glob("iclouddd-worker.*.log.gz"))
            self.assertEqual(len(ARCHIVES), 1)
            with gzip.open(ARCHIVES[0], "rt", encoding="utf-8") as HANDLE:
                self.assertEqual(HANDLE.read().strip(), "old")

# --------------------------------------------------------------------------
# This test confirms daily rollover rotates logs from a previous local date.
# --------------------------------------------------------------------------
    def test_log_line_rotates_previous_day_log(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"
            LOG_FILE.write_text("old-day\n", encoding="utf-8")
            OLD_EPOCH = datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(LOG_FILE, (OLD_EPOCH, OLD_EPOCH))

            with patch.object(
                logger,
                "now_local",
                return_value=datetime(2026, 3, 9, 10, 0, 0, tzinfo=timezone.utc),
            ):
                with patch.object(logger, "get_log_rotate_max_bytes", return_value=10 * 1024 * 1024):
                    with patch.object(logger, "get_timestamp", return_value="2026-03-09 10:00:00 UTC"):
                        with patch.dict("os.environ", {"LOG_ROTATE_DAILY": "true"}, clear=True):
                            with patch("builtins.print"):
                                logger.log_line(LOG_FILE, "info", "new-day")

            ARCHIVES = list(Path(TMPDIR).glob("iclouddd-worker.*.log.gz"))
            self.assertEqual(len(ARCHIVES), 1)
            with gzip.open(ARCHIVES[0], "rt", encoding="utf-8") as HANDLE:
                self.assertEqual(HANDLE.read().strip(), "old-day")

# --------------------------------------------------------------------------
# This test confirms old rotated archives are removed by retention policy.
# --------------------------------------------------------------------------
    def test_prune_rotated_logs_removes_expired_archives(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "iclouddd-worker.log"
            OLD_ARCHIVE = Path(TMPDIR) / "iclouddd-worker.20200101-000000.log.gz"
            NEW_ARCHIVE = Path(TMPDIR) / "iclouddd-worker.20990101-000000.log.gz"
            OLD_ARCHIVE.write_bytes(b"x")
            NEW_ARCHIVE.write_bytes(b"y")

            FIXED_NOW = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
            OLD_EPOCH = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            NEW_EPOCH = datetime(2026, 3, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(OLD_ARCHIVE, (OLD_EPOCH, OLD_EPOCH))
            os.utime(NEW_ARCHIVE, (NEW_EPOCH, NEW_EPOCH))

            with patch.object(logger, "now_local", return_value=FIXED_NOW):
                with patch.object(logger, "get_log_rotate_keep_days", return_value=14):
                    logger.prune_rotated_logs(LOG_FILE)

            self.assertFalse(OLD_ARCHIVE.exists())
            self.assertTrue(NEW_ARCHIVE.exists())


if __name__ == "__main__":
    unittest.main()
