import json, pathlib, subprocess
scratch=pathlib.Path('/private/tmp/sf-hor-b1')
source=pathlib.Path('subfleet/hooks.py')
pristine=source.read_bytes()
(scratch/'hooks-pristine.py').write_bytes(pristine)
base='tests/unit/test_hooks_install_equality.py::'
mutants=[
(1, 'else _place_ours(list(existing), event, resolved, group))', 'else [*_strip_ours(list(existing), event, resolved), group])', base+'test_any_permutation_holding_the_entry_once_is_installed',150),
(2, 'if _same(updated, existing):', 'if updated == existing:',base+'test_any_single_change_to_the_entry_is_drift_and_is_replaced_in_place',200),
(3, '        if _same(updated, existing):\n            continue', '        if _same(updated, existing):\n            proposed_hooks[event] = updated\n            continue',base+'test_an_unchanged_event_keeps_its_own_spelling_when_another_is_rewritten',1),
(4, '"duplicated" if ours > 1', '"differs" if ours > 1',base+'test_a_duplicate_is_drift_and_install_keeps_the_first',1),
(5, '            at = len(kept)\n        kept.extend(_strip_ours([existing], event, command))', '            at = len(kept) + len(_strip_ours([existing], event, command))\n        kept.extend(_strip_ours([existing], event, command))',base+'test_an_entry_shared_with_another_tools_hook_differs',1),
(6, '        return type(a) is type(b) and a == b\n    if isinstance(a, (int, float))', '        return a == b\n    if isinstance(a, (int, float))',base+'test_same_is_json_value_equality',300),
(7, 'if at is None and _holds_ours(existing, event, command):', 'if _holds_ours(existing, event, command):',base+'test_a_duplicate_is_drift_and_install_keeps_the_first',1),
(8, 'kept.insert(len(kept) if at is None else at, group)', 'kept.insert(0 if at is None else at, group)','tests/unit/test_hooks_install_differential.py::test_install_agrees_with_release_when_owned_entries_are_alone_and_last',150),
(9, '            "drift": report["drift"],\n', '',base+'test_a_missing_entry_is_drift_and_is_appended',1),
]
results=[]
try:
    for number,old,new,killer,examples in mutants:
        text=pristine.decode()
        assert text.count(old)==1,(number,text.count(old),old)
        source.write_text(text.replace(old,new,1))
        command=['.venv/bin/python','-m','pytest','-p','sf_job_paths',killer,'--basetemp='+str(scratch/('mutant-'+str(number))),'-q']
        result=subprocess.run(['python3',str(scratch/'run.py'),*command],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (scratch/('mutant-'+str(number)+'.log')).write_text(result.stdout)
        killed=result.returncode==1 and '1 failed' in result.stdout and 'AssertionError' in result.stdout
        row=dict(mutant=number,killer=killer,examples=examples,exit=result.returncode,killed=killed)
        results.append(row)
        print(json.dumps(row),flush=True)
        source.write_bytes(pristine)
        assert source.read_bytes()==pristine
        (scratch/'mutants.json').write_text(json.dumps(results,indent=2)+'\n')
        if not killed:
            print(result.stdout[-12000:],flush=True)
            break
finally:
    source.write_bytes(pristine)
