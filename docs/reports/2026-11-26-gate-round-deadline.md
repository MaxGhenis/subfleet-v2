# Issue #32 — gate round admission and queue deadline

Started at `2026-11-26T16:20:28Z`. The assigned checkout reported `a4d21501e` and had a detached HEAD. The hub's destination is `fix/gate-round-deadline-217`.

A gate now exits 4 before submission when every lane has a standing refusal or a closure beyond the queue deadline. A poll or continuation cancels an expired queued peer job before launch and moves its round to `unplaced_rounds`. A terminal peer job with no attempt takes the same path. An approved continuation can end the old round and submit a fresh one in a single call.

## Changes by file

- `subfleet/gate/service.py` checks the peer job's routing shape against the scheduler before building the round. Start, continuation and an automatic re-run all use that path. C-6.12 still handles an unevaluable route at admission. Poll and continuation check job age and attempt existence, use the daemon's guarded cancellation, and journal the outcome as `round-unplaceable`. A continuation reports whether a peer is queued or already placed.
- `subfleet/contracts.py` adds the positive integer cap `gate_queue_s` with a default of 1800 seconds. The existing policy loader supplies and validates it.
- `tests/unit/test_gate_queue_deadline.py` covers 15 cases through the fake CLI, real gate service and synthetic daemon store.
- `tests/unit/test_gate_admission.py` adds the no-op timer merge to `core`. The existing cancellation methods needed no extra fixture attributes.
- `docs/acceptance-contract.md` updates C-23.53 and its version 3 changelog. `docs/gates.md` documents the cap, budget, and how to kill and reroute a queued Fable peer job.
- `docs/reports/2026-11-26-gate-round-deadline.md` records implementation and verification.

## Defect at `a4d21501e`

- `subfleet/gate/service.py:237–283` prepared, recorded, and submitted the round after the cap and revision checks. `subfleet/gate/round.py:57,66–72` pinned the peer model and gave the review job one attempt and a numbered lease. There was no routing check before the write.
- `subfleet/daemon.py:5085–5100,8194–8209,9831–9893` charged running time, exempted admission waits and failed pinned jobs only after a standing refusal. In `subfleet/scheduler.py:1073–1104` a closure within the pin hold horizon counted as a wait. A weekly reset could therefore keep the job queued. `subfleet/gate/service.py:325–343,437–446` kept polling or reported the queued peer as running, and `subfleet/gate/cli.py:68,114–116` bounded individual requests only.
- A queued kill set the job to `cancelled` with rc 130 in `subfleet/daemon.py:5346–5353`. `subfleet/gate/service.py:345–423` consumed the round in place, using a budget slot. The fail-first test observed that the missing lease check at lines 397–402 replaced the initial dispatch error with the abandoned-output error in the cap message.
- The issue's Opus peer came from unmerged PR #28. The available peers were Fable and Astra at `subfleet/gate/cli.py:39`. `subfleet/scheduler.py:1051–1053` excludes the reserved model itself and other providers from the Fable reserve, so neither peer could hit `reserve:fable:unmeasured` on this base.

## Invariants

Case numbers refer to `tests/unit/test_gate_queue_deadline.py` in source order.

| Invariant | Verification |
| --- | --- |
| 1. An unplaceable round leaves `rounds` unchanged and creates no round directory or job. | Cases 1, 2 and 4 verify the gate state, no submission, the journal transition, directory absence and CLI exit 4. |
| 2. The budget retains rounds with an attempt and failed submissions. The latest queued round can remain pending until a poll or continuation handles its expiry. | Cases 5, 6, 7, 8, 9, 10 and 15 verify pending, expired, terminal and admitted rounds. The existing failed-submit path stays in `_submit`. |
| 3. The deadline cannot cancel a job with an attempt. | Case 10 checks the live attempt and case 11 checks `without_attempt=True` and a cancellation refusal. The flag is enforced in the same transaction as cancellation at `subfleet/daemon.py:5303–5304`. |
| 4. A placed round and a queued round under the deadline retain their ordinary consumption behavior. | Cases 3, 6 and 10 verify queuing, polling, and eventual approval. Case 10 confirms `finish` did not admit a second attempt. The existing `test_round_lease_refuses_concurrent_continue` checks the running message. |
| 5. A fresh round after an unplaced round can submit and admit under the same budget. | Cases 7, 8 and 9 exercise fresh submission. Case 7 admits the replacement and issues a certificate under `--max-rounds 1`, with round number 1 and a distinct token. |
| 6. Continuation without approval can end and report an expired round and submits no replacement. | Case 15 verifies code 4, cancellation, the history move and a single job. |

