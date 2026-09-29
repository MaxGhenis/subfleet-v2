# Job retention without deleting unpreserved work: design

Revision 2, 2026-09-29. Binding clauses: C-8.4, C-13.4 and C-17.1 in
`docs/acceptance-contract.md`.

- **Revision 1** (2026-09-28, ccc85387) had Subfleet archive worktrees itself.
  Two independent design reviews returned CHANGES NEEDED:
  - Astra: `~/reviews/retention-2026-09-28/archive-design/review-design-astra.md`
  - Opus: `review-design-opus.md`, in the same folder
- **Revision 2** keeps revision 1's rule, that nothing is deleted before it is
  preserved and verified. It moves the one hard part, preserving a Git
  worktree, to the machine's worktree archiver.
- **What it replaces.** Retention rounds 1 to 4 were proof-based
  (`fix/retention-progress`, `fix/retention-r2`, `fix/retention-r4`). None of
  them reached `release/217`.

## 1. Decision

**Subfleet's retention never deletes, moves or writes a worktree.**
- An allocated worktree (C-13.4) is retired only by the *worktree archiver*
  named in policy.
- A job that owns a worktree is pruned only after the worktree has gone.
- With no archiver configured, such a job is kept, and so is its worktree.

On Max's machine the archiver is chief-of-staff's `worktree-archive-sweep`
(`~/chief-of-staff/docs/worktree-archive-sweep.md`). For each worktree it
takes, it does these steps in order:

1. **Preserve.** It stores the worktree's local-only state in the main
   repository as refs under `refs/archive-snapshots/wt/`:
   - HEAD;
   - the working tree, as a commit;
   - the index, when it differs from HEAD;
   - commits that only the reflogs hold.

   It copies every changed or untracked file, and ignored files that are not
   regenerable, byte for byte. The copies keep modes and extended attributes.
   It also copies the admin directory.
2. **Verify.** It checks those copies against the worktree.
3. **Quarantine.** It renames the worktree to a quarantine name.
4. **Re-check liveness.** It checks again that nothing uses it: `lsof`, `ps`,
   Subfleet's store, open Claude sessions and launchd.
5. **Remove.** Only then does it run `git worktree remove --force`, and it
   writes a restore recipe.

It refuses nested repositories, sparse checkouts, unmerged indexes and
oversized ignored or untracked content. It has had three adversarial review
rounds, with property, fuzz and concurrency tests (chief-of-staff `tests/`).

Subfleet keeps and archives **its own records**, the job directory and the
job's rows:
1. It writes a compressed tar, a manifest and `rows.json`.
2. It fsyncs them (`F_FULLFSYNC` on macOS) and verifies them by reading them
   back.
3. It deletes the rows in a transaction that checks they still match
   `rows.json`.
4. It then deletes, from the job directory, only entries the manifest lists
   with an unchanged signature. Directories go only by `rmdir`.

**Why not revision 1.** It would have made Subfleet a second worktree
archiver, less proven than the one already running on this machine, and the
reviews found it unsafe as written:
- It omitted tracked files whose objects live in a repository Subfleet does
  not control. The reviewers reproduced loss from a corrupted object, from a
  deleted throwaway clone, and from alternates.
- A locked registration does not keep everything its admin directory names.
  Thousands of locked registrations also slow Git badly: `git switch` took
  43–70 s with 3,000 of them.
- Create-only anchors wedged any retry.
- Two `lsof` scans do not exclude a writer holding a descriptor queued over a
  socket. A scan also takes 4–6 minutes at this machine's load.

## 2. Pass

A pass runs every hour, and every 5 s while it is catching up (section 5).

1. **Recover** (6.3). Finish any job-directory deletion whose rows are gone.
   Clear any unfinished archive whose rows remain.
2. **Size.** Measure job directories and owned worktrees with a resumable walk,
   and cache the sizes (5).
3. **Select.** In each pool, while the job count or the measured bytes exceed
   the budget, take terminal jobs oldest first, skipping the ones that are
   pinned (7) or deferred (5).
4. **Fence.** In a transaction, re-check pins and take the lease
   `retire:<job>`, held by `retention:<job>`.
   - It is deliberately not a path lease. Disk-guard treats every
     `worktree:/…` or `out:/…` lease as live and would refuse the worktree
     Subfleet asks it to archive.
   - Resume refuses a source that holds `retire:` (7).
