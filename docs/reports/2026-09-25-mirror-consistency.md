# Session state across accounts: liveness, invariants, verification, 2026-09-25

Max asked for session state to be consistent across accounts, and whether it can
be formally verified. This report records what went wrong on 2026-09-25, what
the mirror now guarantees, how each guarantee is established, what it does not
guarantee, and whether an explicit intent ledger should replace the merge base.
It builds on `2026-09-24-mirror-load-gap.md`.

## What happened

- **The unarchives.** The close-out pass unarchived 84 sessions at about
  17:40Z, all in one account.
- **The switch.** Max switched accounts at about 18:30Z, and the new account
  still showed them archived.
- **No pass finished after 17:04Z,** so the unarchives never left the account
  they were made in.

The cancellations had one cause: the daemon was crashing, not the mirror.

- **The crash.** `serve_forever` died on `OSError: [Errno 24] Too many open
  files` at `socket.accept()`. The daemon ran under launchd's default soft
  limit of 256 descriptors, which nothing raised. In the wedged process, 87
  of its 109 open descriptors were unix sockets.
- **The cancellation.** The daemon's exit path calls `Timers.stop()`, which
  sets the event that cancels the mirror pass. That is the 18:47:23Z
  `cancelled` in the sidecar.
- **The wedge.** The process then hung in shutdown with the main thread in
  `pthread_cond_wait` (sample: `~/chief-of-staff/state/diag/subfleet-daemon-wedge-93697-*.txt`).
  It kept the lock, so launchd's replacements logged "another daemon holds
  daemon.lock".
- **The crash loop.** After a restart at 19:26Z, each new daemon hit the same
  EMFILE within minutes, so every pass inside the daemon was cancelled before
  it could finish. The installed mirror needed about 17 minutes for a pass
  (the stopgap below ran from 19:41:58Z to 19:59:25Z).
- **The fixes are elsewhere.** The EMFILE crash is fixed in PR #43, which
  raises the soft limit to 65536, caps connections and waits out a failed
  accept. The shutdown wedge is PR #40's area. Neither has merged: GitHub
  Actions billing blocks CI (`d193`).

