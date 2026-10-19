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
- Tests use a 50 ms stdin budget and require return within 750 ms. Fake stopped CLI requests use an immediate process stub and a 200 ms socket budget. The real-child case proves that no connection is queued; it makes no 1.5 s production promise. Detection needs one process-probe budget (5 s) plus parsing, after any one-second pacing wait and separately bounded boot-identity reads, excluding OS scheduling. Repeated calls and settlement prove no queued connection with a controlled probe clock.
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

## Round-1 verification status

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


## Round 2: review fixes for #232

### Base and review

Started from `c658e8d01204`. The local `release/217` at `b053e3de8f2e` is already an ancestor, so no merge or conflict resolution was needed. No network remote was contacted. The checkout contained no `review-r1.md`; its path was requested while work proceeded from all findings supplied in the brief.

### Changes and cost

- Every send on a verifying client now takes a fresh holder observation. There is no cached running verdict. `holder_report` spaces process-observation starts by at least **1 s** per client, including failed reads and failure diagnostics; a rapid next request waits for that slot before checking and sending. Thus `wait`, `watch`, busy retries and settlement cannot spin `ps` or send during a cached-running gap. State and identity still share **one `ps` per preflight**. Existing connect/read-failure diagnosis can make one additional, paced observation; boot-identity reads retain release's behavior. C-15.6's explicit hook opt-out is preserved, and a stop diagnosed after sending enables preflight for the retry.
- A process observation verifies identity only for **rc 0 with exactly one well-formed state/start row**. The parser checks the state token, C-locale date columns and valid numeric date/time. **rc 1 with no output** alone means absent; other failed, empty or malformed results remain unverifiable, regardless of stdout.
- Ping calls the shared stdin reader for its terminal/missing-stream health check, putting `isatty()` inside the reader's error handling. Closed `StringIO` now produces a clear exit-2 refusal before any client is constructed.
- C-5.13 states the real detection bound: **one process-probe budget (5 s) plus parsing**, after any probe-spacing wait and the separately bounded boot-identity reads. OS scheduling is excluded; 1.5 s is not guaranteed under load. The real-child test now proves no socket connection, and a controlled 1.6 s probe test demonstrates the probe cost.
- C-17.8 explicitly documents the existing size behavior: even regular files can be read up to **8 MiB + 1 byte** before rejection. No file-size pre-check was added.

### Tests first and baseline evidence

`0085ca37f` committed the round-2 tests before production changed. One foreground shared-lock hold against unchanged `c658e8d01` production code produced **5 expected failures** in 5.05 s (475.42 s including lock wait):

- `test_cached_client_rechecks_before_wait`: a real `Client` successfully pinged an `S` holder, then sent `wait` to its `T+` backlog and raised `ResponseLost` after the 0.1 s socket timeout.
- `test_failed_ps_cannot_verify_stopped_holder[1]` and `[2]`: both failed observations established a live identity from `T+` stdout.
- `test_ping_closed_python_stdin_is_refused`: `isatty()` raised `ValueError`.
- `test_ps_state_classification_is_exact`: the expanded property rejected failed stdout and multiple rows. It uses the bounded `ci` settings (40 deterministic examples, no database), with explicit counterexamples and generated return codes, row shapes, state letters and modifiers.

### Round-2 execution status

