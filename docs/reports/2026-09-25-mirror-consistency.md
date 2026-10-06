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
  accept. The shutdown wedge is bounded by C-5.8a
  (`2026-09-25-daemon-stop-wedge.md`; PR #40 paces inspection and does not
  bound `close()`). Neither has merged: GitHub Actions billing blocks CI
  (`d193`).

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
Each account folder holds a copy. The merge base in `mirror-flags.json` holds
the last decision and, for each copy, its reference: the value that decision
gave the copy or read there. It stores only the references that differ from
the decision, which is none once a publish goes through (2026-09-29, review
round 5 finding F2; see below).

- **Decide.** A pass reads every copy, then decides. A copy votes only when it
  differs from its own reference. Votes that agree win; with no vote the last
  decision stands; votes that disagree fall back to the change from the last
  decision. With no base yet every copy votes, and archived-anywhere wins
  (v1's rule for the historical backlog). While every reference is the base,
  which is always so except after a publish that did not go through, this is
  the rule of 2026-09-25: agreement wins, otherwise the change from the base.
- **Check.** It re-reads every copy it will write. If any no longer holds the
  value it read, nothing is written or recorded, and the base is kept.
- **Record.** It writes each copy's next version to a temporary file beside
  the copy, then appends the publish to `mirror-publish.jsonl` and fsyncs it.
  The record holds the session's merge-base record before and after, and each
  copy with its temporary and the flags the pass read there. Only then is
  anything renamed into place.
- **Write.** It renames the copies into place one at a time, in path order. A
  rename that finds its copy rewritten since the check stops the publish. The
  copies already written are then put back, except any rewritten since, and
  the put-backs are recorded before they run. No temporary is removed before
  the publish is resolved, so one still standing proves its rename never ran.
- **Resolve.** The publish is resolved by what landed.
  - No copy reached (none renamed, or every one put back): nothing changes.
  - Every copy reached: the base and every reference take the decided value.
  - Otherwise the decision stands for the copies it reached. A copy it did
    not reach keeps, as its reference, the value its file holds.
- **Advance.** The journal is saved first, then the merge base, and only then
  are the record and its temporaries dropped.
- **Recover.** A record left behind by a crash or a failed base write is
  resolved the same way at the start of the next flag sync, before anything
  is decided. The temporaries still standing say what landed, and the pass's
  own read of each copy says what a copy the publish did not reach holds. The
  mirror's writes that still stand are journaled.
- **Every copy or none.** A session is decided from every copy or not at all.
  - A folder that fails to list is read by name from its last listing, but
    only if its directory is unchanged since that listing. A folder that has
    changed since, or was never listed, holds every session that pass.
  - An account that fails to list keeps the org folders its last listing
    named, but only if its directory is unchanged since and each of them has
    been listed itself. Otherwise, or if
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
| Convergence | A pass with nothing written in between leaves every copy, the base and every reference equal to the value decided. |
| Change wins, both ways | When every copy that differs from its reference holds the same value, that value is decided (archive or unarchive). |
| Idempotence | A converged state, with every copy and every reference at the base, decides itself and writes nothing. |
| Cancellation safety | A cancelled pass changes no copy, no base and no reference. |
| No lost update | The mirror writes only a copy that nobody has rewritten since it last checked or wrote it. Resolving a record, a failed base write and a crash write no copy. |
| The record comes first | No copy write moves the base or a reference, and a publish starts only once its record is durable. |
| All or nothing | A held session writes and records nothing. A write that fails puts back every copy the publish wrote, except one rewritten since. A publish every write of which was put back keeps the base and every reference. |
| Base agreement | After a publish that went through, the base and every reference are the decided value. Every copy the pass read differently holds that value, unless the app or the user rewrote it since. |
| The mirror's own writes do not vote | A copy whose file is the mirror's own last write holds its reference, so it never reads as a change, whatever became of the pass that wrote it (F2). |
| Intent wins | With a base, if the user set only one value since the last publish that converged, a pass decides that value. This is the brief's "no resurrection" for a user's change. A user who set both values since then is exempt. |
| Latest wins | With a base, a pass decides the value of the user's latest action, unless some copy, a decision in flight or a value a rollback could put back still holds the other value from an action the user did not know of when they acted. Knowledge follows values: acting on a copy knows what its value came from, and a value the mirror writes comes from the copies it read holding it. This is what F2 broke, and intent wins could not see it, since a user who reverts has set both values. |
| Never undo a settled value | Once a clean publish converged every copy and no user has acted since, no pass writes any other value. This is "no resurrection" for a value every copy agreed on. It cannot fire with an honest app (a user action clears it), which is why intent wins and latest wins exist. |

Both user ghosts exempt the bootstrap rule, which may override the user by
design:
- they are checked only by a pass with a base;
- a decision taken with no base clears `intent`;
- `latest` guards only actions taken while a base is recorded;
- a bootstrap decision counts as an actor of its own, known only where its
  writes are seen.

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

### The mirror's own writes after a failure (F2)

Review round 5 found three failures that left copies ahead of the merge base:
- a put-back write that raises;
- a crash between a session's first copy write and its base write;
- a base write that raises. Since PR #41 this fails the pass, but its copies
  are already written.

If the user then reverted before the next pass, that pass read the mirror's
own writes as a change from the base and undid the revert, in every account.
On 2026-09-29 the reviewer's reproductions still did so on `release/217`.

The design first proposed was that a copy whose current file is the one the
journal records the mirror writing does not vote. The model with the faults
added refuted it in two cases:
- **A revert where the archive was seen.** Every copy is written and the
  process dies at the base write. The user unarchives in B, where they saw
  the mirror's archive. B is now the user's file, so it votes, and so does
  A's original archive; against the base the crash left behind, the archive
  wins.
- **A skipped put-back, with no fault at all.** The app saves B right after
  the mirror's write and C before it, so C's write fails and B's put-back is
  skipped. The user then unarchives in B, and A's archive outvotes it. The
  rule of 2026-09-25 loses this case too; it needs only a racing app save.

What the user saw in B was the mirror's decision. So the mirror has to know,
per copy, which decision the copy last received. A file's identity cannot tell
it that; a reference per copy can. With references, an honest app's save of
the mirror's value (B above, before the user's unarchive) does not vote
either, which file identity could not give.

