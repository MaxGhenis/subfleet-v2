VERDICT #272: REQUEST_CHANGES

Reviewed `7f1888960bb1` against `release/217` at `c2f08a156`. The original R3-F1/R3-F2 inputs pass, but discovery still misses an object-directory dependency.

**P2 — R4-F1: the initial object-directory target is neither recorded nor revalidated.** [dependencies.py:229](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/subfleet/dependencies.py:229) passes `<common>/objects` directly into alternates traversal.

Executed five valid failures:

- Retarget `.git/objects` during discovery, with either no alternates file or an unchanged, hard-linked alternates inode. Both running-job and terminal-exporter cases return complete-looking evidence and retire both allocated trees, including one with a NULL worktree row.
- Keep `.git/objects` stably linked into an owner’s tree. A running borrower successfully reads the owner-only commit through real Git, but retention retires its storage.

These require neither a swap-back nor a metadata-preserving write. Fix: record the resolved initial object-directory dependency and retain its original spelling for post-traversal revalidation. An in-memory trial passes all six focused cases, including the terminal-export negative control.

**P3 — R4-F2: old-journal persistence is described too broadly.** [The main report:13](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/docs/reports/2026-10-27-retention-dependency-pins.md:13) and fix-r3 report say older journals acquire `job_folder` and persist it before any rename.

Executed real journals with that field removed in `locked`, `quarantined` and `archived` states. The locked case persists it before quarantine’s renames. The other two reach `final_check` and archive publication without it. This needs a wording correction:

> New journals record `job_folder` in `begin`. Old journals entering quarantine gain it in memory and save it before quarantine’s renames. Already-quarantined or archived journals can reach the final check and publication without it; commit computes the fallback outside the transaction.

The [new regression file](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/tests/fake/test_retention_dependency_review_r4.py:13) finishes with **48 passed and 5 strict expected failures naming R4-F1**. Its final focused head run with `--runxfail` gives **5 failed / 1 passed**.

Checks 1–7:

1. **Changes required.** Original R3-F1 live/exporting `.git`-symlink inputs pass. Another 44 controls pass across eleven checkout, `.git`, gitdir, commondir, alternate-entry and parent spellings, testing retargets/removals in both states. Detected failures retain both allocated trees. Discovery and canonicalization remain outside transactions; transactional rechecks use database reads and strings. R4-F1 is an additional gap. For the distinct-spelling bound and duplicate-error skip, 1,000 repeated resolutions with first-only, recheck-only and combined failures each preserve one incomplete observation.
2. **Pass — R3-F2 fixed.** Both legacy recovery cases and both recorded-evidence controls pass. Separate controls prove census construction precedes recovery, the pass/archive context share the identical object, and a census exception prevents recovery entirely.
3. **Product pass; text correction required.** The unchanged T2 property passes its five explicit and 40 generated examples, preserving the live cwd and retiring the selected job afterward. The repair removes the premature guard save and persists new-journal spelling in `begin`. Quarantine saves before its own renames; already-quarantined/archived recovery paths need the qualification above.
4. **Pass.** Exact reconstruction proves only the authorized marks covering four r3 cases disappeared from that file. All seven required mutants die to their named killers. The additional object-alias mutant also dies against the green in-memory trial.
5. **Pass.** Both merges preserve every complete contract clause from a parent, without missing clauses or new duplicates. The warned-about inherited repetitions are not attributed to this branch.
6. **Changes required.** C-8.3’s census/recovery description is accurate. Preserve C-8.4’s normative guarantee and repair R4-F1. The desktop and report descriptions need the additional object-directory gap and the journal qualification. Historical 452/682-node evidence remains valid as dated evidence. [Exact text replacements and locations](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/docs/reports/2026-10-29-retention-pins-review-r4-data/text-corrections.json) are preserved.
7. **CI pending.** The single permitted check read returned **pass 4, pending 6, fail 0, skipping 0, cancel 0**. Passing: Python 3.12 shards 1/4 and Python 3.14 shards 2/4. Pending: Python 3.12 shards 2/3/5 and Python 3.14 shards 1/3/5. Neither D-CI-GIT256 nor D-QT-FLAKE1 appears as a reported failure, and no T2 failure is reported. Pending test outcomes remain unknown. [Snapshot](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/docs/reports/2026-10-29-retention-pins-review-r4-data/pr-checks.txt).

