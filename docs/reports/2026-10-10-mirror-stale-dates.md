# A stale date beside a stale fork, 2026-10-10

Max opened the session "Partner" under max@axiom.org and found it six weeks
behind. He had opened a fork of it. This report records what the store held,
why the fork looked like the newer of two rows, what the mirror now does about
it, the invariants that change keeps, how each is checked, and what is left for
Max to rule on.

## What happened

- **One session id, two transcripts.** The app's id `local_ace3f157…` opened
  transcript `67eb7695…` under 93 account folders and `181ca5ca…` under 41.
  The second begins with a prompt that was queued on 2026-08-28 when a usage
  limit forced an account switch.
- **Two rows, one title.** Both transcripts exist, so the mirror gave each a
  row in every folder (below). Both were titled "Partner", and both were pinned.
- **The dates.** Under the loaded login the row of `67eb7695…` showed
  2026-08-16, and the row of `181ca5ca…` showed that day. The session behind
  the first row had last run at 12:26Z that morning, under another login. Max
  opened the second.
- **A restart changed nothing,** because nothing was wrong in the app's memory:
  the dates were in the files.
- The fork was unpinned and archived that day at Max's request.

The first three points are as the session that diagnosed it recorded them
(`reference_stale_fork_same_title_sidebar` in Max's memory). By the time of
this survey Max had used both rows, so both carried that day's date. The fork
holds five typed turns the original does not (three from 2026-08-28 and two
from 2026-10-10), and the original holds 225 the fork does not.

## What the store held

Read-only survey of every record, 2026-10-10 about 14:30Z to 16:30Z: 134
account folders, 410,585 records, 14,940 distinct contents, 3,189 conversation
ids, 3,063 of them with a transcript.

**Stale dates, with no fork involved.**

- Every record held an integer `lastActivityAt`.
- Of the 3,063 conversations, 999 had an unarchived copy. Of those 999 rows in
  the loaded folder, 663 showed a date more than an hour older than the
  newest copy of the same session, and 229 more than a day older. The furthest
  was 61.3 days behind (it showed 2026-07-28; its newest copy said 2026-09-27).
- A dry run of the new code on the live store found 750 sessions with a copy
  more than an hour behind, 94,383 copies in all.

So the date under one login was the date of that login's own last run of the
session, or of the copy's placement. The fork only made it matter.

**Session ids that open two conversations.**

- 12 record names held more than one conversation with a transcript. Five
  were split 133 folders to 1. The most even were 94 to 40 and 93 to 41
  ("Partner").
- Ten of the 12 pairs had one title. In four of the 12, one side's records
  name the other in `priorCliSessionIds`; in the other eight, neither side's do.
- By the brief's test (a copy names a prior conversation, both transcripts
  exist, and the prior one has the later last typed turn): two, "Partner" and
  "NSF POSE Phase 2 application". A typed turn here is a main-chain user entry
  whose `origin.kind` is `human`.
- Only "Partner" had an unarchived side. With its fork archived, **no session
  is in that state now**: the 12 splits each show at most one row.

## What the mirror did

Read in `subfleet/sessions/mirror.py` at release/217 `f09ad5f1`:

- **The second row is the mirror's.** `_spread` puts each openable
  conversation into every folder that lacks it. Where the name is taken by
  another conversation, it writes the copy as `local_<conversation>.json`.
  The second rows in the store carry those names: the original is
  `local_67eb7695….json` in the 41 folders, and the fork is
  `local_181ca5ca….json` in the 93. That is the collision rule working as
  written: both transcripts are sessions.
- **The date was copied once.** `lastActivityAt` is in `PROJECTED`, which a
  pass reads, and not in `FLAG_WRITES`, which a publish writes. A copy carried
  the date of the record it was copied from, and nothing raised it afterwards.
- **Flags are grouped by conversation,** so archiving the fork under one login
  archived the fork's copies everywhere and left the original's alone.

## What the app does with the date

Read in the app bundle 2.31226.0 (`app.asar`), main process only:

- `codeCleanupHold` treats a session as recent while the later of its
  `lastActivityAt` and its last focus is within a number of days, and
  `listCodeCleanupCandidates` lists the others. So a copy with a stale date
  and no recent focus counts as idle there, whatever the session did under
  another login.
- A lookup of the "last" session takes the unarchived one with the greatest
  `lastActivityAt`.
- The other uses read treat a later date as a more recently active session:
  a worktree is not taken from a session "active since the scan", an archived
  session's kept worktree expires by the later of this date and two others,
  and the CLI pin is reused only within an hour of it.

The sidebar is drawn by the renderer, which this bundle does not hold, so how
the sidebar orders its rows was not read. What was observed is that a row's
date matched its record's `lastActivityAt`.

A poll of every record modified in the previous 15 minutes (277 records, for
139 s) saw four saves of one record and no change of `lastActivityAt`: the
app does not rewrite the date many times a minute.

## The change

**1. The date follows the session.** The publish that syncs a session's flags
also raises `lastActivityAt`:

- a copy more than `sessions.mirror_activity_lag_s` (3600 s) behind the
  session's newest copy is raised to one millisecond before the newest;
- a copy within the lag is left alone, so a session in use does not rewrite
  133 files on every turn;
- zero switches the sync off, as zero does for every window under `sessions`;
- a session whose flag decision is archived keeps its dates;
- one flag sync raises at most ten sessions, those furthest behind first, and
  counts the rest in `activity_waiting`, so the 750-session backlog drains
  over about 75 passes and no publish is long;
- a session it chose and could not publish goes last in the next sync.

A date is a voice only if it is a number above zero, within JavaScript's safe
integers, and not more than five minutes past the pass's own clock. Anything
else in the field is left as it is and decided without.

One millisecond short, because `_rank` picks the record a new folder is copied
from by this date. The copy the app last ran the session in holds the model
and folder it last ran with; if a raised copy tied it, a new login's folder
could be copied from the older record.

**2. Split ids are reported.** A full pass records each record name whose
copies hold more than one conversation with a transcript, and how many of them
still show two rows. Only a pass that listed every folder and read every copy
replaces the report, and no process puts an older report over a later one. `sessions mirror --status` names each of those with both
conversations, the folders that open each, and each one's newest date.
`doctor` has a row for it: `warn` while one shows, and `unknown` when no pass
has replaced the report for thirty minutes. The mirror archives, retitles and
rebinds none of them.

Not built, and Max's to rule on: archiving or retitling the older side of a
split without asking (decision d1258).

## Invariants

For one session, with the date in each folder's copy:

- **Bounded lag.** A pass that decides for a session decides for every copy
  more than the lag behind its newest, and a pass with nothing rewritten
  meanwhile leaves every copy within the lag.
- **Raise below newest.** Every date written is above the copy's own and
  below the newest the pass read. The mirror invents no date.
- **The lead is kept.** No write changes the newest date or which copies hold it.
- **Never lowered.** A write that goes through lowers no copy. A later date
  the app saved between the pass's read and its write is kept.
- **Not from the future.** A date more than five minutes past the pass's own
  clock is no voice: it is not raised, and it is never the newest. The app
  writes its clock's now, so such a date is a bad record. A raise is never
  undone, so a bad date raised from would sit in every folder and be raised
  again from each. The date sync therefore never spreads one. No record in
  the store held one.
- **Any number.** Whatever number a record holds in the field, no pass fails
  on it. A number not above zero, or outside JavaScript's safe integers, is
  no voice. Within them one millisecond before a date is a different number,
  exactly.
- **A date is not a flag write.** A write that changed only a date changes no
  field a flag decision reads, so it does not make the full pass read the
  flags again. Flags, titles and bases keep the liveness they had.
- **Fair under the bound.** A session chosen and not published yields its
  place in the next sync, so sessions that never go through cannot keep the
  bound from the rest.
- **Idempotence.** With every copy within the lag, nothing is decided and
  nothing is written.
- **No lost update.** The mirror writes only a copy nobody rewrote since its
  pre-check or since the mirror wrote it.
- **All or nothing.** The date is in the session's batch. A held session
  writes no date. A write that finds its copy rewritten puts back every copy
  the batch wrote, date and flag, except one rewritten since.
