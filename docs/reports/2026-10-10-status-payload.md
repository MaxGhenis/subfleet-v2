# D-ST2: bounded current-capacity status

Audited and measured in the assigned checkout of 5b92a28ee32b. No live
`~/.subfleet` store was read, no providers were called, and no network push was
made. The measurements below use disposable synthetic stores.

## Consumers and required fields

Locations refer to the final source. None of the production consumers below
reads `attempts[].evidence_json` from a capacity view or status response.

| Consumer | Location | Fields needed |
| --- | --- | --- |
| CLI `status --json` | `subfleet/cli.py:484`, `:512` | Passes the entire response through without inspecting attempts. All top-level keys remain; the attempt projection and job membership are documented in C-16.2/C-17.4 and help. |
| CLI status table | `subfleet/cli.py:337`, `:252`, `:274` | `now`, `lanes`, `readings`, `weekly_samples`, `closures`, `alerts`, `claude_cards`, `disk`, `priority_callers`, `admission`, `jobs` (or legacy `running`/`turns`). Lane id/provider/account/owner/desktop/enabled/identity status/in-flight; reading lane/window/scope/utilization/label/observed/reset times; closure lane/scope/until/reason/clock source; nonterminal job id/state/kind/workdir/caller/rc/name and optional ledger display columns. Every existing job column is retained for included jobs. |
| Daemon's status text, also `readings.status` | `subfleet/render.py:134`, `:173`, `:179`, `:192` | Lanes, readings, weekly history, active closures, `now`, `reading_ttl_s`, disk; active attempt `job_id`, `attempt_id`, `lane_id`, `model_requested`, `state`; job `job_id`, `kind`, `state`, `wait_reason`, `next_check_at`. No finished attempts or recent window. |
| Capacity counts and pilots | `subfleet/capacity.py:394`, `:435`; `subfleet/daemon.py:1251`, `:1266` | Attempts' `state`, `lane_id`, `job_id`; jobs' `job_id`, `kind`; pilots additionally `attempt_id`, `reserved_at`. Quarantined attempts remain visible but never count as in-flight. `seq` is retained as part of the row identity/order. |
| `daemon.status` diagnostics | `subfleet/daemon.py:2951`, `:3047` | Rendered status, pid/version/state root, timers/alerts/cards, active count, admission (including open lanes and route counters), priority callers, descriptors, read pool, wait hub. All remain. Disk's separate indexed read extracts only `disk_reservation` from live evidence (`:2965`); it does not return evidence blobs. |
| CLI daemon status / doctor live | `subfleet/cli.py:2537`, `:2545`; `subfleet/doctor.py:532` | `descriptors`: limits, open count, connections/cap, refused/idle-closed/abandoned/accept-failure counts. |
| Daemon-start health and reap | `subfleet/cli.py:2333`, `:1731` | Successful response or daemon error only; no result fields. |
| `lanes hold` / release | `subfleet/daemon.py:1134`, `:1160`; `subfleet/cli.py:2045` | Capacity-enriched `lanes`, active `closures`; CLI reads `held`/`released` or passes JSON through. Attempts/jobs needed only to build lane counts and pilots. |
| `lanes list` | `subfleet/daemon.py:2846`; `subfleet/cli.py:2095` | Enriched lane rows and lane leases; no attempt history. |
| `readings` | `subfleet/daemon.py:2848` | Latest readings, active closures, rendered status text. Same live attempt/job needs as the status renderer. |
| Native lane picker | `subfleet/daemon.py:2759`; `subfleet/picker.py:29` | Capacity lanes/readings/closures, clock/TTL, in-flight maps, probe/pilot blocks and lane leases. Scheduler counts need live attempt id/job/lane/state and job kind. Picker candidates have no parent, so retained ancestor jobs are not needed here. |
| Native brief / previews | `subfleet/operations.py:140`, `:46`, `:148`; `subfleet/timers.py:904`; `subfleet/alerts.py:36` | Enriched lanes, latest readings, closures, clock, probe/credential/reset marks and fleet counts. No finished attempt history or evidence. |
| Admission idle logging | `subfleet/daemon.py:4637`; `subfleet/capacity.py:599` | Open lane ids from enriched lanes, readings, closures and probe/pilot blocks. |
| `why`, dry-run routing, admission routing | `subfleet/daemon.py:2851`, `:2993`, `:1729` | **Route path**, not the non-route snapshot: `ROUTE_ATTEMPTS`' seven fields and `ROUTE_JOBS`' `job_id`, `parent_job_id`, `state`, `kind`. Stored decisions and target-job/queue reads are separate. These SQL statements and their ancestry behavior are unchanged. |
| status.json writer / app | `subfleet/timers.py:864`, `:291`; `subfleet/capacity.py:628`; `subfleet/status_json.py:192`, `:202`, `:211`, `:239` | **Separate timer snapshot**, not `daemon.status` or `_capacity_rows`. Needs every live job, 8 recent finished detached jobs, and their latest attempts (`job_id`, `seq`, `lane_id`, `model_requested`); job name/state/kind/reason/check time/sandbox/worktree/workdir/pin/timestamps/rc, batch labels, conversations and enriched lanes. Its existing full history read and recent menu are unchanged. |
| MCP status tools | This revision's `subfleet/adapters/claude_mcp.py:1`, `tests/unit/test_mcp_cli_contract.py:1`, `tests/unit/test_mcp_scheduler_contract.py:1` | No MCP status-tool/server implementation exists in this checkout. These modules configure external MCP opt-in and route jobs; they do not consume status. An external wrapper invoking `status --json` receives the documented CLI response. Installed external wrappers are outside the assigned checkout and were not inspected. |
| Measurement/harness callers | `tools/measure_release_gates.py:130`; `tools/store_contention_repro.py:586`, `:608`; `tests/fake/conftest.py:73` | Round-trip success only; contention harness also keeps `admission`; fake daemon health checks the response envelope's `ok`. |