The mirror holds no descriptors in the session store (0 of the wedged
process's 109), so it did not contribute to the exhaustion.

A second stall followed.

- **The stopgap.** It finished its pass at 19:59:25Z.
- **The stall.** The next daemon's pass, running the installed code, started
  at 20:00:16Z and was still at "finding transcripts" at 20:41Z. The daemon
  had not crashed this time.
- **The machine.** It was thrashing, with 17.2 GB of 18 GB swap in use and a
  load average of 66. Three PolicyEngine Python processes held about 10 GB
  resident each.
- **The mirror thread.** A 3 s `sample` put it in the garbage collector on
  every sample. Over 5 s `top` measured about 2,000 page faults a second and
  2% CPU, and the process had used 34 s of CPU in 57 minutes. The collector
  was walking a heap that had been paged out, so the thread waited on memory,
  not on the processor.
- **The alert.** `mirror-watch` raised it at 20:32Z.
- **The end.** At 20:54:33Z the same daemon crashed on EMFILE at
  `socket.accept()` and cancelled the pass at "reading entries". So the first
  failure recurred, and #43 still has to land.
- **The relief.** With Max's approval, another session stopped the three
  processes. It reported that swap in use fell from 25.7 GB to 9.2 GB, and at
  20:58Z it was 8.9 GB.

A shorter pass loses less to a stall like this, but no mirror change can stop
the machine from starving the daemon.

## Liveness

- **Seconds, not tens of minutes** (PR #41). A warm full pass takes 1.0–1.6 s
  on the live store (218k files), and a cold one, after a daemon restart, 47 s.
  A 2 s hot pass spreads new sessions. The index keeps a stat signature per
  file and parses only changed content.
- **A cancelled pass loses nothing.** Flag sync decides after the inventory
  and publishes once, with no cancellation point inside the publish. A pass
  cancelled before it publishes writes nothing, so every user change stays on
  disk for the next pass. That is the cancellation-safety invariant below.
- **Resuming.** In one process the index survives a cancelled pass, so the
  next pass re-reads only what changed. After a restart the first pass is cold,
  47 s.
- **An alert that does not depend on the daemon.**
  `~/chief-of-staff/bin/mirror-watch` (launchd `com.maxghenis.cos.mirror-watch`,
  every 5 min) reads the sidecar, `policy.json` and `daemon.lock`. When no
  pass has finished for 10 minutes it sends one `say --class alert --key
  subfleet-mirror-stale`, naming the stalled stage and, if it is alive, the
  daemon lock's holder. It logs the recovery. This is stricter than C-23.28,
  which calls a pass in flight healthy for 30 minutes. That is deliberate: with
  PR #41 a pass takes seconds, so a pass still running after 10 minutes is
  itself the fault.
- **The stopgap on 2026-09-25.** With the daemon in a crash loop, one pass of
  the installed mirror ran as a separate `subfleet sessions mirror --once`
  process, which the daemon's crashes could not cancel. It finished at
  19:59:25Z. At 20:41Z all 120 copies of each of the 84 restored sessions read
  unarchived, and the merge base held `isArchived: false` for all 84. After
  the 20:54Z crash a second stopgap pass ran from 20:59:01Z to 21:01:00Z,
  after the memory pressure was relieved. It took two minutes and added 476
  copies. The 84 were unchanged.

## Invariants

The flag protocol covers one session and one boolean flag. The flag is
`isArchived`; `isStarred` runs through the same loop with its own no-base
value, starred-anywhere, which `test_starred_anywhere_wins_with_no_base` pins.
Each account folder holds a copy, and the merge base in `mirror-flags.json`
holds the last synced value.

- **Decide.** A pass reads every copy, then decides. Agreement wins; otherwise
  the change from the base wins. With no base yet, archived-anywhere wins
  (v1's rule for the historical backlog).
- **Check.** It re-reads every copy it will write. If any no longer holds the
  value it read, nothing is written and the base is kept.
- **Write.** It writes the copies one at a time, in path order. A write that
  finds its copy rewritten since the check fails, and the copies already
  written are put back, except any rewritten since then. The base is kept.
- **Advance.** After the last write, the base takes the decided value.

| Invariant | Statement |
|---|---|
| Convergence | A pass with nothing written in between leaves every copy and the base equal to the value decided. |
| Change wins, both ways | When every copy that differs from the base holds the same value, that value is decided (archive or unarchive). |
| Idempotence | A converged state with its base decides itself and writes nothing. |
| Cancellation safety | A cancelled pass changes no copy and no base. |
| No lost update | The mirror writes only a copy that nobody has rewritten since it last checked or wrote it. |
| All or nothing | A held session writes nothing and keeps its base. A write that fails puts back every copy the publish wrote, except one rewritten since, and keeps the base. |
| Base agreement | After a publish that went through, the base is the decided value. Every copy the pass read differently holds that value, unless the app or the user rewrote it since. |
| Never undo a settled value | Once a clean publish converged every copy and no user has acted since, no pass writes any other value. This is the brief's "no resurrection", in both directions. |

"No lost update" has one gap, which no check can close. The app can rename its
save into place in the instant between the mirror's last signature check and
the mirror's own rename, because rename(2) cannot compare first. That save is
then overwritten. The model leaves this window of one syscall out. The code
re-checks right before the rename to keep it that narrow.

### How they are established

| Method | Where | Result |
|---|---|---|
| Specification | `docs/formal/MirrorFlags.tla`, with configs `MirrorFlags.cfg` (honest app) and `MirrorFlagsStale.cfg` (stale saves) | Written, not run under TLC. On 2026-09-25 Max ruled to skip TLC for now. Running it needs `tla2tools.jar`, which is not installed; a Homebrew OpenJDK is, off `PATH`. The twin below checks the same properties. TLC can join CI later. |
| Exhaustive model check | `tests/mirror_flags_model.py`, the spec's executable twin, explored breadth-first by `tests/unit/test_mirror_flags_model.py` (three accounts, every reachable state and action) | Honest app: all 15,164 states, and every property holds. Stale saves allowed: 20,640 states, and only "never undo a settled value" fails. |
| Differential | `tests/unit/test_mirror_flags_stateful.py`: a Hypothesis state machine drives the real `Mirror` on real files in lockstep with the model | After every step, every file's flag and the merge base equal the model's. See below for the steps and coverage. |
| Examples | `tests/unit/test_sessions_mirror_load_gap.py` | The rollback leaves an app save made after the mirror's write. A rolled-back copy still counts in the load-gap report. A rollback that cannot write journals the write it left. The no-base star rule holds. |
| Mutation | Eleven hand-written mutants of `sync_flags` and its writes, each run against the mirror's three test files | All eleven killed; see the table below. |

The differential test's steps are:
- user archive, unarchive and flips;
- switches;
- focus rewrites;
- stale re-saves;
- full passes;
- passes with writes between the read and the check;
- passes with writes between two of the publish's writes;
- cancelled passes.

One run of 100 examples reaches 535 publishes, 39 of them rolled back.

| Mutant | What it breaks | Killed by |
|---|---|---|
| no pre-check | writes over a copy that changed since the read | the stateful differential test |
| write ignores its check | a write replaces a copy rewritten since the check | the stateful differential test |
| no rollback | a failed write leaves the copies before it written | the stateful differential test |
| rollback ignores its check | the rollback overwrites a save made after the mirror's write | the stateful differential test |
| held advances base | a held session still moves the base | the stateful differential test |
| bootstrap flipped | with no base, unarchived-anywhere wins | the stateful differential test |
| change loses | the base value wins over a change | the stateful differential test |
| no cancellation point | a pass cancelled at publish still publishes | the stateful differential test |
| star bootstrap flipped | with no base, unstarred-anywhere wins | `test_starred_anywhere_wins_with_no_base` |
| base not synced | the merge base can be lost to a crash | `test_the_mirror_syncs_what_it_writes_before_the_rename` |
| rollback not journaled | the load-gap report reads a rollback as the app's rewrite | `test_a_rolled_back_copy_still_waits_for_the_load` |

"Honest app" means an app save never changes the flag in the file, as when the
app's memory is current. The real app serializes each record from memory, so
it is honest only while its memory is current. With stale saves allowed, the
checker finds two shapes of counterexample.

- **From the unsynced start**, the shortest is the bootstrap rule:
  1. With no base, the user unarchives in the loaded account.
  2. Archived-anywhere overrides the unarchive.
  3. The app's memory of the unarchive then comes back and spreads.
- **From a settled state with a base**, the shortest is the known limit itself
  (`test_from_a_settled_state_a_parked_accounts_stale_save_undoes_the_users_change`):
  1. The user switches accounts and archives, and a clean pass settles it
     everywhere.
  2. The app still holds the first account's record from before the mirror's
     write (a parked session), and saves the old value there.
  3. The next pass reads that save as a change from the base and spreads it to
     every account.

## Design question: an authoritative intent ledger

The proposal: replace the implicit merge base with an explicit record in
Subfleet state, one last-writer-wins register per session and field holding
(value, timestamp, account seen). The mirror would reconcile every copy toward
it. Logpile would read it and not own it.

- **Today's incident.** No protocol could have prevented it: the mirror was
  not running. Liveness and the alert are the fix.
- **The one known inconsistency.** Stale re-saves win under both designs.
  - A stale re-save never agrees with the base, so the merge base reads it as
    a change and lets it win.
  - A register ranked by file time lets it win too, because the re-save is
    always later than the user's change it undoes.
  - A register ranked by when the mirror observed a change is the merge base
    under another name.

  The fact that separates a user's change from a stale re-save is which
  folder the app had loaded, and since when, at each write. A user can act
  only in the loaded folder. A stale save comes from memory that predates the
  mirror's write. That is an attribution problem, and the ledger's shape does
  not solve it. A guard built on the app's load times was attempted in PR #41.
  It was removed after three adversarial review rounds found ways it undid a
  user's real change.
- **Where the merge base is better.** File time would rank an honest app save
  that keeps the old value, such as a focus update in another account, above
  the user's change. The merge base ignores any copy that agrees with it.
- **What a ledger would add.** Provenance: which account a value was first
  seen in, and when. That helps diagnosis and fits in the existing
  `mirror-flags.json` records. It does not need a new store: the mirror also
  runs outside the daemon (`sessions mirror --once`), and a JSON file under the
  mirror lock serves both.

**Recommendation.** Keep the merge base. It is the account-agnostic register,
and it is now specified, model-checked and property-tested. A last-writer-wins
ledger by file time would fix nothing on the known limit and would lose to
honest saves elsewhere. Attribute intent by load intervals as its own design,
specified first. The design target is this spec with `StaleSaves = TRUE`:
keep every invariant while an app may re-save a value it never saw change. It
is recorded as a follow-up.
