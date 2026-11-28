# #356 · D-REMOTE-JOBS1 build report

Implemented remote-job adoption, listing, forgetting and status for the first
slice. The worktree started clean at
`cd81ae6ebf89031b3ad87d70c7c857e32b1e875f` on 2026-11-28 at 02:49:21 UTC.
Git denied the worktree index lock, so the changes are available for salvage
and HEAD remains at the base. [Issue #356](https://github.com/MaxGhenis/subfleet-v2/issues/356)

Round 1 wrote the design and drafted the fake tests. Its uv cache and system
temporary-directory permissions prevented a test run. Round 2 used the
prebuilt lane environment with worktree scratch.

## Changes

| File | Ruling and location |
| --- | --- |
| `subfleet/store.py` | 1 at lines 48–50 and 154–163. 2, 3, 5 and 7 at lines 1044–1078. The separate table is migrated additively. Adoption and forgetting each commit one event with the row change. |
| `subfleet/store_schema.sql` | 1–3 and 8 at lines 71–86. Fresh schema, unique provider/id pairs, supported states, nullable poll time and no local job or lease relation. |
| `subfleet/remote_jobs.py` | 2, 4 and 5 at lines 12–74. HTTPS host and path checks, canonicalization, metadata validation and closed refusals. |
| `subfleet/protocol.py` | 4 and 5 at lines 65 and 278–288. Local RPC operations and argument shapes. |
| `subfleet/refusals.py` | 4 and 5 at lines 74–81. Static refusal messages and fixes. |
| `subfleet/descriptors.py` | 5 at line 68. Classifies remote listing as a read. |
| `subfleet/daemon.py` | 4 and 5 at lines 4410–4417. 6 at lines 4630–4656. Online operations and conditional status rows. |
| `subfleet/offline.py` | 5 and 6 at lines 350–356, 407 and 433. Reads the same records and tolerates older schemas. |
| `subfleet/cli.py` | 4–6 at lines 602–604, 731–769 and 3708–3719. Commands and conditional status section. |
| `tests/unit/test_remote_jobs_store.py` | 1 and 4 at lines 18–113. Previous schema, reopening, fresh schema and URL cases. |
| `tests/unit/test_remote_jobs.py` | 2–7 at lines 78–227. CLI lifecycle, refusals, privacy, events, status, and the offline reader. |
| `tests/fake/test_remote_jobs_admission.py` | 8 at lines 52–128. One-lane admission, leases, workspaces, disk reserve, retention and `wait --mine`. |
| `tests/unit/test_store_notice_wake_migration.py` | 1 at lines 37–79. Checks the notice-wake step by name within the complete migration history. |
| `tests/unit/test_prune_217.py` | 1 at lines 29–33. Includes the new table in the existing exhaustive schema verification. |
| `docs/acceptance-contract.md` | All eight rulings at lines 706–708, plus the changelog at line 9 and offline listing at line 698. |
| `docs/design/remote-jobs.md` | 1 and 6 at lines 82–90. Clarifies offline schema reporting. The five-part design remains in place. |
| `docs/reports/2026-11-28-remote-jobs.md` | This report. |

The hub verified the Claude URL shape `https://claude.ai/code/session_<id>`,
with a nonempty suffix of ASCII letters and digits. The hub assumed the
Codex shape `https://chatgpt.com/codex/tasks/task_<id>`, with a nonempty
suffix of ASCII letters, digits or underscores. The comment above the Codex
pattern records that assumption. All test URLs are synthetic. The parser
discards queries and fragments, and adoption rejects provider ids and
metadata that match known credential shapes. Adoption caps each of those
fields at 512 characters to bound the credential check.

For a fixed store and clock, empty status preserves the existing text and
JSON bytes. The offline JSON's existing `schema_version` field advances when
the database migrates. The design's **Show** section now makes that
distinction explicit. C-17.10 was the next free CLI clause. The invariants
index covers a fixed historical ledger and needed no entry.

## Evidence

With the base source in place, the final tests produced 32 failures. After
restoring and completing the implementation, the same 32 checks passed.

| Cases | Base failure | Pass |
| --- | --- | ---: |
| Three migration cases in `test_remote_jobs_store.py` | Table absent when migrating and on fresh creation. Reopening check could not read the table. | 3 |
| Accepted URL cases `claude`, `codex`, `strip-query-and-fragment` | Parser module absent. | 3 |
| Refused URL cases `other-host`, `lookalike-host`, `http`, `missing-id`, `empty-claude-suffix`, `empty-codex-suffix`, `invalid-claude-id`, `invalid-codex-id`, `wrong-provider-id`, `extra-path`, `path-whitespace`, `explicit-port`, `url-userinfo` | Parser module absent. | 13 |
| CLI lifecycle, duplicate, unknown forget and online and offline status | Base CLI lacked the remote verb. Both status cases reached adoption after the empty-output assertions. | 5 |
| CLI refusals `account-email`, `url-host`, `provider-id-credential`, `metadata-credential` | Base usage error lacked the expected closed refusal message. | 4 |
| Fake admission and disk reserve, retention and `wait --mine` | Base controls passed. Each failed when the CLI refused the remote verb. | 3 |
| Existing exhaustive table proof in `test_prune_217.py` | Actual schema lacked the added `remote_jobs` table. | 1 |

The initial admission draft failed in its local control. It expected one
workspace preparation and a `reserved` job. The pass can prepare another
local job before deferring it, and uses a `running` job with a `reserved`
attempt. The corrected test verifies the one reservation, exact disk bytes,
untouched leases at adoption, only local workspace preparations, and two
pending jobs. The final red run confirmed the corrected test reached adoption
on the base.

| Mutant | Test that caught it | Pass / fail / skip |
| --- | --- | ---: |
| Accept any host with a matching path | `test_parse_remote_url_refuses_other_hosts_and_shapes[other-host]` and `[lookalike-host]` | 0 / 2 / 0 |
| Insert the adopted row into `jobs` | `test_remote_adoption_keeps_local_admission_and_disk_reserve` found a remote id among the local jobs immediately after adoption. | 0 / 1 / 0 |
| Always emit `remote_jobs` in status JSON | `test_cli_online_status_includes_remote_rows_only_when_present` and `test_cli_offline_status_preserves_base_output_and_reads_remote_rows` | 0 / 2 / 0 |

Final outcome per test case across the end runs and their reruns. Total **3048 passed, 0 failed, 0 skipped**.

| Initial run | Module | Pass | Fail | Skip |
| --- | --- | ---: | ---: | ---: |
| 10-end-core | `tests/fake/test_remote_jobs_admission.py` | 3 | 0 | 0 |
| 10-end-core | `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| 10-end-core | `tests/unit/test_cli.py` | 256 | 0 | 0 |
| 10-end-core | `tests/unit/test_compat.py` | 1615 | 0 | 0 |
| 10-end-core | `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 |
| 10-end-core | `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 |
| 10-end-core | `tests/unit/test_handler_refusals.py` | 400 | 0 | 0 |
| 10-end-core | `tests/unit/test_invariants_index.py` | 25 | 0 | 0 |
| 10-end-core | `tests/unit/test_protocol_registry.py` | 4 | 0 | 0 |
| 10-end-core | `tests/unit/test_remote_jobs.py` | 9 | 0 | 0 |
| 10-end-core | `tests/unit/test_remote_jobs_store.py` | 19 | 0 | 0 |
| 10-end-core | `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_conversation_store.py` | 32 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_gate_admission.py` | 24 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_gate_service.py` | 25 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_host_push.py::test_schema_six_migration_preserves_jobs_without_authorizing_push` | 1 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_offline.py` | 33 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_prune_217.py` | 37 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_qos_review_r1.py` | 9 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_qos_review_r2.py` | 5 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_store_ids.py` | 27 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_store_migration_2.py` | 21 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_store_notice_wake_migration.py` | 8 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_store_qos_migration.py` | 13 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_suggestions.py` | 30 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 |
| 11-end-migrations | `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 |
| 12-end-status | `tests/fake/test_status_payload.py::test_bounded_operator_rows_preserve_both_status_renderers` | 1 | 0 | 0 |
| 12-end-status | `tests/fake/test_task_hold_admission.py` | 49 | 0 | 0 |
| 12-end-status | `tests/unit/test_cli_turns.py` | 23 | 0 | 0 |
| 12-end-status | `tests/unit/test_status_hold_source.py` | 135 | 0 | 0 |
| 12-end-status | `tests/unit/test_status_json.py` | 56 | 0 | 0 |
| 13-end-stateful | `tests/fake/test_admission_stateful.py` | 17 | 0 | 0 |

The initial core run reported three wall-clock failures against existing
750 ms stdin assertions and two 540 s timeouts in the extra daemon refusal
module. The timeout cases reached a job wait because Git found the enclosing
checkout above their synthetic workdirs. The migration batch's 25 failures
came from the same Git discovery issue in gate and suggestion fixtures that
require neutral directories. Its schema migration checks passed on the first
run.

The Git-fixture reruns used `RJ_ISOLATE_GIT=1` on the scratch-only plugin
to set `GIT_CEILING_DIRECTORIES` both in the test environment and in the
daemon Git helper's filtered subprocess environment. Nested fixture
repositories remained discoverable. The two timeout cases ran separately
with `RJ_TEST_TIMEOUT=900`. A diagnostic load sample reached 101.46 while the
second timed case was outstanding. No checked-in assertions changed.

Only the migration case in `test_host_push.py` ran. Other cases in that
module run Git pushes against local fixture remotes. The supplementary
`test_status_payload.py` selection was the exact-rendering comparison to
avoid combining its large-history fixture with other scratch use.

One stdin timing case passed on the first focused retry. The other two
missed the time bound again in isolated and paired runs. Both passed on
the base. Profiling the same two checks then passed on both versions,
with measured parser times of 0.128–0.213 s on the implementation and
0.176–0.241 s on the base. The final run of the complete CLI module
passed all 256 cases without profiling. No tests skipped.

## Commands

The first command was `date -u +%FT%TZ && git rev-parse HEAD && git status
--porcelain --untracked-files=no`. The issue read was
`gh issue view 356 --repo MaxGhenis/subfleet-v2`. The lane import check
`uv --no-cache run --no-sync python -c 'import subfleet; print(subfleet.__file__)'`
resolved to this worktree's `subfleet/__init__.py`.

Each run used the following command, with the label and selections listed below.
The initial run omitted the scratch-only isolation plugin. The run log
recorded `date -u +%FT%TZ`, `df -H /`, `uptime` and `du -sm "$R"` around
each invocation.

```sh
R="$PWD/build/rj-build-r2"
export TMPDIR="$R/runtime" HYPOTHESIS_STORAGE_DIRECTORY="$R/hypothesis" PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/remote-jobs/.venv PYTHONPATH="$PWD"
uv --no-cache run --no-sync python -m pytest -p build.rj-build-r2.pytest_isolation \
  -p no:cacheprovider -n 4 --timeout=540 --basetemp "$TMPDIR/pytest-$label" \
  -q -rA -m "not real_lsof and not real_cwd" --junitxml "$R/$label.xml" "$@"
```

The draft and focused runs selected the following paths and test IDs.

```text
01-red-store-fake tests/unit/test_remote_jobs_store.py tests/fake/test_remote_jobs_admission.py
02-red-cli tests/unit/test_remote_jobs.py
03-green-remote tests/unit/test_remote_jobs.py tests/unit/test_remote_jobs_store.py tests/fake/test_remote_jobs_admission.py
04-red-corrected tests/fake/test_remote_jobs_admission.py tests/unit/test_prune_217.py::test_current_schema_has_no_decision_dependents_and_hashes_every_table
05-red-final tests/unit/test_remote_jobs.py tests/unit/test_remote_jobs_store.py tests/fake/test_remote_jobs_admission.py tests/unit/test_prune_217.py::test_current_schema_has_no_decision_dependents_and_hashes_every_table
06-green-final tests/unit/test_remote_jobs.py tests/unit/test_remote_jobs_store.py tests/fake/test_remote_jobs_admission.py tests/unit/test_prune_217.py::test_current_schema_has_no_decision_dependents_and_hashes_every_table
07-mutant-host 'tests/unit/test_remote_jobs_store.py::test_parse_remote_url_refuses_other_hosts_and_shapes[other-host]' 'tests/unit/test_remote_jobs_store.py::test_parse_remote_url_refuses_other_hosts_and_shapes[lookalike-host]'
08-mutant-jobs tests/fake/test_remote_jobs_admission.py::test_remote_adoption_keeps_local_admission_and_disk_reserve
09-mutant-json tests/unit/test_remote_jobs.py::test_cli_online_status_includes_remote_rows_only_when_present tests/unit/test_remote_jobs.py::test_cli_offline_status_preserves_base_output_and_reads_remote_rows
14-retry-cli tests/unit/test_cli.py::test_suggest_open_pipe_is_bounded tests/unit/test_cli.py::test_ping_continuous_input_has_a_total_deadline tests/unit/test_cli.py::test_ping_character_device_with_no_eof_is_bounded
15-retry-refusal-direct 'tests/unit/test_daemon_refusals_cli.py::test_cli_writable_outside_git_names_the_policy_fix[False]'
16-retry-refusal-policy 'tests/unit/test_daemon_refusals_cli.py::test_cli_writable_outside_git_names_the_policy_fix[True]'
17-retry-git-fixtures tests/unit/test_gate_admission.py tests/unit/test_gate_service.py tests/unit/test_suggestions.py tests/unit/test_suggestions_r1b_adversarial.py
18-retry-suggest tests/unit/test_cli.py::test_suggest_open_pipe_is_bounded
19-retry-ping tests/unit/test_cli.py::test_ping_continuous_input_has_a_total_deadline
20-base-stdin, 21-current-stdin, 22-profile-current, 23-profile-base tests/unit/test_cli.py::test_suggest_open_pipe_is_bounded tests/unit/test_cli.py::test_ping_continuous_input_has_a_total_deadline
24-end-cli-confirm tests/unit/test_cli.py
```

The final suites selected the modules listed in the counts table, grouped
by run label. The two individual selections there use their explicit node
IDs. Runs 15–17 enabled the scratch Git ceiling, runs 15–16 used a
900-second timeout, and runs 22–23 enabled the scratch timing tracer
with `RJ_MEASURE_STDIN=1`. For the corrected red and base comparison
runs, I copied the implementation into scratch
and temporarily loaded each previously tracked implementation file from
`git show cd81ae6ebf89031b3ad87d70c7c857e32b1e875f:<path>`. I moved the new
parser module into scratch, then restored all implementation files before
the green run. The mutation runs also restored each edited file before
continuing.

The named-file staging attempts were grouped as shown below. Each call
failed before its commit command could run. The failing lock was
`/Users/maxghenis/subfleet-v2/.git/worktrees/20261127-214905-rj-build-r2/index.lock`,
with `Operation not permitted`.

```sh
git add tests/unit/test_remote_jobs.py tests/unit/test_remote_jobs_store.py tests/unit/test_store_notice_wake_migration.py
git add tests/fake/test_remote_jobs_admission.py tests/unit/test_remote_jobs.py tests/unit/test_remote_jobs_store.py tests/unit/test_store_notice_wake_migration.py tests/unit/test_prune_217.py
git add subfleet/cli.py subfleet/daemon.py subfleet/descriptors.py subfleet/offline.py subfleet/protocol.py subfleet/refusals.py subfleet/remote_jobs.py subfleet/store.py subfleet/store_schema.sql
git add docs/acceptance-contract.md docs/design/remote-jobs.md
git add docs/reports/2026-11-28-remote-jobs.md
git diff --check
git log --format='%H %s' cd81ae6ebf89031b3ad87d70c7c857e32b1e875f..HEAD
```

## Footprint and limits

| Run | UTC on Nov 28 | Free disk | Load averages | Scratch MiB after run / cleanup | Pass / fail / skip |
| --- | --- | ---: | --- | ---: | ---: |
| 01-red-store-fake | 02:53:31 | 72G | 73.81 61.21 60.33 | 3 / 1 | 0 / 22 / 0 |
| 02-red-cli | 02:56:57 | 71G | 82.82 65.21 61.83 | 4 / 1 | 0 / 7 / 0 |
| 03-green-remote | 03:00:17 | 71G | 54.86 57.68 59.26 | 7 / 1 | 28 / 1 / 0 |
| 04-red-corrected | 03:02:50 | 71G | 52.69 55.85 58.34 | 4 / 2 | 0 / 4 / 0 |
| 05-red-final | 03:04:54 | 71G | 67.17 59.42 59.37 | 10 / 2 | 0 / 32 / 0 |
| 06-green-final | 03:06:54 | 72G | 51.40 57.20 58.58 | 11 / 2 | 32 / 0 / 0 |
| 07-mutant-host | 03:09:20 | 72G | 40.17 54.21 57.39 | 2 / 2 | 0 / 2 / 0 |
| 08-mutant-jobs | 03:11:57 | 71G | 86.27 57.62 57.53 | 3 / 2 | 0 / 1 / 0 |
| 09-mutant-json | 03:14:34 | 71G | 109.35 80.56 67.07 | 4 / 2 | 0 / 2 / 0 |
| 10-end-core | 03:17:38 | 70G | 31.97 60.69 61.86 | 33 / 3 | 2380 / 5 / 0 |
| 11-end-migrations | 03:47:37 | 73G | 13.74 56.46 66.65 | 123 / 4 | 357 / 25 / 0 |
| 12-end-status | 03:59:52 | 73G | 12.56 43.46 59.04 | 48 / 4 | 264 / 0 / 0 |
| 13-end-stateful | 04:13:05 | 73G | 32.24 45.44 56.29 | 4 / 4 | 17 / 0 / 0 |
| 14-retry-cli | 04:25:44 | 74G | 41.12 36.60 45.99 | 4 / 4 | 1 / 2 / 0 |
| 15-retry-refusal-direct | 04:26:23 | 74G | 46.12 38.47 46.25 | 4 / 4 | 1 / 0 / 0 |
| 16-retry-refusal-policy | 04:26:59 | 74G | 45.98 39.40 46.28 | 4 / 4 | 1 / 0 / 0 |
| 17-retry-git-fixtures | 04:27:26 | 74G | 44.59 39.69 46.15 | 51 / 5 | 109 / 0 / 0 |
| 18-retry-suggest | 04:28:39 | 74G | 46.25 41.09 46.15 | 4 / 4 | 0 / 1 / 0 |
| 19-retry-ping | 04:29:10 | 73G | 45.51 41.39 46.08 | 5 / 5 | 0 / 1 / 0 |
| 20-base-stdin | 04:30:23 | 72G | 33.90 39.75 45.13 | 5 / 5 | 2 / 0 / 0 |
| 21-current-stdin | 04:31:12 | 73G | 21.15 35.21 43.13 | 5 / 5 | 0 / 2 / 0 |
| 22-profile-current | 04:32:03 | 73G | 27.82 34.74 42.49 | 5 / 5 | 2 / 0 / 0 |
| 23-profile-base | 04:32:17 | 73G | 28.94 34.65 42.33 | 5 / 5 | 2 / 0 / 0 |
| 24-end-cli-confirm | 04:32:46 | 73G | 28.87 34.18 41.93 | 5 / 5 | 256 / 0 / 0 |

Pytest's existing failure-state hook first used its default `/tmp/sf-failed`
location. The initial run reported no prune. From run 02 onward a scratch-only
plugin set its root to `$R/failed-state` and redirected the fixture's explicitly
short Unix-socket roots into `$R`. Eight-character names directly inside
`$R` fit Darwin's socket limit. The plugin changed only fixture paths, and was
removed with the run logs and JUnit files.

The worktree scratch included runtime basetemps, isolated pytest homes,
Hypothesis storage, source and mutant backups, runner scripts and logs.
Each run's basetemp was deleted afterward. The final cleanup removed
`build/rj-build-r2` with a resolved-path check. The largest recorded post-run footprint was 123 MiB.
The files in the changes table remain for salvage. The two existing
v1 flag-coverage cases read the installed v1 parser source as data.
They invoked no v1 command.
Test subprocesses also left 1,155,897 bytes of generated bytecode
in four worktree `__pycache__` directories outside `$R`. I removed them
after verifying they contained only bytecode files.

Adoption and forgetting require the daemon's write path. Listing and status
use its existing read paths, with an offline store fallback. There were no
provider calls, dependency downloads, pushes, or live store operations.

Polling, launching, steering and usage accounting are deferred. Polling
needs verified read-only provider commands that can see the right account and
workspace, plus a mapping from provider evidence to Subfleet states. Keep
the last successful observation when a workspace is invisible. A polling
result needs an identity that distinguishes a forgotten record from a
same-second re-adoption. The remaining questions concern Codex's real URL
shape and workspace visibility, result links, staleness and remote wait
semantics, steering acknowledgements, and attribution to the launching
account.

The retained files are the implementation, tests and documentation listed above.

| Path | Bytes |
| --- | ---: |
| `docs/acceptance-contract.md` | 1107469 |
| `docs/design/remote-jobs.md` | 7590 |
| `subfleet/cli.py` | 205518 |
| `subfleet/daemon.py` | 911649 |
| `subfleet/descriptors.py` | 13074 |
| `subfleet/offline.py` | 26247 |
| `subfleet/protocol.py` | 25825 |
| `subfleet/refusals.py` | 50238 |
| `subfleet/store.py` | 70539 |
| `subfleet/store_schema.sql` | 15431 |
| `tests/fake/test_remote_jobs_admission.py` | 5411 |
| `tests/unit/test_prune_217.py` | 20334 |
| `tests/unit/test_store_notice_wake_migration.py` | 6332 |
| `subfleet/remote_jobs.py` | 3817 |
| `tests/unit/test_remote_jobs.py` | 10964 |
| `tests/unit/test_remote_jobs_store.py` | 5458 |
| `docs/reports/2026-11-28-remote-jobs.md` | 20773 |