## Fail-first and post-change

The valid fail-first run used base production code and the new tests and fixture. Its pytest short summary was:

```text
=========================== short test summary info ============================
FAILED tests/unit/test_gate_queue_deadline.py::test_queued_round_ends_unplaced_after_deadline
FAILED tests/unit/test_gate_queue_deadline.py::test_round_after_an_unplaced_one_has_the_full_budget
FAILED tests/unit/test_gate_queue_deadline.py::test_far_closure_refuses_the_round_before_submit
FAILED tests/unit/test_gate_queue_deadline.py::test_unknown_pinned_lane_refuses_before_submit
FAILED tests/unit/test_gate_queue_deadline.py::test_continue_ends_an_expired_queued_round_and_starts_fresh
FAILED tests/unit/test_gate_queue_deadline.py::test_cli_exits_4_for_an_unplaceable_round
FAILED tests/unit/test_gate_queue_deadline.py::test_killed_queued_round_spends_no_round
FAILED tests/unit/test_gate_queue_deadline.py::test_cancel_that_finds_an_attempt_keeps_waiting
FAILED tests/unit/test_gate_queue_deadline.py::test_queue_deadline_default_and_refusals
FAILED tests/unit/test_gate_queue_deadline.py::test_queued_continue_names_the_queue
FAILED tests/unit/test_gate_queue_deadline.py::test_continue_without_approval_reports_the_unplaced_round
11 failed, 4 passed in 52.02s
```

Cases 1, 2, 4, 5, 7, 8, 9, 11, 12 and 15 failed at the intended behavior. Case 14 rejected all five invalid values before its default assertion raised `KeyError: 'gate_queue_s'`. Cases 3, 6, 10 and 13 held by construction and passed. The CLI in case 2 returned 0 on base because the fake peer completed. Case 9 reported the cap with the abandoned-output error described above.

The first post-change run exposed that some `gate.state` events have empty data and the new journal assertion indexed those events directly. After changing the assertion to read optional fields, all 15 cases passed in 43.59 seconds.


## Mutation checks

Each mutant ran the full 15-case module in isolation. All eight were caught.

| Mutant | Edit | Failing cases |
| --- | --- | --- |
| M1 | Remove routing preflight | 1, 2, 4 |
| M2 | Use a zero closure horizon | 3 |
| M3 | Omit the transactional cancellation guard | 11 |
| M4 | Retain the unplaced record in the budget | 5, 7, 8, 9, 15 |
| M5 | Skip terminal jobs | 9 |
| M6 | Reverse the age check | 5, 6, 7, 8, 11, 12, 15 |
| M7 | Return immediately even with approval | 8 |
| M8 | Fall through without approval | 15 |

## Commands and counts

Each named run used the following command with a fresh basetemp. For the first
`fail-first` run, `job` was `$PWD/build/grd-build-r1`. For the later runs it was
`grd-build-r1` inside the sandbox's dedicated Darwin temp attempt directory.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$job/pytest-$run" -q \
  tests/unit/test_gate_queue_deadline.py
```

The `full` run used the same pytest options with `--basetemp "$job/pytest-full"`
and this module list.

```sh
  tests/unit/test_gate_queue_deadline.py \
  tests/unit/test_gate_service.py \
  tests/unit/test_gate_admission.py \
  tests/fake/test_gate_end_to_end.py \
  tests/fake/test_launch_gates_above_stdio.py \
  tests/unit/test_gate_merge.py \
  tests/unit/test_gate_replay.py \
  tests/unit/test_gate_revision.py \
  tests/unit/test_gate_verdict.py \
  tests/unit/test_store_contention_queries.py \
  tests/unit/test_policy.py \
  tests/unit/test_cli.py \
  tests/fake/test_state_contract.py \
  tests/unit/test_invariants_index.py \
  tests/unit/test_test_module_names.py \
  tests/unit/test_handler_refusals.py
