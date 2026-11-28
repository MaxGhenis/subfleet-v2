# Delegated fixture and exact-comparison command audit

These are the shell commands run by the `test_map` delegate, reconstructed from its
tool transcript. Paths are relative to the assigned worktree unless absolute.
Independent commands were submitted together where possible; polling an existing
tool session did not start another command. No pytest, mutation, credential access,
or live-state command was run by this delegate. File edits used `apply_patch`.

## Initial read-only reconnaissance

All commands in this section exited 0 except the explicitly noted missing-file
diagnostic. Some broad inspection output was truncated by the tool; subsequent
targeted reads resolved the relevant portions.

1. `pwd; rg --files -g 'AGENTS.md' -g 'tests/lane_settings_cases.py' -g 'tests/fixtures/lane_settings/argv-676906f85.json' -g 'tests/unit/test_claude_v1_parity.py' -g 'subfleet/adapters/claude.py' -g 'subfleet/conversations/claude_turn.py'`
   — confirmed assigned worktree and five relevant paths; no `AGENTS.md` found.
2. `sed -n '1,260p' tests/lane_settings_cases.py` — inspected grid and real builders.
3. `sed -n '1,280p' tests/unit/test_claude_v1_parity.py` — identified hardcoded live-home source path.
4. `rg -n 'lane_settings|claude_settings_args|reconstruct_v1_argv|build_launch|PERMISSION_FLAGS|ULTRACODE|effort' tests --glob '*.py'` — broad symbol inventory, output truncated.
5. `sed -n '1,160p' tests/fixtures/lane_settings/argv-676906f85.json` — historical fixture shape, output truncated.
6. `rg -l 'lane_settings|claude_settings_args|reconstruct_v1_argv|build_launch|PERMISSION_FLAGS|ULTRACODE|effort' tests --glob '*.py'` — symbol file inventory, including helper modules.
7. `sed -n '1,300p' tests/unit/test_lane_settings.py` — inspected watchdog exact expectations and properties.
8. `rg -n 'assert.*(argv|settings)|--settings|lane_settings' tests/unit/test_claude_turn.py tests/unit/test_claude_adapter.py tests/unit/test_review_publish.py tests/unit/test_claude_v1_parity.py`
   — exited **2** because `tests/unit/test_review_publish.py` does not exist; returned matches from the three existing files.
9. `sed -n '2080,2210p' subfleet/adapters/claude.py` — inspected offline parity reference.
10. `sed -n '280,430p' tests/lane_settings_cases.py` — inspected normalization and differential removal.
11. `sed -n '300,640p' tests/unit/test_lane_settings.py` — inspected fake-provider checks and builder inventory.
12. `sed -n '1,280p' tests/unit/test_lane_settings_review_r1.py` — inspected independent settings checks.
13. `sed -n '1,270p' tests/unit/test_ultracode_effort.py` — located exact ultracode settings payloads.

The following counting command exited 0. It reported 237 original cases, 76 with
explicit `effort=None`, 108 with named effort, and therefore 53 without an effort
input (129 unnamed total). Path counts were job 72, resume 48, probe 8, timer 4,
gate 4, turn 96, and five enrollment/probe/heal cases. It found 74 matching
`test_*.py` files before the new property file was added.

```sh
python3 - <<'PY'
import ast, json
from pathlib import Path
p=Path('tests/fixtures/lane_settings/argv-676906f85.json')
x=json.loads(p.read_text())
c=x['cases']
print('fixture keys',list(x))
print('cases',len(c),'none',sum('|effort=None|' in k or k.endswith('|effort=None') for k in c),'named',sum('|effort=' in k and '|effort=None|' not in k and not k.endswith('|effort=None') for k in c))
for base in sorted({k.split('|')[0] for k in c}):
 print(base,sum(k.split('|')[0]==base for k in c))
print('test files',len([p for p in Path('tests').rglob('test_*.py') if any(s in p.read_text() for s in ('lane_settings','claude_settings_args','reconstruct_v1_argv','build_launch','PERMISSION_FLAGS','ULTRACODE','effort'))]))
PY
```

## Safe v1 source investigation

All commands exited 0. Searches remained in the worktree or its repository's git
objects. No live v1 runner was opened or statted by the delegate.

1. `rg -n 'subfleet-claude|V1_RUNNER|v1_source|PERM_ARGS=\(' tests subfleet docs pyproject.toml --glob '*.py' --glob '*.sh' --glob '*.md' --glob '*.toml'`
   — repository references only, broad output truncated.
