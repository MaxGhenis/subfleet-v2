# D-NOTICE-KEEP1: unread notice retention

Continued from checkpoint `7e982649f549a09d4a5b15a243a9be15946008db`, based on
`a8624a4899a2`, in the assigned worktree. No live state or policy was read or changed;
no push or history rewrite was attempted.

This continuation started around 04:00 UTC. Verification stopped at
06:53:15 UTC on 2026-11-05 to finalize this report within the three-hour limit.

## Rule and files

Before, any `pending` or `offered` job notice with a nonempty session pinned its
job indefinitely. Now it pins only while its age is **strictly less than
`retention.notice_keep_days`**, default **7** days. **0 means forever**. Times
are parsed and compared as UTC datetimes, including the cutoff's fractional
seconds; malformed timestamps conservatively retain the old pin. Notices
without sessions still pin nothing (IR-17). Other pins, budgets, notice states,
and hook delivery remain unchanged.

Changed files across the checkpoint and continuation:

- `subfleet/contracts.py`, `policy.py`: default and policy validation, including
  fractional days and zero.
- `subfleet/retention.py`, `daemon.py`, `retention_survey.py`: keep-time plumbing
  through selection, commit rechecks, daemon, offline survey and sampler.
- `subfleet/retention_notices.py`, `retention_archive.py`: timestamp handling,
  transactional retirement outcomes, expiry and later archive lookup.
- `subfleet/store.py`, `store_schema.sql`: identify outcome service notices by
  their event's id/session/creation time, with an index; no new table.
- `subfleet/retention_cli.py`: read-only `retention outcomes --session ID [--json]`.
- `tests/unit/test_notice_keep.py`, `test_retention_outcomes.py`, `test_policy.py`,
  `tests/fake/test_notice_keep_delivery.py`: boundaries, properties, parity,
  real retirement, delivery, expiry, rollback and durable lookup.
- `tools/notice_keep_mutants.py`: reproducible isolated behavioral mutations,
  unmutated controls, assertion-only kill receipts and source hash checks.
- `docs/acceptance-contract.md`, `desktop/retention-archive.md`, `invariants.md`,
  `invariants.json`, `invariant-gaps.md`, `lanes/timers.md`, and this report:
  updated contract/changelog and current pin descriptions. Historical reports
  retain their original descriptions.

## Outcome and its lifetime

The retirement transaction writes one service notice per affected session
before deleting the job and its original notices. It preserves each distinct
first line, says when retention retired the job unread and the keep period,
and gives the archive and restore command. A `retention.notice_retired` event
identifies it, and `retention.pruned.outcome_notices` lists its ids. Failure
rolls back both the outcome and deletion.

Service notices use the existing delivery/marking path, including sessions
launched by Subfleet, without changing hooks. No retention pin reads their
table. Once surfaced or acknowledged they follow the existing 14-day cleanup.
Still-unread outcomes are withdrawn after another `notice_keep_days`, with
an expiry audit event. Thus the unread delivery queue retains only one time
window, rather than accumulating for an absent session. With policy zero,
unread notices prevent retirement, so these retirements generate no new outcomes.
Expiry uses the current policy: changing it to zero also leaves existing
unread outcomes unexpired, but creates no further unread-retirement outcomes.

For a session returning after that window, `subfleet retention outcomes
--session ID [--json]` reconstructs the original first lines and retirement
details from the archived notices and retirement event. It neither recreates
queue rows nor writes records or pins. This explicit lookup is needed after
automatic delivery expires. Archives and the audit ledger retain their existing
durable-history lifecycle; a person removing an archive makes that outcome
unavailable, which the lookup reports explicitly. The lookup rejects database
and archive links outside the supplied state root.

Boundedness here applies to the delivery carrier. The new retirement audit
events are not age-pruned: audit/archive storage remains proportional to total
retirements, under the existing durable-history design. No separate permanent
per-session inbox or replay cursor is added.

