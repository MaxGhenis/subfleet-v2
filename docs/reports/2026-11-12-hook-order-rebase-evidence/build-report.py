from pathlib import Path
import datetime, json, shutil
scratch=Path('/private/tmp/sf-hor-b1')
workspace=Path.cwd()
report=Path('docs/reports/2026-11-12-hook-order-rebase.md')
evidence=report.with_name('2026-11-12-hook-order-rebase-evidence')
evidence.mkdir(parents=True,exist_ok=True)
selected=['commands.jsonl','bootstrap-commands.md','environment.md','step0.md','mutant-audit.md','audit-verification.py','audit-verification.log','exact-compare-grep.log','targeted-filenames.log','mutants.py','mutants.json','cleanup.json','cleanup.py','run.py','checks.py','sf_job_paths.py','rebase-message.txt','build-report.py']
for path in scratch.glob('*.log'):
    selected.append(path.name)
for name in sorted(set(selected)):
    shutil.copy2(scratch/name,evidence/name)
rows=[json.loads(line) for line in (scratch/'commands.jsonl').read_text().splitlines()]
mutants=json.loads((scratch/'mutants.json').read_text())
assert len(mutants)==9 and all(row['killed'] for row in mutants)
assert any('test_state_contract.py' in row['command'] and row['exit']==0 for row in rows)
now=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
text=f'''# Hook-order rebase report — 2026-11-12

Started **2026-11-12T05:51:30Z** from the first command's `date -u`; report prepared **{now}**, before the 08:51:30Z deadline. Workspace: `{workspace}`.

Rebased #86's two commits onto **b51f08f961e1b1dcfb93347eb080f9ddd9a9260d**, resolved the contract, and committed the new differential test separately. All nine mutants were killed. Validation: equality **18 passed**; differential **3 passed**; targeted matrix **2,204 passed, 10 skipped**; final contract/state group **425 passed**. Invariant 8 is limited by three sandbox-related skips in the unchanged core release files.

**Delivery limitation:** the sandbox refused the shared Git administrative directory. The history was built in private metadata on the assigned branch name, `rebase/hook-order-drift-217`. Original refs and the workspace gitfile remain untouched at `62103194a83650d3ac4ea532776f632e476390d5`. Import the accompanying `docs/reports/2026-11-12-hook-order-rebase.gitbundle` with `git bundle unbundle <bundle-path>` before the hub sets #86's branch with its lease. The bundle is generated after the report commit as a delivery artifact; nothing was pushed.

The code/test history before this report is:

- `a84182d5862bad5470295de0bdcd9a8889a7ff97`: original 839c6a4db, message preserved exactly.
- `511322701b3a66ff193a2ae29de0671b1e2e54e4`: original 62103194a, original message retained with the requested resolution note.
- `aacdfeac9c16e77939080dafee8785741d06d037`: differential test commit.

'''
audit=(scratch/'report-audit-sections.md').read_text()
audit=audit.replace('Their complete files are included in the requested targeted verification.', 'The targeted run and a skip-detail follow-up report 143 passed and 3 skipped: test_hooks.py:341 needs native ps inspection after its hook output/mark assertions passed; test_doctor.py:605 and :617 need permitted sysctl/ps inspection and macOS boot identity. Thus full execution of invariant 8 remains for CI.')
audit=audit.replace('content/order, top-level keys, and events the installer does not own.', 'content/order. The install property checks top-level/unowned preservation; the new uninstall differential property also explicitly checks those unowned values.')
text+=audit
text+='''
## Mutants

Every mutant was applied alone from a pristine byte copy and restored afterward. Each killer pytest exit was 1 with the expected assertion failure, not a setup/collection failure. No example-count overrides were used. Independent audit confirmed committed test bytes and final `hooks.py` SHA-256 `83907562e9621a5fc2311a5262bb0a418b584680cb400ca6f4e064875f567812`.

`E` means `tests/unit/test_hooks_install_equality.py`; `D` means `tests/unit/test_hooks_install_differential.py`. Counts are committed `max_examples`, or one deterministic example.

| Mutant | Killer | Count | Failed assertion |
|---|---|---:|---|
'''
descriptions={1:('Append ours last','E:414, no drift'),2:('Python == in plan','E:472, differs reason'),3:('Re-render unchanged events','E:262, key order'),4:('Duplicate reported as differs','E:237, duplicated reason'),5:('Insert after shared group','E:224, placement'),6:('Conflate bool and number','E:550, independent canonical equality'),7:('Use last owned position','E:241, first position'),8:('Insert missing entry at index zero','D:129, canonical proposed document; explicit example'),9:('Drop installed drift key','E:157, PostToolUse (missing) doctor detail')}
for row in mutants:
    filename,test=row['killer'].split('::')
    alias='D' if 'differential' in filename else 'E'
    description,assertion=descriptions[row['mutant']]
    text+=f"| {row['mutant']}. {description} | `{alias}::{test}` | {row['examples']} | {assertion} |\n"