All **680 requested existing nodes passed**. Final ordinary accounting is **733 passed / 5 strict expected failures**, including the new controls and five scratch probes. An initial stable-terminal-export oracle was incorrect; it was corrected to permit retirement with complete discovery, and that obsolete failure is excluded.

All times below are UTC on October 30. `F` means `tests/fake`, `U` means `tests/unit`. Starts/loads are the first ordinary hold per file; the [complete 85-hold ledger](/Users/maxghenis/.subfleet/worktrees/20261029-222604-pr272-review-r4/docs/reports/2026-10-29-retention-pins-review-r4-data/ledger.json) includes every split hold, phase and duration.

| File | Passed / xfailed | Start | Load 1/5/15 |
|---|---:|---|---|
| F/test_turn_retention_job_properties.py | 1 / 0 | 02:51:31 | 20.56/25.08/25.96 |
| U/test_retention_archive.py | 108 / 0 | 02:51:32 | 20.67/25.02/25.94 |
| F/test_retention_dependency_review_r3.py | 6 / 0 | 02:52:11 | 21.70/24.86/25.84 |
| U/test_retention_dependency_aliases.py | 10 / 0 | 02:52:35 | 21.04/24.48/25.68 |
| F/test_retention_recovery_census.py | 2 / 0 | 02:52:43 | 25.43/25.29/25.95 |
| Review-r3 scratch probes | 2 / 0 | 02:52:51 | 24.92/25.19/25.91 |
| F/test_retention_dependency_review_r2.py | 21 / 0 | 02:53:09 | 26.21/25.49/26.00 |
| U/test_retention_alternates_review_r2.py | 15 / 0 | 02:53:58 | 24.62/25.35/25.93 |
| F/test_retention_dependency_review_r1.py | 18 / 0 | 02:54:14 | 21.66/24.66/25.67 |
| F/test_retention_dependency_incomplete.py | 19 / 0 | 02:54:30 | 21.13/24.40/25.56 |
| U/test_retention_alternates.py | 23 / 0 | 02:55:24 | 22.00/24.21/25.42 |
| U/test_retention_job_dependencies.py | 12 / 0 | 02:56:31 | 27.28/25.30/25.74 |
| F/test_retention_dependencies.py | 30 / 0 | 02:56:52 | 26.29/25.26/25.72 |
| F/test_retention_dependency_spelling.py | 16 / 0 | 02:58:03 | 24.17/25.02/25.60 |
| U/test_retention_dependency_units.py | 19 / 0 | 02:58:28 | 26.87/25.56/25.77 |
| U/test_retention_folded_pin.py | 38 / 0 | 02:59:01 | 24.54/25.30/25.68 |
| U/test_retention_archive_properties.py | 2 / 0 | 03:00:34 | 26.42/25.17/25.56 |
| U/test_retention_dependency_properties.py | 4 / 0 | 03:00:48 | 29.57/25.93/25.82 |
| U/test_retention_nested_leases.py | 27 / 0 | 03:02:08 | 18.92/23.69/24.99 |
| U/test_retention_salvage_properties.py | 2 / 0 | 03:03:35 | 21.02/22.86/24.53 |
| U/test_retention_shared_folders.py | 62 / 0 | 03:06:00 | 19.70/21.25/23.59 |
| Old-journal scratch probes | 3 / 0 | 03:07:52 | 28.89/24.54/24.63 |
| F/test_detached_retention_fence.py | 89 / 0 | 03:09:46 | 25.82/25.07/24.84 |
| U/test_retention_sweep_race.py | 13 / 0 | 03:10:27 | 29.50/26.01/25.19 |
| U/test_retention_81_notes.py | 38 / 0 | 03:16:47 | 25.90/26.79/26.02 |
| F/test_writer_retention_fence.py | 23 / 0 | 03:17:41 | 25.67/26.52/25.96 |
| F/test_state_contract.py | 37 / 0 | 03:18:14 | 24.87/26.25/25.89 |
| U/test_test_module_names.py | 1 / 0 | 03:18:41 | 22.90/25.72/25.71 |
| U/test_invariants_index.py | 25 / 0 | 03:18:43 | 21.71/25.42/25.60 |
| F/test_turn_retention_cwd.py | 6 / 0 | 03:18:46 | 21.71/25.42/25.60 |
| F/test_turn_retention_quarantine.py | 10 / 0 | 03:18:58 | 18.91/24.63/25.32 |
| F/test_turn_retention_cwd_properties.py | 1 / 0 | 03:19:16 | 21.25/24.86/25.39 |
| F/test_turn_retention_job_directory.py | 2 / 0 | 03:19:24 | 19.14/24.29/25.18 |
| F/test_retention_dependency_review_r4.py | 48 / 5 | 03:22:37 | 20.29/23.20/24.58 |