## Invariants and evidence

All tests below are in `test_notice_keep.py` unless another file is named.

| Invariant | Tests |
| --- | --- |
| Default 7; policy accepts zero/fractional days and rejects invalid values | `test_the_default_is_seven_days_and_the_pass_uses_it`; `tests/unit/test_policy.py::test_conversation_and_retention_sections_default_and_follow_the_policy_file`, `test_conversation_and_retention_values_are_validated` |
| Under the keep pins; at/over it does not; zero is forever | `test_a_notice_just_under_the_keep_pins_and_one_just_over_does_not`, `test_zero_keeps_an_unread_notice_for_ever`, `test_zero_keeps_for_ever_through_a_pass` |
| Timestamp spellings cannot change age | `test_ages_compare_as_times_not_as_strings` |
| No session means no pin, at any age | `test_a_notice_with_no_session_pins_nothing_at_any_age` |
| Exact pinned set, time monotonicity, acknowledgement never adds a pin | The three `test_property_*` Hypothesis tests: 60 examples each, states/ages/sessions/multiple notices |
| Survey and actual retirement agree for 0/7/30 days; sampler receives policy | `test_the_survey_and_a_pass_give_the_same_pins`, `test_the_sampler_reads_the_same_keep` |
| An expired notice alone permits actual worktree retirement; fresh control stays | `test_a_pass_retires_a_job_whose_only_pin_is_an_expired_notice` |
| Outcome reaches the next session delivery with its original first line | `tests/fake/test_notice_keep_delivery.py::test_the_outcome_reaches_the_session_on_its_next_delivery`, both hooks and launched/unlaunched sessions |
| Outcome and deletion are atomic; outcome pins nothing | `test_outcome_and_job_deletion_roll_back_together`, `test_the_outcome_pins_nothing` |
| Outcome expires safely; pings, reused ids and concurrently surfaced rows survive | `test_an_undelivered_outcome_is_withdrawn_after_the_keep_and_a_delivered_one_is_left`, reused-id and surfaced-race tests, daemon expiry test |
| A later session can retrieve an expired outcome without new state | Eight tests in `tests/unit/test_retention_outcomes.py`, also covering malformed/unavailable archives and path escapes |

New record/event keys were searched against tests' exact comparisons:
`notice_keep_days`, `notice_keep_s`, `notice_ids`, `notices_created_at`,
`outcome_notices`, `first_lines`, `retired_at`, `retention.notice_retired` and
`retention.notice_outcome_expired`. Existing retention event consumers compare
subsets; new tests assert the added data. Policy whole-dict comparisons use
`RETENTION_DEFAULTS`. No new daemon refusal was introduced; handler-refusal
tests are included in verification.

## Mutation verification

**7 control tests passed; 8 mutants killed, 0 survived, 0 invalid.** The runner
installs each fault in its own pytest process; production files are never
edited. All eight kills were call-phase assertion failures, with no
collection/setup/teardown failures; monitored source hashes stayed unchanged.
Execution took 10.909 s of subprocess time. Evidence is in
`/private/tmp/notice-keep-days-20261105T0400/mutants/summary.json` and its
per-run logs and phase receipts.

All killers below are in `tests/unit/test_notice_keep.py`.

| Mutant | Test that killed it |
| --- | --- |
| (a) Age clause omitted | `test_a_notice_just_under_the_keep_pins_and_one_just_over_does_not[7]` |
| (b) Raw timestamp string comparison | `test_ages_compare_as_times_not_as_strings` |
| (c) Survey still pins forever | `test_the_survey_and_a_pass_give_the_same_pins[7]` |
| (d) Zero interpreted as zero seconds | `test_zero_keeps_an_unread_notice_for_ever` |
| (e) Outcome not written | `test_one_outcome_per_session_and_none_for_a_notice_no_one_can_read_or_has_read` |
| (f) Outcome pins its job | `test_the_outcome_pins_nothing` |
| (g) Extra: unread outcomes never expire | `test_an_undelivered_outcome_is_withdrawn_after_the_keep_and_a_delivered_one_is_left` |
| (h) Extra: exact-cutoff notice still pins | Boundary test used for (a) |

