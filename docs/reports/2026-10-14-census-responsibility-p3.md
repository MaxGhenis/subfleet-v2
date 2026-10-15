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
