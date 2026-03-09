# iCloud Drive Backup Container

This project is a Docker setup for incremental iCloud Drive backups with
Telegram control and MFA prompts.

It runs two isolated worker containers out of the box (Alice and Bob), each
with separate config, output, and logs.

## Quick start

1. Copy `compose.yml.example` to `compose.yml`.
2. Copy `.env.example` to `.env`.
3. Set host and service values in `.env`.
4. Create secret files under `${H_DKR_SECRETS}` (default
   `/var/lib/docker/secrets`):
   `telegram_bot_token.txt`, `alice_icloud_email.txt`,
   `alice_icloud_password.txt`, `bob_icloud_email.txt`,
   `bob_icloud_password.txt`.
5. Start containers:

```bash
docker compose up -d --build
```

6. Check status:

```bash
docker compose ps
docker inspect --format='{{json .State.Health}}' icloud_alice
docker inspect --format='{{json .State.Health}}' icloud_bob
```

## Read this next

- [CONFIGURATION.md](CONFIGURATION.md): env variables, paths, and config layout.
- [SCHEDULING.md](SCHEDULING.md): schedule modes, compatibility, and manual
  backup behaviour.
- [TELEGRAM.md](TELEGRAM.md): command format, auth flow, and message outputs.
- [OPERATIONS.md](OPERATIONS.md): runtime behaviour, one-shot mode,
  performance, and safety-net behaviour.
