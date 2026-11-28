CLI subagent preparation and staging actions

This inventory covers recoverable non-pytest shell commands and edits performed by the CLI subagent. Test invocations, exit codes, case counts, elapsed times and basetemps are in `test-commands.jsonl`. Repeated retrievals of the same test log and tool-session polls are grouped; polls launched no new shell command. Reads below returned the requested source or scratch artifact unless a different result is stated. No environment values, host process ancestry or live state were inspected.

Startup and source inspection:

```sh
date -u
sed -n '1,260p' /Users/maxghenis/reviews/subfleet-hub/review-guidance-2026-11-13/review-a2-r1.md
rg --files -g 'AGENTS.md' -g 'test*cli*' -g '*resume*' -g '*caller*' -g '*review*' tests subfleet .agents
sed -n '1,420p' subfleet/cli.py
```

The start-time read returned `Sun Nov 15 23:16:56 UTC 2026`; the parent agent's earlier start governs the job deadline. The review read succeeded. The discovery `rg` reported `.agents: No such file or directory` (exit 2 for that command); the following `sed` succeeded. No `.agents` file was read.

```sh
sed -n '760,1050p' subfleet/cli.py
sed -n '2120,2350p' subfleet/cli.py
sed -n '1,340p' tests/unit/test_review_subject_cli.py
sed -n '1,280p' tests/fake/test_resume_cli.py
rg -n '_daemon_capabilities|capabilit|def format_runs' subfleet/cli.py tests/unit/test_cli.py tests/fake/test_resume_cli.py
sed -n '425,465p' subfleet/cli.py
sed -n '1060,1270p' subfleet/cli.py
sed -n '1680,1745p' subfleet/cli.py
sed -n '2220,2310p' subfleet/cli.py
sed -n '1,240p' tests/unit/conftest.py
rg -n 'REVIEW.*CAPABILITY|review.subject|review_subject' subfleet/protocol.py subfleet/daemon.py subfleet/sessions/client.py tests/conftest.py tests/unit/conftest.py
sed -n '1265,1415p' subfleet/cli.py
sed -n '1,260p' subfleet/sessions/client.py
sed -n '90,155p' subfleet/protocol.py
sed -n '465,495p' subfleet/cli.py
rg -n 'caller_family\(|session_id\(' tests/unit/test_cli.py tests/unit/test_sessions_cli.py tests/unit/test_sessions*.py tests/fake/test_resume_contract.py tests/unit/test_host_push_cli.py
rg -n 'CAPABILITY' subfleet/protocol.py
sed -n '1,160p' tests/fake/test_review_subject_submission.py
sed -n '1,180p' tests/conftest.py
sed -n '25,60p' subfleet/protocol.py
sed -n '1,100p' /private/tmp/sf-a2-fix1/run.py
rg -n '_require_review_capability' subfleet/cli.py
sed -n '90,145p' tests/unit/test_review_subject_cli.py
rg -n 'server = daemon|daemon\(\{"submit"' tests/unit/test_review_subject_cli.py
rg -n 'resume =|resume.add_argument|commands.add_parser\("resume|sub.add_parser\("resume' subfleet/cli.py
rg -n 'resume|resume-codex' subfleet/cli.py | tail -18
sed -n '3970,4030p' subfleet/cli.py
rg -n 'family = caller_family|_review_caller_family' subfleet/cli.py
rg --files tests/unit | rg 'sessions.*client|cli|host_push|mcp_cli'
```

These reads established the capability pattern, all four CLI submission boundaries, resume source handling, family precedence and table layout. Some combined source outputs were truncated by the tool; narrower follow-up reads supplied the relevant definitions. The first `resume =|...` discovery returned no matching parser spelling; the subsequent search found `p_resume`.

Changes were made with `apply_patch`, only in the assigned worktree:

- Product: `subfleet/cli.py`, `subfleet/protocol.py`, `subfleet/sessions/client.py`.
- Tests: `tests/unit/test_review_subject_cli.py`, `tests/unit/test_review_subject_resume_cli.py`, `tests/unit/test_caller_family_ancestry.py`, `tests/unit/test_review_runs_columns.py`, `tests/unit/test_cli.py`.
- The parent agent edited the daemon capability advertisement in `subfleet/conversations/service.py`; that named file was included in the requested capability commit attempt.
- No test was added without failing evidence; four supplemental cases ran against an archived original CLI, while current shared product files remained untouched.

