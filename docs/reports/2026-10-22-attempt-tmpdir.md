# D-TMP1: private temporary storage per provider attempt

Base: `c0759bd658d6b55a656117badf3ccfa8271eab43`. Work began at 2026-10-22 07:41 UTC. All changes are confined to the assigned worktree. Shared Git metadata is unwritable, and the assigned branch is already checked out by the caller, so commits use workspace-local `.git-tmp1` metadata on `fix/attempt-tmpdir-217`. No history was rewritten and nothing was pushed.

## Round-1 fix (#246 review r1, 2026-10-23)

Review r1 asked for changes: three P2s and seven P3s, all executed. This section reports the fix round. It began at 06:42 UTC in a detached checkout at `a9b7c6195`. Where the build's text below disagrees with this section (salvage holds, `tmpdir_keep`, marker-based ageing, the single shared 24-hour clock), this section wins, and so does C-12.10.

Commits, oldest first. Shared Git metadata was writable this time, so these are ordinary commits; nothing was pushed.
- `7b4d43e7b`: merge `origin/release/217` at `7e2dc5a35` (#243, #245, #247, #249).
- `f931baee3`: test (the regressions, written before the fix).
- `8370b6e53`: fix.
- `529979343`: docs (C-12.10 and its change log; the disk comment).
- `af8001c86`: test (the c14_2 assertion reads `TMPDIR` from preflight's environment; `env_pass` is a tuple of names).
- `174bbc226`: docs (this section, without counts, and the PR body).
- `9a28f5343`: test, then `134908a5c`: fix. Both are for a defect hold h6 found (below).
- A last docs commit adds the counts.

### The merge

Two files conflicted. Both kept both sides.
- **`docs/acceptance-contract.md`, the version 3 change log.** It kept this branch's C-12.10 entry and every entry release added since the build's base, in release's order: C-20.6 (#226 r2's P3s), C-13.1 (D-SALV1), C-26.4/C-9.2, C-17.8/C-17.9, C-5.13, C-15.2, C-12.3 and C-17.9. The round-1 C-12.10 entry now sits above them, newest first.
- **`subfleet/daemon.py`, `_resolve_quarantine`.** Release added `self._cascade_final_wall(a, wall_exited)` inside the quarantine-release transaction, where release put it. This branch's TMPDIR cleanup still runs after that transaction commits. `tests/conftest.py` merged without conflict.

### Findings: cause and fix

**P2-1, the c14_2 test.**
- *Cause:* the build added `TMPDIR` to `credential_env`, which becomes guard preflight's environment and `env_pass`. The test still expected only `CODEX_HOME` and the certificate names.
- *Fix:* the expected set includes `TMPDIR`, as C-12.10 says it should. The test also checks that preflight's `TMPDIR` is the attempt's recorded folder with its trailing slash.

**P2-2, scratch that pinned its job for ever.**
- *Cause:* `remove()` refused a salvage-held attempt before it looked for the folder. So an attempt with `salvage_error` or `salvage_skipped` never got a receipt, and retention pinned its job. Salvage holds were written once and never cleared, and nothing wrote `tmpdir_keep`. Every sweep also took a census of each held attempt, and `_cleanup_probe_tmpdir` saved a probe record every time it ran. And `rmtree` could not delete a 0555 directory.
- *Fix, in `subfleet/attempt_tmp.py` and the daemon:*
  - **Holds.** `remove()` looks for the folder first: absent returns `"absent"`, whatever holds it. Otherwise only a terminal, non-quarantined holder with a verified-empty census may proceed. Salvage evidence is never read, and `retained()` and `tmpdir_keep` are gone.
  - **Release receipts.** A recorded allocation gets a receipt (`removed_at`, `outcome`) when it is `absent`, `removed`, `replaced` (its path holds something else) or `outside` (its path is not under this temp root). Nothing is removed in the last two cases.
  - **`cleared_at`.** A verified-empty census writes `cleared_at`. Retention's pin and `probe_tmpdir_rows(..., pinning=True)` skip an allocation with `cleared_at`, so a removal that keeps failing never pins its job. It is retried on each sweep with no new census.
  - **No repeated work.** `_release_tmpdir` takes no census and measures nothing for a live or quarantined holder. A terminal holder whose census found members (`live_pids`) is remembered with its state and gets no census again until that state changes. A daemon restart forgets this. An unverifiable census is asked again on the next pass.
  - **Probes.** A probe whose containment census was verified empty uses that census, and its record is saved only when the TMPDIR is released or cleared. The save merges into the newest record inside one transaction.
  - **Read-only trees.** Removal deletes through descriptors (`_empty`). It makes each directory inside the verified tree readable, writable and searchable by its owner first, never following a symlink and never above the tree. Symlinks are unlinked, never followed, and another device is never entered.
  - Quarantine still resolves only through `kill --confirm-dead`, after which the holder is terminal and the next sweep removes the folder (`test_quarantine_released_by_confirm_dead_then_swept`).

**P2-3, Codex's writable roots shrink.**
- *Cause:* this is D-TMP1 working as designed. Codex derives a workspace-write root from the child's `TMPDIR`, so the user temp root as a whole is no longer writable.
- *Fix:* kept. C-12.10, this report and the PR body now carry the operational note: briefs keep their scratch under `$TMPDIR`. The hub's `${TMPDIR%/}/sf-<tag>` works before and after this ships. Nothing in the hub's briefs was changed here.

**P3-4, age and identity came from the provider.**
- *Cause:* the sweep took the attempt id from the folder's marker and aged an unknown folder by `max(marker created_at, st_birthtime)`. A provider can write both: `utime(0, 0)` sets APFS birth time to 0.
- *Fix:*
  - The store decides everything. A folder is *known* if any allocation record names its path. That means an attempt's evidence or a probe's newest record, released or not.
  - An *unknown* folder is aged from the store's first sighting, `tmpdir.unknown_seen`. That event records the path, device, inode, owner and the daemon's clock. The folder is removed 24 hours later, after an identity check against that sighting, and the removal is recorded as `tmpdir.unknown_removed`. A different folder at the path starts a new sighting.
  - The allocation record itself is the attempt's evidence (`attempt.tmpdir`) or the probe record. It holds the path, holder, daemon-clock creation time, device, inode and owner.
  - The marker no longer carries a clock. It is consulted only to refuse: a folder without this store's marker, written for that exact folder, is never recorded or removed as unknown, and that covers another store's folders. Choosing it as a veto and not as authority is my reading of "never trust the marker's contents"; the review had confirmed that unmarked and foreign folders should be kept.
  - The false "cannot backdate the OS birth time" comment is gone, and the module docstring says what the store decides.

**P3-5, one allocation per attempt.**
- *Cause:* `_launch` called `create()` on every try. An `OSError` from `create()` escaped `_launch`, which left the attempt `reserved`.
- *Fix:* `_prepare_attempt_tmpdir` reuses the folder the attempt's evidence names while it is present and not released, and probes do the same. An allocation `OSError` becomes `_launch_failure(a, "attempt TMPDIR could not be allocated: <class>: [Errno N] <text>")`, and the attempt finalizes with that reason.

**P3-6, extra censuses.**
- No census is taken for an absent folder or a held holder (above), and none at probe completion when containment was verified empty.
- The startup sweep no longer runs in `Daemon.__init__`. `_recover_then_start_timers` schedules it as a one-shot worker job (`tmpdir-sweep`) once recovery has finished and the daemon is serving. A non-blocking lock keeps it from overlapping retention's sweep.
- `_tmpdir_turn` lets one cleanup per holder run at a time, so finalization and a sweep never remove the same folder at once.
- Re-timing, in one hold (h1, load 45 to 60). `test_probe_gate_rechecks_terminal_or_stamp[failed]` took 0.046 s at head (`8370b6e53`), 0.723 s at release/217 (`7e2dc5a35`) and 0.403 s at the build (`a9b7c6195`). These are pytest's setup, call and teardown times. The review's 3.38 s at host load 116 did not recur at this load.

**P3-7, the trailing slash.** `attempt_tmp.env_value()` gives the folder with one trailing slash. The attempt launch, guard preflight, probes and enrollment all pass that form, and the evidence keeps the canonical path without it.

**P3-8, identity after `owns()`.**
- *Cause:* `rmtree` ran on the path name after the check, so a directory renamed in between was deleted.
- *Fix, `AttemptTemp._remove`:*
  - Rename the folder to a fresh `subfleet-attempt-removing-<random>` tombstone in the same parent, then `lstat` the tombstone. It must be a directory of this user with the recorded device and inode: never a symlink, and the store's record.
  - Open it with `O_NOFOLLOW` and check its identity, then empty it through descriptors and `rmdir` it.
  - On a mismatch or any failure, rename it back, but never over a name that has reappeared, and refuse, logging why.
  - A crash leaves a tombstone. That is an unknown folder carrying this store's marker, which the sweep ages and removes.

**P3-9, conversation turns.** C-12.10 now says it: a turn is its own attempt, so a file a turn leaves in `$TMPDIR`, or a path under it shown to the person, is gone once the turn finalizes.

**P3-10, the budget sentence.** C-12.10 and the `disk.py` comment now say exactly what happens, and no policy value changed:
- Disk admission reserves `placement_reserve_gb` (1.5 GB) per placed attempt. It holds for `reserve_ttl_s` (600 s), or until execution ends, against the free space measured on the state root's volume.
- That one provisional amount stands for the growth of the worktree and the TMPDIR together until their bytes appear in measured free space.
- It is an estimate, not a quota, and measured scratch can exceed it: the build measured 1.56 GB by block sum.

### Invariants (stated and tested)

1. **Two attempts never share a TMPDIR.**
   - Unique names, two live launches, the generated model (`assert path not in all_paths`), and the `reuse` mutation.
2. **The folder exists, is 0700 and belongs to the user when the provider starts.**
   - `test_provider_starts_with_private_tmpdir` in all four modes, and the guardian test.
3. **A TMPDIR is never removed while its holder is live or quarantined or its census is not verified empty.**
   - `test_cleanup_requires_terminal_empty_attempt`, the held-sweep test, the generated model's held-identity check, and the `no-census` mutation.
4. **The sweep never removes a folder outside the prefix, a symlink, or a live attempt's folder.**
   - The prefix and symlink tests and mutations, the named-folder case, and the model's protected folder and symlink.
5. **Probes and turns get one.** The mode-parametrized spawn test.
6. **No permanent pin.** A job is never pinned by a TMPDIR that is absent, or whose holder is terminal with a verified-empty census.
   - `test_absent_folder_is_released_whatever_holds_it` and the `absent-no-receipt` mutation.
   - `test_salvage_skipped_jobs_both_retire_whatever_their_scratch`.
   - `test_a_cleared_or_absent_allocation_pins_nothing`.
   - The generated model's closing check that every absent or terminal-and-empty allocation is released.
   - `test_salvage_evidence_never_holds_scratch` and the `salvage-hold` mutation.
7. **One folder per attempt.** Three launch tries leave one folder, and the evidence names it (`test_launch_tries_of_one_attempt_share_one_allocation`).
8. **Identity.** A folder is removed only if it is the store's recorded allocation, or for an unknown folder the store's recorded sighting. A directory or symlink swapped in is never removed.
   - `test_directory_renamed_in_after_the_check_is_refused_and_put_back` and the `no-rename-verify` mutation.
   - The replaced-folder test and the swap-restarts-sighting test.
   - The generated model's victims, which are never removed.
   - Age: `test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time`, with the `age-from-marker` and `age-from-birth-time` mutations.

The property test (Hypothesis ci profile, at most 40 examples of at most 35 steps) now also generates:
- quarantine resolution;
- unknown allocations that carry this store's marker;
- swaps of a known folder for an unrelated directory;
- folders a provider deletes;
- the clock moving in 6-hour steps.

At each step it checks invariants 1, 3, 4 and 8. It also checks that no unknown folder goes before 24 hours have passed since the store's first sighting, and it ends with invariant 6.

### Mutations

Each mutant is the production module with one decision changed, executed in memory, and each must fail a named test. A second parametrized test runs every oracle against the real module, so a kill is the mutation's doing.

| Mutation | What changes | Test that fails |
| --- | --- | --- |
| reuse | one shared folder | `test_attempt_names_are_unique_even_after_path_sanitizing` |
| no-census | removal without the verified-empty check | `test_cleanup_requires_terminal_empty_attempt[succeeded-False]` |
| no-prefix | prefix check dropped | `test_sweep_requires_prefix_even_with_valid_creation_record` |
| follow-symlink | stat, open and chmod follow symlinks | `test_sweep_never_follows_symlink` |
| salvage-hold | salvage evidence keeps TMPDIR | `test_salvage_evidence_never_holds_scratch[salvage_skipped]` |
| absent-no-receipt | an absent folder is released only when terminal and verified | `test_absent_folder_is_released_whatever_holds_it[quarantined-False]` |
| no-rename-verify | delete the path with no tombstone or identity check | `test_directory_renamed_in_after_the_check_is_refused_and_put_back` |
| age-from-marker | age from the marker's `created_at` | `test_unknown_folder_ages_from_the_store_never_its_marker_or_birth_time` |
| age-from-birth-time | age from `st_birthtime` | the same test |

All nine are killed at head (h1: 18 of 18 in `test_attempt_tmpdir_mutations.py`).

### Regressions at `a9b7c6195`

In hold h1, the round-1 test files ran on `a9b7c6195`'s tree with only those five test files replaced by `f931baee3`'s: 64 failed and 19 passed. Every new or changed regression is among the 64, each failing on its new assertion:
- the c14_2 set: on the trailing slash, once the test's own `env_pass` bug is fixed (run in hold h3);
- both salvage-skipped jobs retiring: the job with TMPDIR evidence stayed;
- the 0555 tree;
- three sweeps over held scratch: censuses for the quarantined attempt and both probes;
- three launch tries: 3 folders;
- `ENOSPC` and `EACCES`: the `OSError` escaped `_launch`;
- the renamed-in directory, the backdated marker and birth time, and the trailing slash;
- startup sweeps in the constructor, the property, and every new mutation.

The 19 that passed are the build's unchanged cases.

### Found during validation, and fixed

Hold h6 ran the `tests/unit` modules that no round-1 hold had covered yet. In `tests/unit/test_daemon_settle.py`, 33 cases failed with `AttributeError: 'Daemon' object has no attribute '_tmpdir_guard'`.
- *Cause:* that module builds a `Daemon` with `object.__new__` and no `__init__`. Round 1's per-holder lock (`_tmpdir_turn`) and the sweep's lock were taken outside the `try` that keeps TMPDIR cleanup from ever failing finalization. The build had kept its whole cleanup inside that `try`.
- *Fix:* `134908a5c` moves both locks inside the guard. The regression test, `9a28f5343`, `test_tmpdir_cleanup_and_sweep_never_raise_into_their_callers`, deletes both locks from a fixture daemon and calls the attempt cleanup, the probe cleanup and the sweep: none raises, each logs, and the folder is kept.
- In hold h7, `test_daemon_settle.py` then passed 61 of 61 at `134908a5c`.
- This was this round's defect, not a base failure.

### Sandboxes, re-checked

Codex 0.159.0's `codex debug prompt-input -c sandbox_mode="workspace-write"` ran with a dummy `CODEX_HOME`, a dummy key, a dummy prompt and no model request, in a working folder inside this job's scratch. It listed the same writable root, the canonical folder with no slash, whether `TMPDIR` was given with its trailing slash or without. The two outputs differ only at line 4, presumably a per-run identifier. So P3-7's slash changes nothing for Codex, and `"${TMPDIR}name"` now names a file inside the folder (`test_tmpdir_value_has_one_trailing_slash`). I didn't re-run the review's `codex sandbox` write probe. Claude's `--add-dir` builders are unchanged since the build; the value they get now ends in `/`.

### Holds

Every pytest ran under `suite_hold.sh` with `TMPDIR` set to the job's own `sf-r246f1/hold-<pid>/` and Hypothesis storage in `sf-r246f1/hyp`. Each was single-process (pytest-xdist isn't installed) and started with `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`. The trees were `git archive` copies of the named commit, so a running hold never saw the working tree change. pytest ran in-process (`<venv>/bin/python -c 'pytest.main(...)'`) with a `SIGALRM` that stops it at 1,140 s or less, writing JUnit XML for per-file counts. Holds h1 and h3 each ran their pytests one after another inside one hold, to compare trees under the same load; nothing ran concurrently. No hold reached 20 minutes. Each hold's wait for a slot was separate from its run time.

