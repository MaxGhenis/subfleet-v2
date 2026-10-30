# D-KILL4: a terminal attempt is contained before it is finalized

Base: `3d94eb563` (#240 after fix round 1b), as the hub cut it. Branch `fix/kill4-terminal-containment-217`; the commits sit on this job's detached checkout (the caller's checkout holds the branch), so Subfleet keeps them as a salvage ref. Nothing is pushed.

Process inspection was available in this sandbox: `ps`, `sysctl kern.bootsessionuuid` and libproc reads of this job's own processes all worked (checked first). The live daemon, its store and its log were not read, so whether the installed daemon roots its guardians was not checked (decision 6).

## The cause, in the code at the base

Every terminal path that met live census members either quarantined without signalling or signalled only the recorded group and `owned_identities`:

| Path (base `subfleet/daemon.py`) | Base behaviour with live census members |
|---|---|
| Start grace expired, no receipt (`_process_attempt`) | `quarantined` at once, nothing signalled. The guardian of `hub-build-mi-household-members-std/a1` (stopped or slow before its `start.json`) later ran its provider for 56 min beside the rerun. |
| Exit receipt, census not empty past `exit_settle_s` (`_finalize`) | `quarantined` ("writers remain after exit receipt"), nothing signalled (`hub-us9981-review-r3b/a1`). |
| Dead guardian without receipt (`_inspect_running`) | `_kill_attempt(lost=True)`: `killpg` of a group whose leader is gone is refused, and only `owned_identities` were signalled one by one, so escaped members were never signalled; usually `quarantined`. |
| Dead guardian, census empty, then a non-empty finalization census (`_lost` → `_finalize`) | As the exit-receipt row. |
| Cancel and wall stop (`_kill_attempt`) | TERM and KILL to the group, KILL to owned survivors. Marker, walk, responsibility and counted members outside the group were never signalled. A hidden-marker shell loop outside every source (`hub-us10299-review-shell-b/a2`) respawned marked children after the kill, and the attempt ended `interrupted`. |
| Stopped census member (any path above) | A TERM to a stopped process stays pending. A stopped group member stayed stopped through the whole `term_grace_s` (15 s) until the group KILL; a stopped escaped member was never signalled. |
| `subfleet kill <job>` on a finished job | "already finished", no census. `--confirm-dead` on a finished, non-quarantined job: "already finished", no census. |
| Gate closed unopened (`guardian-identity-unavailable`, a turn cancelled during launch) | `_unlaunched` (and possibly a retry) without waiting for the gated guardian, which exits 127 on the end of file. |

## The fix

1. **The census is the kill protocol's signal authority** (`_signal_targets`, `_kill_protocol`, C-5.4, C-5.6). TERM goes to the recorded group after its leader's identity, and to every other census member one by one, each with the identity the census captured, re-checked by `procs.signal_process` (C-5.3) just before its syscall. Members found during the grace or the settle window are signalled too. Never a target: a cwd holder, a held parent, a member without a captured identity, launchd, the daemon, its parent, or another attempt's recorded guardian (`_never_signalled`). The group's members, marked members and parent-chain members are attributed by the census itself. A member found only by the walk or responsibility is a target only in a verifiable census, and never when it, or a census ancestor in that census's shapes, was first recorded by an unverifiable census. Such records are tracked as `unverified_identities`, kept while a seed key still holds that birth. An unverifiable census may retain seeds whose births it could not compare, and responsibility strangers it could not bound (C-5.5). Every census also records what it sees, so a recorded identity cannot vouch for itself. A first version let one vouch; the D-CEN1 tests that pin uncertain root-death evidence as hold-only caught it.
2. **Every terminal path with members runs it first.** Start grace with members now calls `_kill_attempt(lost=True, reason="start grace expired without a receipt")`; writers after the exit receipt go through `_contain_writers` (no `killed_by`: the provider ended before any signal, so its verdict stands); the dead-guardian path keeps `_kill_attempt(lost=True)`. Verified empty finalizes as before; survivors are `quarantined` with the old reasons. A finalization whose writers reappear after three protocol runs quarantines at once (`TERMINAL_CONTAINMENT_ROUNDS`).
3. **No retry while the census lives.** A start-grace attempt with members is a loss only after the protocol verified its census empty; a read-only loss then retries (C-4.5). A gate-closed guardian is waited for and, if need be, killed by its own unreaped spawn handle (`_reap_gated`, 2 s) before `_unlaunched`.
4. **The parent-chain rule** (`procs._parent_chain`, C-5.5). From each marked member whose parent the census lacks, follow the census snapshot's parent links up. A marked child younger than the snapshot, which a respawning loop's children often are, is followed from its own kernel record's `pbi_ppid`; the host's real `/bin/sh` loop showed this. Add each ancestor that is live, pid > 1, not excluded, of the daemon's uid by its kernel record (`pbi_uid`, read from the same `proc_pidinfo` record as the unique id), not exiting, and whose canonical cwd is the job's own worktree or inside it. Added ancestors are census members (`chain_pids`, reasons in `chain`), persisted as counted identities, and so signalled. An ancestor this attempt counted is a walk seed, hence already a member, and the chain stops there.
5. **Held parents** (beyond the brief, and why). In an in-place workdir there is no owned worktree, so an uncounted hidden loop can never be added. Without more, a census landing between two of its children reads verified empty and the attempt ends with the loop alive, the very shape of `us10299-review-shell-b`. So the first same-uid ancestor that does not qualify is **held**: `held_pids` makes the census not empty while it lives with its recorded birth (`held_parents` evidence), but it is never a target and never a walk seed. The attempt quarantines instead of finalizing, naming the pid, and the operator decides. Nothing above a held parent is examined.
6. **Stopped members** (C-5.6). Right after its TERM, each target the census shows stopped (`ps` state `T`) gets SIGCONT, so it acts on the TERM. Nothing else is continued; KILL needs no continue.
7. **Re-containment** (decision 4, C-5.7). Plain `kill` on a finished job whose last attempt had a guardian and whose census (cwd holders aside) is not empty answers `containment requested` with the pids, runs the protocol on a worker (`_recontain`), records `recontainment` and `attempt.recontained`, and leaves the attempt's state. A quarantine still needs `--confirm-dead`, and a finished verdict is not reopened. The next `kill` reports the last result. `--confirm-dead` on a finished, non-quarantined job whose census is not empty answers `not quarantined`, names the pids and releases nothing.
8. **Evidence.** Each protocol run records `attempt.containment` (the starting census as C-5.5 records it, the group's signals, the pids signalled one by one, contained or not, chain reasons) and a bounded `terminal_containment` list in the attempt's evidence. Pids and reasons only, never commands.

## Every terminal path, old and new

| # | Path | Before | Now |
|---|---|---|---|
| 1 | Start grace expired with members | quarantined, **no containment** | kill protocol (`recovery`), its group KILLed at once (fix 1); verified empty → unlaunched, `failed: starting-no-receipt`, retried while attempts remain (fix round 2b, decision 1; it was `lost`); else quarantined. Before it, a live, running, verified guardian alone gets up to `START_GRACE_MAX_S` (D-QUAR-START1 (a)), and the abandon handshake precedes every signal and end (D-QUAR-START1 (b)) |
| 2 | Start grace expired, census empty | `failed`/`interrupted`, retry | unchanged (cwd-only waits 30 s, #240), after the abandon handshake (fix round 2b) |
| 3 | Dead guardian with members | group (refused, leader dead) + owned only: **partial** | full census |
| 4 | Dead guardian, empty, then members at finalization | quarantined, **no containment** | C-5.9 window, then the protocol |
| 5 | Exit receipt, members past `exit_settle_s` | quarantined, **no containment** | C-5.9 window, then the protocol; verdict from the receipt |
| 6 | Exit receipt, census empty | finalized from the receipt | unchanged |
| 7 | Cancel, wall stop | group + owned only: **partial** | full census, chain, continue |
| 8 | Turn stop | relay interrupt, then 7, or 5 on a receipt | same, through 5 and 7 |
| 9 | `_launch_failure` (adapter refusal, failed launch-time check) | finalized; nothing launched | unchanged |
| 10 | `_unlaunched`: `reserved-no-launch`, `cancelled-before-launch`, unfinished launch-time check | nothing launched | unchanged |
| 11 | `_unlaunched`: gate closed after spawn | **no wait** for the gated guardian | reaped by handle first |
| 12 | `--confirm-dead` on a quarantine | releases only on verified empty | unchanged |
| 13 | `--force-release` | operator override, recorded | unchanged |
| 14 | `kill` on a finished job | "already finished" | re-containment (decision 4) |
| 15 | Probe containment (`_contain_probe`) | group + owned | unchanged: not a job attempt |
| 16 | Offline kill (`offline.py`), daemon down | recorded group | unchanged |

## The parent-chain rule's safety argument

- **Start.** Only a marked member, whose own argument buffer holds both whole strings `SUBFLEET_ATTEMPT=<this attempt>` and `SUBFLEET_ROOT=<this state root>`.
- **Direction.** Up the snapshot's parent links only, never down. An added ancestor is kept by its birth in `chain_identities`, with its reason, never as a walk seed, so its other descendants are members only when another source attributes them; a held parent seeds no walk either. So an operator's other processes are never pulled in. (Until fix round 1 an added ancestor was persisted as a counted walk seed, so its whole subtree became members and was signalled: review r1's P3-3. This line claimed the opposite then.)
- **Qualification.** Live, pid > 1, not launchd, the daemon, its parent or another attempt's recorded guardian, of the daemon's effective uid by the kernel record (other users' processes are EPERM and read as gone), not exiting, and a canonical cwd (APFS case and `/private` spelling, as admission compares) equal to or under the job's own daemon-allocated worktree (`owned_worktree`, C-13.4: never an in-place workdir, never a probe).
- **Stop.** The first ancestor that does not qualify is held and nothing above it is examined. An operator's shell outside the worktree is never signalled, and neither is its parent.
- **cwd alone.** A cwd holder that is no ancestor of a marked member is neither added nor held. Cwd-only censuses keep #240's rule: they hold the worktree and never change a verdict.
- **Identity.** Every signal still re-checks pid, boot and start just before the syscall, so pid reuse can redirect one only within C-5.3's same-second limit.
- **Residual risk.** One shape is signalled by design: an operator process whose cwd is inside this job's own worktree and which is an ancestor of a process carrying this attempt's exact markers. Examples are an operator's shell in the worktree that ran a command with the attempt's environment, or a tmux server started in the worktree hosting the agent's pane. The hub accepted it.

## What remains open

- **Seen by no census.** A hidden-marker loop that leaves the group and the walk before any inspection counts it, and that has no marked child alive at any census, is seen by no source. This is D-KILL3's documented limitation, extended. A census landing between two children of such a loop reads verified empty; the fixture's 0.4 s children showed it, and a real loop's `pytest` child lives far longer.
- **Held loops: in-place jobs, and owned-worktree jobs whose loop runs outside the worktree** (a scratch folder, which hub briefs require). A loop seen as the parent of a marked child is held, so the attempt quarantines, but it is never signalled: the operator-safety limit the hub set. `kill` and `--confirm-dead` name it; the operator ends it and then runs `--confirm-dead`, or uses `--force-release`. Since fix round 1 a held parent is held again only while the last census saw a marked process name it (the kill protocol's censuses count as one), so one whose marked processes are gone releases. A slow respawner whose children are absent at two deciding censuses in a row is released between them, and its next marked child is a census member of the finished attempt, which `kill` re-contains.
- **Unrooted daemons.** No responsibility source; decision 6 below.
- **Uncertain records fall back to quarantine.** A member first recorded by an unverifiable census, or below one, is never signalled.
- **Same-second pid reuse.** C-5.3's limit still applies to every identity check before a signal.
- **The accepted operator case.** An operator process whose cwd is in the job's own worktree and which is an ancestor of a marked member is signalled by design.
- **Probes and offline kills** keep their containment (group and owned only). They are not job attempts, and run no parent chain.
- **Markers are signal authority wherever a process runs.** A process that inherited the attempt's environment is signalled even outside the attempt's lineage, for example a pane of a tmux server the agent started (review r1, check 2, read only). Accepted; C-5.5 says so.
- **A stranger's leaderless group at the recorded pgid holds the attempt.** Since fix round 1 its members are never signalled, but they are census members. Since fix round 2b `kill` on the finished job runs no protocol for them and answers `already finished`, naming them as held, never signalled; `--confirm-dead` refuses while they live. `--force-release` resolves only a quarantine: on a finished job that is not quarantined it answers `already finished` and releases nothing, so the hold lasts until they exit (corrected in fix round 2b, review r2's P3-2).

## Decision 6: does every guardian record a responsibility root?

Every guardian start goes through `Daemon._spawn_guardian`: job, resume, revive, each retry (a new attempt through `_launch`), turns (also `_launch`), probes (`_contain_probe`'s launch) and the re-enrolment turn (`_enrollment_turn`). It disclaims responsibility, and so records `responsible_root`, iff `self._disclaim_guardians` is true. That is set once at daemon start by `procs.guardian_disclaim_allowed()`: the SPI symbols resolved, and `responsibility_get_pid_responsible_for_pid(getpid()) == getpid()`, which holds for a daemon launchd started.

- **A self-responsible (launchd) daemon** roots every path. A successful disclaimed spawn always records a root, with `unique_id` None if its metadata read fails. A failed spawn ends the launch.
- **A daemon started from a terminal or an app**, one whose startup responsibility read failed, or one on a macOS without the SPI, roots **no** path. Each uses Popen and logs once. This is deliberate (C-5.1): a disclaimed guardian becomes its providers' macOS privacy (TCC) identity, so the providers of a terminal-started daemon would lose the terminal's privacy grants.
- **Attempts recovered after a restart** keep whatever their launch recorded.

Rooting the unrooted paths would change privacy attribution for every provider of a terminal-started daemon. That is a permissions decision with user-visible consequences, not something this build may take, so the pinned limitation tests stay as they are (`tests/fake/test_census_self_exit_daemon.py::…leaving_between_inspections…`, `tests/fake/test_census_inspection_finalization.py::…no_inspection_before…`, `tests/process/test_census_hidden_environment_process.py`'s `documented_limitation`).

What closing it would take:
- **(a)** An explicit operator opt-in to disclaim from a non-self-responsible daemon, accepting the TCC change; with it, those tests flip to contained on that path.
- **(b)** A lineage source that survives `setsid` without changing TCC attribution. Not investigated here.

For jobs with an owned worktree, the cwd hold and now the parent-chain rule cover a hidden runner whose cwd is inside it. For in-place jobs, a held parent turns the residual into a quarantine whenever a marked child names it.

## Stopped processes: how the base behaved

- **A stopped group member that handles TERM** (node, the guardian) received the group TERM and stayed stopped, with the TERM pending, through all of `term_grace_s` (15 s). The census stayed non-empty until the group KILL. A stopped member with TERM's default action ended on the TERM at once: review r1 measured a stopped Python process go from T to Z within 0.5 s of SIGTERM, while one with a TERM handler stayed T until SIGCONT. (This line said every stopped member waited, which overstated it.)
- **A stopped escaped member** was never signalled, so the attempt quarantined (cancel) or quarantined without any signal (finalization, start grace).
- **Now:** both get SIGCONT right after their TERM. The regressions end within the grace by TERM, not KILL (`test_a_stopped_group_member_is_continued_after_its_term_and_ends_within_the_grace`, `test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is`, and the real-daemon stopped-guardian case).

## Invariants (tested)

1. **No terminal state with live census pids.** An attempt that ends anything but `quarantined` has a verified-empty census at its end, cwd holders aside (property, regressions).
2. **Nothing outside the census is signalled.** Every pid signalled one by one was a live member of a census the protocol took; group signals go only to the recorded group (property).
3. **Continue follows TERM.** Every CONT delivered to a process follows a TERM delivered to that same process, pid and birth; the guardian is never continued (fix round 1; property, regressions).
4. **Never signalled:** an operator's process in the worktree that is no ancestor of a marked member, a held parent (by the census that held it: since fix round 2b a later census's own attribution supersedes a hold, C-5.5), another user's process, the daemon, its parent, another attempt's guardian, and (fix round 1) a stranger in a leaderless group at the recorded pgid (property, regressions).
5. **No retry before containment.** No retry is reserved while an earlier attempt's census is not empty (regressions, in process and through a real daemon).
6. **cwd never decides a verdict.** A cwd-only census never changes a verdict (#240; regression and mutation).

## Tests

New files:

| File | Tests | What |
|---|---:|---|
| `tests/unit/test_kill4_terminal_containment.py` | 17 | every terminal path on a simulated host (`tests/kill4_world.py`), the cwd negative case, stopped members, exclusions, uncertain attribution, decision 4, the gated guardian |
| `tests/unit/test_census_parent_chain.py` | 14 | the parent-chain rule and held parents |
| `tests/unit/test_kill4_properties.py` | 1 | bounded Hypothesis property, 120 derandomized examples (invariants 1 to 4) |
| `tests/unit/test_kill4_mutations.py` | 8 | strict-xfail mutation controls |
| `tests/fake/test_kill4_terminal_daemon.py` | 6 | the three 2026-10-20 shapes through a real daemon, guardian and shell provider |
| `tests/process/test_census_parent_chain_process.py` | 3 | the rule on a real `/bin/sh` loop (real libproc uid and cwd reads) |

Mutations, each detected by its named regression (strict xfail, all 8 xfailed as required in holds 3 and 4):

| Mutant | Named regression |
|---|---|
| finalize `lost` without containment (`lost-uncontained`) | `test_a_dead_guardians_escaped_marked_runner_is_signalled_not_left` |
| quarantine writers after the receipt uncontained (`receipt-uncontained`) | `test_writers_left_after_the_exit_receipt_are_contained_then_the_receipt_decides` |
| reserve a retry before containment (`retry-first`) | `test_start_grace_contains_a_stopped_guardian_before_any_retry` |
| drop the parent-chain rule (`no-parent-chain`) | `test_a_cancel_contains_a_hidden_driver_in_the_worktree_with_its_respawning_child` |
| signal an uncounted cwd process (`signal-cwd`) | `test_an_uncounted_operator_process_in_the_worktree_is_never_signalled_and_the_verdict_stands` |
| let a cwd-only census change a verdict (`cwd-verdict`) | the same |
| TERM a stopped member without continuing it (`no-continue`) | `test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is` |
| forget held parents (`no-hold`) | `test_an_in_place_hidden_driver_is_held_never_signalled_and_the_attempt_quarantines` |

**At the base.** All 40 new regressions failed on `3d94eb563` (hold 4, the new files copied into an archive of the base and run with its code on `PYTHONPATH`):
- **Daemon shapes, as intended:** the counted loop is quarantined with the loop alive; the held loop has no `held_pids`; a finished job's `kill` answers "already finished"; `--confirm-dead` answers "already finished".
- **The 13 in-process cases** failed there first on the fixture: the base's `KernelProcess` has no `uid`. Hold 5 reruns them with that copy of the fixture made base-compatible (below).
- **The parent-chain and gate-reap cases** fail because the rule and the helper do not exist at the base.

Existing tests changed deliberately, because D-KILL4 changes the signal authority they pinned. Each change is named in its commit:
- `tests/fake/test_census_escapers_daemon.py`: the D-CEN1 retitled and renamed escapers and D-KILL2's lockf are now contained, not left waiting in quarantine. The assertions keep their sources (marker, responsibility, recorded identity), and the stranger is still never signalled.
- `tests/fake/test_census_self_exit_daemon.py`: D-KILL3's inspected lockf is contained.
- `tests/unit/test_daemon_settle.py`: start grace with a live guardian is contained first.
- `tests/unit/test_census_survivors_review.py`: TERM, then KILL.
- `tests/unit/test_census_recycled_pid.py`: the walk below matching roots is allowed; recycled roots and strangers never are.
- `tests/fake/test_census_cwd_outcome.py`: counted members are signalled, cwd holders never.
- `tests/fake/test_census_recycled_pid_daemon.py`: a responsibility member is signalled under its own birth; the uncertain-evidence case is unchanged and passes.
- `tests/fake/test_daemon_contract.py`: the nested-setsid escaper is contained (new test); quarantine and force-release now use a held loop.

The limitation tests pinned by #240 are unchanged and pass (decision 6).

Holds (each through `suite_hold.sh`, GIL CPython 3.14 venv in scratch, single process, at most 20 minutes):

| Hold | Files | Result |
|---|---|---|
| 1 | the parent-chain unit file | 14 passed |
| 2 | briefed census, procs, settle, salvage, replay, process, state-contract and module-name files, plus the new unit files | 514 passed, 18 failed, 8 xfailed (13 min). Failures: existing tests pinning the old authority (fixed above), my daemon tests' wrong shell check, and four harness 5 s start timeouts |
| 3 | the same plus the new property, mutation and process files | 513 passed, 22 failed, 16 xfailed (18 min). 18 of the failures were the harness's 5 s daemon-start wait at load 75 to 82 (pre-existing, review r1 of #240); the 4 others were fixed (a leftover assertion, an allowed set, the young-child ppid) |
| 4 | base run as above; head: the new daemon and process files, escapers, self-exit, cwd outcome, daemon contract, the wall files, lost acknowledgement, r185, r223 wall probes, inspection load and pacing, cwd rechecks | base 40 failed (intended); head 248 passed, 3 failed, 8 xfailed (9 min). Failures: the held-loop timing (fixture children lived 0.4 s, so the one pre-finalization census landed between two; children now live 5 s) and the nested-setsid contract test (rewritten) |
| 5 | base: the 31 in-process cases with the fixture made base-compatible in the scratch copy; head: the new daemon file, the daemon contract, the new unit, mutation, property and process files, crash matrix, resume contract, record secrecy, probe cancel and recovery, turn trees, notice header | base 29 failed, 2 passed. The 2 were safety cases the base also satisfies (nothing signalled); both now also assert that the protocol ran. Head 185 passed, 28 failed, 8 xfailed (14 min): 26 were the harness's 5 s daemon-start wait (25 `condition timed out after 5s`, 1 crash-matrix `TimeoutExpired` after 5 s); 2 were my held-loop cases. One was counted by an inspection racing the detached loop's intermediate parent, and so rightly contained; the other's cleanup called `--confirm-dead` once while the loop's last child still lived. Both are fixed in the test (20 s inspection pacing; freeze the loop, end its children, then end it) |
| 6 | base: the two strengthened safety cases; head: the new daemon file, both rewritten contract cases, the new unit and mutation files, state contract, module names | base 2 failed (intended); head **63 passed, 8 xfailed (the 8 mutants), 0 failed** (2 min) |

**Every new regression fails at the base.**
- 17 in-process cases (holds 5 and 6).
- 14 parent-chain cases.
- 6 real-daemon cases (hold 4).
- 3 real-process cases (hold 4).

**Every new or changed test passes at head** in its latest run:
- The real-daemon file and both rewritten contract cases (hold 6).
- The escapers, self-exit, cwd-outcome, recycled-pid, settle, census and procs files (holds 3 and 4).
- The property (holds 2 to 5) and the mutations (holds 3 to 6).

**Not run, or not verified at head:**
- **The briefed whole-directory runs of `tests/unit` and `tests/fake`.** About 13,400 and 2,100 tests could not fit in the time left. Each hold waited 20 to 40 minutes for a slot (12 to 14 holds queued host-wide, load 41 to 82), and holds 2 to 5 used most of the window. Unit and fake files outside the holds above are unrun here; CI runs them.
- **`tests/fake/test_crash_matrix.py`.** Its 9 cases hit only the harness's 5 s start wait in hold 5. A last guarded hold for it ran only if it got a slot before the 4 h 30 m cutoff; see the final answer.
- **Other `test_daemon_contract.py` cases.** Hold 4 ran them (all but the rewritten one passed); in hold 5 they hit the same start wait.
- **No real Codex or Claude provider** was run.

The harness's 5 s daemon-start wait (`tests/fake/conftest.py:87`) is pre-existing (review r1 of #240) and was left unchanged. Only this build's own daemon file waits up to 60 s, through a local wrapper.

## Fix round 1 (review r1 of #250, 2026-10-23)

Review r1 asked for changes: one P1, one P2 and nine P3s, each executed against `96be9454d` and the base `3d94eb563`. Every finding below is fixed, each with a regression that fails at `96be9454d` (plus the restack merge).

### Restack onto #240's round 2

`fix/kill3-self-exit-217` at `3aa49607c` (#240's fix round 2 and release `15a73b7bd`, which brings #241) is merged in `90e8e362a`.
- **The contract** was merged by the hub's `merge_contract.py HEAD 3aa49607c`: 254 clauses, none duplicated.
- **`subfleet/daemon.py`, the one conflict:** `_forget_paced` drops the per-attempt dicts both sides added (`_cwd_wait_next` from #240, `_containment_rounds` from D-KILL4).
- **A semantic conflict the merge needed fixed:** #240 round 2 made `_cwd_wait` take the time its census began (`began=`, for pacing). D-KILL4's receipt-writers path calls it after the kill protocol's last census, so it passes `time.monotonic()`. Without this, every finalization whose writers were contained would raise `TypeError`. The kill4 cwd-rule unit test clears `_cwd_wait_next` as #240's own tests do.
- **#240's rules kept:**
  - the cwd worker's lease fence (`CWD_WORKTREE_HOLDS`);
  - the paced waits (`_cwd_census_due`);
  - containment-only evidence in `_resolve_quarantine`;
  - the wall's dead-guardian branch: `_lost` on an empty containment census, else D-KILL4's `_kill_attempt(lost=True)`. #240 review r2's P3 on that branch running a protocol with no targets no longer applies.
- **What the merge broke** (the reviewer's holds, on the merge):
  - #240's round-1 strict mutant `cwd-quarantines` (`_finalize` deciding on `verified_empty`) passed its named regression. #240 round 2 moved that test's clock (29.9 s to 29.0 s). Under D-KILL4 the mutant's only effect is a kill protocol with no targets, so neither the verdict nor its timing showed it. The regression now asserts what C-5.5 says: a cwd-only census starts no kill protocol (`9ce8b1501`).
  - Nothing else. Hold 2's 21 failures were all `Harness.start`'s 5 s readiness wait at load 105 to 118, before any test behaviour ran. That wait is now 60 s (`949d6811c`).

### Findings: cause and fix

- **P1: a recycled, leaderless group was signalled as the attempt's.**
  - **Cause:** `is_recycled` returned False for an absent leader, so the census kept `seen.group(pgid)` with no birth or boot check. `_signal_targets` then treated every group member as attributed by the census, in any census. `_contain` also persisted those members as counted identities, which made them walk seeds from the next census on.
  - **Fix:**
    - The census verifies the recorded leader: alive in this table with its recorded birth and boot (`Containment.leader_verified`).
    - A group member is a target only when the leader is verified (the group's TERM reaches it anyway) or when its identity is in `owned_identities`. Otherwise it holds the attempt and is never counted for its group.
    - A recorded boot that is provably not this one drops the group source (`recycled`: `group of another boot`). One that cannot be compared leaves the members holding, never signalled for their group, and makes the census unverifiable. The brief said to drop that group too; dropping it would change no signal, would hide its members from the evidence, and would break C-5.3's four pinned cases (`test_census_recycled_pid.py`: "unknown identity retains the root and group"), so members of an uncomparable group are held instead.
    - C-5.4 and C-5.5 say so.
- **P2: start grace continued a stopped guardian, which then launched its provider.**
  - **Cause:** start grace sent the group a TERM, which the guardian ignores by design (`guardian.py:186`). The protocol then continued every stopped member, the guardian included.
  - **Fix:**
    - Start grace with no `start.json` KILLs the group at once (`unlaunched`). The guardian has launched nothing, and its TERM is a no-op. A guardian stopped before its setsid does not yet lead its group, so killpg is refused: it is KILLed alone, never TERMed (F1 found this shape through the real daemon, under load).
    - The protocol never continues the guardian, on any path.
    - Start grace also passes its deciding census to the protocol, rather than taking a second one.
- **P3-1: later joiners were continued without a TERM.**
  - **Cause:** `reached` credited a delivered group TERM to every current group member, including those born after the killpg.
  - **Fix:** the group's TERM is credited only to the (pid, birth) pairs of the census taken before it that are still in the group. A member found since gets its own TERM before any continue. After a KILL, each surviving target still gets a KILL of its own.
- **P3-2: held parents could quarantine forever, and `kill` did not name them.**
  - **Cause:** `_parent_chain` held a recorded parent by its birth whether or not any marked process remained, and `_census_words` listed only live pids.
  - **Fix, holding:**
    - A held parent is held again only while the last census saw a marked process name it (`held_marked`, written from `Containment.held_fresh`).
    - The kill protocol's censuses count as one census. Without that, each child of a respawning driver that the protocol TERMs opens a gap, and two censuses 0.1 s apart would release the driver mid-protocol.
    - An attempt recorded before this key falls back to `held_parents`.
  - **Fix, naming:** `kill` and `--confirm-dead` answers name held parents (`held parent, never signalled`).
  - **The report:** "What remains open" now names owned-worktree jobs whose loop runs outside the worktree.
- **P3-3: a parent-chain ancestor became a walk seed.**
  - **Cause:** `_contain` persisted every live member as a counted identity, chain ancestors included, so the walk descended from them.
  - **Fix:**
    - Chain ancestors are kept by their birth in `chain_identities`, with their reasons in `chain_reasons` (event `attempt.chain_recorded`). Later censuses re-add them while they live with that birth (`ParentChain.recorded`).
    - They are never counted and never seed a walk, so their other descendants are members only when another source attributes them.
    - The report's safety argument (its "Direction" line) is corrected.
- **P3-4: chain reasons never reached durable evidence on the incident paths.**
  - **Cause:** the deciding census on those paths persisted the ancestor only as a counted identity, and `_record_containment` kept reasons from the protocol's first and last censuses only.
  - **Fix:**
    - Every census persists each chain addition with its reason, on every path.
    - The protocol accumulates the chain and held reasons of every census it takes.
    - Each run's evidence lists every signal it sent one by one, with the pid, why that pid is the attempt's, the census sources that counted it then, and its parent (`signals`, up to 256), plus the group's signals and their reason.
- **P3-5: taint could be laundered.**
  - **Cause:** taint spread only along the current census's parent links. Once the tainted ancestor exited, its orphaned child looked clean.
  - **Fix:** a census persists the identity of every seed it finds tainted, through its parent links, into `unverified_identities` (event `attempt.taint_recorded`). The taint then outlives the ancestor.
- **P3-6: the stopped-process premise was overstated.**
  - **Fix:** the report and C-5.6 now state what was measured. TERM's default action ends a stopped process. A handler leaves the TERM pending until continued.
  - **`tests/kill4_world.py` models both kinds** (`term_handler`), and SIG_IGN too.
- **P3-7: C-5.5's probe sentence was wrong.** Probes run no parent chain: their records have no attempt id. The sentence is removed (the hub's default), and C-5.5 now speaks of in-place jobs only.
- **P3-8: weakened tests are restored.**
  - **`test_census_escapers_daemon.py`:**
    - the lockf cases, rooted and unrooted, again check D-KILL2's counting by recorded identity with ppid 1, and the 10/18 `--confirm-dead` step while lockf waits. The escapers ignore TERM (an inherited SIG_IGN, which a waiting lockf keeps, measured), and the daemon runs a 4 s TERM grace;
    - the `:223` case checks the KILL sent after the guardian's death, not only the first census;
    - the docstrings say what the tests now check.
  - **`test_daemon_settle.py`:** start grace's census list is strict again: exactly one census of its own, the rest the protocol's.
  - **`test_census_self_exit_daemon.py`:** asserts `contained`, and that the runner is dead at the verdict.
- **P3-9:** `docs/release-gates.md` points at the current quarantine test.
- **Markers as signal authority** (check 2, read only): a process that inherited the attempt's environment is signalled wherever it runs, a tmux pane included. Stated as accepted in C-5.5 and under "What remains open".

### Invariants

1. **No stranger is signalled.** A process is signalled only when attributed by one of:
   - a verified leader;
   - an owned identity;
   - a recorded counted identity of matching birth;
   - the attempt's markers;
   - the parent-chain rule.

   Never by a recycled or leaderless group, cwd alone, or a held parent. Tested by the property (strangers in a leaderless group at the recorded pgid are reached by no signal), the six P1 regressions, and the real leaderless-group process test.
2. **Contained before anything follows.** On every terminal path, no retry is reserved and no verdict is written while a census member lives, unless the attempt quarantines. Tested by the property (invariant 1) and the build's regressions.
3. **No CONT without that pid's TERM, and never a CONT to the guardian.** Every CONT delivered follows a TERM delivered to that same (pid, birth). Tested by the property, the joiner regressions (simulated and real), and the guardian regressions (simulated, real guardian and real daemon).
4. **Held parents release.** A held parent with no marked descendant in the last census no longer holds. Tested by `test_a_held_parent_with_no_marked_descendant_left_releases`, with `test_a_held_parent_stays_held_while_a_marked_child_is_seen` as its control.
5. **Evidence is complete.** Every pid signalled one by one appears in durable evidence with its reason, and every chain addition with its reason, on every path. Tested by the property (invariant 5) and `test_every_path_records_the_chain_ancestor_and_its_reason` (start grace, dead guardian, exit receipt, cancel, wall).

### Mutations

Each mutant is detected by its named regression, as a strict xfail in `tests/unit/test_kill4_fix1_mutations.py`:

| Mutant | Named regression |
|---|---|
| keep a recycled group's members as targets | `test_kill_on_a_finished_job_never_signals_a_strangers_leaderless_group` |
| continue the guardian | `test_the_guardian_is_never_continued_on_a_cancel` |
| credit the group TERM to later joiners | `test_self_stopping_children_born_after_the_group_term_each_get_a_term_before_any_cont` |
| re-hold parents forever | `test_a_held_parent_with_no_marked_descendant_left_releases` |
| seed walks from chain ancestors | `test_a_chain_ancestors_unmarked_child_is_never_signalled` |
| drop chain reasons from evidence | `test_every_path_records_the_chain_ancestor_and_its_reason[exit-receipt]` |

The build's `retry-first` mutant now targets the new start-grace call.

### Fix round 1 holds

Each hold was one pytest through `suite_hold.sh` on a GIL CPython 3.14.7 venv in scratch, single process. Each ran from a `git archive` snapshot of the named commit, with `PYTHONPATH` set to it, so edits made while a hold waited could not leak in. Load was 70 to 145, with up to about ten holds queued host-wide, and slot waits ran 17 to 90 minutes.

| Hold | Commit | Files | Result |
|---|---|---|---|
| m-hold1 | merge `90e8e362a` | review r1's hold 1 | 252 passed, 12 xfailed, 1 failed: #240's `cwd-quarantines` mutant passed its regression (fixed in `9ce8b1501`); 10 m 41 s |
| m-hold2 | merge | review r1's hold 2 | 187 passed, 21 failed: every failure was `Harness.start`'s 5 s readiness wait at load 105 to 118 (now 60 s, `949d6811c`); 11 m 50 s |
| h-hold3 | merge code, the new tests and the 60 s wait | review r1's hold 3, plus the new regressions run against the head | hold 3: 61 passed, 4 xfailed (D-KILL2's documented cases), 1 failed. The failure is `test_d_cen1_a_real_recycled_root_and_its_child_are_strangers[after-absent-ps-True]`, a pure-`procs` test the merge did not touch (neither `procs.py` nor the test changed): a host-wide marker scan made the census unverifiable under load; rerun in f-hold2. The new regressions at the head: 30 failed, each for its finding (below), and 2 controls passed; 2 m 55 s |
| f-hold1 | `6b220c43c` plus the joiner grace (`200532a83`) | the new unit, mutation, property and process files, review r1's hold 1, `test_daemon_settle`, `test_state_contract`, `test_census_cwd_outcome` | 380 passed, 18 xfailed, 8 failed, all fixed in `9714784c3` and `bf787a891`: C-5.3's four uncomparable-boot cases, D-KILL2's counted property, the moved `signal-cwd` mutant target, and the two real-daemon stopped-guardian cases (stopped before its setsid); 7 m 05 s |
| f-hold2 | `ec3fe49f2` | f-hold1's eight failures and their files, the new unit, mutation, property and process files, review r1's hold 2 and hold 3 (capped at 19 min) | 258 passed, 18 xfailed, 4 failed; 13 m 20 s. Every f-hold1 failure passes. The restored escaper checks pass (9 of 9, rooted and unrooted), as do the crash matrix (9 of 9) and the responsibility process file (12 of 12, the merge-time flake included). The failures: two `test_daemon_contract` cases whose jobs ended correctly about 1 s after the harness's own 5 s `until` (a1 `starting-no-receipt`, a2 `succeeded`; running reached at the edge); the self-exit `stopped-turn` case, where an inspection ran before the escaper's setsid, so lockf was recorded as owned, not counted (the attempt was still contained and accepted); and the stopped-guardian real-daemon case, where the harness's 0.65 s start grace KILLed the guardian before the test could stop it (now run under a 3 s start grace, `11552924f`) |

**At the head, each new regression failed for its finding** (h-hold3):
- **P1:** the six simulated shapes TERMed the stranger (`[('pid', 5600, 15)]`, `[('pid', 5601, 15)]`), and the real leaderless group's member was signalled.
- **P2:** the real guardian started its provider ("the protocol started the provider it was stopping"). The real daemon's start grace sent the group `[9, 15]`, and the simulated guardian was continued.
- **P3-1:** joiners were continued with no TERM of their own.
- **P3-2:** `kill` and `--confirm-dead` did not name the held parent, and it never released.
- **P3-3:** the chain ancestor's unmarked child was TERMed (`('pid', 6202, 15)`).
- **P3-4:** no `chain_reasons` on any of the five paths.
- **P3-5:** the orphan was TERMed (`('pid', 5700, 15)`).
- **P3-8:** start grace took two censuses before acting.
- **The property** found three distinct failures: evidence missing a signalled pid, a CONT with no TERM of its own, and a stranger signalled.
- **The restored escaper checks** failed at the head only on the evidence the fix adds (`signals`); the behaviour they check held there.

f-hold1, per file:

| File | Result |
|---|---|
| `tests/unit/test_kill4_fix1.py` | 20 passed |
| `tests/unit/test_kill4_fix1_mutations.py` | 6 xfailed (all mutants detected) |
| `tests/process/test_kill4_fix1_process.py` | 3 passed (real guardian, real leaderless group, real joiners) |
| `tests/unit/test_kill4_properties.py` | 1 passed (120 examples) |
| `tests/unit/test_kill4_terminal_containment.py` | 17 passed |
| `tests/unit/test_kill4_mutations.py` | 7 xfailed, 1 failed (`signal-cwd` target moved; fixed) |
| `tests/fake/test_kill4_terminal_daemon.py` | 5 passed, 2 failed (stopped before setsid; fixed) |
| `tests/process/test_census_parent_chain_process.py` | 3 passed |
| `tests/unit/test_daemon_settle.py` | 68 passed |
| `tests/fake/test_state_contract.py` | 37 passed |
| `tests/fake/test_census_cwd_outcome.py` | 6 passed |
| `tests/unit/test_census_recycled_pid.py` | 21 passed, 4 failed (fixed) |
| `tests/unit/test_census_counted.py` | 4 passed, 1 failed (fixed) |
| the other 14 `tests/unit/test_census*.py` files | 195 passed, 5 xfailed (#240's round-1 and round-2 mutants) |

f-hold2, per file: `test_kill4_fix1` 21 passed; `test_kill4_fix1_mutations` 6 xfailed; `test_kill4_mutations` 8 xfailed; `test_kill4_properties` 1 passed; `test_kill4_terminal_containment` 17 passed; `test_census_recycled_pid` 25 passed; `test_census_counted` 5 passed; `test_census_parent_chain` 14 passed; `test_census_survivors_review` 14 passed; `tests/process/test_kill4_fix1_process` 3 passed; `tests/process/test_census_hidden_environment_process` 15 passed, 4 xfailed; `test_census_parent_chain_process` 3 passed; `test_census_raw_markers_process` 3 passed; `test_census_responsibility_process` 12 passed; `fake/test_census_escapers_daemon` 9 passed; `fake/test_census_recycled_pid_daemon` 29 passed; `fake/test_census_inspection_finalization` 3 passed; `fake/test_crash_matrix` 9 passed; `fake/test_salvage_finalization` 47 passed; `fake/test_daemon_contract` 20 passed, 2 failed; `fake/test_census_self_exit_daemon` 2 passed, 1 failed; `fake/test_kill4_terminal_daemon` 6 passed, 1 failed.

## Fix round 2a (review r2 of #250, 2026-10-26): #240's approved head, #259, and P2-1

Review r2 (REQUEST_CHANGES at `380b626cf`) found one P2 and four P3s, and its check 4 planned how #250 meets #259 (D-QUAR-REUSE1, boot-scoped census seeds). #240 was approved at `8fe3413ef` (review r7, plus its merge of `release/217` at `4a40fbe45`, which carries #259 as `33baa9cca`), and GitHub reported #250 CONFLICTING against it. This round merges that head, combines the two rules as check 4 planned, and fixes P2-1. r2's P3s and the start-grace defect D-QUAR-START1 are round 2b's.

Commits, on `380b626cf`:
- `a4568ca08`: the merge of `origin/fix/kill3-self-exit-217`, which resolved to `8fe3413ef`, with the contract and the code combined;
- `679fde004`: #259's differentials measure against this line's census before the boot scope;
- `e083418cc`: the combination tests (`tests/unit/test_kill4_boot_scope.py`);
- `08c3cf82f`: #259's invariant-4 fake test asks what the kill protocol targets;
- `cdd867e24`: P2-1;
- `bac000ac8`: the mutation controls;
- then this report.

### The merge

`git merge origin/fix/kill3-self-exit-217` conflicted in the three files r2's trial merge named: `docs/acceptance-contract.md`, `subfleet/daemon.py` and `subfleet/procs.py`. Everything else merged cleanly.

**The contract.** `merge_contract.py HEAD MERGE_HEAD` resolved it against the base `4cddd7925`: 255 clauses, none duplicated, and the `uniq -d` check prints nothing.
- **Changed on both sides:** C-5.5 and C-5.9, as expected. Every clause only one side changed equals that side's text, word for word (C-5.4, C-5.6 and C-5.7 from #250; 15 clauses from #240 and #259).
- **C-5.9:** #250 changed its third sentence (survivors past the window are contained first) and #240 changed sentences 11, 17 and 19 (the cwd reader row at every ending, and the paced loss). The edits touch different sentences, so the tool's word merge (449 + 43 + 245 = 737 words) reads as one rule, and nothing was added.
- **C-5.5:** the word merge is exact (2,266 + 659 + 625 = 3,550 words), but the two sides described the recorded group twice: #259's "the recorded group is excluded" once the attempt's boot is over, and #250's leader check ("group of another boot", "boot identity unavailable for group"). It now says which comes first: the boot scope, then the leader check, which decides only for an attempt on this boot or with an unknown boot. It also says that #250's keys take the boot filter (below), and that a held parent's boot is compared too.
- **C-5.4:** an owned identity attributes a group member only when it is not of another boot than the attempt's.
- **The change log:** both sides added entries at the top. The tool placed #250's two entries (2026-10-23 and 2026-10-22) above #240's three (2026-10-26, 2026-10-26, 2026-10-25). They are now in date order, newest first, with #250's entries beside #240's of the same dates. A new 2026-10-26 entry for this round heads the list.

### Each conflict hunk

**`subfleet/procs.py`, `containment`, five hunks:**

| Hunk | #250 (`380b626cf`) meant | #240 / #259 (`8fe3413ef`) meant | Kept |
|---|---|---|---|
| 1, the signature | a `parent_chain` keyword (D-KILL4's chain rule) | an `attempt_boot` keyword (the boot the guardian identity records) | both, keyword-only: `parent_chain`, then `attempt_boot` |
| 2, the helpers | `leader_boot()` (compare the recorded leader's boot, C-5.12's fresh read on a mismatch) and `leader_verified = False` | `attempt_boot_over()` (read once, only when a seed or a failed root check needs it) and `exclude()` (record a seed or the leader under `recycled`) | both, unchanged: they read different records and write different things |
| 3 and 4, the group block (git split one block in two) | `compared = len(errors)`, drop a group whose present leader is proven recycled, then the leader check: verified only alive with its birth and boot, `group of another boot` for a provably other boot, `boot identity unavailable for group` when it cannot be compared; then the walk's roots | when the attempt's boot is a known UUID that is over, exclude every recorded pid the table shows (or that parents a row) and the group's leader, clear the group, and seed nothing; otherwise the base path (drop a recycled leader's group, seed the roots) | r2's plan: #259's boot-over branch first; #250's `compared`, recycled-leader drop and leader check in its else branch, before the roots. The `leader_verified and pgid in groups` guard on the return is kept. |
| 5, the return | `chain_pids`, `held_*`, `held_fresh` and `leader_verified`, by keyword | `other_boot`, positional after `responsible_pids` | `other_boot` in its dataclass position, then #250's keywords |

**Can both group rules hold at once?** Yes; there is no counterexample, so nothing was picked silently.
- **Where both apply** (the attempt's boot is a known UUID and over, and the group has members): both drop the group. #259 records the leader under `recycled` for "different boot identity (attempt's boot)", or the reason its birth proves; #250 would have recorded `group of another boot`. They differ only in that reason, and r2's plan, like C-5.5, takes #259's. The leader is then recorded once.
- **Where only #250's applies:** the attempt's boot is this one, or unknown (no boot recorded, legacy seconds, or a current boot that cannot be read or fell back to seconds). The leader check runs exactly as at `380b626cf`.
- **#250 never holds what #259 drops:** #259's branch runs only when the attempt's boot provably differs from this one. There the leader's recorded identity is the guardian's, on the attempt's boot, so #250's check would find that boot provably other and drop the group too; it holds members only for a leader on this boot that is absent, or one whose boot cannot be compared. A `pgid` other than the guardian's comes from a stored identity, which #259's filter drops if it is of another boot.
- **`group of another boot` is still reachable,** but only for an attempt whose own boot is not a UUID while its leader's identity is: for example a row recorded with no boot, or with a non-UUID boot string, as `test_a_group_recorded_in_another_boot_is_no_source_at_all` does.

**`subfleet/daemon.py`, `_contain`, two hunks:**

| Hunk | #250 meant | #259 meant | Kept |
|---|---|---|---|
| 1, the census call | pass `parent_chain` when the attempt has an id | pass `attempt_boot=boot`, then define `attempts(pid)`: a census identity of another boot than the attempt's is never kept | both arguments. `attempts(pid)` is now `on_boot(identity)` over the census identity, so the same filter serves held identities too |
| 2, `sighted` (the counted write) | subtract chain ancestors and members only a leaderless group names | keep only `attempts(pid)` | both |

**The combination, beyond the conflict hunks** (r2's plan, check 4):
- **Reads.** #259's `attempts()` filter in `containment` also covers `parent_chain.held` (`held_marked`, or `held_parents` for an older row) and `parent_chain.recorded` (`chain_identities`). A stored chain ancestor or held parent of another boot than the attempt's is never re-added or held, and is named in `other_boot`. A new `ParentChain.carried` takes what the kill protocol's own censuses held (`_held_scope`). Those are this boot's and are not stored, so they skip that filter, and a respawning driver stays held through one protocol run on any boot, as before.
- **Re-holding a parent** compares its boot, not only its start: `_parent_chain`'s `same_boot()`. Only a proven other boot releases it (after C-5.12's fresh read); an unknown or unreadable comparison keeps holding, which never signals.
- **Writes.** `on_boot`/`attempts` guard every key #250 writes: `held_parents`, `chain_identities`, `held_marked` (from `_contain`, and the kill protocol's stored `fresh` set) and the taint write into `unverified_identities`.
- **Taint.** `_tainted` never matches a record of another boot than the member's or the attempt's. Legacy seconds and a missing boot still compare by start.
- **Ownership.** `_signal_reasons` ignores an owned identity of another boot than the attempt's (`_attempt_boot`). #259's review found no writer that can store one today; this makes invariant 1 hold by construction.

### Semantic conflicts the merge needed fixed

Four, besides P2-1:
1. **#259's differentials** (`test_census_boot_seeds.py`'s `at_base`) called release/217's census (15bd8c172) with every argument `_contain` passes except `attempt_boot`. `_contain` now also passes `parent_chain`, so they raised `TypeError`. And #250's parent-chain rule and leader check change the census on every boot, so "on the attempt's own boot the census is 15bd8c172's" no longer holds on this line.
   - **Fix:** the census #259's boot scope changes here is #250's at `380b626cf`. `tests/reference_census_380b626cf.py` keeps a verbatim copy (lines 808-1172, rebound to `procs`' globals, as the 15bd8c172 copy is), and `at_base` compares with it.
   - Its `_parent_chain` is today's, which differs from `380b626cf`'s only for a held parent stored or carried. `at_base` asserts that the generated worlds pass none.
2. **#259's orphaned-group test** documents what release/217 counted ("Release/217 counted both"). #250 alone already drops that group as another boot's, so its `before` now asks 15bd8c172's census (`at_release_217`).
3. **#259's invariant-4 fake test** said "the kill protocol signals by `owned_identities` alone". Since D-KILL4 the census is the authority, so the marker process 90 is a target. `signalled == []` still held only because the fixture's `ps` answers one start for every pid (#259's review r1, P3-5). It now records what the protocol asks `procs.signal_process` for: never 83026 or 83000 (boot B's counted and responsible identities), and exactly 90.
4. **P2-1** (below).

### P2-1

`test_an_uncounted_operator_process_in_the_worktree_is_never_signalled_and_the_verdict_stands` asserted the exclusive `worktree:<tree>` key after the job ended. #240's round 3 (merged at `380b626cf`) converts it when a cwd holder remains (`_export_locked` → `_keep_for_cwd`). The test now asserts that the exclusive key is gone and that `folders.turn_key(str(tree), JOB, writable=False)` is held by `folders.cwd_holder(JOB)`, as #240's `test_r3_export_converts_the_hold_and_frees_the_lifetime_row` does. Its comment says the worktree stays held as a reader row. It passes: 17 of 17 in its file at 22:14Z, and again in the holds below.

### The 12 cases

`tests/unit/test_kill4_boot_scope.py` runs each case on the simulated host (`tests/kill4_world.py`) with a current boot the test sets (`BootWorld`: a boot-session UUID, legacy seconds after a failed UUID read, or unreadable). C-5.3's real `same_process` and `liveness` run over it, so a signal is delivered only when its captured identity, boot included, is the live process's. `world.tried` records every signal asked for, delivered or refused.

**How each parent was run.** Each parent ran from a `git archive` tree in scratch, holding the head's test file and the parent's product code. The holds checked that `subfleet` was imported from that tree.
- `380b626cf` needed nothing else: its test helpers are the head's.
- `8fe3413ef` lacks `tests/kill4_world.py` and `tests/unit/test_kill4_terminal_containment.py` (for `owned_worktree`), so the head's copies were added. The head's `tests/fake_kernel.py` was added too, with a two-line shim, because `8fe3413ef`'s `KernelProcess` has no `uid`. Its product code was not touched.

Results per case. "API" means the test failed because the parent lacks something #250 added; that failure says nothing about behaviour.

| Case | Tests | `380b626cf` (#250 alone) | `8fe3413ef` (#240 with #259) | Head |
|---|---|---|---|---|
| 1. A leaderless group at the recorded pgid, same boot: held, never signalled | 3 (census, `kill`, dead guardian) | pass 3 | fail 3: `kill` answers "already finished" (it never re-contains a finished job); the dead-guardian path keeps the stranger 5600 in `counted_identities`, a walk seed from then on; the census has no `leader_verified` (API) | pass 3 |
| 2. The same with the attempt's boot over: no group, no hold, recycled once with #259's reason, `--confirm-dead` releases | 1 | fail: the leader is recorded as `group of another boot` | fail (API: `leader_verified`); #259's reason is right there | pass |
| 3. A legacy or unreadable boot: members hold with "boot identity unavailable for group", never signalled | 3 (legacy attempt boot; unreadable and legacy current boot) | pass 3 | fail 3: no group error, so the uncomparable leader leaves the census verifiable in two of them | pass 3 |
| 4. A live process at the guardian pid with the recorded start after a reboot: excluded, leader not verified | 3 (census, `kill`, dead guardian) | pass 3 | fail 3 (API: `leader_verified`) | pass 3 |
| 5. An owned identity from another boot: never a seed, never a target | 1 seed test, 1 attribution test | fail 2: 5700 and its child 5701 are walk members, and the protocol asks a TERM for each (both live with that identity, so the world delivers it); `_signal_targets` attributes the member by that owned identity | seed test passes; attribution test fails (API: `_signal_targets`) | pass 2 |
| 6. A counted or responsible identity from another boot: never a seed | 2 | fail 2: a TERM asked for 5700 and 5701 | pass 2 | pass 2 |
| 7. A chain identity or held parent from another boot: never re-added, never held | 1 chain, 5 held (on the attempt's boot A and stored on B and C), 3 guards | fail 6: a TERM asked for the chain ancestor 5900; 6000 held by its start in all five; guards pass 3 | fail 9 (API: no chain or held keys) | pass 9 |
| 8. A marked process on a later boot: a member and a target; chain ancestors persisted? | 1 | fail: 6100 kept in `counted_identities`, the first key the test checks | fail (API: `chain_pids`); there the protocol would not target 6100 at all | pass. Pinned: 6100, its chain ancestor 6099 and its held parent 6098 are in no stored key. 6100 and 6099 are TERMed; 6098 is never signalled |
| 9. A taint record from another boot never matches | 6 unit, 1 write | unit fail 6 (API: `_tainted` takes no boot); the write fails: 7100 written to `unverified_identities` from boot B's record | fail 7 (API) | pass 7 |
| 10. #259's trackb shape through #250's protocol | 2 (`--confirm-dead` then `kill`; the receipt) | fail 2: stays quarantined (`recorded identity unavailable for pid 4243`), and the receipt path quarantines | pass 2 | pass 2 |
| 11. r1's six P1 shapes, same boot and after a reboot | 12 | same boot: pass 6; after a reboot: fail 6 (`group of another boot`) | fail 8: `kill` and `--confirm-dead` never re-contain or take a census, the uncomparable-boot shape (API), and the non-UUID recorded boot has no `group of another boot`; pass 4 (both dead-guardian shapes, the UUID recorded boot after a reboot, another attempt's provider on the same boot) | pass 12 |
| 12. The property: attempt boot (UUID, legacy, none) x current boot (same, other UUID, legacy, unreadable) x every stored key, 60 derandomized examples | 1 | fail: a held parent stored on boot A held again on boot B by its start; a `held_parents` write of boot B | fail (API: `held_pids`) | pass |

Totals over the 46 tests: `380b626cf` 28 failed, 18 passed (the 22:29Z hold); `8fe3413ef` 37 failed, 9 passed (the 22:57Z hold); head 46 passed (hold H2, below). Each parent run also held one scratch-only check, which passed: `subfleet` was imported from that parent's tree (so those holds report 19 and 10 passed).

**Why some cases pass at a parent:**
- **Cases 1, 3 and 4 pass at `380b626cf`.** Each is the same-boot (or uncomparable-boot) ground #250 already covered (r1's P1 shapes, and C-5.3's uncomparable-boot cases); #259 adds nothing there. They are guards that the merge keeps #250's rule where #259 does not apply.
- **Case 4's behaviour is the same at both parents:** a live process at the guardian pid whose boot differs is proven recycled by `is_recycled` at `380b626cf`, and by the boot scope at `8fe3413ef`. At `8fe3413ef` it fails only because `Containment.leader_verified` does not exist there.
- **The order of the two group rules** shows only where both apply, so cases 2 and 11 (after a reboot) are the ones that fail at `380b626cf`: there the leader is recorded as `group of another boot`, not for the attempt's boot.
- **At `8fe3413ef`** the census is not the signal authority (no `_signal_targets`, no `leader_verified`, no chain or held keys), so the cases that ask about #250's protocol fail there for that reason; the behaviour #259 alone gives is noted in the table.

### Invariants

1. **No stranger is signalled, on any boot.** Nothing stored on another boot than the attempt's, or recorded on another boot than the current one, seeds the walk, holds the attempt or is a signal target. Executed by the property (every stored key, every attempt boot and current boot) and cases 5 to 9 and 11.
2. **#259 cannot add a signal.** With the attempt's boot over, the group, walk and responsibility sources are empty; the only members are marked processes and the worktree ancestors the chain adds for them. Executed by the property and cases 2, 4, 8, 10 and 11.
3. **#250 cannot weaken #259.** No key #250 stores (`chain_identities`, `held_parents`, `held_marked`, the taint in `unverified_identities`) gains an identity of another boot than the attempt's. Executed by the property and cases 8 and 9.
4. **Each side's own regressions still pass:** #250's must-checks (r2's table: the six P1 shapes, cases 3 to 5 of r2's must-check table, stranger reuse) and #259's tests. Executed by the holds below.

The property found no counterexample at the head. At `380b626cf` it found two:
- a parent stored in `held_marked` on the attempt's boot A, held again on boot B by its start alone (invariant 1);
- a `held_parents` write of boot B (invariant 3).

### Mutants

`tests/unit/test_kill4_boot_scope_mutations.py` holds the brief's three mutants as strict xfails (each must fail its named regression with an `AssertionError`). The first is split into its read and write halves, so each half is shown detected on its own. All four are detected (H3, at the committed text: 4 xfailed). r2's earlier mutants are detected too: the build's 8 and fix round 1's 6, 14 xfailed in H1.

| Mutant | The change | Named regression | Detected |
|---|---|---|---|
| unscoped-reads | drop #250's keys from the boot filter on read: `containment` no longer filters `parent_chain.held` and `.recorded` | `test_case_7_a_chain_ancestor_stored_on_another_boot_is_never_re_added` | yes |
| unscoped-writes | drop #250's keys from the boot filter on write: `held_parents`, `chain_identities`, `held_marked`, the taint write, and the protocol's stored `fresh` set | `test_case_8_a_marked_process_on_a_later_boot_is_a_target_and_nothing_is_kept` | yes |
| start-only-rehold | compare the start time only when re-holding a parent (`_parent_chain` without `same_boot`) | `test_case_7_a_held_parent_from_another_boot_is_never_held[attempt-boot-marked]` | yes |
| leader-first | move #250's leader block before #259's boot-over branch | `test_case_2_with_the_attempts_boot_over_the_group_is_no_source_and_confirm_dead_releases` | yes |


### Fix round 2a holds

One pytest per hold, under the shared suite lock, at the head (`bac000ac8` and this report). Counts per file:

| Hold | File | Result |
|---|---|---|
| H1 (23:06Z, load 173) | `tests/unit/test_kill4_terminal_containment.py` | 16 passed, 1 failed: `test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is`'s 2.5 s bound took 15.1 s at load 173. It passed on its rerun in H2 (load 67); the bound is unchanged |
| H1 | `tests/unit/test_kill4_properties.py` | 1 passed |
| H1 | `tests/unit/test_kill4_fix1.py` | 21 passed |
| H1 | `tests/unit/test_kill4_mutations.py` | 8 xfailed (the build's mutants, each detected) |
| H1 | `tests/unit/test_kill4_fix1_mutations.py` | 6 xfailed (fix round 1's mutants, each detected) |
| H1 | `tests/unit/test_census_parent_chain.py` | 14 passed |
| H1 | `tests/unit/test_census_counted.py` | 5 passed |
| H2 (23:17Z, load 67) | `tests/unit/test_census_recycled_pid.py` | 25 passed |
| H2 | `tests/unit/test_census_survivors_review.py` | 14 passed |
| H2 | `tests/unit/test_daemon_settle.py` | 68 passed |
| H2 | `tests/unit/test_census_boot_seeds.py` (#259) | 36 passed |
| H2 | `tests/unit/test_kill4_boot_scope.py` (the combination) | 46 passed |
| H2 | `tests/unit/test_invariants_index.py` | 25 passed |
| H2 | `tests/unit/test_test_module_names.py` | 1 passed |
| H2 | H1's failing test, rerun | passed |
| H3 (00:45Z, load 136) | `tests/fake/test_census_boot_seeds_daemon.py` | 5 passed (#259's fakes) |
| H3 | `tests/fake/test_salvage_adversarial.py` | 8 passed |
| H3 | `tests/fake/test_salvage_worktree_gone.py` | 6 passed |
| H3 | `tests/unit/test_kill4_boot_scope_mutations.py` | 4 xfailed (strict, AssertionError only): every combination mutant detected |
| H4 (01:06Z, load 50) | `tests/fake/test_kill4_terminal_daemon.py` | 7 passed |
| H4 | `tests/fake/test_census_escapers_daemon.py` | 4 passed, 1 failed (load, below); stopped at 7 min by SIGINT, since at about 40 s per test the hold would have passed 10 minutes |
| H5 (01:16Z, load 27) | `tests/fake/test_census_escapers_daemon.py` | 9 passed, H4's failure among them (rerun once) |
| H6 (01:19Z, load 52) | `tests/fake/test_state_contract.py` | 37 passed |
| H6 | `tests/process/test_census_parent_chain_process.py` | 3 passed |
| H6 | `tests/process/test_kill4_fix1_process.py` | 3 passed |
| H6 | `tests/fake/test_census_cwd_outcome.py` | 6 passed |
| H7 (01:21Z, load 45) | `tests/fake/test_daemon_contract.py` | 21 passed, 1 failed: `test_c5_8_second_daemon_exits_69_and_stale_lock_is_taken_over` (load, below) |
| H8 (01:25Z, load 43) | `tests/fake/test_census_recycled_pid_daemon.py` | 29 passed |
| H8 | `tests/fake/test_daemon_contract.py::test_c5_8_second_daemon_exits_69_and_stale_lock_is_taken_over` | failed again, the same way |
| 01:27Z and 01:28Z (load 48, 55) | `tests/fake/test_daemon_contract.py::test_c5_8_second_daemon_exits_69_and_stale_lock_is_taken_over at each parent` | `380b626cf`: fails the same way; `8fe3413ef`: fails earlier, at that line's 5 s daemon-start wait |
| H9 (01:29Z, load 40) | `tests/fake/test_census_self_exit_daemon.py` | 3 passed (round 2b's; run because time remained) |

Every file the brief lists ran.

**The three failures, all timing bounds under load; none is the merge's:**
- **H1, `test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is`:** #250's 2.5 s bound on `_finalize` took 15.1 s at load 173. It passed on its rerun (H2, load 67). The bound is unchanged.
- **H4, `test_d_cen1_an_escaper_seen_through_the_guardian_stays_counted_after_the_kill_protocol`:** the attempt was quarantined with the TERM-ignoring escaper alive. The kept state root showed why. The kill protocol's first census recorded `marker read unavailable for pid 81493`, a process gone by the time the kept root was read, so that census was unverifiable. The escaper 81347 was only a responsibility member, and C-5.4 makes such a member a target only in a verifiable census, never once an unverifiable census has recorded it (`unverified_identities`, where its identity was). So the protocol signalled only the verified group (`group_signals` 9 and 15, `signalled: []`), and the escaper, in another process group (81328), outlived it. On one boot the merge leaves marker reads and that rule as `380b626cf` has them; its taint change concerns records of another boot only. It passed on its rerun (H5, load 27).
- **H7 and H8, `test_c5_8_second_daemon_exits_69_and_stale_lock_is_taken_over`:** the second daemon did not exit 69 within the test's 3 s `subprocess.run` timeout, at load 45 and again at load 43. `python -c 'import tests.fake.run_daemon'` itself takes 0.45 s. It fails the same way at `380b626cf`, whose test file and `run_daemon.py` are the head's. At `8fe3413ef` it fails earlier, at that line's 5 s daemon-start wait, which this branch raised to 60 s on 2026-10-23 (`949d6811c`) (the lane rules list that wait as a local-only failure). So the 3 s bound fails on this host without the merge. It was not loosened, and CI runs it.

Each failure root the harness kept under `/tmp/sf-failed` was read, then removed.

## Fix round 2b (review r2 of #250, P3-1 to P3-4, and D-QUAR-START1), finished by its continuation round 2c

Round 2b (job `20261026-191802-pr250-fix2b`) built the hub's rulings on r2's four P3s (decisions 1 to 4) and the start-grace defect D-QUAR-START1 (decisions 5 and 6) on `b52a63842` in nine commits. Its provider exited (rc 1) at 03:49:30Z on 2026-10-27, during hold H3 at load 205, before it had written this section, run the rest of its holds or taken its measurement past 12 samples. Its continuation, round 2c (job `20261026-210244-pr250-fix2c`), audited those commits against round 2b's brief decision by decision, finished what was missing, ran every hold, and wrote this section.

Commits, on `b52a63842`:
- round 2b: `8ce56cc2f`, `3c81bb71b` and `333f5e827` (the red tests), `08ff8ce4c` (decision 1), `05f9dc58a` (decision 2), `f2e0ab9a9` (decision 5), `690ee9e7d` (decision 6), `5e7143d54` (its hold H1's findings), `37438d1bb` (the contract, and this report's line on `--force-release`);
- round 2c: `9e844698a` (decision 4's property, finished), `923413a34` (the orphan, below), `d02820eb4` (the handshake property forces its order), `cdfbbf5bc` and `f568ff61b` (two regressions hold E found, from `5e7143d54` and `690ee9e7d`), then this section.

### The orphan that ended round 2b

At 03:49:20Z round 2b's hold H3 started a fake daemon for `tests/fake/test_quar_start1_daemon.py`'s past-the-bound case (`python -m tests.fake.run_daemon --state-root /private/tmp/sf-8hn7l747 --start-delay 60 --start-grace-s 2 --start-grace-max-s 8`). The harness starts it in a session of its own (`isolated_group`). Ten seconds later the provider exited and pytest went with it, before any fixture teardown ran. The fake daemon ran on with ppid 1, the real daemon quarantined the round ("writers remain after exit receipt"), and the job ended `lost`. The hub ended the process by its exact pid. The fake daemon had nothing tying it to its test:
- **`tests/fake/run_daemon.py`, `ParentWatch`:** the fake daemon compares `os.getppid()`, every 0.2 s, with the test process that started it. The harness passes its own pid (`--parent-pid`), so a test that is gone before the daemon could first look is caught too; without the flag it is the parent at start. Once the two differ, the daemon stops as SIGTERM stops it, then `end_attempt_groups` KILLs the group of every guardian the store or a `start.json` records that is still alive by its recorded identity (pid, boot, start). A stop that has not ended the process 10 s later ends the groups and the process. Every caller was checked: the harness (plain and rooted, whose `sh -c exec` keeps pytest as the parent) and C-5.8's second daemon in `test_daemon_contract.py` all start it straight from pytest, and none needs it to outlive its parent. `tests/e2e` imports only `audit_publication` from it.
- **The harness's teardown** (`tests/fake/conftest.py`, the `daemon` fixture) already stopped the daemon on a failed test, and pytest runs fixture teardown on KeyboardInterrupt too, at session finish (`_pytest/runner.py`, `pytest_sessionfinish`). It ended only guardians with a `start.json`, though, so a guardian still starting outlived a failed test. It now uses the same `end_attempt_groups`.
- **`tests/guardian_steps.py`**, the paused real guardian of the D-QUAR-START1 process tests: a pause exits once its test process is gone (`GUARDIAN_STEPS_PARENT`), not after its 120 s deadline. Each of those tests now creates its guardian inside the `try` whose `finally` stops it; two created it before.
- **Tests** (`tests/fake/test_fake_daemon_parent_watch.py`, 5): SIGKILL the helper process that stands for the test, after it started (a) a serving daemon with a guardian still starting (`--start-delay 60`), (b) a daemon not yet answering, or (c) a paused guardian. Each must be gone within 15 s; here they went in 0.61, 0.86 and 0.23 s, and (a)'s guardian with it. A scratch pytest session's failed test and interrupted test (`raise KeyboardInterrupt`) must leave neither the daemon nor its starting guardian. With the harness of `9e844698a` all five fail: (a) to (c) outlive 15 s, and in the two teardown cases the daemon is stopped but the starting guardian outlives it.

### Round 2b against its brief

| Brief item | At `37438d1bb` | Commit | Test | Round 2c |
|---|---|---|---|---|
| Decision 1: one end for a contained start grace | done | `08ff8ce4c` | `test_kill4_fix2b.py::test_a_contained_start_grace_has_one_end_whether_or_not_a_cwd_holder_is_present` (2) | none |
| Decision 1: C-4.2 and C-5.9, change log | done | `37438d1bb` | `test_state_contract.py` | none |
| Decision 2: `kill` with nothing it may signal | done | `05f9dc58a`, `5e7143d54` | `test_kill_force_release_kill_kill_on_a_held_only_census_runs_no_protocol`, `test_kill_on_a_finished_job_with_a_signallable_member_still_contains_it`, boot-scope cases 1 and 11 | none |
| Decision 2: the report's line on `--force-release` | done | `37438d1bb` | | none |
| Decision 3: no pid signalled from a census that held it (property) | done | `8ce56cc2f` | `test_kill4_properties.py` (invariant 4) | none |
| Decision 3: C-5.5 | done | `37438d1bb` | | none |
| Decision 3: r2's counterexample pinned | partly: an example test pins r2's shape, but the property's CI profile never generates it (r2) | `3c81bb71b`, `333f5e827` | `test_a_pid_held_by_one_census_is_signalled_when_a_later_census_attributes_it` | added as the property's `@example` (`9e844698a`), spelled so that census 0 holds 5001 and census 1 TERMs it by lineage (traced) |
| Decision 4: every ending in `trees()` | done | `8ce56cc2f` | the property: receipt, cancel, start grace, dead guardian, wall, `kill` on a finished job | none |
| Decision 4: an unverifiable first census, a stranger's failed marker read | built, but did not do its job: the "marker ignored" mutant survived | `8ce56cc2f` | | invariant 6 and a strict-xfail control (`9e844698a`), below |
| Decision 4: the joiner test on a fake clock | done | `8ce56cc2f`, `5e7143d54` | `test_kill4_fix1.py::test_self_stopping_children_born_after_the_group_term_each_get_a_term_before_any_cont` | none |
| Decision 5: extension within `START_GRACE_MAX_S` | done | `f2e0ab9a9` | `test_kill4_fix2b.py` (running guardian; five refusals; the property), `test_quar_start1_daemon.py` (2), `test_kill4_terminal_daemon.py` (stopped) | none |
| Decision 5: measurement, 30 or more samples | 12 samples | salvage | | 27 more, 39 in all, below |
| Decision 6: the daemon's abandon on every path | done | `690ee9e7d`, `5e7143d54` | `test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end` (4), `test_unlaunched_writes_the_abandon_for_a_starting_attempt_whatever_its_caller` | `_kill_attempt` read the job row on every kill, launched ones too, and its starting check read `a["state"]` from rows that have none: three census tests failed (`cdfbbf5bc`, `f568ff61b`) |
| Decision 6: the guardian's two checks, the abandoned receipt | done | `690ee9e7d` | `test_quar_start1_process.py` (4), `test_an_abandoned_receipt_reads_as_unlaunched_never_as_a_provider_result` (3) | the interleaving property now forces its order (`d02820eb4`) |
| Invariants 1 to 5 | tested | | below | invariant 6 of the property added |
| Mutants | all 9 the brief names built; at `5e7143d54` skip-abandon-unlaunched failed its control, and its fix (`37438d1bb`) never ran | `8ce56cc2f`, `5e7143d54`, `37438d1bb` | `test_kill4_fix2b_mutations.py`, `test_quar_start1_process.py` | all run, all detected (table below) |
| The report section | not written | | | this section |
| Holds from H3 on | cut off in H3 | | | all run, below |
| `[stopped-turn]` alone, three times | not run | | | passed 3 of 3 |

### Decisions as built

1. **One end for a contained start grace (r2's P3-1).** When the kill protocol of an unlaunched attempt (no `start.json` at the abandon's last read) ends with no member left, cwd holders aside, the attempt ends through `_unlaunched(…, census=census)`: `failed`, `starting-no-receipt`, the job queued again while it has attempts left. Nothing launched, so the retry is safe. With a shell in the worktree the queued job keeps its worktree key while the shell sits there; with none the key goes, and the retry takes it again. The regression runs r2's input (a writable owned worktree, `max_attempts` 2, a guardian stopped before its `start.json`) with and without an operator's shell in the worktree and expects one state and one retry decision. Before, the case without the shell ended `lost` with no retry. Four earlier tests that expected `lost` now expect the unlaunched end.
2. **A `kill` with nothing it may signal schedules no protocol (r2's P3-2).** `_may_signal` is true for an unverifiable census (a later read may verify it) or when `_signal_reasons` names a member. Otherwise `kill` answers `already finished` and names the members `held, never signalled`; `--confirm-dead` says none of them may be signalled. r2's sequence (`kill`, `--force-release`, `kill` twice) answers `already finished` four times in under a second: nothing scheduled, no signal, no `terminal_containment` or `recontainment` round, no `attempt.recontainment_started`.
3. **A later attribution supersedes an earlier hold (r2's P3-3).** No product change: as r2 read it, the code held per census, and the property's oracle was wrong. The property now asserts that no pid is signalled from a census that held it (each signal is matched to the census taken before it), and C-5.5 says a hold never authorizes a signal and vetoes none. r2's counterexample is pinned twice: the example test, and the property's `@example` (pid 5000 of another uid in the guardian's group with its cwd in the worktree; its child 5001 in its own group, unmarked, cwd elsewhere, respawning marked children). Traced: census 0 holds 5001, and census 1 TERMs it.
4. **Property coverage and a fake clock (r2's P3-4).** `trees()` draws all six endings, and draws an unverifiable first census (the first table read fails) and a stranger's failed marker read (pid 5200, whose argument buffer the first censuses cannot read). The joiner test runs on a fake clock that advances by each wait (`FakeClock`), so its 2 s grace takes 20 censuses whatever the load.
   - **What round 2c found:** r2 named the read injection "the only injection that detects the marker-ignored mutant" (case 5's workers attributed through the parent chain only: `_signal_reasons` without its marker branch). Round 2c ran that mutant against round 2b's property: it passed, 120 derandomized examples with the injection and 120 without (04:26Z, load 54). A marked member no other source reaches is never signalled, and the attempt is quarantined with it alive, which invariant 1 allows.
   - **Invariant 6** (below) closes that. A first form, "every process any census found marked is gone", failed on the real code: a driver of another uid, which nobody may signal, respawned a marked child into the protocol's last census, and the attempt was quarantined with that child alive. That is the design (the settle loop's last census decides and does not act: "the last census, never a guess about a pid, decides"), so the invariant excludes the last census. With it the property passes 120 derandomized and 400 random examples, and the mutant fails it with the read injection and without it. A strict-xfail control keeps it so (`test_the_marker_ignored_mutant_fails_the_kill4_property`).
5. **D-QUAR-START1 (a): a slow, healthy start is given time.** At start grace's expiry with no receipt, `_start_extends` extends the deadline only while the census is verifiable, has no held member, and has exactly one member: the attempt's guardian by its recorded pid, start and boot, in a ps state that is neither `T` nor `Z`. The deadline is looked at again at the census's pacing (the start-grace branch runs after `_inspect_running`'s interval and `_cwd_census_due`). The bound is `START_GRACE_MAX_S` = 6 × `START_GRACE_S` = 60 s from the deadline's start: the launch, or the first look of a daemon restarted since, as the deadline lives in daemon memory. The first extension is an `attempt.start_extended` event. Past the bound, or the moment a condition fails, the existing branch runs unchanged: the group KILL, then decision 1's end. Measured below: 60 s has ample headroom.
6. **D-QUAR-START1 (b): the abandon handshake.**
   - **The daemon** (`_abandon_start`): publish `abandon.json` (`{"at", "reason"}`, by atomic rename, unless it is there), then read `start.json` for the last time. A marker that cannot be written raises, and nothing ends.
   - **The guardian** (`guardian.abandoned`): it looks once its launch gate opens, and again after it publishes `start.json`, immediately before anything of the provider's is opened or spawned. Any entry at that name is a marker, readable or not; a folder it cannot look in counts as one. On finding one it spawns nothing and publishes `exit.json` with `rc` 127, `spawn_error` `abandoned before launch: <reason>` and `abandoned` {`at`, `reason`, `stage`: `gate` or `spawn`}. A reader that predates the key reads a provider that could not be spawned.
   - **The last read:** no `start.json`, and the attempt ends unlaunched (its group KILLed first if it has members, decision 1). A `start.json`, and the attempt may have launched: a fresh census, the full protocol with the group's TERM first, and the receipt decides. An abandoned receipt reads as unlaunched in `_finalize` and `_kill_attempt`, never as a provider's result.
   - **Every path that ends or signals a `starting` attempt** writes the marker first:
     1. start grace with an empty census (`_process_attempt`), before its cwd wait and `_unlaunched`;
     2. start grace with members, or with a `start.json` found by the last read: `_kill_attempt(lost=True, unlaunched=True)`, which runs the handshake at its top;
     3. a cancel, a client kill or a wall cancel reaching `_process_attempt`'s cancel branch: `_kill_attempt`;
     4. the wall enforced after an inspection: `_kill_attempt`;
     5. a daemon restart's recovery: a starting attempt's deadline is set again on the restarted daemon's first look, then 1 or 2;
     6. the launch gate's own failure (`guardian-identity-unavailable`): `_unlaunched` writes the marker for any `starting` row, though that gate is closed;
     7. an abandoned receipt read by `_finalize` or `_kill_attempt`: `_unlaunched`, the marker already there.

     The other `_unlaunched` callers (`cancelled-before-launch`, `needs a shell`, an unfinished launch-time check, `reserved-no-launch`) end `reserved` attempts, which have no guardian yet. `_enforce_wall` begins a starting attempt's finalization only on its exit receipt (path 7 when that receipt is an abandoned one) and otherwise requests the cancel of path 3, `_inspect_running` leaves a starting attempt to start grace, and a turn's `conversations.stop` takes no starting attempt, as its runner is registered only from `start.json`.
   - **Why it is safe:** each side writes its own file before it reads the other's, and both write by atomic rename. If the daemon's read finds no `start.json`, the guardian's write came after that read, so after the daemon's abandon; the guardian's look follows its own write, finds the abandon, and spawns nothing. If the guardian's look finds no abandon, the daemon's write came after that look, so after the guardian's `start.json`; the daemon's read follows its own write, finds `start.json`, and contains the attempt as launched. Both may see each other; then the guardian spawns nothing and the abandoned receipt ends the attempt unlaunched after containment. C-4.2 states the same argument.
   - **No live state:** the marker lives only in the attempt folder, and only the guardian and the daemon read it.

### Invariants

Each is executed; the property-based ones say so.
1. **No orphan provider:** in every interleaving of the daemon's write-abandon, read-`start.json` and the guardian's gate look, write-`start.json`, look, spawn, a provider never runs once the daemon has found nothing launched. A Hypothesis property over the orders on real processes (`test_no_provider_runs_for_an_attempt_the_daemon_ended_unlaunched_in_any_interleaving`, each of the 10 orders an explicit example). Round 2c made it force its order: it released one side's step and moved on without waiting for that step to finish, so the two raced. The real code passes every order regardless, but an order mutant failed only on some runs, and Hypothesis reported it flaky (hold B). Now each released step is done before the other side moves.
2. **A slow but healthy start is not lost:** a running, verified guardian whose `start.json` lands within the bound is never killed and never finalized for want of a receipt. Unit (`test_a_running_guardian_alone_past_start_grace_is_given_more_time_and_then_runs`) and through a real daemon: 20 s before `start.json` under a 10 s grace succeeds on its one attempt, no retry, no abandon, `attempt.start_extended` recorded, no `attempt.kill_started`.
3. **Bounded:** no extension passes `START_GRACE_MAX_S`, and a stopped guardian or an unverifiable census never extends. A property over stopped × unverifiable × another member × elapsed time (60 derandomized examples), the five refusals as examples, and a real daemon KILLing a 60 s delay at its 8 s bound, not at its 2 s grace.
4. **One end:** a contained start grace ends in the same state with the same retry decision whether or not a cwd holder is present (decision 1's regression, both ways).
5. **Holds are not sticky, and never authorize:** no pid is signalled from a census that held it. The kill4 property (its invariant 4) and r2's counterexample, as an example test and as the property's `@example`.
6. **The kill4 property's invariant 6 (round 2c):** every process that a census other than the last found carrying the attempt's markers, with the identity it captured and not held, is gone by the end, whatever the verdict (C-5.4: a marker attributes in any census; C-5.6). Property-based; detects the marker-ignored mutant.

### Decision 5's measurement

Round 2b's `measure.py`, run unchanged from its salvage: one guardian at a time, spawned as the daemon spawns one (`procs.spawn_disclaimed`, the daemon's identity reads, the launch gate opened), its provider `/usr/bin/true`, timed from spawn to its `start.json`, with `sysctl -n vm.loadavg` read just before each. It generates no load beyond the guardian it measures, and ran outside the suite lock, as it runs no test. Round 2b took 12 samples (03:19 and 03:32Z); round 2c took 27 in batches of three spread across its run, never during one of its own holds.

- **39 samples**, load1 21.2 to 71.3; every guardian wrote its `start.json`.
- **Spawn to `start.json`:** min 0.30 s, median 0.82 s, 90th percentile 2.94 s, max 4.54 s (load1 65.4, 03:32:37Z).
- **Against load:** a least-squares line gives 0.044 s per unit of load1 (r = 0.63), and 3.9 s at load 100, past the 71 the samples reached.

| load1 | samples | median | max |
|---|---|---|---|
| 0 to 30 | 15 | 0.44 s | 3.44 s |
| 30 to 50 | 12 | 0.89 s | 1.48 s |
| 50 to 70 | 11 | 2.01 s | 4.54 s |
| 70 and over | 1 | 4.52 s | 4.52 s |

**Against the bound.** The worst start measured, 4.54 s, is under half of `START_GRACE_S` (10 s), and `START_GRACE_MAX_S` (60 s) is about 13 times it. The samples do not reproduce the incident: by the brief's timeline its attempt started at 18:33:37.7Z, under load 60 to 100, and its guardian wrote `start.json` and launched the provider at 18:34:09Z, about 31 s later. What made that start slow is not visible here, and the host never passed load 71 while these were taken. 60 s covers the incident's 31 s about twice over and every start measured by more than ten times, so no change is proposed.

| UTC | round | load1, load5 before | spawn to gate open | spawn to `start.json` |
|---|---|---|---|---|
| 03:19:12 | 2b | 37.54, 39.46 | 0.011 s | 0.699 s |
| 03:19:16 | 2b | 38.38, 39.60 | 0.008 s | 0.977 s |
| 03:19:21 | 2b | 36.34, 39.16 | 0.048 s | 1.030 s |
| 03:19:25 | 2b | 36.32, 39.11 | 0.007 s | 0.959 s |
| 03:19:30 | 2b | 36.29, 39.06 | 0.009 s | 1.482 s |
| 03:19:35 | 2b | 36.51, 39.06 | 0.013 s | 1.229 s |
| 03:32:15 | 2b | 62.71, 59.57 | 0.020 s | 2.817 s |
| 03:32:21 | 2b | 62.25, 59.52 | 0.015 s | 2.011 s |
| 03:32:29 | 2b | 64.07, 59.95 | 0.030 s | 3.828 s |
| 03:32:37 | 2b | 65.42, 60.38 | 0.520 s | 4.540 s |
| 03:32:44 | 2b | 68.91, 61.18 | 0.010 s | 2.554 s |
| 03:32:53 | 2b | 71.30, 61.94 | 0.489 s | 4.519 s |
| 04:12:27 | 2c | 64.02, 53.14 | 0.034 s | 1.255 s |
| 04:12:32 | 2c | 61.05, 52.71 | 0.038 s | 1.482 s |
| 04:12:39 | 2c | 58.57, 52.33 | 0.177 s | 2.394 s |
| 04:42:51 | 2c | 55.42, 48.34 | 0.015 s | 0.435 s |
| 04:42:55 | 2c | 55.31, 48.43 | 0.008 s | 0.571 s |
| 04:42:59 | 2c | 55.31, 48.43 | 0.006 s | 0.408 s |
| 05:11:49 | 2c | 36.72, 45.24 | 0.038 s | 0.575 s |
| 05:11:53 | 2c | 38.03, 45.37 | 0.041 s | 0.408 s |
| 05:11:57 | 2c | 37.87, 45.22 | 0.049 s | 0.582 s |
| 05:36:59 | 2c | 22.58, 25.60 | 0.009 s | 0.475 s |
| 05:37:04 | 2c | 21.81, 25.39 | 0.004 s | 1.390 s |
| 05:37:11 | 2c | 22.39, 25.45 | 0.050 s | 3.443 s |
| 05:55:53 | 2c | 24.71, 23.32 | 0.012 s | 0.301 s |
| 05:55:56 | 2c | 25.37, 23.48 | 0.031 s | 0.373 s |
| 05:56:00 | 2c | 25.37, 23.48 | 0.057 s | 0.416 s |
| 06:00:51 | 2c | 23.91, 26.19 | 0.013 s | 0.333 s |
| 06:00:55 | 2c | 24.88, 26.36 | 0.012 s | 0.316 s |
| 06:00:58 | 2c | 26.89, 26.75 | 0.044 s | 0.341 s |
| 06:05:18 | 2c | 21.25, 22.56 | 0.016 s | 0.594 s |
| 06:05:22 | 2c | 21.25, 22.56 | 0.006 s | 0.444 s |
| 06:05:26 | 2c | 21.31, 22.55 | 0.030 s | 1.005 s |
| 06:08:55 | 2c | 25.22, 25.36 | 0.029 s | 0.311 s |
| 06:08:59 | 2c | 23.92, 25.08 | 0.022 s | 0.487 s |
| 06:09:03 | 2c | 22.65, 24.80 | 0.010 s | 0.841 s |
| 06:12:11 | 2c | 35.60, 29.57 | 0.083 s | 0.824 s |
| 06:12:15 | 2c | 36.83, 29.92 | 0.007 s | 0.724 s |
| 06:12:19 | 2c | 37.56, 30.19 | 0.013 s | 1.027 s |

### Mutants

Strict xfail, AssertionError only; a missing mutation target raises RuntimeError and fails the test. All were run at `f568ff61b` (holds A2 and B3) and all were detected.

| Mutant | The change | Named test | Detected |
|---|---|---|---|
| extend-stopped | extend without the stopped check | `test_start_grace_extends_for_no_guardian_but_a_live_verified_running_one[stopped]` | yes |
| extend-unbounded | extend without the bound | `…[past-bound]` | yes |
| guardian-checks-first | the guardian looks for the abandon before it writes `start.json`, not after (`tests/guardian_steps.py`) | the interleaving property | yes |
| daemon-reads-first | the daemon reads `start.json` before it writes the abandon | the interleaving property | yes (in hold B, before the property forced its order, Hypothesis raised FlakyFailure) |
| skip-abandon-start-grace-empty | start grace's empty census ends with no abandon | `test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end[start-grace-empty]` | yes |
| skip-abandon-kill-attempt | `_kill_attempt` (start grace with members, a cancel, the wall) signals and ends a starting attempt with no abandon | `…[cancel]` | yes |
| skip-abandon-unlaunched | `_unlaunched` ends a starting attempt with no marker | `test_unlaunched_writes_the_abandon_for_a_starting_attempt_whatever_its_caller` | yes |
| recontain-no-targets | P3-2: `_recontain` scheduled with no signal reasons | `test_kill_force_release_kill_kill_on_a_held_only_census_runs_no_protocol` | yes |
| sticky-hold | P3-3: every pid in the evidence's `held_parents` is never signalled again | `test_a_pid_held_by_one_census_is_signalled_when_a_later_census_attributes_it` | yes |
| marker-ignored | P3-4: `_signal_reasons` without its marker branch | the kill4 property (invariant 6) | yes; round 2b's property did not detect it |

The earlier rounds' controls are still detected in the same holds: the build's 8, fix round 1's 6, round 2a's 4.

### Red runs

- **Round 2b, 02:48Z** (the red tests over `b52a63842`'s code): 24 failed, 5 passed. 18 of `test_kill4_fix2b.py`'s 21 failed, and so did all four real-process handshake tests and both real-daemon D-QUAR-START1 tests. Three of the passes pin what the base already did: decision 1 with the operator's shell (r2: that case already ended unlaunched), decision 2's other side (a signallable member is still contained) and an abandoned receipt through the kill protocol. The other two are in files the salvaged output does not name.
- **Round 2b, 03:07Z** (`3c81bb71b`): 5 failed. The P3-3 example failed on its own world: without the provider's owned identity census 1 was unverifiable, so taint, not a hold, kept 5001. `333f5e827` gave it that identity, as the property's world does.
- **Round 2c:** the marker-ignored mutant against round 2b's property: not detected, with the read injection and without it (04:26Z). The new orphan tests against `9e844698a`'s harness: 5 failed (04:55Z).

### Holds

Round 2c's, one pytest per hold under the suite lock, in this worktree. Nothing ran outside the lock. Counts per file at the head `f568ff61b`:

| Hold (UTC, load) | File | Result |
|---|---|---|
| A2 (05:38, 18) | `tests/unit/test_kill4_fix2b.py` | 21 passed |
| A2 | `tests/unit/test_kill4_fix2b_mutations.py` | 8 xfailed (every mutant detected) |
| A2 | `tests/unit/test_kill4_properties.py` | 1 passed |
| A2 | `tests/unit/test_kill4_terminal_containment.py` | 17 passed |
| A2 | `tests/unit/test_kill4_fix1.py` | 21 passed |
| A2 | `tests/unit/test_kill4_mutations.py` | 8 xfailed |
| A2 | `tests/unit/test_kill4_fix1_mutations.py` | 6 xfailed |
| A2 | `tests/unit/test_kill4_boot_scope.py` | 46 passed |
| A2 | `tests/unit/test_kill4_boot_scope_mutations.py` | 4 xfailed |
| B3 (05:42, 26) | `tests/process/test_quar_start1_process.py` | 4 passed, 2 xfailed |
| B3 | `tests/process/test_kill4_fix1_process.py` | 3 passed |
| B3 | `tests/process/test_census_parent_chain_process.py` | 3 passed |
| C2 (05:44, 34) | `tests/fake/test_quar_start1_daemon.py` | 2 passed |
| C2 | `tests/fake/test_kill4_terminal_daemon.py` | 7 passed |
| C2 | `tests/fake/test_fake_daemon_parent_watch.py` | 5 passed |
| C2 | `tests/unit/test_failed_keep_teardown.py` | 6 passed |
| E3 (05:35, 24) | `tests/unit/test_census_parent_chain.py` | 14 passed |
| E3 | `tests/unit/test_census_counted.py` | 5 passed |
| E3 | `tests/unit/test_census_recycled_pid.py` | 25 passed |
| E3 | `tests/unit/test_census_survivors_review.py` | 14 passed |
| E3 | `tests/unit/test_daemon_settle.py` | 68 passed |
| E3 | `tests/unit/test_census_boot_seeds.py` | 36 passed |
| E3 | `tests/unit/test_invariants_index.py` | 25 passed |
| E3 | `tests/unit/test_test_module_names.py` | 1 passed |
| F (05:47, 32) | `tests/fake/test_census_escapers_daemon.py` | 9 passed |
| F | `tests/fake/test_census_self_exit_daemon.py` | 3 passed |
| G (05:49, 25) | `tests/fake/test_census_recycled_pid_daemon.py` | 29 passed |
| G | `tests/fake/test_census_cwd_outcome.py` | 6 passed |
| G | `tests/fake/test_census_boot_seeds_daemon.py` | 5 passed |
| G | `tests/fake/test_salvage_adversarial.py` | 8 passed |
| G | `tests/fake/test_salvage_worktree_gone.py` | 6 passed |
| H (05:50, 23) | `tests/fake/test_daemon_contract.py` | 22 passed, C-5.8's 3 s second-daemon test among them (it failed at load 43 to 45 in round 2a) |
| H | `tests/fake/test_state_contract.py` | 37 passed |
| J (05:53, 21) | `tests/fake/test_crash_matrix.py` | 9 passed |
| J | `tests/fake/test_salvage_finalization.py` | 47 passed |
| J | `tests/fake/test_census_inspection_finalization.py` | 3 passed |

E3 ran at `cdfbbf5bc` with `f568ff61b`'s one-line change in the tree, before it was committed; every other hold above ran at `f568ff61b`. `test_fake_daemon_parent_watch.py` changed after C2 (`70aa01224`: a 300 s start grace, so no census under load can end its guardian early), and its final form passed alone in hold orphan2 (06:10Z, load 31): 5 passed, the daemons gone 0.86 and 0.73 s and the paused guardian 0.05 s after their test process was killed.

**`[stopped-turn]` alone, three times** (`test_census_self_exit_daemon.py::test_d_kill3_inspection_keeps_a_hidden_lockf_after_its_shell_provider_ends[stopped-turn]`, at `d02820eb4`): passed in 23.3 s (05:17Z, load 35), 19.0 s (05:19Z, load 27) and 12.6 s (05:24Z, load 24). r2's hold-C failure, a 60 s wait for the escaper's pid file, did not recur.

**Earlier holds, and what they found:**
- **A, B, C** (05:03 to 05:16Z, at `923413a34` and `d02820eb4`) passed as A2, B3 and C2 did, except hold B's daemon-reads-first control (FlakyFailure; invariant 1 above), fixed in `d02820eb4`. `test_kill4_fix1_process.py`'s joiner test, which failed in round 2b's H2 (4 joiners against 5, load 47 to 66), passed in B, B2 and B3.
- **E** (05:25Z, `d02820eb4`): 3 failed, 185 passed. `test_census_recycled_pid.py::test_e_recycled_root_never_becomes_a_new_kill_target` and both `test_census_survivors_review.py::test_recorded_setsid_survivor_is_individually_signalled` cases drive `_kill_attempt` on a core with no job lookup. `5e7143d54` read the job row there on every kill, launched attempts included. `cdfbbf5bc` reads it only for an unlaunched end. E2 then failed the same three on `a["state"]`, which `690ee9e7d` had begun to read: their rows (a running attempt's identities and evidence) have no state. `_kill_attempt` already reads `killed_by` and `pgid` with `.get`, and now reads `state` so too (`f568ff61b`); a store row always has its state. Round 2b ran neither file after `690ee9e7d`: they would have failed its H1, its H2's 89 tests are six other files, and its H3 ended after one test.
- **Scratch checks under the lock:** the marker-ignored mutant against round 2b's property (04:26Z); invariant 6 on the real code, first form failing and final form passing, and the mutant against it (04:32 to 04:39Z); the property at 400 random examples (05:56Z, passed in 174 s).

**Not run here:** the rest of `tests/unit`, `tests/fake`, `tests/process` and `tests/e2e`, which CI runs. The harness change reaches every fake test; the 15 fake files above passed with it. `tests/frontend/test_core_live.py` cannot run on a lane.

## Follow-up to review r3 of #250 (P3-1 to P3-3, and check 5's start-bound record)

Review r3 approved #250 at `966262ed7` with three P3s; #250 merged into `release/217` at `c9430763f`. This follow-up builds the hub's four decisions on that head (branch `fix/kill4-p3-217`).

### Decisions as built

1. **P3-1: an unverifiable census with no attributable member runs no protocol on `kill`** (`Daemon._may_signal`). A census with a member `_signal_reasons` allows still runs it, verifiable or not. A verifiable census keeps the rest of fix round 2b's rule. An unverifiable one now runs the protocol only when a verifiable read could attribute a member. That is a walk or responsibility member, minus held parents, `_never_signalled` pids and tainted members, or a census with no member at all, as when the listing failed. A member only a leaderless group at the recorded pgid names never qualifies.
   - **Taint:** the decision named walk and responsibility members as the ones a verifiable read could attribute. A tainted one (first recorded by an unverifiable census, or below one) is attributed by no census, verifiable or not (C-5.4). So it does not qualify either; otherwise the same repeat would continue for such a member.
   - **An unreadable identity:** a walk or responsibility member whose identity this census could not read still qualifies, unless a tainted census parent taints it: `_tainted` matches a member's own record by its identity, but follows parent links without one. A marked or parent-chain member, or a group member under a verified leader, whose identity this census could not read did not qualify as built here, which review r1 of #276 found (P2-1); it qualifies since fix round r1 (below), and an owned member since fix round r2. A member only a leaderless group at the recorded pgid names never qualifies, read or not.
   - **Case 3's world** (a stranger's leaderless group at pgid 4242, with a legacy attempt boot or an unreadable current boot) now answers `already finished … held, never signalled` to every `kill` of review r2's sequence. Nothing is scheduled, signalled or appended.
   - **What remains:** an unverifiable census that will never verify (a legacy attempt boot with that group) and also has an untainted walk member still runs the protocol on every `kill`. The rule allows it, and nothing wrong is signalled.
2. **P3-2: C-5.6 names every immediate KILL of a starting attempt.** It now says that a `starting` attempt whose last read finds no `start.json` has its group KILLed at once with no TERM: at an expired start grace, and at a cancel or a wall stop while it is starting (`_kill_attempt` → `_abandon_start` → `_kill_protocol(…, unlaunched=True)`, whose first group signal is `SIGKILL if unlaunched else SIGTERM`). "Need none" is limited to the `reserved` path; a starting attempt's `cancelled-before-launch` follows the protocol. No code changed.
3. **P3-3: the extension property sees the exact bound.** `test_extension_is_granted_exactly_while_every_condition_holds_and_never_past_the_bound` runs on a fake clock, with `@example`s at `START_GRACE_MAX_S` − 1e-6, exactly at it and + 1e-6. It asserts the expected answer for every draw: the 0.05 s band it used to skip is gone, as the clock's own reading decides. Under the ci profile, `>` for `>=` fails the at-the-bound example. A bound counted from the deadline (70 s) fails the just-past one, and the random draws between 60 and 70 s.
4. **Check 5: `attempt.start_landed`, the bound's production record** (`Daemon._start_landed`).
   - **When:** the first read that finds the `start.json` of an attempt with an `attempt.start_extended` event records it, once. That read can be the pass's first read, the read after start grace's census, or the abandon's last read (`_abandon_start`): past the bound, or at a cancel, a wall stop or a failed condition.
   - **What it holds:** `seconds` from the deadline's start to that read; `deadline_start`, `launch` or `restart`; and the grace and bound in force. `restart` means a restarted daemon's first look: the deadline lives in daemon memory, so `_starting_first_look` keeps the attempts whose deadline began there, and a `start.json` already there at that look reads 0 s.
   - **Not recorded:** the daemon keeps no count of extensions, so the event has none. An attempt never extended records nothing.
   - **No behaviour change:** nothing reads the record. The `daemon-reads-first` mutant is retargeted at `_abandon_start`'s new body, with the same reordering.

### Red runs

At `c9430763f`'s code with the red tests (`72e5436d4`), one hold at 05:40Z: 13 failed, 8 passed.
- **P3-1:** all three of case 3's variants and both of the new sequence's answered `containment requested`, and so did the tainted walk member's test. Both controls (listing failed, an untainted walk member) passed, as they should.
- **P3-3:** both mutants survived the committed property and were detected by the new one; the new property passes on the real code.
- **Check 5:** the five landing tests failed (no event); the two "records nothing" tests passed, as they should.

### Mutants

Strict xfail (`tests/unit/test_kill4_p3_mutations.py`); all detected in hold U1a.

| Mutant | The change | Named test |
|---|---|---|
| p3-1-unverifiable-always | every unverifiable census runs the protocol again (review r3's input) | `test_case_3_kill_force_release_kill_kill_on_an_unverifiable_census_runs_no_protocol[legacy-attempt-boot]` |
| p3-1-taint-ignored | a tainted walk member qualifies | `test_an_unverifiable_census_whose_walk_members_are_tainted_runs_no_protocol` |
| p3-2-starting-stop-terms-first | a cancel or wall stop while starting TERMs first | `test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end[cancel]` and `[wall]` |
| p3-3-extend-at-the-bound | `>` for `>=` | the extension property (the at-the-bound example) |
| p3-3-bound-from-the-deadline | the bound counted from the deadline (+10 s) | the extension property (the just-past example) |
| check-5-landed-unextended | a landing recorded without an extension | `test_a_start_that_lands_within_start_grace_records_nothing` |

### Holds

One pytest per hold under the suite lock, at `f224c5e90`'s code, from this worktree; load1 was 44 to 170.

| Hold (UTC) | Files | Result |
|---|---|---|
| U1a (06:51) | `test_kill4_boot_scope` 51, `test_quar_start1_landed` 7; `test_kill4_boot_scope_mutations` 4 xfailed, `test_kill4_p3_mutations` 7 xfailed | 58 passed, 11 xfailed |
| G (07:01) | `tests/fake/test_state_contract.py` 37, `test_test_module_names` 1, `test_invariants_index` 25 | 63 passed |
| U1b (07:12) | `test_kill4_fix2b` 21, `test_kill4_review_r3` 12, `test_kill4_properties` 1; `test_kill4_fix2b_mutations` 8 xfailed, `test_kill4_review_r3` 1 xfailed | 34 passed, 9 xfailed |
| P1 (07:31) | `tests/process/test_quar_start1_process.py` 4 passed and 2 xfailed (both handshake order mutants, `daemon-reads-first` retargeted), `test_kill4_fix1_process` 3, `test_census_parent_chain_process` 3 | 10 passed, 2 xfailed, in 632 s at load 99 to 138 |

Two earlier holds found two faults of this round, both fixed in `b4fe073dd`: cores built without `__init__` had no `_starting_first_look`, and the tests' `landed()` counted `add_event`'s bare audit row.

**Not run here,** because the lock queue at load 120 to 200 used the round's time:
- the rest of review r3's U1: `test_kill4_terminal_containment`, `test_kill4_fix1`, `test_kill4_mutations` and `test_kill4_fix1_mutations`. Their `kill` tests assert that nothing is signalled, or that a signallable member is contained, and decision 1 only removes protocol runs;
- review r3's F1, U2 and F2;
- `tests/frontend/test_core_live.py`, which cannot run on a lane.

CI runs them.

### Fix round r1 (review r1 of #276)

Review r1 asked for changes at `66274272e`: one P2 and six P3s. Its own commits (`6fa46225b`, `3208ee4dd`) closed the test gaps P3-1 to P3-3. This round builds the hub's decisions on `3208ee4dd`.

1. **P2-1: `_may_signal` counts an attributable member whose identity the census could not read.**
   - **The gap:** `_signal_reasons` skips a member with no captured identity, and P3-1's fallback counted only walk and responsibility members. A finished job whose escaped, marked runner had no start in the kill's first process table answered `already finished` and left the runner running. The rule before P3-1 ran the protocol, whose next census read the runner and TERMed it.
   - **The fix, review r1's:** in an unverifiable census, the unread members are the marked ones, the parent-chain ones, and the group's when this census verified its leader, minus the identities the census captured. Any of them that is neither a held parent nor `_never_signalled` runs the protocol.
   - **Not built:** the broader `live_pids` minus the identities. It counts a leaderless group's member with no identity; in review r1's run it sent both case 3 `[unreadable-current-boot]` tests back to `containment requested`, the loop P3-1 removed.
   - **No new signal path:** `_may_signal` only decides whether `kill` schedules `_recontain` and how `--confirm-dead` words its answer (`daemon.py:4581`, `:4601`). It names no target, so the protocol keeps its targets: the group after its leader's identity check, or a member `_signal_reasons` names (review r1, check 1). Case 3's sequences (`[legacy-attempt-boot]`, `[unreadable-current-boot]`) and the tainted-walk test still answer `already finished` four times and signal nothing (`world.tried` is empty).
   - **`--confirm-dead`:** on the runner's shape it now says "`subfleet kill <job>` contains them", and the `kill` that follows TERMs the runner.
   - **Tests** (`tests/unit/test_pr276_review_r1.py`): review r1's two strict xfails are plain cases. Constructed censuses pin each term: a parent-chain member and a verified group's member with no identity run the protocol; a leaderless group's member with no identity, and a never-signalled marked member with none, do not. A test runs `--confirm-dead` and then `kill` on the runner's shape.
2. **Review r1's observation on `_terminal_census`, left as it is:** it returns the attempt row it read before its census, so a member that same census first records as unverified is not yet tainted for `_may_signal`, and that one `kill` can run a protocol that signals nothing, a known gap that errs only toward running. `--confirm-dead`, worded from the same row, then says `subfleet kill <job>` contains them, and the `kill` that follows runs no protocol.
3. **Text:**
   - C-5.7's "a verifiable read could attribute" now also names a marked or parent-chain member, or a member of the recorded group under a verified leader, whose identity the census could not read.
   - "Changes in version 3" has one entry for C-4.2, C-5.6 and C-5.7 (review r3 of #250's P3-1, P3-2 and check 5), with P2-1's correction (P3-4).
   - In this follow-up's decisions above, the unreadable-identity bullet now says which members qualify, and the abandon's last read is past the bound, or at a cancel, a wall stop or a failed condition (P3-5).
4. **Review r1's unrun mutant:** `_start_landed` without its once-only check, now a strict-xfail control. Both recorded-once landings detect it: the read after start grace's census and the next pass's first read record 20.0 s and 21.0 s, and the abandon's last read, run twice, records 61.0 s twice.

#### Red and green

- **Red** at `3208ee4dd` (an archive of it with this round's venv, which imports the archive's `subfleet`): `test_pr276_review_r1.py` 7 passed, 7 xfailed. Both P2-1 cases xfailed strictly: the marked-runner test's `[p3-1]` and the constructed `[unidentified-marker-member]`.
- **Green** at `e92324032`, with the marks removed: 14 passed, 7 xfailed.
  - Its first run, at `520ad91f1`, failed one case this round added: the `--confirm-dead` test read `world.tried`, which only the boot-scope world has, after its answer checks had passed. `e92324032` fixes it.
  - At `6d5438f36`, with the once-only control: 14 passed, 9 xfailed.

#### Holds

All ran under the suite lock, one pytest per hold, with no xdist. Times are UTC; load is the 1-minute average at admission and at the end.

| Hold | Admitted (load) → end (load) | Per file |
|---|---|---|
| Red | 23:58:44, slot A (33) → 23:58:57 (33) | `pr276_review_r1` 7 + 7 xfailed, at `3208ee4dd` |
| Green | 00:03:16, slot B → 00:03:54 (44) | `pr276_review_r1` 14 + 7 xfailed |
| C1 | 00:04:25, slot B (52) → 00:07:16 (44) | `boot_scope` 51, `terminal_containment` 17, `fix1` 21, `fix2b` 21, `properties` 1 |
| C2 | 00:11:01, slot B (21) → 00:13:03 (26) | `review_r3` 12 + 1 xfailed, `quar_start1_census` 21 + 17 xfailed, `quar_start1_landed` 16; xfailed: `fix2b_mutations` 8, `p3_mutations` 7 |
| F1 | 00:13:31, slot B (32) → 00:15:53 (30) | `kill4_terminal_daemon` 6 passed, 1 failed |
| F2 | 00:16:58, slot B (37) → 00:18:44 (24) | F1's failure, rerun: passed; `fake_daemon_parent_watch` 5 |
| G | 00:23:37, slot A (27) → 00:26:17 (57) | `state_contract` 37, `test_module_names` 1, `invariants_index` 25 |
| Green 2 | 00:28:30, slot B (23) → 00:29:37 (42) | `pr276_review_r1` 14 + 9 xfailed, at `6d5438f36` |
| M | 00:30:17, slot A (54) → 00:31:21 (55) | the 16 mutant controls of `pr276_review_r1` and `p3_mutations` run with `--runxfail`: each failed at its named assertion |

- **F1's failure:** `test_a_counted_loop_driver_is_contained_with_its_children[exit-receipt]` failed its evidence assertion: the driver's pid was not in `counted_identities`. The kept root has it in `owned_identities`: an inspection recorded it while it was still in the guardian's group, and none counted it. The kill protocol still signalled it, and the one counted pid, with `contained: True`. It is the shape of the merge round's `[walled]` failure, on the other ending. The test runs no `kill`, so `_may_signal` is not on its path, and it passed on the rerun at load 37.
- **`[stopped]` and `[stopped-short-delay]`** passed in F1 at load 32 to 30. Review r1 found `[stopped]` failing at release too, at load 20 to 26, on the 2 s setsid wait (`tests/fake/test_kill4_terminal_daemon.py:194`). That wait is not this PR's to change, and this round loosened no bound.

#### Mutants

| Mutant | Killing test | Result |
|---|---|---|
| p3-1-unverifiable-always (round 3) | case 3's sequence, `[legacy-attempt-boot]` | detected |
| p3-1-taint-ignored (round 3) | the tainted-walk test | detected |
| `_kill_attempt`'s handshake inlined, no landing record (review r1) | the last-read landing at the bound, `[guardian-only]`: no record | detected |
| p3-1-responsibility-dropped (review r1) | `[responsibility-member]` | detected |
| p3-1-never-ignored (review r1) | `[never-signalled-walk-member]` | detected |
| `>` for `>=` against D4's property (review r1's P3-3) | D4's extension property, at the bound | detected |
| p2-1-chain-dropped (new) | `[unidentified-chain-member]` | detected |
| p2-1-group-unchecked (new) | `[unidentified-leaderless-group-member]` | detected |
| start-landed-not-once (review r1's unrun one) | the landing `[after-the-census]` and the abandon's last read `[stranger-unread]` | detected by both |

C2 or Green 2 detected each as a strict xfail, and M showed each failing at its named assertion.

**Not run here:** `tests/frontend/test_core_live.py`, which cannot run on a lane; `tests/process`, which this round's brief did not list; and the rest of the suite, which CI runs.

### Fix round r2 (review r2 of #276)

Review r2 asked for changes at `4dd6165a4`: one P2 and two P3s. Its own commits (`bce4d2d2c`, `a310992c5`) added `tests/unit/test_pr276_review_r2.py`, with two strict xfails for the P2. This round builds the hub's decisions on `a310992c5`.

1. **P2-1: `_may_signal`'s unread set counts owned members.**
   - **The gap:** C-5.4 lets four sources attribute a member in any census: a marker, the parent chain, the recorded group under a verified leader, and an owned identity. Fix round r1's unread set named the first three. `_signal_reasons` reads an owned record before taint and before unverifiability. An owned member with no identity in an unverifiable census reached `_may_signal` only through the walk term, which drops it when a tainted census parent taints it by its link. A finished job with such a member answered `already finished` and left it running.
   - **The fix, review r2's:** every live pid whose `owned_identities` record is not on another boot than the attempt's joins the unread set when this census could not read its identity. The filter is `_signal_reasons`'s own (`Daemon._attempt_boot`, `procs.boot_identity.differ`). `_may_signal` still names no target, so this adds protocol runs only.
   - **Tests:** review r2's two strict xfails are plain cases. A third constructed case pins the boot filter: an owned record of another boot attributes nothing in any read, and the unread set does not count it.
2. **Mutants:** the owned term dropped fails the constructed case `[below-a-tainted-parent]` and the owned member's kill. Owned records counted whatever their boot fail `[owned-on-another-boot]`. Review r2's scratch mutant, never-signalled pids kept in the unread set, is now a control against `[unidentified-never-signalled-marker-member]`.
3. **D-KILL4-EVIDENCE1:** `test_a_counted_loop_driver_is_contained_with_its_children` accepts the driver's identity from `owned_identities` or `counted_identities`, and checks its start. The fake adapter's shell provider backgrounds `fakeprov --driver` in the guardian's group, and the driver stays there until its `os.setsid()` (`tests/bin/fakeprov:148`). An inspection in that window rightly records it as owned, and `_record_owned` never counts an owned identity again. The protocol still signalled and contained the driver in each recorded failure (`[walled]` in the merge round, `[exit-receipt]` in fix round r1 and review r2).
4. **Text:** C-5.7 and the "Changes in version 3" entry name the owned member. This report's unreadable-identity bullet says when taint drops a member with no identity, and the `_terminal_census` item names `--confirm-dead`'s wording.
5. **The unread rule's soundness, a property** (`test_pr276_review_r2.py`). Over constructed unverifiable censuses, whenever the verifiable read of the same members gives `_signal_reasons` a target, `_may_signal` returns True: `kill` never skips the protocol only because the census could not read a member's identity. The censuses have up to four members, any mix of the five sources, parent links and unread identities. Their owned and unverified records name the process there now, an earlier one at that pid, or (owned only) another boot. Dropping the chain, verified-group or owned term fails it. Without the explicit examples, random search finds the chain and verified-group mutants at every seed review r3 tried, but the owned one at 17 of 30 fixed seeds and not at the stripped test's own seed; the `[below-a-tainted-parent]` example pins it.
   - **The converse is no property:** a walk member with no identity counts, though its read may show it tainted by its own record. That errs toward running only.
   - **The probe's shrunk counterexample for the owned term:** an owned member that only the leaderless group names, with no identity. No real census has that shape: `procs.containment` seeds its walk with every recorded identity it has not proven recycled, so a live process its owned record names is always a walk member, and only taint drops it from the walk term, as review r2 found. A pid the census proves recycled is not, and this round's owned term still counted it with no identity (review r3's P3-1; fix round r3 leaves it out).
6. **Left for a follow-up:** review r2's observation that `_contain`'s `fresh` checks only `responsible_identities`. `_contain` is unchanged.

#### Red and green

- **Red** at `a310992c5`, with `--runxfail`: `test_pr276_review_r2.py` 6 passed, 3 failed. The failures were the two P2-1 cases (`already finished` for the owned member's kill; `_may_signal` False on the constructed census below a tainted parent), and the verified-group mutant control, which `--runxfail` fails by design.
- **Green** at `a11729342`, with the marks removed: 9 passed, 1 xfailed.

#### Holds

All ran under the suite lock, one pytest per hold, with no xdist. Times are UTC; load is the 1-minute average at admission and at the end.

| Hold | Admitted (load) → end (load) | Per file |
|---|---|---|
| Red | 03:43:00 (12) → 03:43:04 (12) | `pr276_review_r2` with `--runxfail`: 6 passed, 3 failed, at `a310992c5` |
| Green | 03:48:41 (8) → 03:48:47 (8) | `pr276_review_r2` 9 + 1 xfailed, at `a11729342` |
| H1 | 04:01:44, slot B (21) → 04:03:01 (22) | `pr276_review_r1` 14 + 9 xfailed, `pr276_review_r2` 9 + 5 xfailed, `quar_start1_landed` 16, `quar_start1_census` 21 + 17 xfailed, `review_r3` 12 + 1 xfailed, `p3_mutations` 7 xfailed |
| U1 | 04:03:59, slot B (24) → 04:06:42 (18) | `terminal_containment` 17, `fix1` 21, `fix2b` 21, `boot_scope` 51, `properties` 1; xfailed: `kill4_mutations` 8, `fix1_mutations` 6, `fix2b_mutations` 8, `boot_scope_mutations` 4 |
| F1a, F1a again | 04:07:51, slot B (13) → 04:09:03 (8); 04:09:48, slot B (7) → 04:11:05 (7) | `kill4_terminal_daemon` 7, then 7 |
| F1b to F1e | 04:12:10 to 04:14:02, slot B (9 to 18) | `quar_start1_daemon` 2, `fake_daemon_parent_watch` 5, `census_cwd_outcome` 6, `census_boot_seeds_daemon` 5 |
| P1 | 04:14:14, slot B (16) → 04:15:12 (12) | `kill4_fix1_process` 3, `quar_start1_process` 4 + 2 xfailed |
| M | 04:16:32, slot B (10) → 04:17:07 (15) | the 21 mutant controls of `pr276_review_r1`, `pr276_review_r2` and `p3_mutations` with `--runxfail`: each failed at its named assertion |
| Property | 04:19:54, slot B (21) → 04:20:17 (18) | `pr276_review_r2` 10 + 8 xfailed, and its 3 new controls with `--runxfail` at 04:20:30: each failed at the property's assertion |
| G | 04:23:05, slot B (20) → 04:23:57 (11) | `state_contract` 37, `test_module_names` 1, `invariants_index` 25; `pr276_review_r2` 10 + 8 xfailed, at `255d99f53` |

- **`kill4_terminal_daemon`** passed both runs at load 7 to 13. At that load the window before the driver's `setsid` is short, so these runs cannot show which record held the driver; the assertion now accepts either, and the protocol's own assertions are unchanged.

#### Mutants

| Mutant | Killing test | Result |
|---|---|---|
| p3-1-unverifiable-always (round 3) | case 3's sequence, `[legacy-attempt-boot]` (`test_kill4_boot_scope.py:276`) | detected |
| p3-1-taint-ignored (round 3) | the tainted-walk test (`test_kill4_boot_scope.py:344`) | detected |
| `_kill_attempt`'s handshake inlined (review r1) | the last-read landing, `[guardian-only]` (`test_pr276_review_r1.py:240`) | detected |
| p3-1-responsibility-dropped (review r1) | `[responsibility-member]` (`test_pr276_review_r1.py:187`) | detected |
| p3-1-never-ignored (review r1) | `[never-signalled-walk-member]` (`:187`) | detected |
| `>` for `>=` against D4's property (review r1's P3-3) | D4's extension property (`test_quar_start1_census.py:253`) | detected |
| p2-1-chain-dropped (fix round r1) | `[unidentified-chain-member]` (`test_pr276_review_r1.py:187`); the soundness property | detected by both |
| p2-1-group-unchecked (fix round r1) | `[unidentified-leaderless-group-member]` (`:187`) | detected |
| start-landed-not-once (review r1) | the landing `[after-the-census]` (`test_quar_start1_landed.py:102`) and the abandon's last read (`:255`) | detected by both |
| p2-1-group-dropped (review r2) | `[unidentified-verified-group-member]` (`test_pr276_review_r1.py:187`); the soundness property | detected by both |
| p2-1-never-kept (review r2's scratch mutant, now a control) | `[unidentified-never-signalled-marker-member]` (`:187`) | detected |
| p2-1-owned-dropped (this round) | the constructed `[below-a-tainted-parent]` (`test_pr276_review_r2.py:175`); the owned member's kill (`:140`); the soundness property | detected by all three |
| p2-1-owned-boot-unchecked (this round) | the constructed `[owned-on-another-boot]` (`test_pr276_review_r2.py:175`) | detected |

H1, the property hold and the final G hold ran each control as a strict xfail, and M and the property hold's `--runxfail` run showed each failing at its named assertion.

**Not run here:** `tests/frontend/test_core_live.py`, which cannot run on a lane, and the rest of the suite, which CI runs.

### Fix round r3 (review r3 of #276)

Review r3 approved `0eec8c92f` with four P3s, and found a P2 in release/217's `procs._parent_chain` that predates this PR. Its own commits (`73a816cb6`, `f7524e3fd`) added `tests/unit/test_pr276_review_r3.py`, with one strict xfail for P3-1 and `documented_limitation` tests for the P2. The hub merged `release/217` at `bc108d3ad` (#239) in `90b3c5029`, and this round builds the hub's decisions on it.

1. **P3-1: the owned term leaves out the census's proven-recycled pids.**
   - **The gap:** a stranger's member of a leaderless group at the recorded pgid holds a pid an inspection once recorded as owned for an earlier process, and the current boot cannot be read (case 3's `[unreadable-current-boot]`). The census puts the pid in `recycled` (its start differs from the owned record's) but cannot read its identity. Fix round r2's owned term counted it, so each `kill` of review r2's four-request sequence answered `containment requested` and ran a protocol that could signal nothing. Fix round r1's rule had answered `already finished`.
   - **The fix, review r3's:** the owned term takes the owned pids whose record is not on another boot than the attempt's, minus `census.recycled`. `_may_signal` still names no target.
   - **Why the subtraction is sound:** the owned source attributes only the process its record names, and a pid is in `Containment.recycled` only when the census proved the process there is not that one. `procs.containment` adds a pid there in three places:
     - `is_recycled`: every recorded birth at the pid, the primary record and each alternative, differs from the table's process by its start, or by its boot after C-5.12's fresh read. A comparison it cannot decide keeps the pid out. `boot_identity.matches` never returns False for a recorded legacy seconds value.
     - `exclude`: the attempt's boot UUID is over, by the same fresh read, so nothing alive now is of the attempt's boot.
     - The recorded group's absent leader, whose recorded boot is provably not this one (`group of another boot`).
   - **The owned record is always one of those births:** `_contain` passes every `owned_identities` record to the census. Each is the primary record at its pid, or an alternative where the guardian's own birth takes the pid. The census's attempt-boot filter drops only records on another boot than the attempt's, which `_may_signal` leaves out already, and `_signal_reasons` reads the same records (attempt rows have no `owned_identities` column, so both read `evidence_json`). A read at a recycled pid therefore captures another process than the owned record names. `_signal_reasons` attributes an owned member only when the read identity equals the record, so it never attributes that pid through the owned source. The marker, chain, verified-group and walk terms still count such a pid on their own, so the subtraction removes only protocol runs that no read could use.
   - **Tests:** review r3's strict xfail is a plain case, and its census case also asserts that `_may_signal` skips the protocol on that census. The soundness property on the daemon's own census generates owned records of an earlier process at the same pid, which the census can prove recycled, and still passes.
2. **Mutant:** the owned term keeping the recycled pids (`owned-recycled-kept`) fails both stranger cases: `_may_signal` on the census (`test_pr276_review_r3.py:170`) and the four-kill sequence (`:179`).
3. **Text:** review r3's P3-4 lines, in its words: this report's fix round r2 section (the unread-set bullet, the property's random-search result, the shrunk counterexample's walk claim, and the mutant table's line numbers), `_may_signal`'s docstring, C-5.7 with P3-1's fix built, `test_kill4_boot_scope.py`'s tainted-walk docstring, and a sentence in the "Changes in version 3" entry.
4. **Left for a follow-up:** review r3's P2 outside this PR. Release/217's parent-chain rule drops a recorded ancestor, or a held parent, when the table shows no start, and records no error. This round changes neither `procs.py` nor `_contain`, and review r3's `documented_limitation` tests record the gap.

#### Red and green

- **Red** at `90b3c5029`, with `--runxfail`: `test_pr276_review_r3.py` 22 passed, 6 failed. P3-1's case failed at its first assertion (each `kill` answered `containment requested`), and the other five were review r3's mutant controls, which `--runxfail` fails by design.
- **Green** at `c44b3e07d`, with the mark removed: 23 passed, 5 xfailed.

#### Holds

All ran under the suite lock, one pytest per hold, with no xdist. Times are UTC; load is the 1-minute average at admission and at the end. The briefed globs match five `test_kill4_*mutations.py` files, and all five ran.

| Hold | Admitted (load) → end (load) | Per file |
|---|---|---|
| Red | 10:50:52, slot B (20) → 10:51:24 (23) | `pr276_review_r3` with `--runxfail`: 22 passed, 6 failed, at `90b3c5029` |
| Green | 11:12:26, slot B (44) → 11:13:12 (39) | `pr276_review_r3` 23 + 5 xfailed, at `c44b3e07d` |
| R3M, M0 | 11:14:41, slot B (35) → 11:15:33 (31); 11:22:24, slot A (31) → 11:22:49 (25) | `pr276_review_r3` 23 + 7 xfailed with the new control; the control's two cases with `--runxfail` failed at `:170` and `:179` |
| H1 | 11:46:39, slot B (11) → 11:48:27 (12) | `pr276_review_r1` 14 + 9 xfailed, `pr276_review_r2` 10 + 8 xfailed, `pr276_review_r3` 23 + 7 xfailed, `quar_start1_census` 21 + 17 xfailed, `quar_start1_landed` 16, `census_parent_chain` 14, `census_boot_seeds` 36, `p3_mutations` 7 xfailed |
| U1 | 11:49:12, slot A (12) → 11:51:07 (25) | `boot_scope` 51, `terminal_containment` 17, `fix1` 21, `fix2b` 21; xfailed: `kill4_mutations` 8, `fix1_mutations` 6, `fix2b_mutations` 8, `boot_scope_mutations` 4 |
| F1 | 11:51:26, slot A (27) → 11:53:07 (30) | `kill4_terminal_daemon` 7 |
| P1 | 12:08:55, slot A (14) → 12:09:21 (17) | `kill4_fix1_process` 3 |
| G | 12:10:17, slot B (22) → 12:11:22 (25) | `state_contract` 37, `test_module_names` 1, `invariants_index` 25 |
| M | 12:19:12, slot B (19) → 12:20:03 (19) | the 26 mutant control cases of `p3_mutations`, `pr276_review_r1`, `pr276_review_r2` and `pr276_review_r3` with `--runxfail`: each failed at its named assertion |
| M2, U2 | 12:21:49, slot A (17) → 12:22:18 (15); 12:23:36, slot B (17) → 12:24:26 (15) | after `5a069ba37` kept the tainted-walk docstring at three lines: the two P3-1 controls failed at `boot_scope:276` and `:344`; `boot_scope` 51, `p3_mutations` 7 xfailed, `boot_scope_mutations` 4 xfailed |
| M3 | 12:28:39, slot A (22) → 12:28:55 (19) | review r1's constructed cases under `marker-dropped` (a scratch plugin): `[unidentified-marker-member]` failed at `test_pr276_review_r1.py:187`, the other 6 passed |
| Probe | 12:31:41, slot B (20) → 12:31:57 (17); 12:34:49, slot A (21) → 12:35:07 (19) | a scratch copy of review r3's daemon-census property with the current boot unreadable on the partial read, so a proven-recycled pid can be an unread owned member (seeds 0 and 1, 300 examples each): it never failed. A strategy with only launchd children in the leaderless group (300 examples) had the subtraction decide 5 examples, none with a target in the whole read |

#### Mutants

| Mutant | Killing test | Result |
|---|---|---|
| p3-1-unverifiable-always (round 3) | case 3's sequence, `[legacy-attempt-boot]` (`test_kill4_boot_scope.py:276`) | detected |
| p3-1-taint-ignored (round 3) | the tainted-walk test (`test_kill4_boot_scope.py:344`) | detected |
| `_kill_attempt`'s handshake inlined (review r1) | the last-read landing, `[guardian-only]` (`test_pr276_review_r1.py:240`) | detected |
| p3-1-responsibility-dropped (review r1) | `[responsibility-member]` (`test_pr276_review_r1.py:187`) | detected |
| p3-1-never-ignored (review r1) | `[never-signalled-walk-member]` (`:187`) | detected |
| `>` for `>=` against D4's property (review r1's P3-3) | D4's extension property (`test_quar_start1_census.py:253`) | detected |
| p2-1-chain-dropped (fix round r1) | `[unidentified-chain-member]` (`test_pr276_review_r1.py:187`); the soundness property (`test_pr276_review_r2.py:339`) | detected by both |
| p2-1-group-unchecked (fix round r1) | `[unidentified-leaderless-group-member]` (`test_pr276_review_r1.py:187`) | detected |
| start-landed-not-once (review r1) | the landing `[after-the-census]` (`test_quar_start1_landed.py:102`) and the abandon's last read (`:255`) | detected by both |
| p2-1-group-dropped (review r2) | `[unidentified-verified-group-member]` (`test_pr276_review_r1.py:187`); the soundness property (`test_pr276_review_r2.py:339`) | detected by both |
| p2-1-never-kept (review r2) | `[unidentified-never-signalled-marker-member]` (`test_pr276_review_r1.py:187`) | detected |
| p2-1-owned-dropped (fix round r2) | `[below-a-tainted-parent]` (`test_pr276_review_r2.py:175`); the owned member's kill (`:140`); the soundness property (`:339`); the property on the daemon's census (`test_pr276_review_r3.py:306`) | detected by all four |
| p2-1-owned-boot-unchecked (fix round r2) | `[owned-on-another-boot]` (`test_pr276_review_r2.py:175`) | detected |
| owned-boot-exact (review r3) | the owned filter's `[legacy-seconds]` and `[none]` records (`test_pr276_review_r3.py:105`) | detected by both |
| owned-not-live (review r3) | the exited owned record (`test_pr276_review_r3.py:135`) | detected |
| marker-dropped (review r3) | the property on the daemon's census (`test_pr276_review_r3.py:306`); `[unidentified-marker-member]` (`test_pr276_review_r1.py:187`, M3) | detected by both |
| owned-recycled-kept (this round) | the stranger's census (`test_pr276_review_r3.py:170`) and its four kills (`:179`) | detected by both |

H1, U1 and U2 ran each control as a strict xfail, and M, M0, M2 and M3 showed each failing at its named assertion.

**Not run here:** `tests/frontend/test_core_live.py`, which cannot run on a lane, and the rest of the suite, which CI runs.
