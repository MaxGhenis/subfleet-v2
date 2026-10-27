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
4. **Never signalled:** an operator's process in the worktree that is no ancestor of a marked member, a held parent, another user's process, the daemon, its parent, another attempt's guardian, and (fix round 1) a stranger in a leaderless group at the recorded pgid (property, regressions).
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

Totals: `380b626cf` 28 failed, 19 passed (the 22:29Z hold); `8fe3413ef` 37 failed, 10 passed (the 22:57Z hold); head 46 passed (hold H2, below). In each parent run a scratch-only check passed: `subfleet` was imported from that parent's tree.

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

`tests/unit/test_kill4_boot_scope_mutations.py` holds the brief's three mutants as strict xfails (each must fail its named regression with an `AssertionError`). The first is split into its read and write halves, so each half is shown detected on its own. All four are detected (in H1, 4 xfailed; H3 reruns the file at its committed text). r2's earlier mutants are detected too: the build's 8 and fix round 1's 6, 14 xfailed in H1.

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


**Not run in this round:**
- #259's fakes: `tests/fake/test_census_boot_seeds_daemon.py`, `test_salvage_adversarial.py`, `test_salvage_worktree_gone.py` (H3, queued)
- group 3's fakes: `tests/fake/test_kill4_terminal_daemon.py`, `test_census_escapers_daemon.py`, `test_census_recycled_pid_daemon.py`, `test_census_cwd_outcome.py`, `test_daemon_contract.py`
- the process tests: `tests/process/test_census_parent_chain_process.py`, `test_kill4_fix1_process.py`
- `tests/fake/test_state_contract.py`
- `tests/fake/test_census_self_exit_daemon.py` (round 2b's)