Historical CLI red-test setup used this Python operation (successful; one package tree extracted):

```python
scratch = Path('/private/tmp/sf-a2-fix1')
historical = scratch / 'historical-cli'
historical.mkdir(exist_ok=True)
archive = subprocess.run(['git', 'archive', '5b06c1826bc0', 'subfleet'],
                         capture_output=True, check=True)
subprocess.run(['tar', '-x', '-C', str(historical)], input=archive.stdout, check=True)
launcher = (scratch / 'run.py').read_text()
launcher = launcher.replace(
    'sys.path.insert(0, str(REPO))',
    "sys.path.insert(0, str(REPO))\nsys.path.insert(0, '/private/tmp/sf-a2-fix1/historical-cli')\nimport subfleet.cli")
(scratch / 'run_historical_cli.py').write_text(launcher)
```

Fixture-regression inspection:

```sh
rg -n -C 6 'setenv\("CLAUDE|setenv\("CODEX|caller_family|ops\(\)' tests/unit/test_cli.py tests/unit/test_cli_batch.py tests/unit/test_sessions_cli.py tests/unit/test_host_push_cli.py tests/unit/test_mcp_cli_contract.py
sed -n '1148,1175p' tests/unit/test_cli.py
sed -n '1,135p' tests/unit/test_cli.py
rg -n 'caller_family|CLAUDECODE|CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID' tests/unit/test_*sessions* tests/unit/test_cli*.py tests/unit/test_*client*.py tests/fake/test_*cli*
rg -n 'CLAUDECODE|CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CODEX_SESSION_ID|caller_family' tests/unit/test_cli_batch.py tests/unit/test_sessions_cli.py tests/unit/test_sessions_*.py
rg -n 'review_capabilities' tests/unit/test_cli.py
git grep -l -E 'CLAUDECODE|CLAUDE_CODE_SESSION_ID' -- tests | wc -l
git grep -n 'SUBFLEET_CALLER_FAMILY\|CODEX_THREAD_ID' -- tests | head -n 70
git grep -l -E 'CLAUDECODE|CLAUDE_CODE_SESSION_ID' -- tests
rg -n -C 4 'CLAUDECODE|CLAUDE_CODE_SESSION_ID' tests/unit/test_outcome_unknown.py tests/unit/test_daemon_refusals_cli.py tests/unit/test_daemon_verbs.py
```

The native-marker census found 18 tracked test files. Four existing CLI positive fixtures needed the capability handler; batch and sessions fixtures needed no further update. The changed `test_cli.py` then passed 256 tests. Test-log monitoring used `tail -n 5`, `-n 6`, `-n 7`, and `-n 8` on `/private/tmp/sf-a2-fix1/cli-existing-green1.log`; each read succeeded. One tool clock read returned `2026-11-15 23:40:05 UTC` for time tracking.

Named staging/commit attempts (every `git add` and every `git commit` below returned exit 128, denied `index.lock`; no index or history change succeeded):

```sh
git add subfleet/cli.py subfleet/protocol.py subfleet/sessions/client.py tests/unit/test_review_subject_cli.py
git commit -m 'fix(cli): refuse review metadata on older daemons'
git add subfleet/cli.py tests/unit/test_review_subject_resume_cli.py
git commit -m 'fix(review): retain subject and round on CLI resume'
git add subfleet/cli.py subfleet/sessions/client.py tests/unit/test_review_subject_cli.py tests/unit/test_caller_family_ancestry.py
git commit -m 'fix(cli): identify nested model callers by process ancestry'
git add subfleet/cli.py tests/unit/test_review_runs_columns.py
git commit -m 'fix(cli): preserve readable runs columns for nonreviews'
git add subfleet/cli.py subfleet/protocol.py subfleet/sessions/client.py subfleet/conversations/service.py tests/unit/test_review_subject_cli.py
git commit -m 'fix(cli): require daemon support for review submission metadata'
git add tests/unit/test_cli.py
git commit -m 'test(cli): advertise review metadata support in session fixtures'
```

`git diff --check` was run after the final CLI changes, after staging attempts and after fixture updates; every run returned exit 0 with no output. No staging used `git add -A`, and no stash, push, history rewrite or merge command was executed.

Read-only contract audit (no product or documentation edit in this follow-up):