2. `git ls-tree -r --name-only HEAD` — tracked-file inventory, broad output truncated.
3. `sed -n '130,240p' tests/unit/test_claude_v1_parity.py` — remaining source assertions.
4. `git log --all --format='%h %s' -- tests/unit/test_claude_v1_parity.py` — parity test history, output truncated.
5. `rg --files tests/fixtures docs/reference | rg '(v1|subfleet-claude|parity|runner)'` — gate v1 fixtures existed; no runner fixture.
6. `git log --all --format='%h %s' -10 -- bin/subfleet-claude` — no matching history.
7. `sed -n '1,120p' tests/conftest.py` — inspected collection-time home isolation.
8. `rg -n 'pwd|getpwuid|HOME|chief-of-staff|V1_RUNNER' tests/unit/test_claude_v1_parity.py tests/conftest.py` — verified parity bypassed isolated HOME through `pwd`.
9. `git rev-list --all --objects -- '*subfleet-claude'` — no runner blob; only two root commit-name entries (`desktop-2.1.6`, `desktop-2.1.7`). The process exited before proceeding.
10. `rg -n 'CLAUDE_CODE_RETRY_WATCHDOG|LANE_SETTINGS_ENV|lane_settings_args\(' tests --glob 'test_*.py'` — located pinned settings literals.
11. `sed -n '1,170p' subfleet/conversations/claude_turn.py` — inspected effort translation and settings assembly.
12. `sed -n '320,365p' subfleet/adapters/claude.py` — inspected original merge helper.

## Fixture preparation and generation

These diagnostic commands all exited 0:

1. `head -n 8 tests/fixtures/lane_settings/argv-676906f85.json` — inspected insertion point.
2. `tail -n 6 tests/fixtures/lane_settings/argv-676906f85.json` — verified original final case has no comma.
3. `sed -n '370,465p' tests/unit/test_claude_adapter.py` — inspected offline launch comparison and hook remover.
4. `sed -n '250,282p' tests/lane_settings_cases.py` — inspected turn builder inputs.
5. `git diff --stat` — four edited compatibility files at that point.
6. `command -v python3` — `/opt/homebrew/bin/python3`.
7. `rg -n '\{WATCHDOG: OFF\}|expected_env|without_override|EFFORTS =' tests/unit/test_lane_settings.py tests/lane_settings_cases.py` — verified changed expectation sites.
8. `git status --short` — assigned test edits and root report files; no delegate-created folders outside scratch.

The following generation command exited 0. It archived only committed historical
`subfleet` source, used a minimal child environment and `-I -B`, checked every old
argv against the generated historical argv, and inserted only new medium-effort
case lines. Result: **237 original case lines retained, 129 unnamed unchanged,
70 additions, 307 total**. Scratch folders were all beneath
`/private/tmp/sf-eff-b1/fixture-extension`; none were created elsewhere.

```sh
python3 - <<'PY'
import io, json, os, subprocess, sys, tarfile
from pathlib import Path
repo=Path.cwd()
scratch=Path('/private/tmp/sf-eff-b1/fixture-extension')
scratch.mkdir(parents=True, exist_ok=True)
old=scratch/'old'
old.mkdir(exist_ok=True)
archive=subprocess.run(['git','archive','--format=tar','676906f854f5','subfleet'], capture_output=True, check=True).stdout
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(old, filter='data')
for path in (scratch/'home',scratch/'tmp'):
    path.mkdir(exist_ok=True)
command=[sys.executable,'-I','-B',str(repo/'tests/lane_settings_cases.py'),str(old),str(scratch/'cases')]
result=subprocess.run(command,capture_output=True,text=True,env={'PATH':'/usr/bin:/bin','HOME':str(scratch/'home'),'TMPDIR':str(scratch/'tmp')},timeout=300)
print('historical generator',result.returncode)
if result.returncode:
    print(result.stderr[-4000:]); raise SystemExit(result.returncode)
recording=repo/'tests/fixtures/lane_settings/argv-676906f85.json'
before=recording.read_bytes()
original=json.loads(before)['cases']
generated=json.loads(result.stdout)['cases']
assert all(generated[key]==value for key,value in original.items())
added={key:value for key,value in generated.items() if key not in original}
assert len(added)==70,len(added)
rows=''.join('  '+json.dumps(key)+': '+json.dumps(value)+',\n' for key,value in sorted(added.items()))
after=before.replace(b' "cases": {\n',b' "cases": {\n'+rows.encode(),1)
assert len(json.loads(after)['cases'])==307
assert all(line in after.splitlines() for line in before.splitlines())
recording.write_bytes(after)
(scratch/'generated-argv.json').write_text(result.stdout)
(scratch/'fixture-original.json').write_bytes(before)
none=[key for key in original if '|effort=' not in key or '|effort=None|' in key or key.endswith('|effort=None')]
assert len(none)==129
print('original cases',len(original),'unchanged literal case lines',len(original),'unnamed unchanged',len(none),'added',len(added),'total',len(generated))
PY
```

