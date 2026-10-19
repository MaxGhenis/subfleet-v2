# Stopped daemon and bounded stdin: #35 port to release/217

## Source and scope

Base: `3fbc4b7c5615b582f11b6f766f91125304818bd7` (`origin/release/217`). The requested `origin/pr/35` name is absent locally. Its preserved equivalent, `merge/pr35-stopped-daemon` at `749ccbeae36a42b6ce1e2a4a4a4c3b5ba7bab425`, has the expected seven commits after its merge base with `origin/main` and the expected eleven-file diff. Both history and the full diff were read locally; no network was used. This is a selective port to release's seams, not a cherry-pick.

`C-5.13` and `C-17.8` were both free. Added both clauses and the first bullet under “Changes in version 3”; updated the CLI lane brief.

## Ported behavior

- One process observation supplies state and start time. A verified holder in `T` or `T` plus flags is alive but unavailable: refuse before connecting, exit 69, name the pid/state, and print the continue repair. No production diagnosis sends a signal. Zombies remain gone; the other specified states remain live. Unknown identity remains unknown.
- A stop observed after a sent request preserves exit 1/unknown outcome. The next send checks the lock again, preventing a settlement retry from adding another request to the paused backlog. Connect failures also diagnose the holder. Silent verified running holders get logs/stop advice; absence still gets start advice.
- Stopped status, JSON state/diagnosis, start refusal, offline explanations, doctor failures, and error repair propagation through the CLI, sessions and gate. Guard attribution requires the pauser's own marker listing this pid; all marker tests use fixture files.
- `ping` and `suggest --brief -` share a bounded stdin reader. The latter is the read at release's former line 197: it submits a suggestion and could hang exactly like ping. `run`'s job submission does not read stdin through that path.
- Ping renders the notice id/session actually returned and retains explicit transport delivery replies.

## Release behavior retained and deliberate adaptations

- Retained release's boot-session UUID cache, conservative legacy clock-drift handling, zombie rule, busy/refused retry windows, total response-line deadline, descriptor reporting, scrubbed suggestion errors and newer offline filters.
- Doctor already had a live ping and already sent no text (`{}`), leaving no notice. Kept that implementation and its existing no-notice test.
- Release intentionally accepts empty/blank text as a health check, uses the configured operator session when appropriate, and has a `sys.stdin is None or isatty()` branch. Kept those semantics: terminals are never read and missing stdin immediately becomes an empty-text health request. EOF on `/dev/null` also remains a health request. Therefore #35's terminal/missing/empty/blank exit-2 cases were adapted to release's behavior.
- A literal `ping -` remains a literal message. Did not add #35's unbounded dash escape: the requested invariant requires finite stdin reads. An absolute deadline replaces #35's reset-after-each-chunk wait, so continuous input is bounded too. Readiness failures refuse unread rather than falling back to an unbounded read.
- Kept release's C-5.8a shutdown/escalation. Did not port #35's production SIGCONT during `daemon stop`, following this task's instruction to report a continue command rather than send it. Its automatic-continue test is consequently not applicable.
- Release had no `tests/unit/test_client*.py` files. Added `test_client_availability.py` for the requested client group.

## Bounds

- Stdin: one total **2 s** deadline by default, including first-byte waiting and continuous delivery. `SUBFLEET_PING_STDIN_WAIT_S` accepts finite values from 0 through 86400 s; invalid, negative, non-finite and excessive values fall back to 2 s. Input is capped at **8 MiB** (in-memory text streams also have a finite character cap). Pipes/sockets return bytes received by the deadline; no-data open sources and incomplete files/devices are refused. `/dev/zero` cannot grow the read forever. These are read/wait budgets; OS scheduling, an individual regular-file I/O operation and the separately bounded RPC are outside them.
- Existing client process probes retain their **5 s per subprocess** budget. A verified stopped holder never incurs a socket response timeout. Existing RPC defaults remain 15 s, settlement 60 s; daemon status retains its 5 s RPC budget.
- Tests use a 50 ms stdin budget and require return within 750 ms. Fake stopped CLI requests must finish before 150 ms with a 200 ms socket budget; the real-child case must finish before 1.5 s with a 2 s socket budget. Repeated calls/settled calls have 30 s RPC budgets and must refuse within 500 ms without queueing a connection.
- Hypothesis uses the `ci` profile: 40 deterministic examples, no database, no deep profile. States are `R`, `S`, `T`, `Z`, `U`, `I` with permutations of `s`, `+`, `<`; explicit examples also cover `T+` and other BSD flags.

## Executed base regressions

Production files were unchanged when these were run under the shared lock:

| Test | Base result |
| --- | --- |
| `test_fake_stopped_holder_returns_69_before_timeout` | Failed: exit 1 and “no response … within 0.2s”, rather than exit 69/stopped repair. |
| `test_real_stopped_holder_returns_69_before_timeout` | Failed: `ResponseLost` after the 2 s response timeout. A child held a real flock and was actually stopped; a real listening backlog accepted the client. |
| `test_fake_stopped_doctor_row_fails` | Failed: doctor returned `pass`. |
| `test_ping_open_pipe_is_bounded` (empty and partial payload) | Both failed: reader still blocked at 750 ms with writer open; closing the writer in `finally` released it. |
| `test_suggest_open_pipe_is_bounded` | Failed for the same blocked-read reason; rerun with the complete canned suggestion reply also failed. |
| `test_ping_regular_file_is_read`, `test_ping_dev_null_keeps_release_health_check` | Already passed; these preserve working release behavior. |

