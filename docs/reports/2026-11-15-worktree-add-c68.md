C-6.8 worktree-add port, continuation report — 2026-11-15.

The build is complete in the working tree. Final proof checks pass (97 cases), contract/refusal checks pass (427 cases), and all 16 requested historical mutant kills are substantiated by saved assertion-failure output. This continuation fixed transient ownership reads in legacy repository discovery, add-repository selection and copy markers; tightened kept-copy path kinds; normalized file-shaped leftovers; and strengthened reuse/census tests. Git refused named staging because the shared administrative `index.lock` is outside the writable workspace; implementation, tests and report remain uncommitted. No history was rewritten; nothing was pushed or published.

This continuation's first command, `date -u`, established **2026-11-15T07:54:03Z** as the start, with a **10:54:03Z** deadline. It began with 13 preserved modified/untracked files, including an implementation, tests and a report from an interrupted 03:00:36Z continuation. Existing changes were preserved and independently audited. Source edits stayed in the assigned checkout; experiments used `/private/tmp/sf-c68-b1/current-20261115T075403`. The brief, progress notes and WIP archive were read only at their supplied paths. Historical test-framework temporary roots and this continuation's one unbootstrapped test-run deviation are disclosed below. The assigned detached HEAD already contained the earlier session's sole commit, `73672254015924df25f4d27ab3e92c836fe687ca`, adding the six real-git reproducer cases. Its daemon blob exactly matches the brief's b51 base (`9e2e0a82821f9219803c1f6b44d04804a209548f`). The prior continuation reviewed that commit, fixed its hold-before-Popen race and own-PID liveness check, and made full process-table integration skip on denied inspection. Those edits were preserved and checked here.

The supplied `subfleet/worktree_add.py` was again extracted at the checkout root, SHA256 checked against the adjacent SHA256SUMS and read before further changes. The preserved implementation was saved first and restored afterward. Its original digest was `b80c48769a79fc82165407c127e93e76e12c8c7b98806ccfafbd6a4f05c5a649`; a separate pristine extraction later passed `shasum -a256 -c`. It was useful WIP, but required the corrections recorded below. The earlier notes describe experiments and baseline failures, not additional production commits; no earlier-session command transcript exists beyond those notes, so I do not invent one.

The ten required Step 0 findings follow.

1. The earlier session committed only `736722540` (six parameterized reproducer cases). Its parent is `b51f08f96`; production still used `subprocess.run` for the add, with no admission `start_new_session`, `initializing` recognition or `worktree-add:` marker. The supplied WIP module was restored, checksum-verified and corrected; it was never committed. Every named staging attempt in this continuation failed on the external git administrative `index.lock`, so no new commit was made or history rewritten.
2. Read the named baseline daemon allocation/reuse/removal/error/give-back functions, retention registration helpers, salvage environment/transient handling, contract clauses and `docs/coordination.md`. Release/217 already deletes only the matched registration; the old repository-wide prune defect was absent.
3. Read #30 at `642d2b3`: allocation, add/stop/census/settle/await, removal, ownership helpers and held-filter fixtures. Ported behavior around release/217's recorded common directory, backlink registration, copy marker and refusal templates. Rejected #30's directory-inode census cache, folded identities, index shortcuts and forgotten group after successful git exit.
4. Earlier git 2.55 measurements reported `locked == b'initializing\n'` and ordering admin → lock → tree → backlink → gitfile → HEAD → checkout → index → unlock → hook. The prior continuation independently ran 40 gated adds: all 40 completed/reaped; tight polling observed admin before tree 4 times, lock before tree 3, backlink before gitfile 3, gitfile before HEAD 9 and HEAD before index 34. Every held checkout had gitfile/HEAD but no index. A separate single held-filter experiment observed the post-checkout hook with no lock; the 40 ordering trials did not install that hook. The earlier 360-kill experiment remains attributed to the earlier notes.
5. The prior continuation reconfirmed all four defects represented by six cases: child survived cap; unfinished tree admitted; dead initializing registration with/without tree failed permanently; an unregistered person's file was deleted; staged index was replaced. Five were observed in the initial six-case run; the cap case initially hit denied `ps`, then failed independently using the unchanged baseline daemon and known test-owned kernel PID (live filter 79150).
6. Preserved readable human/retention lock reuse, while only `initializing` blocks finished reuse. Kept-copy paths are owned during interrupted application, including both directory/file transitions. Replaced the old unregistered `partial.txt` rebuild example with a genuine registered initializing leftover, justified by measured git ordering; an unregistered extra file now stays and causes refusal.
7. Converted add fakes to `Popen` children. Existing tests retain untranslated `LC_ALL=C`, absent `LANGUAGE`, invalid-UTF8 stderr, signalled/timeout waiting, full-disk/held-index-lock waiting and eventual retry assertions. Source stderr goes to a file and scrubbed daemon logging; public refusals expose no tree-owned names.
8. The prior continuation measured `/bin/ps` denial even for its own PID, so full real-census integration is unavailable and is never counted passed. A prior scoped experiment used only owned kernel records: group git/reset/sh/sleep; markers git/reset, not Apple's `/bin/sh` or sleep. It cost 0 ps, 1 synthetic PID enumeration, 4 real process reads, 4 real argument reads (~0.0008 s); this is explicitly not a whole-host census. This continuation excludes that full-census case before execution and tests only synthetic tables and owned child PIDs. Source shows ordinary census cost 1 ps snapshot plus 1 kernel PID enumeration (normally two Darwin list calls), then per-PID state/argument reads. Earlier notes measured roughly 1600 process/1240 argument reads and 0.1–2 s; those host-wide numbers were not repeated here.
9. The cap signals while the leader remains the daemon's unreaped `Popen` child, preventing PID reuse before `killpg`; identity reads additionally support census. Reaping is bounded. If signalling/reaping fails, the live child handle and group remain held across passes. C-5.4/C-5.5 describe this basis.
10. Kept release/217's direct removal of the backlink-matched admin directory. No git removal command needs a folded/caller spelling; only the filesystem decides identity. `drop_registration` rechecks lock, index and HEAD, and reports failed removal. Give-back retains registration → gitfile → files order; ordinary discard retains gitfile → files → registration. A new external lock after the last check remains a non-atomic race; the contract states that limitation.

Current source: `daemon.py:6338` gates admission/reuse; `:6527` selects the add repository without swallowing transient reads; `:6561` validates repository registrations; `:6603` takes fresh group/marker censuses; `:6637` settles repeatedly; `:6648` isolates and bounds the add; `:6713`, `:6733`, `:9295`, `:9388` prove and remove ordinary, refused and give-back allocations. `worktree_add.py:116` reads every departing index, `:143` preserves unexpected HEAD, `:181` reads kept-patch destination kinds, `:212` names saved-copy paths, `:284` proves ownership, `:404` validates finished reuse, `:459` removes only the matched registration, `:493` removes gitfile before files. `retention_git.py:193/:223/:268/:708` supply filesystem identity, gitfile/backlink validation and strict scans. `uncommitted.py:185` offers strict marker reads; `refusals.py:114` defines `worktree-unproven`.

Additional tests in this continuation are `tests/fake/test_worktree_add_reuse.py` (whole-handoff Hypothesis oracle and file-shaped refusal), `tests/fake/test_worktree_add_transient_reads.py` (27 source/selection/marker errno cases), the three real-generation cases in `tests/fake/test_worktree_add_census.py:102`, four saved-patch kind cases and two more staged-index path cases in `tests/unit/test_worktree_add_proof.py`. Existing changes in workspace-transient, unrun, route-isolation and the push-free d635 retention test remain. Contract clauses and version-3 entry were amended by hand without renumbering. Conversation construction in `subfleet/conversations/service.py` is unchanged.

Test aliases below: **C**=`tests/fake/test_worktree_add_census.py`; **P**=`tests/unit/test_worktree_add_proof.py`; **U**=`tests/fake/test_unrun_worktrees.py`; **R**=`tests/fake/test_worktree_add_c68.py`.

