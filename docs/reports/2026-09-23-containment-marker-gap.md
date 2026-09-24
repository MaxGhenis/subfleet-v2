# Containment marker gap, 2026-09-23

## What was observed

A process started with `SUBFLEET_ATTEMPT=worktree-add:probe` and
`SUBFLEET_ROOT=/probe` in its environment was found by
`procs.containment(None, None, None, "worktree-add:probe", root="/probe")` when
it was a uv or Homebrew Python. It was not found when it was `/bin/sleep 20` or
`/bin/sh -c 'sleep 20; :'`. `ps -E` printed the arguments of those two and no
environment. The same script on Darwin 25.6.0 (xnu-12377.161.14, macOS 26.6.2,
SIP on) also found `/usr/bin/python3` and `/usr/bin/git`. Those are
xcode-select shims that exec Xcode's binaries in the same pid.

## Why: `CS_RESTRICT` withholds the environment

`ps -E` reads `KERN_PROCARGS2`. In xnu-12377.121.6, `sysctl_procargsx`
(`bsd/kern/kern_sysctl.c:1486-1499`) keeps the environment only in four cases:
the target is the caller, the target is not `cs_restricted`,
`csr_check(CSR_ALLOW_UNRESTRICTED_DTRACE)` passes (SIP relaxed), or the caller
holds `com.apple.private.read-environment-variables`. Otherwise it cuts the copy
at the end of argv (`:1619-1645`), so argv still comes back. `cs_restricted(p)`
is the `CS_RESTRICT` bit of the live code-signing flags
(`bsd/kern/kern_cs.c:1739-1742`).

The bit is not in the binary's own signature. `codesign -dv /bin/sleep` shows
code-directory flags `0x0`, and the kernel's blob parsing sets only `CS_VALID`
from those (`bsd/kern/ubc_subr.c:4102`). A MAC policy gets a writable flags
pointer during exec (`ubc_subr.c:4861-4886`, `mac_vnode_check_signature`), and
the kernel commits the result (`bsd/kern/kern_exec.c:2166-2172`, `:8580`). The
policy that sets it for Apple's binaries, presumably AMFI, is closed source.

`tools/marker_visibility.py` spawns each executable suspended with a marker in
its environment. It reads the live flags with `csops(CS_OPS_STATUS)` and asks
`ps -E` for the marker. On 2026-09-24, in every case, the environment was hidden
exactly when the process was `CS_RESTRICT`:

| Run | Result |
|---|---|
| `--dir /bin` | 36 of 36 measured restricted (`/bin/ps` skipped, setuid) |
| default list, Apple executables | all restricted: the common `/usr/bin` tools (`env`, `tail`, `head`, `grep`, `sed`, `awk`, `find`, `xargs`, `tee`, `sort`, `perl`, `ruby`, `ssh`, `curl`, `rsync`, `tar`, `nohup`, `caffeinate`, `osascript`, `sandbox-exec`) and the `git`, `python3`, `make`, `clang` shims before they exec |
| default list, readable | Homebrew `git`, `node`, `uv`, `rg`; `bun`; uv's CPython; Claude Code 2.1.280; the Xcode `git`, `python3`, `make`, `clang` that the shims exec |

The `codex` on PATH is a `#!/usr/bin/env node` script. It measures as `env`
(restricted) at spawn and becomes node, which is readable, once env execs it.
The native codex binary it launches
(`@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`, measured
with the tool's `measure()`) is not restricted, and its marker is visible.
Hardened runtime alone does not hide the environment: an ad hoc copy of
`/bin/sleep` signed `-o runtime` was readable, and one signed `-o restrict` was
not (2026-09-23).

A one-off sweep on 2026-09-23 found every Apple executable it ran restricted
across `/usr/bin`, `/usr/sbin` and the top level of `/usr/libexec`: Mach-O and
scripts, flags read at spawn, shims before they exec. Its raw output was not
retained. It also spawned launch-constrained programs that the kernel killed at
exec, which left crash reports. The committed tool therefore sweeps only what it
is asked to.

What attempts run is mostly restricted:

- On this Mac, Claude Code runs each Bash tool command as `/bin/zsh`, a session
  leader in a process group of its own. The tool shell that ran these checks
  shows stat `Ss`, pgid equal to its own pid, and a parent `claude` in another
  group.
- git runs a `#!/bin/sh` post-checkout hook as a restricted `/bin/sh`, and a
  smudge filter with shell metacharacters under a restricted `/bin/sh -c`.
  Re-checked 2026-09-24 with Homebrew git 2.55.0 and Xcode git: all of them sit
  in the add's process group, and git itself is readable.

## Is there another way to read their environment as the same user?

No, not while SIP is on, without root or an Apple entitlement. Tried on
2026-09-23 from an unentitled, ad hoc signed Python:

| Route | Result |
|---|---|
| `KERN_PROCARGS2` via `sysctl` directly | argv and exec path, 0 environment strings (same as `ps`) |
| `task_for_pid` | `KERN_FAILURE` (5), also for an unrestricted Homebrew Python |
| `task_read_for_pid`, `task_inspect_for_pid` | `EPERM` |
| `task_name_for_pid` | succeeds; a name port reads no memory (audit token only) |
| any `proc_info` flavor | none reads the argument area (source grep of `bsd/kern/proc_info.c`) |
| DTrace, `/dev/auditpipe`, `launchctl procinfo` | need root |

An unprivileged process can still read a lot about a restricted process of the
same user, because `proc_info` checks a MAC hook and the uid and never `csflags`.
That covers its parent, group, state, open files, and cwd, and the unique-id
flavors described below.

## Where subfleet relied on the marker alone

On `origin/main` no attempt path relies on the marker alone. The daemon records
the guardian's pid and group before it opens the launch gate (attempt
`starting`, probes, enrollment). So every census has the group and a walk root,
and a gate that closes on a daemon crash spawns nothing.

