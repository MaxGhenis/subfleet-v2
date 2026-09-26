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
- **Every copy or none.** A session is decided from every copy or not at all.
  - A folder that fails to list is read by name from its last listing, but
    only if its directory is unchanged since that listing. A folder that has
    changed since, or was never listed, holds every session that pass.
  - An account that fails to list keeps the org folders its last listing
    named, but only if each of them has been listed itself. Otherwise, or if
    the account has never listed, the pass holds every session. The 2 s hot
    pass follows the same rules, so it never forgets a folder it could not
    list.
  - An account excluded by name is never listed, so it cannot hold.
  - A copy that exists but cannot be read (EMFILE, on 2026-09-25) holds its
    session, and the pass writes nothing for it and keeps its base. Its
    session is the one the folder's last listing saw in it, which survives
    failed reads. It is never guessed from a same-named file elsewhere, since
    names are not unique across accounts. If it is unknown, every session is
    held.
  - A path the app cannot read either is in no sidebar, because the app runs
    as the same user. That covers permission denied, a directory where a
    record should be, and a symlink loop. It holds nothing, so it cannot
    freeze flag sync. Listings never follow symlinks. A store that fails to
    list fails the pass.
  - Bases of sessions a pass did not see survive it.
  - A failed account's known folders are still listed one by one. Only a
    folder that does not list itself is read by name or holds.
  - Held sessions are counted in `flags_held`, including a session held
    because a copy changed while the pass published.
    - The first few causes are recorded in `held_by`, each with a path and a
      reason.
    - Both show in the pass summary, in health's detail and in
      `sessions mirror --status`.
    - mirror-watch alerts when a hold lasts 15 minutes, quoting the causes,
      and alerts again if the hold grows.

| Invariant | Statement |
|---|---|
| Convergence | A pass with nothing written in between leaves every copy and the base equal to the value decided. |
| Change wins, both ways | When every copy that differs from the base holds the same value, that value is decided (archive or unarchive). |
| Idempotence | A converged state with its base decides itself and writes nothing. |
| Cancellation safety | A cancelled pass changes no copy and no base. |
| No lost update | The mirror writes only a copy that nobody has rewritten since it last checked or wrote it. |
| All or nothing | A held session writes nothing and keeps its base. A write that fails puts back every copy the publish wrote, except one rewritten since, and keeps the base. |
| Base agreement | After a publish that went through, the base is the decided value. Every copy the pass read differently holds that value, unless the app or the user rewrote it since. |
| Intent wins | With a base, if the user set only one value since the last publish that converged, a pass decides that value. This is the brief's "no resurrection" for a user's change. A user who set both values since then is exempt, because the merge base cannot order them. |
| Never undo a settled value | Once a clean publish converged every copy and no user has acted since, no pass writes any other value. This is "no resurrection" for a value every copy agreed on. It cannot fire with an honest app (a user action clears it), which is why intent wins exists. |

Review rounds 5, 6 and 7 found that passes used to decide over whichever
copies they had read. A copy skipped because its folder did not list, or because its read
failed, then read as a user's change on the next pass. That pass undid the
change it had just spread, in every account. "Intent wins" catches this in the
model, and the code now decides from every copy or not at all.

"No lost update" has one gap, which no check can close. The app can rename its
save into place in the instant between the mirror's last signature check and
the mirror's own rename, because rename(2) cannot compare first. That save is
then overwritten. The model leaves this window of one syscall out. The code
re-checks right before the rename to keep it that narrow.

### How they are established

| Method | Where | Result |
|---|---|---|
| Specification | `docs/formal/MirrorFlags.tla`, with configs `MirrorFlags.cfg` (honest app), `MirrorFlagsStale.cfg` (stale saves: what should still hold) and `MirrorFlagsStaleUndo.cfg` (the two that should fail) | Written, not run under TLC. On 2026-09-25 Max ruled to skip TLC for now. Running it needs `tla2tools.jar`, which is not installed; a Homebrew OpenJDK is, off `PATH`. The twin below checks the same properties. TLC can join CI later. |
| Exhaustive model check | `tests/mirror_flags_model.py`, the spec's executable twin, explored breadth-first by `tests/unit/test_mirror_flags_model.py` (three accounts, every reachable state and action) | Honest app: all 22,038 states, and every property holds; "intent wins" is exercised on 218 of 1,053 decisions. Stale saves allowed: 44,058 states, and only "intent wins" and "never undo a settled value" fail. |
| Differential | `tests/unit/test_mirror_flags_stateful.py`: a Hypothesis state machine drives the real `Mirror` on real files in lockstep with the model | After every step, every file's flag and the merge base equal the model's. See below for the steps and coverage. |
| Examples | `tests/unit/test_sessions_mirror_load_gap.py`, `tests/unit/test_mirror_flags_faults.py` | See the list below. |
| Mutation | 37 hand-written mutants of `sync_flags`, its writes, its journal and its inventory, each run against the mirror's four test files | All 37 killed; see the table below. |

