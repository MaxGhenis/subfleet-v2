# Job retention by archive: design

Revision 1, 2026-09-28. Replaces the proof-based retention of rounds 1 to 4
(`fix/retention-progress` 530d7bcb, `fix/retention-r2` 8da2bdf7 and 22014743,
`fix/retention-r4` d1579deb). None of those reached `release/217`. The binding
clauses are C-8.4 and C-13.4 in `docs/acceptance-contract.md`.

## 1. Decision

Retention never decides whether a job's files are valuable. Before it deletes
anything, it writes every byte of the job's allocated worktree and job
directory into a compressed archive. It then verifies the archive by reading it
back. It deletes an entry only if the archive holds that entry and the entry is
unchanged since it was archived. The job's Git registration is locked, never
removed, so Git itself keeps every commit, reflog entry and staged blob the
worktree referred to.

There is one exception to "every byte". A tracked file is left out of the
archive when its raw bytes hash to the blob at the same path in HEAD's tree. The
archive records that blob id, and a ref that retention creates keeps HEAD's
commit reachable. Equality is checked on the file's actual bytes, not on Git's
stat cache, filters or index.

So deletion is safe by construction, not by proof: nothing needs to be shown to
exist elsewhere, because retention first puts it somewhere else. It is option
(c) from the brief. It combines (a), archive before delete, with (b)'s
safeguards, quiescence and re-validated signatures. Section 3 compares the
options.

## 2. Why the proof approach failed

Rounds 1 to 4 tried to prove, before `git worktree remove`, that nothing in the
tree existed only there. Each round closed the previous round's findings and
drew new High findings, because each proof leaned on a model of Git or the
filesystem that some case contradicted:

| Round | High findings |
|---|---|
| 1 (review of 530d7bcb) | Detached-HEAD commits; ignored output; nested repositories and worktrees; a killed `worktree remove` wedging the job |
| 2 (hard and standard reviews) | An unheld baseline; the cache allow-list (a microcosm `.h5` under a real `build/` package); staged-only content; repository-wide `worktree prune` |
| 3 | Stat-cache reuse; edits inside `node_modules` and `.venv`; lossy clean filters; edits during the proof; a symlink race in deletion; a reused registration; commits held only by `refs/worktree` |
| 4 | Writes after the final check by a process whose cwd is inside the tree; commits held only by the reflog or `ORIG_HEAD`; an interrupted check livelocking the queue |

The pattern is that every new kind of state (another ref namespace, another
Git setting, another writer) needed another rule. An archive needs no rules
about what the state means. It only has to copy bytes faithfully and to delete
only bytes it copied.

## 3. Options

**(a) Archive, then delete.** Safe for every class of content at once, and
reversible. It costs disk: section 9 estimates archives at about 15% of the
tree bytes they replace. It still needs (b)'s safeguards. A process writing into
the tree after the archive is taken would otherwise lose that write.

**(b) Delete only a quiesced, re-validated tree.** Making the tree read-only
and checking with `lsof` that no process has a cwd or open file inside it stops
post-check writes. It says nothing about *what* is deleted, though, so it still
needs the proof that failed four times. On its own, it is not enough.

**(c) Hybrid: (a) with (b)'s quiescence and signatures.** Chosen. The
archive removes the need for any proof about content. Quiescence and the
per-entry signatures guarantee that the archive is complete for the tree that
is actually deleted.

One piece of (b) is deliberately left out: making the tree read-only.
- New entries are already safe without it: deletion removes directories only
  with `rmdir`, never recursively, so any entry the archive does not list is
  never deleted.
- It cannot stop writes through descriptors that are already open. Only the
  holder check (section 5.4) addresses those.
- It changes the modes being archived, which would need a journal of original
  modes to undo on rollback.

## 4. Scope

**In scope:**
- The allocated worktree of a `workspace-write`, not-in-place job:
  `<state>/worktrees/<job id>`, owned under C-13.4.
