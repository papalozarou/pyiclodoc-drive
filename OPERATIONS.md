# Operations

## Runtime notes

- Compose `init: true` is required by the provided service definitions.
- Health checks use `parallel` from the microcheck toolbox image.
- A background heartbeat updater refreshes `/logs/iclouddd-heartbeat.txt` every 30
  seconds in both recurring and one-shot execution paths.
- Telegram commands are ignored unless they come from `H_TGM_CHAT_ID`.
- Entrypoint starts as root only to read Docker secret files, then drops to
  `PUID:PGID` before launching the worker process.
- Services keep `cap_drop: ALL` and add only `SETUID` and `SETGID` so
  privilege drop works.
- Set `LOG_LEVEL=debug` in Compose `default-env` for verbose runtime
  diagnostics.
- At `LOG_LEVEL=info`, worker logs include stage boundary markers for
  traversal and transfer start/finish so run progress is still visible.
- Traversal can take a long time for large iCloud libraries, and transfer can
  also run for extended periods when many changed files are queued.
- During transfer execution, debug logs include periodic in-run progress
  lines every 30 seconds so long backups remain observable.
- During traversal/listing, debug logs include periodic in-run progress
  lines every 30 seconds while iCloud entry discovery is still running.
- Progress updates are wrapped with separator lines in debug output for easier
  visual scanning in container logs.
- Error lines are coloured red in container stdout; file logs remain plain text.
- Worker logs rotate daily and at size threshold, are compressed to
  `iclouddd-worker.*.log.gz`, and are pruned by configured retention days.

## Privilege model

- Worker runtime is non-root.
- Root is used at startup only for secret file access under `/run/secrets`.
- If your Docker runtime blocks group switching (`setgroups`), startup can fail
  during the privilege drop step.

## Scheduling

For full scheduling behaviour, option compatibility, manual command effects, and
validation rules, see [SCHEDULING.md](SCHEDULING.md).

## One-shot mode

- Enable with `<SVC>_RUN_ONCE=true`.
- Set `<SVC>_RESTART_POLICY=no` to avoid automatic restarts.
- This pairing is required: one-shot with `unless-stopped` (or similar) will
  restart the container after exit and loop.
- Worker waits for Telegram `auth` or `reauth` commands when MFA or reauth is
  pending, then runs one backup attempt and exits.
- While one-shot is running, heartbeat updates continue so container health
  status reflects liveness during auth wait and backup execution.
- If auth does not complete within the one-shot wait window, worker exits
  non-zero.
- Exit is non-zero when auth is incomplete, reauth is pending, or first-run
  safety net blocks backup.

## Transfer performance

- Incremental sync uses `iclouddd-manifest.json` and skips unchanged files.
- On first run with an empty manifest, worker reconciles existing local files
  under `/output` against remote metadata (size and modified time) and seeds
  manifest entries without re-downloading matched files.
- Directory traversal can run in bounded parallel mode with
  `SYNC_TRAVERSAL_WORKERS`.
- Changed-file downloads run in parallel automatically based on host CPU.
- Worker count is internally bounded and can be overridden with
  `SYNC_DOWNLOAD_WORKERS`.
- Download stream chunk size can be tuned with `SYNC_DOWNLOAD_CHUNK_MIB`.
- Successful downloads preserve remote modified timestamps on local files.
- When direct file download fails on package-style nodes, worker falls back to
  recursive package export for directory-like items such as app and bundle
  structures.
- Optional mirror-delete behaviour can be enabled with
  `BACKUP_DELETE_REMOVED=true`, which prunes local files and empty directories
  under `/output` when they no longer exist in iCloud.
- Transient transfer exceptions (for example iCloud throttling and 5xx errors)
  are retried with bounded backoff before being marked as failed.
- Directory traversal applies bounded retry/backoff for transient iCloud API
  failures before treating a node as unavailable.

## Safety-net behaviour

On first run only, each worker samples existing files in `/output` and checks
UID and GID for consistency against the container runtime user.

If mismatches are found, backup is blocked. Details are written to worker logs
and sent via Telegram. This is intended to avoid destructive rewrites over
existing backup trees with mixed ownership.
