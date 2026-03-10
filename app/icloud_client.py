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
# This function creates a pyicloud client with constructor compatibility
# across library versions.
#
# Returns: Initialised "PyiCloudService" instance.
# --------------------------------------------------------------------------
    def _create_service(self) -> PyiCloudService:
        SERVICE_KWARGS = {
            "cookie_directory": str(self.config.cookie_dir),
            "session_directory": str(self.config.session_dir),
        }

        try:
            return PyiCloudService(
                self.config.icloud_email,
                self.config.icloud_password,
                **SERVICE_KWARGS,
            )
        except TypeError as ERROR:
            ERROR_TEXT = str(ERROR)

            if "session_directory" not in ERROR_TEXT:
                raise

            SERVICE_KWARGS.pop("session_directory", None)
            return PyiCloudService(
                self.config.icloud_email,
                self.config.icloud_password,
                **SERVICE_KWARGS,
            )

# --------------------------------------------------------------------------
# This function starts an iCloud authentication attempt.
#
# Returns: Tuple "(is_authenticated, details_message)".
# --------------------------------------------------------------------------
    def start_authentication(self) -> tuple[bool, str]:
        self.prepare_compat_paths()
        self.api = self._create_service()

        if self.api.requires_2fa:
            return False, "Two-factor code is required."

        if getattr(self.api, "requires_2sa", False):
            return False, "Two-step authentication is required; use app-specific passwords where possible."

        return True, "Authenticated successfully."

# --------------------------------------------------------------------------
# This function completes a pending authentication challenge with an MFA code.
#
# 1. "CODE" is the MFA code to validate.
#
# Returns: Tuple "(is_authenticated, details_message)".
# --------------------------------------------------------------------------
    def complete_authentication(self, CODE: str) -> tuple[bool, str]:
        if self.api is None:
            return False, "Authentication session is not initialised."

        CODE = CODE.strip()

        if not CODE:
            return False, "Two-factor code is required."

        if not self.api.requires_2fa:
            return True, "Authenticated successfully."

        IS_VALID = self.api.validate_2fa_code(CODE)

        if not IS_VALID:
            return False, "Two-factor code was rejected by Apple."

        if self.api.is_trusted_session:
            return True, "Authenticated successfully with 2FA."

        self.api.trust_session()
        return True, "Authenticated successfully with trusted 2FA session."

# --------------------------------------------------------------------------
# This function authenticates with iCloud and optionally completes MFA.
#
# 1. "CODE_PROVIDER" is a zero-argument callable returning an MFA code when
#    needed.
#
# Returns: Tuple "(is_authenticated, details_message)".
# --------------------------------------------------------------------------
    def authenticate(self, CODE_PROVIDER: Callable[[], str]) -> tuple[bool, str]:
        CODE = CODE_PROVIDER().strip()

        if CODE:
            return self.complete_authentication(CODE)

        return self.start_authentication()

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
        NAMES = DIRECTORY_INFO.get("names", [])

        if isinstance(NAMES, list) and NAMES:
            RESULT.extend(self._entries_from_names(NODE, CURRENT_PATH, NAMES))
            return RESULT

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
            return {"dirs": [], "files": [], "names": []}

        return self._normalise_dir_payload(PAYLOAD)

# --------------------------------------------------------------------------
# This function normalises pyicloud directory payload variants.
#
# 1. "PAYLOAD" is the value returned from "NODE.dir()".
#
# Returns: Dictionary with canonical "dirs" and "files" lists.
# --------------------------------------------------------------------------
    def _normalise_dir_payload(self, PAYLOAD: Any) -> dict[str, Any]:
        if isinstance(PAYLOAD, list):
            if all(isinstance(ITEM, str) for ITEM in PAYLOAD):
                return {"dirs": [], "files": [], "names": PAYLOAD}

            return self._normalise_items_payload(PAYLOAD)

        if not isinstance(PAYLOAD, dict):
            return {"dirs": [], "files": [], "names": []}

        if isinstance(PAYLOAD.get("dirs"), list) and isinstance(PAYLOAD.get("files"), list):
            return {
                "dirs": PAYLOAD.get("dirs", []),
                "files": PAYLOAD.get("files", []),
                "names": [],
            }

        if isinstance(PAYLOAD.get("folders"), list) and isinstance(PAYLOAD.get("files"), list):
            return {
                "dirs": PAYLOAD.get("folders", []),
                "files": PAYLOAD.get("files", []),
                "names": [],
            }

        for KEY in ("items", "children", "entries", "contents"):
            ITEMS = PAYLOAD.get(KEY)

            if isinstance(ITEMS, list):
                return self._normalise_items_payload(ITEMS)

        return {"dirs": [], "files": [], "names": []}

# --------------------------------------------------------------------------
# This function splits mixed item payloads into canonical directories/files.
#
# 1. "ITEMS" is a list of drive item dictionaries.
#
# Returns: Dictionary with canonical "dirs" and "files" lists.
# --------------------------------------------------------------------------
    def _normalise_items_payload(self, ITEMS: list[Any]) -> dict[str, Any]:
        DIRS: list[Any] = []
        FILES: list[Any] = []

        for ITEM in ITEMS:
            if not isinstance(ITEM, dict):
                continue

            ITEM_TYPE = str(
                ITEM.get("type", ITEM.get("item_type", ITEM.get("itemType", "")))
            ).lower()
            IS_DIR = bool(ITEM.get("isFolder", False))
            IS_DIR = IS_DIR or ITEM_TYPE in {"folder", "directory", "dir"}
            IS_DIR = IS_DIR or bool(ITEM.get("is_folder", False))

            if IS_DIR:
                DIRS.append(ITEM)
                continue

            FILES.append(ITEM)

        return {"dirs": DIRS, "files": FILES, "names": []}

