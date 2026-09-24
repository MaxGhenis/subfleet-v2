# Sessions missing from the sidebar after an account switch, 2026-09-24

## What happened

At 16:35:46 EDT the desktop app switched from account `0217c50d…` to
`a1767bbd…`, and at 16:38:05 to `d1e7c8a9…`, org `8b35fb7b…`. The switch tore
down seven running sessions, among them `local_e7e41400-…` ("Operationalizing
certified components"). The app loaded the `d1e7c8a9…/8b35fb7b…` session folder
at 16:38:11, relaunched at 16:59:45, and loaded the folder again at 16:59:47.

That session's record, and 18 others from sessions last active under accounts
`ee3763f4…` and `0217c50d…` between 13:45 and 16:35, reached the folder at
17:13:39 to 17:14:09. The file ctimes show the copy times. That was after both
loads, and the running app never listed them. `get_session` answered "not
found" for `local_e7e41400-…` and `local_17085712-…`, and `list_sessions`
omitted all 19.

A diff of every record in the folder against the app's list split them into
three groups:

- 45 records present before the 16:59:47 load: all listed.
- 19 copied after that load and not otherwise touched by the app: all missing.
- 11 changed after the load by the app itself: all listed.

The app relaunched again at 17:24:45 and loaded the folder at 17:24:47. All 19
were then listed: `get_session` found each of them. This is the positive
control: the files, unchanged since 17:14:09, became visible only through a new
load.

## How the app reads a session folder

Read from the app bundle (version 2.7032.0, `app.asar`, main-process chunks
under `.vite/build/`):

- **The only reader.** `doInitialize` → `loadSessions` → `loadSessionRecords`
  is the only code that puts `local_*.json` records into the in-memory list.
- **What starts a load.** `doInitialize` runs only through
  `initializeWithAccount`, which runs:
  - at launch;
  - when the account changes, including the first login after a logout;
  - when the `lastActiveOrg` cookie changes;
  - when a new app window builds its session API.
- **Nothing reloads it otherwise.** No watcher, timer, focus handler, IPC call,
  deep link or MCP tool reloads the folder. `list_sessions` answers from
  memory. The only `fs.watch` calls in the bundle watch other paths.
- **The load keeps two kinds of file.** It keeps names that start with
  `local_` and end with `.json`. It also promotes an orphaned
  `local_*.json.tmp`.
- **What the log lines mean.** `Initialization succeeded — accountId=…,
  orgId=…, existingSessions=N` is logged before the list is cleared and the
  folder is read. N is the list's size before that. `Loaded N persisted
  sessions from <folder>` follows the read.
- **A re-login is not a full reload.** A re-login to the same account and org
  re-reads the listing without clearing the list, so it adds new ids only.
- **Writes are atomic.** A record is written as `<file>.tmp`, fsynced and
  renamed. A plain write is used only when the rename fails. The whole record
  is serialized from memory, with no read-merge.
- **Parked sessions.** Sessions running at a switch are parked, and keep
  saving into their original folder.

So the mirror's copy reaches a sidebar only if it is in the folder when the
app loads it. The module docstring said "Copying the index into every folder
unifies the sidebars". That held only for records copied before the next load.

## Why the copies were 35 minutes late

`local_61cbd005-…` was created in `1d7d2285…/02fd7d4d…` and last written there
at 13:47:12 EDT. Its copies in the other 119 folders all have ctimes of
17:13:39. No copy of it existed anywhere else for three and a half hours.

- **Timer events stopped being a minute apart.** The daemon's `timer.run`
  events for the mirror were one minute apart until 16:14:31Z. After that they
  came at 16:33:31Z, 17:07:45Z, 17:11:15Z, 17:16:13Z, 17:18:10Z, 18:03:20Z,
  18:54:31Z, 20:55:59Z, 21:14:17Z and 21:21:12Z. A `timer.run` event is written
  when a pass ends for any reason, including cancellation. The pass's own state
  is only in the sidecar, which keeps one pass.
- **Restarts discarded progress.** The daemon restarted at 20:46:02Z, 20:56:02Z
  and 21:21:15Z (`admission.recovered`). Each restart cancels the pass in flight
  and discards its in-memory cache.
- **Cancelled passes were far from done.** The pass cancelled at 21:21:12Z had
  read 116,859 of about 218,000 entries. The next one read about 170 entries per
  second, so it needed about 21 minutes to finish reading, before copying
  anything.
- **The cache was too small for the data.** The store held 217,706 entries in
  120 folders (2.58 GB), with 9,619 distinct contents. Parsed in full, those
  distinct payloads take about 299 MiB of Python objects (tracemalloc), and the
  cache was capped at 128 MB. `_remember` does not cache an entry whose payload
  does not fit. Most entries were therefore re-read and re-parsed on every
  pass, not only after a restart.
- **The archive walk found nothing.** Every pass also walked the 56k-file
  archive glob for 126 dead sessions, none of which is in it.

## The fix

Commit on `fix/mirror-load-time-gap`; clause C-23.28, second half.

- **Hot pass.** Every `sessions.mirror_hot_interval_s` (2 s) the daemon runs a
  hot pass on the mirror's worker and lock.
  - It re-lists only folders whose directory mtime changed and spreads each new
    or changed record at once.
  - The app writes by rename, so every app write changes its folder's mtime.
    That includes the old account's folder, where parked sessions keep saving.
    The live store had 0 in-place rewrites among 217,706 files, and an 8-minute
    watch saw every write bump the directory.
  - A record whose transcript does not exist yet is retried on later hot
    passes.
  - The hot pass does not wait for a logout. On 2026-09-24 a logout preceded
    the next load by 12 s to 10.5 min, but an org switch loads the next folder in
    the same second it is logged. What matters is that everything written up to
    ~2 s before any switch is already in every folder.
- **Incremental full passes.**
  - The cache holds only the twelve fields a pass reads, keyed by a hash of the
    file's bytes: one payload per distinct content, 9,619 payloads in 16.4 MB
    on the live store.
  - A folder with an unchanged directory is not re-listed, and a changed one is
    diffed by inode.
  - A stat sweep every 10 minutes catches a rewrite that kept its inode.
  - Transcripts are listed one level down (the 34k deeper `.jsonl` files are
    subagent logs), with a per-directory cache.
  - The archive is re-walked only when a new dead session appears, or every 30
    minutes.
- **The load-gap report.** Every copy and rewrite the mirror makes is journaled
  in `<state root>/sessions/mirror-writes.json` with the file's ctime.
  `sessions/desktop.py` tails the app's `main.log` for the loads it records.
  - A copy into the loaded folder counts as waiting for a relaunch when all of
    these hold:
    - it postdates that load, meaning the "Initialization succeeded" second or
      later;
    - the app has not rewritten it since (a rewrite means the app holds it);
    - its session is not archived.
  - A copy that replaced a stale empty record waits for a load that started from
    an empty list.
  - The count and titles appear in `subfleet sessions mirror --status` (text
    and `--json`), in a note under `subfleet sessions list`, and in a new
    `desktop sidebar load` doctor row, which uses the new `warn` status.
- **Write safety.**
  - Copies are assembled beside the destination and renamed in, keeping the
    source's mtime and mode.
  - New files are owner-only (0600), like the app's own. Before this, the
    mirror's rewrites and fallback copies took the process umask, and 200,852
    of the 217,706 records were 0644. All 120 org folders are 0700, so no other
    user could reach them.
  - A copy whose source vanished no longer aborts the whole pass.
  - Flag sync re-reads the record and patches only the synced fields. If those
    fields moved since the inventory, it skips the write and holds that
    session's merge base, so the next pass decides on what is there.
  - When the app's latest load found its folder missing (a first login to that
    account and org), the full pass creates the folder, so it can be seeded and
    a relaunch lists it.

Measured against the live store with dry runs of the new code (no store writes):

| Pass | Before (daemon, 2026-09-24) | After |
|---|---|---|
| Full pass, cold (after a restart) | ~21 min reading alone at 170 entries/s; often cancelled | 42.5 s |
| Full pass, warm | re-read most entries; ended 19 to 120 min apart after 16:14Z | 1.4–1.8 s |
| Hot pass, nothing changed | (none) | < 0.01 s |
| Hot pass, 119 folders changed | (none) | 1.4 s |
| Stat sweep, every 10 min | (every pass) | 8.9 s |
| Load gap from the live log | (none) | 0.1 s |

After a cold pass the new mirror retains 200 MiB of Python objects (tracemalloc;
peak 257 MiB). Most of that is the per-entry signature index: its keys are path
strings, because a `Path` key also retains its parsed parts, which measured
about 300 MiB more. It keeps only the dead sessions' archive files. The
daemon running the old code had 1.1 GB resident at the time; that is a
different measure (RSS), so it is given only for scale.

## What remains

- **Records written less than one hot interval before a switch.** Such a record
  can still miss the load. The report then counts it.
- **Writes into the loaded folder.** A flag, title or setting the mirror writes
  into the folder the app has loaded does not reach the running app, and the
  app's next save of that record overwrites it there. The report counts these
  as `stale`. Preventing that needs the app to reload, which only it can do.
- **Dependence on the app's log.** The report relies on the app's log lines.
  The log is a diagnostic, not an interface. If the lines change, the report
  says `unknown` rather than guessing, and copying is unaffected.
- **A first login to a new account and org.** Its sidebar is empty until a
  relaunch after the mirror creates and seeds the folder, and the report says
  so.
