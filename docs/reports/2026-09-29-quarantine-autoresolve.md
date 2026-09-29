# Quarantines that never end, and the tool processes that escape a kill (2026-09-29)

## What was seen

1. **Quarantines never resolve on their own.** On 2026-09-29 at about 14:30Z, `~/.subfleet/state.sqlite3` held 78 attempts in state `quarantined`:
   - 54 were `writers remain after exit receipt`, 20 `termination could not verify containment`, and 4 `start grace expired without a receipt`.
   - Every one still held its leases: 78 `out:` and 43 `worktree:`. The oldest dated from 2026-09-22T17:55Z.
   - Only 3 still had a live process: a member of the recorded group, or a recorded identity alive with its recorded start.

   On 9/28 and 9/29, turns that shared a scratch workspace sat `waiting` with hold `lease-held` behind dead quarantined turns for hours. Examples are `turn-cv-1790623627433-144725096286` and `turn-cv-1790623873056-979b4d57c8ac`. They moved only when someone ran `subfleet kill <job> --confirm-dead`.

   Nothing re-censuses a quarantined attempt. `_quarantine` (daemon.py) makes it terminal. After that, the control loop offers only live attempts (`LIVE_ATTEMPTS`), and `_resolve_quarantine` runs only on an operator's `kill --confirm-dead` or `--force-release`. C-5.6's `kill_settle_s` and C-5.9's `exit_settle_s` end early on the first verified-empty census, but only inside their 3 s windows.

2. **Tool processes escape the recorded group.** Examples:
   - **fix2-microcosm-888** (job 20260928-231438-fix2-microcosm-888-888, attempt a2 on codex-5, guardian and pgid 10490). The wall limit killed it at 09:14:45Z. Its quarantine evidence lists three live pids, all found by the environment marker alone: `uv run pytest` 47229 (ppid 46802), python 47238 and python 43729. All three are in process group 46802. The recorded group and the walk from the guardian were empty. The attempt's `owned_identities` held ten pids of group 10490 and none of group 46802. The pytest tree ran until a session killed it by pid at about 09:33Z.
   - **pr162-fix3 and m1-rework** (jobs 20260929-081036-pr162-fix3 and 20260929-085816-m1-rework, both on codex-6). An operator killed both at 14:07Z and both were quarantined `termination could not verify containment`. The survivors were bun and node builds: pids 34434, 36727, 34829, 10335, 24478 and 29637. The census shows each as a session leader (`Rs`/`Ss`, pgid equal to its pid) with ppid 1, except 10335, which is in 34829's group. None was in the attempt's owned record, and a session stopped them by pid.

## Why they escape

The process table of every live attempt on 2026-09-29 (read-only; `ps -axo pid,ppid,pgid,stat`) shows that both providers run tool commands in new sessions:

- **Claude Code** starts every Bash command as `/bin/zsh`, a session leader (`Ss`, pgid equal to its own pid) whose parent is `claude`. The command's processes (`uv`, `python`, `cargo`, `rustc`, `gh`, `git`) are in zsh's group.
- **Codex** starts its tool commands and `codex-code-mode-host` as session leaders whose parent is `codex`.

So the recorded group holds only the guardian, the provider and whatever the provider runs without a new session. The kill protocol signals that group. It signals individually only the members recorded as owned, and those are group members recorded while the guardian led the group (C-5.6, C-5.12). When the provider dies, a tool session's processes are reparented to launchd (ppid 1). From then on only the environment marker can find them, and nothing is allowed to signal them.

The marker cannot see every process. `ps -E` reads `KERN_PROCARGS2`, and XNU's `sysctl_procargsx` (`bsd/kern/kern_sysctl.c`) omits the environment of a code-signing-restricted process (`cs_restricted`) unless SIP is off or the caller holds an entitlement. Apple's own executables are restricted, `/bin/zsh` and `/bin/sleep` among them. The earlier, unmerged branch `fix/containment-marker-gap` (f715cb14, 2026-09-24) found the same thing. For each of the 40 `/bin/zsh` processes checked, `ps -E -ww -p <pid>` printed none of `HOME`, `PATH`, `USER`, `SHELL`, `TMPDIR` or `SUBFLEET_ATTEMPT`. Their children (`uv`, `python`) do show the marker. So a zsh left behind is invisible to all three census sources once its parent dies.

