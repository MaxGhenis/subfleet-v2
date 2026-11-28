D-F6 reserve verdict CI flake

The test race is confirmed. The fix waits for the asserted lane's verdict with
the original 30-second timeout. Production code is unchanged. Full daemon e2e
validation remains blocked by this sandbox's denial of macOS process inspection.

`usage_rows` queries committed `readings`, not HTTP request logs
(`tests/e2e/test_reserve.py:13`). `E2E.rows` opens a fresh read-only SQLite
connection (`tests/e2e/conftest.py:235`). The daemon observer installs the fake
transport (`tests/e2e/conftest.py:44`, `tests/fake/profile.py:164`); its usage
branch returns the fixture payload or raises HTTP 429 with `Retry-After: 3035`
(`tests/fake/profile.py:157`, `tests/fake/profile.py:131`). The real adapter maps
429 to a rate-limited result with no provider readings
(`subfleet/adapters/claude.py:1070`).

The timer reads lanes concurrently (`subfleet/timers.py:967`), collects their
results in completion order (`subfleet/timers.py:977`), then publishes each lane
separately (`subfleet/timers.py:988`). A lane's readings and verdict share one
transaction (`subfleet/timers.py:841`, `subfleet/timers.py:845`,
`subfleet/timers.py:861`), committed at `subfleet/store.py:349`. Thus claude-2's
account and Fable weekly readings can satisfy the old two-row wait before
claude-1's transaction starts. There is no cross-lane commit ordering guarantee.

This checkpoint also appends an empty audit event after an explicit event
(`subfleet/store.py:617`, `subfleet/store.py:345`). A temporary database confirmed
the verdict payload followed by a second `timer.verdict` row containing `{}`.
The new query excludes that envelope and orders payloads by `event_id`.

Every matching count-based readiness check found in `tests/e2e` was changed:

- `test_c9_9_a_rate_limited_lane_is_left_alone_until_retry_after`
  (`tests/e2e/test_reserve.py:68`) waits for claude-1's payload-bearing verdict,
  then immediately asserts status and Retry-After. Missing or incorrect values
  still fail; the timeout remains 30 seconds.
- `test_c11_7_no_slack_anywhere_holds_non_fable_work_and_says_why`
  (`tests/e2e/test_reserve.py:37`) requires all four lane/scope pairs, so repeated
  readings from one lane cannot satisfy readiness. Its timeout remains 30 seconds.
- `test_periodic_codex_usage_uses_local_transport_and_keeps_lanes_enabled`
  (`tests/e2e/test_http_isolation.py:6`) waits for both Codex verdicts, then checks
  both lanes' provider readings and enabled states. Its old pattern was protected
  by each lane's atomic transaction; the new wait states the assertion directly.
  Its timeout remains 20 seconds.

The other reserve tests already wait for named lane/scope pairs or a verdict
status. Text searches and an AST scan found no additional waits that use a
reading count to precede a verdict assertion elsewhere in `tests/e2e`.

Python 3.12.14 deterministic controls invoked the original and fixed C-9.9 test
bodies against the real Claude adapter, fake HTTP transport, timer probe cycle,
SQLite store, and `E2E.rows`/`E2E.until`. The test-only harness replaced daemon
startup with a probe-cycle thread. A monkeypatch forced the legal publication
order claude-2 before claude-1 and held `_persist` for claude-1 outside its
transaction, after confirming the processed response was 429/3035. The gate
released two seconds after the test observed the empty verdict. No production
hook, database row, or verdict was fabricated.

| Control | Observed result |
| --- | --- |
| Original test with publication held | Same empty-verdict assertion failure, 0.354 s; both weekly readings belonged to claude-2 |
| Fixed test with the same gate | Passed, 2.221 s |
| Suppress claude-1's verdict write | Failed with the original 30 s timeout; waiter measured 30.093 s |
| Write wrong status | Assertion failed immediately, 0.083 s |
| Write Retry-After 3034 | Assertion failed immediately, 0.263 s |
| Fixed test without delay | Passed, 0.011 s |

The verdict arrived after gate release; this reproduction did not reveal a lost
verdict product bug. These controls do not replace full daemon e2e execution.

Under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, timer probe,
Claude usage, and busy-read unit files passed: **178 tests in 43.01 s**. The reserve
and HTTP isolation e2e files produced **5 skips**. Twenty sequential invocations
of `tests/e2e/test_reserve.py` without delay produced **80 skips**, not passes.
All skips came from `tests/e2e/conftest.py:315`: macOS boot identity is unavailable
because sysctl/ps inspection is denied. The full delayed daemon reproduction and
20 successful e2e repetitions still require a process-inspection-capable runner.
Compilation and `git diff --check` passed. Task TMPDIR was created under
`getconf DARWIN_USER_TEMP_DIR` and removed after validation; no logs or JSON
evidence were committed.

Shared Git metadata is read-only. Commits are in `.git-local` on
`refs/heads/fix/reserve-verdict-flake`, based on
`694723d9c0cf4d848ec9b7ae42913111e3d33335`. The final head is reported with delivery;
it is also available via `git --git-dir=.git-local rev-parse HEAD`.