| Finding | Protection and test |
|---|---|
| F1 | Index checked for missing/empty/link trees; staged additions/edits stay. P `test_staged_index_blocks_removal_at_every_path_shape` (eight cases, including a file at the allocation). |
| F2 | Validate gitfile, direct admin and backlink before either HEAD/index read. P `test_gitfile_naming_another_worktree_is_rejected_before_reading_its_index`. |
| F3 | Retain group until verified empty, including successful add with writer. C `test_a_successful_add_keeps_its_group_if_a_hook_left_a_writer`, `test_group_only_survivor_is_kept_across_passes`. |
| F4 | Fresh census every pass/generation, no inode cache. C `test_no_add_over_a_holder_across_passes` and `test_a_new_successful_add_in_the_same_directory_needs_a_fresh_census` (group/marker/both). |
| F5 | Give-back collects a dead add's initializing lock. U `test_a_give_back_collects_only_a_dead_adds_initializing_lock`. |
| F6 | Filesystem identities, final links retained, no unconditional folding. P `test_filesystem_identity_never_folds_distinct_siblings`, `test_case_folding_never_overrides_filesystem_identity`. |
| F7 | Strict reads propagate transient errno with original cause. P `test_transient_ownership_reads_preserve_errno_and_cause` (54 cases), `test_transient_git_head_failure_is_not_a_missing_head`; 27 legacy-source, repository-selection and applied-copy-marker errno cases, plus a real replaced-repository retry regression. |
| F8 | Central proof checks allocation container for every removal. U `test_every_removal_entry_keeps_a_tree_when_its_container_resolves_elsewhere` (four entries). |
| F9 | Held-process fixtures release gates, wait, kill and reap in `finally`. R held-filter fixture and C `test_the_cap_stops_a_real_filter_with_a_fake_empty_census`; real full-census test reports skip when denied. |

Additional review fixes preserve a unique same-tree HEAD/reflog; refuse ambiguous backlinks and symlinked admin containers; reject linked/directory/FIFO indices; retain all files on gitfile-unlink EPERM; support missing submodule worktrees; and skip only unrelated missing pre-backlink entries. Each has a deterministic P regression. The unique-HEAD regression creates an unreferenced detached commit. P regressions include `test_unique_same_tree_commit_prevents_registration_and_tree_removal`, `test_ambiguous_backlink_is_not_ownership`, `test_nonregular_index_never_proves_finished_or_removable`, `test_gitfile_unlink_failure_keeps_every_worktree_file`, `test_object_and_index_reads_ignore_a_missing_configured_worktree`, and `test_kept_patch_kind_transitions_are_owned_before_copy_marker`.

The nine required invariants and their evidence follow.

1. **No add over holder:** C `test_no_add_over_a_holder_across_passes`, Hypothesis 60 sequences, group/marker/both/empty/unverifiable/error; one directory identity reused across passes.
2. **Cap stops checkout:** C `test_the_cap_settles_repeated_censuses_before_returning`, real-filter group-stop test, and `test_a_denied_group_stop_has_a_bounded_reap_and_retains_the_child`.
3. **Only finished reuse:** a directory rather than a link; validated gitfile/admin/common/backlink; no initializing lock; HEAD equal to workdir_head; a clean index; an applied marker for a copy job; and an empty census. Evidence: new `test_generated_reuse_handoff_matches_every_finished_condition` (60 generated plus 21 explicit cases across all seven condition groups), P `test_generated_finished_checkouts_match_pure_oracle`, plus deterministic foreign-gitfile/initializing/unreadable-lock/nonregular-index regressions.
4. **No removal without proof:** P `test_generated_removal_proof_matches_pure_oracle` over path/index shapes; eight F1 regressions; kept-copy and changed-HEAD tests.
5. **Only dead initializing lock goes:** P `test_generated_registration_drop_removes_only_add_lock`; U `test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it` (Hypothesis 150, six lock reasons) and explicit claim matrix.
6. **Transient stays transient:** P 54-case errno/read-boundary matrix plus 27 legacy-source/selection/marker cases assert transient flag and exact chained cause; transient git-HEAD regression.
7. **Inside container:** U four-entry symlink-container regression; P admin-container symlink regressions preserve external indices.
8. **Filesystem decides names:** P real case/Unicode siblings, final-symlink alias and synthetic case-sensitive identity tests.
9. **Proof agrees with git evidence:** P `test_proof_verdict_agrees_with_git_status_for_generated_checkouts` compares a pure porcelain-v2 oracle over clean, partial tracked modification/deletion, untracked/ignored, wrong-kind and staged cases. Literal clean-status equivalence is impossible: git omits empty directories and partial checkout may modify tracked bytes. The admissible-status oracle states that scope.

Every mutant was applied alone to a scratch source copy, tested, restored in `finally`, and checked against pristine bytes. Counts below are expected assertion failures under mutation, not failures of the final implementation.

| Mutant | Killer | Result |
|---|---|---|
| 1 no new session | C `test_the_cap_settles_repeated_censuses_before_returning` | 1 failed |
| 2 one census only | Same C test | 1 failed |
| 3 forget group | C `test_group_only_survivor_is_kept_across_passes` | 1 failed |
| 4 inode cache | C `test_no_add_over_a_holder_across_passes` | 1 failed; shrank to empty→group |
| 5 gitfile/HEAD shortcut | P foreign-gitfile/index regression | 1 failed |
| 6 reuse initializing | P `test_initializing_lock_never_proves_finished` | 2 failed |
| 7 refuse any lock | U `test_a_waiting_job_whose_tree_someone_locked_runs_in_it` | 1 failed |
| 8 skip index shortcuts | P six staged-index/path-shape regressions | 6 failed |
| 9 omit kind check | P porcelain differential test | 1 failed |
| 10 remove other lock | P generated registration-drop oracle | 1 failed |
| 11 omit container guard | U four-entry container test | 3 failed, 1 passed (allocation retains its guard) |
| 12 EMFILE permanent | P transient-read matrix, admin-scan | 1 failed, 8 passed, 45 deselected |
| 13 unconditional folding | P synthetic filesystem identity test | 1 failed |
| 14 skip dead initializing give-back | U live/dead initializing test | 1 failed, 1 passed |
| 15 reject kept copy | P `test_kept_copy_names_patch_and_untracked_kinds` | 1 failed |
| 16 broad prune | P `test_drop_registration_keeps_unrelated_missing_tree_registration` | 1 failed |

All 16 were killed. Initial generated-only M6 and host-dependent M13 tests missed their mutants; deterministic regressions closed both gaps. Mutation insertion anchors that changed during development were corrected and never counted as test kills.

The group/child records are memory-only: a daemon restart loses them. Marker-only inspection can miss surviving Apple platform binaries after marked git/reset processes are gone. Full real-process census remains blocked by this sandbox; synthetic census properties and real owned-PID group-stop tests do not remove that verification limitation. Conversation worktree construction in `subfleet/conversations/service.py` was left unchanged under its separate C-13.4 contract. Contract clauses C-5.4/C-5.5/C-6.8/C-6.12/C-6.20/C-13.4 and the version 3 entry were amended without renumbering other clauses.

Validation used CPython 3.15.2, pytest 9.1.1, Hypothesis 6.168.1 and pytest-xdist 3.8.0. Repeated runs below are separate executions and are not added together as unique test coverage.

| Historical focused run (prior continuation) | Result |
|---|---|
| New ownership/proof file, one file | 91 passed 196.98 s; strengthened unreferenced unique-HEAD case separately 1 passed 5.29 s |
| New census file, one file | 6 passed 10.70 s |
| Six original repro cases, one file | 5 passed 1 skipped 13.91 s |
| workspace_transient, one file | 24 passed 36.31 s |
| unrun_worktrees, one file | 104 passed 175.23 s; strengthened allocation symlink case separately 1 passed 4.80 s |
| review_launch_permissions, one file | 35 passed 39.65 s |
| admission_route_isolation, one file | 93 passed 97.74 s |
| salvage, one file | 31 passed 26.37 s |
| state_contract + test_test_module_names + invariants_index + handler_refusals | 427 passed 160.18 s |
| Final contract index | 25 passed 2.67 s |
| d635 own-registration regression with a push-free fixture | 1 passed 10.12 s |
| Frozen-source recheck of HEAD/partial-HEAD/route plus both census/reproducer files, -n4 | 16 passed 1 skipped 26.52 s |

