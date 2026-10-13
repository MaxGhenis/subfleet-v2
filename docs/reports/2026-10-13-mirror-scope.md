# Where a mirrored session belongs, and pruning the rest, 2026-10-13

C-23.28a. The sidebar mirror copied every openable session into every
`<account>/<org>` folder of the desktop app's session store. This report
measures what that grew to, records what the app does with the folders (read
from its bundle), and describes the scope rules and the removal that bound it.
It builds on `2026-09-24-mirror-load-gap.md` and
`2026-09-25-mirror-consistency.md`.

## The store

| | 2026-09-24 | 2026-09-25 | 2026-10-13 |
|---|--:|--:|--:|
| Folders | 120 | 120 | 135 |
| Copies (`local_*.json`) | 217,706 | 227,924 | 442,801 |
| Bytes (logical) | 2.58 GB | 2.96 GB | 9.51 GB |
| Sessions | about 1,818 | 2,064 | 3,405 |

The 2026-09-25 figures come from a read-only snapshot of every record; the
2026-10-13 ones from the dry run below. In eighteen days the copies doubled
and the bytes tripled: the app now writes about 21 KB per record against 13 KB
then, and 15 new folders were created. The app's loads now take about ten
seconds to list a folder (`Initialization succeeded` at 21:40:14, `Loaded 1264
persisted sessions ... (2175 archived deferred ...)` at 21:40:24 on 2026-10-12).

## What the app does with a session folder

Read from the installed bundle, 2.31226.1 (`app.asar`, `.vite/build/`
chunks, extracted read-only). The 2026-09-25 investigation read 2.9939.2 and
found the same on every point below.

- **The load lists and promotes, and deletes nothing.** `loadSessionRecords`
  reads the directory, promotes an orphaned `local_*.json.tmp`, keeps names
  that start with `local_` and end with `.json`, and contains no unlink, rm or
  rename (`index.chunk-BCHP6ll9.js` @1318914 to @1328943). v1's reason for
  leaving `--prune` off, that the app prunes dead copies itself on load, does
  not hold.
- **Archived records are loaded too, a moment later.** The active phase defers
  records whose head says `isArchived` (or that `archived-sessions.idx` names),
  and its end schedules the archived phase at once:
  `kick:setImmediate(()=>{this.ensureArchivedSessionsLoaded("after_active")})`
  (@1328853). The log shows it (`Loaded 2167 deferred archived sessions from
  ...`). So within seconds of a load the app holds every record of the folder
  in memory.
- **A held record is saved from memory, file or no file.** `writeSessionToDisk`
  returns only if the session is no longer held; otherwise it makes the
  storage directory and writes the whole record from memory, atomically
  (@1339934). A copy removed from the loaded folder would be written back at
  the record's next save.
- **`archived-sessions.idx` needs nothing from the mirror.** The load hands
  the idx the deferred names and the archived records it holds (`adopt`) and
  reconciles it after the archived phase; the active phase walks the
  directory listing and consults the idx only for names in it, so a name
  whose file is gone is ignored.
- **Nothing watches the folder.** None of the 13 watch calls in the bundle
  refers to the session store.
