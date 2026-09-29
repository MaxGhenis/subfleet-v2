# Job retention by archive

Revision 3, 2026-09-29: as built after the in-session review of a9a6cbf4
(`~/reviews/retention-2026-09-28/d635-review-opus-insession.md`) and the
brief's disk-relief correction; section 2 lists what changed. Revision 2 was
the first build under Max's d635 ruling ("ship at the archive-sweep bar").
Revision 1 (ccc85387, 2026-09-28) was the design; its two reviews
(`~/reviews/retention-2026-09-28/archive-design/review-design-{opus,astra}.md`)
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
  a volume without clones. Two exceptions, both deleted without a copy:
  - a tracked file whose raw bytes hash to a blob that a network remote holds
    (section 6);
  - regenerable output: what a virtualenv's creator, a package manager,
    Python's bytecode compiler or a tool cache writes, identified by its
    structure, in a directory git tracks nothing in and ignores entirely
    (section 7). Anything else in such a directory is archived.
- **Git.** The worktree's admin directory (`<repo>/.git/worktrees/<id>`) byte
  for byte, and every object it names: HEAD, the index, `ORIG_HEAD`,
  `MERGE_HEAD`, `FETCH_HEAD`, `refs/worktree/*`, `refs/bisect/*`, rebase and
  sequencer state, every reflog entry. Those objects, the job's salvage
  commits and its baseline become one synthetic *anchor* commit, the only head
  of a bundle of every commit no network remote holds.
- **Rows.** The job's database rows, as `rows.json`.

Then it deletes only entries whose `lstat` signature is still the archived one,
re-validated right before each unlink. Anything new or changed goes to a
conflicts folder instead. Nothing depends on a repository retention does not
control, and no proof about what a file means is needed: retention first puts
it somewhere else, or proves by its structure that the project's own tools
make it again.

## 2. What changed

### Revision 3 (review of a9a6cbf4 and the disk-relief correction)

| Finding | Revision 2 | Revision 3 |
|---|---|---|
| B1: a salvage commit a network remote already held could never retire | Salvage refs were bundle heads; `git bundle` drops a head a remote holds, so the bundle lacked it, or was empty without a registration, and every attempt rolled back | The anchor is built whenever the repository is known (parents: the admin-named commits, the salvage commits, the baseline) and is the bundle's only head. The manifest records each salvage `{artifact_id, ref, commit}` and which commits are ancestors of the anchor; a salvage artifact counts as archived when its commit is one. An anchor a remote already reaches needs no bundle. Restore recreates `refs/subfleet-restored/<job>/<ref>` from the manifest |
| B2: the conversation service ran a repository-wide `git worktree prune` | `_cut_worktree` pruned before re-adding a broken worktree | `retention_git.discard_registration` removes only the tree's own unlocked registration; the daemon and the conversation service both call it |
| N2: one job that could not be measured kept retention in 5-second catch-up | Counted as unmeasured for ever | Its size is unknown, which decides it; it is measured again after 1 h, doubling to 24 h. A pass reports `progressed`; the daemon doubles the catch-up wait, up to an hour, while passes report more work but change nothing |
| N10: an error dropped the archive cache | The whole tree was read again every 6 h | The cache is kept on every rollback except when the job is in use again (pinned, rows or leases changed); a repeated error doubles its deferral to 24 h; a stored copy that does not read back is removed so the next attempt clones it anew |
| N8 (jobs with a cache): deferrals lived only in memory | A restart retried every deferred job at once | The idle journal records the error count and, in wall-clock time, when the job may be tried again; a restarted daemon recalls it |
| N3: a slice could stop making progress, and parked jobs queued behind the first | A slice stopped mid-file, or before its cached re-walk reached anything new, and the next started over; in-flight jobs were sliced in name order | One file's read runs to its end; a slice parks only after it has written new progress; in-flight jobs are sliced least recently sliced first, so they take turns at a pass's time |
| N12: the bundle cache ignored ref values | Keyed on the held globs | Keyed on the remote-tracking refs' names and values too |
| N7: retirement freed only omitted tracked bytes | Everything else moved into the archive, and events reported the whole tree as reclaimed | Regenerable output is deleted, not archived (section 7): only the entries each tool writes at the top level of its directory, because live job trees held agents' logs, scripts, a bundle and a bare repository inside tagged caches and a project folder inside a `.venv`. The manifest, events, pools and log lines say what was freed and what was moved (section 11) |

