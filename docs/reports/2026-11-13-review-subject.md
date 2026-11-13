# D-REVIEW-SUBJECT1 build report

Started 2026-11-13 13:27:20 UTC (`date -u`, first command); deadline 17:27:20 UTC. Implementation edits are confined to the assigned checkout; temporary-directory deviations are recorded below. Final verification is recorded below; the four-hour deadline remains binding.

Implemented caller identity, review subjects and rounds, migration 11, CLI views/help, README command examples and acceptance clauses C-3.8, C-6.21–22 and C-17.2, plus the C-6.5 refusal index. Reviewer selection by model family remains A3.

Code-inspection limitation: populating Codex caller ids also brings them under existing session writer checks. PID detection still reads only `CLAUDE_PID`, so a second overlapping writable submission from one Codex thread can be refused as `unidentified-writer`. I5 proves the same daemon inputs match release/217; it does not prove unchanged CLI admission after adding this identity. Writer policy was left unchanged.

`run` chooses its explicit `--caller-family` before `SUBFLEET_CALLER_FAMILY`, then native Claude markers (`CLAUDECODE` or `CLAUDE_CODE_SESSION_ID`), then native Codex markers (`CODEX_THREAD_ID` or `CODEX_SESSION_ID`), otherwise null. Invalid inputs name the flag/environment variable; invalid daemon input uses refusal reason `caller-family`. Resume and the sessions kit's handoff/revive submit boundary also detect the current caller's family; revive retains its target session id. Internal daemon-created gate/turn jobs without model caller context keep null: their executing provider does not identify who asked.

Codex evidence starts at `command -v codex`: `/Users/maxghenis/.bun/bin/codex`, the installed `@openai/codex` wrapper, package version 0.159.0. Environment inspection printed names only, including `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, never values. OpenAI Docs research did not establish these command markers; the matching official source does:

- [process_manager.rs, 1443–1454](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/core/src/unified_exec/process_manager.rs#L1443-L1454) builds each command environment, inserts its thread id after shell policy selection, and calls session injection.
- [exec_env.rs, 40–48](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/core/src/exec_env.rs#L40-L48) injects the shared root session id.
- [shell_environment.rs, 150–156](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/protocol/src/shell_environment.rs#L150-L156) reinserts the thread marker after `include_only` filtering.

Caller session precedence is `CLAUDE_CODE_SESSION_ID`, `SUBFLEET_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`. The thread id comes first among Codex ids because it identifies the actual calling thread. Both `--mine` diagnostics name Codex's marker and the explicit Subfleet override. Under default environment selection, a nested lane submission records the lane's executing family: provider environment selection removes the parent's markers and family override, then Claude or Codex supplies its own native command markers. Explicit submission overrides or an explicit policy `env.pass` that preserves attribution variables still take precedence.

The subject normalizer trims outer whitespace and accepts 1–2000 printable characters. Recognized GitHub HTTP(S) PR links become `https://github.com/<owner>/<repo>/pull/<number>`: host, owner and repo are lower case; trailing slash, `/files`, query and fragment disappear. Other caller keys retain their spelling. Round-only input and review fields on other tasks are refused by `review-fields`. Explicit rounds must be positive integers; above four uses the distinct `review-round-limit` refusal, exit 7, with the requested handoff message. The existing refusal renderer substitutes “this subject” if a subject fails its credential-safe display check; storage still retains the key. Explicit rounds 1–4 remain as supplied, including re-runs after a later round succeeded.

`review_subject.assign_round` runs inside `Daemon.submit`'s existing `job.submitted` transaction. `Store.transaction` acquires SQLite's writer with `BEGIN IMMEDIATE` before the allocator reads. It chooses exactly one above the highest succeeded review with an accepted attempt. If a queued/running/waiting review already holds that candidate, it refuses with `review-round-held` (exit 7). Pending work never advances the candidate. Two racing automatic submissions therefore serialize; while the first is pending, the second is refused. Failed, cancelled and lost jobs release the round; refusals add no job/event. A cap or held-round refusal removes its newly prepared job directory so a later ordinary submission cannot collide with an orphan. Accepted subject/round history is copied into the durable `retention.pruned` audit event in the same transaction that deletes the job row. The allocator reads the higher maximum from indexed job rows and retirement events (using the existing event-kind index); writer serialization means it sees either the job or its retirement record, with no gap. Requested subject/round enter the payload digest only when present; the automatic assigned value does not, preserving retries. Caller identity remains outside the digest.

Migration 11 is the next free migration in this checkout (schema 10 before it). It adds nullable `caller_family`, `review_subject`, `review_round` columns and the partial `jobs_review_subject` index. Fresh stores and upgraded stores have the same fields/index. Old read-only stores expose three null fields without migration. Historical rows are unchanged except for the new null columns and the normal migration/version audit appends. Existing migration-loop fixtures were updated for the new indexed column and multiple ordered migration events. **PR #307 also holds migration 11: the hub must renumber one migration and its pinned test at merge.** Retention's existing `SELECT *` capture carries the fields into `rows.json`; restore still reads a schema-10 archive.

Invariant evidence:

| Invariant | Test |
| --- | --- |
| I1: explicit family wins; invalid names refused | `test_review_subject_cli.py::test_explicit_caller_family_wins_over_detection` and invalid-family CLI/daemon tests |
| I2: accepted history advances 1,2,3,4, then refuses; failures/cancellation/refusal do not advance it | `test_review_subject_submission.py::test_i2_only_accepted_reviews_advance_assigned_rounds` (30 generated sequences, two explicit examples, each extended through the cap) |
| I3: two PR URL spellings share one subject | `test_review_subject_submission.py::test_i3_pr_url_spellings_share_accepted_history` |
| I4: racing automatic submissions never share an assigned round | `test_review_subject_submission.py::test_i4_racing_submissions_allocate_inside_the_sql_transaction` (two independent store connections and submit locks, deterministic transaction gate; one pending admission and one refusal, then round-1 reuse after failure; `test_pending_review_holds_then_releases_the_next_accepted_round` also checks queued/running/waiting × failure/cancellation) |
| I5: unscoped admission with identical daemon inputs matches release/217 rows/events | `test_review_subject_submission.py::test_i5_no_subject_rows_and_events_match_release_217` (three task cases, frozen exact baseline submit/args/digest with SHA256 checks) |
| I6: migration preserves every historical row | `test_store_review_subject_migration.py::test_i6_migration_preserves_every_existing_row_and_adds_only_null_job_fields` |
| I7: archive keeps fields; old archive restores | `test_i7_retired_job_rows_keep_caller_family_subject_and_round` and `test_i7_restore_reads_an_archive_written_from_schema_10` in `test_retention_review_subject.py` (plus eight accepted-history/type cases) |

The public cap refusal preserves its distinct `review-round-limit` wire reason while dropping untrusted details. Resume validates review fields again after inheriting its source task. Archival reads ignore malformed JSON and noninteger/out-of-range rounds. Additional tests cover JSON views, readable round labels, CLI help, no-subject defaults, round type validation, explicit repeats, idempotent normalized retries, resume attribution, sessions-kit attribution and named wire refusals.

Mutants were applied individually from saved pristine bytes. Matching bytecode caches were invalidated, and each file was restored in `finally` and compared byte-for-byte before the next mutation.

| Mutant | Killer | Result |
| --- | --- | --- |
| 1: count failed jobs | I2 property | 1 failed, 40.04 s |
| 2: refuse at four | I2 property | 1 failed, 18.85 s |
| 3: detection overrides explicit family | explicit-family precedence test | 5 failed, 7 passed, 32 deselected, 20.27 s |
| 4: keep trailing `/files` | I3 URL/history test | 1 failed, 12.21 s |
| 5: read round outside submission transaction | I4 race/transaction test | 1 failed, 13.26 s |

Restored SHA256: `review_subject.py` `7f272385314cc36aa9c2458afffca872fd2af037fa8bf499ac69378dc31df07f`; `daemon.py` `25aaa1100f2ff052a5884013d41275cf6579aa0acea69976868c55f74753b5d7`; `cli.py` `626f66fd00bfb05c8a23eb2cf18dd69a32563007c16d81b9f06edc1d810a328b`; `refusals.py` `04be23f3c3e387607c435013e0b94132051fcf39c7091aaee084c92180ffafb9`. The four round/URL/transaction mutants were replayed from fresh corrected bytes after the pending-round correction; the table gives the fourth, final pass. The third pass also killed all four (138.56, 213.94, 55.34, 148.85 s). The first pass also killed all four (40.03, 46.99, 26.91 and 50.11 s respectively). The second pass also killed them (36.10, 41.00, 37.11, 27.09 s). Sessions-client attribution did not alter the CLI mutant target.

Verification snapshot 2026-11-13 17:19:35 UTC. All 117 current new tests passed complete file runs (54 CLI, 12 submission, 30 validation, 11 migration, 10 retention). Final verification recorded 2026-11-13 17:12:15 UTC. No full-suite green claim is made. Whole-file nonzero results: `tests/unit/test_wait_hub.py`: 1 failed, 43 passed in 155.29s (0:02:35); `tests/unit/test_store_readers.py`: 1 failed, 27 passed in 227.41s (0:03:47); `tests/unit/test_retention_delete_apply.py`: 4 passed in 884.53s (0:14:44). The deadline prevented completed runs of `tests/unit/test_retention_delete_apply.py`, `tests/unit/test_retention_delete_properties.py`, `tests/unit/test_retention_regenerable.py`, `tests/unit/test_retention_archive_properties.py`, `tests/fake/test_admission_properties.py`, `tests/fake/test_admission_stateful.py`, `tests/fake/test_admission_visibility.py`, `tests/fake/test_admission_liveness.py`, `tests/fake/test_admission_route_isolation.py`, `tests/fake/test_reset_credit_incident.py`, `tests/fake/test_unadmittable_pin_review.py`, `tests/fake/test_probe_cancel.py`, `tests/fake/test_unrun_worktrees.py`, `tests/unit/test_daemon_verbs.py`, `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py`. These are unverified, not passes.

No full suite was run. The final driver processes one file at a time, using at most four workers with isolated HOME, temporary files and failed-artifact roots. Read-only `rg` and AST checks located exact job-row/schema compares; all four extra compare files were queued before inherited admission checks. Schema-2 grep hits for conversations.sqlite3 belong to its separate conversation store, not the jobs migration. Unsafe publication-related suites were limited to safe schema nodes. No write-shaped command was executed as a guard probe.

The full archive file passed all 109 tests. Its initial sequential driver then exited during cleanup because a fixture directory was read-only; I initially mistook the leftover tree for cleanup still running. A guarded cleanup repaired directory permissions and removed that completed basetemp. A separate sequential priority driver continued the source checks; unrun archive followups take priority after it, followed by isolated timing rechecks and inherited admission files. A guarded queue removes only basetemps whose commands have returned.

