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
| 1 | Start grace expired with members | quarantined, **no containment** | kill protocol (`recovery`); verified empty → `lost` (read-only retries); else quarantined |
| 2 | Start grace expired, census empty | `failed`/`interrupted`, retry | unchanged (cwd-only waits 30 s, #240) |
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
- **Direction.** Up the snapshot's parent links only, never down. A held parent seeds no walk, so an operator's other processes are never pulled in.
- **Qualification.** Live, pid > 1, not launchd, the daemon, its parent or another attempt's recorded guardian, of the daemon's effective uid by the kernel record (other users' processes are EPERM and read as gone), not exiting, and a canonical cwd (APFS case and `/private` spelling, as admission compares) equal to or under the job's own daemon-allocated worktree (`owned_worktree`, C-13.4: never an in-place workdir, never a probe).
- **Stop.** The first ancestor that does not qualify is held and nothing above it is examined. An operator's shell outside the worktree is never signalled, and neither is its parent.
- **cwd alone.** A cwd holder that is no ancestor of a marked member is neither added nor held. Cwd-only censuses keep #240's rule: they hold the worktree and never change a verdict.
- **Identity.** Every signal still re-checks pid, boot and start just before the syscall, so pid reuse can redirect one only within C-5.3's same-second limit.
- **Residual risk.** One shape is signalled by design: an operator process whose cwd is inside this job's own worktree and which is an ancestor of a process carrying this attempt's exact markers. Examples are an operator's shell in the worktree that ran a command with the attempt's environment, or a tmux server started in the worktree hosting the agent's pane. The hub accepted it.

## What remains open

- **Seen by no census.** A hidden-marker loop that leaves the group and the walk before any inspection counts it, and that has no marked child alive at any census, is seen by no source. This is D-KILL3's documented limitation, extended. A census landing between two children of such a loop reads verified empty; the fixture's 0.4 s children showed it, and a real loop's `pytest` child lives far longer.
- **In-place jobs.** A loop seen as the parent of a marked child is held, so the attempt quarantines, but it is never signalled: the operator-safety limit the hub set. The operator ends it and then runs `--confirm-dead`.
- **Unrooted daemons.** No responsibility source; decision 6 below.
- **Uncertain records fall back to quarantine.** A member first recorded by an unverifiable census, or below one, is never signalled.
- **Same-second pid reuse.** C-5.3's limit still applies to every identity check before a signal.
- **The accepted operator case.** An operator process whose cwd is in the job's own worktree and which is an ancestor of a marked member is signalled by design.
- **Probes and offline kills** keep their containment (group and owned only). They are not job attempts.

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

- **A stopped group member** received the group TERM and stayed stopped, with the TERM pending, through all of `term_grace_s` (15 s). The census stayed non-empty until the group KILL.
- **A stopped escaped member** was never signalled, so the attempt quarantined (cancel) or quarantined without any signal (finalization, start grace).
- **Now:** both get SIGCONT right after their TERM. The regressions end within the grace by TERM, not KILL (`test_a_stopped_group_member_is_continued_after_its_term_and_ends_within_the_grace`, `test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is`, and the real-daemon stopped-guardian case).

## Invariants (tested)

1. **No terminal state with live census pids.** An attempt that ends anything but `quarantined` has a verified-empty census at its end, cwd holders aside (property, regressions).
2. **Nothing outside the census is signalled.** Every pid signalled one by one was a live member of a census the protocol took; group signals go only to the recorded group (property).
3. **Continue follows TERM.** A process is continued only right after the protocol sent it TERM (property, regressions).
4. **Never signalled:** an operator's process in the worktree that is no ancestor of a marked member, a held parent, another user's process, the daemon, its parent, another attempt's guardian (property, regressions).
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