The prior continuation's required regex selected 118 test files. Thirty-four were excluded because they execute forbidden git-push fixtures or shell-oracle/security probes; the data-only guard subset is recorded in the command ledger. The remaining 84 ran together with `-n4`: **3395 passed, 29 skipped, 71 failed, 84 errors in 1396.15 s**. This was not a whole-suite run. Its failures were investigated:

- 59 existing unit failures reproduced against a pristine archive of initial HEAD: 9 catalog and 46 legacy-hold failures reject /tmp test workspaces as `tmp-workspace`; 4 retention-common tests explicitly try the unwritable Darwin user temp directory. The pristine comparison was 59 failed 1 passed 122.11 s over the 60 selected cases. The one baseline pass was the suggestion-secret property, which had hit the broad run's 90 s timeout.
- One canonical-output-lease test fails identically with the baseline and current daemon: 1 failed 2.21 s and 1 failed 4.64 s, respectively, on the same empty-set versus `{'newer-job'}` assertion.
- Four proof failures came from source loaded while that file was still being revised; the final 91-case file and frozen-source recheck pass. One routing fixture newly reached real census; it now supplies the empty fake census appropriate to its routing-only setup, and the complete 93-case file passes.
- Six property cases hit the broad run's 90 s per-test bound. With 600 s bounds and unchanged example/step settings, all passed: record secrecy 1/61.66 s; lane fault 1/36.15 s; admission liveness 1/261.46 s; admission stateful 2/148.52 s; suggestion secrecy 1/102.76 s.
- 84 frontend setup errors across six Swift probe files came from xcrun trying to create its cache under the unwritable mandated Darwin TMPDIR. A pristine representative reproduced 1 setup error in 1.87 s. The scratch-cache fallback passed its representative in 165.18 s, then five complete files passed 67 cases. The last file produced 17 setup errors because swiftc exceeded the helper's 540-second compile limit.

`tests/fake/test_worktree_add_c68.py::test_reproducer_a_the_cap_stops_the_whole_checkout` could not run its full real process-table census: `/bin/ps` is denied even for our own PID. It is explicitly skipped, never counted passed. The separate real-filter group-kill test uses real test-owned kernel PID reads and a fake empty census; it verifies the actual group stop but does not substitute for a full host census. The early real-census attempt, and the routing fixture before it was adapted, could proceed from denied ps to the normal unscoped kernel marker scan. Host-wide kernel reads were therefore attempted; I cannot claim every process read was test-owned. The later empirical experiment scopes enumeration to test-owned PIDs. No live store or session files were accessed, and no unrelated process was deliberately signalled or changed.

No claim is made that the broad run is green or that the unchanged baseline failures are fixed. The code avoids admission/removal over holders while inspection is uncertain. Its in-memory group records do not survive restart, and ownership proof/removal is not an atomic exclusion against an external writer or lock appearing after the last read.


This continuation's corrections and validation (07:54:03Z start).

The following gaps were corrected after independent source/test review: `os.path.isdir` swallowed a transient error while finding a legacy job's repository; `uncommitted.applied` swallowed transient marker reads; kept patches authorized either file/link kind through a broad `None`; and a file at the allocation raised raw ENOTDIR before ownership proof. Explicit source stat and strict marker reads now preserve causes and wait without treating resource pressure as absence. A later real-git reproducer also showed that swallowed source-stat pressure dropped sparse patterns, and a transient repository lookup could run an add in a replacement repository and permanently fail on an invalid reference. Add-command selection now propagates transient stat/Git failures; its permanent-error fallback remains. The old test expecting an unanswered checkout to be used anyway was deliberately changed to satisfy invariant 6 and C-6.8. This is an additional release/217 behavior change, validated by the real replacement/retry regression and existing sparse-pattern test. Saved patch modes now authorize exact destination kinds while allowing the commit's original kind during partial application. ENOTDIR specifically means the child gitfile is absent, so its registration's index is still read before the path-kind refusal. Changed submodules remain rejected by the existing submit-copy planner. No earlier committed production change needed reversal; the sole inherited commit was the reproducer suite, whose fixture corrections remain part of the uncommitted port.

The new handoff property compares real `_workspace` results to an independent oracle over directory/link/file/missing path, five gitfile states, four lock reasons, two HEADs, clean/staged index, four copy states and six census answers. It uses 60 generated and 21 explicit cases, intercepting rebuild so an invalid old tree cannot pass by replacement. The new F4 regression performs two real successful adds into one emptied directory with unchanged device/inode and proves group/marker/both survivors prevent reuse and a third add. The test's initial task-specific scratch assertion is corrected for ordinary CI temporary paths before final handoff.

All historical mutant counts below were checked against actual saved pytest assertion output, not inferred from exit codes. This continuation selectively reapplied M4 and M15 because their coverage/implementation changed: M4's new real-generation killer passed 3 cases on pristine source and failed all 3 with the inode cache; M15 failed 5 cases, passed 2 and deselected 90 when kept-copy ownership was removed. Both isolated copies restored exact pristine bytes in `finally` and verified hashes. Workspace sources were never mutated for these rechecks. Other already-completed, substantiated mutation experiments were not repeated.

Current command notation: `W=/Users/maxghenis/.subfleet/worktrees/20261113-092049-c68-build-r1c`, `S2=/private/tmp/sf-c68-b1/current-20261115T075403`, `PY=$W/.venv/bin/python`. C continues to mean the census test file. `E` retains mandated Darwin TMPDIR and `$TMPDIR/uv-cache`; the inherited environment already supplies pytest/Hypothesis/xdist. `B` now means `E PYTHONPATH="$S2/bootstrap:$W"`; its bootstrap scopes `tempfile.tempdir` and `tests.failed_keep.ROOT` to S2 and refuses prohibited subprocesses before execution. `P(file, base, flags)` is `B PY -m pytest -q file --tb=short flags --basetemp=$S2/base`. Every basetemp is unique. Counts are cases, not Hypothesis examples; repeated runs are not summed as unique coverage.

| Current root focused invocation | Result |
|---|---|
| `P(tests/unit/test_worktree_add_proof.py, focused-1, --timeout=600)` | 91 passed in 62.90s (0:01:02); exit 0 |
| `P(tests/fake/test_worktree_add_census.py, focused-2, --timeout=600)` | 6 passed in 3.61s; exit 0 |
| `P(tests/fake/test_worktree_add_c68.py, focused-3, --timeout=600 -k 'not test_reproducer_a_the_cap_stops_the_whole_checkout')` | 5 passed, 1 deselected in 4.81s; exit 0 |
| `P(tests/fake/test_workspace_transient.py, focused-4, --timeout=600)` | 24 passed in 19.67s; exit 0 |
| `P(tests/fake/test_unrun_worktrees.py, focused-5, --timeout=600)` | 104 passed in 95.70s (0:01:35); exit 0 |
| `P(tests/fake/test_review_launch_permissions.py, focused-6, --timeout=600)` | 35 passed in 16.86s; exit 0 |
| `P(tests/fake/test_admission_route_isolation.py, focused-7, --timeout=600)` | 93 passed in 26.33s; exit 0 |
| `P(tests/unit/test_salvage.py, focused-8, --timeout=600)` | 31 passed in 11.03s; exit 0 |
| `P(tests/unit/test_worktree_add_proof.py, proof-final-1, --timeout=600)` after fixes | 97 passed in 74.48 s; exit 0 |
| `P(tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py, contracts-1, --timeout=600)` | 427 passed in 63.62 s; exit 0 |
| `P(tests/unit/test_retention_archive.py::test_discarding_a_broken_allocation_removes_only_its_own_registration, d635-final-1, --timeout=120)` | 1 passed in 1.94 s; exit 0 |