Direct test consumers and their assertions:

| Test locations | Fields / behavior |
| --- | --- |
| `tests/fake/test_route_view.py:99`, `:100`, `:107`, `:243` | Route decisions/counts/parent ancestry; attempts' identities/states/evidence omission and jobs' ids/membership. The differential oracle explicitly reads the full ledger so narrowing operator views cannot weaken route equivalence checks. |
| `tests/fake/test_status_payload.py:11`, `:61` | Wire size, actual query plans, all five included states, projection, job membership, counts, hold closure, and equality of both renderers with the old full view. |
| `tests/fake/test_admission_visibility.py:100`, `:325`, `:337`, `:432` | Priority callers, admission pending/open lanes/reasons/idle times, jobs' ids, read-pool fields, wait-hub counters. Historical-job membership assertion updated. |
| `tests/fake/test_admission_disk.py:74`, `:109`; `test_admission_route_isolation.py:175` | Disk reservations/floors/free space, rendered text, priority callers and admission reasons. |
| `tests/fake/test_admission_route_isolation.py:610`, `:713`; `test_admission_liveness.py:153` | Latest reading lane/label, lane roster and scheduler decision evidence. |
| `tests/fake/test_admission_priority.py:201` | Desktop registry read/record behavior; no status result fields. |
| `tests/fake/test_routing_end_to_end.py:275`; `test_routing_tier_preferences.py:52`, `:66` | Lanes/latest windows/closures/counts, live job ids/kinds and attempt model/lane/seq for `build_status`; route choice. |
| `tests/fake/test_probe_turn.py:619`; `test_probe_recovery.py:115`, `:118`; `test_unadmittable_pin_admission.py:424` | Scheduler reach, lane blocks/counts, quarantine probe text, readings/closures/clock. |
| `tests/fake/test_timers_end_to_end.py:132`, `:156` | Timer probe last-run/status. |
| `tests/fake/test_descriptor_exhaustion.py:124`, `:166`, `:174`, `:221` | Descriptor counters/limits/connections and read-pool state. |
| `tests/fake/test_daemon_contract.py:296`; `test_state_contract.py:232`; `tests/unit/test_daemon_connections.py:262`, `:321`, `:374`, `:645`; `test_conversation_connections.py:248` | Status responsiveness, connection scheduling/retry and descriptors; no attempt evidence or history. |
| `tests/unit/test_daemon_integration.py:89`, `:110`; `test_status_json.py:761`, `:823` | Lane desktop/identity flags, probe holder/state and dispatchability. The latter compare the independent timer publication. |
| `tests/unit/test_operator_notices.py:289`; `test_render_quota_projection.py:264`, `:265`; `test_store_readers.py:471` | Active alerts; weekly history included only in operator reads; no read connection held during view building. |
| `tests/unit/test_cli.py:195`, `:724`, `:741`, `:794`, `:905`, `:1552`, `:1593`, `:1606`; `test_cli_turns.py:180` | CLI rendering/JSON pass-through, old-daemon full-ledger filtering, thin/malformed replies, health, descriptors, live turns. Older-daemon fixture shapes remain supported. |
| `tests/unit/test_doctor.py:541`, `:554`, `:561`; `test_reap_liveness.py:38`; `test_daemon_verbs.py:30` | Descriptor diagnostics and health only. |
| `tests/unit/test_render.py:30`, `:93`, `:165`; `test_status_json.py:36`, `:192` | Renderer fixtures (no daemon response read): live jobs/attempts and waiting/turn sections; timer live/recent menu and latest attempts. |

