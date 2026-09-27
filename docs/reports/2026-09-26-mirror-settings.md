# One model and one place per session across accounts, 2026-09-26

The desktop sidebar mirror kept every account's folder holding a copy of every
session, but only the copy's flags and title stayed in step. This report records
what was stale, what the app does with the stale fields, the rule the mirror now
uses to converge them, why that rule rather than a simpler one, how it is
verified, and what it still cannot do. It builds on
`2026-09-24-mirror-load-gap.md` and `2026-09-25-mirror-consistency.md`.

## What was stale

The census on 2026-09-26, 04:43Z (read-only, 121 folders, 232,958 records, 2,061
sessions grouped by `cliSessionId`) compared every copy with the session's most
active copy (greatest `lastActivityAt`):

| Field | Open sessions with a stale copy / stale copies | All sessions |
|---|---|---|
| `model` | 32 / 2,548 | 181 / 7,625 |
| `effort` | 1 / 7 | 44 / 1,344 |
| `cwd` | 18 / 1,667 | 68 / 3,504 |
| `worktreePath`, `worktreeName` | 31 / 1,918 | 132 / 5,314 |
| `originCwd` | 4 / 5 | 9 / 240 |

2,311 copies of 20 open sessions said Fable where the newest copy said
`claude-opus-5-5`. In the folder then loaded (`c0861266…/eedf19ad…`), 19 open
sessions had a stale model, `cwd` or worktree (14 a stale model or `cwd`), and
5 of those 19 copies had been re-saved by the app after the load with the stale
value.

The cause: `_spread` copies a session only into folders that lack it, and flag
sync patches only `isArchived`, `isStarred`, the title and `sessionSettings`.
Nothing ever updated `model`, `effort` or the place fields of an existing copy.

## What the app does with them

Read from the app bundle, 2.9939.2 (`app.asar`, `LocalSessionManager`), on
2026-09-26:

- **A resumed session runs on its copy.** The spawn options come from the
  record: `model` becomes `--model` and `effort` becomes `--effort`. The
  working directory is `worktreePath || cwd` for a warm start and `cwd` for a
  cold one. The transcript is resumed by `cliSessionId`, then copied under the
  working directory's project folder if a larger copy is elsewhere.
- **A pick never raises `lastActivityAt`.** The model picker, the
  `set_session_model` tool and `applyModelByRestart` all end in
  `commitSessionModel`. It sets `model`, may re-clamp `effort`, and saves. It
  never touches `lastActivityAt` or `lastFocusedAt`. `set_session_effort` is
  the same.
- **Moves change the place fields together, without raising it either.**
  - `change_directory` applies after the turn: `cwd = originCwd = the new
    directory`, `gitAnchors` recomputed, grants under the old directory dropped.
  - Worktree creation, recovery and SSH recreation set `cwd`, `worktreePath`,
    `worktreeName`, `branch` and `sourceBranch` together.
  - A worktree detach unsets `worktreePath` and `worktreeName`.
  - `EnterWorktree` and `ExitWorktree` in the CLI change only memory
    (`harnessCwd`), not the record.
  - Only a new record (create, fork) sets `lastActivityAt` along with a place.
- **What raises `lastActivityAt`:** turns and their frames, sends, clear,
  rewind, and every respawn of an existing record (`createOrResumeSession`).
- **No persisted order for settings.** `modelChangeSeq` and `modelRecordSeq`
  order picks within one app run, and neither is saved.
- **The app saves its whole in-memory record** on a 1 s debounce (3 s while
  running), with no read-merge. It never re-reads a record it holds. A session
  running at an account switch parks and keeps saving into its old folder.
- **Ids.** `/clear` records the old `cliSessionId` in `priorCliSessionIds` and
  empties it. An undone clear puts the newer id into `priorCliSessionIds` and
  restores the older one. A resume that finds no conversation drops the id
  without recording it.

## Why neither simple rule works

**"The most active copy wins"** is the brief's rule. Because a pick never
raises `lastActivityAt`, it overwrites any pick made in a folder that is not
the most active. The live store has the case: in 10 open sessions, 120 copies
last ran on Fable at about 00:52Z on 2026-09-23. A few minutes later, one
account (`9921292e…/84eeaed7…`) picked `claude-opus-5-5` on its copy without a
turn. The newest-activity rule would put Fable back into that copy too.

