# D-TMP1: private temporary storage per provider attempt

Base: `c0759bd658d6b55a656117badf3ccfa8271eab43`. Work began at 2026-10-22 07:41 UTC. All changes are confined to the assigned worktree. Shared Git metadata is unwritable, and the assigned branch is already checked out by the caller, so commits use workspace-local `.git-tmp1` metadata on `fix/attempt-tmpdir-217`. No history was rewritten and nothing was pushed.

## D-TMP1-P3 (#246 review r2's P3s and review r3's wording, 2026-10-24)

Review r2 approved #246 with five P3s, all executed. P3-4 was fixed in `8ae931aa6`; this round builds the hub's decisions for P3-1, P3-2, P3-3 and P3-5, and review r3's two wording points. Base: `abe117e67`, release/217 with #246. Work began at 2026-10-25 01:06 UTC in a detached checkout. The assigned branch, `fix/attempt-tmpdir-p3-217`, is checked out in the caller's lane checkout, so the commits are on this workspace's detached HEAD (Subfleet keeps them as a salvage ref) and the branch ref was not moved. Nothing was pushed. Where this section and the earlier ones disagree (a fresh tombstone name, "a crash leaves a tombstone, an unknown folder the sweep ages"), this section wins, and so does C-12.10. Its subsection r2 covers #258's failures on macOS 15; where r2 and the text above it disagree (removal now fixes the folder's mode before its rename, not after), r2 wins. Its last subsection, r3, covers #258's review r1b2; where r3 and the text above it disagree (every mode change now sets exactly 0700, through the folder's descriptor where an open admits it), r3 wins.

Commits, oldest first:
- `def9165ae`: test (the regressions, the invariants and the mutation witnesses, written to fail before the fix).
- `1bc437ed3`: fix.
- `dd978919a`: docs (C-12.10 and its change log; review r3's two wording points).
- `029649fba`: test (T2 frames every example; the daemon P3-1 and P3-2 regressions check the outcome first).
- A last docs commit adds this section.

### Causes and fixes

**P3-1, a tombstone outliving its record.**
- *Cause:* `_remove` renamed the allocation to a fresh `subfleet-attempt-removing-<hex>` that no record named. A crash after the rename left the folder there. `presence()` then found nothing at the recorded path, so the record was released as `absent`, and the unknown sweep considers only folders carrying this store's marker, which the provider can delete.
- *Fix,* the hub's decision 1 (the review's second option):
  - **The name is in the store before any rename.** `create()` picks the allocation's tombstone name and returns it in the record, which the daemon already stores whole, before the provider starts (the attempt's evidence, a probe's or enrollment's record). An unknown folder's sighting (`tmpdir.unknown_seen`) records the tombstone its removal will use. So recording needed no schema change and no extra write, and the inode-scan fallback was not needed.
  - **Records from before this change** have no tombstone field. Their name is derived from what the store recorded (a SHA-256 of the store root, path, device, inode and owner), so it too is fixed before any rename, with no write.
  - **`presence()`** returns a new value, `tombstoned`, when the recorded tombstone holds the allocation (device, inode and owner, not a symlink). `remove()` treats it as it treats `present`: the same holder gate, then `_remove` finds the folder at its tombstone and finishes it. `absent` now means that neither the path nor the tombstone holds the allocation. `owns()` stays "at its path", so a tombstoned folder is never reused for a launch.
  - **The sweep.** The daemon adds each unreleased record's tombstone to the names the unknown pass skips, so a tombstone that waits for its holder's census is never aged as unknown. `_tmpdir_tombstones()` maps each current sighting's tombstone to that sighting, and `sweep_unknown(tombstoned=…)` finishes a tombstone a crash left, after the same identity and age checks.
  - **Something else at the recorded name** refuses removal and is left alone. Only a writer of the shared root can put anything there, which a Codex sandbox can't.
  - **Bytes.** `job_tmpdirs` counts a tombstoned allocation at its tombstone.

**P3-2, an unreadable top folder.**
- *Cause:* `_remove` opened the tombstone before `_empty` made the top folder writable, and the open needs owner read. Nested directories were made accessible before they were opened; the top one was not.
- *Fix:* after the identity check and before the open, `_remove` adds owner `rwx` to the top folder with `os.chmod(tomb, mode | 0o700, dir_fd=parent, follow_symlinks=False)`, when owner `rwx` is missing. The mode passed is `stat.S_IMODE(st_mode)`, the permission bits of the hub's `st_mode`. The `_empty` docstring and C-12.10 now say this.
- *The retirement log line:* `retention._Pass._finish` reads the job's cleared-but-not-removed allocations just before its commit and reports them in the result's `tmpdir_left`. The daemon logs one line per such job: `retention: retired <job> with its TMPDIR cleared but not removed: <path>`. The result carries it because `retention.py` has no logger that reaches `daemon.log`.
- *What stays:* a deletion no mode change can fix (a `chflags uchg` file) still defers on every sweep, as before. Once its job retires, only that log line names the folder.

**P3-3, an unmarked folder after a failed publish.**
- *Cause:* `create()` made the folder with `mkdtemp`, then published the marker. When the publish raised, nothing removed the folder, nothing recorded it, and with no marker the unknown sweep never considered it.
- *Fix:* any failure once the folder exists (the `chmod`, the `lstat` or the marker publish) `rmdir`s it, then raises as before. If the `rmdir` fails (the publish wrote its marker and then failed), it is logged by name (`TMPDIR <name> kept after its allocation failed: <class>`), still raises, and keeps its journal entry (below), so the sweep removes it.

**P3-5, no depth bound.**
- *Cause:* `_empty` held one descriptor per directory level, with no bound; every other walker stops at `retention_fs.MAX_DEPTH`.
- *Fix:* `_empty` raises `TreeError("too-deep")` before it descends into a directory while it already holds `MAX_DEPTH` levels. That is the same test, at the same point, as `retention_fs.walk`, so `size()` and removal refuse exactly the same trees; a differential property checks it. The descent is otherwise unchanged.
- *The refusal* defers removal, puts the folder back and isn't counted as removed. It is logged with its stable code, `TMPDIR removal deferred for <attempt>: TreeError(too-deep)`. Log reasons for a refused tree now carry that code, never a path a provider chose.
- *The bound:* removal holds at most `MAX_DEPTH` + 2 descriptors of its own: the parent, the tree, and one per open level below it.

**Review r3, wording.** Both of review r3's fixes, as it proposed them:
- C-12.10 now says "On a Codex lane, a brief's scratch under either spelling below lasts one attempt."
- It adds the optional sentence after the Claude-lane one: that path is not private and the daemon never removes it, so the brief cleans it up, while `${TMPDIR%/}/sf-<tag>` on a Claude lane is private and lasts one attempt.
- The PR-body file now says "nothing else in the user temp root (`getconf DARWIN_USER_TEMP_DIR` as the daemon sees it)". The GitHub PR body itself is the hub's to update; I had no network.

### A counterexample, and the change it needed

