# CI r1 command inventory

The first shell command began with `date -u` and returned **2026-11-13 12:49:02 UTC**.
The deadline is **14:49:02 UTC**. All shell scopes are the assigned worktree,
explicitly supplied review inputs, repository Git objects/metadata, or job scratch.
No live-state directory or process was inspected. Tool session polls and bounded
clock sleeps only waited on this job's commands.

## Setup and reads

| Commands (repeated status reads grouped) | Result |
|---|---|
| `date -u; pwd; git status --short; git branch --show-current; cat /Users/maxghenis/reviews/subfleet-hub/sandbox-hold-2026-11-10/ci-309-py312-shard3.fail.txt; cat /Users/maxghenis/reviews/subfleet-hub/sandbox-hold-2026-11-10/brief-build.md; cat docs/reports/2026-11-12-sandbox-hold.md` | Exit 0; correct workspace, clean detached `ea28630b5`. Default Git reads were slow; the read session was polled to completion. |
| Separate `cat` of the same three requested inputs, with the brief and CI/report reads batched independently. | Exit 0; Rules and full failure chains read before implementation. |
| `git --no-optional-locks -c core.fsmonitor=false status --short`; `log -1 --oneline`; `rev-parse HEAD`; `diff --quiet -- subfleet`; `diff --check`; scoped `diff -- tests/unit/test_host_push_build_budget.py tests/unit/test_host_push_store_completion_pr183.py`. | Exit 0; clean baseline, full head `ea28630b51603439fa4a402f6390916ef5f0dbf6`, production unchanged, no whitespace errors. |
| `rg --files -g AGENTS.md -g pyproject.toml -g uv.lock -g conftest.py -g '*scratch*' -g '.gitnexus*'`; `cat AGENTS.md`; `cat pyproject.toml`; `cat .gitignore`; `cat tests/unit/conftest.py`; `sed -n '1,260p' tests/conftest.py`. | Reads succeeded except `cat AGENTS.md` (absent). No worktree AGENTS file was found. The batch continued to successful reads. |
| `getconf DARWIN_USER_TEMP_DIR`; `command -v uv`; `/private/tmp/sf-sbh-c1/venv/bin/python --version`. | Exit 0; Darwin temp `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`; uv available; final test interpreter CPython 3.12.14. |
| `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"; mkdir -p "$TMPDIR/sf-sbh-c1"`; `mkdir -p /private/tmp/sf-sbh-c1`; `mkdir -p docs/reports/2026-11-12-sandbox-hold-ci-r1-data`. | Default temp child denied; permitted fallback and temporary evidence destination created. |
| `cp docs/reports/2026-11-12-sandbox-hold-data/pytest_scratch.py /private/tmp/sf-sbh-c1/pytest_scratch.py`; Python replaced the old scratch marker and installed guarded external-fixture deletion logging; `cp` preserved that plugin in evidence. | Exit 0. The plugin redirects explicit `/tmp` fixture allocations and failed-state pruning, records temp directories, and asserts `sf-sbh-c1` before deleting recorded external fixture roots. |
| `cat docs/reports/2026-11-12-sandbox-hold-data/run_selected.py`; `sed -n '1,260p' docs/reports/2026-11-12-sandbox-hold-data/pytest_scratch.py`. | Exit 0; reused the established isolation convention. |
| `TMPDIR=/private/tmp/sf-sbh-c1 UV_CACHE_DIR=/private/tmp/sf-sbh-c1/uv-cache UV_PROJECT_ENVIRONMENT=/private/tmp/sf-sbh-c1/venv UV_PYTHON_INSTALL_DIR=/private/tmp/sf-sbh-c1/python uv --cache-dir /private/tmp/sf-sbh-c1/uv-cache sync --group dev --extra api`. | uv refused the absent `api` extra (exit 2); `sync-api.log`. It selected existing Python 3.14 freethreaded before the explicit 3.12 pin. |
| Same environment and cache command, `sync --group dev`; then `sync --group dev --python 3.12`; then the 3.12 command again after both completed. | Successful fallback, 9 packages installed; final verification checked 9 packages. Logs `sync-dev.log`, `sync-py312.log`, `sync-py312-verify.log`. The first two fallback syncs overlapped; final sequential verification established the environment before any pytest run. |
| `rg -n HYPOTHESIS_STORAGE_DIRECTORY /private/tmp/sf-sbh-c1/venv/lib/python3.12/site-packages/hypothesis`; `rg -n 'uv|python-version' .github/workflows/tests.yml`; `rg -n '^\[\[|^python|^resolution|^required-version' uv.lock` (first 15 lines). | Exit 0; verified scratch database environment support and CI Python/dev setup. |

## Implementation inspections

The independent caller/stand-in audit has its exact search expressions, read ranges,
classification, and results in `standin-audit.md`. The new-test author's additional
reads were scoped to daemon push/hold methods, Store/schema, task_hold_support,
host_push fixtures and sandbox contracts; all succeeded. Its scoped AGENTS search
had no matches. A second review read the new test, budget lines 168–221, Rows lines
67–116, daemon lines 6288–6370 and Store lines 400–449 (all exit 0, no issues).

