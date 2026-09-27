# The daemon's scheduling, 2026-09-27

The installed daemon ran under launchd's `ProcessType` `Standard`. On this Mac (Apple M5 Max, 18 cores, macOS 26.6.2) that clamps all of its threads, about 150 of them, and every process it starts, to the `utility` QoS (priority 20). A shell's processes run at 31, and so does most of the machine's load. At 15:10 EDT, 57 `claude`, 50 `node` and some 160 Python processes ran at 31. At 15:40, 69 processes of the Claude app's bundled Claude Code (`~/Library/Application Support/Claude/claude-code/<version>/claude.app`) ran at 31. Those counts were read with `ps` and not saved to a file. Under that load a `utility` daemon waits behind all of them.

This report measures what that costs, then describes the change (C-5.1):

- the daemon runs at the default QoS (`ProcessType` `Interactive`);
- the guardian starts every provider clamped to `utility`, and the daemon's `git` commands that run repository code are clamped the same way, so agent work stays where it was, below the operator's apps.

The first version (885142a5, in release 2.1.8) clamped providers through `taskpolicy`. An independent review (Astra, below) found problems with that, and this revision spawns them with `posix_spawn`'s QoS attribute instead.

**What 2.1.8 showed live.** 2.1.8 was installed at 21:40Z with `Interactive`. The transition session then measured at load 115 to 180:
- the session mirror's full pass took about 80 s for 248,556 entries, where it had crawled at about one entry a second;
- `ping` answered in 0.0 s;
- smoke turns were placed within 3 s.
Those are its observations, not this report's runs.

**Where the numbers come from.** Every other number below comes from a command run on 2026-09-27 on this machine, and the raw output is under `docs/reports/2026-09-27-daemon-qos/`. No measurement touched the installed daemon: each run started its own fake-provider daemon or census reader, and the live daemon was only read with `ps -M` and `launchctl print`. Observations that were not saved to a file are marked as such where they appear.

## What each scheduling gives a process and its children

`mechanism_probe.py` (in the data directory) runs in a fresh interpreter per variant. It reports `ps -M`'s priority for:
- itself;
- a new thread;
- children started by fork+exec (`close_fds=True`), by `posix_spawn` (`close_fds=False`) and by `taskpolicy -c utility`.

It then tries to raise its own thread to `user-initiated`. It was run from a shell, and as a temporary launchd job for each `ProcessType` under other labels (`com.subfleet.qos-probe.*`), each booted out afterwards. The version that wrote `mech-*.json` read every numeric token of `ps -M`, so each array there starts with the pid and the priorities follow. The parser is now fixed.

| Context | Itself | Raised to user-initiated | Children (fork+exec, posix_spawn) | Child under `taskpolicy -c utility` | After its own thread set `utility` | After `nice 11` |
|---|---|---|---|---|---|---|
| Shell | 31 | 31 | 31, 31 | 20 | itself 20 (15 while busy), children 31 | itself 31, children 31 |
| launchd `Standard` | 20 | 20 | 20, 20 | 20 | 20, children 20 | 20, 20 |
| launchd `Interactive` | 31 | 37 | 31, 31 | 20 | itself 20, children 31 | 31, 31 |
| launchd `Adaptive` | 4 | 4 | 4, 4 | 4 | 4 | 4 |
| launchd `Background` | wrote nothing within the 3 minutes it was given, at load ~120; its error log was empty | | | | | |

What it shows:

- **`Standard` is a clamp.** No thread in the job, and no child, can rise above `utility` by asking for a QoS class. The daemon cannot fix its own scheduling from inside.
- **`Adaptive` puts the job in darwinbg** (priority 4). launchd.plist(5) says Adaptive jobs "move between the Background and Interactive classifications based on activity over XPC connections". The daemon's clients use a Unix socket.
- **`Interactive` runs at the default QoS.** launchd.plist(5) gives such jobs "the same resource limitations as apps, that is to say, none", and says to use the key only when an app's responsiveness depends on the job. The Subfleet app's does. `launchctl print` shows `spawn type = interactive (4)` for it and `daemon (3)` for `Standard`; the jetsam priority is 40 for both.
- **Only a clamp reaches a child that execs.**
  - A thread's own QoS (`pthread_set_qos_class_self_np`) changes that thread alone. A new Python thread, and every child started by fork+exec or `posix_spawn`, still runs at 31.
  - A child forked without an exec keeps its parent thread's requested class. The review measured a requested 33 with the effective priority unchanged. No provider is started that way.
  - `nice` changes nothing that `ps -M` shows.
  - A clamp holds the child, its own children and their raise attempts at 20. Both the clamp `taskpolicy -c utility` sets and the one `posix_spawnattr_set_qos_class_np` sets do, measured below.
  - A clamp caps QoS classes, not everything. The review measured a thread under `taskpolicy -c utility` taking the realtime policy (`THREAD_TIME_CONSTRAINT_POLICY`) without privilege, moving from 20 to 97. Priority donation is not capped either. The same is true under `Standard`'s clamp, so this is not a new escape, but "none can raise it" holds only for QoS requests.

