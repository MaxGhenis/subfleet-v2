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
- **A stranger's leaderless group at the recorded pgid holds the attempt.** Since fix round 1 its members are never signalled, but they are census members: `kill` on the finished job answers `containment requested` and its protocol ends with them as survivors, and `--confirm-dead` refuses while they live. `--force-release` resolves it.

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