Root's additional reads, all successful unless noted:

- `sed -n '1,260p' tests/unit/test_host_push_build_budget.py`; `sed -n '1,220p' tests/unit/test_host_push_store_interleavings.py`; `sed -n '1,240p' tests/unit/test_host_push_store_completion_pr183.py`; `sed -n '1,200p' tests/unit/test_host_push.py`.
- `sed -n '1720,1785p'`, `'1810,1850p'`, `'2070,2110p'`, `'6120,6220p'`, `'6300,6390p'` on `subfleet/daemon.py`; `sed -n '235,260p' subfleet/task_holds.py`; `sed -n '1,140p'` and `'320,335p'` on `subfleet/store.py`; `rg -n -A45 'class Store|def transaction|def get_job' subfleet/store.py`.
- `rg -n -A10 -B5 'generated_preparation_pass|busy_store_completion'` on the two interleaving files; `rg -n 'def _job|def _fail_queued|def _refused_job' subfleet/daemon.py`; scoped `rg -n` for Rows imports, schemas, inserts and queries in completion, interleavings and retry-budget files.
- `rg -n -A100 -B8 'KeyError: .0.|AttributeError|sqlite3.OperationalError|FAILURES|passed|failed'` on the supplied CI file; `sed -n '310,515p'` on that file exposed both primary missing-column errors and secondary exceptions.
- `cat tests/unit/test_host_push_sandbox_hold.py`; `sed -n '1,190p' tests/unit/test_host_push_storage_retry_budget.py`; `sed -n '1,120p' tests/unit/test_reset_credit_claim_timing.py`; `sed -n '280,620p' tests/unit/test_host_push_store_interleavings.py`.
- `rg -n -A20 'class .*Store|class .*Connection|def execute|def transaction' tests/unit/test_reset_credit_claim_timing.py` found no matching stand-in definition (exit 1); the later full read confirmed wrappers with differently named functions.
- `rg -n -A50 'def transaction_boundary|def after_writer_wait|def import_before_claim'` on probe_cancel, probe_deadline and timers_reset_credits; scoped `rg` for query/schema/task fields and final changed line anchors in the three changed test files.
- Repeated `tail -n 3/5/6/8/10` on owned logs, `date -u`, `wc -l` of original files/selected inventory; `du -sh /private/tmp/sf-sbh-c1/pytest-repro-build-budget`: successful progress reads only.

## Inventories and checks

- Python generated `named-tests.txt` (45), `hold-reference-tests.txt` (23) and their union plus the four audit wrappers, `selected-tests.txt` (55). `rg --files tests/unit tests/fake -g '*host_push*.py' -g '*task_hold*.py' -g '*sandbox_hold*.py' -g '*pin*.py'` produced `named-tests-rg.txt`; Python asserted the recursive inventory exactly matches all 45 names (exit 0).
- Python AST comparisons against `git show ea28630b5:<file>` verified all **176 assertions**, **5 settings calls**, and **5 generated-input decorators/strategy bounds** are unchanged in the three original files. `assertion-audit.json` records per-file counts and unchanged production (exit 0).
- Structured `rg -n 'xfail|skip' <55 selected paths>` wrote `expected-outcomes.txt` (exit 0): opt-in matrix/benchmark and filesystem prerequisites are existing skips; admission_properties contains the existing strict expected failure.
- `apply_patch` added the real-store test and validation/evidence scripts, and repaired only the two shared fixture files; no production edits. All patch calls succeeded.

## Tests and commits

`run_tests.py` records every fully expanded pytest command, explicit environment,
exit status, and outer elapsed time in `<label>.command.json`, with full output in
`<label>.log`. Each invocation uses a fresh scratch basetemp; the final report gives
its pytest counts/time. The three baseline files ran serially and finished before
fixture repair. The three focused post-fix processes overlapped (three workers
total); their files/state were independent. The broader run uses `-n 4`.

`git -c core.fsmonitor=false switch -c fix/sandbox-hold-ci-r1` failed at the parent
ref lock (exit 128), captured in `branch.log`. `git_step.py` attempts named-file
staging followed by a named-file-only commit only if staging succeeds. The witness
and fixture steps both failed staging at the assigned checkout's `index.lock`
(exit 128), captured in `commit-witness.json` and `commit-fixtures.json`; no history
was changed. The report records the final named-file attempt and cleanup result.

## Follow-up review and validation inventory

The independent report review used the following read commands, all exit 0,
with no tests, edits, or commits during that review:

- Python `pathlib` read `docs/reports/2026-11-12-sandbox-hold.md` and printed the
  substring from `s.index('## CI fix r1')` onward.
