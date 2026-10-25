"""Mutation check for D-STATUS-SRC1: status and alerts name the source of every lane closure.

Each mutant takes one decision of `docs/reports/2026-10-24-status-hold-source.md`
back out of the code, and is paired with the tests whose assertions must catch
it. The unmutated tree runs every named test first and must pass; then each
mutant runs its own tests and must fail at least one of them with a `FAILED`
line (an import or collection error does not count as killing it). Run from the
repository root, with the mutated files clean:

    .venv/bin/python tools/status_hold_source_mutations.py

It rewrites the files in place, one mutant at a time, and restores them whatever
happens. It exits 1 if the baseline fails, or a mutant survives or no longer
applies. It is a development check, not part of the suite.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

STATUS, ALERTS, DAEMON = Path("subfleet/status_json.py"), Path("subfleet/alerts.py"), Path("subfleet/daemon.py")
TESTS = "tests/unit/test_status_hold_source.py"
SIX_HELD = f"{TESTS}::test_all_six_codex_lanes_held_by_an_operator_read_held_not_limited"
I1 = f"{TESTS}::test_i1_dispatchable_equals_the_frozen_release_217_function"
I2 = f"{TESTS}::test_i2_limited_iff_an_account_limit_and_held_iff_only_a_hold"
I3 = f"{TESTS}::test_i3_fleet_empty_counts_sum_and_limited_words_need_a_limited_lane"
I4 = f"{TESTS}::test_i4_a_note_round_trips_or_is_refused_with_the_store_unchanged"

HELD_PART = '        parts.append(f"{len(held)} held by operator"'
LIMITED_PART = '        parts.append(f"{len(limited)} provider-limited"'
OTHER_PART = '            parts.append(f"{len(causes[cause])} {words}")'
READY = '''    return (_admission_verdict(lane) in READY_VERDICTS
            and not any(row.get("scope") == "account" for row in lane.get("closures", ())))
'''

#: name: ([(file, the decision's code, what replaces it)], the tests that must catch it)
MUTANTS = {
    "M1 verdict held -> limited": (
        [(STATUS, '    if operator_held(lane):\n        return "held"\n',
          '    if operator_held(lane):\n        return "limited"\n')],
        [SIX_HELD, I2, f"{TESTS}::test_i1_a_held_lane_is_not_dispatchable_whatever_its_cycle_said"]),
    "M2 held counts as dispatchable": (
        [(STATUS, READY, READY.rstrip("\n").removesuffix(")") + '\n            or lane_verdict(lane) == "held")\n')],
        [I1, f"{TESTS}::test_i1_a_held_lane_is_not_dispatchable_whatever_its_cycle_said"]),
    "M3 the per-cause counts dropped": (
        [(ALERTS, HELD_PART, '        parts.append("held by operator"'),
         (ALERTS, LIMITED_PART, '        parts.append("provider-limited"'),
         (ALERTS, OTHER_PART, '            parts.append(f"{words}")')],
        [SIX_HELD, I3, f"{TESTS}::test_a_mix_of_holds_and_provider_limits_names_each_with_counts"]),
    "M4 holds said to be exhausted": (
        [(ALERTS, HELD_PART, '        parts.append(f"{len(held)} held by operator, exhausted"')],
        [SIX_HELD, I3]),
    "M5 the note stored without validation": (
        [(DAEMON, "                note = hold_note(a.note)\n", "                note = a.note\n")],
        [I4]),
    "M6 a per-lane limit alert for a hold": (
        [(ALERTS, '            if closure.get("reason") in ("auth-dead", "operator-hold"):\n',
          '            if closure.get("reason") == "auth-dead":\n')],
        [SIX_HELD, f"{TESTS}::test_claude_lanes_all_held_raise_no_per_lane_limit_and_stay_warn"]),
    "M7 an all-held fleet left critical": (
        [(ALERTS, '            add(key, "warn" if held_only or provider != "codex" else "critical",\n',
          '            add(key, "critical" if provider == "codex" else "warn",\n')],
        [SIX_HELD, I3]),
}


def run(nodes: list[str]) -> tuple[int, list[str]]:
    """pytest over `nodes`: its exit status and its `FAILED` summary lines."""
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "-rf",
                           *dict.fromkeys(nodes)], capture_output=True, text=True)
    return done.returncode, [line[len("FAILED "):] for line in done.stdout.splitlines() if line.startswith("FAILED ")]


def main() -> int:
    files = sorted({path for edits, _ in MUTANTS.values() for path, _, _ in edits})
    if subprocess.run(["git", "diff", "--quiet", "--", *map(str, files)]).returncode:
        print(f"{', '.join(map(str, files))}: uncommitted changes; commit them first", file=sys.stderr)
        return 2
    named = [node for _, nodes in MUTANTS.values() for node in nodes]
    rc, failed = run(named)
    print(f"baseline  {'passed' if rc == 0 else 'FAILED'}  {len(dict.fromkeys(named))} named tests")
    if rc:
        print("\n".join(failed))
        return 1
    originals, survived = {path: path.read_text() for path in files}, []
    try:
        for name, (edits, nodes) in MUTANTS.items():
            texts = dict(originals)
            if any(texts[path].count(rule) != 1 for path, rule, _ in edits):
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            for path, rule, replacement in edits:
                texts[path] = texts[path].replace(rule, replacement)
            for path in {path for path, _, _ in edits}:
                path.write_text(texts[path])
            try:
                rc, failed = run(nodes)
            finally:
                for path in files:
                    path.write_text(originals[path])
            caught = [line for line in failed if line.split(" - ")[0].split("[")[0] in nodes]
            print(f"{'killed  ' if rc and caught else 'SURVIVED'}  {name}")
            for line in caught:
                print(f"            {line[:220]}")
            if not (rc and caught):
                survived.append(name)
    finally:
        for path, text in originals.items():
            path.write_text(text)
    print(f"{len(MUTANTS) - len(survived)} of {len(MUTANTS)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
