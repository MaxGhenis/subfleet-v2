# The daemon's scheduling, 2026-09-27

The installed daemon runs under launchd's `ProcessType` `Standard`. On this Mac (Apple M5 Max, 18 cores, macOS 26.6.2) that clamps all of its roughly 150 threads, and every process it starts, to the `utility` QoS (priority 20). A shell's processes run at 31, and so does most of the machine's load: at 15:10 EDT, 57 `claude`, 50 `node` and some 160 Python processes ran at 31. At 15:40, 69 processes of the Claude app's bundled Claude Code (`~/Library/Application Support/Claude/claude-code/<version>/claude.app`) ran at 31. Under that load a `utility` daemon waits behind all of them. This report measures what that costs, then describes the change:

- the daemon runs at the default QoS (`ProcessType` `Interactive`);
- the guardian starts every provider under a `utility` QoS clamp, so agent work stays where it was, below the operator's apps (C-5.1).

Every number below comes from a command run on 2026-09-27 on this machine. The raw output is under `docs/reports/2026-09-27-daemon-qos/`: `store-*.json` and `census-*.json` per run, `runs.json` and `summary.md` for the harness, and `mech-*.json` with the probe that wrote them. The measurements never touched the installed daemon: each run started its own fake-provider daemon or census reader. The live daemon was only read, with `ps -M` and `launchctl print`.

## What each scheduling gives a process and its children

`mechanism_probe.py` (in the data directory) runs in a fresh interpreter per variant. It reports `ps -M`'s priority for itself, for a new thread, and for children started by fork+exec (`close_fds=True`), by `posix_spawn` (`close_fds=False`) and by `taskpolicy -c utility`. It then tries to raise its own thread to `user-initiated`. It was run from a shell and as a temporary launchd job for each `ProcessType`, under other labels (`com.subfleet.qos-probe.*`), each booted out afterwards.

| Context | Itself | Raised to user-initiated | Children (fork+exec, posix_spawn) | Child under `taskpolicy -c utility` | After its own thread set `utility` | After `nice 11` |
|---|---|---|---|---|---|---|
| Shell | 31 | 31 | 31, 31 | 20 | itself 20 (15 while busy), children 31 | itself 31, children 31 |
| launchd `Standard` | 20 | 20 | 20, 20 | 20 | 20, children 20 | 20, 20 |
| launchd `Interactive` | 31 | 37 | 31, 31 | 20 | itself 20, children 31 | 31, 31 |
| launchd `Adaptive` | 4 | 4 | 4, 4 | 4 | 4 | 4 |
| launchd `Background` | did not finish within 3 minutes at load ~120 | | | | | |

What it shows:

- `Standard` is a clamp. No thread in the job, and no child, can rise above `utility`. The daemon cannot fix its own scheduling from inside.
- `Adaptive` puts the job in darwinbg (priority 4). launchd.plist(5) says Adaptive jobs "move between the Background and Interactive classifications based on activity over XPC connections". The daemon's clients use a Unix socket, so it would stay in Background.
- `Interactive` runs at the default QoS. launchd.plist(5) says such jobs have "the same resource limitations as apps, that is to say, none", and says to use the key only when an app's responsiveness depends on the job, which the Subfleet app's does. `launchctl print` shows `spawn type = interactive (4)` for it and `daemon (3)` for `Standard`. The jetsam priority is 40 for both.
- **Only a clamp reaches a child.** A thread's own QoS (`pthread_set_qos_class_self_np`) changes that thread alone. A new Python thread, and every child whether forked or spawned, still runs at 31. `nice` changes nothing a `ps -M` shows: a thread with a QoS ignores it. `taskpolicy -c utility` sets a clamp that the child, its own children and its own raise attempts all stay under.

## What the `utility` QoS costs the daemon

`tools/daemon_qos_compare.py` alternates the two schedulings in ABBA order, so both meet the machine's drifting load alike. Each run does two things:

- It starts `tools/store_contention_repro.py` with release/217's code (fd7797a2) and the daemon under `taskpolicy -c utility` (`--daemon-qos utility`) or unclamped (`inherit`). The load is fixed:
  - 6 running jobs and 21 hook sessions;
  - a 48-job backlog and a conversation turn every 10 s;
  - probes of `daemon.status`, `ping`, `list` and `show` once a second;
  - a 90 s measured window after 20 s of warm-up.
  - The rig now records every outermost store-lock hold, the daemon's thread priorities mid-run and the load average.
