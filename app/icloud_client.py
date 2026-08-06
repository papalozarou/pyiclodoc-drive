# ------------------------------------------------------------------------------
# This module wraps pyicloud authentication, MFA, session persistence, and
# drive file access.
# ------------------------------------------------------------------------------

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any, TypedDict
import shutil
import threading
import time

from pyicloud import PyiCloudService

from app.config import AppConfig
from app.logger import log_line

DIR_RETRY_ATTEMPTS = 4
DIR_RETRY_BASE_DELAY_SECONDS = 0.05
DIR_RETRY_MAX_DELAY_SECONDS = 0.40
TRAVERSAL_SLOW_DIR_SECONDS = 5.0
TRAVERSAL_SLOW_DIR_LIMIT = 5
TRAVERSAL_FAILURE_SAMPLE_LIMIT = 5
TRAVERSAL_WORKER_WAIT_TIMEOUT_SECONDS = 30.0
ICLOUD_SESSION_CONNECT_TIMEOUT_SECONDS = 10.0
ICLOUD_SESSION_READ_TIMEOUT_SECONDS = 30.0


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
# This data class models one download outcome without shared mutable state.
#
# 1. "is_success" records whether the requested transfer completed.
# 2. "failure_reason" stores the stable machine-facing failure token.
#
# N.B.
# Callers must consume this result directly. The client does not retain any
# global per-transfer failure state because downloads may run concurrently.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class DownloadResult:
    is_success: bool
    failure_reason: str = ""


# ------------------------------------------------------------------------------
# This typed dictionary stores one slow-directory traversal sample.
# ------------------------------------------------------------------------------
class SlowDirectorySample(TypedDict):
    path: str
    duration_seconds: float


# ------------------------------------------------------------------------------
# This typed dictionary stores one traversal failure sample.
# ------------------------------------------------------------------------------
class TraversalFailureSample(TypedDict):
    path: str
    status: str
    reason: str


# ------------------------------------------------------------------------------
# This typed dictionary stores traversal telemetry returned to callers.
# ------------------------------------------------------------------------------
class TraversalStatsSnapshot(TypedDict):
    directories_completed: int
    directories_pending: int
    workers_active: int
    entries_discovered: int
    files_discovered: int
    directories_discovered: int
    dir_reads: int
    dir_retries: int
    dir_non_directory: int
    dir_retryable_errors: int
    dir_hard_failures: int
    dir_failure_samples: list[TraversalFailureSample]
    slow_dirs: list[SlowDirectorySample]


# ------------------------------------------------------------------------------
# This typed dictionary stores the traversal counters used to decide whether
# useful work is still advancing during a parallel wait window.
# ------------------------------------------------------------------------------
class TraversalProgressSnapshot(TypedDict):
    directories_completed: int
    entries_discovered: int
    dir_reads: int
    dir_retries: int


