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

Each timer keeps one failure record (C-18.5), exactly, in `timers.json`: `first_at`, `since`, `last_at`, `runs` (failed runs in a row), `failed_runs`, `error_type`, `message` (scrubbed with the handoff list, then cut to one line of 240 characters), `raised_at` (innermost frame under the package) and `recovered_at`. `timers.json` is rewritten whenever a record changes and read back at start. `daemon.status`, `status --json` and every `timer.run` event carry the record. The message is scrubbed again for token shapes the handoff list misses (see the review section). `subfleet status` prints a `timers:` block after the alerts. `doctor` has a `daemon timers` row read from the store and `timers.json`, and `--live` adds the daemon's own.

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

Hours are counted on the daemon's monotonic clock, so setting the wall clock back adds no event. A record of n failed runs over D such hours therefore writes at most 8 + ⌊log₂ n⌋ + 1 + ⌊D⌋ events per daemon process. A 2 s hot pass that fails all day writes 37 events, where it wrote 43,200 before. An event the store refuses is held, the first never replaced, and written by the timer's next run. Every failed run is within an hour of an event, and each event carries the record, so the count in it is the count so far. `mirror_hot` writes a `timer.run` with each of those events. It writes a change between failing and not on its first run at least a minute after the last change it wrote.

### What `tools/soak_report.py` reads

Its `timer.run` check is unchanged: a probe `timer.run` with no `last_error_type` on the day, and probe runs are still written every cycle. Its events-by-kind table counts the bounded `timer.error` events. A new section lists the day's `timer.error` events by timer, type and place, with the failed runs they count. It names no message, and its verdict does not change.

### Retention

`retention` runs on the daemon's worker, not `Timers._run`, so its exception reached `mark` only as a type name. It now passes the exception. The one `TimeoutError` a pass raises after progress, only to stay due, is now `RetentionCatchUp`. It is still reported as `TimeoutError` and opens no record.

## Review of #171, round 1 (2026-10-18)

The GPT-6.1 Sol review (`~/reviews/subfleet-v2-timer-failure-visible/review-171-r1.OUTPUT.md`) asked for changes:

| finding | fix |
|---|---|
| P1: a JWT whose header does not encode to `eyJ` passed the handoff scrub, so the message carried it to every reader | `redact_opaque` replaces three dot-separated base64url segments that hold a digit, any 20 or more token characters with an upper-case letter, a lower-case letter and a digit, and 32 or more hex digits. A run never starts at a `/`, so paths, job ids and module names survive. A Hypothesis property tries token shapes in many surroundings. |
| P2: `mirror_hot`'s bounded events lost its exact record over a restart, in both directions | `timers.json` in the state root holds every record exactly. It is written whole and renamed into place only when a record changes, and the daemon, `doctor` and offline `status` read it. Example tests replay the reviewer's three sequences, and the sequence property now restarts both timers. |
| P2: `reset_credits` and `alerts` failing inside a probe cycle marked only `probe` | Each marks its own record with the exception, then the cycle ends as before. |
| P2: a failing non-hot timer still writes a `timer.run` per run | Kept by design. A clean non-hot timer writes the same `timer.run` per run, and the probe check in `soak_report.py` and the start-up replay rely on that cadence. Before this change a failing timer wrote two events per run; now the failure adds only the bounded `timer.error` events. C-18.5 now says this explicitly instead of implying a bound on all events. |
| P2: a `timer.error` the store refused was replaced by the next; a refused snapshot left `doctor` blind | A refused event is held, the first never replaced, and written by the timer's next run of either kind. `doctor` reads `timers.json`, which does not depend on the store's write. |
| P2: a wall clock set back an hour each run wrote an event every run | Pacing reads `time.monotonic()`. The reviewer's 101-run sequence now writes 7 events. |
| P2: a record without a time zone crashed `status` and `doctor` | `failure_record` refuses one, and `failure_shown` cannot raise. A parametrized test persists such records in both places. |
| P2: the wedge test passed with the fix removed | The test claims the timer as `tick` does, refuses each of the two writes in turn, and checks that the next tick starts it. That mutant is now caught. |
| P3: the soak report credited a whole record's count to one kind | The section lists each record's count where the day's first and last events saw it, with its kinds, and never a count per kind. |

The mutation check now has 15 mutants. The original seven, the wedge, the replaced first event, wall-clock pacing, an unwritten records file, an unmarked companion, accepting naive times, no redaction and a restart that ignores the records file are each caught by the test file.

A 300-example run also found that the reference model revived a record when the wall clock was set back inside its day after a clean run had already ended it. The code was right: an ended record stays ended. The model now says so.

## Review of #171, round 2 (2026-10-18)

Round 2 was served by Opus 5.5 because the Codex lanes were closed. Its findings are in `~/reviews/subfleet-v2-timer-failure-visible/review-171-r2.out.md`. It marked all nine round-1 findings resolved and accepted the reasoning on the non-hot `timer.run` cadence. It found:

| finding | fix |
|---|---|
| P1: a lone surrogate in a message (retention decodes git paths with `surrogateescape`) could be neither stored nor sent. Every write for the timer raised, its queued event blocked the rest for good, and `daemon.status`, offline `status` and the whole `doctor` table broke. | The type, message, place and trace are made valid UTF-8 (`\uXXXX` escapes). A `timer.error` the store can never take is dropped; one it refuses with `OSError` or `sqlite3.Error` waits. |
| P2: `timers.json` always won though it was written after the events, and its failed writes were silent. A kill between them reverted a record, and a full or read-only state root misled `doctor`. | Each record has a revision, carried in the file and in every `timer.run`. The newer revision wins at start and in every reader. The file is written before the run's events. A failed write is kept and shown: `daemon.status` `timers_file`, a `status` line, `doctor --live`, and offline `doctor` and `status` when an event is newer than the file. |
| P3: offline `doctor` said `mirror_hot` ran clean and also that its last run reported an error | The newer revision supplies the whole status, `last_error_type` included. |
| P3: the mutants for change pacing on the wall clock and for no last send at `stop()` survived | New tests kill both. |
| P3: the token property was flaky under load (a Hypothesis health check) | Tokens are built to hold all three character classes, not filtered; 10 runs in a row pass. |
| P3: when the store refused a companion's record, probe's record showed the store's error | The companion's mark is guarded, and its own error ends the cycle. |
| P3: `stop()` racing a late mark could write an event twice | One timer's events are written by one thread at a time. |
| P3: C-18.5 overstated when held events are written | Now: after the run's records file and before its own `timer.run`. |

Left as follow-ups:

- Redaction does not cover base64 with `/` or `+`, single-case tokens, or URL-encoded ones; C-18.5 states the rules exactly.
- The demand-stage events of `reset_credits`.

The mutation check now has 24 mutants, and the test file catches each.

## Tests

`tests/unit/test_timers_failure.py` holds example tests for each reader and each review finding, and four Hypothesis properties:

- Over random sequences of clean and raising runs, the record equals an independent reference model after every run. The sequences have gaps from a second to over a day, wall clocks set back, and daemon restarts of both `keepalive` and `mirror_hot`.
- Over the same sequences, what `doctor` reads (the store and `timers.json`) equals the daemon's record after every run.
- The `timer.error` count per record stays within the bound, and the first failure and every monotonic hour of a continuing failure are written. `mirror_hot` writes at most one `timer.run` per error event, plus one a minute and one per restart.
- No token-shaped value survives into a failure message, and no malformed record can reach a reader.

Twenty-four deliberate mutants of `timers.py` were each caught (see the review sections above).
