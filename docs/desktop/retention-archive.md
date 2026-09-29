# Job retention without deleting unpreserved work: design

Revision 3, 2026-09-29. Binding clauses: C-8.4, C-13.4 and C-17.1 in
`docs/acceptance-contract.md`.

| Revision | What it did | Outcome |
|---|---|---|
| 1 (2026-09-28, ccc85387) | Subfleet archived worktrees itself | Two independent design reviews: CHANGES NEEDED |
| 2 (2026-09-29, 2531e22c) | Subfleet ran the machine's worktree archiver for its candidates | One more design review: CHANGES NEEDED |
| 3 (this document) | Subfleet never touches a worktree, and never runs anything for one | — |

Revision 3 prunes a job's records only once the machine's archiver has taken
its worktree. It keeps only the part that was sound in every revision: a job
directory is archived, verified, and deleted entry by entry.

The reviews are in `~/reviews/retention-2026-09-28/archive-design/`:
- `review-design-astra.md` and `review-design-opus.md` review revision 1;
- the revision 2 review is summarized in section 8.

This design replaces the proof-based retention rounds 1 to 4
(`fix/retention-progress`, `fix/retention-r2`, `fix/retention-r4`). None of
them reached `release/217`.

## 1. Decision

**Retention never deletes, moves, writes or runs anything for a worktree.**

Allocated worktrees are reclaimed by the machine's worktree archiver, which
preserves local-only work before it removes a tree. On Max's machine that is
chief-of-staff's tools:
- `disk-guard` runs from launchd and removes only clean, pushed trees;
- `worktree-archive-sweep` handles the rest, and is run by hand.

Retention prunes a job that owns a worktree only once the worktree is **gone
for real**:

1. no entry under `<state>/worktrees/` is the tree, or a copy set aside under a
   suffixed name (`.disk-guard-removing.<name>`, the archiver's quarantine);
2. the job's repository has no existing checkout registered for that tree,
   matched by the registration's name, which `git worktree add` gives the job
   and `git worktree move` never changes, or by the checkout's name;
3. if that repository exists but cannot be read, or the check would run past
   its share of the pass deadline, the tree counts as present for now.

A job whose tree is present is kept, with the reason reported under `kept`.
The check is repeated freshly just before each commit. It checks the name,
then the quarantine name, then the name again, so a single rename between two
reads cannot hide the tree.

**Subfleet archives its own records before deleting them.** For a job
directory and its rows, retention:
1. writes a compressed tar, a manifest and `rows.json`;
2. syncs them with `F_FULLFSYNC` and verifies them by reading back;
3. deletes the rows in a transaction that re-checks the pins, its `retire:`
   lease and the rows' digest;
4. deletes only the entries the manifest lists whose signature is unchanged,
   and removes directories only by `rmdir`.

**Budgets count what retention can reclaim.** Byte budgets count job
directories. Worktrees are the archiver's and are never walked. A detached
job's record is kept at least a day after the job ends.

### Why not the earlier revisions

| Revision | Why it was dropped |
|---|---|
| 1 | Subfleet would have been a second, less proven worktree archiver. Omission depended on repositories Subfleet does not control, locked registrations slow Git badly, anchors wedged, and `lsof` cannot exclude a descriptor queued on a socket |
| 2 | Calling the archiver from Subfleet added risk for little gain (section 8): its default free-space floor made it reclaim nothing below 41 GB; its lock is contended by disk-guard about half the time; its one-day idle floor and open-session checks refuse the newest trees; and its quarantine made "path absent" an unsound test of success. The archiver already processes `~/.subfleet/worktrees` on its own |

What the archiver preserves, and how durably, is the archiver's guarantee, not
this design's. The revision 2 review raised these points about it, and they
were sent to the session that owns it (section 8):
- snapshots and commits kept only in the source repository;
- regenerable-cache exceptions;
- its quarantine.

## 2. Pass

A pass runs hourly, and 5 s after a pass that left work.

1. **Recover** (6.3). Finish any job-directory deletion whose rows are gone.
   Undo any archive whose rows remain.
2. **Classify.** For each terminal job that owns a worktree, run the "gone"
   test (section 1). If the tree is present, pin the job and give the reason.
3. **Size.** Measure job directories, terminal jobs oldest first and then live
   ones, within half the deadline. Walks resume across passes. Only a terminal
   job left unmeasured makes the pass "unfinished".
4. **Select.** In each pool, while the count or the measured bytes exceed the
   budget, take terminal jobs oldest first, skipping pinned and deferred ones.
   Take at most 200.
5. **Fence.** In a transaction, re-check the pins and take `retire:<job>`,
   held by `retention:<job>`. It is not a path lease: disk-guard treats every
   `worktree:/…` or `out:/…` lease as a tree in use.
6. **Archive the job directory** and its rows (6.1). If the job owns a worktree,
   ask the "gone" question again, freshly. If the tree is back, discard the
   archive and keep the job.
7. **Commit.** In one transaction, re-check the pins and the lease, check that
   the rows still hash to `rows.json`'s digest, then delete the rows. If
   anything differs, commit nothing and discard the archive.
8. **Delete** the job directory by verified deletion (6.2), then release
   `retire:<job>`.

No subprocess, `stat` or file write happens inside a transaction (C-3.3).

## 3. Invariants

Each is tested by property-based tests (Hypothesis) and by the probes in
section 9.

- **W1.** Retention never unlinks, renames, `chmod`s or writes inside an owned
  worktree. It never runs a Git command that changes a worktree, a
  registration or an object. It never walks a worktree.
- **W2.** A job with an owned worktree is pruned only when the tree is gone
  for real: no path, no set-aside copy, and no registered checkout that exists.
  When the repository cannot be read, the job is kept.
- **J1.** A job directory is deleted only after its archive is durable and
  verified by reading it back. Verification checks every member's name, type,
  mode, size, sha256 and link target.
- **J2.** While a job's rows exist, no byte of its job directory has been
  deleted.
- **J3.** Deletion unlinks only entries the manifest lists whose `lstat`
  signature (type, mode, inode, device, size, mtime, ctime) is unchanged.
  Directories are matched by identity only and removed only by `rmdir`.
  Anything else is kept and goes to `retention-conflicts/`.
- **J4.** Restoring a job directory reproduces every archived entry: path,
  type, permission bits, bytes, symlink target, hard-link grouping and mtime.
- **R1.** `rows.json` holds exactly the rows the commit deleted, checked by
  digest inside the deleting transaction.
- **P1.** Every pin holds at selection and again at commit. The pins are:
  - the C-8.4 pins;
  - a present worktree;
  - a detached job that ended less than a day ago;
  - a parent.
- **G1.** A pass over budget does one of these:
  - prunes a job;
  - measures a terminal size it did not have;
  - finishes a deletion;
  - reports a reason for every candidate it skipped.

  A deferral counts as progress: it moves the queue on. A deferred candidate
  does not block the next one. A pass that made progress never raises
  `TimeoutError`.
- **A1.** For each pool, `bytes_after` equals `bytes_before` minus the pruned
  jobs' sizes. `jobs_after` equals `jobs_before` minus the number pruned.
  Pruning is oldest first, and stops as soon as the pool fits.
- **D1.** Differential: a `dry_run` names exactly what a real pass then prunes,
  and writes nothing.

## 4. Pins and fences

Unchanged from `release/217`:
- non-terminal jobs, gate reviews, quarantined or live attempts;
- unread notices addressed to a session;
- parents;
- job and attempt leases;
- another holder's `worktree:` lease;
- gate or merge evidence;
- the conversation service's pins;
- `turn_keep_days`.

Changed:
- **Worktree present.** A terminal job whose owned worktree is present (W2) is
  kept.
- **One-day floor.** A detached job is kept for `MIN_AGE_S` (24 h) after it
  ends (revision 2 review, finding 5). Many jobs are pinned, so the count
  budget alone would otherwise prune records minutes after a job ends, and
  `runs show` would fail.
- **Pinned jobs count toward the budget.** They count toward the 500 as they
  did on `release/217`. On 2026-09-29, 1,222 detached jobs were pinned, 258 of
  them by their worktrees. So the pool stays over its count, and in practice an
  unpinned detached record is pruned once it is a day old. It stays
  restorable from its archive.
- **Salvage no longer pins.** Retention never touches refs or worktrees.
  `rows.json` keeps the salvage artifact's ref name. The ref stays in the
  repository.
- **Resume fence.** Resume, and every child job, is refused inside the
  transaction that inserts it (`_validate_conflicts`) while its parent holds
  `retire:`. The job directory a resume reads is deleted only after the
  parent's rows are gone and before the lease is released. So a child that
  read the directory mid-deletion can never be inserted.

## 5. Sizes, progress and the timeout

The installed release sizes every job and worktree before it prunes, within a
60 s deadline, and times out on every pass.

- **What is sized.** Only job directories: 3.8 GB on 2026-09-29, against about
  190 GB of worktrees. Sizes are cached per job: 6 h for terminal jobs, 1 h
  for live ones. A walk cut off by the deadline resumes on the next pass.
- **Pruning under a partial measurement.** While terminal jobs remain
  unmeasured, the measured sum is a lower bound. A pool that is over budget by
  that bound prunes now. The job count needs no measurement.
- **Deferrals.** 1 h when a directory changed while it was archived. 24 h
  when it cannot be archived (unreadable, on another device, or too little
  free space).
- **Daemon.**
  - A pass left unfinished after progress logs "retention catch-up: pruned N
    jobs, measured M; continuing in 5 seconds" and runs again in 5 s.
  - "Unfinished" means either of these:
    - `interrupted: deadline`: terminal sizes are left unmeasured;
    - `interrupted: capped`: more than 200 candidates were due.
  - A pass left unfinished having done nothing raises `TimeoutError`, keeping
    the existing backoff.
  - A completed pass rearms the hourly timer.
  - Live jobs never make a pass "unfinished", so catch-up cannot loop on them.

## 6. Job-directory archive

### 6.1 Writing

`<state>/archive/<job>/` holds:
- `manifest.json`: format, job, request id, original path, compression, and
  each entry's signature, sha256 and link target;
- `job.tar.zst`, a PAX tar compressed with zstd level 3, or `job.tar.gz` where
  Python's `tarfile` lacks zstd (3.12 on CI);
- `rows.json`: the job's rows from `jobs`, `attempts`, `artifacts`,
  `readings`, `notices` and `decisions`, with a sha256 of their canonical
  JSON.

How it is written:
- The archive is written as `.partial-<job>` and renamed into place only after
  verification.
- The walk is descriptor-relative (`openat` with `O_NOFOLLOW | O_NONBLOCK`)
  and never follows symlinks.
- Each file is re-`fstat`ed after it is read. A file that changed while it was
  read defers the job.
- An entry on another device, or one that cannot be read, defers the job.
- An archive that would leave less than 2 GiB free is refused.

A terminal job's directory has no writer: the provider process has been
contained, and an export holds a lease that pins the job. So there is no
`lsof`. Verified deletion (6.2) is the backstop.

### 6.2 Verified deletion

- The walk is descriptor-relative and post-order, with one directory
  descriptor open at a time.
- Each directory is opened with `O_DIRECTORY | O_NOFOLLOW` and matched by
  (type, device, inode). Each other entry is unlinked only if its signature
  equals the manifest's.
- **Hard links.** Unlinking one link changes the inode's ctime for the others.
  The deleter reads the new value through a descriptor taken before the unlink.
  After an interruption, a remaining link's changed ctime is accepted only if
  everything else matches and the drop in its link count equals the number of
  its archived paths already gone.
- **Directories without owner permissions** get `fchmod` u+rwx after their
  identity check.
- Deletion is idempotent, so an interrupted run resumes.

### 6.3 Recovery

Each `retire:` lease left by a pass is resolved as follows:

| Job rows | Archive | Action |
|---|---|---|
| present | none, partial or this request's | Discard it. The directory was never touched. Release the lease |
| absent | complete | Re-verify the archive by reading it back, run verified deletion, release the lease |
| absent | partial or missing | Keep the directory. Move it to `retention-conflicts/<job>`, emit an event, release the lease |

- An archive at the final name whose request id is not this job's is left
  alone.
- A `.partial-*` archive with no lease is discarded; the directory it copied
  is untouched.
- A legacy `retention:` holder on a `worktree:` lease, left by the installed
  code, is released. The live store had none on 2026-09-28.

### 6.4 Verbs

- `subfleet retention archives [--json]` lists each archive: job, state,
  bytes, archive bytes, and any salvage refs.
- `subfleet retention restore <job> [--to DIR] [--check]` re-verifies the
  archive and extracts it to `DIR/<job>`, or to `<state>/jobs/<job>` when that
  is free. It extracts with `filter="tar"`, then applies the modes and mtimes,
  directories last. Rows are not re-inserted.
- `subfleet retention preview [--json] [--policy FILE]` is a dry run against a
  read-only store. It lists what the next passes would prune and how many jobs
  are kept for their worktree.

Worktrees are restored with the archiver's own recipe. Archives are never
deleted by Subfleet.

## 7. Threat model and residual risk

The adversary is accidental: a leftover process, an agent in an old tree, the
archiver, a person's `git` command.

- **W1 holds whatever happens.** Retention has no code path that writes to a
  worktree.
- **W2 depends on the tree being findable.** It is found at its path, beside
  the path with a `.<name>` suffix (disk-guard's quarantine), or through a
  registration named after the job whose checkout exists (any
  `git worktree move`).
  - A plain `mv` to an unrelated name, outside Git, would make a tree look gone.
  - So would the one step inside `git worktree move` between its rename and
    its `gitdir` rewrite.
  - In either case the job is pruned while the tree still exists: nothing is
    deleted, but the record goes early.
- **A process that writes into a terminal job's directory after its archive**
  keeps its file (J3). Every file is re-checked by signature just before it is
  unlinked. The remaining window is the single `stat` to `unlink` gap for one
  file.
- **Extended attributes, ACLs and BSD flags are not archived** for job
  directories, which hold logs, prompts and deliverables.

## 8. Review dispositions

**Revision 3** (the same reviewer again, with experiments; APPROVE WITH
CHANGES, and no path found that deletes unpreserved work):

| Finding | Disposition |
|---|---|
| 1. All 200 candidates deferred raised `TimeoutError` | Fixed: deferrals count as progress, and the cap reports `capped` |
| 2. A tree moved to an unrelated name looked gone | Fixed: registrations are matched by their name |
| 3. The worktree pin was a snapshot, and `listdir` races a rename | Fixed: a fresh check just before the commit, and a name / quarantine name / name sequence |
| 4. The pool stays over its count because of pins | Documented (section 4) |
| 5a. Classification had no time bound | Fixed: it gets a share of the deadline |
| 5b. A broken linked checkout hid the repository | Fixed: the repository is found through its `.git` file |
| 5c. "Its own schedule" was inaccurate | Corrected: the sweep is run by hand |

**Revision 1** (two reviews):

| Findings | Disposition |
|---|---|
| Omission and anchors (Astra 1, 2, 7; Opus 1, 2) | Removed: Subfleet archives no worktree |
| Locks and registrations (Astra 4, 5; Opus 3, 5) | Removed: Subfleet touches no registration |
| `lsof` (Astra 3; Opus 4) | Removed |
| Rows durability (Astra 6) | Adopted |
| Sizes (Astra 8) | Adopted |
| Resume fence (Astra 9) | Adopted |
| `F_FULLFSYNC` and re-verify (Opus 6) | Adopted |
| Free-space floor (Opus 7) | Adopted |
| Interrupted deletion (Opus 8) | Adopted |
| Lows (Opus 9) | Adopted where they still apply |

**Revision 2** (one review, in-session Opus with experiments; CHANGES NEEDED):

| Finding | Disposition |
|---|---|
| 1. The archiver's 40 GB floor frees nothing below 41 GB | Moot: Subfleet no longer calls it. Sent to the archiver's owner |
| 2. The archiver keeps commits and unchanged files only in the source repository, and does not read its objects back | The archiver's guarantee; sent to its owner with the evidence. Subfleet no longer hands it trees, so revision 3 changes nothing here compared with the archiver's own schedule |
| 3. `lexists` after the call is not a sound success test | Fixed: the three-part "gone" test in section 1 |
| 4. Lock contention with disk-guard | Moot: no call, no lock |
| 5. The byte budget cannot be met, so every recent job is pruned | Fixed: budgets count job directories only, plus the one-day floor |
| 6. Catch-up and `TimeoutError` | Fixed: only terminal sizing leaves a pass unfinished. Archiver time no longer exists |
| 7. The archiver deletes regenerable caches without backup | The archiver's policy; sent to its owner |
| 8. Lows | The quarantine rename, the `SUBFLEET_HOME` path and the fence scope are handled in sections 1 and 4. The rest concerned the archiver call, which is removed |

## 9. Probes

`tests/unit/test_retention_archive_probes.py`:

- **Every worktree-content finding of rounds 1 to 4** is a scenario:
  - detached-HEAD commits;
  - ignored output;
  - nested and `.GIT` repositories;
  - a microcosm `.h5` under `build/`;
  - staged-only content;
  - a stat-cache edit;
  - `node_modules` and `.venv` edits;
  - a clean filter;
  - `refs/worktree`;
  - reflog and `ORIG_HEAD`;
  - a symlink pointing outside;
  - an unrelated missing registration.

  For each, Subfleet must leave every byte, the admin directory and the
  repository's refs and objects unchanged. The tree is tested in place and
  quarantined the way the archiver does it. Subfleet may prune the job only
  after a simulated archiver has set the tree aside and removed it.
- **Every other finding** has its own probe: stale sizes, deferral expiry,
  salvage refs, missing trees, large trees, read-only and deep directories,
  many jobs, late writers, the stuck oldest job, rows durability, and every
  pin.
- **The original probe files in `review-probes/`, rerun unchanged:**
  - The cache killers pass, except the worktree-bytes case. Worktree bytes are
    intentionally no longer budgeted.
  - Each of the five runnable `test_bug_*` salvage probes now fails, because
    its defect is absent.
  - The remaining probes call the removed proof API; their scenarios are
    ported.

## 10. Contract changes

- **C-8.4.**
  - Retention never touches a worktree, and a job waits for its tree to be
    gone for real.
  - Budgets count job directories.
  - A detached job's record is kept a day.
  - Job directories are archived, verified and deleted entry by entry.
  - Salvage no longer pins.
  - Archives are kept.
- **C-13.4.** An allocated worktree is removed only by the machine's worktree
  archiver. Retention prunes its job only after the tree is gone.
- **C-17.1.** The verbs `retention archives`, `retention restore` and
  `retention preview`.
