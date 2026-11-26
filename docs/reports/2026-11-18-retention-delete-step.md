# Retention delete step after a due pass (d3085), build r1

Max ruled d3152 yes on 2026-11-18 at 13:20Z. Step 2 of that ruling covers d3085: build fix (a), get a review from the other model family, run CI, then install release/217 with the kit. The hub built this in-session under Fleet ops' exemption 21. Base: release/217 at 35f8615af.

## The problem

The operator delete path (C-8.7) takes one job per turn, and only while no hourly retention pass is due (`_retention_delete_turn`). The control loop offers the retention key every tick (50 ms) while a pass or a delete step is due (`_retention_due`).

How a pass ends decides whether the path gets a turn:
- **A pass that reaches its 180 s deadline after progress raises `TimeoutError`, and a pass that fails raises its error.** Neither touches the hourly clock (`_last_maintenance`), so the pass stays due. `_schedule` then holds the key for its C-5.10 retry delay (0.5 s doubling to 60 s), and when the delay ends the next turn finds the pass due again. Through a run of such passes, the path never takes a step.
- **A clean catch-up, or a holder-scan failure that leaves a job waiting for its check 2, sets the clock 5 s out (`RETENTION_CATCH_UP_S`).** The control loop offers the key at once, so a pending request steps inside that window. A busy worker pool can miss the window.
- **A pass that sets the hourly clock** leaves the path an hour of turns.

The live daemon (pin 85769b826) shows the first case. `~/.subfleet/daemon.log` stamps no line, but guard preflight lines carry times. From the first stamped line after 03Z on 11/17 (05:49:59Z) to the clean pass that ended the stretch at about 07:28Z, the log records 14 passes and no delete step. All 14 ended at their deadline after progress, with no catch-up, scan failure or clean pass among them. The C-5.10 count reached 16 in a row. The Disk manager's d3005 dry run, started at 06:26Z, sat inside this stretch and made no progress. The first turn it got, after the 07:28Z pass, found it stopped.

The second case shows in the same log: the dry run of 11/16 at about 18:01Z took its two steps straight after a catch-up.

**A correction to the 11/17 analysis.** The hub's `disk-asks-20261117/starvation-cures.md` reported that retention was due, and so the path starved, for 440 of 951 minutes between 11/16 15:37Z and 11/17 07:28Z, and it counted catch-up stretches as starved. Catch-ups leave a window, so 440 minutes is the time retention was due, not the time the path starved. The starved time is the time inside runs of deadline-ended or failing passes, and nobody has measured it.

## The fix

A pass that leaves retention due now owes the delete path one step, which runs before the next pass.
- **The flag.** `Daemon._retention_delete_owed` has a class default of `False`, so partial daemons built without `__init__` still work.
- **Setting it.** `_retention` sets the flag when a pass starts, after the delete-turn check and before the sweeps.
- **Clearing it.** `_retention` clears the flag at each of the five branches that set the hourly clock: dormant, cancelled, scan failed with nothing waiting, deadline before progress, and normal completion. A catch-up, a scan failure with a job waiting for check 2, a deadline after progress and a raise all leave it set.
- **Stepping.** `_retention_delete_turn` steps when the path is ready and either no pass is due or a step is owed. It clears the flag before the step, so a step that raises still settles the debt. The existing handler still fails the request.
- **The return value.** `_retention` uses the gate's clock reading and returns `DEFERRED` for a step taken while the pass is due. `_schedule` holds the whole retention key through the retry wait, so the owed step runs when the wait ends. From the eighth failure in a row, the ready step waits 60 s. `_schedule` counts `DEFERRED` as neither a success nor a failure (C-5.10, C-5.11). It keeps the key's failure count and clears the retry time, so the daemon can offer the next pass as soon as the step finishes. Consecutive failing passes still back off as before, with one step after each wait.
  - A step in a catch-up window, or in the hour, returns `None` as before.
  - Without this, the step's normal return cleared the count. A pass that fails at once would then be retried every 0.5 s, and logged each time, for as long as a request was pending.
- **Unchanged.** The step still runs under the unused-tree sweep lock and leaves the pass's clock alone. A step and a pass never share a turn.

Text changes:
- C-8.7 gains the owed step and its C-5.10 accounting.
- The version 3 change list gains one line.
- `docs/desktop/retention-archive.md` §18 gains one sentence.
- The `DEFERRED` comment names its new caller.

## What it changes on the live daemon

Through a run of deadline-ended passes, the path gets one step per retry cycle. Each cycle is the 180 s pass plus a retry delay that reaches 60 s from the eighth failure in a row, so about one step every 3 to 4 minutes. During catch-ups nothing changes, unless the pool misses the window: then the owed step runs before the next pass instead of being lost.

Each step first resumes every restart journal whose retry time has come, then does one job. It adds that recovery time plus one job's delete time before the next pass. These rates come from the code and the log's cycle times. The install verification should read the `retention delete:` job-result lines in `daemon.log`. A step that only resumes journals writes no such line.

