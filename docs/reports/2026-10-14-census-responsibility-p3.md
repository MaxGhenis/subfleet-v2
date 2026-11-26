# Census responsibility P3 follow-ups, 2026-10-14

Base: `73aff8cd33ad` (`release/217`, #205 merged). Read review r2 and all
three scripts in its adjacent evidence folder.

P3-A: a root recorded with `unique_id: None` supplies responsibility members
only when the snapshot shows its pid live with its recorded start time.
Otherwise that source contributes nothing and reports
`responsibility root identity unavailable for pid <pid>`, making the census
unverifiable. Proven recycled roots retain their existing exclusion evidence.
This blocks the stranger's child from entering `responsible_identities`.
The real-process regression uses self-responsible S and its child C, with an
old recorded start; omitting S and C from the snapshot reproduces the review's
timing. It checks containment and `Daemon._contain` evidence writes.

P3-B: C-5.9 now points to C-5.2's wait after the receipt. C-5.5 describes a
process a member starts with responsibility disclaimed. C-5.1/C-5.5 describe
the missing-unique-id rule, and the version history records both follow-ups.

Against unchanged base census code, the new regression file returned **7
failed, 3 passed**; the direct input counted S and C without errors. At head it
returned **10 passed**, including the property over unique id present/None ×
reuse before/after the snapshot, bounded to 40 examples per combination in
the ci profile. The initial head run exposed an inconsistent other-boot root
fixture; its recorded boot was corrected, its assertions retained, and all
52 tests in that file passed on rerun.

Final distinct-case results: **222 passed, 21 skipped**, no remaining failures.
CPython 3.14.7, GIL enabled. Every pytest invocation ran serially under its own
`lockf -k` hold without a timeout, with `-p no:cacheprovider`, scratch TMPDIR and
Hypothesis storage. Pytest runtimes were 32.69 s (base), 135.63 s (initial head),
18.10 s (affected-file rerun), 5.28 s (guardian), and 36.51 s (state/module names).
No deep profile was used. Temporary results/storage were removed with the
requested Python cleanup; no `.hypothesis` directory or test wrapper remains.

| File under `tests/` | Passed | Skipped |
|---|---:|---:|
| unit/test_procs.py | 47 | 0 |
| unit/test_census_identity_uncertainty.py | 8 | 0 |
| unit/test_census_procargs.py | 52 | 0 |
| unit/test_census_recycled_pid.py | 25 | 0 |
| unit/test_census_responsibility_failures.py | 20 | 0 |
| unit/test_census_responsibility_identity.py | 10 | 0 |
| unit/test_census_survivors_review.py | 14 | 0 |
| fake/test_census_recycled_pid_daemon.py | 8 | 0 |
| fake/test_census_escapers_daemon.py | 0 | 4 |
| process/test_census_raw_markers_process.py | 0 | 3 |
| process/test_census_responsibility_process.py | 0 | 10 |
| process/test_guardian_process.py | 0 | 4 |
| fake/test_state_contract.py | 37 | 0 |
| unit/test_test_module_names.py | 1 | 0 |

Host work remains: this sandbox denies `ps`/boot-identity inspection, including
inspection of this task's own Python process. All 21 skipped cases need the
hub's host. The real regression's base/head execution is unrun here: compare
`2c31db7b4` (base production code) with `2bb5bbb5b`, selecting
`test_d_cen1_a_real_recycled_root_and_its_child_are_strangers`. The remaining
suite and Python 3.12 were not run. Git whitespace checks passed.

The shared Git index rejected writes with EPERM. Commits are on
`fix/census-responsibility-p3` in workspace-local `.git-local`; inspect with
`GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`. The original checkout
metadata was preserved. A transfer bundle at
`.git-local/census-responsibility-p3.bundle` contains the four new commits.

- `2c31db7b4`: regressions first, including real processes and the property.
- `a3d6b55c9`: the two stale contract sentences and version history.
- `2bb5bbb5b`: census guard, identity diagnostic and contract rule.
- This report is committed separately after validation.

Only named source, test, contract and report files were staged. Every commit
carries the requested co-author trailer. No push, remote operation, sub-agent,
live-store access or unrelated process modification.


## Round 2: PR #220 review r1, 2026-10-15

Base: `fd43f6e6f10b`. Tests-first commit `667648558` adds the review's exact
timing: the snapshot shows original pid 42 with its recorded start, then
replacement S holds 42 with a different start and unique id 100; child C is
pid 60, unique id 101. Neither carries markers or has a recorded child seed.
Against unchanged base production code, the unit regression file returned
**6 failed, 11 passed in 38.64 s**, reproducing
`responsible_pids={42,60}`, `errors=()`. The regression also exercises
`Daemon._contain` and checks that no stranger identity or recording event is
persisted.

Fix `7fbfd6584` requires a no-unique-id root's current `proc_start`, read after
the matching snapshot, to equal its recorded start. A changed, absent or
unreadable start disables the responsibility source and reports
`responsibility root identity unavailable for pid <pid>`, making the census
unverifiable and preventing release. Its historical group/walk row may remain
in that census, but supplies no new responsibility seeds. Known-id bounds,
marker collection, and guardian waiting remain unchanged. C-5.1/C-5.5 describe
the check; C-5.5/C-5.12 account for the additional start-time read.

The ci property covers known/missing unique id crossed with reuse before ps,
after absent ps, and after matching ps: 40 examples per combination. Fresh
matching, absent and failed reads have controls, including read order. All
previous controls passed. The real-kernel regression adds both matching-ps
cases, leaving individual start and kernel reads real for the hub.

Head results: **191 passed, 4 skipped**, with **15 process cases not run**.
Unit pytest took 101.70 s; fake pytest took 6.61 s. Counts below are per file;
process counts are static parametrization counts, not executed results.

| File under `tests/` | Passed | Skipped | Not run |
|---|---:|---:|---:|
| unit/test_procs.py | 47 | 0 | 0 |
| unit/test_census_identity_uncertainty.py | 8 | 0 | 0 |
| unit/test_census_procargs.py | 52 | 0 | 0 |
| unit/test_census_recycled_pid.py | 25 | 0 | 0 |
| unit/test_census_responsibility_failures.py | 20 | 0 | 0 |
| unit/test_census_responsibility_identity.py | 17 | 0 | 0 |
| unit/test_census_survivors_review.py | 14 | 0 | 0 |
| fake/test_census_recycled_pid_daemon.py | 8 | 0 | 0 |
| fake/test_census_escapers_daemon.py | 0 | 4 | 0 |
| process/test_census_responsibility_process.py | 0 | 0 | 12 |
| process/test_census_raw_markers_process.py | 0 | 0 | 3 |

This sandbox denied inspection of this task's own Python process (`ps
inspection unavailable`); fake escapers skipped because boot identity was
unavailable. The hub must run the skipped and unrun cases. Compare
`667648558` (base production code) with `7fbfd6584` for the new real-kernel
regression.

CPython 3.14.7, GIL enabled. Each pytest ran serially in the foreground under
its own shared `lockf -k` hold, waiting without a timeout, with one worker,
`-p no:cacheprovider`, the ci profile and scratch Hypothesis storage. No deep
profile was used. Scratch directories/results were removed with the requested
Python cleanup; no `.hypothesis` directory or test wrapper remains. Whitespace
checks passed. Final production prose edits have the same AST as the
unit-tested fix after excluding docstrings.

Shared Git metadata is outside the sandbox's writable roots; writing the
worktree's `.git` pointer was rejected with EPERM. All three commits are on
`fix/census-responsibility-p3` in workspace-local `.git-local`, with the original
pointer and shared metadata preserved. Inspect with
`GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`. Transfer bundle:
`.git-local/census-responsibility-p3-r2.bundle` (requires the base commit).
Only named source/test/contract/report files were staged, and each commit has
the requested co-author trailer. No push, Git remote operation, sub-agent or
live-store access.


## Round 3: PR #220 review r2, 2026-10-15

Base: `fc38f9e7dcbb`. Read
`~/reviews/subfleet-2110/census-responsibility-p3/review-r2.md`.

Tests-first commit `1ad81abd5` adds containment and durable-walk-seed
regressions for replacement between the early recheck and the member reads,
and after the member reads but before the final check. Each timing covers a
changed start, `None`, and `InspectionError`: 12 regression cases. Three new
bounded ci property cases cover replacement before the early recheck,
between the recheck and member reads, and after member reads. Each generated
input also checks a still-holding root: all its real children are counted and
saved, while replacement cases save no responsibility identity or event.
There are 40 examples per timing, with no database or deadline. Existing
known-id and snapshot-timing properties remain.

Fix `a2ca55905` keeps the early read and re-reads `proc_start(root)` immediately
after `_kernel_sources` returns. A different, absent or unreadable late start
discards that pass's responsibility members and appends
`responsibility root identity unavailable for pid <pid>`. The census is then
unverifiable, and `_contain` has no new responsibility members to persist.
Independent marker results and member-read failures remain available. The
exact default fixture has S=42/100 and C=60/101. Test correction `9d1ebadc2`
accounts for the separate start read of child 60, which was absent from the
snapshot, after the two root checks.

C-5.1/C-5.5/C-5.12 and the version-3 changelog describe both checks and all
three replacement timings. **The read count changes:** a no-unique-id root
that passes the early check costs two root-start `ps` reads, in addition to
the process-table snapshot and any reads for pids absent from it. An early
rejection costs only the first root read and skips responsibility member
reads. Known-id roots require no extra root-start reads. Same-second reuse
retains C-5.3's existing limitation.

**Runtime validation is blocked, not passed or skipped.** The foreground
baseline attempt used the requested shared `lockf -k` hold, default uv cache,
`-p no:cacheprovider`, and scratch TMPDIR/Hypothesis storage. It waited for the
lock without a timeout. Once admitted, `uv run` failed before starting pytest:

```
error: Failed to initialize cache at `/Users/maxghenis/.cache/uv`
  Caused by: failed to open file `/Users/maxghenis/.cache/uv/sdists-v9/.git`:
  Operation not permitted (os error 1)
```

A `uv run --no-sync --offline` Python probe failed at the same cache access.
No cache override, new cache, cache cleanup or alternate runner was used.
A question about a read-only-cache/local-environment fallback is pending;
the requested default-cache runner cannot work inside this sandbox.
Consequently the unchanged-base regressions, all three requested suite groups,
and the drop-second-read mutation have **not run**. The new regression's
failure at base and under mutation must still be demonstrated by the hub.

A standalone probe of this task's own Python pid returned
`Own-process inspection unavailable: ps inspection unavailable`. Real process
tests also require the hub. No unrelated process was inspected or modified.

| File under `tests/` | Executed | Status |
|---|---:|---|
| unit/test_procs.py | 0 | Not run: uv cache denied |
| unit/test_census_identity_uncertainty.py | 0 | Not run: uv cache denied |
| unit/test_census_procargs.py | 0 | Not run: uv cache denied |
| unit/test_census_recycled_pid.py | 0 | Not run: uv cache denied |
| unit/test_census_responsibility_failures.py | 0 | Not run: uv cache denied |
| unit/test_census_responsibility_identity.py | 0 | Runner failed before pytest |
| unit/test_census_survivors_review.py | 0 | Not run: uv cache denied |
| fake/test_census_recycled_pid_daemon.py | 0 | Not run: uv cache denied |
| fake/test_census_escapers_daemon.py | 0 | Not run: uv cache/inspection denied |
| fake/test_state_contract.py | 0 | Not run: uv cache denied |
| unit/test_test_module_names.py | 0 | Not run: uv cache denied |
| process/test_census_responsibility_process.py | 0 | Not run: inspection denied |
| process/test_census_raw_markers_process.py | 0 | Not run: inspection denied |
| process/test_guardian_process.py | 0 | Not run: inspection denied |

Python AST syntax checks and Git whitespace checks pass. CPython 3.14.7.
The failed runner's scratch directory was removed with the requested Python
cleanup; no `.hypothesis` directory or task-owned process remains.

The shared Git index rejected writes with EPERM. All round-3 commits are on
`fix/census-responsibility-p3` in workspace-local `.git-local`; the original
worktree pointer and shared metadata remain intact. Inspect with
`GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`. Delivery bundle:
`.git-local/census-responsibility-p3-r3.bundle` (requires `fc38f9e7dcbb`).
Only named test/source/contract/report files were staged. Every commit has
the requested co-author trailer. No push, remote operation, sub-agent,
live-store access, cache relocation or history rewrite.

## Round 4: PR #220 review r3, 2026-10-16

Base: `85a7b384f906`, including `release/217` at `2012bbed4`. Read the entire
`~/reviews/subfleet-2110/census-responsibility-p3/review-r3.md`. This section supersedes
the round-3 discard and its contract statements.

Tests-first commits: `d3bd42fd9` (Inputs A/B, known-id mid-scan replacement, member-pid
reuse, five timing boundaries and signal-path regressions), then `c08dac975` (persisted
evidence exercises the safety premise independently of new discovery, and a member
without a readable unique id is retained). Additional tests-first commits `ecd22e2c5`,
`8d061d178` and `243349727` cover stale group authority, normal completion for a known
dead root, and the exact S=42/100, C=60/101 mutation probes.

Fix `a447cf1f5` replaces the final blanket clear with one in-process holder read for
both kinds of root. A readable successor excludes only observations at or above its
unique id. Missing final ordering keeps collected members with an identity diagnostic. A
known root already gone without provisional members can still verify empty. A changed
kernel birth removes stale snapshot group membership before any ownership promotion. The
superseded 12 blanket- discard regression cases and three-timing property are replaced
by the new birth-order regressions and ten property cases (both root kinds × five
timings), each with 40 ci examples and all three fates: alive, reaped or replaced. Each
scenario checks the discovery pass and a later pass. Inputs A/B also check their full
three-pass sequence.

C-5.1, C-5.5, C-5.12, the version-3 line and the D-CEN1 entry now agree: one
process-table ps, one additional early root-start ps only for a missing-id root whose
live snapshot matches, and one additional in-process holder read after each applicable
responsibility scan. There is no final root-start ps. A failed listing, early rejection
or proven other boot needs no final holder read. Responsibility births use no additional
process or syscall beyond the existing member kernel record; members reuse that captured
live state even when the snapshot describes an older holder. No single-pid ps is needed
for responsibility observations.

Signal-authority audit (item 2): `Daemon._contain` merges `responsible_identities` only
into census seeds. `_kill_attempt` and `_contain_probe` obtain individual signal
authority only from `owned_identities`, adding independently observed group members only
while the recorded leader is still alive. Neither promotes a responsibility seed or its
walked descendants. `_finalize` checks containment before salvage; `_resolve_quarantine`
performs no signalling, including forced release. Salvage does not read responsibility
identities. `_quarantine` abandons only the stored guardian handle, and
`spawn_disclaimed` reaps only those handles with `waitpid`; it never constructs handles
from census evidence. The regression intercepts both raw signal functions and `waitpid`,
covers a seed plus its walk-only child, and exercises kill, probe containment,
finalization, quarantine resolution and reaping, both without ownership and with an
older ownership birth at that same pid. Fresh signal checks are tied to the actual
current birth, so an accidental promotion would produce a failing raw-signal assertion.
No signal path using evidence-only seeds was found.

Item 3 uses a bounded fix: capture each responsibility member's birth from its existing
BSD kernel record, alongside its unique id. The census uses that captured start rather
than an older snapshot identity or a later `identity(pid)`. The note-2 regression
replaces K with unrelated Y after the member scan: only K's original identity may be
persisted, and the following census excludes Y by its different start. Rendering uses
literal English weekday/month names and UTC, preserving ps's second precision
independently of ambient locale. The reviewed reuse after the member read is closed.
Reuse between the separate metadata and responsibility calls remains a non-atomic
observation limit, now explicit in C-5.5. Same-second reuse remains C-5.3's existing
limitation.

PR-body paragraph (corrected invariant):

> For both kinds of responsibility root, when the final read identifies a different
> holder with a readable unique id, observations with readable unique ids at or above
> that holder's birth bound are excluded. A real child whose live birth and
> responsibility were consistently captured before R dies predates every successor, so
> the final root check preserves its captured identity; later censuses keep that
> identity as a walk seed while it matches. When the final check cannot identify R or
> bound a successor, collected observations remain evidence with an unverifiable result.
> A known-id root already gone without observations can still verify empty. This
> evidence can hold an attempt but grants no signal authority, including to descendants
> found by walking from it. Capturing a member's birth with its kernel unique id
> prevents a later pid holder from supplying its identity to that observation. The claim
> does not assert that every counted process was started by R: same-second reuse without
> a recorded root unique id, non-atomic kernel observations, and unavailable member or
> successor ordering may conservatively retain strangers. A ceiling bounds the observed
> holder; an intervening holder can leave older strangers below that ceiling. Member pid
> reuse between the separate metadata and responsibility calls can still prevent a
> consistent capture, as C-5.5 now states. A child whose root dies before its member
> read, a descendant started with responsibility disclaimed that also escapes the other
> sources, and an observation without a capturable identity remain C-5.5 limits. Start
> identities retain C-5.3's second precision.

Baseline on unchanged `85a7b384f906` production code: **11 failed, 5 passed, 28
deselected in 22.94 s**. The unit file contributed six expected failures: Inputs A/B,
known-id mid-scan replacement, both member-reuse variants and the new kernel-birth
field. The five just-observed daemon paths failed because K was discarded; all five
independently pre-persisted no-signal paths passed. The prepared production fix was
applied only after this run completed. The first full unit hold returned **207 passed, 1
failed in 120.94 s**. The only failure was the pre-existing per-candidate read-count
assertion, which needed the new final root-holder read. The new races file passed all 25
cases. Tests-first follow-up `1209b0f60` corrects that assertion, adds stale-row and
overlapping-identity regressions, varies the mid-scan change after any number of
real-child reads, and checks a later-listed stranger. The focused red run against
`a447cf1f5` returned **7 failed, 2 passed, 25 deselected in 3.12 s**. Four failures
reproduce the stale zombie or missing-start row, one the newer responsibility seed
masked by old ownership, and two a new responsibility birth discarded because an older
owned birth was recycled. The two controls retain a live owned birth and correctly
exclude a pid when both recorded births have been recycled.

Refinement `7a45dc285` preserves the live state and birth already read from the kernel,
omits contradictory snapshot shapes, and retains an identity even when its snapshot row
lacks a start (with the existing unverifiable diagnostic). `_contain` passes conflicting
recorded births as census alternatives; a pid is pruned only after all its recorded
births are proven different. Recycle explanations include `also_recorded` when needed. A
new responsibility member is saved even when that pass excluded an older owned birth at
the same pid. None of these alternatives modifies signal ownership. All five no-signal
paths now cover both an empty ownership map and an older owned incarnation.

Final unit validation: **217 passed in 54.25 s**, including all 34 cases in
`test_census_responsibility_races.py`. The fake-daemon group returned **29 passed, 4
skipped in 6.52 s**. All 20 no-signal cases passed, including the older-owned-birth
variants, as did the separate stale-group-authority regression. The four real-escaper
cases skipped because boot identity inspection is unavailable in this sandbox. State and
module-name checks returned **38 passed in 34.32 s** (37 state contract cases and one
module-name case). Positive validation totals **284 passed, 4 skipped**, with no
failures or errors. Both mutations completed in the foreground runner under separate
shared-lock holds, with the required failures and no unexpected errors. All holds were
started before the conservative 13:42 UTC cutoff. The default uv cache rejected writes
with EPERM, so the authorized fallback cache is inside that scratch directory. The test
environment is CPython 3.14.7 with the GIL enabled. Every pytest uses its own foreground
`lockf -k` hold, without a lock timeout, one worker, `env -u SUBFLEET_RETENTION_DORMANT
-u PYTHONPATH`, and scratch Hypothesis storage. New timing properties use the bounded ci
profile (40 examples).

Process inspection probe: `/bin/ps -p <this task's own Python pid> -o lstart=` was
denied with EPERM. Real process tests require the hub; no live daemon, live store,
unrelated process or caller checkout was accessed for mutation.

Shared Git index writes were denied with EPERM. Commits therefore use workspace-local
`.git-local` on `fix/census-responsibility-p3`; the original `.git` pointer and shared
index are unchanged. Inspect with `GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git
log`. No push or Git remote operation; no sub-agents.

Final positive validation, by file:

| File under `tests/` | Passed | Skipped | Status |
|---|---:|---:|---|
| fake/test_census_escapers_daemon.py | 0 | 4 | Inspection denied |
| fake/test_census_recycled_pid_daemon.py | 29 | 0 | Passed |
| fake/test_state_contract.py | 37 | 0 | Passed |
| unit/test_census_identity_uncertainty.py | 8 | 0 | Passed |
| unit/test_census_procargs.py | 52 | 0 | Passed |
| unit/test_census_recycled_pid.py | 25 | 0 | Passed |
| unit/test_census_responsibility_failures.py | 20 | 0 | Passed |
| unit/test_census_responsibility_identity.py | 17 | 0 | Passed |
| unit/test_census_responsibility_races.py | 34 | 0 | Passed |
| unit/test_census_survivors_review.py | 14 | 0 | Passed |
| unit/test_procs.py | 47 | 0 | Passed |
| unit/test_test_module_names.py | 1 | 0 | Passed |
| process/test_census_responsibility_process.py | 0 | 0 | Not run: inspection denied |
| process/test_census_raw_markers_process.py | 0 | 0 | Not run: inspection denied |
| process/test_guardian_process.py | 0 | 0 | Not run: inspection denied |

Mutation (a), compiled in memory before pytest import: restoring the blanket clear
returned **2 failed, 32 deselected in 17.02 s**. Inputs A and B both failed at the
first-pass assertion that K remains a responsibility member. Mutation (b), omitting the
after-scan holder read, returned **3 failed, 31 deselected in 6.46 s**: the known-id
mid-scan input and both exact S=42/100, C=60/101 late-successor timings. All failed
because stranger 60 re-entered the responsibility source. Both mutations were applied in
memory only; the production file remained unchanged.

The required deciding unit, fake, state-contract and module-name groups are complete.
The three process files and four skipped real-escaper cases need the hub's
inspection-enabled host. The rest of the repository suite and Python 3.12 were not run.
No deep Hypothesis profile was used.

Delivery bundle: `.git-local/census-responsibility-p3-r4.bundle`, requiring base
`85a7b384f906`. All commits are on the assigned local branch and carry the requested
trailer; only named source, test, contract and report files were staged. No logs, JSON
evidence or bundles were staged.
Cleanup: removed task scratch (including the uv cache, pytest temporary
stores, XML reports and mutation runners), the task-created virtualenv and
bytecode caches with Python `shutil`. No `.hypothesis` directory, pytest
wrapper or task-owned test process remains. Git whitespace checks passed.
