# ------------------------------------------------------------------------------
# This helper module installs dependency stubs for isolated unit testing.
#
# Notes:
# https://docs.python.org/3/library/sys.html#sys.modules
# ------------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
import sys
import types


# ------------------------------------------------------------------------------
# This function injects stubs for optional runtime dependencies.
# ------------------------------------------------------------------------------
def install_dependency_stubs() -> None:
    if "requests" not in sys.modules:
        REQUESTS_MODULE = types.ModuleType("requests")

        class _Response:
            """This response stub mirrors only attributes used by project code."""

            ok = True

            def json(self):
                return {"ok": True, "result": []}

        def post(*ARGS, **KWARGS):
            _ = (ARGS, KWARGS)
            return _Response()

        def get(*ARGS, **KWARGS):
            _ = (ARGS, KWARGS)
            return _Response()

        REQUESTS_MODULE.post = post
        REQUESTS_MODULE.get = get
        REQUESTS_MODULE.RequestException = Exception
        sys.modules["requests"] = REQUESTS_MODULE

    if "pyicloud" not in sys.modules:
        PYICLOUD_MODULE = types.ModuleType("pyicloud")

        class PyiCloudService:
            """This pyicloud stub provides constructor compatibility for imports."""

            def __init__(self, *ARGS, **KWARGS):
                _ = (ARGS, KWARGS)
                self.requires_2fa = False
                self.requires_2sa = False
                self.is_trusted_session = True
                self.drive = None

        PYICLOUD_MODULE.PyiCloudService = PyiCloudService
        sys.modules["pyicloud"] = PYICLOUD_MODULE

    if "dateutil" not in sys.modules:
        DATEUTIL_MODULE = types.ModuleType("dateutil")
        PARSER_MODULE = types.ModuleType("dateutil.parser")

        def isoparse(VALUE: str):
            return datetime.fromisoformat(VALUE)

        PARSER_MODULE.isoparse = isoparse
        DATEUTIL_MODULE.parser = PARSER_MODULE
        sys.modules["dateutil"] = DATEUTIL_MODULE
        sys.modules["dateutil.parser"] = PARSER_MODULE

    if "keyring" not in sys.modules:
        KEYRING_MODULE = types.ModuleType("keyring")
        STORAGE = {}

        def set_password(SERVICE_NAME, USERNAME, VALUE):
            STORAGE[(SERVICE_NAME, USERNAME)] = VALUE

        def get_password(SERVICE_NAME, USERNAME):
            return STORAGE.get((SERVICE_NAME, USERNAME))

        def set_keyring(_keyring):
            return None

        KEYRING_MODULE.set_password = set_password
        KEYRING_MODULE.get_password = get_password
        KEYRING_MODULE.set_keyring = set_keyring
        sys.modules["keyring"] = KEYRING_MODULE

    if "keyrings" not in sys.modules:
        KEYRINGS_MODULE = types.ModuleType("keyrings")
        ALT_MODULE = types.ModuleType("keyrings.alt")
        FILE_MODULE = types.ModuleType("keyrings.alt.file")

        class PlaintextKeyring:
            """This keyring backend stub satisfies runtime imports for tests."""

            pass

        FILE_MODULE.PlaintextKeyring = PlaintextKeyring
        KEYRINGS_MODULE.alt = ALT_MODULE
        ALT_MODULE.file = FILE_MODULE
        sys.modules["keyrings"] = KEYRINGS_MODULE
        sys.modules["keyrings.alt"] = ALT_MODULE
        sys.modules["keyrings.alt.file"] = FILE_MODULE
