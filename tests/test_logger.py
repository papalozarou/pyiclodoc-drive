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


if __name__ == "__main__":
    unittest.main()
