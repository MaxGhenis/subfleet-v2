# Steer daemon implementation report

Implemented the daemon side of the fixed `steer.v1` interface. A queued person
message can be claimed for a running Claude or Codex turn, handed over through
the existing guardian relay, correlated with provider evidence, and settled
before its host. It never receives its own turn job when delivered as a steer.

The requested provider claim table is
[steer-provider-claims.md](steer-provider-claims.md). It distinguishes static
confirmation from live behavior and records the timeout contradiction plus a
cancellation caveat: an unacknowledged timeout cancellation does not prove
non-delivery, and consumed Claude folds can later be called cancelled. The latter
reinforces the design’s existing consumed-first settlement precedence.

## Changes by design section

| Design section | Implementation and file anchors |
| --- | --- |
| §2 protocol and states | `subfleet/protocol.py:22` adds `message.steer` immediately after `message.cancel`; `subfleet/conversations/service.py:64` and `:243` advertise `steer.v1` and `steer_providers`. `subfleet/conversations/turn.py:23` adds live `steering` and terminal `steered`; `:111` adds `Outcome.steers`. `subfleet/conversations/store.py:1473` derives `steered_into` without changing schema 2; receipts, status and change rows carry it. |
| §3 service validation and durable claim | `subfleet/conversations/service.py:675` validates person-only intent, queued state, runner availability and steerable phase, then durably claims before queueing the runner command. `subfleet/conversations/store.py:1045` checks queue head/repair priority, host state, stop/blocked/archive flags, and permission widening in the claim transaction. Idempotent `steering`/`steered` requests return receipts. |
| §3 runner and handover | `subfleet/conversations/runner.py:215` exposes current steerability; `:370` restores written tags before stdout replay and defers unwritten claims; `:399` handles the command and host-shaped attachments. `:593` and `:717` serialize actual writes against both host stop and steer cancel, taking striped locks in a consistent order. `:443`, `:733` and `:748` account for withdrawn, oversized and relay-lost frames. An oversized steer requeues without closing stdin. `:763` runs the bounded unseen-steer watchdog; `:860` merges provider and relay facts into `turn.json`, preserving recorded positive delivery evidence. |
| §3 settlement and recovery | `subfleet/conversations/service.py:2093` settles children before the host; `:2162` maps consumed/delivered to `steered`, unanswered Codex echoes to `steered-unanswered`, proven misses to their original queue sequence, and uncertain writes to `delivery-unknown`. `:1903`, `:2021` and `:1575` extend replay, unstarted settlement and retention pins to child bindings. `:699` permits cancel only before handover; `:796` routes interrupt of a live steer to its host; `:1307` refuses handoff with a live steer and skips terminal steered history. |
| §4 Claude driver | `subfleet/conversations/claude_turn.py:240` emits `priority:"next"` messages with the same content/image shape as the host, requiring `msg_lifecycle_v1`. `:252` bounds unseen messages after a result. `:668`, `:710` and `:734` process result consumption IDs, lifecycle ordering, and cancellation/interrupt receipts. Folded steers and steers that become their own provider turn both work; approvals remain active until the final result. Stop and cancel-turn approvals sweep queued commands when supported. Late cancellation cannot erase established delivery. |
| §4 Codex driver | `subfleet/conversations/codex_turn.py:207` holds steer input until the active turn ID is known, then emits schema-validated `turn/steer`. `:304` handles response success/refusal even after the host has ended; `:608` correlates `userMessage.clientId` and tracks unanswered delivery. Subsequent agent work marks it answered; compaction and unknown items do not. `:621` treats accepted but unechoed input as cancelled only after a proven interrupted terminal, with late echoes taking precedence. Requested interrupt or unexplained EOF alone leaves delivery unknown. |
| §4 fake providers | `tests/fake/interactive_claude.py:365` implements UUID lifecycle, queued cancellation, result consumption IDs and result numbering; `:407` folds at a tool boundary, while `:414` misses that boundary and runs the queued input after the first result in the same process. `tests/fake/interactive_codex.py:225` implements active-turn validation, steer RPC responses and client-ID echoes; `:290` exercises answered/refused/unanswered scenarios. |
| §5 contract | `docs/acceptance-contract.md:9` adds the change-list line; `:366`–`:370` amend C-24.5/C-24.6/C-24.7 and add C-24.9; `:375`, `:387`, `:388` and `:419` amend C-25.2, C-26.5, C-26.6 and C-29.7. `docs/desktop/ledger.json:134` adds the corresponding M-9b acceptance row, kept open pending the app UI and live verification. |
| §6 fixed app interface | Interface names, op order, refusal codes, states/reasons, binding fields, host-stream events, frame tag and driver signature are retained. Only a minimal Swift protocol/test bridge was added in `app/Sources/Protocol.swift:107` and `tests/frontend/CoreProbe.swift:81` so the existing cross-language contract tests can round-trip the new op and fields. Composer/timeline UI implementation and an app build are outside this daemon-side change. |