The journal is not part of the protocol. It still comes before the base,
because once the base is written the record goes, and the load-gap report
must still read the pass's writes as the mirror's after a crash.
`test_a_flag_write_is_journaled_before_the_publish_record_goes` holds that
order.

A publish that did not reach every copy used to keep the old base. Now its
decision stands for the copies it reached, the case that fixes F2. The same
holds for a bootstrap publish, one decided with no base. Voiding it instead
would bring F2 back for sessions never synced. Say the user archived in A,
the publish reached B and the process died, and then the user unarchived A.
With no base, archived-anywhere would count B, the mirror's own write
(`test_an_incomplete_bootstrap_publish_stands_and_a_revert_stands`).

### How they are established

| Method | Where | Result |
|---|---|---|
| Specification | `docs/formal/MirrorFlags.tla`, with every fault on, and configs `MirrorFlags.cfg` (honest app), `MirrorFlagsStale.cfg` (stale saves: what should still hold) and `MirrorFlagsStaleUndo.cfg` (the three that should fail) | Written, not run under TLC. On 2026-09-25 Max ruled to skip TLC for now. Running it needs `tla2tools.jar`, which is not installed; a Homebrew OpenJDK is, off `PATH`. The action ids behind "latest wins" are unbounded, so TLC would need a view that renumbers them. The twin below checks the same properties. |
| Exhaustive model check | `tests/mirror_flags_model.py`, the spec's executable twin, explored breadth-first by `tests/unit/test_mirror_flags_model.py` (three accounts, every reachable state and action) | Honest app: 146,927 states without faults and 255,385 with every fault, and every property holds. Stale saves allowed: 269,278 states, and only "intent wins" and "never undo a settled value" fail. |
| Bounded model check | The same twin with the causal ghost behind "latest wins" | The full space runs to tens of gigabytes (an uncapped run reached 17 GB on 2026-09-29), so it is explored to 150,000 states (about 0.35 GB), every fault on. Nothing breaks. |
| Rules compared | `tests/unit/test_mirror_flags_model.py`, replaying fixed traces under each rule | Under the rule of 2026-09-25, a crash breaks "the mirror's own writes do not vote" and "latest wins", and "intent wins" does not notice. The design first proposed breaks "latest wins" on a revert where the archive was seen. Both break it on a skipped put-back with no fault. The per-copy references break nothing on any of the three. |
| Differential | `tests/unit/test_mirror_flags_stateful.py`: a Hypothesis state machine drives the real `Mirror` on real files in lockstep with the model | After every step, every file's flag, the merge base, every copy's reference and the pending record equal the model's. See below for the steps and coverage. `test_mirror_flags_publish.py` also holds `_decide_flag` and `_resolve_publish` to the model's `decide` and `resolve` on random inputs. |
| Examples | `tests/unit/test_mirror_flags_publish.py`, `tests/unit/test_sessions_mirror_load_gap.py`, `tests/unit/test_mirror_flags_faults.py` | See the list below. |
| Mutation | 22 mutants of the F2 fix, and 10 earlier mutants of the publish, each run against the mirror's tests | All 32 killed; see the table below. The other 28 of the 38 earlier mutants (inventory, holds and the journal's guards) were not run again. The tests that killed them are unchanged and pass. |

