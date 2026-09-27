# Retention progress fix, 2026-09-27

## Incident and evidence

The [read-only live audit](2026-09-27-retention-live.md) measures 57.947 GiB in
161 of 162 worktree paths, with one additional pinned path exceeding both
bounded `du` attempts. All 156 job-owned paths are detached jobs; turn jobs own
none. The fix makes 107 paths / 35.096 GiB eligible. Excluding a recorded dirty
worktree with no salvage gives **about 34.496 GiB of reclaim candidates**.
Fresh pins, dirty-tree checks, removal errors and APFS sharing can reduce
physical reclamation. Remaining pins already account for 21.993 GiB plus the
unknown tree, so 2 GiB is not attainable without resolving those pins.

## Implementation and progress argument

Maintenance keeps a cache on its Store, scoped by state-root path. A scan
retains its file iterator and byte accumulator; cancellation is raised by the
consumer, so it does not close the iterator. Completed settled-terminal sizes
are reused while the job's state, finish time and ownership paths match.
Completed live/unsettled sizes are refreshed; their partial walks can resume.
A daemon restart starts fresh. These walks are not filesystem snapshots:
concurrent edits, especially to live jobs, retain the usual non-atomic sizing
limitation. No file descriptors are held between os.walk directory yields.

Each pool tracks count and measured-byte pressure separately. Count pressure
selects its oldest eligible candidate without scanning the rest of the pool;
a candidate is still sized and checked so unreadable trees remain protected.
Every completed measurement can trigger pruning immediately. Unknown totals
are reported as unknown. A deadline preserves both committed deletions and
partial/completed scans, including an interruption inside a single directory.
Thus, for a finite stable population and filesystem operations that finish,
repeated passes advance scans or remove eligible jobs until the pool fits or
all remaining candidates are protected. A pass need not finish the whole scan
or delete a job to make progress.

Salvage proofs run only for deletion candidates. Fresh candidates are tried
oldest-first; failed proofs/removals are revisited after fresh candidates,
least-recently attempted first. This prevents arbitrarily many slow Git
failures from repeatedly consuming every deadline. Negative outcomes never
become deletion permissions. Git preflight interruptions preserve completed
sizes; only an actual removal attempt invalidates them. Partial deletions and
late pin checks cannot charge already removed bytes against newer jobs.

Both selection and final deletion transactions recheck pins. Git, stat and
removal stay outside transactions. Cooperative checkpoints remain between
filesystem operations; Git commands are bounded by the remaining deadline
and the existing 15-second command cap. A single filesystem call, os.walk's
single-directory enumeration or shutil.rmtree is not forcibly preempted.
Deletion errors retain the job and are audited before interruptible
remeasurement, including failures at the deadline.

## Salvage decision

The daemon supplies a fresh reachability proof with the same deadline and
cancellation event as maintenance. A resolved commit must match the artifact's
SHA-256 fingerprint and be held by a named ref in a common Git directory
outside the paths being deleted. Its own shared refs/subfleet-salvage ref
qualifies: worktree removal preserves that ref and the common object store.
A capped regular salvage receipt can recover the original commit ID when a
ref was moved, but another shared named ref must then prove reachability.
Detached HEADs, reflogs, missing repositories and unproved objects stay pinned.
The existing exact dirty-tree preservation check is unchanged, including its
conservative refusal when the original salvage ref is missing.

The live audit proved 196 of 220 artifacts, including all 113 salvage-bearing
worktrees still on disk. Of 178 completed ancestry checks, 177 found only the
shared salvage ref, so demanding an additional branch would retain nearly all
build worktrees unnecessarily. The remaining 24 artifacts could not be proved
and remain pinned.

## Validation

The environment uses the requested production CPython 3.14.4 interpreter:
`UV_CACHE_DIR="$PWD/.uv-cache" uv sync --python ~/.local/share/uv/python/cpython-3.14.4-macos-aarch64-none/bin/python3.14`.
The cache override keeps dependency writes inside this workspace.

All 14 progress regressions and all four fairness regressions were separately
run against the original 03de432d retention module and failed. Three daemon
salvage regressions also failed with the original Daemon._retention method.
The tests cover both pools, pinned data, repeated deadline convergence,
interruption within a directory, already-pruned jobs, live-job starvation,
failed deletion accounting/auditing, late pins, Git preflight interruption,
slow proofs, shared refs and unreachable salvage.

Final focused verification:

`UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -q tests/unit/test_retention*.py tests/unit/test_timers_retention.py --basetemp="$PWD/.pytest_cache/retention-complete"`

**62 passed in 229.91 seconds**, using the final implementation. `git diff --check`
also passed.

The prescribed serial full-suite command was started, then the complete test
set was run with four workers because individual property tests were taking
several minutes:

`UV_CACHE_DIR="$PWD/.uv-cache" uv run --with pytest-xdist pytest -q --ignore=tests/live -n 4 --dist=worksteal --basetemp="$PWD/.pytest_cache/full-parallel"`

**6,295 passed, 121 skipped, 169 failed, 44 errors in 1,104.57 seconds**
(6,629 collected). pytest-xdist 3.8.0 was installed only in the workspace cache;
project dependencies and the lockfile did not change. No retention or timers
retention test failed in this run.

The full suite is **not green in this restricted environment**. Its failures
include macOS boot/process inspection being unavailable or denied, SwiftUI's
macro server returning a malformed response, and fixtures that assumed their
temporary directories were outside a Git repository. All temporary roots were
kept inside this assigned checkout, so plain fixture directories could inherit
the enclosing repository. There were no Unix-socket path-length errors.

Baseline checks on 03de432d reproduced seven failures and one setup error in
eight representative Python tests, including process inspection and fixture
Git discovery. A separate baseline run reproduced the SwiftUI macro error and
a wait-hub boot-identity error (two setup errors in 74.79 seconds), even with
writable workspace temp/module caches and serial execution. All 34 app/frontend
source files are unchanged from baseline. Of the full run's 44 setup errors,
37 concern boot identity and seven share the SwiftUI probe build failure.
These samples establish the main failure categories; not every failed case
was individually rerun against baseline.

Five affected Git-fixture tests passed on the current code when rerun with
`GIT_CEILING_DIRECTORIES` set to their workspace fixture root: conversation
creation/diff, two gate checks, and the salvage trailing-space-path test. This
bounds Git discovery without writing outside the workspace. No unrelated
production code was changed to accommodate the test environment.

## Commit delivery

The assigned checkout's Git metadata lives outside its writable workspace.
Normal git add/commit is refused while creating index.lock. Coherent commits
are therefore recorded in a workspace-local Git repository and exported as
`retention-progress.bundle`, based on 03de432df957. Audit commit 122054ad
records the live evidence; implementation commit bde460d6 contains the fix,
regressions and contract update; the final documentation commit records these
validation results. Each commit ends with the requested Claude co-author
footer. The checkout files themselves
hold the complete changes for Subfleet to salvage. No caller checkout, live
state, or remote branch was changed.