- **Deletes leave tombstones the mirror ignores.** The app writes
  `deleted_<id>` files holding the time (@746545); in 2.9939.2 deleting a
  session removed its record in its own folder only. The mirror reads no
  tombstone and copies the session back from other folders. That is a
  separate defect, not fixed here (task "Stop the mirror resurrecting
  sessions deleted in the app").

## Switch folders

During an account switch the app first initializes the account it is leaving
with the first org of the account it is going to, lists that folder, and
moves on. When the folder is missing it is created: in 2.9939.2 the
scheduled-tasks store wrote `scheduled-tasks.json` on an empty load, and
nothing but a UUID-shape check stood between the org cookie and the pair. The
app's log from 2026-09-14 to 09-25 showed 39 such loads in 75 switches, each
left within 4 s. Of the 120 folders then, 17 were folders the app settled in
(one per account, no two sharing an org), 26 it only passed through, and 77
it never loaded in those ten days; 74 of those 77 have a
`scheduled-tasks.json` born within 0.4 s of the folder, the mark of that
creation.

`folder_kinds.py` learns from the log, and keeps in the state root, which
folders the app settled in (a stay of 20 s, or one a logout ended) and which
accounts own which orgs (a stay of 10 minutes). A switch folder is one never
settled in whose org another account owns. Learning from the log the app
keeps on 2026-10-13 (from 2026-10-05 15:24, about seven and a half days): 17
settled, 100 switch, 18 unknown. The unknown ones are the folders of three
orgs no account stayed ten minutes in within that window (`040097f6…`,
`84eeaed7…`, `7c4b7006…`); they are seeded as before. Once deployed, the
registry keeps an owner after the log that showed it rotates away.

## The rules

| Policy key | Default | Effect |
|---|---|---|
| `sessions.mirror_skip_switch_folders` | true | no session's scope holds a switch folder |
| `sessions.mirror_archive_days` | 14 | an archived, unstarred, openable session quiet this long belongs in the home folder only |
| `sessions.mirror_dead_days` | 7 | a session with no transcript quiet this long belongs in the home folder only, or nowhere |
| `sessions.mirror_prune` | false | also remove copies outside their session's scope |

The home folder is `dead_home` in `~/.claude/cc-mirror.json` (here org
`8b35fb7b…`, resolving to `d1e7c8a9…/8b35fb7b…`). It is resolved over every
folder the store is known to hold, listed that pass or not, and once resolved
it stays put: recorded in the folder registry (one record per `dead_home`
value, so a one-off `--dead-home` pass leaves the daemon's alone), changed or
recorded only by a pass whose inventory is complete (one that is not, such as
a restart that cannot list the home's account, keeps the recorded home), kept
while the store holds it even if another folder
of its org settles or the registry comes to read it as a switch folder (then
reported, and the home is seeded all the same), and kept when the store no
longer shows it. Unhoming the archive would spread every archived session
into every folder in one pass; moving the home would move the whole archive.
A home the store does not hold or the pass could not list, one that reads as
a switch folder, a `dead_home` that names no folder, and a registry that
cannot read the app's logs are warnings in the pass's account and in
`sessions mirror --status`, which keeps the last ones while a pass runs.
Quiet means since the later
of the session's newest activity, focus or creation time over every copy and
its archive stamp. Archiving leaves the activity times alone (of 850 archived
records the app wrote into one folder on 2026-09-22, 621 had been quiet 30
days or more), and the file's mtime is
no clock either (191 archived records in the loaded folder were rewritten
within a day on 2026-09-25), so the merge base stamps the archive: now when it
sees the flag go from unarchived to archived, and, for an archive it did not
see, the newest mtime of the session's copies, once.

Both passes spread a session only into its scope, so the first three rules
stop the growth without deleting anything.

## Removal

With `mirror_prune` on, a full pass removes copies outside their session's
scope, never:

- a copy in the home folder;
- the session's newest copies, when no copy in scope is as new: the newest by
  its activity, focus and creation times, and the newest by the spread's own
  ranking (`lastActivityAt` first). Copies of one session differ (1,396 of
  1,906 sessions with more than one copy, 2026-09-25): the copy where the
  work happened carries the newest record (a moved `cwd`, a changed model),
  and a later focus elsewhere makes another copy newest by focus alone. An
  archived session whose home copy is older keeps two or three copies, and
  the mirror never rewrites one;
- a live session's copy in a switch folder until the registry has learned
  for 30 days: a folder someone uses less often than the registry has been
  learning looks like a switch folder until then;
- anything while the loaded folder is unknown (no load in the log, or a log
  that cannot be read now), or anything in it: the app holds its records and
  would save them back. The log is read again right before each session's
  removals, so a switch since the plan protects its folder;
- an archived or dead session's copy while the home folder was not listed
  that pass;
- a dead session's copy unless a project directory its transcript would be
  in (`projects/<slug>` of a copy's `cwd`) was listed without it: no
  transcript where none was looked for proves nothing. A `projects/` read in
  the wrong place, or recreated empty, would otherwise make every session
  dead. On 2026-10-13, 120 of the 126 dead sessions had their project
  directory listed;
- a session flag sync held or wrote that pass, or anything at all when the
  inventory is incomplete, `projects/` is missing or a project directory in
  it could not be listed (its sessions would read as dead; a symlinked one is
  listed through its link, as the app opens transcripts through it, and one
  whose target cannot be read leaves the listing incomplete, while a link
  that loops is no directory and fails only itself), flag
  sync is off or held, or the store's removal lock cannot be had. The lock
  is keyed by the store's path and lives in a directory no environment
  variable moves (macOS's per-user temporary directory, not `TMPDIR`, which
  launchd's agent lacks and a Terminal shell has), so two mirrors with
  different state roots or environments never remove from one store at once;
  a pass that will remove nothing does not take it.

Each removal re-reads every copy it counts on and the copy (archived and
unstarred, for an archived session; still without a transcript under any
of their cwds, for a dead one), then renames the copy onto a fresh name of the mirror's own and
compares what it took with what it checked (device, inode, mtime, size).
Anything else is put back create-only, so a removal never destroys an app
save it did not inspect, and a crash between the two steps is put back at the
folder's next listing. Two windows remain, both narrower than flag sync's
one: a writer holding the old file open could write into it after the
compare, and an unarchive and re-archive inside one pass keeps the old stamp.
A removal is undone by turning its rule off, which spreads the session back,
or by unarchiving the session where a copy remains.

## Invariants and how they are checked

| Invariant | Where |
|---|---|
| Every session keeps a copy, and its newest by both measures; nothing without a session id or in an unlisted folder goes | `test_mirror_prune_plan.py`, property over random stores |
| Minimal: a copy outside the scope is kept as a newest only when no copy in scope is as new | same |
| Nothing in the loaded folder or the home; nothing while the load is unknown, removal is off, the pass is blocked, or flags are unsettled; no live session's switch-folder copy while the registry learns; no archived or dead session's copy while the home is unlisted; no dead session's copy without a listed project directory | same |
| No churn: a removed copy is outside its scope before and after | same |
| Idempotence: a plan applied leaves nothing to remove | same |
| Accounting: outside = newest kept + held + removed; the holds the construction makes unreachable never occur | same |
| Monotone: longer thresholds, seeding switch folders, a registry still learning, or fewer project directories listed never remove more | same |
| The home is always seeded, even when it reads as a switch folder | same |
| Off is off: with every rule off every session belongs everywhere | same |
| The sessions left unplanned have nothing outside their scope | same |
| Registry: idempotent, the same in any chunking, facts only accumulate, a switch folder was never settled in; nothing folded until a replay has read every log | `test_mirror_folder_kinds.py`, properties over random logs, examples |
| The real pass removes exactly what the planner plans | `test_mirror_prune.py`, differential over random stores (60 examples; 81% remove something) |
| A removal takes only the file it checked; a crash is put back; every scenario from both review rounds holds | `test_mirror_prune.py`, examples |

## The dry run, 2026-10-13 (read-only)

`SUBFLEET_HOME=<scratch> subfleet sessions mirror --dry-run --prune --json`,
with a copy of the live merge base in the scratch state root, so nothing under
`~/.subfleet` or the store was written and no lock the daemon uses was taken.
Run on the code as of review round 1's fixes (04:42Z to 04:47Z), and again,
each with a fresh copy of the merge base, on round 2's (05:22Z to 05:25Z)
and on the final code (`752070614`, 06:00Z to 06:02Z, with no warnings), which
gave the same numbers to the copy:

| | Copies | Bytes |
|---|--:|--:|
| In the store | 442,808 | 9.51 GB |
| Outside their scope: archived sessions outside the home | 222,981 | |
| Outside their scope: live sessions in switch folders | 161,500 | |
| Kept: a session's newest copy, outside its scope | 1,421 | |
| Kept: in the loaded folder, until the app leaves it | 1,653 | |
| Kept: in switch folders, while the registry learns | 161,500 | |
| Removed on the first pass with `mirror_prune` on | 219,907 | 2.68 GB |

The switch-folder copies wait for the registry's 30 days of learning; an
earlier dry run the same day, before that rule, would have removed them too
(381,751 copies and 7.69 GB in all, leaving 61,050 copies and 1.82 GB).

Sessions: 1,615 live, 1,664 archived and quiet 14 days, 126 dead (all quiet
over a week, and already only in the home folder: none had a copy outside
it). Folders: 17 settled, 100 switch, 18 unknown. Home:
`d1e7c8a9…/8b35fb7b…`; loaded: `1ec0b854…/e7460da2…`.

The cold pass took 263 s (164 s for the earlier run; the host was busier) and
peaked at 667 MB resident (821 MB for the earlier run, which planned every
copy at once); round 2's took 210 s and peaked at 677 MB, and the final
code's 125 s and 676 MB. The mirror now
plans one session at a time, with removal off only on the ten-minute sweep,
and decides spread scope from a per-session summary. The registry's replay
read all five logs (`main4.log` to `main.log`, about 50 MB) and found the
same folder kinds.

## What remains

- Turning removal on is the operator's call (`sessions.mirror_prune: true`
  in `~/.subfleet/policy.json`, then a daemon restart). The first pass would
  remove about 220k files (2.7 GB); the switch folders' 161k follow once the
  registry has learned 30 days; later passes remove what newly ages out.
- A folder an account uses less often than the registry has learned gets no
  new sessions until someone stays in it 20 s (then it is seeded and the app
  lists them at its next load); removing from it waits the 30 days.
- Tombstones: a session deleted in the app is copied back from other folders
  (separate task).
- Claude Code's transcript retention: the app renews the mtimes of transcripts
  for sessions it holds. Retention is 3650 days here, so this is moot; with a
  short `cleanupPeriodDays`, a session kept only in folders the app does not
  load could lose its transcript sooner than before.
- Accepted minor points from review (five rounds, reports beside the PR):
  - An unarchive and re-archive inside one pass interval keeps the old
    archive stamp.
  - `--status` describes the last pass, so a one-off `sessions mirror` pass in
    the daemon's state root shows its own warnings until the daemon's next
    pass (60 s). An empty store clears them.
  - On an incomplete pass, a home that was really deleted reads as "could not
    be listed", and an org whose only account failed to list as "no folder";
    both clear at the next complete pass.
  - The folder registry file has no lock. A save keeps the home records
    another process wrote, but one written between a save's read and its
    write is lost, and a daemon writes back as its own the records it read at
    start. A lost record costs only a fresh resolution of that value.
