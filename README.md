# iCloud Drive Backup Container

This project provides an Alpine-based Docker container that performs incremental iCloud Drive backups with Telegram-driven control and authentication prompts.

The example Compose setup runs two isolated worker services, one for Alice and one for Bob, with separate state, output, and log paths.

## Features

- Multi-stage image build with a reduced runtime footprint.
- Required `microcheck`-backed health checks with heartbeat age validation.
- Runtime user and group mapping via container `PUID` and `PGID`.
- Incremental sync model backed by `/config/manifest.json`.
- Automatic host-based transfer concurrency tuning for changed-file downloads.
- Session persistence in `/config/session` and cookies in `/config/cookies`.
- Compatibility symlinks in `/config/icloudpd/{cookies,session}`.
- First-run safety net to detect risky local permission mismatches.
- Telegram command handling for backup and authentication workflows.

## Telegram commands

Send commands from the chat configured by `H_TGM_CHAT_ID`.

- `<username> backup`
- `<username> auth`
- `<username> auth 123456`
- `<username> reauth`
- `<username> reauth 123456`

`<username>` must match `CONTAINER_USERNAME`.

## Configuration model

The Compose example uses:

- Host-scoped variables prefixed with `H_`.
- Shared container variables prefixed with `C_`.
- Service-specific variables prefixed with `ALICE_` and `BOB_`.

### Host-scoped variables (`H_`)

- `H_UID`, host user ID mapped into containers.
- `H_GID`, host group ID mapped into containers.
- `H_TZ`, timezone used by worker time calculations.
- `H_TGM_CHAT_ID`, Telegram chat ID accepted by command parser.
- `H_DKR_SECRETS`, host path used for Docker secret source files.
- `H_DATA_PATH`, host base path used for worker data directories.

### Shared container variables (`C_`)

- `C_UID`, container user ID mapped from host.
- `C_GID`, container group ID mapped from host.
- `C_DKR_SECRETS`, in-container secret path root for `_FILE` variables.

### Service-scoped variables (`ALICE_*`, `BOB_*`)

- `<SVC>_CONFIG_PATH`, host path mounted to `/config`.
- `<SVC>_OUTPUT_PATH`, host path mounted to `/output`.
- `<SVC>_LOGS_PATH`, host path mounted to `/logs`.
- `<SVC>_BACKUP_INTERVAL_MINUTES`, scheduled backup interval in minutes
  (default `1440`).
- `<SVC>_RUN_ONCE`, run one backup pass and exit when set to `true`
  (default `false`).
- `<SVC>_STARTUP_DELAY_SECONDS`, startup delay to spread API load
  (defaults: Alice `15`, Bob `60`).
- `<SVC>_REAUTH_INTERVAL_DAYS`, reauthentication window length
  (default `30`).
- `<SVC>_TGM_BOT_TOKEN_FILE`, bot token secret path.
- `<SVC>_ICLOUD_EMAIL_FILE`, iCloud email secret path.
- `<SVC>_ICLOUD_PASSWORD_FILE`, iCloud password secret path.
  This value can point to either an Apple Account password or an
  app-specific password.

### Build variables

- `ALP_VER`, Alpine base image version passed as a Compose build argument.

### Worker path defaults

- `/config` for auth state, manifest, session, and cookie data.
- `/output` for downloaded iCloud Drive files.
- `/logs` for worker logs and health heartbeat files.

### Config directory structure

Each worker mounts `/config` from its own host path:

- `icloud_alice` maps `/config` to `${ALICE_CONFIG_PATH}`.
- `icloud_bob` maps `/config` to `${BOB_CONFIG_PATH}`.

Runtime layout inside each worker `/config` directory:

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

Notes:

- `safety_net_done.flag` is created when the first-run safety check passes.
- `safety_net_blocked.flag` is created when the first-run safety check blocks.
- `icloudpd/cookies` and `icloudpd/session` are compatibility symlinks.

## Run with Docker Compose

1. Copy `compose.yml.example` to `compose.yml` for local use.
2. Copy `.env.example` to `.env` and set host/service values.
3. Set host data mount paths in `.env` using `H_DATA_PATH` and the
   per-service path variables (`ALICE_*_PATH`, `BOB_*_PATH`).
4. Create secret files under `${H_DKR_SECRETS}` (default
   `/var/lib/docker/secrets`):
   `telegram_bot_token.txt`, `alice_icloud_email.txt`,
   `alice_icloud_password.txt`, `bob_icloud_email.txt`,
   `bob_icloud_password.txt`.
5. Build and run:

```bash
docker compose up -d --build
```

6. Check container and health status:

```bash
docker compose ps
docker inspect --format='{{json .State.Health}}' icloud_alice
docker inspect --format='{{json .State.Health}}' icloud_bob
```

## Runtime notes

- Compose `init: true` is required by the provided service definitions.
- Health checks require `microcheck`, bundled into the image build.
- Telegram commands are ignored unless they come from `H_TGM_CHAT_ID`.
- One-shot mode is enabled per service with `<SVC>_RUN_ONCE=true`.
- For one-shot runs, use `restart: "no"` to avoid automatic restarts.
- In one-shot mode, the container exits after a single backup attempt.
- One-shot exits non-zero if authentication is incomplete, reauthentication is
  pending, or the first-run safety net blocks backup.
- Changed-file downloads run in parallel using an automatic worker count
  derived from host CPU capacity and bounded internally to `1..8`.
- No extra tuning variables are required for transfer concurrency.

## First-run authentication behaviour

On startup, each worker attempts iCloud authentication immediately using the
configured credentials and persisted session/cookie state under `/config`.

If authentication requires MFA, the worker sends a Telegram prompt and marks
authentication as pending. Backups are skipped while authentication is
incomplete.

The password secret file passed by `<SVC>_ICLOUD_PASSWORD_FILE` can contain an
Apple Account password or an app-specific password. The value is passed
directly to `pyicloud`, and MFA handling continues to follow Apple account
requirements and policy.

To complete MFA, send one of:

- `<username> auth 123456`
- `<username> reauth 123456`

When authentication succeeds, the worker clears pending auth state and resumes
scheduled or manually requested backups.

## Safety net behaviour

On first run only, each worker samples existing files in `/output` and checks
whether permissions are consistent.

If mismatches are found, backup is blocked. Details are written to worker logs
and sent via Telegram. This prevents destructive rewrites over existing backup
trees created with different ownership or mode patterns.