5. **Retire worktrees.** Batch every selected job whose owned worktree still
   exists (`lexists`), and run the archiver once with all of them (4). Then
   check each path again:
   - **gone:** the job goes on to step 6;
   - **still there:** the job is kept and deferred, with the archiver's last
     line as the reason.
6. **Archive the job directory** (6.1) and write `rows.json` into the archive.
   Verify the archive.
7. **Commit.** In one transaction:
   - re-check pins and the `retire:` lease;
   - check that the job's rows still hash to the digest recorded in
     `rows.json`;
   - delete the rows.

   If anything differs, commit nothing and clear the archive (6.3).
8. **Delete the job directory** by verified deletion (6.2), then release
   `retire:<job>`.

No subprocess, `stat` or file write happens inside a transaction (C-3.3).

## 3. Invariants

Each is tested by property-based tests (Hypothesis) and by the probes in
section 9.

- **W1.** Retention never unlinks, renames, `chmod`s or writes inside an owned
  worktree. It never runs a Git command that changes a registration.
  Everything retention itself does to a worktree is read-only. Removal is the
  archiver's, in its own process.
- **W2.** A job with an owned worktree is pruned only after `lexists(worktree)`
  is false once the archiver has returned. With no archiver configured, such
  a job is never pruned.
- **J1.** A job directory is deleted only after its archive is durable and has
  been verified by reading it back. Durable means fsynced, plus `F_FULLFSYNC`
  where it exists. Verified means every member matches the manifest's name,
  type, mode, size, sha256 and link target.
- **J2.** While a job's rows exist, no byte of its job directory has been
  deleted.
- **J3.** Deletion unlinks only entries the manifest lists whose `lstat`
  signature is unchanged: type, mode, inode, device, size, mtime and ctime.
  Directories are matched by type, device and inode, and removed only by
  `rmdir`. Anything else is kept, and the remainder goes to
  `retention-conflicts/`.
- **J4.** Restoring a job directory reproduces every archived entry: path,
  type, permission bits, bytes, symlink target, hard-link grouping and mtime.
- **R1.** `rows.json` holds exactly the rows the commit deleted, checked by
  digest inside the deleting transaction.
- **P1.** Every pin in C-8.4 holds at selection and again at commit.
- **G1.** A pass over budget does at least one of these:
  - prunes a job;
  - measures a size it did not have;
  - finishes a deletion;
  - reports, for every candidate it skipped, a reason with a deferral.

  A deferred candidate stops blocking the next one. A pass that made progress
  never raises `TimeoutError`.
- **A1.** For each pool, `bytes_after` equals `bytes_before` minus the cached
  sizes of the jobs pruned. `jobs_after` equals `jobs_before` minus the number
  pruned.

## 4. The worktree archiver

Policy `retention.worktree_archiver`:

```json
{"argv": ["/Users/maxghenis/chief-of-staff/bin/worktree-archive-sweep", "--apply",
          "--only-under", "{worktrees}", "--min-idle-days", "1", "--idle-days", "1",
          "--lock-wait", "0", "--budget-s", "1800"],
 "per_worktree": ["--only", "{path}"],
 "timeout_s": 3600}
```

- `{worktrees}` becomes `<state>/worktrees`. `per_worktree` is repeated for
  each path.
- With the key absent or null, no worktree is ever retired.
- The call runs outside any transaction, in its own process group, with stdin
  from `/dev/null`. Its output is capped at 64 KiB.
- The pass deadline does not stop the call. Daemon shutdown sends SIGTERM to
  the group, then SIGKILL after 10 s. The sweep is crash-safe: its next run
  renames a quarantined worktree back or finishes the removal.
- Exit status never proves anything. Only `lexists` after the call decides.
  - A timeout, a non-zero exit (75 means the lock is held) or a path still
    present keeps the job and defers it for an hour.
  - The reason recorded is the exit status and the last line of output.
- The archiver owns preservation and its policy: idle floors, caps, nested
  repositories, the free-space floor. Subfleet does not second-guess a removal
  it did not perform.

Jobs whose worktree is already gone are pruned without the archiver. Their
worktree was removed by the sweep's own schedule, by hand, or never
allocated.

## 5. Sizes, progress and the timeout

The installed release sizes every job before it prunes anything, within a
60 s deadline, and so times out on every pass.

**Sizes.**
- Each job's size is kept in memory for the daemon's lifetime, keyed by
  (job id, owned worktree path).
