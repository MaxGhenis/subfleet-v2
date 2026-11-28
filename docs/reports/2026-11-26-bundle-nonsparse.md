# D-BUNDLE-SPARSE1 — retention bundle packing

Started at `2026-11-26T10:30:08Z` on detached `53a7bac93d01afd9165588bcaf789dae75540757` (release/217), with Git 2.55.0. The worktree's Git common directory was `/Users/maxghenis/subfleet-v2/.git`.

## Change and cause

Git 2.55 defaults to a sparse pack walk. It excludes prerequisite blobs by path, descending where an included tree shares a path with an excluded tree. The retention anchor names index blobs by object ID under `index/`, so even unchanged blobs under directories like `subfleet/` and `tests/` were packed again. The Disk manager's `verify-inflow.md` items 8–10 and 12 counted 9,075 index blobs on anchor `fe50fe37507ee60fc5cc4b5ecb55cba8419c991c`. The prior archive packed 9,073 objects (9,070 blobs, one commit and two trees). The five blobs at the prerequisite's root were omitted. The manager measured 87,362,681 bytes with the default and 442,983 bytes with a non-sparse walk. Among 277 new bundles, 57 full-tree packs held 2.505 GB for 0.045 GB of unique bundle data.

- `subfleet/retention_git.py` adds keyword-only `config` to `run`, with each `-c` pair after the standard settings and before `--git-dir`. Before `953c2819d`, cancellation reported `git --git-dir=… cancelled` for callers passing `git_dir` and `git -c cancelled` for callers starting `args` with `-c` without `git_dir`. At `953c2819d`, the message took its name from `args[0]`, fixing the `git_dir` case even with per-command settings. Round 1 also skips leading `-c` pairs in `args`, so the delete verifier's inline setting yields `git fetch cancelled`. `partial_clone` shares clone detection with `scratch_reason`. With held refs, `create_bundle` disables the sparse walk after a successful check for a complete clone. A partial clone or a probe `GitError`, such as a timeout, leaves the default walk in effect for the bundle attempt. An unparseable config can still make that attempt fail. Cancellation propagates. Empty held lists skip detection and use the original command.
- `tests/unit/test_retention_bundle_pack.py` covers pack membership, header bytes, restore, clone configuration and the guarded delete invocation.
- `docs/acceptance-contract.md` updates C-8.4 and the version 3 changelog. `docs/desktop/retention-archive.md` adds revision 9 and describes bundle packing and staged-blob availability. I3 already permits a blob to come from either the bundle or the remote.
- `docs/reports/2026-11-26-bundle-nonsparse.md` records the validation and measurement.

## Invariants and tests

1. **Same header.** `test_bundle_has_same_header_as_git_default` compares version, prerequisites and heads byte for byte up to the pack offset for identical refs and held refs.
2. **Thin pack.** `test_anchor_bundle_omits_prerequisite_tree_objects` checks the real archive's pack header against both the `rev-list --objects` count plus one and the cap of eight. It indexes the pack and checks that its original object IDs have no overlap with the prerequisite tree, excluding any delta bases appended by `index-pack --fix-thin`.
3. **Restore round trip.** `test_anchor_bundle_restores_the_worktree_and_every_staged_blob` checks the archive, restores the worktree and compares the full snapshot and index. It checks the staged change and cached diff, verifies every staged object in the source, and fetches the bundle into a fresh clone of the synthetic server and checks every staged object there.
4. **Partial clones keep the default.** `test_partial_clone_keeps_default_pack_walk` checks the legacy extension, promisor values, filter-only and real filtered clone cases. `test_only_bundle_create_sets_non_sparse` and `test_archive_only_sets_non_sparse_for_bundle_create` check that the normal held-ref case adds the setting once, before `bundle`, and confines it to bundle creation. The `test_repository_reason_reports_*` tests check the exact partial-clone reason and the unreadable-config prefix.
5. **Delete bundles are self-contained.** `test_delete_bundle_skips_pack_setting_and_clone_detection` records the guarded delete Git argv for an inner repository with one commit. Verification fetches its bundle into a fresh empty repository. The test checks that the empty-held bundle has no sparse override and the builder adds no partial-clone config reads. `test_retention_delete_commit_preservation.py` covers standalone restoration and refusal of a bundle with source prerequisites.