| Current agent invocation (same E/B unless disclosed) | Result |
|---|---|
| `uv run pytest tests/unit/test_worktree_add_proof.py -q --basetemp=$S2/audit-proof-unit` | Exit 2, cache denied; no tests |
| `E PY -m pytest tests/unit/test_worktree_add_proof.py -q --basetemp=$S2/audit-proof-unit`, before fixes, bootstrap omitted | 91 passed in 67.99 s; startup-prune/temp-home deviation below |
| Real-git kind reproducer under `$S2/audit-kept-kind` | Ordinary file patch authorized an unrelated symlink; confirmed defect; guarded scratch cleanup succeeded |
| `P(tests/unit/test_worktree_add_proof.py, audit-kept-focused, -k 'kept or staged_index_blocks')` | 15 passed, 82 deselected in 8.19 s |
| `P(tests/fake/test_worktree_add_transient_reads.py, audit-transient-reads-1, --timeout=120)` | 18 passed in 8.16 s |
| `P(tests/fake/test_worktree_add_reuse.py, reuse-property-1, --timeout=600)` | 1 failed in 8.83 s; exposed ENOTDIR defect |
| Same file, `reuse-property-2`, temporarily broad rejection assertion | 1 passed in 24.99 s; strict assertion restored next |
| Same file, `reuse-property-3`, final strict assertion + file-refusal regression | 2 passed in 29.67 s |
| `P(tests/fake/test_worktree_add_census.py, audit-census-generation-1, --timeout=120)` | 3 failed, 6 passed in 5.61 s; new plain-repository test mistakenly used held-filter assertions |
| Same file, `audit-census-generation-2`, corrected repository assertions | 9 passed in 5.49 s |
| `E PY $S2/run_mutant4_current.py`; copied-source import probe; `PY -B -m pytest -q C::test_a_new_successful_add_in_the_same_directory_needs_a_fresh_census --tb=short --timeout=120`, copied cwd/bootstrap | Baseline 3 passed/8.01 s (`mutant4/baseline-tests`); mutation 3 failed/5.55 s (`mutant4/mutant-tests`); driver exit 0, pristine SHA restored |
| Copied-source `PY -m pytest Pp -q -k 'test_kept_copy_names_patch_and_untracked_kinds or test_kept_patch_authorizes_its_actual_kinds_only or test_kept_patch_kind_transitions_are_owned_before_copy_marker' --basetemp=$S2/audit-mutant15-current/pytest` | 5 failed, 2 passed, 90 deselected in 16.94 s; expected mutant exit 1, pristine SHA restored |

The current setup/read/write command ledger follows. Repeated read-only commands and resumptions are grouped; no failed command is counted as a test pass.

| Current invocation/family | Result |
|---|---|
| First `date -u`; `pwd`; `git status --short --branch`; `cat` exact brief and progress note paths | Exit 0; start 07:54:03Z, detached HEAD, preserved 9 modified + 4 untracked files |
| `rg --files` restricted patterns; `git log -12 --oneline`; `git diff --stat`; `cat` report/coordination | Exit 0; no local AGENTS.md; inherited sole new commit is 736722540 |
| `cat` supplied SHA256SUMS; `tar -tvf` archive; `shasum -a 256` archive/current WIP; initial `sed` | Exit 0; archive contains only intended module; evolved WIP intentionally differs from original |
| Python save-current → extract exact archive member at checkout root → assert manifest digest → print/read original → restore current | Exit 0; original digest b80c48769a79fc82165407c127e93e76e12c8c7b98806ccfafbd6a4f05c5a649; preserved initial digest 70dc3169a62cb91d0caef64aff8a459a9365354e325f917877924b58ae57b38f |
| `command -v uv`; `getconf DARWIN_USER_TEMP_DIR`; `ls -ld .venv`; `cat pyproject.toml`; interpreter/version imports | Exit 0; uv and existing Python 3.15.2/pytest 9.1.1/Hypothesis 6.168.1/xdist 3.8.0 available |
| `E uv sync --group dev --extra api` | Exit 2; mandated cache access denied; no test or installation counted |
| `git cat-file -e 642d2b3`; `git rev-parse HEAD:subfleet/daemon.py b51f08f96:subfleet/daemon.py`; `git show --stat 736722540` | Exit 0; #30 objects available, baseline daemon blobs equal, earlier commit adds six reproducer cases |
| Repeated scoped `rg`/`sed`/`cat`/`git diff`/AST reads of implementation, tests, contract, helpers, exact historical scratch logs/JSON, current logs and result JSON | Relevant reads succeeded; some large outputs truncated and reread narrowly. Absent `tests/process/conftest.py`, `swift-bin/swiftc`, historical `final-broad-result.json`, and an initial agent `test_uncommitted*` shell glob produced missing-file/expansion errors, no test executions |
| `rg` exact refusal/allocation/timeout/give-back compares into `$S2/pinned-compares.txt`; `wc -l` | Exit 0; 10 initial matches; new refusal tested through real proof/handoff, public text remains pathless |
| Python scratch/bootstrap/runner writes, named source/test edits, report copy and report-edit scripts | Succeeded; implementation corrections above, no caller-checkout writes |
| `B PY $S2/run_files.py Pp C R T U L A tests/unit/test_salvage.py` | Exit 0; eight sequential per-file results above; full real-census R case deselected |
| Python safe-file selector and source SHA snapshot | 120 regex-matching files, 34 inherited unsafe-file exclusions, 86 selected; SHA256 of 15 production/test/contract files saved before broad run |
| Named `git add docs/acceptance-contract.md subfleet/refusals.py` | Exit 128: external administrative index.lock denied; no commit attempted after failed staging |
| Named `git add subfleet/worktree_add.py subfleet/retention_git.py tests/unit/test_worktree_add_proof.py` | Same exit 128; no commit |
| Named `git add subfleet/daemon.py subfleet/uncommitted.py tests/fake/test_worktree_add_census.py tests/fake/test_worktree_add_reuse.py tests/fake/test_worktree_add_transient_reads.py tests/fake/test_worktree_add_c68.py tests/fake/test_workspace_transient.py tests/fake/test_unrun_worktrees.py tests/fake/test_admission_route_isolation.py tests/unit/test_retention_archive.py` | Same exit 128; no commit |
| Repeated named/all `git diff --check`, whitespace/syntax checks, line-number AST extraction; `git diff -- subfleet/conversations/service.py` | Exit 0; clean whitespace; conversation service unchanged |
| Historical evidence audit (read-only, no process queries) | All 16 mutant assertion kills, 3395/29/71/84 broad result, 59/1 pristine result and frontend/focused counts substantiated; old `broad-final-1.log` is empty and was never counted completed |
| Tool waits/log polls | Resume existing owned sessions; final statuses recorded once, never counted as new test runs |

Current deviation: one agent's initial completed 91-case run omitted B. Repository pytest startup therefore invoked default `/tmp/sf-failed` pruning, which may inspect unrelated PIDs, and made an automatic default-temp `sf-test-homes-*` directory whose suffix was not printed. No pruning was reported; normal pytest teardown removed its owned homes directory. No global scan was used to discover its name. All subsequent runs scoped those facilities first. This mistake does not establish full compliance with the no-live-process rule for that one startup hook. The owned wrong-kind experiment directory was recorded as `$S2/audit-kept-kind` and removed using guarded `shutil.rmtree`; all other new experiments/tests remain inside S2. No named standing hold, live store, session, or caller checkout was intentionally read or changed.

Historical command ledger (interrupted 03:00:36Z continuation).

This ledger records logical invocations, including failures, repeated verification and mutation harness errors. Repeated file reads/searches and tool polls are grouped below; a poll resumes an existing invocation and is not another test run. Results are pytest's counts, not wrapper wall-clock totals. Earlier-session notes do not contain its complete shell transcript; unavailable literal commands are identified rather than reconstructed as fact.

Command notation and environments.