```sh
rg -n 'C-3\.8|C-6\.5|C-6\.21|C-6\.22|caller.family|--subject|--round|resume|runs' docs/acceptance-contract.md README.md | head -n 140
sed -n '128,225p' subfleet/cli.py
sed -n '432,495p' subfleet/cli.py
sed -n '2290,2400p' subfleet/cli.py
sed -n '3875,3890p' subfleet/cli.py
sed -n '145,169p' README.md
rg -n 'caller_family != "codex"|caller_family == "codex"|_session_writer_ok|unidentified-writer|def _resume_submission' subfleet/daemon.py
sed -n '1,145p' subfleet/review_subject.py
sed -n '335,395p' subfleet/store.py
sed -n '395,475p' subfleet/store.py
sed -n '3610,3685p' subfleet/daemon.py
sed -n '3945,4015p' subfleet/daemon.py
sed -n '2890,3005p' subfleet/daemon.py
rg -n '^def _writer_session|_writer_session_check|caller_family' subfleet/daemon.py | tail -n 15
rg -n '^def _require_review_capability|^def _review_caller_family|review_label|caller-family' subfleet/cli.py | head -n 18
```

A small Python read selected only the C-3.8, C-6.5, C-6.21 and C-6.22 clause lines after the broad initial `rg` output was truncated. The clauses matched the implementation; README family precedence, resume flag and runnable examples were reported to the parent, which made the documentation corrections.

Scratch ledger preparation (all successful, no tests run):

- Python `json.loads` reads of `ledger.jsonl` first listed CLI rows, then all rows, then rows 29 onward; counts were retained exactly, excluding the empty setup/teardown statistics key from case totals.
- `sed -n '1,210p' /private/tmp/sf-a2-fix1/run.py`; `sed -n '1,185p' /private/tmp/sf-a2-fix1/run4.py`; `sed -n '1,160p' /private/tmp/sf-a2-fix1/run-importer-batches.py`.
- `cat /private/tmp/sf-a2-fix1/schema-ledger.md`; `cat /private/tmp/sf-a2-fix1/importer-batches.json`; scratch `rg --files` discovery limited to launcher/ledger/importer filenames.
- Created and extended `render-ledger.py` with `apply_patch`; ran `python /private/tmp/sf-a2-fix1/render-ledger.py` after each extension. Successive snapshots rendered 30, 31, 32 and 35 completed command records with distinct basetemps. Its final rerunnable outputs include the command table, nonoverlapping aggregate, selected-module sidecar and summary.
- Read generated output using `sed -n '1,32p' ledger-table.md`, `tail -n 21 ledger-table.md`, `tail -n 1 ledger.jsonl`, `wc -l ledger-table.md render-ledger.py`, `cat ledger-summary.md`, and `sed -n '1,85p' ledger-aggregate.json` under the scratch root.

Failure-report preparation (read-only product inspection, no tests run):

```sh
sed -n '36,68p' /private/tmp/sf-a2-fix1/importer-batch-01.log
sed -n '3010,3085p' subfleet/daemon.py
sed -n '1,140p' /private/tmp/sf-a2-fix1/sf_fix1_safety.py
cat /private/tmp/sf-a2-fix1/importer-inventory-summary.txt
cat /private/tmp/sf-a2-fix1/batch-results.jsonl
rg -n -C 5 'source\["task"\].*review|review-fields|review_subject.*source' subfleet/daemon.py | tail -n 48
rg -n -C 5 'test_close_waits_for_a_read_in_progress_and_refuses_later_ones|FAILED|assert' /private/tmp/sf-a2-fix1/importer-batch-00.log | tail -n 55
rg --files /private/tmp/sf-a2-fix1 -g 'progress-*.jsonl' | head -n 15
```

The timing assertion and real resume guard regression are described separately in `cli-report-notes.md`. This follow-up changed only scratch evidence files. The parent owns final scratch cleanup; initial unlogged temporary-root deviations are listed in the generated command table.


Late safety audit and guarded continuation preparation:

- Read `importer-batches.json`, selected test/helper sources and existing progress JSONL only. Searches used `rg`, and the helper import-closure scan used `ast.parse`; no process inspection command ran for the audit. Repeated source/log reads were grouped. The selected closure was 334 direct modules plus 79 helper modules.
- Read `run.py`, `run4.py`, `sf_fix1_safety.py`, `tests/frontend/swift.py`, frontend conftest, `test_quar_start1_census.py` and `test_stopped_daemon.py` to identify subprocess boundaries, native premise nodes and fixture closures. A broad `rg` named nonexistent `subfleet/frontends.py` (exit 2); narrower source reads succeeded. Some broad source output was truncated, followed by narrower reads.
- Created `sitecustomize-proposal.py`, `guard-proof-data.py`, `install_guard.py`, `guard-removal.py`, and `safety-audit.md` in job scratch. Updated the two scratch launchers and safety plugin. Syntax checks used `ast.parse` only, before installation.
- `.venv/bin/python /private/tmp/sf-a2-fix1/install_guard.py` exited 0. It exclusively created the scratch, repository-root and project-venv `sitecustomize.py` copies and recorded their paths/hashes in `guard-installations.jsonl`. Source inspection established that guard bootstrap imports only bind process-reader symbols without calling them, then replace the native readers before tests.
- `.venv/bin/python /private/tmp/sf-a2-fix1/guard-proof-data.py` exited 0: 11 predicate/replaced-function proofs, 0 external commands executed. Forbidden command arrays were inert arguments to the guard predicate, never subprocess calls. The native proof called only the replaced refusal function.
- An attempted patch to replace a nonexistent sentence in `safety-audit.md` failed verification and changed no file. The subsequent explicit audit text update succeeded, including correction of duplicate controller/worker observations to one confirmed synthetic security execution and one denied ps attempt.
- Updated `render-ledger.py` to prefer the latest completed identical primary selection over an interrupted overlapping selection and to report guard environment skips separately. `.venv/bin/python /private/tmp/sf-a2-fix1/render-ledger.py` exited 0 and most recently rendered 39 distinct command/basetemp rows. Renderer runs execute no tests. Raw interrupted rows remain preserved in `ledger.jsonl`.
- `guard-removal.py` was parsed but not executed. It requires `--evidence-copied`, checks all manifest paths/hashes before unlinking any file, records removals, and leaves source/evidence intact. The parent owns final removal and generated bytecode cleanup.
- The parent reported an attempted nested `run-importer-batches.py 11` driver blocked by the guard before any tests: its uv subprocess was refused, no pytest basetemp was created, and no duplicate pytest row is counted. The parent continued with a direct uv launcher through the external tool boundary, preserving the strict guard.

No pytest run, host-state read, or process inspection was launched by this subagent during these late audit/guard tasks.


Guard V2 preparation (while V1 pool remained active):

- Preserved the installed V1 source as `guard-v1.py`; a direct byte comparison confirmed installed `sitecustomize.py` still matched it. No active guard copy or safety plugin was changed during the pool.
- Prepared V2 source in `sitecustomize-proposal.py`: recursively validate the inner command of exactly `/usr/sbin/taskpolicy -c utility`; require actual frontend output paths below job scratch/permitted basetemp, a successful compile/cache approval record, Mach-O signature and matching current SHA-256.
- Updated the uninstalled `frontend-conditional-plugin.py.txt` wrapper to append the actual successful returned binary path/hash. Existing compile helper retains app source paths and routes cache/module-cache into scratch.
- Prepared `install_guard.py --upgrade`: preflight latest recorded old hashes, log each hash transition before atomic replacement, and upgrade still-present recorded overlays as well as scratch/root/.venv. It leaves the current process's V1 audit hooks in place and requires a fresh next interpreter for V2. Removal helper already reads the latest row for each path.
- Extended `guard-proof-data.py` with seven V2 predicate-only checks (18 total under V2); this extended proof was parsed but not executed by this subagent.
- All changed preparation files were checked with `ast.parse`. No tests, upgrade installation, process reads or product edits ran for this preparation.


Additional read-only failure classification and exclusion preparation:

- Filtered controller `progress-38539.jsonl` and guard-denial JSONL for exact salvage, census lease, Round10 version and review-publish environment nodes; read only those test sources plus `guardian.linger`, retention Git/QoS and guard preflight definitions. No process state was inspected.
- Prepared `prepare-guard-exclusions.py --after-pool-stopped`, initially for nine, then ten exact opaque-fixture cases as proof accumulated. Added three exact native-guardian functions (11 collected cases) after source/logs showed an infinite denied-reader linger. The helper was AST parsed only; no active plugin file changed and the helper was not executed by this subagent.


Interrupted V1 reconstruction and parent-run V2 continuation:

- Read controller `progress-38539.jsonl`, four known worker progress files and `importer-batch-11.log` only. Reconstructed one manual ledger row: 4,765 passed, 188 failed, 109 skipped, 52 xfailed, 11 unfinished; worker collection count 5,125 each; observed progress span 834.90 seconds. The script asserted no existing batch11 row and matched controller failures with terminal F markers before appending. Created `importer-batch-11-observed-failures.json` with exact failed nodes/module counts; no process inspection or test execution occurred.
- `.venv/bin/python /private/tmp/sf-a2-fix1/render-ledger.py` exited 0 after reconstruction, rendering 40 unique command/basetemp rows. Updated renderer wording to label manual observed spans explicitly rather than treating them as launcher timing.
- Read the parent's V1/V2 guard proof result JSON and installation hash-chain manifest. Parent reported upgrade/exclusion preparation/proof exited 0, with V2 proof 18 checks and zero external commands. These were parent executions, not additional subagent tests.
- Added actual proof filenames, V1 source, exclusion helper, observed-failure JSON and optional frontend approval manifest to the scratch evidence copier. Parsed copier and renderer with AST only; no evidence copy/removal was executed by this subagent.


V2 remaining failure diagnosis and exact synthetic callback preparation:

- Read batch12 traceback/source and guard-denial locations for the six R5 failures. Captured warnings explicitly identify an external gh guard refusal. The fake_gh helper installs a shell executable; no native-reader inference was needed. A broad traceback search also matched the ledger line and printed its synthetic selected-file list; later reads filtered ledger lines and emitted only counts.
- One exploratory `rg` named nonexistent PR helper modules and exited2; a following definition search located query_prs directly in conversations/wakes.py. Read query_prs and the fake_gh/poll helpers to preserve the exact parser boundary.
- Read exact ledger rows41–43 without printing module lists: batch12=4977pass/90skip/52xfail/6fail903.43s; extras=82pass/107skip/1fail32.83s; mixed=99pass2.37s.
- Prepared scratch-only `sf_fix1_graphql.py` and `graphql-callback-proof.py`, then AST parsed both. The plugin is limited to two exact existing R5 functions and an exact synthetic read-only GraphQL response boundary. No executable/PATH is changed. No plugin import, inert proof, or tests were executed by this subagent.
- Added callback/plugin/proof-result/log filenames to the scratch evidence copier and AST parsed it. No active guard/product/repository-test source changed.


Final parent-run callback and contract outcomes:

- Parent ran the inert callback proof: six checks, zero external commands, exit0; artifact `graphql-callback-proof.json`.
- Parent's first full R5 recheck:11passed6failed0.58s launcher time. The scratch plugin had assumed `tests.unit.test_review_r5_repro` module name; replaced that qualification with an exact resolved file-path comparison and added static activation records. AST parsed the plugin only; no subagent tests ran.
- Parent's corrected R5 recheck:17passed0.50s (pytest summary0.42s), with all six callback cases exercised and no external gh execution.
- Parent's CLI reason check:330passed11.72s (pytest summary11.64s). The first required-final invariant had identified two missing reason fields in new ProtocolError constructions; parent added the registered refused-args reason.
- Read the final ledger row48: required four files447passed19.45s. Read-only verified actual callback/proof/log/direct-shell artifact names. Added `graphql-callback-proof.json` and `final-direct-02.sh` to the copier optional mapping; AST verified all seven requested callback/proof/direct artifacts are mapped and exist. No copier execution/removal or test run by this subagent.


Final evidence handoff:

- Renderer finalized48 commands with48 distinct basetemps. Latest disjoint whole-file focused green selections contain468 passes across15 files, including the17-case R5 callback recheck; the earlier corresponding selection had451 across14. Required final four files passed447 separately. These scopes are not added to primary invocation totals.
- Read-only callback audit confirmed12 static log records: six fixture activations and six synthetic read callbacks across six cases; every record reports0 external commands. Inert proof result confirms6 checks/0 external commands.
- Created compact `guard-case-results.json`: exact seven raw baseline-failure nodes, six resolved by the17-pass callback recheck, one installed-publish-hook environment limitation retained, plus final447-pass reference. Added it to the optional copier map. No full test/provider logs were copied by this subagent.
- Copier optional map now includes the actual `graphql-callback-proof.json`, callback source/proof/log, both guard-proof result files, and `final-direct-02.sh`. AST parsing and path-existence checks passed. All scratch preparation writes end at this handoff; the parent owns evidence copy and cleanup.
