# A failing daemon timer is seen (C-18.5)

2026-10-10, at release/217 4c6a8a9c9.

## What was wrong

`Timers._run` caught an exception from a timer's cycle and kept only its type:

- in a `timer.error` event, one per failed run, with `{"timer", "error_type"}`;
- in `last_error_type` of `Timers.status()` and of the run's `timer.run` event, replaced by the next run.

Nothing kept the message, the traceback or the place, and nothing was logged. No module under `subfleet/` read `timer.error`. `daemon.status` returned `Timers.status()` under `timers`, and `subfleet status --json` printed it, but the text `status`, `doctor` and the desktop app read none of it. So any of the eight timers (`probe`, `keepalive`, `reset_credits`, `alerts`, `retention`, `mirror`, `mirror_hot`, `claude_cards`) could raise on every run while `doctor` and `status` said nothing. A failure of the 2 s `mirror_hot` was visible for 2 s.

Two more defects came out of the same reading:

- A failing `mirror_hot` wrote a `timer.error` and a `timer.run` on every 2 s run, and the store keeps `timer.run` events and replays them at start.
- When the `timer.error` write itself raised (a locked or full store), `_run`'s `finally` was skipped. The timer stayed in `_running`, so no tick started it again until the daemon restarted.

`Store.add_event` writes a second, empty event of the same kind beside each event: it inserts the row inside `Store.transaction(kind)`, and `transaction` adds an audit event with `{}` whenever the connection's change count moved (`store.py`, `transaction`). That holds for every event kind, one to one. This change does not alter it. The new store read skips those rows.

## Reproduction

A real `Daemon` over a temporary state root. The control loop never ran. One timer's cycle was replaced by a function that raises `RuntimeError` with a mock credential in its message, and `Timers._run` was called directly, on a fake clock. Each scene ran 100 times.

| scene | after | before: status text, doctor | before: `timer.error` / `timer.run` | after: record | after: status text, doctor | after: `timer.error` / `timer.run` |
|---|---|---|---|---|---|---|
| keepalive (5 h) raises once | 1 | nothing, no row | 1 / 1 | failing, 1 run | named, FAIL | 1 / 1 |
| | 100 | nothing, no row | 1 / 100 | gone (the clean run was 21 days back) | nothing, PASS | 1 / 100 |
| keepalive raises every run | 100 | nothing, no row | 100 / 100 | failing, 100 in a row | named, FAIL | 100 / 100 |
| mirror_hot (2 s) raises once | 100 | nothing, no row | 1 / 1 | ran clean 2 s later; shown for a day | named, WARN | 1 / 2 |
| mirror_hot raises every run | 100 | nothing, no row | 100 / 100 | failing, 100 in a row | named, FAIL | 7 / 7 |

Each count of events has the same number of empty companion events beside it. `daemon.status`'s `timers` equalled `Timers.status()` in every observation. The mock credential reached no output. The five-hourly keepalive writes a `timer.error` on each failed run because each comes more than an hour after the last event (the hourly rule below).

`status` text after 100 failed hot passes:

```
timers: 1 failing
  mirror_hot  failing since 2026-10-10T12:00:00Z: 100 runs in a row, the latest at 2026-10-10T12:03:18Z: RuntimeError: boom on pass 100: token=[REDACTED] (timers.py:432 in _run)
```

The place is `timers.py … in _run` because the fixture's raising function is outside the package; a real cycle's place is the innermost frame under `subfleet/` it raised through, such as `sessions/mirror.py:1535 in _spread`.

## The live store

Read only, on 2026-10-10, counts by timer and type:

- `timer.error`: 14 from `mirror` (`OSError`, 2026-09-20 to 2026-10-07) and 3 from `probe` (`OperationalError`, 2026-09-22 to 2026-10-04), with 17 empty companion events.
- `timer.run` with a `last_error_type`: `probe` `TimeoutError` 207, `AdapterError` 47, `URLError` 24, `OperationalError` 3; `retention` `TimeoutError` 6,946, `CancelledError` 11; `mirror` `OSError` 14; `reset_credits` `TimeoutError` 2. Most of these are types a run reported without raising (a lane's usage read, a retention pass's deadline); only the 17 above ended a run by raising.

## What changed

Each timer keeps one failure record (C-18.5): `first_at`, `since`, `last_at`, `runs` (failed runs in a row), `failed_runs`, `error_type`, `message` (scrubbed with the handoff list, then cut to one line of 240 characters), `raised_at` (innermost frame under the package) and `recovered_at`. `daemon.status`, `status --json`, every `timer.run` event and the daemon's start-up replay carry it. `subfleet status` prints a `timers:` block after the alerts. `doctor` has a `daemon timers` row read from the store, and `--live` adds the daemon's own.

### How long a failure stays visible

A record is shown while the timer fails, and for a day after its next clean run. I chose a day for three reasons:

- An operator runs `doctor` and `status` by hand, so a failure must outlast the gap between two looks, which is often overnight.
- The slowest timers run every 5 h 05 m (`keepalive`) and 6 h (`claude_cards`), so a day spans four or more of their runs.
- After a day, a recovered transient stops showing. `doctor` reports a recovered record as `warn`, which never changes its exit code. It reports `fail` only while the last run failed.

A failed run within the day continues the record (`runs` from 1 again, `failed_runs` on). A failed run after the day starts a new record.

### Event volume

A failed run writes a `timer.error` event only when:

- it is the record's first failed run that this daemon process has seen;
- its type and place are a kind not yet written for the record (at most eight kinds);
- `failed_runs` reaches a power of two;
- an hour has passed since the record's last event.

A record of n failed runs over D hours therefore writes at most 8 + ⌊log₂ n⌋ + 1 + ⌊D⌋ events per daemon process. A 2 s hot pass that fails all day writes 37 events, where it wrote 43,200 before. The first failed run is always written, every failed run is within an hour of an event, and each event carries the record, so the count in it is the count so far. `mirror_hot` writes a `timer.run` with each of those events. It writes a change between failing and not on its first run at least a minute after the last change it wrote.

### What `tools/soak_report.py` reads

Its `timer.run` check is unchanged: a probe `timer.run` with no `last_error_type` on the day, and probe runs are still written every cycle. Its events-by-kind table counts the bounded `timer.error` events. A new section lists the day's `timer.error` events by timer, type and place, with the failed runs they count. It names no message, and its verdict does not change.

### Retention

`retention` runs on the daemon's worker, not `Timers._run`, so its exception reached `mark` only as a type name. It now passes the exception. The one `TimeoutError` a pass raises after progress, only to stay due, is now `RetentionCatchUp`. It is still reported as `TimeoutError` and opens no record.

## Tests

`tests/unit/test_timers_failure.py` holds example tests for each reader, and three Hypothesis properties:

- Over random sequences of clean and raising runs, with gaps from a second to over a day and daemon restarts, the record equals an independent reference model after every run.
- Over the same sequences, the `timer.error` count per record stays within the bound, and the first failure and every hour of a continuing failure are written.
- For `keepalive`, the store's record equals the daemon's after every run. For `mirror_hot`, the two disagree only within a minute of the last change written.

Seven deliberate mutants of `timers.py` were each caught, including no hourly event, every failed run written, and a streak not reset by a clean run.