- `cat docs/reports/2026-11-12-sandbox-hold-ci-r1-data/root-commands.md` and
  `cat docs/reports/2026-11-12-sandbox-hold-ci-r1-data/standin-audit.md`.
- `cat docs/reports/2026-11-12-sandbox-hold-ci-r1-data/assertion-audit.json`;
  `cat docs/reports/2026-11-12-sandbox-hold-ci-r1-data/run_tests.py`;
  `sed -n '178,225p' tests/unit/test_host_push_build_budget.py`;
  `sed -n '73,118p' tests/unit/test_host_push_store_completion_pr183.py`.
- Python `pathlib` read `selected-tests.txt`, `named-tests.txt`, and
  `hold-reference-tests.txt` beneath the same evidence directory and printed
  `len(lines), len(set(lines))`: respectively **55/55**, **45/45**, **23/23**.

The first two read groups and last two read groups ran as independent
`Promise.allSettled` batches; every result was inspected. The review found no
factual inconsistency in the causes, fixture repairs, preserved assertions or
bounds, baseline counts, or file inventories. It requested explicit disclosure
that Python 3.14 and remote CI were not rerun, and exact final evidence paths.

Subsequent root commands and results:

| Command / evidence | Result |
|---|---|
| `py_compile` of seven Python files, recorded in `compile-check.json`. | Exit 0; all seven compiled. |
| Selected 55-file run, `-n 4`, recorded in `selected.command.json` and `selected.log`. | Exit 1: **1 failed, 1310 passed, 7 skipped, 1 xfailed**, **3119.97 s**. |
| `sed -n '24,205p'` on the owned `selected.log`; `sed -n '175,245p' tests/unit/test_host_push.py`; `sed -n '370,407p' tests/unit/test_host_push_store_interleavings.py`. | Exit 0; isolated the additional failure to initial repository setup. |
| `rg` on `subfleet/host_push.py` for the timeout/Git expression quoted below. | Exit 0; inspected the Git call timeout. |
| `rg` on `tools/ci_retry_known_flakes.py` and `.github/workflows/tests.yml` for the timeout/pytest/subprocess expression quoted below. | Exit 0; inspected existing retry handling and the CI timeout of 1200 s. |
| Python updated the draft report section twice. | Exit 0 both times. |
| Repeated `tail` reads of owned validation logs and `date -u`. | Exit 0; progress/time checks only. |

The two root `rg` search expressions were `GIT_TIMEOUT|TIMEOUT|def git` and
`timeout|pytest|subprocess`, respectively.

The sole additional selected-run failure is
`test_restart_finds_unrecorded_completion_after_busy_ownership[False]`. Its
initial `submit` entered quarantine setup and `git init` exceeded
`TIMEOUT_S=60`, before the changed hold query was reached. Production code is
unchanged. A serial retry of the complete
`tests/unit/test_host_push_store_interleavings.py` file (**26 tests**) is running;
its command/result record is `retry-store-interleavings.command.json`. This
inventory does not claim that retry has passed.

The required contract bundle is also running with `-n 3` against the same four
files named in the brief: `tests/fake/test_state_contract.py`,
`tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py`, and
`tests/unit/test_handler_refusals.py`. It runs alongside the single-worker
interleavings retry, for at most four workers total. Results remain pending at
this inventory update.

The audit agent's failed artifact read and interrupted read details are retained
in `standin-audit.md`. After evidence collection, final cleanup and named-file
staging results will be recorded in `ci-r1-cleanup.json` and
`ci-r1-commit-final.json`. This follow-up inventory update changes only this
Markdown command inventory; it runs no tests and creates no commit.

Final outcomes before cleanup: contract bundle completed with 427 passed in 1133.24 s (wrapper elapsed 1176.61 s), exit 0. The serial interleavings repeat showed 23 passing markers, including both restart cases, before interruption for the two-hour deadline. Tool stdin Ctrl-C ended its wrapper with KeyboardInterrupt/exit 130; no final pytest summary was emitted. A subsequent signal attempt scoped solely to this job's exact retry basetemp (`/usr/bin/pkill -INT -f -- /private/tmp/sf-sbh-c1/pytest-retry-store-interleaving[s]`) failed with exit 3 because sysmond/process-list access is unavailable; no process list was returned. No test timeout or bound changed. The complete broad run remains reported as 1 failed / 1310 passed / 7 skipped / 1 xfailed.

Final pre-cleanup reads: `cat cleanup.py`, `tail` retry log/root inventory/report, `cat contract-bundle.command.json`, `cat run_tests.py`, `cat pytest_scratch.py`, `sed -n /## Rules/,$p` of the supplied brief, `date -u`: exit 0. Final Python retry-outcome and inventory update: exit 0. Cleanup command: `python3 docs/reports/2026-11-12-sandbox-hold-ci-r1-data/cleanup.py`; its full result/counts follow in ci-r1-cleanup.json. Final report edits, diff/status/head checks and named-file staging follow in the report and ci-r1-commit-final.json.