- It runs `tools/census_under_load.py` (from fix/probe-under-load) with `--qos utility` or `inherit`, with 4 threads hogging the interpreter lock, for 60 s.

The first scenario ran on the machine's own load only:

```
uv run python tools/daemon_qos_compare.py --out <dir> --code <release/217 checkout> \
    --census-tool <fix/probe-under-load>/tools/census_under_load.py --rounds 2 --warmup 20 --duration 90 --census-seconds 60
```

### The machine's own load (125 to 173)

| Run | Daemon QoS | Load (start → end) | Daemon threads by priority | Store-lock holds p50 / p99 / max (s) | Store-lock waits p99 (s) | `daemon.status` p50 / p99 (s) | `ping` p50 / p99 (s) | Hook `list` p50 / p99 (s) | Turn queued → reserved p50 / p99 (s) | Requests answered | Daemon CPU (cores) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | utility | 172.7 → 163.3 | 76 at 20 | 0.081 / 1.901 / 1.901 | 2.603 | 10.107 / 26.761 | 0.240 / 20.636 | 2.128 / 29.461 | 13.868 / 19.922 | 415 (4.6/s), 9 dropped | 0.019 |
| 2 | default | 131.0 → 126.7 | 64 at 31 | 0.001 / 0.135 / 0.664 | 0.034 | 0.036 / 0.265 | 0.001 / 0.009 | 0.001 / 0.071 | 0.103 / 0.296 | 1,723 (19.1/s), 0 dropped | 0.199 |
| 3 | default | 125.2 → 141.3 | 58 at 31, 3 others | 0.001 / 0.347 / 0.375 | 0.177 | 0.063 / 1.554 | 0.001 / 0.064 | 0.001 / 0.130 | 0.249 / 0.735 | 1,707 (19.0/s), 0 dropped | 0.180 |
| 4 | utility | 136.9 → 134.4 | 76 at 20, 1 at 46 | 0.036 / 1.075 / 1.150 | 0.937 | 1.236 / 9.156 | 0.116 / 6.660 | 0.256 / 9.829 | 6.092 / 10.121 | 1,088 (12.1/s), 32 dropped | 0.057 |

"Dropped" counts client connections that ended without an answer (`ConnectionError`, `ConnectionRefusedError`).

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

A second scenario added 18 busy processes at the `utility` QoS, as agent work runs (`--spinners 18`). A session restart stopped it after one run of each scheduling, and they were not an ABBA pair, so no number from it is reported here.

### What the numbers say

- **The daemon's answers.** At `utility`, `daemon.status` took seconds at the median and tens of seconds at the tail, and clients dropped connections. At the default QoS the same load was answered in tens of milliseconds, with nothing dropped.
- **The store lock is held long because its holder is not running.** The CPU a holder spent inside a store-lock hold was at most 4.3 ms in every run (p99 2.1 to 3.4 ms). The wall time of a hold had a p99 of 1.1 to 1.9 s at `utility`, against 0.14 to 0.35 s at the default QoS, whose longest hold was 0.66 s. The 11 s holds seen on the live daemon at load ~200 on 2026-09-27 (a timer thread on an indexed SELECT that takes no time alone) have this shape. They were written off as "environment, not code"; the daemon's scheduling is the likely cause. The default QoS makes them rarer and shorter, not impossible: one hold here still took 0.66 s at load 131.
- **Admission.** A conversation turn waited 6 to 14 s at the median to be reserved at `utility`, against 0.1 to 0.25 s at the default QoS.
- **The census.** At `utility` even fix/probe-under-load's socket reader took 5 to 8 s at the median, and 2.1.7's pipe reader passed its 10 s cap every time. At the default QoS the socket reader took 0.4 to 0.8 s at the median and the pipe reader 1.4 to 1.6 s, never near the cap. The reader fix and this change are complementary. The fix bounds what a slow read can decide; this change makes reads fast.
- **CPU.** At the default QoS the rig's daemon used 0.18 to 0.20 cores; at `utility`, 0.02 to 0.06. That is not a cost of the default QoS. At `utility` the daemon did less of the work asked of it: it answered 4.6 or 12.1 requests a second against 19, and its 20 Hz control loop ran late. The rig's client load is heavy: 21 hook sessions, a gate poller at 4 Hz, four probes a second and a 48-job backlog. The live daemon used 2.25% of a core over a 60 s window at 15:24 EDT (load ~140, at `utility`).