The mutant table uses R2/R3/R4 for their respective `test_retention_dependency_review_rN.py` files.

| Mutant | Named killer | Result |
|---|---|---|
| r2-post-read-identity-check-dropped | R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Killed |
| r2-terminal-legacy-incomplete-dropped | R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Killed |
| r3-post-traversal-alias-check-dropped | R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Killed |
| r3-census-moved-after-recovery | R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Killed |
| r3-write-metadata-check-dropped | probes::test_changes[same-size-running] | Killed |
| r3-final-path-check-dropped | probes::test_changes[symlink-info-running] | Killed |
| r3-terminal-census-live-only | R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Killed |
| r4-initial-objects-revalidation-dropped | R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Killed |

The seven required mutants use head source. The additional mutant drops object-alias observation from the tested in-memory repair while retaining resolved-target recording. Every kill is a call assertion with passing setup/teardown. Definitions, hashes and timings are preserved.

Replace the live PR lead with:

> Fix round r3 closes the original R3-F1/R3-F2 inputs and the T2 journal regression. Review r4 at 7f1888960 requests changes for R4-F1: the initial `<common>/objects` target is neither recorded nor revalidated. Five valid real-Git cases fail: four retargeting cases covering live jobs and terminal exporters, and a stable live-job dependency. Retention remains dormant; repairing R4-F1, approving review and completed CI remain merge gates.

Also qualify the old-journal bullet as above, label 682 passes/seven mutants as the fix-round result at `4d8fa5d55`, put the stale fix-r2 “current checks” description in the past tense, and change Next to repairing R4-F1, correcting the wording, obtaining approval and completing CI.

For the descriptive discovery paragraphs in the desktop/main/fix-r2/fix-r3 reports, use:

> Current revalidation covers tracked checkout/.git aliases, gitdir and commondir pointer paths/targets, and parsed alternate-entry paths. At 7f1888960 it neither records nor revalidates the initial `<common>/objects` target. Stable live dependencies and retargeting with absent or unchanged alternates can therefore permit retirement (R4-F1), independently of the documented swap-back and timestamp bounds.

Runs used Python **3.14.7**, Git **2.55.0**, normal property settings and the shared lock, with no outside-lock fallback. First hold: **02:51:31**, within two minutes of starting. Longest pytest: **300.43 seconds**; peak observed pytest RSS: **128.8 MiB**.

Not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`, the full suite, local Python 3.12, and the person-only live frontend. Historical base-red runs were inspected rather than repeated.

No commits, pushes or GitHub writes were made. The new test and evidence remain in the worktree. All owned processes ended; **593.7 MiB** of scratch was removed with guarded cleanup, and no generated workspace artifacts remain.

DONE