- The job directory `<state>/jobs/<job id>`, for every job.
- The job's rows in `jobs`, `attempts`, `artifacts`, `readings`, `notices` and
  `decisions`. These are deleted, as today, after being written to the archive
  as JSON.

**Never touched:**
- An in-place job's workdir, or any path outside `<state>`.
- The job's `-o` output.
- Salvage refs.
- Events.
- The conversation store.
- Orphan directories under `<state>/worktrees` that no job row owns.
- The source repository. Retention adds exactly two things there, and neither
  removes anything:
  - a `locked` file in the job's own worktree registration;
  - a ref `refs/subfleet-archive/<job id>/<commit>`.

Resume jobs run in place in their parent's worktree (read-only survey: 6 such
pairs live). The parent owns the tree. The parent is pinned while any child row
exists. A resume submitted while its parent is being retired is refused (5.8).

## 5. Protocol

### 5.1 Layout

```
<state>/retention/<job>/journal.json      the retirement's journal, written before each step
<state>/retention/<job>/worktree/         the renamed worktree (quarantine)
<state>/retention/<job>/job/              the renamed job directory
<state>/retention/<job>/archive.partial/  the archive while it is written
<state>/archive/<job>/                    the verified archive:
    manifest.json   every entry: path, type, mode, size, mtime, dev, ino, ctime,
                    sha256 (archived) or blob id (omitted), link target, hard-link group
    worktree.tar.zst, job.tar.zst         PAX tar, zstd level 3 (Python 3.14 `compression.zstd`)
    rows.json       the job's database rows, as deleted
<state>/retention-conflicts/<job>/        anything deletion refused to delete (5.7)
```

The quarantine and archive directories are on the same filesystem as
`<state>/worktrees` and `<state>/jobs`. A rename that would cross devices is
refused before anything moves.

### 5.2 Steps

Each step's intent is written to the journal (temp file, fsync, rename)
before its effect. "Roll back" is defined in 5.6.

1. **Select** (transaction `retention.selected`).
   - Re-check pins (5.8) inside the transaction.
   - Take `worktree:<realpath>` (owned worktree only) and `retire:<job>`, both
     held by `retention:<job>`.
   - As today, no filesystem work happens inside the transaction.
2. **Lock the registration** (owned worktree only).
   - Read `<tree>/.git`, a `gitdir:` file, to find the admin directory.
   - Lock only if the admin directory's `gitdir` backlink names this tree,
     so we never lock someone else's registration.
   - If there is no `locked` file, write one whose text names the job and
     the archive. The journal records that retention wrote it, so that
     rollback removes only a lock that retention wrote.
   - This comes before the rename: once the directory is gone, an unlocked
     registration is prunable by any `git worktree prune` or `git gc`.
3. **Move into quarantine.** Rename the worktree to `retention/<job>/worktree`
   and the job directory to `retention/<job>/job`. After this no process can
   reach either tree by path, except through the quarantine path, which only
   retention uses.
4. **Quiesce, check 1.** Every process that can still write into the trees
   must hold a handle it had before the rename. Retention lists the handles of
   all processes (5.4).
   - Any cwd, root, directory descriptor, writable descriptor or mapping
     inside either tree makes the job busy.
   - Retention re-checks up to 3 times over about 10 s, then rolls back and
     defers the job for an hour.
