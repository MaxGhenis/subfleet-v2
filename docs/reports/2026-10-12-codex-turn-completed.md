# D-SHUT1: detached Codex turns must complete

## Evidence and rule

The supplied live-store audit reports that the 2026-10-12 11:38:52Z reboot
ended 40 running Codex attempts, all with rc 0 and no signal. Six had both
`turn.completed` and a non-empty `last.md`; the other 34 had neither and
were nevertheless accepted from 87–408-byte interim progress notes. The
daemon sent those processes no signal, so `killed_by` stayed null and C-9.2's
rule that a signalled attempt is never `ok` could not apply.

Correction (hub, from the attempts table): the reboot's 34 attempts are the
only cut-off turns accepted as a job's result. Each has `killed_by` null;
earlier signalled attempts were not accepted. Among 4,830 genuine `ok`
attempts, every one had both `turn.completed` and a non-empty `last.md`, so
requiring `turn.completed` rejects none of them.
This report uses the supplied evidence; no live store or live process was read.

`CodexAdapter.classify()` now requires this attempt's `turn.completed` event,
rc 0, no `turn.failed`, no spawn error, and a nonblank deliverable for `ok`.
Verified success precedes recovered authentication, admission and quota text.
An unfinished rc-0 turn still passes through
authentication, CLI age, content-filter and quota classification. Otherwise,
it is `transient`, detail **Codex exited before its turn completed**, for
C-4.5's retry. Terminal `turn.failed` classifications remain unchanged.
Evidence records `turn_completed` and `last_event_type`; `why` shows the
latest attempt's recorded cut-off reason and event, and notices keep the reason.

`last.md` alone proves nothing: the 2026-09-24 incident wrote an interim note
there. Deliverable extraction still retains attempt-local text as evidence.
The daemon rechecks completion and terminal failure from this attempt's frozen
stream, including cached `finalization.json`, legacy receipts and restart
recovery. For unfinished detached Codex finalizations, a cached non-`ok` becomes
`ok` when the frozen stream, captured nonblank deliverable, rc-0 receipt and
absence of spawn error or `killed_by` prove verified success. This reconciliation
precedes lane, closure and acceptance effects; recovered textual diagnostics
cannot disable the successful lane or close it. An incomplete cached `ok`
becomes `transient`; a terminal failure or spawn error disqualifies success.
The original verdict remains `provider_verdict` evidence, as for C-9.2's signal
override. Claimed completion fields in a cached verdict cannot replace the
stream's proof. Replays recompute the same class from those same frozen inputs
without repeating classification, attestation or deliverable extraction.
Claude's structured admission verdicts and conversation driver outcomes retain
their authority. Terminal jobs are not retroactively repaired.
`_export_locked()` still requires `accepted_attempt_id` and
exports only that attempt's artifact.

`runs show` renders the accepted result separately from unaccepted artifact
paths. With no accepted attempt, `runs show --out` emits no stdout, reports
“no accepted deliverable recorded” on stderr, and returns 1. The ordinary
form names unaccepted evidence without printing its contents, and returns 1
for a failed, cancelled or lost job. JSON metadata and `--err` remain available.
It never substitutes an earlier attempt or an existing `-o` file for a missing
accepted artifact.

Recovery preserves `next_check_at` for capacity waits whose latest attempt is
transient, including existing waiting rows; ordinary capacity and route waits
still receive their first-pass recheck. The latest-attempt test deliberately
retains future capacity deadlines after a transient even if admission has
already looked again: recovery cannot prove that such a deadline is safe to
shorten. Admission reserves the retry when the saved deadline is due.

Fresh and `codex exec resume` launches use the same classifier and their own
attempt stream. `model_answered()` agrees on the event spelling: positive
usage in `turn.completed` proves the model answered, while completion itself
does not require usage counters. An interim model item can prove answering
without proving completion. Conversation turns in `codex_turn.py` are
unchanged: their driver handles app-server `turn/completed`, plus the existing
`thread/status/changed: idle` settlement after a grace period for tool turns.
They use the recorded driver outcome rather than this detached classifier.

## Invariants and coverage

- **I1:** adapter `ok` implies the attempt stream contains `turn.completed`,
  rc is 0, no `turn.failed` was read, and a deliverable exists. A detached
  daemon `succeeded` implies its own stream contains `turn.completed` through
  every finalization path, including cached and legacy verdicts.
