# ------------------------------------------------------------------------------
# This test module verifies timestamped logging output and file writes.
# ------------------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
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
            LOG_FILE = Path(TMPDIR) / "worker.log"

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
            LOG_FILE = Path(TMPDIR) / "worker.log"

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
            LOG_FILE = Path(TMPDIR) / "worker.log"

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
            LOG_FILE = Path(TMPDIR) / "worker.log"

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
            LOG_FILE = Path(TMPDIR) / "worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "info"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "error", "Visible error.")

            EXPECTED = "[2026-03-09 12:34:56 UTC] [ERROR] Visible error."
            PRINT.assert_called_once_with(EXPECTED, flush=True)
            CONTENTS = LOG_FILE.read_text(encoding="utf-8").strip()
            self.assertEqual(CONTENTS, EXPECTED)

# --------------------------------------------------------------------------
# This test confirms invalid LOG_LEVEL values fall back to info threshold.
# --------------------------------------------------------------------------
    def test_log_line_invalid_log_level_falls_back_to_info(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            LOG_FILE = Path(TMPDIR) / "worker.log"

            with patch.dict("os.environ", {"LOG_LEVEL": "invalid"}):
                with patch.object(logger, "get_timestamp", return_value="2026-03-09 12:34:56 UTC"):
                    with patch("builtins.print") as PRINT:
                        logger.log_line(LOG_FILE, "debug", "Hidden debug.")

            PRINT.assert_not_called()
            self.assertFalse(LOG_FILE.exists())


if __name__ == "__main__":
    unittest.main()
