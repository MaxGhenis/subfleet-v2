"""Rebuild the round-1 appendix from durable result artifacts."""
from pathlib import Path
import json
import ast
import shlex
import xml.etree.ElementTree as ET
ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'docs/reports'
PREFIX = '2026-11-06-tree-thrash-fix-r1-'
path = REPORTS / '2026-11-06-tree-thrash.md'
original = path.read_text().split('\n\n## Fix round 1\n')[0]
original = original.replace('the existing C-6.20 sweep returns that tree.', 'C-6.20\'s sweep returns that tree only if a fresh eligibility check still finds no lane.')
original = original.replace('the sweep\'s implementation and safety checks were not changed.', 'the original build left the sweep implementation unchanged; Fix round 1 adds the eligibility recheck described below and retains its other safety checks.')
original = original.replace('generated lane open/close sequences with at most one labelled race cut per pass.', 'generated single-lane sequences inject at most one labelled race per pass; the new two-pinned-lane example counts both racing looks.')
text = '''\n\n## Fix round 1\n\nWork began on 2026-11-11 at approximately 17:04 UTC from `a22ac891703147078f62329dfbd06da69650bb8b`, in the assigned detached workspace. Only supplied read-only evidence, repository objects and isolated test state were used. The original build results above are historical; this section records the fix round.\n\n### Changes and CI failures\n\nThe three shard-1 failures depended on `git show a8624a4899a2:subfleet/daemon.py`. The tests now bundle the exact original `_admit_pass` in `tests/fixtures/tree_thrash_a8624a4899a2.py`, validate its SHA-256 at load time and compile it against the daemon namespace. The frozen method is byte-for-byte equal to the base: SHA-256 `a894326e79493fd6dc79b8fea75fd392add53d8283f123f4e49e328ee4afbcf5`; the original complete daemon hashes to `c78dfc024c802627dfd83f0504fa872baf6d11605b4d5dbab8d2e6818faa753e`. No test requires that historical Git object or an environment override. An archive with no `.git` imported its own daemon, and `git cat-file -e a8624a4899a2` exited 128 there (saved `history-proof.log`).\n\nThe shard-2 retention oracle encoded eager preparation when a retirement fence stood above the future allocated tree but not above the source or its dependencies: it expected `_workspace` to run once before waiting. The provisional transaction now detects that same fence using the predicted write target and waits before preparation. The relevant C-8.4 outcome remains no reservation under a fence; C-6.5 still leases only the allocated write target. The correction retains exact fence/hold attribution and strengthens checks of foreign holders, absence of attempts and job-held leases, and absence of an allocated tree. It does not permit any reservation previously forbidden by either clause. Reproduction and full-module results follow below.\n\nFor review r1's reopening finding, `_give_back_unrun` rechecks `scheduler.unadmittable(..., far_s=0)` under the detached pass lock before returning an `unprepared` tree. Fresh lane and closure rows replace the stale hold as the authority; desktop eligibility uses the same lookup as admission, with any refresh outside the store lock. Advisory cached hints alone cannot justify a destructive give-back. Terminal and disk-held trees retain their existing checks. The existing synthetic sweep property now supplies a real standing refusal when it models an `unprepared` hold. The permanent `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` cuts a real tree, closes during preparation, reopens without admission, and verifies that the sweep keeps it.\n\nC-6.20, C-6.8, the change-log entry and this report now qualify I4: rejection by the provisional placement checks runs no workspace Git and cuts no tree. A lane, output lease, retention fence or capacity change during preparation can still leave a prepared job waiting under the reservation recheck's hold and clock. `test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation` permanently demonstrates a real tree plus an output-lease wait. Workspace errors can remain undiscovered indefinitely while placement never permits preparation; their labels, clocks and workspace-ready notices can therefore occur later or be omitted when intervening capacity waits replace the workspace wait.\n\n**Restated I1:** each job has at most one successful deferred preparation per pass. With valid preparations and otherwise stable non-lane constraints, cuts are bounded by reservations plus the sum of looks whose lane closes between the successful provisional check and reservation. Multiple jobs can each contribute a race in one pass. `test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass` demonstrates two preparations and zero reservations, one preparation per job. The original generated test deliberately injects at most one race per pass; that fixture bound is not a universal guarantee. Cancellation, workspace failures and unrelated lease/fence changes remain separate races.\n\nThe provisional placement path now skips `_slot_lease`: `_route_stands` still verifies that the pool has room, and final reservation selects the lowest available slot number. This removes the repeated one-lane slot search (45,150 extra SQL probes at 300 placements). The submitted-HEAD example spies on slot selection and requires exactly one call after the real tree exists. Pass pacing is unchanged. No product event/record key or refusal was added; existing `unprepared`/`refused_now` assertions were searched.\n\n### Ordered checks\n\nAll tests use Python 3.15.2, uv's synced development dependencies, at most four pytest workers, a fresh `--basetemp` per run and `--timeout=1200`. The first archive run selected Homebrew Git; its measured-shape timeout prompted a retry with system Git. Subsequent runs set `PATH=/usr/bin:/bin:/opt/homebrew/bin`. The initial three-case run completed before retention reproduction began; its extra measured-shape retry overlaps only the next validation stage, with total pytest workers capped at four.\n\n'''
checks_file = REPORTS / (PREFIX + 'checks-results.json')
check_results = {row['label']: row for row in json.loads(checks_file.read_text())} if checks_file.exists() else {}
for suffix in ['ordered-1', 'ordered-1-system-git', 'retention-reproduce', 'retention', 'retention-ci', 'ordered-3', 'ordered-4', 'ordered-5']:
    if suffix in check_results:
        result = check_results[suffix]
        text += f'- `{suffix}`: attempted; exit {result["exit"]}, interrupted for the run budget after {result["seconds"]:.3f} s; no confirmed completed cases. Interrupted JUnit placeholders are not passes.\n'
        continue
    if suffix in ('retention-reproduce', 'retention'):
        text += f'- `{suffix}`: interrupted; no confirmed completed cases. See retention-ready.json for commands and results.\n'
        continue
    xml = REPORTS / (PREFIX + suffix + '.xml')
    if not xml.exists():
        if suffix in check_results:
            result = check_results[suffix]
            text += f'- `{suffix}`: attempted; exit {result["exit"]}, interrupted for the run budget after {result["seconds"]:.3f} s; no completed-case JUnit file.\n'
        elif suffix.startswith('retention'):
            text += f'- `{suffix}`: attempted but incomplete; no completed-case JUnit file (details in `retention-ready.json`).\n'
        else:
            text += f'- `{suffix}`: not completed.\n'
        continue
    try:
        root = ET.parse(xml).getroot()
    except ET.ParseError:
        text += f'- `{suffix}`: XML not complete.\n'
        continue
    cases = root.findall('.//testcase')
    failed = sum(c.find('failure') is not None or c.find('error') is not None for c in cases)
    skipped = sum(c.find('skipped') is not None for c in cases)
    suites = root.findall('testsuite') if root.tag != 'testsuite' else [root]
    seconds = sum(float(s.get('time', 0)) for s in suites)
    text += f'- `{suffix}`: {len(cases)-failed-skipped} passed, {failed} failed/errors, {skipped} skipped; XML wall {seconds:.3f} s.\n'
