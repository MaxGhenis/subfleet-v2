# Conditional task holds and releases (D-TASKHOLD-CAS1)

2026-11-07. Base: `a8624a4899a2`. All source and report changes stayed in the assigned workspace; tests and mutants used permitted temporary directories.

Added `--if-note-prefix P` to `tasks hold` and `tasks release`. This prevents battery-backoff from overwriting or releasing an operator or booking hold committed after its listing. Both validate P with `task_holds.note_text` and compare the stored, trimmed note using case-sensitive `str.startswith`. A conditional hold also requires its new note to start with P (`task-hold-note`, exit 2). Its condition considers only a hold in force; release checks an expired row too. Matching renewals preserve `since`; there is no `--if-since`. Unconditional wire payloads, replies, rows and events retain their previous behavior. C-11.10, its change log, C-17.3 and command help document the additions.

**Exit codes and version skew.** Failed conditions use existing `Exit.REFUSED` (7). Missing capability uses existing `Exit.CLI_TOO_OLD` (6), treating an old daemon executable as a version incompatibility; C-17.3 now explicitly includes that use. Neither code was returned by the pre-change task handlers. Existing task outcomes use 0 (success), 1 (operational failure), 2 (invalid input) and 69 (daemon unavailable, including an older daemon without `tasks`). Thus 69 already has a tasks path and cannot distinguish missing conditional support. No enum member was added.

An older daemon's `coerce_args` silently drops `if_note_prefix` and would perform an unconditional mutation. The daemon now advertises `tasks.if_note_prefix.v1`. A conditional CLI invocation first requests capabilities and sends no `tasks` request unless that exact capability is listed. A missing or malformed list, or an unknown capabilities op, produces `task-hold-capability`, exit 6. Calls without a condition make no discovery request.

**Structured refusals.** Added optional `ErrorBody.reason` and `ErrorBody.details`, carried into `DaemonError`. Only the new condition reason can carry a specifically checked four-field `task_hold`; arbitrary handler details and exception prose are never reflected. This extends the existing checked-details mechanism without allowing free text into the generic refusal-template vocabulary. Unused fields are omitted, preserving existing error objects. Older decoders project `code`, `message` and `fix`, so they still receive exit 7 and the fully named blocking hold. Notes use the same whole-text scrubber as `note_text`'s credential check, in both text and structured data; supported notes stay identical to status. Nullable `since` stays nullable.

With `--json`, stdout contains exactly one object and prose remains on stderr. Condition failure (exit 7):

```json
{"refused":"task-hold-condition","task_hold":{"task":"build","until":"2026-11-07T12:10:00Z","note":"operator booking","since":"2026-11-07T12:00:00Z"}}
```

Missing capability (exit 6):

```json
{"refused":"task-hold-capability","capability":"tasks.if_note_prefix.v1"}
```

**Invariants and tests.** Names below are pytest test functions; daemon cases are in `tests/fake/test_task_hold_cas.py`, CLI cases in `tests/unit/test_task_hold_cas_cli.py` unless otherwise stated.