- **I2:** adding `turn.completed` to a stream lacking it never removes `ok`;
  removing completion from an `ok` stream always disqualifies it.
- **I3:** authentication > admission > quota precedence applies to runs without
  verified success, including completed streams with no nonblank deliverable.
  Verified success takes precedence over recovered textual diagnostics.

`tests/unit/test_codex_turn_completed.py` generates event sequences containing
`thread.started`, `turn.started`, agent-message/command-execution
`item.started` and `item.completed`, `turn.completed`, `turn.failed`, and
`error`. Exit receipts range over rc {0, 1, 143, 137}; `last.md` is absent,
empty, interim or final. I1 and I2 each configure 500 Hypothesis examples and
one explicit regression example. I3 has eight completed-stream combinations
and eight incomplete-stream combinations. The fake module exhausts all 48
combinations of fresh classification, cached replay, legacy receipt and restart
recovery; present/absent completion; rc 0/nonzero; and absent/interim/final
`last.md`. Every case checks acceptance and export, with cached classification
forbidden from running again. Restart tests advance a controlled clock through
the middle and end of a 60-second retry backoff, for current and legacy rows.

Examples cover the unchanged success fixture, three synthetic rc-0 cut-off
fixtures without `last.md`, CA/DNS/write-stdin errors, interim and final
last-message files without completion, terminal failure, and native resume.
`git grep -n 'output-last-message\|last.md' -- tests/` identified the detached
fixture runner in `tests/bin/codex`. Its success fixture already emits
`turn.completed`; stream-derived `last.md` now requires that event, preserving
cut-off fixture shape. The deliberately incomplete SIGTERM fake stays
incomplete. `FakeAdapter` keeps its own classifier; its successful provider
and state receipts now carry an explicit `turn.completed` stream event so they also
satisfy the daemon's acceptance rule. Real Codex fixtures retain their own
stream evidence. App-server fakes are unchanged.

## Validation

Fix round 1 completed **22 foreground acquisitions: 21 denied before pytest,
1 acquired**. Each request used its own shared lock with a 60-second queue
timeout. The acquired mutation hold finished within 20 minutes: pytest took
123.00 seconds; the entire acquisition, run and cleanup took 294.88 seconds.
Every requested hold was bounded to 1,100 seconds plus at most 10 seconds of
interruption grace. Each attempt used a fresh `TMPDIR`, cleaned with the
requested `shutil.rmtree` command. All test and mutation runners ended.

**Ordinary pytest counts: 0 executed, 0 passed, 0 failed.** All required files
were requested separately; the shared lock refused each before pytest started.
The main Codex unit, adapter and fake files were requested again after a
mutation acquired the lock; those repeats were denied too. The unchanged
CLI control (11 selected accepted-result and ownership cases) was also denied.
The hub must run these ordinary checks:

| File | Passed | Failed | Status |
| --- | ---: | ---: | --- |
| `tests/unit/test_codex_turn_completed.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_codex_adapter.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_notice_job_state.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_recovery.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_test_module_names.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_cli.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_state_contract.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_finalization_replay.py` | 0 | 0 | Unrun: lock denied |

I1 and I2 still configure 500 generated examples each; **generated examples
executed this round: 0**. The exhaustive daemon matrix has 48 cases; all remain
unrun. The required e2e caller, notice-state and recovery checks remain unrun.

Process-local mutation attempts never edited product files. Removing the
accepted-artifact ownership check produced **4 failed, 4 passed, 178 deselected**
in `tests/unit/test_cli.py`. All four failures were
`test_show_out_never_falls_back_to_unaccepted_artifacts_or_export` cases with
an accepted a2 and an unowned or a1 artifact; they printed interim text and
returned 0. This is an observed mutation failure, **not a confirmed mutation
kill**, because the unchanged control was denied before it could run. The
other seven mutation requests were denied. Expected detectors are identified
below as predictions wherever no test ran:

| Mutation | Detector | Executed result |
| --- | --- | --- |
| Drop adapter completion check | `test_last_message_file_alone_does_not_prove_completion` | Unrun: lock denied |
| Drop daemon completion override | `test_i1_succeeded_requires_completion_on_every_finalization_path[interim-0-False-cached]` | Unrun: lock denied |
| Drop recovery retry deadline protection | `test_restart_in_transient_backoff_preserves_deadline` | Unrun: lock denied |
| Drop CLI accepted-attempt check | `test_show_out_never_falls_back_to_unaccepted_artifacts_or_export` (unowned artifact, no accepted attempt) | Unrun: lock denied |
| Drop CLI artifact ownership check | Same regression, accepted a2 with unowned/a1 artifact | 4 observed failures; unchanged control unrun |
| Accept `last.md` as completion (review mutation) | `test_last_message_file_alone_does_not_prove_completion` | Unrun: lock denied |
| Drop completion evidence fields (review mutation) | `test_cut_off_fixtures_retry_with_completion_evidence` | Unrun: lock denied |
| Classify cut-off fallback as `unknown` (review mutation) | `test_cut_off_fixtures_retry_with_completion_evidence` | Unrun: lock denied |

The reviewer helpers were read from
`/Users/maxghenis/.subfleet/worktrees/20261012-083810-pr195-review-r1/tests/fake/pr195_review_inputs.py`.
Their legacy acceptance observation is now a desired-behavior regression in
the exhaustive finalization matrix. The corrected `runs show` text assertions
are ported with the actual fixture-derived interim text; the restart assertion
now expects the original deadline to survive. The large-stream input is ported
to the unit module. The review's observed unsafe behavior is not kept as a test
expectation.

All ten changed Python files parse, and the committed Python versions compile. All eight process-local mutation
transformations compile without editing product files; compilation does not
establish that a test kills a mutation. `git diff --check` passes.

## Contract and delivery

C-9.2 records the daemon completion check and retained provider verdict.
C-12.6 records the accepted-result rendering and `--out` behavior. C-6.10 now
distinguishes the durable transient retry deadline from ordinary capacity
waits brought forward by recovery.

This change is independent of PR #119. Per the supplied brief, #119 handles
shutdown receipts with rc 143/137 or signal 15/9; D-SHUT1 handles the rc-0,
signal-free receipts it misses and transport errors with the same incomplete
turn shape. The two meet at transient classification and C-4.5 retry; no
#119 changes were imported. Previously finalized false successes are not
retroactively reclassified.

Fix-round commits are on `fix/codex-turn-completed` in workspace-local
`.git-local` metadata, based on assigned head `488e19685db3`. The shared Git
index is outside the writable workspace, and the branch is held by the caller's
checkout. The caller's checkout and its branch were left untouched; no history
was rewritten and nothing was pushed. All commits use the required co-author
trailer. Only named source, test and documentation files were staged; validation
JSON and temporary mutation helpers were not staged.

Code commits in this round:

- `81d527812`: completion proof at daemon acceptance, cached/legacy/recovery
  matrix, large-stream input, and explicit completion in generic fake receipts.
- `95562749e`: accepted-result rendering and CLI/fake/e2e caller assertions.
- `642aa40da`: durable retry deadlines and restart/ordinary-wait regressions.

## Fix round 2

The hub's three fake failures were overstrict expectations, not acceptance
failures. Fresh rc-1 attempts without `turn.completed` retain the adapter's
existing `unknown` class. The 48-case matrix now expects that class while
continuing to forbid acceptance and export; cached `ok` verdicts without
completion still become `transient`. No classifier or contract change was
needed. Commit: `eb80c3f82`.

The three e2e failures came from CLI rendering: `_why_job()` already included
the cut-off explanation in `text`, but `cmd_why()` reconstructed its output
from queue and routing fields and discarded that line. The response now
carries `attempt_explanation`, which the CLI prints alongside routing. The
e2e assertion remains a requirement and now checks attempt a1, its last event,
and text/JSON agreement. Unit regressions cover a routing decision and no
decision; fake coverage also checks that a successful retry clears the
explanation. Commit: `1586baa84`.

Validation used one foreground pytest command per shared-lock hold:

| Files | Result |
| --- | --- |
| `tests/fake/test_codex_turn_completed_fake.py` | **60 passed**, 578.35 seconds |
| `tests/e2e/test_codex_turn_completed_e2e.py`, `tests/e2e/test_notice_job_state.py` | **Unrun**: lock acquisition timed out after 120 seconds, exit 75 |
| `tests/unit/test_codex_turn_completed.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_cli.py` | **Unrun**: lock acquisition timed out after 120 seconds, exit 75 |