`git diff --check` then exited 0 with no output. This was a whitespace check,
not a pytest run.

## Offline parity extension and production review

All commands exited 0.
The resulting offline parity test adds 12 cases (six efforts by two sandboxes).
No test command was run by the delegate.

1. `sed -n '375,425p' tests/unit/test_claude_adapter.py`
2. `git diff -- subfleet/adapters/claude.py subfleet/conversations/claude_turn.py`
3. `sed -n '1,35p' tests/unit/test_claude_adapter.py`
4. `rg -n 'def claude_settings_args|def build_launch|if effort|env_add =' subfleet/review_publish.py subfleet/adapters/claude.py`
5. `sed -n '1265,1458p' subfleet/adapters/claude.py`
6. `sed -n '1,190p' subfleet/review_publish.py`
7. `rg -n 'CLAUDE_CODE_EFFORT_LEVEL|effort=' tests/unit/test_effort_env.py`

Review found no production defect: the settings and env are copied, the effort
joins the existing single payload, the publish hook preserves it, process
`env_add` is unchanged, and turns translate ultracode to xhigh for both values.

## Final exact-comparison review

All commands exited 0. The inspection found three missed high-effort JSON
literals in two files; those were amended after root authorization. The
`effort=None` bypass literal remained untouched. Remaining exact comparisons
were either unnamed launch settings, unmodified flag slices, provider protocol
frames, other providers, or unrelated application settings.

1. `sed -n '1,180p' docs/reports/2026-11-12-effort-env-data/all-exact-compares.txt`
2. `sed -n '181,380p' docs/reports/2026-11-12-effort-env-data/all-exact-compares.txt`
3. `git diff -- tests/unit/test_claude_adapter.py tests/unit/test_claude_turn.py tests/unit/test_ultracode_effort.py tests/unit/test_lane_settings.py tests/unit/test_effort_env.py tests/lane_settings_cases.py`
4. `rg -n 'claude|lane_settings|ultracode|review_publish_guard|effort_env' docs/reports/2026-11-12-effort-env-data/all-exact-compares.txt`
5. `sed -n '1,95p' tests/unit/test_claude_conversation_mcp_scope.py`
6. `sed -n '350,395p' tests/unit/test_review_publish_guard.py`
7. `sed -n '1,76p' tests/unit/test_claude_turn.py`
8. `sed -n '180,255p' tests/process/test_claude_isolation.py`
9. `sed -n '1,95p' tests/process/test_claude_isolation.py`
10. `sed -n '45,100p' tests/unit/test_review_publish_guard.py`
11. `sed -n '720,760p' tests/unit/test_claude_adapter.py`
12. `sed -n '190,255p' docs/reports/2026-11-12-effort-env-data/all-exact-compares.txt`
13. `sed -n '1,230p' tests/unit/test_effort_env.py`
14. `rg -n '(argv|command|settings|turn).*(==|!=)|(==|!=).*(argv|command|settings|turn)' docs/reports/2026-11-12-effort-env-data/all-exact-compares.txt | rg -v '(codex|census|retention|guardian|daemon_log|disk|suggestions|importer|core_|app_cutover|cli|compat|turn_runner|turn_replay|operator_notices|legacy_hold|procs|sessions_mirror|prune_restore|conversation_titles|conversation_store|handoff|task_hold|filesyst|hidden_environment|boot_identity|guard_trust|live_store_guard|policy|launch_env_round|conversation_wakes|review_round|hold_note|unit/test_offline)'`
15. `sed -n '20,48p' tests/unit/test_review_publish_guard.py`
16. `rg -n 'assert.*(==|!=).*\[|assert.*(==|!=).*\{' tests/unit/test_claude_adapter.py tests/unit/test_claude_turn.py tests/unit/test_claude_conversation_mcp_scope.py tests/unit/test_ultracode_effort.py tests/unit/test_review_publish_guard.py tests/process/test_claude_isolation.py`
17. `git diff -- tests/unit/test_claude_turn.py tests/unit/test_claude_conversation_mcp_scope.py`