Nothing changes when retention isn't due, when the path has no request and no restart journal due, or while retention is dormant. In each of those cases `_retention_delete_ready` is false or no pass is due, and the flag has no effect. A daemon with two due recovery journals and no request replays both in one owed step, which delays the next pass and logs no job-result line.

## Tests

`tests/unit/test_retention_delete_daemon.py` adds 23 cases in eight tests. They use a monkeypatched `maintenance` that returns, or raises, each outcome in turn:
- **Each due-leaving outcome** (4 cases): the pass runs, then the owed step runs while the pass is due. The step returns `DEFERRED`, holds the lock and leaves the clock alone. Then the next pass runs.
- **Each hourly outcome** (5 cases): the flag is clear, `next_due` is about an hour out, and the next due pass runs before any step.
- **An owed step needs a pending request:** with none, the pass runs.
- **Dormancy:** a dormant daemon takes no owed step, and its pass settles the debt.
- **A step that raises still settles the debt:** the request fails with `step: RuntimeError`, and the next turn runs the pass.
- **A catch-up leaves a window:** a turn inside it takes a plain step (`None`) and settles the debt.
- **Through `_schedule`** (2 cases: a deadline after progress, and a raise). The test uses a pool that runs each job at once:
  - the failing pass holds the key for its retry delay;
  - the owed step runs when the delay ends, keeps the failure count at 1 and frees the key;
  - the pass runs next and fails again, now with count 2 and a longer delay;
  - its owed step runs;
  - a finishing pass clears the count.
- **Randomized orders** (seeds 0-7, 60 outcomes each). Every turn is offered while retention is due, which is the worst case: a pool that misses every catch-up window. The test checks that:
  - a due-leaving pass is always followed by exactly one step, and an hourly pass never is;
  - a step's turn runs no pass;
  - the number of steps equals the number of due-leaving passes before the last one.

  At most three turns are allowed per pass, so a debt that never clears fails the test instead of hanging it.

Results (venv `daemon-quiet/.venv`, serial, private basetemp):
- **On this branch:** 39 passed in 0.93 s.
- **Fail-before:** the new test file, run against 35f8615af's `subfleet/`, gives 22 failed and 17 passed.
  - 15 cases fail on behaviour: the 4 due-leaving cases (a second pass ran where a step was expected), the raising-step case (no step ran), the 8 randomized seeds, and both `_schedule` cases (no step ran when the retry delay ended).
  - 7 fail only because they read the new flag: the 5 hourly cases, the catch-up window and dormancy. What they check for behaviour already held before the fix.
  - The 16 cases that predate this change pass, and so does the new case with no pending request.
- **Mutants:** 10 of 10 killed (2026-11-18T14:48:41Z). The mutants:
  - drop the flag's set at pass start;
  - drop its clear in the turn;
  - drop each of the five hourly clears in turn;
  - make the gate ignore the debt;
  - return `None` from an owed step;
  - return `DEFERRED` from every step.

## Wider suites

The first run took place before the `DEFERRED` change. It covered 36 files:
- the contract-reading tests: `test_invariants_index`, `test_retention_turn_contract`, `test_protocol_registry`, `test_publish_rule_round7`, `test_notice_keep`, `test_desktop_ledger` and `test_live_store_guard_round8-10`;
- every test that reads the retention clock: `test_retention_listings`, `test_retention_listings_properties`, `test_daemon_settle`, `fake/test_retention_dormant`, `fake/test_review130_ab` and `fake/test_native_maintenance_startup`;
- `test_retention_deadline_r2`, `test_timers_retention`, `test_retention_qos` and `test_retention_turns`;
- every `test_retention_delete_*` file.

Result: 996 passed in 761 s, serial, from 13:57:03Z to 14:15:53Z on 2026-11-18. The basetemp reached 0.194 GB.

After the `DEFERRED` change, a second run covered every test file that calls `_retention()`, reads the contract, the retention clock or `DEFERRED`, plus `test_retention_deadline_r2` and `test_timers_retention`. The hub stopped that run partway, at 14:50Z, when free disk fell under the 49 GB stop line of Fleet ops' exemption 21, before it printed a result. The run is owed before the review starts, and CI runs the whole suite.

CI runs the whole suite in 7 shards on the PR.

## Not done here

- The review from the other model family, on a lane.
- The install, built from d6db9da85 (#335's merge commit; its parent 4aed7a3c9 is release/217 just before #177, 14 merges past the installed pin 85769b826). The kit for that install is separate work, with its own review.
- Measuring the live step rate (see "What it changes on the live daemon").
- Measuring the time the path was starved on 11/16-17 (see the correction above).

## Corrections (2026-11-26)

Issue #336 prompted five corrections.

- An owed step runs after the retry wait, and the turn's answer uses the gate's clock reading.
- Step timing includes due journal recovery. Job results use the `retention delete:` prefix.
- Due journals can make the path ready without a request. A step that only replays journals delays the next pass and logs no job result.
- Removed the trailing spaces from the blank line in the test description.
- Identified d6db9da85 as the install commit and counted 14 merges from the pin to its parent.
