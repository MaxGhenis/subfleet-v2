# Stopped daemon and bounded stdin: #35 port to release/217

## Source and scope

Base: `3fbc4b7c5615b582f11b6f766f91125304818bd7` (`origin/release/217`). The requested `origin/pr/35` name is absent locally. Its preserved equivalent, `merge/pr35-stopped-daemon` at `749ccbeae36a42b6ce1e2a4a4a4c3b5ba7bab425`, has the expected seven commits after its merge base with `origin/main` and the expected eleven-file diff. Both history and the full diff were read locally; no network was used. This is a selective port to release's seams, not a cherry-pick.

`C-5.13` and `C-17.9` were both free. Added both clauses and the first bullet under “Changes in version 3”; updated the CLI lane brief.

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
- `a0ba9fbe6`: bounded stdin, suggestion submission and C-17.9.
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
- C-17.9 explicitly documents the existing size behavior: even regular files can be read up to **8 MiB + 1 byte** before rejection. No file-size pre-check was added.

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
C-5.13/C-17.9. Release's shutdown behavior remains intact.

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
resolution was necessary. No Git remote was contacted. The acceptance contract's
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
represents tokens with a virtual next-token time: ps may start when
`next_token - 3 * interval` is due, then advances that time by one interval
from the later of its current value and now. This is equivalent to four stored
tokens with one token refilled per second; idle time caps at four tokens, and
inspection time also contributes elapsed refill time. Round 5 moves consumption
to immediately before ps, after boot reads. Failed ps reads and
failure diagnostics use the same bucket. Every verifying send still receives
a fresh state/identity read; no running verdict is cached. C-15.6's explicit
hook opt-out remains.

Four immediate sends sleep zero seconds; the fifth waits for a token. A long
loop has rate at most one ps start per second after its burst. The bounded
property checks **every sub-window** of generated send sequences, including
repeated zero gaps, slow boot reads and long idle refills: actual ps starts
never exceed `4 + elapsed / HOLDER_PROBE_INTERVAL_S`. **Round-5 correction to
round 4's line 442:** counting whole observation starts allowed a 1.6 s boot
read to refill an already-spent token before the first ps, followed by five ps
starts at 1.6 s. Tokens are now spent after boot reads, immediately before ps.
The explicit regression observes four starts at 1.6 s and the fifth at 2.6 s;
the property varies boot costs independently of send gaps. A saturated refused-connect diagnostic still
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
1 s of work time. If the poll uses its whole share, about .5 s remains for
delivery and pending receives at most about .25 s. An immediate terminal reply
leaves about 1 s for delivery and pending receives at most about .5 s. Mark
gets the remaining time, about .25 s or .5 s respectively when lookup uses its
full share.
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


## Round 5 (2026-10-20)

### Release merge and workspace

Merged the existing local `origin/release/217` ref, `473fe2941`, including
#222, the `f99da54b0` launch-environment allowlist and later release merges.
No Git remote was contacted. Only `docs/acceptance-contract.md` conflicted. Both
sides' complete change-log entries were retained; the two C-17.9 additions
(stdin bounds and environment diagnostics) now share that clause as separate
paragraphs. All other clauses from both sides are retained. CLI and doctor
merged automatically.

The shared Git metadata rejected writing `ORIG_HEAD.lock` under the sandbox.
All round-5 commits therefore use `.git-local/` inside this workspace, on
`fix/stopped-daemon-217`, with a read-only alternate to the original objects.
The caller's checkout, branch and shared index are untouched. Nothing is pushed.

Validation and remaining work are recorded below as each step completes.

### Tests first and allowlist

Commit `d62ed3b4e` adds the review regressions before production fixes.
Against the merged head, the initial CI reproduction produced **178 passed,
7 failed**: launch-env round 3 66/1, hook cost 2/1, operator notices 75/4,
unadmittable pin rereview 5/1 (passed/failed); daemon connections passed 30.
The separate new-regression hold produced **6 passed, 3 failed**: the explicit
slow-boot case and the property both observed five ps starts at 1.6 s, and
the new launched-child test showed the missing ping setting. Settlement,
shared ceiling, late observation and both notice-split examples already pass
on the head; their new assertions are intended to kill the requested mutants.
These are regression coverage for existing protections, not claims that those
protections themselves were broken at the head.

`SUBFLEET_PING_STDIN_WAIT_S` is now in `CONFIG_NAMES`, alphabetically between
`SUBFLEET_NOTIFY_MODE` and `SUBFLEET_PROVIDER_QOS`. A two-case test follows both
it and `SUBFLEET_HOOK_TIMEOUT_S` through `cli.daemon_env`, provider environment
selection and an actual launched Python child, asserting the configured value
at every boundary. C-12.3/C-17.9 and the change log state that classification.

The first complete #222 hold ran 384 cases: **371 passed, 13 failed**.
Round 3 passed all 69, including both real-child cases and the package-name
scan. Eleven failures were old `_daemon_alive` stand-ins rejecting `timeout`;
two refused-roster tests mistook the new stopped-holder boot inspection for
a daemon spawn. All launch-env stand-ins now accept the deadline keyword.
The latter tests explicitly model the already-closed core's stopped-holder
preflight and retain their fatal assertion on any daemon launch. These are
fixture adaptations to C-5.13's intentional fresh preflight and deadline
propagation; production checks are retained. The full rerun passed, as
recorded below.

### Review r4 P3s and client behavior

1. Settlement: the regression spends two tokens before `Client(timeout=.1)`
   calls `call_settled('kill', requery_timeout=.2)` on a silent socket. The
   first send and its diagnosis exhaust the bucket. The resend's slot expires
   at .3 s before sending, so the test requires `OutcomeUnknown`, the original
   no-response reason, the "before sending" reason, and exactly one wire send.
