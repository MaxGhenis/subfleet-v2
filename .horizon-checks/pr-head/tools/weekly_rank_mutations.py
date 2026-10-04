"""Run scheduler ranking mutants against C-11.3's generated laws.

    .venv/bin/python tools/weekly_rank_mutations.py

Use `--only NAME ...` to split the checks into short foreground slices.

Each worker compiles one modified `rank_key` in memory and runs the real
scheduler through its Hypothesis fleet properties. Source files are never
rewritten. A mutation counts as killed only by a failing test assertion, not
by collection errors or a mutation that no longer applies. Workers run in the
foreground and are allowed to finish; the checker never terminates processes.
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/unit/test_scheduler_weekly.py"
MUTATIONS = {
    "latest-reset-first": (
        'detail["seven_day_reset"] or "9999"',
        '-_time(detail["seven_day_reset"]).timestamp() if detail["seven_day_reset"] else float("inf")',
        "earlier_weekly_reset_never_ranks_worse",
    ),
    "less-weekly-headroom-first": (
        '-(detail["weekly_headroom"] or 0)',
        '+(detail["weekly_headroom"] or 0)',
        "equal_resets_more_weekly_headroom_never_ranks_worse",
    ),
    "ignore-weekly-reserve": (
        'detail["weekly_reserve"]',
        'False',
        "a_lane_under_either_reserve_never_precedes",
    ),
    "ignore-five-hour-reserve": (
        'detail["five_hour_reserve"]',
        'False',
        "a_lane_under_either_reserve_never_precedes",
    ),
    "swap-reserve-precedence": (
        'detail["weekly_reserve"], detail["five_hour_reserve"]',
        'detail["five_hour_reserve"], detail["weekly_reserve"]',
        "weekly_reserve_precedes_five_hour_reserve",
    ),
    "ignore-load-band": (
        'band = detail["in_flight"] // spread if spread else 0',
        'band = 0',
        "load_band_precedes_measured_and_weekly_preferences",
    ),
    "ignore-measured-precedence": (
        'not detail["measured"]',
        'False',
        "measured_precedes_unmeasured_in_the_same_band",
    ),
    "ignore-desktop-precedence": (
        'desktop = bool(detail.get("desktop"))',
        'desktop = False',
        "desktop_last_and_a_turns_affinity_first",
    ),
    "ignore-affinity-precedence": (
        'identity != affinity',
        'False',
        "desktop_last_and_a_turns_affinity_first",
    ),
    "ignore-claude-stranded-precedence": (
        'not bool(detail.get("stranded_scopes")))',
        'True)',
        "claude_stranded_term_remains_before_measured_and_reserve_preferences",
    ),
}


def worker(name: str) -> int:
    import pytest
    from hypothesis import Phase, settings
    from subfleet import scheduler

    # Killing needs a real failing generated case, not a minimized or explained
    # counterexample. Skip those extra phases so foreground mutation slices stay
    # short; the ordinary property suite retains all of its default phases.
    settings.register_profile("weekly-rank-mutant", phases=[Phase.generate])
    settings.load_profile("weekly-rank-mutant")
    original, replacement, law = MUTATIONS[name]
    source = inspect.getsource(scheduler.rank_key)
    if source.count(original) != 1:
        print(f"Mutation no longer applies: {name}")
        return 2
    scope = dict(vars(scheduler))
    exec(compile(source.replace(original, replacement), f"<weekly-rank-mutant:{name}>", "exec"), scope)
    scheduler.rank_key = scope["rank_key"]
    return int(pytest.main(["-q", "-x", "-p", "no:cacheprovider", TESTS, "-k", law]))


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--mutation":
        return worker(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=list(MUTATIONS), default=list(MUTATIONS))
    selected = parser.parse_args().only
    failed = []
    for name in selected:
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--mutation", name],
                             cwd=ROOT, capture_output=True, text=True)
        assertions = [line for line in run.stdout.splitlines() if line.startswith("FAILED ")]
        killed = run.returncode == 1 and bool(assertions) and "AssertionError" in run.stdout
        print(f"{'KILLED' if killed else 'SURVIVED/ERROR'} {name}"
              + (f": {assertions[0].split(' - ')[0][7:]}" if assertions else ""), flush=True)
        if not killed:
            failed.append(name)
            print((run.stdout + run.stderr)[-3000:])
    print(f"{len(selected) - len(failed)}/{len(selected)} ranking mutations killed", flush=True)
    return int(bool(failed))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
