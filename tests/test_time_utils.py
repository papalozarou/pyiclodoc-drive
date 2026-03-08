# ------------------------------------------------------------------------------
# This test module verifies timezone selection from the "TZ" environment
# variable.
#
# Notes:
# https://docs.python.org/3/library/zoneinfo.html
# ------------------------------------------------------------------------------

import os
import unittest

from app.time_utils import configured_timezone


# ------------------------------------------------------------------------------
# These tests ensure valid and invalid "TZ" values are handled safely.
# ------------------------------------------------------------------------------
class TestTimeUtils(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms a valid TZ value is used when provided.
# --------------------------------------------------------------------------
    def test_configured_timezone_uses_valid_tz(self) -> None:
        PREVIOUS = os.environ.get("TZ")

        try:
            os.environ["TZ"] = "Europe/London"
            TZ = configured_timezone()
            self.assertEqual(str(TZ), "Europe/London")
        finally:
            if PREVIOUS is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = PREVIOUS

# --------------------------------------------------------------------------
# This test confirms invalid TZ values fall back to UTC.
# --------------------------------------------------------------------------
    def test_configured_timezone_falls_back_to_utc(self) -> None:
        PREVIOUS = os.environ.get("TZ")

        try:
            os.environ["TZ"] = "Invalid/Timezone"
            TZ = configured_timezone()
            self.assertEqual(getattr(TZ, "key", "UTC"), "UTC")
        finally:
            if PREVIOUS is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = PREVIOUS


if __name__ == "__main__":
    unittest.main()