The implementation and test sources remain fixed at the restored hashes above. The contract also indexes the cap in C-6.5. `git diff --check` passed; 18 changed/new Python files parsed successfully.

Compact verification ledger generated 2026-11-13 17:19:18 UTC.

`P` = the scratch launcher `python sf-rv-a2/run-tests.py -q` in the uv-prepared environment; `P4` adds `-p xdist_scratch -n 4`; `D` = direct uv pytest before guarded routing. Each test command includes its recorded fresh `--basetemp`. Exact basetemps, complete argv, logs, restoration hashes, and duplicate-source evidence remain in sf-rv-a2/verification-ledger.md and verification-records.jsonl. File drivers are sequential. P4 requests at most four workers; from the priority protocol-registry file onward the scratch plugin selects serial execution for its explicit small-unit-file set, logs SERIAL, and retains temp isolation/probes. Heavy/fake/archive/CLI256 files retain workers.

Attempt notation: launcher/stage, exit code, p=passed, f=failed, e=errors, d=deselected, s=skipped, then seconds from pytest. M1–M5 identify deliberate mutants; exit 1 is their expected successful kill, and every mutant was restored byte for byte. Every recorded attempt is retained; grouped source order does not imply chronological order. Deferred inherited commands executed zero tests, rather than passing.

132 distinct completed command/results grouped into 82 command rows. Only complete JSONL lines are read, so the final pipeline may still add results.

