# Scheduling

This project supports both interval and calendar-based schedules, plus a
one-shot mode.

## Mode overview

### `RUN_ONCE=true`

- Runs one backup attempt and exits.
- Recurring scheduling values are effectively ignored for execution.

### `SCHEDULE_MODE=interval`

- Runs every `<SVC>_BACKUP_INTERVAL_MINUTES`.

### `SCHEDULE_MODE=daily_time`

- Runs once per day at `<SVC>_BACKUP_DAILY_TIME` local time.

### `SCHEDULE_MODE=weekly`

- Runs on the single day set in `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_BACKUP_DAILY_TIME`.

### `SCHEDULE_MODE=twice_weekly`

- Runs on both days in `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_BACKUP_DAILY_TIME`.

### `SCHEDULE_MODE=monthly`

- Runs on `<SVC>_SCHEDULE_MONTHLY_WEEK` day from
  `<SVC>_SCHEDULE_WEEKDAYS` at
  `<SVC>_BACKUP_DAILY_TIME`.
- Example: `first monday` at `02:00`.

## Which options work together

- `RUN_ONCE=true`
  - Works with any `SCHEDULE_MODE` value.
  - Recurring schedule settings are not used for repeated runs.

- `SCHEDULE_MODE=interval`
  - Uses: `BACKUP_INTERVAL_MINUTES`.
  - Ignores: `BACKUP_DAILY_TIME`, `SCHEDULE_WEEKDAYS`,
    `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=daily_time`
  - Uses: `BACKUP_DAILY_TIME`.
  - Ignores: `BACKUP_INTERVAL_MINUTES`, `SCHEDULE_WEEKDAYS`,
    `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=weekly`
  - Uses: `SCHEDULE_WEEKDAYS` (exactly one day), `BACKUP_DAILY_TIME`.
  - Ignores: `BACKUP_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=twice_weekly`
  - Uses: `SCHEDULE_WEEKDAYS` (exactly two distinct days),
    `BACKUP_DAILY_TIME`.
  - Ignores: `BACKUP_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK`.

- `SCHEDULE_MODE=monthly`
  - Uses: `SCHEDULE_MONTHLY_WEEK`, `SCHEDULE_WEEKDAYS` (exactly one day),
    `BACKUP_DAILY_TIME`.
  - Ignores: `BACKUP_INTERVAL_MINUTES`.

## Validation rules

Startup validation fails when:

- `SCHEDULE_MODE` is invalid.
- `BACKUP_DAILY_TIME` is not valid `HH:MM` for calendar modes.
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `weekly`.
- `SCHEDULE_WEEKDAYS` is not exactly two distinct weekdays for
  `twice_weekly`.
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `monthly`.
- `SCHEDULE_MONTHLY_WEEK` is not one of `first`, `second`, `third`, `fourth`,
  `last` for `monthly`.
- `BACKUP_INTERVAL_MINUTES < 1` in `interval` mode when not running one-shot.

## Manual backup command behaviour

If a user sends `<username> backup`, backup runs immediately.

After that manual run:

- in `interval` mode, next run is recalculated from command run time;
- in calendar-based modes, next run remains pinned to the next valid calendar
  slot.
