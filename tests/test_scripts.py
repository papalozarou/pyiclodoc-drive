# ------------------------------------------------------------------------------
# This test module validates shell-script syntax and healthcheck behaviour.
# ------------------------------------------------------------------------------

from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest


# ------------------------------------------------------------------------------
# This function returns the repository root for script execution tests.
# ------------------------------------------------------------------------------
def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------------
# These tests validate script syntax and healthcheck exit behaviour.
# ------------------------------------------------------------------------------
class TestScripts(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms shell scripts pass POSIX syntax checks.
# --------------------------------------------------------------------------
    def test_scripts_have_valid_shell_syntax(self) -> None:
        REPO_ROOT = get_repo_root()
        SCRIPT_PATHS = [
            REPO_ROOT / "scripts" / "entrypoint.sh",
            REPO_ROOT / "scripts" / "start.sh",
            REPO_ROOT / "scripts" / "healthcheck.sh",
        ]

        for SCRIPT_PATH in SCRIPT_PATHS:
            RESULT = subprocess.run(
                ["sh", "-n", str(SCRIPT_PATH)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(RESULT.returncode, 0, msg=f"{SCRIPT_PATH}: {RESULT.stderr}")

# --------------------------------------------------------------------------
# This test confirms healthcheck passes with a fresh heartbeat file.
# --------------------------------------------------------------------------
    def test_healthcheck_passes_with_recent_heartbeat(self) -> None:
        REPO_ROOT = get_repo_root()
        HEALTHCHECK_PATH = REPO_ROOT / "scripts" / "healthcheck.sh"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            HEARTBEAT_PATH = ROOT_DIR / "iclouddd-heartbeat.txt"
            HEARTBEAT_PATH.write_text("ok\n", encoding="utf-8")

            BIN_DIR = ROOT_DIR / "bin"
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            PARALLEL_PATH = BIN_DIR / "parallel"
            PARALLEL_PATH.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            PARALLEL_PATH.chmod(PARALLEL_PATH.stat().st_mode | stat.S_IXUSR)

            ENV = os.environ.copy()
            ENV["PATH"] = f"{BIN_DIR}{os.pathsep}{ENV.get('PATH', '')}"
            ENV["HEARTBEAT_FILE"] = str(HEARTBEAT_PATH)
            ENV["HEALTHCHECK_MAX_AGE_SECONDS"] = "900"

            RESULT = subprocess.run(
                ["sh", str(HEALTHCHECK_PATH)],
                check=False,
                capture_output=True,
                text=True,
                env=ENV,
            )

            self.assertEqual(RESULT.returncode, 0, msg=RESULT.stderr)

# --------------------------------------------------------------------------
# This test confirms healthcheck fails when heartbeat file is absent.
# --------------------------------------------------------------------------
    def test_healthcheck_fails_without_heartbeat_file(self) -> None:
        REPO_ROOT = get_repo_root()
        HEALTHCHECK_PATH = REPO_ROOT / "scripts" / "healthcheck.sh"

        with tempfile.TemporaryDirectory() as TMPDIR:
            ROOT_DIR = Path(TMPDIR)
            HEARTBEAT_PATH = ROOT_DIR / "missing-iclouddd-heartbeat.txt"

            BIN_DIR = ROOT_DIR / "bin"
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            PARALLEL_PATH = BIN_DIR / "parallel"
            PARALLEL_PATH.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            PARALLEL_PATH.chmod(PARALLEL_PATH.stat().st_mode | stat.S_IXUSR)

            ENV = os.environ.copy()
            ENV["PATH"] = f"{BIN_DIR}{os.pathsep}{ENV.get('PATH', '')}"
            ENV["HEARTBEAT_FILE"] = str(HEARTBEAT_PATH)
            ENV["HEALTHCHECK_MAX_AGE_SECONDS"] = "900"

            RESULT = subprocess.run(
                ["sh", str(HEALTHCHECK_PATH)],
                check=False,
                capture_output=True,
                text=True,
                env=ENV,
            )

            self.assertNotEqual(RESULT.returncode, 0)


if __name__ == "__main__":
    unittest.main()