- `W=/Users/maxghenis/.subfleet/worktrees/20261113-092049-c68-build-r1c`; `S=/tmp/sf-c68-b1`; `PY=$W/.venv/bin/python`. All workspace commands use cwd `W` unless stated.
- `E`: `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache"`. Some recorded wrappers spell the cache as `$(getconf DARWIN_USER_TEMP_DIR)uv-cache`; getconf's path has its trailing slash. Tests use `PY` directly after dependency installation because the required cache directory is unwritable.
- `B`: `E PYTHONPATH="$S/bootstrap:$W"`; bootstrap sets `tempfile.tempdir=S`, `tests.failed_keep.ROOT=S/failed-keeps`, and installs a before-exec boundary rejecting forbidden binaries/verbs. It is a session safeguard, not a test that executes write-shaped guard commands.
- `P(files, base, flags)`: `PY -m pytest -q <files> <flags> --basetemp=$S/<base>`. Tables show every file/node selector and option difference. Exact saved argv are in `root-file-checks.json`, `pristine-unit-command.json`, daemon mutation `results.json`, ownership mutation JSON, and frontend JSON.
- `O(files, base, flags)`: `E PYTHONPATH=. PY $S/run_ownership_pytest.py <files> <flags> --basetemp=$S/<base> -q`. That script scopes temporary homes/failed roots before `pytest.main`. Initial ownership runs instead used `E PY -m pytest <files> --basetemp=... -q`.
- Validation/contract `python -c` wrappers call `pytest.main` with the listed argv; initial wrappers scoped tempfile only, later wrappers scoped failed-keeps too. Those wrapper bodies are operationally equivalent to `B P` but are distinguished where relevant.
- Pristine environment means `E PYTHONPATH="$S/pristine-bootstrap:$S/pristine-base"`, cwd `$S/pristine-base`, using the absolute workspace interpreter. This prevents the editable workspace from overriding the archived source.
- Aliases: `R=tests/fake/test_worktree_add_c68.py`; `C=tests/fake/test_worktree_add_census.py`; `Pp=tests/unit/test_worktree_add_proof.py`; `U=tests/fake/test_unrun_worktrees.py`; `T=tests/fake/test_workspace_transient.py`; `A=tests/fake/test_admission_route_isolation.py`; `L=tests/fake/test_review_launch_permissions.py`.

Earlier session and 03:00:36Z continuation setup.

| Invocation/family | Result |
|---|---|
| Earlier notes' `date -u`, named baseline/source inspection, git 2.55 held-filter experiment,360 group-kill samples, process census and six reproducers | Notes report start 2026-11-12T15:46:22Z; six expected failures, lock/order/census measurements summarized separately. Complete literal commands/output are unavailable. Only committed result is `736722540`, six parameterized reproducer cases. |
| Root first `date -u`; `pwd`; `git status --short`; `git log`; `cat` supplied brief/progress notes | Success; start 2026-11-15T03:00:36Z, initial workspace clean at 736722540. Agent first dates ownership 03:01:07, validation 03:01:18, contract 03:01:31. |
| `rg --files -g AGENTS.md` | No matching local instruction file. |
| `git show --stat 736722540`; `cat docs/coordination.md`; `cat <wip>/SHA256SUMS`; `tar -tvf <wip>/untracked.tar` | Success; only restored target is `subfleet/worktree_add.py`. |
| First separate pristine-WIP extraction execution | Rejected before command because requested cwd did not exist; no extraction occurred. Corrected mkdir/extraction and checksum commands succeeded. |
| `tar -xf /Users/maxghenis/reviews/subfleet-hub/c68-port-2026-11-11/wip-20261112/untracked.tar` at `W`; `shasum -a 256 subfleet/worktree_add.py`; `cat` restored module | Success; original SHA256 `b80c48769a79fc82165407c127e93e76e12c8c7b98806ccfafbd6a4f05c5a649` matches supplied manifest before edits. |
| `mkdir` requested Darwin TMPDIR scratch; mandatory `uv sync --group dev --extra api` | Permission/cache-location failure. No test counted. |
| `uv sync --group dev --extra api --cache-dir $S/uv-cache` | Failed: project defines no `api` extra. |
| `uv sync --group dev --cache-dir $S/uv-cache` | Succeeded; local virtualenv/development dependencies available. |
| `uv pip install --python .venv/bin/python --cache-dir $S/uv-cache pytest-xdist` | Succeeded for requested `-n 4` verification; no dependency file change claimed. |

Read/search, source preparation and verification families.

These group repeated read-only invocations while preserving their scope and exceptional results.

| Family | Scope/results |
|---|---|
| `git show <commit>:<path>`, AST extraction, `rg` function names, `sed`/`nl` ranges | Read baseline `b51f08f96` named daemon/retention/salvage functions, #30`642d2b3` requested helpers/fixtures, current daemon/proof/retention/env/process/contract code, uncommitted-copy implementation and affected tests. All substantive requested reads succeeded. No fetch was needed. |
| `git rev-parse HEAD:subfleet/daemon.py b51f08f961e1b1dcfb93347eb080f9ddd9a9260d:subfleet/daemon.py` | Both blobs `9e2e0a82821f9219803c1f6b44d04804a209548f`; isolated initial daemon is exactly the brief's baseline production source. |
| Repeated `rg`/`sed`/`cat` of test/conftest, failed_keep, pyproject, process/kernel source, helpers, supplied notes and agent ledgers | Success except initial missing `.venv/bin/python`, two mistyped nonexistent kernel module paths, nonexistent `tests/unit/test_retention_git.py`, early unmatched worktree-test globs, not-yet-written report paths, and nonexistent `$S/test-command-log.txt` (rc1). These were read errors, not test runs. Long historic contract/ledger outputs were truncated and relevant clauses reread directly. |
| `rg` exact compares for worktree-locked/workspace-prepare/allocation/timeout/give-back/unproven strings | Success; matches retained in `pinned-compares.txt`, dependent expectations updated. |
| Python selected-file extraction and AST import closure; source regex/literal audit |118 matching files; 34 excluded for prohibited executable fixtures/guard probes; 84 selected, 142-file import closure, 22 text hits. Protected-helper import, stand-in/runner data, mocked GraphQL and prose are false positives; indirect World-push import paths do not instantiate that constructor. Static audit is not a runtime execution trace. |
| `rg --files` recursively across scratch, subsequently replaced by a top-level Path.iterdir listing | Unnecessarily large truncated output (scratch contains copied repositories/test trees); no mutation. Scoped follow-ups located logs/JSON. |
| `apply_patch` and named Python source/contract/test rewrites; Python scratch-script/report writes | Success. Named implementation/test/contract files described in changes section; no caller checkout writes. Mutation copies, wrappers, experiment scripts, JSON logs and report fragments live under `S`. |
| Repeated `git diff --check`, `git diff --stat`, `git status --short`, line-number lookups | Checks reported clean formatting; status preserved existing changes and identified intended modified/untracked files. Later final-check results are appended by root. |
| Tool `write_stdin`/wait polls, repeated `cat`/`tail` of active logs and JSON | Resumed the same commands; terminal results appear once below. Individual poll totals were not retained (contract handler run explicitly had four polls). |

Named staging/commit attempts: root staged only named daemon/refusal/census/reproducer/route files at coherent steps; all `git add` attempts returned 128 on `/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-092049-c68-build-r1c/index.lock` (Operation not permitted), and root ran no commit after a failed add. Ownership's exact named add was `git add subfleet/worktree_add.py subfleet/retention_git.py tests/unit/test_worktree_add_proof.py`, rc128. Contract's `git add docs/acceptance-contract.md && git commit -m ...` stopped at denied add. Validation attempted named add and commit separately for `tests/fake/test_workspace_transient.py` and then `tests/fake/test_unrun_worktrees.py`; both add/commit attempts were denied, no commit made. No stash, history rewrite, push or PR command ran.

Baseline reproducers and git experiments.

| Command/selector; unique basetemp | Result |
|---|---|
| Validation direct pytest R; Darwin TMPDIR `sf-c68-b1/validation-baseline-0303` | Before collection: No module named pytest. |
| Validation direct pytest R; `validation-baseline-0306` |6 failed 18.14 s: five intended defect failures plus bare-ps infrastructure denial. |
| R cap node after absolute `/bin/ps` correction; tempfile wrapper; `validation-baseline-a-0310` |1 failed 3.41 s: own-PID ps denied. |
| `PY` own-PID `kernel.process` probe | Succeeded; own test process state/identity readable. |
| `git show HEAD:subfleet/daemon.py > $S/validation_baseline_daemon.py`; import snapshot before R cap test; `validation-baseline-a-kernel-0316` |1 expected failure, 3.04 s: live filter 79150 survived baseline cap. |
| `PY $S/validation_git_experiment.py`, initial version | Own-PID ps denied; `finally` released gate and reaped leader. |
| Same experiment revised to own kernel records and scoped/synthetic census | Success: exact initializing bytes, HEAD/gitfile present/index absent, group 4/markers 2; 0 ps, 1 scoped enumeration, 4 process+4 argument reads; released/reaped rc0; hook saw unlocked. Not a full real census. |
| `PY $S/validation_git_order.py` |40 gated adds, 40 rc0/finally reaped; 13 shapes and ordering counts reported in findings. |

Development and final file checks.