2. Shared ceiling: a real socket replies busy, then its listener closes while
   its path remains. With `timeout=2`, two diagnoses each request a 1.6 s boot
   read plus 5 s ps; all work ends at 7.0 s on the deterministic observation clock (within the review's 7.006 s bound).
   Separate coverage rejects successful ps output observed after its absolute
   observation deadline. The terminal-hook tests now spend the full lookup
   allowance and assert that pending gets exactly half the delivery time,
   including immediate and end-of-poll replies. These strengthen tests for
   protections already implemented at the head.
3. Burst: boot inspection used to spend a token before ps existed, and the
   intervening boot read refilled it. `identity_status` now reserves through
   the client's callback immediately before reading ps. Budget exhaustion
   starts no ps, and waits retain the call and observation ceilings. Availability
   is awaited before boot reads without consuming the token, then rechecked
   and charged at ps; an expired slot starts no boot read either. The
   bounded property varies boot costs and send gaps and checks every time
   window of actual ps starts against 4 + elapsed/interval. C-5.13 and the
   earlier report's incorrect line 442 now state this process-start bound.
4. Hook wording: C-15.2 and this report distinguish pending's roughly .25 s
   allowance after a full poll from .5 s after an immediate terminal reply
   at `SUBFLEET_HOOK_TIMEOUT_S=6`; mark receives the time remaining.

Fresh **identity and state**, rather than state alone, remain required before
every verifying send: a reused pid's T state cannot establish that it is the
recorded daemon. Connection fixtures now provide matching boot/start
observations on both sides and remove obsolete `_checked` assignments.
The hook-cost failure did **not** show a connect-before-preflight reorder:
`check_available` still precedes socket creation. Its old `lock_report` mock
was bypassed by C-5.13's combined `holder_report`. The revised test supplies
a real stale record, spies on ps, and guarantees a too-long socket path,
requiring the stale message first. It also exposed a real opt-out regression:
failed connects called inspection even for `verify_lock=False`. They now
retain the opt-out, including refused-after-busy checks; a sent unanswered
request still gets C-5.13's diagnosis and can enable later preflight.

### CI behavior and caller repairs

- Pin-notice `_deliver`: the sole external direct caller now supplies its
  absolute deadline and monotonic clock. A repository-wide `_deliver(` search
  found no other missing caller; production already supplies both.
- Daemon connections: all six obsolete `_checked` assignments were removed.
  Their fixture now observes its fake boot/start consistently in the client
  and daemon; every real preflight check remains. This is a test adaptation
  to C-5.13's intentional fresh identity/state checks, not a production bypass.
- Hook cost: replace a superseded private-method mock with actual stale-lock
  input, verify the stale diagnostic precedes a guaranteed path-length error,
  and preserve no ps on opted-out refused connects (including busy refusals).
  Sent unanswered requests still retain their unknown-outcome diagnosis.
- Operator notices: the Direct stand-ins accept `timeout`. Empty/blank input
  failed because the finite stdin reader recognized StringIO and BytesIO but
  refused TextIOWrapper(BytesIO), the release tests' finite stream. That
  wrapper now reads its BytesIO buffer under the same byte cap and replacement
  decoding. New small/oversized/malformed-byte cases check the cap and decoder.
  Ping's unrequested queued-notice wording is restored to release's
  `parked for <recipient>` and reason. The daemon still chooses the operator
  inbox when no session is named, and empty/blank input still means liveness.
  The PR-added queued-output test now asserts the required release behavior;
  existing operator-notice behavior assertions are unchanged. C-17.9 and its
  change log record this correction.

The complete #222 rerun passed **384/384** (44.96 s of pytest time): unit
allowlist 27, helpers 20, mutations 41, policy 26, round 2 67, round 3 69,
round 4 40, round 5 39; fake daemon 49, fake providers 6.

The required client hold passed 103 cases (stopped daemon 71, availability 22,
boot identity 10). The required CLI hold passed 440 (CLI 247, doctor 44,
sessions CLI 142, suggest CLI 7). A subsequent self-review split slot
availability from token consumption: wait before boot reads, then recheck
and charge at ps. A new expired-slot regression requires no boot read. The
final client/CI hold and whole-directory runs below revalidate that addition.

The first required hook/offline/outcome hold produced **241 passed, 6 skipped,
1 failed** in 145.82 s. The Scripted transport in descriptor review fixes
set `verify_lock=False` while asserting a fresh dead-holder query after a
busy/refused retry. Its `_call_once` already bypasses preflight entirely.
It now retains the default verifying policy so that existing liveness
assertion remains meaningful; actual opted-out clients keep their C-15.6
no-inspection behavior. The combined final briefed-file hold rechecks this
fixture, all six required files, the client/CI cases and state/name checks.

The final combined briefed-file hold produced **502 passed, 6 skipped,
1 failed** in 522.54 s (8m42s, within the hold bound). All reported CI
regressions, the descriptor fixture correction, state contract (37), module
names (1), and the new expired-slot case passed. The one failure was the
PR's older fake-stop test's total CLI wall-time assertion: .239 s exceeded
its .15 s threshold even though it returned 69 and printed the correct
continue repair. C-5.13 bounds request/probe work and explicitly excludes
OS scheduling; CLI setup is outside the request deadline too. The test now
asserts the stronger intended invariant directly: no connection entered the
backlog and the controlled request clock spent zero time, retaining the
.2 s request timeout and the exact status/diagnostic assertions. This is a
test correction to C-5.13, not a production timing change. Its targeted rerun
is included as an unmutated case in the single mutation hold.

### Mutation proof

`tools/stopped_daemon_mutations.py` runs one pytest process under one lock
hold. For each named case it compiles exactly one source change and replaces
only that function's code for the call, preserving the test's actual module
globals, clocks and subprocess stand-ins; it restores the function before the
next case and never writes production sources. The command succeeds only
when all seven mutant cases fail, all five mutation types are represented,
and any additional unmutated selection passes.

The hold completed with **seven expected failures and one unmutated pass**
in 11.65 s. Inspection of the failure messages confirmed the intended cause
for every mutant, rather than an import, setup or harness error. The extra
pass was the corrected fake-stop/no-connection regression.

| Mutation | Named failing test(s) | Observed mutant failure |
| --- | --- | --- |
| Remove RequestTimeout from call_settled's resend exception handler | test_settlement_probe_wait_keeps_unknown_outcome | Bare RequestTimeout at .3 s instead of OutcomeUnknown; first wire send remains unanswered. |
| Remove the call-deadline + 5 cap | test_busy_then_refused_diagnoses_share_original_call_ceiling | 10.006 s on the controlled clock exceeds 7.006 s; unchanged head ends at 7.0 s. |
| Remove _run's late-observation rejection | test_late_ps_observation_cannot_verify_stopped_holder | A T+ read returned at 5.01 s incorrectly establishes alive=True. |
| Give notice.pending the whole remaining delivery budget | test_terminal_poll_keeps_delivery_reserve[immediate], [at-poll-deadline] | Pending receives 1.0 instead of .5 s, or .5 instead of .25 s. |
| Spend the token before boot reads | test_slow_boot_lookup_paces_actual_ps_starts, test_probe_bucket_rate_property | Five ps starts at 1.6 s; the fifth should be at 2.6 s, and the zero-duration window exceeds four. |

Reproduction inside the normal shared-lock/scratch environment:
`uv run python tools/stopped_daemon_mutations.py -q -p no:cacheprovider`.
The seven pytest failures are the proof; the harness exits zero only when
all mutants are killed. All production sources remain identical to their
committed versions after the hold.

### Broader validation and environment

All normal round-5 holds use Python 3.14.7, one foreground pytest process
under each shared-lock hold, isolated temporary/cache directories, no pytest
cache provider, and bounded Hypothesis settings. The default uv cache is
outside the writable sandbox, so the task uses its private scratch cache.
`PYTHONPATH` and `SUBFLEET_RETENTION_DORMANT` are removed from test launches.
No 3.12 pytest hold has run; the prepared interpreter environment alone is
not counted as validation.

The full Python 3.14 shard-4 mapping from `.github/test-shards.json` completed
in three holds: **959 passed / 23 skipped**, **1,217 passed / 3 skipped**,
and **1,589 passed / 7 skipped**. All **102 mapped files** ran, totaling
**3,765 passed, zero failed, 33 skipped**. Pytest durations were 309.85,
743.29 and 712.89 seconds, each below 20 minutes. No additional PR failure
appeared in this shard. Skips are unavailable native boot/process inspection
(30) and three `hdiutil create` failures (`Device not configured` for
case-sensitive HFS+, HFS+ and ExFAT). The earlier briefed-file holds also
skip native-inspection cases; they do not establish those native paths.
One shard-4 warning is an existing pytest deprecation for a `zip`
parametrization in `test_codex_turn_completed.py`.

The exact latest normal counts per file, directory totals, and distinction
between initial verification and the requested whole-directory sweep follow
in the final ledger. Expected mutation failures and initial failing
reproductions do not enter those totals. Shared-lock waiting time counts
against the task's wall clock; the prescribed no-new-holds cutoff is
11:22 UTC (four hours thirty minutes after the 06:52 UTC start).

### Final broader hold and outstanding scope

The final 63-file hold was invoked at 10:46 UTC, acquired the lock at
11:23:01 UTC after roughly 36 minutes waiting, and finished at 11:29 UTC:
**1,659 passed, four failed, 26 skipped, one xfailed**, in **405.74 s**
(6m45s). No further test invocation was started after the 11:22 UTC cutoff.
All foreground runners have completed. The expected xfail is the existing
P3-6 hung-filesystem bound case; the 26 skips require unavailable native
boot/process inspection.

The four failures are retained, not converted into skips or hidden by a
fixture change:

- `tests/unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos`:
  `kernel_open_files_ceiling()` returns `None`. Its unchanged implementation
  directly executes `/usr/sbin/sysctl -n kern.maxfilesperproc`; unreadable
  native inspection returns `None` by design.
- `tests/unit/test_importer.py::test_states_map_from_rc`: the reaped fixture
  pid remains `running` rather than `lost` when process inspection cannot
  establish that it is gone.
- `tests/unit/test_importer.py::test_the_daemon_never_adopts_an_external_run`:
  that same unverifiable reaped pid stays in the external live-attempt list.
- `tests/unit/test_importer.py::test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]`:
  `/bin/ps -Ewwp` cannot establish the scratch child's Subfleet marker, so the
  unchanged catalog treats ownership as unknown and holds history back.

Attribution to the sandbox is based on the observed unavailable native
inspection, these failure values and source-path inspection. Importer's
`_run_is_live` deliberately treats `procs.InspectionError` as live; catalog's
`_subfleet_owned` deliberately treats an unsuccessful ps read as unowned.
`subfleet/descriptors.py`, `importer.py`, `procs.py`,
`conversations/catalog.py`, `conversations/legacy.py`, `sessions/registry.py`
and both failing test files are byte-identical to `473fe2941`. These paths
use their own native readers, not the changed client probe path. A release
pytest comparison was not run after the cutoff, so this is an attribution
from the evidence rather than a claimed passing release rerun. No production
or test change was made for these four environment-dependent failures.

The requested whole-directory sweep is **incomplete**: **122/327 unit files**
and **37/106 fake files** were swept after briefed verification. Including
all briefed holds, **137 unit files** ran (**5,709 passed, four failed,
15 skipped**) and **40 fake files** ran (**685 passed, 27 skipped, one
xfailed**). **256 files were never run** (190 unit, 66 fake); **274 files**
still lack the requested full-sweep run, including some already verified in
the briefed holds. Every filename and latest count is listed below. Python
3.12 and the skipped native cases remain unverified.

### Commits, recovery and cleanup

Round-5 commits, in order:

- `63cbb04c7`: merge release, retaining both contract additions and change logs.
- `d62ed3b4e`: tests first, review regressions and deadline/identity caller fixtures.
- `5e4ad9de0`: classify and forward the ping timeout; adapt launch stand-ins.
- `1717ee7fc`: charge actual ps starts, preserve hook opt-outs, restore ping input/output and amend the contract.
- `e9eb8f8bd`: retain verifying policy in scripted busy/refusal diagnosis tests.
- `2805aa56c`: replace the flaky CLI wall threshold with no-send and controlled-clock assertions.
- `709ac8972`: seven named failures kill all five review mutation types.
- `c38a70f99`: complete shard-4 results and the initial broader-sweep ledger.
- The final report commit records the final broader hold, four native failures and exact outstanding scope.

All commits use the requested co-author trailer and workspace-local Git
metadata on `fix/stopped-daemon-217`. The recovery artifact is
`.git-local/round5.bundle`; its prerequisites are the existing
`58eb80d79882` base and `473fe2941` release commits. The bundle and local Git
metadata are retained inside the assigned workspace, outside tracked files.
Nothing was pushed and no history was rewritten.

All foreground test runners exited, and an `lsof` check found no open handles
to any task test log before cleanup. The private scratch tree, uv cache,
prepared 3.12 environment and workspace-created `.venv` have been removed
with `shutil.rmtree`. No root `.hypothesis` directory remains. No task
lockf holder is left running. The caller's checkout and shared index remain
untouched.

<!-- round5-validation-ledger -->

### Final validation ledger

One foreground pytest process per shared-lock hold, no cache provider; bounded Hypothesis settings. Per-file counts are the latest complete normal file run plus targeted rechecks. Expected mutation failures and initial failing reproductions are excluded from directory totals. Skipped includes xfail when present. The full-sweep column separately records the requested runs after briefed verification.

| Hold | Passed | Failed | Skipped | Pytest seconds |
| --- | ---: | ---: | ---: | ---: |
| baseline-ci | 178 | 7 | 0 | 96.59 |
| regressions-before | 6 | 3 | 0 | 5.63 |
| allowlist | 371 | 13 | 0 | 92.93 |
| allowlist-fixed | 384 | 0 | 0 | 44.96 |
| required-client | 103 | 0 | 0 | 14.18 |
| required-cli | 440 | 0 | 0 | 70.58 |
| required-hooks | 241 | 1 | 6 | 145.82 |
| required-final | 502 | 1 | 6 | 522.54 |
| review-mutants | 1 | 7 | 0 | 11.65 |
| full-suite-shard4-1 | 959 | 0 | 23 | 309.85 |
| full-suite-shard4-2 | 1217 | 0 | 3 | 743.29 |
| full-suite-shard4-3 | 1589 | 0 | 7 | 712.89 |
| full-suite-rest-1 | 1659 | 4 | 27 | 405.74 |

Initial reproductions and review-mutant failures are intentional evidence, described above. Later fixture corrections and rechecks resolve the PR failures described above. The four native-environment failures from the final broader hold remain in the totals.

| Directory | Files run | Passed | Failed | Skipped / xfail | Never run | Full-sweep files |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| tests/unit | 137/327 | 5709 | 4 | 15 | 190 | 122/327 |
| tests/fake | 40/106 | 685 | 0 | 28 | 66 | 37/106 |

Python 3.14 shard 4: 102/102 mapped files verified; 102/102 covered in the whole-file sweep.

Files never run:

- `tests/fake/test_admission_disk.py`
- `tests/fake/test_admission_latency.py`
- `tests/fake/test_admission_lease_waiters.py`
- `tests/fake/test_admission_liveness.py`
- `tests/fake/test_admission_priority.py`
- `tests/fake/test_admission_properties.py`
- `tests/fake/test_admission_ramp.py`
- `tests/fake/test_admission_ramp_retention.py`
- `tests/fake/test_admission_stateful.py`
- `tests/fake/test_admission_visibility.py`
- `tests/fake/test_canonical_identity.py`
- `tests/fake/test_canonical_identity_fix2.py`
- `tests/fake/test_canonical_identity_fix4.py`
- `tests/fake/test_canonical_identity_fix5.py`
- `tests/fake/test_capacity_wait_backoff.py`
- `tests/fake/test_census_escapers_daemon.py`
- `tests/fake/test_census_recycled_pid_daemon.py`
- `tests/fake/test_codex_ca_bundle_fake.py`
- `tests/fake/test_codex_turn_completed_fake.py`
- `tests/fake/test_crash_matrix.py`
- `tests/fake/test_daemon_descriptors.py`
- `tests/fake/test_daemon_launch.py`
- `tests/fake/test_daemon_stacks.py`
- `tests/fake/test_descriptor_exhaustion.py`
- `tests/fake/test_fake_provider.py`
- `tests/fake/test_finalization_replay.py`
- `tests/fake/test_folder_spelling.py`
- `tests/fake/test_frozen_stream_nonregular.py`
- `tests/fake/test_identity_rollback.py`
- `tests/fake/test_idle_cost.py`
- `tests/fake/test_lane_fault.py`
- `tests/fake/test_lane_pilot.py`
- `tests/fake/test_lanes_enroll.py`
- `tests/fake/test_lanes_reenroll.py`
- `tests/fake/test_lanes_reenroll_keychain.py`
- `tests/fake/test_launch_gates_above_stdio.py`
- `tests/fake/test_lost_acknowledgement.py`
- `tests/fake/test_native_maintenance_startup.py`
- `tests/fake/test_notice_delivery.py`
- `tests/fake/test_notice_header.py`
- `tests/fake/test_probe_recovery.py`
- `tests/fake/test_prompt_seam.py`
- `tests/fake/test_provider_active_cap_fake.py`
- `tests/fake/test_provider_cap_review_evidence.py`
- `tests/fake/test_record_secrecy.py`
- `tests/fake/test_reserve_authorization_validation.py`
- `tests/fake/test_resume_cli.py`
- `tests/fake/test_retention_dormant.py`
- `tests/fake/test_review_r185_adversarial.py`
- `tests/fake/test_route_view.py`
- `tests/fake/test_salvage_finalization.py`
- `tests/fake/test_sessions_end_to_end.py`
- `tests/fake/test_state_fixture_processes.py`
- `tests/fake/test_status_payload.py`
- `tests/fake/test_timers_busy_admission.py`
- `tests/fake/test_timers_end_to_end.py`
- `tests/fake/test_turn_retention_job_directory.py`
- `tests/fake/test_turn_retention_job_properties.py`
- `tests/fake/test_turn_retention_quarantine.py`
- `tests/fake/test_unadmittable_pin_admission.py`
- `tests/fake/test_unadmittable_pin_review.py`
- `tests/fake/test_workspace_contract.py`
- `tests/fake/test_workspace_enospc.py`
- `tests/fake/test_workspace_transient.py`
- `tests/fake/test_writable_fanout.py`
- `tests/fake/test_writer_retention_fence.py`
- `tests/unit/test_admission_uncapped.py`
- `tests/unit/test_agent_secret_credentials.py`
- `tests/unit/test_app_cutover_daemon.py`
- `tests/unit/test_approval_resolution.py`
- `tests/unit/test_capacity_steer_mutations.py`
- `tests/unit/test_capacity_view.py`
- `tests/unit/test_census_counted.py`
- `tests/unit/test_census_identity_uncertainty.py`
- `tests/unit/test_census_responsibility_identity.py`
- `tests/unit/test_census_responsibility_races.py`
- `tests/unit/test_census_survivors_review.py`
- `tests/unit/test_ci_shard_plugin.py`
- `tests/unit/test_claude_adapter.py`
- `tests/unit/test_claude_cards_wiring.py`
- `tests/unit/test_claude_conversation_mcp_scope.py`
- `tests/unit/test_claude_identity.py`
- `tests/unit/test_claude_mcp.py`
- `tests/unit/test_claude_usage.py`
- `tests/unit/test_claude_v1_parity.py`
- `tests/unit/test_cli_batch.py`
- `tests/unit/test_codex_adapter.py`
- `tests/unit/test_codex_ca_bundle_provider.py`
- `tests/unit/test_codex_probe.py`
- `tests/unit/test_codex_turn.py`
- `tests/unit/test_compat.py`
- `tests/unit/test_conversation_fifo_reads.py`
- `tests/unit/test_conversation_handoff_briefs.py`
- `tests/unit/test_conversation_handoff_dispatch_races.py`
- `tests/unit/test_conversation_history.py`
- `tests/unit/test_conversation_peers_attachments.py`
- `tests/unit/test_conversation_service.py`
- `tests/unit/test_conversation_state_reads.py`
- `tests/unit/test_conversation_store.py`
- `tests/unit/test_conversation_stream_assertion.py`
- `tests/unit/test_conversation_titles.py`
- `tests/unit/test_conversation_wakes.py`
- `tests/unit/test_daemon_fifo_reads.py`
- `tests/unit/test_daemon_init_release.py`
- `tests/unit/test_daemon_integration.py`
- `tests/unit/test_daemon_qos_tools.py`
- `tests/unit/test_daemon_settle.py`
- `tests/unit/test_decision_horizon.py`
- `tests/unit/test_descriptors_desktop.py`
- `tests/unit/test_desktop_account_cache.py`
- `tests/unit/test_desktop_ledger.py`
- `tests/unit/test_disk_admission.py`
- `tests/unit/test_disk_floor.py`
- `tests/unit/test_doctor_identity.py`
- `tests/unit/test_enrollment_runner.py`
- `tests/unit/test_fake_codex_http.py`
- `tests/unit/test_folders.py`
- `tests/unit/test_forget_paced.py`
- `tests/unit/test_fresh_until.py`
- `tests/unit/test_frozen_provider_streams.py`
- `tests/unit/test_gate_admission.py`
- `tests/unit/test_gate_merge.py`
- `tests/unit/test_gate_replay.py`
- `tests/unit/test_gate_revision.py`
- `tests/unit/test_gate_service.py`
- `tests/unit/test_guard_overlay.py`
- `tests/unit/test_guard_trust.py`
- `tests/unit/test_guardian.py`
- `tests/unit/test_handler_refusals.py`
- `tests/unit/test_hidden_environment.py`
- `tests/unit/test_host_push_admission.py`
- `tests/unit/test_host_push_build.py`
- `tests/unit/test_host_push_build_budget.py`
- `tests/unit/test_host_push_cli.py`
- `tests/unit/test_host_push_copy.py`
- `tests/unit/test_host_push_export.py`
- `tests/unit/test_host_push_metadata.py`
- `tests/unit/test_host_push_mutations.py`
- `tests/unit/test_host_push_quarantine_restart.py`
- `tests/unit/test_host_push_restart.py`
- `tests/unit/test_host_push_storage_recovery.py`
- `tests/unit/test_host_push_storage_retry_budget.py`
- `tests/unit/test_host_push_store_completion_pr183.py`
- `tests/unit/test_host_push_trees.py`
- `tests/unit/test_host_push_validated_top.py`
- `tests/unit/test_inspection_pacing_properties.py`
- `tests/unit/test_lane_answers.py`
- `tests/unit/test_lease_waiter_selection.py`
- `tests/unit/test_limit_from_model_text.py`
- `tests/unit/test_live_store_guard_round3.py`
- `tests/unit/test_live_store_guard_round4.py`
- `tests/unit/test_live_store_guard_round5.py`
- `tests/unit/test_live_store_guard_round6.py`
- `tests/unit/test_live_store_guard_round7_mutations.py`
- `tests/unit/test_live_store_guard_round8.py`
- `tests/unit/test_live_store_guard_round8_mutations.py`
- `tests/unit/test_live_store_guard_round9.py`
- `tests/unit/test_live_store_guard_state_root.py`
- `tests/unit/test_mcp_scheduler_contract.py`
- `tests/unit/test_mirror_flags_model.py`
- `tests/unit/test_native_operations.py`
- `tests/unit/test_notify_push.py`
- `tests/unit/test_operator_hold_closures.py`
- `tests/unit/test_person_boundary.py`
- `tests/unit/test_picker.py`
- `tests/unit/test_policy_disk.py`
- `tests/unit/test_policy_ramp.py`
- `tests/unit/test_policy_support.py`
- `tests/unit/test_policy_tier_preferences.py`
- `tests/unit/test_pr124_fixes.py`
- `tests/unit/test_pr124_review_r2_fixes.py`
- `tests/unit/test_pr192_review_r1.py`
- `tests/unit/test_pr192_review_r2.py`
- `tests/unit/test_procs.py`
- `tests/unit/test_protocol_secrecy.py`
- `tests/unit/test_prune_217.py`
- `tests/unit/test_prune_decision_fingerprints.py`
- `tests/unit/test_prune_restore.py`
- `tests/unit/test_quota_projection.py`
- `tests/unit/test_rank_review_report.py`
- `tests/unit/test_relay.py`
- `tests/unit/test_render.py`
- `tests/unit/test_render_quota_projection.py`
- `tests/unit/test_reserve_authorization_plumbing.py`
- `tests/unit/test_reset_credit_claim_timing.py`
- `tests/unit/test_reset_credit_properties.py`
- `tests/unit/test_reset_credit_review_r3.py`
- `tests/unit/test_reset_credit_review_r3_mutants.py`
- `tests/unit/test_retention_archive.py`
- `tests/unit/test_retention_case_alias.py`
- `tests/unit/test_retention_nested_leases.py`
- `tests/unit/test_retention_ordinary_trees.py`
- `tests/unit/test_retention_qos.py`
- `tests/unit/test_retention_readback.py`
- `tests/unit/test_retention_rediscovery.py`
- `tests/unit/test_retention_regenerable.py`
- `tests/unit/test_retention_salvage_properties.py`
- `tests/unit/test_retention_shared_folders.py`
- `tests/unit/test_retention_turn_contract.py`
- `tests/unit/test_retention_turn_workdirs.py`
- `tests/unit/test_retention_turns.py`
- `tests/unit/test_retention_worktrees.py`
- `tests/unit/test_review130_pass.py`
- `tests/unit/test_review_pr127_fixes.py`
- `tests/unit/test_review_pr127_report.py`
- `tests/unit/test_review_pr127_service.py`
- `tests/unit/test_review_r3_repro.py`
- `tests/unit/test_review_r4_repro.py`
- `tests/unit/test_review_r5_repro.py`
- `tests/unit/test_review_r6_probes.py`
- `tests/unit/test_review_r6_restart.py`
- `tests/unit/test_round10_refusal_context.py`
- `tests/unit/test_salvage.py`
- `tests/unit/test_salvage_optional_transient.py`
- `tests/unit/test_salvage_unindexable.py`
- `tests/unit/test_scheduler_priority_callers.py`
- `tests/unit/test_sessions_handoff_excerpts.py`
- `tests/unit/test_sessions_headless_properties.py`
- `tests/unit/test_sessions_mirror.py`
- `tests/unit/test_sessions_mirror_cache_coherence.py`
- `tests/unit/test_sessions_mirror_fifo.py`
- `tests/unit/test_sessions_mirror_hot_health.py`
- `tests/unit/test_sessions_mirror_hot_progress.py`
- `tests/unit/test_sessions_mirror_hot_regressions.py`
- `tests/unit/test_sessions_mirror_inventory.py`
- `tests/unit/test_sessions_mirror_load_gap.py`
- `tests/unit/test_sessions_mirror_safety.py`
- `tests/unit/test_sessions_mirror_write_churn.py`
- `tests/unit/test_sessions_nudge.py`
- `tests/unit/test_sessions_revive_reads.py`
- `tests/unit/test_sessions_transcripts.py`
- `tests/unit/test_settle_properties.py`
- `tests/unit/test_status_json.py`
- `tests/unit/test_stop_alarm.py`
- `tests/unit/test_stop_watchdog.py`
- `tests/unit/test_store_migration_2.py`
- `tests/unit/test_store_properties.py`
- `tests/unit/test_store_readers.py`
- `tests/unit/test_suggest_hint.py`
- `tests/unit/test_suggestion_error_boundaries.py`
- `tests/unit/test_suggestions_r1b_adversarial.py`
- `tests/unit/test_suggestions_r3_channels.py`
- `tests/unit/test_suggestions_review_multiline.py`
- `tests/unit/test_suggestions_review_response.py`
- `tests/unit/test_tick_prefilter.py`
- `tests/unit/test_timers_alerts.py`
- `tests/unit/test_timers_probe.py`
- `tests/unit/test_timers_reset_credits.py`
- `tests/unit/test_title_review_regressions.py`
- `tests/unit/test_turn_attachment_replay.py`
- `tests/unit/test_turn_replay.py`
- `tests/unit/test_turn_runner.py`
- `tests/unit/test_ultracode_effort.py`
- `tests/unit/test_wait_hub.py`
- `tests/unit/test_wake_cli.py`
- `tests/unit/test_weekly_rank_vs_base.py`

Full-sweep files still pending (some were already verified in the briefed holds):

- `tests/fake/test_admission_disk.py`
- `tests/fake/test_admission_latency.py`
- `tests/fake/test_admission_lease_waiters.py`
- `tests/fake/test_admission_liveness.py`
- `tests/fake/test_admission_priority.py`
- `tests/fake/test_admission_properties.py`
- `tests/fake/test_admission_ramp.py`
- `tests/fake/test_admission_ramp_retention.py`
- `tests/fake/test_admission_stateful.py`
- `tests/fake/test_admission_visibility.py`
- `tests/fake/test_canonical_identity.py`
- `tests/fake/test_canonical_identity_fix2.py`
- `tests/fake/test_canonical_identity_fix4.py`
- `tests/fake/test_canonical_identity_fix5.py`
- `tests/fake/test_capacity_wait_backoff.py`
- `tests/fake/test_census_escapers_daemon.py`
- `tests/fake/test_census_recycled_pid_daemon.py`
- `tests/fake/test_codex_ca_bundle_fake.py`
- `tests/fake/test_codex_turn_completed_fake.py`
- `tests/fake/test_crash_matrix.py`
- `tests/fake/test_daemon_descriptors.py`
- `tests/fake/test_daemon_launch.py`
- `tests/fake/test_daemon_stacks.py`
- `tests/fake/test_descriptor_exhaustion.py`
- `tests/fake/test_fake_provider.py`
- `tests/fake/test_finalization_replay.py`
- `tests/fake/test_folder_spelling.py`
- `tests/fake/test_frozen_stream_nonregular.py`
- `tests/fake/test_identity_rollback.py`
- `tests/fake/test_idle_cost.py`
- `tests/fake/test_lane_fault.py`
- `tests/fake/test_lane_pilot.py`
- `tests/fake/test_lanes_enroll.py`
- `tests/fake/test_lanes_reenroll.py`
- `tests/fake/test_lanes_reenroll_keychain.py`
- `tests/fake/test_launch_env_daemon.py`
- `tests/fake/test_launch_env_providers.py`
- `tests/fake/test_launch_gates_above_stdio.py`
- `tests/fake/test_lost_acknowledgement.py`
- `tests/fake/test_native_maintenance_startup.py`
- `tests/fake/test_notice_delivery.py`
- `tests/fake/test_notice_header.py`
- `tests/fake/test_probe_recovery.py`
- `tests/fake/test_prompt_seam.py`
- `tests/fake/test_provider_active_cap_fake.py`
- `tests/fake/test_provider_cap_review_evidence.py`
- `tests/fake/test_record_secrecy.py`
- `tests/fake/test_reserve_authorization_validation.py`
- `tests/fake/test_resume_cli.py`
- `tests/fake/test_retention_dormant.py`
- `tests/fake/test_review_r185_adversarial.py`
- `tests/fake/test_route_view.py`
- `tests/fake/test_salvage_finalization.py`
- `tests/fake/test_sessions_end_to_end.py`
- `tests/fake/test_state_fixture_processes.py`
- `tests/fake/test_status_payload.py`
- `tests/fake/test_timers_busy_admission.py`
- `tests/fake/test_timers_end_to_end.py`
- `tests/fake/test_turn_retention_job_directory.py`
- `tests/fake/test_turn_retention_job_properties.py`
- `tests/fake/test_turn_retention_quarantine.py`
- `tests/fake/test_unadmittable_pin_admission.py`
- `tests/fake/test_unadmittable_pin_rereview.py`
- `tests/fake/test_unadmittable_pin_review.py`
- `tests/fake/test_workspace_contract.py`
- `tests/fake/test_workspace_enospc.py`
- `tests/fake/test_workspace_transient.py`
- `tests/fake/test_writable_fanout.py`
- `tests/fake/test_writer_retention_fence.py`
- `tests/unit/test_admission_uncapped.py`
- `tests/unit/test_agent_secret_credentials.py`
- `tests/unit/test_app_cutover_daemon.py`
- `tests/unit/test_approval_resolution.py`
- `tests/unit/test_boot_identity.py`
- `tests/unit/test_capacity_steer_mutations.py`
- `tests/unit/test_capacity_view.py`
- `tests/unit/test_census_counted.py`
- `tests/unit/test_census_identity_uncertainty.py`
- `tests/unit/test_census_responsibility_identity.py`
- `tests/unit/test_census_responsibility_races.py`
- `tests/unit/test_census_survivors_review.py`
- `tests/unit/test_ci_shard_plugin.py`
- `tests/unit/test_claude_adapter.py`
- `tests/unit/test_claude_cards_wiring.py`
- `tests/unit/test_claude_conversation_mcp_scope.py`
- `tests/unit/test_claude_identity.py`
- `tests/unit/test_claude_mcp.py`
- `tests/unit/test_claude_usage.py`
- `tests/unit/test_claude_v1_parity.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cli_batch.py`
- `tests/unit/test_codex_adapter.py`
- `tests/unit/test_codex_ca_bundle_provider.py`
- `tests/unit/test_codex_probe.py`
- `tests/unit/test_codex_turn.py`
- `tests/unit/test_compat.py`
- `tests/unit/test_conversation_fifo_reads.py`
- `tests/unit/test_conversation_handoff_briefs.py`
- `tests/unit/test_conversation_handoff_dispatch_races.py`
- `tests/unit/test_conversation_history.py`
- `tests/unit/test_conversation_peers_attachments.py`
- `tests/unit/test_conversation_service.py`
- `tests/unit/test_conversation_state_reads.py`
- `tests/unit/test_conversation_store.py`
- `tests/unit/test_conversation_stream_assertion.py`
- `tests/unit/test_conversation_titles.py`
- `tests/unit/test_conversation_wakes.py`
- `tests/unit/test_daemon_connections.py`
- `tests/unit/test_daemon_fifo_reads.py`
- `tests/unit/test_daemon_init_release.py`
- `tests/unit/test_daemon_integration.py`
- `tests/unit/test_daemon_qos_tools.py`
- `tests/unit/test_daemon_settle.py`
- `tests/unit/test_decision_horizon.py`
- `tests/unit/test_descriptors_desktop.py`
- `tests/unit/test_desktop_account_cache.py`
- `tests/unit/test_desktop_ledger.py`
- `tests/unit/test_disk_admission.py`
- `tests/unit/test_disk_floor.py`
- `tests/unit/test_doctor.py`
- `tests/unit/test_doctor_identity.py`
- `tests/unit/test_enrollment_runner.py`
- `tests/unit/test_fake_codex_http.py`
- `tests/unit/test_folders.py`
- `tests/unit/test_forget_paced.py`
- `tests/unit/test_fresh_until.py`
- `tests/unit/test_frozen_provider_streams.py`
- `tests/unit/test_gate_admission.py`
- `tests/unit/test_gate_merge.py`
- `tests/unit/test_gate_replay.py`
- `tests/unit/test_gate_revision.py`
- `tests/unit/test_gate_service.py`
- `tests/unit/test_guard_overlay.py`
- `tests/unit/test_guard_trust.py`
- `tests/unit/test_guardian.py`
- `tests/unit/test_handler_refusals.py`
- `tests/unit/test_hidden_environment.py`
- `tests/unit/test_hook_cost.py`
- `tests/unit/test_hooks.py`
- `tests/unit/test_host_push_admission.py`
- `tests/unit/test_host_push_build.py`
- `tests/unit/test_host_push_build_budget.py`
- `tests/unit/test_host_push_cli.py`
- `tests/unit/test_host_push_copy.py`
- `tests/unit/test_host_push_export.py`
- `tests/unit/test_host_push_metadata.py`
- `tests/unit/test_host_push_mutations.py`
- `tests/unit/test_host_push_quarantine_restart.py`
- `tests/unit/test_host_push_restart.py`
- `tests/unit/test_host_push_storage_recovery.py`
- `tests/unit/test_host_push_storage_retry_budget.py`
- `tests/unit/test_host_push_store_completion_pr183.py`
- `tests/unit/test_host_push_trees.py`
- `tests/unit/test_host_push_validated_top.py`
- `tests/unit/test_inspection_pacing_properties.py`
- `tests/unit/test_lane_answers.py`
- `tests/unit/test_launch_env_allowlist.py`
- `tests/unit/test_launch_env_mutations.py`
- `tests/unit/test_launch_env_policy.py`
- `tests/unit/test_launch_env_round2.py`
- `tests/unit/test_launch_env_round3.py`
- `tests/unit/test_launch_env_round4.py`
- `tests/unit/test_lease_waiter_selection.py`
- `tests/unit/test_limit_from_model_text.py`
- `tests/unit/test_live_store_guard_round3.py`
- `tests/unit/test_live_store_guard_round4.py`
- `tests/unit/test_live_store_guard_round5.py`
- `tests/unit/test_live_store_guard_round6.py`
- `tests/unit/test_live_store_guard_round7_mutations.py`
- `tests/unit/test_live_store_guard_round8.py`
- `tests/unit/test_live_store_guard_round8_mutations.py`
- `tests/unit/test_live_store_guard_round9.py`
- `tests/unit/test_live_store_guard_state_root.py`
- `tests/unit/test_mcp_scheduler_contract.py`
- `tests/unit/test_mirror_flags_model.py`
- `tests/unit/test_native_operations.py`
- `tests/unit/test_notify_push.py`
- `tests/unit/test_operator_hold_closures.py`
- `tests/unit/test_outcome_unknown.py`
- `tests/unit/test_person_boundary.py`
- `tests/unit/test_picker.py`
- `tests/unit/test_policy_disk.py`
- `tests/unit/test_policy_ramp.py`
- `tests/unit/test_policy_support.py`
- `tests/unit/test_policy_tier_preferences.py`
- `tests/unit/test_pr124_fixes.py`
- `tests/unit/test_pr124_review_r2_fixes.py`
- `tests/unit/test_pr192_review_r1.py`
- `tests/unit/test_pr192_review_r2.py`
- `tests/unit/test_procs.py`
- `tests/unit/test_protocol_secrecy.py`
- `tests/unit/test_prune_217.py`
- `tests/unit/test_prune_decision_fingerprints.py`
- `tests/unit/test_prune_restore.py`
- `tests/unit/test_quota_projection.py`
- `tests/unit/test_rank_review_report.py`
- `tests/unit/test_relay.py`
- `tests/unit/test_render.py`
- `tests/unit/test_render_quota_projection.py`
- `tests/unit/test_reserve_authorization_plumbing.py`
- `tests/unit/test_reset_credit_claim_timing.py`
- `tests/unit/test_reset_credit_properties.py`
- `tests/unit/test_reset_credit_review_r3.py`
- `tests/unit/test_reset_credit_review_r3_mutants.py`
- `tests/unit/test_retention_archive.py`
- `tests/unit/test_retention_case_alias.py`
- `tests/unit/test_retention_nested_leases.py`
- `tests/unit/test_retention_ordinary_trees.py`
- `tests/unit/test_retention_qos.py`
- `tests/unit/test_retention_readback.py`
- `tests/unit/test_retention_rediscovery.py`
- `tests/unit/test_retention_regenerable.py`
- `tests/unit/test_retention_salvage_properties.py`
- `tests/unit/test_retention_shared_folders.py`
- `tests/unit/test_retention_turn_contract.py`
- `tests/unit/test_retention_turn_workdirs.py`
- `tests/unit/test_retention_turns.py`
- `tests/unit/test_retention_worktrees.py`
- `tests/unit/test_review130_pass.py`
- `tests/unit/test_review_pr127_fixes.py`
- `tests/unit/test_review_pr127_report.py`
- `tests/unit/test_review_pr127_service.py`
- `tests/unit/test_review_r3_repro.py`
- `tests/unit/test_review_r4_repro.py`
- `tests/unit/test_review_r5_repro.py`
- `tests/unit/test_review_r6_probes.py`
- `tests/unit/test_review_r6_restart.py`
- `tests/unit/test_round10_refusal_context.py`
- `tests/unit/test_salvage.py`
- `tests/unit/test_salvage_optional_transient.py`
- `tests/unit/test_salvage_unindexable.py`
- `tests/unit/test_scheduler_priority_callers.py`
- `tests/unit/test_sessions_cli.py`
- `tests/unit/test_sessions_handoff_excerpts.py`
- `tests/unit/test_sessions_headless_properties.py`
- `tests/unit/test_sessions_mirror.py`
- `tests/unit/test_sessions_mirror_cache_coherence.py`
- `tests/unit/test_sessions_mirror_fifo.py`
- `tests/unit/test_sessions_mirror_hot_health.py`
- `tests/unit/test_sessions_mirror_hot_progress.py`
- `tests/unit/test_sessions_mirror_hot_regressions.py`
- `tests/unit/test_sessions_mirror_inventory.py`
- `tests/unit/test_sessions_mirror_load_gap.py`
- `tests/unit/test_sessions_mirror_safety.py`
- `tests/unit/test_sessions_mirror_write_churn.py`
- `tests/unit/test_sessions_nudge.py`
- `tests/unit/test_sessions_revive_reads.py`
- `tests/unit/test_sessions_transcripts.py`
- `tests/unit/test_settle_properties.py`
- `tests/unit/test_status_json.py`
- `tests/unit/test_stop_alarm.py`
- `tests/unit/test_stop_watchdog.py`
- `tests/unit/test_stopped_daemon.py`
- `tests/unit/test_store_migration_2.py`
- `tests/unit/test_store_properties.py`
- `tests/unit/test_store_readers.py`
- `tests/unit/test_suggest_hint.py`
- `tests/unit/test_suggestion_error_boundaries.py`
- `tests/unit/test_suggestions_r1b_adversarial.py`
- `tests/unit/test_suggestions_r3_channels.py`
- `tests/unit/test_suggestions_review_multiline.py`
- `tests/unit/test_suggestions_review_response.py`
- `tests/unit/test_tick_prefilter.py`
- `tests/unit/test_timers_alerts.py`
- `tests/unit/test_timers_probe.py`
- `tests/unit/test_timers_reset_credits.py`
- `tests/unit/test_title_review_regressions.py`
- `tests/unit/test_turn_attachment_replay.py`
- `tests/unit/test_turn_replay.py`
- `tests/unit/test_turn_runner.py`
- `tests/unit/test_ultracode_effort.py`
- `tests/unit/test_wait_hub.py`
- `tests/unit/test_wake_cli.py`
- `tests/unit/test_weekly_rank_vs_base.py`

#### tests/unit: per-file counts

| File | Passed | Failed | Skipped | Full sweep |
| --- | ---: | ---: | ---: | --- |
| [test_admission_uncapped.py](../../tests/unit/test_admission_uncapped.py) | 0 | 0 | 0 | unrun |
| [test_agent_secret_credentials.py](../../tests/unit/test_agent_secret_credentials.py) | 0 | 0 | 0 | unrun |
| [test_app_cutover_daemon.py](../../tests/unit/test_app_cutover_daemon.py) | 0 | 0 | 0 | unrun |
| [test_approval_commit.py](../../tests/unit/test_approval_commit.py) | 6 | 0 | 0 | done |
| [test_approval_resolution.py](../../tests/unit/test_approval_resolution.py) | 0 | 0 | 0 | unrun |
| [test_boot_identity.py](../../tests/unit/test_boot_identity.py) | 10 | 0 | 0 | pending |
| [test_capacity_desktop.py](../../tests/unit/test_capacity_desktop.py) | 20 | 0 | 0 | done |
| [test_capacity_steer_mutations.py](../../tests/unit/test_capacity_steer_mutations.py) | 0 | 0 | 0 | unrun |
| [test_capacity_steer_route.py](../../tests/unit/test_capacity_steer_route.py) | 22 | 0 | 0 | done |
| [test_capacity_view.py](../../tests/unit/test_capacity_view.py) | 0 | 0 | 0 | unrun |
| [test_census_counted.py](../../tests/unit/test_census_counted.py) | 0 | 0 | 0 | unrun |
| [test_census_identity_uncertainty.py](../../tests/unit/test_census_identity_uncertainty.py) | 0 | 0 | 0 | unrun |
| [test_census_procargs.py](../../tests/unit/test_census_procargs.py) | 52 | 0 | 0 | done |
| [test_census_recycled_pid.py](../../tests/unit/test_census_recycled_pid.py) | 25 | 0 | 0 | done |
| [test_census_responsibility_failures.py](../../tests/unit/test_census_responsibility_failures.py) | 20 | 0 | 0 | done |
| [test_census_responsibility_identity.py](../../tests/unit/test_census_responsibility_identity.py) | 0 | 0 | 0 | unrun |
| [test_census_responsibility_races.py](../../tests/unit/test_census_responsibility_races.py) | 0 | 0 | 0 | unrun |
| [test_census_survivors_review.py](../../tests/unit/test_census_survivors_review.py) | 0 | 0 | 0 | unrun |
| [test_ci_retry_known_flakes.py](../../tests/unit/test_ci_retry_known_flakes.py) | 72 | 0 | 0 | done |
| [test_ci_shard_plugin.py](../../tests/unit/test_ci_shard_plugin.py) | 0 | 0 | 0 | unrun |
| [test_claude_adapter.py](../../tests/unit/test_claude_adapter.py) | 0 | 0 | 0 | unrun |
| [test_claude_attest.py](../../tests/unit/test_claude_attest.py) | 19 | 0 | 0 | done |
| [test_claude_cards.py](../../tests/unit/test_claude_cards.py) | 96 | 0 | 0 | done |
| [test_claude_cards_wiring.py](../../tests/unit/test_claude_cards_wiring.py) | 0 | 0 | 0 | unrun |
| [test_claude_conversation_mcp_scope.py](../../tests/unit/test_claude_conversation_mcp_scope.py) | 0 | 0 | 0 | unrun |
| [test_claude_identity.py](../../tests/unit/test_claude_identity.py) | 0 | 0 | 0 | unrun |
| [test_claude_large_stream.py](../../tests/unit/test_claude_large_stream.py) | 30 | 0 | 0 | done |
| [test_claude_mcp.py](../../tests/unit/test_claude_mcp.py) | 0 | 0 | 0 | unrun |
| [test_claude_mcp_launch.py](../../tests/unit/test_claude_mcp_launch.py) | 11 | 0 | 0 | done |
| [test_claude_stream.py](../../tests/unit/test_claude_stream.py) | 60 | 0 | 0 | done |
| [test_claude_synthetic_error.py](../../tests/unit/test_claude_synthetic_error.py) | 63 | 0 | 0 | done |
| [test_claude_turn.py](../../tests/unit/test_claude_turn.py) | 61 | 0 | 0 | done |
| [test_claude_usage.py](../../tests/unit/test_claude_usage.py) | 0 | 0 | 0 | unrun |
| [test_claude_v1_parity.py](../../tests/unit/test_claude_v1_parity.py) | 0 | 0 | 0 | unrun |
| [test_cli.py](../../tests/unit/test_cli.py) | 247 | 0 | 0 | pending |
| [test_cli_batch.py](../../tests/unit/test_cli_batch.py) | 0 | 0 | 0 | unrun |
| [test_cli_turns.py](../../tests/unit/test_cli_turns.py) | 23 | 0 | 0 | done |
| [test_client_availability.py](../../tests/unit/test_client_availability.py) | 22 | 0 | 0 | done |
| [test_codex_adapter.py](../../tests/unit/test_codex_adapter.py) | 0 | 0 | 0 | unrun |
| [test_codex_ca_bundle.py](../../tests/unit/test_codex_ca_bundle.py) | 46 | 0 | 0 | done |
| [test_codex_ca_bundle_provider.py](../../tests/unit/test_codex_ca_bundle_provider.py) | 0 | 0 | 0 | unrun |
| [test_codex_probe.py](../../tests/unit/test_codex_probe.py) | 0 | 0 | 0 | unrun |
| [test_codex_shim.py](../../tests/unit/test_codex_shim.py) | 31 | 0 | 0 | done |
| [test_codex_turn.py](../../tests/unit/test_codex_turn.py) | 0 | 0 | 0 | unrun |
| [test_codex_turn_completed.py](../../tests/unit/test_codex_turn_completed.py) | 41 | 0 | 0 | done |
| [test_compat.py](../../tests/unit/test_compat.py) | 0 | 0 | 0 | unrun |
| [test_continuation_r2.py](../../tests/unit/test_continuation_r2.py) | 11 | 0 | 0 | done |
| [test_conversation_catalog.py](../../tests/unit/test_conversation_catalog.py) | 15 | 0 | 0 | done |
| [test_conversation_catalog_lifecycle.py](../../tests/unit/test_conversation_catalog_lifecycle.py) | 21 | 0 | 0 | done |
| [test_conversation_connections.py](../../tests/unit/test_conversation_connections.py) | 9 | 0 | 0 | done |
| [test_conversation_diff.py](../../tests/unit/test_conversation_diff.py) | 39 | 0 | 0 | done |
| [test_conversation_fifo_reads.py](../../tests/unit/test_conversation_fifo_reads.py) | 0 | 0 | 0 | unrun |
| [test_conversation_handoff.py](../../tests/unit/test_conversation_handoff.py) | 48 | 0 | 0 | done |
| [test_conversation_handoff_briefs.py](../../tests/unit/test_conversation_handoff_briefs.py) | 0 | 0 | 0 | unrun |
| [test_conversation_handoff_dispatch_races.py](../../tests/unit/test_conversation_handoff_dispatch_races.py) | 0 | 0 | 0 | unrun |
| [test_conversation_history.py](../../tests/unit/test_conversation_history.py) | 0 | 0 | 0 | unrun |
| [test_conversation_peers_attachments.py](../../tests/unit/test_conversation_peers_attachments.py) | 0 | 0 | 0 | unrun |
| [test_conversation_reconcile.py](../../tests/unit/test_conversation_reconcile.py) | 36 | 0 | 0 | done |
| [test_conversation_redact.py](../../tests/unit/test_conversation_redact.py) | 9 | 0 | 0 | done |
| [test_conversation_service.py](../../tests/unit/test_conversation_service.py) | 0 | 0 | 0 | unrun |
| [test_conversation_state_reads.py](../../tests/unit/test_conversation_state_reads.py) | 0 | 0 | 0 | unrun |
| [test_conversation_status.py](../../tests/unit/test_conversation_status.py) | 9 | 0 | 0 | done |
| [test_conversation_steer_service.py](../../tests/unit/test_conversation_steer_service.py) | 45 | 0 | 0 | done |
| [test_conversation_store.py](../../tests/unit/test_conversation_store.py) | 0 | 0 | 0 | unrun |
| [test_conversation_stream_assertion.py](../../tests/unit/test_conversation_stream_assertion.py) | 0 | 0 | 0 | unrun |
| [test_conversation_titles.py](../../tests/unit/test_conversation_titles.py) | 0 | 0 | 0 | unrun |
| [test_conversation_waits.py](../../tests/unit/test_conversation_waits.py) | 9 | 0 | 0 | done |
| [test_conversation_wakes.py](../../tests/unit/test_conversation_wakes.py) | 0 | 0 | 0 | unrun |
| [test_cpu_scaling.py](../../tests/unit/test_cpu_scaling.py) | 2 | 0 | 0 | done |
| [test_daemon_accept.py](../../tests/unit/test_daemon_accept.py) | 5 | 0 | 0 | done |
| [test_daemon_connections.py](../../tests/unit/test_daemon_connections.py) | 30 | 0 | 0 | pending |
| [test_daemon_fifo_reads.py](../../tests/unit/test_daemon_fifo_reads.py) | 0 | 0 | 0 | unrun |
| [test_daemon_init_release.py](../../tests/unit/test_daemon_init_release.py) | 0 | 0 | 0 | unrun |
| [test_daemon_inspection_load.py](../../tests/unit/test_daemon_inspection_load.py) | 22 | 0 | 0 | done |
| [test_daemon_inspection_properties.py](../../tests/unit/test_daemon_inspection_properties.py) | 4 | 0 | 0 | done |
| [test_daemon_integration.py](../../tests/unit/test_daemon_integration.py) | 0 | 0 | 0 | unrun |
| [test_daemon_log_scrubbing.py](../../tests/unit/test_daemon_log_scrubbing.py) | 86 | 0 | 0 | done |
| [test_daemon_qos_tools.py](../../tests/unit/test_daemon_qos_tools.py) | 0 | 0 | 0 | unrun |
| [test_daemon_refusals_cli.py](../../tests/unit/test_daemon_refusals_cli.py) | 8 | 0 | 0 | done |
| [test_daemon_settle.py](../../tests/unit/test_daemon_settle.py) | 0 | 0 | 0 | unrun |
| [test_daemon_verbs.py](../../tests/unit/test_daemon_verbs.py) | 15 | 0 | 5 | done |
| [test_decision_horizon.py](../../tests/unit/test_decision_horizon.py) | 0 | 0 | 0 | unrun |
| [test_descriptor_review_fixes.py](../../tests/unit/test_descriptor_review_fixes.py) | 56 | 0 | 0 | done |
| [test_descriptors.py](../../tests/unit/test_descriptors.py) | 15 | 1 | 0 | done |
| [test_descriptors_desktop.py](../../tests/unit/test_descriptors_desktop.py) | 0 | 0 | 0 | unrun |
| [test_desktop_account_cache.py](../../tests/unit/test_desktop_account_cache.py) | 0 | 0 | 0 | unrun |
| [test_desktop_in_use.py](../../tests/unit/test_desktop_in_use.py) | 13 | 0 | 0 | done |
| [test_desktop_ledger.py](../../tests/unit/test_desktop_ledger.py) | 0 | 0 | 0 | unrun |
| [test_disk_admission.py](../../tests/unit/test_disk_admission.py) | 0 | 0 | 0 | unrun |
| [test_disk_floor.py](../../tests/unit/test_disk_floor.py) | 0 | 0 | 0 | unrun |
| [test_doctor.py](../../tests/unit/test_doctor.py) | 44 | 0 | 0 | pending |
| [test_doctor_ca_bundle.py](../../tests/unit/test_doctor_ca_bundle.py) | 16 | 0 | 0 | done |
| [test_doctor_identity.py](../../tests/unit/test_doctor_identity.py) | 0 | 0 | 0 | unrun |
| [test_empty_provider_results.py](../../tests/unit/test_empty_provider_results.py) | 334 | 0 | 0 | done |
| [test_enrollment_runner.py](../../tests/unit/test_enrollment_runner.py) | 0 | 0 | 0 | unrun |
| [test_fake_codex_http.py](../../tests/unit/test_fake_codex_http.py) | 0 | 0 | 0 | unrun |
| [test_filesystem_identity_agreement.py](../../tests/unit/test_filesystem_identity_agreement.py) | 37 | 0 | 3 | done |
| [test_folders.py](../../tests/unit/test_folders.py) | 0 | 0 | 0 | unrun |
| [test_forget_paced.py](../../tests/unit/test_forget_paced.py) | 0 | 0 | 0 | unrun |
| [test_fresh_until.py](../../tests/unit/test_fresh_until.py) | 0 | 0 | 0 | unrun |
| [test_frozen_provider_streams.py](../../tests/unit/test_frozen_provider_streams.py) | 0 | 0 | 0 | unrun |
| [test_gate_admission.py](../../tests/unit/test_gate_admission.py) | 0 | 0 | 0 | unrun |
| [test_gate_merge.py](../../tests/unit/test_gate_merge.py) | 0 | 0 | 0 | unrun |
| [test_gate_replay.py](../../tests/unit/test_gate_replay.py) | 0 | 0 | 0 | unrun |
| [test_gate_revision.py](../../tests/unit/test_gate_revision.py) | 0 | 0 | 0 | unrun |
| [test_gate_service.py](../../tests/unit/test_gate_service.py) | 0 | 0 | 0 | unrun |
| [test_gate_verdict.py](../../tests/unit/test_gate_verdict.py) | 45 | 0 | 0 | done |
| [test_guard_overlay.py](../../tests/unit/test_guard_overlay.py) | 0 | 0 | 0 | unrun |
| [test_guard_trust.py](../../tests/unit/test_guard_trust.py) | 0 | 0 | 0 | unrun |
| [test_guardian.py](../../tests/unit/test_guardian.py) | 0 | 0 | 0 | unrun |
| [test_guardian_qos.py](../../tests/unit/test_guardian_qos.py) | 14 | 0 | 0 | done |
| [test_handler_refusals.py](../../tests/unit/test_handler_refusals.py) | 0 | 0 | 0 | unrun |
| [test_held_waiter_holdback.py](../../tests/unit/test_held_waiter_holdback.py) | 6 | 0 | 0 | done |
| [test_hidden_environment.py](../../tests/unit/test_hidden_environment.py) | 0 | 0 | 0 | unrun |
| [test_hook_cost.py](../../tests/unit/test_hook_cost.py) | 4 | 0 | 0 | pending |
| [test_hooks.py](../../tests/unit/test_hooks.py) | 72 | 0 | 1 | pending |
| [test_hooks_native_guard.py](../../tests/unit/test_hooks_native_guard.py) | 18 | 0 | 0 | done |
| [test_host_push.py](../../tests/unit/test_host_push.py) | 118 | 0 | 0 | done |
| [test_host_push_admission.py](../../tests/unit/test_host_push_admission.py) | 0 | 0 | 0 | unrun |
| [test_host_push_build.py](../../tests/unit/test_host_push_build.py) | 0 | 0 | 0 | unrun |
| [test_host_push_build_budget.py](../../tests/unit/test_host_push_build_budget.py) | 0 | 0 | 0 | unrun |
| [test_host_push_cli.py](../../tests/unit/test_host_push_cli.py) | 0 | 0 | 0 | unrun |
| [test_host_push_config_cost.py](../../tests/unit/test_host_push_config_cost.py) | 7 | 0 | 0 | done |
| [test_host_push_copy.py](../../tests/unit/test_host_push_copy.py) | 0 | 0 | 0 | unrun |
| [test_host_push_export.py](../../tests/unit/test_host_push_export.py) | 0 | 0 | 0 | unrun |
| [test_host_push_intake.py](../../tests/unit/test_host_push_intake.py) | 13 | 0 | 0 | done |
| [test_host_push_metadata.py](../../tests/unit/test_host_push_metadata.py) | 0 | 0 | 0 | unrun |
| [test_host_push_mutations.py](../../tests/unit/test_host_push_mutations.py) | 0 | 0 | 0 | unrun |
| [test_host_push_names.py](../../tests/unit/test_host_push_names.py) | 13 | 0 | 0 | done |
| [test_host_push_quarantine_restart.py](../../tests/unit/test_host_push_quarantine_restart.py) | 0 | 0 | 0 | unrun |
| [test_host_push_restart.py](../../tests/unit/test_host_push_restart.py) | 0 | 0 | 0 | unrun |
| [test_host_push_retention.py](../../tests/unit/test_host_push_retention.py) | 2 | 0 | 0 | done |
| [test_host_push_storage_recovery.py](../../tests/unit/test_host_push_storage_recovery.py) | 0 | 0 | 0 | unrun |
| [test_host_push_storage_retry_budget.py](../../tests/unit/test_host_push_storage_retry_budget.py) | 0 | 0 | 0 | unrun |
| [test_host_push_store_completion_pr183.py](../../tests/unit/test_host_push_store_completion_pr183.py) | 0 | 0 | 0 | unrun |
| [test_host_push_store_interleavings.py](../../tests/unit/test_host_push_store_interleavings.py) | 26 | 0 | 0 | done |
| [test_host_push_trees.py](../../tests/unit/test_host_push_trees.py) | 0 | 0 | 0 | unrun |
| [test_host_push_validated_top.py](../../tests/unit/test_host_push_validated_top.py) | 0 | 0 | 0 | unrun |
| [test_importer.py](../../tests/unit/test_importer.py) | 145 | 3 | 0 | done |
| [test_inspection_pacing_properties.py](../../tests/unit/test_inspection_pacing_properties.py) | 0 | 0 | 0 | unrun |
| [test_invariants_index.py](../../tests/unit/test_invariants_index.py) | 25 | 0 | 0 | done |
| [test_keychain_exec_guard_properties.py](../../tests/unit/test_keychain_exec_guard_properties.py) | 3 | 0 | 0 | done |
| [test_lane_answers.py](../../tests/unit/test_lane_answers.py) | 0 | 0 | 0 | unrun |
| [test_lanes_transfer.py](../../tests/unit/test_lanes_transfer.py) | 79 | 0 | 0 | done |
| [test_launch_env_allowlist.py](../../tests/unit/test_launch_env_allowlist.py) | 27 | 0 | 0 | pending |
| [test_launch_env_helpers.py](../../tests/unit/test_launch_env_helpers.py) | 20 | 0 | 0 | done |
| [test_launch_env_mutations.py](../../tests/unit/test_launch_env_mutations.py) | 41 | 0 | 0 | pending |
| [test_launch_env_policy.py](../../tests/unit/test_launch_env_policy.py) | 26 | 0 | 0 | pending |
| [test_launch_env_round2.py](../../tests/unit/test_launch_env_round2.py) | 67 | 0 | 0 | pending |
| [test_launch_env_round3.py](../../tests/unit/test_launch_env_round3.py) | 69 | 0 | 0 | pending |
| [test_launch_env_round4.py](../../tests/unit/test_launch_env_round4.py) | 40 | 0 | 0 | pending |
| [test_launch_env_round5.py](../../tests/unit/test_launch_env_round5.py) | 39 | 0 | 0 | done |
| [test_lease_waiter_selection.py](../../tests/unit/test_lease_waiter_selection.py) | 0 | 0 | 0 | unrun |
| [test_legacy_hold.py](../../tests/unit/test_legacy_hold.py) | 50 | 0 | 6 | done |
| [test_limit_from_model_text.py](../../tests/unit/test_limit_from_model_text.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard.py](../../tests/unit/test_live_store_guard.py) | 324 | 0 | 0 | done |
| [test_live_store_guard_round10.py](../../tests/unit/test_live_store_guard_round10.py) | 60 | 0 | 0 | done |
| [test_live_store_guard_round10_mutations.py](../../tests/unit/test_live_store_guard_round10_mutations.py) | 13 | 0 | 0 | done |
| [test_live_store_guard_round3.py](../../tests/unit/test_live_store_guard_round3.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round4.py](../../tests/unit/test_live_store_guard_round4.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round5.py](../../tests/unit/test_live_store_guard_round5.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round6.py](../../tests/unit/test_live_store_guard_round6.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round7.py](../../tests/unit/test_live_store_guard_round7.py) | 50 | 0 | 0 | done |
| [test_live_store_guard_round7_mutations.py](../../tests/unit/test_live_store_guard_round7_mutations.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round8.py](../../tests/unit/test_live_store_guard_round8.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round8_mutations.py](../../tests/unit/test_live_store_guard_round8_mutations.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round9.py](../../tests/unit/test_live_store_guard_round9.py) | 0 | 0 | 0 | unrun |
| [test_live_store_guard_round9_mutations.py](../../tests/unit/test_live_store_guard_round9_mutations.py) | 12 | 0 | 0 | done |
| [test_live_store_guard_state_root.py](../../tests/unit/test_live_store_guard_state_root.py) | 0 | 0 | 0 | unrun |
| [test_lockwatch.py](../../tests/unit/test_lockwatch.py) | 20 | 0 | 0 | done |
| [test_mcp_cli_contract.py](../../tests/unit/test_mcp_cli_contract.py) | 6 | 0 | 0 | done |
| [test_mcp_scheduler_contract.py](../../tests/unit/test_mcp_scheduler_contract.py) | 0 | 0 | 0 | unrun |
| [test_mirror_flags_faults.py](../../tests/unit/test_mirror_flags_faults.py) | 29 | 0 | 0 | done |
| [test_mirror_flags_model.py](../../tests/unit/test_mirror_flags_model.py) | 0 | 0 | 0 | unrun |
| [test_mirror_flags_stateful.py](../../tests/unit/test_mirror_flags_stateful.py) | 2 | 0 | 0 | done |
| [test_native_operations.py](../../tests/unit/test_native_operations.py) | 0 | 0 | 0 | unrun |
| [test_notice_rendering.py](../../tests/unit/test_notice_rendering.py) | 21 | 0 | 0 | done |
| [test_notify_push.py](../../tests/unit/test_notify_push.py) | 0 | 0 | 0 | unrun |
| [test_offline.py](../../tests/unit/test_offline.py) | 33 | 0 | 0 | done |
| [test_operator_hold_closures.py](../../tests/unit/test_operator_hold_closures.py) | 0 | 0 | 0 | unrun |
| [test_operator_notices.py](../../tests/unit/test_operator_notices.py) | 79 | 0 | 0 | done |
| [test_outcome_unknown.py](../../tests/unit/test_outcome_unknown.py) | 58 | 0 | 0 | pending |
| [test_person_boundary.py](../../tests/unit/test_person_boundary.py) | 0 | 0 | 0 | unrun |
| [test_person_boundary_identity.py](../../tests/unit/test_person_boundary_identity.py) | 25 | 0 | 0 | done |
| [test_picker.py](../../tests/unit/test_picker.py) | 0 | 0 | 0 | unrun |
| [test_placement_ramp.py](../../tests/unit/test_placement_ramp.py) | 26 | 0 | 0 | done |
| [test_policy.py](../../tests/unit/test_policy.py) | 178 | 0 | 0 | done |
| [test_policy_disk.py](../../tests/unit/test_policy_disk.py) | 0 | 0 | 0 | unrun |
| [test_policy_ramp.py](../../tests/unit/test_policy_ramp.py) | 0 | 0 | 0 | unrun |
| [test_policy_support.py](../../tests/unit/test_policy_support.py) | 0 | 0 | 0 | unrun |
| [test_policy_tier_preferences.py](../../tests/unit/test_policy_tier_preferences.py) | 0 | 0 | 0 | unrun |
| [test_pr124_fixes.py](../../tests/unit/test_pr124_fixes.py) | 0 | 0 | 0 | unrun |
| [test_pr124_review_r2_fixes.py](../../tests/unit/test_pr124_review_r2_fixes.py) | 0 | 0 | 0 | unrun |
| [test_pr192_review_r1.py](../../tests/unit/test_pr192_review_r1.py) | 0 | 0 | 0 | unrun |
| [test_pr192_review_r2.py](../../tests/unit/test_pr192_review_r2.py) | 0 | 0 | 0 | unrun |
| [test_procs.py](../../tests/unit/test_procs.py) | 0 | 0 | 0 | unrun |
| [test_protocol_secrecy.py](../../tests/unit/test_protocol_secrecy.py) | 0 | 0 | 0 | unrun |
| [test_provider_active_cap.py](../../tests/unit/test_provider_active_cap.py) | 50 | 0 | 0 | done |
| [test_prune_217.py](../../tests/unit/test_prune_217.py) | 0 | 0 | 0 | unrun |
| [test_prune_decision_backup_verification.py](../../tests/unit/test_prune_decision_backup_verification.py) | 23 | 0 | 0 | done |
| [test_prune_decision_fingerprints.py](../../tests/unit/test_prune_decision_fingerprints.py) | 0 | 0 | 0 | unrun |
| [test_prune_decisions.py](../../tests/unit/test_prune_decisions.py) | 48 | 0 | 0 | done |
| [test_prune_restore.py](../../tests/unit/test_prune_restore.py) | 0 | 0 | 0 | unrun |
| [test_quota_projection.py](../../tests/unit/test_quota_projection.py) | 0 | 0 | 0 | unrun |
| [test_rank_review_report.py](../../tests/unit/test_rank_review_report.py) | 0 | 0 | 0 | unrun |
| [test_reap_liveness.py](../../tests/unit/test_reap_liveness.py) | 7 | 0 | 0 | done |
| [test_rediscovery_git_trace.py](../../tests/unit/test_rediscovery_git_trace.py) | 17 | 0 | 0 | done |
| [test_relay.py](../../tests/unit/test_relay.py) | 0 | 0 | 0 | unrun |
| [test_render.py](../../tests/unit/test_render.py) | 0 | 0 | 0 | unrun |
| [test_render_quota_projection.py](../../tests/unit/test_render_quota_projection.py) | 0 | 0 | 0 | unrun |
| [test_reserve_authorization_plumbing.py](../../tests/unit/test_reserve_authorization_plumbing.py) | 0 | 0 | 0 | unrun |
| [test_reset_credit_claim_timing.py](../../tests/unit/test_reset_credit_claim_timing.py) | 0 | 0 | 0 | unrun |
| [test_reset_credit_demand.py](../../tests/unit/test_reset_credit_demand.py) | 68 | 0 | 0 | done |
| [test_reset_credit_properties.py](../../tests/unit/test_reset_credit_properties.py) | 0 | 0 | 0 | unrun |
| [test_reset_credit_review_r1.py](../../tests/unit/test_reset_credit_review_r1.py) | 22 | 0 | 0 | done |
| [test_reset_credit_review_r2.py](../../tests/unit/test_reset_credit_review_r2.py) | 52 | 0 | 0 | done |
| [test_reset_credit_review_r3.py](../../tests/unit/test_reset_credit_review_r3.py) | 0 | 0 | 0 | unrun |
| [test_reset_credit_review_r3_mutants.py](../../tests/unit/test_reset_credit_review_r3_mutants.py) | 0 | 0 | 0 | unrun |
| [test_resource_identity.py](../../tests/unit/test_resource_identity.py) | 10 | 0 | 0 | done |
| [test_retention_81_notes.py](../../tests/unit/test_retention_81_notes.py) | 38 | 0 | 0 | done |
| [test_retention_archive.py](../../tests/unit/test_retention_archive.py) | 0 | 0 | 0 | unrun |
| [test_retention_archive_properties.py](../../tests/unit/test_retention_archive_properties.py) | 2 | 0 | 0 | done |
| [test_retention_case_alias.py](../../tests/unit/test_retention_case_alias.py) | 0 | 0 | 0 | unrun |
| [test_retention_nested_leases.py](../../tests/unit/test_retention_nested_leases.py) | 0 | 0 | 0 | unrun |
| [test_retention_ordinary_trees.py](../../tests/unit/test_retention_ordinary_trees.py) | 0 | 0 | 0 | unrun |
| [test_retention_qos.py](../../tests/unit/test_retention_qos.py) | 0 | 0 | 0 | unrun |
| [test_retention_readback.py](../../tests/unit/test_retention_readback.py) | 0 | 0 | 0 | unrun |
| [test_retention_rediscovery.py](../../tests/unit/test_retention_rediscovery.py) | 0 | 0 | 0 | unrun |
| [test_retention_regenerable.py](../../tests/unit/test_retention_regenerable.py) | 0 | 0 | 0 | unrun |
| [test_retention_regenerable_properties.py](../../tests/unit/test_retention_regenerable_properties.py) | 1 | 0 | 0 | done |
| [test_retention_salvage_properties.py](../../tests/unit/test_retention_salvage_properties.py) | 0 | 0 | 0 | unrun |
| [test_retention_shared_folders.py](../../tests/unit/test_retention_shared_folders.py) | 0 | 0 | 0 | unrun |
| [test_retention_sweep_race.py](../../tests/unit/test_retention_sweep_race.py) | 13 | 0 | 0 | done |
| [test_retention_turn_contract.py](../../tests/unit/test_retention_turn_contract.py) | 0 | 0 | 0 | unrun |
| [test_retention_turn_workdirs.py](../../tests/unit/test_retention_turn_workdirs.py) | 0 | 0 | 0 | unrun |
| [test_retention_turns.py](../../tests/unit/test_retention_turns.py) | 0 | 0 | 0 | unrun |
| [test_retention_worktrees.py](../../tests/unit/test_retention_worktrees.py) | 0 | 0 | 0 | unrun |
| [test_review130_pass.py](../../tests/unit/test_review130_pass.py) | 0 | 0 | 0 | unrun |
| [test_review_pr127_fixes.py](../../tests/unit/test_review_pr127_fixes.py) | 0 | 0 | 0 | unrun |
| [test_review_pr127_history.py](../../tests/unit/test_review_pr127_history.py) | 6 | 0 | 0 | done |
| [test_review_pr127_pr_wakes.py](../../tests/unit/test_review_pr127_pr_wakes.py) | 7 | 0 | 0 | done |
| [test_review_pr127_report.py](../../tests/unit/test_review_pr127_report.py) | 0 | 0 | 0 | unrun |
| [test_review_pr127_requests.py](../../tests/unit/test_review_pr127_requests.py) | 17 | 0 | 0 | done |
| [test_review_pr127_service.py](../../tests/unit/test_review_pr127_service.py) | 0 | 0 | 0 | unrun |
| [test_review_r2_repro.py](../../tests/unit/test_review_r2_repro.py) | 31 | 0 | 0 | done |
| [test_review_r3_measure.py](../../tests/unit/test_review_r3_measure.py) | 6 | 0 | 0 | done |
| [test_review_r3_repro.py](../../tests/unit/test_review_r3_repro.py) | 0 | 0 | 0 | unrun |
| [test_review_r4_repro.py](../../tests/unit/test_review_r4_repro.py) | 0 | 0 | 0 | unrun |
| [test_review_r5_repro.py](../../tests/unit/test_review_r5_repro.py) | 0 | 0 | 0 | unrun |
| [test_review_r6_probes.py](../../tests/unit/test_review_r6_probes.py) | 0 | 0 | 0 | unrun |
| [test_review_r6_restart.py](../../tests/unit/test_review_r6_restart.py) | 0 | 0 | 0 | unrun |
| [test_round10_refusal_context.py](../../tests/unit/test_round10_refusal_context.py) | 0 | 0 | 0 | unrun |
| [test_route_check.py](../../tests/unit/test_route_check.py) | 24 | 0 | 0 | done |
| [test_salvage.py](../../tests/unit/test_salvage.py) | 0 | 0 | 0 | unrun |
| [test_salvage_optional_transient.py](../../tests/unit/test_salvage_optional_transient.py) | 0 | 0 | 0 | unrun |
| [test_salvage_unindexable.py](../../tests/unit/test_salvage_unindexable.py) | 0 | 0 | 0 | unrun |
| [test_scheduler.py](../../tests/unit/test_scheduler.py) | 97 | 0 | 0 | done |
| [test_scheduler_priority_callers.py](../../tests/unit/test_scheduler_priority_callers.py) | 0 | 0 | 0 | unrun |
| [test_scheduler_split.py](../../tests/unit/test_scheduler_split.py) | 3 | 0 | 0 | done |
| [test_scheduler_tier_preferences.py](../../tests/unit/test_scheduler_tier_preferences.py) | 9 | 0 | 0 | done |
| [test_scheduler_turns.py](../../tests/unit/test_scheduler_turns.py) | 10 | 0 | 0 | done |
| [test_scheduler_weekly.py](../../tests/unit/test_scheduler_weekly.py) | 33 | 0 | 0 | done |
| [test_scoped_stream_capacity.py](../../tests/unit/test_scoped_stream_capacity.py) | 17 | 0 | 0 | done |
| [test_scrub_key_order.py](../../tests/unit/test_scrub_key_order.py) | 70 | 0 | 0 | done |
| [test_sessions_cli.py](../../tests/unit/test_sessions_cli.py) | 142 | 0 | 0 | pending |
| [test_sessions_handoff.py](../../tests/unit/test_sessions_handoff.py) | 117 | 0 | 0 | done |
| [test_sessions_handoff_excerpts.py](../../tests/unit/test_sessions_handoff_excerpts.py) | 0 | 0 | 0 | unrun |
| [test_sessions_headless_properties.py](../../tests/unit/test_sessions_headless_properties.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror.py](../../tests/unit/test_sessions_mirror.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_cache_coherence.py](../../tests/unit/test_sessions_mirror_cache_coherence.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_fifo.py](../../tests/unit/test_sessions_mirror_fifo.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_hot_health.py](../../tests/unit/test_sessions_mirror_hot_health.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_hot_progress.py](../../tests/unit/test_sessions_mirror_hot_progress.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_hot_regressions.py](../../tests/unit/test_sessions_mirror_hot_regressions.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_inventory.py](../../tests/unit/test_sessions_mirror_inventory.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_load_gap.py](../../tests/unit/test_sessions_mirror_load_gap.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_safety.py](../../tests/unit/test_sessions_mirror_safety.py) | 0 | 0 | 0 | unrun |
| [test_sessions_mirror_write_churn.py](../../tests/unit/test_sessions_mirror_write_churn.py) | 0 | 0 | 0 | unrun |
| [test_sessions_nudge.py](../../tests/unit/test_sessions_nudge.py) | 0 | 0 | 0 | unrun |
| [test_sessions_registry.py](../../tests/unit/test_sessions_registry.py) | 24 | 0 | 0 | done |
| [test_sessions_revive.py](../../tests/unit/test_sessions_revive.py) | 45 | 0 | 0 | done |
| [test_sessions_revive_reads.py](../../tests/unit/test_sessions_revive_reads.py) | 0 | 0 | 0 | unrun |
| [test_sessions_transcripts.py](../../tests/unit/test_sessions_transcripts.py) | 0 | 0 | 0 | unrun |
| [test_settle_properties.py](../../tests/unit/test_settle_properties.py) | 0 | 0 | 0 | unrun |
| [test_soak_tools.py](../../tests/unit/test_soak_tools.py) | 49 | 0 | 0 | done |
| [test_status_json.py](../../tests/unit/test_status_json.py) | 0 | 0 | 0 | unrun |
| [test_steer_invariants.py](../../tests/unit/test_steer_invariants.py) | 248 | 0 | 0 | done |
| [test_stop_alarm.py](../../tests/unit/test_stop_alarm.py) | 0 | 0 | 0 | unrun |
| [test_stop_watchdog.py](../../tests/unit/test_stop_watchdog.py) | 0 | 0 | 0 | unrun |
| [test_stopped_daemon.py](../../tests/unit/test_stopped_daemon.py) | 72 | 0 | 0 | pending |
| [test_store_contention_queries.py](../../tests/unit/test_store_contention_queries.py) | 212 | 0 | 0 | done |
| [test_store_ids.py](../../tests/unit/test_store_ids.py) | 27 | 0 | 0 | done |
| [test_store_migration_2.py](../../tests/unit/test_store_migration_2.py) | 0 | 0 | 0 | unrun |
| [test_store_properties.py](../../tests/unit/test_store_properties.py) | 0 | 0 | 0 | unrun |
| [test_store_readers.py](../../tests/unit/test_store_readers.py) | 0 | 0 | 0 | unrun |
| [test_store_snapshot_plans.py](../../tests/unit/test_store_snapshot_plans.py) | 4 | 0 | 0 | done |
| [test_suggest_cli.py](../../tests/unit/test_suggest_cli.py) | 7 | 0 | 0 | done |
| [test_suggest_hint.py](../../tests/unit/test_suggest_hint.py) | 0 | 0 | 0 | unrun |
| [test_suggestion_error_boundaries.py](../../tests/unit/test_suggestion_error_boundaries.py) | 0 | 0 | 0 | unrun |
| [test_suggestion_properties.py](../../tests/unit/test_suggestion_properties.py) | 1 | 0 | 0 | done |
| [test_suggestion_secret_properties.py](../../tests/unit/test_suggestion_secret_properties.py) | 7 | 0 | 0 | done |
| [test_suggestion_structural_errors.py](../../tests/unit/test_suggestion_structural_errors.py) | 40 | 0 | 0 | done |
| [test_suggestions.py](../../tests/unit/test_suggestions.py) | 30 | 0 | 0 | done |
| [test_suggestions_r1b_adversarial.py](../../tests/unit/test_suggestions_r1b_adversarial.py) | 0 | 0 | 0 | unrun |
| [test_suggestions_r3_channels.py](../../tests/unit/test_suggestions_r3_channels.py) | 0 | 0 | 0 | unrun |
| [test_suggestions_review_boundaries.py](../../tests/unit/test_suggestions_review_boundaries.py) | 7 | 0 | 0 | done |
| [test_suggestions_review_multiline.py](../../tests/unit/test_suggestions_review_multiline.py) | 0 | 0 | 0 | unrun |
| [test_suggestions_review_response.py](../../tests/unit/test_suggestions_review_response.py) | 0 | 0 | 0 | unrun |
| [test_test_module_names.py](../../tests/unit/test_test_module_names.py) | 1 | 0 | 0 | done |
| [test_tick_prefilter.py](../../tests/unit/test_tick_prefilter.py) | 0 | 0 | 0 | unrun |
| [test_timers_alerts.py](../../tests/unit/test_timers_alerts.py) | 0 | 0 | 0 | unrun |
| [test_timers_busy_read.py](../../tests/unit/test_timers_busy_read.py) | 115 | 0 | 0 | done |
| [test_timers_daemon.py](../../tests/unit/test_timers_daemon.py) | 4 | 0 | 0 | done |
| [test_timers_keepalive.py](../../tests/unit/test_timers_keepalive.py) | 14 | 0 | 0 | done |
| [test_timers_probe.py](../../tests/unit/test_timers_probe.py) | 0 | 0 | 0 | unrun |
| [test_timers_reset_credits.py](../../tests/unit/test_timers_reset_credits.py) | 0 | 0 | 0 | unrun |
| [test_timers_retention.py](../../tests/unit/test_timers_retention.py) | 20 | 0 | 0 | done |
| [test_title_review_regressions.py](../../tests/unit/test_title_review_regressions.py) | 0 | 0 | 0 | unrun |
| [test_turn_attachment_replay.py](../../tests/unit/test_turn_attachment_replay.py) | 0 | 0 | 0 | unrun |
| [test_turn_overlap.py](../../tests/unit/test_turn_overlap.py) | 10 | 0 | 0 | done |
| [test_turn_replay.py](../../tests/unit/test_turn_replay.py) | 0 | 0 | 0 | unrun |
| [test_turn_runner.py](../../tests/unit/test_turn_runner.py) | 0 | 0 | 0 | unrun |
| [test_ultracode_effort.py](../../tests/unit/test_ultracode_effort.py) | 0 | 0 | 0 | unrun |
| [test_unadmittable_pin.py](../../tests/unit/test_unadmittable_pin.py) | 12 | 0 | 0 | done |
| [test_wait_hub.py](../../tests/unit/test_wait_hub.py) | 0 | 0 | 0 | unrun |
| [test_wake_cli.py](../../tests/unit/test_wake_cli.py) | 0 | 0 | 0 | unrun |
| [test_weekly_rank_vs_base.py](../../tests/unit/test_weekly_rank_vs_base.py) | 0 | 0 | 0 | unrun |
#### tests/fake: per-file counts

| File | Passed | Failed | Skipped | Full sweep |
| --- | ---: | ---: | ---: | --- |
| [test_admission_disk.py](../../tests/fake/test_admission_disk.py) | 0 | 0 | 0 | unrun |
| [test_admission_held_waiters.py](../../tests/fake/test_admission_held_waiters.py) | 1 | 0 | 0 | done |
| [test_admission_latency.py](../../tests/fake/test_admission_latency.py) | 0 | 0 | 0 | unrun |
| [test_admission_lease_waiters.py](../../tests/fake/test_admission_lease_waiters.py) | 0 | 0 | 0 | unrun |
| [test_admission_liveness.py](../../tests/fake/test_admission_liveness.py) | 0 | 0 | 0 | unrun |
| [test_admission_priority.py](../../tests/fake/test_admission_priority.py) | 0 | 0 | 0 | unrun |
| [test_admission_properties.py](../../tests/fake/test_admission_properties.py) | 0 | 0 | 0 | unrun |
| [test_admission_ramp.py](../../tests/fake/test_admission_ramp.py) | 0 | 0 | 0 | unrun |
| [test_admission_ramp_retention.py](../../tests/fake/test_admission_ramp_retention.py) | 0 | 0 | 0 | unrun |
| [test_admission_route_isolation.py](../../tests/fake/test_admission_route_isolation.py) | 93 | 0 | 0 | done |
| [test_admission_stateful.py](../../tests/fake/test_admission_stateful.py) | 0 | 0 | 0 | unrun |
| [test_admission_visibility.py](../../tests/fake/test_admission_visibility.py) | 0 | 0 | 0 | unrun |
| [test_batch_label.py](../../tests/fake/test_batch_label.py) | 9 | 0 | 0 | done |
| [test_canonical_identity.py](../../tests/fake/test_canonical_identity.py) | 0 | 0 | 0 | unrun |
| [test_canonical_identity_fix2.py](../../tests/fake/test_canonical_identity_fix2.py) | 0 | 0 | 0 | unrun |
| [test_canonical_identity_fix3.py](../../tests/fake/test_canonical_identity_fix3.py) | 17 | 0 | 1 | done |
| [test_canonical_identity_fix4.py](../../tests/fake/test_canonical_identity_fix4.py) | 0 | 0 | 0 | unrun |
| [test_canonical_identity_fix5.py](../../tests/fake/test_canonical_identity_fix5.py) | 0 | 0 | 0 | unrun |
| [test_capacity_steer_daemon.py](../../tests/fake/test_capacity_steer_daemon.py) | 54 | 0 | 0 | done |
| [test_capacity_wait_backoff.py](../../tests/fake/test_capacity_wait_backoff.py) | 0 | 0 | 0 | unrun |
| [test_census_escapers_daemon.py](../../tests/fake/test_census_escapers_daemon.py) | 0 | 0 | 0 | unrun |
| [test_census_recycled_pid_daemon.py](../../tests/fake/test_census_recycled_pid_daemon.py) | 0 | 0 | 0 | unrun |
| [test_codex_ca_bundle_fake.py](../../tests/fake/test_codex_ca_bundle_fake.py) | 0 | 0 | 0 | unrun |
| [test_codex_turn_completed_fake.py](../../tests/fake/test_codex_turn_completed_fake.py) | 0 | 0 | 0 | unrun |
| [test_conversation_pools.py](../../tests/fake/test_conversation_pools.py) | 2 | 0 | 0 | done |
| [test_conversation_wakes_fake.py](../../tests/fake/test_conversation_wakes_fake.py) | 4 | 0 | 0 | done |
| [test_crash_matrix.py](../../tests/fake/test_crash_matrix.py) | 0 | 0 | 0 | unrun |
| [test_daemon_contract.py](../../tests/fake/test_daemon_contract.py) | 0 | 0 | 21 | done |
| [test_daemon_descriptors.py](../../tests/fake/test_daemon_descriptors.py) | 0 | 0 | 0 | unrun |
| [test_daemon_launch.py](../../tests/fake/test_daemon_launch.py) | 0 | 0 | 0 | unrun |
| [test_daemon_stacks.py](../../tests/fake/test_daemon_stacks.py) | 0 | 0 | 0 | unrun |
| [test_descriptor_exhaustion.py](../../tests/fake/test_descriptor_exhaustion.py) | 0 | 0 | 0 | unrun |
| [test_detached_cwd_pins.py](../../tests/fake/test_detached_cwd_pins.py) | 18 | 0 | 0 | done |
| [test_detached_retention_fence.py](../../tests/fake/test_detached_retention_fence.py) | 89 | 0 | 0 | done |
| [test_enrollment_guardian.py](../../tests/fake/test_enrollment_guardian.py) | 0 | 0 | 1 | done |
| [test_fake_provider.py](../../tests/fake/test_fake_provider.py) | 0 | 0 | 0 | unrun |
| [test_finalization_replay.py](../../tests/fake/test_finalization_replay.py) | 0 | 0 | 0 | unrun |
| [test_folder_spelling.py](../../tests/fake/test_folder_spelling.py) | 0 | 0 | 0 | unrun |
| [test_frozen_stream_nonregular.py](../../tests/fake/test_frozen_stream_nonregular.py) | 0 | 0 | 0 | unrun |
| [test_gate_end_to_end.py](../../tests/fake/test_gate_end_to_end.py) | 10 | 0 | 1 | done |
| [test_identity_rollback.py](../../tests/fake/test_identity_rollback.py) | 0 | 0 | 0 | unrun |
| [test_idle_cost.py](../../tests/fake/test_idle_cost.py) | 0 | 0 | 0 | unrun |
| [test_interactive_claude_titles.py](../../tests/fake/test_interactive_claude_titles.py) | 6 | 0 | 0 | done |
| [test_interactive_steer.py](../../tests/fake/test_interactive_steer.py) | 13 | 0 | 0 | done |
| [test_lane_fault.py](../../tests/fake/test_lane_fault.py) | 0 | 0 | 0 | unrun |
| [test_lane_pilot.py](../../tests/fake/test_lane_pilot.py) | 0 | 0 | 0 | unrun |
| [test_lanes_enroll.py](../../tests/fake/test_lanes_enroll.py) | 0 | 0 | 0 | unrun |
| [test_lanes_reenroll.py](../../tests/fake/test_lanes_reenroll.py) | 0 | 0 | 0 | unrun |
| [test_lanes_reenroll_keychain.py](../../tests/fake/test_lanes_reenroll_keychain.py) | 0 | 0 | 0 | unrun |
| [test_launch_env_daemon.py](../../tests/fake/test_launch_env_daemon.py) | 49 | 0 | 0 | pending |
| [test_launch_env_providers.py](../../tests/fake/test_launch_env_providers.py) | 6 | 0 | 0 | pending |
| [test_launch_gates_above_stdio.py](../../tests/fake/test_launch_gates_above_stdio.py) | 0 | 0 | 0 | unrun |
| [test_lost_acknowledgement.py](../../tests/fake/test_lost_acknowledgement.py) | 0 | 0 | 0 | unrun |
| [test_main_refusal_write_dir.py](../../tests/fake/test_main_refusal_write_dir.py) | 5 | 0 | 0 | done |
| [test_mcp_job_contract.py](../../tests/fake/test_mcp_job_contract.py) | 12 | 0 | 0 | done |
| [test_native_maintenance_startup.py](../../tests/fake/test_native_maintenance_startup.py) | 0 | 0 | 0 | unrun |
| [test_notice_delivery.py](../../tests/fake/test_notice_delivery.py) | 0 | 0 | 0 | unrun |
| [test_notice_header.py](../../tests/fake/test_notice_header.py) | 0 | 0 | 0 | unrun |
| [test_picker_native.py](../../tests/fake/test_picker_native.py) | 3 | 0 | 0 | done |
| [test_policy_sandbox.py](../../tests/fake/test_policy_sandbox.py) | 25 | 0 | 0 | done |
| [test_probe_cancel.py](../../tests/fake/test_probe_cancel.py) | 24 | 0 | 0 | done |
| [test_probe_recovery.py](../../tests/fake/test_probe_recovery.py) | 0 | 0 | 0 | unrun |
| [test_probe_turn.py](../../tests/fake/test_probe_turn.py) | 43 | 0 | 0 | done |
| [test_prompt_seam.py](../../tests/fake/test_prompt_seam.py) | 0 | 0 | 0 | unrun |
| [test_provider_active_cap_fake.py](../../tests/fake/test_provider_active_cap_fake.py) | 0 | 0 | 0 | unrun |
| [test_provider_cap_review_evidence.py](../../tests/fake/test_provider_cap_review_evidence.py) | 0 | 0 | 0 | unrun |
| [test_provider_qos.py](../../tests/fake/test_provider_qos.py) | 0 | 0 | 2 | done |
| [test_record_secrecy.py](../../tests/fake/test_record_secrecy.py) | 0 | 0 | 0 | unrun |
| [test_request_routing.py](../../tests/fake/test_request_routing.py) | 3 | 0 | 0 | done |
| [test_reserve_authorization.py](../../tests/fake/test_reserve_authorization.py) | 12 | 0 | 0 | done |
| [test_reserve_authorization_validation.py](../../tests/fake/test_reserve_authorization_validation.py) | 0 | 0 | 0 | unrun |
| [test_reset_credit_incident.py](../../tests/fake/test_reset_credit_incident.py) | 19 | 0 | 0 | done |
| [test_resume_cli.py](../../tests/fake/test_resume_cli.py) | 0 | 0 | 0 | unrun |
| [test_resume_contract.py](../../tests/fake/test_resume_contract.py) | 23 | 0 | 0 | done |
| [test_retention_dormant.py](../../tests/fake/test_retention_dormant.py) | 0 | 0 | 0 | unrun |
| [test_review130_ab.py](../../tests/fake/test_review130_ab.py) | 12 | 0 | 0 | done |
| [test_review_r185_adversarial.py](../../tests/fake/test_review_r185_adversarial.py) | 0 | 0 | 0 | unrun |
| [test_route_view.py](../../tests/fake/test_route_view.py) | 0 | 0 | 0 | unrun |
| [test_routing_end_to_end.py](../../tests/fake/test_routing_end_to_end.py) | 14 | 0 | 2 | done |
| [test_routing_tier_preferences.py](../../tests/fake/test_routing_tier_preferences.py) | 5 | 0 | 0 | done |
| [test_salvage_adversarial.py](../../tests/fake/test_salvage_adversarial.py) | 8 | 0 | 0 | done |
| [test_salvage_finalization.py](../../tests/fake/test_salvage_finalization.py) | 0 | 0 | 0 | unrun |
| [test_sessions_end_to_end.py](../../tests/fake/test_sessions_end_to_end.py) | 0 | 0 | 0 | unrun |
| [test_state_contract.py](../../tests/fake/test_state_contract.py) | 37 | 0 | 0 | done |
| [test_state_fixture_processes.py](../../tests/fake/test_state_fixture_processes.py) | 0 | 0 | 0 | unrun |
| [test_status_payload.py](../../tests/fake/test_status_payload.py) | 0 | 0 | 0 | unrun |
| [test_suggestions_fake.py](../../tests/fake/test_suggestions_fake.py) | 1 | 0 | 0 | done |
| [test_tier_hold_by_demand.py](../../tests/fake/test_tier_hold_by_demand.py) | 27 | 0 | 0 | done |
| [test_timers_busy_admission.py](../../tests/fake/test_timers_busy_admission.py) | 0 | 0 | 0 | unrun |
| [test_timers_end_to_end.py](../../tests/fake/test_timers_end_to_end.py) | 0 | 0 | 0 | unrun |
| [test_turn_listing.py](../../tests/fake/test_turn_listing.py) | 9 | 0 | 0 | done |
| [test_turn_retention_cwd.py](../../tests/fake/test_turn_retention_cwd.py) | 6 | 0 | 0 | done |
| [test_turn_retention_cwd_properties.py](../../tests/fake/test_turn_retention_cwd_properties.py) | 1 | 0 | 0 | done |
| [test_turn_retention_job_directory.py](../../tests/fake/test_turn_retention_job_directory.py) | 0 | 0 | 0 | unrun |
| [test_turn_retention_job_properties.py](../../tests/fake/test_turn_retention_job_properties.py) | 0 | 0 | 0 | unrun |
| [test_turn_retention_quarantine.py](../../tests/fake/test_turn_retention_quarantine.py) | 0 | 0 | 0 | unrun |
| [test_turn_trees.py](../../tests/fake/test_turn_trees.py) | 12 | 0 | 0 | done |
| [test_turn_wait_reasons.py](../../tests/fake/test_turn_wait_reasons.py) | 7 | 0 | 0 | done |
| [test_unadmittable_pin_admission.py](../../tests/fake/test_unadmittable_pin_admission.py) | 0 | 0 | 0 | unrun |
| [test_unadmittable_pin_rereview.py](../../tests/fake/test_unadmittable_pin_rereview.py) | 6 | 0 | 0 | pending |
| [test_unadmittable_pin_review.py](../../tests/fake/test_unadmittable_pin_review.py) | 0 | 0 | 0 | unrun |
| [test_workspace_contract.py](../../tests/fake/test_workspace_contract.py) | 0 | 0 | 0 | unrun |
| [test_workspace_enospc.py](../../tests/fake/test_workspace_enospc.py) | 0 | 0 | 0 | unrun |
| [test_workspace_transient.py](../../tests/fake/test_workspace_transient.py) | 0 | 0 | 0 | unrun |
| [test_writable_fanout.py](../../tests/fake/test_writable_fanout.py) | 0 | 0 | 0 | unrun |
| [test_writer_retention_fence.py](../../tests/fake/test_writer_retention_fence.py) | 0 | 0 | 0 | unrun |

#### Shard 4 extra integration files

| File | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| [tests/e2e/test_capacity_steer_e2e.py](../../tests/e2e/test_capacity_steer_e2e.py) | 0 | 0 | 2 |
| [tests/e2e/test_conversation_wakes_e2e.py](../../tests/e2e/test_conversation_wakes_e2e.py) | 0 | 0 | 4 |
| [tests/e2e/test_milestone1_codex.py](../../tests/e2e/test_milestone1_codex.py) | 0 | 0 | 9 |
| [tests/e2e/test_stop_bound.py](../../tests/e2e/test_stop_bound.py) | 2 | 0 | 1 |
| [tests/e2e/test_suggestions_e2e.py](../../tests/e2e/test_suggestions_e2e.py) | 0 | 0 | 2 |
| [tests/process/test_codex_isolation.py](../../tests/process/test_codex_isolation.py) | 38 | 0 | 0 |

## Hub note, 2026-10-20: this PR's clause is C-17.9

This PR's ping-stdin clause was added as C-17.8 when that number was free. Release later took C-17.8 for #222's `doctor` environment-name report. The round-5 merge (`63cbb04c7`) kept this PR's clause under the shared number and folded release's clause into it as an indented paragraph, so two different rules shared one number. The hub's union check found it.

This PR's clause is now C-17.9, and release's C-17.8 is its own clause again, word for word. This report, `subfleet/cli.py`, `tests/unit/test_cli.py` and `tests/unit/test_launch_env_round3.py` now cite C-17.9 wherever they meant this PR's clause, including the earlier rounds' history lines above. Release's own C-17.8 citations, in `daemon.py`, `doctor.py`, `test_launch_env_round4.py` and the launch-environment report, are unchanged.
