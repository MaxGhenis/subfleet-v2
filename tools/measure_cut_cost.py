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

With `--logs`, instead, on a realistic daemon.log (`realistic_log`: admission lines,
tracebacks, exception groups behind their margins, warnings and records with quoted
words; review r4 of #233 measured round 5's figure on one admission line repeated):
what `daemon logs -n N` costs (`daemon_log.scrubbed_tail`, which scrubs the whole range,
cut to twice it and the bound more) when its N lines hold the whole file, and at the
default 40 lines; then the default 40 lines of the same file with a reminder tag on its
first line, an orphan and a whole reminder (round 6); the best of `--runs` CPU times.

`--budget-s` stops starting rows once that much time has passed, and says which rows it
left out. Run it under the suite lock (each run under 20 minutes), never beside a pytest:
    .venv/bin/python tools/measure_cut_cost.py --sizes 1,4,16 --reference-sizes 1,4,16 > table.md
    .venv/bin/python tools/measure_cut_cost.py --logs --sizes 1,4,16 --runs 3
"""

from __future__ import annotations

import argparse
import gc
import logging
import random
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


def _review_r4_shapes() -> dict:
    from tests.unit import test_daemon_log_cut_windows as windows
    return {"quote kinds (review r4)": windows.REVIEW_R4_SHAPES["quote-kinds"],
            "data URI body into the tail's window": windows.REVIEW_R4_SHAPES["data-uri-body"],
            "480 data URI prefixes": windows.REVIEW_R4_SHAPES["data-uri-prefixes"]}


def _review_r5_shapes() -> dict:
    """Review r5's (P3-1): the 8,000-backslash runs at both ends of plain text; and the
    spaced margin-only lines in a text Python stores four bytes a character (an emoji, `r>`
    and the three characters `_REMINDER_FOLDS` reads, on the first line), alone and with
    plain lines between them."""
    from tests.unit import test_daemon_log_cut_windows as windows
    return {"backslash runs (review r5)": windows.REVIEW_R5_SHAPES["backslash-runs"],
            "margin-only lines `  | \\n`, four-byte text": windows.REVIEW_R5_SHAPES["four-byte-margin-lines"],
            "margin-only lines `  | \\n` among plain lines": lambda size: ("  | \n" * 8 + "plain words only\n") * (size // 57)}


#: In the order they are measured: `--budget-s` leaves out the last first.
SHAPES = {
    "plain": lambda size: "plain words only\n" * (size // 17),
    "margin-only lines `  |\\n`": lambda size: "  |\n" * (size // 4),
    "margin-only lines `  | \\n`": lambda size: "  | \n" * (size // 5),
    "margin-only lines, long and short": lambda size: ("  |" + " " * 70 + "\n  |\n") * (size // 79),
    **_review_r4_shapes(),
    **_review_r5_shapes(),
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


#: Rows `--budget-s` left out, named in the output.
SKIPPED: list[str] = []


def _within(deadline: float | None, row: str) -> bool:
    if deadline is not None and time.monotonic() > deadline:
        SKIPPED.append(row)
        return False
    return True


def measure(sizes: list[int], runs: int, reference_sizes: set[int], every_shape: bool,
            deadline: float | None = None, rows: list[dict] | None = None) -> list[dict]:
    """The rows, appended to `rows` as each is measured, so a stop keeps those done."""
    formatter = daemon_log.ScrubbingFormatter()
    rows = [] if rows is None else rows
    shapes = {**SHAPES, **_cost_shapes()} if every_shape else SHAPES
    for size in sizes:
        plain = None
        with_reference = size in reference_sizes
        for name, build in shapes.items():
            if name != "plain" and not _within(deadline, f"{name} at {size // MIB} MiB"):
                continue
            text = build(size)
            row = {"shape": name, "MiB": size // MIB, "chars": len(text)}
            row["cut"] = best_cpu(lambda: handoff.cut_whole(text, LIMIT), runs)
            row["reference"] = float("nan")
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
        if not _within(deadline, f"raised exception group at {size // MIB} MiB"):
            continue
        group = _group(size)
        text = formatter.formatException((type(group), group, group.__traceback__))
        row = {"shape": "raised exception group (its traceback)", "MiB": size // MIB, "chars": len(text)}
        row["cut"] = best_cpu(lambda: handoff.cut_whole(text, LIMIT), runs)
        row["reference"] = float("nan")
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
            cells.append("" if row["reference"] != row["reference"] else f"{row['reference']:.1f}")
        cells += [f"{row['message']:.1f}", f"{row['traceback']:.1f}", f"{row['python']:.1f}",
                  f"{row['traceback/plain']:.2f}"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def realistic_log(size: int, seed: int = 0) -> str:
    """About `size` characters of a daemon.log as a daemon writes one: admission lines,
    a traceback now and then, an exception group behind its margins, warnings and records
    with quoted words, each line under 200 characters. No credential: what it costs to
    show, not what it hides."""
    rng = random.Random(seed)
    parts, total = [], 0
    while total < size:
        stamp = f"2026-10-20 {rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}"
        kind = rng.random()
        if kind < 0.62:
            part = (f"{stamp} INFO subfleet.daemon admission: placed job {rng.randrange(10 ** 6)} on lane "
                    f"codex-{rng.randrange(8)} (tier {rng.choice(['easy', 'standard', 'hard'])}, waited "
                    f"{rng.randrange(90)} s)\n")
        elif kind < 0.76:
            part = (f"{stamp} ERROR subfleet.daemon turn {rng.randrange(10 ** 4)} failed\n"
                    "Traceback (most recent call last):\n"
                    f'  File "/x/subfleet/daemon.py", line {rng.randrange(2000)}, in _run\n'
                    "    result = runner.step(turn, 'resume')\n"
                    '  File "/x/subfleet/conversations/runner.py", line 212, in step\n'
                    "    raise RuntimeError(f\"provider said: {said!r}\")\n"
                    f"RuntimeError: provider said: 'rate limited' on \"lane codex-{rng.randrange(8)}\"\n")
        elif kind < 0.84:
            part = (f"{stamp} ERROR subfleet.relay cleanup failed\n"
                    "  + Exception Group Traceback (most recent call last):\n"
                    '  |   File "/x/subfleet/relay.py", line 40, in close\n'
                    "  |     raise ExceptionGroup('cleanup', errors)\n"
                    "  | ExceptionGroup: cleanup (2 sub-exceptions)\n"
                    "  +-+---------------- 1 ----------------\n"
                    "    | Traceback (most recent call last):\n"
                    '    |   File "/x/subfleet/relay.py", line 88, in _close_pipe\n'
                    "    | OSError: [Errno 32] Broken pipe: 'stdin.jsonl'\n"
                    "    +---------------- 2 ----------------\n"
                    "    | ValueError: invalid frame 'x'\n"
                    "    +------------------------------------\n")
        elif kind < 0.93:
            part = (f"{stamp} WARNING subfleet.policy policy said: model 'gpt-6-luna' is retired; use "
                    f"\"luna\" (it's the alias for lane codex-{rng.randrange(8)})\n")
        else:
            part = (f"{stamp} INFO subfleet.daemon stopping: reason='idle' lanes=\"codex-0,codex-1\" "
                    f"it's done after {rng.randrange(600)} s\n")
        parts.append(part)
        total += len(part)
    return "".join(parts)


def measure_logs(sizes: list[int], runs: int, deadline: float | None = None,
                 lines: list[str] | None = None) -> str:
    """`daemon logs` on `realistic_log`: the whole file as its range, and its default 40
    lines; then the default 40 lines with a reminder tag on the first line. The table's
    lines are appended to `lines` as each row is measured, so a stop keeps those done."""
    lines = [] if lines is None else lines
    lines += ["| file MiB | lines | whole range ms | ms per MiB | `-n 40` ms | `-n 40`, orphan tag on line 1, ms "
             "| `-n 40`, whole reminder on line 1, ms |", "|---|---|---|---|---|---|---|"]
    for size in sizes:
        if not _within(deadline, f"daemon logs at {size // MIB} MiB"):
            continue
        text = realistic_log(size)
        count = text.count("\n")
        whole = best_cpu(lambda: daemon_log.scrubbed_tail(text, count), runs)
        forty = best_cpu(lambda: daemon_log.scrubbed_tail(text, 40), runs)
        orphan_text = "turn failed: model said: <system-reminder>The user's excerpt was cut\n" + text
        whole_text = "turn failed: model said: <system-reminder>note</system-reminder>\n" + text
        orphan = best_cpu(lambda: daemon_log.scrubbed_tail(orphan_text, 40), runs)
        reminder = best_cpu(lambda: daemon_log.scrubbed_tail(whole_text, 40), runs)
        assert daemon_log.scrubbed_tail(orphan_text, 40) == daemon_log.scrubbed_tail(text, 40)
        lines.append(f"| {size // MIB} | {count:,} | {whole:.0f} | {whole / (len(text) / MIB):.0f} | {forty:.1f} "
                     f"| {orphan:.1f} | {reminder:.1f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", default="1,4,16", help="MiB, comma-separated (default 1,4,16)")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--reference-sizes", default="1,4", help="MiB at which the cce8f1af7 cut runs too")
    parser.add_argument("--every-shape", action="store_true", help="the round-2 cost shapes too")
    parser.add_argument("--logs", action="store_true", help="measure `daemon logs` on a realistic log instead")
    parser.add_argument("--budget-s", type=float, default=None, help="start no row after this many seconds")
    args = parser.parse_args(argv)
    sizes = [int(part) * MIB for part in args.sizes.split(",")]
    deadline = None if args.budget_s is None else time.monotonic() + args.budget_s
    started = time.monotonic()
    if args.logs:
        print(f"Python {sys.version.split()[0]}; best of {args.runs} CPU times (time.process_time)")
        lines: list[str] = []
        try:
            measure_logs(sizes, args.runs, deadline, lines)
        except KeyboardInterrupt:           # a hold's cap: keep the rows measured
            SKIPPED.append("the rows after a stop")
        print("\n".join(lines))
    else:
        print(f"Python {sys.version.split()[0]}, GIL {'on' if getattr(sys, '_is_gil_enabled', lambda: True)() else 'off'}; "
              f"best of {args.runs} CPU times (time.process_time), limit {LIMIT:,}")
        reference_sizes = {int(part) * MIB for part in args.reference_sizes.split(",") if part}
        rows: list[dict] = []
        try:
            measure(sizes, args.runs, reference_sizes, args.every_shape, deadline, rows)
        except KeyboardInterrupt:           # a hold's cap: keep the rows measured
            SKIPPED.append("the rows after a stop")
        print(table(rows, True))
    print(f"\n{time.monotonic() - started:.0f} s; left out by --budget-s: {', '.join(SKIPPED) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
