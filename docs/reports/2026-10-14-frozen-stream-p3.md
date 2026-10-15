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
