# Guard startup scheduling, 2026-09-20

Several Codex attempts stopped before provider launch because the local app-server did not answer `hooks/list` within the guard deadline. A metadata-only replay against the same lane home, executable, and workdir verified the installed hook hash, pinned Codex 0.153.3, and runtime hook trust. No model turn was requested.

The installed daemon used launchd `ProcessType=Background`. The local macOS `launchd.plist(5)` manual describes Background as work not directly requested by the user, with resource restrictions intended to protect interactive work. Standard uses the default light CPU/I/O limits. Subfleet's user-requested dispatch belongs in Standard; it does not need unrestricted Interactive scheduling.

Controlled metadata-only checks on this Mac produced:

| Invocation | Elapsed | Result |
|---|---:|---|
| Direct process | 0.32 s | Guard trusted |
| Direct process with plist environment | 0.44 s | Guard trusted |
| Temporary launchd job, Background | 17.03 s | hooks/list timeout |
| Temporary launchd job, Standard | 10.65 s | Guard trusted |
| Temporary launchd job, Background, later warm run | 3.67 s | Guard trusted |

These observations reproduce an intermittent startup timeout and support removing unnecessary Background restrictions. They do not isolate scheduling from filesystem-cache state or prove every timeout has that cause. Elapsed time includes the version check and teardown; the hooks/list deadline was still ten seconds at this point (superseded below: it is now `CODEX_GUARD_PREFLIGHT_TIMEOUT`, default 60 s). Temporary diagnostic jobs were removed afterward.

The installer now emits Standard. Timeout refusals still return code 7 without a launch override, but their advice identifies local scheduling/load and unverified trust rather than asserting that guard files or the pinned version must be restored. Hash, version, hook identity, enabled/trusted status, and runtime response checks remain mandatory.

## Second pass, 2026-09-20 afternoon: what the surviving evidence shows

Ten Codex attempts on `codex-6` were refused with "did not answer hooks/list before the deadline" between 14:48:05Z and 15:27:33Z (the five named in the incident plus 20260920-095757-oasdi-insured-status-contract at 14:48:05Z and four `disk-cleanup`/`native-other-disability` attempts at 15:20–15:27Z). Three attempts in the same window succeeded (14:48:22Z, 14:48:28Z start after 6 s; 15:39:46Z after 1 s), and no Codex attempt has run through the daemon since the Standard-scheduling release was installed at 15:45:56Z. Reserved-to-finished times on the refused attempts were 12–26 s: the 10-second hooks/list deadline plus the version check, the TERM/KILL teardown and the control tick.

The machine had rebooted at 14:45:46Z (`kern.boottime`), two minutes before the first refusal. The unified log for the r2 window (15:08:42–15:08:58Z) shows the probe's npm launcher `node[63033]` starting at 15:08:44.33Z and the native `codex[63179]` binary (220 MB) first active at 15:08:51.14Z, 6.8 s later; warm, that gap is under 0.1 s. Nothing in `tccd`, `securityd` or `sandboxd` mentions the daemon's python, node or codex in either failure window, and the codex process only connected to `cfprefsd`. So the startup latency was in paging in and starting node and the native binary from a cold post-reboot cache under load (load average 5–21) while the daemon and its children ran under launchd `ProcessType=Background`.

Ruled out from the code and by experiment:

- Concurrency and locks: `_launch` runs on the daemon's 12-thread `workers` pool with no lock around the preflight, each probe has its own scratch home under `$SUBFLEET_HOME/tmp`, and `inspect_codex` (the second app-server probe) runs only for `-I` isolated reviews, which these were not. Nothing shares a pipe: the guardian's launch pipe is passed only to the guardian with `close_fds=True`, and the probe's stdout pipe is created and read on the calling thread.
- Binary resolution: `shutil.which("codex")` under the daemon's plist `PATH` and under a shell both resolve to `~/.bun/bin/codex` (the npm launcher for codex-cli 0.153.3).
- The launchd context itself: a phase-timed replica of the probe run as a temporary launchd job completed hooks/list in 0.18 s (Background) and 0.06 s (Standard) once warm, against 0.07 s from a shell and 0.17 s under `taskpolicy -b`. No TTY, TCC or keychain interaction blocks `codex app-server`.

What could not be reproduced: the cold, throttled startup itself (the page cache cannot be dropped without root). The Standard-scheduling change removes the throttle; this pass removes the tight deadline as a failure mode and makes the next slow start diagnosable:

- The hooks/list deadline is `CODEX_GUARD_PREFLIGHT_TIMEOUT` seconds (v1's variable), default 60 instead of 10.
- A verified verdict is cached per (Codex version, lane home, override, seeded-config fingerprint) for 30 days (C-23.5), so a repeat launch on an unchanged lane runs no app-server at all.
- A timeout is reported as "Guard preflight timed out … guard trust is unverified, not mismatched" with the probe pid, elapsed time and deadline; trust, version, configuration and environment refusals keep their own kinds and fixes.
- Every preflight writes `guard-preflight.json` (kind, cached, elapsed, probe pid, deadline, request/response lines, app-server stderr tail) into the attempt directory and one `guard preflight …` line into `daemon.log`; before this the daemon logged nothing about a launch.
- `subfleet doctor` shows the effective deadline and marker count; `subfleet doctor --live` runs the preflight per enabled Codex lane.

An independent Fable review of the change (subfleet job `20260920-130248-pr8-guard-preflight-fable-review`) found no unguarded launch, no cached refusal and no credential in any record, and asked for four changes that were made: a reap that outlives its SIGKILL wait, a broken pipe on the request write, an app-server death or a stray I/O error, and a `--version` that fails to run are each their own refusal kind (`probe` or `environment`) with the probe's stderr tail and exit status kept, instead of being filed as guard-file or version drift; the marker's fingerprint is computed from the bytes actually copied into the scratch home; a marker stamped in the future is discarded; a relative cache override resolves under the state root; the daemon passes its own root for scratch and markers; and the launcher-exits-before-its-child case is tested as a process-group reap (invariant 53).