## Change and query plans

`subfleet/daemon.py:113` and `:123` define the operator queries used by
`_capacity_rows` at `:1237`. The attempt query selects seven columns and unions
four active states with quarantine. A hidden rowid tie breaker preserves the
old reservation-index order when timestamps and sequence numbers tie. The job query selects all columns of
nonterminal jobs or jobs of those attempts. `+created_at` prevents the job-order
index from attracting an all-history scan. Both queries sort only their selected
rows. `subfleet/store_schema.sql:155` adds the quarantine partial index on every
normal store open, including existing stores; no index is replaced.

`EXPLAIN QUERY PLAN` on the synthetic store gives:

- Attempts: `SEARCH attempts USING INDEX attempts_live (state=?)` and
  `SEARCH attempts USING INDEX attempts_quarantined (state=?)`.
- Jobs: `MULTI-INDEX OR`, `SEARCH jobs USING INDEX jobs_state (state=?)`,
  the same indexed attempt subqueries, and
  `SEARCH jobs USING INDEX sqlite_autoindex_jobs_1 (job_id=?)`.
- Both sort the small selected result; neither scans `attempts` or `jobs`.

`subfleet/cli.py:2916` documents online JSON membership and the exact attempt
fields in help. C-16.2's status clause and C-17.4 in
`docs/acceptance-contract.md:316` / `:328` document the bounded shape and the
full-history alternatives. All other top-level status keys and included job
columns remain. Response size still scales with current work/quarantine and
capacity readings, rather than retained finished jobs and attempt evidence.

## Before / after measurement

CPython 3.14.7 freethreaded, same host; five calls per operation after one warmup,
no serving socket or background admission. 8,000 newly finished attempts/jobs,
32 live attempts/jobs (one turn), one quarantined attempt of a cancelled turn,
one queued and one waiting job. Each attempt carries 12,288 payload bytes of
evidence. This deliberately tests even *recently* finished history.

Bytes include the normal protocol envelope and newline. Read/build time is
`dispatch`; total adds the daemon's protocol encoding (including dataclass
conversion). Baseline was measured before editing the daemon; the committed
tool's `--legacy` flag can reproduce the old read after the fix.

| Operation | Before bytes | After bytes | Before dispatch ms | After dispatch ms | Before with JSON ms | After with JSON ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| daemon.status | 111,889,603 | 46,602 | 125.36 | 0.64 | 352.93 | 0.93 |
| lanes hold | 1,300 | 1,300 | 114.50 | 0.83 | 114.54 | 0.85 |

