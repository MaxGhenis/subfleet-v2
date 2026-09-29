# Job retention by archive

Revision 2, 2026-09-29: as built, under Max's d635 ruling ("ship at the
archive-sweep bar"). Revision 1 (ccc85387, 2026-09-28) was the design; its two
reviews (`~/reviews/retention-2026-09-28/archive-design/review-design-{opus,astra}.md`)
and the ruling changed it as section 2 lists. The binding clauses are C-8.4,
C-13.4 and C-17.1 in `docs/acceptance-contract.md`. Code:
`subfleet/retention.py` (the pass), `retention_archive.py` (one job's
journaled retirement, restore), `retention_git.py`, `retention_fs.py`,
`retention_holders.py`, `retention_survey.py`, `retention_cli.py`.

## 1. Decision

Before a finished job's trees are deleted, retention preserves everything in
them that exists nowhere else, in an archive it owns, and reads the archive
back:

- **Files.** Every file of the job's allocated worktree and of its job
  directory, uncommitted, untracked and ignored alike, byte for byte: an APFS
  clone (`fclonefileat`, no space until either side changes), or a byte copy on
  a volume without clones. The one exception: a tracked file whose raw bytes
  hash to a blob that a network remote holds (section 6).
- **Git.** The worktree's admin directory (`<repo>/.git/worktrees/<id>`) byte
  for byte, and every object it names — HEAD, the index, `ORIG_HEAD`,
  `MERGE_HEAD`, `FETCH_HEAD`, `refs/worktree/*`, `refs/bisect/*`, rebase and
  sequencer state, every reflog entry — together with the job's salvage
  commits, as one synthetic *anchor* commit, in a bundle of every commit no
  network remote holds.
- **Rows.** The job's database rows, as `rows.json`.

Then it deletes only entries whose `lstat` signature is still the archived one,
re-validated right before each unlink. Anything new or changed goes to a
conflicts folder instead. Nothing depends on a repository retention does not
control, and no proof about what a file means is needed: retention first puts
it somewhere else.

## 2. What changed from revision 1

