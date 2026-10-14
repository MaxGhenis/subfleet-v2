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
