# Scheduling

Pick one schedule per container. These examples use `ALICE_`, but the same
settings work for `BOB_` or any other container you add to Compose.

Use lowercase weekday names: `monday` to `sunday`.

N.B.

Set `H_TZ` in `.env` so Compose passes the correct `TZ` into the container.
Calendar schedules use the container timezone. If the timezone is wrong or
missing, `02:00` will not necessarily mean 02:00 where the backup host lives.

## Run once and stop

Use one-shot mode when you want the container to start, authenticate if needed,
run one backup, and exit.

```env
ALICE_RUN_ONCE=true
ALICE_RESTART_POLICY=no
```

N.B.

Set `ALICE_RESTART_POLICY=no`. If the restart policy is `unless-stopped` or
similar, Compose will start the container again after it exits. That turns a
one-shot backup into a loop.

One-shot mode does not use recurring schedule values for repeat runs. It still
waits for Telegram `auth` or `reauth` commands if iCloud needs MFA.

## Run every day

Use daily mode for the usual "run overnight" setup.

```env
ALICE_SCHEDULE_MODE=daily
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

`ALICE_SCHEDULE_BACKUP_TIME` uses `HH:MM` in the container timezone.

## Run once a week

Use weekly mode when one backup per week is enough.

```env
ALICE_SCHEDULE_MODE=weekly
ALICE_SCHEDULE_WEEKDAYS=monday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

Weekly mode needs exactly one weekday.

## Run twice a week

Use twice-weekly mode when daily is too much, but weekly is too sparse.

```env
ALICE_SCHEDULE_MODE=twice_weekly
ALICE_SCHEDULE_WEEKDAYS=monday,thursday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

Use exactly two different days, separated by a comma. Do not add spaces.

## Run once a month

Use monthly mode for a calendar slot such as "first Monday".

```env
ALICE_SCHEDULE_MODE=monthly
ALICE_SCHEDULE_MONTHLY_WEEK=first
ALICE_SCHEDULE_WEEKDAYS=monday
ALICE_SCHEDULE_BACKUP_TIME=02:00
```

`ALICE_SCHEDULE_MONTHLY_WEEK` accepts `first`, `second`, `third`, `fourth`, or
`last`.

## Run every N minutes

Use interval mode when the next run should be based on the previous run time,
not a fixed clock slot.

```env
ALICE_SCHEDULE_MODE=interval
ALICE_SCHEDULE_INTERVAL_MINUTES=1440
```

`1440` minutes is one day.

## Trigger a manual backup

Send the container a Telegram backup command:

```text
alice backup
```

After a manual run:

- interval mode recalculates the next run from the manual run time
- daily, weekly, twice-weekly, and monthly modes stay pinned to the next valid
  calendar slot

## Rules the app enforces

Startup validation fails when:

- `SCHEDULE_MODE` is not `interval`, `daily`, `weekly`, `twice_weekly`, or
  `monthly`
- `SCHEDULE_BACKUP_TIME` is not valid `HH:MM` for calendar modes
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `weekly`
- `SCHEDULE_WEEKDAYS` is not exactly two distinct weekdays for `twice_weekly`
- `SCHEDULE_WEEKDAYS` is not exactly one valid weekday for `monthly`
- `SCHEDULE_MONTHLY_WEEK` is not `first`, `second`, `third`, `fourth`, or
  `last` for `monthly`
- `SCHEDULE_INTERVAL_MINUTES` is less than `1` in interval mode, unless the
  container is running one-shot

## What each mode uses

| Mode | Uses | Ignores |
| --- | --- | --- |
| `interval` | `SCHEDULE_INTERVAL_MINUTES` | `SCHEDULE_BACKUP_TIME`, `SCHEDULE_WEEKDAYS`, `SCHEDULE_MONTHLY_WEEK` |
| `daily` | `SCHEDULE_BACKUP_TIME` | `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_WEEKDAYS`, `SCHEDULE_MONTHLY_WEEK` |
| `weekly` | `SCHEDULE_WEEKDAYS`, `SCHEDULE_BACKUP_TIME` | `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK` |
| `twice_weekly` | `SCHEDULE_WEEKDAYS`, `SCHEDULE_BACKUP_TIME` | `SCHEDULE_INTERVAL_MINUTES`, `SCHEDULE_MONTHLY_WEEK` |
| `monthly` | `SCHEDULE_MONTHLY_WEEK`, `SCHEDULE_WEEKDAYS`, `SCHEDULE_BACKUP_TIME` | `SCHEDULE_INTERVAL_MINUTES` |

`RUN_ONCE=true` can be used with any `SCHEDULE_MODE` value because one-shot mode
does not repeat.
