# Fix round 2: exclusions required by the command restrictions

The brief prohibits executing `git push` and the listed GitHub write commands even behind stubs. The selected importer tests contain real local pushes and one stub push. Passing their source text to search tools was read-only; no test or prohibited command was run by this audit.

## Required exclusions

Ignore these nine selected files:

1. `tests/unit/test_host_push.py`
2. `tests/unit/test_host_push_admission.py`
3. `tests/unit/test_host_push_build_budget.py`
4. `tests/unit/test_host_push_mutations.py`
5. `tests/unit/test_host_push_restart.py`
6. `tests/unit/test_host_push_storage_retry_budget.py`
7. `tests/unit/test_host_push_store_completion_pr183.py`
8. `tests/unit/test_host_push_store_interleavings.py`
9. `tests/unit/test_host_push_validated_top.py`

All nine define or import and use `test_host_push.worlds`. Calling the fixture's returned context manager executes a real local `git push` before yielding (`test_host_push.py:89`). The helper at lines 37–50 forwards its arguments to `subprocess.run`; the fixture's local file remote does not change the brief's prohibition. Several tests also execute production host-push paths or additional direct pushes. Whole-file exclusions also omit some pure unit tests in those files; that conservative scope is recorded rather than claiming that every test in each file executes a push.

`tests/unit/test_host_push.py::test_git_timeout_stops_the_transport_process_group` is additionally prohibited without `worlds`: it substitutes a shell script for Git and calls `host_push.git(..., "push", ...)` at line 695. The brief explicitly prohibits executing the command behind a stub; its shell script also starts a background sleep.

Deselect this individual test in another selected file:

```text
tests/unit/test_retention_archive.py::test_a_remote_that_holds_only_unrelated_history_counts_as_none
```

It calls the real Git helper with `push` at line 1892 to seed a local bare repository. The remainder of this file can remain selected.

`tests/unit/test_host_push_quarantine_restart.py` can remain selected: the inspected code covers startup/quarantine cleanup, does not import `worlds`, and contains no push execution.

The resulting run selects **229 of the original 238 files**, with the one retention test additionally deselected. These exclusions are an explicit deviation from running every importer file, required by the stronger command restriction. The parent stopped the previous importer run while its log still showed node startup and no executed-test progress, then prepared this restricted selection; the parent's command ledger records that run's outcome.

## Audit result and limits

Source searches across all 238 selected files found the concrete push executions above. Searches included command strings, separated argv literals (`push`, `POST`, `PATCH`, `DELETE`, `merge`, `rerun`), case-insensitive command spellings, and publish-wrapper/guard references. Relevant publish-launch and guard helpers were inspected. No other literal execution of the brief's forbidden GitHub write commands was found in those selected files. Guard payloads passed as data and source-inspection assertions are allowed. This is a focused source audit, not a claim that arbitrary dynamically assembled commands have been exhaustively proved absent.

## Read-only command ledger

All 13 shell command invocations below ran in the assigned workspace and exited 0. They only read source and the already preserved importer list. Tool polling did not start additional shell commands. No tests ran in this audit, and this evidence file is its only write.

Initial selected-file search:

```sh
python3 -u - <<'PY'
import subprocess
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
pattern=r'gh.{0,80}(api|merge|rerun)|git.{0,20}push|send-pack|http-push'
r=subprocess.run(['rg','-n',pattern,*files],text=True,capture_output=True)
print(r.stdout)
PY
```

Result: located the direct host-push helper calls, the retention push, and unrelated refusal text. The initial findings were sent to the parent immediately so the importer run could be stopped.

Focused fixture and retention reads:

```sh
sed -n '1,180p' tests/unit/test_host_push.py
sed -n '1840,1910p' tests/unit/test_retention_archive.py
sed -n '1,135p' tests/unit/test_host_push_restart.py
```

Result: confirmed the unconditional local push inside `worlds()` and the exact retention test name.

Separated GitHub argv/publish-helper search:

The command is kept byte for byte in [write-command-exclusions-publish-search.txt](write-command-exclusions-publish-search.txt) (fix round 3 moved it there, because the hygiene test reads its regex as a markdown link).

Result: only AST/source assertions referring to `wrapper_main`; no further executing GitHub command found.

Independent enumeration of the selected host-push test modules and their fixtures:

```sh
python3 -u - <<'PY'
import ast,json,subprocess
from pathlib import Path
selected=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
for name in selected:
 text=Path(name).read_text()
 if 'test_host_push' in text or name.endswith('/test_host_push.py'):
  tree=ast.parse(text)
  print(name)
  for node in tree.body:
   if isinstance(node,(ast.Import,ast.ImportFrom)) and 'test_host_push' in (ast.get_source_segment(text,node) or ''):
    print('  import:',ast.get_source_segment(text,node))
   if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'):
    print('  test:',node.name,'args:',','.join(a.arg for a in node.args.args))
PY
```

Result: identified the nine selected files listed above. Direct and imported `worlds` cases are included; whole-file exclusion avoids missing fixture dependencies and mutation helpers.

Focused additional fixture/stub inspection:

```sh
rg -n '^(from|import)|^def test_|^@pytest.fixture|world|push' tests/unit/test_host_push_quarantine_restart.py tests/unit/test_host_push_build_budget.py tests/unit/test_host_push_storage_retry_budget.py
sed -n '660,710p' tests/unit/test_host_push.py
```

Result: quarantine-restart tests are startup cleanup only; the timeout test really invokes the substituted Git command with `push`.

Separated write-verb search across the selected files:

```sh
python3 -u - <<'PY'
import subprocess
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
r=subprocess.run(['rg','-n',r'"push"|\x27push\x27|"POST"|\x27POST\x27|"PATCH"|\x27PATCH\x27|"DELETE"|\x27DELETE\x27|"rerun"|\x27rerun\x27|"merge"|\x27merge\x27',*files],text=True,capture_output=True)
print(r.stdout)
PY
```

Result: remaining matches were synthetic state shapes, SQL verbs, stub HTTP request methods, Git merge data and the already identified push cases; no additional forbidden GitHub execution found.

Relevant publish/guard review:

```sh
sed -n '1,165p' tests/fake/test_review_launch_permissions.py
sed -n '1,155p' tests/unit/test_review_publish_env_round8.py
sed -n '1,150p' tests/unit/test_publish_trust_refusal_round8.py
python3 -u - <<'PY'
import subprocess
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
r=subprocess.run(['rg','-ni',r'gh.{0,100}(api|merge|rerun)|git.{0,40}push|test_(review_)?publish|publish_rule|review_publish|review_launch_permissions',*files],text=True,capture_output=True)
print(r.stdout)
PY
```

Result: inspected launch construction, fake guardian execution and guard-refusal tests. No additional literal execution of the forbidden GitHub commands was found. The parent received the final nine-file/one-test exclusion list before restarting the restricted importer run.