The example tests check:
- F2's three failures, each followed by a revert that must stand;
- a revert where the user saw the mirror's archive, after a crash at the base
  write;
- a revert after a put-back the app's save made the mirror skip, with no fault;
- an incomplete bootstrap publish;
- the record is durable before the first rename;
- a rename that finds its copy rewritten keeps its temporary;
- put-backs are recorded before they run;
- temporaries outlive a crash at the base write;
- a sweep never removes a pending record's temporaries;
- recovery resolves by what the pass read, not by a newer save;
- a record the base already holds is not resolved again;
- the journal is saved before the record goes, and recovery journals the
  writes that still stand;
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
- passes with writes between two of the publish's writes, some whose
  put-backs then fail;
- passes whose base write fails;
- passes whose process dies before a copy's rename, at the first base write,
  or once the base is written and before the record is dropped, each followed
  by a new process;
- passes that cannot list a folder or read a copy, with a second, untouched
  session that such a hold must not take unless the folder's contents are
  unknown;
- cancelled passes.

One run of 100 examples resolves 565 publishes. Of those, 462 went through,
76 stood for the copies they reached, and 27 changed nothing. In the same
run:
- 20 renames found their copy rewritten;
- 63 base writes failed;
- the process died 32 and 27 times before a copy's rename, 47 times at the
  base write, and 72 times after it.

The test fails if any of these happens fewer than five times.