| Hold | Queued, started (UTC) | Tree | What | Result |
| --- | --- | --- | --- | --- |
| h1 | 07:18, 07:31 (A) | `7e2dc5a35` (release) | `test_probe_cancel.py` | 23 passed, then the 150 s alarm |
| | | `8370b6e53` | 10 focused files, then `test_retention*` | 300 passed, 1 failed (the c14_2 test's own `env_pass` bug), alarm at 668 s |
| | | `a9b7c6195` + `f931baee3`'s 5 test files | the round-1 regressions | 64 failed, 19 passed (as intended) |
| | | `a9b7c6195` | `test_probe_gate_rechecks_terminal_or_stamp[failed]` | 1 passed |
| h2 | 07:47, 07:50 (A) | `af8001c86` | c14_2, then `test_retention*` (rest), then `tests/fake` | 213 passed, 4 skipped, alarm at 1,139 s |
| h3 | 08:10, 08:11 (B) | `a9b7c6195` + `af8001c86`'s c14_2 | c14_2 | failed on the trailing slash (as intended) |
| | | `af8001c86` | `tests/fake` from the start | 1,097 passed, 2 xfailed, alarm at 1,146 s |
| h4 | 08:30, 08:32 (B) | `af8001c86` | the rest of `tests/fake` | 1,020 passed, complete |
| h5 | 08:46, 08:57 (B) | `af8001c86` | the 55 unit modules r1 listed (61 files), plus the retention modules not yet run | 1,753 passed, 1 skipped, complete |
| h6 | 09:14, 09:17 (A) | `174bbc226` | 10 focused files, then the rest of `tests/unit` | 7,148 passed, 33 failed (`test_daemon_settle.py`, above), 1 skipped, alarm at 1,142 s |
| h7 | 09:37, 09:48 (B) | `134908a5c` | `test_daemon_settle.py`, the round-1 fake file, two possibly partial retention modules, then the rest | 3,934 passed, alarm at 1,141 s |
| h8 | 10:08, 10:08 | `134908a5c` | the last 17 unit modules | 671 passed, 12 skipped, complete |
| h9 | 10:10, 10:10 | `134908a5c` | the 10 focused files | 168 passed, complete |

The PR's six test files, `test_daemon_launch.py`, `test_probe_cancel.py`, `test_state_contract.py` and `test_test_module_names.py` all ran at the final code head (`134908a5c`, hold h9): 168 passed, 0 failed. Per file:
- `test_attempt_tmpdir_guardian.py`: 2
- `test_attempt_tmpdir_lifecycle.py`: 35
- `test_attempt_tmpdir_mutations.py`: 18
- `test_attempt_tmpdir_retention.py`: 6
- `test_attempt_tmpdir_sandboxes.py`: 12
- `test_attempt_tmpdir_daemon.py`: 24
- `test_daemon_launch.py`: 9
- `test_probe_cancel.py`: 24
- `test_state_contract.py`: 37
- `test_test_module_names.py`: 1

### Counts

Each file is counted from its latest complete run. A file a hold's alarm stopped in is counted from the run that finished it, and no partial count remains. All 452 modules of the two directories ran this round, and none is unrun.

| Directory | Modules | Passed | Skipped | xfailed | Failed |
| --- | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 339 of 339 | 13,468 | 18 | 0 | 0 |
| `tests/fake` | 113 of 113 | 2,152 | 0 | 2 | 0 |

Code head per run:
- `tests/fake`: at `af8001c86` (holds h3 and h4), whose code is `8370b6e53`'s. Its four focused files ran again at `134908a5c` (h9).
- `tests/unit`: at `af8001c86`, `174bbc226` or `134908a5c`.
- `134908a5c` changed only where cleanup and the sweep take their locks, inside their exception guard. A full daemon behaves as before.

No failure remains at any head, so none needed a base run to classify it. In order of appearance, the failures and skips were:
- **The c14_2 test's own assertion bug (h1)**, fixed in `af8001c86`.
- **The 33 `test_daemon_settle.py` cases (h6)**, this round's defect, fixed in `134908a5c`.
- **Skips, all environmental:**
  - `test_weekly_rank_vs_base.py`, 12 skipped: `git archive f3bffcea` fails in an archive tree with no Git history.
  - `test_suggestions_r1b_adversarial.py`, 1 skipped: its base commit isn't in an archive tree.
  - `test_retention_ordinary_trees.py`, 4 skipped: `sortedcontainers` isn't in the offline uv cache.
  - `test_host_push_build.py`, 1 skipped: an explicit local benchmark.
  - `tests/fake`, 2 xfailed: `test_admission_properties.py` and `test_canonical_identity_fix3.py`, existing markers.

Lane markers (`SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`) stayed set, as the brief's command leaves them, and no test failed because of them.

### Deviations and housekeeping

- **`rm -rf` once.** While refreshing my archive tree before hold h2, I removed `sf-r246f1/tree-head` with `rm -rf`, which breaks the brief's cleanup rule. It touched only that tree in this job's own scratch folder. Every later removal used the brief's `shutil.rmtree` command with its `/sf-` assertion.
- **`<venv>/bin/python` instead of `uv run pytest`.** I ran pytest from the scratch venv rather than `uv run pytest`, so that runs from the archive trees never re-synced the shared venv and never waited on uv's cache lock. The venv was built once, through `uv_sync_hold.sh`, as the brief says.
- **An in-process sanity pass.** Before the first hold, I called the unit lifecycle and mutation test functions in-process from a plain Python script, with no pytest runner and in scratch only. That caught nothing; it was to avoid spending a lock wait on an obvious mistake. Its results are not counted above.
- **A stray file search.** A search of `~/reviews` for older JUnit files was moved to the background by the shell and stopped within a minute. It wrote nothing.
- **Scratch and processes.** No real `subfleet-attempt-*` folder was created in the user temp root. Every test daemon used its pytest session's scratch, and the Codex probe used a folder inside `sf-r246f1`. No `.hypothesis` folder was left in the worktree.

### Round-1 per-file counts

Each file's latest complete run, by hold:

| File | Hold | Passed | Skipped | xfailed | Failed |
| --- | --- | ---: | ---: | ---: | ---: |
| `tests/fake/test_admission_disk.py` | h3 | 27 | 0 | 0 | 0 |
| `tests/fake/test_admission_held_waiters.py` | h3 | 1 | 0 | 0 | 0 |
| `tests/fake/test_admission_latency.py` | h3 | 14 | 0 | 0 | 0 |
| `tests/fake/test_admission_lease_waiters.py` | h3 | 16 | 0 | 0 | 0 |
| `tests/fake/test_admission_liveness.py` | h3 | 32 | 0 | 0 | 0 |
| `tests/fake/test_admission_priority.py` | h3 | 32 | 0 | 0 | 0 |
| `tests/fake/test_admission_properties.py` | h3 | 27 | 0 | 1 | 0 |
| `tests/fake/test_admission_ramp.py` | h3 | 19 | 0 | 0 | 0 |
| `tests/fake/test_admission_ramp_retention.py` | h3 | 11 | 0 | 0 | 0 |
| `tests/fake/test_admission_route_isolation.py` | h3 | 93 | 0 | 0 | 0 |
| `tests/fake/test_admission_stateful.py` | h3 | 17 | 0 | 0 | 0 |
| `tests/fake/test_admission_visibility.py` | h3 | 43 | 0 | 0 | 0 |
| `tests/fake/test_attempt_tmpdir_daemon.py` | h9 | 24 | 0 | 0 | 0 |
| `tests/fake/test_batch_label.py` | h3 | 9 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity.py` | h3 | 24 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix2.py` | h3 | 21 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix3.py` | h3 | 17 | 0 | 1 | 0 |
| `tests/fake/test_canonical_identity_fix4.py` | h3 | 11 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix5.py` | h3 | 14 | 0 | 0 | 0 |
| `tests/fake/test_capacity_steer_daemon.py` | h3 | 72 | 0 | 0 | 0 |
| `tests/fake/test_capacity_wait_backoff.py` | h3 | 30 | 0 | 0 | 0 |
| `tests/fake/test_census_escapers_daemon.py` | h3 | 9 | 0 | 0 | 0 |
| `tests/fake/test_census_recycled_pid_daemon.py` | h3 | 29 | 0 | 0 | 0 |
| `tests/fake/test_codex_ca_bundle_fake.py` | h3 | 6 | 0 | 0 | 0 |
| `tests/fake/test_codex_turn_completed_fake.py` | h3 | 109 | 0 | 0 | 0 |
| `tests/fake/test_conversation_pools.py` | h3 | 2 | 0 | 0 | 0 |
| `tests/fake/test_conversation_wakes_fake.py` | h3 | 4 | 0 | 0 | 0 |
| `tests/fake/test_crash_matrix.py` | h3 | 9 | 0 | 0 | 0 |
| `tests/fake/test_daemon_contract.py` | h3 | 21 | 0 | 0 | 0 |
| `tests/fake/test_daemon_descriptors.py` | h3 | 13 | 0 | 0 | 0 |
| `tests/fake/test_daemon_launch.py` | h9 | 9 | 0 | 0 | 0 |
| `tests/fake/test_daemon_stacks.py` | h3 | 6 | 0 | 0 | 0 |
| `tests/fake/test_descriptor_exhaustion.py` | h3 | 2 | 0 | 0 | 0 |
| `tests/fake/test_detached_cwd_pins.py` | h3 | 18 | 0 | 0 | 0 |
| `tests/fake/test_detached_retention_fence.py` | h3 | 89 | 0 | 0 | 0 |
| `tests/fake/test_enrollment_guardian.py` | h3 | 1 | 0 | 0 | 0 |
| `tests/fake/test_fake_provider.py` | h3 | 13 | 0 | 0 | 0 |
| `tests/fake/test_finalization_replay.py` | h3 | 10 | 0 | 0 | 0 |
| `tests/fake/test_folder_spelling.py` | h3 | 7 | 0 | 0 | 0 |
| `tests/fake/test_frozen_stream_nonregular.py` | h3 | 140 | 0 | 0 | 0 |
| `tests/fake/test_gate_end_to_end.py` | h3 | 11 | 0 | 0 | 0 |
| `tests/fake/test_identity_rollback.py` | h3 | 5 | 0 | 0 | 0 |
| `tests/fake/test_idle_cost.py` | h3 | 2 | 0 | 0 | 0 |
| `tests/fake/test_interactive_claude_titles.py` | h3 | 6 | 0 | 0 | 0 |
| `tests/fake/test_interactive_steer.py` | h3 | 13 | 0 | 0 | 0 |
| `tests/fake/test_lane_fault.py` | h3 | 20 | 0 | 0 | 0 |
| `tests/fake/test_lane_pilot.py` | h4 | 24 | 0 | 0 | 0 |
| `tests/fake/test_lanes_enroll.py` | h4 | 15 | 0 | 0 | 0 |
| `tests/fake/test_lanes_reenroll.py` | h4 | 19 | 0 | 0 | 0 |
| `tests/fake/test_lanes_reenroll_keychain.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_launch_env_daemon.py` | h4 | 49 | 0 | 0 | 0 |
| `tests/fake/test_launch_env_providers.py` | h4 | 6 | 0 | 0 | 0 |
| `tests/fake/test_launch_gates_above_stdio.py` | h4 | 9 | 0 | 0 | 0 |
| `tests/fake/test_lost_acknowledgement.py` | h4 | 3 | 0 | 0 | 0 |
| `tests/fake/test_main_refusal_write_dir.py` | h4 | 5 | 0 | 0 | 0 |
| `tests/fake/test_mcp_job_contract.py` | h4 | 12 | 0 | 0 | 0 |
| `tests/fake/test_native_maintenance_startup.py` | h4 | 41 | 0 | 0 | 0 |
| `tests/fake/test_notice_delivery.py` | h4 | 15 | 0 | 0 | 0 |
| `tests/fake/test_notice_header.py` | h4 | 23 | 0 | 0 | 0 |
| `tests/fake/test_picker_native.py` | h4 | 3 | 0 | 0 | 0 |
| `tests/fake/test_policy_sandbox.py` | h4 | 25 | 0 | 0 | 0 |
| `tests/fake/test_probe_cancel.py` | h9 | 24 | 0 | 0 | 0 |
| `tests/fake/test_probe_recovery.py` | h4 | 9 | 0 | 0 | 0 |
| `tests/fake/test_probe_turn.py` | h4 | 43 | 0 | 0 | 0 |
| `tests/fake/test_prompt_seam.py` | h4 | 8 | 0 | 0 | 0 |
| `tests/fake/test_provider_active_cap_fake.py` | h4 | 60 | 0 | 0 | 0 |
| `tests/fake/test_provider_cap_review_evidence.py` | h4 | 5 | 0 | 0 | 0 |
| `tests/fake/test_provider_qos.py` | h4 | 2 | 0 | 0 | 0 |
| `tests/fake/test_record_secrecy.py` | h4 | 44 | 0 | 0 | 0 |
| `tests/fake/test_request_routing.py` | h4 | 3 | 0 | 0 | 0 |
| `tests/fake/test_reserve_authorization.py` | h4 | 12 | 0 | 0 | 0 |
| `tests/fake/test_reserve_authorization_validation.py` | h4 | 11 | 0 | 0 | 0 |
| `tests/fake/test_reset_credit_incident.py` | h4 | 19 | 0 | 0 | 0 |
| `tests/fake/test_resume_cli.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_resume_contract.py` | h4 | 23 | 0 | 0 | 0 |
| `tests/fake/test_retention_dormant.py` | h4 | 7 | 0 | 0 | 0 |
| `tests/fake/test_retry_for_good_refusals.py` | h4 | 8 | 0 | 0 | 0 |
| `tests/fake/test_review130_ab.py` | h4 | 12 | 0 | 0 | 0 |
| `tests/fake/test_review_r185_adversarial.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_review_r223_wall_probes.py` | h4 | 23 | 0 | 0 | 0 |
| `tests/fake/test_route_view.py` | h4 | 8 | 0 | 0 | 0 |
| `tests/fake/test_routing_end_to_end.py` | h4 | 16 | 0 | 0 | 0 |
| `tests/fake/test_routing_tier_preferences.py` | h4 | 5 | 0 | 0 | 0 |
| `tests/fake/test_salvage_adversarial.py` | h4 | 8 | 0 | 0 | 0 |
| `tests/fake/test_salvage_finalization.py` | h4 | 47 | 0 | 0 | 0 |
| `tests/fake/test_sessions_end_to_end.py` | h4 | 33 | 0 | 0 | 0 |
| `tests/fake/test_state_contract.py` | h9 | 37 | 0 | 0 | 0 |
| `tests/fake/test_state_fixture_processes.py` | h4 | 4 | 0 | 0 | 0 |
| `tests/fake/test_status_payload.py` | h4 | 2 | 0 | 0 | 0 |
| `tests/fake/test_suggestions_fake.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_tier_hold_by_demand.py` | h4 | 27 | 0 | 0 | 0 |
| `tests/fake/test_timers_busy_admission.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_timers_end_to_end.py` | h4 | 7 | 0 | 0 | 0 |
| `tests/fake/test_turn_listing.py` | h4 | 9 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_cwd.py` | h4 | 6 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_cwd_properties.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_job_directory.py` | h4 | 2 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_job_properties.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_quarantine.py` | h4 | 10 | 0 | 0 | 0 |
| `tests/fake/test_turn_trees.py` | h4 | 18 | 0 | 0 | 0 |
| `tests/fake/test_turn_wait_reasons.py` | h4 | 7 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_admission.py` | h4 | 24 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_rereview.py` | h4 | 6 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_review.py` | h4 | 7 | 0 | 0 | 0 |
| `tests/fake/test_wall_cancel_races.py` | h4 | 30 | 0 | 0 | 0 |
| `tests/fake/test_wall_inspection_order.py` | h4 | 18 | 0 | 0 | 0 |
| `tests/fake/test_wall_running_time.py` | h4 | 35 | 0 | 0 | 0 |
| `tests/fake/test_wall_stop_deliveries.py` | h4 | 25 | 0 | 0 | 0 |
| `tests/fake/test_workspace_contract.py` | h4 | 8 | 0 | 0 | 0 |
| `tests/fake/test_workspace_enospc.py` | h4 | 1 | 0 | 0 | 0 |
| `tests/fake/test_workspace_transient.py` | h4 | 24 | 0 | 0 | 0 |
| `tests/fake/test_writable_fanout.py` | h4 | 28 | 0 | 0 | 0 |
| `tests/fake/test_writer_retention_fence.py` | h4 | 23 | 0 | 0 | 0 |
| `tests/unit/test_admission_uncapped.py` | h6 | 27 | 0 | 0 | 0 |
| `tests/unit/test_agent_secret_credentials.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_app_cutover_daemon.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_approval_commit.py` | h6 | 6 | 0 | 0 | 0 |
| `tests/unit/test_approval_resolution.py` | h6 | 33 | 0 | 0 | 0 |
| `tests/unit/test_attempt_tmpdir_guardian.py` | h9 | 2 | 0 | 0 | 0 |
| `tests/unit/test_attempt_tmpdir_lifecycle.py` | h9 | 35 | 0 | 0 | 0 |
| `tests/unit/test_attempt_tmpdir_mutations.py` | h9 | 18 | 0 | 0 | 0 |
| `tests/unit/test_attempt_tmpdir_retention.py` | h9 | 6 | 0 | 0 | 0 |
| `tests/unit/test_attempt_tmpdir_sandboxes.py` | h9 | 12 | 0 | 0 | 0 |
| `tests/unit/test_boot_identity.py` | h6 | 10 | 0 | 0 | 0 |
| `tests/unit/test_capacity_desktop.py` | h6 | 20 | 0 | 0 | 0 |
| `tests/unit/test_capacity_steer_mutations.py` | h6 | 10 | 0 | 0 | 0 |
| `tests/unit/test_capacity_steer_route.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_capacity_view.py` | h6 | 20 | 0 | 0 | 0 |
| `tests/unit/test_census_counted.py` | h6 | 5 | 0 | 0 | 0 |
| `tests/unit/test_census_identity_uncertainty.py` | h6 | 8 | 0 | 0 | 0 |
| `tests/unit/test_census_procargs.py` | h6 | 52 | 0 | 0 | 0 |
| `tests/unit/test_census_recycled_pid.py` | h6 | 25 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_failures.py` | h6 | 20 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_identity.py` | h6 | 17 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_races.py` | h6 | 34 | 0 | 0 | 0 |
| `tests/unit/test_census_survivors_review.py` | h6 | 14 | 0 | 0 | 0 |
| `tests/unit/test_ci_retry_known_flakes.py` | h6 | 72 | 0 | 0 | 0 |
| `tests/unit/test_ci_shard_plugin.py` | h6 | 48 | 0 | 0 | 0 |
| `tests/unit/test_claude_adapter.py` | h6 | 170 | 0 | 0 | 0 |
| `tests/unit/test_claude_attest.py` | h6 | 19 | 0 | 0 | 0 |
| `tests/unit/test_claude_cards.py` | h6 | 96 | 0 | 0 | 0 |
| `tests/unit/test_claude_cards_wiring.py` | h6 | 15 | 0 | 0 | 0 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | h6 | 8 | 0 | 0 | 0 |
| `tests/unit/test_claude_identity.py` | h6 | 30 | 0 | 0 | 0 |
| `tests/unit/test_claude_large_stream.py` | h6 | 30 | 0 | 0 | 0 |
| `tests/unit/test_claude_mcp.py` | h6 | 32 | 0 | 0 | 0 |
| `tests/unit/test_claude_mcp_launch.py` | h6 | 11 | 0 | 0 | 0 |
| `tests/unit/test_claude_stream.py` | h6 | 60 | 0 | 0 | 0 |
| `tests/unit/test_claude_synthetic_error.py` | h6 | 63 | 0 | 0 | 0 |
| `tests/unit/test_claude_turn.py` | h6 | 69 | 0 | 0 | 0 |
| `tests/unit/test_claude_usage.py` | h6 | 28 | 0 | 0 | 0 |
| `tests/unit/test_claude_v1_parity.py` | h6 | 6 | 0 | 0 | 0 |
| `tests/unit/test_cli.py` | h6 | 248 | 0 | 0 | 0 |
| `tests/unit/test_cli_batch.py` | h6 | 18 | 0 | 0 | 0 |
| `tests/unit/test_cli_turns.py` | h6 | 23 | 0 | 0 | 0 |
| `tests/unit/test_client_availability.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_codex_adapter.py` | h6 | 172 | 0 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle.py` | h6 | 46 | 0 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle_provider.py` | h6 | 6 | 0 | 0 | 0 |
| `tests/unit/test_codex_probe.py` | h6 | 55 | 0 | 0 | 0 |
| `tests/unit/test_codex_shim.py` | h6 | 31 | 0 | 0 | 0 |
| `tests/unit/test_codex_turn.py` | h6 | 67 | 0 | 0 | 0 |
| `tests/unit/test_codex_turn_completed.py` | h6 | 41 | 0 | 0 | 0 |
| `tests/unit/test_compat.py` | h6 | 1615 | 0 | 0 | 0 |
| `tests/unit/test_continuation_r2.py` | h6 | 11 | 0 | 0 | 0 |
| `tests/unit/test_conversation_catalog.py` | h6 | 15 | 0 | 0 | 0 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | h6 | 21 | 0 | 0 | 0 |
| `tests/unit/test_conversation_connections.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_diff.py` | h6 | 39 | 0 | 0 | 0 |
| `tests/unit/test_conversation_fifo_reads.py` | h6 | 26 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff.py` | h6 | 48 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff_briefs.py` | h6 | 28 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_history.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_conversation_peers_attachments.py` | h6 | 44 | 0 | 0 | 0 |
| `tests/unit/test_conversation_reconcile.py` | h6 | 36 | 0 | 0 | 0 |
| `tests/unit/test_conversation_redact.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_service.py` | h6 | 100 | 0 | 0 | 0 |
| `tests/unit/test_conversation_state_reads.py` | h6 | 18 | 0 | 0 | 0 |
| `tests/unit/test_conversation_status.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_steer_service.py` | h6 | 45 | 0 | 0 | 0 |
| `tests/unit/test_conversation_store.py` | h6 | 32 | 0 | 0 | 0 |
| `tests/unit/test_conversation_stream_assertion.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_conversation_titles.py` | h6 | 89 | 0 | 0 | 0 |
| `tests/unit/test_conversation_waits.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_wakes.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_cpu_scaling.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_daemon_accept.py` | h6 | 5 | 0 | 0 | 0 |
| `tests/unit/test_daemon_connections.py` | h6 | 31 | 0 | 0 | 0 |
| `tests/unit/test_daemon_fifo_reads.py` | h6 | 27 | 0 | 0 | 0 |
| `tests/unit/test_daemon_init_release.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_daemon_inspection_load.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_daemon_inspection_properties.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_daemon_integration.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_daemon_log_scrubbing.py` | h6 | 86 | 0 | 0 | 0 |
| `tests/unit/test_daemon_qos_tools.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | h6 | 8 | 0 | 0 | 0 |
| `tests/unit/test_daemon_settle.py` | h7 | 61 | 0 | 0 | 0 |
| `tests/unit/test_daemon_verbs.py` | h6 | 20 | 0 | 0 | 0 |
| `tests/unit/test_decision_horizon.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_descriptor_review_fixes.py` | h6 | 56 | 0 | 0 | 0 |
| `tests/unit/test_descriptors.py` | h6 | 16 | 0 | 0 | 0 |
| `tests/unit/test_descriptors_desktop.py` | h6 | 8 | 0 | 0 | 0 |
| `tests/unit/test_desktop_account_cache.py` | h6 | 3 | 0 | 0 | 0 |
| `tests/unit/test_desktop_in_use.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_desktop_ledger.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_disk_admission.py` | h6 | 27 | 0 | 0 | 0 |
| `tests/unit/test_disk_floor.py` | h6 | 60 | 0 | 0 | 0 |
| `tests/unit/test_doctor.py` | h6 | 44 | 0 | 0 | 0 |
| `tests/unit/test_doctor_ca_bundle.py` | h6 | 16 | 0 | 0 | 0 |
| `tests/unit/test_doctor_identity.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_empty_provider_results.py` | h6 | 334 | 0 | 0 | 0 |
| `tests/unit/test_enrollment_runner.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_failed_keep.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_failed_keep_layout.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_failed_keep_prune.py` | h6 | 25 | 0 | 0 | 0 |
| `tests/unit/test_failed_keep_teardown.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_fake_codex_http.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_filesystem_identity_agreement.py` | h6 | 40 | 0 | 0 | 0 |
| `tests/unit/test_folders.py` | h6 | 23 | 0 | 0 | 0 |
| `tests/unit/test_forget_paced.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_fresh_until.py` | h6 | 1 | 0 | 0 | 0 |
| `tests/unit/test_frontend_probe_cache.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_frozen_provider_streams.py` | h6 | 217 | 0 | 0 | 0 |
| `tests/unit/test_gate_admission.py` | h6 | 24 | 0 | 0 | 0 |
| `tests/unit/test_gate_merge.py` | h6 | 59 | 0 | 0 | 0 |
| `tests/unit/test_gate_replay.py` | h6 | 23 | 0 | 0 | 0 |
| `tests/unit/test_gate_revision.py` | h6 | 16 | 0 | 0 | 0 |
| `tests/unit/test_gate_service.py` | h6 | 25 | 0 | 0 | 0 |
| `tests/unit/test_gate_verdict.py` | h6 | 45 | 0 | 0 | 0 |
| `tests/unit/test_guard_overlay.py` | h6 | 22 | 0 | 0 | 0 |
| `tests/unit/test_guard_trust.py` | h6 | 98 | 0 | 0 | 0 |
| `tests/unit/test_guardian.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_guardian_qos.py` | h6 | 14 | 0 | 0 | 0 |
| `tests/unit/test_handler_refusals.py` | h6 | 317 | 0 | 0 | 0 |
| `tests/unit/test_held_waiter_holdback.py` | h6 | 6 | 0 | 0 | 0 |
| `tests/unit/test_hidden_environment.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_hook_cost.py` | h6 | 5 | 0 | 0 | 0 |
| `tests/unit/test_hooks.py` | h6 | 73 | 0 | 0 | 0 |
| `tests/unit/test_hooks_native_guard.py` | h6 | 18 | 0 | 0 | 0 |
| `tests/unit/test_host_push.py` | h6 | 118 | 0 | 0 | 0 |
| `tests/unit/test_host_push_admission.py` | h6 | 15 | 0 | 0 | 0 |
| `tests/unit/test_host_push_build.py` | h6 | 10 | 1 | 0 | 0 |
| `tests/unit/test_host_push_build_budget.py` | h6 | 15 | 0 | 0 | 0 |
| `tests/unit/test_host_push_cli.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_host_push_config_cost.py` | h6 | 7 | 0 | 0 | 0 |
| `tests/unit/test_host_push_copy.py` | h6 | 11 | 0 | 0 | 0 |
| `tests/unit/test_host_push_export.py` | h6 | 10 | 0 | 0 | 0 |
| `tests/unit/test_host_push_intake.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_host_push_metadata.py` | h6 | 33 | 0 | 0 | 0 |
| `tests/unit/test_host_push_mutations.py` | h6 | 24 | 0 | 0 | 0 |
| `tests/unit/test_host_push_names.py` | h6 | 13 | 0 | 0 | 0 |
| `tests/unit/test_host_push_quarantine_restart.py` | h6 | 8 | 0 | 0 | 0 |
| `tests/unit/test_host_push_restart.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_host_push_retention.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_storage_recovery.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_storage_retry_budget.py` | h6 | 60 | 0 | 0 | 0 |
| `tests/unit/test_host_push_store_completion_pr183.py` | h6 | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_store_interleavings.py` | h6 | 26 | 0 | 0 | 0 |
| `tests/unit/test_host_push_trees.py` | h7 | 5 | 0 | 0 | 0 |
| `tests/unit/test_host_push_validated_top.py` | h7 | 8 | 0 | 0 | 0 |
| `tests/unit/test_importer.py` | h7 | 148 | 0 | 0 | 0 |
| `tests/unit/test_inspection_pacing_properties.py` | h7 | 1 | 0 | 0 | 0 |
| `tests/unit/test_invariants_index.py` | h7 | 25 | 0 | 0 | 0 |
| `tests/unit/test_keychain_exec_guard_properties.py` | h7 | 3 | 0 | 0 | 0 |
| `tests/unit/test_lane_answers.py` | h7 | 27 | 0 | 0 | 0 |
| `tests/unit/test_lanes_transfer.py` | h7 | 79 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_allowlist.py` | h6 | 27 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_helpers.py` | h6 | 20 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_mutations.py` | h6 | 41 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_policy.py` | h6 | 26 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round2.py` | h6 | 67 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round3.py` | h6 | 69 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round4.py` | h6 | 40 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round5.py` | h6 | 39 | 0 | 0 | 0 |
| `tests/unit/test_lease_waiter_selection.py` | h7 | 6 | 0 | 0 | 0 |
| `tests/unit/test_legacy_hold.py` | h7 | 56 | 0 | 0 | 0 |
| `tests/unit/test_limit_from_model_text.py` | h7 | 95 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard.py` | h7 | 324 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round10.py` | h7 | 66 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | h7 | 18 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round3.py` | h7 | 22 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round4.py` | h7 | 8 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round5.py` | h7 | 11 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round6.py` | h7 | 112 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round7.py` | h7 | 50 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | h7 | 23 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round8.py` | h7 | 100 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | h7 | 4 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round9.py` | h7 | 89 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | h7 | 12 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_state_root.py` | h7 | 22 | 0 | 0 | 0 |
| `tests/unit/test_lockwatch.py` | h7 | 20 | 0 | 0 | 0 |
| `tests/unit/test_mcp_cli_contract.py` | h7 | 6 | 0 | 0 | 0 |
| `tests/unit/test_mcp_scheduler_contract.py` | h7 | 5 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_faults.py` | h7 | 29 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_model.py` | h7 | 12 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_stateful.py` | h7 | 2 | 0 | 0 | 0 |
| `tests/unit/test_native_operations.py` | h7 | 28 | 0 | 0 | 0 |
| `tests/unit/test_notice_rendering.py` | h7 | 21 | 0 | 0 | 0 |
| `tests/unit/test_notify_push.py` | h7 | 31 | 0 | 0 | 0 |
| `tests/unit/test_offline.py` | h7 | 33 | 0 | 0 | 0 |
| `tests/unit/test_operator_hold_closures.py` | h7 | 15 | 0 | 0 | 0 |
| `tests/unit/test_operator_notices.py` | h7 | 79 | 0 | 0 | 0 |
| `tests/unit/test_outcome_unknown.py` | h7 | 58 | 0 | 0 | 0 |
| `tests/unit/test_person_boundary.py` | h7 | 97 | 0 | 0 | 0 |
| `tests/unit/test_person_boundary_identity.py` | h7 | 25 | 0 | 0 | 0 |
| `tests/unit/test_picker.py` | h7 | 39 | 0 | 0 | 0 |
| `tests/unit/test_placement_ramp.py` | h7 | 26 | 0 | 0 | 0 |
| `tests/unit/test_policy.py` | h7 | 178 | 0 | 0 | 0 |
| `tests/unit/test_policy_disk.py` | h6 | 57 | 0 | 0 | 0 |
| `tests/unit/test_policy_ramp.py` | h7 | 46 | 0 | 0 | 0 |
| `tests/unit/test_policy_support.py` | h7 | 14 | 0 | 0 | 0 |
| `tests/unit/test_policy_tier_preferences.py` | h7 | 15 | 0 | 0 | 0 |
| `tests/unit/test_pr124_fixes.py` | h7 | 10 | 0 | 0 | 0 |
| `tests/unit/test_pr124_review_r2_fixes.py` | h7 | 6 | 0 | 0 | 0 |
| `tests/unit/test_pr192_review_r1.py` | h7 | 21 | 0 | 0 | 0 |
| `tests/unit/test_pr192_review_r2.py` | h7 | 9 | 0 | 0 | 0 |
| `tests/unit/test_procs.py` | h7 | 48 | 0 | 0 | 0 |
| `tests/unit/test_protocol_secrecy.py` | h7 | 322 | 0 | 0 | 0 |
| `tests/unit/test_provider_active_cap.py` | h7 | 50 | 0 | 0 | 0 |
| `tests/unit/test_prune_217.py` | h7 | 9 | 0 | 0 | 0 |
| `tests/unit/test_prune_decision_backup_verification.py` | h7 | 23 | 0 | 0 | 0 |
| `tests/unit/test_prune_decision_fingerprints.py` | h7 | 15 | 0 | 0 | 0 |
| `tests/unit/test_prune_decisions.py` | h7 | 48 | 0 | 0 | 0 |
| `tests/unit/test_prune_restore.py` | h7 | 4 | 0 | 0 | 0 |
| `tests/unit/test_quota_projection.py` | h7 | 28 | 0 | 0 | 0 |
| `tests/unit/test_rank_review_report.py` | h7 | 3 | 0 | 0 | 0 |
| `tests/unit/test_reap_liveness.py` | h7 | 7 | 0 | 0 | 0 |
| `tests/unit/test_rediscovery_git_trace.py` | h7 | 17 | 0 | 0 | 0 |
| `tests/unit/test_relay.py` | h7 | 30 | 0 | 0 | 0 |
| `tests/unit/test_render.py` | h7 | 20 | 0 | 0 | 0 |
| `tests/unit/test_render_quota_projection.py` | h7 | 23 | 0 | 0 | 0 |
| `tests/unit/test_reserve_authorization_plumbing.py` | h7 | 5 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_claim_timing.py` | h7 | 2 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_demand.py` | h7 | 68 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_properties.py` | h7 | 11 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r1.py` | h7 | 22 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r2.py` | h7 | 52 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r3.py` | h7 | 23 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | h7 | 12 | 0 | 0 | 0 |
| `tests/unit/test_resource_identity.py` | h7 | 10 | 0 | 0 | 0 |
| `tests/unit/test_retention_81_notes.py` | h1 | 38 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive.py` | h7 | 109 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive_properties.py` | h2 | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_case_alias.py` | h2 | 56 | 0 | 0 | 0 |
| `tests/unit/test_retention_nested_leases.py` | h2 | 27 | 0 | 0 | 0 |
| `tests/unit/test_retention_ordinary_trees.py` | h2 | 0 | 4 | 0 | 0 |
| `tests/unit/test_retention_qos.py` | h2 | 8 | 0 | 0 | 0 |
| `tests/unit/test_retention_readback.py` | h2 | 37 | 0 | 0 | 0 |
| `tests/unit/test_retention_rediscovery.py` | h2 | 17 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable.py` | h2 | 26 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable_properties.py` | h2 | 1 | 0 | 0 | 0 |
| `tests/unit/test_retention_salvage_properties.py` | h2 | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_shared_folders.py` | h7 | 62 | 0 | 0 | 0 |
| `tests/unit/test_retention_sweep_race.py` | h5 | 13 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_contract.py` | h5 | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_workdirs.py` | h5 | 1 | 0 | 0 | 0 |
| `tests/unit/test_retention_turns.py` | h5 | 7 | 0 | 0 | 0 |
| `tests/unit/test_retention_worktrees.py` | h5 | 15 | 0 | 0 | 0 |
| `tests/unit/test_review130_pass.py` | h7 | 21 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_fixes.py` | h7 | 12 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_history.py` | h7 | 6 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_pr_wakes.py` | h7 | 7 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_report.py` | h7 | 1 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_requests.py` | h7 | 17 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_service.py` | h7 | 8 | 0 | 0 | 0 |
| `tests/unit/test_review_r223_conv_probes.py` | h6 | 17 | 0 | 0 | 0 |
| `tests/unit/test_review_r2_repro.py` | h7 | 31 | 0 | 0 | 0 |
| `tests/unit/test_review_r3_measure.py` | h7 | 6 | 0 | 0 | 0 |
| `tests/unit/test_review_r3_repro.py` | h7 | 12 | 0 | 0 | 0 |
| `tests/unit/test_review_r4_repro.py` | h7 | 27 | 0 | 0 | 0 |
| `tests/unit/test_review_r5_repro.py` | h7 | 17 | 0 | 0 | 0 |
| `tests/unit/test_review_r6_probes.py` | h6 | 30 | 0 | 0 | 0 |
| `tests/unit/test_review_r6_restart.py` | h7 | 5 | 0 | 0 | 0 |
| `tests/unit/test_round10_refusal_context.py` | h7 | 59 | 0 | 0 | 0 |
| `tests/unit/test_route_check.py` | h7 | 24 | 0 | 0 | 0 |
| `tests/unit/test_salvage.py` | h5 | 31 | 0 | 0 | 0 |
| `tests/unit/test_salvage_optional_transient.py` | h5 | 15 | 0 | 0 | 0 |
| `tests/unit/test_salvage_ref_race.py` | h6 | 9 | 0 | 0 | 0 |
| `tests/unit/test_salvage_unindexable.py` | h5 | 86 | 0 | 0 | 0 |
| `tests/unit/test_scheduler.py` | h7 | 97 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_priority_callers.py` | h7 | 9 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_split.py` | h5 | 3 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_tier_preferences.py` | h7 | 9 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_turns.py` | h7 | 10 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_weekly.py` | h5 | 33 | 0 | 0 | 0 |
| `tests/unit/test_scoped_stream_capacity.py` | h7 | 17 | 0 | 0 | 0 |
| `tests/unit/test_scrub_key_order.py` | h5 | 70 | 0 | 0 | 0 |
| `tests/unit/test_sessions_cli.py` | h5 | 142 | 0 | 0 | 0 |
| `tests/unit/test_sessions_handoff.py` | h5 | 117 | 0 | 0 | 0 |
| `tests/unit/test_sessions_handoff_excerpts.py` | h5 | 153 | 0 | 0 | 0 |
| `tests/unit/test_sessions_headless_properties.py` | h5 | 5 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror.py` | h5 | 41 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | h5 | 4 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_fifo.py` | h5 | 49 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_health.py` | h5 | 6 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_progress.py` | h5 | 16 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | h5 | 22 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_inventory.py` | h5 | 8 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_load_gap.py` | h5 | 85 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_safety.py` | h5 | 28 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_write_churn.py` | h5 | 3 | 0 | 0 | 0 |
| `tests/unit/test_sessions_nudge.py` | h5 | 44 | 0 | 0 | 0 |
| `tests/unit/test_sessions_registry.py` | h5 | 24 | 0 | 0 | 0 |
| `tests/unit/test_sessions_revive.py` | h5 | 45 | 0 | 0 | 0 |
| `tests/unit/test_sessions_revive_reads.py` | h5 | 10 | 0 | 0 | 0 |
| `tests/unit/test_sessions_transcripts.py` | h5 | 71 | 0 | 0 | 0 |
| `tests/unit/test_settle_properties.py` | h7 | 129 | 0 | 0 | 0 |
| `tests/unit/test_soak_tools.py` | h5 | 49 | 0 | 0 | 0 |
| `tests/unit/test_status_json.py` | h7 | 56 | 0 | 0 | 0 |
| `tests/unit/test_steer_invariants.py` | h5 | 248 | 0 | 0 | 0 |
| `tests/unit/test_stop_alarm.py` | h7 | 2 | 0 | 0 | 0 |
| `tests/unit/test_stop_watchdog.py` | h5 | 29 | 0 | 0 | 0 |
| `tests/unit/test_stopped_daemon.py` | h6 | 72 | 0 | 0 | 0 |
| `tests/unit/test_store_contention_queries.py` | h8 | 212 | 0 | 0 | 0 |
| `tests/unit/test_store_ids.py` | h8 | 27 | 0 | 0 | 0 |
| `tests/unit/test_store_migration_2.py` | h8 | 10 | 0 | 0 | 0 |
| `tests/unit/test_store_properties.py` | h5 | 3 | 0 | 0 | 0 |
| `tests/unit/test_store_readers.py` | h8 | 26 | 0 | 0 | 0 |
| `tests/unit/test_store_snapshot_plans.py` | h8 | 4 | 0 | 0 | 0 |
| `tests/unit/test_suggest_cli.py` | h5 | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggest_hint.py` | h5 | 10 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_error_boundaries.py` | h5 | 22 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_properties.py` | h5 | 1 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_secret_properties.py` | h5 | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_structural_errors.py` | h5 | 40 | 0 | 0 | 0 |
| `tests/unit/test_suggestions.py` | h5 | 30 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | h5 | 29 | 1 | 0 | 0 |
| `tests/unit/test_suggestions_r3_channels.py` | h5 | 14 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_boundaries.py` | h5 | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_multiline.py` | h5 | 6 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_response.py` | h5 | 3 | 0 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | h9 | 1 | 0 | 0 | 0 |
| `tests/unit/test_tick_prefilter.py` | h8 | 30 | 0 | 0 | 0 |
| `tests/unit/test_timers_alerts.py` | h8 | 17 | 0 | 0 | 0 |
| `tests/unit/test_timers_busy_read.py` | h8 | 115 | 0 | 0 | 0 |
| `tests/unit/test_timers_daemon.py` | h6 | 4 | 0 | 0 | 0 |
| `tests/unit/test_timers_keepalive.py` | h8 | 14 | 0 | 0 | 0 |
| `tests/unit/test_timers_probe.py` | h6 | 35 | 0 | 0 | 0 |
| `tests/unit/test_timers_reset_credits.py` | h8 | 47 | 0 | 0 | 0 |
| `tests/unit/test_timers_retention.py` | h8 | 20 | 0 | 0 | 0 |
| `tests/unit/test_title_review_regressions.py` | h5 | 4 | 0 | 0 | 0 |
| `tests/unit/test_turn_attachment_replay.py` | h8 | 13 | 0 | 0 | 0 |
| `tests/unit/test_turn_overlap.py` | h8 | 10 | 0 | 0 | 0 |
| `tests/unit/test_turn_replay.py` | h8 | 33 | 0 | 0 | 0 |
| `tests/unit/test_turn_runner.py` | h8 | 77 | 0 | 0 | 0 |
| `tests/unit/test_ultracode_effort.py` | h5 | 39 | 0 | 0 | 0 |
| `tests/unit/test_unadmittable_pin.py` | h5 | 12 | 0 | 0 | 0 |
| `tests/unit/test_wait_hub.py` | h5 | 44 | 0 | 0 | 0 |
| `tests/unit/test_wake_cli.py` | h8 | 2 | 0 | 0 | 0 |
| `tests/unit/test_weekly_rank_vs_base.py` | h8 | 14 | 12 | 0 | 0 |


## The build (2026-10-22)


Each launch allocates an exclusive `subfleet-attempt-<safe-id>-<random>` directory with mode 0700 under the user's canonical Darwin temp root. The Darwin SDK defines `_CS_DARWIN_USER_TEMP_DIR` as 65537; Python uses that numeric constant when its `confstr_names` lacks the spelling. Production does not derive this root from the daemon's inherited TMPDIR. Test daemons use isolated fixture or test-session scratch. The daemon records the path, creation time, uid, device and inode before releasing the guardian's launch gate. A marker binds each allocation to its store, preventing one store's sweep from reclaiming another store's live work. Only TMPDIR is added or replaced; no other environment variable is introduced. The same path reaches guard preflight, the guardian, provider and its children. Retries, resumes, turns, capacity probes and enrollment all use this lifecycle.

Cleanup follows finalization and verified-empty containment. Both the terminal state and the census check are required at the deletion boundary. Quarantine, unknown containment, live members, salvage errors/skipped content and an explicit `tmpdir_keep` retain scratch. The deletion check requires the canonical temp parent, exact prefix, owning uid, directory type, no symlink, and the recorded device/inode. A known allocation remains identifiable from its durable evidence if a provider removes the discovery marker; unknown allocations require their store-bound marker. Descriptor-relative `shutil.rmtree` supplies its own symlink-swap protection. Nested symlinks are unlinked without traversing their targets. Deletion errors are logged and never fail finalization.

A probe's `contained` state is intermediate: classification may still need its scratch. Only `completed` probes are reclaimed, and enrollment cleanup follows consumption of the adapter result. Cleanup measurements merge into the current evidence after the census, preserving identities and any retention hold the census recorded. Only `FileNotFoundError` counts as an absent allocation; permission failures retain it for retry.

Startup and retention's cadence sweep leftovers, including while archive retention is dormant. Known terminal attempts get a fresh census; unknown allocations from this store must be older than 24 hours. Foreign-store, live, quarantined and salvage-held folders remain, as do unmarked or unverifiable unknown folders. Known folders whose marker was removed are reconciled from durable allocation identity without weakening the census gate. Successful cleanup commits `tmpdir.removed_at`. Until then retention pins the attempt's database evidence, including if physical deletion succeeded but receipt publication was interrupted. Retention's transactional pin checks read SQL evidence only. A provider may remove its own scratch; cleanup reconciles an absent allocation after containment.

The existing provisional disk placement reservation covers anticipated worktree and TMPDIR growth together. Already allocated scratch reduces filesystem free space on that volume, so an additional reservation for those same bytes would count them twice. Remaining scratch is included in the job's measured retention bytes and recorded at final cleanup. Attended turns and probes retain their existing admission exemption. Probe scratch measurements are saved in their durable probe records. As with worktrees, a disk policy pointed at a different volume measures that configured volume; this change does not add another volume policy.

Codex's installed 0.159.0 executable was inspected without a model request, real home or real credentials. `debug prompt-input` with `sandbox_mode="workspace-write"`, dummy credentials, an isolated CODEX_HOME and a dummy provider URL emitted the private TMPDIR in its writable roots. Repeating it with `sandbox_workspace_write.exclude_tmpdir_env_var=true` removed that root. This directly confirms that Codex derives it from the child environment. No explicit Codex directory flag is needed under the default policy. An actual nested Seatbelt write probe was attempted and refused by the enclosing workspace sandbox (`sandbox-exec: sandbox_apply: Operation not permitted`); it is not claimed as a successful execution probe.

Claude's detached writable jobs use bypass permissions. Its conversation turns also support ask and acceptEdits. All writable Claude launch builders now explicitly add the private scratch directory with `--add-dir`, preserving their existing approval modes. Read-only jobs and probes keep their existing restricted tool surface. The installed Claude 2.1.284 CLI's help and local launch code were inspected with an isolated config and dummy credentials; no model request was sent.

The required invariants and their witnesses are:

| Invariant | Test witnesses |
| --- | --- |
| 1. Two attempts never share TMPDIR. | Distinct live daemon launches, colliding sanitized ids, repeated ids, and the generated lifecycle sequences. |
| 2. TMPDIR exists, is 0700 and user-owned when the provider starts. | All four daemon spawn seams check existence, mode and uid. |
| 3. Nonempty/uncertain census and quarantine prohibit deletion. | Finalization quarantine regression, deletion predicate matrix, sweep guards and generated lifecycle sequences. |
| 4. The sweep cannot remove outside-prefix folders, symlinks or live attempts. | Prefix and symlink tests, live/quarantine/salvage sweep matrix, ownership/replacement checks and generated sequences. |
| 5. Probes and conversation turns receive private scratch. | Explicit turn, capacity-probe and enrollment regressions. |

Hypothesis generates at most 40 examples of at most 35 operations (start, finalize, quarantine, sweep), with five possible attempt identities. It uses the ci profile and isolated storage. Mutation tests execute altered production code in memory and require a named lifecycle assertion to fail: shared-folder reuse, omitted census check, omitted prefix check, and symlink-following validation. Production files are not rewritten by the mutation tests.

The four mutation witnesses use these named assertions:

| Mutation | Named assertion that must fail |
| --- | --- |
| Reuse one folder | `test_attempt_names_are_unique_even_after_path_sanitizing` |
| Remove without census check | `test_cleanup_requires_terminal_empty_unretained_attempt` |
| Omit sweep prefix check | `test_sweep_requires_prefix_even_with_valid_creation_record` |
| Follow a symlink | `test_sweep_never_follows_symlink` |

The test-session root also survives fixture undo and replacement of `os.environ`. Each fixture daemon uses a sibling temporary directory, so it cannot alter tests that assert exact contents of their own `tmp_path`.

<!-- TMP1_VALIDATION -->
## Validation

The tests-first base run failed all 20 initial cases. The corrected focused hold passed **271 cases and skipped one** real enrollment guardian case because process inspection is unavailable in this sandbox. All **55 initial D-TMP1 cases passed**, including the bounded property check and all four mutation witnesses. A later marker-removal follow-up adds three cases and extends the existing retention case. The initial broader focused hold had five fixture/journal failures; its complete rerun passed after fixture isolation and journal-transition assertions were corrected.

Required `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py`: **38 passed** (37 and 1 respectively). All pytest invocations used the host suite wrapper, one pytest at a time, one worker, no pytest cache, isolated TMPDIR/Hypothesis storage, and the ci profile. Broad holds had a 19-minute execution guard; slot waits are separate from execution time.

Counts below use the latest result of each test case, avoiding double counting reruns and preserving cases completed before an interrupted resume. Partial modules count only finished cases. The complete file inventory appears below, including every unrun module. This is not a clean full-suite result.

| Directory | Passed | Skipped | Failed | Errors | Modules seen | Partial | Unrun modules |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 10070 | 15 | 9 | 0 | 245/331 | 2 | 86 |
| `tests/fake` | 174 | 1 | 0 | 0 | 9/107 | 0 | 98 |

Final post-marker-removal focused hold: 312 passed, 1 skipped. **All 58 new D-TMP1 cases passed**, including all four mutation witnesses and the marker-removal regressions. Their latest per-case results appear below.

| Hold | Passed | Skipped | Failed | Errors | Pytest seconds | Wrapper wall seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `focused` | 266 | 0 | 5 | 0 | 192.04 | 3486.22 |
| `focused-rerun` | 271 | 1 | 0 | 0 | 49.83 | 1096.75 |
| `contract` | 38 | 0 | 0 | 0 | 6.9 | 484.6 |
| `unit-01` | 3516 | 0 | 0 | 0 | 329.77 | 1508.41 |
| `unit-remaining-01` | 2859 | 9 | 5 | 0 | 645.27 | 1248.36 |
| `unit-remaining-02` | 3387 | 6 | 3 | 0 | 424.9 | 645.81 |
| `unit-remaining-03` | 249 | 0 | 1 | 0 | 1149.52 | — |
| `fake-retention-all` | 42 | 0 | 0 | 0 | 1147.09 | 1555.01 |
| `fake-focused-final` | 312 | 1 | 0 | 0 | 406.21 | 1266.0 |

`unit-remaining-03` was interrupted by its execution guard after 19m10s, with its interrupt stack in Git-index copying in unchanged salvage code; the cause of the long runtime was not established. The post-pytest measurement runner was interrupted and reaped after the wrapper exited. The first queued `unit-retention-all` request was cancelled before pytest started so retention and fake files could share one bounded hold. Neither cancellation counts as a completed validation run. The queued broad `fake-unit-finish` request was also cancelled before pytest started to prioritize the final marker-removal focused hold. Broad runs preceded that follow-up; the final focused hold rechecked all affected launch, guardian, census, retention, probe and contract files.

Observed failures in the latest per-case runs:

- `tests/unit/test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process` — PermissionError: [Errno 1] Operation not permitted: '/bin/ps'
- `tests/unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos` — assert (False) + where False = isinstance(None, int)
- `tests/unit/test_doctor.py::test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone` — AssertionError: assert ('unknown' == 'fail' - fail + unknown)
- `tests/unit/test_doctor.py::test_daemon_lock_row_passes_against_a_live_daemon` — AssertionError: assert 'unknown' == 'pass' - pass + unknown
- `tests/unit/test_hooks.py::test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs` — PermissionError: [Errno 1] Operation not permitted: '/bin/ps'
- `tests/unit/test_importer.py::test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]` — AssertionError: assert 'session-held-by-legacy-owner' == 'history' - history + session-held-by-legacy-owner
- `tests/unit/test_importer.py::test_states_map_from_rc` — AssertionError: assert 'running' == 'lost' - lost + running
- `tests/unit/test_importer.py::test_the_daemon_never_adopts_an_external_run` — AssertionError: assert ['20260905-10...0400-live/a1'] == ['20260905-100400-live/a1'] At index 0 diff: '20260905-100300-lost/a1' != '20260905-100400-live/a1' Left contains one more item: '20260905-100400-live/a1' Use -v to get more diff
- `tests/unit/test_review_pr127_service.py::test_open_is_not_queued_behind_worktree_and_diff` — TimeoutError

The native process/QoS/doctor/kernel-ceiling failures expose denied `/bin/ps`, sysctl or unavailable process identities. Importer liveness assertions also fail under conservative unknown identity; that connection is an inference until compared at the base. The one-second conversation-open timeout occurred in an unchanged test; its cause was not isolated. These failures were not masked or used to weaken production checks.

A baseline comparison of the eight process-inspection/importer failures was prepared but has not run. No baseline failure claim is made for those eight cases.

## Scratch measurement

This build lane supplies the measured workload: one Python development environment, dependency cache, recording-provider fixtures, contract checks and broad unit/fake runs. It is a build-lane sample, not a production survey. A foreground runner periodically walked its single `sf-tmp1` scratch tree without following symlinks, summing regular-file contents and `st_blocks * 512`. Pauses between walks were initially 5 seconds, then 15 seconds; walk time adds to that interval. Peaks are observed lower bounds. APFS clones can share blocks, so the block sum is not unique physical allocation. The measurement includes accumulated test fixtures and the offline baseline source copy.

Largest observed scratch sample: **1,430,910,989 bytes** of contents (1.431 GB), **1,556,500,480 bytes** by block sum (1.557 GB), during `fake-retention-all-measurement` (2 completed samples).

The configured default placement reservation is 1.5 GB (`subfleet/policy.py`). This is an anticipated-growth budget, not a directory quota. The observed block sum is a sizing input; periodic samples and shared APFS blocks prevent treating it as a guaranteed upper bound.

| Component / observation | Contents bytes | Block-sum bytes |
| --- | ---: | ---: |
| Initial development venv | 13,726,189 | 15,601,664 |
| Initial uv cache | 10,223,025 | 12,025,856 |
| Initial whole scratch | 35,334,717 | 40,517,632 |
| `focused-measurement` observed peak | 106,911,804 | 120,532,992 |
| `focused-rerun-measurement` observed peak | 178,336,166 | 200,171,520 |
| `contract-measurement` observed peak | 197,060,030 | 220,450,816 |
| `unit-01-measurement` observed peak | 446,182,642 | 489,115,648 |
| `fake-focused-final-measurement` observed peak | 476,708,882 | 547,033,088 |
| `unit-remaining-01-measurement` observed peak | 885,011,420 | 948,162,560 |
| `unit-remaining-02-measurement` observed peak | 1,354,000,544 | 1,473,609,728 |
| `fake-retention-all-measurement` observed peak | 1,430,910,989 | 1,556,500,480 |

The largest observed block sum exceeds the default 1.5 GB reservation by 56,500,480 bytes. The sample is a sizing input, not an upper bound. One provisional reservation covers anticipated combined growth; already allocated scratch reduces filesystem free space. Remaining scratch is counted in retention measurement; the existing configured volume/free-space policy is unchanged.

## Delivery and constraints

Setup deviation: the required uv setup sync downloaded dependencies before offline mode was enabled, contrary to the no-network rule. The default cache was unwritable, so setup used the permitted scratch cache. Subsequent uv invocations set `UV_OFFLINE=1`. This checkout was not fetched and no branch was pushed.

Commits use workspace-local `.git-tmp1` metadata because the shared Git index is unwritable. The implementation and regression steps, oldest first:

No further hold requests were started after 12:10 UTC. The existing final focused hold began at 12:09:48 UTC and finished with 312 passed and 1 skipped; all 58 new D-TMP1 cases passed. The controller stopped before requesting the prepared baseline comparison. Whole-directory validation remains incomplete: 184 modules are unrun and two are partial. Required pgrep returned 3 because global process listing is unavailable here; all recorded runner, wrapper and pytest PIDs were absent after completion. The earlier housekeeping pass completed and was reaped.

- `5753ebaf2` test: specify private attempt TMPDIR lifecycle (D-TMP1)
- `50f17eaa4` test: require Claude writable modes to include attempt scratch
- `8de7d98b8` fix: isolate provider attempt temporary storage (D-TMP1)
- `6a9c10c95` test: keep attempt scratch isolated after fixture undo
- `c87dc5672` test: pin temporary root baseline for the pytest session
- `4a69cda20` test: retain scratch when allocation inspection is denied
- `bfeb3f623` fix: distinguish missing scratch from failed inspection
- `07e6b5a5b` test: preserve census evidence and retention changes during cleanup
- `2b676e67b` fix: merge scratch measurements with current census evidence
- `3e2b73724` test: retain probe scratch until classification is finalized
- `c3807c604` fix: reclaim probe scratch only after finalization completes
- `0a8de118f` test: isolate attempt scratch beside per-test working directories
- `dea5c9e02` test: check probe transitions across scratch evidence records
- `80d8a62a7` test: reclaim evidenced scratch after its marker is removed
- `724b12cfd` fix: trust durable allocation identity after marker removal
- `eccc6d7d3` docs: record TMPDIR design and validation cutoff status

The final report/PR-body commit follows these steps. No push was performed. Scratch logs, JSON evidence, archives and bundles are not staged.

Completion check (2026-10-22 12:26 UTC): the prescribed scratch cleanup finished, and `sf-tmp1` is absent. Thirteen generated workspace cache directories were moved into scratch before removal; no `__pycache__`, `.pytest_cache` or `.hypothesis` remains in the worktree. The final required pgrep check returned 3; process listing is unavailable in this sandbox. All previously recorded runner, wrapper and pytest PIDs were absent, and every foreground command was reaped.

## Per-file results

| File | Passed | Skipped | Failed | Errors | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | Run |
| `tests/fake/test_admission_held_waiters.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_latency.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_lease_waiters.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_liveness.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_priority.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_ramp.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_ramp_retention.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_route_isolation.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_stateful.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_admission_visibility.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_attempt_tmpdir_daemon.py` | 15 | 0 | 0 | 0 | Run |
| `tests/fake/test_batch_label.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix2.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix3.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix4.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_canonical_identity_fix5.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_capacity_steer_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_capacity_wait_backoff.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_census_recycled_pid_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_conversation_pools.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_conversation_wakes_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_descriptors.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_launch.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_daemon_stacks.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_detached_cwd_pins.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_detached_retention_fence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_enrollment_guardian.py` | 0 | 1 | 0 | 0 | Run |
| `tests/fake/test_fake_provider.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_finalization_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_folder_spelling.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_frozen_stream_nonregular.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_gate_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_identity_rollback.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_interactive_claude_titles.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_interactive_steer.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lane_fault.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lane_pilot.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_enroll.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_reenroll.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lanes_reenroll_keychain.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 | Run |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | Run |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_main_refusal_write_dir.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_mcp_job_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_native_maintenance_startup.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_notice_delivery.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_notice_header.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_picker_native.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_policy_sandbox.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | Run |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | Run |
| `tests/fake/test_probe_turn.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_prompt_seam.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_active_cap_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_cap_review_evidence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_record_secrecy.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_request_routing.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reserve_authorization.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reserve_authorization_validation.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_reset_credit_incident.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_resume_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | Run |
| `tests/fake/test_review130_ab.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_review_r185_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_route_view.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_routing_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_routing_tier_preferences.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_salvage_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_salvage_finalization.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_sessions_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | Run |
| `tests/fake/test_state_fixture_processes.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_status_payload.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_suggestions_fake.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_tier_hold_by_demand.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_timers_busy_admission.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_timers_end_to_end.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_listing.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_cwd.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_cwd_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_job_directory.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_job_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_retention_quarantine.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_trees.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_turn_wait_reasons.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_admission.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_rereview.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_unadmittable_pin_review.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_enospc.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_workspace_transient.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_writable_fanout.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/fake/test_writer_retention_fence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_guardian.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_lifecycle.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_mutations.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_retention.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_attempt_tmpdir_sandboxes.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | Run |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | Run |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli.py` | 222 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | Run |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | Run |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | Run |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 0 | 1 | 0 | Run |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 | Run |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 | 0 | 0 | Run |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | Run |
| `tests/unit/test_descriptors.py` | 15 | 0 | 1 | 0 | Run |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_doctor.py` | 42 | 0 | 2 | 0 | Run |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | Run |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | Run |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_failed_keep.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 3 | 0 | 0 | Run |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | Run |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | Run |
| `tests/unit/test_guardian.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | Run |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_hook_cost.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_hooks.py` | 66 | 0 | 1 | 0 | Run |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push.py` | 118 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_build.py` | 10 | 1 | 0 | 0 | Run |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_importer.py` | 145 | 0 | 3 | 0 | Run |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round3.py` | 67 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 | Run |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_legacy_hold.py` | 50 | 6 | 0 | 0 | Run |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | Run |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | Run |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | Run |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | Run |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | Run |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | Run |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy.py` | 178 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | Run |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 | Run |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | Run |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | Run |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | Run |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | Run |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | Run |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | Run |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | Run |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | Run |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | Run |
| `tests/unit/test_retention_archive.py` | 4 | 0 | 0 | 0 | Partial; interrupted |
| `tests/unit/test_retention_archive_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_case_alias.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_nested_leases.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_qos.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_readback.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_rediscovery.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_regenerable.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_regenerable_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_salvage_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_shared_folders.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_sweep_race.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turn_contract.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turn_workdirs.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_turns.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | Run |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_pr127_service.py` | 7 | 0 | 1 | 0 | Run |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | Run |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | Run |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | Run |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | Run |
| `tests/unit/test_salvage.py` | 6 | 0 | 0 | 0 | Partial; interrupted |
| `tests/unit/test_salvage_optional_transient.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_salvage_unindexable.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_priority_callers.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_split.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_tier_preferences.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_turns.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scheduler_weekly.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scoped_stream_capacity.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_scrub_key_order.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_handoff.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_handoff_excerpts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_headless_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_fifo.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_health.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_inventory.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_load_gap.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_safety.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_mirror_write_churn.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_nudge.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_registry.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_revive.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_revive_reads.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_sessions_transcripts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_settle_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_soak_tools.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_status_json.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_steer_invariants.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_stop_alarm.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_stop_watchdog.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_contention_queries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_ids.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_migration_2.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_readers.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_store_snapshot_plans.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggest_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggest_hint.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_error_boundaries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_secret_properties.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestion_structural_errors.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_r3_channels.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_boundaries.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_multiline.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_suggestions_review_response.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | Run |
| `tests/unit/test_tick_prefilter.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_alerts.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_busy_read.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_daemon.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_keepalive.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_probe.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_reset_credits.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_timers_retention.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_title_review_regressions.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_attachment_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_overlap.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_replay.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_turn_runner.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_ultracode_effort.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_unadmittable_pin.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_wait_hub.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_wake_cli.py` | 0 | 0 | 0 | 0 | Unrun |
| `tests/unit/test_weekly_rank_vs_base.py` | 0 | 0 | 0 | 0 | Unrun |
