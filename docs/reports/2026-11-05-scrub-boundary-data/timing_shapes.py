"""D-SCRUB-BOUNDARY1 fix round 1 timing: `scrub_bounded(text, None)` process CPU, best of 3, at the
budget and at 10 times it, for review r1's many-block shapes and the hub's `datauri-longword-many`.

Run from the root of the tree to time:
`python docs/reports/2026-11-05-scrub-boundary-data/timing_shapes.py LABEL [SHAPE ...]`. The tree's
own `subfleet` is imported first (the script puts the working directory on `sys.path`), so a
`git archive` of another commit is timed by running this file from inside it. Prints one JSON line per
shape, with the 1-minute load average read before and after each time.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from subfleet.sessions import handoff  # noqa: E402

BUDGET = handoff.MAX_SCRUB_CHARS
_TIMING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timing.py")


def _fixed(total: int) -> str:
    """The build's `timing.py` text."""
    namespace: dict = {}
    source = open(_TIMING).read().split("def best(")[0]
    exec(compile(source.replace("from subfleet.sessions import handoff", ""), _TIMING, "exec"),
         {"handoff": handoff, "__name__": "timing"} | namespace, namespace)
    return namespace["text_of"](total)


def repeated(unit: str, total: int) -> str:
    return (unit * (total // len(unit) + 1))[:total]


PROSE = "ordinary words in a line of prose, with nothing in it worth hiding\n"
PEM = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
SHAPES = {
    "fixed": (_fixed, False),
    "pem-many": (lambda total: repeated(PEM, total), False),
    "reminder-many": (lambda total: repeated("<system-reminder>note</system-reminder>\nx = 1\n", total), True),
    "datauri-key-many": (lambda total: repeated("data:image/png;base64," + "QUJD" * 8 + '\ntoken = "Zq9x1"\n', total),
                         False),
    "pem-sparse": (lambda total: repeated(PEM + PROSE * 60, total), False),
    "datauri-longword-many": (lambda total: repeated("data:image/png;base64," + "A" * 20_000 + 'password = "Zq9x2"\n',
                                                     total), False),
}


def best(call, runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        started = time.process_time()
        call()
        times.append(time.process_time() - started)
    return min(times)


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    names = sys.argv[2:] or list(SHAPES)
    for name in names:
        make, strip = SHAPES[name]
        row = {"label": label, "shape": name}
        for size, total in (("budget", BUDGET), ("ten_times", 10 * BUDGET)):
            text = make(total)
            assert len(text) == total
            before = os.getloadavg()[0]
            row[size] = round(best(lambda: handoff.scrub_bounded(text, None, strip_reminders=strip)), 4)
            row[f"load_{size}"] = [round(before, 1), round(os.getloadavg()[0], 1)]
        row["ratio"] = round(row["ten_times"] / row["budget"], 2)
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