text += '''\nLogs and JUnit files use the common prefix `docs/reports/2026-11-06-tree-thrash-fix-r1-`. The durable `checks.py` runner records exact commands, exit codes and budget interruptions in `checks-results.json`.\n\n### Cost and mutants\n\nThe old fully-open pair added 27.49% retired instructions, 20.12% wall time, 25.63% thread CPU (0.562689 seconds), 3,300 counted Store reads and 46,350 returned rows. New results and mutant outcomes are pending. The unchanged benchmark runner hashes to `e87dfe9d767014e4091b84f72fdc10e104c8f657b53882564ccb443bf103187d`. Old runs used Python 3.14.7; compare each paired base/head percentage rather than absolute costs across Python versions. The benchmark's measured `_admit_pass` is byte-for-byte equal to the final placement code; later sweep-only eligibility revisions are recorded in `benchmark-source.json`.\n\n### Commands, environment and limits\n\n```sh\nexport TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)subfleet-attempt-20261107-112016-tt-fix-r1-a1-tw4lucor"\nexport UV_CACHE_DIR="$TMPDIR/uv-cache"\nexport PATH=/usr/bin:/bin:/opt/homebrew/bin\n```\n\n- Read-only `cat` of the supplied report, build brief and full review r1: completed. Scoped `rg`/`sed` inspected admission, sweep, lease/fence helpers, tests, contract and tools; no `AGENTS.md` was present. GitNexus debugging skill was read, but no GitNexus tools were available, so source tracing was used.\n- `git status --short`, `git branch --show-current`, `git rev-parse HEAD`: initial workspace clean and detached at the supplied head.\n- `gh run view 42274833825 --repo MaxGhenis/subfleet-v2 --log-failed`: exit 0; saved `ci.log`, confirming both CI failure classes on both interpreters. `gh pr view 295 --repo MaxGhenis/subfleet-v2 --json body,title`: exit 0; saved original PR wording.\n- `getconf DARWIN_USER_TEMP_DIR`: completed. Initial `uv sync --group dev` failed because the shared default cache denied `CACHEDIR.TAG`. `uv --cache-dir "$UV_CACHE_DIR" sync --group dev` in the permitted child succeeded, installing nine development packages. `uv run --no-sync --with 'pytest-xdist>=3.6' python -c 'import xdist; print(xdist.__version__)'`: passed, xdist 3.8.0.\n- `/usr/bin/git archive HEAD | tar -xf - -C "$archive"`, followed by named daemon/test/frozen-fixture overlays and `test ! -e .git`: completed. The first three named cases ran there through `uv run --no-sync --project WORKSPACE --with 'pytest-xdist>=3.6' python -m pytest -n 3 -q ...`; the measured-shape retry uses the same archive serially and system Git. Both pass a new basetemp, durations, JUnit, short traceback and the 1,200-second cap. `history-proof.log` records the archive cwd/import and failing cat-file command.\n- `python -m py_compile`/`compileall -q` of changed daemon, tests, frozen fixture and mutation tool: passed. Frozen-source AST extraction, byte comparison and SHA-256 checks: passed. Repeated `git diff --check`: passed. Existing hold comparisons were searched; no handler-refusal run is needed because no refusal was added.\n- Named-file staging/commit attempts for daemon + sweep fixture, frozen oracle + permanent probes, and mutation tool all failed exit 128: the sandbox denies `/Users/maxghenis/subfleet-v2/.git/worktrees/20261107-112016-tt-fix-r1/index.lock`. No commit ran, no history was rewritten and nothing was pushed. Files remain for hub collection.\n\nCompletion status, CI projection, final tool commands and remaining checks will be recorded before the 20:04 UTC deadline.\n\nHead built: `a22ac891703147078f62329dfbd06da69650bb8b` plus the preserved working-tree changes (commits denied).\n'''
text = text.replace('All tests use Python 3.15.2,', 'The initial archive and retention attempts used Python 3.15.2; the final retention retry, ordered suites and mutant campaign use CI\'s Python 3.14.7. Runs use')
text = text.replace('New results and mutant outcomes are pending.', 'The new paired results and mutant outcomes are recorded below.')
text = text.replace('Completion status, CI projection, final tool commands and remaining checks will be recorded before the 20:04 UTC deadline.', '')
text = text.replace('The original build results above are historical;', 'Git could not create a commit. Validation is incomplete. The original build results above are historical;')
cost = REPORTS / (PREFIX + 'tooling.md')
if cost.exists() and '## Cost\n' in cost.read_text():
    start = text.index('The old fully-open pair added')
    end = text.index('\n### Commands, environment and limits', start)
    tool_section = cost.read_text().split('## Cost\n', 1)[1].strip()
    tool_section = '\n'.join('### ' + line[3:] if line.startswith('## ') else line for line in tool_section.splitlines())
    text = text[:start] + tool_section + '\n' + text[end:]