## What the `utility` QoS costs the daemon

`tools/daemon_qos_compare.py` alternates the two schedulings in ABBA order, so both meet the machine's drifting load alike. Each run does two things:

- It starts `tools/store_contention_repro.py` with the daemon under `taskpolicy -c utility` (`--daemon-qos utility`) or unclamped (`inherit`), at a fixed load:
  - 6 running jobs and 21 hook sessions;
  - a 48-job backlog and a conversation turn every 10 s;
  - probes of `daemon.status`, `ping`, `list` and `show` once a second;
  - a 90 s measured window after 20 s of warm-up.
  The rig records every outermost store-lock hold, the daemon's thread priorities mid-run and the load average.
- It runs `tools/census_under_load.py` (from fix/probe-under-load) with `--qos utility` or `inherit` and four threads hogging the interpreter lock, for 60 s.

```
uv run python tools/daemon_qos_compare.py --out <dir> --code <checkout> \
    --census-tool <fix/probe-under-load>/tools/census_under_load.py --rounds 2 --warmup 20 --duration 90 --census-seconds 60
```

### The machine's own load, 125 to 173 (release/217 fd7797a2)

| Run | Daemon QoS | Load (start → end) | Daemon threads by priority | `daemon.status` p50 / p99 (s) | `ping` p50 / p99 (s) | Hook `list` p50 / p99 (s) | Turn queued → reserved p50 / p99 (s) | Requests answered | Daemon CPU (cores) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | utility | 172.7 → 163.3 | 76 at 20 | 10.107 / 26.761 | 0.240 / 20.636 | 2.128 / 29.461 | 13.868 / 19.922 | 415 (4.6/s), 9 dropped | 0.019 |
| 2 | default | 131.0 → 126.7 | 64 at 31 | 0.036 / 0.265 | 0.001 / 0.009 | 0.001 / 0.071 | 0.103 / 0.296 | 1,723 (19.1/s), 0 dropped | 0.199 |
| 3 | default | 125.2 → 141.3 | 58 at 31, 3 others | 0.063 / 1.554 | 0.001 / 0.064 | 0.001 / 0.130 | 0.249 / 0.735 | 1,707 (19.0/s), 0 dropped | 0.180 |
| 4 | utility | 136.9 → 134.4 | 76 at 20, 1 at 46 | 1.236 / 9.156 | 0.116 / 6.660 | 0.256 / 9.829 | 6.092 / 10.121 | 1,088 (12.1/s), 32 dropped | 0.057 |

"Dropped" counts client connections that ended without an answer (`ConnectionError`, `ConnectionRefusedError`).

**This run's store-lock columns are withdrawn.** The rig timed a hold after `WatchedLock.release` returned. That call frees the lock and then logs, so time when the lock was already free was counted as held (review finding 4). The raw JSON still has those columns. The timer is fixed, a test fails on the old one, and the holds were measured again below.