The successful hold, including environment setup and cleanup, finished within
20 minutes. Both unsuccessful holds ended before pytest started. Each hold
used its own `sf-r195f2-$$` temporary directory and the prescribed Python
cleanup. No runner remains. The hub must run the unrun checks. All five edited
Python files parse, and `git diff --check` passes.

Commits use workspace-local `.git-local` metadata on
`fix/codex-turn-completed`, based on assigned head `6fbf871696d2`, because the
sandbox denies writes to the shared Git index and refs. The original `.git`
pointer and caller's checkout remain untouched. Only named source, test and
report files are staged; no logs or JSON evidence are committed.

## Fix round 3

Review r2 P1 exposed a pending-finalization inconsistency: the adapter at
`4658ea6d2` recorded `auth-dead` before checking verified success. Restoring
that receipt bypassed the daemon's completion check, which only examined
`ok`. Finalization now reconciles cached non-`ok` detached Codex verdicts with
the frozen attempt evidence before any lane, closure or acceptance effect.
The original `finalization.json` and attestation remain unchanged; when the
deciding class differs, `provider_verdict` retains the original class and
detail. Reconciliation removes a superseded textual closure. The stream scan
records `turn_failed` as well as completion and the last event, so a completion
after a terminal failure cannot establish success.

C-4.3 replay determinism comes from recomputing the decision from the same
frozen raw stream, captured `deliverable.md`, exit receipt and `killed_by`
record on each unfinished finalization. The adapter is not reclassified and
an existing deliverable is not extracted again. The regression interrupts
before acceptance, observes `ok` on both executions, then changes stdout and
`last.md` to prove that frozen evidence still decides. A further call after
acceptance leaves the terminal attempt and its single notice untouched.
Claude's structured rejection remains authoritative; the reconciliation only
applies to detached Codex. P3 is corrected above: I3's diagnostic precedence
applies to runs without verified success.

The review's two authentication sources (`error` event and stderr) are ported
across fresh classification, immediate replay and restart recovery. Their
serialized legacy outcomes were independently compared with the actual
`4658ea6d2` adapter and matched in both cases. The four replay/recovery inputs
failed their desired-success assertions against the exact assigned
`b4d8a8719` daemon, while both fresh controls passed. At the fixed head, all
six pass: the attempt is accepted, `Finished.` is exported, the lane remains
enabled, and no closure is added.

Direct validation results (isolated Python inputs, **not pytest counts**):

| Input | Passed | Failed |
| --- | ---: | ---: |
| Exact `b4d8a8719` daemon: fresh controls and replay/recovery regressions | 2 | 4 expected |
| Fixed daemon: the same six inputs | 6 | 0 |
| Fixed daemon: terminal failure, receipt/cached spawn error, daemon signal, nonzero rc, blank capture, missing completion | 7 | 0 |
| Legacy serialization comparisons, terminal-stream proof, structured rejection, module names | 5 | 0 |
| Disposable mutant with only the reconciliation block removed | 2 | 4 expected |

Thus **18 direct checks pass at the fixed head**. The disposable mutant uses
the same current regressions; all four replay/recovery cases catch removal of
the reconciliation, while both fresh controls still pass. Its source was
verified to differ from the fixed daemon by exactly that block. The baseline
source hash was verified against Git's assigned commit. Direct state inputs
stub process identity, containment and launches; isolate state and desktop
paths; disable the desktop prober; and reject network calls. They start no
provider, guardian or daemon serve loop.

Foreground pytest requests each use the shared lock, a fresh `sf-r195f3-$$`
temporary directory, a 60-second acquisition timeout and a 1,100-second total
bound with at most 10 seconds of termination grace. Each runner cleans its
temporary directory with the prescribed Python/shutil command. Only one
pytest acquisition request runs at a time.

| Requested files | Pytest result |
| --- | --- |
| Six baseline replay/fresh inputs in `tests/fake/test_codex_turn_completed_fake.py` | Unrun: lock denied, exit 75 |
| `tests/unit/test_codex_turn_completed.py` | Unrun: lock denied, exit 75 |
| `tests/fake/test_codex_turn_completed_fake.py` | Unrun: lock denied, exit 75 |
| `tests/fake/test_finalization_replay.py` | Unrun: lock denied, exit 75 |
| `tests/fake/test_state_contract.py` | Unrun: lock denied, exit 75 |
| `tests/unit/test_empty_provider_results.py`, `tests/unit/test_limit_from_model_text.py` | Unrun: lock denied, exit 75 |
| `tests/unit/test_test_module_names.py` | Unrun: lock denied, exit 75 |

