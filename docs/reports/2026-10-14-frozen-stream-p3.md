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


## Fix 6: review r5, base `54db127f14f4`

Review `~/reviews/subfleet-2110/frozen-stream-p3/review-r5.md` was read in full.
No network Git remote, sub-agent or push was used. Work stays in the assigned
workspace. The original worktree's shared metadata and `.git` pointer cannot
be written in this sandbox; step commits use `GIT_DIR=$PWD/.git-local` on
`fix/frozen-stream-p3`, borrowing existing objects read-only. The original
tracked `.coverage` is preserved.

First merged the available `origin/release/217` at `473fe2941`, including #222
and later merges, in `e03f0cdc8`. The daemon conflict combined launch-environment
imports with frozen-stream imports, retaining `stream_digest`. The contract
conflict retained both sides' change-log entries. All clause IDs from both
parents remain (255 from the PR, 256 from release); every change-log entry
from both parents remains verbatim. Other clauses retain the combined behavior
through Git's automatic merge. No fetch was performed.

Regressions were committed first in `4694b44c8`:

- P3-1: both providers, replay and restart, after a crash or ENOSPC before
  verdict publication, or a classifier/attester interruption. A subsequent
  EIO, EMFILE or actual mode-000 stderr must raise, preserve the copy and receipt,
  and recover to `ok` with the original `Finished.` export. Receipt presence,
  rather than verdict presence, establishes an existing capture.
- P3-2: directories, FIFOs and sockets matching full-copy or partial-copy names
  must survive cleanup without blocking first capture. Cleanup checks `lstat`
  and unlinks only regular files and symlinks.
- P3-3: reject copy 1 after receipt publication, then crash before copy 2's
  receipt. Simulate process death by renaming copy 2 before raising, so Python's
  `finally` cannot remove its inode. Both freeze and restore must remove unnamed
  full/partial leftovers behind an invalid receipt, while preserving different
  files named by the receipt and verdict and preserving a directory decoy.
- Retry reset: two bursts of interrupted checks, separated by completed checks
  and interrupted salvage, each receive a fresh retry budget. This clause
  already works at the reviewed head; its regression must fail when the reset
  `pop` is removed, rather than falsely claiming a base-code failure.
- Failure detail: exhausted checks must record and persist
  `provider stream copy could not be checked: retry checks exhausted` instead
  of claiming Codex never completed. The outcome class and durable reason stay
  the same.

C-4.3 and C-9.2 are amended with a fix-6 change-log entry. No new `SUBFLEET_*`
name is introduced. No new test starts a guardian directly. The merged #222
classification and guardian environment conventions are covered by its tests.
The brief names two files under `tests/unit` that live under `tests/fake`:
`test_frozen_stream_nonregular.py` and `test_turn_trees.py`; validation uses
the existing files.

No change to the review's error-code observation: ENXIO, EOPNOTSUPP/ENOTSUP,
ELOOP, EINVAL and EISDIR retain their documented `NotRegularFile` mapping;
EACCES/EPERM still need proof from reinspection. No real regular-local-file
trigger was found by the review. The three consecutive interrupted checks
still follow worker backoff (about 0.5 s then 1 s), reset after a completed
check, and start at zero in a new daemon. This round changes neither code
mapping nor timing.

Baseline regression hold: **41 failed, 1 passed, 315 deselected**, in 10.11 s,
against the release merge before any production fixes. All 24 no-verdict
interruption cases, 14 cleanup cases and three exhausted-detail cases fail for
the reviewed reasons. The existing retry-reset behavior passes its new control.
The frozen capture/check implementation exercised is unchanged from the reviewed
head; #222 changes the adapter factory after the check. Per file:
`unit/test_frozen_provider_streams.py`: 14 failed;
`fake/test_frozen_stream_nonregular.py`: 27 failed, 1 passed.

Production fix committed as `a8276f48d`. The first post-fix briefed hold passes:
`unit/test_frozen_provider_streams.py`: **217 passed**;
`fake/test_frozen_stream_nonregular.py`: **140 passed**;
**357 passed** in 12.63 s.

All five briefed holds pass: **1,215 passed, no failures or skips**, across
21 files. Each ran one foreground pytest under its own exact `/usr/bin/lockf -k`
shared-lock hold, with `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`,
`HYPOTHESIS_PROFILE=ci`, and disposable TMPDIR/Hypothesis storage/cache. The
sandbox refuses writes to the default uv cache, so the required scratch-cache
fallback is used. Pytest runtimes were 12.63, 26.09, 38.28, 10.36 and 11.98 s.
Runtime: CPython 3.14.7 freethreaded with `PYTHON_GIL=1`. No deep profile is used; the briefed holds and both probes use one worker.