| Command/file and selectors | Every attempt: launcher/stage → exit, counts, time |
| --- | --- |
| `tests/unit/test_sessions_handoff.py` | P/caller-handoff-rerun-base-0-59488 → exit 0: 145p 275.06s; P/caller-session-regression-2 → exit 1: 3f/142p 393.12s |
| `tests/unit/test_review_subject_cli.py` | D/caller-pytest-1 → exit 0: 42p 25.70s; P/caller-regression-0 → exit 0: 44p 24.47s; P/caller-session-regression-0 → exit 1: 5f/44p 37.64s; P/caller-wire-base-0-46503 → exit 0: 54p 114.51s; P4/priority-base-8-53383 → exit 0: 54p 43.46s |
| `tests/unit/test_cli.py` | D/caller-pytest-cli-1 → exit 0: 256p 143.53s; P/caller-cli-final → exit 0: 256p 145.51s; P4/priority-base-9-53383 → exit 0: 256p 63.41s |
| `tests/unit/test_review_subject_cli.py -k test_explicit_caller_family_wins_over_detection` | P/M3 → exit 1: 5f/7p/32d 20.27s |
| `tests/unit/test_cli_turns.py` | P/caller-regression-1 → exit 0: 23p 57.83s; P4/priority-base-10-53383 → exit 0: 23p 39.07s |
| `tests/unit/test_cli_batch.py` | P/caller-regression-2 → exit 0: 18p 24.65s; P4/priority-base-11-53383 → exit 0: 18p 25.26s |
| `tests/unit/test_cli_qos.py` | P/caller-regression-3 → exit 0: 12p 15.60s; P4/priority-base-12-53383 → exit 0: 12p 29.43s |
| `tests/unit/test_launch_env_round3.py` | P/caller-regression-4 → exit 0: 70p 95.07s |
| `tests/unit/test_sessions_cli.py` | P/caller-session-regression-1 → exit 0: 142p 154.24s |
| `tests/unit/test_sessions_revive.py` | P/caller-session-regression-3 → exit 0: 45p 41.62s |
| `tests/unit/test_handler_refusals.py` | P/corrected-base-0-31954 → exit 0: 382p 142.59s; P4/priority-base-3-53383 → exit 0: 384p 93.88s; P/root-base-0-11030 → exit 1: 1f/381p 171.87s |
| `tests/fake/test_review_subject_validation.py` | P/corrected-base-1-31954 → exit 0: 16p 157.45s; P/latest-validation-base-0-87657 → exit 0: 24p 589.19s; P4/priority-base-1-53383 → exit 0: 30p 31.93s; P/root-base-2-11030 → exit 1: 1f/15p 70.29s |
| `tests/unit/test_protocol_secrecy.py` | P/corrected-base-2-31954 → exit 0: 331p 72.48s; P4/priority-base-14-53383 → exit 0: 331p 66.11s |
| `tests/fake/test_review_subject_submission.py` | P4/final-base-0-47342 → exit 0: 12p 360.97s; P4/priority-base-0-53383 → exit 0: 12p 86.80s; P/review-tests-final → exit 0: 12p 307.49s; P/review-tests-1 → exit 1: 5f/7p 66.16s; P/review-tests-2 → exit 0: 12p 159.80s |
| `tests/unit/test_retention_review_subject.py` | P4/final-base-1-47342 → exit 0: 10p 183.29s; P4/priority-base-2-53383 → exit 0: 10p 67.96s; D/storage-retention-1 → exit 0: 2p 23.99s; P/storage-base-0-88860 → exit 0: 10p 125.22s |
| `tests/unit/test_retention_archive.py` | P4/final-base-2-47342 → exit 0: 109p 1289.20s; P/storage-retention-archive-1 → exit 1: 1f/108p 5303.77s |
| `tests/fake/test_capacity_wait_backoff.py` | P/inherited-base-0-4118 → exit 0: 30p 121.24s |
| `tests/fake/test_admission_latency.py` | P/inherited-base-1-4118 → exit 0: 14p 2713.06s |
| `tests/fake/test_admission_properties.py` | P/inherited-base-2-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_admission_stateful.py` | P/inherited-base-3-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_admission_visibility.py` | P/inherited-base-4-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_admission_liveness.py` | P/inherited-base-5-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_admission_route_isolation.py` | P/inherited-base-6-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_task_hold_admission.py` | P/inherited-base-7-4118 → exit 81: 0 run; deferred before pytest; P4/priority-base-18-53383 → exit 0: 49p 279.49s |
| `tests/fake/test_reset_credit_incident.py` | P/inherited-base-8-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_unadmittable_pin_review.py` | P/inherited-base-9-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_probe_cancel.py` | P/inherited-base-10-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_unrun_worktrees.py` | P/inherited-base-11-4118 → exit 81: 0 run; deferred before pytest |
| `tests/unit/test_daemon_verbs.py` | P/inherited-base-12-4118 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_state_contract.py` | P/inherited-base-13-4118 → exit 81: 0 run; deferred before pytest |
| `tests/unit/test_test_module_names.py` | P/inherited-base-14-4118 → exit 81: 0 run; deferred before pytest |
| `tests/unit/test_invariants_index.py` | P/inherited-base-15-4118 → exit 81: 0 run; deferred before pytest |
| `cat sf-rv-a2/priority-19-test_lane_fault.log ps -p 32152 -o pid,ppid,etime,time,state,command sed -n 1,460p tests/fake/test_lane_fault.py` | lane-fault-diagnosis → exit 0: Read-only diagnosis: controller log had 18 dots; exact ps -p 32152 denied operation not permitted (individual ps exit not captured); final sed succeeded. No broad process query, signals or tests. |
| `python3 - '<<PY' from pathlib import Path from datetime import datetime,timezone import time 'root=Path(/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-080029-rv-a2-subject-build-a1-guqlvaa3/sf-rv-a2/priority-base-19-53383)' 'now=time.time()' 'print(AT,datetime.now(timezone.utc).isoformat())' for p in '[root,*sorted(root.iterdir())]:' 'st=p.stat();' 'print(round(now-st.st_mtime,1),' DIR if 'p.is_dir()' else 'st.st_size,str(p))' if 'p.is_dir()' and 'p!=root:' for q in 'sorted(p.iterdir()):' 'st=q.stat();' 'print( ,round(now-st.st_mtime,1),DIR' if 'q.is_dir()' else 'st.st_size,q.name)' 'scratch=Path(/private/tmp/sf-rv-a2-tests-32152)' 'print(OWNED_SHORT_ROOT,scratch.exists())' if 'scratch.exists():' for p in 'sorted(scratch.iterdir()):' 'st=p.stat();' 'print(round(now-st.st_mtime,1),DIR' if 'p.is_dir()' else 'st.st_size,p.name)' PY sed -n 460,650p tests/fake/test_lane_fault.py sed -n 1,145p sf-rv-a2/xdist_scratch.py` | lane-fault-diagnosis → exit 0: Owned gw0 scratch: fresh lane-fault30 age 13.1s, lane-fault28/29 age 126.9/72.0s. Remaining property/self-mutant examples are advancing despite no pytest completion dots. gw3 latest lane-fault5 directory age 1430.6s. |
| `python3 - '<<PY' from pathlib import Path from datetime import datetime,timezone import time,json 'root=Path(/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-080029-rv-a2-subject-build-a1-guqlvaa3/sf-rv-a2/priority-base-19-53383)' 'now=time.time()' 'print(AT,datetime.now(timezone.utc).isoformat())' for worker in 'sorted(root.iterdir()):' if not 'worker.name.startswith(popen-gw):continue' 'samples=[p' for p in 'worker.glob(lane-fault[0-9]*)' if 'p.is_dir()]' if not samples:continue 'latest=max(samples,key=lambda' 'p:p.stat().st_mtime)' 'print(worker.name,EXAMPLES,len(samples),LATEST,latest.name,AGE,round(now-latest.stat().st_mtime,1))' for p in '[latest,*latest.glob(*),*latest.glob(state/*)]:' if 'p.is_symlink():continue' 'st=p.stat();' 'print( ,round(now-st.st_mtime,1),DIR' if 'p.is_dir()' else 'st.st_size,str(p.relative_to(worker)))' 'print(OWNED WORKER METADATA)' for raw in 'Path(sf-rv-a2/worker-scratch-paths.txt).read_text().splitlines():' 'try:r=json.loads(raw)' except Exception:continue if 'r.get(run)==32152:' 'print(json.dumps({k:r[k]' for k in '(phase,pid,run,worker,path)' if k in 'r}))' PY` | lane-fault-diagnosis → exit 0: gw0 advanced to 33 example directories, latest lane-fault32 age 43.4s; SQLite database mtime 18.7s, shm/WAL 17.6s. gw3 latest lane-fault5/WAL age 1498.7/1482.7s. Exact owned run 32152 worker metadata recorded all workers/controller removed. Fresh gw0 examples prove recent progress; no global hang established. |
| `python3 scratch diagnosis-ledger writer '(heredoc)'` | lane-fault-diagnosis → exit 1: Scratch-only ledger write failed before writing because embedded PY terminator ended the outer heredoc. Following sed source reads ran; no product/test file changed or tests launched. Recreated the ledger using apply_patch. |
| `tests/unit/test_reserve_authorization_plumbing.py` | P/latest-validation-base-1-87657 → exit 0: 5p 128.62s; P/root-base-11-11030 → exit 1: 2p/3e 14.97s |
| `tests/unit/test_protocol_registry.py` | P/latest-validation-base-2-87657 → exit 0: 4p 72.82s; P4/priority-base-4-53383 → exit 0: 4p 21.10s; P/root-base-1-11030 → exit 0: 4p 21.14s |
| `git add -- README.md docs/acceptance-contract.md docs/reports/2026-11-13-review-subject.md subfleet/cli.py subfleet/client.py subfleet/contracts.py subfleet/daemon.py subfleet/ids.py subfleet/launch_env.py subfleet/protocol.py subfleet/refusals.py subfleet/retention_archive.py subfleet/review_subject.py subfleet/sessions/client.py subfleet/store.py subfleet/store_schema.sql tests/unit/conftest.py tests/unit/retention_world.py tests/unit/test_handler_refusals.py tests/unit/test_reserve_authorization_plumbing.py tests/unit/test_retention_archive.py tests/unit/test_store_migration_2.py tests/unit/test_store_qos_migration.py tests/fake/test_review_subject_submission.py tests/fake/test_review_subject_validation.py tests/fixtures/review_subject_release_217.py tests/unit/test_retention_review_subject.py tests/unit/test_review_subject_cli.py tests/unit/test_store_review_subject_migration.py` | manual → exit 128: 29 named source/documentation/test files including report; index.lock Operation not permitted; no files staged or committed. |
| `python sf-rv-a2/run-files.py final sf-rv-a2/final-files.txt` | manual → exit 1: Driver cleanup PermissionError after archive file passed109; read-only fixture final-base-2-47342/back/worktree/ro. Subsequent files not started by this driver; continued using serial priority/archive/admission drivers. |
| `python sf-rv-a2/run-rechecks.py` | manual → exit 1: deadline waiting for priority checks; zero additional tests started |
| `python sf-rv-a2/run-archive-extra.py` | manual → exit 1: deadline waiting for priority file checks; zero additional tests started |
| `python sf-rv-a2/run-admission-final.py` | manual → exit 1: deadline waiting for archive file checks; zero additional tests started |
| `git rev-parse HEAD` | manual → exit 0: 85769b8269162806513030094b362cb3554cbf5a; no commits possible |
| `git diff --check` | manual → exit 0: Final whitespace check passed; source unchanged after final test runs |
| `/usr/bin/python3 -B sf-rv-a2/final-cleanup.py` | manual → exit 0: 184 cleanup roots recorded; zero errors: task temp container,170 known launcher/worker roots and13 generated workspace roots all absent; source/evidence preserved |
| `tests/unit/test_store_review_subject_migration.py` | P4/priority-base-5-53383 → exit 0: 11p 34.96s; D/sf-rv-a2-storage-migration-1 → exit 2: uv cache initialization denied: default UV_CACHE_DIR sdists-v9/.git Operation not permitted; D/storage-migration-2 → exit 0: 11p 52.79s; P4/xdist-trial-base-6872 → exit 0: 11p 113.76s |
| `tests/unit/test_store_migration_2.py` | P4/priority-base-6-53383 → exit 0: 21p 44.58s; P/storage-migration-existing-1 → exit 0: 21p 32.54s |
| `tests/unit/test_store_qos_migration.py` | P4/priority-base-7-53383 → exit 0: 13p 46.77s; P/storage-qos-existing-1 → exit 1: 1f/12p 19.36s; P/storage-qos-existing-2 → exit 0: 13p 8.02s |
| `tests/unit/test_wait_hub.py` | P4/priority-base-13-53383 → exit 1: 1f/43p 155.29s |
| `tests/unit/test_gate_admission.py` | P4/priority-base-15-53383 → exit 0: 24p 50.19s; P/root-base-6-11030 → exit 1: 4f/20p 100.90s |
| `tests/unit/test_store_readers.py` | P4/priority-base-16-53383 → exit 1: 1f/27p 227.41s; P/root-base-5-11030 → exit 1: 1f/27p 129.02s |
| `tests/unit/test_prune_217.py` | P4/priority-base-17-53383 → exit 0: 31p 685.16s; P/root-base-9-11030 → exit 0: 31p 175.31s |
| `tests/fake/test_lane_fault.py` | P4/priority-base-19-53383 → exit 0: 20p 1694.84s |
| `tests/unit/test_retention_delete_apply.py` | P4/priority-base-20-53383 → exit 2: 4p 884.53s |
| `tests/unit/test_retention_delete_properties.py` | P4/priority-base-21-53383 → exit 81: 0 run; deferred before pytest |
| `tests/fake/test_review_subject_submission.py::test_i2_only_accepted_reviews_advance_assigned_rounds -x` | P/M1/pass4 → exit 1: 1f 40.04s; P/M2/pass4 → exit 1: 1f 18.85s; P/M1/pass3 → exit 1: 1f 138.56s; P/M2/pass3 → exit 1: 1f 213.94s; P/M1/pass1 → exit 1: 1f 40.03s; P/M2/pass1 → exit 1: 1f 46.99s; P/M1/pass2 → exit 1: 1f 36.10s; P/M2/pass2 → exit 1: 1f 41.00s |
| `tests/fake/test_review_subject_submission.py::test_i3_pr_url_spellings_share_accepted_history -x` | P/M4/pass4 → exit 1: 1f 12.21s; P/M4/pass3 → exit 1: 1f 55.34s; P/M4/pass1 → exit 1: 1f 26.91s; P/M4/pass2 → exit 1: 1f 37.11s |
| `tests/fake/test_review_subject_submission.py::test_i4_racing_submissions_allocate_inside_the_sql_transaction -x` | P/M5/pass4 → exit 1: 1f 13.26s; P/M5/pass3 → exit 1: 1f 148.85s; P/M5/pass1 → exit 1: 1f 50.11s; P/M5/pass2 → exit 1: 1f 27.09s |
| `tests/fake/test_review_subject_submission.py::test_i4_racing_submissions_allocate_inside_the_sql_transaction tests/fake/test_review_subject_validation.py::test_automatic_retry_keeps_its_round_and_changed_subject_is_a_new_payload tests/fake/test_review_subject_validation.py::test_pending_review_holds_then_releases_the_next_accepted_round` | P/review-round-correction-focused-1539 → exit 0: 8p 17.72s |
| `tests/unit/test_store_ids.py` | P/root-base-3-11030 → exit 0: 27p 32.62s |
| `tests/unit/test_offline.py` | P/root-base-4-11030 → exit 0: 33p 151.28s |
| `tests/unit/test_gate_service.py -k offline_reader_understands_gate_schema` | P/root-base-7-11030 → exit 0: 1p/24d 3.35s |
| `tests/unit/test_host_push.py -k schema_six_migration_preserves_jobs_without_authorizing_push` | P/root-base-8-11030 → exit 0: 1p/119d 12.25s |
| `tests/unit/test_prune_decisions.py` | P/root-base-10-11030 → exit 0: 48p 155.07s |
| `tests/unit/test_qos_review_r2.py` | P/root-base-12-11030 → exit 0: 5p 17.66s |
| `tests/unit/test_timers_daemon.py` | P/root-base-13-11030 → exit 0: 4p 17.02s |
| `tests/fake/test_status_payload.py` | P/root-base-14-11030 → exit 0: 2p 36.92s |
| `tests/unit/test_status_json.py` | P/root-base-15-11030 → exit 0: 56p 52.68s |
| `tests/unit/test_protocol.py` | P/root-base-16-11030 → exit 4: 0 run; missing path 8.12s |
| `tests/unit/test_ids.py` | P/root-base-17-11030 → exit 4: 0 run; missing path 7.18s |
| `python3 -m py_compile tests/unit/test_store_review_subject_migration.py tests/unit/test_retention_review_subject.py` | storage → exit 0: compiled both new test files |
| `git diff --check -- subfleet/store.py subfleet/store_schema.sql subfleet/contracts.py tests/unit/test_store_migration_2.py tests/unit/test_store_qos_migration.py tests/unit/test_store_review_subject_migration.py tests/unit/test_retention_review_subject.py` | storage → exit 0: no whitespace errors |
| `kill -INT 60892` | storage → exit 1: signal to owned archive pytest runner denied operation not permitted; left running |
| `tests/unit/test_policy_support.py` | P/storage-base-1-88860 → exit 0: 14p 19.32s |
| `git diff --check -- subfleet/retention_archive.py tests/unit/test_retention_review_subject.py` | storage → exit 0: no whitespace errors after durable archival history change |
| `python3 -m py_compile subfleet/retention_archive.py tests/unit/test_retention_review_subject.py` | storage → exit 0: compiled durable archival history implementation and tests |
| `tests/unit/test_timers_retention.py` | P/storage-base-2-88860 → exit 0: 20p 157.98s |
| `tests/unit/test_timers_busy_read.py` | P/storage-base-3-88860 → exit 0: 115p 2263.13s |
| `python3 -` | storage → exit 0: Guarded cleanup completed: asserted sf-rv-a2 in old basetemp path, exact scratch parent, and no root symlink; chmod owned nested dirs, shutil.rmtree; verified old basetemp and runner safe-home absent |
| `uv --cache-dir sf-rv-a2/uv-cache run --no-sync python sf-rv-a2/trial-xdist.py` | xdist-trial-deviation.jsonl → exit 1: Wrapper post-check initially found worker87375 root after pytest itself passed11; guarded manual cleanup and controller fallback resolved it. |
| `tests/fake/test_review_subject_submission.py -k 'i4 or i5'` | P/review-tests-3 → exit 0: 4p/8d 45.06s |

Failure and deviation explanations:

- New fixture omitted SubmitArgs.sandbox in five cases; corrected fixture.
- Basetemp inside this git worktree made nominal non-git fixture directories inherit .git; rerun outside git.
- Stopped before pytest; required inherited suite deferred, zero tests executed.
- Existing lease-write timing assertion exceeded its unchanged 2s bound (15.375s initially, 4.618s in the priority rerun); cause not diagnosed.
- Existing row oracle lacked new review_subject field; fixture oracle updated.
- Retry fixture sent a changed subject payload; corrected fixture.
- Old schema fixture omitted review_subject needed by new index; updated fixture.
- Requested path did not exist; zero tests executed.
- Default cache denied; retry used workspace scratch cache.
- Property exposed refused-submit artifact/job-id reuse; fixed refusal cleanup and fixture lock/workdir setup.
- Existing QoS migration-event fixture query needed ORDER BY event_id after migration 11 added another event; corrected and rerun.

Preparation and infrastructure notes (root-reported where stated):

| Command/action | Result |
| --- | --- |
| `date -u` (root first command) | 2026-11-13 13:27:20 UTC; hard stop 17:27:20 UTC. |
| `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache" uv sync --group dev` (root-reported) | Exit 2: default cache denied. |
| `uv --cache-dir "$PWD/sf-rv-a2/uv-cache" sync --group dev` (root-reported) | Exit 0: nine packages; Python 3.15.2. |
| Named-source `git add` attempts, twice (root-reported) | Exit 128: shared index.lock denied; no commit made by these attempts. Latest named set: subfleet/cli.py, client.py, contracts.py, daemon.py, ids.py, launch_env.py, protocol.py, refusals.py, retention_archive.py, review_subject.py, sessions/client.py, store.py, store_schema.sql. |
| Scratch driver toggle exact-string replacement (root-reported) | Assertion failed because the identical SF_RV_A2_XDIST toggle was already installed; failed script made no file writes; final driver used the correct toggle. |
| Xdist trial wrapper post-check | Pytest itself passed 11; wrapper exit 1 found worker87375 still present. Exact known path removed with guarded shutil.rmtree; all five trial worker roots confirmed absent. Controller cleanup fallback was then added; no product fixtures changed. |
| Initial direct caller and storage test runs | Preceded the guarded tempfile launcher. Some ordinary fixture temp directories lacked the required sf-rv-a2 name and were removed by their fixtures; exact paths were not recorded. This is a deviation, not a claimed guarded cleanup. |
| Read-only inspection/search | Scoped to assigned source/tests/docs plus the named external hub rules, exact installed Codex package, official Codex source/docs, and own environment variable names only. Exact-row/schema-version searches determined additional fixture and migration regressions. No live caller checkout writes or environment values were printed. |
| Guard checks | Commands were passed as data; no gh delete, gh merge, or git push command was executed as a guard probe. |
| Scratch-only serial override | Installed xdist/plugin.py lines 305–328 maps numprocesses into dist/tx and disables distribution at zero. A tryfirst scratch hook clears numprocesses, dist, and tx for exactly one explicitly listed small unit file; it logs SERIAL, preserves routing/probes, and leaves heavy/fake/archive/CLI256 files unchanged. The priority protocol-registry file passed under this override; no overlapping test trial was started. |


