# Descriptor follow-ups and app busy handling

Continued checkpoint `3e38833b9414f4b54a2c1c11a33e952c89e9b721`. Its tree matches preserved snapshot `refs/subfleet-salvage/detached-20260925T232746Z-a1`; no preserved changes were discarded. The assigned checkout remains detached at that checkpoint; no caller branch, history, remote, installed daemon, or user state was changed.

The checkpoint already implemented the requested follow-ups. This continuation found and fixed two remaining problems: a reader could execute a request before a failing `submit` returned a busy refusal, and CLI/gate retry transport calls could outlive the original deadline. It also made the regression evidence runnable without listener permission and removed races in the abandoned-wait and dropped-accept tests.

## Requested items

All Python paths below are relative to the repository. Daemon test names refer to `tests/fake/test_daemon_descriptors.py`; CLI test names refer to `tests/unit/test_cli.py`.

| Item | Implementation and changes | Regression and fail-without-change evidence |
| --- | --- | --- |
| F1: count readers | `subfleet/daemon.py:361`, `:3940`, `:4032`: `_reading` is added at admission and released in the reader's finally; `_connections` remains for shutdown. | `test_clients_that_left_their_waits_no_longer_count_against_the_cap` (`:421`): MAX_CONNECTIONS=2, both waits confirmed running before clients close, then ping succeeds. **Fails** when cap counts `_connections`, and independently when reader slots are not released. |
| F1: cancel pending / arrival deadline | `subfleet/daemon.py:3955`, `:3979`, `:2046`: cancel unstarted requests when the client fully leaves; preserve half-close replies; propagate arrival time to wait. | `test_a_request_no_pool_has_started_is_dropped_when_its_client_leaves` (`:447`) **fails** without cancellation; `test_a_wait_that_starts_after_its_deadline_answers_at_once` (`:485`) **fails** without arrival propagation. Half-close coverage at `:464` passes. |
| F4 | `subfleet/daemon.py:4006`, `:4026`: SOMAXCONN backlog and corrected macOS drop comment. Dropped-accept clients no longer race a send against an intentional close. | `test_accept_drops_failed_socket_and_serves_next_pair` (`:215`) closes the real socket endpoint before EMFILE, then serves the next ping. **Fails** independently without accept recovery and with backlog 64. Original real-listener test (`:144`) and queue-capacity test (`:178`) are preserved but skipped here because bind is denied. Comment-only changes are reviewed directly. |
| F6 | `subfleet/daemon.py:4032`: catch reader-submit RuntimeError, roll back admission, refuse busy, throttle logging. **New fix:** hold the admission lock through submission/rollback so a queued worker cannot dispatch before a failed submit is known. | `test_a_reader_queued_before_submit_raises_never_dispatches` (`:538`): forces worker entry before submit raises, checks no dispatch, 69, empty counts, one warning, and subsequent ping. **Fails against the checkpoint's admission method**. Original listener recovery test remains at `:509`. |
| F8 | `subfleet/cli.py:1019`, `:1028`, `subfleet/client.py:84`, `subfleet/gate/cli.py:70`: retry busy with increasing backoff. **New fixes:** transport timeouts use remaining time; gate stops when backoff reaches its deadline. | `test_wait_busy_backoff_and_deadline_without_a_listener` (`:1490`), four wait/kill variants: **all fail** without retry or the strict transport bound. `test_gate_busy_retries_share_one_poll_budget` (`:1538`) and `test_gate_busy_deadline_prevents_an_extra_poll` (`:1555`) **both fail against checkpoint**. Existing gate recovery test also **fails** when busy retry is removed. |
| F9 | `subfleet/daemon.py:1809`, `:4059`, `:4088`: report reading/open/cap/refused_busy counts. `subfleet/cli.py:1952`: answered DaemonError means reachable, with busy/refused detail and exit 0. | `test_daemon_status_reports_the_connections` (`:577`) **fails** when status omits counts. `test_daemon_busy_status_without_a_listener` (`tests/unit/test_cli.py:1520`) checks text and JSON and **fails** if busy becomes unreachable. |
| F10 | `subfleet/daemon.py:403`, `:405`: log clocks start at `-math.inf`. | `test_the_first_shortage_is_logged_in_the_machines_first_minute` (`:609`) **fails** when the two initial values become zero; it checks warning output at uptime 5s. |
| App F2/F7 | `app/Sources/Protocol.swift:67`, `:70` and `DaemonClient.swift:132`, `:469`: 69 is transient/retryable and availability busy. `ConversationStore.swift:841`, `UIModel.swift:151`: first successful watch after failure triggers another availability check. Existing app implementation preserved. Added scripted wire transport to the existing Swift probe so these behaviors can run without binding a socket. | `test_c16_1_scripted_busy_send_retries_after_backoff` (`tests/frontend/test_core_outbox.py:336`), `test_c29_2_scripted_busy_availability` (`tests/frontend/test_core_client.py:261`), and `test_c29_2_scripted_watch_recovery_rechecks_availability` (`:271`) all pass. **Each fails without its fix**: classification removal produces incompatible/failed states; recovery-callback removal omits regained/recheck. Original socket probes remain intact. |
| Contract | `docs/acceptance-contract.md:42`, `:43`, `:209`, `:213`, `:373`: C-16.1, C-15.4, C-29.2 and change list cover the requested behaviors; this continuation clarifies admission ordering and retry deadlines. | Reviewed against the regressions above; documentation itself has no behavioral mutation test. |