## The change

1. **`daemon install` writes `ProcessType` `Interactive`** (`subfleet/cli.py`). It is not `Adaptive` (darwinbg without XPC) and not `Standard` (the clamp above).
2. **The guardian clamps the provider** (`subfleet/guardian.py`). It starts the provider as `/usr/sbin/taskpolicy -c utility <argv>`.
   - taskpolicy execs the provider in its own place (measured): the pid, process group, parent, environment (and so C-5.5's markers), argv, stdin/stdout/stderr, exit status and signals are those of the unclamped launch.
   - The guardian itself keeps its daemon's QoS. Its receipts, its relay (C-26.4) and its handling of the kill protocol stay prompt, and it uses almost no CPU.
   - All three launch paths go through the guardian, so all of them are clamped: attempts (including conversation turns), admission probes, and a Claude lane's re-enrolment turn.
3. **A spawn failure reads as before (C-5.2).** Unclamped, Popen raises and the guardian records rc 127 with `spawn_error`. Clamped, taskpolicy itself starts and then fails: it exits 66 with `taskpolicy: posix_spawn: <reason>` on the provider's stderr. The guardian turns exactly that into rc 127, no child, and the `spawn_error` Popen would have written (`[Errno 2] No such file or directory: 'codex'`). It also empties the stderr file.
   - Emptying it matters. Left there, the line would reach the adapters. Codex's transient pattern matches "Resource temporarily unavailable" and five other reasons, and its limit pattern matches "Disc quota exceeded", which would close the lane. Found by checking all 106 reasons against every adapter pattern.
   - A differential test compares the receipt and both streams, clamped and unclamped, for a missing path, a missing bare name, a file that is not executable, and a directory.
   - One difference is intended and pinned. An executable text file with no `#!` line runs under `/bin/sh` through taskpolicy's `posix_spawnp`, as `execvp(3)` does, where Popen refuses it with ENOEXEC. No provider is such a file.
4. **The opt-out.** `SUBFLEET_PROVIDER_QOS=inherit` in the daemon's environment (the plist) runs providers at the guardian's QoS instead. So does a host without taskpolicy. That is not macOS, where it ships in the base system.
5. **The contract.** C-5.1 says all of this, and there is one change-list line.

The daemon's other children now run at the default QoS with it: `ps` and `sysctl` (the census and identity reads, which is the point), `git worktree add` and salvage's `git`, the catalog refresh, the Codex guard preflight's `app-server`, `security` and the mirror thread. Each is the daemon's own bookkeeping, bounded, and on a path someone waits on.

One model turn does not go through a guardian: a first-time Claude lane enrolment (C-10.2, `ClaudeAdapter._run_turn`). It is one Haiku turn that the operator starts with `subfleet lanes enroll` and waits on, so it runs at the daemon's QoS. Re-enrolment goes through a guardian and is clamped.

## The thermal guard

`~/bin/clamshell-guard` (read for this report, not run) acts only while the lid is closed and the Mac is on battery.

- **What it does then:**
  - It kills `caffeinate`.
  - It always SIGSTOPs `find`, `rg`, `fd` and `codex*`.
  - It SIGSTOPs any of the user's processes with `ps`'s `%cpu` at 20 or more whose executable is not under `/System`, `/Applications`, `/Library`, `/usr/libexec`, `/sbin`, `~/Applications` or `~/Library/Application Support`.
  - It resumes them all when the lid opens or power returns.
- **What it reads:** it never reads priority or QoS.

How the change meets it:

- **Providers.** They stay at `utility` and are paused exactly as before.
- **The daemon.** Its Python lives under `~/.local/share/subfleet`, so the guard pauses it whenever its `%cpu` reaches 20, as on 2026-09-20.
  - QoS does not change what the daemon asks for. It changes whether the daemon gets it while other work competes.
  - In the guard's condition the machine is mostly asleep (caffeinate is gone), and the guard itself has paused the heavy processes. The daemon then gets its demand under either scheduling, so the guard's verdict on it depends on that demand, not on QoS.
  - The demand is small in steady state: 1.9% of a core with four attempts running (C-5.12's report), and 2.25% measured today.
  - Under a client load as heavy as the rig's it was 0.18 to 0.20 cores. That is at the guard's threshold, and at either scheduling the daemon would be paused once it gets that much CPU.
- **Where the change can show:** in the seconds before the guard's pauses take effect. There, a default-QoS daemon's `%cpu` reflects more of its demand than a starved one's did.
- **Not measured:** whether the `utility` QoS also places threads on this chip's efficiency cores or at lower clock, and so costs less energy per unit of work. The daemon's work is a few percent of a core, so any such difference is small.

## Installing it

The plist lives outside the repository and is changed only by an install. The step belongs in the desktop line's installer (`install_desktop_217.py`); it is a patch, `install_desktop_217-processtype.patch`, handed to the transition session:

1. **Before the daemon is stopped**, the installer asserts:
   - the release being installed has the clamp (`guardian.PROVIDER_QOS == 'utility'`, imported from the new release's Python, which the installer already runs);
   - taskpolicy is executable;
   - the plist does not opt providers out.
2. **While the daemon is booted out**, it runs `plutil -replace ProcessType -string Interactive`, beside `ExitTimeOut` 40. It asserts that no other key changed.
3. **After the bootstrap**, it asserts that `launchctl print` shows `spawn type = interactive (4)`, and it prints the daemon's thread priorities, which should be 31.
4. **Rollback** is the plist in the install's backup, which the installer already copies.

**Ordering.** Never set `Interactive` on a release without the guardian clamp. Every provider would then run at the default QoS beside the operator's apps.

## Tests

- `tests/unit/test_guardian_qos.py`:
  - how the clamp is chosen and how argv is wrapped;
  - the spawn-failure differential;
  - the intended ENOEXEC divergence;
  - two Hypothesis properties: every errno taskpolicy can report becomes Popen's words, and no other exit status or stderr is read as a spawn failure;
  - the host's taskpolicy failure shape, pinned.
- `tests/process/test_guardian_qos_process.py`, with real processes:
  - the provider and its child run at exactly 20 and cannot raise themselves, while the guardian keeps the test's 31;
  - the provider keeps its pid, group, parent and census marker;
  - `inherit` opts out.
- `tests/fake/test_provider_qos.py`, through a real daemon:
  - a launched provider is clamped;
  - the kill protocol still contains a clamped, TERM-ignoring provider.
- `tests/unit/test_daemon_verbs.py`: the plist says `Interactive`.
- `tests/unit/test_daemon_qos_tools.py`: the harness's ABBA order (a property), and the repro tool's QoS helpers.

The suite runs providers clamped, as production does; no fixture opts out. The directories that start providers were run that way:

- **The clamped run.** `tests/process`, `tests/fake` and `tests/e2e` gave 747 passed and 8 failed, at load 150 to 180.
- **The eight re-run.** Each was run clamped and then with `SUBFLEET_PROVIDER_QOS=inherit`, twice, interleaved, at load 100 to 155. Seven passed both ways every time. They were load timeouts in processes the clamp does not touch, such as the CLI's `run -d` and `daemon stop` at their 20 s bounds.
- **The one clamp failure.** The eighth, `test_ignore_sigterm_escalates_and_verifies_containment`, failed only clamped. It took one census right after SIGKILL and found the provider still exiting (`ps` state `?E`).
- **Why the daemon was never at risk.** The daemon re-reads that census for C-5.6's `kill_settle_s` (3 s). Measured at load ~190 with the test's own shape, 15 trials each:
  - a clamped provider left the table within 0.78 s at worst (p50 0.31 s), and 4 of 15 first reads still saw it;
  - an unclamped one was gone on every first read (p50 0.26 s).
- **The fix.** The test now re-reads as the daemon does. Production providers already ran at `utility` under `Standard`, so this timing is not new there.

## Not changed

- **Conversation turns stay at `utility`, like every provider.** The Claude app runs its own sessions at the default QoS, so a Subfleet turn's tool calls compete below the app's. Raising turns would let the operator's own conversation, and every command its agent runs, compete with the operator's apps. That is a separate choice.
- **The daemon's request threads stay at the default QoS, not `user-initiated`.** Under `Interactive` a thread can raise itself to `user-initiated` (37, measured). That would put the daemon above the default-QoS load too, not just level with it. At the default QoS, today's measured load is already answered in tens of milliseconds.