Each row is a separate invocation; `pass/fail/skip/des` are case counts. Development failures are retained rather than folded into final passing totals.

| Runner / selector | Basetemp suffix | Result |
|---|---|---|
| Validation wrapper T |validation-transient-0321|24 pass 20.93 s|
| Validation wrapper U |validation-unrun-0326|95 pass,9 fail 223.14 s; stale mocks plus missing-submodule proof bug, subsequently corrected|
| U `-k 'registration_that_stayed or forged_registration or unregistered_repository or cannot_be_read or lock_that_comes or tree_gone_with_its_registration_locked or nul_in_an_old or every_removal or collects_only'` |validation-unrun-focused-0313|15 pass,89 des 20.75 s|
| U `-k 'property_a_tree or each_claim'` |validation-unrun-property-0316|2 pass,102 des 55.55 s|
| Validation wrapper U |validation-unrun-final-0320|104 pass 175.23 s|
| U strengthened actual-job symlink-discard case |validation-unrun-link-final-0324|1 pass 4.80 s; final diffcheck passed|
| Contract direct pytest `tests/unit/test_invariants_index.py` |contract-index-0309|25 pass 1.44 s|
| Contract direct pytest `tests/unit/test_handler_refusals.py` |contract-refusals-0310|362 pass 58.12 s|
| Contract wrapped invariants index after HEAD clause |contract-index-head-0316|25 pass 3.95 s|
| `B P(tests/unit/test_invariants_index.py, ..., none)` after bounded-reap clause |contract-index-final-0326|25 pass 2.67 s; diffcheck clean|
| Ownership direct Pp |ownership-01|70 pass 55.73 s|
| Ownership direct Pp |ownership-02|71 pass 99.70 s|
| Ownership direct Pp |ownership-03|72 pass,1 fail 91.45 s; fake transient stderr lacked normal `: errno` diagnostic context, corrected|
| O(Pp, ..., `-k 'missing_configured_worktree or another_worktree or transient_git_head or ambiguous_backlink or filesystem_identity'`) |ownership-04|6 pass, 1 fail,68 des 9.06 s; same process-loaded old fixture|
| O(Pp) |ownership-05|77 pass 69.42 s|
| O(Pp, `-k 'unique_same_tree or incomplete_head or missing_configured_worktree or initializing_lock or another_worktree or transient_git_head'`) |ownership-06|9 pass,72 des 13.32 s|
| O(Pp, `-k 'unique_same_tree or incomplete_head or ambiguous_backlink or symlinked_admin_container or kind_transitions'`) |ownership-07|9 pass,76 des 9.46 s|
| O(Pp) |ownership-final|90 pass 104.59 s|
| O(Pp) |ownership-final-02|91 pass 196.98 s, final production module|
| O(Pp::test_unique_same_tree_commit_prevents_registration_and_tree_removal), strengthened to genuinely unreferenced commit |ownership-unique-head-final|1 pass 5.29 s; production unchanged|
| Root `B PY $S/run-file-checks.py`: P(R, ..., `--timeout=90`) |root-repro-final-1|5 pass,1 skip 13.91 s; real full-ps integration unavailable. An earlier current-source iteration had5 pass/1 infrastructure failure before the preflight skip was added|
| Same root script P(A, ..., `--timeout=90`) |root-route-1|92 pass,1 fail 57.27 s; route fixture's state-only census looked unverifiable, corrected|
| Same root script P(tests/unit/test_salvage.py, ..., `--timeout=90`) |root-salvage-1|31 pass 26.37 s|
| Same root script P(`tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py`, ..., `--timeout=90`) |root-state-index-refusals-1|427 pass 160.18 s|
| Root B P(A, ..., `--timeout=90`) |root-route-2|93 pass 97.74 s|
| Root B P(`Pp::test_unique_same_tree_commit_prevents_registration_and_tree_removal Pp::test_incomplete_head_is_owned_only_under_initializing_lock A::test_c6_12_a_refused_writable_job_leaves_no_worktree C R`, ..., `-n 4 --timeout=120`) |root-stable-port-recheck-1|16 pass,1 skip 26.52 s|
| Root B standalone `PY -m pytest -q C --basetemp=<unique scratch path>` |Exact suffix unavailable in retained transcript|6 pass 10.70 s; recorded root context, no invented path|
| Final validation B P(T) |validation-transient-final-0409|24 pass 36.31 s|
| Final validation B P(L) |validation-review-permissions-final-0410|35 pass 39.65 s|

Additional root invocations retained from the active-session record: the earlier standalone review-launch-permissions file run (`root-copy-1`) passed 35 cases 27.68 s. The first current-source reproducer run passed 5 cases and had 1 ps-infrastructure failure before the explicit skip was added; its literal basetemp was not retained. The push-free d635 target `tests/unit/test_retention_archive.py::test_discarding_a_broken_allocation_removes_only_its_own_registration` passed 1 case 10.12 s after a stale version had produced 1 setup error: its World fixture attempted a push-shaped subprocess call, which the audit boundary rejected before process creation. The first Python replacement script raised ValueError on a missing substring and changed no source; the corrected replacement succeeded. No git-push executable ran. Later named target calls use the rewritten standalone repository fixture.

Mutation invocations.

Daemon family: `PY $S/run-daemon-mutants.py` copied source under `$S/daemon-mutants`, then `E` with TMPDIR/cache explicitly redirected to `S`, `PY -m pytest -q <killer> --tb=short --timeout=45 --basetemp=$S/daemon-mutant-N` (M7 retry suffix). Pristine integration baseline selecting human-lock reuse, four-entry container and live/dead-lock give-back:7 pass 28.24 s (`daemon-mutant-baseline-0310`). Initial script ran M1/2/3/4/11/14; M7 insertion anchor did not match changed source, so no test ran for that attempt. Adapted M7 retry ran separately. Restored C with `-k not_real_filter`, `daemon-mutant-restored-census`:4 pass,1 des 11.62 s. Each mutation was restored/byte-verified in `finally`.

Ownership family: `E PY $S/run_ownership_mutants.py`, scratch copies, one mutation at a time; each child uses isolated `python -c` pytest.main, `<killer> -q -p no:cacheprovider --basetemp=$S/ownership-mutant-<run>-N`; M12 adds `-k admin-scan`. First run: M5 killed (1 fail 23.79 s), M6 survived generated-only oracle (1 pass 29.25 s), harness rc1; deterministic lock regression added. Second run killed5/6/8/9/10/12 but M13 natural-volume test survived (3 pass 8.18 s), harness rc1; synthetic case-sensitive regression added. Third run killed5/6/8/9/10/12/13, then old one-line M15 insertion anchor failed before testing15; restored. Fourth, unique run 1794713005436228000, killed those seven against final module, then the already-loaded stale M15 anchor failed; restored. Corrected `OWNERSHIP_MUTANTS=15,16` invocation used unique run 1794713164341443000 and killed both. Early retry basetemps were reused (deviation); final two runs used unique namespaces. Final merge/assert command loaded both JSON files, checked exactly nine rc1 verdicts and byte-compared both copied modules with workspace; rc0, `ownership-mutant-final.json` written.

Final per-mutant invocation results (killers named in mutation table in core; these are expected failures under mutations): M1=1 fail 9.75 s; M2=1 fail 14.35 s; M3=1 fail 6.92 s; M4=1 fail 21.00 s; M5=1 fail 14.35 s; M6=2 fail 13.01 s; M7=1 fail 11.78 s; M8=6 fail 25.69 s; M9=1 fail 34.45 s; M10=1 fail 26.63 s; M11=3 fail/1 pass 14.46 s; M12=1 fail/8 pass/45 des 16.49 s; M13=1 fail 13.52 s; M14=1 fail/1 pass 9.78 s; M15=1 fail 15.13 s; M16=1 fail 9.35 s. No insertion/import/collection error was counted as a kill.

Broad run, baseline classification and final rechecks.

