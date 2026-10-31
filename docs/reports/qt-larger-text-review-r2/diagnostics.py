"""Executed inputs for decisions 1, 3 and 6; production scenes stay unchanged."""
import ast
import json
from pathlib import Path

import pytest

from tests.frontend.conftest import run_probe
from tests.frontend.swift import ROOT, compile_probe
from tests.frontend import test_review_r219_fix10_view as shrink

ARTIFACTS = Path(__file__).resolve().parent
REFERENCES = ROOT / 'docs/reports/qt-larger-text-r1b'
SCRATCH = Path('/private/tmp/sf-r284r2')


def diagnostic_source(tmp_path, mode, extra=''):
    source = (ROOT / 'tests/frontend/ReviewR219Fix8ViewProbe.swift').read_text()
    if mode == 'navigation':
        old = 'ConversationView(model: model, conversation: shown)\n'
        assert source.count(old) == 1
        source = source.replace(old, 'ConversationView(model: model, conversation: shown).id(cid)\n')
        anchor = '        if CommandLine.arguments.last == "fix10-resize-schedule" {'
        assert source.count(anchor) == 1
        source = source.replace(anchor, '''        if CommandLine.arguments.last == "navigation-matrix" {
            let result = await navigationMatrix(URL(fileURLWithPath: CommandLine.arguments[1]))
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if CommandLine.arguments.last == "navigation-input" {
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


@pytest.fixture(scope='module')
def navigation_binary(tmp_path_factory):
    return diagnostic_source(tmp_path_factory.mktemp('r2-navigation'), 'navigation')


def test_navigation_drops_a_pending_reveal_without_suspending_b(tmp_path, navigation_binary):
    binary = navigation_binary
    result = run_probe(binary, tmp_path, 'navigation-input', timeout=240)
    (ARTIFACTS / 'navigation-input.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['a_suspended'] and result['a_pending'] and result['pin_replaced'], result
    requested = next(i for i, e in enumerate(result['a_trace']) if e['event'] == 'reveal')
    after_request = result['a_trace'][requested:]
    assert not any(e['event'] in {'reveal_landed', 'person_scrolled', 'reveal_abandoned'}
                   for e in after_request), result
    assert all(e['suspended'] for e in after_request if 'suspended' in e), result

    assert result['b_caught_up'] and result['b_position']['from_end'] <= shrink.END, result
    states = [e for e in result['b_trace'] if 'suspended' in e]
    assert states and all(not e['suspended'] for e in states), result
    assert any(e['event'] == 'move' and e['move'] == 'jump' for e in states), result


def test_native_two_reveal_landing_and_real_release_count(tmp_path, monkeypatch):
    binary = diagnostic_source(tmp_path, 'two-reveals')
    result, steps, _ = shrink.shrink_replay(binary, tmp_path, 'landed', 'larger_text')
    (ARTIFACTS / 'two-reveal-input.json').write_text(json.dumps(result, indent=2) + '\n')
    assert steps['landed']['reveals'] == 2, result
    shrink.assert_landed_shrink_order(result, steps)
    shrink.assert_landed_shrink_releases(result, steps)
    assert len(shrink.releases_after(result, steps['shrinking']['at'])) == 1, result
    # Exercise the changed regression function on this fresh native input.
    monkeypatch.setattr(shrink, 'shrink_replay', lambda *args: (result, steps, 'native two-reveal input'))
    shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, 'larger_text')
    with pytest.raises(AssertionError):
        assert steps['landed']['reveals'] == 1
    old = definitions(REFERENCES / 'old-order-helpers.py', dict(vars(shrink)))
    (ARTIFACTS / 'fresh-two-comparison.json').write_text(json.dumps({
        'reveals': steps['landed']['reveals'],
        'current_regression': 'passed order, landing and follow behavior',
        'old_exact_one': 'rejected',
        'old_release_count': len(old['releases_after'](result, steps['shrinking']['at'])),
        'real_release_count': len(shrink.releases_after(result, steps['shrinking']['at'])),
    }, indent=2) + '\n')



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
    old = definitions(REFERENCES / 'old-order-helpers.py', dict(vars(shrink)))
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


@pytest.mark.parametrize('order', [
    'change,reveal,shrink', 'change,shrink,reveal',
    'reveal,change,shrink', 'reveal,shrink,change',
    'shrink,change,reveal', 'shrink,reveal,change',
])
def test_navigation_change_reveal_shrink_orders(tmp_path, navigation_binary, order):
    result = run_probe(navigation_binary, tmp_path, 'navigation-matrix',
                       env={'R2_NAVIGATION_ORDER': order}, timeout=240)
    (ARTIFACTS / ('navigation-' + order.replace(',', '-') + '.json')).write_text(
        json.dumps(result, indent=2) + '\n')
    assert result['a_suspended'] and result['pin_replaced'], result
    assert result['b_caught_up'] and result['b_position']['from_end'] <= shrink.END, result
    states = [e for e in result['b_trace'] if 'suspended' in e]
    assert states and not states[-1]['suspended'], result
    assert any(e['event'] == 'move' and e['move'] == 'jump' for e in states), result


def test_six_release_sensitive_mutant_runs_failed_for_measured_behavior():
    from tests.frontend.test_review_r219_fix8_mutations import ROUND7

    rows = []
    for name in ['round7-marker', 'round7-distance', 'round7-never-lands', 'round7-larger']:
        paths = sorted((SCRATCH / 'cases' / name).rglob('result.json'))
        assert len(paths) == (3 if name == 'round7-larger' else 1), (name, paths)
        for path in paths:
            result = json.loads(path.read_text())
            assert result['layout']['text_scale'] == (1.728 if name == 'round7-larger' else 1), result
            assert result['layout']['window_height'] == 760, result
            steps = {step['step']: step for step in result['steps']}
            mutation = ('reveal_never_lands' if result['mode'] == 'landed' else
                        'marker_releases_pending_reveal' if 'marker_callback' in steps else
                        'confirm_by_distance_only')
            _, order, rule = ROUND7[mutation]
            order(result, steps)
            with pytest.raises(AssertionError) as rejected:
                rule(result, steps)
            cutoff = steps['shrinking']['at'] if result['mode'] == 'landed' else steps['reveal']['at']
            releases = shrink.releases_after(result, cutoff)
            tick = shrink.settling_tick(result, steps)
            assert rejected.traceback[-1].name == rule.__name__, rejected.getrepr(style='short')
            if mutation == 'reveal_never_lands':
                assert not releases and tick['suspended'] and tick['move'] == 'stay', result
            else:
                assert releases and not tick['suspended'] and tick['move'] == 'jump', result
                if mutation == 'marker_releases_pending_reveal':
                    assert releases[0]['event'] == 'marker_appeared', result
                else:
                    assert releases[0]['event'] == 'reveal_end_visible', result
                    assert any(e['event'] == 'reveal_check' and not e['presented']
                               and e['distance'] <= shrink.END for e in result['trace']), result
            rows.append({'run': name, 'mutation': mutation, 'order': 'passed',
                         'failed_rule': rule.__name__, 'cutoff': cutoff,
                         'release_count': len(releases),
                         'release_triggers': [e['event'] for e in releases],
                         'tick': tick, 'caught_up': steps['caught_up']['position']})
    assert len(rows) == 6
    (ARTIFACTS / 'release-mutant-audit.json').write_text(json.dumps(rows, indent=2) + '\n')