5. **Archive.** Walk both trees with descriptor-relative calls (`openat` with
   `O_NOFOLLOW`), in sorted byte order.
   - For every entry, record its `lstat` signature in the manifest.
   - Stream each regular file into the tar while computing its sha256.
   - Re-`fstat` the file afterwards. A change in size, mtime or ctime while
     it was read fails the attempt: the tree is not quiet, so roll back.
   - Symlinks are stored as links and never followed. FIFOs and sockets are
     recorded in the manifest with no content.
   - An entry on another device (a mount point) or an unreadable entry fails
     the attempt. Retention rolls back and defers the job for 24 h with the
     reason.
   - **Omission:** a regular file of the owned tree is left out if its raw
     bytes hash (`blob <n>\0…`, in the repository's object format) to the
     blob at the same path in the tree of HEAD's commit C. This applies only
     when the admin directory is ours and the object store is outside the
     tree. The manifest records the blob id and the file's actual mode and
     mtime.
6. **Verify.** Fsync the archive files and directory. Read the tars back, and
   check each member's name, type, size, mode, link target and sha256 against
   the manifest. Check with `git cat-file --batch-check` that every omitted
   blob exists. Then rename `archive.partial` to `<state>/archive/<job>` and
   fsync the parent.
7. **Anchor** (only if any file was omitted). Create
   `refs/subfleet-archive/<job>/<C>`, pointing at C, with `git update-ref`
   (create-only). If this fails, roll back.
8. **Quiesce, check 2.** The same holder check, matching also on the
   manifest's (dev, ino) set. Busy means roll back.
9. **Commit** (transaction `retention.pruned`).
   - Write `rows.json` just before the transaction.
   - Inside it, re-check pins and that the leases are still ours. If anything
     is pinned, roll back after the transaction.
   - Otherwise delete the rows and the leases, and record the event: bytes,
     pool, archive path and size, omitted bytes.
   - This is the point of no return. **While the job's rows exist, no byte of
     its trees has been deleted.**
10. **Verified deletion** (5.7), then remove the journal and the quarantine
    directory.

The pass deadline (60 s) is checked only between steps 1 and 2. A retirement
that has started runs to the end. Only cancellation (daemon shutdown) or its
own time cap stops it early; the cap is `max_archive_s`, default 1800 s.
Either way it rolls back (5.6). A job that hits the cap is deferred for 24 h
with the reason `archive-too-slow`.

### 5.3 What the archive covers

| Where work can live | How it survives retirement |
|---|---|
| Tracked files, modified or not | Archived. Unmodified ones are omitted only when their raw bytes equal the anchored blob |
| Untracked and ignored files, including `build/`, `node_modules`, `.venv` and data | Archived |
| Staged content | The index stays in the locked admin directory, and Git keeps its blobs reachable (E1) |
| Detached HEAD, reflog-only, `ORIG_HEAD`, `refs/worktree/*` and rebase state | The locked admin directory stays intact, and gc treats it as reachable (E1). HEAD's commit is also anchored |
| Nested repositories (`.git` directories, bare repositories) | Archived byte for byte, including their object stores and their own worktrees' admin directories |
| Nested linked worktrees (a `.git` file inside the tree) | Archived. Their registration, in another repository, is locked like our own when its backlink names the nested path (5.5) |
| The job directory: logs, prompts, attempt records | Archived |
| The job's database rows | `rows.json` in the archive |

### 5.4 Holder check

On macOS it runs `lsof -n -P -w -F pcftaDin` once over all processes; on the
reference machine that took 1.8 s for 219k lines (E3). On Linux it reads
`/proc/*/{cwd,root,fd,fdinfo,maps}`.

A process is a holder if it has any of these:
- a cwd or root inside the watched paths;
- a text or memory mapping of a watched file;
- any descriptor on a watched directory;
- a descriptor opened for writing on a watched file.

"Inside" is matched two ways: by path prefix (the quarantine path and the
original path), and at check 2 also by (device, inode) against the manifest.

A read-only descriptor on a regular file is not a holder: it cannot change the
file, and after deletion its reader keeps reading the unlinked inode.

The check fails closed. If `lsof` is missing, errors or times out, the job
counts as busy. E3 confirmed on this machine that `lsof` reports the
post-rename path and inode for a cwd, a write descriptor, a read descriptor and
a directory descriptor.

### 5.5 Git handling

Retention runs three Git commands, with `--git-dir=<admin>`, so no worktree is
needed:
- `rev-parse --verify HEAD^{commit}`, then `rev-parse --show-object-format`;
- `ls-tree -r -z --full-tree <C>`;
- `cat-file --batch-check`.

Its only writes are `update-ref` (create-only) and the `locked` file.

It never runs:
- `worktree remove` or `worktree prune`;
- `git gc`;
- anything else that deletes.

E1, run with Git 2.55 (script in the review folder):
1. In a linked worktree: make detached commits U1 and U2; put U3 only in the
   reflog; stage a blob only in the index; set and delete a `refs/worktree`
   ref.
2. Move the directory away.
3. Run `worktree prune` and `gc --prune=now` with `gc.worktreePruneExpire=now`.

With the admin directory locked, all four objects survived and the
registration was listed as locked. Without the lock, all four were lost and
the registration was pruned.

A nested gitfile inside the tree is handled the same way as the root's. If its
admin directory's backlink names the nested path, retention locks that
registration too. That covers a nested linked worktree whose admin lives in
another repository, the 2026-09-23 `yale-campaign-full-v2` case. Locking is
additive and reversible; `git worktree unlock` or restore undoes it.

### 5.6 Rollback and recovery

Every pass starts by reading each `<state>/retention/<job>/journal.json`.

| Journal | Job rows | Action |
|---|---|---|
| any step before commit | present | Roll back |
| archive verified | present | Roll back, then delete the verified archive (the trees are back) |
| any | absent, verified archive present | Continue with verified deletion (5.7) |
| any | absent, no verified archive | Keep the quarantine as a conflict. Never delete. Emit an event |

**Roll back:**
1. Rename the trees back to their original paths, if those paths are free. An
   occupied path moves our tree to `retention-conflicts/`; nothing is deleted.
2. Remove the `locked` file, only if the journal says retention wrote it and
   its text is ours.
3. Remove `archive.partial`, a copy of trees that are back in place.
4. Release the job's leases.
5. Defer the job.

A `retention:` lease with no journal is a legacy lease; the installed code
holds one only if it was killed mid-removal. Recovery releases it, and the job
is retired normally from whatever remains. The live store had no `retention:`
leases on 2026-09-28 (read-only query).

Anchor refs are never deleted by retention, including on rollback. A
rolled-back attempt can leave one harmless extra ref. Keeping it is safer than
deleting a ref whose commit may since have lost its other references.

### 5.7 Verified deletion

The walk is descriptor-relative and post-order. Each directory is opened with
`O_DIRECTORY | O_NOFOLLOW` and its (dev, ino) checked against the manifest.
For every entry actually present:

- If the manifest does not list it, keep it.
- If its `lstat` signature (type, mode, ino, size, mtime_ns, ctime_ns)
  differs from the manifest's, keep it.
- Otherwise unlink it, or recurse and `rmdir` it. An `rmdir` that fails
  because the directory is not empty keeps the directory.

Two details:
- **Hard links.** Unlinking one link changes the inode's ctime for the other
  links (verified on APFS). So the deleter opens the inode first, unlinks, and
  takes the new ctime from `fstat` as the expected value for the remaining
  links.
- **Directories without owner write permission** (Go's module cache uses
  0555) get `fchmod` u+w after their signature check and before their entries
  are removed.

If anything is kept, the quarantine directory is renamed to
`<state>/retention-conflicts/<job>`. A `retention.conflict` event names the
kept paths (up to 50) and the daemon log says so. Nothing there is ever
deleted automatically.

Deletion is idempotent. An interrupted deletion resumes at the next pass from
the journal, because entries already gone are simply absent.

### 5.8 Pins

Unchanged from `release/217`:
- non-terminal jobs, gate reviews, quarantined or live attempts;
- unread notices addressed to a session;
- parents of any job;
- job and attempt leases;
- another holder's `worktree:` lease;
- gate or merge evidence;
- the conversation service's pins;
- `turn_keep_days`.

Changed:
- **Salvage no longer pins.** An unlanded salvage ref used to pin its job
  forever, because deletion depended on the ref matching the tree. Retention
  never deletes refs, and the tree is archived, so the pin protects nothing.
  The daemon never supplied `salvage_referenced_elsewhere`, so on
  `release/217` every salvage-bearing job was pinned.
- **Resume refuses a source that holds a `retire:` lease**, with the message
  "being archived by retention; retry in a minute". Before, a resume submitted
  mid-retirement read a quarantined job directory and got a degraded manifest.
  A writable resume was already refused by the `worktree:` lease. The child
  pins its parent in any case, so the commit transaction rolls the parent back.

### 5.9 Restore

`subfleet retention restore <job> [--to DIR] [--check]` runs offline and
touches only its destination.

1. Re-verify the archive: sha256 of every member, and that every omitted blob
   exists.
2. Extract `worktree.tar.zst`. The destination is the original worktree path,
   or `DIR/worktree`, and it must not exist.
3. Write each omitted file from its blob with `git cat-file blob`, checking
   the blob hash of what was written.
4. Apply the manifest's modes and mtimes, directories last.
5. Extract the job directory to `<state>/jobs/<job>`, or `DIR/job`, if
   absent. It comes back as an orphan directory: its rows are not re-inserted.
6. If restored to the original path, remove retention's `locked` file.

`--check` does step 1 only. `subfleet retention archives [--json]` lists
archives with their dates, original paths, bytes, archive bytes and omitted
bytes.

## 6. Invariants

Each is tested by property-based tests (Hypothesis) and by the probes in
section 10.

- **I1, nothing lost.** For every retired job, restore reproduces every entry
  that existed at archive time in the owned worktree and the job directory.
  That means the same path, type, permission bits, content bytes, symlink
  target, hard-link grouping and mtime.
- **I2, delete only what is archived.** Retention unlinks an entry only if
  the verified manifest lists it with a signature equal to its `lstat` at
  unlink time. It removes directories only with `rmdir`. Any entry created or
  changed after archiving remains on disk.
- **I3, Git state kept.** Retention never removes or rewrites a worktree
  registration; it only adds `locked`. It never deletes a ref, so every
  object reachable before retirement stays reachable. The commit whose blobs
  were omitted is reachable from a ref retention created.
- **I4, confinement.** Retention modifies nothing except:
  - `<state>/worktrees/<job>`, `<state>/jobs/<job>`,
    `<state>/retention/<job>`, `<state>/archive/<job>` and
    `<state>/retention-conflicts/<job>`;
  - the `locked` files of 5.2 and 5.5, and `refs/subfleet-archive/<job>/*`.

  Symlinks, including ones swapped in during the pass, are never followed.
- **I5, atomic by rows.** While a job's rows exist, none of its bytes has been
  deleted. An interruption at any point before commit is rolled back by the
  next pass, with the trees at their original paths. After commit, the next
  pass finishes deletion from the verified archive.
- **I6, pins.** A job pinned at selection or at commit keeps its rows and has
  its trees back at their original paths.
- **I7, quiescence.** No commit happens while any process holds a cwd, root,
  directory descriptor, writable descriptor or mapping inside the trees. A
  failed holder scan counts as busy.
- **I8, verified before deletion.** No deletion is authorized by an archive
  that failed read-back verification, whether truncated, bit-flipped or
  missing a member.
- **I9, progress.** A pass that is over budget does at least one of these:
  - archives a job;
  - measures a size it did not have;
  - finishes a deletion;
  - reports a reason for every candidate it skipped.

  A busy or failed candidate is deferred, so the next candidate gets its
  turn; round 4's livelock cannot recur. A pass that makes progress never
  raises `TimeoutError`.
- **I10, accounting.** For each pool, `bytes_after` equals `bytes_before`
  minus the bytes the archive walks measured for the jobs retired. `jobs_after`
  equals `jobs_before` minus the number retired.

## 7. Progress, sizes and the timeout

The installed release sizes every job before pruning, under a 60 s deadline,
and times out on every pass.

In the new design:
- **Sizes are measured lazily and cached for the daemon's lifetime.** A walk
  of a large tree resumes where the last pass stopped. The pass prunes as soon
  as the measured lower bound exceeds the budget, or the job count does.
- **Stale sizes cannot cause loss.** At worst a job is archived a little
  earlier than needed, which restore reverses. They are never a safety input.
- **Candidates are taken oldest first.** A deferred candidate (busy,
  unreadable, too slow, pinned) is skipped until its deferral expires.
- **The daemon's `_retention`:**
  - a pass interrupted after progress logs "retention catch-up: archived N
    jobs, measured M; continuing in 5 seconds" and runs again in 5 s;
  - only an interrupted pass that did nothing raises `TimeoutError`;
  - a completed pass rearms the hourly timer.

## 8. Threat model and residual risk

The adversary is accidental, not malicious: a background process left behind
by a job, an agent that cds into an old worktree, a user's `git gc`. These
cases remain:

- **Processes of other users, including root, are invisible to a non-root
  `lsof`.** Owned worktrees are mode 0700 (C-13.4 allocation), so only root
  can write there, and root bypasses a read-only tree as well.
- **A holder that `lsof` misses** (for example, a fork racing the scan) and
  that then writes between an entry's signature check and its unlink. The
  window is one system call. Check 2 runs minutes after check 1, and the
  per-file re-`fstat` during archiving catches writers active while the
  archive is taken.
- **Extended attributes, ACLs and BSD file flags are not archived.** Python
  has no `listxattr` on macOS. The ones seen on job trees (quarantine,
  provenance) carry no job output. A `uchg` flag makes unlink fail, which
  keeps the file as a conflict.
- **SHA-1 blob collisions** would let a file be omitted wrongly. Git's own
  collision detection guards the objects; a sha256 repository uses sha256.
- **The source repository is deleted** (for example, a `/tmp` clone). The
  omitted tracked files then go with it, and so does its history. This is no
  worse than keeping the tree: its `.git` file would point at nothing, and its
  untracked content is in the archive.
- **Spotlight's `mdworker` (a same-user process) opening files.** It holds
  read-only descriptors, which are not holders. A directory descriptor it
  holds makes the job busy for a retry.

## 9. Disk

Measured read-only on 2026-09-28 over a random sample of 24 of the 279 live
worktrees:
- 9.22 GB in total, of which 4.85 GB (53%) is in the index;
- the other 4.37 GB compresses with zstd level 3 to about 1.38 GB;
- so an archive is about **15% of the tree**, and retention reclaims about
  85%. "In the index" is an upper bound on omission; a modified tracked file
  is archived.

The live job directories hold 2.0 GB of mostly text logs.

Archives are never deleted by the daemon; deleting one is deleting data that
may exist nowhere else. At 90 allocated worktrees a day (the rate of
2026-09-27 and 2026-09-28), archives would grow about 5 GB a day. Leaving the
trees would grow about 35 GB a day. 215 GiB were free on 2026-09-28.

- **Notice.** When archives exceed `ARCHIVE_NOTICE_BYTES` (25 GiB), the pass
  writes a service notice to the operator session, at most once a day. It
  gives the total and how to remove archives a person no longer wants
  (`rm -rf <state>/archive/<job>` and the matching `refs/subfleet-archive/<job>`).
- **Not in this change:**
  - a cross-job content store: many worktrees carry identical `.venv` or
    `node_modules` files, so this would cut archive growth several-fold;
  - a `purge` verb.

  Both are follow-ups. Neither changes the safety argument.

## 10. Every prior finding

"Probe" names the test in `tests/unit/test_retention_archive_probes.py`. Each
probe builds the finding's scenario, runs retention, and passes only if the
defect is absent: the content is restorable byte for byte, or retention
correctly refuses.

| Finding | Why it cannot happen now | Probe |
|---|---|---|
| R1-1a detached-HEAD commits | Registration locked, HEAD anchored | `test_r1_1a_detached_commits_survive_gc` |
| R1-1b ignored files | Archived | `test_r1_1b_ignored_output_restores` |
| R1-1c nested repositories and worktrees | Archived; a nested registration is locked | `test_r1_1c_nested_repo_and_nested_worktree` |
| R1-2 killed `worktree remove` wedges | No `worktree remove`; the rename is atomic and deletion is resumable | `test_r1_2_crash_at_every_step_recovers` |
| R1-3 interrupted passes orphan leases | Journal recovery releases leases | `test_r1_3_interrupted_pass_releases_leases` |
| R1-4 stale sizes | Sizes are not a safety input; loss is impossible | `test_r1_4_stale_size_loses_nothing` |
| R1-5 permanent demotion | Deferrals expire (1 h or 24 h) | `test_r1_5_deferral_expires` |
| R1-6 prunable ref namespaces | No proof uses refs; retention never deletes refs | `test_r1_6_salvage_refs_untouched` |
| R1-7 missing worktree | Archives the job directory only | `test_r1_7_missing_worktree_is_retired` |
| R1-8 surviving mutants | The mutation run is repeated on the new code (section 11) | — |
| R1-9 progress logged as `TimeoutError` | Section 7 | `test_r1_9_progress_is_not_timeout` |
| R2h-1 unheld baseline | HEAD anchored; nothing depends on the baseline | `test_r2h_1_unheld_baseline_restorable` |
| R2h-2, R2s-1 `build/`, `dist/`, microcosm `.h5` | Archived; there is no allow-list | `test_r2_microcosm_h5_under_build_package` |
| R2h-3 staged-only content | Index kept in the locked admin directory | `test_r2h_3_staged_only_blob_survives_gc` |
| R2h-4, R2s-2 repository-wide prune | No prune of any kind | `test_r2_no_prune_unrelated_registration_intact` |
| R2h-5 stale sizes after an interrupt | As R1-4 | `test_r1_4_stale_size_loses_nothing` |
| R2h-6, R2s-3, R4-6 legacy waivers | No waivers exist; a legacy lease is released and the job archived | `test_r4_6_legacy_lease_released_and_archived` |
| R2s-4 largest trees never finish | A started retirement runs to the end; deferral on the time cap | `test_r2s_4_long_archive_is_not_interrupted_by_deadline` |
| R2s-5, R4-5 read-only or deep trash | Iterative deleter, 0555 directories, conflicts kept | `test_r4_5_readonly_and_deep_trees` |
| R2s-6 restore wedge | An occupied path goes to conflicts | `test_r2s_6_rollback_collision_keeps_both` |
| R2s-7 `.GIT` | Archived byte for byte | `test_r2s_7_uppercase_git_dir_archived` |
| R3-1 stat cache | Omission hashes raw bytes | `test_r3_1_same_size_old_mtime_edit_is_archived` |
| R3-2 `node_modules` and `.venv` edits | Archived | `test_r3_2_node_modules_edit_restores` |
| R3-3 lossy clean filters | Raw bytes, no filters | `test_r3_3_clean_filter_hidden_edit_is_archived` |
| R3-4 edits during the proof | Holder check, re-`fstat`, signatures | `test_r3_4_edit_during_archive_rolls_back_or_is_kept` |
| R3-5 symlink race in deletion | Descriptor-relative, `O_NOFOLLOW`, (dev, ino) checks | `test_r3_5_symlink_swap_cannot_escape` |
| R3-6 reused registration | Registrations are never removed; backlink checked before locking | `test_r3_6_reused_registration_untouched` |
| R3-7 `refs/worktree` | Admin directory kept and locked | `test_r3_7_worktree_ref_commit_survives_gc` |
| R3-8 size expiry livelock | No expiry; lower-bound pruning | `test_r3_8_many_jobs_reach_budget` |
| R3-9 stale-size window | As R1-4 | `test_r1_4_stale_size_loses_nothing` |
| R4-1 writes after the final check | Rename, holder checks, signatures, `rmdir` only | `test_r4_1_background_writer_blocks_or_is_kept` |
| R4-2 reflog and `ORIG_HEAD` | Admin directory kept and locked | `test_r4_2_reflog_and_orig_head_survive_gc` |
| R4-3 interrupted check livelock | Deferral; a started retirement is not cut by the deadline | `test_r4_3_busy_oldest_does_not_block_queue` |
| R4-4 per-file size evidence | Only per-job totals are kept | covered by `test_r3_8_many_jobs_reach_budget` |

The probe files in `~/reviews/retention-2026-09-28/review-probes/` target the
removed proof API, for example `SalvageReachability`. Their scenarios are
carried into the file above, with the polarity corrected.

## 11. Test plan

- **Properties (Hypothesis).**
  - Generated trees: files, empty directories, symlinks to inside and
    outside, hard links, FIFOs, modes including 0555 and 0600, non-ASCII
    names, and tracked, modified, untracked and ignored content with staged
    and detached commits.
  - Checked: I1 round trip, I3 gc reachability, I4 by a sentinel outside the
    tree and a symlink swapped in mid-pass, I5 crash injection at every step
    followed by recovery, and I2 random mutations injected between steps.
- **The probe table in section 10.**
- **Unchanged contracts.** The existing pool, turn and pin tests
  (`test_retention_turns.py`, `test_policy_support.py`,
  `test_timers_retention.py`, `tests/fake/test_resume_contract.py`) still
  pass. Tests whose expectations change (the registration now remains,
  locked) are rewritten against I1 to I10.
- **Mutation run.** Deleting each guard must fail a test: the holder check,
  the signature check, the re-`fstat`, verification, the lock, the anchor and
  the pin re-check.
- **Live survey, read-only** (`GIT_OPTIONAL_LOCKS=0`, SQLite `mode=ro`). For
  every owned worktree, report what it would archive and omit, and what makes
  it busy, without writing anything.

## 12. Contract changes

- **C-8.4.**
  - Pruning archives, then deletes (sections 5 and 6).
  - Salvage no longer pins.
  - Archives are kept until a person removes them.
  - A service notice fires above 25 GiB.
- **C-13.4.** An allocated worktree is removed only by retention's verified
  deletion. Its registration is locked, not removed.
- **C-17.1.** New verbs: `retention archives [--json]` and
  `retention restore <job> [--to DIR] [--check]`.
- **C-3.3.** Unchanged: no subprocess or filesystem work inside a
  transaction.

## 13. Experiments cited

- **E1, locked registration survives gc.** Git 2.55.0, macOS.
  `~/reviews/retention-2026-09-28/archive-design/e1_git_lock.sh`: all four
  objects kept with the lock, all four lost without it.
- **E3, `lsof` after rename.** `/usr/sbin/lsof` on macOS 25.6. After the
  rename it reported the new path and the inode for a cwd, a write
  descriptor, a read descriptor and a directory descriptor. The full listing
  took 1.76 s.
- **Filesystem facts (APFS).**
  - Reading a file leaves its ctime unchanged.
  - Unlinking one hard link changes the other link's ctime.
  - Renaming a directory changes its own ctime.
  - `open`, `stat`, `unlink` and `rmdir` all support `dir_fd`, and
    `os.scandir` accepts a descriptor.
- **Python.** The daemon's Python is 3.14.4, with `compression.zstd`
  (libzstd 1.5.7) and `tarfile` mode `w:zst`.
- **Size sample.** `size_sample.py`, as summarized in section 9.