| Run | Census QoS | Load (start → end) | Reader | Reads taken | p50 (s) | max (s) | Past the 10 s cap |
|---|---|---|---|---|---|---|---|
| 1 | utility | 153.3 → 142.9 | pipe (2.1.7's) | 4 | 11.702 | 11.727 | 4 of 4 |
| 1 | utility | | socket (fix/probe-under-load) | 4 | 8.025 | 8.624 | 0 |
| 2 | default | 126.7 → 118.5 | pipe | 32 | 1.361 | 1.948 | 0 |
| 2 | default | | socket | 32 | 0.423 | 1.870 | 0 |
| 3 | default | 141.3 → 139.7 | pipe | 25 | 1.604 | 2.302 | 0 |
| 3 | default | | socket | 25 | 0.815 | 1.305 | 0 |
| 4 | utility | 138.1 → 130.2 | pipe | 4 | 11.046 | 11.907 | 4 of 4 |
| 4 | utility | | socket | 4 | 4.846 | 6.149 | 0 |

The census tool reports the median and the maximum. With 4 reads in 60 s, the maximum is the tail there is.

### The corrected hold timer, load 37 to 107 (this branch)

The store rig was run again with the corrected timer, in the same ABBA order (`corrected-timer/`). The machine was far quieter than in the first run.

| Run | Daemon QoS | Load (start → end) | Store-lock holds p50 / p99 / max (s) | Hold CPU max (ms) | Store-lock waits p99 (s) | `daemon.status` p50 / p99 (s) | `ping` p99 (s) | Hook `list` p99 (s) | Turn queued → reserved p50 / p99 (s) | Requests answered | Daemon CPU (cores) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | utility | 84.5 → 37.5 | 0.001 / 0.133 / 0.171 | 2.5 | 0.018 | 0.081 / 1.328 | 0.840 | 0.459 | 0.408 / 0.746 | 1,653 (18.4/s) | 0.133 |
| 2 | default | 42.4 → 41.4 | 0.001 / 0.155 / 0.354 | 2.1 | 0.001 | 0.026 / 0.069 | 0.006 | 0.017 | 0.115 / 0.330 | 1,731 (19.2/s) | 0.274 |
| 3 | default | 41.8 → 60.3 | 0.001 / 0.160 / 0.168 | 3.2 | 0.280 | 0.026 / 0.820 | 0.004 | 0.006 | 0.107 / 0.301 | 1,732 (19.2/s) | 0.272 |
| 4 | utility | 69.2 → 106.7 | 0.003 / 0.479 / 0.650 | 4.1 | 0.458 | 0.158 / 3.193 | 1.930 | 1.949 | 0.623 / 2.625 | 1,474 (16.4/s) | 0.091 |

No connection was dropped in these runs.

A second scenario added 18 busy processes at the `utility` QoS (`--spinners 18`). A session restart stopped it after one run of each scheduling, and those two runs are not an ABBA pair, so no number from it is reported.

### What the numbers say

- **The daemon's answers.**
  - At load 125 to 173 and `utility`, `daemon.status` took 1.2 to 10.1 s at the median and 9 to 27 s at p99, and clients dropped connections. At the default QoS the same load was answered in 0.04 to 0.06 s at the median, with nothing dropped.
  - At load 37 to 107 the gap is smaller but in the same direction: 0.08 to 0.16 s at `utility` against 0.026 s.
  - At `utility` the daemon also got through less of what was asked of it: 4.6 or 12.1 requests a second at the high load, against 19.
- **Admission.** A conversation turn waited 6 to 14 s at the median to be reserved at `utility` under the high load, against 0.1 to 0.25 s. Under the lower load it was 0.4 to 0.6 s against 0.11 s.
- **The census.** At `utility` under the high load, even fix/probe-under-load's socket reader took 5 to 8 s at the median, and 2.1.7's pipe reader passed its 10 s cap every time. At the default QoS the socket reader took 0.4 to 0.8 s and the pipe reader 1.4 to 1.6 s. The reader fix bounds what a slow read can decide; this change makes reads fast.
- **The store lock.**
  - With the corrected timer at load 37 to 107, holds had a p99 of 0.13 to 0.48 s at `utility` against 0.15 to 0.16 s at the default QoS.
  - A holder's own CPU in a hold was at most 4.1 ms, so a long hold is a holder waiting for the CPU, not working. That is why the difference grows with load.
  - The comparison at the high load was not repeated with the corrected timer, because the machine's load cannot be set on demand.
  - An earlier session's notes record 11 s holds on the live daemon at load ~200, on a timer thread's indexed SELECT that takes no time alone. Scheduling is a likely cause of those; this report does not show it.
- **CPU.** When it keeps up, the rig's daemon uses 0.18 to 0.27 cores; it used less at `utility` only because it did less.
  - The rig's clients are heavy: 21 hook sessions, a gate poller at 4 Hz, four probes a second and a 48-job backlog.
  - The live daemon used 2.25% of a core over 60 s at 15:24 EDT, at load ~140 and at `utility`. That was read with `ps -o time` twice, 60 s apart, and not saved to a file.

## The change

1. **`daemon install` writes `ProcessType` `Interactive`** (`subfleet/cli.py`). It is not `Adaptive` (darwinbg without XPC) and not `Standard` (the clamp above).
2. **The provider is clamped** (`subfleet/qos.py`, used by `subfleet/guardian.py`). The guardian starts it with `posix_spawn` and `posix_spawnattr_set_qos_class_np(QOS_CLASS_UTILITY)` through ctypes (standard library only).
   - **The clamp.** Measured: the spawned child runs at 20, its own children at 20, and its thread stays at 20 after asking for `user-initiated`. The header describes the attribute as determining "the interpretation by the system of all QOS class values requested by threads in the process".
   - **The same start as Popen.** The provider starts as `subprocess.Popen(argv, cwd=...)` started it:
     - the same argv, environment, directory and process group;
     - descriptors 0 to 2 from the guardian and every other one closed (`POSIX_SPAWN_CLOEXEC_DEFAULT`, as `close_fds=True`);
     - SIGPIPE and SIGXFSZ back at their defaults (`POSIX_SPAWN_SETSIGDEF`, as `restore_signals`);
     - Popen's PATH search: a later entry that runs beats an earlier EACCES, and when none runs the first real error wins, always naming `argv[0]`.
   - **A failed start is Popen's failure.** posix_spawn returns the exec's errno directly; measured ENOENT, EACCES and ENOEXEC. The guardian raises the same OSError Popen would, and C-5.2 records it as rc 127 and `spawn_error`. A missing, unenterable or non-directory cwd is reported against the directory, as Popen does. Nothing is read from the provider's streams.
   - **An executable with no `#!` line is refused with ENOEXEC,** as Popen refuses it. Plain `posix_spawn` never falls back to `/bin/sh`, where `posix_spawnp` and `execvp` do.
   - **Popen's handle.** The handle the guardian and its relay use has Popen's `pid`, `returncode`, `poll`, `wait` and `send_signal`. One lock guards reaping, so a poll during a wait neither blocks nor steals the status, and no signal is sent after the child has been reaped.
   - **The guardian itself keeps its daemon's QoS,** so its receipts, its relay (C-26.4) and its part in the kill protocol stay prompt.
   - **All three launch paths go through the guardian:** attempts (including conversation turns), admission probes, and a Claude lane's re-enrolment turn.
3. **Repository code runs clamped.** The daemon's `git` commands that run the repository's hooks or filters start under `/usr/sbin/taskpolicy -c utility`:
   - `worktree add` for a job's workspace or a worktree conversation (hooks such as post-checkout, and smudge filters);
   - salvage's `add` (clean filters, fsmonitor);
   - salvage's `update-index`;
   - salvage's `update-ref` (the reference-transaction hook).

   Measured: a post-checkout hook runs at 20 through both worktree paths. Through a job's, it runs at the daemon's QoS again with `inherit`. Reads such as `rev-parse`, `symbolic-ref` and `read-tree` keep the daemon's QoS, because a submission waits on them. A conversation's diff passes `--no-ext-diff --no-textconv` and keeps it too.
4. **The opt-out.** `SUBFLEET_PROVIDER_QOS=inherit` in the daemon's environment (the plist) turns all of this off. Providers then start through Popen as before, and `git` runs unwrapped. A host without `posix_spawnattr_set_qos_class_np` (not macOS) starts providers the same way.
5. **The contract.** C-5.1 says all of this, and the change list records the change and its review.

**Now at the daemon's QoS.** These other children run at the default QoS with the daemon:
- `ps` and `sysctl`, the census and identity reads, which is the point;
- the catalog refresh;
- the Codex guard preflight's and the isolated-review inspection's `app-server`, which are metadata only, with no model turn;
- `security` and the mirror thread;
- a first-time Claude lane enrolment's one Haiku turn (C-10.2, `ClaudeAdapter._run_turn`), which the operator starts with `subfleet lanes enroll` and waits on. Re-enrolment goes through a guardian and is clamped.

The catalog and the mirror can use real CPU. They serve the operator's app, and their cost at the default QoS was not measured separately.

## The thermal guard

`~/bin/clamshell-guard` (read, not run) acts only while the lid is closed and the Mac is on battery. It never reads priority or QoS. In that state it does three things:

- it kills `caffeinate`;
- it always SIGSTOPs `find`, `rg`, `fd` and `codex*`;
- it SIGSTOPs any of the user's processes whose `ps` `%cpu` is 20 or more, unless its path matches the exemptions (`/System`, `/Applications`, `/Library`, `/usr/libexec`, `/sbin`, `~/Applications`, `~/Library/Application Support`) or it is a shell, `launchd`, `ps`, `awk` or the guard.

It resumes them all when the lid opens or power returns.

**Its path parsing has a bug.** It splits `ps` output on whitespace (`path=$5`), so a path with a space is cut at the space. Anything under `~/Library/Application Support`, the Claude app's bundled Claude Code included, therefore misses its exemption and is paused like any other process at 20%. The review executed the predicate on synthetic rows to show this, and a chip is filed to fix the script.

How the change meets the guard:

- **Providers** stay at `utility` and are selected as before.
- **The daemon** (under `~/.local/share/subfleet`) is selected whenever its `%cpu` reaches 20.
  - QoS does not change what the daemon asks for. It changes how much of that it gets while other work competes. A starved `utility` daemon could stay under 20% where a default-QoS daemon doing the same work does not.
  - When the rig's heavy client load is kept up with, the daemon uses 0.18 to 0.27 cores, which is around the threshold. Those are window averages, not `ps`'s decaying `%cpu`.
  - The live daemon's steady use is small: 1.9% of a core with four attempts running (C-5.12's report), and 2.25% measured today.
  - So under heavy client traffic with the lid closed, the default-QoS daemon is more likely to be paused than the starved one was.
  - The guard's own pausing reduces competition, which narrows the gap, but it does not remove the exempt processes: GUI apps, and any Claude Code sessions once the parsing bug is fixed.
