# Operations

## Runtime notes

- Compose `init: true` is required by the provided service definitions.
- Health checks use `microcheck` (bundled in the image).
- Telegram commands are ignored unless they come from `H_TGM_CHAT_ID`.

## Scheduling

For full scheduling behaviour, option compatibility, manual command effects, and
validation rules, see [SCHEDULING.md](SCHEDULING.md).

## One-shot mode

- Enable with `<SVC>_RUN_ONCE=true`.
- Recommended with `restart: "no"` to avoid automatic restarts.
- Worker exits after one backup attempt.
- Exit is non-zero when auth is incomplete, reauth is pending, or first-run
  safety net blocks backup.

## Transfer performance

- Incremental sync uses `manifest.json` and skips unchanged files.
- Changed-file downloads run in parallel automatically based on host CPU.
- Worker count is internally bounded to `1..8`.
- No extra tuning variables are required.

## Safety-net behaviour

On first run only, each worker samples existing files in `/output` and checks
permissions for consistency.

If mismatches are found, backup is blocked. Details are written to worker logs
and sent via Telegram. This is intended to avoid destructive rewrites over
existing backup trees with mixed ownership/modes.