The first real-child setup could not inspect its start time: this sandbox denies execution of `/bin/ps`. The fixture now uses native ps when permitted; otherwise only for its own child it observes the actual SIGSTOP through `waitpid(WUNTRACED)` and supplies that observation at the ps seam. The corrected fixture was rerun against the unchanged production base and produced the timeout above. Native ps integration remains unverified in this sandbox. Every fixture-created child receives SIGCONT and kill/wait cleanup in `finally`; `Popen` methods avoid signalling a child already reaped. No unrelated process was signalled.

## Verification status

Final required groups: **399 passed**, no skips or failures.

| File | Passed cases | Coverage relevant to this port |
| --- | ---: | --- |
| `tests/unit/test_stopped_daemon.py` | 49 | Fake and actual stopped child; no-connect refusal; repeated call/settled-call bounds; mid-call stops and unknown outcomes; status/start/offline/doctor/pauser repairs; bounded state property. |
| `tests/unit/test_cli.py` | 243 | Release CLI regressions plus open/closed pipes, sockets, files, `/dev/null`, `/dev/zero`, terminal/missing stdin, continuous data, readiness failure, wait validation, suggestion stdin and actual notice/delivery replies. |
| `tests/unit/test_boot_identity.py` | 10 | UUID and legacy-drift safety at the combined process-state seam. |
| `tests/unit/test_client_availability.py` | 15 | One-read identity/state, unknown versus gone, stopped reused pid and refusal after busy. |
| `tests/unit/test_doctor.py` | 44 | Existing table checks and notice-free live probing with portable live/gone observations. |
| `tests/fake/test_state_contract.py` | 37 | Deterministic daemon state/transaction/flock/recovery invariants. |
| `tests/unit/test_test_module_names.py` | 1 | New test modules keep unique basenames. |

Required holds, in the requested groupings: 302 passed (repeated after fixture/profile hardening), 59 passed, and 38 passed. The longest completed required pytest was 67.13 s.

Both requested mutations were executed separately, one named test per hold, and restored in `finally`:

- `STOPPED_STATES = ()` (treat `T` as live) made `test_fake_stopped_holder_returns_69_before_timeout` fail: exit 1 after 0.2 s instead of 69.
- Replacing `_stdin_bytes` with an unbounded `.read()` made `test_ping_open_pipe_is_bounded[partial message]` fail: its reader was still blocked at 750 ms while the writer remained open. Writer cleanup released it, so no thread was left running.

Git verified that both production files exactly matched their committed contents after restoration. Final affected-path hold: **195 passed**, including four repeated regression cases after restoring the mutations. This adds **191 distinct cases** beyond the required groups, for **590 distinct passing cases** overall:

| Additional file | Passed cases | Scope |
| --- | ---: | --- |
| `tests/unit/test_suggest_cli.py` | 7 | Full file. |
| `tests/unit/test_sessions_cli.py` | 142 | Full file. |
| `tests/unit/test_offline.py` | 33 | Full file; updated the confirmed-absence fixture to `proc_status`. |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | Full file; synthetic daemon identity now supplies state and start together. |
| `tests/unit/test_daemon_verbs.py` | 1 | Only `test_daemon_stop_on_a_stale_lock_is_a_no_op`; an explicit guard rejects every signal attempt. |

The first additional run found two stale offline mocks of `proc_start`; the final run above passed after moving those and the remaining daemon mocks to `proc_status`. The continuous-input test now seeds its first byte before starting its producer, avoiding dependence on thread scheduling. The four repeated cases are the fake stopped-holder regression, both open-pipe payloads, and the continuous-input deadline test.

**Unrun:** native ps integration is unavailable because the sandbox denies `/bin/ps`; the remaining daemon-verbs cases and broader native-process/provider/whole-repository suites were not run. No required group or named mutation was omitted. The native child was actually stopped, continued and reaped, with its observed stopped state supplied through the ps seam as described above. Syntax checks of all five modified production modules and `git diff --check` also passed.

All pytest runs use the prescribed foreground `lockf -k` shared lock, one process per hold, no xdist workers, with `SUBFLEET_RETENTION_DORMANT` and the installed release's `PYTHONPATH` removed. Each test has a 90 s watchdog in final runs; no hold approached 20 minutes.

The first required group passed 302 cases; the first client/doctor group exposed two older doctor tests depending on native ps. Their live/gone observations were moved to the process-state seam, and the group then passed 59 cases. No production liveness rule was weakened to accommodate the sandbox.

## Step commits

All commits are in local metadata on `fix/stopped-daemon-217`:

- `9b6b85b55`: tests first, base defect regressions and adapted #35 stopped-holder suite.
- `ea54ad07d`: client identity and continuous-input invariants.
- `dbcafbeb8`: real reply rendering and a safe endless-device test.
- `5875f3e2f`: stopped-daemon/client/doctor/CLI port and C-5.13.
- `a0ba9fbe6`: bounded stdin, suggestion submission and C-17.8.
- `e91851eea`: doctor observation seams, child cleanup and the `ci` profile.
- `fa8966df5`: remaining offline/daemon observation seams and deterministic first-byte setup.
- The final documentation commit records this report; its hash is included in the handoff.

## Workspace and delivery

All writes are in the assigned workspace or its temporary scratch. The caller's assigned branch is already checked out elsewhere, and the sandbox rejects creation of the shared worktree's `index.lock`. Step commits therefore live on `fix/stopped-daemon-217` in workspace-local `.git-local`; use `GIT_DIR=.git-local git log` to inspect them. The normal shared checkout metadata remains at the supplied detached checkpoint. All step commits include the requested co-author trailer. Nothing was pushed.

Dependencies were installed offline from read-only cached artifacts into a workspace virtualenv. The default uv cache was not writable, so uv used the task scratch cache; subsequent runs use `UV_NO_SYNC=1` and `UV_OFFLINE=1` with the already-installed dependencies. Scratch, Hypothesis output and the task-created virtualenv are removed after verification; local commit metadata is retained.