| Briefed file under `tests/` | Passed |
| --- | ---: |
| `unit/test_frozen_provider_streams.py` | 217 |
| `fake/test_frozen_stream_nonregular.py` | 140 |
| `unit/test_daemon_settle.py` | 61 |
| `unit/test_settle_properties.py` | 129 |
| `fake/test_finalization_replay.py` | 10 |
| `fake/test_salvage_finalization.py` | 47 |
| `fake/test_turn_trees.py` | 18 |
| `unit/test_turn_runner.py` | 76 |
| `unit/test_turn_replay.py` | 33 |
| `unit/test_claude_turn.py` | 61 |
| `unit/test_codex_turn.py` | 58 |
| `unit/test_launch_env_allowlist.py` | 27 |
| `unit/test_launch_env_helpers.py` | 20 |
| `unit/test_launch_env_mutations.py` | 41 |
| `unit/test_launch_env_policy.py` | 26 |
| `unit/test_launch_env_round2.py` | 67 |
| `unit/test_launch_env_round3.py` | 67 |
| `unit/test_launch_env_round4.py` | 40 |
| `unit/test_launch_env_round5.py` | 39 |
| `fake/test_state_contract.py` | 37 |
| `unit/test_test_module_names.py` | 1 |

The exact reviewed `54db127f14f4` versions of `subfleet/daemon.py` and
`subfleet/frozen_stream.py` were also temporarily restored for a separate
regression hold: **41 failed, 1 passed, 315 deselected**, in 21.19 s. Per file:
`unit/test_frozen_provider_streams.py`: 14 failed;
`fake/test_frozen_stream_nonregular.py`: 27 failed, 1 passed. This confirms every
changed-behavior regression against the requested head itself. Both source
files were restored in `finally` before the next hold.

The review's surviving mutation is now caught. Removing only the completed-check
`_stream_check_failures.pop(...)` produced **1 failed, 139 deselected**, in 2.79 s:
the regression observes the stale count of 2 immediately after a completed
check. Source was restored before full-suite validation. This closes the test
gap without changing the already-correct reset implementation.

Full-directory validation began after all briefed files passed. The first unit
batch passed **3,585 tests across 54 files**, in **1,079.08 s (17m 59s)**, within
the 20-minute hold bound. The only warning was the existing Codex `zip`
parametrization deprecation. To shorten subsequent holds, the disposable venv
now includes `pytest-xdist==3.8.0` and `execnet==2.1.2`; remaining full batches use
four workers, within the brief's cap. Project dependency files are unchanged.
The same exact lockf command and a single pytest invocation govern each hold.

The second full unit hold completed in **262.67 s (4m 22s)** with
**1,922 passed, 4 failed, 8 skipped**. All four failures are native host probes,
and their tests and supporting code are unchanged from `origin/release/217`:

- `unit/test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process`:
  the sandbox denies `/bin/ps` with `PermissionError`/EPERM.
- `unit/test_doctor.py::test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone`
  and `::test_daemon_lock_row_passes_against_a_live_daemon`: native identity
  inspection is unavailable, so the diagnostic correctly returns `unknown`.
- `unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos`:
  `/usr/sbin/sysctl -n kern.maxfilesperproc` returns `Operation not permitted`,
  independently reproduced outside pytest, so the ceiling is `None`.

These failures remain visible; no test or native probe was changed to hide them.
The skipped cases are five in `unit/test_daemon_verbs.py` and three in
`unit/test_filesystem_identity_agreement.py`. Coverage continues with the
remaining files despite the environment failures. The disposable pytest
entrypoint also checks the 15:56:42 UTC cutoff after any queued lock wait,
without changing the required lockf command or adding an alarm loop.

### Continuation after the power cut (2026-10-20)

The first fix-6 job (`20261020-072642-pr212-fix6`) was lost when the host lost
power at about 15:25 UTC, returning rc 125 without a final answer. Its seven
first-parent commits through `0c8be2de0522` are preserved. This continuation
read the complete original brief, review r5, report, and step commits.

The cached `origin/release/217` remains `473fe2941953`, already an ancestor of
`0c8be2de0522` through `e03f0cdc8`; no new merge or conflict resolution is needed.
An independent check confirms that the merge retains every clause ID and every
version-3 change-log entry from both parents. No fetch was performed.

| Original brief item | Audit result | Existing implementation / regression |
| --- | --- | --- |
| 1: receipted copy with no verdict | Done: receipt existence selects saved-copy retry handling, including crash, ENOSPC, classify and attest interruptions | `a8276f48d` / `4694b44c8` |
| 2: non-regular cleanup decoys | Done: `lstat` permits unlinking only regular files and symlinks | `a8276f48d` / `4694b44c8` |
| 3: later-copy crash leftovers | Done: invalid-receipt cleanup retains receipt/verdict names and removes other full/partial copies | `a8276f48d` / `4694b44c8` |
| 4: completed-check retry reset | Done: regression checks two fresh retry budgets; removing the reset is caught | `4694b44c8`; mutation evidence in `b3c1dcf99` |
| 5: exhausted-check detail | Done: stored outcome and attempt detail state that the provider stream copy could not be checked | `a8276f48d` / `4694b44c8` |
| 6: error mapping and retry window | Done: deliberately unchanged, for the reasons recorded above | No production change requested |
| Release integration / #222 | Done: cached release is merged; fix 6 introduces no `SUBFLEET_*` name or direct guardian launch | `e03f0cdc8` |
| Tests first / step commits / contract | Done: tests precede production; C-4.3 and C-9.2 and their change-log entry are amended; all seven commits have the required trailer | `4694b44c8`, `a8276f48d` |

