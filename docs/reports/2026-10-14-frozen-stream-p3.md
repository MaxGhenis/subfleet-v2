Frozen provider stream P3 follow-ups, 2026-10-14

## Round 1 (historical; authority rule superseded below)

Base: `75597f81bb3b` (release/217, #209 merged). All three review findings are
fixed; work stayed in the assigned checkout. No network Git remotes, sub-agents
or pushes were used.

Finalization atomically copies every regular stream with `st_nlink > 1` into a
fresh inode before classification or replay. This conservative rule is smaller
than maintaining a provider-specific list of writable artifacts: it covers all
hard-linked aliases, including stdout, stderr and last.md. A raw path naming
stdout itself is live; its snapshot is captured at canonical `stream.jsonl`
and reused on replay. Under C-9.2, later writes through an alias therefore cannot
add completion proof and turn a cached rejection into an accepted interim answer.

The `raw-stream` artifact now records the authoritative regular candidate,
including the canonical file beside an absent custom launch path, with its path,
byte count and SHA-256. Non-regular entries still supply no events or artifact.
An entry swapped between inspection and open maps ENXIO, EOPNOTSUPP (102 on
Darwin), ENOTSUP, ELOOP, EISDIR and EINVAL to `NotRegularFile`. Other I/O errors
still propagate. C-4.3, C-8.2, C-9.2, C-12.6 and version history were updated.

Regressions were committed before production fixes. Against unchanged base
production code, the corrected baseline selection had **20 expected failures
and 44 passing controls**: six writable-alias replay/restart failures, four
canonical artifact omissions and ten socket/error-mapping failures. The review's
cached auth-dead, rc-0 and captured-interim input now preserves its rejection and
previous export, including a writer descriptor held across atomic publication.
The frozen generator includes hard links to actual stdout and Unix sockets.
Bounded properties cover frozen authority, fallback equivalence and refusal to
open non-regular entries; streams contain at most ten events.

Final validation: **987 passed, no failures or skips**, across all requested files.

| File under `tests/` | Passed |
| --- | ---: |
| `unit/test_frozen_provider_streams.py` | 125 |
| `fake/test_frozen_stream_nonregular.py` | 46 |
| `unit/test_codex_turn_completed.py` | 41 |
| `fake/test_codex_turn_completed_fake.py` | 105 |
| `fake/test_finalization_replay.py` | 10 |
| `fake/test_salvage_finalization.py` | 47 |
| `unit/test_claude_adapter.py` | 170 |
| `unit/test_empty_provider_results.py` | 334 |
| `unit/test_sessions_transcripts.py` | 71 |
| `fake/test_state_contract.py` | 37 |
| `unit/test_test_module_names.py` | 1 |

The five final foreground pytest runs used separate shared-lock holds, taking
9.60, 20.30, 36.73, 7.78 and 5.93 pytest seconds. Every hold stayed below 20
minutes; tests ran serially with `HYPOTHESIS_PROFILE=ci`, disposable TMPDIR and
Hypothesis storage, and the cache provider disabled. Temporary evidence was
removed with Python `shutil.rmtree`; no `.hypothesis` folder remains.
Dependencies were installed with `uv sync --locked`; Python 3.14.7 freethreaded
ran with `PYTHON_GIL=1`. The sole warning is the existing Codex `zip`
parametrization deprecation. Broader full, process, e2e, live suites and CI were
not run. `git diff --check` passes.

Shared Git metadata lies outside the writable workspace. Commits use
`.git-local/repository.git` on `fix/frozen-stream-p3`, borrowing base objects
read-only; the original `.git` pointer and detached HEAD are preserved. Only
named source, test and documentation files were staged, and every commit has
the requested co-author trailer. Delivery is the ignored, unstaged
`docs/reports/2026-10-14-frozen-stream-p3.bundle`, with prerequisite
`75597f81bb3b` and the workspace-local branch. No logs, JSON evidence or bundles
were staged.

## Round 2: review r1, recovered through `7974068bc`

Reviewed base: `e0d50b1c8`; review: `~/reviews/subfleet-2110/frozen-stream-p3/review-r1.md`.
The recovered regression commits `57a19c3b0`, `3e4817d04`, `34bdd142a`,
`a61ab5c84`, and `f9a877f70`, followed by production fix `440e4751b` and the
additional regressions at `7974068bc`, were preserved. The cold-boot patch
contained only this unfinished report section; it applied cleanly. Inspection
confirmed that the production fix already covers both P2s, both P3s, and chunked
copying. Report recovery and baseline findings were committed as `576311f59`.
This continuation adds regression coverage and completes validation and reporting.

Round 2 replaces round 1's link-count rule with descriptor identity. At first
capture, the candidate is opened once and its `(st_dev, st_ino)` is compared
with the resolved stdout, stderr, raw path, and Codex `last.md` destinations.
Each regular candidate is copied from that descriptor into a fresh file,
published with file fsync, rename, and directory fsync. Copying every candidate
also covers an unlinked inode still held by a provider's writable descriptor
and mutations after the identity check. Neither names nor link counts prove
that the provider can no longer change an inode.

C-9.2 requires completion proof from the attempt's own frozen evidence before
acceptance. A file the provider can still write means an inode reached through
its output destinations or retained writable descriptors, including directory
aliases, hard links, and an unlinked open writer. C-4.3 and C-12.6 now connect
that requirement to the fresh copy. `stream.freeze.json` records the copy's
identity and digest; replay and restart reuse that copy. A missing, replaced,
changed, or newly writable copy invalidates cached classification and extraction
together, so a new capture is classified and extracted from scratch.

Finding dispositions:

- **P2: stdout aliases.** Directory aliases and `raw = stdout = stream.jsonl`
  receive independent copies. Appended completion cannot accept cached interim
  text on replay or restart.
- **P2: link-count authority.** Links to the original candidate, unlinked writers,
  and a link introduced at `fstat` cannot change the recorded copy.
  Publication-time links to a new copy trigger another fresh copy from the same
  source descriptor. A later link from stdout to the published copy invalidates
  its authority: replay/restart classify and extract again rather than retain
  the cached result. The continuation adds both replay and restart regressions
  for this case, retaining the copy's inode while writing through the new link.
- **P3: mode-000 directory replacement.** EACCES/EPERM becomes `NotRegularFile`
  only when reinspection proves a non-regular replacement; genuine regular-file
  permission errors propagate.
- **P3: post-check ENOENT.** ENOENT on a still-present regular entry propagates,
  as do EIO and EACCES; genuine disappearance captures no events.
- **P3: copy memory.** Copying, digest verification, and artifact hashing use
  reads of at most 1 MiB. The regression counts candidate opens by inode and
  rejects an unbounded read.

Recovered test commit: `7974068bc` (`test: cover directory aliases and stream
open error controls`, recovered from `d68991170`). Deterministic cases and the
bounded generated property now include directory aliases at every freeze
boundary: before open, after open, `fstat`, read, file fsync, rename, directory
fsync, and after freeze.
The ENOENT/EIO/EACCES cases cover both providers. Permission controls also cover
EPERM, explicitly distinguish real permission errors from `NotRegularFile`, and
exercise a mode-000 replacement through the real kernel open.

Continuation test commit: `5625e2b82` (`test: reclassify copies linked to stdout
after publication`). This adds two cases to the existing invalid-copy test,
covering both in-process replay and daemon restart without replacing the
published inode. No further production changes were necessary.

The recovered test edit in `57a19c3b0` was incomplete: in
`test_completed_success_survives_premerge_auth_receipt`, it reused `frozen` for
the stream `Path`, overwriting the saved verdict JSON string. The first full
Codex pair run therefore had 4 failed and 142 passed tests: all four failures
were the final text-versus-Path comparison, after acceptance and export checks
had passed. Commit `c31ca5d0e` gives the stream path its own `frozen_stream`
variable and preserves the original verdict immutability assertion.
The full Codex pair rerun passed all 146 tests.

Baseline verification used a workspace-local archive of `e0d50b1c8`, with only
the current regression tests overlaid. The selected 95 deterministic cases
produced **79 expected failures and 16 passing controls** in 6.10 seconds.
All executed review inputs failed: directory aliases, `raw = stdout =
stream.jsonl`, post-copy links, unlinked writers, the link at `fstat`, the real
mode-000 directory replacement, and post-check ENOENT for both providers.
EIO/EACCES and genuine regular-file permission controls passed. The two new
post-copy-link cases also failed because classification and extraction were
not rerun. The selection also reproduced the unbounded-read failure and
publication-time copy-alias failures. Baseline failures were assertion failures
or the expected permission errors, with no collection or fixture errors.

Final validation completed on 2026-10-15 at code head `c31ca5d0e`:
**1,080 distinct requested tests passed**, with no failures or skips in the
latest passing runs. All requested files ran; counts below supersede round 1.

| File under `tests/` | Passed |
| --- | ---: |
| `unit/test_frozen_provider_streams.py` | 198 |
| `fake/test_frozen_stream_nonregular.py` | 66 |
| `unit/test_codex_turn_completed.py` | 41 |
| `fake/test_codex_turn_completed_fake.py` | 105 |
| `fake/test_finalization_replay.py` | 10 |
| `fake/test_salvage_finalization.py` | 47 |
| `unit/test_claude_adapter.py` | 170 |
| `unit/test_empty_provider_results.py` | 334 |
| `unit/test_sessions_transcripts.py` | 71 |
| `fake/test_state_contract.py` | 37 |
| `unit/test_test_module_names.py` | 1 |

The six passing foreground head runs used separate shared-lock holds, taking
35.55, 7.28, 199.84, 107.71, 89.49 and 25.19 reported pytest seconds. The first
hold included 64 fake frozen-stream cases; that file's later 66-case run includes
the two new cases, and the table counts each case once. Baseline verification
and the initial failing Codex pair each had their own additional hold. Long lock
queues were waited out without a timeout or relaunch; all pytest durations were
under 20 minutes. No workers ran in parallel.

Dependencies came from `uv sync --locked` into the workspace's `.venv`, using
Homebrew Python 3.14.7 with `PYTHON_GIL=1` and a workspace-local uv cache. Runs
set `HYPOTHESIS_PROFILE=ci`, kept the existing bounded generated settings, and
never used `deep`. Each hold used disposable TMPDIR and Hypothesis storage,
disabled pytest's cache provider, and cleaned its temporary directory with
Python `shutil.rmtree`. No `.hypothesis` folder remains. The Codex pair's only
warning is the existing pytest `zip` parametrization deprecation.

Broader full, process, e2e, live and CI suites, actual bind mounts, and a repeat
of the separate 64 MiB timing/heap benchmark were not run. Bounded copy reads
are verified by the regression that failed on the baseline. `git diff --check`
passes.

Shared Git metadata is outside the permitted writable roots. New commits use
workspace-local `.git-local/repository.git` on `fix/frozen-stream-p3`, borrowing
base objects read-only. The original `.git` pointer and detached HEAD remain
unchanged. The verified, ignored delivery bundle is
`docs/reports/2026-10-14-frozen-stream-p3.bundle`, now with prerequisite
`7974068bc`, containing all continuation commits including this final report.
Only named test and report files were staged; all commits carry the requested
co-author trailer. No logs, JSON evidence or bundles were staged. No network
Git remotes, pushes, sub-agents or live state were used.


## Round 3: CI and settlement, base `3abdaf523911`

Status: code and regressions committed; **runtime validation blocked by the
sandbox's denial of writes to the default uv cache**. No pytest test was
collected or executed in this round. The round-2 pass counts above do not
validate the round-3 changes. This job did not exhaust its time window: the
blocker is the required test environment, rather than test duration.

### Stream authority and reader audit (items 1 and 5)

Decision **(b)**: after finalization, consumers use the recorded `raw-stream`
artifact. `stream.jsonl` is a candidate name, not a required finalized output.
C-4.3 requires the daemon's independent copy, identified by
`stream.freeze.json`; C-8.2 records its path, size and SHA-256. `freeze_stream`
always publishes a fresh `stream.frozen-<uuid>.jsonl`, including when its source
is stdout because the launch's raw path never existed. `guardian.py` redirects
to `Launch.stdout_path`; Codex's `build_launch` assigns stdout separately from
the prospective `raw_stream_path`. Recreating the canonical name is unnecessary
and would give readers another name they could mistake for completion proof.
The source candidate remains intact when it existed; no canonical entry is
required to be created when capture falls back to stdout.

Both e2e tests now read the artifact row, match its path against the freeze
receipt, verify its bytes, size and digest, and verify that stdout is a different
inode. The Codex test additionally checks that `runs show` exposes the same raw
artifact. Its three original failures are reproduced in both provided
`ci-3abdaf523-*shard-3-5.log` files: all earlier rejection assertions passed,
then the hard-coded canonical read raised FileNotFoundError. The provided
`ci-7974068bc-failed.log` shows the Claude case failing solely on the basename
comparison (`stream.frozen-...jsonl` versus `stream.jsonl`), on both Pythons.
That assertion was still present at this job's base. Its replacement follows
the same artifact rule. A new run at head could not be performed; no pass is
claimed for that case.

The audit searched the entire committed package, tests, app, bin, private hooks,
tools and workflow sources for `stream.jsonl`, `raw_stream_path`, `raw-stream`,
and the stream-reader call sites. Findings:

| Reader / source | Result |
| --- | --- |
| `subfleet/frozen_stream.py` | Chooses raw/canonical/stdout only for capture; replay validates the recorded copy. |
| `subfleet/adapters/base.py`, `codex.py`, `claude.py` | Finalization supplies the frozen path through a replaced Launch before classification, completion checks, attestation and extraction. Their standalone tests explicitly create their own candidates. |
| `subfleet/daemon.py::_read_answer` | Reads live stdout only while an attempt runs; it does not decide completion. |
| `subfleet/daemon.py::_finalize`, `store.py`, `cli.py`, `compat.py` | Records and exposes artifact paths; `runs show` uses those rows, including in offline metadata. |
| `subfleet/importer.py::_artifact_paths` | Imports recorded v1 rollout/transcript paths, independent of v2 candidate names. |
| `subfleet/salvage.py` | Reads worktree Git contents, not the provider stream. |
| `subfleet/retention.py`, `retention_survey.py`, `retention_archive.py` | Walk or archive the entire job directory and record artifact rows; no canonical-only filter drops the new files. |
| `subfleet/sessions/transcripts.py`, `subfleet/hooks.py`, `private/guard/never-rules-hook.sh` | Session hooks receive native transcript paths. None constructs an attempt stream filename. |
| `subfleet/gate/service.py`, `subfleet/conversations/peers.py`, `tests/fake/gate_peer.py` | Gate peers read the accepted deliverable artifact. Person checks do not read streams. The fake peer itself writes its explicit launch raw path; that candidate remains present. |
| `subfleet/conversations/history.py`, `catalog.py`, `service.py`, `classify.py` | History uses native transcripts/rollouts, and turn completion uses the driver's `turn.json`; their stdout usage readers still find stdout. |
| `app/Sources/UIModel.swift`, `ConversationStore.swift`, `Timeline.swift`, `Protocol.swift`, `UIReading.swift` | The reading view consumes conversation history/events, not attempt stream paths. No app source contains a canonical stream/raw-artifact path reader. |
| `tests/e2e/test_milestone1_codex.py`, `tests/fake/test_finalization_replay.py` | Already read raw-stream artifact paths. |
| `tests/process/test_codex_isolation.py` | Its helper explicitly opens `launch.raw_stream_path` as provider stdout; later reads target the file it created, without daemon finalization. |
| Remaining literal hits in unit/fake tests and shared fixtures | Create candidate files, test path selection, or inspect deliberately constructed filesystem entries. They do not assume the daemon creates a canonical output. Visual/fixture JSON names are static provenance, not runtime filesystem lookups. |

No additional production reader needed a path change. The two broken e2e
assumptions are corrected instead of adding a second authority name. Historical
reports and the desktop maps explicitly pinned to older commits retain their
historical observations.

### Freeze errors and bare cores (items 2, 3 and 4)

`Daemon` now has class-level `crash_hook = None` and `publish_hook = None`, with
the same types/defaults as `__init__`. **Both fixture lines the hub added are
kept**, as explicit descriptions of their constructed cores. A bare-core
regression exercises the real boundary and publication methods without setting
either attribute.

Settlement catches ordinary and unexpected exceptions from freezing and reading
the freeze receipt. It marks `classification.stream_capture` as `unreadable`,
with the scrubbed error and exception type; it bypasses cached success and live
adapter classification/extraction. An exited detached Codex attempt becomes
`transient` with false completion evidence; other provider/turn attempts become
`unknown`. Missing exit receipts remain `lost`. No raw-stream artifact or export
is accepted from a failed capture. Cached deliverables remain diagnostic files.
On that path, repeated OSError failures while hashing diagnostic artifacts are
best effort, so the same unreadable stdout cannot prevent settlement again. The
capture failure is logged after the transaction records the attempt's end.
C-4.3 and C-8.2 now document this behavior and the consumer path rule.

The new regression matrix has 16 cases: OSError/RuntimeError, Codex/Claude,
present/missing exit receipt, first capture/cached success. It drives one real
`_process_attempt` pass and forbids live classification and extraction. Another
case covers repeated diagnostic read errors, and one covers bare-hook defaults:
18 new regression cases in total. The tests were committed before their fixes.
At base, the unguarded freeze call raises and bare hooks are absent, so these
regressions address paths not covered by the two hub fixture assignments.
They could not be executed against base or head because of the environment
blocker described below. The existing CI failures provide baseline runtime
evidence for the two e2e test corrections; a new failing test would only repeat
the erroneous canonical-name contract. The provided CI logs show the pacing
property passing at `3abdaf523`; it remains unconfirmed at this round's head.

### Validation and delivery

`uv sync --locked` failed with:

```
Failed to initialize cache at /Users/maxghenis/.cache/uv
failed to open .../sdists-v9/.git: Operation not permitted (os error 1)
```

The first required foreground shared-lock test command waited for the lock,
then failed at the same uv initialization, before pytest started. No lock
timeout was used, no pytest process ran in parallel, and no background process
was launched. Its disposable TMPDIR was removed using the requested Python
`shutil.rmtree` cleanup. The default cache was neither relocated nor cleaned.
An optional environment fallback was requested and has not been authorized;
none was used. No further lock holds were started for a command known to fail
before collection. All tests below remain unrun (per-file execution count 0),
including the requested e2e file-by-file sweep and the reader coverage:

| File | Executed | Status |
| --- | ---: | --- |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_conversation_titles_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_conversation_wakes_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_conversations.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_guard_and_isolation.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_http_isolation.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_legacy_cockpit.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_milestone1_codex.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_milestone2_claude.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_notice_job_state.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_recovery.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_reserve.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_review_r2_strand_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_review_r3_strand_timing_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_stop_bound.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_suggestions_e2e.py` | 0 | unrun: uv cache denied |
| `tests/e2e/test_suggestions_r1b_peer_e2e.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_census_recycled_pid_daemon.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_finalization_replay.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_frozen_stream_nonregular.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_salvage_finalization.py` | 0 | unrun: uv cache denied |
| `tests/fake/test_state_contract.py` | 0 | unrun: uv cache denied |
| `tests/frontend/test_core_app_cutover_reading.py` | 0 | unrun: uv cache denied |
| `tests/frontend/test_core_timeline.py` | 0 | unrun: uv cache denied |
| `tests/frontend/test_reading_view.py` | 0 | unrun: uv cache denied |
| `tests/process/test_codex_isolation.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_claude_adapter.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_claude_attest.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_claude_synthetic_error.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_cli.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_codex_adapter.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_codex_turn_completed.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_compat.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_conversation_catalog.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_conversation_history.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_conversation_peers_attachments.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_daemon_fifo_reads.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_daemon_settle.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_empty_provider_results.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_frozen_provider_streams.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_gate_admission.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_hooks.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_host_push.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_importer.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_inspection_pacing_properties.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_lane_answers.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_limit_from_model_text.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_retention_archive.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_salvage.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_scoped_stream_capacity.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_sessions_transcripts.py` | 0 | unrun: uv cache denied |
| `tests/unit/test_settle_properties.py` | 0 | unrun: uv cache denied |

Static validation: Python compilation of all changed Python files and
`git diff --check` pass. No `.hypothesis` directory exists. No test success is
inferred from those checks.

The shared Git index is outside the sandbox's writable roots. All round-3
commits use workspace-local `.git-local/repository.git` on
`fix/frozen-stream-p3`, borrowing base objects read-only. The original `.git`
pointer and detached HEAD are unchanged. The ignored delivery bundle is
`docs/reports/2026-10-15-frozen-stream-p3-round3.bundle`, with prerequisite
`3abdaf523911`; it contains the round-3 commits and this report. Only named
source, test, and documentation files were staged. Every commit has the
requested co-author trailer. No logs, JSON evidence, bundles, cache files or
dependencies were staged. No remote Git operation, push, sub-agent, live state
write or live process operation was performed. No task process remains running.

Round-3 commits before this report:

- `589044eb1`: settlement and bare-core regressions.
- `747f4d670`: class-level optional hook defaults.
- `06571c8a5`: artifact-based Codex and Claude e2e assertions.
- `9a5e41555`: first-capture and repeated-read-error regressions.
- `73386b6be`: freeze failures settle without completion proof, plus contract.


## Round 4: review r3, base `d8e4c0c0de44`

Review: `~/reviews/subfleet-2110/frozen-stream-p3/review-r3.md`, read in full.
Work stayed in the assigned checkout; no network Git remotes, pushes, sub-agents
or production state/processes were used.

Regressions were committed first in `0cb14ce5a`. With production unchanged at
`d8e4c0c0de44`, all **45 selected cases failed**: 36 replay/copy-authority cases,
six turn cases and three rejected-copy cleanup cases. Both recorded-failure
and recorded-success replay shapes failed in process and after constructing a
new `Daemon`; the real mode-000 stderr cases use the kernel's PermissionError,
restore readability before replay, and have no capture stub. Failure cases
include already-complete streams, late completion and legacy round-3 records.
The changelog finding is documentary; it has no meaningful runtime regression.

Finding dispositions:

1. Capture failures now have an explicit durable `stream_capture` record with
   class and error in `finalization.json`. Replay also recognises round-3's
   outcome-evidence-only failure records. A saved failure skips freezing,
   extraction and the completion reader; its verdict remains byte-identical.
   A saved success validates its recorded copy without calling the capture
   routine. Missing, replaced, modified or newly writable recorded copies
   become capture failures and never trigger another live-candidate read.
   Cached deliverables cannot become artifact rows or exports on that path.
   The receipt reader audit found `_finalize` is the sole direct reader;
   salvage retries, `_process_attempt` and restart recovery route through it.
   Run inspection and offline CLI views consume artifact rows. The legacy
   `reset_finalization` marker also rejects an unbound cached verdict after an
   older daemon replaced its copy; both replay and restart have regressions.
2. Conversation turns bypass stream capture. The real `TurnAdapter` classifies
   a driver's `turn.json` complete as ok/succeeded despite injected ENOSPC or
   EIO on the capture path; both providers store no frozen stream or receipt.
   C-4.3 and C-26.8 state the exemption.
3. Each rejected copy is unlinked before retry and on any exception, including
   failure after receipt publication. The regressions check cleanup between
   retries and after all three rejected copies, plus a validation I/O error.
4. The contract changelog now records rounds 3 and 4: transient/unknown failure
   settlement, best-effort diagnostics, artifact readers, sticky failure
   verdicts and the turn exemption. C-4.3/C-8.2 remove the superseded recapture
   rule. The round-2 changelog marks that historical rule as superseded.

Follow-up commits: `3a8040a96` (durable verdicts and round-3/4 changelog),
`925b4f0cc` (turn exemption), `03e099a43` (rejected-copy cleanup),
`d4ec51f9f` (additional legacy-recapture tests), and `a76317a6b` (legacy receipt
binding and clearing captured-artifact state on failure). The two additional
cases were added after the executed 45-case baseline check; at base, the
`reset_finalization` branch reclassifies and extracts instead of recording a
capture failure. They were not separately executed against base.

Final validation on 2026-10-16, functional code head `a76317a6b`:
**2,267 distinct tests passed, 69 skipped, zero post-fix failures**. Counts use
the latest full run of each file, without double-counting repeated stream and
settlement checks. Every requested file was invoked. All 47 fix-specific cases
passed, including both additional legacy-recapture cases.

The sandbox cannot obtain macOS boot/process identity. Existing C-5.3 guards
skipped 63 e2e cases (all four e2e files below) and six legacy-hold process cases.
Those cases did not execute; no end-to-end pass is claimed. The sole warning
is the existing Codex `zip` parametrization deprecation.

Turn coverage follows the literal `turn.json`/`TurnAdapter` matches and actual
settlement calls, including imported `_on_outcome`/`_settle_runner` and
`writable_turn` helpers, the runner/driver checks and conversation e2e file.
Incidental JSON-return helpers, settings checks and static view fixtures do not
drive a turn through settlement.

| File under `tests/` | Passed | Skipped |
| --- | ---: | ---: |
| `e2e/test_codex_turn_completed_e2e.py` | 0 | 3 |
| `e2e/test_conversations.py` | 0 | 44 |
| `e2e/test_milestone1_codex.py` | 0 | 9 |
| `e2e/test_milestone2_claude.py` | 0 | 7 |
| `fake/test_codex_turn_completed_fake.py` | 105 | 0 |
| `fake/test_finalization_replay.py` | 10 | 0 |
| `fake/test_frozen_stream_nonregular.py` | 94 | 0 |
| `fake/test_record_secrecy.py` | 44 | 0 |
| `fake/test_salvage_adversarial.py` | 8 | 0 |
| `fake/test_salvage_finalization.py` | 47 | 0 |
| `fake/test_state_contract.py` | 37 | 0 |
| `fake/test_turn_trees.py` | 18 | 0 |
| `unit/test_approval_resolution.py` | 33 | 0 |
| `unit/test_claude_turn.py` | 61 | 0 |
| `unit/test_codex_turn.py` | 58 | 0 |
| `unit/test_codex_turn_completed.py` | 41 | 0 |
| `unit/test_conversation_fifo_reads.py` | 26 | 0 |
| `unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 |
| `unit/test_conversation_service.py` | 98 | 0 |
| `unit/test_conversation_state_reads.py` | 18 | 0 |
| `unit/test_conversation_steer_service.py` | 45 | 0 |
| `unit/test_conversation_titles.py` | 89 | 0 |
| `unit/test_daemon_settle.py` | 61 | 0 |
| `unit/test_empty_provider_results.py` | 334 | 0 |
| `unit/test_frozen_provider_streams.py` | 201 | 0 |
| `unit/test_inspection_pacing_properties.py` | 1 | 0 |
| `unit/test_legacy_hold.py` | 50 | 6 |
| `unit/test_prune_217.py` | 9 | 0 |
| `unit/test_retention_archive.py` | 109 | 0 |
| `unit/test_review_r2_repro.py` | 31 | 0 |
| `unit/test_review_r5_repro.py` | 17 | 0 |
| `unit/test_review_r6_probes.py` | 30 | 0 |
| `unit/test_review_r6_restart.py` | 5 | 0 |
| `unit/test_sessions_transcripts.py` | 71 | 0 |
| `unit/test_settle_properties.py` | 129 | 0 |
| `unit/test_steer_invariants.py` | 248 | 0 |
| `unit/test_test_module_names.py` | 1 | 0 |
| `unit/test_timers_retention.py` | 20 | 0 |
| `unit/test_turn_replay.py` | 33 | 0 |
| `unit/test_turn_runner.py` | 76 | 0 |

All runs used separate foreground `lockf -k` holds, no lock timeout, one
pytest process and one worker, `env -u SUBFLEET_RETENTION_DORMANT`, disabled
pytest cache, disposable TMPDIR/Hypothesis storage and `HYPOTHESIS_PROFILE=ci`.
Tests kept their bounded settings; `deep` was never used. Dependencies were
installed with `uv run` into this checkout's `.venv`, with `UV_CACHE_DIR` inside
each task TMPDIR because the default cache is outside writable roots. Python
was 3.14.7 freethreaded with `PYTHON_GIL=1`. No test was skipped for cache access.

| Hold | Result | Pytest seconds |
| --- | --- | ---: |
| Baseline selected regressions | 45 expected failures | 74.35 |
| Initial stream/settlement checks | 351 passed, 3 deselected | 137.66 |
| Full stream pair | 293 passed | 107.63 |
| Turn unit/fake coverage and Claude e2e | 1,154 passed, 13 skipped | 737.57 |
| Codex completion | 146 passed, 3 skipped | 50.82 |
| Settlement/replay/salvage | 248 passed | 75.85 |
| Retention | 138 passed, 9 skipped | 234.75 |
| Contract plus stream recheck | 333 passed | 234.39 |
| Conversation e2e and steer properties | 248 passed, 44 skipped | 267.54 |

Every hold stayed below 20 minutes. Lock queues were waited out without
relaunching a runner. All requested holds started before the 4 h 30 m cutoff.
The skipped cases listed above, Python 3.12, the broader full/process/frontend/
live suites and CI were not executed in this job.

Cleanup removed task TMPDIRs with Python `shutil.rmtree`, restoring owner
write/search permission on read-only test directories first when needed.
Temporary dependencies and bytecode were removed; no `.hypothesis` folder
remains. The foreground runner finished with no task process left running.
`git diff --check` passes.

Shared Git metadata is outside the writable roots. All commits use
`.git-local/repository.git` on `fix/frozen-stream-p3`, borrowing base objects
read-only. The original `.git` pointer and detached HEAD are preserved. The
ignored delivery bundle is `docs/reports/2026-10-14-frozen-stream-p3.bundle`,
with prerequisite `d8e4c0c0de44` and the workspace-local branch. Only named source,
test and documentation files were staged; every commit has the requested
co-author trailer. No logs, JSON evidence or bundles were staged. No push.

DONE


## Round 5: review r4, base `780d83806e5f`

Review: `~/reviews/subfleet-2110/frozen-stream-p3/review-r4.md`, read in full.
The local `release/217` was `b053e3de8f2e`, already an ancestor of the assigned
head (`git rev-list HEAD..release/217 --count` returned zero). No merge or
conflict resolution was needed. Work stayed in the assigned checkout, with no
network Git remotes, pushes, sub-agents or production state/process changes.

Regressions were committed first in `c19be2e5a`. With production unchanged at
`780d83806e5f`, all **17 selected cases failed** in 150.60 pytest seconds:
the review's six Codex replay/restart cases, Claude one-EIO on replay and
restart, four additional stdout/last.md permission cases, three bounded-retry
cases and both crash leftovers. Copy-open faults patch `os.open`, so the real
`restore_stream` and `_restored_copy` run; permission cases use real mode-000
files with no capture stub. Full crash copies are made with real `atomic_copy`;
partial files match its temporary naming scheme. Both must be gone before the
next copy starts. The documentary changelog correction has no runtime test.

Finding dispositions and commits:

- **P3-1**, `47a6b3431`: `InvalidFrozenStream` identifies proven invalidity
  (a failed copy validation or a receipt naming a different file). Errors that
  interrupt checking a saved verdict preserve its bytes and raise for existing
  worker backoff. After `STREAM_CHECK_TRIES` (three) interrupted checks in one
  daemon run, settlement records the sticky `copy cannot be checked` case.
  A completed check clears the count; restart starts at zero, as with salvage.
  Initial capture failures still settle immediately. C-4.3 and C-9.2 state the
  distinction and bounded retry. Later last.md cannot replace captured text.
- **P3-2**, `a7ab7f23e`: before fresh capture without a receipt, unlink every
  `stream.frozen-*.jsonl` and `.stream.frozen-*` temporary in the attempt folder.
  Recorded copies remain subject to the existing validation rules. C-4.3 names
  crash cleanup, and regressions cover both full and partial leftovers.
- **P3-3**, `3e78e0083`: the round-4 changelog lists the clauses actually changed
  there: C-4.3, C-8.2 and C-26.8. It explicitly says only this PR's unmerged
  rounds ever wrote `reset_finalization`. Round 5 changes C-4.3 and C-9.2.
- `6c326cd8f` aligns two hand-built daemon fixtures with the new retry state.
  `48730a461` updates eight cached-copy cases to expect the new proven-invalidity
  diagnostic type, `InvalidFrozenStream`, instead of generic `OSError`. The
  initial settlement hold had 240 passes and these eight assertion failures
  (230.94 pytest seconds); those failures needed only diagnostic assertion updates.

- `ff767571d` adds eight later controls: four legacy-receipt interrupted checks
  and four legacy-verdict first-capture failures. A source audit found the
  retry guard must distinguish an existing copy from a legacy verdict alone.
  These cases were added after the 17-case base run and were not rerun against
  base. `ca634faab` restricts retries to named copies or existing legacy
  receipts. After proven receipt absence, an error in the first capture is
  recorded immediately, even with a cached legacy verdict and deliverable.

Final validation, functional head `ca634faab`: **1,134 passed, 10 skipped,
zero final failures**, across all 21 requested files. All 17 original fix cases
and the eight later legacy cases passed. The file table uses each file's latest
full run: the final hold repeated all 115 fake-stream cases after the guard
refinement alongside the five requested retention/state/module-name files.
The earlier 107 fake-stream passes are not counted twice.

| File under `tests/` | Passed | Skipped |
| --- | ---: | ---: |
| `unit/test_frozen_provider_streams.py` | 203 | 0 |
| `fake/test_frozen_stream_nonregular.py` | 115 | 0 |
| `unit/test_daemon_settle.py` | 61 | 0 |
| `unit/test_settle_properties.py` | 129 | 0 |
| `unit/test_inspection_pacing_properties.py` | 1 | 0 |
| `fake/test_finalization_replay.py` | 10 | 0 |
| `fake/test_salvage_finalization.py` | 47 | 0 |
| `fake/test_turn_trees.py` | 18 | 0 |
| `unit/test_turn_runner.py` | 76 | 0 |
| `unit/test_turn_replay.py` | 33 | 0 |
| `unit/test_claude_turn.py` | 61 | 0 |
| `unit/test_codex_turn.py` | 58 | 0 |
| `unit/test_codex_turn_completed.py` | 41 | 0 |
| `fake/test_codex_turn_completed_fake.py` | 105 | 0 |
| `e2e/test_codex_turn_completed_e2e.py` | 0 | 3 |
| `e2e/test_milestone2_claude.py` | 0 | 7 |
| `unit/test_retention_archive.py` | 109 | 0 |
| `unit/test_timers_retention.py` | 20 | 0 |
| `unit/test_prune_217.py` | 9 | 0 |
| `fake/test_state_contract.py` | 37 | 0 |
| `unit/test_test_module_names.py` | 1 | 0 |

All ten skips are the two e2e files above: C-5.3 requires permitted macOS
boot/process inspection, and boot identity is unavailable in this sandbox.
Those cases did not execute; no e2e pass is claimed. The sole warning is the
existing Codex `zip` parametrization deprecation.

| Hold | Result | Pytest seconds |
| --- | --- | ---: |
| Selected regressions against unchanged base production | 17 expected failures, 293 deselected | 150.60 |
| Initial settlement check | 240 passed, 8 old diagnostic assertions failed | 230.94 |
| Final stream pair | 310 passed | 235.81 |
| Final settlement/pacing/replay/salvage | 248 passed | 143.48 |
| Final turn/tree/runner/provider coverage | 246 passed | 752.61 |
| Final completion and Claude milestone | 146 passed, 10 skipped | 254.18 |
| Final retention/state/module names plus affected fake-stream file | 291 passed | 522.81 |

Every pytest process ran in the foreground, single worker, under its own shared
`lockf -k` hold, with `SUBFLEET_RETENTION_DORMANT` unset, the cache provider
disabled, bounded `HYPOTHESIS_PROFILE=ci`, and separate disposable TMPDIR and
Hypothesis storage. No lock timeout or detached runner was used. Lock/runner
waits were honoured without relaunching. Every pytest duration was below
20 minutes; the last requested hold was launched at 19:48 UTC, before the
4 h 30 m cutoff, and all runners exited before that cutoff.

Runtime: CPython 3.14.7, regular GIL build, with `PYTHON_GIL=1`. Dependencies
were installed by `uv run` in this checkout. The default cache is outside the
writable roots, so each run used `UV_CACHE_DIR=$TMPDIR/uv-cache`. No test was
skipped for cache access.

Unrun: the ten skipped e2e cases, Python 3.12, the broader full/process/frontend/
live suites, and CI. The eight later legacy cases were not separately executed
against base; only the original 17-case base failure run is claimed.

Cleanup removed every task TMPDIR using Python `shutil.rmtree`; the final
scratch folder needed owner directory permissions restored before removal.
Temporary dependencies and project bytecode were removed. No `.hypothesis`
folder, task runner or task-held lock remains. The original tracked
`.coverage` file is preserved. `git diff --check` passes.

Shared Git metadata is outside the writable roots. All step commits use
`.git-local/repository.git` on `fix/frozen-stream-p3`, borrowing base objects
read-only; the original `.git` pointer and detached HEAD are preserved.
The ignored delivery bundle `docs/reports/2026-10-14-frozen-stream-p3.bundle`
contains the local branch, with prerequisite `780d83806e5f`, and is verified
after the report commit. Only named source, test and documentation files were
staged; every commit has the requested co-author trailer. No logs, JSON
evidence or bundles were staged. No push.

DONE
