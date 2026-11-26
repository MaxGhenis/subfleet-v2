"""Executed inputs for decisions 1, 3 and 6; production scenes stay unchanged."""
import ast
import json
from pathlib import Path

import pytest

from tests.frontend.conftest import run_probe
from tests.frontend.swift import ROOT, compile_probe
from tests.frontend import test_review_r219_fix10_view as shrink

ARTIFACTS = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-fix284r1b')


def diagnostic_source(tmp_path, mode, extra=''):
    source = (ROOT / 'tests/frontend/ReviewR219Fix8ViewProbe.swift').read_text()
    if mode == 'navigation':
        old = 'ConversationView(model: model, conversation: shown)\n'
        assert source.count(old) == 1
        source = source.replace(old, 'ConversationView(model: model, conversation: shown).id(cid)\n')
        anchor = '        if CommandLine.arguments.last == "fix10-resize-schedule" {'
        assert source.count(anchor) == 1
        source = source.replace(anchor, '''        if CommandLine.arguments.last == "navigation-input" {
            let result = await navigationInput(URL(fileURLWithPath: CommandLine.arguments[1]))
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
''' + anchor)
        source += '\n' + (ARTIFACTS / 'NavigationInput.swift').read_text()
    elif mode == 'two-reveals':
        old = '} while revealUnmet() != nil && reveals < 3'
        assert source.count(old) == 1
        source = source.replace(old, '} while (reveals < 2 || revealUnmet() != nil) && reveals < 3')
    path = tmp_path / (mode + '.swift')
    path.write_text(source + extra)
    return compile_probe(tmp_path / 'probe', path, 'SUBFLEET_VIEW_TEST', timeout=900)


def test_navigation_drops_a_pending_reveal_without_suspending_b(tmp_path):
    binary = diagnostic_source(tmp_path, 'navigation')
    result = run_probe(binary, tmp_path, 'navigation-input', timeout=240)
    (ARTIFACTS / 'navigation-input.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['a_suspended'] and result['a_pending'] and result['pin_replaced'], result
    assert result['b_caught_up'] and result['b_position']['from_end'] <= shrink.END, result
    states = [e for e in result['b_trace'] if 'suspended' in e]
    assert states and all(not e['suspended'] for e in states), result
    assert any(e['event'] == 'move' and e['move'] == 'jump' for e in states), result


def test_native_two_reveal_landing_and_real_release_count(tmp_path):
    binary = diagnostic_source(tmp_path, 'two-reveals')
    result, steps, _ = shrink.shrink_replay(binary, tmp_path, 'landed', 'larger_text')
    (ARTIFACTS / 'two-reveal-input.json').write_text(json.dumps(result, indent=2) + '\n')
    assert steps['landed']['reveals'] == 2, result
    shrink.assert_landed_shrink_order(result, steps)
    shrink.assert_landed_shrink_releases(result, steps)
    assert len(shrink.releases_after(result, steps['shrinking']['at'])) == 1, result


def definitions(path, namespace):
    source = path.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def test_saved_attempt2_old_and_new_helpers():
    log = Path('/Users/maxghenis/reviews/subfleet-hub/qt-larger-text-2026-10-30/log-282-attempt2-shard3-under-pending.txt').read_text()
    result = json.loads((ROOT / 'tests/fixtures/qt-flake/pr284-pending-attempt2.json').read_text())
    # Confirm the fixture against the executed CI diagnostic, rather than memory.
    decoder = json.JSONDecoder()
    candidates = []
    for line in log.splitlines():
        marker = 'shrink replay:'
        if marker in line and '{"' in line:
            try:
                candidates.append(decoder.raw_decode(line[line.index('{"'):])[0])
            except json.JSONDecodeError:
                pass
    assert result in candidates, 'saved fixture must match the supplied #282 log'
    steps = {s['step']: s for s in result['steps']}
    old = definitions(ARTIFACTS / 'old-order-helpers.py', dict(vars(shrink)))
    with pytest.raises(AssertionError):
        old['assert_replayed_shrink_order'](result, steps)
    shrink.assert_replayed_shrink_order(result, steps)
    old['assert_shrink_releases_nothing'](result, steps)
    shrink.assert_shrink_releases_nothing(result, steps)
    checks = [e for e in result['trace'] if e['event'] == 'reveal_check'
              and e['distance'] <= shrink.END
              and steps['shrinking']['at'] <= e['at'] <= steps['shrunk']['at']]
    assert checks and steps['shrinking']['reveal']['outstanding']
    (ARTIFACTS / 'attempt2-comparison.json').write_text(json.dumps({
        'old_order': 'rejected', 'new_order': 'accepted',
        'old_behavior': 'passed', 'new_behavior': 'passed',
        'pending': steps['shrinking']['reveal'], 'geometry_checks': checks,
    }, indent=2) + '\n')