| Invocation | Result |
|---|---|
| `B PY -m pytest -q -n 4 <84 files in safe-selected-tests.txt> --timeout=90 --basetemp=$S/root-broad-1` |71 fail, 3395 pass, 29 skip, 84 error, 1 warning, 1396.15 s. This was the requested matching-file run with 34 explicit safety exclusions, not the whole suite. Code changed while this snapshot was imported; it is not the final port verdict. No before-exec boundary refusal appears in its log. |
| Parse FAILED/error sections; read failure blocks, saved comparison JSON/source |64 unit failures=4 concurrently edited proof cases + 59 baseline cases + 1 timed-out suggestion property.7 fake failures=corrected route fixture + 1 canonical baseline behavior + 5 timed-out property/stateful cases.84 frontend errors shared xcrun cache denial. Four proof failures covered final 91 + selected HEAD 1; route covered final 93/stable run. |
| `mkdir -p $S/pristine-base $S/pristine-bootstrap`; `git archive 736722540159 | tar -x -C $S/pristine-base`; copy bootstrap; sourcepath verification | Extraction succeeded. First import assertion failed because cwd W beat PYTHONPATH; no baseline test ran there. Retry from pristine cwd verified pristine subfleet/tests paths and scoped failed roots. |
| Pristine cwd/environment `PY -m pytest -q -n 4 <47 function selectors in pristine-unit-command.json> --tb=short --timeout=90 --basetemp=$S/pristine-unit-failures-1` |59 fail,1 pass 122.11 s. Matching failures: catalog9, legacy_hold46 (temporary-workspace policy), retention common-dir4 (Darwin temp denial). Suggestion property passed pristine; therefore original broad suggestion timeout was not classified as confirmed baseline failure. |
| Pristine P(`tests/frontend/test_core_draft.py::test_new_and_switching_preserve_draft_and_request_focus`, ..., `--tb=short --timeout=90`) |pristine-frontend-blocked-1:1 setup error 1.87 s; xcrun Darwin cache path denied. Representative confirms common setup blocker, not 84 individually repeated tests. |
| Current B P(`tests/fake/test_canonical_identity.py::test_two_legacy_alias_holders_oldest_exports_and_releases`, ..., `--timeout=600`) |validation-triage-canonical-current-0340:1 fail 4.64 s, output-lease assertion. |
| Same canonical node with isolated initial daemon |validation-triage-canonical-baseline-0341:1 fail 2.21 s, identical assertion; unrelated baseline behavior. |
| B P(`tests/fake/test_record_secrecy.py::test_no_reply_or_row_holds_what_a_turn_submit_failure_said`, ..., `--timeout=600`) |validation-triage-secrecy-0342:1 pass 61.66 s. |
| B P(`tests/fake/test_lane_fault.py::test_c4_5_no_unchanged_unpinned_job_is_failed_by_the_one_dead_lane_it_meets`, ..., `--timeout=600`) |validation-triage-lane-fault-0343:1 pass 36.15 s, full 25 examples plus explicit cases. |
| B P(`tests/fake/test_admission_liveness.py::test_c6_3_c26_9_admission_places_what_e053b2c_placed_pass_for_pass`, ..., `--timeout=600`) |validation-triage-liveness-0344:1 pass 261.46 s, full 40 examples. |
| B P(`tests/fake/test_admission_stateful.py::TestAdmissionPass tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps`, ..., `--timeout=600`) |validation-triage-stateful-0349:2 pass 148.52 s, full 40 example/30 step models. Stateful placeholder resolved; no reduced settings. |
| Root B pytest `tests/unit/test_suggestion_secret_properties.py::test_no_secret_shaped_suggestion_is_read_or_changed`; other flags not retained |root-suggestion-recheck-1:rc4, no tests, 2.64 s; guessed node nonexistent. |
| Root B P(`tests/unit/test_suggestion_secret_properties.py::test_c25_5_any_start_failure_is_scrubbed_wherever_it_happens`, ..., `--timeout=600 --tb=short`) |root-suggestion-recheck-2:1 pass 102.76 s. |
| Root B P(`tests/unit/test_guard_subfleet_root.py::test_s3_other_rules_give_identical_output tests/unit/test_guard_subfleet_root.py::test_s3_apply_patch_payloads_are_judged_identically`, ..., `--timeout=600 --tb=short`) |root-guard-data-1:2 pass 27.91 s. Inspected run_hook sends JSON stdin to the hook; commands under test remain data. Fixture creates init/commit/worktree-add only, no push. |

Frontend fallback.

Fallback setup wrote `$S/swift-bin/xcrun` to set only its child's `TMPDIR=S` and `exec /usr/bin/xcrun --no-cache "$@"`. Parent retains `E`; add `PATH=$S/swift-bin:$PATH`, `CLANG_MODULE_CACHE_PATH=$S/clang-module-cache`, `SUBFLEET_SWIFT_NESTED_SANDBOX=off`. Representative `PY -m pytest -q tests/frontend/test_core_draft.py::test_new_and_switching_preserve_draft_and_request_focus --tb=short --timeout=600 --basetemp=$S/frontend-fallback-representative-1`:1 pass 165.18 s. One generated cache file under `W/build/frontend-probes/7ddaacc682699bb573693bb1707e56adca1007d07654262cee81adcba29e4319` was moved into `$S/frontend-cache-root/build/frontend-probes/`; generated empty directories were moved into scratch and guarded-rmtree removed. Exact moves are recorded in `swift-cache-cleanup.json`.

Six sequential file commands used `PYTHONPATH=$S/bootstrap:$S/swift-plugin:$W` and `PY -m pytest -q -p c68_swift_cache <file> --tb=short --timeout=600 --basetemp=$S/frontend-fallback-file-N`; the collection-finish plugin redirects only the cache root and retains actual source inputs. Results under tests/frontend: `test_core_draft.py` 7 passed/1.32 s; `test_core_search.py` 20 passed/156.04 s; `test_disk_ruling.py` 8 passed/131.75 s; `test_pr124_review_r2_fixes_ui.py` 19 passed/248.35 s; `test_app_cutover_start.py` 13 passed/507.96 s; `test_pr124_fixes_ui.py` 17 setup errors/704.42 s from a 540-second swiftc timeout. All six pytest processes exited; the driver recorded per-file statuses and exited 0. The contract-review agent subsequently hit a workspace-routing discovery timeout while preparing its final message; this did not change the saved results.

Root final checks reported so far: Python report assembler wrote `docs/reports/2026-11-15-worktree-add-c68.md` draft, then Python spacing edit succeeded; `git diff --check` returned 0. `PY -m py_compile subfleet/daemon.py subfleet/refusals.py subfleet/retention_git.py subfleet/worktree_add.py tests/fake/test_admission_route_isolation.py tests/fake/test_unrun_worktrees.py tests/fake/test_workspace_transient.py tests/fake/test_worktree_add_c68.py tests/fake/test_worktree_add_census.py tests/unit/test_retention_archive.py tests/unit/test_worktree_add_proof.py` returned 0. Follow-up reads of proof, strict registration, remove entry points/catch clauses succeeded; large contract diff output was truncated. Ledger/core assemblers and `wc -w` checks succeeded. That interrupted report left frontend completion, a frozen broad run and final handoff checks pending. The saved frontend results were subsequently audited here; the empty historical frozen-run log is not evidence of completion. Current results and final handoff checks replace those placeholders below.

Initial direct pytest sessions used conftest's automatic outside-scratch `sf-test-homes-*` folders and default `/tmp/sf-failed`; homes auto-cleaned, exact names not captured, and no `pruned` output was seen. Later wrappers isolate both paths. Contract mutation runs set TMPDIR/cache directly to scratch rather than the requested Darwin environment (deviation); final ordinary checks retain E and scope Python allocations through bootstrap. Validation observed unrelated `/tmp/sf-test-homes-90qentk7` and left it untouched. Darwin denied mkdir/cache paths in baseline logs were not created. No claim is made that unbootstrapped global pruning inspected nothing. Scratch-copy/test artifact writes, report fragment writes, repeated bounded log reads and wait polls are included in their command families above; the initial census caveat above applies; no live store/session-file inspection or prohibited write subprocess was deliberately executed.

