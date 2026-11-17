"""Install exact opaque/native-guardian exclusions only after the pool ends."""

import argparse
import ast
import json
from pathlib import Path

ROOT = Path('/private/tmp/sf-a2-fix1')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-pool-stopped', action='store_true', required=True)
    parser.parse_args()
    plugin = ROOT / 'sf_fix1_safety.py'
    source = plugin.read_text()
    before = """        if native_premise or holder:
            reason = ('sf-a2-fix1 safety guard: native process premise excluded before execution'
                      if native_premise else
                      'sf-a2-fix1 safety guard: native holder fixture excluded before execution')
"""
    after = """        opaque = (
            (item.path.name == 'test_launch_env_helper_scope.py'
             and getattr(item, 'originalname', item.name) ==
             'test_review_r2_the_isolated_inspection_receives_no_withheld_hook')
            or (item.path.name == 'test_salvage_finalization.py'
                and getattr(item, 'originalname', item.name) ==
                'test_c13_1_live_git_past_its_cap_is_tried_again_then_recorded')
            or (item.path.name == 'test_round10_refusal_context.py'
                and getattr(item, 'originalname', item.name) ==
                'test_real_guard_version_verdict_asks_for_the_supported_version'))
        native_guardian = (
            item.path.name == 'test_review_publish_env_round8.py'
            and getattr(item, 'originalname', item.name) in {
                'test_each_review_launch_keeps_its_pins_through_selection_and_the_guardian',
                'test_a_parent_s_pinned_names_reach_no_launch_that_is_not_a_review',
                'test_bounded_property_the_pins_hold_whatever_the_parent_or_env_pass'})
        if native_premise or holder or opaque or native_guardian:
            reason = ('sf-a2-fix1 safety guard: native guardian linger premise excluded before execution'
                      if native_guardian else
                      'sf-a2-fix1 safety guard: opaque synthetic executable fixture excluded before execution'
                      if opaque else
                      'sf-a2-fix1 safety guard: native process premise excluded before execution'
                      if native_premise else
                      'sf-a2-fix1 safety guard: native holder fixture excluded before execution')
"""
    if after not in source:
        assert source.count(before) == 1, 'review the changed collection hook before applying'
        source = source.replace(before, after, 1)
        ast.parse(source)
        plugin.write_text(source)
    print(json.dumps({'exact_opaque_functions': 3, 'expected_excluded_cases': 10,
                      'exact_native_guardian_functions': 3, 'expected_native_guardian_cases': 11,
                      'plugin_updated': True, 'tests_executed': 0}))


if __name__ == '__main__':
    main()