- **Not measured:** how long that exposure lasts, and whether `utility` also places threads on the efficiency cores or at a lower clock, which would cost less energy per unit of work.

## Installing it

2.1.8 is installed with `Interactive` (Max said yes to d493). This revision installs like any release: it changes no plist key. The installer's checks still pass: `guardian.PROVIDER_QOS == 'utility'` and `guardian.TASKPOLICY` executable (it is now used for `git`). `PROVIDER_QOS` checks the release's shape; the end-to-end check is the provider's own priority. To take it after an install:
- start one job;
- read `ps -M -p <pid>` for its provider (the attempt's `exit.json` names `child_pid`, and a running one is the guardian's child);
- expect 20.

`~/reviews/daemon-qos-2026-09-27/installer/` has the tools used for 2.1.8:
- `install_desktop_217-processtype.patch`, which sets the key only with `SUBFLEET_INSTALL_PROCESS_TYPE=Interactive`;
- `apply_process_type.py Interactive|Standard`, which changes that one key on an installed release and checks `spawn type`. A dry run on a copy of the live plist round-tripped byte for byte.

**Never** set `Interactive` on a release without the provider clamp. Every provider would then run at the default QoS beside the operator's apps.

## Tests

- `tests/unit/test_guardian_qos.py`:
  - **Choosing the clamp:** the clamp is chosen unless `inherit`; a host without the attribute inherits; the installer's names are still there.
  - **Start failures:** a spawn-failure differential against Popen for eight cases (missing path, missing bare name, not executable, a directory, no `#!` line, missing cwd, cwd a file, cwd not searchable), comparing rc, `child_pid`, `spawn_error` and both streams.
  - **Starts:**
    - a start differential (argv, environment, cwd, stdin bytes, open descriptors, process group, exit status, what `/bin/sh` sees ignored);
    - a signalled provider;
    - PATH search against Popen (EACCES then a later hit, a relative entry, EACCES alone, nothing);
    - the process handle (a poll during a wait, no signal after reaping);
    - a Hypothesis property that any argv reaches the provider unchanged.
  - **Astra's two reproducers, now regressions:** a provider that runs `taskpolicy` itself and exits 66, and a provider that swaps its stderr for a FIFO.
  - **Repository code:** salvage clamps exactly the repository-code subcommands, and `inherit` opts out.