records = {}
for label in ['ordered-1', 'ordered-1-system-git', 'final-controls']:
    xml = REPORTS / (PREFIX + label + '.xml')
    if not xml.exists():
        continue
    try:
        cases = ET.parse(xml).getroot().findall('.//testcase')
    except ET.ParseError:
        continue
    for case in cases:
        if case.get('classname', '').rsplit('.', 1)[-1] not in ('test_admission_tree_thrash', 'test_admission_tree_thrash_head'):
            continue
        name = case.get('name', '')
        if not name.startswith('test_'):
            continue
        status = 'failed/incomplete' if case.find('failure') is not None or case.find('error') is not None else 'passed'
        if label == 'final-controls' and name != 'test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree':
            continue
        records[name] = (float(case.get('time', 0)), status, label)
rows = []
for filename in ['test_admission_tree_thrash.py', 'test_admission_tree_thrash_head.py']:
    module = ast.parse((ROOT / 'tests/fake' / filename).read_text())
    for function in module.body:
        if not isinstance(function, ast.FunctionDef) or not function.name.startswith('test_'):
            continue
        found = [(name, value) for name, value in records.items() if name.split('[', 1)[0] == function.name]
        if found:
            rows.extend(f'| `{name}` | {value[0]:.3f} | {value[1]}; `{value[2]}` |' for name, value in found)
        else:
            rows.append(f'| `{function.name}` | — | No completed timing in this round |')
