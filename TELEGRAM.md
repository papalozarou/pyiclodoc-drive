# Telegram

## Command format

Commands are only accepted from the chat ID configured in `H_TGM_CHAT_ID`.

Supported command forms:

- `<username> backup`
- `<username> auth`
- `<username> auth 123456`
- `<username> reauth`
- `<username> reauth 123456`

N.B.

`<username>` must match the container username for that worker service.

## Authentication and reauthentication flow

1. On startup, the worker attempts iCloud authentication using saved session
   state and configured credentials.
2. If MFA is required, the worker marks auth pending and sends a prompt.
3. The user sends either `auth <code>` or `reauth <code>` via Telegram to
   complete the current pending challenge.
4. `auth <code>` and `reauth <code>` do not start a fresh login attempt; they
   only validate against the active pending session.
5. If a worker restart clears in-memory auth session state, send `auth` or
   `reauth` without a code first to trigger a new challenge prompt.
6. If successful, pending auth state is cleared and normal backup flow resumes.

## Password file behaviour

`<SVC>_ICLOUD_PASSWORD_FILE` can hold either:

- an Apple Account password; or
- an app-specific password.

The value is passed directly to `pyicloud`, and final auth/MFA handling still
follows Apple account policy.

## Outbound Telegram messages

Messages use this compact structure:

- Bold emoji header in sentence case.
- One-line action summary including Apple ID.
- Optional compact status lines.

Current message templates include:

- `*🟢 iCloudDD - Container started*`
- `*🛑 iCloudDD - Container stopped*`
- `*🔑 iCloudDD - Authentication required*`
- `*🔑 iCloudDD - Reauthentication required*`
- `*🔒 iCloudDD - Authentication complete*`
- `*❌ iCloudDD - Authentication failed*`
- `*📥 iCloudDD - Backup requested*`
- `*⬇️ iCloudDD - Backup started*`
- `*📦 iCloudDD - Backup complete*`
- `*⏭️ iCloudDD - Backup skipped*`
- `*⚠️ iCloudDD - Safety net blocked*`
- `*📣 iCloudDD - Reauth reminder*`

Backup completion messages include:

- `Transferred: <done>/<total>`
- `Skipped: <count>`
- `Errors: <count>`
- `Duration: <hh:mm:ss>`
- `Average speed: <value> MiB/s` (only when files were downloaded)

Backup start messages include:

- `Schedule: <plain English schedule>`
- `Schedule: Manual, then <plain English schedule>`

Safety-net blocked messages include an explicit expected ownership line:

- `Expected: uid <uid>, gid <gid>`
