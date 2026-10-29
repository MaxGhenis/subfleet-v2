# Retention dependency pins: fix round r2, 2026-10-29

Base: `ad4d612ee93a` (fix round r1 plus the hub's review r2 evidence and release/217 merge). First recorded clock: 10:46:55 UTC; first correctness hold: 10:47:51 UTC. The four-hour new-hold cutoff is 14:46 UTC; the six-hour wall is 16:46 UTC. Only the assigned worktree is edited. The sandbox denied the requested Darwin scratch directory, so the equivalent private fixture scratch is `/private/tmp/sf-fix272r2`. Shared Git metadata also refused writes; ordered commits use private metadata and are delivered in a verified bundle. These are operational departures; no product decision changed.

## Current behavior after review r3 and fix round r3

Review r3 showed that round r2's descriptor metadata and resolved-path checks
did not revalidate an original repository alias, and that recovery preceded
the legacy census. [Fix round r3](2026-10-29-retention-pins-fix-r3.md) repairs both:
discovery retains original `.git` spellings and all followed pointers/targets,
then revalidates them after traversal outside transactions. Changed or vanished
aliases record incomplete evidence. The live/exporting legacy census now precedes
recovery and is shared with the archive context; if it raises, recovery makes
no move. Recorded-evidence controls still prevent quarantine.

An alias swapped and swapped back within one traversal remains invisible to
these final comparisons. Metadata-preserving writes within one timestamp tick
and dependencies introduced after discovery are additional observation bounds.
The decisions, six-case red/green result and 452-node ledger below are dated
round-r2 evidence, not coverage claims for the new r3 inputs.

## Decisions built

1. **R2-F1:** compare the open descriptor's device/inode, size, nanosecond mtime and nanosecond ctime before and after reading; then compare the final pathname's device/inode with that descriptor. Disappearance, replacement or a detected write records incomplete discovery, including when reading the open descriptor itself succeeds. All filesystem checks stay inside the existing off-transaction discovery. Fix commit: `3d744c816`.
2. **R2-F2:** the legacy census includes every job holding an `out:` lease, terminal jobs included, as well as live jobs. Discovery and canonicalization stay outside transactions. The database-only exporter recheck carries `legacy_places.incomplete` alongside recorded `depends.incomplete`. A job becoming terminal after the census keeps that census's incomplete evidence while its export lease remains. Fix commit: `0b1e4bcb2`.
3. **Tests and mutants:** remove only the three strict expected-failure decorators covering the six repaired cases. No other byte of either review r2 regression file changes; review r2's report and data remain unchanged. Add exactly two named mutants, each run against its named deterministic killing test. Definitions, loaded-source hashes and outcomes accompany this report.
4. **Text:** apply review r2's corrections to the dated r1 claims, then describe the repaired behavior in C-8.3/C-8.4, the changelog, desktop dependency description and I16 evidence, the main report's “Review r2 and fix round r2” section, the fix r1 report and the c4a resolution. Earlier counts, timings and CI snapshots remain dated evidence. The review r2 report remains the immutable record of its reviewed head.

At round r2 the stability check validated only the opened descriptor and resolved pathname, leaving the original-alias and pre-census recovery gaps described above; round r3 repairs them. The descriptor stability check cannot see a write that leaves size, mtime and ctime unchanged inside one timestamp tick. At the current head, post-traversal alias revalidation additionally checks original spellings, but cannot see an alias swapped away and back within that traversal. These checks do not cover arbitrary dependencies introduced after discovery. There is no claim of an atomic filesystem snapshot. The line buffer remains bounded at 1 MiB plus its overflow byte; results/frontier remain proportional to distinct paths. Admission still accepts incomplete discovery. While the affected job is live or exporting, its incomplete evidence prevents retirement of any allocated worktree and names the job and file.

## Red and green

Both review r2 files ran at original `ad4d612ee93a` source with `--runxfail`: **30 passed / 6 failed**. The failed nodes were replacement, rewrite, actual unlink and the three legacy-export stages (database, full-pass, already-exporting). After the fixes, both complete files run without expected-failure decorators: **36 passed**. Separate intermediate checks gave **3 passed** after each fix. Full start/load/duration records are in the hold ledger below and the adjacent JSON/log evidence.

## Execution and evidence

The locked dev group was synced once with Python 3.14.7 and the prescribed sync wrapper. Runs use `uv run --no-sync`, unset `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH`, disable pytest's cache, and keep TMPDIR, Hypothesis storage, fixture homes and uv files under scratch. The supplied fixture baseline isolates stores and blocks real providers/keychain access; no live store, production CLI, provider, repository fetch/merge/push, external message or sub-agent is used. Network is used only for environment sync. The only mutants are the two requested for this round.

One pytest per hold; two suite workers, with at most four owned pytest holds active or queued when the two mutants queue. No xdist. Larger files split by `-k`, retaining all normal property settings and phases, with seed 272. Each pytest has a 570-second whole-run alarm. Long runs stay attached to tool-managed background sessions and are polled to completion. The observer uses `os.getloadavg()` after lock admission because this sandbox denies the wrapper's sysctl probes. It records actual test phases and resource peak without importing test modules early. The initial and later observer sources are retained: later holds use neutral `cases-<pid>` fixture paths so rendered paths cannot accidentally satisfy the existing fence tests' assertions about the word “retention”; hold selection and pytest settings are unchanged. Ordinary test results below exclude red runs, intermediate three-case checks and mutant kills.

The complete results and delivery accounting follow the PR-body replacements below.

## PR-body replacements for this round

- **Status (saved lines 1 and 3):** Review r2's R2-F1 and R2-F2 are fixed on top of `ad4d612ee93a`; approving review and CI remain external gates. Retention remains dormant. Review r1's original fixes, exporter destination join and canonical job-directory boundary remain covered.
- **Changes (saved lines 4, 5 and 18):** Detect alternates disappearance, replacement and writes during discovery by descriptor metadata plus final pathname identity. Discover terminal legacy `out:` holders outside transactions and preserve incomplete evidence through database-only exporter rechecks, including a job ending after census. Incomplete evidence keeps allocated worktrees while the job is live or exporting and identifies the job/file; admission proceeds. The observation bounds are stated in the fix r2 report.
- **Invariants:** C-6.5, C-6.11, C-8.3, C-8.4 and I16; filesystem discovery stays outside transactions, and selection/commit use database-only dependency rechecks. Historical continuation 5's 169 nodes and earlier measurements remain dated evidence.
- **Evidence (saved line 7):** Both review r2 files: red **30 passed / 6 failed** with `--runxfail` at base, green **36 passed** without expected-failure marks. Link this report's per-file ledger and two named mutant kills for current verification.
- **Merge gates (saved lines 29 and 34):** Obtain an approving review for the repaired R2-F1/R2-F2 inputs and complete CI before merge or re-enablement. No new CI snapshot was read in this round.

The local report supplies these replacements; no live PR body was read or written.

## Final test accounting

**452 distinct ordinary nodes passed across all 17 requested files; zero final failures, skips, expected failures or unrun nodes.** All 32 ordinary holds completed. Counts exclude the baseline red, intermediate checks and two successful mutant kills. First observed starts below are after lock admission, on 2026-10-29 UTC. Full split starts and all three load averages follow.

| File | P/F/S/X/U | First start UTC | Load (1/5/15 min) |
|---|---|---|---|
| `tests/fake/test_retention_dependency_review_r2.py` | 21/0/0/0/0 | 10:49:47.537 | 16.79 / 13.51 / 11.33 |
| `tests/unit/test_retention_alternates_review_r2.py` | 15/0/0/0/0 | 10:49:47.537 | 16.79 / 13.51 / 11.33 |
| `tests/fake/test_retention_dependency_review_r1.py` | 18/0/0/0/0 | 10:49:47.540 | 16.79 / 13.51 / 11.33 |
| `tests/fake/test_retention_dependency_incomplete.py` | 19/0/0/0/0 | 10:50:03.059 | 19.15 / 14.25 / 11.64 |
| `tests/unit/test_retention_alternates.py` | 23/0/0/0/0 | 10:50:26.396 | 22.87 / 15.36 / 12.10 |
| `tests/unit/test_retention_job_dependencies.py` | 12/0/0/0/0 | 10:51:25.623 | 23.33 / 16.62 / 12.77 |
| `tests/fake/test_retention_dependencies.py` | 30/0/0/0/0 | 10:51:11.293 | 20.63 / 15.73 / 12.40 |
| `tests/fake/test_retention_dependency_spelling.py` | 16/0/0/0/0 | 10:51:41.802 | 22.58 / 16.77 / 12.89 |
| `tests/unit/test_retention_dependency_units.py` | 19/0/0/0/0 | 10:51:58.786 | 25.34 / 17.77 / 13.34 |
| `tests/unit/test_retention_folded_pin.py` | 38/0/0/0/0 | 10:51:59.270 | 25.34 / 17.77 / 13.34 |
| `tests/unit/test_retention_dependency_properties.py` | 4/0/0/0/0 | 10:53:09.060 | 21.33 / 18.20 / 13.85 |
| `tests/unit/test_retention_shared_folders.py` | 62/0/0/0/0 | 10:56:17.825 | 25.24 / 20.09 / 15.38 |
| `tests/fake/test_detached_retention_fence.py` | 89/0/0/0/0 | 11:04:34.414 | 41.49 / 34.03 / 24.86 |
| `tests/fake/test_writer_retention_fence.py` | 23/0/0/0/0 | 10:52:23.209 | 24.82 / 18.25 / 13.64 |
| `tests/fake/test_state_contract.py` | 37/0/0/0/0 | 11:12:14.298 | 38.73 / 39.07 / 31.69 |
| `tests/unit/test_test_module_names.py` | 1/0/0/0/0 | 11:13:17.304 | 38.83 / 39.30 / 32.29 |
| `tests/unit/test_invariants_index.py` | 25/0/0/0/0 | 11:14:04.794 | 35.70 / 38.36 / 32.34 |

## Named mutant table

Each mutation matches once, compiles, and loads exactly one mutated `subfleet.dependencies` module from this workspace. The selected test already passes in the ordinary green run. Each mutant exits with its named assertion failing; source files stay unchanged. These are the only two mutant runs this round.

| Mutant | Killing test | Result | Start UTC | Load (1/5/15 min) |
|---|---|---|---|---|
| `r2-post-read-identity-check-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | KILLED (0 passed / 1 failed) | 11:13:17.213 | 38.83 / 39.30 / 32.29 |
| `r2-terminal-legacy-incomplete-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | KILLED (0 passed / 1 failed) | 10:58:20.868 | 45.02 / 27.41 / 18.79 |

## Complete hold ledger

Durations are observer wall time inside the admitted process, excluding lock waits. Every hold used the shared lock; no outside-lock fallback, timeout or harness retry was needed. No single pytest reached ten minutes.

| Hold | Start UTC | Load (1/5/15 min) | Passed / failed | Seconds |
|---|---|---|---|---|
| `red-review-r2` | 10:47:51.739 | 16.49 / 11.71 / 10.45 | 30 / 6 | 36.98 |
| `green-f1` | 10:48:46.110 | 15.47 / 12.36 / 10.78 | 3 / 0 | 3.76 |
| `green-f2` | 10:49:06.956 | 15.98 / 12.70 / 10.94 | 3 / 0 | 5.57 |
| `green-review-r2` | 10:49:47.537 | 16.79 / 13.51 / 11.33 | 36 / 0 | 37.15 |
| `test_retention_dependency_review_r1` | 10:49:47.540 | 16.79 / 13.51 / 11.33 | 18 / 0 | 14.17 |
| `test_retention_dependency_incomplete` | 10:50:03.059 | 19.15 / 14.25 / 11.64 | 19 / 0 | 49.05 |
| `test_retention_alternates` | 10:50:26.396 | 22.87 / 15.36 / 12.10 | 23 / 0 | 43.93 |
| `test_retention_dependencies` | 10:51:11.293 | 20.63 / 15.73 / 12.40 | 30 / 0 | 46.41 |
| `test_retention_job_dependencies` | 10:51:25.623 | 23.33 / 16.62 / 12.77 | 12 / 0 | 12.97 |
| `test_retention_dependency_spelling` | 10:51:41.802 | 22.58 / 16.77 / 12.89 | 16 / 0 | 16.28 |
| `test_retention_dependency_units` | 10:51:58.786 | 25.34 / 17.77 / 13.34 | 19 / 0 | 23.26 |
| `test_retention_folded_pin` | 10:51:59.270 | 25.34 / 17.77 / 13.34 | 38 / 0 | 69.57 |
| `test_writer_retention_fence` | 10:52:23.209 | 24.82 / 18.25 / 13.64 | 23 / 0 | 44.20 |
| `test_retention_dependency_properties-1` | 10:53:09.060 | 21.33 / 18.20 / 13.85 | 1 / 0 | 112.57 |
| `test_retention_dependency_properties-2` | 10:53:09.930 | 21.33 / 18.20 / 13.85 | 1 / 0 | 110.80 |
| `test_retention_dependency_properties-3` | 10:55:04.957 | 20.22 / 18.23 / 14.37 | 1 / 0 | 223.21 |
| `test_retention_dependency_properties-4` | 10:55:04.958 | 20.22 / 18.23 / 14.37 | 1 / 0 | 71.26 |
| `test_retention_shared_folders-1` | 10:56:17.825 | 25.24 / 20.09 / 15.38 | 12 / 0 | 35.55 |
| `test_retention_shared_folders-2` | 10:56:54.830 | 22.52 / 20.04 / 15.57 | 16 / 0 | 84.02 |
| `mutant-r2-terminal-legacy-incomplete-dropped` | 10:58:20.868 | 45.02 / 27.41 / 18.79 | 0 / 1 | 6.40 |
| `test_retention_shared_folders-3` | 10:58:32.368 | 40.92 / 27.13 / 18.79 | 5 / 0 | 356.36 |
| `test_retention_shared_folders-4` | 10:58:49.873 | 37.52 / 27.21 / 19.01 | 4 / 0 | 196.05 |
| `test_retention_shared_folders-5` | 11:02:13.270 | 34.41 / 30.71 / 22.33 | 9 / 0 | 56.92 |
| `test_retention_shared_folders-6` | 11:03:16.665 | 32.04 / 30.60 / 22.84 | 9 / 0 | 66.27 |
| `test_retention_shared_folders-7` | 11:04:30.046 | 41.45 / 33.90 / 24.76 | 7 / 0 | 80.52 |
| `test_detached_retention_fence-1` | 11:04:34.414 | 41.49 / 34.03 / 24.86 | 12 / 0 | 79.17 |
| `test_detached_retention_fence-3` | 11:06:01.722 | 43.84 / 36.37 / 26.61 | 8 / 0 | 74.75 |
| `test_detached_retention_fence-4` | 11:07:31.540 | 47.12 / 39.28 / 28.71 | 13 / 0 | 62.33 |
| `test_detached_retention_fence-5` | 11:08:41.907 | 45.36 / 40.27 / 29.90 | 18 / 0 | 129.20 |
| `test_detached_retention_fence-6` | 11:10:55.062 | 37.85 / 39.52 / 31.13 | 8 / 0 | 14.69 |
| `test_detached_retention_fence-7` | 11:11:13.281 | 34.24 / 38.57 / 30.99 | 12 / 0 | 22.45 |
| `test_detached_retention_fence-2` | 11:11:41.542 | 36.08 / 38.64 / 31.23 | 10 / 0 | 28.39 |
| `test_state_contract` | 11:12:14.298 | 38.73 / 39.07 / 31.69 | 37 / 0 | 56.24 |
| `mutant-r2-post-read-identity-check-dropped` | 11:13:17.213 | 38.83 / 39.30 / 32.29 | 0 / 1 | 25.27 |
| `test_test_module_names` | 11:13:17.304 | 38.83 / 39.30 / 32.29 | 1 / 0 | 14.99 |
| `test_detached_retention_fence-8` | 11:13:38.125 | 36.94 / 38.83 / 32.28 | 8 / 0 | 19.09 |
| `test_invariants_index` | 11:14:04.794 | 35.70 / 38.36 / 32.34 | 25 / 0 | 13.41 |

Longest admitted run: `test_retention_shared_folders-3`, **356.36 seconds** including observer startup/cleanup. Highest observed pytest RSS: **128.8 MiB**. Scratch stays below the 1.5 GB budget and is removed with the prescribed guarded cleanup after the bundle is verified. All owned tool sessions finish before delivery.

## Not run

No requested local file remains unrun. The full suite, Python 3.12, CI and `tests/frontend/test_core_live.py` were not run. The live frontend is person-only and cannot run on a lane; CI remains responsible for it. No new CI snapshot or approving review is claimed. Historical pending macOS shards in review r2 are not current status evidence.

## Delivery

The two fix commits are followed by the named-mutant evidence commit and the documentation/evidence commit. Every commit message ends with the prescribed Claude Opus 5.5 co-author line. The assigned shared metadata remains untouched after its write refusal. The branch is delivered as `retention-fix272r2.bundle` at the workspace root, excluding base `ad4d612ee93a`; `git bundle verify` must succeed before scratch cleanup. The final handoff names its exact head. No fetch, merge, push or history rewrite is performed.