**"Any change from the merge base wins"** is the flag protocol's rule. The app
re-saves its whole record from memory on focus, on a PR poll and for any other
field. So once the mirror writes a new value into a folder the app has loaded,
the app's next save puts the old one back, and the merge base reads that as a
change. For flags this is the known limit of the 2026-09-25 report. For
settings it would spread Fable from every folder whose memory still held it,
with no one doing anything.

Both are checked as mutants of the model below: the first breaks "intent
wins", and the second breaks "never undo a settled value".

## The rule

Each session's settings are decided from every copy, unit by unit. Each unit
is taken whole from one copy, so a record never mixes two moves.

| Unit | Fields |
|---|---|
| model | `model` |
| effort | `effort`, `effortInherited` |
| place | `cwd`, `originCwd`, `worktreePath`, `worktreeName`, `worktreeLazy`, `branch`, `sourceBranch`, `gitAnchors`, `gitAnchorsLookupOnly` |

An absent field is a value: a detached worktree spreads as detached.

The fields that stay per account:
- Permission grants (`sessionPermissionUpdates`), which are per-account permissions.
- Worktree retention (`worktreePinned`, `keptDirty*`), which the app's reaper reads.
- `permissionMode`, which the CLI reports at init.

The settings base, in `mirror-flags.json` under the session's `settings`, holds:
- `rank`, the greatest `lastActivityAt` the deciding pass read;
- per unit, the decided value (`v`, its digest, and `value`);
- per unit, `seen`: the digests of every value a deciding pass read or a publish displaced.

`decide_setting` applies, in order:

1. **agree**: every copy holds the same value.
2. **first** (no value decided yet): the most active copy wins, by rank, then
   latest write (mtime), then folder order. There is one exception, for model
   and effort. If a value is held only by copies the app wrote more than 60 s
   (`SETTLE_MS`) after the session's last activity, it was picked after that
   activity, and the latest such write wins. This does not apply to places: a
   model fix re-saves the whole record, stale place included, so a place saved
   late is no evidence of a move.
3. **new**: a value no pass has seen wins, the most active such copy first.
   The app's memory holds only values it read from a file. The mirror read
   every value a file held before it overwrote it, so an unseen value can only
   be a change the app made since: a pick or a move.
4. **activity**: a copy whose rank exceeds the base's `rank` had a turn or a
   respawn since the last decision. The most active such copy wins.
5. **base**: otherwise the decided value stands. A copy that differs without
   new activity holds a value the app re-saved from stale memory, or a pick of
   a value the session had before. The latter spreads with the session's next
   activity in that account.

**Publishing** is flag sync's own publish, whole per session:
- Before writing, every copy is re-read. It must still hold what the pass read
  in its flag fields and in every setting field. A copy getting a setting
  write must also still have the same rank, so a copy that ran a turn since
  the read is never overwritten with older values. If any check fails, the
  session is held and decided again next pass.
- A write that finds its copy saved since the check puts back the copies
  already written.
- Every write keeps the file's mtime, which is the sidebar's order.
- Before the first write, the values the publish will displace are added to
  `seen` and the base file is synced: a write-ahead. Without it, a crash
  between the copy writes and the base write could let a displaced value that
  the app still holds in memory read as new.

## Invariants and how they are established

These hold for every input. The model twin and its tests state each one
precisely.

| Invariant | Statement |
|---|---|
| Convergence | One pass with nothing written in between leaves every copy and the base on the decided value, and that value is one some copy held. |
| Idempotence | A converged store decides itself and writes nothing. |
| Newer never overwritten by older | The mirror writes a value over a copy only if one of these holds: a copy holding that value is at least as active; the value is a change no pass had seen; or the copy has had no activity since the base was decided. |
| No lost update, all or nothing | A publish writes only copies unchanged, in value and rank, since the read. A held session writes nothing and keeps its decided value and rank; only `seen` grows. |
| Keeps rank and mtime | The mirror never changes `lastActivityAt` or a file's mtime. |
| Never undo a settled value | With no pick and no activity since a clean publish, no pass decides anything else, even when the app re-saves stale values. |
| Intent wins | A single pick of a value no pass had seen is decided, even against a later turn elsewhere. |
| Activity wins | With no pick since the last clean publish, the most recent activity's value is decided. |
| Flags untouched | A store's flag outcome is the same whatever its settings. |