Recorded scratch roots snapshot 2026-11-13 17:19:18 UTC.

Each exact path is its prefix concatenated with one listed suffix. Present paths may belong to the running final pipeline; this read-only generator deletes nothing. The full lifecycle/evidence table is in sf-rv-a2/scratch-roots.md.

Task scratch container: `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-080029-rv-a2-subject-build-a1-guqlvaa3/sf-rv-a2` (absent).

Prefix `/private/tmp/sf-rv-a2-tests-`; absent suffixes (89): `1024`, `1062`, `10701`, `11305`, `11327`, `11330`, `1171`, `12902`, `13071`, `16029`, `16031`, `17069`, `18994`, `20516`, `20980`, `21111`, `21199`, `25100`, `26665`, `26984`, `27538`, `29461`, `29927`, `30181`, `30242`, `30249`, `30255`, `30317`, `31397`, `32124`, `32152`, `32467`, `32854`, `35741`, `38281`, `3830`, `3890`, `39312`, `40321`, `40491`, `41317`, `4179`, `43994`, `44280`, `46449`, `46555`, `47562`, `48191`, `50737`, `50784`, `51011`, `51779`, `53153`, `53703`, `54187`, `54743`, `58369`, `59899`, `60892`, `61521`, `65453`, `67429`, `69834`, `70021`, `70346`, `72040`, `72963`, `76455`, `79594`, `80060`, `80189`, `80944`, `83391`, `83835`, `86152`, `87203`, `87940`, `89030`, `89596`, `91445`, `93097`, `93988`, `94584`, `94599`, `9478`, `95164`, `96684`, `98412`, `98717`.