## Durable fixture audit

The final `python3 - <<'PY'` command read the archived `fixture-original.json`,
archived `generated-argv.json`, and current committed-fixture path; it wrote only
`fixture-audit.json` beside this file. It exited 0 and printed:

```text
{"added_case_count": 70, "final_case_count": 307, "original_case_count": 237, "original_case_lines_byte_identical": 237, "original_named_effort_cases": 108, "original_unnamed_effort_cases": 129}
removing only added lines recovers entire pristine fixture: true
```

That command asserted the following, independently of the generation command:

- The historical generator's 307 argv equal the final fixture's 307 argv.
- Every original parsed argv is preserved.
- The exact original case-line bytes, including their newline, match all 237
  current case lines by unique JSON case ID.
- Added IDs are disjoint and total 70; originals comprise 108 named and 129 unnamed.
- Removing only the 70 added case lines reconstructs the **entire pristine file
  byte for byte**, including metadata, ordering and punctuation.

The audit records SHA-256 for all three inputs, baseline commit `676906f854f5`,
and all 70 added case IDs in sorted order. No production/test file was changed
during this durable-audit step.

The exact durable-audit command was:

```sh
python3 - <<'PY'
import hashlib, json, re
from pathlib import Path
scratch=Path('/private/tmp/sf-eff-b1/fixture-extension')
pristine=(scratch/'fixture-original.json').read_bytes()
generated=(scratch/'generated-argv.json').read_bytes()
fixture=Path('tests/fixtures/lane_settings/argv-676906f85.json')
current=fixture.read_bytes()
original=json.loads(pristine)['cases']
recorded=json.loads(current)['cases']
rebuilt=json.loads(generated)['cases']
added=sorted(set(recorded)-set(original))
assert len(original)==237 and len(recorded)==307 and len(added)==70
assert rebuilt==recorded
assert all(recorded[key]==value for key,value in original.items())
pattern=re.compile(rb'  ("(?:[^"\\]|\\.)*"): ')
def key(line):
    found=pattern.match(line)
    return json.loads(found.group(1)) if found else None
old_lines={key(line):line for line in pristine.splitlines(keepends=True) if key(line) is not None}
new_lines={key(line):line for line in current.splitlines(keepends=True) if key(line) is not None}
assert set(old_lines)==set(original)
assert set(new_lines)==set(recorded)
assert all(new_lines[name]==line for name,line in old_lines.items())
recovered=b''.join(line for line in current.splitlines(keepends=True) if key(line) not in added)
assert recovered==pristine
unnamed=[name for name in original if '|effort=' not in name or '|effort=None|' in name or name.endswith('|effort=None')]
assert len(unnamed)==129
result={
 'baseline_commit':json.loads(pristine)['commit'],
 'fixture':str(fixture),
 'original_case_count':len(original),
 'original_named_effort_cases':len(original)-len(unnamed),
 'original_unnamed_effort_cases':len(unnamed),
 'added_case_count':len(added),
 'final_case_count':len(recorded),
 'original_case_lines_byte_identical':len(old_lines),
 'original_unnamed_case_lines_byte_identical':len(unnamed),
 'original_named_case_lines_byte_identical':len(original)-len(unnamed),
 'assertions':{
   'historical_generator_matches_all_final_argv':True,
   'every_original_parsed_argv_preserved':True,
   'every_original_case_line_preserved_byte_for_byte':True,
   'removing_only_added_case_lines_recovers_entire_pristine_file':True,
   'added_case_ids_disjoint_from_original':True,
 },
 'sha256':{
   'pristine_fixture':hashlib.sha256(pristine).hexdigest(),
   'final_fixture':hashlib.sha256(current).hexdigest(),
   'historical_generator_output':hashlib.sha256(generated).hexdigest(),
 },
 'scratch_sources':{
   'pristine_fixture':str(scratch/'fixture-original.json'),
   'historical_generator_output':str(scratch/'generated-argv.json'),
 },
 'added_case_ids':added,
}
output=Path('docs/reports/2026-11-12-effort-env-data/fixture-audit.json')
output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
print(json.dumps({key:result[key] for key in ('original_case_count','original_named_effort_cases','original_unnamed_effort_cases','added_case_count','final_case_count','original_case_lines_byte_identical')},sort_keys=True))
print('removing only added lines recovers entire pristine fixture: true')
PY
```
