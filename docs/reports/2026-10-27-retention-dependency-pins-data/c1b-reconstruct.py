"""Verify salvaged plan expansion and remove only complete, successful results."""
import collections
import json
import runpy
from pathlib import Path
from _pytest.mark import KeywordMatcher
from _pytest.mark.expression import Expression

DATA = Path(__file__).resolve().parent
original = (DATA / ('c1-original-remaining-nodes.txt' if (DATA / 'c1-original-remaining-nodes.txt').exists() else 'remaining-nodes.txt')).read_text()
runpy.run_path(str(DATA / 'c1-reconstruct.py'))
assert (DATA / 'remaining-nodes.txt').read_text() == original
plans = json.loads((DATA / 'c1-planned-nodes.json').read_text())
collected = json.loads((DATA / 'c1-collect-plans-collected.json').read_text())
for plan in plans:
    args = dict(json.loads((DATA / plan['plan']).read_text()))[plan['label']]
    targets = [a for a in args if a.startswith('tests/')]
    expr = Expression.compile(args[args.index('-k') + 1]) if '-k' in args else None
    selected = []
    for node in collected:
        if not any(node == t or node.startswith(t + '::') for t in targets):
            continue
        matcher = KeywordMatcher(set(node.replace('::', '/').split('/')))
        if expr is None or expr.evaluate(matcher):
            selected.append(node)
    assert selected == plan['nodes'], (plan['plan'], plan['label'], set(selected) ^ set(plan['nodes']))
remaining = set(original.splitlines())
completed = {}
for file in sorted(DATA.glob('c1-*-results.jsonl')):
    reports = collections.defaultdict(dict)
    for line in file.read_text().splitlines():
        row = json.loads(line)
        reports[row['nodeid']][row['when']] = row['outcome']
    done = {node for node, phases in reports.items()
            if phases.get('teardown') == 'passed'
            and (phases.get('call') in ('passed', 'skipped') or phases.get('setup') == 'skipped')
            and 'failed' not in phases.values()}
    completed[file.name] = sorted(done)
    remaining -= done
assert len(original.splitlines()) == 761
assert len(remaining) == 759
(DATA / 'c1-original-remaining-nodes.txt').write_text(original)
(DATA / 'remaining-nodes.txt').write_text(''.join(n + '\n' for n in sorted(remaining)))
(DATA / 'c1b-salvage-accounting.json').write_text(json.dumps({
    'head': '6ca8f4b0bc36eca4142b7183a2de5c3eab24e300',
    'plans_verified': len(plans), 'original_remaining': 761, 'remaining': len(remaining),
    'completed_salvaged_results': completed,
}, indent=2) + '\n')
print('Verified plans; 761 original remaining, 2 more completed in salvage, 759 to run.')
