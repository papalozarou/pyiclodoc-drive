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

## Shared container variables (`C_`)

These are usually left as-is unless you have a specific reason to change them.

- `C_UID`: container user ID (normally mirrors `H_UID`).
- `C_GID`: container group ID (normally mirrors `H_GID`).
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

- `<SVC>_SCHEDULE_MODE`: `interval`, `daily_time`, `weekly`, `twice_weekly`,
  or `monthly` (default `interval`).
- `<SVC>_BACKUP_INTERVAL_MINUTES`: interval run spacing in minutes (default
  `1440`).
- `<SVC>_BACKUP_DAILY_TIME`: local run time in `HH:MM` 24-hour format
  (default `02:00`).
- `<SVC>_SCHEDULE_WEEKDAY`: single weekday name (`monday`..`sunday`).
- `<SVC>_SCHEDULE_WEEKDAYS`: two comma-separated distinct weekday names,
  for example `monday,thursday`.
- `<SVC>_SCHEDULE_MONTHLY_WEEK`: one of `first`, `second`, `third`, `fourth`,
  `last`.
- `<SVC>_RUN_ONCE`: run one backup pass and exit (`true`/`false`, default
  `false`).
- `<SVC>_STARTUP_DELAY_SECONDS`: startup delay to spread API load (defaults:
  Alice `15`, Bob `60`).
- `<SVC>_REAUTH_INTERVAL_DAYS`: reauthentication window (default `30`).

N.B.

For scheduling compatibility and mode-specific behaviour, see
[SCHEDULING.md](SCHEDULING.md).

## Build variable

- `ALP_VER`: Alpine base image version used during Docker build.

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
