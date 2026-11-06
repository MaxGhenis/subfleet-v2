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

`retention outcomes` writes no store records. Its JSON keeps `read_only=true`
and explicitly narrows that to `read_only_scope=records`; `state_root_files`
reports `unchanged` or `sqlite-coordination`.

- **Closed, checkpointed store without WAL/SHM:** a short-lived child acquires
  SQLite's Unix byte-range locks without touching `daemon.lock`, verifies
  there is no WAL/recovery journal, and copies the database into private
  temporary storage. The lookup reads that copy and creates nothing in the
  state root. The test compares all state-root file bytes and mtimes.
- **Live WAL writer:** the exclusive copy lock cannot be acquired, so ordinary
  SQLite read-only WAL coordination reads committed events, including events
  still in the WAL. The test gets the correct archived outcome, creates no new
  state-root files and leaves the ledger unchanged while `daemon.lock` is held.
- **Writer opening during the read:** after the locked copy is complete, the
  writer opens and changes the original while the lookup queries its private
  snapshot. The lookup returns the correct pre-write outcome; a subsequent
  lookup sees the writer's committed change.
- **Narrower concurrent fallback:** if a busy writer closes between the copy
  attempt and the SQLite read-only open, that open can create
  `state.sqlite3-wal` and `state.sqlite3-shm`. An existing WAL or unavailable
  write-lock permission also selects this safe fallback. If TMPDIR is inside
  the state root, the lookup skips temporary allocation entirely and selects
  the same fallback; a test pins that protection too. These are SQLite's
  recovery/reader-coordination files, not new store records. A deterministic
  race test pins this explicitly permitted exception. The lookup never
  deletes state-root files, acquires `daemon.lock`, or uses immutable mode on
  a potentially live database. The contract and docstring now say this rather
  than promising unconditional filesystem purity. A separate process is
  necessary because POSIX record locks belong to a process, and the caller
  may already have a Store open.

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
