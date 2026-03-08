# ------------------------------------------------------------------------------
# This module wraps pyicloud authentication, MFA, session persistence, and
# drive file access.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any
import shutil

from pyicloud import PyiCloudService

from app.config import AppConfig


# ------------------------------------------------------------------------------
# This data class represents a discovered remote iCloud Drive entry.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class RemoteEntry:
    path: str
    is_dir: bool
    size: int
    modified: str


# ------------------------------------------------------------------------------
# This class encapsulates iCloud auth, traversal, and download operations.
# ------------------------------------------------------------------------------
class ICloudDriveClient:
# --------------------------------------------------------------------------
# This function stores runtime configuration and initialises client state.
#
# 1. "CONFIG" is the runtime configuration model used by this client.
#
# Returns: None.
# --------------------------------------------------------------------------
    def __init__(self, CONFIG: AppConfig):
        self.config = CONFIG
        self.api: PyiCloudService | None = None

# --------------------------------------------------------------------------
# This function aligns cookie and session paths with an
# icloudpd-compatible folder layout.
#
# Returns: None.
# --------------------------------------------------------------------------
    def prepare_compat_paths(self) -> None:
        self.config.icloudpd_compat_dir.mkdir(parents=True, exist_ok=True)
        COOKIE_LINK = self.config.icloudpd_compat_dir / "cookies"
        SESSION_LINK = self.config.icloudpd_compat_dir / "session"
        self._ensure_link(COOKIE_LINK, self.config.cookie_dir)
        self._ensure_link(SESSION_LINK, self.config.session_dir)

# --------------------------------------------------------------------------
# This function creates a symlink and removes incompatible existing paths.
#
# 1. "LINK_PATH" is the compatibility symlink path.
# 2. "TARGET_PATH" is the canonical storage directory.
#
# Returns: None.
# --------------------------------------------------------------------------
    def _ensure_link(self, LINK_PATH: Path, TARGET_PATH: Path) -> None:
        if not LINK_PATH.is_symlink():
            self._replace_path_with_symlink(LINK_PATH, TARGET_PATH)
            return

        try:
            RESOLVED_MATCH = LINK_PATH.resolve() == TARGET_PATH.resolve()
        except FileNotFoundError:
            RESOLVED_MATCH = False

        if RESOLVED_MATCH:
            return

        LINK_PATH.unlink()
        LINK_PATH.symlink_to(TARGET_PATH, target_is_directory=True)

# --------------------------------------------------------------------------
# This function replaces a path with a compatibility symlink.
#
# 1. "LINK_PATH" is the compatibility symlink path.
# 2. "TARGET_PATH" is the canonical storage directory.
#
# Returns: None.
# --------------------------------------------------------------------------
    def _replace_path_with_symlink(self, LINK_PATH: Path, TARGET_PATH: Path) -> None:
        if not LINK_PATH.exists():
            LINK_PATH.symlink_to(TARGET_PATH, target_is_directory=True)
            return

        if LINK_PATH.is_dir():
            shutil.rmtree(LINK_PATH)
            LINK_PATH.symlink_to(TARGET_PATH, target_is_directory=True)
            return

        LINK_PATH.unlink()
        LINK_PATH.symlink_to(TARGET_PATH, target_is_directory=True)

# --------------------------------------------------------------------------
# This function authenticates with iCloud and completes MFA using a
# callback-supplied code.
#
# 1. "CODE_PROVIDER" is a zero-argument callable returning an MFA code when
#    needed.
#
# Returns: Tuple "(is_authenticated, details_message)".
#
# Notes: Client behaviour follows pyicloud session/cookie usage:
# https://github.com/picklepete/pyicloud
# --------------------------------------------------------------------------
    def authenticate(self, CODE_PROVIDER: Callable[[], str]) -> tuple[bool, str]:
        self.prepare_compat_paths()

        self.api = PyiCloudService(
            self.config.icloud_email,
            self.config.icloud_password,
            cookie_directory=str(self.config.cookie_dir),
            session_directory=str(self.config.session_dir),
        )

        if self.api.requires_2fa:
            return self._handle_2fa(CODE_PROVIDER)

        if getattr(self.api, "requires_2sa", False):
            return False, "Two-step authentication is required; use app-specific passwords where possible."

        return True, "Authenticated successfully."

