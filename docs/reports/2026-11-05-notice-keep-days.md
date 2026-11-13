# D-NOTICE-KEEP1: unread notice retention

PR #294 was reviewed at committed head `d4b21a244c43`, based on
`a8624a4899a2`. The earlier build continued from checkpoint
`7e982649f549a09d4a5b15a243a9be15946008db`; its continuation files are included
in the reviewed PR, not nine outstanding uncommitted files. No live state or policy was read or changed;
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
  their event's id/session/creation time/text SHA-256, with an index; no new table.
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
  That was the earlier build workspace's state, not the reviewed PR's state:
  the nine continuation files were subsequently included in committed PR head
  `d4b21a244c43`. Round 1's workspace and commit results are recorded below.
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

## Fix round 1

### Findings and identity

- Reproduced both runtime findings on reviewed head `d4b21a244c43` before
  changing production code. The corrected baseline ran seven cases: **4 failed,
  3 passed in 45.14 s**. Failures were the same-second daemon `ping`, a
  same-second/text replacement between expiry's read and delete, SQLite
  sidecar creation, and the missing `#294` change-log reference.
- Retirement now records `outcome_sha256`, the full SHA-256 of the exact
  outcome text. Expiry matches that digest alongside id, session and creation
  time before opening its write transaction. The delete rechecks the exact
  text, creation time, session, id and unread state. Hashing stays outside the
  write transaction; no schema change, SQL hash function or new identity
  table is required. Identical text/id/session/time is the same notice under
  the existing notice-fingerprint semantics; a different same-second ping is
  not. Events without a digest cannot authorize deletion and are left alone;
  surfaced/acknowledged rows retain normal 14-day cleanup.
- Kept the read-then-delete structure and the 10,000-row case's **21 SELECTs**
  outside its single write transaction. Added a permanent timing probe.
- Added all three reviewer coverage probes. The creation-time race preserves
  the original text so the new text check cannot mask a missing time check;
  a separate same-time/text-change race guards the delete's new identity check.
  The 300-header case preserves every distinct header, deduplicates repeats,
  retains mixed sessions/states and expires all three outcomes. It rendered
  **21,449 bytes / 301 lines** here; the reviewer's 21,358-byte result used a
  shorter archive path.
- Added `#294` to the change log. Corrected the report to identify the reviewed
  committed PR head and distinguish the earlier build's uncommitted state.

### Lookup filesystem promise

Updated by fix round 2: `retention outcomes` opens the original database with
`Store(database, read_only=True)`, as the survey and prune-decision readers do.
Its JSON keeps `read_only=true` and `read_only_scope=records`; the unshipped
`state_root_files` key has been removed.

The lookup writes no records and leaves the main database's bytes unchanged.
SQLite may create or update `state.sqlite3-wal` and `state.sqlite3-shm` for
reader coordination, as C-8.4 permits. It creates no `daemon.lock`, allocates
no database copy and starts no subprocess. A writable Store can open and
commit while the lookup holds its read open, whether the store starts closed
or already has a live WAL writer.

The round-1 private-copy implementation ran only on a closed, checkpointed
store. A small size cap would never copy the hub's reported 9.84 GB store;
an uncapped copy could block daemon startup for minutes and require about
10 GB of free disk. Removal avoids both the copy's locks and the orphaned
helper/partial-copy lifecycle. The round-1 measurements and commands below
are historical evidence for the implementation tested then; fix round 2's
replacement checks and measurements appear at the end of this report.

### Verification and commands

- **Final regression: 1,029 passed in 2,253.62 s (37:33)**, across all fifteen
  requested files plus `tests/unit/test_handler_refusals.py`, with four xdist
  workers and no failures, skips or timeouts. This includes the final TMPDIR
  safeguard and the stronger fallback file-bytes/mtime comparison. No new
  daemon refusal was introduced. CI still owns the whole-suite run.
- Earlier focused checks: **49 passed in 355.72 s** for notice keep and
  **12 passed in 136.23 s** for the lookup before its additional TMPDIR test.
  Searched all tests for `outcome_sha256`, `read_only_scope` and
  `state_root_files`; no existing whole-record expectation was silently
  relaxed. `git diff --check` passes.