Reproduce in a disposable TMPDIR under the Darwin user temp directory:
`python -m tools.measure_status_payload --repeats 5`, then the same command
with `--legacy`. The tool deletes its synthetic stores, locks and logs itself.

## Guards and validation

`tests/fake/test_status_payload.py:11` builds the large store, confirms evidence
alone exceeds 64 MiB, and requires both the status and lane-hold replies to be
under 4 MiB. It rejects `list_attempts`, checks the actual operator statements
executed and their indexed plans, and verifies lane/count/quarantine/waiting/turn
behavior. `:61` compares both renderers against a full-history snapshot and
checks every included job retains all its fields. `tests/unit/test_store_snapshot_plans.py:37`
guards the job plan; `:44` checks reopening an existing store adds the new index.
The route property tests retain their full-ledger oracle and unchanged route SQL.

Final suite: **778 passed, 2 skipped, 12 deselected in 75.42 s**. All pytest
runs were serialized with `/usr/bin/lockf -k
/private/tmp/claude-501/subfleet-suites.lock`; TMPDIR and Hypothesis storage
were under the Darwin user temp directory and removed after validation.
`git diff --check` passed. Shared Git metadata is read-only; the commit lives
in the workspace's `.git-local` repository on `fix/status-payload` (the final
response names its head).

| Files run | Passed | Automatically skipped |
| --- | ---: | ---: |
| `tests/fake/test_status_payload.py`, `tests/unit/test_store_snapshot_plans.py`, `tests/fake/test_route_view.py` | 14 | 0 |
| `tests/unit/test_cli.py`, `test_cli_turns.py` | 199 | 0 |
| `tests/unit/test_render.py`, `test_render_quota_projection.py`, `test_status_json.py`, `test_capacity_view.py` | 119 | 0 |
| `tests/unit/test_lanes_transfer.py`; `tests/fake/test_lanes_enroll.py`, `test_lanes_reenroll.py`, `test_lanes_reenroll_keychain.py` | 103 | 0 |
| `tests/unit/test_mcp_cli_contract.py`, `test_mcp_scheduler_contract.py`, `test_claude_mcp.py`; `tests/fake/test_mcp_job_contract.py` | 55 | 0 |
| `tests/fake/test_routing_end_to_end.py`, `test_admission_visibility.py`, `test_admission_liveness.py` (why/routing/status) | 89 | 2 |
| `tests/unit/test_doctor.py` | 42 | 0 |
| `tests/unit/test_gate_admission.py`, `test_legacy_hold.py` (hand-built Daemons) | 70 | 0 |
| `tests/unit/test_native_operations.py`, `test_picker.py`, `test_store_readers.py` | 87 | 0 |

Host-only cases: `/bin/ps` is denied and macOS boot identity is unavailable
in the sandbox. The unfiltered run had 12 failures for those restrictions;
the final run explicitly deselected those cases. No process-inspection code
or tests were changed to mask them. Run these on the hub's host:

- `tests/unit/test_cli.py::test_a_lock_whose_holder_is_dead_is_no_daemon`
- `tests/unit/test_cli.py::test_an_identity_mismatch_names_the_rendering_trap`
- `tests/unit/test_doctor.py::test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone`
- `tests/unit/test_doctor.py::test_daemon_lock_row_passes_against_a_live_daemon`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait[False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait[True]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[same-False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[same-True]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[upper-stored-False]`
- `tests/unit/test_legacy_hold.py::test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait[lower-stored-False]`
- `tests/unit/test_store_readers.py::test_the_daemon_answers_reads_while_a_transaction_is_open`
- `tests/unit/test_store_readers.py::test_capacity_views_hold_no_read_connection_while_they_build`

Automatically skipped for the same restriction:

- `tests/fake/test_routing_end_to_end.py::test_c11_5_socket_submission_and_cli_why_print_recorded_walk`
- `tests/fake/test_routing_end_to_end.py::test_c11_4_generic_adapter_probe_classifies_a_real_tiny_fake_turn`