All **seven acquisition requests timed out before pytest started** (60.73–61.49
seconds each). No shared-lock hold occurred. **Pytest counts: 0 executed,
0 passed, 0 failed.** The requested full suites must be run by the hub.
Other full adapter, CLI and e2e suites were not requested or run
in this round. Four edited Python files pass AST parsing and `git diff --check`
passes. All runners ended; their temporary directories, disposable baseline
and mutant, and untracked validation helpers/logs were removed with Python/shutil.

Commits use workspace-local `.git-local` metadata on
`fix/codex-turn-completed`, based on `b4d8a87192c6`; shared Git metadata is
outside the sandbox's writable roots. The original `.git` pointer and caller's
checkout remain untouched. Every commit carries the requested co-author
trailer, and only named source, test and report files are staged.

- `1bbe899d3`: port the six review regression/control inputs.
- `9d2485f71`: interrupt before acceptance on both success and retry paths.
- `dd27b6a08`: reconcile pending verdicts and guard the frozen success evidence.
- `1346fc157`: pin replay consistency and captured-evidence ownership.

## Fix round 4

Review r3 P1 found that `_stream_path` skipped an existing zero-byte frozen
stream. Reconciliation then scanned changed live stdout, promoting a cut-off
cached `auth-dead` verdict to `ok` and exporting its captured interim text.
The selector now uses regular-file presence, regardless of size. Its three
readers (`completion_evidence`, `classify`, and stream-based `deliverable`)
share that rule; none adds a size-based fallback. Missing frozen artifacts
retain the legacy stdout fallback.

The regression ports the review's empty stdout, rc-0 signal-free receipt,
nonblank interim `last.md`, revoked-token stderr and cached `auth-dead` verdict.
It interrupts `_finalize` before acceptance, freezes an empty stream and the
interim deliverable, then appends `turn.completed` to stdout only. Both replay
and restart must keep `auth-dead`, leave acceptance/export absent, and preserve
all frozen files byte-for-byte. Classification, attestation and captured
extraction are forbidden from repeating. Authentication rejection retains the
existing C-4.5 behavior: disable that lane and wait without counting the attempt.
Four unit cases exercise all stream readers, including custom and legacy raw
paths; three controls verify fallback when the frozen artifact is absent.

The new replay and restart assertions fail against the exact assigned
`e3a3aeef1` production code: **2 failed, 73 deselected**, 4.22 seconds. Both
observe `auth-dead` followed by `ok`, after verifying that the frozen inputs
are unchanged. At the fixed head both pass in the full fake suite.

Restoring only the size predicate in a disposable copy produces **2 failed,
73 deselected**, 30.84 seconds, at the same deciding-class assertions. The
mutant selector's AST matches the exact assigned selector; the tracked fixed
source is never changed. This confirms both regressions kill the mutation.

Each pytest run uses its own foreground `/usr/bin/lockf -k` acquisition of
`/private/tmp/claude-501/subfleet-suites.lock`, followed by `uv run pytest -q
-p no:cacheprovider`. Only one pytest acquisition request runs at a time. Each request
uses a fresh `sf-r195f4-<pid>/` TMPDIR and a 1,100-second total bound with at
most five seconds of termination grace; the final extra-control retry also
sets a 60-second acquisition timeout. The prescribed Python/shutil command
removes that TMPDIR even on failure. No waiting for host load occurs under
the lock.

| Required file | Passed | Failed | Pytest seconds | Acquisition/run/cleanup seconds |
| --- | ---: | ---: | ---: | ---: |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 10.71 | 19.13 |
| `tests/fake/test_codex_turn_completed_fake.py` | 75 | 0 | 30.27 | 186.61 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 6.44 | 11.94 |
| `tests/unit/test_codex_adapter.py` | 93 | 0 | 43.72 | 897.37 |

