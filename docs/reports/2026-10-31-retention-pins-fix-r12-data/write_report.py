"""Render the fixed-row r12 evidence without inferring success from a queued hold."""
from collections import Counter
import json
from pathlib import Path
from tables import build, receipts

DATA = Path(__file__).resolve().parent
REPORT = DATA.parent / '2026-10-31-retention-pins-fix-r12.md'


def write():
    summary = build()
    runs = receipts()
    route_rows = json.loads((DATA / 'r12-routes.json').read_text())
    route_lines = ['| Input / reader | Route | Places / incomplete | Read before / after | Owner retired |',
                   '|---|---|---:|---:|---|']
    for row in route_rows:
        label = ('92a8635a0' if row['hold'].startswith('red-') else 'r12 repair') + ' / ' + row['reader']
        route_lines.append(f"| {label} | {row['route']} | {row['places']} / {row['incomplete']} | {row['before']} / {row['after']} | {'yes' if row['pruned'] else 'no'} |")
    differential = json.loads((DATA / 'differential-summary.json').read_text())
    ordinary = [r for r in runs if not r['name'].startswith(('red-', 'mutant-'))]
    ordinary_counts = Counter(p['outcome'] for r in ordinary for p in r['phases'] if p['phase'] == 'call')
    rows = []
    for r in runs:
        for o in r['observations']:
            if o['key'] in ('long-cycle', 'stat-table'):
                rows.append(dict(hold=r['name'], node=o['node'], kind=o['key'], **json.loads(o['value'])))
    (DATA / 'fixed-rows.json').write_text(json.dumps(rows, indent=2) + '\n')
    routine_lines = ['| Routine layout | Normal | Linked TMPDIR | Record equals 475f63278 |', '|---|---|---|---|']
    for layout in ('plain', 'worktree', 'shared', 'new-workdir', 'none'):
        checks = []
        for name in ('green-readable', 'linked-readable'):
            run = next((r for r in runs if r['name'] == name), {})
            observations = [json.loads(o['value']) for o in run.get('observations', []) if o['key'] == 'differential' and o['node'].endswith('test_routine_differential[' + layout + ']')]
            checks.append(len(observations) == 1 and observations[0]['actual'] == observations[0]['baseline'])
        routine_lines.append(f"| {layout} | {'pass' if checks[0] else 'pending'} | {'pass' if checks[1] else 'pending'} | {'yes' if all(checks) else 'pending'} |")
    mutant_note = ('The 16 r8/r9/r10 mutants die at byte-identical assertion expressions. '
                   '`r11-stat-gate-dropped` also dies at the same expression. '
                   '`r11-any-oserror-drops` dies in the same test at the same assertion site; '
                   'the expression changes from the old two-error expectation to the required ELOOP-only expectation. '
                   'The new r12 mutant dies at both named assertions. '
                   '`r11-enametoolong-kept` is retired because its behavior is now the product.')
    hold_table = (DATA / 'hold-table.md').read_text().replace('](', '](2026-10-31-retention-pins-fix-r12-data/')
    text = f'''# Retention dependency pins — fix round r12

R12-F1 is repaired by `e61c9e383`: `_unreadable_location` drops a failed spelling only on `ELOOP`. All other results keep the string `475f63278` kept. The four requested long-cycle rows expose a contradiction in decision 4's exact-baseline expectation; the executed counterexample and smallest test-only adjustment are documented below. No executable product decision is changed.

Started **2026-10-31 20:10:51 UTC**, input head **92a8635a05d6**. First hold wrapper admitted at **20:11:57 UTC**; the pytest evidence starts **20:12:07 UTC**. The hold-start cutoff is **23:10:51 UTC**, with a six-hour wall at **02:10:51 UTC November 1**.

The requested Darwin scratch mkdir was denied. All environments and fixtures use `/private/tmp/sf-fix272r12`; linked holds reach its fixture directory through `link-tmp`. Shared Git refused the branch ref lock. Four ordered commits are built using private metadata, and a verified bundle with `92a8635a0` as prerequisite is the delivery. The caller's checkout and original shared metadata are unchanged.

## Decisions 1–6 as built

1. The product predicate is exactly `return exc.errno == errno.ELOOP`. It runs only after an equation fails. Successful stat, `ENAMETOOLONG`, `ENOENT`, `ENOTDIR`, `EACCES` and every other error preserve the place. No other executable product code changes.
2. Review r12's walked-spelling trial is not applied. No second state map is added. `_target`, `check`, the component walker, `scan`, and list-before-check order remain unchanged.
3. D-R12-1 states the cost: a suffix-growing cycle whose canonical string exceeds `PATH_MAX` is kept unspelled, as at `475f63278`. Executed gitfile and commondir inputs at fixture bases of 371 and 500 bytes exercise real `ENAMETOOLONG`. Nothing reads through that cycle string; its only added effect is at most an extra pin until the job ends. The baseline records are **2 places / 4 incomplete** for gitfile and **3 / 3** for commondir. Each also contains one shorter ELOOP string (701 or 830 bytes), which decision 1 requires omitting. Head therefore records **1 / 4** and **2 / 3**, retaining the ENAMETOOLONG string and exact incomplete evidence. Decision 4's full equality is impossible under the stated invariant. The smallest adjustment is to expect the baseline record minus this shorter ELOOP string; product code remains unchanged. `decision-4-counterexample.json` preserves all four executed inputs.
4. Only the strict xfail decorator is removed from review r12. Its bodies, every earlier review file, both strategy modules and the cycle file are byte-identical. The readable module changes only the ENAMETOOLONG keep expectation, exact long-cycle record/count expectations with the documented decision-4 correction, and the differential's ELOOP-only assertion. Full ordered incomplete evidence is preserved.
5. The named mutant set is 19 definitions / 20 killer tests. {mutant_note}
6. The discovery docstring and desktop paragraph scope one spelling to a filesystem that holds still and strings the kernel can walk. ELOOP identifies a live cycle; ENAMETOOLONG rejects only one spelling. D-R12-1 is added, C-8.3/C-8.4 and the whole acceptance contract stay byte-identical, and the main report retains the entire dated r11 section. Review r12 stopped on a provider content filter with its work kept. Its report is partial and supplies no completed verdict; its opening provisional finding is not an approving review. The PR text asks for review r13.

## R12 routes and red/green

The unchanged review-r12 bodies run at `92a8635a0` with `--runxfail`: **2 passed / 2 failed**, both product failures at the owner-preservation assertion. After repair: **4 passed** normally and **4 passed** under linked TMPDIR. Both routes' fsck and pre-retention Git reads return 0. Neither route adds incomplete evidence. The baseline reader is `475f63278` throughout.

{chr(10).join(route_lines)}

Raw accepted paths are shorter than 1024 bytes while canonical absolute spellings exceed 1024 bytes; the real stale-string stat returns `ENAMETOOLONG`. Full paths, byte counts, records and retention results are retained in `r12-routes.json` and the hold receipts.

## Differential and routine controls

**{differential['comparisons']} same-input comparisons** across normal and linked runs. Every head git place is in the baseline set; each omitted string's real stat raises ELOOP and only ELOOP. The exact ordered incomplete lists match `475f63278` and the saved r10 reader. Dropped errno observations: **{dict(differential['dropped'])}**. The count includes repeated filesystem comparisons and generated inputs; it is not a count of unique spellings. Strategy bodies and settings remain unchanged.

{chr(10).join(routine_lines)}

The table's five full records equal `475f63278` in both environments. The two existing strategy modules are also instrumented for same-filesystem baseline/head comparisons without editing them. The 371/500-byte real-cycle records (including the initial expected-count failures) and the stat outcome table are retained in `fixed-rows.json`; only ELOOP drops. ENOENT/ENOTDIR/EACCES/ENAMETOOLONG use controlled equation failures because ordinary canonicalization preserves those unresolved strings; stat remains real. Success is a real case alias and ELOOP a real suffix cycle.

## Green results and scope

{summary['unique_passed']} distinct ordinary nodes pass across {len(summary['per_file'])} files. There are {ordinary_counts['passed']} passed call reports across normal, linked and repeated checks; the four initial decision-4 expectation failures are corrected. Every completed hold is locked: **{summary['locked']}**. Longest hold: **{summary['longest_seconds']:.2f} seconds**; largest pytest process RSS: **{summary['peak_rss_mib']:.2f} MiB**. No hold exceeds ten minutes. Each hold runs one serial pytest, within the four-worker ceiling. The harness retains queue receipts, actual pytest starts, loads, per-phase outcomes and observations.

The run scope is exactly the ten green files, those ten plus r8/dependency-units/fake-dependencies through linked TMPDIR, the ten files under brief-fix-r8's **The dependency set**, and the three lane-rule guards. No new adversarial input or worst-case search is added.

Uncompleted planned holds: {', '.join(summary['planned_holds_not_completed']) or 'none'}. Missing or unfinished collected ordinary nodes: {', '.join(summary['missing_or_unfinished']) or 'none'}. The four original long-cycle count expectations failed, then all four passed after the documented test-only correction. One cycle hold was paused while that counterexample was recorded and later rerun to completion.

Raw wrapper logs are retained byte for byte. Their blank `load ` lines contain trailing spaces after the sandbox rejects `sysctl`; the JSON receipts retain actual `os.getloadavg` values. The whitespace check covers source, prose and structured evidence, excluding raw `.log` files.

The R-files, turns, admission/archive holds and r10's extra files are outside this round's narrowed run list; they were not run. In particular, no archive path-trap rows or live-lsof row were run. The full suite and live frontend remain for CI; `tests/frontend/test_core_live.py` cannot run on a lane. No real CLI, provider, live store, fetch, merge, push, GitHub call or remote write is used.

## Per-file counts

{(DATA / 'file-table.md').read_text()}
## Every hold, start time and load

Each ordinary hold selects one file, so its receipt gives that file's count, UTC start and load. Mutant receipts select only named killers. A hold interrupted by its nine-minute guard is never counted as a pass.

{hold_table}
## Mutants

{mutant_note}

{(DATA / 'mutant-table.md').read_text()}
## PR-body replacements

{(DATA / 'pr-body-lines.md').read_text() if (DATA / 'pr-body-lines.md').exists() else 'Pending final evidence.'}

## Delivery and cleanup

The ordered commits are repair with the flipped mark, tests and ordinary evidence, named mutants, then text and final evidence. Every commit ends with the required co-author trailer. The delivery is `retention-pins-fix-r12.bundle` in the worktree, with `92a8635a0` as prerequisite; verify it before handoff. Cleanup uses the lane rules' guarded deletion and records disk allocation, removal and artifact checks in `cleanup.json`. All test and mutant processes finish before cleanup and delivery.
'''
    if (DATA / 'cleanup.json').exists():
        cleanup = json.loads((DATA / 'cleanup.json').read_text())
        text += f"\nScratch allocation before cleanup: {cleanup['allocated_before_cleanup']} bytes. Owned scratch roots were removed; bytecode, Hypothesis, build and kept-failure artifacts are absent. Private Git metadata is removed after bundle verification.\n"
    REPORT.write_text(text)
    return summary


if __name__ == '__main__':
    write()