### Revision 2 (the d635 build)

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
| Salvage | Stopped pinning | Still pins (C-8.4: "salvage refs referenced nowhere else"), and the archive's verified anchor is that elsewhere; a salvage ref that cannot be resolved keeps its job, as before |

## 3. Layout

```
<state>/retention/<job>/journal.json   the retirement's state, written before each step
<state>/retention/<job>/worktree/      the quarantined worktree
<state>/retention/<job>/job/           the quarantined job directory
<state>/retention/<job>/archive/       the archive while it is built (progress.jsonl makes it resumable)
<state>/archive/<job>/                 the verified archive, published once the rows are gone:
    manifest.json    every entry of every tree: path, lstat signature, and the sha256 and stored name,
                     the omitted blob id, or the regenerable mark; link targets; hard-link groups;
                     the salvage commits; the regenerable directories; the totals (section 11)
    summary.json     totals, for `retention archives`
    files/           the stored files (clones), named by the signature of the version archived
    commits.bundle   the anchor, its only head, with every commit no network remote holds
                     (absent when a network remote already reaches the anchor)
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
   backlink names its path; without one, the repository is the job's source
   (`rev-parse --git-common-dir` in its workdir). Resolve every salvage ref;
   check the trees are on the state root's volume. A backlink that names
   another tree, an unresolvable salvage ref or another volume defers the job.
3. **Lock** the registration with `locked` (text `subfleet retention: <job>`),
   so no `git worktree prune` or `git gc` drops it while the tree is away. A
   lock someone else wrote defers the job.
4. **Quarantine**: rename the worktree and the job directory into
   `retention/<job>/`. No process can reach them by path afterwards.
5. **Holder check 1** (one `lsof` for the whole batch, section 8).
6. **Archive**, within the job's time slice (default 120 s): list, with git,
   the worktree's tracked and untracked-unignored paths (again after the
   walk, section 7); walk the trees
   descriptor-relative, never following a link, in byte order; clone, omit or
   mark regenerable each entry, re-`fstat`ing a file around its read (a change
   defers the job); walk the admin directory in place; verify the omitted
   blobs whole; build the anchor, create its ref, write and verify the bundle;
   write the manifest; read every stored file back and check its size and
   sha256. A job whose slice runs out is parked in quarantine with its progress
   and continues in the next pass. A slice stops only between files and only
   after it has written new progress, so every pass moves a parked job forward.
7. **Holder check 2** (one `lsof`, also matching the archived inodes).
8. **Final check**: walk every tree again; every entry must still be there with
   its archived signature, and nothing may have been added.
9. **Commit**, in one transaction: write `rows.json` and read it back first;
   inside, compare the rows with it, ask every pin again (the conversation
   service's included, and salvage released only for the commits the verified
   anchor reaches), check both leases are still retention's, then delete the
   rows and the leases. This is the point of no return. While the rows exist,
   no byte of the job has been deleted.
10. **Publish**: rename the archive to `<state>/archive/<job>`.
11. **Reclaim**: verified deletion (section 9) of the worktree, the job
    directory and the admin directory, which removes the registration. If
    anything in the admin directory changed after the final check, it is kept
    (locked), and what it names is anchored under a `late-` ref.

**Rollback** (any step before commit): rename the trees back (an occupied
original path sends ours to conflicts and keeps the lock), remove the lock if
retention wrote it, release the leases, and defer the job: 1 hour when busy or
changed, 15 minutes when the listing failed, 6 hours on an error (doubling on
each repeat, to 24 hours), 24 hours for a lasting condition (a nested linked
worktree, a foreign lock, an unresolvable salvage ref, another volume). The
archive built so far is kept, with an idle journal that records the error
count and when the job may be tried again, so the next attempt reads only
what changed and a restarted daemon does not retry at once. It is dropped
when the job is in use again (pinned, its rows or leases changed), when its
rows are gone, or after two idle days. Anchor refs stay; they are harmless.

**Recovery** (start of every pass): a `retention:` lease with no journal is
released (the old retention's, or a selection that died before its journal); a
journal in `committing` is resolved by whether the rows exist; everything
committed is published and reclaimed; everything before commit continues; an
idle journal's deferral is recalled once per daemon.

## 5. What the archive covers

| Where work can live | How it survives |
|---|---|
| Tracked files, modified or not | Archived byte for byte, unless the raw bytes equal a blob a network remote holds (section 6) |
| Untracked and ignored files: `build/`, `dist/`, `target/`, `.cache/`, data, logs | Archived byte for byte |
| Regenerable output: a virtualenv, `node_modules`, `__pycache__`, tool caches | The tool's own entries are deleted with the tree, not archived, when section 7 identifies them; anything else in the directory, and everything section 7 does not identify, is archived |
| Staged content | The index file is archived; every blob it stages is in the anchor's `index/` tree, in the bundle |
| Detached, reflog-only, `ORIG_HEAD`, `MERGE_HEAD`, `FETCH_HEAD`, `refs/worktree/*`, `refs/bisect/*`, rebase state | Every commit, tree and blob id the admin directory names is a parent or entry of the anchor, in the bundle; the directory itself is archived |
| Salvage commits | Parents of the anchor, so in the bundle or on a network remote; the manifest records each ref and commit; the refs themselves are never touched |
| Nested repositories (`.git` directories, bare repositories) | Archived byte for byte, object stores included, wherever they are, inside a would-be virtualenv too |
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
  same path in a commit that those remotes' `refs/remotes/*` reach: HEAD or
  the job's baseline when they are pushed, else the held commits at the
  boundary of their unpushed history;
- that blob reads back whole from the object store and hashes to its id;
- it has one link.

The manifest records the blob id with the file's actual mode and mtime. When in
doubt, the bytes are archived.

## 7. Regenerable output

The disk relief Max asked for: a retired job's virtualenv or `node_modules` is
often most of its size, and archiving it by clone frees nothing. Part of the
worktree is deleted with the tree, not archived, only if all of these hold
(`retention_fs.regenerable` and `RegenerableWalk`, one rule for the archive and
the survey):

- **The directory's structure says a tool wrote it**, never its name alone;
  every marker is a regular file, never a link:
  - `.venv` or `venv` holding a `pyvenv.cfg` with a `home` key (PEP 405's
    virtual environment marker);
  - `node_modules` beside a `package.json` (both live `node_modules` sit
    beside one);
  - `__pycache__` holding only regular `.pyc` and `.pyo` files;
  - `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.uv-cache` or `.tox`
    holding a `CACHEDIR.TAG` that starts with the cache-directory standard's
    signature. Live trees (2026-09-29): 75 of 78 `.pytest_cache`, all 44
    `.ruff_cache`, all 4 `.mypy_cache` and all 18 `.uv-cache` carry it; the
    three `.pytest_cache` without one were made by agents for their logs. No
    live tree has a `.tox`; whether tox writes the tag was not checked, and a
    `.tox` without one is archived.
- **Only the tool's own entries go.** Of each directory but `__pycache__`,
  the top-level entries the tool writes are dropped, each with everything
  under it; anything else stays and is archived:

  | Directory | The tool's own top-level entries |
  |---|---|
  | virtualenv | `pyvenv.cfg`, `CACHEDIR.TAG`, `.gitignore`, `.lock` (files); `bin`, `lib`, `include`, `share`, `etc`, `man`, `Lib`, `Scripts`, `Include` (directories); `lib64` (directory or link) |
  | `node_modules` | a package directory (holding a regular `package.json`); an `@scope` directory of packages and links; a link not starting with `.`; `.bin`, `.pnpm`; `.package-lock.json`, `.modules.yaml`, `.yarn-integrity`, `.yarn-state.yml` |
  | `.pytest_cache` | `CACHEDIR.TAG`, `README.md`, `.gitignore`, `v/` |
  | `.ruff_cache` | `CACHEDIR.TAG`, `.gitignore`, version directories (`0.16.9`) |
  | `.mypy_cache` | `CACHEDIR.TAG`, `.gitignore`, `missing_stubs`, Python version directories (`3.14`) |
  | `.uv-cache` | `CACHEDIR.TAG`, `.gitignore`, `.lock`, bucket directories (`archive-v0`, `wheels-v6`), temporary entries (`.tmp` and six letters or digits) |
  | `.tox` | `CACHEDIR.TAG`, `.gitignore`, environment directories (each a virtualenv by its `pyvenv.cfg`) |
  | `__pycache__` | all of it |

  These lists are what the tools write in the live trees; everything else
  found there was an agent's: logs, JUnit reports, `.py` scripts, a git bundle
  and a bare repository (`*.git`) at the top of tagged `.pytest_cache` and
  `.uv-cache` directories, and a project folder at the top of a `.venv`. Each
  entry is matched by its inode and type as listed when the directory was
  found, so one replaced before the walk reaches it is archived.
- **Git tracks nothing in the directory and ignores everything in it**: no
  path of one `ls-files --cached --others --exclude-standard` over the
  quarantined tree (tracked paths, untracked files no rule ignores, a nested
  repository as its directory) is the directory, inside it or one of its
  parents. The listing is read again after the walk; a directory no longer
  clear (an index or `.gitignore` changed before the walk recorded it) defers
  the job. A change after the walk, the final check sees.
- **No repository answers for it**: neither the directory nor a directory
  between it and the tree's root holds a `.git` (the root's own is the job's
  repository), and a dropped entry holds no `.git` anywhere (a `pip install -e
  git+...` clone in `site-packages`). Meeting one under a dropped entry sends
  the walk round again with that entry archived, and the finding is written to
  the progress log, so later slices and attempts archive it without looking.
- **The job has a registration**, so git can answer; without one, nothing is
  regenerable.

`build/`, `dist/`, `target/` and `.cache/` are never regenerable here, whatever
they hold: an earlier round found real data under a `build/` package, and a
live `target/` holds hand-built binaries and source folders next to cargo's
`CACHEDIR.TAG`.

Every dropped entry is still in the manifest with its signature, so the holder
check, the final check and verified deletion treat it like any other: anything
new or changed after the final check goes to conflicts. Its files are neither
read nor stored, and restore does not recreate them (it lists each directory
under `not_restored`, with the entries dropped); the project's own tools (`uv
sync`, `bun install`, the next test run) make them again. The directory itself,
and whatever else it held, is restored.

## 8. Holder check

`lsof -n -P -w -F pcftaDin` once per check per pass, for the whole batch. A
process holds a job if it has its current or root directory, a text or memory
mapping, any directory descriptor, or a descriptor open for writing, under the
job's quarantine, its original paths, or its admin directory, or at the second
check on one of the archived (device, inode) pairs (regenerable ones
included: a process running from the job's virtualenv holds it). A read-only
descriptor on a file is not a hold. The check fails closed: `lsof` missing,
failing or timing out (15 minutes) defers the batch.

## 9. Verified deletion

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

## 10. Pins

A job is kept while any of these holds (C-8.4, unchanged from `release/217`
except salvage): it is not terminal; it is a gate review; an attempt is live or
quarantined; an unread notice addressed to a session; it is a parent of any
job; it or an attempt holds a lease; another holder has its worktree lease; a
resume holds its `retire:` fence; a salvage ref of an in-place job (or one that
cannot be resolved, or whose commit the verified anchor does not reach); gate
or merge evidence names it; the conversation service names it (asked again
inside the commit transaction); a turn job within `turn_keep_days`; an
explicit reference.

## 11. Accounting

The manifest's totals, the `retention.pruned` and `retention.reclaimed`
events, a pass's result and the daemon's log line keep apart:

- `freed_bytes`: deleted without a copy, the omitted tracked files
  (`omitted_bytes`) and the regenerable output (`regenerable_bytes`);
- `freed_disk_bytes`: what deleting those gives back on disk, measured at
  archive time as the blocks no clone or other link shares
  (`getattrlist` `ATTR_CMNEXT_PRIVATESIZE` on APFS; 0 for a file with other
  links). A virtualenv uv cloned from its cache frees little;
- `archived_bytes`: moved into the archive. These are clones, so deleting the
  originals frees nothing; the space comes back only when the archive is
  removed;
- `unlinked_bytes` (`retention.reclaimed`): every file removed from the trees.

All are apparent sizes (`st_size`) except `freed_disk_bytes`. A pool's
`bytes_before` and `bytes_after` count live trees (C-8.4's budget), not disk:
the archive is outside the pools. Each pool in a pass's result also carries
the `freed_bytes`, `freed_disk_bytes` and `archived_bytes` of the jobs it
reclaimed, and the daemon's catch-up and hourly log lines name both. APFS snapshots (Time Machine's local ones)
keep deleted blocks until they expire.

## 12. Progress

- **Oldest first, a bounded batch** (32 jobs, in-flight ones included).
- **No sizing before acting.** A pool over its job count needs no sizes. Sizes
  are measured lazily, oldest first, cached for six hours, and only until a
  pool's lower bound passes its byte budget. A stale size is a scheduling input
  only: at worst a job is archived a little early, which restore reverses. A
  job whose size cannot be measured is decided (its size is unknown), kept in
  that pass, and measured again after 1 hour, doubling to 24.
- **Slices.** A job archives for at most 120 s per pass, then parks with its
  progress. Other jobs of the batch go on. A slice parks only between files and
  after new progress, so a tree whose re-walk or one file outlasts it still
  finishes. In-flight jobs are sliced least recently sliced first (never
  sliced first, oldest first among those), so jobs that park take turns
  instead of queueing behind the first.
- **Deferral.** A busy, changed or failing job is put back and skipped until its
  deferral ends, so the queue never waits on it.
- **Pacing.** The daemon runs a pass hourly; while a pass reports more waiting
  (a parked job, a full batch, undecided sizes) the next runs 5 s later
  ("retention catch-up: ..." in the daemon log). A pass that reports more but
  changed nothing (`progressed` false) doubles that wait, up to an hour; one
  that made progress sets it back to 5 s. A pass that did work never raises
  `TimeoutError`; the daemon's worker pool has one more thread for it.

## 13. Restore

`subfleet retention restore <job> [--to DIR] [--repository R] [--check]`, offline:

1. Re-verify the archive: every stored file's sha256 and the bundle's heads.
2. Fetch the bundle into the source repository (or `--repository`, any clone of
   the project) under `refs/subfleet-restored/<job>/`, and recreate each
   salvage ref there from the manifest (`refs/subfleet-restored/<job>/
   subfleet-salvage/...`).
3. Recreate each tree at its original path (which must not exist), or at
   `DIR/worktree`, `DIR/job`, `DIR/admin`: directories, stored files (cloned
   back), omitted files from their blobs (each checked against its id),
   symlinks, hard links and FIFOs; then modes and mtimes, directories last.
   Regenerable directories are not recreated; the report lists them under
   `not_restored`. Restored to its original place, the admin directory
   re-registers the worktree (without retention's lock), with its HEAD, index
   and reflogs.

Without Subfleet: `manifest.json` lists every entry; `files/<name>` holds each
stored file; `git fetch <archive>/commits.bundle '+refs/*:refs/restored/*'` in
any clone of the project brings back every commit (a bundle whose repository
had no network remote has no prerequisites and fetches into an empty
repository); `git cat-file blob <id>` there gives each omitted file; the
manifest's `salvage` names each salvage ref's commit. `subfleet retention
archives [--json]` lists archives; `subfleet retention survey [--json]` is a
read-only dry run of a pass over the live state, and `survey --sample N`
estimates from N sampled jobs what retiring frees versus archives.

## 14. Invariants

Each is tested (section 16).

- **I1, nothing lost.** Restoring gives back every entry that existed at
  archive time in the worktree, the job directory and the admin directory,
  except inside a regenerable directory (I11): path, type, permission bits,
  bytes, link target, hard-link grouping, mtime.
- **I2, delete only what is archived or regenerable.** An entry is unlinked
  only if the verified manifest lists it with a signature equal to its `lstat`
  just before the unlink; directories only with `rmdir`. Anything created or
  changed after archiving stays, in conflicts.
- **I3, commits kept.** Every commit and staged blob the worktree reached
  before retirement, and every salvage commit, is in the bundle or reachable
  from a network remote's refs, so a fresh clone of the remote plus the bundle
  holds all of them. No ref is ever moved or deleted.
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
- **I9, progress.** Every pass retires, parks with new progress, measures, or
  defers each candidate with a reason; none waits on another; a pass that
  changes nothing makes the daemon wait longer, never shorter.
- **I10, idempotence.** The anchor for the same inputs is the same commit, and
  every step can be run again after a crash.
- **I11, only regenerable output is dropped, and all of it.** An entry is
  deleted without a copy exactly when section 7 identifies it as a tool's own
  (or inside one); the accounting (`regenerable_bytes`, `freed_bytes`) is the
  sum of what was so deleted.

## 15. Residual risks (accepted by the d635 ruling)

- A write in the microseconds between an entry's final signature check and its
  unlink.
- A writable descriptor passed over a Unix socket and held by no process at the
  moment of a listing, or any other deliberately adversarial same-user trick;
  processes of other users are invisible to a non-root `lsof` (worktrees are
  0700).
- A remote-tracking ref whose commit the remote itself later dropped (a
  force-push, then the server's gc): such a commit counted as held. The source
  repository keeps its objects while it exists.
- A file a person put by hand *inside one of a tool's own entries* (under a
  virtualenv's `lib/` or `bin/`, a package in `node_modules`, `.pytest_cache/v/`,
  a uv cache bucket), in a directory the project ignores, is deleted with it.
  Anything put at the top of the tool's directory is archived (section 7); in
  the live trees every agent-made file was there. A `.git` under a dropped
  entry keeps it; a bare repository not named `.git` under one (uv's own
  `git-v0` checkouts are such) does not.
- Extended attributes, ACLs and file flags are not in the manifest; a clone
  keeps them, a byte copy and a restore do not.
- Archives are never deleted automatically. With clones, an archive costs the
  blocks of the files it keeps (they would otherwise have been freed); `retention
  archives` lists them, and removing one is `rm -r <state>/archive/<job>` plus
  its `refs/subfleet-archive/<job>/*`.

## 16. Tests

`tests/unit/test_retention_archive.py` (real git repositories, a fake clock and
a fake process listing), `test_retention_regenerable.py`,
`test_retention_archive_properties.py` and
`test_retention_regenerable_properties.py` (Hypothesis), and the rewritten
`test_retention_worktrees.py`, `test_timers_retention.py` and
`test_policy_support.py`. One regression for each d635 item and each review
finding:

| Item | Tests |
|---|---|
| Round trip | `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_bundle_alone_restores_the_commits_in_a_fresh_clone`, both properties |
| Nothing depends on an uncontrolled repository | `test_scratch_source_clone_deleted_after_retirement_is_still_restorable`, `test_clone_of_a_local_repository_carries_its_whole_history`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch`, `test_temporary_directory_source_is_scratch_by_default` |
| Omission hashes the whole object | `test_corrupted_loose_object_is_not_trusted_for_omission[wrong-bytes, truncated]`, `test_object_reader_hashes_every_byte` |
| Anchor idempotent | `test_anchor_is_deterministic_and_create_is_idempotent`, `test_rollback_after_anchoring_then_next_pass_retires` |
| Every admin-held commit anchored | `test_every_commit_only_the_admin_directory_names_survives_gc`, `test_missing_worktree_with_a_live_registration_is_anchored` |
| B1, salvage a remote holds | `test_salvage_a_network_remote_already_holds_retires[with-registration, without-registration]`, `test_salvage_is_bundled_and_its_ref_kept`, `test_an_anchor_a_network_remote_already_holds_needs_no_bundle` |
| N12, bundle cache | `test_a_cached_bundle_is_rebuilt_when_remote_tracking_refs_move` |
| Nothing written after the final check deleted | `test_file_written_after_the_final_check_is_kept_in_conflicts`, `test_new_file_before_commit_rolls_back_the_job`, `test_a_swapped_directory_sends_nothing_outside`, `test_a_file_written_into_regenerable_output_after_the_check_is_kept` |
| Slow or interrupted checks defer, no livelock | `test_a_slow_archive_parks_while_other_jobs_retire_in_the_same_pass`, `test_a_busy_oldest_job_does_not_block_the_queue`, `test_a_failed_process_listing_defers_the_batch`, `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`, `test_a_slice_moves_forward_when_one_file_outlasts_it`, `test_a_slice_moves_forward_when_the_cached_rewalk_outlasts_it`, `test_parked_jobs_take_turns_at_the_pass_time` |
| N2, unmeasurable job; daemon backoff | `test_an_unmeasurable_job_is_decided_and_backs_off`, `test_retention_catch_up_backs_off_while_a_pass_changes_nothing` |
| N10, cache kept on error | `test_a_persistent_error_keeps_the_cache_and_backs_off`, `test_an_archive_that_does_not_read_back_authorizes_nothing` |
| Staged, resumable removal | `test_interrupted_removal_resumes_and_leaves_no_half_tree`, `test_a_crash_at_any_step_is_recovered_by_the_next_pass[7 steps]`, `test_an_entry_deletion_cannot_remove_is_set_aside_not_left_half_deleted` |
| No repository-wide prune (B2) | `test_retention_never_prunes_other_registrations`, `test_discarding_a_broken_allocation_removes_only_its_own_registration`, `test_rebuilding_a_conversation_worktree_removes_only_its_own_registration[plain-directory, detached-worktree]` |
| Every pin | `test_every_pin_keeps_its_job[16 pins]`, `test_pinned_at_commit_rolls_back_then_retires_when_unpinned`, `test_resume_fence_and_retention_exclude_each_other`, `test_in_place_salvage_still_pins`, `test_salvage_is_bundled_and_its_ref_kept` |
| Progress under load | `test_batch_bounds_a_pass_and_takes_the_oldest_first`, `test_a_pool_over_its_count_is_pruned_without_sizing_everything` |
| Disk relief (I11) | `test_regenerable_output_is_deleted_with_the_tree_not_archived`, `test_work_left_inside_a_tool_directory_is_archived` (the live trees' findings), `test_lookalikes_are_archived_byte_for_byte`, `test_a_repository_inside_regenerable_output_is_archived`, `test_without_git_nothing_is_regenerable`, `test_ignore_rules_that_change_during_the_archive_defer_the_job`, `test_a_file_written_into_regenerable_output_after_the_check_is_kept`, `test_a_replaced_tool_entry_is_archived`, `test_tool_layouts_split_the_tools_entries_from_everything_else[4 live layouts]`, `test_ignored_matches_git_status`, `test_sampled_survey_is_read_only_and_matches_the_retirement`, `test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored` (Hypothesis, against an independent oracle and `git status --ignored`) |