A restricted writer was still lost whenever it left the group and its parent
then exited, because the census kept nothing it had seen. Reproduced through the
fake-daemon harness with a provider that starts `/bin/sh -c 'exec /bin/sleep …'`
in a new session:

- **Kill** (`_kill_attempt`, from a cancel or `max_wall_s`): the attempt ended
  `interrupted` and the job `cancelled` (rc 130), and every lease was released
  while the sleep ran. The Python escapee in the same run was quarantined, as
  the release gate expects.
- **Exit 0** (`_finalize`): the job was accepted `succeeded` and every lease was
  released while the sleep ran.
- **Same sources elsewhere:** a guardian lost without a receipt (`_lost`),
  `kill --confirm-dead`, and probes and enrollment turns all use the same three
  sources.

The release gate "Zero workspace reuse after an unverified termination" was
green only because its fixture's escaped writer is a Python process.

Claude Code makes this ordinary rather than rare. Its tool shells are session
leaders outside the attempt's group, so a SIGTERM to the group does not reach
them. When `claude` dies they have no walk root either. They are restricted, so
the marker never sees them.

The unmerged lane `fix/worktree-add-timeout-orphans` (C-6.8) adds the one census
that relies on the marker alone. `Daemon._await_worktree_add` calls
`procs.containment(None, None, None, "worktree-add:<job>", root=…)` before it
reuses or removes `worktrees/<job id>/`. The add's group id lives only in the
in-memory `Popen`, so it is gone at the next pass, not only after a restart.
Replaying the lane's own hook test at its commit 35f2fee:

- Pass N deferred on the hook's `/bin/sh` writer.
- Pass N+1, with that writer still alive and appending into the worktree, set
  the job `running` and reserved an attempt there.
- A Python writer kept the job waiting.

## The fix: keep what the census has seen

The census already ran every 0.5 s while an attempt's guardian lived. It
recorded `owned_identities` for group members, but only as signal authority
(C-5.6), and those were never a census source. Now:

- **A fourth source.** `procs.containment` counts the processes an earlier census
  kept (`recorded`) that are still the same process. Each is checked against the
  one process-table snapshot, which now carries `lstart`, so the check costs no
  `ps` call. A reused pid, a zombie, an absent pid, or a different boot-session
  UUID counts as dead. A legacy boot timestamp that differs is unknown and makes
  the census unverifiable (C-5.3). A match is reported under the current boot id.
  So an identity kept under `boot_id()`'s `kern.boottime` fallback heals the next
  time it is kept.
- **Groups of kept escapees.** A kept process found outside the attempt's
  group leading its own group is noted in `census_leaders`. The members of that
  group count, also after the leader has exited, for as long as the group has
  members. That covers a shell's background job, or the group of a setsid'd
  helper. xnu allocates no pid that is still a group's id
  (`bsd/kern/kern_fork.c:955-973`), so while the group lives it is still the
  kept process's. Once it is empty the id may be reused, and it stops counting.
  It also does not count when the pid now belongs to a process with another
  start time, or when the kept process was recorded on another boot. An exited
  leader whose boot cannot be told, while its group has members, makes the
  census unverifiable. The live descendants of kept processes and of these group
  members count too.
- **Which censuses keep.** When the census's own snapshot shows the recorded
  leader (`leader_verified`), or the leader is still alive afterwards, a census
  keeps every pid it found: group members as `owned_identities` (signal
  authority, only ever added to, as before) and every other pid as
  `census_identities`. Without a verified leader it keeps only what is the
  attempt's regardless: marker matches and kept processes. These all keep: the
  0.5 s census, each census of a kill (first, grace, after SIGKILL, settle), each
  census of finalization that finds something, `kill --confirm-dead`, and the
  probe and enrollment censuses. The kept identities live in the attempt's
  evidence or the probe's record, so they survive a restart.
