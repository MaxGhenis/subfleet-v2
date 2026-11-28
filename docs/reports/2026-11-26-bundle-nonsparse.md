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
- **Per-command configuration.** `run` rejects keys already in `GIT_CONFIG`, including mixed-case and valueless forms, and rejects per-command configuration under a command guard. Tests check both refusals before subprocess launch and read the permitted `pack.useSparse=false` from Git outside a guard. Fix round 2 replaced the refusal of any configuration under a guard with an allowlist that admits only `pack.useSparse=false`; see [Fix round 2](#fix-round-2).
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
- `test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight` failed because the normal pass pruned neither job. `test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept[keeping-5]` returned `failed` on the retry. These two assertions were left unisolated within the time budget. Fix round 2 found the normal-pass failure's cause, round 1's guard refusal of the archive bundle's `pack.useSparse=false`; see [Fix round 2](#fix-round-2).

The mutation runner restored the source byte for byte before the next run. Setup used `build/bsp-fix-r1/` after Darwin denied creation of the selected temporary directory. UV could not open its shared cache for writes, so the job used a local copy. The worktree had no `.venv`. The import probe failed and `uv sync --group dev --offline` installed the development dependencies. Xdist resolved offline. A job-local pytest plugin redirected failed-state pruning and copies into the current basetemp. The launcher set `TMPDIR` to a separate runtime directory beside each basetemp. Existing fixtures that call `tempfile` directly used that job-local directory. The sibling layout also prevented the ordinary temporary-directory check from recognizing the basetemp as temporary. The new tests used their own basetemps.

The sandbox denied `git add` for the two test files with `Operation not permitted` on the worktree's `index.lock`. The changes are available together in this worktree for salvage. The base-to-HEAD log is empty. The uncommitted files are `tests/unit/test_retention_bundle_pack.py`, `tests/unit/test_retention_git_process_r2.py`, `subfleet/retention_git.py`, `docs/acceptance-contract.md`, `docs/desktop/retention-archive.md` and this report.

Read-only inspection from the assigned worktree also used `git diff-tree` and `git show` for the base commit. The I3 comparison used `git cat-file`.

All five guarded basetemp removals completed. The job removed the corresponding `runtime-*` directories, `build/bsp-fix-r1/` with its transport probe, cache copy, scripts and logs, the empty `build/` directory, `.venv`, `.pytest_cache` and the Python `__pycache__` directories it created under `subfleet/` and `tests/`. It created no folder outside the worktree. The pre-existing Darwin UV cache was left in place. No recorded basetemp reached 200 MB. No new pack measurements were performed. `git diff --check` passed, and the six named modifications were the only workspace artifacts left.

## Fix round 2

Started `2026-11-28T06:02:20Z` on detached `944a68b273000623850d7528e867b0f2ed0989bb`, the hub's commit of round 1, with a clean index and no untracked files. The job used Git 2.55.0 (`/opt/homebrew/bin/git`, the only `git` on `PATH`) and the lane checkout's venv (Python 3.15.2, a GIL build). Line numbers below are at the base unless marked "head".

### The failing node and its cause

`tests/unit/test_retention_delete_apply.py::test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight` failed at the base on its assertion at line 522: `'n-flight' not in [] and 'n-normal' in []`. Round 1 made `run` refuse any per-command `config` while a command guard is set (`retention_git.py:132–134`). The test's `on_step` callback runs a whole `retention.maintenance` pass at the `renamed` step, and `on_step` runs inside the delete path's guarded `_one` (`retention_delete.py:1334`; `_saved` calls `_hook` at `:1873`, and `_hook` calls `on_step` at `:1421–1423`). That pass's archive step calls `create_bundle` with held refs (`retention_archive.py:2146`). With no partial clone, `create_bundle` passes `config=("pack.useSparse=false",)` (`retention_git.py:1273–1284`), so `run` raised `ValueError` and the pass put `n-normal` back.

A pytest plugin under the job's scratch folder wrapped `retention._Pass._rollback` and recorded each call. With `subfleet/retention_git.py` at the base's bytes, the node failed, and the plugin recorded `rollback job=n-normal reason=error detail=ValueError: per-command Git config is disallowed under a command guard`. At head the node passed, and the plugin recorded no rollback.

### Production reach

1. **Callers of `create_bundle`.** There are two. `retention_archive.py:2146` passes `held`, the network remotes' refs from `rgit.held_arguments(remotes)` (`:2101`). `retention_delete.py:2109` passes `[]`. `create_bundle` adds the setting only under `if held:` (`retention_git.py:1273`).
2. **Can the held-refs caller run under a guard?** Only through the test seam. `command_guard` is entered only at `retention_delete.py:1334` (`_one`) and `:2479` (`_resume`), and it resets the guard in a `finally` (`retention_git.py:86–87`). The archive's call is in `_Builder._git` (`retention_archive.py:2084`), reached through `_Builder._run` (`:1803`), `Retirement.archive` (`:838`, `:845`) and `_Pass.run` (`retention.py:713`), so only `retention.maintenance` builds an archive bundle. The daemon calls `maintenance` from `_retention` (`daemon.py:6046`) only after `_retention_delete_turn` returns, and that turn takes the delete path's step at `:5993`. The guarded blocks call into `retention_archive` only for `Interrupted`, `journals`, `load_journal` and `tree_away`, into `retention` only for `_owned_worktree` and `_pin_reasons`, and into `retention_survey` only for `job_info`. None of these builds an archive. The callbacks that run inside the guard are `holders` (`lsof_holders` by default), `cwd_pids`, `now`, `clock`, `pins` (the daemon's `_retention_pins`, `daemon.py:5944`, which reads conversation pins and ramp rows), `log` (warning and info lines) and `on_step`. The daemon builds the path as `DeletePath(self.store, self.root, log=self.log)` (`daemon.py:1037`), so `on_step` is `None` in production. No module the guarded blocks reach starts a thread or an executor. A search for `Thread(`, `ThreadPoolExecutor`, `ProcessPoolExecutor`, `.submit(`, `start_new_thread` and `copy_context` in `retention_delete`, `retention_delete_guard`, `retention_survey`, `retention`, `retention_archive`, `retention_holders`, `procs`, `dependencies`, `folders`, `retention_fs`, `retention_git`, `retention_marks`, `retention_qos` and `launch_env` found none. In this venv, `sys.flags.thread_inherit_context` is 0, so a new thread would start without the guard anyway.
3. **Does a production path reach the refusal?** No. Only the test seam does. The delete path's own kept-commits bundle (`_one_guarded` → `_delete` at `retention_delete.py:1372` → `_keep` at `:1963` → `_bundle` at `:2018` → `create_bundle(scratch, …, [])` at `:2109`) carries no `config`. `test_delete_bundle_skips_pack_setting_and_clone_detection` runs that guarded path for real and asserts that no command carries a `pack.useSparse` setting. It passed in the hub's run at the base and in every run of its module here.
4. **Where the nested pass caught it.** The `ValueError` left `_Builder.run` (`retention_archive.py:1729–1736`, which catches only `_Parked`) and `Retirement.archive`. `_Pass.run` caught it at `retention.py:717`, in one clause with `OSError`, `rgit.GitError` and `subprocess.SubprocessError`. That clause calls `_rollback(retirement, "error", rarch.DEFER_ERROR_S, f"{type(exc).__name__}: {exc}")`, so a `ValueError` is handled exactly like a `GitError`: the job is put back and deferred, and nothing is pruned.

### The fix

`subfleet/retention_git.py` gains `_GUARDED_CONFIG = frozenset({("pack.usesparse", "false")})` beside `_GIT_CONFIG_KEYS` (head `:55`). Under a guard, `run` admits a pair only if it contains `=` and its lower-cased key with its exact value is in that set (head `:139–143`). Any other pair raises `ValueError("per-command Git config for <key> is not admitted under a command guard")` before the guard's `check` runs and before Git starts. The message names only the key, so a refused value never reaches a rollback reason or a log.

Why an allowlist:
- It is the narrowest rule that keeps review r1's intent. A guard's `check(args, git_dir, cwd)` never sees `config`, so the guard admits only a setting that changes how `pack-objects` walks objects, not what Git reads, runs or contacts.
- The environment route would need a guard change. `run` applies the guard's forced environment last (`retention_git.py:139`), and `ISOLATED_CONFIG` sets `GIT_CONFIG_COUNT=0` and `GIT_CONFIG_PARAMETERS=""`, so an environment setting is wiped under a guard.
- Moving the pair into `args`, as `_verify_bundle` does with `fetch.fsckObjects`, would put it beyond both refusals.

The key is compared without regard to case, as Git reads keys, so `PACK.USESPARSE=false` is admitted. The value must be exactly `false`. A value-less `pack.useSparse` (Git reads it as true), `pack.useSparse=true` and `pack.useSparse=no` are refused. An exact value avoids reimplementing Git's boolean parser, and no caller passes another spelling of false.

Unchanged by the fix:
- The hardening-key refusal (head `:152–155`) is byte for byte the same and applies inside and outside a guard.
- The guard's `check` still runs for every guarded command, the bundle included: the guarded-bundle test asserts that the check saw `bundle create`.
- No allowlisted key is a `GIT_CONFIG` key, and a test pins that.
- The docstring line at the base's `:127` now states the rule (head `:132–133`).

`retention_delete.py`, `retention_archive.py` and `retention_delete_guard.py` are unchanged.

### Tests

Every new docstring cites C-8.4, and no new test name contains `scratch` or `retention`. "Base" is the red run, against the unchanged `retention_git.py` with the new tests. "Head" is the green run.

| Node | Base | Head |
| --- | --- | --- |
| `test_retention_bundle_pack.py::test_guarded_bundle_with_held_refs_uses_non_sparse_walk` (new: `command_guard(<recording check>, env=ISOLATED_CONFIG)`, `create_bundle(git_dir, …, [SIDE], [HELD])`; the bundle exists with heads `{SIDE}`; only `bundle create` carries `pack.useSparse=false`; the check saw it; the pack holds just the side commit) | failed: `ValueError: per-command Git config is disallowed under a command guard` | passed |
| `test_retention_bundle_pack.py::test_guarded_anchor_bundle_omits_admin_objects_in_prerequisite` (new: under the same guard, an anchor naming a prerequisite subtree and blob packs neither, so the non-sparse walk takes effect despite the isolated environment) | failed: the same `ValueError` | passed |
| `test_retention_git_process_r2.py::test_a_command_guard_admits_only_the_non_sparse_pack_setting[pack.useSparse=false]` and `[PACK.USESPARSE=false]` (rewritten from `test_per_command_config_cannot_bypass_a_command_guard`: under a guard with `ISOLATED_CONFIG`, `config --get pack.useSparse` reads `false` and the check saw the command; `pack.threads=1` then raises before any further Git launch) | both failed: `ValueError` on the admitted pair | both passed |
| `test_retention_git_process_r2.py::test_the_guard_allowlist_names_no_hardening_key` (new) | failed: `AttributeError`, no `_GUARDED_CONFIG` | passed |
| `test_retention_git_process_r2.py::test_a_command_guard_refuses_overrides_of_git_hardening[gc.auto=1, Core.HooksPath=/elsewhere, core.fsmonitor]` (new control, `Popen` fails the test) | 3 passed | 3 passed |
| `test_retention_git_process_r2.py::test_a_command_guard_refuses_config_outside_its_allowlist[other-pack-key, command-key, non-sparse-key-without-value, non-sparse-key-true, non-sparse-key-other-false-spelling]` (new control: `pack.threads=1`, `core.sshCommand=/usr/bin/false`, `pack.useSparse`, `pack.useSparse=true`, `pack.useSparse=no`) | 5 passed | 5 passed |
| `test_retention_git_process_r2.py::test_per_command_config_cannot_override_git_hardening` (3 cases, unchanged) | 3 passed | 3 passed |
| `test_retention_git_process_r2.py::test_per_command_config_reaches_git_outside_a_guard` (unchanged) | passed | passed |
| `test_retention_delete_apply.py::test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight` (unchanged) | failed at line 522, `'n-flight' not in [] and 'n-normal' in []` | passed |

The side commit in the first test shares the prerequisite's tree, so its pack holds one commit under either walk. That check shows a well-formed bundle under the isolated environment, not a changed walk. The anchor test shows the walk change.

### Mutants

Each mutant was a one-run edit of `subfleet/retention_git.py` at head, run on both modules plus the failing node (61 nodes). After each run the file was rewritten from a saved copy, and `git diff --quiet HEAD -- subfleet/retention_git.py` passed with SHA-256 `f5de56c4fc650a036fce921dd17a1ea392c486c2f810a628b97d3ca4f3a91833`.

| Mutant | Result | Failed |
| --- | --- | --- |
| M1: the base's `if config: raise ValueError(...)` in place of the allowlist | 5 failed, 56 passed | both guarded bundle tests, both cases of the rewritten guard test, the failing node |
| M2: the allowlist check removed | 7 failed, 54 passed | all 5 out-of-allowlist cases and both cases of the rewritten guard test (`pack.threads=1` reached Git). The 3 guarded hardening cases passed: the hardening refusal raised `per-command Git config cannot override <key>` (probe run under M2) |
| M3: the hardening refusal removed | 3 failed, 58 passed | the 3 cases of `test_per_command_config_cannot_override_git_hardening` (outside a guard, `Popen` was reached). The 3 guarded hardening cases passed: the allowlist raised `per-command Git config for <key> is not admitted under a command guard` (probe run under M3) |
| M4: M2 and M3 together | 13 failed, 48 passed | the 3 guarded hardening cases, the 3 outside-guard hardening cases, the 5 out-of-allowlist cases and both cases of the rewritten guard test |

### Callers that pass Git config (ruling 3)

- `git grep -n -F 'config=' HEAD -- subfleet/`: the only `rgit.run` caller that passes `config` is `create_bundle` (base `retention_git.py:1284`, head `:1293`). Its pair runs under a guard only when a guarded caller passes held refs. The delete path passes none (point 3 above), so in production it never does; the `on_step` seam is the one way in. The other hits are `mcp_config=` keywords (`adapters/claude.py:1526`, `:1576`; `daemon.py:2256`, `:7128`).
- `git grep -n -F '"-c"' HEAD -- subfleet/` (and `-F "'-c'"`, which found nothing): the only `rgit.run` call whose `args` start with `-c` is `_verify_bundle`'s `-c fetch.fsckObjects=true fetch …` (`retention_delete.py:2060`). It runs under the delete path's guard, reached from `_bundle` (`:2110`) in the guarded `_one`. The guard's `check` sees that pair, because it is in `args`, but `GitReadGuard.__call__` only resolves absolute paths in `args` and checks the repository. Neither the allowlist nor the hardening refusal looks at pairs inside `args`. `fetch.fsckobjects` is not a `GIT_CONFIG` key. The other hits are not `rgit.run` calls: `conversations/diff.py:169`, `:174` (its own `_bounded` runner), `salvage.py:685` (`_git`), `host_push.py` (`SAFE_CONFIG` and its own settings), the Codex adapters and launch arguments, `taskpolicy -c` in `guardian.py:84` and `retention_qos.py:67`, `/bin/sh -c` in `review_publish.py:319`, and argument parsers in `credentials.py` and `publish_rule.py`. This round lists the inline pair and leaves it for the hub.

### Contract and docs

- C-8.4 and its change log are unchanged. At the base, `git grep -n -i -F -e 'per-command' -e 'command guard' 944a68b27 -- docs/` finds only this report and `docs/reports/G-review.md`, so no contract or guide sentence describes the guard refusal. C-8.4's sentence that a held-refs archive bundle from a full repository is packed without the sparse walk now holds under a guard too.
- `docs/desktop/retention-archive.md:1466–1468` is unchanged and still true. The delete path's command context still forces `ISOLATED_CONFIG` into each subprocess's environment and an empty init template. A per-command `-c pack.useSparse=false` is the caller's explicit setting, not ambient config, and the delete path's own commands pass none.
- The docstring at `retention_git.py:127` states the new rule (head `:132–133`).

### Validation

Every run used `uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp "$TMPDIR/pytest-<label>" -q` with `TMPDIR`, `HYPOTHESIS_STORAGE_DIRECTORY` and the basetemp under the worktree's `build/sf-bsp-fix2/`, `UV_PROJECT_ENVIRONMENT` set to the lane checkout's `.venv` and `PYTHONPATH` set to the worktree. The 20-module runs added `-m "not real_lsof and not real_cwd" -o tmp_path_retention_policy=failed`. The serial reruns appended `-n 0`, and the last value wins. Free space is the Available column of `df -k /System/Volumes/Data` in KiB. Load is `uptime`'s 1, 5 and 15-minute averages. Times are UTC on 2026-11-28.

| Run | Targets | Start–end | Load at start / end | Free KiB at start / end | Result | Basetemp KiB |
| --- | --- | --- | --- | --- | --- | ---: |
| red | the 9 targets above (18 nodes) | 06:14:58–06:17:19 | 35.91 38.07 38.37 / 44.67 39.92 38.95 | 71,084,592 / 70,807,792 | 6 failed, 12 passed | 1,184 |
| green | the same | 06:18:31–06:20:04 | 47.11 41.88 39.78 / 30.75 39.59 39.24 | 71,214,012 / 70,984,776 | 18 passed | 1,340 |
| green-modules | both modules | 06:20:22–06:22:05 | 26.73 38.26 38.77 / 32.99 36.26 37.86 | 70,987,448 / 70,980,576 | 60 passed | 20,528 |
| M1 | both modules + node | 06:22:48–06:26:48 | 42.53 38.15 38.47 / 30.89 33.43 36.48 | 71,112,204 / 70,931,676 | 5 failed, 56 passed | 21,172 |
| M2 | both modules + node | 06:27:00–06:30:18 | 31.71 33.48 36.44 / 33.61 35.15 36.71 | 70,935,740 / 71,005,588 | 7 failed, 54 passed | 21,236 |
| M3 | both modules + node | 06:30:57–06:35:36 | 33.72 34.86 36.52 / 37.43 30.83 34.03 | 71,299,316 / 70,961,424 | 3 failed, 58 passed | 21,224 |
| M4 | both modules + node | 06:36:36–06:40:02 | 44.86 34.17 35.06 / 28.21 30.07 33.16 | 70,894,792 / 70,928,788 | 13 failed, 48 passed | 21,292 |
| probe-base | the node, base `retention_git.py`, rollback plugin | 06:41:32–06:43:10 | 29.17 30.09 32.87 / 21.37 28.46 32.02 | 70,693,636 / 71,435,488 | 1 failed | 804 |
| probe-head | the node, head, rollback plugin | 06:43:13–06:45:24 | 21.37 28.46 32.02 / 30.72 29.79 32.02 | 71,429,132 / 71,062,216 | 1 passed | 756 |
| full-p1 | `test_retention_delete_apply.py`, `test_retention_delete_resume.py` | 06:46:43–06:57:02 | 32.98 30.79 32.19 / 22.34 25.68 28.69 | 71,389,620 / 70,783,868 | 35 passed | 0 |
| full-rest | the other 18 modules | 06:57:20–07:17:34, stopped | 22.64 25.56 28.58 / not recorded | 70,761,808 / not recorded | 1,212 of 1,242 reported: 1,210 passed, 2 failed; 30 unreported | 80 |
| rerun-layout | the 2 failed nodes, serially | 07:19:53–07:19:58 | 33.77 28.01 27.23 / 32.83 27.91 27.20 | 71,828,744 / 71,800,016 | 2 failed | 0 |
| rerun-interrupted | `test_interrupted_removal_resumes_and_leaves_no_half_tree`, serially | 07:20:06–07:20:15 | 30.30 27.53 27.07 / 29.14 27.38 27.02 | 71,776,896 / 71,766,556 | 1 passed (3.27 s call) | 80 |
| full-rest2 | the other 18 modules, `--dist worksteal` | 07:20:51–07:35:30 | 26.43 26.88 26.85 / 37.67 32.34 30.08 | 71,995,736 / 72,643,716 | 2 failed, 1,240 passed in 876.97 s | 80 |

The 20-module set has 1,277 nodes at head: the hub's 1,265 at the base plus this round's 12. Across full-p1 and full-rest2, 1,275 passed and 2 failed, and both failures reproduced serially:
- `test_retention_git_common_dir.py::test_no_repository_up_to_the_root_needs_no_process`: `rgit.common_dir(...)` returned `/Users/maxghenis/subfleet-v2/.git`, the shared repository behind this job's linked worktree, for a path under the basetemp inside that worktree. This is a layout failure.
- `test_retention_git_common_dir.py::test_ambient_probe_has_no_template_or_local_config_override`: `rgit.ambient_plain()` returned `False` with the basetemp inside the worktree. This is a layout failure, the same as round 1's.

Fix round 1's `[keeping-5]` case of `test_retention_delete_resume.py::test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept` passed in full-p1.

The tool stopped the full-rest run at its background limit. Each test sets up one `desktop` folder (the autouse `no_desktop_store` fixture), and the stopped basetemp held 1,213 of them across four workers. So one test was still running and 29 had not started, queued behind it on one worker. In full-rest2, `test_retention_sweep_race.py::test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree` took 536.70 s, 3.3 s under the timeout, and was probably that test. The stopped run's leftover `test_interrupted_removal_resum0` folder held a read-only fixture folder: the cleanup restored modes on one folder there. It was not a hang: that node passed serially in 3.27 s and passed in full-rest2. No run printed a `pruned` line. No fixture failed with "transport 'file' not allowed". No basetemp reached 200 MB, and the largest at cleanup was 21,292 KiB.

### What didn't run

- The full suite and CI. The hub runs the 20-module set outside any checkout and reads CI after the release merge.
- The mutants ran only on the two modules plus the failing node, as the brief set.
- The 2 layout failures were not fixed, as the brief set.

### PR body

The PR body is untouched. Two of its sentences are false after this round:
- "`retention_git.run` takes a keyword-only `config` and passes each pair as `-c` before `--git-dir`." Replacement: "`retention_git.run` takes a keyword-only `config` and passes each pair as `-c` after its standard settings and before `--git-dir`. It refuses a key the standard settings set, and under a command guard it admits only `pack.useSparse=false`."
- "`tests/unit/test_retention_bundle_pack.py` (13 tests) checks that:" Round 1 put the count at 32; it is now 34. Replacement: "`tests/unit/test_retention_bundle_pack.py` (34 tests) checks that:", with one more bullet: "a bundle with held refs built under the delete path's command guard still uses the non-sparse walk."

Round 1's list of other stale sentences, above, still applies.

### Deviations

- The 20-module set ran in three invocations, not once. The tool caps a foreground command at 600 s, moves a longer one to the background and stops it 10 minutes later, and the headless rules forbid starting background work. Part 1 (2 modules) finished after the move. The other 18 modules were stopped at 1,212 of 1,242 results. Their rerun with `--dist worksteal` finished in 876.97 s. The job waited on each moved run by polling its log from a foreground command with a bounded deadline.
- The job deleted the stopped run's basetemp before naming its running test, so the full-rest2 rerun replaced that identification.
- In full-rest2, `-v` cancelled the script's `-q`, so it printed dots, not per-test lines.
- Two extra runs (probe-base, probe-head) loaded a rollback-recording pytest plugin from the scratch folder. probe-base briefly replaced `subfleet/retention_git.py` with the base's bytes from `git show 944a68b27:subfleet/retention_git.py`, then restored it (clean `git diff`, same SHA-256).
- Two tests go beyond the brief's list: `test_guarded_anchor_bundle_omits_admin_objects_in_prerequisite`, a walk discriminator under the guard, and `test_the_guard_allowlist_names_no_hardening_key`. The rewritten guard test wraps `Popen` with a recorder rather than failing it, so it can read the admitted pair back and show no launch for the refused key.
- Small mutation and probe scripts ran with the system `python3`; they edit or read only files in the worktree.
- Disk churn was not measured. An estimate goes above the brief's guide of about 1 GB: the hub's full-set basetemp at the base was 279,912 KiB, the set ran about twice here, and the other runs' basetemps totalled about 110 MB. That gives roughly 1.3 GB written plus deleted. Measured at cleanup, the largest basetemp was 21,292 KiB. Peaks during a run were not measured.

### Cleanup

Every run reported 0 skipped and 0 deselected.

Inside the worktree, the job created and deleted `build/sf-bsp-fix2/`, which held:
- `runtime/`, the `TMPDIR`. It held the 14 `pytest-*` basetemps, each removed after its run by a guarded `shutil.rmtree` after owner modes were restored inside it (one folder each in full-rest and full-rest2). It also held what tests made through `TMPDIR`: `retention-salvage-hwwcwzf5` (888 KiB), six `sf-test-homes-*` folders (8 KiB each) and a uv lock file.
- `logs/` (96 KiB), `probe/` (4 KiB), `hypothesis/` (472 KiB), the run, cleanup, mutation and probe scripts, a saved copy of `retention_git.py`, and the two listings.

The folder was 1,596 KiB when it was removed, by a guarded `shutil.rmtree` that checked its realpath was inside the worktree and its last part was `sf-bsp-fix2`. The job then removed the empty `build/`, and `subfleet/__pycache__/__init__.cpython-315.pyc` with its folder. The first import check wrote that file at 06:02:40Z, before `PYTHONDONTWRITEBYTECODE` was set. `git status --porcelain --ignored --untracked-files=all` is now empty.

Outside the worktree, the job's commands created nothing. The Bash tool wrote its own four task-output files (16 KiB) under `/private/tmp/claude-501/-Users-maxghenis--subfleet-worktrees-20261128-010135-bsp-fix-r2/`, and they were left in place.

The `/private/tmp/sf-*` listing at 06:14:28Z had 34 entries. At 07:41Z, `sf-8qhn530s` and `sf-cutover-property-e1ifqzmn` were gone, and `sf-e2e-fq2jsz2z`, `sf-e2e-oodcxoy0`, `sf-e2e-px3ttr8i` and `sf-e2e-v2n62y3o` were new. Within a minute, those four had given way to other `sf-e2e-*` names, created at 07:41Z. `tests/e2e/conftest.py:372` makes `sf-e2e-*` roots. This job never ran an e2e test, and its last run ended at 07:35:30Z, so they belong to another lane and were left in place. `/private/tmp/sf-failed` was empty both times.