- A terminal job's size is re-measured once it is 6 h old, or when its
  worktree appears or disappears. A non-terminal job is re-measured after
  10 minutes.
- A walk that reaches the deadline saves its stack and resumes on the next
  pass.

**Pruning under a partial measurement.** While some jobs are still unmeasured,
the sum of measured bytes is a lower bound on the pool. A pool whose lower
bound is already over budget prunes at once. The job count needs no
measurement.

**Deferrals.** Each has a reason and an expiry:
- a busy or refusing archiver: 1 h;
- an unarchivable job directory: 24 h;
- a job directory that changed during archiving: 1 h.

A deferred job is skipped, so the next candidate gets its turn.

**Daemon.**
- A pass that stops at its deadline after making progress records
  `retention.progress`, logs "retention catch-up: pruned N jobs, measured M;
  continuing in 5 seconds", and runs again 5 s later.
- A pass that stops at its deadline having done nothing raises
  `TimeoutError`, keeping the existing backoff.
- A completed pass rearms the hourly timer.

Deferred jobs are left out of "nothing done", so a pass whose only candidates
are deferred completes; it does not time out.

## 6. Job-directory archive

### 6.1 Writing

`<state>/archive/<job>/` holds:
- `manifest.json`: format version, job id, original path, compression, and one
  entry per path with its signature, sha256 and link target;
- `job.tar.zst`, a PAX tar compressed with zstd level 3 (Python 3.14's
  `compression.zstd`), or `job.tar.gz` where `tarfile` lacks zstd (Python 3.12
  on CI);
- `rows.json`: the job's rows from `jobs`, `attempts`, `artifacts`,
  `readings`, `notices` and `decisions`, with a sha256 of their canonical
  JSON.

How it is written:
- The whole archive is written as `<state>/archive/.partial-<job>` and renamed
  into place after verification.
- The walk is descriptor-relative (`openat` with `O_NOFOLLOW | O_NONBLOCK`)
  and never follows symlinks.
- It re-`fstat`s each file after reading it. A file that changed while it was
  read defers the job.
- An entry on another device, or one it cannot read, defers the job for 24 h.

A terminal job's directory has no writer: the provider process has been
contained, and an export holds a lease that pins the job. So this walk takes
no `lsof` snapshot. Verified deletion (6.2) is the backstop for a writer that
appears anyway.

### 6.2 Verified deletion

- The walk is descriptor-relative and post-order. Only one directory
  descriptor is open at a time.
- Each directory is opened with `O_DIRECTORY | O_NOFOLLOW` and matched by
  (type, device, inode).
- Each other entry is unlinked only if its signature equals the manifest's.
- Unlinking one hard link changes the inode's ctime for its other links. So
  the new ctime is read through a descriptor taken before the unlink, and is
  used as the expected value for the remaining links.
- A directory without owner write permission gets `fchmod` u+rwx after its
  identity check.

Deletion is idempotent, so an interrupted run resumes. Directories, and links
already unlinked, are not compared by ctime.

### 6.3 Recovery

Every pass first scans `<state>/archive/.partial-*` and each archive whose job
still holds a `retire:` lease.

| Job rows | Archive | Action |
|---|---|---|
| present | partial or complete | Delete the archive. The job directory was never touched. Release the lease |
| absent | complete | Re-verify the archive by reading it back, then run verified deletion (6.2) and release the lease |
| absent | partial or missing | Keep the job directory. Move it to `retention-conflicts/<job>`, emit an event, release the lease |

The last row cannot arise from Subfleet's own sequence, because rows are
deleted only after the archive is complete. It covers a hand-edited store.

A legacy `retention:` holder on a `worktree:` lease, left by the installed
code, is released. The live store had none on 2026-09-28.

### 6.4 Restore

- `subfleet retention archives [--json]` lists each archive: job, date,
  original path, bytes, archive bytes, and any salvage refs from `rows.json`.
- `subfleet retention restore <job> [--to DIR] [--check]` re-verifies the
  archive and extracts it to `DIR`, or to `<state>/jobs/<job>` when that is
  free. It extracts with `filter="tar"`, which keeps `.venv`-style absolute
  symlinks. It then applies the manifest's modes and mtimes, directories last.
  `--check` only verifies.
- Rows are not re-inserted.
- Worktrees are restored with the archiver's own recipe, from
  `~/chief-of-staff/state/logs/worktree-archive-sweep.jsonl`.

Archives are never deleted by Subfleet. Job directories were 3.7 GB on
2026-09-29 (read-only `du`), and their logs compress well.