The example tests check:
- the rollback leaves an app save made after the mirror's write;
- a rolled-back copy still counts in the load-gap report;
- the journal guards against misreading the app's saves;
- the no-base star rule;
- the partial-inventory cases:
  - an unlisted folder, unchanged or changed since its listing;
  - an account that does not list, or never listed;
  - an unreadable copy, once or for good, and one sharing a name with another
    session's;
  - a pass that reads nothing;
  - permission denied on a folder or a record;
  - a store that does not list;
- no repair over an unreadable copy;
- a base that cannot be written fails the pass.

The differential test's steps are:
- user archive, unarchive and flips;
- switches;
- focus rewrites;
- stale re-saves;
- full passes;
- passes with writes between the read and the check;
- passes with writes between two of the publish's writes;
- passes that cannot list a folder or read a copy, with a second, untouched
  session that such a hold must not take unless the folder's contents are
  unknown;
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
| five journal and rollback guards | the report misreads the app's saves; a rollback overwrites a save made right after the rename | one example test each |
| no hold for an unread copy | a pass decides without a copy it could not read | `test_an_unreadable_copy_holds_its_session_and_the_users_unarchive_stands` |
| unlisted folder not read by name | a pass decides without an unlisted folder's copies | `test_a_pass_that_lists_nothing_holds_and_keeps_every_base` |
| never-listed or changed folder proceeds | a pass decides without a folder whose contents it does not know | `test_an_unlisted_folder_that_changed_since_its_listing_holds_every_session` |
| stale listing trusted | a by-name read misses a copy the mirror spread since the listing | `test_an_unlisted_folder_that_changed_since_its_listing_holds_every_session` |
| account failure dropped | an account that did not list drops its folders from the decision | `test_an_account_that_does_not_list_keeps_its_folders` |
| failed account ignored | an account never listed does not hold | `test_an_account_never_listed_holds_every_session` |
| store error taken as empty | a store that did not list reads as empty and is forgotten | `test_a_store_that_does_not_list_fails_the_pass_and_writes_nothing` |
| owner not carried | a copy that stays unreadable holds every session | `test_a_persistently_unreadable_copy_holds_only_its_own_session` |
| permission denied holds | a folder the user may not read freezes flag sync | `test_a_folder_the_user_may_not_read_freezes_nothing` |
| failed check after a read | a copy read successfully counts as unknown | `test_a_failed_check_after_a_good_read_still_counts_the_copy` |
| partial account known | a failed account's never-listed folder is left out | `test_an_account_whose_folders_are_not_all_known_holds` |
| hot pass drops kept folders | the hot pass forgets a failed account's folders | `test_a_hot_pass_keeps_the_folders_of_an_account_that_did_not_list` |
| hot pass ignores gaps | a hot pass reads a failed store as empty | `test_a_hot_pass_that_cannot_list_the_store_forgets_nothing` |
| excluded account listed | a failing excluded account holds every session | `test_an_excluded_account_that_does_not_list_holds_nothing` |
| listings follow symlinks | one bad symlink drops a whole account | `test_a_symlink_beside_the_org_folders_does_not_drop_the_account` |
| EISDIR holds | a directory named like a record holds for good | `test_a_directory_named_like_a_record_holds_nothing` |
| publish hold uncounted | a session held at publish is invisible | `test_a_hold_at_publish_is_counted_and_named` |
| unseen bases dropped | a session no copy of which was read loses its base | `test_a_session_no_copy_of_which_could_be_read_keeps_its_base` |
| repair over an unreadable copy | an EMFILE read is taken for an empty record and replaced | `test_an_unreadable_copy_is_never_repaired_over` |
| base write failure swallowed | a pass that did not write the base reports ok | `test_a_base_that_cannot_be_written_fails_the_pass` |

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

### What is still open

A copy the app cannot read either is left out, so that it cannot freeze flag
sync. If it later becomes readable with an old value, that value reads as a
change, as an app's stale re-save does. It belongs to the known limit. After a
daemon restart, an unreadable copy this process has never read has no known
session, so it holds every session until it can be read. For a copy that keeps
failing (EIO, say), that lasts until someone acts. It is visible in `held_by`
and `sessions mirror --status`, and mirror-watch alerts after 15 minutes.

Three failures leave copies ahead of the merge base:
- a rollback write that fails;
- a crash between a session's first copy write and the base write;
- a base write that fails (this one now fails the pass, but its copies are
  already written).

If the user reverts before the next pass, that pass reads the mirror's own
writes as a change and undoes the revert. Each needs an I/O failure or a crash
inside the publish. The fix is to treat the mirror's own writes as the
mirror's: a copy whose current file is the one the journal records the mirror
writing does not vote. That needs the journal saved before the base, and a
write-ahead record for crashes. It is a follow-up (review round 5, finding F2).

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