`subfleet/conversations/service.py:833` also preserves the host’s unfinished-turn
block while several ambiguous steer deliveries are resolved in any order.
The internal resolution metadata carries that requirement without changing the
fixed Receipt shape or the database schema.

## Design adaptations and limits

- No §6 interface change was necessary.
- Claude silence is bounded by 15 seconds before requesting cancellation and
  another 15 seconds for its receipt. A positive cancellation proves a miss;
  missing/negative acknowledgement leaves `delivery-unknown`. The design's
  unconditional timeout-to-missed wording would risk duplicate delivery because
  `cancelled:false` also means the command has already been dequeued. A steer
  already picked up for its own turn can run normally without this unseen-send
  timer cutting off legitimate tool work.
- Delivery evidence outranks later negative lifecycle evidence, including
  evidence preserved from an earlier `turn.json`. This implements exactly-once
  settlement despite Claude's documented cancellation of already-consumed folds
  on turn failures.
- The requested Codex fixture does not retain a steer success-response schema
  or the numeric mapping of no-active-turn/mismatched-turn errors. Those exact
  details remain unverified. The driver treats all RPC errors as refusal and
  requires an exact history client-ID match to establish delivery.
- A live steer with no registered runner cannot be cancelled optimistically:
  cancellation conservatively returns `too-late` until the runner can establish
  whether durable relay intent exists. This avoids calling an ambiguous write
  unsent during adoption/restart.

## Invariants and tests

| Invariant | Tests and what they exercise |
| --- | --- |
| 1. Exactly once: one final disposition; no delivered message requeued or lost | `tests/unit/test_steer_invariants.py:63` generates mixed fate/frame/cancel histories and repeated settlement through the real service/store; it checks one disposition, no own jobs and unchanged event/watch history on replay. `tests/unit/test_claude_turn.py:589` protects consumed folds from late cancellation. `tests/unit/test_turn_runner.py:972` protects recorded delivery from negative replay evidence. |
| 2. Queue order survives misses | `tests/unit/test_steer_invariants.py:100` generates mixed consumed/missed children and checks original sequence and dispatch order before later messages. `tests/e2e/test_conversations.py:594` exercises a refused Codex steer returning to the queue. |
| 3. No writes after close, host stop or own cancel | `tests/unit/test_steer_invariants.py:157` generates barriers and repeated outbox sends against a durable journal. `tests/unit/test_turn_runner.py:855` generates commands for both providers; focused real-relay races begin at `:597` and `:649`. `tests/e2e/test_conversations.py:612` cancels before handover. |
| 4. Restart never rewrites a steer and preserves settlement | `tests/unit/test_steer_invariants.py:180` generates restarts around claim, framing, handover, consumption and settlement. `tests/unit/test_turn_runner.py:888` adds generated Claude/Codex replay boundaries; `:677`, `:952` and `:981` cover written-log/status-handshake recovery. `tests/e2e/test_conversations.py:660` restarts with a native steer in flight; fake-process integration coverage is in `tests/fake/test_interactive_steer.py:146`. |
| 5. Ordinary host delivery/outcome rules stay intact | `tests/unit/test_steer_invariants.py:216` varies host acknowledgement, success/failure and replay count, checking unchanged delivery, one host frame, one close, and no steer facts. Existing driver tests remain intact; the driver files have 101 passing cases after the final driver changes (74 existing plus 27 added). |

Additional coverage includes claim authorization and all refusal paths,
permission narrowing, repair priority, cancellation, multi-unknown resolution,
preservation of unfinished Claude hosts, missing-outcome delivery conservatism,
retention/adoption and settlement idempotence in
`tests/unit/test_conversation_steer_service.py`. Driver tests exercise both
Claude result/lifecycle orders, silent drops, the exact cancellation frame,
Codex held turn IDs, late responses/items, unanswered echoes, and interruption
without terminal evidence. Requested e2e cases are present at
`tests/e2e/test_conversations.py:560`, `:579`, `:594`, `:612`, `:643` and `:660`.

## Validation results

The full requested selection completed: **6,370 passed, 140 failed, 91 skipped,
37 setup errors in 9703.24 s (2:41:43)**. It is not a green full-suite result.
The long run began before the final additions and fixes; the final-source
file-level checks below include them. These are separate runs, not additive
test counts.

The 140 failures and 37 setup errors were classified from their tracebacks:

| Cause / final disposition | Failures | Setup errors |
| --- | ---: | ---: |
| Sandbox denies boot/process inspection: 124 unit and 12 fake failures | 136 | 37 |
| Two fake-routing timing failures, both passing alone | 2 | 0 |
| Unchanged 0.5-second guard probe fixture, still failing alone | 1 | 0 |
| Missing new-clause ledger citation, fixed and rechecked | 1 | 0 |

The environment group includes unavailable macOS boot identity, denied
`/bin/ps`, conservative unknown-process decisions, and guardian/probe
quarantine when group, descendant or marker enumeration is unavailable.
Eight fake failures were reproduced individually with those diagnostics; the
six session tests share the same blocked guardian/probe path. Four other fake
failures explicitly reported unavailable boot identity in the original run.
These were not called load flakes.
No driver, runner, replay, retention or steer-invariant failure appeared in the
broad run. Its two conversation-unit failures were separately reproduced as
the boot-identity restriction and a denied `/bin/ps` call.