- **Cancellation safety.** A cancelled pass changes no copy.
- **The merge base is untouched.** It holds no date, and a pass's flag, title
  and base decisions are the same with the sync on or off. Across passes, the
  title rule's tie-break among several changed copies reads this date; the
  copy that leads keeps its lead there too.
- **Stays fresh.** Once a pass left every copy within the lag, each stays
  there until the session next runs.

The flag protocol's own invariants (2026-09-25 report) are unchanged. Its
batch is wider: it can now hold a copy whose flag needs no write. Such a copy
holds the session if its flag moved, and a failed write of it puts the batch
back.

**Known limit (intended).** The app saves a record from memory. A save from
memory that predates a raise puts the old date back in that one copy. Stays
fresh then fails for that copy until the next pass raises it again. Nothing
else fails: where a stale flag reads as a user's change and spreads, a stale
date is only behind, and a later date always wins. In the loaded folder the
mirror writes once for each such save by the app.

**A second limit, older than this change.** Which record a new folder is
copied from is `_rank`'s rule, and it takes the latest date. A record with a
date from the future can therefore be the one copied into a new login's
folder, whole. The date sync does not raise any copy toward it.

**Outside the models.** Each model is one session. Fairness across sessions
under the bound, and the full pass's refresh after an embedded hot pass, are
tested by example, not explored.

## Verification

- **The date's model, every reachable state**
  (`tests/mirror_activity_model.py`, the twin of
  `docs/formal/MirrorActivity.tla`): 338,227 states for three folders and two
  turns with an honest app, every property holds. With an app that re-saves
  an older date, 157,959 states through one turn, and only stays fresh fails;
  its shortest trace ends in the app's save, not in a write of the mirror's.
  Run by hand to three turns: 1,854,089 states honest, 8,888,527 stale, the
  same result.
- **The flag model with the wider batch** (`tests/mirror_flags_model.py`,
  `docs/formal/MirrorFlags.tla`): every subset of copies added to a publish,
  and every subset of those left out at the pre-check. 485,624 states honest
  (22,038 before), nothing fails. 950,057 states stale (44,058 before), and
  the same two properties fail as before the change.
- **The shipped decision** (`mirror.activity_targets`): Hypothesis properties
  for the bounds above on any dates and any lag, independence from folder
  order, and agreement with the model's own statement of the rule.
- **The mirror against both models at once**
  (`tests/unit/test_mirror_activity_stateful.py`): a Hypothesis state machine
  drives the real `Mirror` on real files through turns, archives, account
  switches, stale saves, writes between the read and the pre-check, writes
  between two of the publish's writes, and cancelled passes. After every step
  each file's flag and date and the merge base equal the models'. 150 traces
  of 30 steps. The run counts the shapes it reaches and fails if one is
  missing: a copy got a flag and a date in one write (470 times), a raised
  date was put back (155), a flag and a date were put back together (133), a
  moved flag held a date (409), a copy the app raised itself was left out
  (241).
- **52 tests on real files** (`tests/unit/test_sessions_mirror_activity.py`),
  among them the 2026-10-10 store, the put-back of a date with a flag, the
  bound of ten and its fairness, the new folder copied from the record that
  leads, numbers of any size, a date from the future, the reviewer's schedule
  of stale saves during a full pass, a split report from a partial
  inventory, and the status and doctor output. One is a property: any three
  dates around the pass's clock, and the bounds above on the files a pass
  leaves.
- **Mutation check:** 28 deliberate faults in `mirror.py`, one at a time
  (raise to the newest, raise at exactly the lag, write over a later date,
  raise an archived session, rewrite a copy that needs nothing, least behind
  first, no bound, ignore the switch, take a future date for a voice, count
  a date as a flag write, take a partial inventory for a report, convert any
  number to a float, and sixteen more). On the final code each fails a test.
  Earlier runs left three alive, and each changed something. The needless
  rewrite was real; its test now checks that the file is not replaced. A
  guard in the decision had been made dead by the safe-integer rule; the
  guard was removed. Taking zero for a date at the decision changed no file
  once the write refused it, but chose the session again every pass; its
  test now checks that a second pass chooses nothing.