Prefix `/private/tmp/sf-rv-a2-worker-`; absent suffixes (81): `11327`, `1139`, `12635`, `14908`, `14953`, `15058`, `18994`, `19173`, `19407`, `19764`, `19787`, `21111`, `2170`, `26665`, `26984`, `29069`, `30242`, `30317`, `31397`, `32133`, `32152`, `33073`, `33082`, `38281`, `3830`, `38562`, `39292`, `39312`, `39372`, `39421`, `47562`, `49008`, `49753`, `49998`, `50274`, `51011`, `51779`, `53703`, `56261`, `5655`, `58101`, `58811`, `59544`, `62060`, `62618`, `62806`, `62966`, `65081`, `65453`, `65865`, `66136`, `66311`, `6896`, `70021`, `80060`, `80944`, `8154`, `8214`, `8280`, `83391`, `83796`, `86284`, `87276`, `87340`, `87375`, `87438`, `87541`, `87674`, `89596`, `93101`, `93460`, `93482`, `93525`, `93555`, `93988`, `9478`, `95164`, `96708`, `98520`, `99178`, `99433`.

Prefix `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-080029-rv-a2-subject-build-a1-guqlvaa3/sf-rv-a2/`; absent suffixes (35): `caller-handoff-rerun-base-0-59488`, `caller-wire-base-0-46503`, `final-base-0-47342`, `final-base-1-47342`, `final-base-2-47342`, `latest-validation-base-0-87657`, `latest-validation-base-1-87657`, `latest-validation-base-2-87657`, `priority-base-0-53383`, `priority-base-1-53383`, `priority-base-10-53383`, `priority-base-11-53383`, `priority-base-12-53383`, `priority-base-13-53383`, `priority-base-14-53383`, `priority-base-15-53383`, `priority-base-16-53383`, `priority-base-17-53383`, `priority-base-18-53383`, `priority-base-19-53383`, `priority-base-2-53383`, `priority-base-20-53383`, `priority-base-21-53383`, `priority-base-3-53383`, `priority-base-4-53383`, `priority-base-5-53383`, `priority-base-6-53383`, `priority-base-7-53383`, `priority-base-8-53383`, `priority-base-9-53383`, `storage-base-0-88860`, `storage-base-1-88860`, `storage-base-2-88860`, `storage-base-3-88860`, `xdist-trial-base-6872`.

