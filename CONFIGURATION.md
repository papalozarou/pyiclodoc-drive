# Configuration

This is the practical map of what needs setting in `.env`, what it does, and
which values you can mostly leave alone.

## How the variables are grouped

- `H_` values describe the host.
- `C_` values describe shared in-container paths and IDs.
- `ALICE_` and `BOB_` values are per-service settings.

## Host variables (`H_`)

Start here first.

- `H_UID`: host user ID mapped into containers.
- `H_GID`: host group ID mapped into containers.
- `H_TZ`: timezone for worker behaviour and schedule calculations.
- `H_TGM_CHAT_ID`: only this Telegram chat is accepted for commands.
- `H_DKR_SECRETS`: host path containing source secret files.
- `H_DATA_PATH`: base host path for service data directories.

## Runtime identity mapping

These are usually left as-is unless you need explicit UID or GID mapping.

- `C_UID`: source UID value in `.env` (normally mirrors `H_UID`).
- `C_GID`: source GID value in `.env` (normally mirrors `H_GID`).
- Compose maps `PUID=${C_UID}` and `PGID=${C_GID}` into each service.
- Entrypoint drops from root to `PUID:PGID` before starting the worker.

## Shared container variables (`C_`)

These are usually left as-is unless you have a specific reason to change them.

- `C_DKR_SECRETS`: in-container secret root used by `_FILE` env vars.

## Service variables (`ALICE_*`, `BOB_*`)

### Paths and secrets

- `<SVC>_CONFIG_PATH`: host path mounted to `/config`.
- `<SVC>_OUTPUT_PATH`: host path mounted to `/output`.
- `<SVC>_LOGS_PATH`: host path mounted to `/logs`.
- `<SVC>_TGM_BOT_TOKEN_FILE`: Telegram bot token file path.
- `<SVC>_ICLOUD_EMAIL_FILE`: iCloud email file path.
- `<SVC>_ICLOUD_PASSWORD_FILE`: iCloud password file path.

### Scheduling and runtime behaviour

- `<SVC>_SCHEDULE_MODE`: `interval`, `daily`, `weekly`, `twice_weekly`,
  or `monthly` (default `interval`).
- `<SVC>_SCHEDULE_INTERVAL_MINUTES`: interval run spacing in minutes (default
  `1440`).
- `<SVC>_TRAVERSAL_WORKERS`: directory traversal worker count, as an integer
  from `1` to `8` (default `1`, serial traversal).
- `<SVC>_SYNC_WORKERS`: transfer worker override, using `auto` or an integer
  from `1` to `16` (default `auto`).
- `<SVC>_DOWNLOAD_CHUNK_MIB`: streamed download chunk size in MiB, as an
  integer from `1` to `16` (default `4`).
- `<SVC>_SCHEDULE_BACKUP_TIME`: local run time in `HH:MM` 24-hour format
  (default `02:00`).
- `<SVC>_SCHEDULE_WEEKDAYS`: comma-separated weekday names.
  Use one day for `weekly` mode, or two distinct days for `twice_weekly`
  mode, and one day for `monthly` mode, for example `monday` or
  `monday,thursday`.
- `<SVC>_SCHEDULE_MONTHLY_WEEK`: one of `first`, `second`, `third`, `fourth`,
  `last`.
- `<SVC>_RUN_ONCE`: run one backup pass and exit (`true`/`false`, default
  `false`).
- `<SVC>_RESTART_POLICY`: Compose restart policy for the service, for example
  `unless-stopped` or `no`.
- `<SVC>_REAUTH_INTERVAL_DAYS`: reauthentication window (default `30`).

## Logging

- `LOG_LEVEL`: global worker log verbosity from Compose `default-env`.
  Supported values are `info` and `debug`; default is `info`.
  `debug` includes per-item sync traces such as directories ensured,
  files queued/transferred, unchanged skips, and transfer failures.

N.B.

For scheduling compatibility and mode-specific behaviour, see
[SCHEDULING.md](SCHEDULING.md).

## Build variable

- `IMG_NAME`: image repository/name used for service image tags, with
  `:alpine-${ALP_VER}` appended in Compose.
- `ALP_VER`: Alpine base image version used during Docker build.
- `MCK_VER`: Microcheck image version used during Docker build.

## Default container paths

- `/config`: auth/session state, manifest, and runtime metadata.
- `/output`: downloaded iCloud Drive files.
- `/logs`: worker and healthcheck output.

## `/config` layout

Each worker mounts `/config` from a different host location:

- `icloud_alice` -> `${ALICE_CONFIG_PATH}`
- `icloud_bob` -> `${BOB_CONFIG_PATH}`

Runtime layout:

```text
/config
├── auth_state.json
├── manifest.json
├── safety_net_done.flag
├── safety_net_blocked.flag
├── cookies/
├── session/
├── icloudpd/
│   ├── cookies -> /config/cookies
│   └── session -> /config/session
└── keyring/
    └── keyring_pass.cfg
```

N.B.

- `safety_net_done.flag` is created when first-run safety checks pass.
- `safety_net_blocked.flag` is created when first-run safety checks block
  backup.
- `icloudpd/cookies` and `icloudpd/session` are compatibility symlinks.
