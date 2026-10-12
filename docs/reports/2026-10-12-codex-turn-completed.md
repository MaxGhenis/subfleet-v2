# D-SHUT1: detached Codex turns must complete

## Evidence and rule

The supplied live-store audit reports that the 2026-10-12 11:38:52Z reboot
ended 40 running Codex attempts, all with rc 0 and no signal. Six had both
`turn.completed` and a non-empty `last.md`; the other 34 had neither and
were nevertheless accepted from 87–408-byte interim progress notes. The
daemon sent those processes no signal, so `killed_by` stayed null and C-9.2's
rule that a signalled attempt is never `ok` could not apply.

Correction (hub, from the attempts table): the build brief also cited 173
`ok` attempts without `turn.completed` since 2026-09-25. That figure read the
adapter's verdict, which `finalization.json` keeps even when the daemon
overrides it. 138 of the 139 before the reboot are recorded `unknown` and
`interrupted` by the daemon, with `killed_by` `max_wall_s` (79) or `operator`
(59), and the remaining one (2026-09-25, operator kill) is on a cancelled job
with nothing accepted. All 139 jobs are `cancelled` with no accepted attempt.
The reboot's 34 are the only cut-off turns accepted as a job's result. Among
4,830 genuine `ok` attempts, every one had both `turn.completed` and a
non-empty `last.md`, so requiring `turn.completed` rejects none of them.
This report uses the supplied evidence; no live store or live process was read.

`CodexAdapter.classify()` now requires this attempt's `turn.completed` event,
rc 0, no `turn.failed`, and a non-empty deliverable for `ok`, with the existing
spawn and authentication checks. An unfinished rc-0 turn still passes through
authentication, CLI age, content-filter and quota classification. Otherwise,
it is `transient`, detail **Codex exited before its turn completed**, for
C-4.5's retry. Terminal `turn.failed` classifications remain unchanged.
Evidence records `turn_completed` and `last_event_type`; `why` shows the
latest attempt's recorded cut-off reason and event, and notices keep the reason.

`last.md` alone proves nothing: the 2026-09-24 incident wrote an interim note
there. Deliverable extraction still retains attempt-local text as evidence.
Finalization accepts only `ok`; `_export_locked()` requires
`accepted_attempt_id` and exports that attempt's artifact. No acceptance or
export gate needed changing. Tests cover preserved existing `-o` content,
replay, retry scheduling, a completed retry's own output, and an empty failed
retry that cannot inherit its predecessor's note.

Fresh and `codex exec resume` launches use the same classifier and their own
attempt stream. `model_answered()` agrees on the event spelling: positive
usage in `turn.completed` proves the model answered, while completion itself
does not require usage counters. An interim model item can prove answering
without proving completion. Conversation turns in `codex_turn.py` are
unchanged: their driver handles app-server `turn/completed`, plus the existing
`thread/status/changed: idle` settlement after a grace period for tool turns.
They use the recorded driver outcome rather than this detached classifier.

## Invariants and coverage

- **I1:** `ok` implies the attempt stream contains `turn.completed`, rc is 0,
  no `turn.failed` was read, and a deliverable exists.
- **I2:** adding `turn.completed` to a stream lacking it never removes `ok`;
  removing completion from an `ok` stream always disqualifies it.
- **I3:** authentication > admission > quota precedence is unchanged when
  `turn.completed` is present.

`tests/unit/test_codex_turn_completed.py` generates event sequences containing
`thread.started`, `turn.started`, agent-message/command-execution
`item.started` and `item.completed`, `turn.completed`, `turn.failed`, and
`error`. Exit receipts range over rc {0, 1, 143, 137}; `last.md` is absent,
empty, interim or final. I1 and I2 each configure 500 Hypothesis examples and
one explicit regression example. I3 has eight completed-stream combinations
and eight incomplete-stream combinations. **Hypothesis examples executed: 0.**

Examples cover the unchanged success fixture, three synthetic rc-0 cut-off
fixtures without `last.md`, CA/DNS/write-stdin errors, interim and final
last-message files without completion, terminal failure, and native resume.
`git grep -n 'output-last-message\|last.md' -- tests/` identified the detached
fixture runner in `tests/bin/codex`. Its success fixture already emits
`turn.completed`; stream-derived `last.md` now requires that event, preserving
cut-off fixture shape. The deliberately incomplete SIGTERM fake stays
incomplete. `FakeAdapter` has its own classifier and does not emulate the
detached Codex JSON protocol. App-server fakes are unchanged.

## Validation

Syntax compilation of the changed Python paths and `git diff --check` passed.
The final static check parses all nine changed Python files and verifies the
three synthetic fixture directories and the restored adapter source.
Pytest and mutation commands used the shared lock, a task-specific `TMPDIR`,
and `UV_CACHE_DIR` inside the workspace. Each lock acquisition had a 60-second
queue timeout; temporary directories were cleaned with `shutil.rmtree`.
No pytest process started, no lock was held, and no runner was left behind.

**Pytest counts: 0 executed, 0 passed, 0 failed.** The shared lock denied all
six acquisitions (four core attempts, one e2e attempt and one mutation attempt)
with exit 75, `already locked`. All of these
files remain unrun and require the hub's host test run:

```text
tests/unit/test_codex_adapter.py
tests/unit/test_codex_turn_completed.py
tests/fake/test_codex_turn_completed.py
tests/fake/test_finalization_replay.py
tests/fake/test_notice_header.py
tests/fake/test_resume_contract.py
tests/fake/test_resume_cli.py
tests/process/test_codex_isolation.py
tests/e2e/test_codex_turn_completed.py
tests/e2e/test_milestone1_codex.py
tests/e2e/test_notice_job_state.py
tests/e2e/test_guard_and_isolation.py
tests/e2e/test_recovery.py
tests/e2e/test_stop_bound.py
tests/e2e/test_conversation_wakes_e2e.py
tests/e2e/test_review_r2_strand_e2e.py
tests/e2e/test_review_r3_strand_timing_e2e.py
tests/e2e/test_conversations.py
```

The mutation attempt removed only `and turn_completed` from the success
condition, leaving all other code intact, and requested
`tests/unit/test_codex_turn_completed.py` under its own lock acquisition.
The lock denied it before pytest started; the original condition was restored
in `finally` and verified against the committed adapter. **Mutation failures
observed: none; the mutation check is unrun.** From the assertions, expected
failures include the completed-item/error cut-off fixtures, last-file-only
cases, rc-0 transport errors, incomplete resume, incomplete admission/quota
cases, I1 and I2. This is a prediction, not an executed mutation result.

## Contract and delivery

C-9.2 and C-12.6 record this incident and the completion rule. Existing
sentences are retained except the now-conditional daemon `ok` override wording
and the empty-deliverable wording, which must distinguish an unfinished
Codex turn (`transient`) from a completed empty turn (`unknown`).

This change is independent of PR #119. Per the supplied brief, #119 handles
shutdown receipts with rc 143/137 or signal 15/9; D-SHUT1 handles the rc-0,
signal-free receipts it misses and transport errors with the same incomplete
turn shape. The two meet at transient classification and C-4.5 retry; no
#119 changes were imported. Previously finalized false successes are not
retroactively reclassified.

Commits are on `fix/codex-turn-completed` in workspace-local `.git-local`
metadata, based on `24be965c5ce8`. Shared Git metadata is outside the writable
workspace, and the branch is held by the caller's checkout. That checkout and
its branch were left untouched; no history was rewritten and nothing was
pushed. All commits use the required co-author trailer. Only named source,
test, synthetic-fixture and documentation files were staged.
