# Fix round 2: independent import audit

This subagent performed read-only review in the assigned worktree. It ran no tests, providers or live-state commands. Its only write is this evidence file. All commands below used the assigned worktree as their working directory. Tool polling of command sessions did not start additional shell commands.

## Final selection

The preserved `daemon-import-tests.txt` contains **235** paths. An independent Python AST walk of tracked test sources found exactly the same **235** `test_*.py` files with an `import subfleet.daemon`, `from subfleet.daemon import ...`, or `from subfleet import daemon` statement, including nested and comma-list imports: **zero missing, zero extra**. None of those actual imports spans multiple lines.

This Git ERE selects **238** test files, including all 235 actual importers:

```sh
git grep -l -E 'from[[:space:]]+subfleet[.]daemon[[:space:]]+import|import[[:space:]]+subfleet[.]daemon|from[[:space:]]+subfleet[[:space:]]+import[[:space:]]+([^#]*[^[:alnum:]_])?daemon([^[:alnum:]_]|$)' -- 'tests/**/test_*.py' 'tests/test_*.py'
```

The three additional textual matches contain embedded source imports rather than actual AST imports:

- `tests/fake/test_launch_gates_above_stdio.py`
- `tests/unit/test_daemon_fifo_reads.py`
- `tests/unit/test_stop_alarm.py`

All three actual importers of the changed test module `tests.fake.test_resume_contract` are already in both selections:

- `tests/fake/test_admission_priority.py:58,594`
- `tests/fake/test_census_round3_interleavings.py:33`
- `tests/fake/test_census_round3_leases.py:23`

Eight non-`test_*.py` helper sources also have actual daemon imports: `tests/conftest.py`, `tests/fake/run_daemon.py`, `tests/frontend/daemon_harness.py`, `tests/frozen_streams.py`, `tests/lane_settings_cases.py`, `tests/task_hold_cas_legacy.py`, `tests/task_hold_mutants.py`, and `tests/task_hold_support.py`. They are helpers or fixture modules, not additional test modules selected by filename.

## Exact comparison commands

These scripts only read source files and Git's tracked-file view. All exited 0. Their subprocess calls are included verbatim.

The initial ERE experiment and independent AST comparison:

```sh
python3 - <<'PY'
import ast, json, subprocess
from pathlib import Path
pattern = r'(^|[[:space:];])(from[[:space:]]+subfleet\.daemon[[:space:]]+import|import[[:space:]]+subfleet\.daemon([[:space:],;]|$)|from[[:space:]]+subfleet[[:space:]]+import[[:space:]]+.*\bdaemon\b)'
args = ['git', 'grep', '-l', '-E', pattern, '--', 'tests/**/*.py', 'tests/*.py']
candidates = set(subprocess.check_output(args, text=True).splitlines())
test_candidates = {f for f in candidates if Path(f).name.startswith('test_')}
preserved = set(Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines())
tracked = subprocess.check_output(['git', 'ls-files', '--', 'tests/**/*.py', 'tests/*.py'], text=True).splitlines()
actual = set()
for f in tracked:
    try:
        tree = ast.parse(Path(f).read_text())
    except SyntaxError:
        continue
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == 'subfleet.daemon' for a in n.names):
            actual.add(f)
        elif isinstance(n, ast.ImportFrom) and (n.module == 'subfleet.daemon' or n.module == 'subfleet' and any(a.name == 'daemon' for a in n.names)):
            actual.add(f)
actual_tests = {f for f in actual if Path(f).name.startswith('test_')}
print(json.dumps({'grep_command':args,'grep_test_count':len(test_candidates),'preserved_count':len(preserved),'grep_missing_from_preserved':sorted(test_candidates-preserved),'preserved_extra_to_grep':sorted(preserved-test_candidates),'ast_test_count':len(actual_tests),'ast_missing_from_preserved':sorted(actual_tests-preserved),'preserved_extra_to_ast':sorted(preserved-actual_tests),'ast_non_test_importers':sorted(actual-actual_tests),'grep_non_test_importers':sorted(candidates-test_candidates)},indent=2))
PY
```

Result: AST235 exactly matched the preserved list. This initial ERE selected 178 test paths: it missed 60 actual importers and added the three embedded-source matches. Its `\b` alternative was unsuitable here; the final ERE uses explicit token delimiters.

A smaller diagnostic grep:

```sh
python3 -u - <<'PY'
import subprocess
print('begin', flush=True)
r = subprocess.run(['git', 'grep', '-l', '-E', 'subfleet[.]daemon|from subfleet import.*daemon', '--', 'tests'], text=True, capture_output=True, timeout=30)
print(r.returncode, len(r.stdout.splitlines()), flush=True)
print(r.stderr, flush=True)
PY
```

Result: Git exit 0, 262 paths over all of `tests`, empty stderr.

The broader candidate selection and multiline-import audit:

```sh
python3 -u - <<'PY'
import ast, json, subprocess
from pathlib import Path
pattern = r'from[[:space:]]+subfleet[.]daemon[[:space:]]+import|import[[:space:]]+subfleet[.]daemon|from[[:space:]]+subfleet[[:space:]]+import[^#]*daemon'
command = ['git', 'grep', '-l', '-E', pattern, '--', 'tests/**/test_*.py', 'tests/test_*.py']
selected = set(subprocess.check_output(command,text=True).splitlines())
preserved = set(Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines())
print(json.dumps({'command':command,'count':len(selected),'missing':sorted(preserved-selected),'extras':sorted(selected-preserved)},indent=2),flush=True)
multiline=[]
for name in sorted(preserved):
    body=Path(name).read_text()
    tree=ast.parse(body)
    for node in ast.walk(tree):
        matches=(isinstance(node,ast.Import) and any(a.name=='subfleet.daemon' for a in node.names) or isinstance(node,ast.ImportFrom) and (node.module=='subfleet.daemon' or node.module=='subfleet' and any(a.name=='daemon' for a in node.names)))
        if matches and node.end_lineno > node.lineno:
            multiline.append([name,node.lineno,ast.get_source_segment(body,node)])
print(json.dumps({'multiline_imports':multiline},indent=2),flush=True)
PY
```

Result: 246 selected, zero missing, 11 extras; no multiline imports. Besides the final three extras, the broad `daemon` substring added `test_daemon_log_cut_windows.py`, `test_daemon_log_first_pass.py`, `test_daemon_log_reminders.py`, `test_daemon_log_tail.py`, `test_daemon_log_trailing_reminders.py`, `test_launch_env_round7.py`, `test_scrub_release_first.py`, and `test_scrub_review_r2.py` under `tests/unit/`.

The final exact ERE comparison and changed-test importer check:

```sh
python3 -u - <<'PY'
import json,subprocess
from pathlib import Path
pattern=r'from[[:space:]]+subfleet[.]daemon[[:space:]]+import|import[[:space:]]+subfleet[.]daemon|from[[:space:]]+subfleet[[:space:]]+import[[:space:]]+([^#]*[^[:alnum:]_])?daemon([^[:alnum:]_]|$)'
selected=set(subprocess.check_output(['git','grep','-l','-E',pattern,'--','tests/**/test_*.py','tests/test_*.py'],text=True).splitlines())
preserved=set(Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines())
print(json.dumps({'count':len(selected),'missing':sorted(preserved-selected),'extras':sorted(selected-preserved)},indent=2),flush=True)
imports=subprocess.run(['git','grep','-n','-E','(^|[[:space:]])(from[[:space:]]+tests[.]fake[.]test_resume_contract[[:space:]]+import|import[[:space:]]+tests[.]fake[.]test_resume_contract)','--','tests'],capture_output=True,text=True)
print(imports.stdout,flush=True)
PY
```

Result: **238 selected, zero missing, exactly the three extras listed above**. Changed-test importer search found exactly the three modules and four import statements listed above.

## Other read-only command ledger

These are the remaining shell commands this subagent executed, grouped by purpose. Each line is one command invocation. All exited 0 unless noted. Some broad searches and the combined contract diff were output-truncated by the tool; later focused reads supplied the relevant text.

Initial source, test and contract exploration:

```sh
rg -n "def _resume_submission|C-12\.4|effort|_resolve" subfleet/daemon.py docs/reports/2026-11-12-effort-env.md docs tests -g '*contract*' -g '*invariant*' -g '*effort*' -g '*changelog*' -g '*CHANGELOG*'
sed -n '3560,3740p' subfleet/daemon.py
sed -n '9630,9890p' subfleet/daemon.py
sed -n '1,155p' tests/fake/test_resume_contract.py
sed -n '1,24p' docs/acceptance-contract.md
sed -n '565,585p' docs/acceptance-contract.md
sed -n '9818,9872p' subfleet/daemon.py
sed -n '80,140p' tests/fake/test_resume_contract.py
rg -n 'model_requested|model_short|model_alias|pinned_model' subfleet/daemon.py subfleet/store.py subfleet/schema.sql subfleet/scheduler.py subfleet/protocol.py
rg -n '^\- \*\*C-12\.4\*\*|^- C-12\.4, C-26\.8' docs/acceptance-contract.md
sed -n '3150,3215p' subfleet/daemon.py
sed -n '9775,9800p' subfleet/daemon.py
rg -n 'def resolve_model|def finished_source|def resume_job|legacy_resume|model_short' subfleet/policy.py tests/fake/test_resume_contract.py tests/fake/test_state_contract.py tests/fake/conftest.py
sed -n '935,982p' subfleet/policy.py
sed -n '248,289p' tests/fake/test_resume_contract.py
```