## Commands and results

Evidence logs are under
`/private/tmp/notice-keep-days-20261105T0400/`.

All pytest runs use `SUBFLEET_LIVE=0`. Intermediate diagnostic invocations,
in addition to the final commands recorded below:

- Initial and second targeted runs: `uv run --no-sync --with
  'pytest-xdist>=3.6' pytest -q -n 4 --timeout=540 --basetemp "$TMPDIR/<run>"
  tests/unit/test_notice_keep.py tests/fake/test_notice_keep_delivery.py
  tests/unit/test_policy.py`, with basetemps `notice-targeted-1` and
  `notice-targeted-2`. The initial invocation also passed
  `--cache-dir /private/tmp/notice-keep-days-20261105T0400/uv-cache` to uv.
- Intermediate focused runs: `uv run --no-sync --with 'pytest-xdist>=3.6'
  pytest tests/unit/test_retention_outcomes.py -n 2 --basetemp
  "$TMPDIR/outcomes-focused-2" -q`; the preceding `outcomes-focused-1` run
  additionally selected `tests/unit/test_notice_keep.py` and
  `tests/fake/test_notice_keep_delivery.py`.
- Corrected rollback: `uv run --no-sync pytest -q --timeout=540 --basetemp
  "$TMPDIR/rollback-corrected"
  tests/unit/test_notice_keep.py::test_outcome_and_job_deletion_roll_back_together`.
- The broad run used `uv run --no-sync --with 'pytest-xdist>=3.6' python -`
  to construct the file selection and invoke pytest as follows. The offline
  outcomes file did not yet exist when this selection was made; excluding it
  reproduces the original 93-file scope in the completed tree:

```python
from pathlib import Path
import os, subprocess, sys
paths = sorted(str(p) for p in Path("tests").rglob("test_*.py")
               if p.name != "test_retention_outcomes.py"
               and any(word in p.name for word in ("retention", "survey", "notice", "hook")))
paths += ["tests/unit/test_handler_refusals.py"]
subprocess.call([sys.executable, "-m", "pytest", "-q", "-n", "4", "--timeout=540",
                 "--basetemp", os.environ["TMPDIR"] + "/retention-notice-hook", *paths])
```