# --------------------------------------------------------------------------
# This function validates a two-factor code and attempts to trust the
# session for reduced prompts.
#
# 1. "CODE_PROVIDER" is a zero-argument callable returning an MFA code.
#
# Returns: Tuple "(is_authenticated, details_message)".
# --------------------------------------------------------------------------
    def _handle_2fa(self, CODE_PROVIDER: Callable[[], str]) -> tuple[bool, str]:
        if self.api is None:
            return False, "Authentication state unavailable."

        CODE = CODE_PROVIDER().strip()

        if not CODE:
            return False, "Two-factor code is required."

        IS_VALID = self.api.validate_2fa_code(CODE)

        if not IS_VALID:
            return False, "Two-factor code was rejected by Apple."

        if self.api.is_trusted_session:
            return True, "Authenticated successfully with 2FA."

        self.api.trust_session()
        return True, "Authenticated successfully with trusted 2FA session."

# --------------------------------------------------------------------------
# This function traverses iCloud Drive and yields flattened entries
# suitable for sync planning.
#
# Returns: Flat list of remote entries covering both files and directories.
# --------------------------------------------------------------------------
    def list_entries(self) -> list[RemoteEntry]:
        if self.api is None:
            return []

        DRIVE_ROOT = self.api.drive
        return self._walk_node(DRIVE_ROOT, "")

# --------------------------------------------------------------------------
# This function recursively walks a drive node and accumulates files
# and directories.
#
# 1. "NODE" is the current drive node.
# 2. "CURRENT_PATH" is the current relative path prefix.
#
# Returns: Flat list of discovered remote entries under the node.
# --------------------------------------------------------------------------
    def _walk_node(self, NODE: Any, CURRENT_PATH: str) -> list[RemoteEntry]:
        RESULT: list[RemoteEntry] = []

        DIRECTORY_INFO = self._node_dir(NODE)
        DIRS = DIRECTORY_INFO.get("dirs", [])
        FILES = DIRECTORY_INFO.get("files", [])

        RESULT.extend(self._entries_from_directories(NODE, CURRENT_PATH, DIRS))
        RESULT.extend(self._entries_from_files(CURRENT_PATH, FILES))

        return RESULT

# --------------------------------------------------------------------------
# This function safely fetches directory metadata from a node.
#
# 1. "NODE" is the current drive node.
#
# Returns: Dictionary containing "dirs" and "files" lists.
# --------------------------------------------------------------------------
    def _node_dir(self, NODE: Any) -> dict[str, Any]:
        try:
            PAYLOAD = NODE.dir()
        except (AttributeError, TypeError, ValueError):
            return {"dirs": [], "files": []}

        if isinstance(PAYLOAD, dict):
            return PAYLOAD

        return {"dirs": [], "files": []}

# --------------------------------------------------------------------------
# This function converts directory items to entries and recursively
# appends child content.
#
# 1. "NODE" is current parent node.
# 2. "CURRENT_PATH" is current relative path.
# 3. "DIRS" is directory metadata.
#
# Returns: Remote entries including directories and their descendants.
# --------------------------------------------------------------------------
    def _entries_from_directories(
        self,
        NODE: Any,
        CURRENT_PATH: str,
        DIRS: list[Any],
    ) -> list[RemoteEntry]:
        RESULT: list[RemoteEntry] = []

        for ITEM in DIRS:
            NAME = str(ITEM.get("name", ""))

            if not NAME:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{NAME}".strip("/")
            RESULT.append(
                RemoteEntry(
                    path=RELATIVE_PATH,
                    is_dir=True,
                    size=0,
                    modified=str(ITEM.get("dateModified", "")),
                )
            )

            CHILD = self._child_node(NODE, NAME)

            if CHILD is None:
                continue

            RESULT.extend(self._walk_node(CHILD, RELATIVE_PATH))

        return RESULT

# --------------------------------------------------------------------------
# This function converts file items to manifest-friendly entry objects.
#
# 1. "CURRENT_PATH" is current relative path prefix.
# 2. "FILES" is file metadata list.
#
# Returns: Remote file entry list.
# --------------------------------------------------------------------------
    def _entries_from_files(self, CURRENT_PATH: str, FILES: list[Any]) -> list[RemoteEntry]:
        RESULT: list[RemoteEntry] = []

        for ITEM in FILES:
            NAME = str(ITEM.get("name", ""))

            if not NAME:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{NAME}".strip("/")
            SIZE = int(ITEM.get("size") or 0)
            MODIFIED = str(ITEM.get("dateModified", ""))
            RESULT.append(
                RemoteEntry(
                    path=RELATIVE_PATH,
                    is_dir=False,
                    size=SIZE,
                    modified=MODIFIED,
                )
            )

        return RESULT