Recorded trial worker roots sf-rv-a2-worker-30317, -83796, -86284, -87375, and -87674 are absent after guarded cleanup. Launcher/worker cleanup asserts the sf-rv-a2 name and /private/tmp parent; task basetemp cleanup asserts sf-rv-a2 and the permitted task temp ancestry. Earlier direct tests used unrecorded ordinary fixture roots; this remains an explicit deviation.

Generated cleanup at 2026-11-13 17:18:08 UTC.

Required completion marker: `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/no-active-tests.json`.

Exact paths and outcomes are recorded in `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/final-cleanup-results.jsonl`.

Task temp container: `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-080029-rv-a2-subject-build-a1-guqlvaa3/sf-rv-a2` — removed.

Known launcher/worker roots: 170 recorded; 0 errors in all cleanup operations.

| Original generated workspace path | Renamed disposal path | Final state |
| --- | --- | --- |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/tools/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/00-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/01-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/adapters/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/02-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/sessions/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/03-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/conversations/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/04-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/gate/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/05-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/subfleet/guard/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/06-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/tests/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/07-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/tests/unit/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/08-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/tests/fixtures/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/09-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/tests/fake/__pycache__` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/10-__pycache__` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/.venv` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/11-.venv` | absent (removed) |
| `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/.pytest_cache` | `/Users/maxghenis/.subfleet/worktrees/20261113-080029-rv-a2-subject-build/sf-rv-a2/dispose/12-.pytest_cache` | absent (removed) |

