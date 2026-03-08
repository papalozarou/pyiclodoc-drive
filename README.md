# iCloud Drive Backup Container

This project provides a minimal Alpine-based Docker container that runs as a non-root process and performs incremental iCloud Drive backups with Telegram-driven control and authentication prompts.

## Features

- Alpine Linux image with a reduced runtime footprint.
- Runtime user and group mapping via `PUID` and `PGID` (default `1000:1000`).
- Container username support via `CONTAINER_USERNAME` for Telegram commands.
- Docker secrets support for iCloud credentials and Telegram bot token.
- Incremental backup driven by a manifest at `/config/manifest.json`.
- One-off backup trigger with `<username> backup`.
- MFA auth and reauth flows via `<username> auth` and `<username> reauth`.
- Reauth reminders at 5 days and 2 days before the configured auth window ends.
- Session persistence in `/config/session` and cookie persistence in `/config/cookies`.
- Compatibility symlinks in `/config/icloudpd/{cookies,session}` for interoperability.
- First-run safety net that samples local file permissions and blocks risky overwrite attempts.
- Healthcheck based on heartbeat freshness, with optional `microcheck` support.

## Telegram commands

Send commands from the configured `TELEGRAM_CHAT_ID`:

- `<username> backup`
- `<username> auth`
- `<username> auth 123456`
- `<username> reauth`
- `<username> reauth 123456`

`<username>` must match `CONTAINER_USERNAME`.

## Runtime configuration

Core environment variables:

- `PUID`, default `1000`
- `PGID`, default `1000`
- `CONTAINER_USERNAME`, default `icloudbot`
- `BACKUP_INTERVAL_MINUTES`, default `720`
- `STARTUP_DELAY_SECONDS`, default `0`
- `REAUTH_INTERVAL_DAYS`, default `30`
- `TZ`, default `UTC` in the compose example (used for app timestamps and reauth timing calculations)
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_BOT_TOKEN` or `TELEGRAM_BOT_TOKEN_FILE`
- `ICLOUD_EMAIL` or `ICLOUD_EMAIL_FILE`
- `ICLOUD_PASSWORD` or `ICLOUD_PASSWORD_FILE`
- `KEYCHAIN_SERVICE_NAME`, default `icloud-drive-backup`

Path overrides (optional):

- `CONFIG_DIR`, default `/config`
- `OUTPUT_DIR`, default `/output`
- `LOGS_DIR`, default `/logs`
- `COOKIE_DIR`, default `/config/cookies`
- `SESSION_DIR`, default `/config/session`
- `ICLOUDPD_COMPAT_DIR`, default `/config/icloudpd`

## Run with Docker Compose

1. Create secret files in `./secrets/`.
2. Set `TELEGRAM_CHAT_ID` in your shell or `.env`.
3. Build and run:

```bash
docker compose up -d --build
```

## Safety net behaviour

On first run only, the worker samples existing files in `/output` and checks whether permissions are consistent.

If mismatches are found, the backup is blocked and details are written to logs and sent via Telegram. This prevents accidental destructive rewrites over pre-existing backups created with different ownership/mode patterns.