- Required initial `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and
  `UV_CACHE_DIR="$TMPDIR/uv-cache"`: `uv sync --group dev` failed because the
  sandbox denies the shared cache's `sdists-v9/.git`. Using a writable cache
  override succeeded (10 packages resolved, 9 checked).
- Initial targeted notice/policy run: **285 passed, 3 failed** in 52.69 s.
  All three failures were sandbox-denied temporary-directory creation in the
  Hypothesis tests, not retention assertions.
- With writable temporary storage, targeted notice/policy run: **288 passed**
  in 44.17 s, including the strict boundary change.
- Intermediate outcomes/notice/delivery run: **51 passed, 1 failed** in 91.22 s;
  the new rollback test incorrectly expected the injected exception to be
  returned. Its expectation was corrected to `pytest.raises`.
- Intermediate archive lookup run: **7 passed** in 30.26 s, before the eighth
  malformed-notice regression was added; all eight passed in the final run.
- Required existing retention/survey/notice/hook selection, plus handler
  refusals: **2,712 passed, 2 failed, 6 skipped, 2 errors** in 1,563.31 s
  (93 files, `-n 4 --timeout=540`). One failure captured the old rollback
  expectation before it was corrected; its corrected standalone run passed
  (**1 passed**, 7.04 s). The other was an existing assertion that the word
  `retention` appear nowhere in `why` output: the selected basetemp was named
  `retention-notice-hook`, and that path appeared in the correct lease
  explanation. The final targeted rerun uses a neutral basetemp name.
  Both errors were queue-tray Swift probe compilation failures in unchanged
  `app/Sources/UIWindow.swift` (missing property-wrapper projections such as
  `$renaming`, immutable-self errors and type-checking failures).
- Mutations: `uv run --no-sync python tools/notice_keep_mutants.py --output-dir
  /private/tmp/notice-keep-days-20261105T0400/mutants`: **7 controls passed,
  8/8 killed**; no environmental kills.
- Final targeted rerun: **298 passed** in 17.80 s:
  `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 3 --timeout=540
  --basetemp "$TMPDIR/targeted-final" tests/unit/test_notice_keep.py
  tests/unit/test_retention_outcomes.py tests/unit/test_policy.py
  tests/fake/test_notice_keep_delivery.py
  tests/fake/test_detached_retention_fence.py::test_c6_11_a_worktree_lease_another_job_holds_is_never_called_retentions`.
- Final `git diff --check`: passed. Continuation status is six modified
  tracked files and three new files; nothing was staged because Git refused
  the index lock.
- Full suite: launched under `/usr/bin/lockf -k
  /private/tmp/claude-501/subfleet-suites.lock`, using
  `uv run --no-sync --with 'pytest-xdist>=3.6' python
  /private/tmp/notice-keep-days-20261105T0400/run_suite.py`.
  The initial controller completed shards 1 and 2 with
  `python -m pytest -q -n 4 --dist=loadfile -p ci_shard_plugin --ci-shard I/12
  --timeout=540 --basetemp "$TMPDIR/suite-shard-I" --junitxml <evidence>/suite-I.xml`.
  Shard 2's single native mutation file took 53 minutes while three workers
  were idle. At its completion the controller was interrupted, the lock was
  reacquired, and `run_suite_continuation.py` runs shards 4–12, then 3, with
  `--dist=load`, basetemps `suite-load-shard-I`, and `suite-load-I.xml` receipts.
  The initial partial shard-3 attempt is retained as
  `suite-3-aborted-startup.log` and excluded from completed-shard totals;
  shard 3 is scheduled last. Completed shards 1 and 2 are not repeated.
  `PYTHONPATH="$PWD/tools:$PWD"`, `SUBFLEET_LIVE=0`, and the test helper's
  `SUBFLEET_SWIFT_NESTED_SANDBOX=off` are set.

The full-suite attempt was interrupted to meet the time limit (controller exit
130). **Four of twelve shards completed: 4,745 passed, 6 failed, 5 errors,
86 skipped, 14 xfailed.** Partial attempts are excluded from these totals:

| Shard | Passed | Failed | Errors | Skipped | Xfailed | Pytest time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 877 | 4 | 0 | 1 | 1 | 519.18 s |
| 2 | 30 | 0 | 0 | 0 | 0 | 3,204.47 s |
| 4 | 1,731 | 1 | 5 | 31 | 13 | 2,645.43 s |
| 5 | 2,107 | 1 | 0 | 54 | 0 | 1,572.77 s |

Shard 3's initial partial attempt has no final summary. Shard 6 was stopped
at about 15%, after **two failure markers**; interruption left no final
summary or JUnit file, so their identities/diagnostics are unavailable and
they remain unclassified. These two are additional to the six completed-shard
failures above. Shards 3 and 6 are incomplete; shards 7–12 were not started.
The controller sessions have ended and released the shared suite lock.

Shard 1's four failures are in unchanged native frontend probes:
`test_c29_7_real_daemon_dispatching_then_started_is_interrupted`,
`test_c29_7_real_daemon_dispatching_then_released_is_withdrawn`,
`test_manual_reload_acknowledges_read_and_loads_replacement`, and
`test_a_failed_live_dependency_stops_and_dumps_the_app_state`. The queue
probes time out; their native gates depend on marker files, whose absence
would explain the timing. The live-failure probe's expected file is absent
after `createFile`, and its subsequent file-handle force unwrap traps; the
reload probe traps during an atomic file replacement. These suggest native
test I/O/environment restrictions, but the underlying OS error was not
retained, so an exact sandbox cause is unproven. Their app/test sources are
unchanged from the base; none exercises notice retention.

Shard 4's failure and five errors all come from unchanged
`tests/unit/test_guard_subfleet_root.py`: two aggregate **540-second
timeouts**, one in the shared `verdicts` fixture (five setup errors), and one
in `test_r3_property_the_verdict_is_the_rule`. The latter precomputes 1,066
shell-hook invocations with four internal workers before Hypothesis examples.
No individual subprocess timeout or permission/path error appears. This is
an expensive cold guard corpus exceeding the deadline, rather than evidence
of a notice-retention regression. These tests use fake homes/filesystems.

Shard 5's failure is a real assertion in the unchanged bounded scrubber:
`tests/unit/test_sessions_handoff_excerpts.py::test_an_excerpt_never_shows_a_value_the_whole_scrub_removes`.
Hypothesis generated a 262,145-character input (one above the scrub budget)
with a synthetic credential at an excerpt boundary after a base64 omission
marker. Whole-text scrubbing removes the value; bounded scrubbing leaves it.
The scrubber and property-test sources are unchanged from the base. This is
an existing redaction edge case, not a sandbox or timeout failure, and needs
separate repair. No real credential or live state was involved.

## Deviations and open questions

- The system temporary root and shared cache are outside writable sandbox
  roots. Subsequent tests use its permitted child:
  `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)/subfleet-attempt-20261104-211650-notice-keep-build-a2-zt45sn4p/notice-keep-temp"`,
  `UV_CACHE_DIR="$TMPDIR/uv-cache"`; each invocation has a fresh basetemp beneath it.