## Validation

Import check used the caller interpreter and confirmed `subfleet.__file__` is this worktree's `subfleet/__init__.py`. Every pytest invocation used:

```sh
PYTHONPATH=$PWD /Users/maxghenis/subfleet-v2-lanes/desktop-integration/.venv/bin/python -m pytest -q -p no:cacheprovider <relevant files>
```

Only requested file families were run. Test daemons and service harnesses use disposable state; catalog indexing is disabled. Full CLI runs additionally use a disposable SUBFLEET_HOME.

- Final daemon descriptors: **12 passed, 5 skipped** (12.74s). Skips require a bound Unix listener. The initial two accept-only test failures from denied bind were eliminated by replacing their already-faked listener setup, while still exercising the real accept loop.
- Daemon negative controls: **9 individual mutation runs each failed by assertion**; all source mutations restored. Evidence: `/tmp/descriptor-daemon-mutations.txt`.
- Final focused CLI busy/deadline/status cases: **9 passed**, 364 deselected (4.31s).
- Final gate files (all six `tests/unit/test_gate_*.py` plus `tests/fake/test_gate_end_to_end.py`): **196 passed, 1 skipped** (20.74s). Existing skip requires permitted process inspection.
- Broader CLI/gate run: **267 passed, 105 failed, 1 skipped** (81.51s). All failures were in CLI tests: 104 Unix socket bind denials, plus one process-identity diagnostic unavailable in this sandbox. No skip rules were added to hide these failures. Log: `/tmp/descriptor-cli-suite.log`.
- CLI negative controls: four wait/kill cases fail without retry; four fail without strict timeout; gate retry fails without retry; both new gate deadline tests fail on checkpoint; status fails when busy is unreachable. Evidence: `/tmp/descriptor-cli-mutations.txt`, `/tmp/descriptor-wait-deadline-before.log`.
- Final focused Swift probes: **3 passed**, 28 deselected (61.08s). Busy classification negative controls: **2 expected failures** (38.42s); removing the watch recovery callback: **1 expected failure** (22.75s). All production app files restored byte-for-byte; their final diff is empty.
- Initial broader Swift run: **86 passed, 1 skipped, 6 failed, 13 errors**; all 19 failures/errors were sandbox-denied Unix socket binds. The skip requires process inspection. An initial compiler cache permission issue was addressed with `CLANG_MODULE_CACHE_PATH` and `SWIFT_MODULECACHE_PATH` set to `/private/tmp/descriptor-swift-module-cache`, without changing user files. Full log: `/private/tmp/descriptor-swift-core.log`; focused log: `/private/tmp/descriptor-swift-scripted.log`.

Counts above describe separate runs and overlap; they are not summed. `git diff --check` passes.

## Commits and remaining work

Preserved implementation commits from the checkpoint history:

- `01e6f76`: daemon reader accounting, cancellation/deadline, accept/reader shortage handling and status.
- `403132a`: synchronize queued-request descriptor regressions.
- `2867115`: distinguish connection admission from job admission.
- `7eb0494`: CLI/gate busy retries and reachable-busy status.
- `088db87`: app transient busy handling, retrying outbox and recovery check.
- `3e38833`: acceptance contract and change list.

**No new commits could be created.** `git add` was refused with `Operation not permitted` while creating `/Users/maxghenis/subfleet-v2/.git/worktrees/20260925-192153-descriptor-followups-and-app-busy/index.lock`. Approval policy provides no escalation path. Changes remain in the assigned workspace for Subfleet salvage; no history was rewritten and nothing was pushed.

Intended coherent commits:

1. `Keep reader admission atomic when thread creation fails` — daemon fix and descriptor regressions.
2. `Bound busy retries by the original wait and gate deadlines` — CLI/gate fix and regressions.
3. `Probe app busy recovery without a listener and record follow-up validation` — Swift probe regressions, contract clarifications, report.

Each intended message ends with:

```text
Co-Authored-By: Codex (GPT-6) <noreply@openai.com>
```

Remaining environmental work: create those commits where git metadata is writable, and run the preserved listener/process-dependent integration tests in an environment that permits them. No known requested implementation item remains open; full integration success is not claimed in this sandbox.
