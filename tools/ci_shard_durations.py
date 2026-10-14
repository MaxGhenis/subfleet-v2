"""Rewrite the CI shard map's per-file seconds from a run's pytest outcome artifacts.

    gh run download <run id> -R MaxGhenis/subfleet-v2 -p 'pytest-outcomes-*' -D <dir>
    python tools/ci_shard_durations.py <dir> --source "run <run id>"

Each shard job uploads its ``first.jsonl`` (``tools/ci_outcomes_plugin.py``: every
phase's node ID and duration) and ``first.session.json`` (how the session ended).
Only a whole report counts: its session ended with exit 0 or 1 and recorded a
teardown for every collected test. A cancelled or interrupted shard's partial
report is skipped, so a file's time is never replaced by part of it. A file's
seconds are the sum of its phases in one report, and the largest across reports
(the interpreters differ). A file no whole report covers keeps its old value
while it still exists; a file that no longer exists is dropped. ``groups`` and
any other keys are kept.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from tools.ci_shard_plugin import DEFAULT_MAP, predicted_loads, validate_map
except ImportError:                      # run as a script: tools/ is sys.path[0]
    from ci_shard_plugin import DEFAULT_MAP, predicted_loads, validate_map

WHOLE_EXITS = (0, 1)                     # pytest ran every collected test (1: some failed)


def incomplete(report: Path) -> str | None:
    """Why a report is not whole, or None when it is."""
    session = report.with_suffix(".session.json")
    try:
        record = json.loads(session.read_text(encoding="utf-8"))
        exitstatus, collected = record["exitstatus"], record["collected"]
    except (OSError, ValueError, KeyError, TypeError):
        return f"no session record ({session.name})"
    if exitstatus not in WHOLE_EXITS:
        return f"session ended with exit {exitstatus}"
    teardowns = sum(1 for line in report.read_text(encoding="utf-8").splitlines()
                    if json.loads(line).get("when") == "teardown")
    if teardowns != collected:
        return f"{teardowns} of {collected} collected tests finished"
    return None


def file_seconds(report: Path) -> dict[str, float]:
    """Seconds per test file in one outcomes report; a malformed line raises."""
    totals: dict[str, float] = {}
    for number, line in enumerate(report.read_text(encoding="utf-8").splitlines(), 1):
        record = json.loads(line)
        if record.get("when") == "collect":   # a failed collection: no test ran
            continue
        node, duration = record.get("nodeid"), record.get("duration")
        if not isinstance(node, str) or not isinstance(duration, (int, float)) or duration < 0:
            raise ValueError(f"{report}:{number}: expected a node ID and a duration >= 0")
        path = node.split("::", 1)[0]
        totals[path] = totals.get(path, 0.0) + duration
    return totals


def merge(reports: list[dict[str, float]], old: dict[str, float], exists) -> tuple[dict[str, float], list[str]]:
    """New seconds per file, and the files that kept their old value."""
    seconds: dict[str, float] = {}
    for totals in reports:
        for path, value in totals.items():
            seconds[path] = max(seconds.get(path, 0.0), value)
    kept = sorted(path for path in old if path not in seconds and exists(path))
    for path in kept:
        seconds[path] = old[path]
    return {path: round(seconds[path], 1) for path in sorted(seconds)}, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", type=Path, help="first.jsonl files or directories holding them")
    parser.add_argument("--root", type=Path, default=Path("."), help="the checkout the map's paths are relative to")
    parser.add_argument("--map", type=Path, default=None, help=f"default: <root>/{DEFAULT_MAP}")
    parser.add_argument("--shards", type=int, default=5, help="shard count to print predicted loads for")
    parser.add_argument("--source", default="", help="where the measurements came from (run ids)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    files = sorted({path for item in args.inputs
                    for path in ([item] if item.is_file() else item.rglob("first.jsonl"))})
    if not files:
        parser.error("no first.jsonl under the inputs")
    reasons = {path: incomplete(path) for path in files}
    for path, reason in reasons.items():
        if reason:
            print(f"skipped {path}: {reason}")
    files = [path for path, reason in reasons.items() if reason is None]
    if not files:
        parser.error("no whole report under the inputs")
    map_path = args.map or args.root / DEFAULT_MAP
    data = json.loads(map_path.read_text(encoding="utf-8"))
    validate_map(data)
    seconds, kept = merge([file_seconds(path) for path in files], data["seconds"],
                          lambda path: (args.root / path).is_file())
    data["seconds"] = seconds
    if args.source:
        data["source"] = args.source
    millis, groups = validate_map(data)
    loads = predicted_loads(millis, groups, args.shards)
    print(f"{len(files)} reports ({len(reasons) - len(files)} skipped), {len(seconds)} files ({len(kept)} kept their old value)")
    print(f"predicted minutes per shard for {args.shards}: " + ", ".join(f"{load / 60000:.1f}" for load in loads))
    for path in kept:
        print(f"  kept: {path}")
    if not args.dry_run:
        map_path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