The full frontend selection within that run was **110 passed, 1 skipped,
0 failed, 0 errors**. The skipped case is
`test_core_live.py::test_the_app_core_drives_a_development_daemon`.

The broad run caught a missing acceptance-ledger citation for the new C-24.9
clause. The M-9b row above fixes it without claiming that the app UI or live
provider verification is complete. Its rerun passed **4 tests in 125.99 s**.

Requested command:

```sh
.venv/bin/python -m pytest -q tests/unit tests/fake tests/e2e/test_conversations.py tests/frontend/test_core_*.py
```

Environment: macOS, CPython 3.14.7 free-threaded, pytest 9.1.1 and Hypothesis
6.168.1, installed with `uv sync --dev --frozen`. The host was heavily loaded
during validation (one measured one-minute load average was 85.38 on 18 CPUs).

Final file-level focused validation covers **215 tests**:

| Coverage | Passed |
| --- | ---: |
| Claude and Codex drivers | 101 |
| Runner, including generated replay and write-barrier schedules | 56 |
| Service/store steer operations and settlement | 27 |
| Five Hypothesis invariant tests | 5 |
| Fake CLI protocol and composed runner/driver/service integration | 13 |
| Python/Swift protocol round trips | 9 |
| Acceptance-ledger consistency | 4 |

A combined run before the final three added cases reported **207 passed, 1
failed**. Its sole failure was the unchanged
`test_a_frame_in_flight_when_the_runner_starts_is_not_taken_for_a_failure`:
the constructor missed the test's 300 ms pending-write window under load.
An isolated rerun passed (**1 passed in 9.22 s**), as did the full final runner
file (**56 passed**). This is a confirmed load flake, not a skipped test.
`git diff --check` passed.

The broad run's two fake-routing timing failures also passed alone, unchanged:
`test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again`
(**1 passed in 27.18 s**) and
`test_c6_9_a_retry_that_lets_its_pin_go_keeps_its_place_behind_older_jobs`
(**1 passed in 127.57 s**). These are confirmed load flakes involving a
two-second override and a wall-clock deadline.

An unchanged guard fixture remains unresolved:
`tests/unit/test_guard_trust.py::test_reap_failure_never_hides_the_probe_diagnostics[slow-reap]`
failed again alone (**1 failed in 113.82 s**) because its report file did not
exist after a 0.5-second probe startup timeout. It is **not** labelled a
recovered load flake or a proven steer regression. The guard and fixture
sources are unchanged by this work.

The final-source existing service/store/handoff/status/replay/retention files
reported **199 passed, 2 failed in 1161.63 s** (201 cases). The FIFO history
case exceeded its 60-second child timeout; the close-request test could not
construct a real Daemon because macOS boot identity is unavailable. Selecting
those two cases again yielded **1 passed, 1 failed in 53.61 s**: the FIFO case
passed and boot inspection failed again. The FIFO-only rerun also passed
(**1 passed in 31.24 s**), confirming that original timeout as a load flake.

The managed sandbox denies process-inspection operations required by parts of
the daemon/e2e harness (`ps`/`sysctl`), so affected real-daemon launch/containment
checks must be distinguished from implementation failures. The fake-provider
and runner integration tests run without live model calls. No existing tests
were deliberately skipped to hide these environmental failures.

The separately completed full `tests/e2e/test_conversations.py` run reported
**0 passed, 0 failed, 36 skipped in 30.10 s**. Its existing fixture requires
permitted process inspection and reports `macOS boot identity is unavailable`
in this sandbox. All seven new e2e cases are among those skips; composed fake
process integration tests cover the core steer flows without that prerequisite.

The separate full `tests/frontend/test_core_*.py` run reported **111 setup
errors in 1048.01 s**: its shared Swift probe compilation exceeded the existing
900-second timeout, so no test body ran. Its first case passed alone with
unchanged sources and timeout (**1 passed in 696.83 s**), confirming a
load-sensitive compilation timeout. The full combined run subsequently
compiled the same sources successfully and passed all 110 runnable frontend
cases, as reported above.

## Workspace and commit status

All edits are in the assigned workspace. No caller checkout, running daemon,
launchd configuration or user Subfleet state was modified. The sandbox rejects
the worktree's Git `index.lock` write, so commits could not be created; changes
remain uncommitted as the user's fallback rule permits. No history was rewritten
and nothing was pushed. Any eventual commits must retain the requested trailer:

```text
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## Remaining live verification

No live model call was attempted. The supplied default-login run hit its weekly
quota before a tool boundary and never sent its second message. The first live
steer after installation must confirm Claude's actual fold/own-turn timing,
cancellation receipt timing and result correlation, and Codex's actual response
shape, error mapping, client-ID echo and answered/unanswered behavior. The
implementation and tests accommodate either Claude fold or own-turn behavior;
static claims and fake tests are not represented as live validation.