- Initial mutation run: **7 controls passed; 7 killed, 0 survived, 0 invalid**,
  taking **740.348 s** of subprocess time. Every kill was a call-phase
  `AssertionError`; there were no collection/setup/teardown failures, and all
  monitored source hashes stayed unchanged. **Final-source confirmation:
  7 controls passed; 7 killed, 0 survived, 0 invalid in 201.185 s**, again with
  only call-phase assertion kills, no other phase failures and unchanged
  monitored source hashes.
- **Isolated expiry check: 1 passed in 12.34 s.** For 10,000 due outcomes,
  expiry took **0.461 s total, 0.071 s inside the write transaction**, with
  **21 read queries**. The same-sized queue with nothing due took **0.066 s**,
  with one read query and no write transaction. This preserves the reviewer's
  approximately 0.41-second overall cost and keeps the write lock shorter than
  its approximately 0.18-second measurement.

| Mutant | Permanent test that kills it |
| --- | --- |
| i: drop 299 distinct headers | `test_three_hundred_headers_and_mixed_state` |
| j: expire at the exact cutoff | `test_expiry_exact_cutoff_is_not_older` |
| k: omit creation-time predicate | `test_row_replaced_between_expiry_read_and_delete_stays[creation-time]` |
| l: omit delete's text predicate | `test_row_replaced_between_expiry_read_and_delete_stays[text]` |
| m: open lookup the old way | `test_outcomes_does_not_create_sqlite_sidecars` |
| n: omit `#294` | `test_changelog_names_pr_294` |
| o: omit event-digest matching | `test_reused_same_second_ping_is_not_expired` |

The creation/text identity and header/boundary killers are in
`tests/unit/test_notice_keep.py`; the sidecar killer is in
`tests/unit/test_retention_outcomes.py`. The reproducible runner is
`tools/notice_keep_mutants.py`; it installs faults in isolated pytest processes
without editing production files.

Commands used after syncing, with `SUBFLEET_LIVE=0`, the permitted TMPDIR below,
`UV_CACHE_DIR=$TMPDIR/uv-cache`, and a fresh `--basetemp` for each pytest run:

```sh
uv run --no-sync pytest -q --tb=short --basetemp "$TMPDIR/baseline-r1c" \
  tests/unit/test_notice_keep.py::test_reused_same_second_ping_is_not_expired \
  tests/unit/test_notice_keep.py::test_expiry_exact_cutoff_is_not_older \
  tests/unit/test_notice_keep.py::test_row_replaced_between_expiry_read_and_delete_stays \
  tests/unit/test_notice_keep.py::test_three_hundred_headers_and_mixed_state \
  tests/unit/test_notice_keep.py::test_changelog_names_pr_294 \
  tests/unit/test_retention_outcomes.py::test_outcomes_does_not_create_sqlite_sidecars

uv run --no-sync pytest -q -s --tb=short --basetemp "$TMPDIR/expiry-r1" tests/unit/test_notice_keep.py
uv run --no-sync pytest -q --tb=short --basetemp "$TMPDIR/lookup-r1" tests/unit/test_retention_outcomes.py
uv run --no-sync python tools/notice_keep_mutants.py --mutants i j k l m n o --output-dir "$TMPDIR/mutants-r1"

uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 --timeout=540 \
  --durations=10 --basetemp "$TMPDIR/final-r1" \
  tests/unit/test_notice_keep.py tests/unit/test_retention_outcomes.py \
  tests/fake/test_notice_keep_delivery.py tests/unit/test_policy.py \
  tests/unit/test_retention_archive.py tests/unit/test_retention_archive_properties.py \
  tests/unit/test_notice_rendering.py tests/unit/test_operator_notices.py \
  tests/fake/test_notice_delivery.py tests/fake/test_notice_header.py \
  tests/unit/test_timers_retention.py tests/fake/test_retention_dormant.py \
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py

uv run --no-sync pytest -q -s --tb=short --basetemp "$TMPDIR/perf-r1-final" \
  tests/unit/test_notice_keep.py::test_expiry_ten_thousand
uv run --no-sync python tools/notice_keep_mutants.py --mutants i j k l m n o --output-dir "$TMPDIR/mutants-r1-final"
```

