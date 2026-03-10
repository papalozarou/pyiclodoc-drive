# ------------------------------------------------------------------------------
# This test module verifies keyring configuration and credential persistence.
# ------------------------------------------------------------------------------

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app import credential_store


# ------------------------------------------------------------------------------
# These tests validate credential-store read/write and keyring setup behaviour.
# ------------------------------------------------------------------------------
class TestCredentialStore(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms keyring setup creates paths and sets env wiring.
# --------------------------------------------------------------------------
    def test_configure_keyring_sets_env_and_backend(self) -> None:
        with tempfile.TemporaryDirectory() as TMPDIR:
            CONFIG_DIR = Path(TMPDIR) / "config"
            EXPECTED_FILE = CONFIG_DIR / "keyring" / "keyring_pass.cfg"

            with patch.object(credential_store.keyring, "set_keyring") as SET_KEYRING:
                credential_store.configure_keyring(CONFIG_DIR)

            self.assertTrue((CONFIG_DIR / "keyring").exists())
            self.assertEqual(os.environ.get("PYTHON_KEYRING_FILENAME"), str(EXPECTED_FILE))
            self.assertEqual(SET_KEYRING.call_count, 1)

# --------------------------------------------------------------------------
# This test confirms credential reads return empty-string fallbacks.
# --------------------------------------------------------------------------
    def test_load_credentials_returns_empty_defaults(self) -> None:
        with patch.object(credential_store.keyring, "get_password", return_value=None):
            EMAIL, PASSWORD = credential_store.load_credentials("svc", "alice")

        self.assertEqual(EMAIL, "")
        self.assertEqual(PASSWORD, "")

# --------------------------------------------------------------------------
# This test confirms credential reads return stored values when present.
# --------------------------------------------------------------------------
    def test_load_credentials_returns_stored_values(self) -> None:
        def fake_get_password(SERVICE_NAME: str, USERNAME: str):
            if USERNAME.endswith(":email"):
                return "alice@example.com"

            if USERNAME.endswith(":password"):
                return "secret"

            return None

        with patch.object(credential_store.keyring, "get_password", side_effect=fake_get_password):
            EMAIL, PASSWORD = credential_store.load_credentials("svc", "alice")

        self.assertEqual(EMAIL, "alice@example.com")
        self.assertEqual(PASSWORD, "secret")

# --------------------------------------------------------------------------
# This test confirms save only writes non-empty credential values.
# --------------------------------------------------------------------------
    def test_save_credentials_writes_only_non_empty_values(self) -> None:
        with patch.object(credential_store.keyring, "set_password") as SET_PASSWORD:
            credential_store.save_credentials("svc", "alice", "alice@example.com", "")

        SET_PASSWORD.assert_called_once_with("svc", "alice:email", "alice@example.com")


if __name__ == "__main__":
    unittest.main()