Final required groups: **616 passed, 5 skipped**, no outstanding failures. All four requested file groups were selected in full, one pytest per foreground shared-lock hold, no xdist workers. The final group results were **85 passed** (26.33 s), **437 passed** (45.20 s), **56 passed / 5 skipped** (10.15 s), and **38 passed** (16.81 s).

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/unit/test_stopped_daemon.py` | 53 | 0 |
| `tests/unit/test_client_availability.py` | 22 | 0 |
| `tests/unit/test_boot_identity.py` | 10 | 0 |
| `tests/unit/test_cli.py` | 244 | 0 |
| `tests/unit/test_doctor.py` | 44 | 0 |
| `tests/unit/test_sessions_cli.py` | 142 | 0 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |

Two initial suite attempts exposed test expectations, both corrected and rerun:

- First group: 83 passed and two failures in the new pacing fixture, which used unsupported op `watch`. Using `conversation.watch` fixed both; no production change was needed.
- Second group: 436 passed and one failure in `test_the_identity_check_runs_once_per_client`, an older assertion of the exact cache behavior this review removes. `84a706300` replaces it with fresh, paced unknown-identity observations using the existing controlled poll clock.

Both requested mutations failed their named tests in separate holds and were restored byte-for-byte in `finally`:

- Checking only on the first send failed `test_cached_client_rechecks_before_wait`: `ResponseLost` after the stopped backlog accepted `wait` (1 failed in 1.83 s).
- Trusting non-zero `ps` stdout failed `test_failed_ps_cannot_verify_stopped_holder[2]`: a failed observation established a live identity (1 failed in 0.67 s).

Git confirmed both production files matched `e324fec94` exactly after restoration. No production code changed afterward. Syntax checks and `git diff --check` passed.

**Skipped/unrun:** the following five cases in `test_daemon_verbs.py` were skipped by the existing native-inspection prerequisite because “macOS boot identity is unavailable”:

- `test_daemon_start_waits_for_the_socket_then_status_and_stop`
- `test_daemon_start_runs_in_its_own_session`
- `test_daemon_start_waits_out_a_self_daemonising_subfleetd`
- `test_daemon_stop_waits_past_the_stop_bound_for_a_daemon_that_ends_itself`
- `test_daemon_stop_kills_a_verified_daemon_whose_bound_never_armed`

Native ps integration remains unverified; the stopped-child fixture can use its owned child's actual `waitpid(WUNTRACED)` observation at the ps seam when native inspection is unavailable. Broader provider, native-process and whole-repository suites were not run. The full `review-r1.md` was unavailable in the checkout; all findings supplied in the brief were addressed.

### Round-2 environment, cleanup and delivery

All nine pytest invocations used the prescribed shared `lockf -k` lock, with `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH` removed, a separate temporary directory and uv cache per hold, no pytest cache, bounded Hypothesis `ci` settings, and a 90 s per-test watchdog. The longest pytest was 47.04 s; the longest wait plus execution was 475.42 s. Every hold was under 20 minutes. No governor stop was signalled or worked around.

The default uv cache was not writable. The task virtualenv used system pytest **8.4.2** and read-only cached Hypothesis **6.168.1** / sortedcontainers **2.4.0**, with `UV_NO_SYNC=1` and `UV_OFFLINE=1`; no dependency download or network remote was needed. Each runner removed its scratch in `finally` with `shutil.rmtree`. The task virtualenv, remaining task helpers and any workspace Hypothesis output were removed after verification. All foreground runners and their lock holders exited; fixture-owned children were cleaned up. No live daemon, store or unrelated process was changed or signalled.

Round-2 step commits on `fix/stopped-daemon-217`:

- `0085ca37f`: tests first and the expanded bounded property.
- `e324fec94`: fresh paced preflight, failed-observation parsing, closed-stdin handling, accurate contract bounds and the watch-fixture correction.
- `84a706300`: replace the obsolete per-client-cache expectation.
- The final documentation commit records this section; its hash is in the handoff.

The assigned branch is checked out in the caller's worktree and shared Git metadata is outside this workspace's writable roots. These commits are retained in workspace-local `.git-local`; inspect with `GIT_DIR=.git-local git log`. The normal shared checkout remains detached at `c658e8d01204`. All commits have the requested co-author trailer. Nothing was pushed.

## Round 3: deadlines, later stopped observations, and ps spacing

### Merge and review

Started from `007dac49a211`; merged the available local `origin/release/217`
(`d72597b5c`) first, in `f85dc67ef`, without contacting a network remote.
The sole conflict was the acceptance-contract change log: kept both sides'
entries. Every clause identifier from both parents remains (256 on this
branch, 254 on release), including release's C-5.8a/C-26.4 and the port's
C-5.13/C-17.8. Release's shutdown behavior remains intact.

`review-r2.md` was absent from both the supplied workspace and the caller's
named checkout. Its location was requested; the standalone file remains
unread. Every finding supplied in the round-3 brief was addressed.

Release has no `CONFIG_NAMES` definition (`rg -n CONFIG_NAMES subfleet/*.py`
finds none), so #222 has not landed in this local release ref. No allowlist
was changed. The hub must add `SUBFLEET_PING_STDIN_WAIT_S` when integrating #222.

### Changes, deadline threading and costs

- `Client.call(timeout=...)` shares its budget across fresh preflight,
  probe-slot waits, connect, send, response reads, and busy/refused diagnosis.
  Availability, holder-report and diagnostic helpers receive the deadline.
  An expired pre-send budget is `RequestTimeout`; a sent reply observed after
  the deadline is `ResponseLost`, never success.
- `wait_jobs` threads remaining time through polls, refused-after-busy holder
  checks and idle backoff, and rejects late results. This covers `wait`,
  `kill --wait` and synchronous run waits. An error whose diagnostic slot
  consumes the deadline also retains wait's normal exit 124.
- RPC `conversation.watch` uses the same `Client.call` path; this checkout
  has no separate native CLI watch polling loop. `call_settled`'s first call,
  re-send and request-id lookups all use that path, retaining their separate
  existing budgets (default 15 s, requery 60 s, lookup the client default).
  A re-send expiring before its probe slot preserves `OutcomeUnknown` for
  the first, already-sent request.
- `daemon status` shares five seconds between its initial observation and
  ping. Doctor's ping and descriptor RPCs use the same five-second call path.
  Start/install readiness pings cap their three-second budget by the outer
  ten-second loop's remaining time.
- The hook's candidate list, wait, refused-after-busy check, notice lookup
  and notice mark receive the remaining hook deadline. Auxiliary RPCs also
  retain the configured client timeout cap. C-15.6's opt-out is preserved.
- Pacing stamps **identity-observation completion**, in `finally`, after boot
  lookup and ps, including failed inspection. The next observation cannot
  begin until one full interval after that stamp; its ps cannot begin sooner,
  so every pair of ps starts is at least `HOLDER_PROBE_INTERVAL_S` apart.
  Cost: slow observations also pay a full one-second gap after completion.
- Preflight still costs one ps (5 s per process-probe budget), plus existing
  separately bounded boot reads and parsing. A ready failure diagnostic may
  add one such observation. Its slot wait never extends the request deadline;
  if the slot is too late, the original failure retains no fresh stopped
  diagnosis. A long preflight cannot then send or report success. OS
  scheduling is excluded from these budgets; no subprocess was added.
- A later `DaemonStopped` replaces status's earlier liveness/state, stopped
  flag, diagnosis, text holder line and repair. JSON includes `fix`; initial
  and later stopped holders both exit 69. The sweep found doctor's offline
  lock row makes no RPC and its live ping row already preserves later stops.
  Fixed the descriptor row's lost repair and `_daemon_alive`'s swallowed stop
  in start/install, preventing a second start after that observation.

### Regression and property evidence

Tests precede their corresponding production fixes in history. The first
baseline hold, with round-2 production plus the release merge, produced
**8 failed, 1 passed** in 1.61 s. Git confirms the four affected production
modules match `007dac49a` exactly at that merge, so these regressions exercise
the requested head:

- `test_wait_probe_slot_respects_deadline`: completion at .75 s satisfied a
  .6 s wait as success at controlled time 1.0 s.
- `test_status_uses_later_stopped_observation[False/True]`: retained alive/S
  after observing T+; omitted the stopped diagnosis/repair.
- `test_slow_boot_lookup_cannot_collapse_ps_spacing`: both ps starts were 1.6 s.
- `test_call_rejects_response_observed_after_deadline`: accepted a .75 s reply
  for a .6 s request budget.
- `test_probe_pacing_deadline_property`: all three explicit counterexamples
  failed. Doctor's descriptor repair and start's later-stop regression failed
  too; doctor's existing ping-row variant passed.

The bounded property uses ci settings: 40 deterministic examples, no database,
with four-call deadlines, boot/probe durations and completion times. Generated
combined observations cost at most five seconds. It asserts return by deadline
plus one observation budget, no success for a post-deadline completion or
observation, and actual ps starts at least one interval apart. The prior
state/parser property remains. Additional failing regressions covered hook
auxiliary budgets (2 failures) and the refused-connect diagnostic timeout
(exit 69 at .6 s); their final versions pass.

Initial verification exposed five older diagnostic fixtures whose 0.2–1 s
budgets could no longer include another probe slot. Their budgets now allow
1.2–2 s while retaining diagnosis/repair/unknown-outcome and no-extra-send
assertions. A hook notice fixture now leaves time after its controlled poll
for notice lookup; no production timeout was loosened. The native hook ps
prerequisite is explicit. Three early runner attempts ended before collection
while correcting the pytest entry point/plugin selection.

### Final verification

**810 passed, 6 skipped**, counting distinct cases in the full files below.
Required groups: **628 passed, 5 skipped**. Additional affected paths:
**182 passed, 1 skipped**. No outstanding failures.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/unit/test_stopped_daemon.py` | 65 | 0 |
| `tests/unit/test_client_availability.py` | 22 | 0 |
| `tests/unit/test_boot_identity.py` | 10 | 0 |
| `tests/unit/test_cli.py` | 244 | 0 |
| `tests/unit/test_doctor.py` | 44 | 0 |
| `tests/unit/test_sessions_cli.py` | 142 | 0 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |
| `tests/unit/test_hooks.py` | 68 | 1 |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 |

Final required holds: 97 passed in 26.15 s, 437 in 39.20 s, 56/5 skipped in
8.39 s, and 38 in 4.15 s. With lock waits these took 224.25, 131.21, 24.53 and
4.78 s. The additional 182/1 skipped hold took 48.15 s (73.11 s with its wait).
Every pytest run used its own foreground `lockf -k` shared-lock hold, no xdist
workers, no pytest cache, a 90 s per-test watchdog, and bounded Hypothesis.
No hold approached 20 minutes; all pytest runtimes were under one minute.

All three requested mutations were killed by their named tests, one expected
failure per separate hold, then restored byte-for-byte in `finally`:

| Mutation | Named failing test | Pytest seconds |
| --- | --- | ---: |
| Unbounded slot wait | `test_wait_probe_slot_respects_deadline` (time 1.0 instead of .6) | 0.96 |
| Status retains first observation | `test_status_uses_later_stopped_observation[False]` (holder alive) | 0.65 |
| Stamp before boot lookup | `test_slow_boot_lookup_cannot_collapse_ps_spacing` (both starts 1.6) | 0.44 |

Git confirms production files match their committed contents after all three
restorations. Syntax checks and `git diff --check` pass.

**Skipped/unrun:** the same five native daemon cases listed in round 2 skip
because macOS boot identity is unavailable. The additional hook integration
case `test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs`
skips when native ps execution is denied, after its hook output/mark checks.
Native ps integration remains unverified; stopped-child tests actually stop,
continue and reap their own children using the existing waitpid seam where
needed. Broader provider/native-process and whole-repository suites were not
run. The conditional launch-environment/credential group was not applicable
because no `CONFIG_NAMES` exists or was changed. No required file was omitted.

### Step commits and cleanup

All commits are on `fix/stopped-daemon-217` in workspace-local `.git-local`:

- `f85dc67ef`: release merge and contract union.
- `92c59084a`, `b53706078`, `cb74074d2`: deadline, later-observation, spacing,
  settlement and hook-wait tests first.
- `93bd93280`: deadline-aware preflight/transport/diagnosis, completion stamp,
  status/doctor/start fixes, settlement outcome and contract.
- `8aad7bb47`: paced diagnostic fixture budgets.
- `2e0db9910`: hook auxiliary-budget regressions first.
- `4f22f725c`: deadline-aware descriptor test stand-ins.
- `174420108`: hook candidate/notice deadline forwarding.
- `df83763a3`: hook notice fixture budget and native ps prerequisite.
- `e79f7ae8a`: refused-connect diagnostic timeout regression first.
- `c6dd5252b`: preserve wait exit 124 after diagnostic budget expiry.
- The final documentation commit records this section; its hash is in the handoff.

Shared Git metadata is outside writable roots; the shared checkout remains
detached at `007dac49a211`. Inspect commits with `GIT_DIR=.git-local git log`.
Every step includes the requested co-author trailer. Nothing was pushed.

Dependencies used system pytest 8.4.2, pytest-timeout and read-only cached
Hypothesis 6.131.0 through a workspace virtualenv. The default uv cache is
outside writable roots; every hold used its own scratch uv cache with
`UV_NO_SYNC=1`, `UV_OFFLINE=1`, and `PYTHONPATH`/`SUBFLEET_RETENTION_DORMANT`
removed. Each scratch was removed with `shutil.rmtree` in `finally`. The task
virtualenv, helpers, bytecode caches and any workspace Hypothesis output were
removed after verification. Local commit metadata is retained. All foreground
runners/lock holders exited, and fixture-owned children were reaped. No live
daemon, live store, unrelated process, network remote or global cache was
changed, signalled or cleaned.

## Round 4: one observation budget, hook delivery, and burst pacing

### Scope and release state

Started from `3672bb537103899293d3118acd900e144fc61b4e` in the assigned
worktree, with no existing changes. The available `origin/release/217` ref
still names `d72597b5c9e2840c4405038a67366484352dbf5a`, already an ancestor of
this head. The local `release/217` branch names the older ancestor
`b053e3de8f2e1aa92dee9a9dc8de24097e37b03f`; no release merge or conflict
resolution was necessary. No remote was contacted. The acceptance contract's
other clauses and historical change-log entries are preserved.

The full `review-r3.md` was unavailable in the checkout and at the named caller
repository's root; its path was requested. This round addresses every finding
and reproduction supplied in the brief. No sub-agents were used.

Shared Git metadata is outside the writable sandbox. Commits are on the
workspace-local `fix/stopped-daemon-217` branch in `.git-local`, whose object
alternate reads the existing repository. Inspect with `git --git-dir=.git-local
log`; the original shared checkout metadata stays detached at `3672bb537`.
Every step uses the requested co-author trailer. Nothing was pushed.

### Cause and budget design

Each subprocess previously received five seconds independently. Consequently,
a 1.6 s boot lookup plus a 5 s process read returned at 6.6 s during a .6 s
call, or 7.2 s when diagnosis began after its socket timeout. A separate boot
allowance made that behavior consistent with the old wording, but violated the
reviewer's deadline-plus-one-budget requirement.

`holder_report` now establishes one absolute observation deadline at
`min(observation_start + 5, call_deadline + 5)`. A context-local deadline reaches
`_run` without changing the existing identity-reader APIs. Every UUID, legacy
fallback or legacy-comparison boot subprocess and the one `ps` receives only
the observation time remaining; exhausted reads start no subprocess and remain
unverifiable. The context resets in `finally`, including on inspection failures.
Preflight and failed-connect/post-send diagnosis share the original call's
absolute `call_deadline + 5` ceiling. Thus a diagnostic cannot restart a new
allowance after earlier work has consumed that ceiling. Bucket waits stop at
the original call deadline. Transport and late-success rejection remain in the
same call budget. C-5.13 removes its separate boot-read allowance and records
this design; subprocess cleanup, parsing and OS scheduling are still outside
the controlled subprocess-time model.

The expanded deterministic clock model honors actual `subprocess.run(timeout)`
semantics. The bounded property generates boot and ps costs **independently
from 0 to 10 s each**, deadlines from .001 to 7 s, completion times from 0 to
20 s, and verifying/non-verifying clients, across four consecutive calls.
Explicit examples include the reviewer's 1.6 + 5 s observation and both stages
at 10 s. It asserts every call ends by deadline + 5 s and every success was
observed by the original deadline. Both preflight and post-send regression
cases failed at the original production head (6.6 and 7.2 s). Their fixed
observations finish at 5.0 and 5.6 s respectively. Another case exercises both
preflight and post-send diagnosis within one call.

### Bucket and refusal before send

Completion-based spacing charged about a second between every pair of calls,
even for short commands. A capacity-four token bucket replaces it. The code
represents tokens with a virtual next-token time: a probe may start when
`next_token - 3 * interval` is due, then advances that time by one interval
from the later of its current value and now. This is equivalent to four stored
tokens with one token refilled per second; idle time caps at four tokens, and
inspection time also contributes elapsed refill time. Failed observations and
failure diagnostics use the same bucket. Every verifying send still receives
a fresh state/identity read; no running verdict is cached. C-15.6's explicit
hook opt-out remains.

Four immediate sends sleep zero seconds; the fifth waits for a token. A long
loop has rate at most one observation per second after its burst. The new
bounded property checks **every sub-window** of generated send sequences,
including repeated zero gaps and long idle refills: observations never exceed
`4 + elapsed / HOLDER_PROBE_INTERVAL_S`. This rule counts whole observation
starts, replacing round 3's spacing between actual ps starts; a slow boot read
may finish just before the next observation's ps within the allowed burst.
The slow-boot regression now also checks refill saturation rather than imposing
the superseded pairwise spacing. A saturated refused-connect diagnostic still
ends `wait --timeout .6` at .6 with exit 124, and the running-to-stopped status,
doctor, start, re-send and no-connect regressions remain covered.

### Hook delivery reserve

The hook previously slept out an early poll's unused time before looking at its
result. On its last poll that discarded an available terminal notice: lookup
had zero time and only a job-summary fallback was printed, without a mark.
It now examines terminal results first and delivers immediately. After reading
candidates, it reserves **the smaller of 2 seconds and half the remaining
budget** for delivery. Poll budgets, busy pauses and idle backoff all stop at
`deadline - reserve`. Notice lookup gets at most half the remaining delivery
time, preserving time for `notice.mark`; neither starts a fresh hook deadline.
For the reviewer's timeout of 6 s, the existing 5 s harness margin leaves about
1 s of work time: about .5 s is reserved for the two notice calls, with at
least half that delivery time retained for the mark when lookup starts.
C-15.2 and its change-log entry record the behavior.

The real `python -m subfleet hook PostToolUse` regression sets
`SUBFLEET_HOOK_TIMEOUT_S=6`, uses controlled candidate/terminal/notice replies,
and asserts the notice text and offered mark. It failed on the original head
with `no notice row for this job`. Controlled-clock tests also cover an
immediate terminal result, one returned exactly at the poll deadline, and an
empty poll whose idle sleep must preserve the reserve.

### Latency measurements

Foreground timing runs started and stopped real `Daemon` instances on private
scratch roots, with real socket routing, worker pools and replies. The control
loop was held idle, and dispatch replies were controlled: the sessions command
used one interrupted scratch transcript and a controlled session listing;
`runs` read capabilities then list; status read lock and daemon.status; watch
sent two immediate empty polls; wait received one empty then one terminal poll.
Sessions sent state, nudged, then ping. Each sample used a fresh Python process through `compat.dispatch`, the actual
`subfleet` command entry point (a fresh client process for the watch pair),
reusing its client for the whole command. Medians below are seven samples per command, including process
startup. Release source came from local `git archive d72597b5c subfleet`; head
used this worktree. All measured head pacing sleeps were exactly **0 s**.

| Command | Head (s) | Release d72597b5c (s) | Delta (ms) | ps attempts head / release | Attempt cost head / release (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| sessions continue (3 calls) | 0.168 | 0.156 | 12.0 | 3 / 1 | 6.0 / 2.0 |
| runs (2 calls) | 0.151 | 0.152 | -1.0 | 2 / 1 | 4.9 / 2.6 |
| daemon status | 0.152 | 0.165 | -13.0 | 2 / 2 | 4.1 / 3.6 |
| conversation.watch (2 calls) | 0.083 | 0.089 | -5.8 | 2 / 1 | 4.4 / 2.0 |
| wait (2 polls) | 0.697 | 0.653 | 43.5 | 2 / 1 | 5.3 / 2.7 |

Every command is within 100 ms of release even before allowing for additional
process observations. Remaining differences include process startup, additional
fresh reads and host scheduling; no one-shot command paid a bucket interval.
These runs do **not** establish native process-inspection latency: this sandbox
denies execution of `/bin/ps`. The harness attempted and charged each actual
probe, then supplied the controlled running identity of its own scratch
daemon. A separate own-pid ps attempt returned `PermissionError` (errno 1) in
9.8 ms; it is not a successful native probe measurement. A successful native ps
comparison and the optional libproc feasibility study remain unrun. Every
scratch daemon was stopped and joined in `finally`, including during harness
setup corrections, and every scratch root was removed.

### Verification

Nine selected tests failed against the original production head in the
regressions-first hold (2.56 s pytest; 111.93 s including the lock wait): both
budget regressions, the expanded deadline property, both burst/failure-state
variants, the short hook command, both terminal-poll variants and idle reserve.
The first full runs found old spacing expectations and two fixture mistakes;
those expectations now exercise the new burst and exhausted-bucket behavior.
No production invariant was weakened to accommodate a test.

Final requested groups: **818 passed, 6 skipped**, no outstanding failures.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/unit/test_stopped_daemon.py` | 69 | 0 |
| `tests/unit/test_client_availability.py` | 22 | 0 |
| `tests/unit/test_boot_identity.py` | 10 | 0 |
| `tests/unit/test_cli.py` | 244 | 0 |
| `tests/unit/test_doctor.py` | 44 | 0 |
| `tests/unit/test_sessions_cli.py` | 142 | 0 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 5 |
| `tests/unit/test_hooks.py` | 72 | 1 |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |

| Final hold | Passed / skipped | Pytest seconds | Seconds including lock wait |
| --- | ---: | ---: | ---: |
| Stopped / availability / boot | 101 / 0 | 10.52 | 11.30 |
| CLI / doctor / sessions / suggest | 437 / 0 | 40.30 | 641.54 |
| Offline / refusals / daemon verbs / hooks / outcome / descriptors | 242 / 6 | 49.41 | 89.36 |
| State contract / module names | 38 / 0 | 3.43 | 16.57 |

Each pytest process ran serially in its own foreground `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout, cache
provider or xdist workers. The per-test watchdog was 90 s; no hold approached
20 minutes (the longest test runtime was under 50 s). All properties used
bounded settings; the three stopped-daemon properties each use 40 deterministic
generated examples with no database, and explicit regression examples. No deep
profile was run. Lock waits were allowed to finish; no pytest was relaunched
while a prior one remained running.

All three requested mutations were caught by their named tests, with one
expected failure in each separate hold:

| Mutation | Named failing test | Pytest seconds |
| --- | --- | ---: |
| Separate five-second per-stage budgets | `test_ping_observation_shares_boot_and_ps_budget[preflight]` (6.6 > 5.6 s) | 0.78 |
| Bucket capacity one | `test_reused_client_probes_are_paced[0-S Sun Sep 20 23:26:42 2026]` (three sleeps before fourth send) | 0.78 |
| Backoff before terminal check | `test_terminal_poll_keeps_delivery_reserve[immediate]` (slept .5 s on a terminal result) | 0.73 |

Each mutation restored the production file in `finally`, removing its generated
bytecode to prevent reuse of a mutated module. Git verified that both production
files match their committed bytes after all three restorations.
After restoration, the same three named tests passed in 0.83 s in one further
foreground shared-lock hold (1127.11 s including its acquisition wait).
These are repeat checks of cases counted above.
Syntax parsing of both production modules and all three changed test modules,
and `git diff --check`, pass.

**Skipped/unrun:** five native daemon start/stop tests require macOS boot and
process identity that the sandbox cannot read. The existing marked-hook command
integration case skips its native ps inspection after its output and mark
assertions pass. Actual stopped-child tests still stop, observe, continue, kill
and reap only their own children, using the existing waitpid seam when native
ps is denied. The complete review file, successful native process-inspection
timings, optional libproc study, and broader provider/native-process/whole-repo
suites were not run. Every requested test file was run. `CONFIG_NAMES` is absent
in both this worktree and the available `origin/release/217` source; #222's
conditional allowlist amendment and launch-environment/credential hold were
therefore not applicable, and no configuration allowlist was changed.

### Step commits and cleanup

- `08df39419`: regressions and properties first, before production changes.
- `49f24cf6d`: one whole-observation budget, shared call ceiling, four-token
  bucket, and C-5.13 amendment/change-log entry.
- `0500b095d`: terminal-first hook delivery, notice reserve, and C-15.2
  amendment/change-log entry.
- `0a5909572`, `e878c4653`: exhausted-bucket fixtures, slow-boot refill
  expectations and the CLI client seam.
- `ad0425d56`, `556dea2ad`: update the existing CLI freshness assertion and its
  sleep expectation to the four-token policy.
- The final documentation commit records this round; its hash is in the handoff.

All are workspace-local commits on the assigned branch, with the requested
co-author trailer. Source changes, tests and Markdown were staged by named path
only; no logs, JSON evidence, bundles or dependency directories were staged.

The tests used a workspace virtualenv with system pytest 8.4.2 and read-only
cached Hypothesis, with `PYTHONPATH` and `SUBFLEET_RETENTION_DORMANT` removed.
Because the default uv cache is outside writable roots, every hold used its
own temporary `UV_CACHE_DIR` and Hypothesis storage, with `UV_NO_SYNC=1` and
`UV_OFFLINE=1`. Every own scratch root and cache was removed with
`shutil.rmtree` in `finally`; no global cache was cleaned. The task virtualenv,
bytecode, helpers, XML/JSON timing evidence and any workspace Hypothesis output
were removed after verification. Workspace-local Git metadata is retained.
All foreground runners and owned lock holders exited. Scratch daemon threads
were stopped and joined, and fixture-owned child processes reaped. No live
daemon/store, unrelated process, network remote or global cache was changed,
signalled or cleaned.