text+='''
## Verification and limits

Each invocation had a fresh `--basetemp`. The required file search selected 13 actual test files, all run together with `-n 4`: the six named release files, equality/differential, and five additional matches in incidental “would write” prose (`test_retention_regenerable_properties.py`, `test_launch_env_round7.py`, `test_daemon_log_sites.py`, `test_steer_invariants.py`, `tests/frontend/test_core_steer.py`). Helpers and fixture/model files were excluded. The full suite was not run.

| Run | Result |
|---|---|
| Equality file alone | 18 passed |
| Differential file alone | 3 passed |
| All 13 matching files, one four-worker run | 2,204 passed, 10 skipped |
| State contract, module names, invariant index, handler refusals | 425 passed |
| Three unchanged release files, skip-detail follow-up | 143 passed, 3 skipped |
| Independent contract/file/frozen-loop audit | All 9 assertions passed, repeated after tests |
| Whitespace check | `git diff --check` passed |
| Code-history bundle check | Valid bundle; release target is its prerequisite |

The core-file repeat was justified by the combined run's unexplained skips. The three core skips are native process/boot-identity checks unavailable in this sandbox; no release test or expectation was edited to bypass them. The other seven combined-run skips remain recorded in that run's summary. No test failed on the restored tree.

## Deviations and cleanup

- Native rebase could not create the shared `rebase-merge` directory. Private Git metadata in `/private/tmp/sf-hor-b1/git`, with a read-only parent-object alternate, allowed the requested rebase and commits without changing caller refs. The hub must import the bundle before using the final tip.
- `getconf DARWIN_USER_TEMP_DIR` returned `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`, but creating its job scratch folder was denied. Tests therefore used `TMPDIR=/private/tmp/sf-hor-b1` and `UV_CACHE_DIR=$TMPDIR/uv-cache`.
- The exact requested `uv sync --group dev --extra api` failed because this release defines no `api` extra. `uv sync --group dev` succeeded. `uv pip install pytest-xdist` supplied xdist only in the disposable environment; pyproject and lock stayed unchanged.
- An external, archived `sf_job_paths` pytest plugin redirected explicit `/tmp` fixture folders into job scratch and `tests.failed_keep.ROOT` there, preventing another job's scratch/prune state from being read. Workspace fixture temp folders got the `sf-hor-b1` prefix and guarded deletion. Swift/Hypothesis/uv caches stayed in scratch; the repository-supported Swift nested-sandbox option was disabled inside the outer sandbox. Repository fixtures/tests were unchanged.
- A contract-resolution script first matched the wrong log prefix/date. Its initial shell continued after the error and temporarily finalized unresolved markers. Before any tests, the new private documentation commit was corrected to the verified resolution and requested message. Only the commits being rebased were corrected; no shared history changed.
- Invariant 8 has the three explicit environment skips above. The literal historical hooks sentence could not be reconfirmed, although today's official page confirms its parallel execution behavior.

All outside-scratch generated folders are recorded below, relative to the workspace given at the top. The fixture folder was already removed with the job guard; dependency/cache/bytecode folders were moved into job scratch and deleted with `shutil.rmtree` after asserting `sf-hor-b1` in the deletion path. All tracked files were checked and preserved. Completed basetemps and scratch caches were also removed. Exact original/deletion paths are in `2026-11-12-hook-order-rebase-evidence/cleanup.json`.

'''
for row in json.loads((scratch/'cleanup.json').read_text()):
    text+='- `'+str(Path(row['path']).relative_to(workspace))+'`\n'
text+='''
## Commands and results

`S=/private/tmp/sf-hor-b1`; `P=.venv/bin/python -m pytest -p sf_job_paths`. Git mutations below used the private `GIT_DIR=S/git`; test commands explicitly removed that variable. The archived `commands.jsonl` retains full unabridged arguments, durations, and output filenames. Every output log is in `2026-11-12-hook-order-rebase-evidence/`. Mutant exit 1 is the expected successful kill.

| Command | Exit | Seconds |
|---|---:|---:|
'''
for row in rows:
    command=row['command'].replace('/private/tmp/sf-hor-b1','S').replace('.venv/bin/python -m pytest -p sf_job_paths','P')
    text+=f"| `{command}` | {row['exit']} | {row['seconds']} |\n"
text+='\n'+(scratch/'bootstrap-commands.md').read_text()
text+='''
The completion plumbing writes this report/evidence, stages those named files only, commits the report, creates the final branch bundle, verifies/unbundles it in isolated scratch, and confirms the advertised tip and release ancestry. The code-only trial bundle verification is logged above; the final tip is supplied with delivery because a commit cannot embed its own hash.

## Paragraph for #86

Rebased the two original commits onto release/217 at `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`. The contract resolution preserves all release change-log entries, prepends #86's dated C-23.25 amendment, takes PR C-23.25 and release C-23.26 verbatim, and records both resolutions in the documentation commit. A separate Hypothesis differential test freezes release's old install loop, proves install/uninstall agreement where it was correct, and pins the intended non-list uninstall preservation difference. All nine mutants are killed; equality/differential and contract checks pass. The targeted run passed 2,204 tests with 10 conditional skips; three skips in the unchanged core files need native process/boot-identity access in CI.
'''
report.write_text(text)
print('Wrote',report,'with',len(text.splitlines()),'lines and',len(set(selected)),'evidence files.')