| Method | Where | Result |
|---|---|---|
| Exhaustive model check | `tests/mirror_settings_model.py`, explored by `tests/unit/test_mirror_settings_model.py` over three folders, every event and pass. The app has current memory (`honest`) or stale memory with parked folders | RESULTS_MODEL |
| Differential | `tests/unit/test_mirror_settings_stateful.py`: a Hypothesis machine drives the real `Mirror` on real files in lockstep with the model: picks, turns, stale saves, switches, passes with writes between the read and the publish, cancellations, unreadable copies | RESULTS_STATEFUL |
| Properties | `tests/unit/test_sessions_mirror_settings.py`: random three-copy stores (model, effort, place, activity, write times, flags) | convergence to a value some copy held; idempotence; first decision = the most active copy's place and model (or the later pick); flags equal to the same store with uniform settings |
| Examples and faults | same file | first decision, ties, later picks, a pick in a less active account, stale saves, a stale-memory turn (the known limit), a turn during the pass, a save between two writes (rollback), a crash after the write-ahead, an unreadable copy, cancellation, the switch, a dry run, the journal, diverged ids |
| Mutation | RESULTS_MUTANTS | RESULTS_MUTANTS_KILLED |

## The rollout

A dry run of this code against the live store, with an empty state root (so
every session is at its first decision, as after install), at RESULTS_DRYRUN_AT:

RESULTS_DRYRUN

After install:
- **Relaunch the desktop app once the first full pass has finished.** The
  running app holds the loaded folder's records in memory from before the
  pass. A session opened or run there before a relaunch runs on the old value.
  Its respawn raises `lastActivityAt`, so the next pass spreads what it ran:
  the known limit below.
- Sessions whose last activity was on Fable stay Fable, because that is what
  they last ran. To move one, pick the model in the loaded account. A model no
  pass has seen for that session wins everywhere. A model the session had
  before takes effect everywhere once the session runs or is opened in that
  account.

## Conversation ids that diverged

The census found 12 session files whose copies hold different `cliSessionId`s,
one of them open ("Partner", `local_ace3f157…`):
- 93 copies hold `67eb7695…`, which was active up to 2026-09-25.
- 28 copies hold `181ca5ca…`, which lists `67eb7695…` in `priorCliSessionIds`
  and was last active on 2026-08-28.

So the id that `/clear` produced is the stale one, and the older id kept
running where no clear happened. `priorCliSessionIds` does not order ids, since
an undone clear records the newer one. The mirror therefore reports these
(`ids_diverged` and `diverged` in the pass record and health, and a line in
`sessions mirror --status`) and does not rewrite them.

One more consequence: the spread's name-collision fallback put each id into
every folder under `local_<cli>.json`, so "Partner" appears twice in every
sidebar. That predates this change and is a separate follow-up.

## Known limits

- **Stale-memory activity spreads.** A turn or respawn in a folder the app
  loaded before the mirror's write runs on the value the app remembers. It
  raises `lastActivityAt`, and nothing on disk tells it from a deliberate
  switch back followed by a turn, so what ran spreads. This is the model's one
  failing property ("no stale resurrection"). A relaunch after a converging
  pass removes the stale memory.
- **Repeated picks need activity.** A pick of a value the session held
  before, made without a turn, looks exactly like a stale re-save. The mirror
  keeps the decided value on disk. The app keeps the pick in memory, and the
  session's next activity in that account spreads it.
- **The first decision is heuristic** for a model or effort picked on a less
  active copy. It needs every holder of that value written more than 60 s
  after the session's last activity.
- **Crash windows narrow, not closed.** The write-ahead covers a crash
  between the copy writes and the base write. As in the flag protocol, an app
  rename in the instant between the mirror's last check and its own rename is
  overwritten.
- **Parked sessions and mirrored copies.** The bundle shows that when a
  parked session also has a copy in the newly loaded folder, the app loads
  that copy and routes its saves into the parked account's folder. This
  predates this change and is recorded as a follow-up.