- `tests/process/test_guardian_qos_process.py`, with real processes:
  - the provider and its child run at exactly 20 and cannot raise themselves, while the guardian keeps the test's 31;
  - the provider keeps its pid, group, parent and census marker;
  - `inherit` opts out;
  - a job worktree's post-checkout hook runs at 20, and at the daemon's QoS with `inherit`.
- `tests/unit/test_conversation_service.py`: a worktree conversation's post-checkout hook runs at 20.
- `tests/fake/test_provider_qos.py`, through a real daemon:
  - a launched provider is clamped;
  - the kill protocol contains a clamped, TERM-ignoring provider.
- `tests/unit/test_daemon_verbs.py`: the plist says `Interactive`.
- `tests/unit/test_daemon_qos_tools.py`:
  - the ABBA order (a property);
  - the rig's QoS helpers;
  - a hold ends when the lock is free (it fails on the old timer).

`mutate-r2.py` (in the data directory) makes 18 mutants of `qos.py`, the guardian, the `git` call sites, the hold timer and the plist, and runs these tests against each (`mutations-r2.txt`).

- **First pass: 16 of 18 caught.** The two survivors were leaving descriptors open and leaving signals ignored in the child. The start differential had tested neither: Python opens its descriptors close-on-exec anyway, and `/bin/sh`'s `trap` does not list signals ignored on entry.
- **The fix.** The test now holds an inheritable descriptor in the guardian while it spawns, and has a shell send itself SIGPIPE and SIGXFSZ, which it survives only if they were left ignored.
- **Second pass: both caught, 18 of 18.**