| Mutant | What it breaks | Killed by |
|---|---|---|
| votes ignore references | the rule of 2026-09-25: the mirror's surviving writes vote | `test_a_rename_that_finds_its_copy_rewritten_keeps_its_temporary` |
| no write-ahead record | a crash or failed base write leaves writes nothing can attribute | `test_a_base_that_cannot_be_written_leaves_the_record_and_a_revert_stands` |
| record after the first rename | a crash in between leaves a write the record does not name | `test_the_record_is_durable_before_the_first_rename` |
| rename drops its temporary | a copy the publish did not reach reads as reached | `test_a_rename_that_finds_its_copy_rewritten_keeps_its_temporary` |
| put-backs not recorded | a put-back reads as the mirror's write standing | `test_put_backs_are_recorded_before_they_run` |
| temporaries removed before the base | a crash at the base write loses what landed | `test_temporaries_outlive_a_crash_at_the_base_write` |
| a failed publish keeps its prior record | F2 itself: the mirror's surviving writes vote | `test_a_put_back_that_fails_leaves_a_write_that_does_not_vote` |
| unreached copies take the decision as reference | an old value the publish did not reach votes | `test_a_revert_after_a_skipped_put_back_stands_without_any_fault` |
| unreached copies keep the value first read | a later save at a copy the publish did not reach is lost | `test_the_resolution_is_the_models` |
| no recovery | a record left behind is never resolved | `test_a_base_that_cannot_be_written_leaves_the_record_and_a_revert_stands` |
| recovery ignores put-backs | a copy put back reads as reached | `test_put_backs_are_recorded_before_they_run` |
| journal after the record goes (journal before base) | a crash then loses the journal rows; the report misreads the writes | `test_a_flag_write_is_journaled_before_the_publish_record_goes` |
| recovery does not journal | the report misreads writes a crashed pass left | `test_recovery_journals_the_writes_that_still_stand` |
| sweep removes a pending record's temporaries | an old unrenamed temporary is taken for a landed write | `test_a_sweep_leaves_the_temporaries_of_a_pending_record` |
| a resolved record is resolved again | a crash after the base write resolves it again from moved files | `test_a_record_the_base_already_holds_is_not_resolved_again` |
| an incomplete bootstrap publish changes nothing | F2 again for a session never synced | `test_an_incomplete_bootstrap_publish_stands_and_a_revert_stands` |
| recovery reads files, not the pass's read | a newer save makes the pass's older read vote | `test_recovery_resolves_by_what_the_pass_read_not_by_a_newer_save` |
| prepared copies not synced | a copy can reach the folder unwritten | `test_the_mirror_syncs_what_it_writes_before_the_rename` |
| record not synced | the record can be lost to a crash | `test_the_mirror_syncs_what_it_writes_before_the_rename` |
| record's folder not synced | the record's name can be lost to a power failure | `test_the_mirror_syncs_what_it_writes_before_the_rename` |
| disagreeing votes fall to the bootstrap value | a conflict ignores the last decision | `test_the_decision_is_the_models` |
| no vote returns the bootstrap value | an untouched session flips to archived | `test_a_revert_after_a_skipped_put_back_stands_without_any_fault` |
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
| account signature ignored | a folder created since a failed account's listing is left out | `test_a_folder_created_since_a_failed_accounts_listing_holds` |
| unseen bases dropped | a session no copy of which was read loses its base | `test_a_session_no_copy_of_which_could_be_read_keeps_its_base` |
| repair over an unreadable copy | an EMFILE read is taken for an empty record and replaced | `test_an_unreadable_copy_is_never_repaired_over` |
| base write failure swallowed | a pass that did not write the base reports ok | `test_a_base_that_cannot_be_written_fails_the_pass` |

On the F2 commit the ten rows from "no pre-check" to "base not synced" were
run again, with these results:
- killed by the new publish tests: no pre-check (by the load-gap batch test),
  "write ignores its check", no rollback, both bootstrap flips, and "change
  loses";
- killed by the load-gap tests: "rollback ignores its check" and "held
  advances base";
- killed by the stateful test: no cancellation point;
- killed by the sync count: "base not synced".

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

Three limits of the F2 fix:
- **A flip made twice at a copy the publish did not reach.** Take a copy the
  publish did not reach. If the user changes it and changes it back before
  the next pass, it holds its reference again, and no value can show the
  flip. The user did not see the decision there, so the model counts this
  as a conflict. The mirror resolves it for the decision it already
  published.
- **Power failure.** The record's durability before the renames rests on
  fsync. On macOS fsync without F_FULLFSYNC does not promise that across a
  power failure. The copies themselves have always relied on the same fsync.
  A process crash, the failure seen in practice, is covered.
- **Only the flags have references.** Titles and settings are written in the
  same batch but still decide against the one recorded title. A title write
  stopped partway can therefore still read as a candidate on the next pass.
  That is a follow-up.

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
and it is now specified, model-checked and property-tested. Since 2026-09-29
it is kept per copy, where a publish left copies apart (see F2 above). A last-writer-wins
ledger by file time would fix nothing on the known limit and would lose to
honest saves elsewhere. Attribute intent by load intervals as its own design,
specified first. The design target is this spec with `StaleSaves = TRUE`:
keep every invariant while an app may re-save a value it never saw change. It
is recorded as a follow-up.
