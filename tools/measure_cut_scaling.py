#!/usr/bin/env python3
"""Measure how the daemon.log cut's cost grows with the text, on the four-byte margin shape (C-25.7, #233 review r6, P3-B).

Review r6 asked whether the cut is linear on review r5's four-byte margin shape (spaced
margin-only lines in a text Python stores four bytes a character): round 7's table gave
58.6 ms at 4 MiB and 409.9 ms at 16 MiB, 7 times for 4 times the text. For each size this
prints, on that shape and on plain text:
- the cut's best CPU time of `--runs` (`time.process_time`) and its ratio to plain text's;
- its steps, each timed alone: the reminder-tag scan (`_holds_reminder_tag`), the reading
  (`_Reading`), the head's and the tail's cut, the reading's time taken off each;
- three C passes over the same text for comparison, each linear by construction: `lower()`,
  a `replace` that copies it, and a copy;
- what the cut reads, counted (`test_daemon_log_cut_windows.Meter`): every regex call by
  the characters it reads, and the `str` scans over the text, as passes (characters read
  over the text's length), which a quadratic step would make grow with the size;
- the peak resident memory of the process that measured that size: each size runs in a
  child of its own, and a size is skipped when the last child's peak, scaled by the sizes'
  ratio, would pass `--memory-mb`.

Run it under the suite lock, never beside a pytest:
    .venv/bin/python tools/measure_cut_scaling.py --sizes 1,4,8,16,32 --runs 7
"""
from __future__ import annotations

import argparse
import gc
import resource
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import daemon_log  # noqa: E402
from subfleet.sessions import handoff  # noqa: E402
from tests.unit import test_daemon_log_cut_windows as windows  # noqa: E402

MIB = 1024 * 1024
LIMIT = daemon_log.RECORD_SCRUB_MAX
#: Review r5's four-byte margin shape, as the count test and `tools/measure_cut_cost.py` build it.
SHAPES = {
    "four-byte margin lines": windows.REVIEW_R5_SHAPES["four-byte-margin-lines"],
    "plain": lambda size: "plain words only\n" * (size // 17),
}


def best_cpu(work, runs: int) -> float:
    best = float("inf")
    for _ in range(runs):
        gc.collect()
        started = time.process_time()
        work()
        best = min(best, time.process_time() - started)
    return best * 1000


def peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3      # bytes on macOS, KiB on Linux


def steps(text: str, runs: int) -> dict[str, float]:
    half = LIMIT // 2
    newline = text.find("\n", len(text) - half - 1)
    tail_start = len(text) if newline < 0 else newline + 1

    def reading():
        return handoff._Reading(text, half + 1, tail_start)
    out = {"cut": best_cpu(lambda: handoff.cut_whole(text, LIMIT), runs),
           "tag scan": best_cpu(lambda: handoff._holds_reminder_tag(text), runs),
           "reading": best_cpu(reading, runs)}
    out["head cut"] = best_cpu(lambda: handoff._whole_head_cut(reading(), half), runs) - out["reading"]
    out["tail cut"] = best_cpu(lambda: handoff._whole_tail_cut(reading(), len(text) - half), runs) - out["reading"]
    out["lower()"] = best_cpu(lambda: text.lower(), runs)
    out["replace"] = best_cpu(lambda: text.replace("\n", "\t"), runs)
    out["copy"] = best_cpu(lambda: "".join((text[:1], text[1:-1], text[-1:])), runs)
    return out


def counted(text: str) -> tuple[int, float, int]:
    patch = pytest.MonkeyPatch()
    try:
        cut, meter = windows.metered_cut(text, patch)
    finally:
        patch.undo()
    assert cut == handoff.cut_whole(text, LIMIT)
    return meter.regex, meter.scanned / len(text), len(meter.looks)


def width(text: str) -> int:
    return round((sys.getsizeof(text) - sys.getsizeof("")) / max(1, len(text)))


HEAD = ["shape", "MiB", "bytes a char", "cut ms", "ms per MiB", "cut / plain", "tag scan", "reading", "head cut",
        "tail cut", "lower()", "replace", "copy", "regex reads", "str passes", "looks", "peak MB"]


def one_size(mib: int, runs: int) -> None:
    """Both shapes' rows at `mib` MiB, then this process's peak on a line of its own."""
    results = {}
    for name, build in SHAPES.items():
        text = build(mib * MIB)
        results[name] = (width(text), steps(text, runs), counted(text))
        del text
        gc.collect()
    plain_cut = results["plain"][1]["cut"]
    for name, (wide, out, (regex, passes, looks)) in results.items():
        cells = [name, str(mib), str(wide), f"{out['cut']:.1f}", f"{out['cut'] / mib:.1f}",
                 f"{out['cut'] / plain_cut:.2f}"]
        cells += [f"{out[key]:.1f}" for key in ("tag scan", "reading", "head cut", "tail cut", "lower()", "replace",
                                                  "copy")]
        cells += [f"{regex:,}", f"{passes:.2f}", str(looks), f"{peak_mb():.0f}"]
        print("| " + " | ".join(cells) + " |", flush=True)
    print(f"peak {peak_mb():.0f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", default="1,4,8,16,32", help="MiB of characters, comma-separated")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--memory-mb", type=float, default=1900,
                        help="skip a size whose peak, scaled from the last size's, would pass this")
    parser.add_argument("--one", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.one is not None:
        one_size(args.one, args.runs)
        return 0
    started = time.monotonic()
    print(f"Python {sys.version.split()[0]}, GIL {'on' if getattr(sys, '_is_gil_enabled', lambda: True)() else 'off'}; "
          f"best of {args.runs} CPU times (time.process_time), limit {LIMIT:,}; each size in a process of its own")
    print("| " + " | ".join(HEAD) + " |")
    print("|" + "---|" * len(HEAD))
    skipped, last = [], None
    for mib in (int(part) for part in args.sizes.split(",")):
        if last is not None and last[1] * mib / last[0] > args.memory_mb:
            skipped.append(f"{mib} MiB ({last[1]:.0f} MB at {last[0]} MiB)")
            continue
        child = subprocess.run([sys.executable, __file__, "--one", str(mib), "--runs", str(args.runs)],
                               capture_output=True, text=True, check=False)
        rows = child.stdout.splitlines()
        print("\n".join(row for row in rows if row.startswith("|")), flush=True)
        peak = next((float(row.split()[1]) for row in rows if row.startswith("peak ")), None)
        if child.returncode or peak is None:
            print(f"{mib} MiB: the child exited {child.returncode}: {child.stderr.strip()[-300:]}")
            return 1
        last = (mib, peak)
    print(f"\n{time.monotonic() - started:.0f} s; skipped for memory: {', '.join(skipped) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