Logs and JSON phase receipts use those basenames under the permitted TMPDIR.
An earlier alternating six-run
synthetic comparison executed the reviewed implementation from
`git show d4b21a244c43:subfleet/retention_notices.py` and the fix against the same
10,000-row data, in new temporary stores. On this loaded host, wall-time
medians were **2.320 s original / 2.479 s fixed**; write-transaction medians
were **0.450 / 0.713 s**, with original write times ranging **0.389–1.052 s**.
That comparison ran while other tests were active and is retained as a
diagnostic, not the isolated performance estimate above. The read-then-delete
query/transaction counts are unchanged.

All validation completed by **03:04:38 UTC on 2026-11-06**, within the round's
three-hour allowance.

### Environment and deviations

- All work stays in the assigned detached worktree; no live state, caller
  checkout, live process, secrets service or daemon-management command was
  inspected or changed. No push, branch creation or history rewrite occurred.
- `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and its `uv-cache` were attempted;
  the sandbox denied cache creation there. Runs use the permitted child
  `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261105-203022-nk-fix-r1-a1-wtrr_cjd`
  with `UV_CACHE_DIR=$TMPDIR/uv-cache` and fresh basetemps, not `/private/tmp`.
- `uv sync --group dev --extra api` failed because this committed project has
  no `api` extra. `uv sync --group dev` initially hit a PyPI timeout; retrying
  with `UV_HTTP_TIMEOUT=120` succeeded. The first test attempt lacked installed
  Hypothesis; an early helper-return mismatch was corrected before recording
  the valid baseline above. Neither invalid diagnostic is a runtime finding.
- Named-file staging attempts after the reproduction, runtime and final-report
  steps were refused: Git cannot create the parent worktree's `index.lock`
  (`Operation not permitted`), so no commit could be made. No lock protection
  was bypassed. All eight changed/new files, including this completed report,
  remain in the assigned workspace for the hub to collect. HEAD remains the
  supplied `d4b21a244c43`; nothing was pushed.

## Fix round 2

All three P2 findings are fixed. The final requested regression passed
**1,046 tests, with no failures, in 1,385.48 s (23m05s)**. Work started at
13:43 UTC on 2026-11-07 from reviewed head `6382a8abaf52` and finished within
the three-hour limit. Only disposable test state was used; no live state was
read or changed.

- **P2-1/P2-2:** removed the private database copy and helper process. The
  lookup opens the original `Store(database, read_only=True)`. It preserves
  main database bytes and store records, permits SQLite WAL/SHM coordination
  files, and no longer returns the unshipped `state_root_files` key. The hub's
  reasons and current filesystem contract are also in “Lookup filesystem promise.”
- **P2-3:** retirement association requires id, session, creation time and
  text SHA-256 at every caller, with lazy cached hashing. Before editing,
  source tracing confirmed that `NOTICE_COLUMNS`, daemon `SELECT *`, and
  expiry's query all include text. No production code rewrites service-notice
  text after recording its digest; insertion and digest recording share the
  same transaction. `job.pin_noticed` and `_withdraw_pin_notice` retain their
  existing three-field identity.

The copy ran only on a closed, checkpointed store. A small cap would skip the
hub's reported 9.84 GB store; an uncapped copy could block a starting daemon
for minutes and need about 10 GB free. C-8.4 already permits SQLite WAL/SHM
coordination. The helper is removed, with no cap or child lifecycle to tune.
C-8.4, the changelog, lookup docstrings and filesystem-promise section now
reflect this behavior.

Full listing retains its existing `notice_fingerprint` hashes for
ack/withdraw. The lazy-hashing rule concerns association in `pin_notice_jobs`;
pings and pin notices incur no additional association digest.

### Invariants and their tests

| Invariant | Permanent test |
| --- | --- |
| 1. Lookup writes no store records and leaves main database bytes unchanged | `test_outcomes_closed_store_preserves_database_and_creates_only_sqlite_sidecars`; `test_outcomes_with_live_wal_writer_preserves_records_and_creates_no_files`; `test_outcomes_opens_store_read_only_and_sqlite_rejects_writes` |
| 2. A writable Store opens and commits during lookup | `test_outcomes_writer_opens_and_commits_while_original_database_read_is_held[closed-store]` and `[live-writer]`; both establish a real read transaction and verify its snapshot survives the writer commit |
| 3. Retirement association requires id, session, creation time and text SHA-256 | `test_retirement_association_requires_all_four_fields`; `test_retirement_event_without_digest_names_nothing`; `test_property_retirement_identity_matches_a_four_field_reference` (100 Hypothesis examples with collisions, matching and differing texts); `test_different_ping_does_not_name_retired_job` (both hooks); original outcome delivery remains covered by `test_the_outcome_reaches_the_session_on_its_next_delivery` |
| 4. Association hashes at most once per row matching a retirement event on its first three fields, never for other rows | `test_retirement_hashes_only_matching_rows_and_each_at_most_once`; `test_a_newer_pin_event_needs_no_digest_or_hash` |
| 5. Pin-notice associations retain the head's behavior | `test_pin_noticed_associations_match_the_previous_key_function`, a differential against the old key function; `test_newest_matching_event_wins_across_event_kinds`; `_withdraw_pin_notice` is unchanged |

Lookup tests are in `tests/unit/test_retention_outcomes.py`, identity tests in
`tests/unit/test_pin_notice_identity.py`, and hook tests in
`tests/fake/test_notice_keep_delivery.py`. Additional lookup tests reject
subprocesses and temporary copies, assert no helper source names remain, and
permit only WAL/SHM files with no `daemon.lock`.

### Timings

Fixture construction is excluded. The listing has 2,000 unresolved service
notices (1,600 pings, 200 pin notices, 200 outcomes; 2 KiB text). Expiry has
10,000 due outcomes. Five-sample medians in seconds:

| Probe | Before | After | Review r2 |
| --- | ---: | ---: | ---: |
| Listing | 0.079662 | 0.116516 | — |
| Expiry total | 0.321152 | 1.281942 | 0.8024 |
| Expiry write transaction | 0.092266 | 0.491370 | 0.2121 |
| Expiry read queries | 21 | 21 | 21 |

The first after-run was noisy. Five additional interleaved before/after pairs,
using fresh subprocesses, a temporary original-head source copy and the
current worktree source while the four-worker regression ran, measured medians **0.339192 / 0.460510 s** for
listing, **3.288117 / 2.106077 s** for expiry, and **1.012126 / 0.224155 s** in
its write transaction. Listing stayed at 10 reads; expiry stayed at 21 reads
and one write transaction. These measurements do not establish a stable
speed difference. Raw samples are retained in
`2026-11-05-notice-keep-r2-data/{before,after,interleaved}.json`; commands and
source hashes are recorded by the later interleaved and populated-lookup probes.

The populated lookup fixture has **1,075,372,032 database bytes** (1 GiB of
synthetic padding plus schema), one retirement event and valid archive rows.
It returned the archived outcome in **0.904477 s**, beside the review's
**124.19 s for 1,001,869,312 bytes**. A starting writer opened and committed in
**0.107450 s while an explicit read transaction remained open**, without
waiting for lookup close; the held-read lookup including that writer took
0.227164 s. The earlier empty-result probe took 0.197504 s, with a 0.010595 s
writer commit. Both are retained; neither measures a large retirement ledger
or promises live-store latency. See `populated-lookup.json` and `after.json`.

### Commands and verification

All runs set `SUBFLEET_LIVE=0` and use Python 3.14.7. The initial default-cache
sync failed with a sandbox permission error; `uv --cache-dir "$UV_CACHE_DIR"
sync --group dev` succeeded with nine packages installed. The final environment
is `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)/subfleet-attempt-20261106-081758-nk-fix-r2-a1-nzs7fcg2"`
and `UV_CACHE_DIR="$TMPDIR/uv-cache"`; each test invocation gets a fresh basetemp.

