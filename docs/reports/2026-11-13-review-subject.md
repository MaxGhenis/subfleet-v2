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


## Fix round 1

Started **2026-11-15 23:13:39 UTC** (`date -u`, first command); deadline **2026-11-16 03:13:39 UTC**. All source changes are confined to the assigned worktree. The initial checkout was clean and detached at `5b06c1826bc0641f65b1293197a505b8c8a4843a`. Findings 1–7 and P3 finding 8 are fixed; no requested finding is deferred.

1. **Migration collision (P1).** Version 11 named two incompatible schemas. Slot 11 now contains exactly #307's notice columns, caller-wake session table and index, without its runtime or historical-notice mutations. Review fields occupy slot 12. Upgrades inspect columns and reconcile both slots for either historical version-11 layout; tables/indexes use `IF NOT EXISTS`. `test_store_upgrade_slots.py` uses SQL dumps created by each original Store after `git archive` extraction of release/217 `a4aded8859fc`, #307 `02ae7d1ed`, and old #314 `5b06c1826bc0`. Three cases preserve historical job/notice data, upgrade to 12 and open twice unchanged; a fourth compares slot 11's migration tuple and SQLite DDL with #307, including its current branch when available. All four failed before the fix and pass afterward. The changed migration-number pin also failed before the fix. Two further cases emulate an already-recorded version 12 over each incomplete version-11 layout: both failed before the follow-up, then repaired and reopened without changing existing version/audit records. Presence reconciliation now runs on current-version opens too, so the hub can retain this behavior when merging #330's additive review fields.
2. **Schema-script failure (P2, pre-existing).** Migration/version commits preceded the schema script. One `BEGIN IMMEDIATE` now covers migration DDL, schema statements and version/audit records; versions are written after every schema statement succeeds. Complete-statement execution avoids `executescript`'s implicit commit. All three historical-store failure/reopen cases failed before the fix and pass afterward, proving full rollback followed by successful retry and unchanged second reopen.
3. **Codex writable fan-out (P1).** Codex acquired a session id without a Claude process id. The daemon now skips only the session writer-instance comparison for Codex, after worktree and revive checks and before configured-cap enforcement. The fleet-fixture regression with synthetic `CODEX_THREAD_ID` failed with `unidentified-writer` before the fix and now admits both writable jobs to distinct worktrees. The same test verifies worktree, revive and configured-cap refusals; the existing fan-out file also passes. The new file is `test_codex_writable_fanout.py` to satisfy the repository's unique-basename rule.
4. **Older daemon drops metadata (P2).** Clients now require `review.subject.v1`, using `_daemon_capabilities`, before submitting any subject, round or caller family through run, batch, resume or Sessions. Missing capability refuses with restart/upgrade fix text before submission. Nine new cases, including an older-daemon resume, failed against the original behavior and pass afterward. The current daemon advertises the capability. Four older positive CLI fixtures were updated to advertise support.
5. **Resume loses subject/round (P2).** CLI resume forwards both fields; the daemon authoritatively inherits its source's subject and round, continuing that round. The CLI regression and the reviewer's four-step daemon scenario each failed before the fix. Both now pass: failed round 1 → resume retains subject/1 → successful resume → next automatic review receives round 2. The importer run caught a follow-up: inheriting fields before validation silently dropped invalid review metadata on a non-review source. The daemon now validates that effective task before inheritance; the full validation file and new resume scenario pass together (31 cases).
6. **PR spelling splits history (P2).** Normalization now recognizes `/changes`, `/commits`, `/checks`, `/files/<sha>`, schemeless links, `www.github.com`, and `owner/repo#N`, including owner/repository case. Each of the seven parameterized cases failed before the fix and passes afterward.
7. **Nested caller family (P2).** When both families' markers are present, the nearest ancestor executable named Claude or Codex decides; matching native session and process attribution follow that family. Unreadable, malformed or cyclic ancestry leaves other tasks unknown and refuses review submissions with fix text naming `--caller-family`. Resume now accepts that flag. Eleven synthetic-ancestry/CLI/Sessions cases failed against the original behavior and pass afterward; tests never inspect the host's ancestry. Explicit flag and environment override precedence remain intact.
8. **Readable runs columns (P3).** The added universal name column shifted workdir. The original columns are restored for every row; review name/round annotations follow workdir. The new column-compatibility case failed before the fix and passes afterward, alongside the existing review-label assertion.

Contract C-3.8, C-6.5, C-6.21–22 and the existing change-log line are amended to describe these behaviors. The new ambiguity refusal is included in the C-6.5 index. README examples now describe capability checks, nested attribution, same-round resume and unchanged readable columns; their review commands include the required tier. Independent review of the final daemon/CLI changes found no additional blocker within this brief.

Verification started with new/changed tests, followed by direct importers and the four required contract/refusal files. All **40 new regression cases** have valid failing-before/passing-after evidence; missing-fixture/setup errors are excluded from that evidence. Initial complete-file checks passed 421 cases across 13 files. After importer-discovered corrections and the synthetic GraphQL recheck, the latest disjoint focused whole-file runs contain **468 passes across 15 files**. These overlap the importer runs and are not added to their totals.

[Every pytest command](2026-11-13-review-subject-fix1-data/commands.md) has its exact arguments, selector, unique basetemp, exit status, count and elapsed time; [raw records](2026-11-13-review-subject-fix1-data/test-commands.jsonl) contain **48 commands and 48 distinct pytest basetemps**. Directory-style commands are restricted by their [selection snapshots](2026-11-13-review-subject-fix1-data/batch-selections.jsonl). Empty raw count keys count setup/teardown reports, not cases. Interrupted manual counts explicitly identify observed progress spans rather than launcher durations. Setup, archive, read-only inspection, edit and named-file commit commands/results are recorded in [root](2026-11-13-review-subject-fix1-data/root-actions.md), [schema](2026-11-13-review-subject-fix1-data/schema-actions.md), [CLI](2026-11-13-review-subject-fix1-data/cli-actions.md) and [subject](2026-11-13-review-subject-fix1-data/subject-actions.md) action ledgers; routine inspections/polls are grouped there rather than reproduced individually in this section.

| Validation | Result |
| --- | --- |
| Latest disjoint focused whole-file runs | 468 passed / 15 files, including 54 schema, 29 fan-out, 330 CLI, 7 normalization, 31 validation/resume and 17 restart/refusal cases. |
| Primary importer selections / 334 files | Historical totals: 12,021 passed, 8 failed, 109 skipped, 94 xfailed, 7 unfinished. Includes interrupted batch 00 and subsequently corrected/rechecked failures; this is not a clean final-suite result. |
| Remaining 202 importer files, guarded batch 12 | 4,977 passed, 6 failed, 90 skipped, 52 xfailed; exit 1, 903.43 s. Supersedes identical interrupted batches 10/11 only for coverage counting. |
| Extra 11 importer files, including seven frontend files | 82 passed, 1 failed, 107 skipped; exit 1, 32.83 s. Six Swift-probe modules were guard-refused; the seventh frontend file ran. |
| Safe subsets of five mixed host-push files | 99 passed; exit 0, 2.37 s; 16 audited selectors with unsafe fixture setup forbidden. |
| CLI after refusal-index correction | 330 passed; exit 0, 11.72 s. |
| Restart/refusal file using synthetic GraphQL callback | 17 passed; exit 0, 0.50 s. |
| Required final four files, repeated last | **447 passed; exit 0, 19.45 s.** |

Batch 00's unfinished daemon-refusal cases and store-reader timing assertion were rechecked together: nine passed under an outside-Git basetemp. The source of the reader methods was unchanged; scheduling explains a possible timing failure but was not proven against a baseline. Batch 01 found the real resume-validation regression described in finding 5; its full validation/resume recheck passed 31 cases. Existing prune fixtures also needed the new `caller_wake_sessions` table and removal of indexes whose partial predicates mention a deliberately dropped column; their focused recheck passed 35 with one environment skip, and the final main importer batch passed the file. The first required four-file run passed 446 and failed one source-index assertion because two new client-side `ProtocolError` constructions lacked a reason. Both now carry the existing request reason `refused-args`; the final repeat passes all 447.

All six batch-12 failures logged the guard's refusal of external `gh` at a read-only GraphQL fixture boundary. A scratch-only, exact-file-qualified Python callback returns only the existing synthetic missing-PR response; production query parsing, durable state and restart behavior still run. Its inert predicate proof passed six checks with zero external commands. An initial callback qualification mistake retained 11 passes/six guard failures; the corrected full-file recheck passed all 17. The [seven-case diagnostic](2026-11-13-review-subject-fix1-data/guard-case-results.json) preserves these classifications. **One validation limitation remains:** `test_review_launch_permissions.py::test_the_installed_publish_hook_is_run_before_launch` cannot pass under this job's prohibition on executing write-shaped `gh` commands, including behind stubs. Its hook self-test was refused, so the expected guardian did not start. The guard was not relaxed; that original failure remains in the ledger.

The [import inventory](2026-11-13-review-subject-fix1-data/importer-inventory.json) starts from `git grep -l`: 677 candidates, 380 directly importing test modules and 12 support files; the separately recorded helper closure contains 544 test modules. Full-file selections cover 360 direct modules, plus two required index/name files; five further direct modules received only the safe selectors above. Fifteen direct modules were entirely excluded, and skipped cases remain incomplete coverage. Publication suites execute prohibited Git/GitHub writes; native census/ancestry suites inspect host processes; one real-provider suite lacked verified executable isolation. Marked `real_lsof`/`real_cwd`, native-holder/guardian, opaque-executable and guard-refused cases were excluded. Exact reasons and final refinements are in [the source audit](2026-11-13-review-subject-fix1-data/safety-audit.md), [exclusion audit](2026-11-13-review-subject-fix1-data/skipped23-audit.json), [refinement](2026-11-13-review-subject-fix1-data/importer-refinement.json) and [skip records](2026-11-13-review-subject-fix1-data/guard-skips.jsonl). Preliminary exclusion lists contain superseded classifications; actual selections/results govern. CI needs the omitted coverage in an authorized environment. The whole suite was not run, as instructed.

Deviations and limits are explicit:

- **Setup.** The mandated scratch/cache children under `getconf DARWIN_USER_TEMP_DIR` were sandbox-denied. `uv sync --group dev` succeeded with Python 3.15.2 and nine development packages using `/private/tmp/sf-a2-fix1/uv-cache`. Commands initially set the requested `TMPDIR`; launchers then used short task-owned fixture temp paths. Later pytest basetemps used the permitted neutral attempt child ending `sf-a2-fix1`, after `/private/tmp` triggered outside-Git fixture fallback. Parallelism never exceeded four. An early default-cache interpreter query was denied; the interpreter was unchanged.
- **Initial folder accounting.** Early socket fixtures created/deleted nine `/tmp/sf-*` roots before interception recorded explicit temporary directories. Their original cleanup lacked the requested task-name assertion. All nine known paths are recorded in the cleanup inventory; earlier successful `sfwork-*` fallback paths were already deleted and cannot be reconstructed. The original folder-recording rule was therefore not fully met. Later paths were recorded and guarded.
- **Host-process attempt.** One early hook test attempted `/bin/ps -axww -o command=`; the sandbox denied it and the test skipped on `PermissionError`. Controller/worker records describe one attempt. No successful host-table read is established.
- **Prohibited synthetic launch.** An early live-store-guard test actually launched its synthetic executable named `security` once. It used synthetic data and did not invoke the macOS binary, but still violated the command-name prohibition. This is retained as a deviation, and continuation excluded the case.
- **Late isolation correction.** Initial marker filtering and parent-only mocks did not isolate native inspection in fake daemon children. Source review found possible census paths; the available records do not establish which short-lived children reached them. Subsequent guards loaded before imports in project/uv/child interpreters, denied native readers and prohibited/opaque commands, and allowed only narrowly checked trusted wrappers or compiled probe paths/hashes. V1/V2 inert proofs passed 11/18 checks with zero external commands. Forbidden command arrays were passed only as data to guard predicates.
- **Interrupted validation.** Initial broad collection ran zero tests before interruption; batch 00 stalled on fixture location; batches 10/11 were interrupted for safety review and denied-reader linger. V1's reconstructed 4,765 passes/188 failures/109 skips/52 xfails/11 unfinished remain recorded. The full V2 run supersedes that identical selection only in aggregate coverage. A Python-to-uv coordinator was itself guard-refused; later runs used foreground direct commands. Tool-owned Ctrl-C ended interrupted commands; an earlier interrupt of the owned runner was sandbox-denied. No unrelated process was inspected or signaled for those actions.
- **Other corrections.** An unrestricted historical archive was interrupted and removed with the task-path assertion before package-only extraction. Missing-fixture/QoS assertion errors, read-only inspection errors and the evidence copier's initial rejection of reviewed upgrade-manifest fields are recorded. The final refusal fix required focused CLI checks and a repeat of the final four files after their initial run. Routine read-only commands are grouped in the action ledgers rather than individually repeated here.
- **Commits.** Named-file staging/commit attempts after coherent steps failed with exit 128 because the shared worktree `index.lock` is not writable. Denied staging also left new files unknown to a subsequent path-only commit attempt. No commit was created, no history was rewritten and nothing was pushed. The hub must collect the final working files.

