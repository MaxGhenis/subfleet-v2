# Job retention by archive

Revision 5, 2026-10-01: the four notes the parallel effort (#81) left on
#76, each reproduced against real repositories and fixed (section 2;
`docs/reports/2026-10-01-retention-81-notes.md`).
Revision 4, 2026-09-30: as built after the final review of e50716e8
(`~/reviews/retention-2026-09-28/final-review-opus3-a2-scratch/REVIEW.md`);
section 2 lists what changed. Revision 3, 2026-09-29, followed the in-session
review of a9a6cbf4 (`~/reviews/retention-2026-09-28/d635-review-opus-insession.md`)
and the brief's disk-relief correction. Revision 2 was
the first build under Max's d635 ruling ("ship at the archive-sweep bar").
Revision 1 (ccc85387, 2026-09-28) was the design; its two reviews
(`~/reviews/retention-2026-09-28/archive-design/review-design-{opus,astra}.md`)
and the ruling changed it as section 2 lists. The binding clauses are C-8.4,
C-13.4 and C-17.1 in `docs/acceptance-contract.md`. Code:
`subfleet/retention.py` (the pass), `retention_archive.py` (one job's
journaled retirement, restore), `retention_git.py`, `retention_fs.py`,
`retention_holders.py`, `retention_qos.py`, `retention_survey.py`,
`retention_cli.py`.

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
  - regenerable output: a file a rule proves the project's own tools make
    again (an installed distribution's file whose sha256 its RECORD names,
    bytecode beside its source, a tool cache's own files), in a directory whose
    structure says a tool wrote it and which git tracks nothing in and
    ignores entirely (section 7). Everything else there is archived.
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

### Revision 5 (#81's notes on #76)

| Note | Revision 4 | Revision 5 |
|---|---|---|
| 1: a changed hard-linked file passed as unchanged | A file whose inode had other links when it was archived was matched without its ctime, so a same-size write whose mtime was then put back (`touch -r`, `rsync -t`, `cp -p`) was deleted: through the tree by a late writer, or through a package store's link (pnpm) that was then dropped, the new bytes existed nowhere afterwards | Every file is matched on its ctime. One that had other links and whose ctime alone moved (unlinking a sibling moves it) is read again and deleted only if its bytes still hash to the archived sha256 (`retention_fs.still_archived`), in the final check, verified deletion and the late admin check (sections 4, 9) |
| 2: the sweep's quarantine | `disk-guard` and `worktree-archive-sweep` `git worktree move` a tree to `.disk-guard-removing.<name>`, check it there, and move it back when a check fails. With the tree away, `begin` found no registration and the job retired without its tree: moved back, it had no row naming it. Moved back between `begin` and `quarantine`, it was archived without its registration, left naming a tree that was gone (its HEAD's commit lost to a prune and gc). Moved away between `begin` and `lock`, its registration was archived and deleted: the sweep could not move back a tree whose `.git` named nothing | A tree held aside (an entry `<anything>.<name>` beside it) keeps its job (`tree away`, an hour at a time). `begin` records whether the tree was there; `quarantine` puts the job back when that changed, and the final check when a gone tree came back (section 4) |
| 3: a tree no row names | `_workspace` allocates `worktrees/<job id>` before the reserving transaction records `jobs.worktree`; a job cancelled in between (four live jobs, 2026-09-29) retired without that tree, which stayed for ever with its registration | `owned_worktree` takes that tree as the job's while `jobs.worktree` is NULL: archived and retired with it, sized, leased, and pinned by `worktree-in-use` like a recorded one (C-13.4) |
| 4: a workdir that is gone | With the tree and the workdir gone (62 of 371 terminal owned jobs, 2026-09-29), `find_registration` ran git in a missing directory and found nothing: the job retired with no anchor while its registration stayed behind, or, with salvage, was kept for ever as `salvage not archivable` (55 live jobs, 2026-09-30) | The registration is looked for, by its name and backlink, in the repository the workdir's nearest existing ancestor is in (or a broken linked checkout's `.git` there names), then in every repository the store's jobs are in; without one, a repository is the job's when it holds every salvage ref the job's rows name. A salvage ref whose repository is not found says so (section 4) |

### Revision 4 (final review of e50716e8)