- **Writes.** A census writes only when it finds a process, or a reused pid, that
  is not kept yet. A kept process exiting costs no write. A write by a census that
  read everything drops `census_identities` entries that are neither alive nor
  the exited leader of a group that still has members.
  Worst-case measurement: a session-leader shell started a new process every
  0.35 s for 16 s, and the kept set was written in 19 of 32 censuses.
  `origin/main` wrote once in the same run. Each write is one small
  `evidence_json` update (399 bytes at the end of that run) plus one
  `attempt.processes_recorded` event. Nothing deletes from `events`; that is not
  new with this change.
- **Evidence, not authority.** Kept identities are evidence for quarantine, never
  authority to signal. Only group members observed under a verified leader are
  signalled, as before.

## Tests

Red before the fix, green after:

- `tests/fake/test_daemon_contract.py`, all four failing with the three sources
  of `origin/main`:
  - `::test_c5_5_restricted_setsid_writer_quarantines_the_kill` (the kill
    reproduction; pins the kill path's own reason);
  - `::test_c5_5_restricted_writer_left_at_exit_quarantines_instead_of_succeeding`
    (the exit reproduction);
  - `::test_c5_5_restricted_writer_started_during_the_kill_grace_quarantines`
    (the writer starts only once the group has had SIGTERM);
  - `::test_c5_5_background_writer_a_kept_shell_leaves_after_exit_quarantines`
    (a kept shell backgrounds the writer after the receipt and exits at once, so
    no census sees the writer with a parent; this one needs the group rule).

  The exit test waits until the daemon has kept the writer before letting the
  provider exit. On old code that wait never ends, so the `succeeded` outcome was
  reproduced separately with a fixed wait. The platform writers are started by an
  intermediate that has already called setsid. A census therefore never catches
  a writer inside the provider's group between its fork and its setsid, which
  would make it a group member that a kill may signal (C-5.6). Review saw that
  flake twice in about 25 runs under load.
- `tests/process/test_guardian_process.py`:
  - `::test_recorded_identity_keeps_a_restricted_setsid_orphan`: group, walk, and
    marker all come back empty while a real orphaned `/bin/sleep` runs, and its
    kept identity finds it.
  - `::test_kept_group_outlives_its_leader_across_a_write`: a kept shell exits;
    its group member starts a new process, so the next census writes; then the
    member double-forks a `/bin/sleep` and exits. Only the exited shell's group
    ties the sleep to the attempt, and it must still be kept after that write.
- `tests/unit/test_procs.py`:
  - the kept-identity rules (reused pid, zombie, exited process, other boot,
    unknown boot);
  - the walk below a kept process, and the group a kept escapee leads (exited,
    zombie, reused pid, other boot, emptied, unknown boot with members);
  - whether the census's own snapshot shows the leader;
  - boot healing, and no extra `ps` calls.
- `tests/unit/test_daemon_settle.py`:
  - what a census keeps with and without the leader, and that an exit alone
    writes nothing;
  - that a write keeps an exited leader while its group lives, and that only
    kept group leaders have their groups counted;
  - that finalization quarantines on a kept writer;
  - that the probe watch keeps and saves, and probe containment quarantines, on
    a kept writer;
  - that `_contain` reads the kept identities fresh, and that attempt and probe
    censuses are given them.

  Review's mutants for the probe keeping and the fresh read now fail these.

These document the host and pass before and after the fix:

- `test_marker_matches_only_environments_the_kernel_returns`: the marker is
  found if and only if the kernel returns the environment.
- `test_marker_only_census_misses_a_restricted_group_member`: the worktree-add
  shape; only the recorded group sees the writer.
- `tests/unit/test_procs.py::test_marker_row_without_an_environment_never_matches`.

`test_writer_orphaned_before_any_census_is_invisible` asserts the remaining
limit. The census comes back verified empty, and not unverifiable, while the
writer runs. It fails the day a source starts seeing that writer.

Whether the kernel hides a restricted environment depends on the host: SIP
relaxed for DTrace, or a development kernel, returns it. `tests/restricted.py`
asks once per session. It uses a Python control to prove the census works, and
the process tests assert the outcome for the host they run on. The process tests
also scope every census to their own state root, so concurrent runs on one
machine no longer see each other's processes.

Review on 2026-09-24 took two rounds, each finding checked by a skeptic.

Round 1 (four reviewers) found:

- the grace-window and post-leader cases, from when only the 0.5 s census and a
  kill's first census kept;
- the write rate, the boot-id fallback, and a pid owned before its setsid never
  being kept as an escapee;
- the SIP and cross-run assumptions in the tests, the fixture's fork-to-setsid
  flake, and the untested probe keeping;
- several overstated claims in the first version of this report.

Round 2 found:

- a write dropping an exited leader whose group still had members;
- an exited leader of unknown boot being skipped silently instead of making the
  census unverifiable;
- every dead kept entry being taken for a possible group leader, which pid reuse
  could turn into a false quarantine;
- the leader being checked only after the census. It is now read from the
  census's own snapshot (`leader_verified`).

All are fixed above.

## What stays invisible, and the options that were measured

Still invisible: a restricted process, or one whose environment was cleared,
that is outside the attempt's group and every kept group, has no live kept
ancestor and no live chain to a walk root, and was never seen by a census.
Examples are a double fork whose intermediate no census saw, and a provider that
starts a detached writer and exits within one census interval. C-5.5 says so.
Options measured on 2026-09-23 (raw logs not retained unless a re-check is
noted):

- **Inherited `flock` canary.** The guardian takes a shared lock on a file and
  passes the fd. The check takes microseconds, the lock is released within
  milliseconds of SIGKILL, and a zombie does not hold it (re-checked 2026-09-24:
  24 µs median, 0.4 ms). It caught escapes that keep the fd, including ones no
  C-5.5 source sees: setsid plus exec of `/bin/sleep`, a double fork,
  `bash -c 'set -m; …&'`, Rust `process_group(0)`, Go `Setsid`, and a setsid
  hook under both gits. But the real providers drop it (re-checked 2026-09-24):
  - node marks inherited fds close-on-exec at startup, and libuv spawns with
    `POSIX_SPAWN_CLOEXEC_DEFAULT`;
  - Bun's children do not get it, and Claude Code 2.1.280 is Bun-compiled;
  - the `codex` on PATH is a node launcher;
  - Python's default `close_fds`, tmux panes and screen also drop it.

  It detects nothing below a provider. It would work for the daemon's own
  `git worktree add`.
- **Quarantine responsibility.** This is private libquarantine SPI: spawn the
  guardian with `responsibility_spawnattrs_setdisclaim`, then read
  `responsibility_get_uniqueid_responsible_for_pid`. It survived setsid, a double
  fork and exec, and found every escapee while the anchor lived. After the anchor
  exits, the lookup falls back to the process itself; the anchor is the
  guardian, which exits right after the receipt. A descendant that disclaims
  hides its subtree. Moving TCC attribution to the guardian's binary is
  untested.
- **Resource coalition.** launchd gives the daemon its own coalition. Every
  descendant stayed in it through setsid, reparenting, exec, closed fds and
  cleared environments. But it is per daemon, not per attempt. Attributing a
  member to one attempt would mean accounting for the daemon's own helpers, and
  one escapee would quarantine every overlapping attempt. The flavor that reads
  it (`PROC_PIDCOALITIONINFO`, 20) is only in the private header, and a daemon
  run by pytest shares Claude.app's coalition.
- **Unique-id lineage.** `p_puniqueid` and `p_orig_ppidversion`, via
  `PROC_PIDUNIQIDENTIFIERINFO` (17, private header), survive reparenting.
  `p_orig_ppidversion` also survives exec, and links one hop to the parent's
  `p_idversion` as it was at the fork (a process's `p_idversion` changes when it
  execs). A census that recorded each kept process's `p_idversion` could match
  its children by `p_orig_ppidversion` after the parent has died. That would
  cover both examples of the stated limit where the parent was kept: a kept
  process whose child calls setsid and outlives it unseen, and a provider (kept
  while it ran) that starts a detached writer and exits within one census
  interval. Reviewers observed the link on 2026-09-24 for a restricted parent's
  setsid'd child. Not adopted here, because it rests on a private struct layout;
  it is the next increment if C-5.5's limit proves too wide.
- **Unavailable:** audit sessions (`setaudit_addr` is root-only), new coalitions
  (`EPERM`), personas (root-only for another pid), and kqueue (`NOTE_TRACK` is
  `ENOTSUP`, and `NOTE_FORK` carries no child pid).

## For the worktree-add lane (C-6.8)

`_await_worktree_add` needs the add's group, not the marker alone:

- Record the add leader's pid and C-5.3 identity durably (an event on the job)
  before waiting on it.
- Pass the pgid to every later census, including after a restart. A pid is not
  reused while a group of that id has members, so the recorded group stays the
  add's for as long as any of it lives.
- Optionally, because the daemon spawns git itself, an inherited `flock` canary
  also covers a hook that daemonizes a restricted writer out of the group
  (measured above).