Cleanup completed after all owned test commands returned. The [main journal](2026-11-13-review-subject-fix1-data/cleanup.jsonl) records all 3,485 inventory entries before deletion: 3,458 allocation records, nine known early roots, 16 generated workspace folders and two scratch roots. Eighteen guarded deletions removed the generated folders and both scratch roots; the nine early roots were already absent. A [supplemental journal](2026-11-13-review-subject-fix1-data/supplemental-cleanup.jsonl) records removal of the generated Hypothesis/frontend caches and disposal folder omitted from that inventory. The tracked pre-existing `.coverage` file is preserved. Guard removal verified 13 manifest paths: three installed files removed, ten overlay paths already absent, zero remaining. Deletions use descriptor-safe `shutil.rmtree` with the required `sf-a2-fix1` assertion, moving generated roots into a marked disposal path when necessary. The initial cleanup dry-run's quadratic protection check was interrupted before deletion and replaced by an equivalent precomputed ancestor set. Final `git diff --check` and AST parsing of all 19 changed/new product/test Python files passed. [Evidence hashes](2026-11-13-review-subject-fix1-data/sha256-manifest.json) cover the retained small ledgers, selectors, guard proofs and cleanup scripts; databases, archives, caches and full test logs were removed. Report completed **2026-11-16 01:53 UTC**, before the four-hour deadline. All fixes remain uncommitted because the shared index is denied.

**Head built:** `5b06c1826bc0641f65b1293197a505b8c8a4843a`


## Release merge 1

Started **2026-11-20 17:36:39 UTC** (`date -u`, first command); deadline 20:36:39 UTC. The job resumed attempt a1's workspace (salvage ref `refs/subfleet-salvage/detached-20261120T162853Z-a1`, commit 4c31a7f58): a1 had started `git merge --no-ff --no-commit 4aed7a3c9` at 2954abd77, left the five conflicted paths resolved in the working tree but unstaged, and committed nothing. I checked each of a1's resolutions against the brief before staging it, reran the contract script and committed the merge.

**Release moved on.** Local `origin/release/217` is at 35f8615af, 21 commits past 4aed7a3c9, and `git fetch --dry-run` shows the remote at 024b23f80. As the brief says, the merge takes 4aed7a3c9. Merge base 85769b826; release has 173 commits since it and the PR has 3.

**Merge commit** 9292b0e640296067eea171bc12da7cbde5910ff7. Its parents (`git log -1 --format=%P`) are 2954abd77bb94ba10a3a082bd7a9ad01898b0cac and 4aed7a3c977d7813262cac21016fb65d37576ba2. `git diff --check` is clean. `git diff --check 2954abd77 HEAD` reports 295 trailing-whitespace lines, all in release's committed evidence logs under `docs/reports/2026-11-0*`; `git diff --check 85769b826 4aed7a3c9` reports the same 295, byte for byte.

### Conflicted files

1. **`subfleet/client.py`, `DaemonError.__init__`.** Release (7dea5c3a6) keeps `task-hold-condition` even when its details don't parse; before, the reason was dropped with the details. The PR keeps its own fixed reason `review-round-limit`, with no details. Result: release's `self.reason = reason if reason == "task-hold-condition" else None`, then the PR's two `review-round-limit` lines.
2. **`subfleet/protocol.py`.**
   - Constants: release's `TASKS_IF_NOTE_PREFIX_V2_CAPABILITY` (7dea5c3a6) and `TASKS_SANDBOX_CAPABILITY` (7918a8aad), then the PR's `REVIEW_SUBJECT_CAPABILITY`, each with its comment.
   - `refuse()`: the brief's seven lines exactly. `condition` decides `task-hold-condition` whether or not its details parse. `review-round-limit`, as an exact `str`, replaces the public reason. The PR's comment above them stays.
3. **`subfleet/conversations/service.py`.** `CAPABILITIES` ends `TASKS_IF_NOTE_PREFIX_CAPABILITY, TASKS_IF_NOTE_PREFIX_V2_CAPABILITY, TASKS_SANDBOX_CAPABILITY, REVIEW_SUBJECT_CAPABILITY`.
4. **`subfleet/retention_archive.py`, `Retirement.commit`'s `else:` branch.**
   - The PR's block comes first: it stamps `review_subject` and `review_round` on an accepted, successful review's event. Release's block (bc407d68e) follows: `rnotices.write_outcomes` writes the outcome notices and records `outcome_notices`. Both run before the `DELETE` statements.
   - The two blocks set different keys of `data_event`. `rnotices` is imported at line 51, and `RetentionContext.notice_keep_s` exists at line 255.
5. **`docs/acceptance-contract.md`.** The hub's script resolved it (below).

### Schema slots (D-SCHEMA-SLOTS1)

On the merge, `subfleet/store.py` has `PROVIDER_QOS_MIGRATION = 10`, `NOTICE_WAKE_MIGRATION = 11`, `REVIEW_SUBJECT_MIGRATION = 12` and `SCHEMA_VERSION = REVIEW_SUBJECT_MIGRATION`. `MIGRATIONS` has keys 4 to 12. Line 1 of `store_schema.sql` reads "version 12 (#307 storage, caller family and review subjects/rounds)", and line 331 is release's `CREATE INDEX IF NOT EXISTS events_notice_retired`. Release at 4aed7a3c9 is still version 10, with no `MIGRATIONS` key above 10.

An AST read of `MIGRATIONS` shows that the merge's `[11]` equals 2954abd77's and #307's at both 963d1afb9 and 02ae7d1ed. `git diff 2954abd77 HEAD` touches nothing in `tests/fixtures/store/review-upgrade/` or `tests/fixtures/review_subject_release_217.py`. No slot was renumbered or added, and no fixture was regenerated.

**The brief's premise about the v10 fixture is wrong.** `release-217-v10.sql` is not the base's store. Its first line says it was created from a4aded8859fc, release/217 on 2026-11-15, after bc407d68e, and it already contains `events_notice_retired`. So does `wake-caller-307-v11.sql`, which was created from 02ae7d1ed. Only `review-subject-old-v11.sql`, from 5b06c1826 on the base, lacks the index, and the schema script adds it on open. This is what the first new test can show (below).

### Contract

I ran `python3 /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py HEAD 4aed7a3c977d7813262cac21016fb65d37576ba2` from the worktree root while the merge was stopped. It printed `merged against 85769b826: 260 clauses, none duplicated` and exited 0. Its output is byte-identical to a1's unstaged working file. No hand resolution was needed.

A checker script (in my scratch folder) compared the three committed versions with the result, line by line. Every check passed:

| Check | Base | PR head | Release | Merged |
| --- | --- | --- | --- | --- |
| File lines (split on `\n`; `wc -l` is one less) | 843 | 847 | 866 | **870** = 847 + (866 − 843) |
| Change-log entries | 344 | 345 | 366 | **367** = 345 + (366 − 344) |
| Clauses, lettered ids included | 260 | 263 | 260 | **263**, none duplicated |
| Clauses, by the script's pattern (no lettered ids) | 257 | 260 | 257 | 260 |