| Finding | Revision 3 | Revision 4 |
|---|---|---|
| N1: a repository with no network remote costs its whole history per retired job, and nothing reported it | The bundle held every object HEAD reached (about 460 MB per `~/chief-of-staff` job); only `summary.json` named its size | `added_bytes` (the bundle, manifest, summary, rows and byte copies) is reported wherever freed bytes are, with the net on disk (section 11). A job whose source repository has no network remote and whose bundle would carry more than `retention.remote_less_history_bytes` (default 64 MiB) is kept, `remote-less-history <size>` (section 4, step 2). The reference-counted base bundle that would lift that limit is section 17 |
| N2: `Ignored.clear` compared git's names and the disk's byte for byte | An NFD-named parent, or a directory whose case changed on disk, made a directory holding work look ignored, and the work was dropped | Both sides NFC-normalized and casefolded, as APFS matches names (section 7) |
| N3: work inside a tool's own entries was deleted without a copy | A tool's own top-level entries (a venv's `lib/`, a package in `node_modules`, a `__pycache__`, `.pytest_cache/v/`) were dropped whole | Inside them each file is judged by its kind's rule, and only a file the rule proves regenerable is dropped (section 7); section 15 no longer calls this class accepted |
| N4: a job registered in a repository inside another job's tree retired without its own anchor | Both chosen in one pass, the host was quarantined first and the hosted job archived bytes-only | The host is pinned while the hosted job has rows (`nested-host`, at selection and at commit), and a hosted job whose host tree is not there is kept, so it retires first with its own anchor and bundle (sections 4, 10) |
| N5: a registration reduced to `index` and `logs/` kept its job for ever | Deferred every day as `admin-unreadable` (the four `mstat6-g*` jobs, whose /tmp clones lost every file) | Such a remnant is no registration: the tree and the remnant's bytes are archived, the remnant removed with the tree (section 4, step 2) |
| N6: every published archive kept its progress log | About 357 bytes per stored file, redundant with the manifest | Removed at publish (section 3) |
| N9: retention's children ran at the operator's priority | `lsof`, git and the object readers spawned directly by a default-QoS daemon | Each starts under `taskpolicy -c utility`; the in-process steps lower their thread's disk I/O policy (section 12) |

The build was then reviewed (`~/reviews/retention-2026-09-28/r4-evidence/review-opus.md`,
REQUEST CHANGES), and these followed:

| Finding | As first built | Now |
|---|---|---|
| 1: a wheel built from a patched source and installed with `--find-links` vouched for its patched files | No `direct_url.json` meant "from an index" | Only uv's installs from an index or a network URL vouch: `INSTALLER` uv, no `uv_cache.json` (uv's mark of a local source), no local `direct_url.json`; pip's installs vouch for nothing (section 7) |
| 2: a job submitted from another job's tree root was deferred hourly for ever once that job was gone | `nested_hosts` used strict containment, `begin` did not | One host check (`host_absent`), strict; a host tree that is there holds nothing up; a gone host keeps the job a day at a time with a reason that says what brings the registration back (section 4) |
| 3: a network remote with no remote-tracking ref skipped the history limit | Any network remote did | A remote holding no ref counts as none (section 4) |
| 4: an over-limit repository was measured again for each of its jobs | Measured per job and heads | Once a pass per repository once over |
| 6: the survey diverged from a pass | No idle journal size, no host check for a tree that is gone | Both, through the same host check |
| 7: the `nested-host` pin did not say what it waited for | `nested-host` | `nested-host: <jobs>` |

The re-review (`r4-evidence/rereview-opus.md`, APPROVE WITH NOTES) then brought:

| Note | Before | Now |
|---|---|---|
| 1: the `node_modules` rule by time dropped an edit made before a later install | Dropped a package file no newer than the install marker | `node_modules` is archived whole (section 7) |
| 2: a server on this machine counted as another machine | Any URL host | `localhost`, loopback addresses, this machine's own name and `.local` / `.localhost` names count as this machine, for a distribution's `direct_url.json` and for a git remote (section 6's omission too) |
| 3: the survey skipped a pass's checks for a job whose tree is gone | Host check only | The same checks as the pass (registration by its backlink, salvage, the history limit) |
| 4: a remote holding only unrelated history turned the limit off | Any remote-tracking ref | The limit applies whenever no network remote holds the job's baseline (after the confirmation review, which found refs from long ago still turning it off) |
| a partial `dist-info` after restore | Its verified files dropped one by one | A `dist-info` goes whole or not at all |

The confirmation review (`r4-evidence/confirm-opus.md`, APPROVE WITH NOTES)
found no new loss path. Its notes: the limit now keys on whether a network
remote holds the job's baseline and measures what the bundle carries (above);
`this_machine` also takes this machine's own name, `127.1` and `0`; an index
or `--find-links` URL on this machine cannot be told from PyPI in a
dist-info (measured, section 15); restoring then running `uv sync` can
reinstall a distribution over a patch restored into it (section 13); held
refs are matched by name prefix (section 15).

A final check (`r4-evidence/final-opus.md`, APPROVE WITH NOTES) found no loss
path or wedge; after it, a kept bundle is held to the limit when reused,
`baseline_held` is asked once a pass per repository and baseline with the
configured git timeout, this machine's short name counts, and sections 4,
13, 14 and 15 say what held refs and a held baseline cover.

Finding 5 (a remnant's `index` and `logs/` name objects that are not
anchored) is left as it is: the remnant's repository has lost its `HEAD` and
`config` in every live case, and the bytes are archived.

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
<state>/retention/<job>/archive/       the archive while it is built (progress.jsonl makes it resumable;
                                       removed at publish, since a published archive never resumes)
<state>/archive/<job>/                 the verified archive, published once the rows are gone:
    manifest.json    every entry of every tree: path, lstat signature, and the sha256 and stored name,
                     the omitted blob id, or the regenerable mark (with the sha256 a RECORD vouched
                     for); link targets; hard-link groups; the salvage commits; the regenerable
                     directories; the totals (section 11)
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
   The journal records whether the tree was there (revision 5).
   - A tree that is gone while an entry named `<anything>.<its name>` is
     beside it is held aside by another tool: `disk-guard` and
     `worktree-archive-sweep` `git worktree move` a tree to
     `.disk-guard-removing.<name>`, check it there, and remove it or move it
     back. The job is kept, an hour at a time (`tree away`). Job ids hold no
     dots, so no job's own tree has such a name.
   - When the workdir is gone too (a lane checkout removed, a folder
     deleted), the registration is looked for, by its name (git names it by
     the tree's basename, digits added when taken; `git worktree move` keeps
     it) and its backlink, in the repository the workdir's nearest existing
     ancestor is in, or that a broken linked checkout's `.git` file there
     names, then in every repository the store's jobs' trees and workdirs
     are in (listed once a pass, only when needed). Without a registration,
     a repository is the job's only when it holds every salvage ref the
     job's rows name; a salvage ref whose repository is not found keeps the
     job with `repository not found (workdir … is gone)`.
   - An admin directory that is only a remnant (a real directory under
     `worktrees` holding nothing but an `index` file and a `logs` directory:
     what a temporary directory's cleaner leaves of a clone whose files it
     deleted) is no registration: the job goes on without one, and the
     remnant's bytes are archived as the admin tree and removed with it.
   - A job whose registration (its gitfile's admin directory, or, when its
     tree is gone, its source directory) is strictly inside another job's
     tree, which is not there, is kept (`nested-host`): retired now it would
     have no anchor and no bundle of its own. While that job is retiring (it
     has rows or a journal) the wait is an hour; once it is gone for good, a
     day at a time, with a reason naming the tree and saying that restoring
     its archive brings the registration back. A source directory that is a
     tree's root registers its worktrees in its own repository, outside the
     tree, and a host tree that is there but lacks the registration has
     nothing to wait for: both go on. Section 10 pins the host so that this
     does not happen in the ordinary course.
   - When no network remote holds the commit the job started from (its
     baseline: the repository has no network remote, or one never fetched,
     fetched only for an unrelated `gh-pages`, or last fetched before the
     baseline was made), the bundle carries the shared history no remote
     holds as well as the job's own commits, paid again by every job. Its
     size is measured first, as the bundle is made, against the held refs
     (`rev-list --objects --disk-usage`; once a repository is over the limit
     in a pass, its other jobs are kept on that measure); above
     `retention.remote_less_history_bytes`
     (default 64 MiB; 0 keeps every such job with history) the job is kept for
     a day, `remote-less-history <size>`, before anything moves. A bundle that
     comes out over the limit although the measure said less (it also carries
     the anchor and a pack's own overhead) is dropped, the job put back, and
     its size kept in the idle journal for the next check; a bundle an
     earlier attempt made and kept is held to the limit as it stands when it
     is used. A job whose baseline a network remote holds is not limited: its
     bundle carries what its heads reach beyond the held refs, its own
     commits and any unpushed history it merged or checked out.
3. **Lock** the registration with `locked` (text `subfleet retention: <job>`),
   so no `git worktree prune` or `git gc` drops it while the tree is away. A
   lock someone else wrote defers the job.
4. **Quarantine**: rename the worktree and the job directory into
   `retention/<job>/`. No process can reach them by path afterwards. A tree
   that left since `begin` read its registration (another tool's `git
   worktree move`), or came back, puts the job back: its registration would
   otherwise be archived and removed while the tree is elsewhere, or the tree
   archived without it. After the lock, `git worktree move` refuses the tree
   (the sweep never forces).
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
   its archived signature (a file that had other links and whose ctime alone
   moved: with its archived bytes, section 9), and nothing may have been
   added. A worktree that was gone must still be gone.
9. **Commit**, in one transaction: write `rows.json` and read it back first;
   inside, compare the rows with it, ask every pin again (the conversation
   service's included, and salvage released only for the commits the verified
   anchor reaches), check both leases are still retention's, then delete the
   rows and the leases. This is the point of no return. While the rows exist,
   no byte of the job has been deleted.
10. **Publish**: rename the archive to `<state>/archive/<job>`, then remove
    its `progress.jsonl` (a crash between the two is finished by the next
    pass).
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
| Regenerable output: a virtualenv, `node_modules`, `__pycache__`, tool caches | Each file a rule of section 7 proves regenerable is deleted with the tree, not archived; everything else in the directory, inside the tool's own entries too, is archived |
| Staged content | The index file is archived; every blob it stages is in the anchor's `index/` tree, in the bundle |
| Detached, reflog-only, `ORIG_HEAD`, `MERGE_HEAD`, `FETCH_HEAD`, `refs/worktree/*`, `refs/bisect/*`, rebase state | Every commit, tree and blob id the admin directory names is a parent or entry of the anchor, in the bundle; the directory itself is archived |
| Salvage commits | Parents of the anchor, so in the bundle or on a network remote; the manifest records each ref and commit; the refs themselves are never touched |
| Nested repositories (`.git` directories, bare repositories) | Archived byte for byte, object stores included, wherever they are, inside a would-be virtualenv too |
| A submodule (gitdir inside the admin directory) | Archived with the admin directory |
| A linked worktree nested in the tree (its admin directory elsewhere) | The job is kept |
| Another job's worktree registered in a repository inside this tree | This job is pinned until that one retires with its own anchor (section 10) |
| An admin directory reduced to `index` and `logs/` | Archived byte for byte as the admin tree |
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
  `user@host:`; not `localhost`, a loopback address, this machine's own name,
  or a `.local` name, which is this machine or another on the local network
  and taken as this one); a local-path remote is another repository retention
  does not control;
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
often most of its size, and archiving it by clone frees nothing. A file of the
worktree is deleted with the tree, not archived, only if all of these hold
(`retention_fs.regenerable`, `RegenerableWalk` and the rules below, one code
path for the archive and the survey):

- **The directory's structure says a tool wrote it**, never its name alone;
  every marker is a regular file, never a link:
  - `.venv` or `venv` holding a `pyvenv.cfg` with a `home` key (PEP 405's
    virtual environment marker);
  - `node_modules` beside a `package.json`;
  - a `__pycache__` directory;
  - `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.uv-cache` or `.tox`
    holding a `CACHEDIR.TAG` that starts with the cache-directory standard's
    signature. Live trees (2026-09-29): 75 of 78 `.pytest_cache`, all 44
    `.ruff_cache`, all 4 `.mypy_cache` and all 18 `.uv-cache` carry it; the
    three `.pytest_cache` without one were made by agents for their logs. No
    live tree has a `.tox`; a `.tox` without a tag is archived.
- **It is under one of the tool's own top-level entries.** Of each directory
  but `__pycache__`, only the top-level entries the tool writes are looked
  into; anything else at the top is archived whole:

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
  found at their top was an agent's: logs, JUnit reports, `.py` scripts, a
  git bundle and a bare repository (`*.git`) in tagged `.pytest_cache` and
  `.uv-cache` directories, and a project folder in a `.venv`. Each entry is
  matched by its inode and type as listed when the directory was found, so
  one replaced before the walk reaches it is archived.
- **Its kind's rule proves the tool makes it again** (final review of
  e50716e8, N3). Until revision 3 a tool's entry was dropped whole, and an
  agent's patch to an installed package, data under a venv's `share/`, a
  notebook in a package directory or a `.pyc` that was not bytecode went
  with it. Now each file is judged, and anything no rule proves is archived:

  | Kind | A file is dropped only if |
  |---|---|
  | virtualenv, and each `.tox` environment | an installed distribution's `*.dist-info/RECORD` lists it (paths relative to `site-packages`, `../../../bin/<script>` included) with a sha256 its bytes match, and a size, if listed, equal to its own. The archive reads the file to check (`Verify`, recorded in the manifest with the sha256). Only a distribution uv installed from a package index or a network URL vouches: its `INSTALLER` is `uv`, it has no `uv_cache.json` (uv writes one for a local source: a path, a directory, a local `--find-links` wheel; none of 3,415 live index installs has one, all 74 live editable installs do) and no `direct_url.json` naming a local URL (PEP 610) or a server on this machine (`localhost`, a loopback address, a `.local` or `.localhost` name). One from a local source, whose source may be the only copy (a wheel built from a patched checkout in /tmp and installed with `--find-links`), vouches for nothing; nor does one pip installed, since pip marks a local `--find-links` no differently from an index. A path two distributions list differently is left out. A `dist-info` directory goes whole or not at all: its files (the `RECORD` too, listed without a hash as the wheel format requires) go only when every file of it but the `RECORD` is listed there and verifies, so a restore never leaves a distribution half there (pip and uv read a partial `dist-info` as a broken distribution). Or it is bytecode, as below |
  | `__pycache__` (anywhere a directory by that name is, and inside a virtualenv) | it is a `.pyc` or `.pyo` whose first four bytes are a magic number (two bytes, then CR LF) and whose source, `<module>.py`, is a regular file beside the `__pycache__` folder (pytest's rewritten `<module>.cpython-314-pytest-9.1.1.pyc` included) |
  | `node_modules` | never (below) |
  | `.pytest_cache` | it is `v/cache/nodeids`, `v/cache/lastfailed` or `v/cache/stepwise` |
  | `.ruff_cache` | it is directly in a version directory and named by digits |
  | `.mypy_cache` | it is under a version directory and ends in `.data.json`, `.meta.json`, `.data.ff` or `.meta.ff`, or is `@plugins_snapshot.json` |
  | `.uv-cache` | never (no rule is proven for uv's cache formats; it is archived) |

  Links are always archived (a link holds no bytes to free). A directory under
  a tool's entry is dropped when it held something and everything in it was
  dropped (a post-order pass after the walk); the tool's directory itself stays,
  but for a `__pycache__`. The virtualenv skeleton (`pyvenv.cfg`, the
  interpreter links, activation scripts, `_virtualenv.py`) is archived: it is
  a few kilobytes, and restored it is a virtualenv `uv sync` fills again.

  `node_modules` is archived whole: no rule by structure proves a file there
  regenerable. npm, pnpm and yarn verify a package tarball's integrity but
  keep no hash of each file they unpack, and the tarballs are not kept. The
  structural rule first built here, which dropped a file inside a package
  when neither its mtime nor its ctime was later than the package manager's
  install marker (`.package-lock.json`, `.modules.yaml`, `.yarn-integrity`,
  `.yarn-state.yml`), was shown to drop an agent's edit to an installed
  package once a later `npm install` rewrote the marker and left that package
  in place (review of the revision-4 build), a loss outside the d635 ruling.
  No live job tree holds a `node_modules` (2026-09-30), so archiving it costs
  no relief today; a proof by content (each file against the package
  manager's cached copy) would be the way to drop any of it.
- **Git tracks nothing in the directory and ignores everything in it**: no
  path of one `ls-files --cached --others --exclude-standard` over the
  quarantined tree (tracked paths, untracked files no rule ignores, a nested
  repository as its directory) is the directory, inside it or one of its
  parents. Git prints the index's case and precomposed (NFC) names, the walk
  the disk's, and APFS matches names regardless of either, so both sides are
  compared NFC-normalized and casefolded; folding only merges names, so it can
  make a directory less clear, never more (N2). The listing is read again
  after the walk; a directory no longer clear (an index or `.gitignore`
  changed before the walk recorded it) defers the job. A change after the
  walk, the final check sees.
- **No repository answers for it**: neither the directory nor a directory
  between it and the tree's root holds a `.git` (the root's own is the job's
  repository), and a tool's entry holds no `.git` anywhere (a `pip install -e
  git+...` clone in `site-packages`). Meeting one under a tool's entry sends
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
new or changed after the final check goes to conflicts. Restore does not
recreate dropped entries (it lists each directory under `not_restored`, with
the paths dropped whose parent was not); the project's own tools (`uv sync`,
`bun install`, the next test run) make them again. Everything else, inside
the tool's entries too, is restored.

Measured read-only on a live job's `uv` virtualenv (2026-09-30,
`20260928-112535-trackb-b1-build-opus/.venv`, 7,702 files, 242,489,519
bytes): the rules drop 7,676 files, 242,443,031 bytes (RECORD-verified files
and bytecode beside its source) and 842 directories, and archive 26 files of
46,488 bytes (the skeleton: `pyvenv.cfg`, `.gitignore`, `.lock`,
`CACHEDIR.TAG`, the activation scripts, `_virtualenv.py`), in 33 s at load
about 50. The per-file rules keep the relief the whole-entry rule gave.

## 8. Holder check

`lsof -n -P -w -F pcftaDin` once per check per pass, for the whole batch. A
process holds a job if it has its current or root directory, a text or memory
mapping, any directory descriptor, or a descriptor open for writing, under the
job's quarantine, its original paths, or its admin directory, or at the second
check on one of the archived (device, inode) pairs (regenerable ones
included: a process running from the job's virtualenv holds it). A read-only
descriptor on a file is not a hold. The check fails closed: `lsof` missing,
failing or timing out (15 minutes) defers the batch. `lsof` runs under the
retention clamp (section 12).

## 9. Verified deletion

Descriptor-relative and post-order. For every entry present: unlisted or
changed (type, device, inode, size, mtime, mode and ctime) goes to
`retention-conflicts/<job>/<tree>/<path>` by rename (a writer's open file
keeps its data); a listed, unchanged file is unlinked. A file whose inode had
other links when it was archived, and whose ctime alone moved (unlinking a
sibling link moves it, mid-deletion or in a deletion an interruption left half
done), is read again and unlinked only if its bytes still hash to the archived
sha256; one whose bytes differ, or for which the archive recorded none
(regenerable bytecode or a cache file), goes to conflicts. Until revision 5
such a file was matched without its ctime, so a same-size write whose mtime
was put back was unlinked (#81's note 1). A
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
inside the commit transaction); a job not yet ended works in its allocated
tree (`worktree-in-use`, the tree recorded in `jobs.worktree` or, while that
is NULL, the `worktrees/<job id>` admission allocated, revision 5); a turn job within `turn_keep_days`; an
explicit reference; another job's worktree is registered in a repository
inside its tree and that job still has rows (`nested-host: <those jobs>`: which job each
owned worktree's gitfile names is read once per pass, and a job whose tree is
gone counts as hosted by the tree its source directory is in; asked again
inside the commit transaction, so a host already in flight is put back). The
hosted job then retires first, while its registration is readable, with its
own anchor and bundle, and the host in a later pass.

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
- `added_bytes`: the space the archive itself adds (final review of
  e50716e8, N1): its `commits.bundle`, `manifest.json`, `summary.json` and
  `rows.json` (`rarch.ADDED`, measured on disk after `rows.json` is written
  for `retention.pruned`, and on the published archive for
  `retention.reclaimed` and `retention archives`), and any byte copies
  (`copied_bytes`, a volume without clones). A remote-less repository's
  bundle is its whole history (section 4, step 2);
- `unlinked_bytes` (`retention.reclaimed`): every file removed from the trees.

The net on disk is `freed_disk_bytes - added_bytes`; what deleting a byte
copy's original gives back is not counted as freed, so it errs low. All are
apparent sizes (`st_size`) except `freed_disk_bytes`. A pool's `bytes_before`
and `bytes_after` count live trees (C-8.4's budget), not disk: the archive is
outside the pools. Each pool in a pass's result also carries the
`freed_bytes`, `freed_disk_bytes`, `archived_bytes` and `added_bytes` of the
jobs it reclaimed; the daemon's catch-up and hourly log lines name all four
and the net; `retention archives` names the added bytes of each archive and
in total; `survey --sample` estimates them per job (the bundle as `git
rev-list --objects --disk-usage` measures the history it would carry, the
manifest from its entries as the builder writes them, `rows.json` exactly) and
reports the net, and the full survey estimates what the retiring jobs'
archives add. APFS snapshots (Time Machine's local ones) keep deleted blocks
until they expire.

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
- **Below the operator's apps** (final review of e50716e8, N9). The daemon
  runs at the default QoS, and a thread's QoS reaches no child
  (`docs/reports/2026-09-27-daemon-qos.md`), so every child retention starts
  (`lsof`, git, the object readers) runs under `taskpolicy -c utility`, the
  guardian's clamp (C-5.1); the in-process steps (archiving, the final check,
  verified deletion, measuring sizes) lower only their thread's disk I/O
  policy (`setiopolicy_np`, thread scope) and put it back. The thread's CPU
  QoS is left alone: it takes the store's lock and the interpreter's, and a
  low-QoS holder of either stalls the daemon; transactions run outside the
  lowered blocks. `utility` is where agent work already runs. Measured on
  2026-09-30 at load 55 to 80, a whole-machine `lsof` took 1.5 and 3.9 s
  under it against 47 and 8 s under `background`, and a cached `git rev-list`
  0.14 s against 9 and 1.3 s: `background` yields to every agent, and with
  dozens running retention would wait behind them. `SUBFLEET_RETENTION_QOS`
  in the daemon's environment chooses `background`, `maintenance` or
  `inherit` instead.

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
   Regenerable entries are not recreated; the report lists each directory
   that dropped some under `not_restored`, with the paths dropped whose parent
   was not. Restored to its original place, the admin directory
   re-registers the worktree (without retention's lock), with its HEAD, index
   and reflogs.

A patch to an installed package comes back without its distribution's
`dist-info` when that `dist-info` verified whole: `uv sync` then reinstalls
the package over the restored patch. Copy a patched file aside, or restore
with `--to DIR`, before letting the project's tools rebuild the environment;
the archive keeps the bytes either way.

Without Subfleet: `manifest.json` lists every entry; `files/<name>` holds each
stored file; `git fetch <archive>/commits.bundle '+refs/*:refs/restored/*'` in
any clone of the project brings back every commit (but those section 15's
held-ref items name; a bundle whose repository
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
  just before the unlink, ctime included, or, for a file that had other links
  when archived and whose ctime alone moved, with bytes that still hash to the
  archived sha256; directories only with `rmdir`. Anything created or changed
  after archiving stays, in conflicts (revision 5: a property over writes,
  links, chmods and interrupted deletions checks the bytes of every entry
  unlinked).
- **I3, commits kept.** Every commit and staged blob the worktree reached
  before retirement, and every salvage commit, is in the bundle or reachable
  from a network remote's refs, so a fresh clone of the remote plus the bundle
  holds all of them, as long as the remote still has what its refs named when
  the bundle was made (section 15); the source repository keeps them all
  through the anchor ref while it exists. No ref is ever moved or deleted.
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
  deleted without a copy exactly when a rule of section 7 proves it
  regenerable (a file), or it is a directory under a tool's entry whose every
  entry was so dropped; the accounting (`regenerable_bytes`, `freed_bytes`) is
  the sum of the regular files so deleted.
- **I12, the archive's own cost is reported.** Every figure of freed bytes
  (the pass result, each pool, both events, the log lines, `retention
  archives`, the sampled survey) comes with `added_bytes`, the published
  archive's bundle, manifest, summary and rows and its byte copies, and a
  remote-less repository's bundle never exceeds
  `retention.remote_less_history_bytes`.
- **I13, a hosted job keeps its own anchor.** A job registered in a
  repository inside another job's tree is never retired in the same pass as
  that job, nor without its registration.
- **I14, never around a tree that is away** (revision 5). A job is not
  retired while another tool holds its tree aside, nor when its tree left or
  came back while it was being retired: a registration is archived and removed
  only with its tree, and a tree is archived only with the registration that
  names it.
- **I15, every allocated tree has an owner** (revision 5). The tree admission
  allocated for a job is retired with that job, recorded in `jobs.worktree` or
  not.

## 15. Residual risks

**Accepted by the d635 ruling** (it accepted only micro-races, `SCM_RIGHTS`
descriptors, and deliberately adversarial same-user tricks):

- A write in the microseconds between an entry's final signature check and its
  unlink (for a file that had other links and whose ctime alone moved: between
  the read that hashes it and its unlink; and a tree another tool moves back
  between the final check and the commit).
- A writable descriptor passed over a Unix socket and held by no process at the
  moment of a listing.
- Deliberately adversarial same-user tricks, among them: a forged RECORD, or a
  forged `direct_url.json`, vouching for an agent's file; data written under a
  tool cache's own file names (`.pytest_cache/v/cache/nodeids`, a numbered
  file in a `.ruff_cache` version directory, `*.data.json` in a
  `.mypy_cache` version directory); data named `<module>.cpython-*.pyc`
  starting with two bytes and CR LF beside a `<module>.py`.

**Not accepted by the ruling**, stated as they stand for Max's decision:

- *Work placed inside a tool's own entries is no longer in this list* (final
  review of e50716e8, N3): until revision 3 it was dropped with them and this
  section wrongly called that accepted by d635; section 7 now archives
  everything no rule proves regenerable, and all of `node_modules`.
- A distribution uv installs from an index or a `--find-links` URL served on
  this machine (a wheel built from a patched checkout in /tmp, served by
  `python -m http.server`) cannot be told from one installed from PyPI:
  measured 2026-09-30 with uv 0.11, such an install writes neither
  `uv_cache.json` nor `direct_url.json`. Its RECORD vouches, and once /tmp is
  cleaned the patched files exist nowhere else. Closing it needs a signal
  outside the dist-info (for `uv sync` installs, a `uv.lock` whose registry
  is on this machine); a `file://` index was not tested and is likely alike.
- Held refs are the network remote's `refs/remotes/<name>/` by name, so refs a
  person fetched there from a local path, or those of a local remote named
  `<name>/<something>`, count as held on the network: for omission, for the
  bundle's boundary (its prerequisites) and for the history limit.
- A remote-tracking ref whose commit the remote itself later dropped (a
  force-push, then the server's gc) counts as held the same three ways. A
  restore into a fresh clone then lacks those commits (and blobs omitted on
  their account); the source repository keeps them, through the anchor ref,
  while it exists.
- `this_machine` does not know every name of this machine: an `~/.ssh/config`
  alias whose `HostName` is `localhost`, its LAN or tailnet address, or
  `2130706433` still count as another machine, with the same consequence as
  the item above for a second repository on the same disk.
- Processes of other users are invisible to a non-root `lsof`; worktrees are
  0700, so only root could hold one.
- Extended attributes, ACLs and file flags are not in the manifest; a clone
  keeps them, a byte copy and a restore do not. A change to them after the
  archive moves a file's ctime: a file with one link then goes to conflicts,
  but one that had other links and still holds its archived bytes is deleted
  (revision 5), with the attributes the clone took.
- A job whose tree and workdir are both gone, in a repository that no job of
  the store is in and no ancestor of its workdir is in (revision 5): its
  registration, if one is left, is not found. Without salvage the job retires
  without an anchor, and the registration stays as it was, neither archived
  nor deleted: its commits are as safe as git leaves a registration whose tree
  is gone (`git gc` prunes it after `gc.worktreePruneExpire`, 3 months), the
  same with the job kept. With salvage it is kept, `repository not found`.
- A tool that moves a job's tree aside to a name other than
  `<anything>.<name>` beside it, before `begin`, and keeps it there: the job
  retires without the tree (its registration is not touched, since its
  backlink names the other place), and a tree moved back later has no row.
  Moved back before the final check, the job is kept (revision 5).

**Costs, not risks:**

- Archives are never deleted automatically. With clones, an archive costs the
  blocks of the files it keeps (they would otherwise have been freed), plus
  `added_bytes`; `retention archives` lists both, and removing one is `rm -r
  <state>/archive/<job>` plus its `refs/subfleet-archive/<job>/*`.
- A job whose baseline no network remote holds, with history over the
  limit, is kept, as before retention by archive, until the base bundle of
  section 17 exists or the limit is raised.
- A job whose baseline a remote holds is not limited, so one that merged or
  checked out a large local history no remote holds (a local `main` far
  ahead of `origin/main`) bundles that history, again for each such job.

## 16. Tests

`tests/unit/test_retention_archive.py` (real git repositories, a fake clock and
a fake process listing), `test_retention_regenerable.py`,
`test_retention_archive_properties.py` and
`test_retention_regenerable_properties.py` (Hypothesis), `test_retention_qos.py`,
and the rewritten `test_retention_worktrees.py`, `test_timers_retention.py` and
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
| N1, what the archive adds | `test_the_space_the_archive_adds_is_reported_wherever_freed_bytes_are`, `test_byte_copies_count_as_added`, `test_retention_log_lines_report_what_the_archives_added`, `test_the_sampled_estimate_nets_what_the_archive_adds` |
| N1, the remote-less history limit | `test_a_remote_less_repository_over_the_history_limit_keeps_its_job`, `test_the_history_limit_is_for_repositories_without_a_network_remote`, `test_a_bundle_over_the_limit_that_the_estimate_missed_keeps_the_job`, `test_the_survey_keeps_what_the_history_limit_keeps`, `test_retention_gets_both_budgets_from_policy_and_the_conversation_services_pins`, `test_conversation_and_retention_values_are_validated[remote_less_history_bytes]` |
| N2, names folded | `test_ignored_compares_names_folded_as_apfs_matches_them`, `test_an_nfd_named_parent_does_not_make_a_visible_venv_ignored`, `test_a_directory_whose_case_changed_on_disk_keeps_its_tracked_edits` |
| N3, only what a rule proves | `test_work_inside_a_tool_entry_is_archived` (the review's four cases), `test_installed_files_are_dropped_only_as_their_record_says`, `test_bytecode_needs_its_magic_number_and_its_source`, `test_a_package_file_changed_after_the_install_is_archived`, `test_a_tool_caches_own_files_are_dropped_and_nothing_else`, the property test (its oracle rewritten for the per-file rules) |
| N4, nested host | `test_a_job_registered_inside_another_jobs_tree_retires_first_with_its_own_anchor`, `test_an_in_flight_host_is_refused_at_commit_and_its_guest_waits_for_it` |
| N5, a remnant registration | `test_a_registration_reduced_to_its_index_and_logs_is_archived_not_kept`, `test_an_admin_directory_that_lost_its_backlink_but_holds_more_keeps_the_job` |
| N6, no progress log published | `test_a_published_archive_keeps_no_progress_log` |
| N9, below the operator's apps | `test_every_child_retention_starts_is_clamped`, `test_the_clamp_follows_its_setting[4]`, `test_a_clamped_git_runs`, `test_the_threads_disk_io_is_lowered_and_put_back`, `test_the_archive_and_the_deletion_run_throttled` |

| Review of the revision-4 build | `test_installed_files_are_dropped_only_as_their_record_says` (a local `--find-links` install, a pip install), the property test's `findlinks` and `pip` cases, `test_a_job_whose_source_was_another_jobs_tree_root_is_not_kept_for_that`, `test_a_guest_whose_host_is_gone_for_good_is_kept_a_day_with_where_its_registration_went`, `test_a_host_that_is_there_without_the_registration_holds_nothing_up`, `test_a_remote_that_holds_nothing_counts_as_none`, `test_a_repositorys_history_is_measured_once_a_pass`, `test_the_survey_remembers_a_bundle_that_came_out_over_the_limit` |
| The re-review | `test_nothing_under_node_modules_is_dropped`, `test_a_dist_info_goes_whole_or_not_at_all`, `test_an_install_from_a_server_on_this_machine_vouches_for_nothing`, `test_a_remote_on_this_machine_is_no_network_remote`, `test_a_remote_that_holds_only_unrelated_history_counts_as_none`, `test_the_survey_keeps_a_job_whose_tree_is_gone_as_the_pass_does` |
| The confirmation review | `test_a_remote_whose_refs_are_from_long_ago_holds_none_of_a_new_baseline`, `test_this_machine_by_any_of_its_names` |
| The final check | `test_a_bundle_kept_from_an_earlier_attempt_is_checked_against_the_limit_now`, `test_this_machine_by_its_short_name` |
| Revision 5, #81's notes (`tests/unit/test_retention_81_notes.py`) | Note 1: `test_a_hard_linked_file_rewritten_with_its_mtime_put_back_is_not_deleted[before-the-final-check, after-the-commit]`, `test_a_hard_link_whose_ctime_moved_only_because_its_sibling_went_is_deleted`, `test_verified_deletion_unlinks_only_the_bytes_the_archive_holds` (Hypothesis). Note 2: `test_a_tree_the_sweep_holds_in_quarantine_keeps_its_job_until_it_is_back`, `test_a_tree_the_sweep_moves_back_before_quarantine_is_not_archived_without_its_registration`, `test_a_tree_the_sweep_moves_away_after_begin_keeps_its_registration`, `test_a_tree_moved_back_while_its_job_retires_without_it_keeps_the_job`, `test_the_survey_keeps_a_job_whose_tree_the_sweep_holds`. Note 3: `test_a_tree_admission_allocated_for_a_job_it_never_recorded_retires_with_it`, `test_a_job_still_to_run_inside_an_unrecorded_allocation_keeps_it`. Note 4: `test_a_gone_workdirs_job_finds_its_registration_through_the_repositories_retention_knows`, `test_a_gone_workdirs_job_finds_its_repository_from_the_workdirs_nearest_ancestor`, `test_a_gone_workdirs_job_with_salvage_retires_with_its_salvage_bundled`, `test_a_gone_workdirs_job_whose_registration_was_pruned_is_found_by_its_salvage_refs`, `test_a_repository_that_does_not_hold_the_jobs_salvage_is_not_taken_for_its_own`, `test_a_job_whose_repository_cannot_be_found_says_so`. Each fix has a mutant its tests kill (`docs/reports/2026-10-01-retention-81-notes.md`) |
## 17. Follow-up: a reference-counted base bundle (lifts the history limit)

Not built (final review of e50716e8, N1). With no network remote, each
retired job's bundle carries the repository's whole history, so the limit of
section 4 keeps such jobs when that history is large. The fix is to pay for
the history once per repository:

- **Layout.** `<state>/archive/.repos/<sha256 of the common directory's real
  path>/`: `base.bundle`, `heads.json` (the base's heads, one ref per commit,
  `refs/subfleet-base/<commit>`), `users.json` (the archives that depend on
  it), and a journal, all written as the archive's files are (temp,
  `F_FULLFSYNC`, rename).
- **Building a job's bundle.** When the repository has no network remote,
  take the base's heads as `held` (`--not <heads>`), after checking that each
  is still a commit in the source repository; the job's bundle is then thin,
  only what the job added. Its manifest names the base (`git.base`: the
  directory and the heads it was made against), and the base's `users.json`
  gains the job, both before the commit transaction. The first job of a
  repository, or one whose history the base does not reach, extends the base:
  a new base bundle of the old heads plus the job's anchor, verified like any
  bundle, replacing the old one by rename.
- **Verification.** Before the commit, the job's bundle is verified in a
  throwaway repository that fetched the base first (its prerequisites are
  the base's heads), with `index-pack --fix-thin`, as today.
- **Restore.** Fetch the base, then the job's bundle; `check_archive` checks
  both.
- **Removal.** Removing an archive removes it from `users.json`; the base is
  removed with its last user. Archives are removed only by a person, so the
  count changes only then.
- **Invariants to test.** I3 with the base fetched first; a base replaced
  while a job's bundle is being built is detected (the manifest's recorded
  heads must be ancestors of the current base's); no base is removed while a
  manifest names it; crash at every step of extending the base.

With it, `~/chief-of-staff`'s roughly 460 MB of history would be paid once,
not per job, and `retention.remote_less_history_bytes` could be raised or
removed.