## Test runs and mutations

The first fail-first run used the original production code at `53a7bac93`. Test (a) printed `Anchor pack object count: 304; rev-list count: 4` and failed with `assert 304 <= 5`. Both (d) tests failed at `create.count(SPARSE_SETTING) == 1` with `assert 0 == 1`. Tests (b), (c), (e) and (f) passed on that run. The new delete test initially used a job outside the operator-delete classes. Its baseline recheck reached a real self-contained bundle and exposed normal selection-time config reads, so the final assertion matches the scratch Git directories of the bundle builders. The restore test's recheck hit the maintenance slice under host load. The final archive fixtures allow a 480-second slice, and the restore test batches the source-side blob queries while retaining an individual `cat-file -e` check for each staged blob in the fresh clone.

All 13 post-change tests passed. Test (a) printed `Anchor pack object count: 4; rev-list count: 4` and found no prerequisite objects in the original pack. Both (d) tests found one override on bundle creation and none on other commands. All six mutants failed on the intended assertions. M6 produced a 315-object pack and a header without the prerequisite.

Every row below ran this command with the indicated run name and targets. The job directory was the assigned workspace's `build/bsp-build-r1`. The `after` and `final` runs set `PYTEST_ADDOPTS=-rP` to print the passing pack count.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$JOB/pytest-$RUN" -q $TARGETS
```

| Run | Start (UTC) | Free | Load (1/5/15 min) | Targets | Pass / fail / skip | End / peak (KiB) |
| --- | --- | ---: | --- | --- | ---: | ---: |
| baseline | 10:38:58Z | 90G | 66.82 / 46.12 / 39.01 | new module | 9 / 4 / 0 | 18412 / 18652 |
| baseline-recheck | 10:47:44Z | 79G | 101.94 / 83.74 / 62.48 | B, G | 0 / 2 / 0 | 5288 / 5812 |
| after | 10:57:11Z | 71G | 90.83 / 80.23 / 69.33 | new module | 13 / 0 / 0 | 18372 / 18904 |
| m1 | 11:00:49Z | 71G | 60.60 / 79.38 / 72.18 | A, D1, D2 | 0 / 3 / 0 | 4352 / 5864 |
| m2 | 11:01:28Z | 71G | 42.51 / 72.58 / 70.04 | C (all three markers) | 0 / 3 / 0 | 1696 / 1832 |
| m3 | 11:01:42Z | 70G | 35.83 / 69.63 / 69.03 | C[promisor], F[promisor] | 0 / 2 / 0 | 1060 / 1120 |
| m4 | 11:02:04Z | 71G | 28.62 / 65.81 / 67.67 | C[extension], F[extension] | 0 / 2 / 0 | 1100 / 1148 |
| m5 | 11:02:25Z | 71G | 24.67 / 62.50 / 66.44 | D1, D2 | 0 / 2 / 0 | 1160 / 1384 |
| m6 | 11:03:03Z | 72G | 16.72 / 55.73 / 63.78 | A, E | 0 / 2 / 0 | 6536 / 9472 |
| full | 11:05:04Z | 88G | 14.75 / 41.99 / 57.24 | all 20 modules listed below | 1234 / 6 / 0 | 282780 / 282780 |
| control-base | 12:26:11Z | 71G | 13.82 / 21.52 / 33.09 | H, I with original production source | 0 / 2 / 0 | 0 / 0 |
| final | 12:26:35Z | 71G | 16.77 / 21.60 / 32.79 | new module, with G scoped to verification | 13 / 0 / 0 | 18308 / 18600 |
| control-after | 12:27:29Z | 71G | 13.86 / 20.16 / 31.56 | J, K, L with updated production source | 3 / 0 / 0 | 1036 / 2760 |

The new module is `tests/unit/test_retention_bundle_pack.py`. Target letters are node IDs within that module.

| ID | Test |
| --- | --- |
| A | `test_anchor_bundle_omits_prerequisite_tree_objects` |
| B | `test_anchor_bundle_restores_the_worktree_and_every_staged_blob` |
| C | `test_partial_clone_keeps_default_pack_walk` |
| D1 | `test_only_bundle_create_sets_non_sparse` |
| D2 | `test_archive_only_sets_non_sparse_for_bundle_create` |
| E | `test_bundle_has_same_header_as_git_default` |
| F | `test_repository_reason_reports_partial_clone` |
| G | `test_delete_bundle_skips_pack_setting_and_clone_detection` |

The baseline control ran H and I. The post-change timeout control ran J, K and L.

- H — `tests/unit/test_retention_git_common_dir.py::test_no_repository_up_to_the_root_needs_no_process`
- I — `tests/unit/test_retention_git_common_dir.py::test_ambient_probe_has_no_template_or_local_config_override`
- J — `tests/unit/test_retention_delete_apply.py::test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do`
- K — `tests/unit/test_retention_alternates.py::test_c8_4_git_quoted_generated_paths_resolve_to_the_same_canonical_objects`
- L — `tests/unit/test_retention_delete_commit_preservation.py::test_arbitrary_multi_entry_pseudo_refs_preserve_every_commit`


| Mutant | Failure that caught it |
| --- | --- |
| M1 removed the per-command setting | A exceeded the object cap. D1 and D2 found zero overrides. |
| M2 set it for partial clones | All three C variants found the override. |
| M3 read only the extension | C[promisor] found the override and F[promisor] returned no partial-clone reason. |
| M4 read only promisor configuration | C[extension] found the override and F[extension] returned no partial-clone reason. |
| M5 set the flag globally | D1 and D2 found the override on other commands. |
| M6 dropped `--not <held>` | A exceeded the object cap and E found a changed header. |

Full-run targets

```text
tests/unit/test_retention_bundle_pack.py
tests/unit/test_retention_archive.py
tests/unit/test_retention_archive_properties.py
tests/unit/test_retention_regenerable.py
tests/unit/test_retention_git_process_r2.py
tests/unit/test_retention_git_common_dir.py
tests/unit/test_retention_alternates.py
tests/unit/test_retention_delete_commit_preservation.py
tests/unit/test_retention_delete_apply.py
tests/unit/test_retention_delete_resume.py
tests/unit/test_retention_delete_final_preservation.py
tests/unit/test_retention_salvage_properties.py
tests/unit/test_retention_81_notes.py
tests/unit/test_retention_packed_retirement_r2.py
tests/unit/test_retention_sweep_race.py
tests/unit/test_notice_keep.py
tests/fake/test_state_contract.py
tests/unit/test_invariants_index.py
tests/unit/test_test_module_names.py
tests/unit/test_handler_refusals.py
```

The one full-module run finished with 1,234 passes and six failures in 4,806.12 seconds. Its sampled peak was 282,780 KiB (about 290 MB), within the approved 1 GB exception. The following failures were observed.

- `test_no_repository_up_to_the_root_needs_no_process` found the enclosing checkout's Git common directory. `test_ambient_probe_has_no_template_or_local_config_override` also failed with the job's temp directory inside the checkout. Both failed again in the targeted control with `subfleet/retention_git.py` at `53a7bac93` under the same fallback.
- `test_delete_bundle_skips_pack_setting_and_clone_detection` hit the 540-second timeout during post-verification deletion checks. The final test runs the real guarded verifier and stops after successful verification. This keeps the argv and self-contained checks and avoids timing the unrelated tree removal.
- `test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do` hit the 540-second timeout while the bundle-heads guard was scanning the enclosing checkout's metadata during apply.
- `test_c8_4_git_quoted_generated_paths_resolve_to_the_same_canonical_objects` hit the 540-second timeout during synthetic repo setup. Hypothesis reported a flaky failure because the timeout did not reproduce on retry.
- `test_arbitrary_multi_entry_pseudo_refs_preserve_every_commit` hit the 540-second timeout while writing a kept ref. Hypothesis also observed an unsuccessful deletion when rerunning the generated case.

The final bundle module passed all 13 tests in 18.92 seconds. The three existing timeout cases passed in the targeted post-change rerun in 51.09 seconds. The full module list ran once.

## Measurement

Both sets streamed bundles directly to `wc -c` from the assigned worktree with `GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1`. Each pair used `git -c core.hooksPath=/dev/null -c gc.auto=0 [ -c pack.useSparse=false ] bundle create -`. The first set used the original `refs/subfleet-archive/20261125-214433-wcall-review-r5/fe50fe37507ee60fc5cc4b5ecb55cba8419c991c --not --glob=refs/remotes/origin/*`. The second used `HEAD --not 53a7bac93d01afd9165588bcaf789dae75540757^`, with HEAD at `53a7bac93d01afd9165588bcaf789dae75540757`.

The initial macOS `/usr/bin/time -l` attempt at 11:31 local (91 GB free, load 28.89/24.41/31.12) streamed 87,363,753 bytes in 37.49 s real, 2.00 s user and 0.45 s sys. Its `kern.clockrate` sysctl was denied before it could print resident memory. The three complete pairs in each set use monotonic elapsed time and Darwin `wait4` child resource usage. RSS is the reported maximum in bytes.

| Case | Start (local) | Free (`df -H /`) | Load average (1/5/15 min) |
| --- | --- | ---: | --- |
| Anchor | 11:33 | 91 GB | 54.33 / 36.08 / 34.88 |
| Ordinary | 11:37 | 90 GB | 53.52 / 39.91 / 36.45 |

| Case | Run | Walk | Bytes | Real (s) | User (s) | Sys (s) | Max RSS (bytes) |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Anchor | 1 | default | 87,362,745 | 72.719 | 1.927 | 0.534 | 201,801,728 |
| Anchor | 1 | non-sparse | 442,983 | 20.026 | 0.056 | 0.113 | 20,594,688 |
| Anchor | 2 | default | 87,371,804 | 61.086 | 1.660 | 0.461 | 202,752,000 |
| Anchor | 2 | non-sparse | 442,983 | 9.332 | 0.065 | 0.174 | 20,856,832 |
| Anchor | 3 | default | 87,371,804 | 49.343 | 1.613 | 0.390 | 180,109,312 |
| Anchor | 3 | non-sparse | 442,983 | 19.966 | 0.076 | 0.189 | 20,889,600 |
| Anchor | median | default | 87,371,804 | 61.086 | 1.660 | 0.461 | 201,801,728 |
| Anchor | median | non-sparse | 442,983 | 19.966 | 0.065 | 0.174 | 20,856,832 |
| Ordinary | 1 | default | 14,713 | 1.283 | 0.032 | 0.024 | 26,099,712 |
| Ordinary | 1 | non-sparse | 14,713 | 0.397 | 0.033 | 0.023 | 33,259,520 |
| Ordinary | 2 | default | 14,713 | 0.416 | 0.027 | 0.018 | 26,509,312 |
| Ordinary | 2 | non-sparse | 14,713 | 0.500 | 0.033 | 0.021 | 33,325,056 |
| Ordinary | 3 | default | 14,713 | 0.610 | 0.030 | 0.022 | 26,034,176 |
| Ordinary | 3 | non-sparse | 14,713 | 0.607 | 0.036 | 0.024 | 32,178,176 |
| Ordinary | median | default | 14,713 | 0.610 | 0.030 | 0.022 | 26,099,712 |
| Ordinary | median | non-sparse | 14,713 | 0.500 | 0.033 | 0.023 | 33,259,520 |

The non-sparse anchor pack was 197× smaller and took about one-third of the default's median wall time. The ordinary pack kept the same byte size and added 0.004 s to median user plus system time and about 7.2 MB to peak RSS. Its wall times varied under host load. These two cases showed no median wall-time slowdown.

### Synthetic 50,000-file case

`git fast-import` built a synthetic bare repository with 50,000 distinct blobs in nested folders. Git plumbing built an anchor with an object-ID-named `index/` tree and one staged change. The prerequisite commit was `cdc304c57eb4577c007957644d6fc9e1b53b881f` and the anchor was `dabe5b136f2866b254585de31a66eebde22d86b6`. The timed pairs used the same stdout pipeline and child resource counters, with `refs/subfleet-archive/scale/anchor --not --glob=refs/remotes/origin/*`. Before setup and again before timing, `df -H /` showed 71 GB free and the 13:34 local load averages were 14.68 / 19.17 / 26.89. The repo used 6,128 KiB before timing and at cleanup, within the 150 MB cap.

| Run | Walk | Bytes | Real (s) | User (s) | Sys (s) | Max RSS (bytes) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | default | 3,291,959 | 0.584 | 0.312 | 0.017 | 32,014,336 |
| 1 | non-sparse | 2,401,924 | 0.298 | 0.185 | 0.014 | 21,872,640 |
| 2 | default | 3,291,959 | 0.544 | 0.292 | 0.018 | 29,655,040 |
| 2 | non-sparse | 2,401,924 | 0.385 | 0.176 | 0.012 | 21,577,728 |
| 3 | default | 3,291,959 | 0.494 | 0.310 | 0.016 | 31,965,184 |
| 3 | non-sparse | 2,401,924 | 0.366 | 0.179 | 0.016 | 21,446,656 |
| median | default | 3,291,959 | 0.544 | 0.310 | 0.017 | 31,965,184 |
| median | non-sparse | 2,401,924 | 0.366 | 0.179 | 0.014 | 21,577,728 |

At 50,000 files the non-sparse median was 0.366 s, down from 0.544 s, with lower CPU time and peak RSS. The measured anchor-shaped bundles got smaller and faster in both repositories.

## Limits and deviations

- Partial clones keep their existing packing behavior. The Disk manager measured 0.749 GB across thesis-ci-speed's two full-tree bundles.
- Jobs kept as `remote-less-history` retain the cached size floor. Parked archives can reuse cached bundles. Published archives are not repacked, and their headers, verification and restore remain compatible.
- The original filtered-clone test used a real `file://` clone with `blob:none` and no checkout, then added the legacy extension. Round 1 checks the config written by a real filtered clone and tests the legacy extension on its own.
- The sandbox denied directory creation in Darwin's user temp root and writes to the existing UV cache. The job used the allowed `build/bsp-build-r1/` fallback and a copy of the cache. The worktree had no `.venv`, so the failed import probe was followed by `uv sync --group dev --offline`. Xdist 3.8.0 resolved offline. A job-local pytest startup hook redirected shared failed-state pruning into each run's basetemp. The pytest process's default temporary directory was also scoped to the job folder.
- Staging the named files failed in the original job because the sandbox denied creation of its worktree's `index.lock`. The hub committed those changes as `953c2819d`. The original mutant runner restored and compared the saved source after each edit because restoring from HEAD would have reverted the finished production change.
- The optional generated-layout Hypothesis property was omitted. The full run and the targeted controls used the remaining window for starting tests.

The full-run cleanup hit a restored read-only fixture directory and left 65,708 KiB of its basetemp during the targeted reruns. Final cleanup restored owner permissions on the synthetic directories and removed the remainder. All test basetemps, the synthetic measurement repo, run logs, UV cache copy and virtual environment were removed. No job artifacts were left outside the worktree. The pre-existing UV cache was left in place.

## Review round 1

Started `2026-11-28T02:34:32Z` on detached `953c2819d19327753863669e51b81a602df160be` with no tracked changes or untracked files. The job's Git was 2.55.0.

- **Partial clones.** `partial_clone` asks Git to normalize promisor booleans and checks for a remote filter or the legacy extension. `scratch_reason` uses the same predicate. The tests cover `true`, `yes`, `1`, `on`, a bare key, a filter alone, `false` with a filter, and `remote.untrue.promisor=false` without a filter. They also check that `maybe` makes the boolean probe raise `GitError` and that bundle creation then attempts Git's default walk. The job's Git allowed `file://` transport for real `blob:none` clones. The modern fixture checks that Git writes `promisor=true` and `partialclonefilter=blob:none` without `extensions.partialClone`. An offline receiver that lacked an unchanged staged blob before fetching the `promisor=yes` sender's bundle can read every staged blob afterward with `GIT_NO_LAZY_FETCH=1`.
- **Admin-directory objects.** The archive table now describes how a prerequisite supplies an admin-directory object. The q7 Part A regression puts a prerequisite subtree under the anchor's `trees/` and a prerequisite blob under `blobs/`. The non-sparse pack carries neither object. An independent receiver holding the prerequisite fetches the bundle and resolves both entries. The guide's I3 still holds because prerequisite objects are reachable from the network remote's refs. Its existing condition that the remote retain those refs' objects covers this case, and its text matches the base.
- **Probe cancellation and fallback.** A `Cancelled` from the probe leaves `create_bundle` without creating a file. A probe `GitError` leaves the default walk in effect and the bundle is created. An unparseable config also leaves the default in effect for the attempt, but Git exits 128 and produces no bundle. The code comment, C-8.4 and archive guide describe the attempt and its possible failure.
- **Per-command configuration.** `run` rejects keys already in `GIT_CONFIG`, including mixed-case and valueless forms, and rejects per-command configuration under a command guard. Tests check both refusals before subprocess launch and read the permitted `pack.useSparse=false` from Git outside a guard.
- **Cancellation message.** `run` skips leading inline `-c` pairs when naming the command. Process tests expect `git for-each-ref cancelled` with `git_dir` and keyword `config`, and `git fetch cancelled` with leading inline pairs. The delete verifier's inline `fetch.fsckObjects=true` call was read from source.
- **Report and scope.** The original commit, cancellation history and one-commit delete fixture are corrected above. The pre-existing reachability-bitmap finding remains with the hub. The work used synthetic repositories and no data covered by the data rule.

The PR body is untouched. “`partial_clone` shares the two config reads that `scratch_reason` already makes” is stale because the shared probe can now check three markers. “`tests/unit/test_retention_bundle_pack.py` (13 tests) checks that” and “all three partial-clone cases keep the default” undercount the module's 32 cases and nine partial-clone cases. “If Git can't read the clone's config, `create_bundle` keeps the default too” describes the walk for the attempt only. Malformed syntax makes Git exit before writing a bundle. “Its cancellation message still names the subcommand” also misstates the old behavior for `git_dir` and inline `-c` callers. The opening claim that the bundle holds only objects the held refs do not reach needs the full-repository and prerequisite-tree conditions. In the `promisor=yes` restore test the default walk deliberately carries staged blobs the prerequisite reaches. [PR #352](https://github.com/MaxGhenis/subfleet-v2/pull/352)

### Round 1 validation

Each run used the assigned worktree and this command, with `JOB="$PWD/build/bsp-fix-r1"`. The `baseline`, `after`, `m7` and `m8` runs used `tests/unit/test_retention_bundle_pack.py` and `tests/unit/test_retention_git_process_r2.py`. The `full` run used the 20 full-run targets listed above, in that order. Its `PYTEST_ADDOPTS='-o tmp_path_retention_policy=failed'` let pytest remove passed tests' temporary paths during the run.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$JOB/pytest-$RUN" -q $TARGETS
```

| Run | Start (UTC, 2026-11-28) | Free (`df -H /`) | Load (1/5/15 min) | Pass / fail / skip | Basetemp at cleanup (KiB) |
| --- | --- | ---: | --- | ---: | ---: |
| baseline | 02:42:32Z | 77G | 79.27 / 74.27 / 59.02 | 27 / 21 / 0 | 20000 |
| after | 02:46:53Z | 76G | 83.15 / 79.07 / 65.06 | 48 / 0 / 0 | 19964 |
| m7 | 02:49:19Z | 76G | 53.58 / 67.96 / 62.85 | 47 / 1 / 0 | 20008 |
| m8 | 02:53:12Z | 72G | 67.40 / 59.21 / 59.62 | 45 / 3 / 0 | 19964 |
| full | 02:57:13Z | 72G | 75.09 / 64.38 / 61.59 | 1239 / 9 / 0, interrupted | 3380 |

The baseline failed on the missed partial markers, the `untrue` false positive, the invalid boolean, the offline staged blob, the configuration refusals and the inline `-c` cancellation message. With the fix all 48 tests passed.

- M7 changed `except GitError` to `except Exception`. `test_bundle_creation_propagates_probe_cancellation` failed because the cancellation was swallowed.
- M8 selected the non-sparse setting in the `GitError` handler. `test_bundle_creation_keeps_default_when_probe_raises_git_error`, `test_invalid_promisor_keeps_default_pack_walk` and `test_bundle_creation_fails_with_unparseable_config` all caught that setting on the bundle attempt.

The one 20-module run was interrupted after 7,282.26 seconds of pytest runtime to leave time for the report and cleanup before the job cutoff. The sandbox denied the direct signal to the pytest controller. Interrupting the tool session produced pytest's summary and exit 130. Some recovery cases were incomplete. The nine reported failures were as follows.

- `test_no_repository_up_to_the_root_needs_no_process` found the enclosing checkout, and `test_ambient_probe_has_no_template_or_local_config_override` rejected that ambient configuration. Both reproduced the original job's workspace fallback failures.
- `test_temporary_directory_source_is_scratch_by_default` observed no scratch reason because this round's launcher placed `TMPDIR` beside its basetemp.
- `test_claimed_remote_history_is_kept_in_a_self_contained_bundle[True]` timed out while the bundle guard scanned the enclosing checkout's objects. `test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree` timed out while waiting for a Git subprocess.
- `test_archive_then_restore_round_trips_a_trusted_repository` and `test_arbitrary_multi_entry_pseudo_refs_preserve_every_commit` reported non-reproducing 540-second timeouts during Hypothesis runs.
- `test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight` failed because the normal pass pruned neither job. `test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept[keeping-5]` returned `failed` on the retry. These two assertions were left unisolated within the time budget.

The mutation runner restored the source byte for byte before the next run. Setup used `build/bsp-fix-r1/` after Darwin denied creation of the selected temporary directory. UV could not open its shared cache for writes, so the job used a local copy. The worktree had no `.venv`. The import probe failed and `uv sync --group dev --offline` installed the development dependencies. Xdist resolved offline. A job-local pytest plugin redirected failed-state pruning and copies into the current basetemp. The launcher set `TMPDIR` to a separate runtime directory beside each basetemp. Existing fixtures that call `tempfile` directly used that job-local directory. The sibling layout also prevented the ordinary temporary-directory check from recognizing the basetemp as temporary. The new tests used their own basetemps.

The sandbox denied `git add` for the two test files with `Operation not permitted` on the worktree's `index.lock`. The changes are available together in this worktree for salvage. The base-to-HEAD log is empty. The uncommitted files are `tests/unit/test_retention_bundle_pack.py`, `tests/unit/test_retention_git_process_r2.py`, `subfleet/retention_git.py`, `docs/acceptance-contract.md`, `docs/desktop/retention-archive.md` and this report.

Read-only inspection from the assigned worktree also used `git diff-tree` and `git show` for the base commit. The I3 comparison used `git cat-file`.

All five guarded basetemp removals completed. The job removed the corresponding `runtime-*` directories, `build/bsp-fix-r1/` with its transport probe, cache copy, scripts and logs, the empty `build/` directory, `.venv`, `.pytest_cache` and the Python `__pycache__` directories it created under `subfleet/` and `tests/`. It created no folder outside the worktree. The pre-existing Darwin UV cache was left in place. No recorded basetemp reached 200 MB. No new pack measurements were performed. `git diff --check` passed, and the six named modifications were the only workspace artifacts left.