- **Change log.**
  - The PR adds one entry. Release adds 22 and edits one more in place: its C-5.12 D-KILL3 entry (2026-10-22, PR #223) gained text. So "release − base = 22" is the net figure.
  - The merged entries, as a multiset, are head + release − base. The PR's entry is first. After it, the merged change log is release's change log exactly and in order, blank line included. Two of release's new entries (C-29.9, 2026-10-12) sit further down, where release put them, not at the top.
  - One entry, "C-20.6, C-23.28 (2026-10-15, D-ST1, PR #198 round 8)", appears twice in base, head, release and the merge. The duplicate predates both sides; neither side's change added it.
- **Clauses.**
  - The PR adds C-3.8, C-6.21 and C-6.22, and edits C-6.5 and C-17.2. Release adds none and edits exactly the 13 the brief names: C-4.5, C-5.12, C-6.10, C-8.4, C-9.3, C-11.10, C-15.3, C-17.1, C-23.14, C-23.25, C-23.26, C-25.7 and C-29.9.
  - Neither side removes a clause, and no clause was edited on both sides. Every merged clause equals the line of the side that changed it, or the base's line where neither did. C-17.1 is release's line byte for byte, and C-17.2 is the PR's.
  - Clause order equals the PR head's order. The whole file's line multiset equals base + both sides' additions − both sides' removals.

No clause text changes in the merge, so no change-log entry was added.

The checker's output (argv `85769b826 2954abd77 4aed7a3c9 docs/acceptance-contract.md`; exit 0):

```text
lines (split): {'base': 843, 'head': 847, 'release': 866, 'merged': 870}  wc -l: {'base': 842, 'head': 846, 'release': 865, 'merged': 869}
PASS file lines 870 == head + (release - base) = 847 + (866 - 843)
change-log entries: {'base': 344, 'head': 345, 'release': 366, 'merged': 367}
PR adds 1 entries, removes 0; release adds 23, removes 1
PASS merged change log 367 == head + (release - base) = 345 + (366 - 344)
release's change-log edits in place (base entry -> release entry):
   base:    - C-5.12 (2026-10-22, D-KILL3 / PR #223 integration): counted expiry and last_observed_alive_at share the distinct-table gate. A repeated shared table
   release: - C-5.12 (2026-10-22, D-KILL3 / PR #223 integration): counted expiry and last_observed_alive_at share the distinct-table gate. A repeated shared table
so release adds 22 entries and edits 1 in place
PASS merged change log == head + release - base as multisets (none lost, none twice)
entries appearing twice: merged {'- C-20.6, C-23.28 (2026-10-15, D-ST1, PR #198 round 8): define the test harness ': 2}  base {'- C-20.6, C-23.28 (2026-10-15, D-ST1, PR #198 round 8): define the test harness ': 2}
PASS no change-log entry appears twice in the merged file that did not already appear twice in the base
PASS PR's entry is first
PASS after the PR's entry, the merged change log is release's change log exactly, in order
PASS the whole merged change-log section, blank lines included, is release's with the PR's line added
base: 260 clauses with lettered ids, 257 by the script's pattern, duplicates []
PASS base has no duplicate clause
head: 263 clauses with lettered ids, 260 by the script's pattern, duplicates []
PASS head has no duplicate clause
release: 260 clauses with lettered ids, 257 by the script's pattern, duplicates []
PASS release has no duplicate clause
merged: 263 clauses with lettered ids, 260 by the script's pattern, duplicates []
PASS merged has no duplicate clause
PR adds ['C-3.8', 'C-6.21', 'C-6.22'] edits ['C-6.5', 'C-17.2'] removes []
release adds [] edits ['C-4.5', 'C-5.12', 'C-6.10', 'C-8.4', 'C-9.3', 'C-11.10', 'C-15.3', 'C-17.1', 'C-23.14', 'C-23.25', 'C-23.26', 'C-25.7', 'C-29.9'] ( 13 ) removes []
PASS PR adds exactly C-3.8, C-6.21, C-6.22
PASS PR edits exactly C-6.5, C-17.2
PASS release adds none
PASS release edits exactly the 13 named clauses
PASS no clause removed on either side
PASS no clause edited on both sides ([])
PASS merged clause count 263 == base 260 + PR's 3 new = 263
PASS every merged clause equals the side that changed it, else the base's (mismatches [])
PASS every base, head and release clause id is in the merged file
PASS C-17.1 is release's line byte for byte
PASS C-17.2 is the PR's line byte for byte
PASS merged clause order equals head's order with release unchanged order
PASS merged multiset of lines == base + both sides' additions - both sides' removals
RESULT: ALL PASS
```

### Semantic conflict outside the five files: one holder listing a pass

Release's b683f221d (D-HOLDERS-LSOF1, change 2) takes one holder listing per retention pass. A job archived in a pass finishes its holder check 2 in the next pass. Every release test module that expects one `maintenance` call to retire a job carries `pytestmark = pytest.mark.retention_catch_up`. With that marker, `tests/conftest.py`'s `catch_up` runs up to three follow-up passes, as the daemon's catch-up would.

The PR's new module `tests/unit/test_retention_review_subject.py` predates that change and has no marker. On the bare merge, all 10 of its existing tests fail with `assert [] == ['review']`: the job is left in `awaiting_check2`. Run `review-subject-file-merge` recorded 11 failed (those 10 and my new one) and 36 `test_prune_217` passes. A probe confirmed the cause: the job was not pinned (`_pin_reasons` returned `{}`), and the pass returned `in_flight: ['review-unread']` and `awaiting_check2: ['review-unread']`.

**Fix:** I added release's marker, with release's two-line comment, to that module (commit 54b2b0ae1). Its 11 tests then pass. No other file the PR changed calls retention without the marker: `test_retention_archive.py` already has release's.

### New tests (commit 54b2b0ae1)

Each was run on the merge and on a `git archive` extraction of 2954abd77 with the new test files copied in. Before trusting either run, I checked that `subfleet.__file__` resolved inside the tree under test.

| Test | At 2954abd77 | On the merge | Other trees |
| --- | --- | --- | --- |
| `test_store_upgrade_slots.py::test_real_historical_store_upgrades_to_12_and_reopens_unchanged`, new assertion that `events_notice_retired` exists after the upgrade | **Fails** for `[review-subject-old-v11]`: `assert None` at line 59. Passes for `[release-217-v10]` and `[wake-caller-307-v11]`, whose dumps already carry the index (see "Schema slots"). | 3 passed | — |
| `test_retention_review_subject.py::test_retiring_an_accepted_review_with_an_unread_notice_keeps_its_round_and_tells_the_session` | **Fails**: `assert [] == ['review-unread']`. At the PR head an unread notice pins its job for ever: a probe on that tree gave pin reason `unread-notice`. | passed | Fails on mutants of the merge: mutant A (release's block removed) gives no outcome notice; B (PR's block removed) gives `KeyError: 'review_subject'`; D (release's block moved after the `DELETE`s) gives no outcome notice, because the notices are gone. On 4aed7a3c9 the module can't import `subfleet.review_subject`. |
| `test_review_subject_cli.py::test_the_daemon_advertises_review_subjects_beside_release_task_capabilities` | **Fails**: `AttributeError: module 'subfleet.protocol' has no attribute 'TASKS_IF_NOTE_PREFIX_V2_CAPABILITY'` | passed | Fails on 4aed7a3c9 (`no attribute 'REVIEW_SUBJECT_CAPABILITY'`). Fails on mutant C, service.py taken from release (`assert 'review.subject.v1' in (...)`). |

The brief's one-line assertion for the third test, `REVIEW_SUBJECT_CAPABILITY in service.CAPABILITIES`, already passes at 2954abd77, because the PR added the capability there. So the test also asserts release's two capabilities: the merged tuple must hold all three. That second line is what fails at 2954abd77, and the first line fails on release's side and on mutant C.

### Test runs

Setup: `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache` and `uv sync --group dev` (exit 0; Python 3.15.2, pytest 9.1.1), with `PYTHONPATH` unset.

Each run went through one script, `$TMPDIR/sf-a2-merge1/run.sh`, which does five things:
- runs `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider` in the tree under test;
- gives each run a new `--basetemp` named `pytest-<label>-<epoch>`;
- uses at most `-n 4` workers and sets no per-test timeout;
- logs `uptime` at the start and end;
- prints where `subfleet` was imported from.

**Deselection.** A selection plugin (`-p sf_merge1_select`, loaded from the scratch folder) deselects some tests and logs each one with its reason. It wraps and patches nothing. It drops three kinds of test:
- tests whose fixtures include `process_inspection_available`, because they start a production daemon child whose containment census reads the host's process table;
- `real_lsof` and `real_cwd` tests;
- exact nodes and files from fix round 1's safety audit, plus the brief's named test.

| Run | What | Start (UTC, load) | End (UTC, load) | Result |
| --- | --- | --- | --- | --- |
| `new-on-merge` | 3 new tests, merge, before the marker | 17:46:06, 17.4 | 17:46:16, 17.6 | 4 passed, 1 failed (the retirement test; see "Semantic conflict") |
| `review-subject-file-merge` | `test_retention_review_subject.py` + `test_prune_217.py`, merge, no marker | 17:47:14, 14.7 | 17:49:13, 61.4 | 36 passed, 11 failed |
| `review-subject-file-merge2` | `test_retention_review_subject.py`, with marker | 17:51:03, 58.3 | 17:52:37, 23.0 | 11 passed |
| `new-at-2954abd77` | 3 new tests at the PR head | 17:54:02, 29.1 | 17:55:03, 25.3 | 2 passed, 3 failed (as expected) |
| `new-at-4aed7a3c9` / `new3-at-4aed7a3c9` | retirement / capability test on release | 17:58:54, 80.1 / 18:02:05, 32.9 | 17:59:30, 74.6 / 18:02:48, 50.8 | collection error (no `subfleet.review_subject`) / 1 failed |
| `new-at-mutA` … `mutD` | 2 tests on each mutant | 17:59:30, 74.6 | 18:01:39, 41.5 | each mutant: 1 failed, 1 passed (as expected) |
| `step1-collect` | step 1, collect only | 18:15:21, 51.5 | 18:15:42, 42.2 | 982 of 983 selected; 1 deselected |
| `step1-b` | step 1: 16 files | 18:16:40, 40.8 | 18:19:00, 18.4 | **827 passed** |
| `step1-a` | step 1: `test_prune_217`, `test_retention_archive`, `test_retention_review_subject` | 18:19:33, 31.3 | 18:27:45, 25.3 | **155 passed** |
| `step2-collect` | 220 step-2 files, collect only | 18:35:22, 15.4 | 18:36:57, 13.9 | 7,021 of 7,328 selected; 307 deselected |
| `step2-b00` | 21 fake files (439 tests) | 18:37:40, 49.4 | stopped about 18:57:40 (44.7 at 18:51) | 437 passed, 1 xfailed, 1 unfinished, 0 failed. The tool stopped it at its 20-minute limit. |
| `step2-pA` | the conflicts' other sides: 8 files | 19:03:33, 25.6 | 19:07:54, 16.8 | **209 passed** |
| `step3` | the four contract files | 19:08:32, 18.1 | 19:10:58, 96.9 | **449 passed** |
| `cwdpin-merge` | `test_detached_cwd_pins.py::test_current_cwd_fence_and_pin_are_atomic_at_admission`, the last test active on b00's gw1 | 19:12:06, 87.5 | 19:12:31, 80.9 | 2 passed in 17.6 s |
| `step2-u00` | 25 unit/frontend files (687 tests) | 19:13:09, 74.2 | stopped about 19:33:10 (106.7 at 19:30) | 521 passed, 1 failed, 8 errors, 157 unfinished. The tool stopped it at its 20-minute limit. |
| `frontend-replay-merge` | u00's failed and errored nodes: all 7 + 8 parameters | 19:34:09, 67.6 | 19:37:41, 25.6 | **15 passed** |
| `step2-key` | 10 files nearest the resolved code (including u00's unstarted `test_conversation_state_reads.py`) | 19:42:18, 45.8 | 19:45:48, 27.0 | **639 passed** |
| `step2-u01` | 23 files | 19:46:00, 24.4 | 19:47:50, 17.4 | **643 passed** |
| `step2-u02` | 9 files | 19:48:06, 18.7 | 19:49:14, 25.4 | **698 passed** |
| `step2-u0304` | 22 files | 19:49:37, 23.7 | 19:58:42, 29.0 | **913 passed, 26 xfailed** |

**Step 1.** All 19 PR files ran: 982 passed and 1 was deselected, `test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (`real_lsof`).

**Step 2.**
- **The set.**
  - The hub's `git grep -l -P` pattern returns 229 files on the merge, the same count as the trial. An AST import walk over every `tests/*test_*.py` found no multi-line import the pattern misses. Three of the 229 match only in strings (`test_conversation_fifo_reads`, `test_daemon_fifo_reads`, `test_live_store_guard_st3_children`); I kept them.
  - All five task-hold and sandbox files are in the set. `test_notice_keep.py`, `fake/test_notice_keep_delivery.py` and `test_pin_notice_identity.py` are not: they import `retention_notices`, not `retention_archive`. I added them, which makes 232.
  - 12 of the 232 had already run in step 1, which leaves 220.
- **What ran on the merge.**
  - 72 files completely: 3,128 passed, 26 xfailed, 0 failed.
  - batch b00: 437 passed and 1 xfailed of 439.
  - u00: 521 passed of 687. Its 1 failure and 8 errors were all in `tests/frontend/`, a timing-sensitive steer test and Swift-probe fixtures at load 74 to 107. All 15 parameters of those two tests passed on replay. Neither side changed either file.
  - No failure remains.
- **What didn't finish or run.**
  - One test in b00, which the `-q` output doesn't name. The test active last on gw1 passes alone.
  - 139 tests in u00. Of its 157 unfinished tests, the 18 in `test_conversation_state_reads.py` ran later in `step2-key`. By name the 139 belong to `test_conversation_service.py` (most), `test_conversation_handoff*.py`, `test_cli_batch.py`, `test_conversation_fifo_reads.py`, `test_conversation_peers_attachments.py`, `test_codex_turn.py`, `test_client_availability.py`, `test_conversation_catalog_lifecycle.py` and `test_app_cutover_daemon.py`.
  - 90 files never started: 43 unit files (2,785 − 1,116 = 1,669 tests) and 47 fake files (1,116 tests).
  - 13 files had nothing selected after deselection.

**Step 3.** The four contract files: **449 passed** (`test_state_contract`, `test_test_module_names`, `test_invariants_index`, `test_handler_refusals`).

**Step 4** (importers of `cli`, `contracts`, `daemon`, `refusals` and `store` beyond step 2's set) didn't start: the time ran out.

### Not run, and why

Deselected from step 2's selection (307 tests), each logged with its reason:
- **271 by file.**
  - The `worlds()` setup of 8 `test_host_push*` files runs `git push origin <head>:refs/heads/trunk`, a write-shaped command, before any test-specific refusal. Fix round 1 found 16 safe nodes in the mixed files; I didn't select them.
  - `e2e/test_conversations.py` (44) runs `peers.process_chain` (`ps -axo`, `ps -Ewwp`) over real ancestry.
  - `process/test_kill4_fix1_process.py` (3) runs the real containment census over the host table.
- **30 by fixture** (`process_inspection_available`, a production daemon child): `fake/test_daemon_stacks.py` 5, `fake/test_descriptor_exhaustion.py` 2, `fake/test_gate_end_to_end.py` 1, `fake/test_lost_acknowledgement.py` 3, `fake/test_sessions_end_to_end.py` 6, `unit/test_daemon_verbs.py` 5, `unit/test_doctor.py` 2 and `unit/test_legacy_hold.py` 6.
- **3 by node.**
  - `fake/test_review_launch_permissions.py::test_the_installed_publish_hook_is_run_before_launch`, named in the brief.
  - `unit/test_hooks.py::test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs`, which runs `/bin/ps -axww -o command=`.
  - `unit/test_live_store_guard_round6.py::test_probes_detect_a_launch_that_is_not_refused`, which launches a stand-in executable named `security`.
- **3 by marker:** `real_lsof` or `real_cwd` in `test_retention_cwd_rechecks.py`.

The exclusion list also names the `test_gate_merge.py` and `test_gate_service.py` (`gh pr merge` through FakeGh runners), `test_idle_cost` and `process/` census files. None of them is in this round's selection.

The other `test_live_store_guard*` tests still ran. Following fix round 1's audit, they hand `security` argv to a guard or Python callback that refuses it, or to a stand-in on a synthetic `PATH`. Only the node above launches.

### Folders and cleanup

- **Scratch** is `$TMPDIR/sf-a2-merge1`, and it predated this attempt: a1 made it, empty. I deleted its `basetemp/` (1.1 GB), `trees/` (the two extracted parents and four mutants) and `probe/` with a guarded `shutil.rmtree`. The guard asserts `sf-a2-merge1` is in the path, and its error handler restores write permission on test-made read-only folders. I kept the run logs, the deselection logs, the checker scripts, the selection plugin and the batch lists (about 2 MB) for the hub.
- **Outside scratch:** I created no folder myself. The suite's `conftest.py` made seven `sf-test-homes-*` folders under `$TMPDIR` during these runs, and they were still there at the end, probably from the two runs the tool stopped:
  - `sf-test-homes-2bab_8zh`
  - `sf-test-homes-4fli8el0`
  - `sf-test-homes-683atcv6`
  - `sf-test-homes-85yrjuo4`
  - `sf-test-homes-b1yus6_5`
  - `sf-test-homes-begh_jj3`
  - `sf-test-homes-ibil81la`

  Their paths don't contain `sf-a2-merge1`, so the guarded delete the brief requires refuses them. I left them, as rounds 1 and 2 left theirs. No `/tmp/sf-*` or `/tmp/sf-failed` folder from these runs remains.
- `$TMPDIR/uv-cache` predates this job, so I kept it. The worktree's ignored `.venv` stays.

### Deviations from the brief

1. **Resumed, not started.** Attempt a1 had started the merge and resolved the five files in the working tree without staging or committing. I verified and kept its resolutions, and reran the contract script, whose output was identical.
2. **The release ref moved:** it is now at 35f8615af locally and 024b23f80 on the remote. I merged 4aed7a3c9, as instructed.
3. **One extra file changed for the merge.** `tests/unit/test_retention_review_subject.py` gained release's `retention_catch_up` marker, which release's b683f221d requires (see "Semantic conflict"). Without it, the PR's 10 existing retirement tests fail on the merge.
4. **Three new tests, not two.** The brief's list has three, and I added all three. The capability test also asserts release's two capabilities. On its own, the one-line assertion the brief gives passes at 2954abd77.
5. **The first new test fails at 2954abd77 for one parameter, not three.** Two of the three fixture dumps already carry `events_notice_retired`. The brief's description of `release-217-v10.sql` as the base's store is wrong: its header says a4aded885.
6. **Step order.** I ran step 2's eight conflict-pair files, then step 3, then the rest of step 2, so that step 3 couldn't be cut off. Step 2 stayed incomplete and step 4 didn't start.
7. **Two runs went past the 600 s foreground limit:** b00 and u00. The tool moved them to the background and stopped each at its 20-minute limit. I polled their logs and started no other test run while either was active. Later batches were sized to finish in the foreground.
8. **One run collected nothing.** The first `step1-b` invocation passed its 16 file names as one argument (zsh doesn't split words) and collected no tests (exit 5). It was rerun at once under the same label, which overwrote its log.
9. **Fake daemon tests weren't excluded wholesale.** Only tests with the `process_inspection_available` fixture were dropped. The PR's three fake files use `state_daemon`, which patches every `procs` reader with a synthetic table.
10. **Network.** All my network use was read-only: `gh pr view 314 --repo MaxGhenis/subfleet-v2` for the PR body, and one `git fetch --dry-run`, which read where the remote release ref sits and updated no ref.

Code and tests built: `54b2b0ae1dcf7c9360ba6155f53424c49f5e57ed` (merge 9292b0e64, then the tests commit). This section is committed on top of it; the job's final answer gives the resulting head.

## Release merge 2

Started at **2026-11-25 21:28:40 UTC**. The cutoff for new test runs was 23:58:40 UTC and the job deadline was 00:28:40 UTC on November 26. The checkout opened at `b84046c4b19cab317f14138ec0a979fb73a09abc` with a clean worktree. The local `origin/release/217` ref was at the requested `53a7bac93d01afd9165588bcaf789dae75540757`. The merge base was `4aed7a3c977d7813262cac21016fb65d37576ba2`. Release added 26 commits across 23 paths through #177, #333 and #334. The PR side added nine commits. I read the PR body through `gh pr view 314 --repo MaxGhenis/subfleet-v2` and used the local objects for the merge.

### Merge and contract

The sandbox denied `ORIG_HEAD.lock` on `git merge --no-ff --no-commit 53a7bac93d01afd9165588bcaf789dae75540757` and `index.lock` on `git add -- docs/acceptance-contract.md`. I built the fallback tree in the assigned worktree. I copied the 21 release-only paths from the release object and verified their bytes and executable modes. I merged `subfleet/daemon.py` with `git merge-file -p` over the three committed inputs. Its exit was 0. The tested tree is the merge of **`b84046c4b19cab317f14138ec0a979fb73a09abc`** and **`53a7bac93d01afd9165588bcaf789dae75540757`** for the hub to record. The actual `HEAD` is still `b84046c4b`, whose parent is `77585b5f2b09eafa6bf0a82db732ed8727da0f12`. The worktree checks `git diff --check 53a7bac93` and `git diff --check b84046c4b` both passed. The legacy-smoke logs have no changes between these two tips.

I read the hub script's docstring and ran the script from the worktree root against the fallback tree. It exited 0.

```text
$ python3 /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py HEAD 53a7bac93d01afd9165588bcaf789dae75540757
merged against 4aed7a3c9: 260 clauses, none duplicated
```

The PR added C-3.8, C-6.21 and C-6.22 and edited C-6.5 and C-17.2. Release changed C-5.5 and C-25.6 in #177 and one continuation line of C-8.7 in #334. The C-8.7 continuation expands the condition after “no hourly pass is due” and keeps the existing sentence on unexpected exceptions. The change log starts with D-REVIEW-SUBJECT1, then #334's C-8.7 entry dated 2026-11-18 and #177's C-5.5/C-25.6 entry dated 2026-11-14. The older entries are byte-identical, including the duplicated C-20.6/C-23.28 (2026-10-15, D-ST1, PR #198 round 8) line that predates the merge. Each changed clause comes from its authoring side, so the merge required no new clause wording or change-log entry.

The Python check of the committed inputs and fallback tree produced the following results.

```text
split lines (base/head/release/merged): 866 870 868 872
wc -l (base/head/release/merged): 865 869 867 871
merged bytes: 1049866; sha256: 3116a82c937b5ab2a1ff52594dfa3b72bc7c9c53335a352f944d0b2220924c51
change-log entries (base/head/release/merged): 366 367 368 369
change log order and preservation: PASS; pre-existing C-20.6, C-23.28 line occurs twice in all four versions
script clause pattern (base/head/release/merged): 257 260 257 260
including lettered (base/head/release/merged): 260 263 260 263
including titled (base/head/release/merged): 262 265 262 265
duplicate clause IDs: none
PR clause blocks differing: ['C-17.2', 'C-3.8', 'C-6.21', 'C-6.22', 'C-6.5']
PR clauses: C-17.2, C-3.8, C-6.21, C-6.22, C-6.5
release clauses: C-25.6, C-5.5, C-8.7 (C-8.7: one continuation line)
all merged clause blocks match the changing side or base: PASS
contract line diffs: release→merged = base→head (5 edit groups); head→merged = base→release (4 edit groups): PASS
daemon.py line diffs: release→merged = base→head (8 edit groups); head→merged = base→release (11 edit groups): PASS
store schema 12; wake 11, review 12; keys 4–12; slot 11 equals PR head and both #307 tuples; events_notice_retired: PASS
```

I read the submission and retention hunks in the result. The submission region has caller-family and review validation, round assignment inside the insert transaction, refusal cleanup, inheritance on resume, and the Codex writer check. The retention region has the `DEFERRED` comment, `_retention_delete_owed` class default, the owed turn before the next due pass, and the five hourly paths that clear the flag. Both sides' complete line diffs survive.

### Schema slots and semantic checks

`SCHEMA_VERSION = REVIEW_SUBJECT_MIGRATION`, with `NOTICE_WAKE_MIGRATION = 11` and `REVIEW_SUBJECT_MIGRATION = 12`. An AST read compared the slot 11 tuple with #307 at `963d1afb9` and `02ae7d1ed`. The schema header says version 12 and the schema creates `events_notice_retired`. The store code, IDs and fixtures match the PR head. No slot or fixture changed in this merge.

- **#334 and review retirement.** The PR tests drive `retention.maintenance` directly and keep the round 1 `retention_catch_up` marker. `World.push()` seeds disposable repositories with local fetches. No PR test creates an operator request or journal or exercises the daemon’s owed-delete scheduling. Normal retirement still stamps accepted-review history on its event, and the operator delete path preserves the job row. The release daemon module covers owed turns, `DEFERRED`, retry counts and the real `DeletePath` on temporary schema-12 stores.
- **#177 and the protocol boundary.** `peers.judge` computes the root marker with `ps_text`, including a trailing space, and rejects unholdable roots before it asks for the process chain. The service translates this `ConversationError` to exit 7. The merge 1 capability tuple is advertised by the separate capability operation. `test_person_boundary.py` uses its own temporary service root and synthetic ancestry, independent of the unit `root` fixture that clears `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `SUBFLEET_SESSION_ID` and `SUBFLEET_CALLER_FAMILY`.

The four release modules passed together on the merged tree, including `test_retention_delete_daemon.py`. The PR suite confirmed the merged capabilities and historical-store upgrades. All three I5 task cases (`None`, `review`, `research`) passed their exact submission comparison with the frozen release/217 code. The test compares replies, canonical job rows and ordered events and checks the new fields are null. The two intersections required no integration fix.

### Test setup and deviations

- The sandbox denied writes to the Darwin temp root and shared uv cache. I used the writable temporary directory allocated to this job as `TMPDIR`, copied the specified round 1 plugin and exclusions from their originals, and ran `uv sync --group dev` against a private copy of the 50 MiB shared cache. The new worktree `.venv` used Python 3.15.2 and pytest 9.1.1. The shared cache was left in place. The only Git fetches were the tests' local fixture operations.
- The first launcher attempt failed on Bash's `/dev/fd` process substitution before pytest started. I changed log redirection and ran the contract batch with the same arguments. I cleared the four session variables for every batch so they could only be set by test fixtures. From `pr-submit`, I put Hypothesis storage under scratch as well.
- Git's lock errors forced the documented file-based fallback, including the contract script invocation without a stopped Git merge. `git add -- docs/reports/2026-11-13-review-subject.md` also hit the `index.lock` denial. The requested commits require the hub's writable Git metadata.

### Test runs

Every pytest run verified that `subfleet.__file__` resolved to `subfleet/__init__.py` in the assigned worktree before it started. The commands below use the round 1 plugin and no pytest cache or per-test timeout override. Deselection counts come from the plugin’s JSONL files. The table includes the UTC interval, the original local-time `uptime` output at each boundary, and the `df -H /` row captured before each run.

| Batch | UTC start → end | Start → end uptime | df -H / | Passed | Failed | Skipped | Deselected | Other | Basetemp |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| `contract` | 21:34:42Z → 21:37:27Z | 22:34  1 user, load averages: 62.62 70.71 61.33<br>22:37  1 user, load averages: 49.01 60.12 58.53 | `/dev/disk3s1s1 3996G 14G 106G 12% 487k 1.0G 0% /` | 449 | 0 | 0 | 0 | — | 21M |
| `release-interface` | 21:37:44Z → 21:40:04Z | 22:37  1 user, load averages: 44.29 58.52 57.98<br>22:40  1 user, load averages: 56.09 58.12 57.86 | `/dev/disk3s1s1 3996G 14G 106G 12% 487k 1.0G 0% /` | 276 | 0 | 0 | 0 | — | 54M |
| `pr-submit` | 21:40:52Z → 21:42:21Z | 22:40  1 user, load averages: 47.03 55.27 56.82<br>22:42  1 user, load averages: 48.95 53.10 55.79 | `/dev/disk3s1s1 3996G 14G 106G 12% 487k 1.0G 0% /` | 61 | 0 | 0 | 0 | — | 30M |
| `pr-cli-store` | 21:42:35Z → 21:43:24Z | 22:42  1 user, load averages: 47.46 52.56 55.55<br>22:43  1 user, load averages: 37.90 49.13 54.11 | `/dev/disk3s1s1 3996G 14G 106G 12% 487k 1.0G 0% /` | 380 | 0 | 0 | 0 | — | 12M |
| `pr-prune` | 21:43:27Z → 21:44:03Z | 22:43  1 user, load averages: 36.95 48.75 53.94<br>22:44  1 user, load averages: 30.70 45.87 52.68 | `/dev/disk3s1s1 3996G 14G 107G 12% 487k 1.1G 0% /` | 47 | 0 | 0 | 0 | — | 16M |
| `pr-archive` | 21:44:07Z → 21:47:52Z | 22:44  1 user, load averages: 33.85 46.27 52.78<br>22:47  1 user, load averages: 34.34 40.04 48.54 | `/dev/disk3s1s1 3996G 14G 108G 12% 487k 1.1G 0% /` | 108 | 0 | 0 | 1 | — | 77M |
| `daemon-retention` | 21:48:48Z → 21:49:46Z | 22:48  1 user, load averages: 39.33 40.89 48.35<br>22:49  1 user, load averages: 35.96 39.43 47.32 | `/dev/disk3s1s1 3996G 14G 107G 12% 487k 1.0G 0% /` | 74 | 0 | 0 | 0 | — | 40M |
| `daemon-attempt-temp` | 21:49:54Z → 21:50:34Z | 22:49  1 user, load averages: 36.47 39.42 47.22<br>22:50  1 user, load averages: 43.38 40.83 47.38 | `/dev/disk3s1s1 3996G 14G 107G 12% 487k 1.0G 0% /` | 23 | 0 | 0 | 21 | — | 11M |
| `daemon-unrun` | 21:50:47Z → 22:03:54Z | 22:50  1 user, load averages: 43.94 41.10 47.36<br>23:03  1 user, load averages: 80.57 89.44 75.33 | `/dev/disk3s1s1 3996G 14G 107G 12% 487k 1.1G 0% /` | 98 | 0 | 0 | 0 | — | 72M |
| `daemon-census` | 22:04:26Z → 22:06:29Z | 23:04  1 user, load averages: 83.00 89.34 75.79<br>23:06  1 user, load averages: 84.46 87.74 76.99 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 32 | 0 | 0 | 0 | — | 32M |
| `daemon-notices` | 22:15:02Z → 22:15:28Z | 23:15  1 user, load averages: 75.91 84.46 76.97<br>23:15  1 user, load averages: 63.79 80.86 75.89 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 83 | 0 | 0 | 0 | — | 26M |
| `daemon-cwd` | 22:15:29Z → 22:19:26Z | 23:15  1 user, load averages: 63.79 80.86 75.89<br>23:19  1 user, load averages: 45.00 66.72 71.19 | `/dev/disk3s1s1 3996G 14G 106G 12% 487k 1.0G 0% /` | 18 | 0 | 0 | 0 | — | 16M |
| `daemon-detached-fence` | 22:19:31Z → 22:21:05Z | 23:19  1 user, load averages: 43.88 66.13 70.95<br>23:21  1 user, load averages: 40.07 58.73 67.57 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 89 | 0 | 0 | 0 | — | 83M |
| `daemon-writer-fence` | 22:21:14Z → 22:21:35Z | 23:21  1 user, load averages: 38.16 57.71 67.10<br>23:21  1 user, load averages: 36.50 56.10 66.31 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 23 | 0 | 0 | 0 | — | 20M |
| `daemon-case` | 22:21:37Z → 22:24:24Z | 23:21  1 user, load averages: 36.38 55.75 66.12<br>23:24  1 user, load averages: 43.99 49.87 61.76 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 94 | 0 | 0 | 0 | — | 38M |
| `daemon-shared` | 22:24:36Z → 22:37:22Z | 23:24  1 user, load averages: 40.15 48.87 61.27<br>23:37  1 user, load averages: 35.61 46.54 54.28 | `/dev/disk3s1s1 3996G 14G 105G 12% 487k 1.0G 0% /` | 62 | 0 | 0 | 0 | — | 46M |
| `daemon-salvage` | 22:37:28Z → 22:38:49Z | 23:37  1 user, load averages: 33.57 45.74 53.91<br>23:38  1 user, load averages: 31.58 42.55 51.97 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 86 | 0 | 0 | 0 | — | 14M |
| `daemon-tick` | 22:38:54Z → 22:39:05Z | 23:38  1 user, load averages: 31.21 42.29 51.83<br>23:39  1 user, load averages: 30.14 41.71 51.51 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 30 | 0 | 0 | 0 | — | 15M |
| `daemon-busy` | 22:39:07Z → 22:39:34Z | 23:39  1 user, load averages: 30.93 41.68 51.44<br>23:39  1 user, load averages: 29.44 40.49 50.73 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 115 | 0 | 0 | 0 | — | 27M |
| `optional-rediscovery` | 22:39:48Z → 22:39:59Z | 23:39  1 user, load averages: 31.13 40.32 50.49<br>23:39  1 user, load averages: 31.45 40.08 50.28 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 17 | 0 | 0 | 0 | — | 12M |
| `optional-internals` | 22:40:03Z → 22:40:13Z | 23:40  1 user, load averages: 32.14 40.08 50.22<br>23:40  1 user, load averages: 32.80 39.96 50.06 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 64 | 0 | 0 | 0 | — | 1.8M |
| `optional-census` | 22:40:17Z → 22:40:26Z | 23:40  1 user, load averages: 32.80 39.96 50.06<br>23:40  1 user, load averages: 32.69 39.70 49.85 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 17 | 0 | 0 | 0 | — | 928K |
| `optional-adjacent` | 22:40:28Z → 22:40:44Z | 23:40  1 user, load averages: 33.04 39.65 49.78<br>23:40  1 user, load averages: 34.42 39.64 49.59 | `/dev/disk3s1s1 3996G 14G 103G 12% 487k 1.0G 0% /` | 339 | 0 | 0 | 0 | — | 928K |

Total across 23 runs was **2,585 passed, 0 failed, 0 skipped and 22 deselected**.

### Test selection

- `pr-archive` deselected 1 by marker: real_lsof/real_cwd reads host processes.
  - `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`
- `daemon-attempt-temp` deselected 21 by node: host processes: detached admission reaches an unstubbed procs.snapshot through the staged Claude registry.

The 16 added attempt-TMPDIR exclusions cover the cases below. Their detached admission reaches the staged Claude registry and calls an unstubbed `procs.snapshot()` over the host process table.

```text
tests/fake/test_attempt_tmpdir_daemon.py::test_allocation_failure_ends_the_attempt_not_left_reserved
tests/fake/test_attempt_tmpdir_daemon.py::test_allocation_failure_is_a_launch_failure_with_its_reason[EACCES]
tests/fake/test_attempt_tmpdir_daemon.py::test_allocation_failure_is_a_launch_failure_with_its_reason[ENOSPC]
tests/fake/test_attempt_tmpdir_daemon.py::test_cleanup_preserves_evidence_committed_during_its_census[False]
tests/fake/test_attempt_tmpdir_daemon.py::test_cleanup_preserves_evidence_committed_during_its_census[True]
tests/fake/test_attempt_tmpdir_daemon.py::test_daemon_start_sweeps_a_clean_terminal_leftover_once_serving
tests/fake/test_attempt_tmpdir_daemon.py::test_daemon_sweep_waits_for_terminal_attempt_census_to_clear
tests/fake/test_attempt_tmpdir_daemon.py::test_launch_tries_of_one_attempt_share_one_allocation
tests/fake/test_attempt_tmpdir_daemon.py::test_quarantine_released_by_confirm_dead_then_swept
tests/fake/test_attempt_tmpdir_daemon.py::test_salvage_skipped_jobs_both_retire_whatever_their_scratch
tests/fake/test_attempt_tmpdir_daemon.py::test_sweep_reclaims_known_allocation_after_provider_removes_marker[False]
tests/fake/test_attempt_tmpdir_daemon.py::test_sweep_reclaims_known_allocation_after_provider_removes_marker[True]
tests/fake/test_attempt_tmpdir_daemon.py::test_sweeps_over_held_scratch_take_no_repeated_census_and_write_nothing
tests/fake/test_attempt_tmpdir_daemon.py::test_tmpdir_cleanup_and_sweep_never_raise_into_their_callers
tests/fake/test_attempt_tmpdir_daemon.py::test_unknown_folder_is_aged_by_the_store_and_a_named_one_never_is
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_1_a_tombstone_a_crash_left_is_finished_after_restart[False]
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_1_a_tombstone_a_crash_left_is_finished_after_restart[True]
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_2_an_unreadable_top_folder_is_removed_by_the_sweep[host-rename]
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_2_an_unreadable_top_folder_is_removed_by_the_sweep[macos15-rename]
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_2_retiring_a_job_whose_scratch_will_not_go_names_the_folder
tests/fake/test_attempt_tmpdir_leaks_daemon.py::test_p3_3_a_launch_whose_marker_publish_fails_leaves_no_folder
```

I also added the three process-suite entries below. The census file calls the real containment census. Both rendering nodes call the host-wide `peers.process_chain` before slicing the results. These paths were outside the selected batches.

- `tests/process/test_census_root_bytes_process.py`
- `tests/process/test_ps_rendering.py::test_the_person_check_finds_a_root_marker_with_a_variable_after_it`
- `tests/process/test_ps_rendering.py::test_the_person_check_finds_a_root_marker_that_ends_the_line`

The optional daemon settle module stayed outside the selected batches. Its four excluded start-grace nodes (11 cases) call `ps -p 4242` through an unstubbed liveness check for a PID the tests did not start.

- `tests/unit/test_daemon_settle.py::test_c4_2_start_grace_reads_the_receipts_again_after_its_census`
- `tests/unit/test_daemon_settle.py::test_c4_2_start_grace_without_a_receipt_still_decides_from_its_census`
- `tests/unit/test_daemon_settle.py::test_c4_2_start_grace_with_start_json_alone_is_a_running_attempt`
- `tests/unit/test_daemon_settle.py::test_c4_2_start_grace_takes_only_a_receipt_the_tick_would_take`

### Footprint and cleanup

The job scratch was `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261125-222751-rva2-merge-r2-a1-lgvli1rc/sf-a2-merge2`. Its remaining directories were `__pycache__/`, `hypothesis/`, `logs/`, `plugin/`, `uv-cache/`. The basetemps are listed below.
Churn estimate using the recorded folder sizes was **751,472 KiB** (770 MB), including 684,180 KiB of basetemps, 54,568 KiB of remaining scratch, and 12,724 KiB for the ignored worktree `.venv`. The largest measured scratch footprint at a run boundary was 139,756 KiB (143 MB).
The before/after comparison found zero new `sf-test-homes-*` folders in this job’s `TMPDIR`. Ten new paths appeared in the shared Darwin temp directory during the run. I could not attribute them to this job and left them outside the cleanup guard.

- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-8m6o2luq`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-c1cpkx4v`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-ehaaslwp`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-jnflw_yf`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-orcu_mnd`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-t2y4m3gx`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-v8962oyz`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-y8rjs1fa`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-yu1nyjmc`
- `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-test-homes-yxds1gol`

Each of these basetemps was removed with guarded `shutil.rmtree`. Paths are relative to `$scratch`.

```text
pytest-contract-1795642477
pytest-release-interface-1795642663
pytest-pr-submit-1795642851
pytest-pr-cli-store-1795642954
pytest-pr-prune-1795643006
pytest-pr-archive-1795643046
pytest-daemon-retention-1795643327
pytest-daemon-attempt-temp-1795643392
pytest-daemon-unrun-1795643444
pytest-daemon-census-1795644264
pytest-daemon-notices-1795644889
pytest-daemon-cwd-1795644929
pytest-daemon-detached-fence-1795645170
pytest-daemon-writer-fence-1795645273
pytest-daemon-case-1795645297
pytest-daemon-shared-1795645475
pytest-daemon-salvage-1795646248
pytest-daemon-tick-1795646334
pytest-daemon-busy-1795646347
pytest-optional-rediscovery-1795646387
pytest-optional-internals-1795646403
pytest-optional-census-1795646416
pytest-optional-adjacent-1795646428
```

I removed the scratch root and its remaining children with the same guarded delete after appending this report. The worktree `.venv` stays.

### Final head and test commands

Final `git rev-parse HEAD` was `b84046c4b19cab317f14138ec0a979fb73a09abc`. The fallback tree and report are in the worktree for hub collection.

The function below expresses the exact per-run pytest commands, with the shared prefix and environment written once. `$scratch` is the path recorded under cleanup.

```bash
pytest_run() {
  local label="$1" workers="$2" base="$3"
  shift 3
  UV_CACHE_DIR="$scratch/uv-cache" PYTHONPATH="$scratch/plugin" \
  SF_MERGE1_EXCLUDE="$scratch/exclude.tsv" SF_MERGE1_LOG="$scratch/logs/$label.deselected.jsonl" \
  uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider \
    -p sf_merge1_select -n "$workers" -q --basetemp "$scratch/$base" "$@"
}

# 449 passed, 0 failed
pytest_run contract 4 pytest-contract-1795642477 \
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py
# 276 passed, 0 failed
pytest_run release-interface 4 pytest-release-interface-1795642663 \
  tests/unit/test_retention_delete_daemon.py tests/unit/test_person_boundary.py \
  tests/unit/test_census_procargs.py tests/unit/test_conversation_peers_attachments.py
# 61 passed, 0 failed
pytest_run pr-submit 4 pytest-pr-submit-1795642851 \
  -vv tests/fake/test_review_subject_resume.py \
  tests/fake/test_review_subject_submission.py tests/fake/test_review_subject_validation.py \
  tests/unit/test_caller_family_ancestry.py tests/unit/test_codex_writable_fanout.py \
  tests/unit/test_reserve_authorization_plumbing.py
# 380 passed, 0 failed
pytest_run pr-cli-store 4 pytest-pr-cli-store-1795642954 \
  tests/unit/test_cli.py tests/unit/test_review_runs_columns.py \
  tests/unit/test_review_subject_cli.py tests/unit/test_review_subject_normalization.py \
  tests/unit/test_review_subject_resume_cli.py tests/unit/test_store_migration_2.py \
  tests/unit/test_store_qos_migration.py tests/unit/test_store_review_subject_migration.py \
  tests/unit/test_store_upgrade_slots.py
# 47 passed, 0 failed
pytest_run pr-prune 4 pytest-pr-prune-1795643006 \
  tests/unit/test_prune_217.py tests/unit/test_retention_review_subject.py
# 108 passed, 0 failed
pytest_run pr-archive 3 pytest-pr-archive-1795643046 \
  tests/unit/test_retention_archive.py
# 74 passed, 0 failed
pytest_run daemon-retention 4 pytest-daemon-retention-1795643327 \
  tests/fake/test_native_maintenance_startup.py tests/fake/test_review130_ab.py \
  tests/fake/test_retention_dormant.py tests/fake/test_admission_ramp_retention.py
# 23 passed, 0 failed
pytest_run daemon-attempt-temp 4 pytest-daemon-attempt-temp-1795643392 \
  tests/fake/test_attempt_tmpdir_daemon.py tests/fake/test_attempt_tmpdir_leaks_daemon.py \
  tests/unit/test_retention_listings.py
# 98 passed, 0 failed
pytest_run daemon-unrun 2 pytest-daemon-unrun-1795643444 \
  tests/fake/test_unrun_worktrees.py
# 32 passed, 0 failed
pytest_run daemon-census 4 pytest-daemon-census-1795644264 \
  tests/fake/test_census_round3_interleavings.py tests/fake/test_census_round3_leases.py \
  tests/fake/test_resume_contract.py
# 83 passed, 0 failed
pytest_run daemon-notices 4 pytest-daemon-notices-1795644889 \
  tests/fake/test_notice_keep_delivery.py tests/unit/test_notice_keep.py \
  tests/unit/test_sandbox_hold_reapers.py
# 18 passed, 0 failed
pytest_run daemon-cwd 3 pytest-daemon-cwd-1795644929 \
  tests/fake/test_detached_cwd_pins.py
# 89 passed, 0 failed
pytest_run daemon-detached-fence 3 pytest-daemon-detached-fence-1795645170 \
  tests/fake/test_detached_retention_fence.py
# 23 passed, 0 failed
pytest_run daemon-writer-fence 3 pytest-daemon-writer-fence-1795645273 \
  tests/fake/test_writer_retention_fence.py
# 94 passed, 0 failed
pytest_run daemon-case 4 pytest-daemon-case-1795645297 \
  tests/unit/test_retention_case_alias.py tests/unit/test_retention_folded_pin.py
# 62 passed, 0 failed
pytest_run daemon-shared 3 pytest-daemon-shared-1795645475 \
  tests/unit/test_retention_shared_folders.py
# 86 passed, 0 failed
pytest_run daemon-salvage 3 pytest-daemon-salvage-1795646248 \
  tests/unit/test_salvage_unindexable.py
# 30 passed, 0 failed
pytest_run daemon-tick 4 pytest-daemon-tick-1795646334 \
  tests/unit/test_tick_prefilter.py
# 115 passed, 0 failed
pytest_run daemon-busy 4 pytest-daemon-busy-1795646347 \
  tests/unit/test_timers_busy_read.py
# 17 passed, 0 failed
pytest_run optional-rediscovery 4 pytest-optional-rediscovery-1795646387 \
  tests/unit/test_retention_rediscovery.py
# 64 passed, 0 failed
pytest_run optional-internals 4 pytest-optional-internals-1795646403 \
  tests/unit/test_batch_submission_queries.py tests/unit/test_fake_daemon_borrowed_methods.py \
  tests/unit/test_store_snapshot_plans.py tests/unit/test_timers_daemon.py
# 17 passed, 0 failed
pytest_run optional-census 4 pytest-optional-census-1795646416 \
  tests/unit/test_census_counted.py tests/unit/test_census_counted_expiry.py \
  tests/unit/test_census_worktree_recheck_pacing.py
# 339 passed, 0 failed
pytest_run optional-adjacent 3 pytest-optional-adjacent-1795646428 \
  tests/unit/test_host_push_sandbox_hold.py tests/unit/test_daemon_init_release.py \
  tests/unit/test_forget_paced.py tests/unit/test_protocol_secrecy.py
```

**Left for the hub and CI.** Record the fallback tree with the two intended parents and commit this report. The required batches finished with the process-dependent cases listed above deselected. The requested `git grep` found 257 daemon importers. Steps 1–4 covered 31 of those paths, and step 5 covered 12 of the remaining 226, leaving 214 for CI. The #177 process suites and #333's `test_guard_overlay.py`, `test_guard_pin_0169.py`, `test_guard_subfleet_root.py` and `test_publish_rule_round7.py` did not run here.

## Release merge 3

Started at **2026-11-26 11:22:24 UTC**. The new-test cutoff was 13:52:24 UTC and the job deadline was 14:22:24 UTC. The initial checks found `d5e6cb4aead4cdb7fbde5425643cf48a13682547`, a clean tracked worktree, and `f2fa1a6d67d39adf07d3579a265c579467b6bbca` of type `commit`. I read the release merge 2 report, the round 1 selection plugin and exclusions, the PR body through `gh pr view 314 --repo MaxGhenis/subfleet-v2`, and C-3.9 at the PR head.

### Merge

The intended parents are **`d5e6cb4aead4cdb7fbde5425643cf48a13682547`** and **`f2fa1a6d67d39adf07d3579a265c579467b6bbca`**, with merge base `53a7bac93d01afd9165588bcaf789dae75540757`. The chain has 172 commits across 663 paths since the base. The PR side has 12 commits. The local `origin/release/217` was at `a4d21501e`, and the six local and remote-tracking PR tips matched the chain links. I used that object without fetching. It brings in release/217 through #310 and #295, then #313 (`b883a35f3`), #315 (`d6aa5b652`), #321 (`749b70a71`), #307 (`94442d693`), #329 (`4a9253f18`) and #316 (`f2fa1a6d6`). This places #314 seventh. The review-subject clause is now **C-3.9**. #315 owns C-3.8. Earlier report sections and evidence retain their historical references.

`git merge --no-ff --no-commit f2fa1a6d67d39adf07d3579a265c579467b6bbca` failed on a sandbox denial of `ORIG_HEAD.lock`. I copied the chain versions of the 649 chain-only paths into the assigned worktree and used `git merge-file -p` on the three committed versions of each of the 14 shared files. Git denied `index.lock` on each named resolution. The tests ran against the resulting worktree files. The actual `HEAD` still has parent `87d09950a9175562cce9c6684c8962facabd6bc5`. The intended parents above require a hub-recorded merge.

The chain-only paths match the chain’s Git blob hashes and modes, including the 598 chain additions that are untracked in the fallback. The checker found zero chain-only deletions and verified that every difference from the chain is on a PR path. `git diff --check f2fa1a6d6` passed in the worktree.

### Conflict resolutions

| File | Resolution |
| --- | --- |
| `subfleet/daemon.py` | Both sides inserted code after the source preamble check. The PR’s effective-task validation runs first and refuses review metadata on a non-review source with `review-fields`. #310 then reads the attempt evidence and computes the model pin with `_resume_pin`. The next `dataclasses.replace` uses #310’s `pinned_model` and the PR’s inherited subject and round. |
| `subfleet/store.py` | Kept the PR’s version-12 constants and slot-12 tuple, and #307’s `NOTICE_WAKE_PREDATES = "before-migration"` with its C-15.9 comment. Used #307’s C-15.1 slot-11 comment. The full slot-11 SQL tuples match on both parents. Combined the two loop hunks by retaining the PR’s lower bound and presence checks, recording newly added columns in #307’s per-step `added` set, and running the C-15.9 update only when that step adds `notices.wake_state`. Kept the PR’s `step > version` guard for launch capabilities and its atomic schema, version and audit writes. |
| `subfleet/store_schema.sql` | Used #307’s C-15.1 comment over caller-wake storage, and kept the PR’s version-12 header, review columns and partial index. |
| `tests/unit/test_prune_217.py` | Used #307’s `_indexes_naming_column` in place of the PR’s inline scan. It checks indexed key columns and the SQL definition, so it finds `notices_caller_wake` even though the index names `wake_state` only in its predicate. The four case and quoting variants cover that lookup. |
| `tests/unit/test_store_migration_2.py` | Used #307’s generalized historical reconstruction. It drops tables created by `CREATE TABLE IF NOT EXISTS`, including `caller_wake_sessions`, and finds dependent indexes in `sqlite_master`. All three slot-12 statements satisfy its `ALTER TABLE ... ADD COLUMN` assertion. Both parents imported `re` in different locations, so I kept one import in the chain’s location. |
| `tests/unit/test_store_qos_migration.py` | Used all three chain hunks, including `test_d_qos_booked1_the_column_has_a_numbered_migration` and `PROVIDER_QOS_MIGRATION` for the fixture assertions. Kept the `_migration_adding` import because the slot assertion and historical fixture still use it. Kept the PR’s null review-field assertions. |
| `docs/acceptance-contract.md` | Used the hub’s script with the two full commit IDs because Git never set `MERGE_HEAD`. It exited 0. |

The seven automatic results match the byte-for-byte union of the two parents’ disjoint changes.

| File | PR hunks | Chain hunks |
| --- | --- | --- |
| `README.md` | Review subject and caller-family flags after resume | Review format and accepted-result guidance |
| `subfleet/cli.py` | Caller/session attribution, review fields, round labels and submission flags | Review result data and `_review_line`, plus #307’s waited-notice offering |
| `subfleet/protocol.py` | Review capability fields, submission arguments and refusal reason | #307’s `NoticePendingArgs` |
| `subfleet/refusals.py` | Review metadata errors and rendering | Worktree and #310 resume-pin refusal entries and rendering |
| `subfleet/retention_archive.py` | Subject and round in the accepted-review retirement event | Bundle deferral and lock cleanup, plus #316 list, check and restore routing |
| `tests/unit/test_handler_refusals.py` | Subject shape and synthetic sentinel assertions | Resume-pin property and example coverage |
| `tests/unit/test_retention_archive.py` | Local fetch for the `gh-pages` fixture | Broken worktree registration cleanup test |

### Contract

```text
$ python3 /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py d5e6cb4aead4cdb7fbde5425643cf48a13682547 f2fa1a6d67d39adf07d3579a265c579467b6bbca
merged against 53a7bac93: 264 clauses, none duplicated
```

The PR entry comes first and #316’s four C-8.8 entries lead the chain additions. The actual chain inputs have 25 changed or added clause lines. The chain’s 20th change-log addition, #295’s D-TREE-THRASH1 entry, sits at position 47 of the chain log. The result preserves that placement. One older duplicate change-log entry is present in all four versions. All 21 new entries occur once. Clause text, line diffs and the file hash matched the checks below. The merge added no change-log entry. C-3.9 describes the contents of slot 11’s DDL, and C-15.9 describes the migration-time stale marking and caller-wake runtime.

```text
split lines (base/PR/chain/merged): 868 872 895 899
wc -l (base/PR/chain/merged): 867 871 894 898
merged bytes: 1111997; SHA-256: 47867ac32e686a89667de493fa766b51ce63061c3548091f7ec316dd3c07f42a
change-log entries (base/PR/chain/merged): 368 369 388 389
chain additions' positions within chain change log: 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 47
change-log order, preservation and single occurrence of all 21 new entries: PASS
pre-existing repeated entries (base/PR/chain/merged): 1 1 1 1
script clauses (base/PR/chain/merged): 257 260 261 264
lettered clauses (base/PR/chain/merged): 260 263 264 267
titled clauses (base/PR/chain/merged): 262 265 266 269
duplicate clause IDs: none; C-3.8 and C-3.9: one each
PR changed clauses: C-3.9, C-6.5, C-6.21, C-6.22, C-17.2
chain changed clauses (25): C-3.2, C-3.8, C-5.4, C-5.5, C-6.7, C-6.8, C-6.10, C-6.11, C-6.12, C-6.20, C-8.4, C-8.8, C-12.4, C-12.10, C-12.11, C-13.4, C-15.2, C-15.3, C-15.6, C-15.9, C-16.5, C-17.1, C-23.16, C-25.7, C-26.8
clause text equals its authoring parent or base: PASS
chain→merged line diff equals base→PR (5 edit groups): PASS
PR→merged line diff equals base→chain (21 edit groups): PASS
```

### Schema slots and semantic checks

```text
SCHEMA_VERSION 12; migration slots 4–12
slot 11 equals both parents (6 statements); slot 12 equals PR (3 statements)
SQL version header, caller wake table, both slot indexes, events_notice_retired: PASS
```

- **#307 and historical stores.** The version-10 fixture gains slot 11’s columns and table plus slot 12’s three columns. Its pending and offered notices become `stale` with `before-migration`. Surfaced and acknowledged notices keep null wake state. The #307 version-11 fixture gains the review columns and their index and preserves all existing wake values. The old #314 version-11 and version-12 fixtures preserve their existing review values and gain the wake objects they lack. The partial version-12 fixture already has `wake_state`, so repairing its remaining columns and table leaves the old notice eligible. Second opens preserve the row, version and audit snapshots, including a newly inserted pending notice that remains eligible. The already-version-12 repairs add no schema version or migration event.
- **#313 and review metadata.** Read online list/show and the offline ledger paths. List JSON keeps the result alongside `caller_family`, `review_subject` and `review_round`. Online show has the fields and result inside the job and exposes the result at top level. Offline show has the fields and result in its flat row. Readable `runs` puts the round beside the name. Bare `runs show` emits metadata on stdout and the review verdict and counts on stderr. The shared prompt path still writes one review-format preamble, including for reviews with `--no-preamble` and resumed reviews. Review metadata normalization and round assignment leave that prompt path intact.
- **#310 and resume.** The resume substitution carries the source subject and round and the pin derived from its recorded attempt model ID and alias. The review resume test keeps round 1 and advances the next accepted round to 2. The resume-contract cases exercise alias retention, fallback and launch effort.
- **#315 and audit names.** A committed `job.submitted` inserts the job during that transaction. A refused round rolls back before inserting a job or emitting the event. Admission still owns `attempt.reserved` and its event IDs. The review code needs no `audit_as` change.
- **#316 and archives.** The PR writes the accepted subject and round into `retention.pruned` and keeps the job fields in `rows.json`. Offload moves manifest-owned payload files and the bundle. The offload/recall test checks that `rows.json` stays inside while offloaded and that the recalled tree equals the original. Neither operation replaces the SQLite audit event.

The bare merge produced four failures in PR-owned tests. Three historical-store cases still expected null wake state where #307 marks unread notices stale. The CLI case expected list JSON to omit #313’s `review` key. I updated the historical fixture assertions and added the offered, surfaced, acknowledged, partial-repair and new-notice cases. I also checked both an older daemon with null `review` and a daemon returning an accepted result alongside all three review fields. The affected batch passed all 382 cases after these test changes. The sandbox’s `index.lock` denial also prevented a separate commit for the test updates.

### Setup and deviations

- The sandbox denied writes in the Darwin temporary directory and its uv cache. I set `TMPDIR` to the job’s provided temporary directory, copied round 1’s two files to scratch and copied the 52,288 KiB shared cache into scratch. `uv sync --group dev` built the missing worktree `.venv` with Python 3.15.2 and pytest 9.1.1. Tests used xdist 3.8.0 with up to four workers. The shared cache stayed in place.
- Every pytest import probe resolved `subfleet.__file__` to the assigned worktree. The launcher cleared inherited caller/session variables and the real-Claude opt-in. The round 1 plugin only deselected tests. A scratch-only pytest plugin redirected the suite’s retained-failure directory to the `.failed-roots` sibling of the selection log, so session startup did not prune the shared `/tmp/sf-failed` or probe PIDs from it. Hypothesis storage also went to scratch. The runner used no per-test timeout override.
- The verified contract preserves the committed chain’s placement of its #295 entry and its 25 clause-line edits, with the expected hash. The merge and test updates are in worktree files because Git metadata is read-only in this job.
- Two #307 tests in chain-only `tests/unit/test_store_notice_wake_migration.py` fail on obsolete latest-version assumptions. `test_d_wake_caller1_notice_delivery_is_a_numbered_additive_migration` equates slot 11 with `SCHEMA_VERSION` and assumes the latest version list ends at 11. `test_d_wake_caller1_reopening_preserves_retry_delivery_and_activity_records` finds the two correct migration events for slots 11 and 12 and expects one. The fallback preserves that chain file byte for byte. The PR-owned historical-store checks exercise the combined behavior.
- The eight real-Claude premise cases were skipped by their opt-in marker.

- `test_event_name_properties.py::TestAdmissionPasses::runTest` stopped at daemon construction with `macOS boot identity is unavailable`, before it exercised any event. The other eligible event-name, audit and session cases reached their assertions.
- The optional archive-path batch exceeded the remaining churn allowance before it started. `df -H /` still showed 71 GB available. I used the remaining allowance for two smaller importer batches.

### Test runs

Each run checked the worktree import, the available space on `/`, and the time budget. The table records elapsed UTC intervals and the original local-time `uptime` readings. Sizes use KiB from the basetemp measured before deletion.

| Batch | UTC start → end | Start → end `uptime` | `df -H /` data row | Passed | Failed | Skipped | Deselected | Basetemp KiB |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| `contract` | 11:41:22Z → 11:46:04Z | 12:41  1 user, load averages: 72.75 70.95 72.31<br>12:46  1 user, load averages: 30.00 58.63 67.16 | `/dev/disk3s1s1   3996G     14G     77G    16%    487k  752M    0%   /` | 463 | 0 | 0 | 0 | 21716 |
| `pr-submit` | 11:46:38Z → 11:51:09Z | 12:46  1 user, load averages: 21.10 53.25 64.85<br>12:51  1 user, load averages: 58.67 48.99 58.93 | `/dev/disk3s1s1   3996G     14G     77G    16%    487k  750M    0%   /` | 61 | 0 | 0 | 0 | 32936 |
| `pr-cli-store-bare` | 11:51:33Z → 11:53:18Z | 12:51  1 user, load averages: 62.43 50.67 59.25<br>12:53  1 user, load averages: 61.00 54.47 59.80 | `/dev/disk3s1s1   3996G     14G     78G    16%    487k  760M    0%   /` | 376 | 4 | 0 | 0 | 12704 |
| `pr-prune` | 11:53:37Z → 11:56:56Z | 12:53  1 user, load averages: 64.05 55.51 60.05<br>12:56  1 user, load averages: 29.81 50.10 57.41 | `/dev/disk3s1s1   3996G     14G     77G    16%    487k  758M    0%   /` | 51 | 0 | 0 | 0 | 17144 |
| `pr-archive` | 11:57:11Z → 12:09:46Z | 12:57  1 user, load averages: 25.45 47.78 56.40<br>13:09  1 user, load averages: 44.94 54.07 55.68 | `/dev/disk3s1s1   3996G     14G     79G    15%    487k  771M    0%   /` | 108 | 0 | 0 | 1 | 79192 |
| `pr-cli-store` | 12:10:19Z → 12:10:35Z | 13:10  1 user, load averages: 36.76 51.06 54.52<br>13:10  1 user, load averages: 32.29 49.37 53.85 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  695M    0%   /` | 382 | 0 | 0 | 0 | 12384 |
| `wake-store` | 12:10:36Z → 12:10:41Z | 13:10  1 user, load averages: 32.29 49.37 53.85<br>13:10  1 user, load averages: 31.55 48.94 53.67 | `/dev/disk3s1s1   3996G     14G     72G    17%    487k  703M    0%   /` | 139 | 2 | 0 | 0 | 16256 |
| `wake-fake` | 12:10:42Z → 12:10:53Z | 13:10  1 user, load averages: 31.55 48.94 53.67<br>13:10  1 user, load averages: 28.06 47.31 53.01 | `/dev/disk3s1s1   3996G     14G     72G    17%    487k  701M    0%   /` | 83 | 0 | 0 | 2 | 49968 |
| `review-contract` | 12:10:54Z → 12:11:17Z | 13:10  1 user, load averages: 28.06 47.31 53.01<br>13:11  1 user, load averages: 28.53 45.92 52.34 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  697M    0%   /` | 822 | 0 | 0 | 0 | 98600 |
| `resume-model` | 12:11:22Z → 12:11:31Z | 13:11  1 user, load averages: 28.53 45.92 52.34<br>13:11  1 user, load averages: 27.80 45.18 52.01 | `/dev/disk3s1s1   3996G     14G     72G    17%    487k  701M    0%   /` | 41 | 0 | 0 | 0 | 24556 |
| `effort` | 12:11:33Z → 12:11:39Z | 13:11  1 user, load averages: 27.89 44.91 51.87<br>13:11  1 user, load averages: 27.42 44.53 51.70 | `/dev/disk3s1s1   3996G     14G     72G    17%    487k  701M    0%   /` | 51 | 0 | 8 | 0 | 8876 |
| `events` | 12:11:41Z → 12:11:47Z | 13:11  1 user, load averages: 27.42 44.53 51.70<br>13:11  1 user, load averages: 29.40 44.40 51.56 | `/dev/disk3s1s1   3996G     14G     72G    17%    487k  700M    0%   /` | 78 | 1 | 0 | 114 | 15004 |
| `event-sessions` | 12:11:48Z → 12:12:19Z | 13:11  1 user, load averages: 29.40 44.40 51.56<br>13:12  1 user, load averages: 26.28 42.25 50.54 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  700M    0%   /` | 50 | 0 | 0 | 8 | 12968 |
| `offload` | 12:12:20Z → 12:15:30Z | 13:12  1 user, load averages: 26.28 42.25 50.54<br>13:15  1 user, load averages: 27.43 34.89 45.68 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  699M    0%   /` | 43 | 0 | 0 | 0 | 51848 |
| `offload-recovery` | 12:15:56Z → 12:16:25Z | 13:15  1 user, load averages: 28.38 34.52 45.24<br>13:16  1 user, load averages: 32.25 34.98 45.04 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 54 | 0 | 0 | 0 | 16208 |
| `optional-store` | 12:17:00Z → 12:17:38Z | 13:17  1 user, load averages: 34.32 35.43 44.81<br>13:17  1 user, load averages: 29.59 34.16 43.92 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 155 | 0 | 0 | 0 | 21784 |
| `optional-contention` | 12:17:39Z → 12:17:51Z | 13:17  1 user, load averages: 29.59 34.16 43.92<br>13:17  1 user, load averages: 28.34 33.74 43.66 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 212 | 0 | 0 | 0 | 74860 |
| `optional-readers` | 12:17:53Z → 12:18:03Z | 13:17  1 user, load averages: 27.35 33.45 43.50<br>13:18  1 user, load averages: 26.80 33.12 43.26 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 28 | 0 | 0 | 0 | 10960 |
| `optional-retention` | 12:18:04Z → 12:18:28Z | 13:18  1 user, load averages: 26.80 33.12 43.26<br>13:18  1 user, load averages: 23.78 31.92 42.54 | `/dev/disk3s1s1   3996G     14G     70G    17%    487k  692M    0%   /` | 96 | 0 | 0 | 0 | 31500 |
| `optional-prune` | 12:18:29Z → 12:18:38Z | 13:18  1 user, load averages: 23.78 31.92 42.54<br>13:18  1 user, load averages: 24.17 31.74 42.35 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 48 | 0 | 0 | 0 | 23524 |
| `optional-operator` | 12:18:39Z → 12:19:09Z | 13:18  1 user, load averages: 24.17 31.74 42.35<br>13:19  1 user, load averages: 29.18 32.21 42.15 | `/dev/disk3s1s1   3996G     14G     70G    17%    487k  692M    0%   /` | 258 | 0 | 0 | 0 | 54336 |
| `optional-reset` | 12:19:12Z → 12:20:11Z | 13:19  1 user, load averages: 29.18 32.21 42.15<br>13:20  1 user, load averages: 28.61 31.73 41.31 | `/dev/disk3s1s1   3996G     14G     70G    17%    487k  692M    0%   /` | 121 | 0 | 0 | 0 | 67308 |
| `optional-retention-ops` | 12:20:12Z → 12:20:21Z | 13:20  1 user, load averages: 28.61 31.73 41.31<br>13:20  1 user, load averages: 27.44 31.38 41.07 | `/dev/disk3s1s1   3996G     14G     70G    17%    487k  691M    0%   /` | 135 | 0 | 0 | 0 | 12124 |
| `optional-timing` | 12:20:22Z → 12:21:11Z | 13:20  1 user, load averages: 27.44 31.38 41.07<br>13:21  1 user, load averages: 19.29 28.67 39.52 | `/dev/disk3s1s1   3996G     14G     70G    17%    487k  691M    0%   /` | 167 | 0 | 0 | 0 | 98772 |
| `optional-small` | 12:22:11Z → 12:22:15Z | 13:22  1 user, load averages: 20.32 27.26 38.25<br>13:22  1 user, load averages: 20.13 27.11 38.13 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 25 | 0 | 0 | 0 | 4040 |
| `optional-timer-policy` | 12:23:00Z → 12:23:04Z | 13:23  1 user, load averages: 21.19 26.46 37.32<br>13:23  1 user, load averages: 20.30 26.19 37.16 | `/dev/disk3s1s1   3996G     14G     71G    17%    487k  692M    0%   /` | 45 | 0 | 0 | 0 | 7780 |

Across 26 runs, including the bare-merge reproduction, **4,092 passed, 7 failed, 8 skipped and 125 deselected.**

### Test selection

- 14 deselected by `process: starts a production daemon child whose census reads the host process table`.
- 1 deselected by `node: host processes: native guardian and production control loop reach unstubbed containment census`.
- 91 deselected by `node: host processes: staged routing_state Claude registry reaches unstubbed procs.snapshot`.
- 18 deselected by `file: host processes: staged routing_state Claude registry reaches unstubbed procs.snapshot`.
- 1 deselected by `marker: real_lsof/real_cwd reads host processes`.

The added selectors below include all parametrizations at each named node and all nodes in each named file. They point to the staged Claude registry and unstubbed `procs.snapshot`, except the handoff node, which reaches the native guardian and control-loop census.

```text
tests/fake/test_admission_route_isolation.py::test_c11_2_a_dry_run_resolves_the_same_way
tests/fake/test_admission_route_isolation.py::test_c11_2_a_later_roster_change_cannot_make_an_accepted_pin_ambiguous
tests/fake/test_admission_route_isolation.py::test_c11_2_a_mismatched_lane_is_dropped_only_within_one_provider
tests/fake/test_admission_route_isolation.py::test_c11_2_a_pin_follows_its_lane_through_a_reenrolment
tests/fake/test_admission_route_isolation.py::test_c11_2_the_jobs_provider_narrows_a_pin_that_names_two_lanes
tests/fake/test_admission_route_isolation.py::test_c11_2_the_pin_roster_names_lanes_as_the_view_admission_evaluates
tests/fake/test_admission_route_isolation.py::test_c4_5_a_retry_let_go_is_evaluated_again_when_it_is_next_due
tests/fake/test_admission_route_isolation.py::test_c4_5_a_retry_that_followed_a_reenrolment_counts_the_account_once
tests/fake/test_admission_route_isolation.py::test_c4_5_a_retry_whose_credential_is_latched_goes_to_the_next_candidate
tests/fake/test_admission_route_isolation.py::test_c4_5_a_retry_whose_lane_is_only_full_keeps_its_lane
tests/fake/test_admission_route_isolation.py::test_c4_5_a_retry_whose_lane_refuses_it_for_more_than_a_slot_goes_to_the_next_candidate
tests/fake/test_admission_route_isolation.py::test_c6_12_a_fleet_held_only_by_route_waits_warns
tests/fake/test_admission_route_isolation.py::test_c6_12_a_pass_with_nothing_to_look_at_builds_no_capacity_view
tests/fake/test_admission_route_isolation.py::test_c6_12_a_pin_that_names_two_lanes_is_refused_and_the_pass_goes_on
tests/fake/test_admission_route_isolation.py::test_c6_12_a_policy_edit_that_moves_a_tier_to_another_provider_waits_and_fails_nothing
tests/fake/test_admission_route_isolation.py::test_c6_12_a_refused_check_rolls_back_what_the_reservation_wrote
tests/fake/test_admission_route_isolation.py::test_c6_12_a_refused_writable_job_leaves_no_worktree
tests/fake/test_admission_route_isolation.py::test_c6_12_a_route_hold_outranks_other_holds_and_no_released_lease_hurries_it
tests/fake/test_admission_route_isolation.py::test_c6_12_a_transient_retry_on_a_model_id_the_policy_dropped_routes_the_job_itself
tests/fake/test_admission_route_isolation.py::test_c6_12_a_transient_retry_whose_id_is_retired_to_another_provider_routes_the_job_itself
tests/fake/test_admission_route_isolation.py::test_c6_12_a_transient_retry_whose_lane_was_disabled_since_routes_the_job_itself
tests/fake/test_admission_route_isolation.py::test_c6_12_an_authorization_bound_to_a_label_is_refused
tests/fake/test_admission_route_isolation.py::test_c6_12_an_authorization_the_probe_check_rejects_is_refused_and_the_pass_goes_on
tests/fake/test_admission_route_isolation.py::test_c6_12_an_empty_task_or_tier_is_none
tests/fake/test_admission_route_isolation.py::test_c6_12_an_evaluation_error_holds_the_job_visibly_and_holds_nobody_else
tests/fake/test_admission_route_isolation.py::test_c6_12_an_evaluation_made_again_off_the_lock_is_isolated_too
tests/fake/test_admission_route_isolation.py::test_c6_12_an_evaluation_that_succeeds_resets_the_route_count
tests/fake/test_admission_route_isolation.py::test_c6_12_any_evaluation_error_of_one_job_is_that_jobs
tests/fake/test_admission_route_isolation.py::test_c6_12_route_waits_back_off_and_a_restart_looks_at_them_at_once
tests/fake/test_admission_route_isolation.py::test_c6_12_route_waits_double_from_5_s_to_a_300_s_ceiling
tests/fake/test_admission_route_isolation.py::test_c6_12_the_incident_a_job_pinned_by_an_ambiguous_email_does_not_stop_the_pass
tests/fake/test_admission_route_isolation.py::test_c6_12_the_incident_through_the_worker_pool_logs_no_failure
tests/fake/test_admission_route_isolation.py::test_c6_12_why_names_a_route_error_it_meets_itself
tests/fake/test_admission_route_isolation.py::test_c6_3_a_cap_that_fills_before_the_reservation_is_judged_on_every_lane
tests/fake/test_admission_route_isolation.py::test_c6_3_a_commit_that_changes_nothing_the_decision_rests_on_does_not_send_it_back
tests/fake/test_admission_route_isolation.py::test_c6_3_a_commit_that_moves_the_decision_is_decided_again_from_the_lanes_it_changed
tests/fake/test_admission_route_isolation.py::test_c6_3_a_long_wait_for_the_store_lock_does_not_void_the_decision
tests/fake/test_admission_route_isolation.py::test_c6_3_a_reading_that_ages_out_before_the_reservation_is_judged_again
tests/fake/test_admission_route_isolation.py::test_c6_3_an_early_evaluation_whose_readings_stay_fresh_is_reserved_on
tests/fake/test_admission_route_isolation.py::test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again
tests/fake/test_admission_route_isolation.py::test_c6_3_one_view_decides_an_override_on_one_clock
tests/fake/test_admission_route_isolation.py::test_c6_3_the_reservation_takes_the_early_evaluation_when_nothing_was_committed
tests/fake/test_admission_route_isolation.py::test_c6_9_a_due_retry_is_looked_at_as_its_pair_before_it_is_evaluated
tests/fake/test_admission_route_isolation.py::test_c6_9_a_pinned_retry_competes_only_for_its_own_pair
tests/fake/test_admission_route_isolation.py::test_c6_9_a_retry_that_lets_its_pin_go_keeps_its_place_behind_older_jobs
tests/fake/test_admission_route_isolation.py::test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go
tests/fake/test_capacity_wait_rollback.py
tests/fake/test_event_names.py::test_c3_8_a_held_lease_records_the_wait
tests/fake/test_event_names.py::test_c3_8_a_pair_that_changed_after_its_probe_records_the_wait
tests/fake/test_event_names.py::test_c3_8_a_wait_that_changes_the_job_s_exclusions_records_it
tests/fake/test_fence_wait_rollback.py
tests/fake/test_idle_capacity_events.py::test_c23_16_demand_is_identical_between_old_and_long_capacity_looks
tests/fake/test_idle_capacity_events.py::test_c6_10_a_condition_seen_only_after_the_census_still_brings_waits_forward
tests/fake/test_idle_capacity_events.py::test_c6_10_a_failed_pass_keeps_capacity_news_for_every_wait_on_retry
tests/fake/test_idle_capacity_events.py::test_c6_10_capacity_events_bring_every_eligible_wait_forward
tests/fake/test_idle_capacity_events.py::test_c6_10_known_operator_reset_still_beats_the_long_clock
tests/fake/test_idle_capacity_events.py::test_c6_10_long_wait_commits_at_most_six_times_per_hour_per_job
tests/fake/test_idle_capacity_events.py::test_c6_10_no_change_never_brings_waits_forward
tests/fake/test_idle_capacity_events.py::test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling
tests/fake/test_idle_capacity_events.py::test_c6_10_released_hold_places_the_old_rules_jobs_in_order_no_later
tests/fake/test_idle_capacity_events.py::test_c6_10_unchanged_operator_wait_reaches_600_seconds
tests/fake/test_idle_capacity_refresh_bound.py
tests/fake/test_notice_delivery.py::test_c15_3_state_a_pin_notice_is_a_message_surfaced_once
tests/fake/test_probe_wait_rollback.py
tests/fake/test_sessions_end_to_end.py::test_handoff_dispatches_detached_through_the_normal_submit_path
tests/fake/test_store_contention_meter.py
```

Round 1’s existing rules also handled the archive test `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` and the production-child `process_inspection_available` fixture wherever collected.

### Footprint and cleanup

Scratch was `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261126-122031-rva2-merge-r3-a1-yt9u2sbq/sf-a2-merge3`.
Its remaining subdirectories were `__pycache__/`, `hypothesis/`, `logs/`, `plugin/`, `uv-cache/`.
I removed each basetemp named in the commands below and the scratch tree with guarded `shutil.rmtree`.
Each deletion asserted that the resolved location was inside `sf-a2-merge3`.

New `sf-test-homes-*` left under the job’s `TMPDIR` after the final run: 0.

Measured churn was about **943,420 KiB** (966 MB), including 877,348 KiB of basetemps, 0 KiB of retained failures, 53,280 KiB of remaining scratch including uv cache, and 12,792 KiB in the ignored worktree `.venv`. The largest accounted scratch footprint at a run boundary was 152,040 KiB.

### Final head and commands

**No commit.** The sandbox denied Git’s metadata locks. The actual existing HEAD is `d5e6cb4aead4cdb7fbde5425643cf48a13682547`.

The commands below expand through this shell function. `scratch` is the path above, with `TMPDIR` set to its parent.

```sh
run() {
  label=$1 workers=$2 base=$3
  shift 3
  PYTHONPATH="$scratch/plugin" UV_CACHE_DIR="$scratch/uv-cache" \
    HYPOTHESIS_STORAGE_DIRECTORY="$scratch/hypothesis" \
    SF_MERGE1_EXCLUDE="$scratch/exclude.tsv" SF_MERGE1_LOG="$scratch/logs/$label.deselected.jsonl" \
    uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider \
      -p sf_merge1_select -p sf_merge3_artifacts -n "$workers" --basetemp "$scratch/$base" -ra "$@"
}

# 463 passed, 0 failed
run contract 4 pytest-contract-1795693282 \
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py

# 61 passed, 0 failed
run pr-submit 4 pytest-pr-submit-1795693598 \
  tests/fake/test_review_subject_resume.py tests/fake/test_review_subject_submission.py \
  tests/fake/test_review_subject_validation.py tests/unit/test_caller_family_ancestry.py \
  tests/unit/test_codex_writable_fanout.py tests/unit/test_reserve_authorization_plumbing.py

# 376 passed, 4 failed
run pr-cli-store-bare 4 pytest-pr-cli-store-bare-1795693893 \
  tests/unit/test_cli.py tests/unit/test_review_runs_columns.py \
  tests/unit/test_review_subject_cli.py tests/unit/test_review_subject_normalization.py \
  tests/unit/test_review_subject_resume_cli.py tests/unit/test_store_migration_2.py \
  tests/unit/test_store_qos_migration.py tests/unit/test_store_review_subject_migration.py \
  tests/unit/test_store_upgrade_slots.py

# 51 passed, 0 failed
run pr-prune 4 pytest-pr-prune-1795694017 \
  tests/unit/test_prune_217.py tests/unit/test_retention_review_subject.py

# 108 passed, 0 failed
run pr-archive 3 pytest-pr-archive-1795694231 \
  tests/unit/test_retention_archive.py

# 382 passed, 0 failed
run pr-cli-store 4 pytest-pr-cli-store-1795695019 \
  tests/unit/test_cli.py tests/unit/test_review_runs_columns.py \
  tests/unit/test_review_subject_cli.py tests/unit/test_review_subject_normalization.py \
  tests/unit/test_review_subject_resume_cli.py tests/unit/test_store_migration_2.py \
  tests/unit/test_store_qos_migration.py tests/unit/test_store_review_subject_migration.py \
  tests/unit/test_store_upgrade_slots.py

# 139 passed, 2 failed
run wake-store 4 pytest-wake-store-1795695036 \
  tests/unit/test_store_notice_wake_migration.py tests/unit/test_notice_claims.py \
  tests/unit/test_notice_rendering.py tests/unit/test_sessions_notice_wake.py

# 83 passed, 0 failed
run wake-fake 4 pytest-wake-fake-1795695042 \
  tests/fake/test_caller_notice_wakes.py tests/fake/test_notice_delivery.py

# 822 passed, 0 failed
run review-contract 4 pytest-review-contract-1795695054 \
  tests/unit/test_review_contract_prompt.py tests/unit/test_review_result.py \
  tests/unit/test_review_result_cli.py tests/unit/test_review_result_storage.py

# 41 passed, 0 failed
run resume-model 4 pytest-resume-model-1795695082 \
  tests/fake/test_resume_contract.py

# 51 passed, 0 failed
run effort 4 pytest-effort-1795695093 \
  tests/unit/test_effort_env.py tests/unit/test_ultracode_effort.py

# 78 passed, 1 failed
run events 4 pytest-events-1795695101 \
  tests/unit/test_store_audit.py tests/fake/test_event_names.py \
  tests/fake/test_event_name_properties.py tests/fake/test_capacity_wait_rollback.py \
  tests/fake/test_probe_wait_rollback.py tests/fake/test_fence_wait_rollback.py \
  tests/fake/test_store_contention_meter.py tests/fake/test_admission_route_isolation.py \
  tests/fake/test_idle_capacity_events.py tests/fake/test_idle_capacity_refresh_bound.py

# 50 passed, 0 failed
run event-sessions 4 pytest-event-sessions-1795695108 \
  tests/fake/test_probe_cancel.py tests/fake/test_sessions_end_to_end.py \
  tests/fake/test_waiting_queue_cost.py

# 43 passed, 0 failed
run offload 4 pytest-offload-1795695140 \
  tests/unit/test_retention_offload.py

# 54 passed, 0 failed
run offload-recovery 4 pytest-offload-recovery-1795695356 \
  tests/unit/test_retention_offload_r1.py tests/unit/test_retention_offload_r2.py \
  tests/unit/test_retention_offload_r3.py tests/unit/test_retention_volume_offload_r1.py

# 155 passed, 0 failed
run optional-store 4 pytest-optional-store-1795695420 \
  tests/unit/test_admission_store_wakes.py tests/unit/test_batch_submission_queries.py \
  tests/unit/test_prune_decision_backup_verification.py tests/unit/test_qos_review_r1.py \
  tests/unit/test_render_quota_projection.py tests/unit/test_store_ids.py \
  tests/unit/test_store_properties.py tests/unit/test_store_snapshot_plans.py

# 212 passed, 0 failed
run optional-contention 3 pytest-optional-contention-1795695459 \
  tests/unit/test_store_contention_queries.py

# 28 passed, 0 failed
run optional-readers 2 pytest-optional-readers-1795695473 \
  tests/unit/test_store_readers.py

# 96 passed, 0 failed
run optional-retention 4 pytest-optional-retention-1795695484 \
  tests/unit/test_pin_notice_identity.py tests/unit/test_retention_outcome_pages.py \
  tests/unit/test_retention_outcome_permissions.py tests/unit/test_retention_outcomes.py \
  tests/unit/test_notice_keep.py

# 48 passed, 0 failed
run optional-prune 2 pytest-optional-prune-1795695509 \
  tests/unit/test_prune_decisions.py

# 258 passed, 0 failed
run optional-operator 4 pytest-optional-operator-1795695519 \
  tests/unit/test_hold_note.py tests/unit/test_operator_hold_closures.py \
  tests/unit/test_operator_notices.py tests/unit/test_status_json.py \
  tests/unit/test_liveness_write_pacing.py

# 121 passed, 0 failed
run optional-reset 3 pytest-optional-reset-1795695552 \
  tests/unit/test_reset_credit_claim_timing.py tests/unit/test_reset_credit_properties.py \
  tests/unit/test_reset_credit_review_r1.py tests/unit/test_reset_credit_review_r2.py \
  tests/unit/test_reset_credit_review_r3.py tests/unit/test_wall_retry_properties.py

# 135 passed, 0 failed
run optional-retention-ops 4 pytest-optional-retention-ops-1795695612 \
  tests/unit/test_retention_delete_daemon.py tests/unit/test_retention_delete_request.py \
  tests/unit/test_retention_turn_contract.py tests/unit/test_retention_turns.py \
  tests/unit/test_timers_retention.py

# 167 passed, 0 failed
run optional-timing 3 pytest-optional-timing-1795695622 \
  tests/unit/test_daemon_inspection_load.py tests/unit/test_daemon_inspection_properties.py \
  tests/unit/test_inspection_pacing_properties.py tests/unit/test_kill4_properties.py \
  tests/unit/test_lockwatch.py tests/unit/test_timers_busy_read.py

# 25 passed, 0 failed
run optional-small 2 pytest-optional-small-1795695731 \
  tests/unit/test_attempt_tmpdir_retention.py tests/unit/test_claude_cards_wiring.py \
  tests/unit/test_timers_daemon.py

# 45 passed, 0 failed
run optional-timer-policy 2 pytest-optional-timer-policy-1795695780 \
  tests/unit/test_timers_alerts.py tests/unit/test_timers_keepalive.py \
  tests/unit/test_policy_support.py
```

The merged worktree had 118 Store importers, including 4 PR-only importers. Of 114 chain Store importers, steps 1–4 collected 9 and the optional runs collected 44 more. 61 remain outside this run.

**Left for the hub and CI.** Record the merged worktree with the intended parents and commit the test updates and report. The two chain-only #307 assertions need to account for slot 12. Recheck the event property where macOS boot identity is available. The process-dependent deselections, real-Claude opt-in cases and remaining optional importers are outside this run.

### Hub collection

The hub recorded the merge in the lane with the intended parents (`76c8ac29a`), copying the seven resolved files from the job's worktree. The contract matched the SHA-256 above, and every other path matched the job's files. The PR-owned test updates went in a separate commit (`82f52185b`). The two #307 tests in `tests/unit/test_store_notice_wake_migration.py` now allow a later slot (`273ab7a87`): slot 11 must be at or below `SCHEMA_VERSION`, and the first open of a version-10 store records one version row and one `schema.migrated` event for each step from 11 to the latest. On `273ab7a87`, the four contract files, the PR's changed test files and `tests/unit/test_store_*.py` passed 1,319 tests with `-n 4`.