- `uv run --no-sync pytest -q --basetemp "$TMPDIR/lookup-focused-1" tests/unit/test_retention_outcomes.py`: **16 passed in 35.11 s**.
- Initial identity/delivery controls, `uv run --no-sync pytest -q --basetemp <task-temp>/identity-controls-1 tests/unit/test_pin_notice_identity.py tests/fake/test_notice_keep_delivery.py`: **17 passed, 2 failed in 89.15 s**. The new ordinary-ping expected text omitted its existing queued-time line; those assertions were corrected.
- The corrected focused command added `tests/unit/test_notice_keep.py` and used `identity-controls-2`: **48 passed, 1 failed in 429.49 s**, then interrupted. All 12 identity and 8 delivery cases passed; an existing property failed to create a temporary directory directly under the sandbox-denied raw Darwin TMPDIR. The final regression uses its permitted child.
- Benchmarks use `.venv/bin/python tools/notice_keep_round2_measure.py` with these arguments (`E=docs/reports/2026-11-05-notice-keep-r2-data`):
  - `--phase before --samples 5 --output "$E/before.json"`
  - `--phase after --samples 5 --lookup-mib 1024 --output "$E/after.json"`
  - `--phase interleaved --compare-ref 6382a8abaf52 --samples 5 --output "$E/interleaved.json"`
  - `--phase populated-lookup --lookup-only --lookup-mib 1024 --output "$E/populated-lookup.json"`
