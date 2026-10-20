#!/usr/bin/env python3
"""Measure the daemon.log cut's cost per shape and size (C-25.7, #233 review r3 P3-1).

For each shape, at each size, the best of five CPU times (`time.process_time`) of:
- `handoff.cut_whole(text, RECORD_SCRUB_MAX)`, this tree's windowed cut;
- the same call on the cut as of cce8f1af7 (`tests/reference_cut_cce8f1af7.py`);
- the daemon's formatter writing a record of the text as its message, and as the
  traceback of a raised exception (Python's own formatting included);
and each against plain text of the same size, as a ratio. Review r3's shapes are
the margin-only lines, the member lines, the leading spaces, the run of `<` and the
raised exception group; the round-2 cost shapes are included for comparison.

With `--logs`, instead: what `daemon logs -n N` costs when its N lines hold 1, 4 or 16
MiB of an admission log (`daemon_log.scrubbed_tail`, which scrubs the whole range, cut
to twice it and the bound more), the best of `--runs` CPU times.

Run it under the suite lock (each run under 20 minutes), never beside a pytest:
    .venv/bin/python tools/measure_cut_cost.py --sizes 1,4,16 > table.md
    .venv/bin/python tools/measure_cut_cost.py --logs --sizes 1,4 --runs 3
"""

from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import daemon_log  # noqa: E402
from subfleet.sessions import handoff  # noqa: E402
from tests import reference_cut_cce8f1af7 as reference  # noqa: E402

MIB = 1024 * 1024
LIMIT = daemon_log.RECORD_SCRUB_MAX