Only manifest-owned generated roots and additional `__pycache__` directories under `subfleet`, `tests`, and `tools` are eligible. Source and evidence paths stay in place. No directory symlinks are followed.

Deviations and practical bounds: At the hard cutoff, 17 regression files remained incomplete or unrun; the final status names every one. All test drivers returned and guarded cleanup finished at 17:18:08 UTC with 184 roots recorded and zero errors. A late contract check corrected the initial allocator, which had incorrectly skipped pending rounds. The final allocator refuses an already-held candidate and counts only accepted history; corrected race/failure tests and a final mutant replay verify it. Initial inherited retention fixtures executed local `git push` commands, a literal-rule violation I missed. Fixture setup now seeds local bare repositories with `git fetch`; the final archive runs use that setup. Interrupting the already-running old archive process was denied; it eventually finished with 108 passed and one temporary-source fixture-location failure. The first few pytest invocations used existing fixtures' ordinary temporary paths before the scratch-aware launcher was available; they did not meet the requested naming/guard-log rule; those fixtures removed them, but their individual names were not recorded. Later short socket/home roots are named `/private/tmp/sf-rv-a2-tests-<pid>` and removed by guarded `shutil.rmtree`. Basetemps inside the checkout caused neutral-directory and temporary-source fixture mismatches; corrected runs use the permitted task temp root's `sf-rv-a2` child. A store-reader timing test saw a 15.38 s commit against its 2 s bound on its unchanged lease-write path; the later whole-file run measured 4.618 s against the same 2 s bound. The additional wait-hub file measured 0.894 s against a 0.600 s poll bound (43 other cases passed). Targeted serial rechecks were queued but did not start before the deadline; the helper returned with zero additional tests. The timing causes were not diagnosed; those assertions were not weakened. A validation run overlapped the deliberate URL mutant and failed its canonicalization case; the clean final validation run passed all 30. The second mutation replay reused pytest basetemp names (pytest recreated them); the third replay used fresh PID paths. A scratch toggle edit assertion raced an identical agent edit, made no writes, and the correct final driver ran. One xdist trial worker missed teardown; guarded cleanup removed it and a controller cleanup fallback now handles that case. The I5 differential fixes identical daemon submission inputs, including the legacy caller context. It does not establish unchanged CLI admission after Codex gains a caller id: existing session writer checks now see that identity, and without `CLAUDE_PID` a second overlapping writable job in the same thread can hit the existing `unidentified-writer` refusal. Writer policy and PID detection were not changed here; strict equivalence across that newly identified CLI context is a limitation. Archive restoration restores artifacts, not SQL jobs; the durable retirement audit preserves accepted-round history independently of restored files. The prepared request manifest keeps a null automatic requested round; the assigned round is authoritative in the committed job row. Dry-run routing creates no round reservation.

Git refused the named-file staging command: `Unable to create '/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-080029-rv-a2-subject-build/index.lock': Operation not permitted`. No commit or history rewrite was possible; the implementation and tests remain in the assigned workspace. A final `git add --` named all 29 source, documentation and test files, including this report; it also exited 128 with the same denial.

Head built: 85769b8269162806513030094b362cb3554cbf5a