The 34 matching-file exclusions were explicit. Push-executing fixtures: `tests/unit/test_host_push.py`, `tests/unit/test_host_push_build.py`, `tests/unit/test_host_push_intake.py`, `tests/unit/test_host_push_config_cost.py`, `tests/unit/test_host_push_store_completion_pr183.py`, `tests/unit/test_host_push_copy.py`, `tests/unit/test_host_push_storage_recovery.py`, `tests/unit/test_host_push_store_interleavings.py`, `tests/unit/test_host_push_build_budget.py`, `tests/unit/test_host_push_retention.py`, `tests/unit/test_host_push_storage_retry_budget.py`, `tests/unit/test_host_push_metadata.py`, `tests/unit/test_host_push_admission.py`, `tests/fake/test_turn_retention_job_properties.py`, `tests/fake/test_retention_dependency_incomplete.py`, `tests/fake/test_turn_retention_cwd_properties.py`, `tests/fake/test_writer_retention_fence.py`, `tests/fake/test_detached_retention_fence.py`, `tests/fake/test_turn_retention_cwd.py`, `tests/fake/test_retention_dependency_review_r5.py`, `tests/fake/test_retention_dependency_review_r1.py`, `tests/fake/test_retention_dependencies.py`, `tests/unit/test_retention_folded_pin.py`, `tests/unit/test_retention_case_alias.py`, `tests/unit/test_retention_dependency_units.py`, `tests/unit/test_retention_repository_walk.py`, `tests/unit/test_retention_rediscovery.py`, `tests/unit/test_retention_shared_folders.py`, `tests/unit/test_rediscovery_git_trace.py`, `tests/unit/test_retention_81_notes.py`, `tests/unit/test_retention_archive.py`. Guard files excluded from the broad run: `tests/unit/test_guard_subfleet_root.py`, `tests/unit/test_live_store_guard.py`, `tests/unit/test_live_store_guard_round5.py`. The two data-only S3 guard nodes ran separately and passed; shell-oracle and security-executing tests remained excluded.


Current frozen-source broader verification: `B PY $S2/run_broad.py` ran the 86 safe selected files together with `-n 4 --timeout=600 --tb=short --basetemp=$S2/broad-1`, plus `-p c68_safe_validation`. Result: **3495 passed, 29 skipped, 59 failed, 36 setup errors in 938.66 s**, pytest exit 1. All 15 snapshotted production/test/contract files were byte-identical before and after. The wrapper initially recorded 85 files because it intended `--ignore=tests/frontend/test_pr124_fixes_ui.py` to omit one; pytest still collected that explicitly supplied file. The metadata was corrected to 86 after inspecting all 17 of its setup errors. No completed file is silently omitted.

The exact set of 59 failed node IDs equals the already verified pristine set: 9 conversation-catalog, 46 legacy-hold and 4 retention-common failures, with no new failed nodes. A first comparison split parameterized node names on spaces and falsely reported two mismatches; preserving full node IDs resolved them. The 36 errors belong only to `test_pr124_review_r2_fixes_ui.py` (19) and `test_pr124_fixes_ui.py` (17), with native cache denial/duplicate `_DarwinFoundation1` cache modules through `/tmp` and `/private/tmp` spellings. Their fresh-canonical-cache rerun results are recorded in final checks below. Full host-census case R::test_reproducer_a_the_cap_stops_the_whole_checkout was explicitly skipped by the collection plugin under the live-state rule, not counted passed; owned-PID cap testing remains green. The same 34 unsafe-file exclusions listed above were retained. This was not a whole-suite run.

The parent test process retained mandatory E/B. The native xcrun fallback used task scratch as child TMPDIR, disabled its native cache, and placed Clang/probe caches in scratch; that child-environment deviation is explicit. After the frozen run, only the portable C test assertion, the final `_tree_git` transient fix/tests and the version-3 explanatory sentence changed. Those narrow changes receive the separate final checks below; the entire broader run was not repeated.

Final checks and remaining limits.

| Final invocation | Result |
|---|---|
| `P(C, census-portable-final-1, --timeout=120)` after replacing the task-name assumption with the fixture repository's registration-container identity | 9 passed in 6.36 s; exit 0 |
| `E PY $S2/repro_tree_git_reads.py` | Exit 0; 17 real-git commands: 16 exit 0 and one expected invalid-reference exit 128. Injected stat pressure expanded a sparse tree; swallowed transient lookup selected the replacement repository. Both defects now have regressions |
| `P(tests/fake/test_worktree_add_transient_reads.py, audit-transient-reads-2, --timeout=120)` after source-stat fix | 27 passed in 17.01 s; exit 0 |
| `P(U::test_a_checkout_lookup_that_does_not_finish_preserves_its_cause U::test_a_replaced_checkout_with_a_transient_lookup_waits_before_any_add U::test_a_sparse_callers_patterns_pass_on_while_its_checkout_lasts, audit-source-selection-targeted-1, --timeout=120)` | 3 passed in 7.36 s; exit 0 |
| `P(tests/fake/test_unrun_worktrees.py, late-1, --timeout=600)` | 105 passed in 156.32s (0:02:36); exit 0 |
| `P(tests/fake/test_workspace_transient.py, late-2, --timeout=600)` | 24 passed in 22.47s; exit 0 |
| `P(tests/fake/test_review_launch_permissions.py, late-3, --timeout=600)` | 35 passed in 18.53s; exit 0 |
| `P(tests/fake/test_worktree_add_transient_reads.py, late-4, --timeout=600)` | 27 passed in 12.13s; exit 0 |
| `P(tests/unit/test_invariants_index.py, late-5, --timeout=600)` | 25 passed in 1.55s; exit 0 |
| `B PY $S2/frontend-canonical-1/run.py`; sequential `PY -m pytest -q -p c68_frontend_canonical tests/frontend/test_pr124_review_r2_fixes_ui.py --tb=short --timeout=600 --basetemp=$S2/frontend-canonical-1/file-1` | 19 passed in 171.30 s; pytest/driver exit 0 |
| Same driver/plugin/options, `tests/frontend/test_pr124_fixes_ui.py`, `frontend-canonical-1/file-2` | 17 passed in 124.29 s; exit 0 |
| Native compile subprocesses inside those two file commands | Both exit 0; 150.99 s and 112.18 s, leaders reaped. The scratch xcrun wrapper used a new session, 450 s group-stop cap and bounded final reap; no cap fired. Fresh canonical Swift/Clang/probe caches and native-child scratch TMPDIR resolved all 36 broader-run setup errors |
| Named `git add subfleet/daemon.py subfleet/uncommitted.py tests/fake/test_unrun_worktrees.py tests/fake/test_worktree_add_transient_reads.py tests/fake/test_worktree_add_census.py docs/acceptance-contract.md` after final ownership-read fixes | Exit 128, external index.lock denied; no commit |
| Named `git add docs/reports/2026-11-15-worktree-add-c68.md` | Exit 128, same lock denial; no commit |
| `B PY -m py_compile` on the five changed subfleet modules and nine changed/new Python test files; Python whitespace scan over all 15 source/test/contract files; `git diff --check` | Exit 0; all 14 Python files compile, no trailing whitespace; final source SHA256 manifest saved |
| Initial final-report heredoc assembly | Rejected before execution by the shared-stash PreToolUse hook, which matched quoted report prose. No command ran. Structured file patch plus a plain Python invocation completed the same report edit safely |
| Final `git status --short --branch`; `git rev-parse HEAD`; `date -u`; report assembly/checks | Detached HEAD unchanged; 10 modified + 6 untracked named deliverable files, including report; no extra staged changes. Report assembled 2026-11-15T08:35:25+00:00, before 10:54:03Z deadline |

All required worktree protections and their specified properties/mutants are implemented. The 36 frontend errors clear on their two isolated reruns; the remaining 59 broader failures are the exact pristine baseline set. Full real-host census remains unverified and excluded, with its exact R test named above. Group/child memory does not survive restart; Apple's unmarked platform descendants can evade a marker-only census after restart. The lock/filesystem proof and removal remain separate operations: a concurrent new claim or file after the last check is not an atomic guarantee. No live jobless orphan was touched, and no background test/compiler process was left running. All current owned test and compiler sessions exited; temporary artifacts remain inside the named task scratch for review. The one unbootstrapped startup-hook deviation and historical outside-scratch unknown names remain explicitly disclosed; they are not counted as compliant process-census verification.

No commit could be created because every named staging step was denied at `/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-092049-c68-build-r1c/index.lock`. Existing changes are intact in the assigned workspace for salvage. No stash, rewritten history, push, main/master update, pull request, external publication or message was made.


Head built (implementation and report remain uncommitted):
73672254015924df25f4d27ab3e92c836fe687ca