timings = '\nLatest recorded all-phase testcase times for the new modules (interrupted cases are not passes; unmeasured rows include their parameter variants):\n\n| Test | Seconds | Result / run |\n| --- | ---: | --- |\n' + '\n'.join(rows) + '\n'
text = text.replace('\n### Cost and mutants\n', timings + '\n### Cost and mutants\n')
ledger = REPORTS / (PREFIX + 'checks-results.json')
commands = '\nFinal ordered command ledger (the common environment above applies; each uses its own named log/XML):\n\n'
if ledger.exists():
    for result in json.loads(ledger.read_text()):
        if 'command' in result:
            command = list(result['command'])
            cache = command[command.index('--cache-dir') + 1]
            temporary = cache.rsplit('/', 1)[0]
            command = [token.replace(str(ROOT), '$WORKSPACE').replace(temporary, '$TMPDIR') for token in command]
            commands += '- `' + shlex.join(command) + f'`: exit {result["exit"]}; {result["seconds"]:.3f} s; budget interruption={result["interrupted_for_budget"]}.\n'
        else:
            commands += f'- `{result["label"]}`: {result["result"]}.\n'
limits = '\n### Remaining validation and handoff\n\nThe required retention module, all cases of the main/head/unrun modules and the combined thirteen-module suite have not completed. Their final xdist attempts were interrupted during worker startup; no green suite is claimed. The twenty-round case timed out twice, so this round does not claim completed 200/40/160 versus 40/40/0 measurements. The two generated archive properties passed without the base object. New-probe timing claims appear only when a named case completes in the table above. The final serial controls are recorded in the tooling evidence. Four mutants remain not run, with zero claimed kills; prior review r1 kills are historical evidence, not executions in this round. Python 3.12 and a fresh Actions run were not run. The closed-pass measurements are slower on head and the no-slowdown requirement remains unproven; no pacing or CI-map change was made.\n\nFinal daemon SHA-256: `176f8c848fc0c36629d6c8518c65d4c436b5dc02358fbf7e904bc5eec1074774`. A final named staging attempt covering the daemon, four test files/fixture, mutation tool and contract was denied the same index lock. The final report and artifacts remain in the assigned workspace for hub collection.\n'
text = text.replace('\nHead built:', limits + '\nHead built:')
text = text.replace('\nHead built:', commands + '''\nAdditional commands: `UV_PROJECT_ENVIRONMENT="$TMPDIR/tree-thrash-r1/py314-env" uv --cache-dir "$UV_CACHE_DIR" sync --python 3.14 --group dev` passed (3.14.7, nine packages). Two idle check drivers were interrupted with exit 130 before starting any tests, to adjust the budget and interpreter; the final driver uses the saved command ledger above. `gh pr edit 295 --repo MaxGhenis/subfleet-v2 --body-file docs/reports/2026-11-06-tree-thrash-fix-r1-pr.md` passed and corrected the PR's race/no-Git wording. The retention attempt ledger in `retention-ready.json` includes its unsuccessful reproduction, cached-Python plugin error and interrupted startup; none is claimed as a pass.\n\nHead built:''')
path.write_text(original + text)