Source, tests and contract are unchanged since `a8276f48d`; the later four
commits only record results. The inherited test counts above describe the lost
job. This continuation reruns the briefed files and both complete directories
using the required regular GIL CPython 3.14.7 (`Py_GIL_DISABLED=0`), rather than
carrying forward the lost job's free-threaded-build results. The locked dev
dependencies are installed in disposable scratch. `pytest-xdist==3.8.0` and
`execnet==2.1.2` are added only to that venv for full-suite batches; dependency
files are unchanged. New commits use this workspace's `.git-local` metadata on
`fix/frozen-stream-p3`, with read-only alternates to existing Git objects. The
original `.git` pointer and detached HEAD are preserved.

<!-- fix6-continuation-validation -->
### Continuation validation checkpoint

The first continuation hold passed **357 tests**, with no failures or skips:
`unit/test_frozen_provider_streams.py`: **217 passed**;
`fake/test_frozen_stream_nonregular.py`: **140 passed**.
Pytest reported **160.19 s (2m 40s)**. These are fresh runs under regular GIL
CPython 3.14.7, not carried-forward counts from the lost job. The remaining
four briefed holds have not returned results yet.

All holds use the exact `/usr/bin/lockf -k` shared lock without a timeout,
`env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`, disabled pytest cache,
`HYPOTHESIS_PROFILE=ci`, disposable TMPDIR/Hypothesis storage and the scratch
uv-cache fallback. Briefed holds use one worker; full-directory holds use four.
The disposable pytest entrypoint checks the 20:17 UTC cutoff after any queued
lock wait, without replacing or polling the lock. Before later invocations it
also asserts regular GIL CPython 3.14. No runner is relaunched if paused.

After all briefed holds pass, the full-directory manifest covers all 431 files
exactly once: first the 106 fake files, then unit batches of 109, 109 and 107.
The inherited full-directory checkpoint below records only the lost job.
<!-- /fix6-continuation-validation -->

<!-- fix6-full-results -->
Full-directory validation:

| Directory | Files with results / total | Passed | Failed | Errors | Skipped | Xfailed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 108 / 325 | 5507 | 4 | 0 | 8 | 0 |
| `tests/fake` | 0 / 106 | 0 | 0 | 0 | 0 | 0 |

| Full hold | Files | Passed | Failed | Errors | Skipped | Xfailed | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `unit-1` | 54 | 3585 | 0 | 0 | 0 | 0 | 1079.08 |
| `unit-2` | 54 | 1922 | 4 | 0 | 8 | 0 | 262.67 |

One serial full hold and the later holds with four workers cover the files
above. The unit manifests were combined into four holds and the fake
manifests into one; redundant initial plan entries perform no pytest run
and take no lock. The plan covers all 431 files exactly once.

**Unrun full-directory files: 323.** The per-file table below names
each one as unrun. Earlier briefed results remain valid for files covered
by a briefed hold.

<details><summary>Full-directory counts per file</summary>

