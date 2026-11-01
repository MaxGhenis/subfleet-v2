"""Assemble final prose from completed receipts, without changing test strategies."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
summary = json.loads((DATA / 'summary.json').read_text())
mutants = json.loads((DATA / 'mutant-results.json').read_text())
classes = json.loads((DATA / 'spelling-classes.json').read_text())
status = json.loads((DATA / 'completion-status.json').read_text())
if status['complete']:
    coverage_note = 'All requested holds and selected nodes completed.'
else:
    names = ', '.join('`' + node + '`' for node in status['unfinished_nodes'])
    coverage_note = (f"All {status['planned_holds']} planned holds ended. "
                     f"{len(status['unfinished_nodes'])} selected properties remain incomplete after the nine-minute guards: {names}.")
    if status['missing_holds']:
        coverage_note += f" {len(status['missing_holds'])} planned holds were unstarted."

assert len(classes) == 2
for observed in classes.values():
    assert observed['inputs'] == observed['distinct'] == 55, observed
    assert len(observed['cells']) == 33, observed
    assert observed['outcomes'] == {'needs-owner': 55}, observed
assert len(mutants) == 16 and all(m['result'] == 'Killed' for m in mutants)
prior = [m for m in mutants if m['name'].startswith(('r8-', 'r9-'))]
archive_failures = []
for path in DATA.glob('archive-*.json'):
    v = json.loads(path.read_text())
    if isinstance(v, dict) and 'phases' in v:
        archive_failures += sorted({p['node'] for p in v['phases'] if p['outcome'] == 'failed'})
report_controls = 23
product_count = summary['ordinary_passing_nodes'] - report_controls
same = sum(m['same_r9_assertion'] is True for m in prior)
pr = f'''**Fix round r10 repairs R10-F1: every returned discovery place has one spelling, including incomplete discoveries.** A shared predicate filters unspelled canonical storage strings and scanned targets at the return boundary. Physical link entries, successful file locations and existing incomplete evidence stay. Discovery builds its return list before alias revalidation, so a link or resolved target changed into a cycle produces the existing alias-changed evidence. The hub's seven-route table widened the scan-only trial to cover both return paths. Retention stays dormant pending approving review r11 and completed CI.

- **Evidence:** review r10 goes from 10 passed / 2 failed with `--runxfail` to 12 passed; the new 20-case cycle file goes from 10 passed / 10 failed to 20 passed. All nine route/cycle rows satisfy both equations; each stable route preserves its exact incomplete list. Every dropped baseline string raises `ELOOP`, and healthy sibling storage matches `475f6327890e` with the cycle removed. Five routine records equal that head. {product_count} distinct product-test nodes and {report_controls} report controls pass; linked-TMPDIR holds pass. {coverage_note} All 16 named mutants die; {same}/13 prior mutants fail at their r9 assertion. Both spelling-strategy files and C-8.3/C-8.4 are unchanged.

**Next:** review r11 judges fix round r10. The hub completes all-pass CI and mergeability checks. Activation remains a separate step.
'''
(DATA / 'pr-body-lines.md').write_text(pr)
route_table = (DATA / 'route-table.md').read_text()
routines = json.loads((DATA / 'routine-comparisons.json').read_text())
routine_table = '| Layout | Input-head places | Head places | Exact record equality |\n|---|---:|---:|---|\n'
for item in routines:
    assert item['before'] == item['after']
    routine_table += f"| {item['layout']} | {len(item['before'].get('git', []))} | {len(item['after'].get('git', []))} | Yes |\n"
file_table = (DATA / 'file-table.md').read_text()
mutant_table = (DATA / 'mutant-table.md').read_text()
partial = json.loads((DATA / 'partial-holds.json').read_text())
partial_description = '\n'.join(f"- {name}: nine-minute guard; {len(value.get('initial_unfinished', value['remaining']))} calls were initially unfinished; {len(value['remaining'])} still lack a completed passing call." for name,value in partial.items()) or 'No hold required a partial rerun.'
class_description = '\n'.join(f"- {name}: {v['inputs']} observations, {v['distinct']} distinct inputs, {len(v['cells'])} nonempty cells, {v['outcomes']}." for name,v in classes.items())
failed_archive = '\n'.join('- `' + n + '`' for n in sorted(set(archive_failures)))
report = f'''# Retention dependency pins: fix round r10

**R10-F1 is repaired: every returned discovery place has one spelling, whether or not discovery is complete.** Canonical storage strings and resolved scan targets must meet both return equations. Physical link entries and successful file locations keep their existing treatment. The returned list is built before the existing alias check. Retention stays dormant pending approving review r11 and completed CI.

Started **2026-10-31 11:33:45 UTC** at **475f6327890e**. The first pytest started at 11:35:55 UTC, inside the first hour. The hold-start cutoff was 15:33:45 UTC. The macOS scratch path was denied; all scratch used `/private/tmp/sf-fix272r10`. Shared Git metadata refused `index.lock`, so the assigned branch `fix/retention-dependency-pins-217` is committed with worktree-local `.lane-git-r10` metadata. The verified delivery bundle is `fix272r10.bundle` in this worktree, with exactly `475f6327890e` as its prerequisite. No shared branch or history was rewritten.

## Decisions as built

1. One small `_spelled_location` predicate states `canonical(dirname(p)) + basename(p) == p` and `canonical(p) == p`. It filters `found`'s canonical strings in `git_storage` and `self.entries` in `locations`. It does not filter `links` or `files`; `scan` has no added guard.
2. `git_storage` builds both portions of its returned list, runs `aliases.check(incomplete)`, then returns. Two real-Git races retarget the scanned link itself or replace its resolved target with a cycle in `locations`, after resolution and after the old check position. Both now produce the existing alias-changed evidence. The named check-before-spelling mutant fails the resolved-target race's evidence assertion, demonstrating the silent-drop regression.
3. An unused cyclic scan reports complete evidence. The filter adds no evidence. On every stable route, the full incomplete list equals the input-head code on the identical filesystem, including order, file and reason. The unit tests pin each entry count and every file independently of errno wording. Every removed baseline string is separately checked with `os.stat` and raises `ELOOP`.
4. The new `tests/unit/test_retention_location_cycles.py` uses real Git for the hub's seven routes plus two-link-suffix scan and alternates. All nine have exact evidence and spelling checks. A healthy sibling repository keeps every previously recorded place, and the report controls compare it to input-head code on the same input with the cycle removed. Both new race variants are covered. Only review r10's strict decorator was removed; its bodies remain byte-identical. Neither spelling-property strategy nor the earlier location-input module changed.
5. All three r10 mutants and the four r9 and nine r8 mutants were run against their named killers, each in one hold. Results and assertion comparisons are below. No other mutation or generated-reversion sweep was run.
6. The discovery docstring and desktop discovery description explain the cost: an unspelled place inside a cycle is omitted because it cannot be read through; physical link entries and incomplete evidence stay. The unconditional invariant stands. Review r10's proposed temporary listed-difference line is retired, not inserted. The existing listed-differences section and entire acceptance contract, including C-8.3 and C-8.4, are unchanged. The main report has a Review r10 and fix round r10 section, and this report supplies PR-body replacements.

No implementation decision was widened or replaced. The hub's route table, rather than the scan-only trial, is the explicit reason both `found` and `entries` are filtered. The 64-hop walker, deeper-reader boundaries and later-change/ABA observations remain unchanged. No new product gap was found.

## Route table: input head and repair

Base is `475f6327890e`. Head columns describe the final repair behavior. The nine comparative controls also include the healthy sibling repository; that adds only healthy storage and no incomplete entries or unspelled strings. Full before/after records, identical incomplete lists, dropped unreadable paths and healthy-storage lists are in `route-comparisons.json`.

{route_table}

Let `L64 = <base>/loop/` followed by 64 `tail` components; for two-link-suffix it is 32 `extra/tail` pairs. The exact error files are:

- Control and scan: none.
- Alternates and objects-link/alternates: `L64` and `L64/info/alternates`.
- Alternates-tail: `L64/x` and `L64/x/info/alternates`.
- Gitfile: `L64`, `<base>/loop/<128 tails>/objects`, `L64/objects/info/alternates`, `L64/commondir`, in that order.
- Commondir: the first three gitfile entries.

The two-link alternates row substitutes its paired suffix in the same two files. These are pinned in the new unit file; no filesystem error prose is pinned there.

## Five routine controls against 475f6327890e

Plain repository, worktree-add checkout, shared clone, nested git-new-workdir layout and no-Git folder each execute the snapshot of the input-head reader on the same real-Git fixture as the repair. All satisfy `baseline == repaired == expected`, including exact list order. `routine-comparisons.json` contains both full records. The unchanged 19-case dependency-unit file also passes under linked TMPDIR.

{routine_table}

## Red, green and generated spelling controls

Review r10's original file with `--runxfail`: **10 passed / 2 failed**, both expected invariant failures. The final new cycle module at input-head code: **10 passed / 10 failed**, eight non-control cyclic routes and both race-evidence assertions. An earlier 19-case draft is retained as red-cycles; the final module has 20 cases. At the repair: **12/12** and **20/20**, respectively. All seven green files and all ten linked-TMPDIR files pass. No mark or strategy was weakened.

{class_description}

The existing deterministic property still uses 55 draws, no database and no explicit examples. Its unchanged strategy reaches all 33 position/spelling cells in both runs. Per-input observations remain in the receipts and the counted table is in `spelling-classes.json`.

## Requested holds and per-file counts

**{product_count} distinct product-test nodes plus {report_controls} report controls pass.** Repeated linked runs count once in the union. Every hold has one serial pytest, no xdist. The maximum measured pytest concurrency is {summary['peak_pytest_concurrency']}; all {summary['locked_runs']} completed invocations held the shared lock. No invocation ran outside it. Longest invocation: {summary['longest_seconds']:.2f} seconds; the runner interrupts at nine minutes. Peak pytest RSS: {summary['peak_rss_mib']:.1f} MiB. The wrapper's sysctl load header is denied by the sandbox; complete load vectors are recorded with os.getloadavg inside the admitted pytest process. Per-file union counts and first passing starts/loads (first attempt for zero-pass files) follow; `hold-table.md` includes every repeated, red, environment-failing and mutant hold.

{file_table}

Under `/private/tmp`, these six archive nodes fail because their source path is classified as scratch:

{failed_archive}

They pass unchanged with TMPDIR in this worktree's new `sf-fix272r10-fixtures` folder, removed at cleanup. The live archive case `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` was explicitly excluded from every archive selection. Additional worktrees, QoS and regenerable-property files pass.

## Bounded holds

{partial_description}

The r4/r2 group is split into six sequential holds; the manifest is `split-holds.json`. {coverage_note}

## Named mutants

Each mutant has one failing call assertion and passing setup/teardown. The runner records the original and mutated source hashes. The prior-assertion column compares against fix r9; r8 comparisons against fix r8 are also retained in `mutant-results.json`.

{mutant_table}

The four r9 and nine r8 mutants all die; {same}/13 match their previous r9 assertion. All nine r8 rows also match their r8 assertions (9/9). The exact expressions and independent comparisons are recorded without substituting a new killer.

## PR-body lines

{pr}

## Delivery and cleanup

The commits are ordered repair with the flipped mark, tests and focused evidence, named mutants, then text and remaining receipts. Every message ends with the required co-author trailer. The final head and verified bundle path are given in the delivery response. No fetch, merge or remote push of the working repository, GitHub call, real provider, real Subfleet CLI or live store was used. All Git repositories and Subfleet stores under test are isolated fixtures. Each started process was waited to completion; none detached. Scratch stayed below 1.5 GB and was removed with the prescribed guarded cleanup; `cleanup.json` records the final measurements and removals. The initial guarded removal left the archive interruption fixture's read-only directory. Owner permissions were restored only on that job-owned directory, and the guarded removal then completed. `cleanup-errors.json` retains the initial errors. Bytecode, Hypothesis storage and worktree fixture roots were removed.

Not run: the mandated live-lsof exclusion; the full suite, Python 3.12, person-only frontend live probe and remote CI; older mutants and generated-only reversions outside this round's named list. {coverage_note}
'''
if not status['complete']:
    report += '\n## Holds and nodes not completed\n\n' + (DATA / 'uncompleted-table.md').read_text()
    if status['unresolved_failures']:
        report += '\nUnresolved ordinary failures: `' + json.dumps(status['unresolved_failures'], sort_keys=True) + '`\n'
    report = report.replace('Additional worktrees, QoS and regenerable-property files pass.',
        'The three additional files are included in the run manifest; any incomplete selection is listed below.')
# Describe only archive controls that actually ran and were retried.
if not archive_failures:
    start = report.index('Under `/private/tmp`, these six archive nodes fail')
    end = report.index('## Bounded holds', start)
    report = report[:start] + 'No completed archive invocation recorded a path-classification failure. Unstarted archive holds, if any, are listed below.\n\n' + report[end:]
else:
    report = report.replace('these six archive nodes fail', f'these {len(set(archive_failures))} archive nodes fail')
    retry = DATA / 'archive-path-controls.json'
    if not retry.exists() or json.loads(retry.read_text()).get('exit') != 0:
        report = report.replace('They pass unchanged with TMPDIR in this worktree\'s new `sf-fix272r10-fixtures` folder, removed at cleanup.',
            'Their worktree-TMPDIR rerun did not finish inside the hold-start window; incomplete selections are listed below.')
(ROOT / 'docs/reports/2026-10-31-retention-pins-fix-r10.md').write_text(report)
print('Wrote final report and PR-body lines.')
