# Telegram

Use Telegram to send commands to each container and receive backup status
messages. It lets you complete iCloud authentication or start a backup without
opening Docker.

Commands only work from the chat ID set in `H_TGM_CHAT_ID`. If `H_TGM_CHAT_ID`
is unset, Telegram command handling is disabled.

## Commands

Send commands in this form:

```text
alice backup
alice auth
alice auth 123456
alice reauth
alice reauth 123456
```

Replace `alice` with the container username set by `CONTAINER_USERNAME`.

The supported commands are:

- `<username> backup`: run a backup now
- `<username> auth`: start or restart the current authentication prompt
- `<username> auth 123456`: submit an MFA code for authentication
- `<username> reauth`: start or restart the current reauthentication prompt
- `<username> reauth 123456`: submit an MFA code for reauthentication

## First-time authentication

On startup, the container tries to reuse its saved iCloud session and the
credentials from your secret files.

If iCloud asks for MFA, the container sends an authentication prompt to Telegram.
Reply with:

```text
alice auth 123456
```

`auth 123456` does not start a fresh login. It answers the active challenge
that iCloud already issued.

If the container restarts and loses the current challenge, send this first:

```text
alice auth
```

That tells the container to start a new challenge and send a fresh prompt.

## Reauthentication

Reauthentication uses the same command shape:

```text
alice reauth
alice reauth 123456
```

Use `reauth` when the container says reauthentication is required. Use
`reauth 123456` to submit the code from Apple.

## Manual backups

Send:

```text
alice backup
```

The container starts a backup as soon as it can. If a backup is already running,
Telegram reports that instead.

## Messages you will see

Messages use compact plain text:

- an emoji header in sentence case
- one-line action summary including the Apple ID
- short status lines where they help

Common headers include:

- `🟢 PCD Drive - Container started`
- `🛑 PCD Drive - Container stopped`
- `🔑 PCD Drive - Authentication required`
- `🔑 PCD Drive - Reauthentication required`
- `🔒 PCD Drive - Authentication complete`
- `❌ PCD Drive - Authentication failed`
- `📥 PCD Drive - Backup requested`
- `⬇️ PCD Drive - Backup started`
- `📦 PCD Drive - Backup complete`
- `⏭️ PCD Drive - Backup skipped`
- `⚠️ PCD Drive - Safety net blocked`
- `📣 PCD Drive - Reauth reminder`

Backup completion messages can include:

- `Transferred: <done>/<total>`
- `Skipped: <count>`
- `Errors: <count>`
- `Delete errors: <count>`
- `Duration: <hh:mm:ss>`
- `Average speed: <value> MiB/s`

`Errors` includes transfer errors and delete-phase errors. `Average speed`
appears only when files were downloaded.

Backup start messages include either:

- `Scheduled <plain English schedule>`
- `Manual, then <plain English schedule>`

Safety-net blocked messages include the UID and GID the container expected:

```text
Expected uid <uid>, gid <gid>
```

## When commands are ignored

Commands are ignored when:

- they come from a different Telegram chat
- `H_TGM_CHAT_ID` is unset
- the username does not match that container
- the command is not one of the supported forms above

On startup, the container discards old queued Telegram updates once, then
switches to live polling. That stops old commands from firing after a restart
while keeping commands that arrive after the container has started.

## Password files

`<SVC>_ICLOUD_PASSWORD_FILE` can contain either:

- an Apple Account password
- an app-specific password

The container passes the value to `pyicloud`. Apple still decides whether MFA is
needed.