The `rg` command naming `subfleet/schema.sql` exited **2**, because that path does not exist; it still returned matches from the other five files. Findings from these reads: attempt evidence records `model_short`; `_launch` reads that alias's current policy entry; legacy imported-resume coverage deliberately supplies evidence without that key; the provider-ID identity check remains separate from the alias.

Import-selection exploration before the exact comparisons:

```sh
git grep -l -E '(from subfleet(\.| )daemon import|from subfleet import daemon|import subfleet\.daemon|from \.test_state_contract import)' -- tests
git grep -n -E '(from subfleet(\.| )daemon import|from subfleet import daemon|import subfleet\.daemon)' -- tests/unit/test_daemon_init_release.py tests/fake/run_daemon.py tests/conftest.py tests/e2e/conftest.py tests/unit/test_protocol_secrecy.py
rg --files docs/reports/2026-11-12-effort-env-data/fix-r2 tests | rg '(daemon-import-tests\.txt|\.py$)'
git grep -n -E '(^|[[:space:];])(from[[:space:]]+subfleet[[:space:]]+import|from[[:space:]]+subfleet\.daemon[[:space:]]+import|import[[:space:]]+subfleet\.daemon)' -- 'tests/**/*.py' 'tests/*.py'
cat docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt
```

These exploratory commands located the preserved list and showed the need to include nested and comma-list imports; the independent comparisons above supply the authoritative counts.

Review of the proposed fix, acceptance wording and request-digest compatibility:

```sh
git diff -- subfleet/daemon.py docs/acceptance-contract.md tests/fake/test_resume_contract.py
git log -4 --oneline
sed -n '2980,3085p' subfleet/daemon.py
sed -n '3230,3335p' subfleet/daemon.py
rg -n 'def _accepted_request|request_digest|request_id.*digest|accepted.*digest|resume.*digest|retry.*upgrade' subfleet/daemon.py tests/fake/test_resume_contract.py tests/unit/test_daemon_accept.py docs/acceptance-contract.md
python3 - <<'PY'
from pathlib import Path
p=Path('docs/acceptance-contract.md')
for line in p.read_text().splitlines():
 if line.startswith('- **C-12.4**'):
  start=line.index('When a launch names an effort')
  end=line.index('For the effort variable',start)
  print(line[start:end])
PY
sed -n '1,85p' tests/fake/test_resume_contract.py
sed -n '290,309p' tests/fake/test_resume_contract.py
rg -n 'def payload_digest|def update_job|payload_digest=' subfleet/ids.py subfleet/store.py tests/fake/test_resume_contract.py tests/unit/test_daemon_accept.py tests/fake/test_state_contract.py
sed -n '104,176p' subfleet/ids.py
sed -n '93,110p' subfleet/ids.py
sed -n '886,918p' subfleet/store.py
sed -n '285,292p' tests/fake/test_resume_contract.py
```

`git log` showed HEAD `4fd796b49` when reviewed. This review found one concrete compatibility concern in the alias-only intermediate: an identical request accepted before the fix could acquire a different model component in its digest after upgrading. The parent was advised to preserve the previous provider-ID normalization for the resume digest while storing and routing through the source alias. The existing four-case regression can additionally install the legacy digest and replay the exact `SubmitArgs`; it still fails on the original code's effort assertions and catches the intermediate retry problem. The helper review confirmed that `payload_digest(prompt, **fields)` supports this capture and that `Store.update_job` permits replacing the isolated fixture's digest.

No tests were run by this subagent. No source reads were performed after the parent announced that `daemon.py` was temporarily restored to baseline for the final regression.

## Final restored-source review

After the parent announced that the final source was restored, these two read-only commands exited 0:

```sh
git diff -- subfleet/daemon.py tests/fake/test_resume_contract.py
python3 - <<'PY'
from pathlib import Path
for line in Path('docs/acceptance-contract.md').read_text().splitlines():
 if line.startswith('- C-12.4, C-26.8'):
  print(line)
 if line.startswith('- **C-12.4**'):
  start=line.index('When a launch names an effort')
  end=line.index('For the effort variable',start)
  print(line[start:end])
PY
```

Result: no remaining concrete concern. The source retains the attempt alias for routing while using the previous provider-ID normalization in the resume digest. The four-case regression installs the legacy digest and replays the exact `SubmitArgs`, then checks native launch and effort. The contract states current-entry effort and accepted-retry compatibility.

## Startup/collection source audit

While the parent investigated a broad importer run with no test progress, this subagent inspected source only. It did not run collection, tests, subprocess probes or process inspection. These **18** additional shell command invocations all exited 0. Session polling added no commands.

Findings:

- Repository conftests contain no collection hooks or import-time provider/network startup. `pyproject.toml` has no collection/plugin `addopts`.
- `tests/conftest.py` installs isolation in `pytest_configure`; `pytest_sessionstart` calls `tests.failed_keep.prune`. `prune(root=None)` reads the module's `ROOT` dynamically (`root = ROOT if root is None else root`), so the parent's safety-plugin redirection applies. The external default path was not inspected.
- Swift compilation and fake daemon startup are fixtures, so they do not explain a run that has not reached fixture-created worker directories.
- Static top-level-call inspection of the 229 safe files found ordinary strategy/parametrization construction, small policy loads, synthetic records and committed-source compilation. It found no import-time subprocess/network execution in the selected source.
- `tests/unit/test_launch_env_round8.py:57` initially looked suspicious because it calls `release_build_environment()` at import. Inspection ruled this out: `tests/fixtures/launch_env_release_217_env2.py` compiles a short committed string with current helpers and returns its function. It performs no Git command, archive extraction, dependency install, temporary allocation or external IO, and provides no cache knob.
- No concrete source-level explanation for the long startup was identified. Splitting into smaller batches with fewer workers was recommended to bound duplicate collection/memory and obtain completed batches before the deadline; collection/memory contention was an inference, not a measured diagnosis. The parent subsequently started two 46-file chunks with two workers each.

Initial configuration/fixture search and reads:

```sh
rg -n 'pytest_|autouse|scope=.session|session.*scope|subprocess|uv|bootstrap|timeout|socket|Popen|run\(' tests/conftest.py tests/fake/conftest.py tests/frontend/conftest.py pyproject.toml
rg --files tests | rg '(^|/)conftest\.py$'
sed -n '1,235p' tests/conftest.py
cat tests/unit/conftest.py
sed -n '1,155p' tests/failed_keep.py
sed -n '1,100p' tests/frontend/conftest.py
sed -n '205,290p' tests/fake/conftest.py
```

Result: five repository conftest files were listed. Relevant hooks and fixture boundaries are described above; no external default directory or process was examined.

First static top-level-call audit (its output was dominated by parametrization and was tool-truncated):

```sh
python3 -u - <<'PY'
import ast,re
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
files=[f for f in files if not (Path(f).name.startswith('test_host_push') and not f.endswith('test_host_push_quarantine_restart.py'))]
class TopCalls(ast.NodeVisitor):
 def __init__(self,name,body): self.name,self.body=name,body
 def visit_FunctionDef(self,node):
  for dec in node.decorator_list: self.visit(dec)
  for default in [*node.args.defaults,*[n for n in node.args.kw_defaults if n is not None]]: self.visit(default)
 visit_AsyncFunctionDef=visit_FunctionDef
 def visit_ClassDef(self,node):
  for dec in node.decorator_list: self.visit(dec)
  for child in node.body: self.visit(child)
 def visit_Call(self,node):
  name=ast.unparse(node.func)
  if re.search(r'run|Popen|check_output|check_call|urlopen|connect|socket|sleep|Thread|start|walk|glob|rglob|read_text|read_bytes|load_policy|compile_probe|resolve|is_dir|exists|which|parametrize|list|product|combinations',name):
   text=ast.get_source_segment(self.body,node) or ''
   if '\n' in text: text=text.splitlines()[0]+' ...'
   print(f'{self.name}:{node.lineno}: {text[:220]}',flush=True)
  self.generic_visit(node)
for name in files:
 body=Path(name).read_text()
 TopCalls(name,body).visit(ast.parse(body))
PY
```

Pytest options and lock/hook source searches:

```sh
sed -n '35,110p' pyproject.toml
rg -n 'suite.?lock|shared.?lock|pytest_sessionstart|pytest_collection|pytest_collect|flock|lock.*slot|slot.*lock' tests subfleet tools -g '*.py' -g '*.toml' -g '*.ini'
```

Result: no collection/plugin options in the project configuration; conftest hook behavior remained as described above. Matches in test bodies and standalone tools were source references, not executed.

Narrower static audit of subprocess/network/read/load calls:

```sh
python3 -u - <<'PY'
import ast,re
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
files=[f for f in files if not (Path(f).name.startswith('test_host_push') and not f.endswith('test_host_push_quarantine_restart.py'))]
class TopCalls(ast.NodeVisitor):
 def __init__(self,name,lines): self.name,self.lines=name,lines
 def visit_FunctionDef(self,node):
  for dec in node.decorator_list: self.visit(dec)
  for default in [*node.args.defaults,*[n for n in node.args.kw_defaults if n is not None]]: self.visit(default)
 visit_AsyncFunctionDef=visit_FunctionDef
 def visit_ClassDef(self,node):
  for dec in node.decorator_list: self.visit(dec)
  for child in node.body: self.visit(child)
 def visit_Call(self,node):
  name=ast.unparse(node.func)
  if re.search(r'(^|[.])(run|Popen|check_output|check_call|urlopen|connect|socket|sleep|Thread|start|walk|glob|rglob|read_text|read_bytes|load_policy|compile_probe|exec|load|git|_load|_reference|product|combinations)$',name):
   print(f'{self.name}:{node.lineno}: {self.lines[node.lineno-1].strip()[:250]}',flush=True)
  self.generic_visit(node)
for name in files:
 body=Path(name).read_text()
 TopCalls(name,body.splitlines()).visit(ast.parse(body))
PY
```

Result: the relevant direct work was compilation of a committed review-shell fixture and small default-policy loads. No direct module-import subprocess/network operation was found.

Audit of additional module-level helper calls:

```sh
python3 -u - <<'PY'
import ast,re
from pathlib import Path
files=Path('docs/reports/2026-11-12-effort-env-data/fix-r2/daemon-import-tests.txt').read_text().splitlines()
files += ['tests/fake/test_launch_gates_above_stdio.py','tests/unit/test_daemon_fifo_reads.py','tests/unit/test_stop_alarm.py']
files=[f for f in files if not (Path(f).name.startswith('test_host_push') and not f.endswith('test_host_push_quarantine_restart.py'))]
skip={'Path','frozenset','set','dict','tuple','list','str','int','float','range','sorted','len','getattr','vars','enumerate','zip','Credential','Lane','partial','settings','field','datetime','timezone','date','timedelta','object','HealthCheck'}
for filename in files:
 body=Path(filename).read_text(); lines=body.splitlines(); tree=ast.parse(body)
 for stmt in tree.body:
  if isinstance(stmt,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Import,ast.ImportFrom)): continue
  for node in ast.walk(stmt):
   if isinstance(node,ast.Call):
    name=ast.unparse(node.func)
    if name in skip or name.startswith(('st.','pytest.','json.','re.','typing.','inspect.')) or name.endswith(('.resolve','.read_text','.read_bytes','.parent','.items','.keys','.values','.append','.extend','.fromisoformat','.split','.join','.fullmatch','.exists','.is_dir','.with_name','.replace','.strip','.lower','.upper','.encode','.decode','.map','.filter','.compile')): continue
    print(f'{filename}:{node.lineno}: {lines[node.lineno-1].strip()[:240]}',flush=True)
PY
```

Result: this conservative AST traversal also lists calls inside module-level lambdas, which are not executed at import. The historical environment loader and policy/spelling helper calls were inspected next rather than assumed expensive.

Focused helper inspection:

```sh
sed -n '1,82p' tests/unit/test_launch_env_round8.py
sed -n '1,100p' tests/unit/test_live_store_guard_st3.py
sed -n '1,82p' tests/unit/test_live_store_guard.py
sed -n '35,80p' tests/unit/test_operator_hold_closures.py
rg -n 'def release_build_environment|subprocess|git|read_text|exec\(|compile\(' tests/fixtures/launch_env_release_217_env2.py tests/unit/test_launch_env_round3.py tests/unit/test_launch_env_allowlist.py tests/live_store_trap.py
cat tests/fixtures/launch_env_release_217_env2.py
```

Result: forbidden-path spellings are bounded string operations; policy helpers read the committed policy; the historical environment loader is the short-source compilation described above. No supported cache setting or expensive extraction/install was found. No tests ran or source files changed during this investigation.
