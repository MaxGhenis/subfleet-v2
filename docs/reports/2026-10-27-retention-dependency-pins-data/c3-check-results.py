"""Reconstruct current continuation coverage from complete per-node phases.

The unchanged base collection is continuation 2's actual 356-node collection.
Validated new tests are appended from their head-only green hold. Mutated tests
never count toward head coverage; a capped hold credits only completed nodes.
"""
from __future__ import annotations
import argparse,json
from collections import Counter
from pathlib import Path


def outcome(phases):
    if any(p['outcome']=='failed' for p in phases):return 'failed'
    teardown=any(p['phase']=='teardown' and p['outcome']=='passed' for p in phases)
    if teardown and any(p['outcome']=='skipped' for p in phases):return 'skipped'
    if teardown and all(any(p['phase']==part and p['outcome']=='passed' for p in phases)
                        for part in ('setup','call')):return 'passed'
    return 'incomplete'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--allow-open',action='store_true');args=parser.parse_args()
    data=Path(__file__).resolve().parent
    required=list(json.loads((data/'c2-guards-plan.json').read_text())['all_collected'])
    for path in sorted(data.glob('c3-killing-tests-green*.json')):
        record=json.loads(path.read_text())
        if record.get('exit')==0:
            required.extend(node for node in record['collected'] if node not in required)
    latest={}
    for path in sorted(data.glob('c3-*.json')):
        if 'mutant-' in path.name or path.name.endswith('-red.json'):continue
        record=json.loads(path.read_text())
        if not isinstance(record,dict) or 'phases' not in record:continue
        for node in {p['node'] for p in record['phases']}:
            phases=[p for p in record['phases'] if p['node']==node]
            row=dict(hold=path.name,start=record['start'],outcome=outcome(phases))
            if node not in latest or row['start']>latest[node]['start']:latest[node]=row
    joined={node:latest.get(node,dict(outcome='missing')) for node in required}
    counts=Counter(row['outcome'] for row in joined.values())
    result=dict(base_required=356,added_required=len(required)-356,required=len(required),counts=dict(counts),nodes=joined)
    (data/'c3-coverage.json').write_text(json.dumps(result,indent=2)+'\n')
    remaining=[node for node,row in joined.items() if row['outcome'] not in ('passed','skipped')]
    (data/'c3-remaining-nodes.txt').write_text(''.join(node+'\n' for node in remaining))
    print(json.dumps({key:result[key] for key in ('base_required','added_required','required','counts')}))
    if not args.allow_open:assert not remaining,counts


if __name__=='__main__':main()