The full suite on this revision's code (`suite-r2.txt`, load 45 to 135; run before the start differential test was strengthened) gave 6,635 passed and 2 failed, both in `tests/e2e/test_conversations.py`, and neither reached the spawn:

- `test_a_stubborn_turn_is_stopped_by_sigint_through_the_relay_on_the_policy_clock`: its turn's attempt stayed `reserved`, with no attempt directory, when the test gave up.
- `test_a_turns_session_hooks_see_the_daemons_markers_and_surface_no_ping`: its first turn's provider ran and exited 0 in 1.2 s, and its second turn was never given a job within 60 s.

Both passed three times in a row, clamped and with `inherit`, at load ~42.

The suite runs providers clamped, as production does, and no fixture opts out. For the first version, `tests/process`, `tests/fake` and `tests/e2e` were run that way at load 150 to 180 (`suite-clamped-process-fake-e2e.txt`): 747 passed and 8 failed.

- **Seven were load timeouts.** Each of the eight was run clamped and with `inherit`, twice, interleaved, at load 100 to 155 (`rerun-eight-clamped-vs-inherit.txt`). Seven passed both ways every time. They had timed out in processes the clamp does not touch, such as the CLI's `run -d` and `daemon stop` at their 20 s bounds.
- **One failed only clamped.** `test_ignore_sigterm_escalates_and_verifies_containment` took one census right after SIGKILL and found the provider still exiting.
  - The daemon re-reads that census for C-5.6's `kill_settle_s` (3 s).
  - At load ~190 with the test's own shape, 15 trials each (`exit-settle.txt`, `exit_settle.py`), a clamped provider left the process table within 0.78 s (p50 0.31 s), and 4 of 15 first reads still saw it. An unclamped one was gone on every first read.
  - The test now re-reads as the daemon does. Providers already ran at `utility` under `Standard`, so this timing is not new in production.

The full suite on 655d26e0 passed 6,616 tests (`suite-655d26e0.txt`). The mutation run on 885142a5 is in `mutations-885142a5.txt`.

## Review of 885142a5

Astra reviewed the first version (lane job 20260927-163114, `~/.subfleet/worktrees/20260927-163114-daemon-qos-review-astra/.review/OUTPUT.md`) and asked for changes. Each finding was executed, and each is addressed here:

1. **A provider could be recorded as never started.** The rc-66 translation read the provider's stderr, so a provider that ran `taskpolicy` itself, failed and exited 66 was recorded as never spawned, with its stderr erased. Fixed: providers are no longer started through taskpolicy, and nothing is read from their streams.
2. **The guardian could hang after its provider exited.** It reopened the stderr path, so a FIFO left there held it, and truncating by path could erase a replacement file. Fixed the same way.
3. **The clamp is not an absolute ceiling.** It does not cap the realtime policy or priority donation. The contract and this report now say so.
4. **The rig's hold timer counted time when the lock was free.** Fixed and tested. The holds were measured again, and the earlier hold figures are withdrawn.
5. **The daemon's `git` ran repository hooks and filters at its own QoS.** Those commands are now clamped.
6. **A fork without an exec keeps the requested QoS.** The wording is corrected.

The review also asked for:
- **ENOEXEC** to be strict or its divergence justified: it is strict now;
- **evidence files for the report's claims:** added, and the rest are marked as unsaved observations;
- **the thermal wording** to be corrected: done above.

## Not changed

- **Conversation turns stay at `utility`, like every provider.** The Claude app runs its own sessions at the default QoS, so a Subfleet turn's tool calls compete below the app's. Raising turns is a separate decision, which a chip is measuring.
- **The daemon's request threads stay at the default QoS, not `user-initiated`.** Under `Interactive` a thread can raise itself to `user-initiated` (37, measured). That would put the daemon above the default-QoS load too, not just level with it.