- This checkout is detached, as supplied. The sandbox denies Git's parent
  worktree `index.lock`, including named-file staging attempts for the boundary
  fix, docs, the source/test/mutation step, and all nine continuation files
  including this completed report. Git reports
  `Operation not permitted` creating
  `/Users/maxghenis/subfleet-v2/.git/worktrees/20261104-211650-notice-keep-build/index.lock`.
  HEAD remains `7e982649f549a09d4a5b15a243a9be15946008db`; checkpoint commits
  are preserved. No continuation commit could be made, including the requested
  report commit. All nine continuation files remain in the assigned workspace.
  No protection was bypassed and nothing was pushed.
- The extra offline lookup closes the gap between bounded automatic delivery
  and a session returning arbitrarily late. After queue expiry this is a manual
  lookup and depends on retained archives/events. Boundedness is interpreted
  as the delivery carrier's lifetime, not total audit storage: the new audit
  metadata is not age-pruned. It adds no permanent per-session queue.
- The full suite uses the existing Swift helper's supported nested-compiler
  sandbox opt-out after the unmodified Swift probes failed to compile. The
  original retained diagnostic tail does not establish the primary compiler
  error, so those initial errors are not claimed as a proven source defect or
  confirmed sandbox denial.
- Test scheduling changed after shard 2: keeping its one file together used
  only one worker and consumed 53 minutes. Remaining shards distribute tests
  across four workers, with backend shards first to maximize relevant coverage
  before the three-hour deadline. The handoff interrupted a partial shard-3
  attempt, so that partial attempt is an extra diagnostic run rather than part
  of the final whole-suite counts. Both controllers hold the requested shared
  lock while testing.
- The twelve-shard suite could not finish within this continuation's
  three-hour allowance; the time cap takes precedence over further runs.
  The completed-shard counts above do not establish a whole-suite pass; CI
  must finish the remaining coverage and decide the PR's suite result.
- Open questions are the unfinished full-suite coverage, the native probes'
  precise I/O failure cause, and separate repair of the existing scrubber
  counterexample. No further notice-retention correctness issue was found in
  the final independent code/report reviews.