| File under `tests/` | Passed | Failed | Errors | Skipped | Xfailed |
| --- | ---: | ---: | ---: | ---: | ---: |
| `fake/test_admission_disk.py` | unrun | — | — | — | — |
| `fake/test_admission_held_waiters.py` | unrun | — | — | — | — |
| `fake/test_admission_latency.py` | unrun | — | — | — | — |
| `fake/test_admission_lease_waiters.py` | unrun | — | — | — | — |
| `fake/test_admission_liveness.py` | unrun | — | — | — | — |
| `fake/test_admission_priority.py` | unrun | — | — | — | — |
| `fake/test_admission_properties.py` | unrun | — | — | — | — |
| `fake/test_admission_ramp.py` | unrun | — | — | — | — |
| `fake/test_admission_ramp_retention.py` | unrun | — | — | — | — |
| `fake/test_admission_route_isolation.py` | unrun | — | — | — | — |
| `fake/test_admission_stateful.py` | unrun | — | — | — | — |
| `fake/test_admission_visibility.py` | unrun | — | — | — | — |
| `fake/test_batch_label.py` | unrun | — | — | — | — |
| `fake/test_canonical_identity.py` | unrun | — | — | — | — |
| `fake/test_canonical_identity_fix2.py` | unrun | — | — | — | — |
| `fake/test_canonical_identity_fix3.py` | unrun | — | — | — | — |
| `fake/test_canonical_identity_fix4.py` | unrun | — | — | — | — |
| `fake/test_canonical_identity_fix5.py` | unrun | — | — | — | — |
| `fake/test_capacity_steer_daemon.py` | unrun | — | — | — | — |
| `fake/test_capacity_wait_backoff.py` | unrun | — | — | — | — |
| `fake/test_census_escapers_daemon.py` | unrun | — | — | — | — |
| `fake/test_census_recycled_pid_daemon.py` | unrun | — | — | — | — |
| `fake/test_codex_ca_bundle_fake.py` | unrun | — | — | — | — |
| `fake/test_codex_turn_completed_fake.py` | unrun | — | — | — | — |
| `fake/test_conversation_pools.py` | unrun | — | — | — | — |
| `fake/test_conversation_wakes_fake.py` | unrun | — | — | — | — |
| `fake/test_crash_matrix.py` | unrun | — | — | — | — |
| `fake/test_daemon_contract.py` | unrun | — | — | — | — |
| `fake/test_daemon_descriptors.py` | unrun | — | — | — | — |
| `fake/test_daemon_launch.py` | unrun | — | — | — | — |
| `fake/test_daemon_stacks.py` | unrun | — | — | — | — |
| `fake/test_descriptor_exhaustion.py` | unrun | — | — | — | — |
| `fake/test_detached_cwd_pins.py` | unrun | — | — | — | — |
| `fake/test_detached_retention_fence.py` | unrun | — | — | — | — |
| `fake/test_enrollment_guardian.py` | unrun | — | — | — | — |
| `fake/test_fake_provider.py` | unrun | — | — | — | — |
| `fake/test_finalization_replay.py` | unrun | — | — | — | — |
| `fake/test_folder_spelling.py` | unrun | — | — | — | — |
| `fake/test_frozen_stream_nonregular.py` | unrun | — | — | — | — |
| `fake/test_gate_end_to_end.py` | unrun | — | — | — | — |
| `fake/test_identity_rollback.py` | unrun | — | — | — | — |
| `fake/test_idle_cost.py` | unrun | — | — | — | — |
| `fake/test_interactive_claude_titles.py` | unrun | — | — | — | — |
| `fake/test_interactive_steer.py` | unrun | — | — | — | — |
| `fake/test_lane_fault.py` | unrun | — | — | — | — |
| `fake/test_lane_pilot.py` | unrun | — | — | — | — |
| `fake/test_lanes_enroll.py` | unrun | — | — | — | — |
| `fake/test_lanes_reenroll.py` | unrun | — | — | — | — |
| `fake/test_lanes_reenroll_keychain.py` | unrun | — | — | — | — |
| `fake/test_launch_env_daemon.py` | unrun | — | — | — | — |
| `fake/test_launch_env_providers.py` | unrun | — | — | — | — |
| `fake/test_launch_gates_above_stdio.py` | unrun | — | — | — | — |
| `fake/test_lost_acknowledgement.py` | unrun | — | — | — | — |
| `fake/test_main_refusal_write_dir.py` | unrun | — | — | — | — |
| `fake/test_mcp_job_contract.py` | unrun | — | — | — | — |
| `fake/test_native_maintenance_startup.py` | unrun | — | — | — | — |
| `fake/test_notice_delivery.py` | unrun | — | — | — | — |
| `fake/test_notice_header.py` | unrun | — | — | — | — |
| `fake/test_picker_native.py` | unrun | — | — | — | — |
| `fake/test_policy_sandbox.py` | unrun | — | — | — | — |
| `fake/test_probe_cancel.py` | unrun | — | — | — | — |
| `fake/test_probe_recovery.py` | unrun | — | — | — | — |
| `fake/test_probe_turn.py` | unrun | — | — | — | — |
| `fake/test_prompt_seam.py` | unrun | — | — | — | — |
| `fake/test_provider_active_cap_fake.py` | unrun | — | — | — | — |
| `fake/test_provider_cap_review_evidence.py` | unrun | — | — | — | — |
| `fake/test_provider_qos.py` | unrun | — | — | — | — |
| `fake/test_record_secrecy.py` | unrun | — | — | — | — |
| `fake/test_request_routing.py` | unrun | — | — | — | — |
| `fake/test_reserve_authorization.py` | unrun | — | — | — | — |
| `fake/test_reserve_authorization_validation.py` | unrun | — | — | — | — |
| `fake/test_reset_credit_incident.py` | unrun | — | — | — | — |
| `fake/test_resume_cli.py` | unrun | — | — | — | — |
| `fake/test_resume_contract.py` | unrun | — | — | — | — |
| `fake/test_retention_dormant.py` | unrun | — | — | — | — |
| `fake/test_review130_ab.py` | unrun | — | — | — | — |
| `fake/test_review_r185_adversarial.py` | unrun | — | — | — | — |
| `fake/test_route_view.py` | unrun | — | — | — | — |
| `fake/test_routing_end_to_end.py` | unrun | — | — | — | — |
| `fake/test_routing_tier_preferences.py` | unrun | — | — | — | — |
| `fake/test_salvage_adversarial.py` | unrun | — | — | — | — |
| `fake/test_salvage_finalization.py` | unrun | — | — | — | — |
| `fake/test_sessions_end_to_end.py` | unrun | — | — | — | — |
| `fake/test_state_contract.py` | unrun | — | — | — | — |
| `fake/test_state_fixture_processes.py` | unrun | — | — | — | — |
| `fake/test_status_payload.py` | unrun | — | — | — | — |
| `fake/test_suggestions_fake.py` | unrun | — | — | — | — |
| `fake/test_tier_hold_by_demand.py` | unrun | — | — | — | — |
| `fake/test_timers_busy_admission.py` | unrun | — | — | — | — |
| `fake/test_timers_end_to_end.py` | unrun | — | — | — | — |
| `fake/test_turn_listing.py` | unrun | — | — | — | — |
| `fake/test_turn_retention_cwd.py` | unrun | — | — | — | — |
| `fake/test_turn_retention_cwd_properties.py` | unrun | — | — | — | — |
| `fake/test_turn_retention_job_directory.py` | unrun | — | — | — | — |
| `fake/test_turn_retention_job_properties.py` | unrun | — | — | — | — |
| `fake/test_turn_retention_quarantine.py` | unrun | — | — | — | — |
| `fake/test_turn_trees.py` | unrun | — | — | — | — |
| `fake/test_turn_wait_reasons.py` | unrun | — | — | — | — |
| `fake/test_unadmittable_pin_admission.py` | unrun | — | — | — | — |
| `fake/test_unadmittable_pin_rereview.py` | unrun | — | — | — | — |
| `fake/test_unadmittable_pin_review.py` | unrun | — | — | — | — |
| `fake/test_workspace_contract.py` | unrun | — | — | — | — |
| `fake/test_workspace_enospc.py` | unrun | — | — | — | — |
| `fake/test_workspace_transient.py` | unrun | — | — | — | — |
| `fake/test_writable_fanout.py` | unrun | — | — | — | — |
| `fake/test_writer_retention_fence.py` | unrun | — | — | — | — |
| `unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 0 |
| `unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | 0 |
| `unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 0 |
| `unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 0 |
| `unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 0 |
| `unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 0 |
| `unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | 0 |
| `unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 0 |
| `unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | 0 |
| `unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | 0 |
| `unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 0 |
| `unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 0 |
| `unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | 0 |
| `unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 0 |
| `unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 0 |
| `unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 0 |
| `unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 0 |
| `unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | 0 |
| `unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | 0 |
| `unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | 0 |
| `unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | 0 |
| `unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 0 |
| `unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | 0 |
| `unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 0 |
| `unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | 0 |
| `unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | 0 |
| `unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 0 |
| `unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | 0 |
| `unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | 0 |
| `unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | 0 |
| `unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | 0 |
| `unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | 0 |
| `unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | 0 |
| `unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 0 |
| `unit/test_cli.py` | 222 | 0 | 0 | 0 | 0 |
| `unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 0 |
| `unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 0 |
| `unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | 0 |
| `unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | 0 |
| `unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 0 |
| `unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | 0 |
| `unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | 0 |
| `unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | 0 |
| `unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | 0 |
| `unit/test_compat.py` | 1615 | 0 | 0 | 0 | 0 |
| `unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | 0 |
| `unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 | 0 |
| `unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_connections.py` | 30 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 | 0 |
| `unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 | 0 |
| `unit/test_daemon_verbs.py` | 15 | 0 | 0 | 5 | 0 |
| `unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | 0 |
| `unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | 0 |
| `unit/test_descriptors.py` | 15 | 1 | 0 | 0 | 0 |
| `unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | 0 |
| `unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | 0 |
| `unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | 0 |
| `unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 0 |
| `unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | 0 |
| `unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | 0 |
| `unit/test_doctor.py` | 42 | 2 | 0 | 0 | 0 |
| `unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | 0 |
| `unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | 0 |
| `unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | 0 |
| `unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | 0 |
| `unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | 0 |
| `unit/test_filesystem_identity_agreement.py` | 37 | 0 | 0 | 3 | 0 |
| `unit/test_folders.py` | 23 | 0 | 0 | 0 | 0 |
| `unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | 0 |
| `unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | 0 |
| `unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 | 0 |
| `unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | 0 |
| `unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | 0 |
| `unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | 0 |
| `unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | 0 |
| `unit/test_gate_service.py` | 25 | 0 | 0 | 0 | 0 |
| `unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | 0 |
| `unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | 0 |
| `unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | 0 |
| `unit/test_guardian.py` | unrun | — | — | — | — |
| `unit/test_guardian_qos.py` | unrun | — | — | — | — |
| `unit/test_handler_refusals.py` | unrun | — | — | — | — |
| `unit/test_held_waiter_holdback.py` | unrun | — | — | — | — |
| `unit/test_hidden_environment.py` | unrun | — | — | — | — |
| `unit/test_hook_cost.py` | unrun | — | — | — | — |
| `unit/test_hooks.py` | unrun | — | — | — | — |
| `unit/test_hooks_native_guard.py` | unrun | — | — | — | — |
| `unit/test_host_push.py` | unrun | — | — | — | — |
| `unit/test_host_push_admission.py` | unrun | — | — | — | — |
| `unit/test_host_push_build.py` | unrun | — | — | — | — |
| `unit/test_host_push_build_budget.py` | unrun | — | — | — | — |
| `unit/test_host_push_cli.py` | unrun | — | — | — | — |
| `unit/test_host_push_config_cost.py` | unrun | — | — | — | — |
| `unit/test_host_push_copy.py` | unrun | — | — | — | — |
| `unit/test_host_push_export.py` | unrun | — | — | — | — |
| `unit/test_host_push_intake.py` | unrun | — | — | — | — |
| `unit/test_host_push_metadata.py` | unrun | — | — | — | — |
| `unit/test_host_push_mutations.py` | unrun | — | — | — | — |
| `unit/test_host_push_names.py` | unrun | — | — | — | — |
| `unit/test_host_push_quarantine_restart.py` | unrun | — | — | — | — |
| `unit/test_host_push_restart.py` | unrun | — | — | — | — |
| `unit/test_host_push_retention.py` | unrun | — | — | — | — |
| `unit/test_host_push_storage_recovery.py` | unrun | — | — | — | — |
| `unit/test_host_push_storage_retry_budget.py` | unrun | — | — | — | — |
| `unit/test_host_push_store_completion_pr183.py` | unrun | — | — | — | — |
| `unit/test_host_push_store_interleavings.py` | unrun | — | — | — | — |
| `unit/test_host_push_trees.py` | unrun | — | — | — | — |
| `unit/test_host_push_validated_top.py` | unrun | — | — | — | — |
| `unit/test_importer.py` | unrun | — | — | — | — |
| `unit/test_inspection_pacing_properties.py` | unrun | — | — | — | — |
| `unit/test_invariants_index.py` | unrun | — | — | — | — |
| `unit/test_keychain_exec_guard_properties.py` | unrun | — | — | — | — |
| `unit/test_lane_answers.py` | unrun | — | — | — | — |
| `unit/test_lanes_transfer.py` | unrun | — | — | — | — |
| `unit/test_launch_env_allowlist.py` | unrun | — | — | — | — |
| `unit/test_launch_env_helpers.py` | unrun | — | — | — | — |
| `unit/test_launch_env_mutations.py` | unrun | — | — | — | — |
| `unit/test_launch_env_policy.py` | unrun | — | — | — | — |
| `unit/test_launch_env_round2.py` | unrun | — | — | — | — |
| `unit/test_launch_env_round3.py` | unrun | — | — | — | — |
| `unit/test_launch_env_round4.py` | unrun | — | — | — | — |
| `unit/test_launch_env_round5.py` | unrun | — | — | — | — |
| `unit/test_lease_waiter_selection.py` | unrun | — | — | — | — |
| `unit/test_legacy_hold.py` | unrun | — | — | — | — |
| `unit/test_limit_from_model_text.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round10.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round10_mutations.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round3.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round4.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round5.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round6.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round7.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round7_mutations.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round8.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round8_mutations.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round9.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_round9_mutations.py` | unrun | — | — | — | — |
| `unit/test_live_store_guard_state_root.py` | unrun | — | — | — | — |
| `unit/test_lockwatch.py` | unrun | — | — | — | — |
| `unit/test_mcp_cli_contract.py` | unrun | — | — | — | — |
| `unit/test_mcp_scheduler_contract.py` | unrun | — | — | — | — |
| `unit/test_mirror_flags_faults.py` | unrun | — | — | — | — |
| `unit/test_mirror_flags_model.py` | unrun | — | — | — | — |
| `unit/test_mirror_flags_stateful.py` | unrun | — | — | — | — |
| `unit/test_native_operations.py` | unrun | — | — | — | — |
| `unit/test_notice_rendering.py` | unrun | — | — | — | — |
| `unit/test_notify_push.py` | unrun | — | — | — | — |
| `unit/test_offline.py` | unrun | — | — | — | — |
| `unit/test_operator_hold_closures.py` | unrun | — | — | — | — |
| `unit/test_operator_notices.py` | unrun | — | — | — | — |
| `unit/test_outcome_unknown.py` | unrun | — | — | — | — |
| `unit/test_person_boundary.py` | unrun | — | — | — | — |
| `unit/test_person_boundary_identity.py` | unrun | — | — | — | — |
| `unit/test_picker.py` | unrun | — | — | — | — |
| `unit/test_placement_ramp.py` | unrun | — | — | — | — |
| `unit/test_policy.py` | unrun | — | — | — | — |
| `unit/test_policy_disk.py` | unrun | — | — | — | — |
| `unit/test_policy_ramp.py` | unrun | — | — | — | — |
| `unit/test_policy_support.py` | unrun | — | — | — | — |
| `unit/test_policy_tier_preferences.py` | unrun | — | — | — | — |
| `unit/test_pr124_fixes.py` | unrun | — | — | — | — |
| `unit/test_pr124_review_r2_fixes.py` | unrun | — | — | — | — |
| `unit/test_pr192_review_r1.py` | unrun | — | — | — | — |
| `unit/test_pr192_review_r2.py` | unrun | — | — | — | — |
| `unit/test_procs.py` | unrun | — | — | — | — |
| `unit/test_protocol_secrecy.py` | unrun | — | — | — | — |
| `unit/test_provider_active_cap.py` | unrun | — | — | — | — |
| `unit/test_prune_217.py` | unrun | — | — | — | — |
| `unit/test_prune_decision_backup_verification.py` | unrun | — | — | — | — |
| `unit/test_prune_decision_fingerprints.py` | unrun | — | — | — | — |
| `unit/test_prune_decisions.py` | unrun | — | — | — | — |
| `unit/test_prune_restore.py` | unrun | — | — | — | — |
| `unit/test_quota_projection.py` | unrun | — | — | — | — |
| `unit/test_rank_review_report.py` | unrun | — | — | — | — |
| `unit/test_reap_liveness.py` | unrun | — | — | — | — |
| `unit/test_rediscovery_git_trace.py` | unrun | — | — | — | — |
| `unit/test_relay.py` | unrun | — | — | — | — |
| `unit/test_render.py` | unrun | — | — | — | — |
| `unit/test_render_quota_projection.py` | unrun | — | — | — | — |
| `unit/test_reserve_authorization_plumbing.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_claim_timing.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_demand.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_properties.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_review_r1.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_review_r2.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_review_r3.py` | unrun | — | — | — | — |
| `unit/test_reset_credit_review_r3_mutants.py` | unrun | — | — | — | — |
| `unit/test_resource_identity.py` | unrun | — | — | — | — |
| `unit/test_retention_81_notes.py` | unrun | — | — | — | — |
| `unit/test_retention_archive.py` | unrun | — | — | — | — |
| `unit/test_retention_archive_properties.py` | unrun | — | — | — | — |
| `unit/test_retention_case_alias.py` | unrun | — | — | — | — |
| `unit/test_retention_nested_leases.py` | unrun | — | — | — | — |
| `unit/test_retention_ordinary_trees.py` | unrun | — | — | — | — |
| `unit/test_retention_qos.py` | unrun | — | — | — | — |
| `unit/test_retention_readback.py` | unrun | — | — | — | — |
| `unit/test_retention_rediscovery.py` | unrun | — | — | — | — |
| `unit/test_retention_regenerable.py` | unrun | — | — | — | — |
| `unit/test_retention_regenerable_properties.py` | unrun | — | — | — | — |
| `unit/test_retention_salvage_properties.py` | unrun | — | — | — | — |
| `unit/test_retention_shared_folders.py` | unrun | — | — | — | — |
| `unit/test_retention_sweep_race.py` | unrun | — | — | — | — |
| `unit/test_retention_turn_contract.py` | unrun | — | — | — | — |
| `unit/test_retention_turn_workdirs.py` | unrun | — | — | — | — |
| `unit/test_retention_turns.py` | unrun | — | — | — | — |
| `unit/test_retention_worktrees.py` | unrun | — | — | — | — |
| `unit/test_review130_pass.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_fixes.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_history.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_pr_wakes.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_report.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_requests.py` | unrun | — | — | — | — |
| `unit/test_review_pr127_service.py` | unrun | — | — | — | — |
| `unit/test_review_r2_repro.py` | unrun | — | — | — | — |
| `unit/test_review_r3_measure.py` | unrun | — | — | — | — |
| `unit/test_review_r3_repro.py` | unrun | — | — | — | — |
| `unit/test_review_r4_repro.py` | unrun | — | — | — | — |
| `unit/test_review_r5_repro.py` | unrun | — | — | — | — |
| `unit/test_review_r6_probes.py` | unrun | — | — | — | — |
| `unit/test_review_r6_restart.py` | unrun | — | — | — | — |
| `unit/test_round10_refusal_context.py` | unrun | — | — | — | — |
| `unit/test_route_check.py` | unrun | — | — | — | — |
| `unit/test_salvage.py` | unrun | — | — | — | — |
| `unit/test_salvage_optional_transient.py` | unrun | — | — | — | — |
| `unit/test_salvage_unindexable.py` | unrun | — | — | — | — |
| `unit/test_scheduler.py` | unrun | — | — | — | — |
| `unit/test_scheduler_priority_callers.py` | unrun | — | — | — | — |
| `unit/test_scheduler_split.py` | unrun | — | — | — | — |
| `unit/test_scheduler_tier_preferences.py` | unrun | — | — | — | — |
| `unit/test_scheduler_turns.py` | unrun | — | — | — | — |
| `unit/test_scheduler_weekly.py` | unrun | — | — | — | — |
| `unit/test_scoped_stream_capacity.py` | unrun | — | — | — | — |
| `unit/test_scrub_key_order.py` | unrun | — | — | — | — |
| `unit/test_sessions_cli.py` | unrun | — | — | — | — |
| `unit/test_sessions_handoff.py` | unrun | — | — | — | — |
| `unit/test_sessions_handoff_excerpts.py` | unrun | — | — | — | — |
| `unit/test_sessions_headless_properties.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_cache_coherence.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_fifo.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_hot_health.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_hot_progress.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_hot_regressions.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_inventory.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_load_gap.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_safety.py` | unrun | — | — | — | — |
| `unit/test_sessions_mirror_write_churn.py` | unrun | — | — | — | — |
| `unit/test_sessions_nudge.py` | unrun | — | — | — | — |
| `unit/test_sessions_registry.py` | unrun | — | — | — | — |
| `unit/test_sessions_revive.py` | unrun | — | — | — | — |
| `unit/test_sessions_revive_reads.py` | unrun | — | — | — | — |
| `unit/test_sessions_transcripts.py` | unrun | — | — | — | — |
| `unit/test_settle_properties.py` | unrun | — | — | — | — |
| `unit/test_soak_tools.py` | unrun | — | — | — | — |
| `unit/test_status_json.py` | unrun | — | — | — | — |
| `unit/test_steer_invariants.py` | unrun | — | — | — | — |
| `unit/test_stop_alarm.py` | unrun | — | — | — | — |
| `unit/test_stop_watchdog.py` | unrun | — | — | — | — |
| `unit/test_store_contention_queries.py` | unrun | — | — | — | — |
| `unit/test_store_ids.py` | unrun | — | — | — | — |
| `unit/test_store_migration_2.py` | unrun | — | — | — | — |
| `unit/test_store_properties.py` | unrun | — | — | — | — |
| `unit/test_store_readers.py` | unrun | — | — | — | — |
| `unit/test_store_snapshot_plans.py` | unrun | — | — | — | — |
| `unit/test_suggest_cli.py` | unrun | — | — | — | — |
| `unit/test_suggest_hint.py` | unrun | — | — | — | — |
| `unit/test_suggestion_error_boundaries.py` | unrun | — | — | — | — |
| `unit/test_suggestion_properties.py` | unrun | — | — | — | — |
| `unit/test_suggestion_secret_properties.py` | unrun | — | — | — | — |
| `unit/test_suggestion_structural_errors.py` | unrun | — | — | — | — |
| `unit/test_suggestions.py` | unrun | — | — | — | — |
| `unit/test_suggestions_r1b_adversarial.py` | unrun | — | — | — | — |
| `unit/test_suggestions_r3_channels.py` | unrun | — | — | — | — |
| `unit/test_suggestions_review_boundaries.py` | unrun | — | — | — | — |
| `unit/test_suggestions_review_multiline.py` | unrun | — | — | — | — |
| `unit/test_suggestions_review_response.py` | unrun | — | — | — | — |
| `unit/test_test_module_names.py` | unrun | — | — | — | — |
| `unit/test_tick_prefilter.py` | unrun | — | — | — | — |
| `unit/test_timers_alerts.py` | unrun | — | — | — | — |
| `unit/test_timers_busy_read.py` | unrun | — | — | — | — |
| `unit/test_timers_daemon.py` | unrun | — | — | — | — |
| `unit/test_timers_keepalive.py` | unrun | — | — | — | — |
| `unit/test_timers_probe.py` | unrun | — | — | — | — |
| `unit/test_timers_reset_credits.py` | unrun | — | — | — | — |
| `unit/test_timers_retention.py` | unrun | — | — | — | — |
| `unit/test_title_review_regressions.py` | unrun | — | — | — | — |
| `unit/test_turn_attachment_replay.py` | unrun | — | — | — | — |
| `unit/test_turn_overlap.py` | unrun | — | — | — | — |
| `unit/test_turn_replay.py` | unrun | — | — | — | — |
| `unit/test_turn_runner.py` | unrun | — | — | — | — |
| `unit/test_ultracode_effort.py` | unrun | — | — | — | — |
| `unit/test_unadmittable_pin.py` | unrun | — | — | — | — |
| `unit/test_wait_hub.py` | unrun | — | — | — | — |
| `unit/test_wake_cli.py` | unrun | — | — | — | — |
| `unit/test_weekly_rank_vs_base.py` | unrun | — | — | — | — |

</details>
<!-- /fix6-full-results -->