Thus all four required files execute: **219 passed, 0 failed**. The completion
unit suite has one existing pytest deprecation warning about zip-based
parametrization. The first fixed fake run had **73 passed, 2 failed** solely
because the new assertion expected terminal failure instead of C-4.5's
waiting state; both deciding classes were already `auth-dead`. Commit
`019183173` corrects that assertion and also checks that the lane is disabled.
The corrected full run passes all 75 cases. Baseline and mutation requests
finish in 19.09 and 163.35 seconds respectively, including cleanup.

Round 3's four authentication replay/restart regressions, both fresh controls,
seven success-proof guards, all 16 I3 combinations and the full 48-case daemon
matrix pass within the required suites above.

A disposable pytest wrapper also prepared the review's 95 passing direct inputs
plus one exact legacy-serialization comparison: **96 additional checks unrun**.
The first request reached its 1,100-second total bound before pytest started
(empty log), ending in 1,101.48 seconds with rc -15. One final request used
`lockf -k -t 60` and was denied before pytest started: exit 75, 61.36 seconds.
Neither request acquired a hold or contributed pytest counts. This broader
wrapper includes the review's independent frozen-evidence boundaries, quota
and stale-closure checks, structured rejections, terminal-job exclusions, and
previous-finalizer/process-local controls. Their full repeat remains unrun;
the required suites' passing controls are not presented as 95 direct checks.

Broader e2e, process, CA, state-contract, CLI and module-name suites are not
rerun in this round. All three edited Python files parse, and `git diff
--check` passes. All runners ended. Every requested TMPDIR was removed with Python/shutil;
the disposable mutant, review helpers and validation logs were also removed.
Seven actual shared-lock holds completed; each entire acquisition/run/cleanup
finished below 20 minutes. The two additional acquisition failures held no lock.

Commits use workspace-local `.git-local` metadata on
`fix/codex-turn-completed`, based on assigned `e3a3aeef1c3a`, because the shared
index and refs are outside the writable sandbox. The original `.git` pointer
and caller's checkout remain untouched. Only named source, test and report
files are staged; logs, JSON evidence, dependencies and disposable helpers are
excluded. Every commit has the required co-author trailer. No remote, sub-agent,
live store or live process was accessed.

- `d4ecc02b0`: port the empty-stream replay/restart regression and reader controls.
- `ce98ef169`: make empty frozen streams authoritative.
- `019183173`: retain authentication rejection's waiting/disabled-lane behavior
  in the regression assertion.

## Hub fix round 4: the first full CI run

#195 had no full CI run until #191 (`8fb1a8239`), which contains this branch
and failed 112 tests on Python 3.14. The hub's host bisect over the seven
worst files found 279 passed at release `f3e042953` and at #142 `01d0cb9d5`,
and 64 failed at this branch's `323ba4d08`. There were three causes, each
fixed at its root, plus two test expectations this PR's rules supersede.

### Codex-lane fixtures without completion proof

`_finalize` checks C-9.2's completion proof on every detached attempt on a
lane whose provider is `codex`, reading the attempt's frozen stream
(`raw_stream_path`, then `stream.jsonl`, then stdout). Three fixtures model
Codex lanes but recorded no stream, so their stubbed `ok` became `transient`:

- `tests/unit/test_host_push.py::accept` is shared by the export, intake,
  names, restart, retention, trees and mutation files. It enrolls
  `Lane("test-lane", "codex", …)`, submits `pinned_model="astra"`, stubs
  `classify` as `ok`, and wrote only `exit.json`. Push jobs therefore ended
  `waiting`. The 13 `test_host_push_mutations.py` failures ("Regex pattern
  did not match") had the same cause: each mutation's invariant calls
  `accept`, which stopped before the mutated check could fail.
- `tests/unit/test_daemon_settle.py::with_launch` enrolls `codex-1`, whose
  stub classifies every exit `ok`, with a stdout of `hello`.
- `tests/fake/gate_peer.py` runs the Astra peer on a Codex lane and wrote
  only its verdict to stdout.

These are Codex lanes, so the rule must apply to them, and a real Codex
success always carries `turn.completed` in its own stream. Each fixture now
writes it, as `receipt_fixture`, `fakeprov` and the #185 census test already
did. The gate peer publishes its own `stream.jsonl`, which is the launch's
raw stream. This needs no product change. No other provider reaches the check,
because it is keyed on the lane's provider and `FakeAdapter` declares
`provider = "codex"`.

### Replaying this daemon's own verdict

Fix round 3 reconciled every cached non-`ok` detached Codex verdict with the
frozen evidence. The target was a verdict from an older classifier that had
ranked an authentication message ahead of verified success. But
`finalization.json` is also this daemon's own cache. `_finalize` writes it
before salvage, and a salvage that times out raises, so the worker finalizes
the attempt again (C-13.1). In `test_salvage_finalization.py` the adapter
classifies a1 `transient`. `receipt_fixture`'s completed stream and
`fixture result` deliverable meet every reconciliation condition, so the
replay turned the first pass's `transient` into `ok`, and the job succeeded
instead of retrying. The same frozen inputs produced two classes, which
violates C-4.3 whether or not a salvage is involved.

A verdict this daemon classifies is now written with
`"rule": "verified-success-first"` (`FINALIZATION_RULE`), and every replay
keeps it. Only a cached verdict without `rule`, which an older daemon
recorded, is reconciled, under the unchanged conditions.

The salvage rules are unchanged: a salvage failure never decides success,
and the retry stands because a1 was `transient`. Nothing changes for the
Codex adapter's fresh verdicts. Since #186 it returns `ok` for exactly the
inputs that satisfy reconciliation, so a replay of its own verdict never
needed reconciling. The pre-merge replay regressions write their cached
verdicts without `rule` and are unchanged. These are
`test_completed_success_survives_premerge_auth_receipt`, the seven cached
reconciliation guards and the empty-stream regression.

`test_replay_keeps_this_daemons_verdict_and_reconciles_only_an_older_one`
crosses three adapter verdicts (`transient`, `auth-dead`, `unknown`), both
recorders, and replay or restart, all on a stream that proves success. This
daemon's own verdict is observed on both passes and never accepted. With
`rule` removed, the same verdict becomes `ok`, is accepted and exported, and
keeps `provider_verdict`.

### `runs show` compatibility

Fix round 1 stopped `runs show` from printing unaccepted interim text, but it
also changed output that callers parse:

- The bare form dropped `--- out.md ---` and printed prose after the
  metadata object. `json.loads(out.split("--- out.md ---")[0])` therefore
  failed with "Extra data" (`test_mcp_cli_contract.py`, `test_offline.py`).
  C-17.1 keeps v1's shape, and v1 printed that line unconditionally
  (`test_compat.py`).
- `--out` reported `no accepted deliverable recorded` instead of
  `no deliverable recorded`.
- An accepted job with no deliverable row lost its `-o` export
  (`test_notice_rendering.py`, C-8.3).

Compatibility is restored and review r1's guarantee is kept:

- The bare form prints the metadata object, `--- out.md ---`, and then the
  accepted deliverable or nothing. It names unaccepted evidence paths on
  stderr, and still returns 1 for a failed, cancelled or lost job without a
  result.
- `--out` reports `no deliverable recorded`.
- An accepted job with no deliverable recorded for any attempt shows its `-o`
  export, unless `export_error` says that export failed. This is the case of
  a v1 import that kept only its export.
- A deliverable recorded only for another attempt is never the result, and
  in that case neither is `-o`. Review r1's eight cases still return 1.
- `--json` was never affected.

Test changes, each made because this PR's rules supersede the expectation or
the fixture was impossible:

- This PR's own `runs show` assertions (`test_cli.py`, the fake and e2e
  cut-off tests) now expect the restored phrase and shape.
- `test_offline.py`'s succeeded fixture job now records
  `accepted_attempt_id`. `_finalize` and the importer always set it together
  with `succeeded`, and C-12.6 gives a job without it no result.
- `test_a_deliverable_on_disk_is_found_without_its_artifact_row` printed the
  unrecorded `deliverable.md` of a running job through `--out`. `_finalize`
  publishes `deliverable.md` before the acceptance transaction that records
  its row, so such a file is unaccepted by construction. On 2026-10-12, 34
  cut-off turns left exactly such files. C-12.6 and C-9.2 forbid printing it
  as the result. The test now checks that offline metadata still names it
  (`deliverable_path`) and that `--out` returns 1 with
  `no deliverable recorded`.

Two new unit tests pin the bare shape and the `-o` fallback. A direct check
outside pytest, with no daemon, confirmed the five `runs show` paths before
any pytest hold was available.
