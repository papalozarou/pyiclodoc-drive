# Configuration

Start with four things:

1. where backups are stored;
2. where config and logs are stored;
3. which iCloud account each container uses;
4. how often each container runs.

The example files use two containers, Alice and Bob. Keep both if you want to
back up two iCloud Drive accounts from the same Compose project. Remove or
ignore the second container if you only need one.

## Quick setup

1. Copy `.env.example` to `.env`.
2. Pick either `compose.yml.example` or `compose.build.yml.example`.
3. Set the host paths in `.env`.
4. Create the secret files the containers read.
5. Pick a schedule for each container.
6. Start Compose.

The examples already set most container paths. Start with the host paths.

## Choose where files go

Each container needs three host paths:

- `<SVC>_CONFIG_PATH`: saved auth details, manifest, and runtime metadata
- `<SVC>_OUTPUT_PATH`: downloaded iCloud Drive files
- `<SVC>_LOGS_PATH`: container and health check logs

For Alice, those settings are:

```env
ALICE_CONFIG_PATH=...
ALICE_OUTPUT_PATH=...
ALICE_LOGS_PATH=...
```

For Bob, use the matching `BOB_` settings.

Keep each container's paths separate. Sharing an output or config directory
between containers makes it hard to tell which backup owns which files.

## Add your secrets

Set `H_DKR_SECRETS` to the host directory that stores your secret files.

The example Compose files expect these files:

```text
telegram_bot_token.txt
alice_icloud_email.txt
alice_icloud_password.txt
bob_icloud_email.txt
bob_icloud_password.txt
```

These settings point each container at those files:

```env
ALICE_TGM_BOT_TOKEN_FILE=/run/secrets/telegram_bot_token
ALICE_ICLOUD_EMAIL_FILE=/run/secrets/alice_icloud_email
ALICE_ICLOUD_PASSWORD_FILE=/run/secrets/alice_icloud_password
```

`<SVC>_ICLOUD_PASSWORD_FILE` can contain an Apple Account password or an
app-specific password. Apple may still require MFA.

## Set Telegram access

Set `H_TGM_CHAT_ID` to the Telegram chat allowed to send commands.

If `H_TGM_CHAT_ID` is unset, Telegram command handling is disabled. The container
can still run scheduled backups, but you will not be able to send `auth`,
`reauth`, or `backup` commands through Telegram.

## Set the user and group

Set these to the host user and group that should own the backup files:

```env
H_UID=1000
H_GID=1000
```

The examples pass those through to the container as:

```env
C_UID=${H_UID}
C_GID=${H_GID}
```

The entrypoint starts as root only long enough to read Docker secrets, then
drops to `PUID:PGID` before running the app.

## Choose the image

For the published image, use:

```env
IMG_NAME=ghcr.io/papalozarou/pyiclodoc-drive
IMG_TAG=latest
```

For a local build, `ALP_VER` sets the Alpine base image version and `MCK_VER`
sets the Microcheck image version used during the Docker build.

## Set logging

Normal logging is enough for routine backups:

```env
C_LOG_LEVEL=info
```

Use debug logging when you are diagnosing traversal, transfer, auth, Telegram,
or scheduling behaviour:

```env
C_LOG_LEVEL=debug
```

Logs rotate daily and when they reach the size limit:

```env
C_LOG_ROTATE_DAILY=true
C_LOG_ROTATE_MAX_MIB=100
C_LOG_ROTATE_KEEP_DAYS=14
```

Rotated logs are compressed as
`pyiclodoc-drive-worker.*.log.gz`.

## Set scheduling

Set the schedule per container:

```env
ALICE_SCHEDULE_MODE=daily
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

For one-shot mode, also set the restart policy:

```env
ALICE_RUN_ONCE=true
ALICE_RESTART_POLICY=no
```

N.B.

One-shot mode needs `<SVC>_RESTART_POLICY=no`. If Compose restarts the
container after exit, the one-shot container will run again.

See [SCHEDULING.md](SCHEDULING.md) for the schedule recipes and validation
rules.

## Useful defaults you can leave alone

Leave these alone unless you know you need them:

- `C_DKR_SECRETS`: in-container secret root used by `_FILE` settings
- `SYNC_TRAVERSAL_WORKERS`: how many directory scans can run at once
- `SYNC_DOWNLOAD_WORKERS`: changed-file download count, or `auto`
- `SYNC_DOWNLOAD_CHUNK_MIB`: download stream chunk size
- `BACKUP_DELETE_REMOVED`: mirror-delete mode, default `false`

Only change the sync settings when a large backup is too slow or you are
diagnosing performance.

## Default container paths

The container uses these paths:

- `/config`: auth details, session files, manifest, and runtime metadata
- `/output`: downloaded iCloud Drive files
- `/logs`: container and health check output

Compose maps each host path to the matching container path.

## `/config` layout

Each container mounts `/config` from a different host location:

- `icloud_alice` uses `${ALICE_CONFIG_PATH}`
- `icloud_bob` uses `${BOB_CONFIG_PATH}`

Runtime layout:

```text
/config
├── pyiclodoc-drive-auth_state.json
├── pyiclodoc-drive-manifest.json
├── pyiclodoc-drive-safety_net_done.flag
├── pyiclodoc-drive-safety_net_blocked.flag
├── cookies/
├── session/
├── icloudpd/
│   ├── cookies -> /config/cookies
│   └── session -> /config/session
└── keyring/
    └── keyring_pass.cfg
```

N.B.

- `pyiclodoc-drive-safety_net_done.flag` is created when first-run safety
  checks pass.
- `pyiclodoc-drive-safety_net_blocked.flag` is created when first-run safety
  checks block backup.
- `icloudpd/cookies` and `icloudpd/session` are compatibility symlinks.
- `keyring/keyring_pass.cfg` stores saved credential data.
- Saved iCloud credentials are only updated after a successful login.
- JSON state files are written with a temporary file and atomic replace.

## Full variable reference

These are the environment names used by the Compose examples.

| Container variable | Accepted values | Source in `.env` |
| --- | --- | --- |
| `CONTAINER_USERNAME` | container label, for example `alice` or `bob` | fixed in Compose |
| `ICLOUD_EMAIL_FILE` | in-container file path containing the Apple ID email | `<SVC>_ICLOUD_EMAIL_FILE` |
| `ICLOUD_PASSWORD_FILE` | in-container file path containing the Apple ID password | `<SVC>_ICLOUD_PASSWORD_FILE` |
| `REAUTH_INTERVAL_DAYS` | integer day count, default `30` | `<SVC>_REAUTH_INTERVAL_DAYS` |
| `RUN_ONCE` | `true` or `false`, default `false` | `<SVC>_RUN_ONCE` |
| `SCHEDULE_MODE` | `interval`, `daily`, `weekly`, `twice_weekly`, or `monthly` | `<SVC>_SCHEDULE_MODE` |
| `SCHEDULE_BACKUP_TIME` | `HH:MM` 24-hour local time, default `02:00` | `<SVC>_SCHEDULE_BACKUP_TIME` |
| `SCHEDULE_WEEKDAYS` | weekday name or comma-separated weekday names | `<SVC>_SCHEDULE_WEEKDAYS` |
| `SCHEDULE_MONTHLY_WEEK` | `first`, `second`, `third`, `fourth`, or `last` | `<SVC>_SCHEDULE_MONTHLY_WEEK` |
| `SCHEDULE_INTERVAL_MINUTES` | integer minute count, default `1440` | `<SVC>_SCHEDULE_INTERVAL_MINUTES` |
| `SYNC_TRAVERSAL_WORKERS` | integer `1` to `8`, default `1` | `<SVC>_SYNC_TRAVERSAL_WORKERS` |
| `SYNC_DOWNLOAD_WORKERS` | `auto` or integer `1` to `16`, default `auto` | `<SVC>_SYNC_DOWNLOAD_WORKERS` |
| `SYNC_DOWNLOAD_CHUNK_MIB` | integer `1` to `16`, default `4` | `<SVC>_SYNC_DOWNLOAD_CHUNK_MIB` |
| `BACKUP_DELETE_REMOVED` | `true` or `false`, default `false` | `<SVC>_BACKUP_DELETE_REMOVED` |
| `TELEGRAM_CHAT_ID` | Telegram chat ID integer string | `H_TGM_CHAT_ID` |
| `TELEGRAM_BOT_TOKEN_FILE` | in-container file path containing the bot token | `<SVC>_TGM_BOT_TOKEN_FILE` |

Common host values:

- `H_UID`: host user ID mapped into containers
- `H_GID`: host group ID mapped into containers
- `H_TZ`: timezone for container behaviour and schedule calculations
- `H_TGM_CHAT_ID`: Telegram chat allowed to control containers
- `H_DKR_SECRETS`: host path containing source secret files
- `H_DATA_PATH`: base host path for service data directories