| Finding | Revision 1 | As built |
|---|---|---|
| Opus 1, Astra 2: omitted files depended on the job's source clone | Omit a tracked file equal to HEAD's blob; lock the registration | Omit only a blob that a network remote's `refs/remotes/*` reach, in a repository that is not scratch; commits no network remote holds go into a bundle the archive owns |
| Astra 1: a corrupted loose object passed `--batch-check` | Check that omitted blobs exist | Read every omitted blob whole (`cat-file --batch`) and hash it; a bad one is archived instead |
| Opus 2, Astra 7: a create-only anchor wedged a rolled-back job | Create-only `update-ref`, roll back on failure | The anchor is deterministic (fixed identity and dates); an existing ref equal to it is success |
| Opus 3, r4 finding 2: the locked registration did not keep `refs/worktree`, `refs/bisect`, `ORIG_HEAD`, `MERGE_HEAD`, `FETCH_HEAD` or reflog-only commits | Lock the registration for ever | Anchor every object the admin directory names (a token scan of its files, per-worktree refs and HEAD's reflog through git, the index), bundle it, archive the directory, and remove the registration |
| Opus 5: thousands of locked registrations slow everyday git | Accumulate | Registrations are removed after archiving; none accumulate |
| r4 finding 1: a write after the final check was deleted | Signatures checked before deletion | Every entry is re-validated right before its unlink; new or changed entries go to `retention-conflicts/<job>/`; admin files that changed keep the registration (locked) and a late anchor ref |
| Opus 4, Astra 8, r4 finding 3: slow checks, 1-hour cycles, livelock | One job at a time, two `lsof` listings each, a 30-minute cap | A batch of jobs shares two listings; each job archives in a time slice and is parked with its progress (resumable across passes and restarts); busy or failing jobs are deferred; catch-up passes 5 s apart |
| Opus 6, Astra 6: archives not durable on macOS; rows written unverified | `fsync` | `F_FULLFSYNC` for every archive file, journal and directory; `rows.json` is read back before the commit transaction, which compares it with the rows it deletes |
| Opus 7: no free-space guard | none | Archiving is by clones (no new space); a byte copy or a bundle never takes free space below 2 GiB |
| Opus 8: an interrupted deletion became a permanent conflict | Full signatures | Directories match on (type, device, inode); a file that had other links matches without its ctime |
| Astra 4: rollback into an occupied path removed the lock | Remove the lock | The tree goes to conflicts and retention's lock stays |
| Astra 5: nested registrations | Lock them too | A linked worktree nested inside the tree (its admin directory elsewhere) keeps the job; a submodule's gitdir inside the admin directory is archived with it |
| Astra 9: a resume could read a job directory mid-retirement | Refuse when a `retire:` lease exists | A resume holds `retire:<source>` while it reads, released in the transaction that inserts its job; retention and a resume exclude each other |
| Opus 9: the daemon's own repository-wide `git worktree prune` | Noted | `_discard_worktree` removes only its own registration |
| Salvage | Stopped pinning | Still pins (C-8.4: "salvage refs referenced nowhere else"), and the archive's verified bundle is that elsewhere; a salvage ref that cannot be bundled keeps its job, as before |

## 3. Layout

```
<state>/retention/<job>/journal.json   the retirement's state, written before each step
<state>/retention/<job>/worktree/      the quarantined worktree
<state>/retention/<job>/job/           the quarantined job directory
<state>/retention/<job>/archive/       the archive while it is built (progress.jsonl makes it resumable)
<state>/archive/<job>/                 the verified archive, published once the rows are gone:
    manifest.json    every entry of every tree: path, lstat signature, sha256 and stored name, or the
                     omitted blob id; link targets; hard-link groups
    summary.json     totals, for `retention archives`
    files/           the stored files (clones), named by the signature of the version archived
    commits.bundle   the anchor and the salvage refs, with every commit no network remote holds
    rows.json        the job's rows, as deleted
<state>/retention-conflicts/<job>/     anything verified deletion found new or changed
```

The source repository gains only objects (the anchor's trees and commits) and
create-only refs `refs/subfleet-archive/<job>/<anchor>` (and `late-<anchor>`),
which retention never moves or deletes.

## 4. One job's retirement

Every step writes the journal first (temp file, `F_FULLFSYNC`, rename, sync
the directory), so a crash anywhere is resumed or undone by the next pass.

1. **Select**, in one transaction: re-check the job's pins; take
   `retire:<job>` and `worktree:<path>` for `retention:<job>`. A `retire:` held
   by a resume, or a worktree lease held by anyone else, keeps the job.
2. **Begin** (no transaction): find the registration (the tree's gitfile names
   an admin directory directly under `<common>/worktrees` whose `gitdir`
   backlink names this tree), or, when the tree is gone, the registration whose
   backlink names its path; resolve every salvage ref; check the trees are on
   the state root's volume. A backlink that names another tree, an
   unresolvable salvage ref or another volume defers the job.
3. **Lock** the registration with `locked` (text `subfleet retention: <job>`),
   so no `git worktree prune` or `git gc` drops it while the tree is away. A
   lock someone else wrote defers the job.
4. **Quarantine**: rename the worktree and the job directory into
   `retention/<job>/`. No process can reach them by path afterwards.
5. **Holder check 1** (one `lsof` for the whole batch, section 7).
6. **Archive**, within the job's time slice (default 120 s): walk the trees
   descriptor-relative, never following a link, in byte order; clone or omit
   each file, re-`fstat`ing it around the read (a change defers the job); walk
   the admin directory in place; verify the omitted blobs whole; build the
   anchor, create its ref, write and verify the bundle; write the manifest;
   read every stored file back and check its size and sha256. A job whose
   slice runs out is parked in quarantine with its progress and continues in
   the next pass.
7. **Holder check 2** (one `lsof`, also matching the archived inodes).
8. **Final check**: walk every tree again; every entry must still be there with
   its archived signature, and nothing may have been added.
9. **Commit**, in one transaction: write `rows.json` and read it back first;
   inside, compare the rows with it, ask every pin again (the conversation
   service's included), check both leases are still retention's, then delete
   the rows and the leases. This is the point of no return. While the rows
   exist, no byte of the job has been deleted.
10. **Publish**: rename the archive to `<state>/archive/<job>`.
11. **Reclaim**: verified deletion (section 8) of the worktree, the job
    directory and the admin directory, which removes the registration. If
    anything in the admin directory changed after the final check, it is kept
    (locked), and what it names is anchored under a `late-` ref.

**Rollback** (any step before commit): rename the trees back (an occupied
original path sends ours to conflicts and keeps the lock), remove the lock if
retention wrote it, release the leases, and defer the job: 1 hour when busy or
changed, 15 minutes when the listing failed, 6 hours on an error, 24 hours for
a lasting condition (a nested linked worktree, a foreign lock, an unresolvable
salvage ref, another volume). Anchor refs stay; they are harmless. A rollback
for a passing reason keeps the archive cache, so the next attempt reads only
what changed.

**Recovery** (start of every pass): a `retention:` lease with no journal is
released (the old retention's, or a selection that died before its journal); a
journal in `committing` is resolved by whether the rows exist; everything
committed is published and reclaimed; everything before commit continues.

## 5. What the archive covers

| Where work can live | How it survives |
|---|---|
| Tracked files, modified or not | Archived byte for byte, unless the raw bytes equal a blob a network remote holds (section 6) |
| Untracked and ignored files: `build/`, `node_modules`, `.venv`, data, logs | Archived byte for byte |
| Staged content | The index file is archived; every blob it stages is in the anchor's `index/` tree, in the bundle |
| Detached, reflog-only, `ORIG_HEAD`, `MERGE_HEAD`, `FETCH_HEAD`, `refs/worktree/*`, `refs/bisect/*`, rebase state | Every commit, tree and blob id the admin directory names is a parent or entry of the anchor, in the bundle; the directory itself is archived |
| Salvage commits | Their refs are in the bundle; the refs themselves are never touched |
| Nested repositories (`.git` directories, bare repositories) | Archived byte for byte, object stores included |
| A submodule (gitdir inside the admin directory) | Archived with the admin directory |
| A linked worktree nested in the tree (its admin directory elsewhere) | The job is kept |
| Symlinks, hard links, FIFOs, modes, mtimes | Recorded and restored; links are never followed |
| The job directory: prompts, logs, attempt records, deliverables | Archived byte for byte |
| The job's rows | `rows.json` |

## 6. Omission

A regular file of the worktree is left out of the byte archive only if all of
these hold:

- the repository is not scratch: not under a temporary directory or the state
  root, no path component named like `scratch` or `tmp`, its own complete
  object store (no alternates, not shallow, not a partial clone), and at least
  one remote whose URL names another machine (`https://`, `ssh://`,
  `user@host:`); a local-path remote is another repository retention does not
  control;
- its raw bytes (read now, no filters, no stat cache) hash to the blob at the
  same path in a commit that those remotes' `refs/remotes/*` reach — HEAD or
  the job's baseline when they are pushed, else the held commits at the
  boundary of their unpushed history;
- that blob reads back whole from the object store and hashes to its id;
- it has one link.

The manifest records the blob id with the file's actual mode and mtime. When in
doubt, the bytes are archived.

## 7. Holder check

`lsof -n -P -w -F pcftaDin` once per check per pass, for the whole batch. A
process holds a job if it has its current or root directory, a text or memory
mapping, any directory descriptor, or a descriptor open for writing, under the
job's quarantine, its original paths, or its admin directory, or at the second
check on one of the archived (device, inode) pairs. A read-only descriptor on a
file is not a hold. The check fails closed: `lsof` missing, failing or timing
out (15 minutes) defers the batch.

## 8. Verified deletion

Descriptor-relative and post-order. For every entry present: unlisted or
changed (type, device, inode, size, mtime, mode, and ctime unless the inode had
other links) goes to `retention-conflicts/<job>/<tree>/<path>` by rename (a
writer's open file keeps its data); a listed, unchanged file is unlinked; a
directory is emptied (made owner-writable first if it was read-only) and
removed with `rmdir`. Directories match on type, device and inode, so a
deletion interrupted half way resumes cleanly. An entry that cannot be
unlinked or moved stays, and its directory goes to conflicts as the
remainder, so no half-deleted tree is left where retention works. A
`retention.conflict` event names what was kept.

## 9. Pins

A job is kept while any of these holds (C-8.4, unchanged from `release/217`
except salvage): it is not terminal; it is a gate review; an attempt is live or
quarantined; an unread notice addressed to a session; it is a parent of any
job; it or an attempt holds a lease; another holder has its worktree lease; a
resume holds its `retire:` fence; a salvage ref of an in-place job (or one that
cannot be bundled); gate or merge evidence names it; the conversation service
names it (asked again inside the commit transaction); a turn job within
`turn_keep_days`; an explicit reference.

## 10. Progress

- **Oldest first, a bounded batch** (32 jobs, in-flight ones included).
- **No sizing before acting.** A pool over its job count needs no sizes. Sizes
  are measured lazily, oldest first, cached for six hours, and only until a
  pool's lower bound passes its byte budget. A stale size is a scheduling input
  only: at worst a job is archived a little early, which restore reverses.
- **Slices.** A job archives for at most 120 s per pass, then parks with its
  progress. Other jobs of the batch go on.
- **Deferral.** A busy, changed or failing job is put back and skipped until its
  deferral ends, so the queue never waits on it.
- **Pacing.** The daemon runs a pass hourly; while a pass reports more waiting
  (a parked job, a full batch, undecided sizes) the next runs 5 s later
  ("retention catch-up: ..." in the daemon log). A pass that did work never
  raises `TimeoutError`; the daemon's worker pool has one more thread for it.

## 11. Restore

`subfleet retention restore <job> [--to DIR] [--repository R] [--check]`, offline:

1. Re-verify the archive: every stored file's sha256 and the bundle's heads.
2. Fetch the bundle into the source repository (or `--repository`, any clone of
   the project) under `refs/subfleet-restored/<job>/`.
3. Recreate each tree at its original path (which must not exist), or at
   `DIR/worktree`, `DIR/job`, `DIR/admin`: directories, stored files (cloned
   back), omitted files from their blobs (each checked against its id),
   symlinks, hard links and FIFOs; then modes and mtimes, directories last.
   Restored to its original place, the admin directory re-registers the
   worktree (without retention's lock), with its HEAD, index and reflogs.

Without Subfleet: `manifest.json` lists every entry; `files/<name>` holds each
stored file; `git fetch <archive>/commits.bundle '+refs/*:refs/restored/*'` in
any clone of the project brings back every commit (a bundle whose repository
had no network remote has no prerequisites and fetches into an empty
repository); `git cat-file blob <id>` there gives each omitted file.
`subfleet retention archives [--json]` lists archives; `subfleet retention
survey [--json]` is a read-only dry run of a pass over the live state.

## 12. Invariants

Each is tested (section 14).

- **I1, nothing lost.** Restoring gives back every entry that existed at
  archive time in the worktree, the job directory and the admin directory:
  path, type, permission bits, bytes, link target, hard-link grouping, mtime.
- **I2, delete only what is archived.** An entry is unlinked only if the
  verified manifest lists it with a signature equal to its `lstat` just before
  the unlink; directories only with `rmdir`. Anything created or changed after
  archiving stays, in conflicts.
- **I3, commits kept.** Every commit and staged blob the worktree reached
  before retirement is in the bundle or reachable from a network remote's
  refs, so a fresh clone of the remote plus the bundle holds all of them. No
  ref is ever moved or deleted.
- **I4, confinement.** Retention modifies only the job's paths under the state
  root, the admin directory it archived, and `refs/subfleet-archive/<job>/`.
- **I5, atomic by rows.** While a job's rows exist, none of its bytes has been
  deleted; any interruption before commit is resumed or undone; after commit,
  deletion resumes from the journal until done.
- **I6, pins.** A job pinned at selection or at commit keeps its rows and its
  trees at their original paths.
- **I7, quiescence.** No commit while a process holds a cwd, root, directory
  descriptor, writable descriptor or mapping in the job's trees; a failed
  listing counts as busy.
- **I8, verified before deletion.** No deletion is authorized by an archive
  whose stored files, bundle or omitted blobs did not read back.
- **I9, progress.** Every pass retires, parks with progress, measures, or
  defers each candidate with a reason; none waits on another.
- **I10, idempotence.** The anchor for the same inputs is the same commit, and
  every step can be run again after a crash.

## 13. Residual risks (accepted by the d635 ruling)

- A write in the microseconds between an entry's final signature check and its
  unlink.
- A writable descriptor passed over a Unix socket and held by no process at the
  moment of a listing, or any other deliberately adversarial same-user trick;
  processes of other users are invisible to a non-root `lsof` (worktrees are
  0700).
- A remote-tracking ref whose commit the remote itself later dropped (a
  force-push, then the server's gc): such a commit counted as held. The source
  repository keeps its objects while it exists.
- Extended attributes, ACLs and file flags are not in the manifest; a clone
  keeps them, a byte copy and a restore do not.
- Archives are never deleted automatically. With clones, an archive costs the
  blocks of the files it keeps (they would otherwise have been freed); `retention
  archives` lists them, and removing one is `rm -r <state>/archive/<job>` plus
  its `refs/subfleet-archive/<job>/*`.

## 14. Tests

`tests/unit/test_retention_archive.py` (real git repositories, a fake clock and
a fake process listing), `test_retention_archive_properties.py` (Hypothesis),
and the rewritten `test_retention_worktrees.py`, `test_timers_retention.py` and
`test_policy_support.py`. One regression for each d635 item:

| d635 item | Tests |
|---|---|
| Round trip | `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_bundle_alone_restores_the_commits_in_a_fresh_clone`, both properties |
| Nothing depends on an uncontrolled repository | `test_scratch_source_clone_deleted_after_retirement_is_still_restorable`, `test_clone_of_a_local_repository_carries_its_whole_history`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch`, `test_temporary_directory_source_is_scratch_by_default` |
| Omission hashes the whole object | `test_corrupted_loose_object_is_not_trusted_for_omission[wrong-bytes, truncated]`, `test_object_reader_hashes_every_byte` |
| Anchor idempotent | `test_anchor_is_deterministic_and_create_is_idempotent`, `test_rollback_after_anchoring_then_next_pass_retires` |
| Every admin-held commit anchored | `test_every_commit_only_the_admin_directory_names_survives_gc`, `test_missing_worktree_with_a_live_registration_is_anchored` |
| Nothing written after the final check deleted | `test_file_written_after_the_final_check_is_kept_in_conflicts`, `test_new_file_before_commit_rolls_back_the_job`, `test_a_swapped_directory_sends_nothing_outside` |
| Slow or interrupted checks defer, no livelock | `test_a_slow_archive_parks_while_other_jobs_retire_in_the_same_pass`, `test_a_busy_oldest_job_does_not_block_the_queue`, `test_a_failed_process_listing_defers_the_batch`, `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` |
| Staged, resumable removal | `test_interrupted_removal_resumes_and_leaves_no_half_tree`, `test_a_crash_at_any_step_is_recovered_by_the_next_pass[7 steps]`, `test_an_entry_deletion_cannot_remove_is_set_aside_not_left_half_deleted` |
| No repository-wide prune | `test_retention_never_prunes_other_registrations`, `test_discarding_a_broken_allocation_removes_only_its_own_registration` |
| Every pin | `test_every_pin_keeps_its_job[16 pins]`, `test_pinned_at_commit_rolls_back_then_retires_when_unpinned`, `test_resume_fence_and_retention_exclude_each_other`, `test_in_place_salvage_still_pins`, `test_salvage_is_bundled_and_its_ref_kept` |
| Progress under load | `test_batch_bounds_a_pass_and_takes_the_oldest_first`, `test_a_pool_over_its_count_is_pruned_without_sizing_everything` |