def _group(size: int) -> BaseException:
    """Review r3's exception group: a member whose message is `size` bytes of lines."""
    member = RuntimeError("\n".join(["member line words"] * max(1, size // 18)))
    try:
        raise ExceptionGroup("ctx", [member, ValueError("other")])
    except ExceptionGroup as group:
        return group


SHAPES = {
    "plain": lambda size: "plain words only\n" * (size // 17),
    "margin-only lines `  |\\n`": lambda size: "  |\n" * (size // 4),
    "margin-only lines `  | \\n`": lambda size: "  | \n" * (size // 5),
    "member lines `  | member line\\n`": lambda size: "  | member line\n" * (size // 16),
    "leading spaces": lambda size: " " * size + "\nx\n",
    "run of `<`": lambda size: "<<<<<<<<<<<<<<<\n" * (size // 16),
}
COST_SHAPES = ("head", "tail", "tail-eight-kinds", "quote-dense", "json-dense", "blank-lines-after-a-key",
               "blank-lines-after-bearer", "blank-lines-after-a-flag")


def _cost_shapes():
    from tests.unit import test_daemon_log_handler as handler_tests
    return {f"round 2: {name}": handler_tests.COST_SHAPES[name] for name in COST_SHAPES}


def best_cpu(work, runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        gc.collect()
        started = time.process_time()
        work()
        best = min(best, time.process_time() - started)
    return best * 1000


def _record(message: str, exc: BaseException | None) -> logging.LogRecord:
    return logging.makeLogRecord({"name": "subfleet.daemon.measure", "levelno": logging.ERROR,
                                  "levelname": "ERROR", "msg": "%s", "args": (message,),
                                  "exc_info": (type(exc), exc, exc.__traceback__) if exc else None})


def _raised(text: str) -> BaseException:
    try:
        raise RuntimeError(text)
    except RuntimeError as exc:
        return exc


def measure(sizes: list[int], runs: int, with_reference: bool) -> list[dict]:
    formatter = daemon_log.ScrubbingFormatter()
    rows = []
    shapes = {**SHAPES, **_cost_shapes()}
    for size in sizes:
        plain = None
        for name, build in shapes.items():
            text = build(size)
            row = {"shape": name, "MiB": size // MIB, "chars": len(text)}
            row["cut"] = best_cpu(lambda: handoff.cut_whole(text, LIMIT), runs)
            if with_reference:
                row["reference"] = best_cpu(lambda: reference.cut_whole(text, LIMIT), runs)
                assert handoff.cut_whole(text, LIMIT) == reference.cut_whole(text, LIMIT), name
            row["message"] = best_cpu(lambda: formatter.format(_record(text, None)), runs)
            exc = _raised(text)
            row["traceback"] = best_cpu(lambda: formatter.format(_record("failed", exc)), runs)
            row["python"] = best_cpu(lambda: formatter.formatException((type(exc), exc, exc.__traceback__)), runs)
            if name == "plain":
                plain = row
            for key in ("cut", "message", "traceback"):
                row[f"{key}/plain"] = row[key] / plain[key] if plain[key] else float("nan")
            rows.append(row)
            del text, exc
        group = _group(size)
        text = formatter.formatException((type(group), group, group.__traceback__))
        row = {"shape": "raised exception group (its traceback)", "MiB": size // MIB, "chars": len(text)}
        row["cut"] = best_cpu(lambda: handoff.cut_whole(text, LIMIT), runs)
        if with_reference:
            row["reference"] = best_cpu(lambda: reference.cut_whole(text, LIMIT), runs)
            assert handoff.cut_whole(text, LIMIT) == reference.cut_whole(text, LIMIT)
        row["message"] = float("nan")
        row["traceback"] = best_cpu(lambda: formatter.format(_record("failed", group)), runs)
        row["python"] = best_cpu(lambda: formatter.formatException((type(group), group, group.__traceback__)), runs)
        row["cut/plain"] = row["cut"] / plain["cut"]
        row["message/plain"] = float("nan")
        row["traceback/plain"] = row["traceback"] / plain["traceback"]
        rows.append(row)
        del text, group
    return rows


def table(rows: list[dict], with_reference: bool) -> str:
    head = ["shape", "MiB", "cut ms", "cut / plain"]
    if with_reference:
        head += ["cce8f1af7 cut ms"]
    head += ["handler, message ms", "handler, traceback ms", "Python's formatting ms", "traceback / plain"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        cells = [row["shape"], str(row["MiB"]), f"{row['cut']:.1f}", f"{row['cut/plain']:.2f}"]
        if with_reference:
            cells.append(f"{row['reference']:.1f}")
        cells += [f"{row['message']:.1f}", f"{row['traceback']:.1f}", f"{row['python']:.1f}",
                  f"{row['traceback/plain']:.2f}"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def measure_logs(sizes: list[int], runs: int) -> str:
    lines = ["| range MiB | lines | `daemon logs` ms | ms per MiB |", "|---|---|---|---|"]
    line = "2026-10-20 admission: placed job 1234 on lane codex-0 (tier hard, waited 2 s)\n"
    for size in sizes:
        count = size // len(line)
        text = line * (count + 1000)
        cost = best_cpu(lambda: daemon_log.scrubbed_tail(text, count), runs)
        lines.append(f"| {size // MIB} | {count:,} | {cost:.0f} | {cost / (size / MIB):.0f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", default="1,4,16", help="MiB, comma-separated (default 1,4,16)")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--no-reference", action="store_true", help="skip the cce8f1af7 cut")
    parser.add_argument("--logs", action="store_true", help="measure `daemon logs` over large ranges instead")
    args = parser.parse_args(argv)
    sizes = [int(part) * MIB for part in args.sizes.split(",")]
    if args.logs:
        print(f"Python {sys.version.split()[0]}; best of {args.runs} CPU times (time.process_time)")
        print(measure_logs(sizes, args.runs))
        return 0
    print(f"Python {sys.version.split()[0]}, GIL {'on' if getattr(sys, '_is_gil_enabled', lambda: True)() else 'off'}; "
          f"best of {args.runs} CPU times (time.process_time), limit {LIMIT:,}")
    print(table(measure(sizes, args.runs, not args.no_reference), not args.no_reference))
    return 0


if __name__ == "__main__":
    sys.exit(main())