## The change

The design was reviewed before it was built (an Opus 5.5 lane, `20260929-101509-qar-design-review`, 17 findings). This section describes the change as built, with those findings folded in. The kernel facts it rests on were read in XNU's source (`apple-oss-distributions/xnu`, 2026-09-29):
- `bsd/kern/kern_fork.c`'s pid allocation loop skips any pid that is held by a process (a zombie included), by a process group, or by a session.
- `kern_exit.c` reparents an exiting process's children to initproc (launchd).
- `mach_process.c` reparents a `ptrace` target to its tracer.
- `setpgid` in `kern_prot.c` takes only the caller or a descendant, in the caller's session.

### Ownership follows ancestry (C-5.4, C-5.6, C-5.12)

A process is **owned** when a process table shows it live and at least one of these holds:
- It is the recorded guardian, alive (C-5.3) and leading the recorded group.
- It is an owned process, shown with its recorded identity.
- Its parent in the table is owned, it is not being traced (`ps` marks a traced process `X`), and it started no earlier than that parent.
- It is a member of a process group an owned process leads.

A group lies in one session, and only the session's own forks can join it. The guardian calls `setsid`, so its session, and every session one of its descendants creates, holds only the attempt's processes. A stranger that a job's debugger attached to is listed under its tracer, but it is marked `X`, so it is never owned through that link.

The daemon keeps each running attempt's owned record in memory, together with the groups owned processes led. It prunes a dead process, or an ended group, only with a table whose read began after the last table that added to the record ended. It writes the record to the attempt's evidence at most every 30 s (`OWNED_PERSIST_S`). The kill protocol and a quarantine write it whole, at once. Writing every gain would cost too much: measured 2026-09-29, 34 running attempts gained an owned process 433 times a minute, and each write is a transaction, an `events` row and a waiter wake. No owned set held more than 18 processes.

The kill protocol records the owned set from a fresh table before it signals anything. It then SIGTERMs:
- the recorded group;
- every group an owned process leads;
- singly, every owned process in none of those groups.

Before it SIGKILLs those groups, it proves the set again. After the census it SIGKILLs owned survivors one by one. Every signal re-checks the identity of its target (C-5.4). A process that left the tree and every owned group before any table recorded it is still never signalled; it quarantines the attempt, as before. The `nested-setsid` fixture makes exactly that case: its intermediate process now `os._exit`s the instant after its fork.

### The census counts what the attempt recorded (C-5.5)

Recorded identities join the walk (when live with their recorded start), and the groups they lead join the group source. Recorded groups keep counting after their leader has gone, unless another process holds the group's id and leads it.

The walk no longer starts at a pid it cannot vouch for. It starts at the guardian only while the table shows it with its recorded start, and never at a receipt's child pid. After a receipt both have exited, and their pids could belong to strangers.

A census reports each recorded group it finds no process in (zombies count as members). Such a group can never be the attempt's again.

What no census can find: a process that left the attempt's tree before any table recorded it, belongs to no recorded group, and whose environment `ps -E` does not show. Finalization (C-5.9) has always released past such a process, and the recheck releases on the same census.

### Quarantines are re-censused (C-5.7b, new)

Every `quarantine_recheck_s` (60 s), one sweep reads one process table and one environment scan, and uses them for every quarantined attempt. If nothing is quarantined, the sweep starts no process.

The attempts that look empty are censused again from one fresh pair of reads before any is released. A sweep therefore starts at most four `ps`, however many attempts it releases.

The census roots:
- the guardian;
- the owned processes and groups;
- every identity any census of this quarantine has found live.