## 7. Pins

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
- **Salvage no longer pins.** Retention never touches refs, and the worktree
  is removed only by the archiver after it has preserved it. `rows.json`
  keeps the salvage artifact's ref name.
- **New pin.** A non-terminal job whose workdir or worktree is inside an owned
  worktree pins that worktree's owner. Example: a job submitted in place into
  another job's tree.
- **Resume fence** (Astra 9). Resume already re-reads its parent in the same
  transaction that inserts it (`_validate_conflicts`). That transaction now
  also refuses when `retire:<parent>` exists: "being archived by retention;
  retry in a minute". The job-directory files a resume reads are deleted only
  after the parent's rows are, and the lease is held until deletion finishes.
  So a resume that read degraded files cannot be inserted.

## 8. Review dispositions (revision 1)

| Finding | Disposition in revision 2 |
|---|---|
| Astra 1 corrupted objects authorize omission; Astra 2, Opus 1 omission depends on a repository Subfleet does not control | No omission. Subfleet archives only job directories, byte for byte |
| Astra 3 queued `SCM_RIGHTS` descriptor and external hard links defeat quiescence | Worktrees: the archiver's domain (quarantine, re-checked liveness). Job directories: no writer for terminal jobs, and J3 keeps changed entries. Residual: a descriptor passed over a socket before containment and used after the signature check. Documented, not reproducible without a deliberate adversary |
| Astra 4 rollback collision removes the lock; Astra 5 nested registrations; Opus 3 locks do not keep everything; Opus 5 locked registrations slow Git | Subfleet never locks, moves or deregisters a worktree |
| Astra 6 `rows.json` durability | Inside the verified archive, fsynced with `F_FULLFSYNC`, digest re-checked in the deleting transaction (R1) |
| Astra 7, Opus 2 anchor wedge | No anchors |
| Astra 8 stale sizes and oversized archives | 6 h and 10 minute size expiry; resumable walks; the archiver has its own budget per call |
| Astra 9 resume fence | Checked in the insert transaction (7) |
| Astra 10 disk forecast | Withdrawn. Subfleet archives only job directories, measured at 3.7 GB |
| Astra 11 Git claims | Withdrawn |
| Opus 4 `lsof` 4–6 min under load | Subfleet runs no `lsof`. The archiver takes one snapshot per call, and a pass makes one call |
| Opus 6 `F_FULLFSYNC`; re-verify after restart | Both adopted (6.1, 6.3) |
| Opus 7 no free-space guard | Job-directory archive refused while free space is under 2 GiB plus the directory's size. The archiver has `--floor-gb` |
| Opus 8 interrupted deletion becomes a permanent conflict | Directories matched by identity only; unlinked links skipped on resume |
| Opus 9 lows | Queued in-place jobs pin (7); a row race fails the digest (R1); `filter="tar"`; `O_NONBLOCK`. The rest are moot |

## 9. Probes

`tests/unit/test_retention_archive_probes.py` has one test per finding from
rounds 1 to 4 and revision 1. Each builds the finding's scenario in a real
repository and store, and runs retention three ways:
- with no archiver;
- with an archiver that refuses;
- with a fake archiver that removes the worktree after copying it aside.

A probe passes only if Subfleet deletes no byte of the worktree itself and
prunes the job only after the path is gone.

The worktree-content scenarios are delegated to the sweep:
- detached-HEAD and reflog-only commits, `ORIG_HEAD`;
- ignored output, a microcosm `.h5` under a real `build/` package;
- edits in `node_modules` or `.venv`;
- stat cache, clean filters, staged-only content;
- nested repositories and `refs/worktree`.

For those, the probe asserts W1 on Subfleet's side. It also runs the real
sweep in a dry run against the scenario when chief-of-staff is present. The
old probe files in `~/reviews/retention-2026-09-28/review-probes/` target the
removed proof API, and their scenarios are carried over.

## 10. Contract changes

- **C-8.4.** Retention never deletes a worktree. An owned worktree is retired
  only by `retention.worktree_archiver`. Job directories are archived,
  verified and then deleted. Salvage no longer pins. Archives are kept until a
  person removes them.
- **C-13.4.** An allocated worktree is removed only by the configured
  worktree archiver. Retention prunes its job only after it has gone.
- **C-17.1.** New verbs: `retention archives [--json]` and
  `retention restore <job> [--to DIR] [--check]`.