# --------------------------------------------------------------------------
# This function builds entries from child-name payloads using child nodes.
#
# 1. "NODE" is current drive node.
# 2. "CURRENT_PATH" is current relative path.
# 3. "NAMES" is list of child names from pyicloud.
#
# Returns: Remote entries discovered from child nodes.
# --------------------------------------------------------------------------
    def _entries_from_names(
        self,
        NODE: Any,
        CURRENT_PATH: str,
        NAMES: list[str],
    ) -> list[RemoteEntry]:
        RESULT: list[RemoteEntry] = []

        for NAME in NAMES:
            CLEAN_NAME = str(NAME).strip()

            if not CLEAN_NAME:
                continue

            CHILD = self._child_node(NODE, CLEAN_NAME)

            if CHILD is None:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{CLEAN_NAME}".strip("/")
            IS_DIR = self._child_is_dir(CHILD)

            if IS_DIR:
                RESULT.append(
                    RemoteEntry(
                        path=RELATIVE_PATH,
                        is_dir=True,
                        size=0,
                        modified=self._child_modified(CHILD),
                    )
                )
                RESULT.extend(self._walk_node(CHILD, RELATIVE_PATH))
                continue

            RESULT.append(
                RemoteEntry(
                    path=RELATIVE_PATH,
                    is_dir=False,
                    size=self._child_size(CHILD),
                    modified=self._child_modified(CHILD),
                )
            )

        return RESULT

# --------------------------------------------------------------------------
# This function infers whether a child node is a directory.
#
# 1. "CHILD" is a pyicloud drive node.
#
# Returns: True for directories, otherwise False.
# --------------------------------------------------------------------------
    def _child_is_dir(self, CHILD: Any) -> bool:
        CHILD_TYPE = str(getattr(CHILD, "type", "")).lower()

        if CHILD_TYPE in {"folder", "directory", "dir"}:
            return True

        if bool(getattr(CHILD, "is_folder", False)):
            return True

        if bool(getattr(CHILD, "isFolder", False)):
            return True

        try:
            PAYLOAD = CHILD.dir()
        except (AttributeError, TypeError, ValueError):
            return False

        return isinstance(PAYLOAD, (dict, list))

# --------------------------------------------------------------------------
# This function extracts child modified timestamp as a string.
#
# 1. "CHILD" is a pyicloud drive node.
#
# Returns: Modified timestamp string, or empty string.
# --------------------------------------------------------------------------
    def _child_modified(self, CHILD: Any) -> str:
        VALUE = getattr(CHILD, "date_modified", "")

        if VALUE:
            return str(VALUE)

        return ""

# --------------------------------------------------------------------------
# This function extracts child file size with integer fallback.
#
# 1. "CHILD" is a pyicloud drive node.
#
# Returns: Non-negative file size.
# --------------------------------------------------------------------------
    def _child_size(self, CHILD: Any) -> int:
        RAW_VALUE = getattr(CHILD, "size", 0)

        try:
            VALUE = int(RAW_VALUE)
        except (TypeError, ValueError):
            return 0

        return VALUE if VALUE >= 0 else 0

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
            NAME = self._item_name(ITEM)

            if not NAME:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{NAME}".strip("/")
            RESULT.append(
                RemoteEntry(
                    path=RELATIVE_PATH,
                    is_dir=True,
                    size=0,
                    modified=self._item_modified(ITEM),
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
            NAME = self._item_name(ITEM)

            if not NAME:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{NAME}".strip("/")
            SIZE = self._item_size(ITEM)
            MODIFIED = self._item_modified(ITEM)
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
# This function extracts a filesystem name from varied pyicloud item shapes.
#
# 1. "ITEM" is a metadata dictionary for a drive node.
#
# Returns: Item name string, or empty string when missing.
# --------------------------------------------------------------------------
    def _item_name(self, ITEM: dict[str, Any]) -> str:
        for KEY in ("name", "filename", "displayName", "title"):
            VALUE = str(ITEM.get(KEY, "")).strip()

            if VALUE:
                return VALUE

        return ""

# --------------------------------------------------------------------------
# This function extracts a modified timestamp from metadata variants.
#
# 1. "ITEM" is a metadata dictionary for a drive node.
#
# Returns: Modified timestamp string, or empty string.
# --------------------------------------------------------------------------
    def _item_modified(self, ITEM: dict[str, Any]) -> str:
        for KEY in ("dateModified", "modified", "date_modified"):
            VALUE = str(ITEM.get(KEY, "")).strip()

            if VALUE:
                return VALUE

        return ""

# --------------------------------------------------------------------------
# This function extracts item byte size with robust integer fallback.
#
# 1. "ITEM" is a metadata dictionary for a drive node.
#
# Returns: Non-negative file size.
# --------------------------------------------------------------------------
    def _item_size(self, ITEM: dict[str, Any]) -> int:
        for KEY in ("size", "bytes", "itemSize"):
            RAW_VALUE = ITEM.get(KEY)

            if RAW_VALUE is None:
                continue

            try:
                VALUE = int(RAW_VALUE)
            except (TypeError, ValueError):
                continue

            return VALUE if VALUE >= 0 else 0

        return 0

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