Groups a census has seen end are left out. An attempt from another boot session has nothing left alive, so for it only the marker is looked for.

A release runs `--confirm-dead`'s steps: first a turn's end snapshot or a writable job's salvage, then `quarantine.auto_resolved`, which releases the leases and ends the attempt `lost` or `interrupted`. The job's state, rc and notice are untouched. A salvage that fails keeps the quarantine.

A failing recheck backs off, doubling to one hour. It is logged by type on the 1st, 2nd, 4th and later powers of two. On the third failure in a row it is written into the evidence (`recheck_error`) and sent once to the operator as a service notice naming `--force-release`.

A live quarantine's evidence only grows (`recorded`, `groups_ended`), and it is written on the row as read inside the writing transaction. So no census, however old its table, drops what a newer census wrote, and no operator's hand edit is lost.

An operator's resolution and the sweep share one lock. A late operator request is recorded, with its note (`quarantine.request_after_release`).

### A resolved quarantine unblocks its conversation (C-24.5)

`_previous_released` sets `blocked_by='quarantined-turn'` when a conversation's previous turn attempt is quarantined. Nothing cleared it:
- `conversation.unblock` refuses it.
- `next_dispatchable` skips a blocked conversation.
- A re-admitted message's job is held `conversation-blocked`.

So the conversation stayed blocked for good, even after an operator resolved the quarantine. The conversation tick now lifts `quarantined-turn`, and only that block (a compare-and-set), once no attempt of the conversation's turns is quarantined. It looks when a quarantined turn is released, and otherwise every 60 s.

## Invariants and their tests

1. **Lease safety and liveness** (`tests/fake/test_quarantine_recheck_state.py`, a Hypothesis property over random process tables, checked against an independent oracle). A quarantined attempt's leases are released **exactly** when all of these hold:
   - no recorded identity is live with its recorded start;
   - no process carries the attempt's marker;
   - the recorded group has no live member, unless it has ended or another process leads it;
   - every read worked.

   In particular, a lease is never released while any recorded identity is alive. A pid reused under another start counts as gone.
2. **Signal authority** (`tests/unit/test_ownership_properties.py`). This runs a model of `fork`, `setsid`, `setpgid`, exit and orphaning, reaping, `ptrace` attach and pid reuse under XNU's allocation rule.
   - `owned_closure` never proves a stranger owned.
   - It returns exactly what a slow reference fixpoint of C-5.6's rules returns.
   - While the guardian's chain is intact, it owns every process the guardian's tree forked.

   What these tests prove is that the code matches the rules over any table. That the rules are sound on macOS rests on the kernel facts read above.
3. **Differential.** A census from a sweep's shared reads equals a census from its own reads of the same table (Hypothesis).
4. **One resolution.** An operator's `--confirm-dead` racing the sweep resolves the attempt once. Resolution changes neither the job's state, its rc, nor its notice.
5. **Bounded cost.**
   - A sweep with nothing quarantined starts no process.
   - A sweep with something quarantined starts at most four `ps`.
   - A live quarantine's evidence is written only when a census adds to it.
   - The owned record is written at most every `OWNED_PERSIST_S`.
6. **Real processes** (`tests/fake/test_quarantine_recheck.py`, a `/bin/sleep` in a session of its own; its environment is hidden from `ps -E`):
   - A kill ends it with the attempt, and the attempt is not quarantined.
   - Outliving its provider, it holds a quarantine that auto-resolves once it exits.
   - A recorded identity alone holds a quarantine; the same pid under another start does not.

## Not in this change

- **Probes** (`_contain_probe`) keep group-only ownership and their own recheck (C-5.7a, which another session is porting to this line).
- **Leftovers after a normal exit are not killed.** A provably owned process left running after the provider exits (a dev server, a watcher) is quarantined (C-5.9) and holds the leases until it exits. Killing such leftovers would change what a job may leave behind, which matters for conversation turns that start servers. That is a separate decision.
