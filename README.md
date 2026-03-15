# PyiCloDoc Drive

A dockerised `pyicloud` implementation for backing up iCloud drives to local storage, with Telegram used for auth prompts and operational control.

It should have all the bits you need for real-world usage, such as:

* persistent auth/session state;
* manifest-driven incremental sync;
* one-shot and scheduled modes;
* performance tuning options;
* comprehensive logging;
* protection of existing backups via a first-run safety net; and
* backup of more than one iCloud drive using all of the above.

It is intended to be set and forget – start it, authorise when needed, and let it do the rest.

*N.B.*

You have probably already guessed from the `PROMPT.md` file that this was AI built.

It started as an experiment to see whether Claude Code or Codex produced better code using the default models, which I believe were Sonnet 4.6 and GPT-5.3-Codex. I asked Claude and Codex to rate the outputs and they both said Codex's was significantly better. The project grew from there because I was not satisfied with roughly 11-hour backups using other images.

## Example usage

The example `compose.yml` and `.env` files run two isolated containers out of the box, Alice and Bob, each with separate config, output, and logs. These examples give you a flavour of what PyiCloDoc Drive can do, and enough information to configure it to your needs. Complete documentation is linked at the end of this README.

### Quick start

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
docker inspect --format='{{json .State.Health}}' icloud_drive_alice
docker inspect --format='{{json .State.Health}}' icloud_drive_bob
```

## Testing

If you're bored, you can run the unit test suite:

```bash
python3 -m unittest -q
```

If you're even more bored, you can run tests with coverage:

```bash
python3 -m coverage run -m unittest -q
python3 -m coverage report -m
```

## Detailed documentation

- [CONFIGURATION.md](CONFIGURATION.md): env variables, paths, and config layout.
- [SCHEDULING.md](SCHEDULING.md): schedule modes, compatibility, and manual
  backup behaviour.
- [TELEGRAM.md](TELEGRAM.md): command format, auth flow, and message outputs.
- [OPERATIONS.md](OPERATIONS.md): runtime behaviour, one-shot mode,
  performance, and safety-net behaviour (one-shot requires
  `<SVC>_RESTART_POLICY=no`).

## License

This project is provided under the GNU General Public License v3.0.

You can use, modify, and redistribute this project, but any redistributed modified version must also remain under GPL-3.0 and include the source code.