| Invariant | Test evidence |
|---|---|
| 1. A conditional hold writes iff there is no hold in force or its note starts with P; refusal preserves the lease, every job and all events. | `test_foreign_hold_refuses_without_changing_any_row_or_event`; `test_expired_foreign_hold_allows_conditional_hold_but_blocks_release`; `test_matching_hold_can_be_renewed_and_released_with_trimmed_prefix` |
| 2. Conditional release deletes iff the row's note starts with P, active or expired; absent row succeeds without changes; foreign row refuses without changes. | `test_matching_release_deletes_both_active_and_expired_rows`; the foreign-hold test; `test_conditional_release_with_no_row_is_successful_and_writes_nothing`; `test_conditional_release_with_no_row_leaves_stale_held_jobs_unchanged` |
| 3. Unconditional replies, rows and events equal the base handlers. | `test_unconditional_handlers_match_frozen_a8624a489_rows_replies_and_events`, using frozen handlers in `tests/task_hold_cas_legacy.py`; `test_unconditional_requests_keep_old_payload_without_capability_query`. Both frozen executable handler bodies were separately compared to the base AST. |
| 4. Condition and write share the writer's transaction; a foreign commit after request start cannot be replaced or deleted. | `test_foreign_commit_before_writer_is_seen_by_condition`, for both verbs: a transaction-entry hook commits a foreign hold through a second `Store` connection before the request acquires its writer, then checks the complete post-foreign state is unchanged. |
| 5. A client never sends the condition without capability advertisement. | `test_condition_is_never_sent_without_advertised_capability`; `test_daemon_predating_capabilities_receives_no_tasks_request` |
| 6. Refusal text and JSON name the blocking hold and exit code; JSON is one object with exactly the hold reply's keys and types. | `test_condition_refusal_names_all_blocking_hold_fields_and_exit_7`; `test_json_refusal_uses_structured_details_instead_of_message_parsing`; wire tests in `tests/unit/test_task_hold_cas_wire.py` cover checked data, older decoding, scrubbed notes and nullable `since`. `test_real_blocking_hold_survives_daemon_socket_and_cli_json` in `tests/unit/test_task_hold_cas_integration.py` connects a real store, daemon dispatch, socket client and CLI for active hold/release and expired release, comparing JSON directly with the original hold reply. |
| 7. The two new outcomes' codes differ from each other and every existing tasks outcome's code. | `test_tasks_outcome_codes_are_distinct_by_outcome_category`, with a table producing old codes `{0,1,2,69}`, condition 7 and capability 6. |

`Store.transaction` acquires `BEGIN IMMEDIATE` before yielding. It adds its event only after successful return and actual changes. An exception rolls back the transaction/savepoint, including any row changes and events. The foreign-row snapshots test this, including every jobs row, and cover both `task.held` and `task.released` refusals.

`tests/fake/test_task_hold_cas_properties.py::test_sequences_match_one_row_model_for_two_automations_and_operator` runs 60 deterministic Hypothesis examples plus an explicit example, drawing up to 45 operations from two automated holders with different prefixes and an operator with free notes, conditional and unconditional holds/releases, and clocks crossing expiry. A separate one-row model checks every reply, persisted lease, list reply, event count and refusal snapshot.

**Mutation checks.** The reproducible runner `docs/reports/2026-11-07-task-hold-cas-mutants.py` applies each mutant to a fresh temporary source copy and runs the named test. Production files are never mutated. Results and failure output are saved in `docs/reports/2026-11-07-task-hold-cas-mutation-results.json`.

| Mutant (15 variants total) | Killing test |
|---|---|
| Check P against new note | `test_foreign_hold_refuses_without_changing_any_row_or_event` |
| Expired hold treated as in force | `test_expired_foreign_hold_allows_conditional_hold_but_blocks_release` |
| Conditional release deletes foreign note | `test_foreign_hold_refuses_without_changing_any_row_or_event` |
| Read before writer, hold and release (2) | `test_foreign_commit_before_writer_is_seen_by_condition` |
| Refuse with exit 0 or exit 2 (2) | `test_foreign_hold_refuses_without_changing_any_row_or_event` |
| Match with `in`, hold and release (2) | `test_foreign_hold_refuses_without_changing_any_row_or_event` |
| Send condition without capability | `test_condition_is_never_sent_without_advertised_capability` |
| Commit `task.held` or `task.released` event on refusal (2) | `test_foreign_hold_refuses_without_changing_any_row_or_event` |
| Failed condition and missing capability share exit 7 | `test_tasks_outcome_codes_are_distinct_by_outcome_category` |
| JSON hold only on stderr, or missing `since` (2) | `test_condition_refusal_names_all_blocking_hold_fields_and_exit_7` |

Mutation results: **15/15 killed; zero survivors**. Each run failed its named test under the intended mutation, with no setup/collection error. No mutant is present in production files.

