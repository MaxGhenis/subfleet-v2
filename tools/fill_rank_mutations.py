"""Apply each D-FILL1 mutant alone and restore byte-identical pristine files.

Run while no other process edits the scheduler or policy. Requires a fresh
--basetemp directory; each child pytest run gets a separate directory below it.
Only assertion failures count as kills. No git checkout or history is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/unit/test_scheduler_fill.py"
MUTANTS = {
    "weekly-ascending": ("subfleet/scheduler.py", '-detail["fill_weekly_used"]',
        '+detail["fill_weekly_used"]', "test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers"),
    "ttl-only": ("subfleet/scheduler.py", 'or not math.isfinite(used) or _time(row["observed_at"]) > now',
        'or not math.isfinite(used) or _time(row["observed_at"]) > now\n'
        '                or (now - _time(row["observed_at"])).total_seconds() > 120',
        "test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]"),
    "expired-counts": ("subfleet/scheduler.py", 'if row.get("resets_at") and _time(row["resets_at"]) > now]',
        'if row.get("resets_at")]', "test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]"),
    "expiring-dropped": ("subfleet/scheduler.py",
        'not detail["fill_expiring"], _time(detail["fill_weekly_reset"]) if detail["fill_expiring"] else "",',
        'False, "",', "test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins"),
    "desktop-dropped": ("subfleet/scheduler.py", 'prefix = (bool(detail.get("desktop")),)',
        'prefix = (False if lane_order(setup["policy"], provider) == "fill" else bool(detail.get("desktop")),)',
        "test_fill_desktop_last_and_turn_affinity_leads_its_class[claude-opus]"),
    "default-codex-fill": ("subfleet/policy.py", '"lane_order": {"claude": "reset", "codex": "reset"}',
        '"lane_order": {"claude": "reset", "codex": "fill"}',
        "test_default_decisions_equal_frozen_release217[codex-astra-omitted]"),
    "default-claude-fill": ("subfleet/policy.py", '"lane_order": {"claude": "reset", "codex": "reset"}',
        '"lane_order": {"claude": "fill", "codex": "reset"}',
        "test_default_decisions_equal_frozen_release217[claude-opus-omitted]"),
    "shipped-default-claude-fill": ("subfleet/default_policy.json", '"lane_order": {\n      "claude": "reset",',
        '"lane_order": {\n      "claude": "fill",',
        "test_default_decisions_equal_frozen_release217[claude-opus-shipped]"),
    "in-flight-dropped": ("subfleet/scheduler.py",
        '-detail["fill_weekly_used"], detail["in_flight"], identity)',
        '-detail["fill_weekly_used"], identity)',
        "test_fill_fewer_in_flight_ranks_first_at_equal_use"),
    "binding-latest-reset": ("subfleet/scheduler.py",
        'key=lambda row: (-row["utilization"], _time(row["resets_at"]), row["scope"])',
        'key=lambda row: (-row["utilization"], -_time(row["resets_at"]).timestamp(), row["scope"])',
        "test_fill_equal_weekly_use_binds_the_earliest_reset"),
    "horizon-fill-future-dropped": ("subfleet/capacity.py",
        '        if observed > instant:\n'
        '            note(row["lane_id"], observed)\n'
        '        elif row.get("resets_at") and (reset := _time(row["resets_at"])) > instant:',
        '        if observed > instant:\n'
        '            pass\n'
        '        elif row.get("resets_at") and (reset := _time(row["resets_at"])) > instant:',
        "tests/unit/test_fill_routing.py::test_fill_horizon_notes_a_future_row_the_admission_view_hides"),
    "picker-fill-first-model-only": ("subfleet/picker.py",
        'view.get("fill_readings", view.get("readings", []))\n'
        '                           if reading["lane_id"] == identity and reading["scope"] in\n'
        '                           ("account", *[policy["models"][name]["id"] for name in models])]',
        'view.get("fill_readings", view.get("readings", []))\n'
        '                           if reading["lane_id"] == identity and reading["scope"] in\n'
        '                           ("account", policy["models"][models[0]]["id"])]',
        "tests/unit/test_fill_routing.py::test_unknown_model_pick_binds_a_scope_other_than_the_first_model"),
    "horizon-fill-claude-only": ("subfleet/capacity.py",
        'if (lane_order(policy, providers.get(row["lane_id"], "")) != "fill"',
        'if (lane_order(policy, providers.get(row["lane_id"], "")) != "fill"'
        ' or providers.get(row["lane_id"]) != "claude"',
        "tests/unit/test_route_check.py::test_c6_3_codex_fill_still_stands_exactly_when_evaluate_now_chooses_the_same"),
    "binding-reset-dropped": ("subfleet/scheduler.py",
        'key=lambda row: (-row["utilization"], _time(row["resets_at"]), row["scope"])',
        'key=lambda row: (-row["utilization"], row["scope"])',
        "test_fill_equal_weekly_use_binds_the_earliest_reset[model-scope]"),
    "binding-scope-before-reset": ("subfleet/scheduler.py",
        'key=lambda row: (-row["utilization"], _time(row["resets_at"]), row["scope"])',
        'key=lambda row: (-row["utilization"], row["scope"], _time(row["resets_at"]))',
        "test_fill_equal_weekly_use_binds_the_earliest_reset[model-scope]"),
    "picker-fill-drops-last-model": ("subfleet/picker.py",
        'view.get("fill_readings", view.get("readings", []))\n'
        '                           if reading["lane_id"] == identity and reading["scope"] in\n'
        '                           ("account", *[policy["models"][name]["id"] for name in models])]',
        'view.get("fill_readings", view.get("readings", []))\n'
        '                           if reading["lane_id"] == identity and reading["scope"] in\n'
        '                           ("account", *[policy["models"][name]["id"] for name in models[:-1]])]',
        "tests/unit/test_fill_routing.py::test_unknown_model_pick_binds_each_configured_claude_scope[haiku]"),
    "timers-fill-ungated": ("subfleet/timers.py",
        "capacity.store_rows(self.store, now=at, fill=fill_on(self.policy))",
        "capacity.store_rows(self.store, now=at)",
        "tests/unit/test_fill_evidence.py::test_timer_snapshot_queries_fill_rows_only_when_requested"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only", nargs="+", choices=list(MUTANTS), default=list(MUTANTS))
    args = parser.parse_args()
    args.basetemp.mkdir(parents=True, exist_ok=False)
    paths = {ROOT / item[0] for item in MUTANTS.values()}
    pristine = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in pristine.items()}
    results = []
    try:
        for name in args.only:
            relative, before, after, test = MUTANTS[name]
            target = ROOT / relative
            for path, data in pristine.items():
                if path.read_bytes() != data:
                    raise RuntimeError(f"Concurrent source edit before {name}: {path.name}")
            source = pristine[target].decode()
            if source.count(before) != 1:
                raise ValueError(f"Mutant no longer applies exactly once: {name}")
            changed = source.replace(before, after)
            if target.suffix == ".json":
                json.loads(changed)
            else:
                compile(changed, str(target), "exec")
            try:
                target.write_text(changed)
                node = test if "::" in test else f"{TESTS}::{test}"
                command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                           "--basetemp", str(args.basetemp / name), node]
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
                failures = [line for line in run.stdout.splitlines() if line.startswith("FAILED ")]
                killed = run.returncode == 1 and bool(failures) and "AssertionError" in run.stdout
                result = {"mutant": name, "killed": killed, "returncode": run.returncode,
                          "test": test, "failure": failures[0] if failures else None,
                          "command": ["python", *[part.replace(str(args.basetemp), "$MUTATION_TEMP")
                                                    for part in command[1:]]]}
                results.append(result)
                print(f"{'KILLED' if killed else 'SURVIVED/ERROR'} {name}: {test}", flush=True)
                if not killed:
                    print((run.stdout + run.stderr)[-4000:], flush=True)
            finally:
                target.write_bytes(pristine[target])
                assert hashlib.sha256(target.read_bytes()).hexdigest() == hashes[target]
    finally:
        for path, data in pristine.items():
            path.write_bytes(data)
            assert path.read_bytes() == data
    payload = {"results": results, "restored_sha256": {str(p.relative_to(ROOT)): value for p, value in hashes.items()}}
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"{sum(row['killed'] for row in results)}/{len(results)} killed; pristine bytes restored", flush=True)
    return int(any(not row["killed"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