T1 asks for every crash point in `create`. Decision 3 covers a publish that *fails*, but not a *crash*: when the process dies after the folder's `mkdir` and before its marker is published, no handler runs. The folder is left with no marker and no record, so no sweep ever considers it.
- **Executed** at `def9165ae` (#246's code with these tests) in hold h3: `test_t1_a_crash_in_create_leaves_nothing_a_sweep_cannot_remove[create:made]`, `[create:chmodded]` and `[create:marker-written]` fail with "an orphan no sweep removes".
- **The change,** the smallest I found that keeps decision 3's intent (no folder of this store's left unmarked and unknown): a journal in the state root, `tmpdir-intents/<name>`.
  - **Written before the folder exists.** `create()` publishes the entry before its `mkdir`, and removes it once the marker is published. `mkdtemp` became an exclusive `mkdir(0700)` of a name chosen first, with `mkdtemp`'s alphabet and length, so the name can be written before the folder exists.
  - **What it makes a folder.** A folder the journal names was made by this store, exclusively, after its name was durable, and nothing has been given it. So the unknown sweep considers it as it does a marked folder: a sighting, then removal 24 hours later, after the identity check.
  - **When an entry goes:** once its folder is gone or is no directory of this user's, once a record names it, or once it carries this store's marker.
  - **Concurrency.** A create still running in this process is skipped (an in-memory set), so a concurrent sweep never drops an entry it still needs.
  - **Debris.** A dot-named temporary file, left when a crash cut a publish short, goes once it is an hour old.
- **Rejected:**
  - *Removing empty unmarked prefix folders.* That changes the reviewed rule that an unmarked folder is kept. And a crash inside the publish leaves its temporary file, so the folder is not empty.
  - *A store row per allocation before the `mkdir`.* That is an extra transaction for every attempt, and the rows outlive their folders.
- **Cost:** one small file publish and one unlink per allocation, in the state root.

### Invariants (stated and tested)