# ------------------------------------------------------------------------------
# This exception signals a bounded parallel traversal stall.
#
# N.B.
# Callers should treat this as an incomplete traversal result, not as proof the
# whole worker process must exit. The client records traversal failure
# telemetry before raising this exception.
# ------------------------------------------------------------------------------
class TraversalWorkerTimeoutError(RuntimeError):
    pass


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
        self._stats_lock = threading.Lock()
        self._traversal_stats = self._build_empty_traversal_stats()

    # --------------------------------------------------------------------------
    # This function writes iCloud client debug detail to the worker log.
    #
    # 1. "MESSAGE" is the already-redacted diagnostic message to write.
    #
    # Returns: None.
    #
    # N.B.
    # This helper deliberately centralises low-level iCloud diagnostics so auth
    # credentials, MFA codes, cookies, and raw Apple response payloads stay out
    # of call-site log messages.
    # --------------------------------------------------------------------------
    def _log_debug(self, MESSAGE: str) -> None:
        log_line(self.config.worker_log_path, "debug", MESSAGE)

    # --------------------------------------------------------------------------
    # This function logs the start of one traversal path probe.
    #
    # 1. "CURRENT_PATH" is the remote path being inspected.
    # 2. "ATTEMPT" is the one-based probe attempt number.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _log_traversal_probe_started(
        self,
        CURRENT_PATH: str,
        ATTEMPT: int,
    ) -> None:
        self._log_debug(
            "Traversal probe started: "
            f"path={CURRENT_PATH or '/'}, "
            f"attempt={ATTEMPT}."
        )

    # --------------------------------------------------------------------------
    # This function logs one discovered traversal child path.
    #
    # 1. "RELATIVE_PATH" is the discovered child path.
    # 2. "SOURCE" is the metadata source used to discover the child.
    # 3. "KIND" is the known or candidate child kind.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _log_traversal_child_discovered(
        self,
        RELATIVE_PATH: str,
        SOURCE: str,
        KIND: str,
    ) -> None:
        self._log_debug(
            "Traversal child discovered: "
            f"path={RELATIVE_PATH}, "
            f"kind={KIND}, "
            f"source={SOURCE}."
        )

    # --------------------------------------------------------------------------
    # This function logs one traversal child classification decision.
    #
    # 1. "RELATIVE_PATH" is the classified child path.
    # 2. "KIND" is the resolved child kind.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _log_traversal_child_classified(
        self,
        RELATIVE_PATH: str,
        KIND: str,
    ) -> None:
        self._log_debug(
            "Traversal child classified: "
            f"path={RELATIVE_PATH}, "
            f"kind={KIND}."
        )

    # --------------------------------------------------------------------------
    # This function creates a clean traversal telemetry dictionary.
    #
    # Returns: Empty traversal statistics dictionary.
    # --------------------------------------------------------------------------
    def _build_empty_traversal_stats(self) -> TraversalStatsSnapshot:
        return {
            "directories_completed": 0,
            "directories_pending": 0,
            "workers_active": 0,
            "entries_discovered": 0,
            "files_discovered": 0,
            "directories_discovered": 0,
            "dir_reads": 0,
            "dir_retries": 0,
            "dir_non_directory": 0,
            "dir_retryable_errors": 0,
            "dir_hard_failures": 0,
            "dir_failure_samples": [],
            "slow_dirs": [],
        }

    # --------------------------------------------------------------------------
    # This function resets traversal telemetry before each list operation.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _reset_traversal_stats(self) -> None:
        with self._stats_lock:
            self._traversal_stats = self._build_empty_traversal_stats()

    # --------------------------------------------------------------------------
    # This function returns a thread-safe snapshot of traversal telemetry.
    #
    # Returns: Traversal statistics snapshot dictionary.
    # --------------------------------------------------------------------------
    def get_traversal_stats_snapshot(self) -> TraversalStatsSnapshot:
        with self._stats_lock:
            return self._copy_traversal_stats(self._traversal_stats)

    # --------------------------------------------------------------------------
    # This function copies traversal stats with detached list payloads.
    #
    # 1. "STATS" is the traversal stats dictionary to copy.
    #
    # Returns: Snapshot safe for callers to read and mutate locally.
    # --------------------------------------------------------------------------
    def _copy_traversal_stats(
        self,
        STATS: TraversalStatsSnapshot,
    ) -> TraversalStatsSnapshot:
        return {
            **STATS,
            "dir_failure_samples": list(STATS["dir_failure_samples"]),
            "slow_dirs": list(STATS["slow_dirs"]),
        }

    # --------------------------------------------------------------------------
    # This function records discovered entry counters for traversal telemetry.
    #
    # 1. "IS_DIR" indicates whether entry is a directory.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_traversal_entry(self, IS_DIR: bool) -> None:
        with self._stats_lock:
            self._traversal_stats["entries_discovered"] += 1
            if IS_DIR:
                self._traversal_stats["directories_discovered"] += 1
                return
            self._traversal_stats["files_discovered"] += 1

    # --------------------------------------------------------------------------
    # This function stores in-flight traversal worker and queue stats.
    #
    # 1. "DIRECTORIES_COMPLETED" is completed directory task count.
    # 2. "DIRECTORIES_PENDING" is queued or running directory task count.
    # 3. "WORKERS_ACTIVE" is active worker count estimate.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_traversal_queue_state(
        self,
        DIRECTORIES_COMPLETED: int,
        DIRECTORIES_PENDING: int,
        WORKERS_ACTIVE: int,
    ) -> None:
        with self._stats_lock:
            self._traversal_stats["directories_completed"] = max(DIRECTORIES_COMPLETED, 0)
            self._traversal_stats["directories_pending"] = max(DIRECTORIES_PENDING, 0)
            self._traversal_stats["workers_active"] = max(WORKERS_ACTIVE, 0)

    # --------------------------------------------------------------------------
    # This function records serial traversal directory-entry state.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_serial_directory_enter(self) -> None:
        with self._stats_lock:
            self._traversal_stats["directories_pending"] += 1
            self._traversal_stats["workers_active"] = 1

    # --------------------------------------------------------------------------
    # This function records serial traversal directory-exit state.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_serial_directory_exit(self) -> None:
        with self._stats_lock:
            self._traversal_stats["directories_pending"] = max(
                self._traversal_stats["directories_pending"] - 1,
                0,
            )
            self._traversal_stats["directories_completed"] += 1
            self._traversal_stats["workers_active"] = (
                1 if self._traversal_stats["directories_pending"] > 0 else 0
            )

    # --------------------------------------------------------------------------
    # This function records a directory-read attempt outcome for traversal
    # telemetry and retains the slowest directory reads.
    #
    # 1. "CURRENT_PATH" is current remote path being listed.
    # 2. "DURATION_SECONDS" is directory read duration.
    # 3. "IS_RETRY" indicates whether this attempt is a retry attempt.
    # 4. "STATUS" is one of: "ok", "non_directory", "retryable_error",
    #    "hard_failure".
    # 5. "FAILURE_REASON" is short exception context for failure states.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_directory_read(
        self,
        CURRENT_PATH: str,
        DURATION_SECONDS: float,
        IS_RETRY: bool,
        STATUS: str,
        FAILURE_REASON: str = "",
    ) -> None:
        with self._stats_lock:
            self._traversal_stats["dir_reads"] += 1
            if IS_RETRY:
                self._traversal_stats["dir_retries"] += 1

            if STATUS == "retryable_error":
                self._traversal_stats["dir_retryable_errors"] += 1
                self._record_directory_failure_sample(CURRENT_PATH, STATUS, FAILURE_REASON)

            if STATUS == "non_directory":
                self._traversal_stats["dir_non_directory"] += 1
            elif STATUS == "hard_failure":
                self._traversal_stats["dir_hard_failures"] += 1
                self._record_directory_failure_sample(CURRENT_PATH, STATUS, FAILURE_REASON)

            if DURATION_SECONDS < TRAVERSAL_SLOW_DIR_SECONDS:
                return

            self._record_slow_directory(CURRENT_PATH, DURATION_SECONDS)

    # --------------------------------------------------------------------------
    # This function records one slow-directory traversal sample.
    #
    # 1. "CURRENT_PATH" is current remote path being listed.
    # 2. "DURATION_SECONDS" is directory read duration.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_slow_directory(
        self,
        CURRENT_PATH: str,
        DURATION_SECONDS: float,
    ) -> None:
        SLOW_DIRS = list(self._traversal_stats["slow_dirs"])
        SLOW_DIRS.append(
            {
                "path": CURRENT_PATH or "/",
                "duration_seconds": round(DURATION_SECONDS, 3),
            }
        )
        SLOW_DIRS.sort(
            key=lambda ITEM: ITEM["duration_seconds"],
            reverse=True,
        )
        self._traversal_stats["slow_dirs"] = SLOW_DIRS[:TRAVERSAL_SLOW_DIR_LIMIT]

    # --------------------------------------------------------------------------
    # This function stores a bounded set of directory read failure samples.
    #
    # 1. "CURRENT_PATH" is current remote path being listed.
    # 2. "STATUS" is failure status label.
    # 3. "FAILURE_REASON" is short exception context.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_directory_failure_sample(
        self,
        CURRENT_PATH: str,
        STATUS: str,
        FAILURE_REASON: str,
    ) -> None:
        FAILURE_SAMPLES = list(self._traversal_stats["dir_failure_samples"])

        if len(FAILURE_SAMPLES) >= TRAVERSAL_FAILURE_SAMPLE_LIMIT:
            return

        FAILURE_SAMPLES.append(
            {
                "path": CURRENT_PATH or "/",
                "status": STATUS,
                "reason": FAILURE_REASON or "<none>",
            }
        )
        self._traversal_stats["dir_failure_samples"] = FAILURE_SAMPLES

    # --------------------------------------------------------------------------
    # This function records a failed drive root fetch as a hard failure.
    #
    # 1. "ERROR" is the exception raised while fetching the drive root.
    #
    # Returns: None.
    #
    # N.B. This mirrors "_record_traversal_worker_timeout" below. The drive
    # root fetch happens once, before any per-directory read or worker-stall
    # detection runs, so it needs its own hard-failure recorder rather than
    # reusing a wait-seconds-shaped message.
    # --------------------------------------------------------------------------
    def _record_drive_root_failure(self, ERROR: Exception) -> None:
        with self._stats_lock:
            self._traversal_stats["dir_hard_failures"] += 1
            self._record_directory_failure_sample(
                "/",
                "hard_failure",
                f"drive_root_unavailable: {type(ERROR).__name__}: {ERROR}",
            )

    # --------------------------------------------------------------------------
    # This function records one traversal worker stall as a hard failure.
    #
    # 1. "CURRENT_PATH" is the stalled directory path.
    # 2. "WAIT_SECONDS" is the worker wait timeout threshold.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _record_traversal_worker_timeout(
        self,
        CURRENT_PATH: str,
        WAIT_SECONDS: float,
    ) -> None:
        with self._stats_lock:
            self._traversal_stats["dir_hard_failures"] += 1
            self._record_directory_failure_sample(
                CURRENT_PATH,
                "hard_failure",
                f"worker_timeout_after_{WAIT_SECONDS:.1f}s",
            )

    # --------------------------------------------------------------------------
    # This function returns the traversal counters used for stall decisions.
    #
    # Returns: Snapshot of counters that should increase while useful traversal
    # work is still happening.
    # --------------------------------------------------------------------------
    def _get_traversal_progress_snapshot(self) -> TraversalProgressSnapshot:
        with self._stats_lock:
            return {
                "directories_completed": self._traversal_stats["directories_completed"],
                "entries_discovered": self._traversal_stats["entries_discovered"],
                "dir_reads": self._traversal_stats["dir_reads"],
                "dir_retries": self._traversal_stats["dir_retries"],
            }

    # --------------------------------------------------------------------------
    # This function checks whether traversal progress advanced between two
    # snapshots.
    #
    # 1. "PREVIOUS_SNAPSHOT" is the earlier progress snapshot.
    # 2. "CURRENT_SNAPSHOT" is the later progress snapshot.
    #
    # Returns: True when at least one monitored progress counter increased.
    # --------------------------------------------------------------------------
    def _has_traversal_progress_advanced(
        self,
        PREVIOUS_SNAPSHOT: TraversalProgressSnapshot,
        CURRENT_SNAPSHOT: TraversalProgressSnapshot,
    ) -> bool:
        return any(
            int(CURRENT_SNAPSHOT[KEY]) > int(PREVIOUS_SNAPSHOT[KEY])
            for KEY in CURRENT_SNAPSHOT
        )

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
        }

        self._log_debug(
            "iCloud service creation started: "
            f"cookie_directory={self.config.cookie_dir.as_posix()}."
        )
        SERVICE = PyiCloudService(
            self.config.icloud_email,
            self.config.icloud_password,
            **SERVICE_KWARGS,
        )
        self._set_default_session_timeout(SERVICE)
        return SERVICE

    # --------------------------------------------------------------------------
    # This function wraps a "PyiCloudService" session so that every request
    # that does not specify its own "timeout" gets a bounded connect/read
    # timeout instead of blocking forever.
    #
    # 1. "SERVICE" is the "PyiCloudService" instance whose session should be
    #    wrapped.
    #
    # Returns: None.
    #
    # N.B.
    # pyicloud's "PyiCloudSession" (a "requests.Session" subclass, see
    # "pyicloud/session.py") never passes a "timeout" to any of its own
    # request calls, so a stalled Apple response can block the underlying
    # socket read indefinitely; there is no library-level or OS-level bound
    # on that wait. "request" and "request_raw" are "PyiCloudSession"'s only
    # two entry points: every "get()"/"post()" call funnels through
    # "request()", and pyicloud's HSA2 challenge polling
    # ("pyicloud/hsa2_bridge.py") calls "request_raw()" directly, bypassing
    # "request()". Both are wrapped here so "global" coverage is actually
    # global rather than missing the HSA2 path.
    #
    # This wraps the two methods on this one service instance only, not the
    # "PyiCloudSession" class, so the change is scoped to this client and
    # cannot affect any other consumer of pyicloud in the same process. It
    # still relies on "PyiCloudSession" continuing to expose "request" and
    # "request_raw" with a "timeout" keyword argument, which pyicloud does
    # not promise as a stable contract across versions.
    # --------------------------------------------------------------------------
    def _set_default_session_timeout(self, SERVICE: PyiCloudService) -> None:
        DEFAULT_TIMEOUT = (
            ICLOUD_SESSION_CONNECT_TIMEOUT_SECONDS,
            ICLOUD_SESSION_READ_TIMEOUT_SECONDS,
        )

        for METHOD_NAME in ("request", "request_raw"):
            ORIGINAL_METHOD = getattr(SERVICE.session, METHOD_NAME)

            def _add_default_timeout(
                *ARGS: Any,
                _ORIGINAL_METHOD: Callable = ORIGINAL_METHOD,
                **KWARGS: Any,
            ) -> Any:
                if KWARGS.get("timeout") is None:
                    KWARGS["timeout"] = DEFAULT_TIMEOUT
                return _ORIGINAL_METHOD(*ARGS, **KWARGS)

            setattr(SERVICE.session, METHOD_NAME, _add_default_timeout)

        self._log_debug(
            "iCloud session default timeout applied: "
            f"connect_seconds={ICLOUD_SESSION_CONNECT_TIMEOUT_SECONDS}, "
            f"read_seconds={ICLOUD_SESSION_READ_TIMEOUT_SECONDS}."
        )

    # --------------------------------------------------------------------------
    # This function starts an iCloud authentication attempt.
    #
    # Returns: Tuple "(is_authenticated, details_message)".
    # --------------------------------------------------------------------------
    def start_authentication(self) -> tuple[bool, str]:
        self.prepare_compat_paths()
        self.api = self._create_service()
        REQUIRES_2FA = bool(getattr(self.api, "requires_2fa", False))
        REQUIRES_2SA = bool(getattr(self.api, "requires_2sa", False))
        TRUSTED_SESSION = bool(getattr(self.api, "is_trusted_session", False))
        self._log_debug(
            "iCloud authentication state: "
            f"requires_2fa={REQUIRES_2FA}, "
            f"requires_2sa={REQUIRES_2SA}, "
            f"is_trusted_session={TRUSTED_SESSION}."
        )

        if REQUIRES_2FA:
            self._log_debug("iCloud authentication blocked: reason=requires_2fa.")
            return False, "Two-factor code is required."

        if REQUIRES_2SA:
            self._log_debug("iCloud authentication blocked: reason=requires_2sa.")
            return False, "Two-step authentication is required; use app-specific passwords where possible."

        self._log_debug("iCloud authentication completed without MFA challenge.")
        return True, "Authenticated successfully."

    # --------------------------------------------------------------------------
    # This function completes a pending authentication challenge with an MFA
    # code.
    #
    # 1. "CODE" is the MFA code to validate.
    #
    # Returns: Tuple "(is_authenticated, details_message)".
    # --------------------------------------------------------------------------
    def complete_authentication(self, CODE: str) -> tuple[bool, str]:
        if self.api is None:
            self._log_debug("iCloud MFA completion failed: reason=session_missing.")
            return False, "Authentication session is not initialised."

        CODE = CODE.strip()

        if not CODE:
            self._log_debug("iCloud MFA completion failed: reason=code_missing.")
            return False, "Two-factor code is required."

        if not self.api.requires_2fa:
            self._log_debug("iCloud MFA completion skipped: reason=challenge_absent.")
            return True, "Authenticated successfully."

        self._log_debug("iCloud MFA validation started: code_present=True.")
        IS_VALID = self.api.validate_2fa_code(CODE)

        if not IS_VALID:
            self._log_debug("iCloud MFA validation failed: reason=apple_rejected_code.")
            return False, "Two-factor code was rejected by Apple."

        if self.api.is_trusted_session:
            self._log_debug("iCloud MFA validation completed: trusted_session=True.")
            return True, "Authenticated successfully with 2FA."

        self._log_debug("iCloud trust-session request started.")
        IS_TRUSTED = self.api.trust_session()

        if not IS_TRUSTED:
            self._log_debug("iCloud trust-session request failed.")
            return False, "Two-factor code was accepted, but Apple did not trust this session."

        self._log_debug("iCloud MFA validation completed: trusted_session=True.")
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
    #
    # N.B. Fetching "self.api.drive" can trigger a PCS consent check against
    # Apple that has no bounded timeout. A failure here is recorded as a
    # traversal hard failure so callers treat this run as incomplete, the
    # same way a stalled or failed directory read does. Without this, a
    # transient iCloud or network failure here would either crash the whole
    # worker process, or (if silently swallowed) return an empty entry list
    # that looks identical to "iCloud Drive is empty". That is dangerous
    # when "BACKUP_DELETE_REMOVED" is enabled, since it would delete the
    # entire local backup tree on the next run.
    # --------------------------------------------------------------------------
    def list_entries(self) -> list[RemoteEntry]:
        if self.api is None:
            self._log_debug("iCloud traversal skipped: reason=not_authenticated.")
            return []

        self._reset_traversal_stats()

        try:
            DRIVE_ROOT = self.api.drive
        except Exception as ERROR:
            self._record_drive_root_failure(ERROR)
            self._log_debug(
                "iCloud traversal failed: reason=drive_root_unavailable, "
                f"error_type={type(ERROR).__name__}."
            )
            return []

        if self.config.traversal_workers == 1:
            self._log_debug("iCloud traversal started: mode=serial, workers=1.")
            ENTRIES = self._walk_node(DRIVE_ROOT, "")
            self._log_debug(
                "iCloud traversal completed: "
                f"mode=serial, entries={len(ENTRIES)}."
            )
            return ENTRIES

        self._log_debug(
            "iCloud traversal started: "
            f"mode=parallel, workers={self.config.traversal_workers}."
        )
        ENTRIES = self._walk_node_parallel(DRIVE_ROOT, "")
        self._log_debug(
            "iCloud traversal completed: "
            f"mode=parallel, entries={len(ENTRIES)}."
        )
        return ENTRIES

    # --------------------------------------------------------------------------
    # This function traverses iCloud Drive using bounded parallel directory
    # reads.
    #
    # 1. "ROOT_NODE" is the iCloud Drive root node.
    # 2. "ROOT_PATH" is the relative path prefix for the root node.
    #
    # Returns: Flat list of discovered remote entries.
    # --------------------------------------------------------------------------
    def _walk_node_parallel(self, ROOT_NODE: Any, ROOT_PATH: str) -> list[RemoteEntry]:
        RESULT: list[RemoteEntry] = []
        DIRECTORIES_COMPLETED = 0

        with ThreadPoolExecutor(max_workers=self.config.traversal_workers) as EXECUTOR:
            FUTURES = {
                EXECUTOR.submit(self._walk_node_shallow, ROOT_NODE, ROOT_PATH): ROOT_PATH,
            }
            self._record_traversal_queue_state(
                DIRECTORIES_COMPLETED,
                len(FUTURES),
                min(len(FUTURES), self.config.traversal_workers),
            )
            LAST_PROGRESS_SNAPSHOT = self._get_traversal_progress_snapshot()

            while FUTURES:
                DONE_FUTURES, _ = wait(
                    FUTURES.keys(),
                    timeout=TRAVERSAL_WORKER_WAIT_TIMEOUT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )

                if not DONE_FUTURES:
                    CURRENT_PROGRESS_SNAPSHOT = self._get_traversal_progress_snapshot()
                    if self._has_traversal_progress_advanced(
                        LAST_PROGRESS_SNAPSHOT,
                        CURRENT_PROGRESS_SNAPSHOT,
                    ):
                        LAST_PROGRESS_SNAPSHOT = CURRENT_PROGRESS_SNAPSHOT
                        self._record_traversal_queue_state(
                            DIRECTORIES_COMPLETED,
                            len(FUTURES),
                            min(len(FUTURES), self.config.traversal_workers),
                        )
                        continue

                    STALLED_PATH = sorted(
                        CURRENT_PATH or "/"
                        for CURRENT_PATH in FUTURES.values()
                    )[0]
                    self._record_traversal_worker_timeout(
                        STALLED_PATH,
                        TRAVERSAL_WORKER_WAIT_TIMEOUT_SECONDS,
                    )
                    self._record_traversal_queue_state(
                        DIRECTORIES_COMPLETED,
                        len(FUTURES),
                        min(len(FUTURES), self.config.traversal_workers),
                    )
                    raise TraversalWorkerTimeoutError(
                        "Traversal worker stalled while reading "
                        f"{STALLED_PATH} after "
                        f"{TRAVERSAL_WORKER_WAIT_TIMEOUT_SECONDS:.1f}s."
                    )

                for FUTURE in DONE_FUTURES:
                    del FUTURES[FUTURE]
                    ENTRIES, CHILD_DIRECTORIES = FUTURE.result()
                    DIRECTORIES_COMPLETED += 1
                    RESULT.extend(ENTRIES)

                    for CHILD_PATH, CHILD_NODE in CHILD_DIRECTORIES:
                        FUTURES[
                            EXECUTOR.submit(self._walk_node_shallow, CHILD_NODE, CHILD_PATH)
                        ] = CHILD_PATH
                    LAST_PROGRESS_SNAPSHOT = self._get_traversal_progress_snapshot()

                self._record_traversal_queue_state(
                    DIRECTORIES_COMPLETED,
                    len(FUTURES),
                    min(len(FUTURES), self.config.traversal_workers),
                )

        return sorted(RESULT, key=lambda ENTRY: ENTRY.path)

    # --------------------------------------------------------------------------
    # This function returns one-level entries and child directories for
    # traversal.
    #
    # 1. "NODE" is the current drive node.
    # 2. "CURRENT_PATH" is the current relative path prefix.
    #
    # Returns: Tuple "(entries, child_directories)".
    # --------------------------------------------------------------------------
    def _walk_node_shallow(
        self,
        NODE: Any,
        CURRENT_PATH: str,
    ) -> tuple[list[RemoteEntry], list[tuple[str, Any]]]:
        DIRECTORY_INFO = self._node_dir(NODE, CURRENT_PATH)
        DIRS = DIRECTORY_INFO.get("dirs", [])
        FILES = DIRECTORY_INFO.get("files", [])
        NAMES = DIRECTORY_INFO.get("names", [])

        if isinstance(NAMES, list) and NAMES:
            return self._shallow_entries_from_names(NODE, CURRENT_PATH, NAMES)

        return self._shallow_entries_from_payload(NODE, CURRENT_PATH, DIRS, FILES)

    # --------------------------------------------------------------------------
    # This function builds one-level entries from child-name payloads.
    #
    # 1. "NODE" is current drive node.
    # 2. "CURRENT_PATH" is current relative path.
    # 3. "NAMES" is list of child names from pyicloud.
    #
    # Returns: Tuple "(entries, child_directories)".
    # --------------------------------------------------------------------------
    def _shallow_entries_from_names(
        self,
        NODE: Any,
        CURRENT_PATH: str,
        NAMES: list[str],
    ) -> tuple[list[RemoteEntry], list[tuple[str, Any]]]:
        ENTRIES: list[RemoteEntry] = []
        CHILD_DIRECTORIES: list[tuple[str, Any]] = []

        for NAME in NAMES:
            CLEAN_NAME = self._normalise_child_name(NAME)

            if not CLEAN_NAME:
                continue

            CHILD = self._child_node(NODE, CLEAN_NAME)

            if self._should_skip_child_node(NODE, CHILD):
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{CLEAN_NAME}".strip("/")
            self._log_traversal_child_discovered(
                RELATIVE_PATH,
                "name_list",
                "unknown",
            )
            IS_DIR = self._child_is_dir(CHILD, RELATIVE_PATH)

            if IS_DIR:
                self._log_traversal_child_classified(RELATIVE_PATH, "directory")
                self._record_traversal_entry(True)
                ENTRIES.append(self._build_child_entry(RELATIVE_PATH, CHILD, True))
                CHILD_DIRECTORIES.append((RELATIVE_PATH, CHILD))
                continue

            self._log_traversal_child_classified(RELATIVE_PATH, "file")
            self._record_traversal_entry(False)
            ENTRIES.append(self._build_child_entry(RELATIVE_PATH, CHILD, False))

        return ENTRIES, CHILD_DIRECTORIES

    # --------------------------------------------------------------------------
    # This function builds one-level entries from normalised dir/file payloads.
    #
    # 1. "NODE" is current drive node.
    # 2. "CURRENT_PATH" is current relative path.
    # 3. "DIRS" is directory metadata list.
    # 4. "FILES" is file metadata list.
    #
    # Returns: Tuple "(entries, child_directories)".
    # --------------------------------------------------------------------------
    def _shallow_entries_from_payload(
        self,
        NODE: Any,
        CURRENT_PATH: str,
        DIRS: list[Any],
        FILES: list[Any],
    ) -> tuple[list[RemoteEntry], list[tuple[str, Any]]]:
        ENTRIES = self._entries_from_files(CURRENT_PATH, FILES)
        CHILD_DIRECTORIES: list[tuple[str, Any]] = []

        for ITEM in DIRS:
            NAME = self._item_name(ITEM)

            if not NAME:
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{NAME}".strip("/")
            self._log_traversal_child_discovered(
                RELATIVE_PATH,
                "directory_payload",
                "directory",
            )
            self._log_traversal_child_classified(RELATIVE_PATH, "directory")
            self._record_traversal_entry(True)
            ENTRIES.append(self._build_directory_item_entry(RELATIVE_PATH, ITEM))

            CHILD = self._child_node(NODE, NAME)

            if self._should_skip_child_node(NODE, CHILD):
                continue

            CHILD_DIRECTORIES.append((RELATIVE_PATH, CHILD))

        return ENTRIES, CHILD_DIRECTORIES

    # --------------------------------------------------------------------------
    # This function reads directory payload with bounded retries for transient
    # failures.
    #
    # 1. "NODE" is the current drive node.
    # 2. "CURRENT_PATH" is the current remote path for log attribution.
    #
    # Returns: Directory payload when available, otherwise None.
    # --------------------------------------------------------------------------
    def _read_dir_payload_with_retry(self, NODE: Any, CURRENT_PATH: str = "") -> Any | None:
        ATTEMPT = 0

        while ATTEMPT < DIR_RETRY_ATTEMPTS:
            STARTED_EPOCH = time.monotonic()
            IS_RETRY = ATTEMPT > 0
            self._log_traversal_probe_started(CURRENT_PATH, ATTEMPT + 1)
            try:
                PAYLOAD = NODE.dir()
                self._record_directory_read(
                    CURRENT_PATH,
                    time.monotonic() - STARTED_EPOCH,
                    IS_RETRY,
                    "ok",
                )
                return PAYLOAD
            except (AttributeError, NotADirectoryError, TypeError):
                self._record_directory_read(
                    CURRENT_PATH,
                    time.monotonic() - STARTED_EPOCH,
                    IS_RETRY,
                    "non_directory",
                )
                self._log_debug(
                    "Traversal probe non-directory: "
                    f"path={CURRENT_PATH or '/'}."
                )
                return None
            except ValueError:
                self._record_directory_read(
                    CURRENT_PATH,
                    time.monotonic() - STARTED_EPOCH,
                    IS_RETRY,
                    "hard_failure",
                    "ValueError",
                )
                self._log_debug(
                    "Traversal probe failure: "
                    f"path={CURRENT_PATH or '/'}, "
                    "reason=value_error."
                )
                return None
            except Exception as ERROR:
                ATTEMPT += 1
                IS_FINAL_ATTEMPT = ATTEMPT >= DIR_RETRY_ATTEMPTS
                STATUS = "hard_failure" if IS_FINAL_ATTEMPT else "retryable_error"
                FAILURE_REASON = f"{type(ERROR).__name__}: {ERROR}"
                self._record_directory_read(
                    CURRENT_PATH,
                    time.monotonic() - STARTED_EPOCH,
                    IS_RETRY,
                    STATUS,
                    FAILURE_REASON,
                )

                if IS_FINAL_ATTEMPT:
                    self._log_debug(
                        "Traversal probe failure: "
                        f"path={CURRENT_PATH or '/'}, "
                        "reason=retry_limit_reached, "
                        f"error_type={type(ERROR).__name__}."
                    )
                    return None

                self._log_debug(
                    "Traversal probe retry: "
                    f"path={CURRENT_PATH or '/'}, "
                    f"attempt={ATTEMPT}, "
                    f"reason={type(ERROR).__name__}."
                )
                time.sleep(self._retry_delay_seconds(ATTEMPT))

        return None

    # --------------------------------------------------------------------------
    # This function computes exponential retry delay for directory reads.
    #
    # 1. "ATTEMPT" is one-based retry attempt index.
    #
    # Returns: Delay in seconds bounded by configured retry limits.
    # --------------------------------------------------------------------------
    def _retry_delay_seconds(self, ATTEMPT: int) -> float:
        DELAY_SECONDS = DIR_RETRY_BASE_DELAY_SECONDS * (2 ** (ATTEMPT - 1))
        return min(DELAY_SECONDS, DIR_RETRY_MAX_DELAY_SECONDS)

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
        self._record_serial_directory_enter()
        RESULT: list[RemoteEntry] = []

        try:
            DIRECTORY_INFO = self._node_dir(NODE, CURRENT_PATH)
            DIRS = DIRECTORY_INFO.get("dirs", [])
            FILES = DIRECTORY_INFO.get("files", [])
            NAMES = DIRECTORY_INFO.get("names", [])

            if isinstance(NAMES, list) and NAMES:
                RESULT.extend(self._entries_from_names(NODE, CURRENT_PATH, NAMES))
                return RESULT

            RESULT.extend(self._entries_from_directories(NODE, CURRENT_PATH, DIRS))
            RESULT.extend(self._entries_from_files(CURRENT_PATH, FILES))

            return RESULT
        finally:
            self._record_serial_directory_exit()

    # --------------------------------------------------------------------------
    # This function safely fetches directory metadata from a node.
    #
    # 1. "NODE" is the current drive node.
    #
    # Returns: Dictionary containing "dirs" and "files" lists.
    # --------------------------------------------------------------------------
    def _node_dir(self, NODE: Any, CURRENT_PATH: str = "") -> dict[str, Any]:
        PAYLOAD = self._read_dir_payload_with_retry(NODE, CURRENT_PATH)

        if PAYLOAD is None:
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
            CLEAN_NAME = self._normalise_child_name(NAME)

            if not CLEAN_NAME:
                continue

            CHILD = self._child_node(NODE, CLEAN_NAME)

            if self._should_skip_child_node(NODE, CHILD):
                continue

            RELATIVE_PATH = f"{CURRENT_PATH}/{CLEAN_NAME}".strip("/")
            self._log_traversal_child_discovered(
                RELATIVE_PATH,
                "name_list",
                "unknown",
            )
            IS_DIR = self._child_is_dir(CHILD, RELATIVE_PATH)

            if IS_DIR:
                self._log_traversal_child_classified(RELATIVE_PATH, "directory")
                self._record_traversal_entry(True)
                RESULT.append(self._build_child_entry(RELATIVE_PATH, CHILD, True))
                RESULT.extend(self._walk_node(CHILD, RELATIVE_PATH))
                continue

            self._log_traversal_child_classified(RELATIVE_PATH, "file")
            self._record_traversal_entry(False)
            RESULT.append(self._build_child_entry(RELATIVE_PATH, CHILD, False))

        return RESULT

    # --------------------------------------------------------------------------
    # This function builds a remote entry from a child node probe result.
    #
    # 1. "RELATIVE_PATH" is the discovered remote path.
    # 2. "CHILD" is the pyicloud child node.
    # 3. "IS_DIR" indicates whether the child is a directory.
    #
    # Returns: Normalised remote entry for traversal results.
    # --------------------------------------------------------------------------
    def _build_child_entry(
        self,
        RELATIVE_PATH: str,
        CHILD: Any,
        IS_DIR: bool,
    ) -> RemoteEntry:
        if IS_DIR:
            return RemoteEntry(
                path=RELATIVE_PATH,
                is_dir=True,
                size=0,
                modified=self._child_modified(CHILD),
            )

        return RemoteEntry(
            path=RELATIVE_PATH,
            is_dir=False,
            size=self._child_size(CHILD),
            modified=self._child_modified(CHILD),
        )

    # --------------------------------------------------------------------------
    # This function builds a directory remote entry from item metadata payload.
    #
    # 1. "RELATIVE_PATH" is the discovered remote path.
    # 2. "ITEM" is the normalised directory metadata item.
    #
    # Returns: Directory remote entry for traversal results.
    # --------------------------------------------------------------------------
    def _build_directory_item_entry(
        self,
        RELATIVE_PATH: str,
        ITEM: Any,
    ) -> RemoteEntry:
        return RemoteEntry(
            path=RELATIVE_PATH,
            is_dir=True,
            size=0,
            modified=self._item_modified(ITEM),
        )

    # --------------------------------------------------------------------------
    # This function infers whether a child node is a directory.
    #
    # 1. "CHILD" is a pyicloud drive node.
    # 2. "CURRENT_PATH" is the remote path used for log attribution.
    #
    # Returns: True for directories, otherwise False.
    # --------------------------------------------------------------------------
    def _child_is_dir(self, CHILD: Any, CURRENT_PATH: str = "") -> bool:
        CHILD_TYPE = str(getattr(CHILD, "type", "")).lower()

        if CHILD_TYPE in {"folder", "directory", "dir"}:
            return True

        if getattr(CHILD, "is_folder", None) is True:
            return True

        if getattr(CHILD, "isFolder", None) is True:
            return True

        if getattr(CHILD, "is_folder", None) is False:
            return False

        if getattr(CHILD, "isFolder", None) is False:
            return False

        PAYLOAD = self._read_dir_payload_with_retry(CHILD, CURRENT_PATH)

        if PAYLOAD is None:
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
            self._log_traversal_child_discovered(
                RELATIVE_PATH,
                "directory_payload",
                "directory",
            )
            self._log_traversal_child_classified(RELATIVE_PATH, "directory")
            self._record_traversal_entry(True)
            RESULT.append(
                RemoteEntry(
                    path=RELATIVE_PATH,
                    is_dir=True,
                    size=0,
                    modified=self._item_modified(ITEM),
                )
            )

            CHILD = self._child_node(NODE, NAME)

            if self._should_skip_child_node(NODE, CHILD):
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
            self._log_traversal_child_discovered(
                RELATIVE_PATH,
                "file_payload",
                "file",
            )
            self._log_traversal_child_classified(RELATIVE_PATH, "file")
            self._record_traversal_entry(False)
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
            VALUE = self._normalise_child_name(ITEM.get(KEY, ""))

            if VALUE:
                return VALUE

        return ""

    # --------------------------------------------------------------------------
    # This function normalises one discovered child name for traversal use.
    #
    # 1. "VALUE" is the raw child name or metadata field value.
    #
    # Returns: Safe relative child name, or empty string when invalid.
    #
    # N.B.
    # - Root markers such as "/", "." and ".." are not real child entries and
    #   must never be fed back into traversal.
    # - Embedded slashes are rejected because traversal names are single path
    #   segments, not relative paths.
    # --------------------------------------------------------------------------
    def _normalise_child_name(self, VALUE: Any) -> str:
        CLEAN_NAME = str(VALUE).strip().strip("/")

        if not CLEAN_NAME:
            return ""

        if CLEAN_NAME in {".", ".."}:
            return ""

        if "/" in CLEAN_NAME:
            return ""

        return CLEAN_NAME

    # --------------------------------------------------------------------------
    # This function rejects missing or self-referential child nodes.
    #
    # 1. "PARENT_NODE" is the current traversal node.
    # 2. "CHILD_NODE" is the resolved child node candidate.
    #
    # Returns: True when traversal should skip the child node.
    # --------------------------------------------------------------------------
    def _should_skip_child_node(self, PARENT_NODE: Any, CHILD_NODE: Any) -> bool:
        if CHILD_NODE is None:
            return True

        return CHILD_NODE is PARENT_NODE

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
    # Returns: "DownloadResult" describing the final transfer outcome.
    # --------------------------------------------------------------------------
    def download_file(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        if self.api is None:
            self._log_debug(
                "iCloud file download skipped: reason=not_authenticated."
            )
            return DownloadResult(False, "not_authenticated")

        self._log_debug(
            "iCloud file download started: "
            f"remote_path={REMOTE_PATH}, "
            f"local_path={LOCAL_PATH.as_posix()}."
        )
        FILE_OBJ = self._resolve_file_object(REMOTE_PATH)

        if FILE_OBJ is None:
            self._log_debug(
                "iCloud file download failed: reason=path_not_found."
            )
            return DownloadResult(False, "path_not_found")

        if self._child_is_dir(FILE_OBJ, REMOTE_PATH):
            self._log_debug(
                "iCloud file download failed: reason=directory_node."
            )
            return DownloadResult(False, "directory_node")

        IS_SUCCESS, FAILURE_REASON = self._download_file_object(FILE_OBJ, LOCAL_PATH)
        self._log_debug(
            "iCloud file download completed: "
            f"is_success={IS_SUCCESS}, "
            f"failure_reason={FAILURE_REASON or 'none'}."
        )
        return DownloadResult(IS_SUCCESS, FAILURE_REASON)

    # --------------------------------------------------------------------------
    # This function downloads package-like directory nodes recursively.
    #
    # 1. "REMOTE_PATH" is slash-separated iCloud Drive path.
    # 2. "LOCAL_PATH" is filesystem destination directory.
    #
    # Returns: "DownloadResult" describing the final transfer outcome.
    # --------------------------------------------------------------------------
    def download_package_tree(self, REMOTE_PATH: str, LOCAL_PATH: Path) -> DownloadResult:
        if self.api is None:
            self._log_debug(
                "iCloud package download skipped: reason=not_authenticated."
            )
            return DownloadResult(False, "not_authenticated")

        self._log_debug(
            "iCloud package download started: "
            f"remote_path={REMOTE_PATH}, "
            f"local_path={LOCAL_PATH.as_posix()}."
        )
        ROOT_NODE = self._resolve_file_object(REMOTE_PATH)
        if ROOT_NODE is None:
            self._log_debug(
                "iCloud package download failed: reason=path_not_found."
            )
            return DownloadResult(False, "path_not_found")

        if not self._child_is_dir(ROOT_NODE, REMOTE_PATH):
            self._log_debug(
                "iCloud package download fallback started: "
                "reason=direct_node_not_directory."
            )
            RESULT = self._download_package_tree_from_parent_metadata(
                REMOTE_PATH,
                LOCAL_PATH,
            )
            if RESULT.is_success:
                self._log_debug(
                    "iCloud package download completed: "
                    "method=parent_metadata, "
                    "is_success=True."
                )
                return RESULT

            if RESULT.failure_reason:
                self._log_debug(
                    "iCloud package download failed: "
                    "method=parent_metadata, "
                    f"failure_reason={RESULT.failure_reason}."
                )
                return RESULT

            self._log_debug(
                "iCloud package download failed: reason=not_directory_node."
            )
            return DownloadResult(False, "not_directory_node")

        RESULT = self._download_package_node(ROOT_NODE, REMOTE_PATH, LOCAL_PATH)
        if RESULT.is_success:
            self._log_debug(
                "iCloud package download completed: "
                "method=node_walk, "
                "is_success=True."
            )
            return RESULT

        if RESULT.failure_reason:
            self._log_debug(
                "iCloud package download failed: "
                "method=node_walk, "
                f"failure_reason={RESULT.failure_reason}."
            )
            return RESULT

        self._log_debug(
            "iCloud package download failed: reason=package_download_failed."
        )
        return DownloadResult(False, "package_download_failed")

    # --------------------------------------------------------------------------
    # This function downloads a resolved file node to the provided local path.
    #
    # 1. "FILE_OBJ" is a resolved pyicloud drive node object.
    # 2. "LOCAL_PATH" is filesystem destination.
    #
    # Returns: Tuple "(is_success, failure_reason)".
    # --------------------------------------------------------------------------
    def _download_file_object(self, FILE_OBJ: Any, LOCAL_PATH: Path) -> tuple[bool, str]:
        LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)

        try:
            OPEN_RESULT = self._open_file_object(FILE_OBJ)
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            self._log_debug(
                "iCloud file object open failed: reason=open_exception."
            )
            return False, "open_failed"

        if OPEN_RESULT is None:
            self._log_debug(
                "iCloud file object open failed: reason=open_unavailable."
            )
            return False, "open_unavailable"

        IS_SUCCESS = self._write_open_result(OPEN_RESULT, LOCAL_PATH)
        if not IS_SUCCESS:
            self._log_debug("iCloud file write failed: reason=write_failed.")
            return False, "write_failed"

        return True, ""

    # --------------------------------------------------------------------------
    # This function recursively exports package directory content to local
    # paths.
    #
    # 1. "NODE" is current package node.
    # 2. "REMOTE_PATH" is remote path for this node.
    # 3. "LOCAL_PATH" is local path for this node.
    #
    # Returns: "DownloadResult" describing the final transfer outcome.
    # --------------------------------------------------------------------------
    def _download_package_node(
        self,
        NODE: Any,
        REMOTE_PATH: str,
        LOCAL_PATH: Path,
    ) -> DownloadResult:
        LOCAL_PATH.mkdir(parents=True, exist_ok=True)
        CHILDREN = self._package_child_names(NODE)

        if not CHILDREN:
            return DownloadResult(True)

        for NAME in CHILDREN:
            CHILD_NODE = self._child_node(NODE, NAME)
            if CHILD_NODE is None:
                return DownloadResult(False, "package_child_missing")

            CHILD_REMOTE_PATH = f"{REMOTE_PATH}/{NAME}".strip("/")
            CHILD_LOCAL_PATH = LOCAL_PATH / NAME
            if self._child_is_dir(CHILD_NODE, CHILD_REMOTE_PATH):
                RESULT = self._download_package_node(
                    CHILD_NODE,
                    CHILD_REMOTE_PATH,
                    CHILD_LOCAL_PATH,
                )
                if RESULT.is_success:
                    continue

                return RESULT

            RESULT = DownloadResult(*self._download_file_object(CHILD_NODE, CHILD_LOCAL_PATH))
            if RESULT.is_success:
                continue

            return RESULT

        return DownloadResult(True)

    # --------------------------------------------------------------------------
    # This function exports package-like nodes using parent directory metadata
    # when direct directory traversal is not available from pyicloud.
    #
    # 1. "REMOTE_PATH" is slash-separated package path.
    # 2. "LOCAL_PATH" is package destination directory.
    #
    # Returns: "DownloadResult" describing the final transfer outcome.
    # --------------------------------------------------------------------------
    def _download_package_tree_from_parent_metadata(
        self,
        REMOTE_PATH: str,
        LOCAL_PATH: Path,
    ) -> DownloadResult:
        PARENT_PATH, ITEM_NAME = self._split_parent_path(REMOTE_PATH)
        PACKAGE_ITEM = self._find_item_metadata(PARENT_PATH, ITEM_NAME)
        if PACKAGE_ITEM is None:
            return DownloadResult(False, "package_item_missing")

        CHILD_ITEMS = self._package_child_items(PACKAGE_ITEM)
        if not CHILD_ITEMS:
            return DownloadResult(False, "package_children_unavailable")

        return self._download_package_items_by_metadata(
            REMOTE_PATH,
            LOCAL_PATH,
            CHILD_ITEMS,
        )

    # --------------------------------------------------------------------------
    # This function recursively exports package child metadata to local files.
    #
    # 1. "PARENT_REMOTE_PATH" is package or subdirectory remote path.
    # 2. "PARENT_LOCAL_PATH" is matching local directory path.
    # 3. "CHILD_ITEMS" is ordered child item metadata list.
    #
    # Returns: "DownloadResult" describing the final transfer outcome.
    # --------------------------------------------------------------------------
    def _download_package_items_by_metadata(
        self,
        PARENT_REMOTE_PATH: str,
        PARENT_LOCAL_PATH: Path,
        CHILD_ITEMS: list[tuple[str, dict[str, Any]]],
    ) -> DownloadResult:
        PARENT_LOCAL_PATH.mkdir(parents=True, exist_ok=True)

        for ITEM_TYPE, ITEM in CHILD_ITEMS:
            CHILD_NAME = self._item_name(ITEM)
            if not CHILD_NAME:
                continue

            CHILD_REMOTE_PATH = f"{PARENT_REMOTE_PATH}/{CHILD_NAME}".strip("/")
            CHILD_LOCAL_PATH = PARENT_LOCAL_PATH / CHILD_NAME
            IS_DIRECTORY_ITEM = ITEM_TYPE == "dir"

            if IS_DIRECTORY_ITEM:
                NESTED_ITEMS = self._package_child_items(ITEM)
                if not NESTED_ITEMS:
                    return DownloadResult(False, "package_children_unavailable")

                RESULT = self._download_package_items_by_metadata(
                    CHILD_REMOTE_PATH,
                    CHILD_LOCAL_PATH,
                    NESTED_ITEMS,
                )
                if RESULT.is_success:
                    continue

                return RESULT

            IS_SUCCESS, FAILURE_REASON = self._download_file_by_remote_path(
                CHILD_REMOTE_PATH,
                CHILD_LOCAL_PATH,
            )
            RESULT = DownloadResult(IS_SUCCESS, FAILURE_REASON)
            if RESULT.is_success:
                continue

            return RESULT

        return DownloadResult(True)

    # --------------------------------------------------------------------------
    # This function downloads a file by remote path without relying on
    # shared mutable failure state.
    #
    # 1. "REMOTE_PATH" is slash-separated iCloud Drive file path.
    # 2. "LOCAL_PATH" is destination file path.
    #
    # Returns: Tuple "(is_success, failure_reason)".
    # --------------------------------------------------------------------------
    def _download_file_by_remote_path(
        self,
        REMOTE_PATH: str,
        LOCAL_PATH: Path,
    ) -> tuple[bool, str]:
        FILE_OBJ = self._resolve_file_object(REMOTE_PATH)
        if FILE_OBJ is None:
            return False, "path_not_found"

        if self._child_is_dir(FILE_OBJ, REMOTE_PATH):
            return False, "directory_node"

        return self._download_file_object(FILE_OBJ, LOCAL_PATH)

    # --------------------------------------------------------------------------
    # This function resolves parent path and item name from a remote path.
    #
    # 1. "REMOTE_PATH" is slash-separated iCloud Drive path.
    #
    # Returns: Tuple "(parent_path, item_name)".
    # --------------------------------------------------------------------------
    def _split_parent_path(self, REMOTE_PATH: str) -> tuple[str, str]:
        PARTS = [PART for PART in REMOTE_PATH.split("/") if PART]
        if not PARTS:
            return "", ""

        ITEM_NAME = PARTS[-1].strip()
        PARENT_PATH = "/".join(PARTS[:-1]).strip("/")
        return PARENT_PATH, ITEM_NAME

    # --------------------------------------------------------------------------
    # This function finds a named item dictionary from parent metadata payload.
    #
    # 1. "PARENT_PATH" is slash-separated parent directory path.
    # 2. "ITEM_NAME" is target child name.
    #
    # Returns: Matching item metadata dictionary, otherwise None.
    # --------------------------------------------------------------------------
    def _find_item_metadata(
        self,
        PARENT_PATH: str,
        ITEM_NAME: str,
    ) -> dict[str, Any] | None:
        PARENT_NODE = self._resolve_file_object(PARENT_PATH)
        if PARENT_NODE is None:
            return None

        PARENT_INFO = self._node_dir(PARENT_NODE)
        SEARCH_ITEMS = PARENT_INFO.get("dirs", []) + PARENT_INFO.get("files", [])

        for ITEM in SEARCH_ITEMS:
            if not isinstance(ITEM, dict):
                continue

            NAME = self._item_name(ITEM)
            if NAME == ITEM_NAME:
                return ITEM

        return None

    # --------------------------------------------------------------------------
    # This function extracts package child metadata from a package item payload.
    #
    # 1. "ITEM" is package metadata dictionary.
    #
    # Returns: Ordered list of tuples "(item_type, item_metadata)" where
    # "item_type" is either "dir" or "file".
    # --------------------------------------------------------------------------
    def _package_child_items(self, ITEM: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        NORMALISED = self._normalise_dir_payload(ITEM)
        RESULT: list[tuple[str, dict[str, Any]]] = []

        for DIR_ITEM in NORMALISED.get("dirs", []):
            if isinstance(DIR_ITEM, dict):
                RESULT.append(("dir", DIR_ITEM))

        for FILE_ITEM in NORMALISED.get("files", []):
            if isinstance(FILE_ITEM, dict):
                RESULT.append(("file", FILE_ITEM))

        return RESULT

    # --------------------------------------------------------------------------
    # This function collects unique child names from package directory metadata.
    #
    # 1. "NODE" is current package node.
    #
    # Returns: Ordered child-name list.
    # --------------------------------------------------------------------------
    def _package_child_names(self, NODE: Any) -> list[str]:
        DIRECTORY_INFO = self._node_dir(NODE)
        NAMES = DIRECTORY_INFO.get("names", [])
        DIRS = DIRECTORY_INFO.get("dirs", [])
        FILES = DIRECTORY_INFO.get("files", [])
        RESULT: list[str] = []

        for ITEM in NAMES:
            NAME = str(ITEM).strip()
            if not NAME:
                continue
            RESULT.append(NAME)

        for ITEM in DIRS + FILES:
            if not isinstance(ITEM, dict):
                continue
            NAME = self._item_name(ITEM)
            if not NAME:
                continue
            RESULT.append(NAME)

        return sorted(set(RESULT))

    # --------------------------------------------------------------------------
    # This function opens a remote file object using stream mode when supported.
    #
    # 1. "FILE_OBJ" is a resolved pyicloud drive node object.
    #
    # Returns: Open-result object from pyicloud node API, or None.
    # --------------------------------------------------------------------------
    def _open_file_object(self, FILE_OBJ: Any) -> Any | None:
        OPEN_METHOD = getattr(FILE_OBJ, "open", None)

        if not callable(OPEN_METHOD):
            return None

        try:
            return OPEN_METHOD(stream=True)
        except TypeError:
            return OPEN_METHOD()

    # --------------------------------------------------------------------------
    # This function writes content from a file-open result and closes it when
    # required.
    #
    # 1. "OPEN_RESULT" is the object returned from "FILE_OBJ.open(stream=True)".
    # 2. "LOCAL_PATH" is filesystem destination.
    #
    # Returns: True on successful write, otherwise False.
    # --------------------------------------------------------------------------
    def _write_open_result(self, OPEN_RESULT: Any, LOCAL_PATH: Path) -> bool:
        if hasattr(OPEN_RESULT, "__enter__") and hasattr(OPEN_RESULT, "__exit__"):
            with OPEN_RESULT as RESPONSE:
                return self._write_downloaded_content(RESPONSE, LOCAL_PATH, CLOSE_RESPONSE=False)

        return self._write_downloaded_content(OPEN_RESULT, LOCAL_PATH)

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
    # 3. "CLOSE_RESPONSE" controls whether this helper owns stream closure.
    #
    # Returns: True on successful write, otherwise False.
    # --------------------------------------------------------------------------
    def _write_downloaded_content(
        self,
        RESPONSE: Any,
        LOCAL_PATH: Path,
        CLOSE_RESPONSE: bool = True,
    ) -> bool:
        try:
            TEMP_PATH = self._temporary_download_path(LOCAL_PATH)
            STATUS_CODE = getattr(RESPONSE, "status_code", None)

            if isinstance(STATUS_CODE, int) and STATUS_CODE >= 400:
                return False

            if hasattr(RESPONSE, "iter_content"):
                return self._write_iter_content(RESPONSE, LOCAL_PATH, TEMP_PATH)

            RAW = getattr(RESPONSE, "raw", None)

            if RAW is not None:
                return self._write_raw_content(RAW, LOCAL_PATH, TEMP_PATH)

            CONTENT = getattr(RESPONSE, "content", None)

            if CONTENT is not None:
                return self._write_byte_content(CONTENT, LOCAL_PATH, TEMP_PATH)

            if hasattr(RESPONSE, "read"):
                return self._write_readable_content(RESPONSE, LOCAL_PATH, TEMP_PATH)

            if isinstance(RESPONSE, (bytes, bytearray, memoryview, str)):
                return self._write_byte_content(RESPONSE, LOCAL_PATH, TEMP_PATH)

            return False
        finally:
            if CLOSE_RESPONSE:
                self._close_download_response(RESPONSE)

    # --------------------------------------------------------------------------
    # This function closes download response objects and nested raw streams.
    #
    # 1. "RESPONSE" is the download response or readable stream object.
    #
    # Returns: None.
    # --------------------------------------------------------------------------
    def _close_download_response(self, RESPONSE: Any) -> None:
        CLOSE_METHOD = getattr(RESPONSE, "close", None)

        if callable(CLOSE_METHOD):
            try:
                CLOSE_METHOD()
            except OSError:
                pass

        RAW = getattr(RESPONSE, "raw", None)

        if RAW is None or RAW is RESPONSE:
            return

        RAW_CLOSE_METHOD = getattr(RAW, "close", None)

        if not callable(RAW_CLOSE_METHOD):
            return

        try:
            RAW_CLOSE_METHOD()
        except OSError:
            pass

    # --------------------------------------------------------------------------
    # This function writes byte-oriented response content atomically.
    #
    # 1. "CONTENT" is bytes-like or string payload.
    # 2. "LOCAL_PATH" is file destination.
    # 3. "TEMP_PATH" is temporary path used for atomic replacement.
    #
    # Returns: True on successful write, otherwise False.
    # --------------------------------------------------------------------------
    def _write_byte_content(self, CONTENT: Any, LOCAL_PATH: Path, TEMP_PATH: Path) -> bool:
        try:
            self._cleanup_temporary_file(TEMP_PATH)
            BYTE_PAYLOAD = self._normalise_byte_payload(CONTENT)

            with TEMP_PATH.open("wb") as HANDLE:
                HANDLE.write(BYTE_PAYLOAD)

            TEMP_PATH.replace(LOCAL_PATH)
        except (AttributeError, OSError, TypeError, ValueError):
            self._cleanup_temporary_file(TEMP_PATH)
            return False

        return True

    # --------------------------------------------------------------------------
    # This function writes content from a file-like object with "read" support.
    #
    # 1. "RESPONSE" is an object exposing a "read" method.
    # 2. "LOCAL_PATH" is file destination.
    # 3. "TEMP_PATH" is temporary path used for atomic replacement.
    #
    # Returns: True on successful write, otherwise False.
    # --------------------------------------------------------------------------
    def _write_readable_content(self, RESPONSE: Any, LOCAL_PATH: Path, TEMP_PATH: Path) -> bool:
        try:
            self._cleanup_temporary_file(TEMP_PATH)

            with TEMP_PATH.open("wb") as HANDLE:
                while True:
                    CHUNK = RESPONSE.read(self.config.download_chunk_mib * 1024 * 1024)

                    if not CHUNK:
                        break

                    HANDLE.write(self._normalise_byte_payload(CHUNK))

            TEMP_PATH.replace(LOCAL_PATH)
        except (AttributeError, OSError, TypeError, ValueError):
            self._cleanup_temporary_file(TEMP_PATH)
            return False

        return True

    # --------------------------------------------------------------------------
    # This function writes content from a response "raw" stream object.
    #
    # 1. "RAW" is response raw stream object.
    # 2. "LOCAL_PATH" is file destination.
    # 3. "TEMP_PATH" is temporary path used for atomic replacement.
    #
    # Returns: True on successful write, otherwise False.
    # --------------------------------------------------------------------------
    def _write_raw_content(self, RAW: Any, LOCAL_PATH: Path, TEMP_PATH: Path) -> bool:
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
    # This function normalises response payloads to byte content.
    #
    # 1. "PAYLOAD" is any bytes-like, string, or scalar payload.
    #
    # Returns: Byte payload ready for file writes.
    # --------------------------------------------------------------------------
    def _normalise_byte_payload(self, PAYLOAD: Any) -> bytes:
        if isinstance(PAYLOAD, bytes):
            return PAYLOAD

        if isinstance(PAYLOAD, bytearray):
            return bytes(PAYLOAD)

        if isinstance(PAYLOAD, memoryview):
            return PAYLOAD.tobytes()

        if isinstance(PAYLOAD, str):
            return PAYLOAD.encode("utf-8")

        return bytes(PAYLOAD)

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
                CHUNK_SIZE_BYTES = self.config.download_chunk_mib * 1024 * 1024

                for CHUNK in RESPONSE.iter_content(chunk_size=CHUNK_SIZE_BYTES):
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