- **The existing mirror tests** pass: 12 files unchanged (293 tests), and the
  flag model's file, which gained three tests for the wider batch.
- **Dry run on the live store** (nothing written): 94.5 s cold, 0 copies to
  add, 0 flags held, 750 sessions to raise, 12 splits and none showing two
  rows, the same 12 an independent read of the store found.
- **TLC.** The independent review parsed both modules with SANY and ran TLC
  2.19 on every configuration at `bc452d310` (below). `WriteBelowNewest` was
  added to `MirrorActivity.tla` after that run; the twin has checked the same
  bound at the write since the first commit.

| Configuration | TLC 2.19 at `bc452d310` |
|---|---|
| `MirrorActivity.cfg` | no error, 450,278 distinct states |
| `MirrorFlags.cfg` | no error, 616,753 |
| `MirrorActivityStale.cfg` | no error, 190,155 |
| `MirrorFlagsStale.cfg` | no error, 1,178,106 |
| `MirrorActivityStaleLowered.cfg` | `StaysFresh` violated, as expected |
| `MirrorFlagsStaleUndo.cfg` | `IntentWins` violated, as expected; `NeverUndoSettled` too, with `IntentWins` left out |

TLC counts more states than the twins because the modules keep a pass's
snapshot after the pass and carry a `clean` ghost (the reviewer's reading).

## Review

Round 1 (GPT-6.1 Sol on a Subfleet lane, at `bc452d310`) asked for changes.
It reproduced each finding, and each is now a test.

- **A date-only write was counted as a flag write.** An app re-saving one old
  date while a full pass took its inventory invalidated both of the pass's
  refreshes, and the pass held every session: another session's new title
  did not sync. A write that changed only a date no longer invalidates
  anything.
- **The split report could read clean when it was not.** A pass that could
  not list a folder replaced the report without that folder's half of a
  split; an emptied store kept the old report; a process with an older
  report wrote it over a later one. All three are fixed.
- **A number could fail a pass.** An integer too large for a float raised
  `OverflowError`, and the pass stayed recorded as running. Very large floats
  could be "raised" to the newest itself. Dates are now compared, never
  converted, and limited to JavaScript's safe integers.
- **The future-date guard claimed too much.** It said a bad date stays one
  folder's; a new folder can still be copied from that record. The claim is
  narrowed and the limit is pinned by a test.
- **The TLA+ module checked less than its twin.** It bounded a raise at the
  decision only. `WriteBelowNewest` bounds it at the write.
- **Two tests claimed more than they exercised.** The lockstep example named
  for a joint put-back archived the session, which defers the date, so it
  put back a flag only; and no test used a date that parses as a float. The
  example now unarchives, the run counts its shapes, and floats are covered.
- **Ten sessions that never publish could starve an eleventh.** A session
  chosen and not published now yields its place.

## Cost

- One raise of one copy: 0.30 ms (1,000 check-and-write cycles of a 12 KB
  record, fsync included, took 0.30 s on this machine).
- One session: 133 copies, about 40 ms. Ten sessions a pass: about 0.4 s.
- The backlog: 94,383 copies over about 75 passes.
- After that, a session rewrites its other copies at most once an hour while
  it is in use. On 2026-10-10, 188 conversations carried a date within the
  previous 24 hours, and 32 within the previous hour.
- Each raise is journaled like any write into the store, about 1,300 rows a
  pass during the backlog, against the journal's 50,000.

## What it does not do

- It does not make the running app show the new date. The app lists a folder
  only when it loads it, so a raised date shows at the next launch or account
  switch (the load gap, 2026-09-24 report).
- It does not merge, archive or retitle a split.
- It does not sync the model, effort or folder of a session across logins;
  PR #49 proposes that on `main`.

## Not established

Why the app bound its id to the fork in 41 folders and to the original in 93.
No code or log that shows it was read, so this report names no cause. What was
read is the mirror's side: it writes a `cliSessionId` only by replacing a
record whose id is empty with the session's resolvable copy, and by placing a
copy under a new name.
