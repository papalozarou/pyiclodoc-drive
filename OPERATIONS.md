# Operations

Use this when you need to start, check, upgrade, or troubleshoot the containers.

For schedule setup, see [SCHEDULING.md](SCHEDULING.md). For Telegram commands,
see [TELEGRAM.md](TELEGRAM.md).

## Start the containers

Start the containers with Compose:

```bash
docker compose up -d
```

Check that the containers are running:

```bash
docker compose ps
```

The provided Compose examples use `init: true`. Leave that in place so child
processes are cleaned up inside the container.

## Check whether backups are running

Container health comes from the heartbeat file:

```text
/logs/pyiclodoc-drive-heartbeat.txt
```

The container updates it every 30 seconds in scheduled mode and one-shot mode.

Check Docker health with:

```bash
docker inspect --format='{{json .State.Health}}' icloud_drive_alice
```

`HEALTHCHECK_MAX_AGE_SECONDS` sets how old the heartbeat can be before the
container or Docker health check fails. If unset, the default is `65` seconds.

N.B.

Do not set `HEALTHCHECK_MAX_AGE_SECONDS` below `30`. That is shorter than the
heartbeat interval and can make a healthy container look unhealthy.

## Read the logs

The main log file is:

```text
pyiclodoc-drive-worker.log
```

In the example setup it lives in the container's logs directory. That directory
is mounted at `/logs` inside the container.

Useful lines to look for:

```text
Traversal finished.
Transfer finished.
Backup complete.
Transfer failure reason detail:
```

At `LOG_LEVEL=info`, the log shows traversal start and finish, transfer start
and finish, and backup completion.

At `LOG_LEVEL=debug`, the log includes auth attempts, Telegram command
handling, schedule checks, manual backup requests, safety-net skips, traversal
progress, transfer progress, and transfer failure reasons.

Debug logs show whether a Telegram command had arguments, but they do not log
MFA codes, passwords, bot tokens, or secret file contents.

Logs rotate daily and when they reach the size limit. Rotated logs are
compressed as:

```text
pyiclodoc-drive-worker.*.log.gz
```

## Trigger a manual backup

Send the container a Telegram command:

```text
alice backup
```

The backup starts as soon as the container can run it. What happens next
depends on the schedule mode:

- interval mode recalculates from the manual run time
- calendar modes stay pinned to the next calendar slot

## Upgrade the image

Pull the current code or update your Compose image tag, then recreate the
containers:

```bash
docker compose down
docker compose up -d
```

For a local source build:

```bash
docker compose up -d --build
```

After startup, check:

```bash
docker compose ps
```

Then watch for the usual run markers in the container log:

```text
Traversal finished.
Transfer finished.
Backup complete.
```

## Run one backup and stop

Set one-shot mode for the container:

```env
ALICE_RUN_ONCE=true
ALICE_RESTART_POLICY=no
```

The container waits for Telegram `auth` or `reauth` if iCloud needs MFA. It
runs one backup attempt, then exits.

Exit is non-zero when auth is incomplete, reauth is pending, or the first-run
safety net blocks backup.

N.B.

If the restart policy is `unless-stopped` or similar, Compose will restart the
container after it exits. That is nearly always the wrong pairing for one-shot
mode.

## Tune large backups

Incremental sync uses:

```text
/config/pyiclodoc-drive-manifest.json
```

Unchanged files are skipped. On first run with an empty manifest, the container
checks existing local files under `/output` against remote size and modified
time, then seeds matching manifest entries without downloading those files
again.

The main settings are:

- `SYNC_TRAVERSAL_WORKERS`: how many directory scans can run at once
- `SYNC_DOWNLOAD_WORKERS`: changed-file download count, or `auto`
- `SYNC_DOWNLOAD_CHUNK_MIB`: download stream chunk size

Leave these alone unless a real backup is too slow or you are testing a
performance change.

Large iCloud Drives can spend a long time in traversal. At `LOG_LEVEL=debug`,
the container writes progress lines every 30 seconds during traversal and
transfer so you can tell the run is still moving.

## Use mirror delete

By default, local files are not deleted just because they disappear from iCloud.

Enable mirror delete with:

```env
ALICE_BACKUP_DELETE_REMOVED=true
```

When enabled, the container prunes local files and empty directories under
`/output` when they no longer exist in iCloud.

Non-empty directories are skipped during cleanup. Other directory deletion
failures are counted and logged as real errors.

## Understand the safety net

On first run only, each container samples existing files in `/output` and checks
UID and GID against the container user.

If ownership does not match, backup is blocked. Details are written to logs and
sent via Telegram.

This is there for the boring but important case: running this over an existing
backup tree from another container. It is better to stop early than rewrite a
large backup with unexpected ownership.

Safety-net files live in `/config`:

```text
pyiclodoc-drive-safety_net_done.flag
pyiclodoc-drive-safety_net_blocked.flag
```

## Troubleshooting

### The one-shot container keeps restarting

Set the container restart policy to `no`:

```env
ALICE_RESTART_POLICY=no
```

Then recreate the container.

### Telegram commands are ignored

Check:

- `H_TGM_CHAT_ID` is set
- the command came from that exact chat
- the username matches `CONTAINER_USERNAME`
- the command uses one of the supported forms in [TELEGRAM.md](TELEGRAM.md)

### Authentication is stuck

If the container restarted after iCloud issued a challenge, the current
challenge may be gone. Send:

```text
alice auth
```

or:

```text
alice reauth
```

The container should request a fresh challenge.

### Traversal takes a long time

Large iCloud Drives can spend a long time in traversal. Set debug logging if
you need to prove the container is still working:

```env
C_LOG_LEVEL=debug
```

Then look for traversal progress and `Traversal finished.` in the container log.

### Transfer errors are non-zero

Look for:

```text
Transfer failure reason detail:
```

That line groups the failure reasons from the run. Start there before reading
per-file lines.

### The safety net blocks backup

Read the Telegram message or container log. It includes the expected UID and
GID.

Fix the ownership mismatch, or choose a clean output directory, before trying
again.

## Runtime reference

These details are for Docker, permissions, and iCloud edge cases.

- The process inside the container runs as non-root.
- The entrypoint starts as root only to read Docker secret files, then drops to
  `PUID:PGID`.
- Services keep `cap_drop: ALL` and add only `SETUID` and `SETGID` so privilege
  drop works.
- If your Docker runtime blocks group switching with `setgroups`, startup can
  fail during privilege drop.
- Health checks run the bundled shell script through `sh` inside the container
  image.
- If heartbeat writes fail from startup and no successful heartbeat is recorded
  within the health budget, the container exits non-zero.
- Telegram notifications are skipped quietly when Telegram is not configured.
- If Telegram rejects a notification or the request fails, the container logs the
  failure detail.
- Startup discards old queued Telegram updates once, then switches to live
  polling from the captured offset.
- Each file reports its own transfer result, so retry counts and failure reasons
  stay tied to the right path.
- Transient transfer exceptions, such as throttling and 5xx responses, are
  retried with limited backoff.
- Directory traversal also uses limited retry and backoff for transient iCloud
  API failures.
- If a remote path changes between file and directory across runs, the container
  replaces the conflicting local path and continues.
- Successful downloads preserve remote modified timestamps on local files.
- Keyring bootstrap sets an explicit keyring file path and XDG data path, but
  does not rewrite `HOME`.
- Stored keyring credentials are updated only after a successful iCloud login.
- JSON state writes use a temporary file and atomic replace.