**Commands and results.** Tests first sample `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and `UV_CACHE_DIR=$TMPDIR/uv-cache`. The default cache was denied, so subsequent commands use the permitted child `.../subfleet-attempt-20261106-192709-task-hold-cas-build2-a1-y0mdib45/task-hold-cas` for TMPDIR/cache and a new `--basetemp` child per run. This also keeps fixtures' own temporary roots inside the writable directory.

- `uv sync --group dev`: default cache initialization denied; retry with `uv --cache-dir "$UV_CACHE_DIR" sync --group dev` succeeded, installing 9 packages.
- New daemon invariants and model: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q tests/fake/test_task_hold_cas.py tests/fake/test_task_hold_cas_properties.py --basetemp .../cas-tests-1`: **29 passed** (44.20s).
- New CLI plus existing task-hold unit and host-push CLI tests: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 --basetemp .../cli-2 tests/unit/test_task_hold_cas_cli.py tests/unit/test_task_holds.py tests/unit/test_host_push_cli.py`: **122 passed** (21.40s). An initial run had 116 passed/5 failed; adding the explicit reason prefix and accounting for help wrapping fixed them, with one regression test added.
- New wire plus handler-refusal tests: **379 passed** (40.69s) before one additional wire test; subsequent wire-only run: **18 passed** (7.76s).
- Final regression: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 --tb=short --basetemp .../final-regression-1` over all requested files, the four initial new CAS files, `test_turn_listing.py`, `test_cli_turns.py`, `test_protocol_registry.py`, `test_app_cutover_daemon.py`, `test_suggestions.py`, `test_protocol_secrecy.py` and `test_client_availability.py`: **1180 passed, 6 skipped** (431.45s). The skips are the existing opt-in legacy task-hold mutation matrix. Full output: `docs/reports/2026-11-07-task-hold-cas-validation.txt`. The required regression files were `tests/unit/test_task_holds.py`, `tests/fake/test_task_hold_admission.py`, `tests/fake/test_task_hold_clocks.py`, `tests/fake/test_task_hold_properties.py`, `tests/fake/test_task_hold_mutants.py`, `tests/fake/test_task_hold_reset_demand.py`, `tests/unit/test_host_push_cli.py`, `tests/unit/test_handler_refusals.py`, `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py` and `tests/unit/test_invariants_index.py`; the new files were `test_task_hold_cas_cli.py`, `test_task_hold_cas_wire.py`, `test_task_hold_cas.py` and `test_task_hold_cas_properties.py`.
- Frontend: `uv run --no-sync pytest -q --tb=short --basetemp .../frontend-capabilities-1 tests/frontend/test_core_protocol.py`: **10 passed** (291.20s), including real Swift capability decoding. Output: `docs/reports/2026-11-07-task-hold-cas-frontend-validation.txt`.
- `uv run --no-sync python docs/reports/2026-11-07-task-hold-cas-mutants.py`: **15/15 mutants killed**, zero survivors; each ran in a separate temporary source copy.
- Integration: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 --basetemp .../cli-integration-2 tests/unit/test_task_hold_cas_integration.py`: **3 passed** (23.21s). Initial run: 3 failed because the fixture sent discovery to the task dispatcher; the fixture was corrected to mirror production conversation routing, with no production change.
- Final capability/guards: `uv run --no-sync pytest -q --tb=short --basetemp .../final-guards-1 tests/unit/test_conversation_steer_service.py::test_capability_and_op_order tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py`: **27 passed** (15.17s), after the integration file was added. Output: `docs/reports/2026-11-07-task-hold-cas-guards-validation.txt`.
- `git diff --check`: passed. Read-only `rg` audits covered exact hold/event/error comparisons and capability/help tests.

**Commits and deviations.** Named-file staging/commit attempts were made for the contract, implementation, CLI and invariant-test steps. Git could not create `/Users/maxghenis/subfleet-v2/.git/worktrees/20261106-192709-task-hold-cas-build2/index.lock` (`Operation not permitted`); no commit was created. Files remain in this workspace for hub collection. No history was rewritten and nothing was pushed. This Git permission failure and the permitted temporary-cache fallback are the execution deviations; no functional scope was deferred. The final report, mutation evidence and validation logs are included in the named-file commit attempt; Git did not permit committing.
