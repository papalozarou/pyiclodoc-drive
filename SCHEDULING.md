# Scheduling

This project supports both interval and calendar-based schedules, plus a
one-shot mode.

## Mode overview

### `RUN_ONCE=true`

- Waits for authentication completion when MFA or reauth is pending, runs one
  backup attempt, then exits.
- Recurring scheduling values are effectively ignored for execution.

### `SCHEDULE_MODE=interval`

- Runs every `<SVC>_SCHEDULE_INTERVAL_MINUTES`.

### `SCHEDULE_MODE=daily`

- Runs once per day at `<SVC>_SCHEDULE_BACKUP_TIME` local time.

### `SCHEDULE_MODE=weekly`

- Runs on the single day set in `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_SCHEDULE_BACKUP_TIME`.

### `SCHEDULE_MODE=twice_weekly`

- Runs on both days in `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_SCHEDULE_BACKUP_TIME`.

### `SCHEDULE_MODE=monthly`

- Runs on `<SVC>_SCHEDULE_MONTHLY_WEEK` day from
  `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_SCHEDULE_BACKUP_TIME`.
- Example: `first monday` at `02:00`.

## Example values

Use lowercase weekday names: `monday` to `sunday`.

### Interval

```env
ALICE_SCHEDULE_MODE=interval
ALICE_SCHEDULE_INTERVAL_MINUTES=1440
```

### Daily

```env
ALICE_SCHEDULE_MODE=daily
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

### Weekly

```env
ALICE_SCHEDULE_MODE=weekly
ALICE_SCHEDULE_WEEKDAYS=monday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

### Twice-weekly

```env
ALICE_SCHEDULE_MODE=twice_weekly
ALICE_SCHEDULE_WEEKDAYS=monday,thursday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

N.B.

- Use exactly two days, comma-separated.
- Do not repeat the same day twice.

### Monthly

```env
ALICE_SCHEDULE_MODE=monthly
ALICE_SCHEDULE_MONTHLY_WEEK=first
ALICE_SCHEDULE_WEEKDAYS=monday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

## Which options work together

- `RUN_ONCE=true`
  - Works with any `SCHEDULE_MODE` value.
  - Recurring schedule settings are not used for repeated runs.
  - Set `<SVC>_RESTART_POLICY=no` so Compose does not restart the one-shot
    container after completion.

- `SCHEDULE_MODE=interval`
  - Uses: `SCHEDULE_INTERVAL_MINUTES`.
  - Ignores: `SCHEDULE_BACKUP_TIME`, `SCHEDULE_WEEKDAYS`,
    `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=daily`
  - Uses: `SCHEDULE_BACKUP_TIME`.
  - Ignores: `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_WEEKDAYS`,
    `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=weekly`
  - Uses: `SCHEDULE_WEEKDAYS` (exactly one day), `SCHEDULE_BACKUP_TIME`.
  - Ignores: `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=twice_weekly`
  - Uses: `SCHEDULE_WEEKDAYS` (exactly two distinct days),
    `SCHEDULE_BACKUP_TIME`.
  - Ignores: `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=monthly`
  - Uses: `SCHEDULE_MONTHLY_WEEK`, `SCHEDULE_WEEKDAYS` (exactly one day),
    `SCHEDULE_BACKUP_TIME`.
  - Ignores: `SCHEDULE_INTERVAL_MINUTES`.

## Validation rules

Startup validation fails when:

- `SCHEDULE_MODE` is invalid.
- `SCHEDULE_BACKUP_TIME` is not valid `HH:MM` for calendar modes.
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `weekly`.
- `SCHEDULE_WEEKDAYS` is not exactly two distinct weekdays for
  `twice_weekly`.
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `monthly`.
- `SCHEDULE_MONTHLY_WEEK` is not one of `first`, `second`, `third`, `fourth`,
  `last` for `monthly`.
- `SCHEDULE_INTERVAL_MINUTES < 1` in `interval` mode when not running one-shot.

## Manual backup command behaviour

If a user sends `<username> backup`, backup runs immediately.

After that manual run:

- in `interval` mode, next run is recalculated from command run time;
- in calendar-based modes, next run remains pinned to the next valid calendar
  slot.