# --------------------------------------------------------------------------
# This function safely resolves a named child node from a drive node.
#
# 1. "NODE" is current drive node.
# 2. "NAME" is child item name.
#
# Returns: Child node when found, otherwise None.
# --------------------------------------------------------------------------
    def _child_node(self, NODE: Any, NAME: str) -> Any | None:
        try:
            return NODE[NAME]
        except (AttributeError, KeyError, TypeError):
            return None

# --------------------------------------------------------------------------
# This function downloads a single remote file to a local path.
#
# 1. "REMOTE_PATH" is slash-separated iCloud Drive path.
# 2. "LOCAL_PATH" is filesystem destination.
#
# Returns: True on successful download/write, otherwise False.
# --------------------------------------------------------------------------
    def download_file(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> bool:
        if self.api is None:
            return False

        FILE_OBJ = self._resolve_file_object(REMOTE_PATH)

        if FILE_OBJ is None:
            return False

        LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)

        try:
            RESPONSE = FILE_OBJ.download()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return False

        return self._write_downloaded_content(RESPONSE, LOCAL_PATH)

# --------------------------------------------------------------------------
# This function resolves a file object from a slash-separated
# iCloud Drive path.
#
# 1. "REMOTE_PATH" is slash-separated iCloud Drive path.
#
# Returns: Resolved file object, otherwise None.
# --------------------------------------------------------------------------
    def _resolve_file_object(self, REMOTE_PATH: str) -> Any | None:
        if self.api is None:
            return None

        NODE: Any = self.api.drive

        for SEGMENT in [PART for PART in REMOTE_PATH.split("/") if PART]:
            NODE = self._child_node(NODE, SEGMENT)

            if NODE is None:
                return None

        return NODE

# --------------------------------------------------------------------------
# This function writes downloaded response content while supporting
# multiple response styles.
#
# 1. "RESPONSE" is download response object.
# 2. "LOCAL_PATH" is file destination.
#
# Returns: True on successful write, otherwise False.
# --------------------------------------------------------------------------
    def _write_downloaded_content(self, RESPONSE: Any, LOCAL_PATH: Path) -> bool:
        TEMP_PATH = self._temporary_download_path(LOCAL_PATH)

        if hasattr(RESPONSE, "iter_content"):
            return self._write_iter_content(RESPONSE, LOCAL_PATH, TEMP_PATH)

        RAW = getattr(RESPONSE, "raw", None)

        if RAW is None:
            return False

        try:
            self._cleanup_temporary_file(TEMP_PATH)

            with TEMP_PATH.open("wb") as HANDLE:
                shutil.copyfileobj(RAW, HANDLE)

            TEMP_PATH.replace(LOCAL_PATH)
        except (AttributeError, OSError, TypeError, ValueError):
            self._cleanup_temporary_file(TEMP_PATH)
            return False

        return True

# --------------------------------------------------------------------------
# This function streams iterable content chunks to disk.
#
# 1. "RESPONSE" is download response exposing "iter_content".
# 2. "LOCAL_PATH" is destination path.
# 3. "TEMP_PATH" is temporary path used for atomic replacement.
#
# Returns: True on successful write, otherwise False.
# --------------------------------------------------------------------------
    def _write_iter_content(
        self,
        RESPONSE: Any,
        LOCAL_PATH: Path,
        TEMP_PATH: Path,
    ) -> bool:
        try:
            self._cleanup_temporary_file(TEMP_PATH)

            with TEMP_PATH.open("wb") as HANDLE:
                for CHUNK in RESPONSE.iter_content(chunk_size=1024 * 1024):
                    if not CHUNK:
                        continue

                    HANDLE.write(CHUNK)

            TEMP_PATH.replace(LOCAL_PATH)
        except (AttributeError, OSError, TypeError, ValueError):
            self._cleanup_temporary_file(TEMP_PATH)
            return False

        return True

# --------------------------------------------------------------------------
# This function returns the temporary path used for safe file writes.
#
# 1. "LOCAL_PATH" is the final destination file path.
#
# Returns: Temporary file path in the same directory as destination.
# --------------------------------------------------------------------------
    def _temporary_download_path(self, LOCAL_PATH: Path) -> Path:
        return LOCAL_PATH.with_name(f".{LOCAL_PATH.name}.partial")

# --------------------------------------------------------------------------
# This function removes an existing temporary file when present.
#
# 1. "TEMP_PATH" is temporary file path.
#
# Returns: None.
# --------------------------------------------------------------------------
    def _cleanup_temporary_file(self, TEMP_PATH: Path) -> None:
        if not TEMP_PATH.exists():
            return

        try:
            TEMP_PATH.unlink()
        except OSError:
            pass