- Final selection: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 --timeout=540 --durations=15 --basetemp "$TMPDIR/final-r2-1" --junitxml "$E/regression.xml"` with all 16 requested files plus `tests/unit/test_pin_notice_identity.py`: **1,046 passed in 1,385.48 s**, exit 0. The [exact command](2026-11-05-notice-keep-r2-data/regression-command.txt), [log](2026-11-05-notice-keep-r2-data/regression.log) and [JUnit receipt](2026-11-05-notice-keep-r2-data/regression.xml) are retained. The whole suite was left to CI.
- `git diff --check` and explicit contract/source checks passed. Searches of tests for `state_root_files`, `read_only_scope` and `outcome_sha256` found only the relevant lookup/retention assertions; removed-key checks now assert absence. No event fields or new refusals were added. Handler refusals are in the final selection.

### Mutation verification

`.venv/bin/python tools/notice_keep_round2_mutants.py --output-dir "$E/mutants"`
ran **6 passing controls and killed all 6 required mutants**, with **0
survivors and 0 invalid kills**. Each fault changes an actual temporary copy
of the source; receipts verify imports from that copy. Every kill is a single
call-phase `AssertionError`, with no collection, setup or teardown failure.
Original source hashes stayed unchanged; no mutant remains in the worktree.
The runner exited 0 and removed its temporary copies. Per-case diffs, logs,
phase receipts and provenance are saved under
`2026-11-05-notice-keep-r2-data/mutants/`.

| Mutant | Test that kills it |
| --- | --- |
| Restore three-field retirement match | `test_retirement_association_requires_all_four_fields[text]` |
| Hash every row | `test_retirement_hashes_only_matching_rows_and_each_at_most_once` |
| Let an event without a digest name its row | `test_retirement_event_without_digest_names_nothing` |
| Require a digest on `job.pin_noticed` | `test_pin_noticed_associations_match_the_previous_key_function` |
| Reintroduce a database copy | `test_outcomes_allocates_no_temporary_database_copy` |
| Open lookup writable | `test_outcomes_opens_store_read_only_and_sqlite_rejects_writes` |

The updated original runner also remains usable:
`.venv/bin/python tools/notice_keep_mutants.py --mutants f m o --output-dir "$E/legacy-smoke"`
passed **3 controls and killed 3 mutants**, with no invalid kills or changed
source hashes. All three tool files passed `py_compile`.

### Deviations and commits

The only execution deviations were the sandbox-denied shared cache/raw temp
root described above and Git's denied index lock. All requested final tests
and six required mutants completed; all owned test, benchmark and mutation
processes ended. No live state, push or history rewrite was involved.

Git did **not** let this workspace commit. Named-file staging attempts for the
lookup, identity and tooling steps were denied when Git tried to create
`/Users/maxghenis/subfleet-v2/.git/worktrees/20261106-081758-nk-fix-r2/index.lock`
(`Operation not permitted`). The final report/evidence staging attempt has
the same restriction. No commit was created; all changes and receipts remain
in the assigned workspace for the hub to collect.

## Fix round 3

Work started at **2026-11-13 12:17:32 UTC**, from `e8d32cca6aef`; the
three-hour cutoff is **15:17:32 UTC**. **1,065 tests passed across 19 files**, with no failures, errors or skips.
Report completed at **2026-11-13 13:43:24 UTC**, within the three-hour limit.

**P2: bounded read snapshots.**
[retention_notices.py:119](../../subfleet/retention_notices.py#L119) replaces
the unrestricted SELECT with keyset pages of at most **1,000 events**. Each
page is an autocommit statement, releasing its snapshot before the next page.
The lookup captures the largest retirement event id once; concurrent events
past that id are excluded, and a later invocation can include them. This
provides a captured ledger prefix, without promising one snapshot across all
pages. [C-8.4:528](../acceptance-contract.md#L528) and its contiguous
changelog entry at line 9 state that behavior and the permission requirement.

[retention_notices.py:81](../../subfleet/retention_notices.py#L81) installs a
SQLite progress handler on the upper-id SELECT and every page, with a
**1-second statement deadline**; the complete lookup, archive reconstruction
included, has a **30-second overall deadline**. Expiry raises
`OutcomesLookupDeadlineError` with `OUTCOMES_LOOKUP_DEADLINE`; it never returns
the pages read so far as complete history. The overall budget permits the
largest measured successful lookup, **25.679227 s**, with **4.320773 s**
headroom. The 1,000-row/one-second limits follow the hub's design and bound
snapshot exposure independently of that overall budget.

The existing `events_kind` index supports both statements: plans show
`SEARCH events USING COVERING INDEX events_kind (kind=?)` for the upper id and
`SEARCH events USING INDEX events_kind (kind=? AND event_id>? AND event_id<?)`
for pages. Session ids exist in JSON with no supporting session index, so pages
scan retirement events across sessions and filter afterward. No index or
schema change was added.

These deadlines are cooperative: SQLite progress and checks between operations
observe expiry. Scheduling or blocking I/O can delay observation. Existing
Store schema/PRAGMA initialization occurs before installing this handler,
using Store's 5-second busy timeout; event queries then use a 1-second busy
timeout. The overall clock starts before opening Store and is checked when
control returns. These are explicit failure budgets, not hard interruption
guarantees for every filesystem operation.

Permanent tests in
[test_retention_outcome_pages.py:53](../../tests/unit/test_retention_outcome_pages.py#L53)
cover page sizes; r3's **6,000 events and requested 1 ms per fetched row** with
the **five-second single-snapshot ceiling**; a real writer checkpoint blocked
within a page and completing with `(0,0,0)` between pages; arrivals excluded
past the captured id and visible next invocation; progress interruption of
both statement types; deadline expiry after a completed page; elapsed busy/I/O
errors; and expiry during archive reconstruction. The delayed reader hit the named statement deadline after **1.048569 s**
with **30 rows fetched**, **one writer commit** and **one checkpoint backlog**;
its snapshot stayed below five seconds and the subsequent TRUNCATE checkpoint
completed with `(0,0,0)`. See the [isolated log](2026-11-05-notice-keep-r3-data/final-r3-17-test_retention_outcome_pages.log).

**Question 19: closed store in a read-only folder.**
[retention_notices.py:320](../../subfleet/retention_notices.py#L320) translates
SQLite's readonly failure into `OutcomesLookupDirectoryError`, a named
`OUTCOMES_LOOKUP_READONLY_DIRECTORY` error. Its message identifies the state
directory and tells the caller to make it writable so SQLite can create
coordination files, or provide usable `state.sqlite3-wal` and
`state.sqlite3-shm`, then retry. The existing ordinary read-only behavior is
preserved: without sidecars, a closed WAL store needs a writable directory.

Three real permission cases in
[test_retention_outcome_permissions.py:61](../../tests/unit/test_retention_outcome_permissions.py#L61)
assert a named/actionable failure in a read-only directory without sidecars,
success in a read-only directory with usable sidecars and no open setup
connection, and success plus WAL/SHM creation in a writable directory without
sidecars. They verify unchanged main database bytes and no daemon lock.
[retention_cli.py:53](../../subfleet/retention_cli.py#L53) catches the named
lookup errors. Six cases in
[test_retention_outcomes.py:241](../../tests/unit/test_retention_outcomes.py#L241)
exercise a real zero-budget lookup through the sole production caller,
`cmd_retention`, and both entrypoints, `cli.main` and `compat.dispatch`, in
text and JSON modes: exit **1**, named deadline on stderr, empty stdout.

**Measurements.** Fixture construction was excluded. Three serial samples
per size requested a session with no matching events; all **12 lookups
succeeded, 0 failed**. Seconds below come from
[measurements.json](2026-11-05-notice-keep-r3-data/measurements.json).

| Retirement events | Complete lookup median | Complete lookup range | Query materialization median | Slowest page |
| ---: | ---: | ---: | ---: | ---: |
| 6,000 | 0.054013 | 0.050893–0.610294 | 0.021132 | 0.013810 |
| 100,000 | 0.764142 | 0.600554–3.118543 | 0.290871 | 0.304296 |
| 1,000,000 | 7.700736 | 5.070311–11.396707 | 3.135441 | 0.222367 |
| 3,000,000 | 13.521442 | 11.631101–25.679227 | 5.639037 | 0.244146 |

r3 measured unrestricted materialization at **1.875/6.971 s** for
one/three million events (complete lookups **3.621/12.639 s**), and
**44.37 s** with the injected slow reader,
holding checkpoints back **44.29 s**. Different loads prevent a speed
conclusion; releasing each page's snapshot is the intended improvement.
An initial probe succeeded at 6,000 events in **1.165777583 s**, maximum page
**0.025455917 s**, then failed explicitly at 100,000 events on the one-second
statement deadline, exit **1**. That aborted probe retained no JSON and is
reported separately from the 12 completed measurements. Listing was untouched
and not benchmarked; question 18 remains out of scope.

**Mutants.** The
[runner receipts](2026-11-05-notice-keep-r3-data/mutants/summary.json) show
**4 passing controls, 4 kills, 0 survivors, 0 invalid**, exit **0**. Each fault
was applied alone to a private source copy, restored from pristine bytes and
checked byte-for-byte and by SHA-256. Worktree source hashes stayed unchanged;
the lookup source hash is
`9535c1dc52144243fbbfd13f3b4d722b8a9a1a51f9552fe5969345de4755dcb1`.
Every kill is a call-phase assertion failure, without collection/setup/teardown
errors. The `LIMIT 1` fault replaces the existing paged limit; bindings were
adjusted so every mutant executes valid SQL.

| Mutant | Killer |
| --- | --- |
| Remove page limit | `test_lookup_pages_never_exceed_1000_events` |
| Drop captured upper id | `test_events_arriving_between_pages_stay_past_captured_upper_id` |
| Return partial history on deadline | `test_overall_deadline_never_returns_pages_read_so_far` |
| r3's `LIMIT 1` | `test_outcomes_cli_has_json_and_readable_offline_output` |

**Commands and verification.** Full available inspection/setup/build commands,
results and staging lists are in
[inspection-commands.md](2026-11-05-notice-keep-r3-data/inspection-commands.md).
Default-cache `uv sync --group dev` exited **2** (`Operation not permitted`);
the permitted `sf-nk-f3` cache fallback exited **0**, installing **9 packages**
with **Python 3.15.2**. Seven files passed `py_compile`; `git diff --check`
passed. Final explicit-cache sync, eight-file compilation, ten-file whitespace
checks, source-hash verification and contiguous-changelog checks also passed;
see [final-checks.json](2026-11-05-notice-keep-r3-data/final-checks.json).
Initial page tests: **8 passed in 119.11 s**, exit **0**.

```sh
.venv/bin/python tools/notice_keep_round3_measure.py --samples 3 --output docs/reports/2026-11-05-notice-keep-r3-data/measurements.json
.venv/bin/python tools/notice_keep_round3_mutants.py --output-dir docs/reports/2026-11-05-notice-keep-r3-data/mutants
.venv/bin/python tools/notice_keep_round3_verify.py --label final-r3 --output-dir docs/reports/2026-11-05-notice-keep-r3-data
```

All three commands exited **0**. The final regression passed **1,065 tests in
2,025.12 s (33m45s)**, with **0 failures, 0 errors and 0 skips**. It runs the brief's **16 files**, `test_pin_notice_identity.py`
and the two new modules, **19 files individually**, at most four workers,
with fresh permitted basetemps. Its
[generated exact commands/results](2026-11-05-notice-keep-r3-data/final-r3-commands.json)
link each log and JUnit receipt. Whole-suite execution remains CI's job.

| File | Passed | Seconds | Exit |
| --- | ---: | ---: | ---: |
| `tests/unit/test_notice_keep.py` | 49 | 122.36 | 0 |
| `tests/unit/test_retention_outcomes.py` | 22 | 57.00 | 0 |
| `tests/fake/test_notice_keep_delivery.py` | 8 | 42.60 | 0 |
| `tests/unit/test_policy.py` | 242 | 73.91 | 0 |
| `tests/unit/test_retention_archive.py` | 109 | 771.29 | 0 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 133.76 | 0 |
| `tests/unit/test_notice_rendering.py` | 40 | 18.05 | 0 |
| `tests/unit/test_operator_notices.py` | 79 | 35.12 | 0 |
| `tests/fake/test_notice_delivery.py` | 15 | 101.08 | 0 |
| `tests/fake/test_notice_header.py` | 24 | 114.98 | 0 |
| `tests/unit/test_timers_retention.py` | 20 | 118.99 | 0 |
| `tests/fake/test_retention_dormant.py` | 7 | 56.77 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 65.33 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 21.82 | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 21.54 | 0 |
| `tests/unit/test_handler_refusals.py` | 360 | 68.22 | 0 |
| `tests/unit/test_pin_notice_identity.py` | 12 | 60.27 | 0 |
| `tests/unit/test_retention_outcome_pages.py` | 10 | 84.14 | 0 |
| `tests/unit/test_retention_outcome_permissions.py` | 3 | 57.85 | 0 |
| **Total: 19 files** | **1,065** | **2025.12** | **0** |

**Deviations and commits.** No test state root outside permitted scratch was needed. Framework caches
were created in the worktree outside scratch; all 13 paths are recorded in
[worktree-cache-cleanup.json](2026-11-05-notice-keep-r3-data/worktree-cache-cleanup.json)
before relocation to guarded `sf-nk-f3` names and deletion. Scratch cleanup completed with guarded `shutil.rmtree`, asserting `sf-nk-f3`
in the path and its exact basename/parent. Its first attempt exited **1**
because an archive fixture left a directory read-only; restoring owner
permissions on that one owned directory allowed retry exit **0**.
[cleanup.json](2026-11-05-notice-keep-r3-data/cleanup.json) records both attempts.
All owned test, benchmark, mutation and cleanup processes were waited to
completion. Permanent evidence remains in the assigned worktree under
`docs/reports/2026-11-05-notice-keep-r3-data/`. Execution deviations were the denied default cache, framework caches outside
scratch, the cleanup retry for a read-only fixture and Git's administrative
index lock. Named runtime staging and later tooling/evidence
staging each exited **128**, denied at
`/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-070657-nk-fix-r3/index.lock`.
The runtime commit attempt exited **1** because unstaged new files were unknown
pathspecs. Final named report/evidence staging also exited **128** at the same lock;
[final-stage.json](2026-11-05-notice-keep-r3-data/final-stage.json) records the
explicit file list and error. Git has produced no commit. Changes remain in the assigned workspace; no live
state, push or history rewrite was involved.

Built head: `e8d32cca6aef761966b8f7db89bd2fff6c6ce320`.