```

The table records the pre-run UTC time, `df -H /` availability, and `uptime` load
averages. After each run, `du -sk` measured the basetemp and guarded deletion
removed it. All basetemps stayed below 200 MB.

| Run | UTC on Nov 26 | Free on / | Load averages | Passed / failed / skipped | Basetemp (KiB) |
| --- | --- | --- | --- | --- | --- |
| `fail-first` | 16:26:09 | 76G | 62.14 51.65 56.86 | 1 / 14 / 0 | 4084 |
| `fail-first-neutral` | 16:27:28 | 76G | 41.20 47.61 54.91 | 4 / 11 / 0 | 4164 |
| `after-change` | 16:31:06 | 75G | 37.51 51.14 55.31 | 14 / 1 / 0 | 4268 |
| `after-change-2` | 16:32:47 | 75G | 55.59 51.48 54.83 | 15 / 0 / 0 | 4232 |
| `M1` | 16:34:45 | 75G | 72.31 58.76 57.24 | 12 / 3 / 0 | 4524 |
| `M2` | 16:37:15 | 75G | 36.43 53.62 55.73 | 14 / 1 / 0 | 4164 |
| `M3` | 16:38:45 | 75G | 22.89 44.74 52.10 | 14 / 1 / 0 | 4268 |
| `M4` | 16:41:21 | 75G | 53.29 46.08 51.16 | 10 / 5 / 0 | 4132 |
| `M5` | 16:43:12 | 75G | 70.71 55.69 54.40 | 14 / 1 / 0 | 4180 |
| `M6` | 16:45:58 | 75G | 68.83 60.52 56.55 | 8 / 7 / 0 | 4264 |
| `M7` | 16:48:01 | 74G | 63.77 62.59 58.01 | 14 / 1 / 0 | 4256 |
| `M8` | 16:51:11 | 75G | 39.42 57.35 57.10 | 14 / 1 / 0 | 4092 |
| `full` | 16:52:33 | 75G | 33.98 50.93 54.63 | 1371 / 3 / 10 | 126656 |

The full run took 267.62 seconds. All gate tests passed. Three C-17.9 tests in
`tests/unit/test_cli.py` exceeded their 0.75-second wall-clock assertions:

- `test_ping_regular_file_is_read` took 1.033 s.
- `test_ping_open_pipe_is_bounded[partial message]` took 0.998 s.
- `test_suggest_open_pipe_is_bounded` took 0.999 s.

The tests run the unchanged `ping` and `suggest` stdin handlers against a
stub socket server. They do not dispatch a gate operation or use the queue cap.
The high pre-run load is recorded above. The 16-module run was performed once.

## Limits

- Preflight evaluates standing refusals and closures beyond the horizon. Reserve decisions stay with admission.
- `--dry-run` still previews the artifact and routing arguments only.
- The CLI still waits using its existing polls, with no extra queued progress line.
- The queue deadline runs on `gate.poll` or `gate.continue`. After a disconnect the durable job can still be admitted before the next gate call, and its verdict counts if it is.
- Killing a peer job that has an attempt still spends a round.

## Workspace and deviations

The first `git add` of the two named test files was denied with `Operation not permitted` creating the assigned worktree's `index.lock`. HEAD remains detached at `a4d21501e` and the commit range is empty. The worktree carries the changes for Subfleet salvage. Each mutant restores the saved implementation byte for byte and verifies the worktree diff against the pre-mutant diff. Restoring from HEAD would discard the implementation because staging was denied.

The checkout arrived without `.venv`. The existing Darwin temp uv cache could be read but uv could not open it for writing. I copied its required shards into the worktree's job folder and ran `uv sync --group dev --offline`. Both pytest and xdist resolved offline. The Darwin user temp directory denied creation of the top-level job folder, so the first fail-first attempt followed the worktree fallback. Real gate submission rejects a peer cwd inside the repository, which invalidated that attempt. The valid fail-first and all later runs used a job folder named `grd-build-r1` inside the sandbox's writable Darwin temp attempt directory. A temporary pytest plugin redirected the shared failed-test state path into that job folder. State, provider commands and desktop identity were synthetic throughout.

The full basetemp reached 126,656 KiB. With the copied uv cache and venv, the
combined scratch allocation briefly reached about 203 MiB, above the 200 MB
budget. Test runs used CPython 3.15.2, pytest 9.1.1 and xdist 3.8.0.

The job-created directory outside the worktree was
`$DARWIN_USER_TEMP_DIR/subfleet-attempt-20261126-171636-grd-build-r1-a1-uu2l6ilg/grd-build-r1`.
Guarded cleanup removed it and its contents. The pre-existing shared uv cache
was left in place. The copied cache, temporary venv, and test artifacts inside
the worktree were also removed.