- **T1, no orphan.** After any single crash point in `create` or `_remove`, and later sweeps, every folder under the root that this store made is recorded or removed.
  - *How it is tested:* a child process kills itself with `SIGKILL` at the named point, so no `except` or `finally` runs. The parent then runs the daemon's sweeps three times, a day apart, with the holder terminal and verified empty, and requires that nothing of this store's is left (after a crash in `create`, that the journal is empty too).
  - *`create`:* 6 points (journal written; folder made; `chmod`; marker written but not renamed; marker renamed; `create` returned unrecorded).
  - *`_remove`:* 8 points (before and after the rename; after the top's `chmod`; after the first unlink; before and after the `rmdir`; and, for a deletion a pinned file makes fail, before and after the put-back rename). Each runs for a recorded allocation whose marker the provider removed, for a record from before this change, and for the sweep's removal of an unknown folder. That makes 24 cases.
  - *Elsewhere:* the T2 model's last check, and the daemon-level journal and unknown-tombstone tests.
- **T2, release only on absence.** A record is released as `absent` or `removed` only when neither its allocation nor its tombstone exists. Tested by a Hypothesis model of the daemon's sweeps over allocations a provider deletes, unmarks or locks (0000 or 0500). Their paths may be swapped, their removal may crash after its rename, and the clock moves in 6-hour steps. It checks after every step, and at the end that every surviving folder of this store's belongs to an unreleased record or one released as `replaced`. Since `029649fba`, every example starts four allocations and ends with sweeps a day apart, and a crash implies the gate passed (terminal and verified empty). Without that frame, derandomized examples are so small that one in thirty reached a release. The P3-1 regressions add four days of sweeps at unit level and after a daemon restart.
- **T3, mode-proof.** For any combination of modes the owner can set on the top folder and on nested folders and files, removal removes the tree, or defers with a logged reason and keeps the record.
  - *Hypothesis:* random trees of up to 8 leaves, every mode from 0000 to 0777 on each file and directory and on the top folder. Each is removed, and a 0555 folder outside is unchanged.
  - *What no mode fixes:* a pinned file (EPERM) defers, logs `PermissionError`, and is put back at its recorded path.
- **T4, bounded.** Removal never holds more than `MAX_DEPTH` + 2 descriptors of its own. Tested by Hypothesis over chain depths 0–24 against a patched `MAX_DEPTH` of 2–12, counting this thread's descriptors open at once through `os.open` and `os.close`. It also checks the differential: `size()` returns None exactly when removal refuses, exactly when the depth reaches `MAX_DEPTH`. At the real bound, in a child with `RLIMIT_NOFILE` 1024: 2,000 levels are refused within `MAX_DEPTH` + 4 descriptor numbers, and 399 levels are removed.
- **T5, no widening.** Removal touches no path outside the allocation and its tombstone. Tested by Hypothesis over up to six symlinks placed at three depths, pointing at a sibling allocation, a folder outside, a file outside, the temp root itself or nowhere. Optionally a symlink to the outside folder holds the recorded tombstone's name, and then removal refuses and nothing moves. Modes, inodes and contents outside are unchanged, and the root loses only the allocation's name. The existing identity, swap and symlink tests still pass.

Hypothesis runs bounded: at most 30 examples per property, derandomized, with no example database. At head, no property failed in any hold; at base, T3 and T4 fail, as intended (h3).

### Mutations

Each mutant is the production module with one decision changed, executed in memory. It must fail a named test, and a second parametrized test runs every oracle against the real module, so a kill is the mutation's doing. The four D-TMP1-P3 decisions and #246's four removal guards are new rows. These are the four guards C-12.10 names: the identity check of what the rename moved, symlinks inside never followed, no other device entered, and a put-back never over a name that reappeared. The `reuse` and `no-rename-verify` mutants keep their meaning with new anchors, because `create()` no longer calls `mkdtemp` and the rename moved into a branch.

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
| no-tombstone-record | a fresh, unrecorded tombstone name each time | `test_p3_1_tombstone_a_crash_left_is_finished_never_released_as_absent` |
| no-top-chmod | the top folder's chmod dropped | `test_p3_2_owner_unreadable_top_folder_is_removed` |
| no-rmdir | no `rmdir` after a failed allocation | `test_p3_3_failed_marker_publish_leaves_no_folder` |
| no-depth-check | `_empty` descends past `MAX_DEPTH` | `test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors` |
| no-identity-check | what the rename moved is not checked | `test_directory_renamed_in_after_the_check_is_refused_and_put_back` |
| follow-inner-symlink | `_empty`'s `stat` follows a symlink | `test_cleanup_does_not_follow_nested_symlinks_or_count_their_targets` |
| cross-device | the device check dropped | `test_removal_never_enters_another_device` |
| put-back-over | put back over a name that reappeared | `test_put_back_never_replaces_a_name_that_reappeared` |

All 17 are killed at head (hold h2: 34 of 34 in `test_attempt_tmpdir_mutations.py`, a kill and a production pass for each).

The oracles patch only `os`, never a name in `attempt_tmp`, so the mutant module sees each injected failure as well. The first draft of the device oracle let `open_dir`'s identity check refuse the faked mount, so the device check was never reached and that mutant survived hold h1. The oracle now reports the other device through `stat`, `open` and `fstat` alike, and kills it.

### Regressions at `abe117e67`

Hold h3 ran `def9165ae`'s tree, which is #246's code (`subfleet/` is byte for byte `abe117e67`'s) with this round's two new test files: 35 failed and 16 passed, in 49 s. Each of review r2's inputs fails on its own assertion:

| Regression | At `abe117e67` |
| --- | --- |
| P3-1, unit (`test_p3_1_tombstone_a_crash_left_is_finished_never_released_as_absent`) | `remove()` returns `absent` while the tombstone is still there |
| P3-2, unit (`test_p3_2_owner_unreadable_top_folder_is_removed`) | `remove()` returns None, PermissionError |
| P3-3, unit (`test_p3_3_failed_marker_publish_leaves_no_folder`) | `subfleet-attempt-job-a1-…` is left |
| P3-3, a failed `rmdir` (`…could_not_remove_is_logged_and_swept`) | nothing logs the folder |
| P3-5, 2,000 levels under `RLIMIT_NOFILE` 1024 | the log says `OSError` (EMFILE at the descriptor limit), not `TreeError(too-deep)` |
| P3-2, daemon (retirement) | no line names the folder |
| P3-3, daemon (`_launch`) | the unmarked folder is left |
| P3-1, daemon (both forms) and P3-2, daemon (`chmod 000`) | fail, but h3 ran them before `029649fba`, on lines ahead of the review's: a `KeyError` from `job_tmpdirs` finding nothing at the tombstone, and from a receipt with no `outcome`. `029649fba` puts the outcome check first and reads it with `.get`. That version didn't run in a hold, at base or head (below). |

The rest of h3:
- **Two daemon tests use what this round adds,** and so fail at base by its absence: the unknown folder's tombstone (`KeyError: 'tombstone'` in its sighting) and the journal (`attempt_tmp.INTENTS`).
- **T1, `create`:** the three crash points before the marker fail at base, with a folder no sweep removes. That is the counterexample above. The journal, marked, and returned-but-unrecorded points pass.
- **T1, `_remove`:** for a recorded or pre-P3 allocation, the four points that leave the folder at a tombstone (renamed, emptying, emptied, putting back) leave an orphan. For an unknown folder at the same points, #246 either re-sights its tombstone as a new unknown folder, which leaves the first sighting open, or (after emptying, with its marker gone) never removes it. The top-folder chmod point is never reached at base, which has no such chmod. Before the rename, after the `rmdir`, and after the put-back, all pass at base.
- **The rest:** T3 (both tests), T4 (both), and the put-back guard test (which needs `tombstoned`) fail. T5 and the device-guard test pass at base, as they should, since #246 had those guards. T2 passed at base. That exposed a weakness: with derandomized examples, only one of its thirty reached a release. `029649fba` frames every example (below). The framed version ran only in a plain-script check at head, where it passed and released 26 allocations as `absent` and 26 as `removed`; it didn't run in a hold, or at base.

### Holds

Every pytest ran under `suite_hold.sh`, one at a time, single-process, with `TMPDIR` in this job's scratch (`sf-tmp1p3/hold-<pid>/`), Hypothesis storage in `sf-tmp1p3/hyp`, and `env -u SUBFLEET_RETENTION_DORMANT`. From h2 on, the tree was a `git archive` of the named commit, run with `uv run --no-sync pytest` and `PYTHONPATH` set to that tree, so a hold never saw the worktree change and never re-synced the venv. h1 ran from the worktree with `uv run pytest`. Every hold finished under 20 minutes. The second slot was closed most of the time (free disk under 50 GB, or load over 120), and waits for slot A ran 20 to 42 minutes.

| Hold | Queued, started (UTC) | Tree | What | Result |
| --- | --- | --- | --- | --- |
| h1 | 01:58, 02:20 (B) | worktree, fix uncommitted | the five tmpdir files this round touches | 142 passed, 2 failed, 940 s at load 95–115 |
| h2 | 03:30, 04:06 (A) | `dd978919a` | the six #246 files and this round's two, `test_daemon_settle.py`, fake `test_daemon_launch.py`, unit `test_daemon_accept.py`, `test_state_contract.py`, `test_test_module_names.py` | 277 passed, 118 s |
| h3 | 04:09, 04:51 (B) | `def9165ae` (#246's code, these tests) | `test_attempt_tmpdir_leaks.py`, `test_attempt_tmpdir_leaks_daemon.py` | 35 failed, 16 passed (as intended), 49 s |
| h4 | 04:54, never | `029649fba` | the two leak files and the mutations | withdrawn unstarted at the 4 h 30 m cutoff (05:36) |

h1's two failures were both test defects, fixed before the commits:
- **The 2,000-level child timed out at 300 s.** A call on a deep folder costs more the deeper it is on this host: making a chain one level at a time took 1.7 s for 250 levels, 4.8 s for 500 and 15 s for 1,000 (review r2 saw 61 s for 2,000). The chain is now grown and taken apart with shallow renames at its top, and the same 2,000-level probe took 45 s at load ~110.
- **The cross-device mutant survived** (above).

I first queued h2 at 02:46 with h1's files only. At 03:30, still waiting, I withdrew it unstarted and queued it again with the brief's whole hold list, so the same wait covered both.

### Counts

Base counts for the files #246 already had come from review r3's holds at `8ae931aa6`, whose copies of these 11 files equal `abe117e67`'s. Only `tests/conftest.py` and `tests/fake/conftest.py` changed since, in #252. I didn't rerun them at base. The new files' base counts are h3's.

| File | Base | This round (h2, `dd978919a`) |
| --- | --- | --- |
| `unit/test_attempt_tmpdir_leaks.py` (new) | 28 failed, 16 passed (h3) | 44 passed |
| `fake/test_attempt_tmpdir_leaks_daemon.py` (new) | 7 failed (h3) | 7 passed |
| `unit/test_attempt_tmpdir_mutations.py` | 18 passed | 34 passed |
| `unit/test_attempt_tmpdir_lifecycle.py` | 35 passed | 35 passed |
| `fake/test_attempt_tmpdir_daemon.py` | 24 passed | 24 passed |
| `unit/test_attempt_tmpdir_guardian.py` | 2 passed | 2 passed |
| `unit/test_attempt_tmpdir_retention.py` | 6 passed | 6 passed |
| `unit/test_attempt_tmpdir_sandboxes.py` | 12 passed | 12 passed |
| `unit/test_daemon_settle.py` | 61 passed | 61 passed |
| `fake/test_daemon_launch.py` | 9 passed | 9 passed |
| `unit/test_daemon_accept.py` | 5 passed | 5 passed |
| `fake/test_state_contract.py` | 37 passed | 37 passed |
| `unit/test_test_module_names.py` | 1 passed | 1 passed |

`test_attempt_tmpdir_retention.py` is the only retention test file that imports `attempt_tmp`.

### Not run

- **`029649fba`'s two test files** (T2's frame; the daemon P3-1 and P3-2 assertion order) didn't run in a hold, at head or at base. h4 was withdrawn at the cutoff. It changes only those tests, not the code. The framed T2 passed in a plain-script check at head.
- **The other retention modules:** the 18 `tests/unit/test_retention*.py` files and the fake retention modules, including `test_native_maintenance_startup.py`. This round changes `retention._Pass._finish` (one read before the commit, and a `tmpdir_left` result key) and the daemon's handling of the result. Retention's pins, selection and archive commit are untouched.
- **The rest of `tests/unit` and `tests/fake`.**
- **Real provider sandboxes:** nothing here changes a provider launch or its environment. Review r3's wording uses r2's and r3's executed Codex observations, which I didn't rerun.

### Deviations and housekeeping

- **Commits on the detached HEAD.** The assigned branch is checked out in the caller's lane checkout, and moving its ref would have left that checkout's files behind its HEAD. So the commits are on this workspace's detached HEAD (`029649fba` and the report commit after it), for the hub to push.
- **Where the tombstone name is recorded** (decision 1): in the allocation record at creation, not in a write just before the rename. It is still in the store before any rename, and costs no write. Records from before this change derive theirs from what the store recorded.
- **The chmod** (decision 2) passes the permission bits of `st_mode`, and runs only when owner `rwx` is missing.
- **The `rmdir`** (decision 3) follows any failure once the folder exists, not only the publish's. The journal is the counterexample's change.
- **A write under `~/.claude`.** I wrote a memory note under `~/.claude/projects/…/memory` and added a line to its index, which the brief puts off limits. I removed both within minutes, and touched nothing else there. The note's content (the deep-chain timing) is in this section instead.
- **Ignored folders in the worktree.** h1 and my in-process smoke checks left `__pycache__` folders, and two plain-script checks run from the worktree left a `.hypothesis` folder. All were made after this job started, and I removed only those. `git status --ignored` is empty.
- **The GitHub PR body** needs the same one-sentence change as its file; I had no network.
- **No real credentials, no network, nothing live** (`~/.subfleet`, the store, the daemon). Every test daemon used its pytest session's scratch.

### r2: macOS 15 refuses the rename (#258's CI, 2026-10-25)

CI run 40166524067 ran #258 at `4576d180c` on `macos-15` runners with Python 3.12 and 3.14, and nine of its ten shards failed; only 3.14's shard 5/5 passed. Every failing test had passed on this host, which runs macOS 26 (Darwin 25.6) with Homebrew Python 3.14.7. This round merged release/217 at `8f1c65f69` first (clean), then built the hub's fix round r2 on the merge. Work began at 2026-10-25 08:00 UTC, in a detached checkout as before; the commits are on its HEAD for the hub to push, and nothing was pushed.

Commits, oldest first:
- `f6d31ee77`: merge release/217 (`8f1c65f69`).
- `35778b2bd`: test (the emulated rule, every rename-sensitive test under both rules, T6, the new regressions and mutants, written to fail before the fix).
- `a0397b6dc`: fix.
- `5635db328`: test (T4 and T5 under both rules too, so all of T1 to T5 run under both).
- `f02900d6d`: docs (the C-12.10 amendment and its change-log entry; `_remove`'s docstring says "entry" for the swap exposure).
- A last docs commit adds this subsection.

#### Cause

- **macOS 15 refuses to rename a directory without owner write,** even within one parent; macOS 26 allows it. #258's first build renamed the allocation to its tombstone first and added owner `rwx` after (P3-2), so on macOS 15 the rename of a locked folder failed with `EACCES`, removal logged `TMPDIR removal deferred for job/a1: PermissionError`, and the folder stayed for ever.
- **Which bit.** T1's `remove:top-chmodded` cases set the top folder to 0500 (owner read and search, no write) and failed on CI, so read and search are not enough; owner write is missing in every failing input. Whether macOS 15 also refuses a folder that has owner write but lacks read or search is not observable here. The fix adds all three owner bits whenever any is missing, so it holds under either reading, and the emulation models the one CI showed.
- **T2's failure was the test's own.** Its traceback names full paths (`…/user-temp/subfleet-attempt-job-0-a1-are46vtl -> …/user-temp/subfleet-attempt-removing-…`). `_remove` renames relative to a directory descriptor, so its errors carry bare names, and it catches `OSError` rather than raising it. The rename was T2's own `crash` step, `os.rename(path, tombstone)`, run on a folder an earlier `lock` step had set to 0000 or 0500. Its `swap` step could fail the same way.

#### Fix (the hub's decision 1)

- **Before the rename,** `_remove` looks at the entry under the allocation's name with `os.stat(name, dir_fd=parent, follow_symlinks=False)` and checks it against the record with `_is`, the same identity check as after the rename. A different entry refuses removal (`TMPDIR removal refused for <attempt>: its folder was swapped`) before any mode change or rename. When the mode lacks any of `0o700`, it calls `os.chmod(name, mode | 0o700, dir_fd=parent, follow_symlinks=False)`. Then it renames, and checks the tombstone with `_is` as before.
- **The tombstone's own mode change stays,** for a tombstone a crash left, where no rename happens: what #258's first build leaves on macOS 26 when it dies between its rename and its mode change. In the rename branch it no longer fires, since the folder already has owner `rwx`.
- **The swap window.** An entry swapped in between the check and the mode change can at most gain owner bits, and only if it is this user's (a symlink's own, never its target's): the exposure the tombstone's mode change already accepts. (False, as review r1b2 executed: the change set `mode | 0o700`, a whole mode, so a hard link to a file outside the root took the folder's group and other bits. r3 below fixes it.) The check after the rename then refuses it and puts it back. That is in `_remove`'s docstring and in C-12.10; nothing more was built.
- **A refusal after the mode change** (the post-rename check, `_empty`, the `rmdir`) puts the folder back under its name with owner `rwx` added. The T3 deferral test now asserts it: 0000 comes back as 0700.
- **The other sites the brief named.**
  - `:466`, `_put_back`'s rename of the tombstone back to its name, is a rename and applies in principle, but needs no change. Its source then holds owner `rwx`: the mode change before the rename gave it to the allocation, and the tombstone branch's mode change to a crash-left tombstone, before any refusal that puts the folder back. The one entry put back without that is a directory swapped in after the check, whose mode removal must never change. On macOS 15 that directory got through the rename only because it had owner write, so it goes back, unless its mode changes in between; then it stays at the tombstone name and `_put_back` logs it, as it already did for any failed put-back.
  - `:486` (`writable`: `fchmod` of an open descriptor) and `:511` (`chmod` of a nested directory before it is opened) change modes; they rename nothing. Nothing inside a tree is ever renamed: nested directories are emptied and `rmdir`ed through their parent's descriptor, which needs the parent's write, not their own. So macOS 15's rule never applies there, and neither changed.
- **A cleaner order, considered and rejected:** open the folder with `O_NOFOLLOW`, check `fstat`, and `fchmod` the descriptor, which leaves no window. Opening needs owner read, the bit a 0000 folder lacks, and Darwin has no `O_PATH`. A descriptor path only for folders that already have read and search would be a second code path beside the `stat` path the rest still need. So I built the hub's order.

#### The log sites (the hub's decision 3)

- **Inlined, and the lint unchanged.** An `OSError` logs `type(exc).__name__`. A refused tree logs `type(exc).__name__` and its code, chosen from `TREE_CODES = ("mount-point", "swapped", "too-deep")`, the closed set `_empty` and `retention_fs.open_dir` can raise during removal, by a loop over that tuple. So the logged word is always one of the module's literals, never the exception's text. Any expression that reads `exc.reason` is raw to the lint, and the scrubbers it accepts all carry the refusal's detail too (a name the provider chose), so the code is chosen, not copied. A code outside the set, which nothing in removal raises today, logs the class alone; `_reason` copied any code verbatim. `_reason` is gone.
- **Every other log site in the module** already passed: each logs `type(exc).__name__` (the failed allocation, the journal entry kept, presence's deferral, the sweep's deferral, and `_put_back`'s kept tombstone). `raw_log_sites` on the fixed module returns nothing, and `test_no_daemon_module_logs_a_caught_exception_raw` passes (below).
- **The test:** `test_a_refused_trees_log_line_carries_a_code_of_the_closed_set_never_a_name` makes `_empty` raise each of the three codes and one outside the set, each with a `SECRET7…` detail, and checks the exact line and that no `SECRET7` reaches the log.

#### The emulated rule (the hub's decision 2)

- **`tests/rename_rules.py`** wraps `os.rename` and `os.replace`, which `Path.rename`, `Path.replace` and `shutil.move` call too, to raise `PermissionError(EACCES)` when the source is a directory (never through a symlink) whose mode lacks owner write. `HOST` leaves this host's rule as it is and is the default. The module renames only through `os.rename` (removal and the put-back) and `atomic_publish`, whose source is a file. `test_the_macos15_rule_refuses_to_rename_exactly_a_directory_without_owner_write` checks the wrapper by path, by descriptor, through `os.replace` and through `Path.rename`, and that owner-`rwx` directories, a read-only file and a symlink to a locked directory still rename. It asserts only modes whose outcome is known on every macOS, so it holds on a real macOS 15 too.
- **Under both rules, the rule in each id** (`host-rename`, `macos15-rename`): P3-2's regression and the new locked-tombstone one, T1 (both tests; the child installs its parent's rule before anything renames), T2, T3 (both tests), T4 (both) and T5, the new swap tests, the daemon-level P3-2 sweep (the fake daemon runs in the test's process, so the wrapper reaches it), and the mutation rows for `no-top-chmod`, `no-rename-verify`, `no-identity-check`, `no-pre-rename-identity-check` and `no-depth-check`.
- **T2's own renames** (its crash, and a swap) now work under either rule, as a provider renaming its own locked folder must on macOS 15: `rename_rules.move_dir` adds owner write, renames, and puts the mode back. Its crash is now either the daemon's own (removal killed right after its rename, through the production code) or, with the step's flag false, a tombstone an earlier build left with the folder's mode as the provider set it.
- **T1's `remove:top-chmodded` point** now matches a `chmod` of any `subfleet-attempt-` name, so it fires at the mode change before the rename, which leaves the folder at its own name with owner `rwx`, and at a crash-left tombstone's.

#### Invariants, under both rename rules

T1 to T5 are as stated above, and now each holds under this host's rule and macOS 15's. T3 (mode-proof) is the one this round restores: under macOS 15's rule, a top folder without owner write was deferred for ever.
- **T1, no orphan:** both T1 tests run under both rules, 60 cases.
- **T2, release only on absence:** the model runs under both rules, its crash through the production code or as a tombstone an earlier build left.
- **T3, mode-proof:** both T3 tests run under both rules.
- **T4, bounded,** and **T5, no widening:** both T4 tests and T5's property run under both rules (`5635db328`). No rename in them depends on a mode they set, so the rule changes nothing there, as it should. P3-5's 2,000-level child stays on this host's rule; it takes about 45 s.
- **T6, rule-independence (new):** for every tree T3 draws (up to 8 leaves, every mode from 0000 to 0777 on each entry and on the top folder), with no undeletable file or with one at a drawn place (EPERM from `unlink`, as `chflags uchg` gives), removal ends the same way under both rules. Removed under both; or deferred under both, with the same lines logged, the folder back at its recorded path, and the same mode on every entry. `test_t6_removal_ends_the_same_way_under_either_rename_rule` builds each drawn tree twice and compares, and also requires that only an undeletable file defers.

#### Mutants

Each runs as before: the production module with one decision changed, executed in memory, must fail a named test, and every oracle also passes on the real module. r2 adds two mutants and gives three existing ones new oracles, because the check before the rename now refuses the earlier swap, and the mode change before the rename covers P3-2's own input. Rows marked "both" run once per rename rule, and each id names its rule.

| Mutant | What changes | Test that fails | Rules |
| --- | --- | --- | --- |
| no-pre-rename-chmod (new) | the mode change before the rename dropped | `test_p3_2_owner_unreadable_top_folder_is_removed[macos15-rename]` | macOS 15's only, which is the point: on this host's rule the tombstone's mode change covers for it |
| no-pre-rename-identity-check (new) | the check before the mode change dropped | `test_a_locked_directory_swapped_in_before_removal_is_neither_changed_nor_moved` | both |
| no-identity-check (the post-rename re-check) | what the rename moved is not checked | `test_a_directory_swapped_in_after_removals_last_look_is_refused_and_put_back` (new oracle) | both |
| no-rename-verify | delete in place, with no rename and no check after it | the same test (new oracle) | both |
| no-top-chmod | a crash-left tombstone's mode change dropped | `test_p3_2_a_locked_tombstone_a_crash_left_is_finished` (new oracle) | both |
| no-depth-check | `_empty` descends past `MAX_DEPTH` | `test_t4_a_chain_past_max_depth_is_refused_in_bounded_descriptors` | both |

The other thirteen mutants are unchanged and still killed by their oracles: reuse, no-census, no-prefix, follow-symlink, salvage-hold, absent-no-receipt, age-from-marker, age-from-birth-time, no-tombstone-record, no-rmdir, follow-inner-symlink, cross-device and put-back-over. `test_the_pre_rename_chmod_mutant_is_caught_by_this_hosts_rule_only_if_it_refuses` pins the first row's point: it probes whether this host renames a 0500 directory, and requires the mutant to survive this host's rule exactly when it does, so it holds on CI's macOS 15 as well, where the host's rule kills it.

#### The e2e failure (the hub's decision 4)

`tests/e2e/test_conversations.py::test_a_daemon_restart_with_a_steer_in_flight_replays_without_resending` failed once, in one of the two Pythons. I had no network, so I didn't read its traceback; this classification rests on the test and the code it drives.
- **#258's code runs in it.** Every conversation turn is an attempt, so `_launch` calls `_prepare_attempt_tmpdir`, which calls `AttemptTemp.create` (#258's journal and exclusive `mkdir`), and the restart's startup sweep runs `_sweep_attempt_tmpdirs` with #258's tombstone and journal handling.
- **Nothing it asserts depends on that.** It asserts that the host turn completes after the restart and its approval, that the steer is delivered once, and their states. The host turn's TMPDIR was made and recorded before the crash; a steer joins the running turn without a job or attempt of its own (C-24.9), so no allocation is in flight at the crash and no journal entry can be pending. The sweep releases only allocations of terminal or quarantined holders and never ages a recorded path, so the live turn's folder is untouched.
- **The other Python passed it at the same commit,** so it is not deterministic at #258's head.
- **Classification:** #258's code is reachable from this test, but I found no path from it to the assertions, so I changed nothing and class it, provisionally, as a timing flake not caused by #258. I can't call it pre-existing: I have no failing run from before #258. The traceback would settle it (`gh run view 40166524067 --log-failed`, the hub's to run): a TMPDIR path, a `tmpdir.*` event or `attempt TMPDIR could not be allocated` in it would make it #258's.

#### Red first

- **CI at `4576d180c` on macOS 15** is the first red run: the P3-2 regression, T1's three `remove:top-chmodded` cases, T2, T3's any-modes case (`('0o0', [])`) and its deferral case, the `no-top-chmod` production pass and the daemon-level P3-2 sweep, in both Pythons, and the lint.
- **Hold h2, step 2,** ran this round's tests (`35778b2bd`) on `4576d180c`'s `attempt_tmp.py` (the merge left it unchanged). The hold's deadline stopped it after 29 tests, of which exactly the two expected at that point failed: `test_p3_2_owner_unreadable_top_folder_is_removed[macos15-rename]`, and T1's `recorded`, `remove:top-chmodded` case under `macos15-rename`. The other 27 it reached passed, the two failures' `host-rename` twins among them. So the emulation fails, on #258's code, the first two of the tests CI failed, and this host's rule passes them.
- **The lint at `4576d180c`:** `raw_log_sites` on the merged base's `attempt_tmp.py` returns `subfleet/attempt_tmp.py:363 passes exc raw`, the finding CI reported; on the fix it returns nothing.
- **A plain-Python probe** (not the test suite: a one-off script, about a second of work, since removed with the scratch) ran the same removals with `4576d180c`'s `attempt_tmp.py` and with the fix's, under both rules:

| Case | `4576d180c`, this host's rule | `4576d180c`, macOS 15's rule | fix, either rule |
| --- | --- | --- | --- |
| top folder 0000 | removed | deferred, `PermissionError`, folder kept | removed |
| top folder 0500 | removed | deferred, `PermissionError`, folder kept | removed |
| top folder 0300 | removed | removed (the emulation refuses only without owner write) | removed |
| locked crash-left tombstone | removed | removed | removed |
| a pinned file, top folder 0000 | deferred, put back at 0700, then removed | deferred, kept at 0000, then deferred again | deferred, put back at 0700, then removed |
| a refused tree outside the closed set | logs `TreeError(SECRET7code9)` | (rule-free) | logs `TreeError` |

  The base's two rows that differ by rule are what T6 compares, so T6 fails there; its own red run was not reached in a hold (below).

#### Holds

Every pytest ran under `suite_hold.sh`, single-process, with `TMPDIR` and Hypothesis storage in this job's scratch (`sf-tmp1p3r2`) and `env -u SUBFLEET_RETENTION_DORMANT`, from `git archive` copies of the named commits, so a hold never saw the worktree change. A hold ran its pytest steps one after another under one 19-minute deadline (`holdplan.py`), each step one pytest invocation. The second slot was closed all day (free disk under 50 GB, at times load over 120), and about ten holds waited on slot A.

| Hold | Queued, started (UTC) | Steps | Result |
| --- | --- | --- | --- |
| red-only | 08:39, never | r2's tests on the base, from the worktree | withdrawn unstarted at 08:52, to run from copies instead and free the worktree |
| red-only | 08:54, never | the same from a copy, plus `4576d180c`'s test files | withdrawn unstarted at 09:10, folded into h1 |
| h1 | 09:15, never | red, the proof, the green core | withdrawn unstarted at 09:51, folded into h2 with the rest of the hold list |
| h2 | 09:51, 11:40 (A) | 1. green core at `f02900d6d`; 2. red at `35778b2bd`; 3. the proof at `4576d180c`; 4–5. the rest of the hold list at `f02900d6d` | 1: 170 passed in 913 s (load 55–110). 2: stopped at the deadline after 29 tests (2 failed, as intended). 3–5: not reached |
| h3 | 12:03, never | steps 3–5, then step 2 again | withdrawn unstarted at 12:30, the 4 h 30 m cutoff |

The T1 children took about six seconds each at that load, so step 1 alone used 16 of h2's 19 minutes.

#### Counts

"Before" is #258 at `4576d180c` on this host, as out-p3 and the hub ran it (these files passed there); "now" is h2 at `f02900d6d`.

| File | Before (`4576d180c`) | Now: rule-free / `host-rename` / `macos15-rename` |
| --- | --- | --- |
| unit `test_attempt_tmpdir_leaks.py` | 44 passed | 93 passed: 13 / 40 / 40 |
| unit `test_attempt_tmpdir_mutations.py` | 34 passed | 49 passed: 27 / 10 / 12 |
| fake `test_attempt_tmpdir_leaks_daemon.py` | 7 passed | 8 passed: 6 / 1 / 1 |
| unit `test_daemon_log_sites.py::test_no_daemon_module_logs_a_caught_exception_raw` | failed on CI (`attempt_tmp.py:363`) | 1 passed |
| unit `test_daemon_log_sites.py::test_the_site_lint_tells_raw_from_scrubbed` | 19 passed | 19 passed |

Every mutant was killed by its named test under each rule its row names, and every oracle passed on the real module.

#### Not run

- **The proof at `4576d180c`.** `4576d180c`'s three test files, copied unchanged except for their basenames and the mutation file's import of its leak suite, were to run under the emulated rule, T1's children included (a scratch plugin installed the rule only for those copies). h2 didn't reach that step and h3 never started. What stands in for it: CI itself on a real macOS 15; h2's partial red run, where the emulation failed the first two of CI's tests on #258's code and this host's rule passed them; and the probe.
- **The rest of the red run:** T6's failure at base, and r2's other tests there (h2's step 2 stopped after 29 of 146).
- **The rest of the hold list at the fix:** unit `test_attempt_tmpdir_lifecycle.py`, `_guardian.py`, `_retention.py` and `_sandboxes.py`; fake `test_attempt_tmpdir_daemon.py`; the rest of `test_daemon_log_sites.py`; `test_daemon_settle.py`; fake `test_daemon_launch.py`; unit `test_daemon_accept.py`; and the contract tests (`test_invariants_index.py`, `test_protocol_registry.py`, `test_retention_turn_contract.py`, fake `test_state_contract.py`, `test_test_module_names.py`). Of what this round changed, those files reach only `_remove` (the lifecycle suite's removal tests, including its swap test, which the new check now refuses earlier) and C-12.10's text. CI runs them all.
- **macOS 15 itself:** nothing here runs it. CI's `macos-15` shards are the proof, as the hub planned.
- **The e2e test,** and the rest of `tests/unit`, `tests/fake` and `tests/e2e`.

#### Deviations and housekeeping

- **The red run is partial** and the proof didn't run (above): the queue for the one open slot ran 1 h 49 min for h2, and h3 waited 27 minutes before the cutoff.
- **Several pytest steps in one hold.** To cut the waits, each hold ran its pytest steps one after another in one slot, under one 19-minute deadline; each step was one pytest invocation, single-process.
- **Commit order.** The brief's order is merge, tests, fix, docs. A second test commit (`5635db328`, T4 and T5 under both rules) came after the fix, and the docs came in two commits: the contract first, so the last hold could run the contract tests, and this report last.
- **The lint is unchanged.** The log sites pass it by an accepted form (decision 3's first choice), so `_reason` wasn't taught to it.
- **`test_directory_renamed_in_after_the_check_is_refused_and_put_back`** (lifecycle): the check before the rename now refuses its swap before anything moves, so nothing is put back there. By reading, every assertion it makes still holds, but the lifecycle file ran in no hold this round. It no longer kills `no-identity-check` or `no-rename-verify`, whose new oracle swaps later.
- **Commits on the detached HEAD,** as before: the branch is checked out in the caller's lane checkout, so its ref was not moved. Nothing was pushed.
- **Nothing of mine outside the workspace and the scratch folder,** beyond the hold wrappers' own logs; no network, no real credentials, nothing live. The scratch folder is removed, and `git status --ignored` is clean.

### r3: every mode change sets 0700 (#258 review r1b2, 2026-10-25)

Review r1b2 asked for changes at `2bdcb2e1f`: one P2 (P2-1) and three P3s, with checks 7 to 12. This round builds the hub's six decisions on `2bdcb2e1f`. Work began at 2026-10-26 04:17 UTC (2026-10-25 local), in a detached checkout as before; the commits are on its HEAD for the hub to push, and nothing was pushed.

Commits, oldest first:
- `af8b5599b`: test (the regressions, written to fail at `2bdcb2e1f`, and review r1b2's three mutants with their oracles).
- `5ea67495a`: fix, with the mutation rows that follow its text and r3's own mutants.
- A last docs commit: C-12.10 and its 2026-10-25 change-log entry, this subsection, and the leak suite's header.

#### The cause (P2-1)

- **A whole mode, by name.** Each of the three mode changes (before the rename, at a crash-left tombstone, and on a directory inside the tree) checked the entry by name, then called `os.chmod(name, mode | 0o700, dir_fd=…, follow_symlinks=False)`. That sets an absolute mode on whatever has the name by then. A provider that can write the user temp root (an unsandboxed Claude provider) can swap in, between the two calls, a hard link to any file of this user's on the volume, and that file takes the folder's mode plus 0700: 0077 and 0577 made a 0600 file outside the root 0777. Removal still refused afterwards, but the mode had changed.
- **The docs claimed otherwise.** `_remove`'s docstring, C-12.10, its change-log entry and r2 above all said such an entry "can at most gain owner permissions".

#### The decisions, as built

1. **Every mode change sets exactly `0o700` (`stat.S_IRWXU`).**
   - **`_own(record, name, parent)`** serves the mode change before the rename and a crash-left tombstone's. It opens the entry with `rfs.O_DIR` (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`) relative to the parent's descriptor, checks `_is` on that descriptor's `fstat`, and `fchmod`s it to 0700. A descriptor that is not the record returns False, and removal refuses (`TMPDIR removal refused for <attempt>: its folder was swapped`) with nothing changed.
   - **Only a `PermissionError` with `EACCES`** (no owner read: 0000, 0300) falls back to `os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)`, after the existing lstat check. Any other open error raises (`ENOTDIR` and `ELOOP` for a file or symlink swapped in, `EMFILE`, `EPERM`), and removal defers as before, logging the class.
   - **Inside the tree,** `_empty` now opens a directory first, with `rfs.open_dir(name, dir_fd=fd, expect=found)`, which checks the descriptor against the directory's lstat (device and inode; there is no record to apply `_is` to there). `writable` then sets 0700 through the descriptor. Only an open refused with `EACCES` sets 0700 by name first, then opens it, checked the same way. The old trigger (`& 0o500 != 0o500`, which changed a 0400 or 0600 directory by name) is gone.
   - **`writable`'s own `fchmod`** sets 0700 too, so every mode change removal makes sets exactly 0700. It was `mode | 0o700` on a verified descriptor, which was safe but put a refused folder back wider than 0700.
   - **Unchanged:** a mode changes only when owner read, write or search is missing, as in r2.
   - **The bound,** now in `_remove`'s docstring, `_empty`'s, C-12.10, its 2026-10-25 change-log entry and this report: an entry swapped in between the check and the mode change can be set to 0700, only if it is this user's (a symlink's own mode, never its target's); it never gains group or other bits. Also: after a refused removal, the folder put back is at 0700.
   - **One wording deviation:** the docs qualify that last sentence ("one that already had owner read, write and search keeps its own mode, which removal never changed"), because removal changes no mode of a folder that already has owner `rwx`. A 0755 folder whose removal is refused goes back at 0755. As a bare sentence it would be false for that folder.
2. **The regression,** `test_an_entry_swapped_in_before_a_mode_change_never_gains_group_or_other_bits`, is the reviewer's property as a real test.
   - **Coverage:** all three sites, both rename rules, folder modes 0000, 0500, 0077 and 0577 with a hard link to a 0600 file outside the root swapped in, and a 0500 folder with a symlink to a 0500 directory outside it swapped in.
   - **Two windows:** right after removal's check of the entry by name (`after-check`), and right before a mode change by name (`at-mode-change`, the reviewer's hook). The first is the descriptor path's only window; the second reaches the fallback's.
   - **The swap works on a real macOS 15 too.** It adds owner write, renames, and puts the mode back, as a provider must there.
   - **What it asserts:** the file outside ends at 0600 or 0700, never with a group or other bit; its contents are unchanged; the symlink's target stays 0500 with its contents; removal refuses or defers whenever the swap ran, and removes the folder when it didn't. In the `after-check` window the swap must run, so that window is never vacuous.
   - **The two path tests:**
     - `test_a_folder_the_owner_can_read_gets_0700_through_its_descriptor_never_by_name` (0500, 0400, at each site, under both rules) requires exactly one mode change, an `fchmod` to 0700 of that directory's inode, and none by name.
     - `test_a_folder_the_owner_cannot_read_gets_0700_by_name_once_its_open_is_refused` (0000, 0300) requires an open refused with `EACCES`, then exactly one `chmod` by name to 0700.
   - **T3's deferral test** now runs from 0000, 0500, 0077 and 0577, with the directory holding the undeletable file at the same mode, and requires both back at exactly 0700.
   - **T1's `remove:top-chmodded` point** now also fires at an `fchmod` (a remove child's first is the top folder's), since a 0500 folder's mode now changes through its descriptor. A new point, `remove:top-unreadable-chmodded`, crashes after a 0000 folder's change by name, for `recorded` and `legacy` allocations. An unknown 0000 folder is left out: its marker can't be read, so no sweep sights it (#246's rule, unchanged).
3. **P3-1:** `test_a_tombstone_value_that_is_not_a_tombstone_name_refuses_removal`, the reviewer's test.
   - **The values:** `../escape-r1b`, a name without the prefix, one with a slash, one longer than 128 characters, and a non-string.
   - **What it asserts:** removal returns None and logs `its recorded tombstone is not one`; the allocation stays where it was with its contents; nothing in the root or around it is renamed; `tombstone_path` is None.
4. **P3-2:** the reviewer's two tests.
   - **`test_a_successful_create_leaves_no_journal_entry`** checks three in-process creates. Each name is in the journal when its folder is made, no entry is left once its marker is published, and each folder is owned and marked. The in-journal-at-mkdir check also kills the reviewer's "intent written after mkdir" mutant in-process, which only T1's child could reach before.
   - **`test_a_journal_entry_naming_a_symlink_or_a_file_is_never_recorded_or_removed`** checks three journal-named entries. A symlink to a 0500 directory outside, a file, and a hard link to a 0600 file outside are swept three times a day apart. None is sighted or removed, and their journal entries go. The link, its target, the file and the file outside are unchanged.
5. **The power-loss gap (check 12):** when `_intend` makes `tmpdir-intents`, it fsyncs the parent (the state root) before publishing the first entry.
   - **The creation test:** the mkdir now uses `exist_ok=False` inside `try`, and an existing directory is accepted as `exist_ok=True` accepted it. So "made here" is exactly the mkdir that succeeded.
   - **The fsync test:** `test_the_journal_folders_own_entry_is_fsynced_once_when_it_is_made` wraps `os.fsync`, `os.mkdir` and `os.rename` in-process. Across three creates (two on one `AttemptTemp`, one on a second over the same store), the event sequence must be: one fsync of the state root, with the folder already there and empty, then entry, folder, entry, folder, entry, folder.
   - **Residuals, not built:**
     - Two creates racing the very first one in one process: the second can publish its entry and make its folder before the first's fsync.
     - An fsync that fails: the folder stays made with its parent unsynced, and later creates don't retry.
     - A state root the same `mkdir(parents=True)` made: it isn't fsynced in its own parent. The daemon makes the state root long before any allocation.

     Each needs a power loss inside a window of microseconds, once per store.
6. **P3-3:** no change. T3's any-modes test passed in hold c1 under both rules (16 s and 12 s), so there is no failing example to keep.

#### A counterexample, and the change it needed

The mutation row `no-pre-rename-identity-check` (the check by name before the mode change dropped) survived its oracle at `5ea67495a`, under both rules.
- **The oracle:** `test_a_locked_directory_swapped_in_before_removal_is_neither_changed_nor_moved` swaps in a 0500 directory of this user's.
- **Why it survived:** with the check dropped, `_own` opens that directory, `_is` on its descriptor fails, and removal refuses before any mode change or rename. Every assertion holds: the descriptor's own check does the work.
- **The intent still holds:** the check by name stands alone only before a change by name, for a directory no open admits.
- **The change:** the oracle is now parametrized by the swapped-in directory's mode (0500 and 0000), and the row uses 0000. The mutant changes the 0000 directory to 0700 by name and fails `and before any mode change`. Production passes both modes. No production change was needed.

#### Invariants

T1 to T6 are as stated above and hold under both rename rules, now with the bound restated. One invariant is new:
- **T7, the mode bound:** for every entry swapped in between removal's check of a directory and its mode change, at any of the three sites, nothing outside the allocation gains a group or other bit, and a symlink's target is never changed. The only mode such an entry of this user's can get is exactly 0700.
- **Where it is tested:** the swap property above, plus the two path tests that pin how it holds (no change by name for a folder an open admits, and a change by name only after `EACCES`).

#### Mutants

Each is the production module with one decision changed, executed in memory, and must fail the named test. Every oracle also passes on the real module ("production" rows). All are in `tests/unit/test_attempt_tmpdir_mutations.py`; "both" rows run once per rename rule.

| Mutant | What changes | Test that fails | Rules |
| --- | --- | --- | --- |
| or-mode-before-rename (new) | r2's `chmod(name, mode \| 0o700)` by name restored before the rename | swap property, `before-rename-0077-hard-link-at-mode-change` | both |
| or-mode-tombstone (new) | the same at a crash-left tombstone | swap property, `crash-left-tombstone-0077-hard-link-at-mode-change` | both |
| or-mode-nested (new) | the nested fallback sets `mode \| 0o700` by name | swap property, `nested-0077-hard-link-at-mode-change` | both |
| or-mode-descriptor (new) | both `fchmod`s set `mode \| 0o700` | T3's deferral, `0577` (put back at 0777) | both |
| by-name-own (new) | `_own` always changes by name: the descriptor path dropped | the descriptor test, `before-rename-0500` and `crash-left-tombstone-0500` | both |
| by-name-nested (new) | `_empty` always changes a directory by name before opening it | the descriptor test, `nested-0500` | both |
| no-journal-fsync (new) | the state root's fsync dropped | the fsync test | |
| any-tombstone (review r1b2) | `_tombstone` accepts any string | the bad-tombstone test, `../escape-r1b` | |
| create-skips-settle (review r1b2) | `create` never settles its entry | create leaves no journal entry | |
| sweep-no-ownership-check (review r1b2) | the sweep's directory-and-owner check dropped | the journal-entry test | |
| no-pre-rename-identity-check | the check by name before the mode change dropped | the locked-swap test, now `0000` (above) | both |
| no-top-chmod, no-pre-rename-chmod | their r2 decisions dropped, at r3's text (`_own`'s calls) | as in r2 | as in r2 |

The other seventeen are unchanged and still killed by their oracles. `test_the_pre_rename_chmod_mutant_is_caught_by_this_hosts_rule_only_if_it_refuses` still passes: the mutant survives this host's rule, as it should on macOS 26. Review r1b2 executed the survival of its three mutants against the PR's earlier tests; this round didn't re-run that.

#### Red first

**Hold red1** ran `af8b5599b`'s tests on `2bdcb2e1f`'s module: the worktree before the fix, the same files as that commit. It selected 116 tests: all of r3's, T3's deferral, T1's two top-chmodded points, and the three new mutation rows. Result: **47 failed, 69 passed**.

| Test | Failed at `2bdcb2e1f` | Why |
| --- | --- | --- |
| swap property, before the rename | 8 of 20 (4 of the 8 hard-link cases per window, as review r1b2 found) | 0077 and 0577, in both windows, under both rules: the file outside became 0777 |
| swap property, crash-left tombstone | 8 of 20 | the same |
| swap property, nested | 4 of 20 | 0077 only: r2's nested site changed a 0577 directory through its descriptor, so a swap there reaches no change by name |
| descriptor test | 10 of 12 | a mode change by name; `nested-0500` passed, because r2 already used the descriptor there |
| fallback test | 12 of 12 | no open was attempted before the change by name |
| T3's deferral | 4 of 8 | 0077 and 0577 came back at 0777 |
| fsync test | 1 of 1 | no fsync of the state root |

The other 69 passed as intended:
- 40 swap cases;
- `nested-0500` under both rules;
- T3's deferral at 0000 and 0500;
- the five P3-1 values;
- the two P3-2 tests;
- T1's ten top-chmodded cases;
- the three reviewer mutants' kill and production rows.

The P3 tests' red is the kill rows: each fails on its mutant at `2bdcb2e1f`.

#### Holds

Every pytest ran under `suite_hold.sh` in slot A, one pytest per hold, single-process, from this workspace, with `TMPDIR` and Hypothesis storage in this job's scratch (`sf-tmpdirp3r3`) and `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`. This job's tool caps a foreground call at 10 minutes and detaching was ruled out, so a small driver ran each hold in its own process group, in the foreground, and would have killed the group at 540 s. No hold reached that deadline.

| Hold | Started (UTC) | Head | Selection | Result |
| --- | --- | --- | --- | --- |
| red1-co | 04:40 | `2bdcb2e1f` + r3's tests | collect-only | 116 of 250 selected |
| red1 | 04:42 | the same | r3's red set | 47 failed, 69 passed in 264 s |
| g1 | 04:52 | the fix, uncommitted | r3's tests, T3's deferral, the mutation file | 183 passed, 2 failed (the counterexample above) |
| g2 | 04:54 | with the oracle change | the locked-swap test and its rows | 8 passed |
| c1 | 04:56 | `5ea67495a` | leaks, all but T1 and P3-5 | 131 passed in 360 s |
| c2a | 05:04 | `5ea67495a` | T1's create cases, and its `recorded` removal cases | 30 passed in 190 s |
| c2b | 05:08 | `5ea67495a` | T1's `legacy` and `unknown` removal cases | 34 passed in 79 s |
| c3 | 05:10 | `5ea67495a` | P3-5, and the whole mutation file | 87 passed in 99 s |
| c4 | 05:12 | `5ea67495a` | fake `test_attempt_tmpdir_leaks_daemon.py` | 8 passed in 36 s |
| guards | after the docs commit | the docs head | the three contract guards | in the next subsection |

`raw_log_sites` on the fixed module returns nothing, as on `2bdcb2e1f`. That was run as a plain call, not as a pytest.

#### Counts

"Before" is r2 at `2bdcb2e1f` as its report gives it; "now" is this round's holds at `5ea67495a`.

| File | Before (rule-free / `host-rename` / `macos15-rename`) | Now |
| --- | --- | --- |
| unit `test_attempt_tmpdir_leaks.py` | 93 passed: 13 / 40 / 40 | 197 passed: 21 / 88 / 88 (c1, c2a, c2b, c3) |
| unit `test_attempt_tmpdir_mutations.py` | 49 passed: 27 / 10 / 12 | 85 passed: 35 / 24 / 26 (c3) |
| fake `test_attempt_tmpdir_leaks_daemon.py` | 8 passed: 6 / 1 / 1 | 8 passed: 6 / 1 / 1 (c4) |

The leak suite's 104 new cases:
- 60 swap cases, 12 descriptor cases and 12 fallback cases;
- 5 P3-1 values, 2 P3-2 tests and 1 fsync test;
- 6 more T3 deferral cases, 2 more locked-swap cases, and 4 T1 cases at the new point.

#### Not run

- **The rest of the suite,** as the brief said: CI runs it. That includes unit `test_attempt_tmpdir_lifecycle.py`, `_guardian.py`, `_retention.py`, fake `test_attempt_tmpdir_daemon.py` and `test_daemon_log_sites.py`.
  - **By reading,** none of them patches `os.open`, `os.fchmod`, `os.chmod` or `os.fsync` around removal or allocation.
  - **The lifecycle suite's read-only tree** (0555 directories and a 0000 one) takes both new paths: the descriptor for 0555, the fallback for 0000.
  - **Its two mutation oracles** passed in c3.
- **A real macOS 15:** CI's runners are the proof, as for r2. The swap's own renames follow that rule, so the property runs there as written.
- **The survival of review r1b2's three mutants** against the PR's earlier tests: the reviewer executed it, and it was not re-run here.

#### Deviations and housekeeping

- **"After a refused removal, the folder put back is at 0700"** is qualified in the docs, as decision 1 above says.
- **Inside the tree,** the descriptor is checked against the directory's lstat (device and inode), not `_is`, because no record names a directory there. That check is #246's `open_dir(expect=…)`, unchanged.
- **The `no-pre-rename-identity-check` oracle changed,** for the counterexample above.
- **Holds were shorter than 20 minutes,** under a 540 s deadline each, because a foreground call here is capped at 10 minutes. So the files ran in seven holds rather than two or three.
- **Commits on the detached HEAD,** as before. Nothing was pushed. No network, no live process, nothing of mine outside the workspace and the scratch folder beyond the hold wrappers' own logs.

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
