"""D-SCRUB-BOUNDARY1 work-bound timing: `scrub_bounded` CPU at the budget and at 10x it.

Run from the repository root: `.venv/bin/python docs/reports/2026-11-05-scrub-boundary-data/timing.py LABEL`.
Each text is the fixed rows' shape (`x = 1` filler, the data URI the stored example holds, and
the test module's synthetic `password = "Zq0x100000"` just after its omission marker), at the
budget and at ten times it. Prints the best process CPU of 5 runs for limit None and 2,048.
"""

from __future__ import annotations

import json
import sys
import time

from subfleet.sessions import handoff

BUDGET = handoff.MAX_SCRUB_CHARS
DATA = "x =data:image/png;base64,\n" + ("QUJD" * 19 + "\n") * 3
VALUE = 'x =password = "Zq0x100000"\npassword = "Zq1x100000"\n'


def text_of(total: int) -> str:
    """`total` characters: filler, the data URI at 107,070, filler to the value at 131,069."""
    filler = "x = 1\n"
    head = (filler * (107_070 // len(filler) + 1))[:107_070] + DATA
    head += (filler * ((131_069 - len(head)) // len(filler) + 1))[:131_069 - len(head)] + VALUE
    return head + (filler * ((total - len(head)) // len(filler) + 1))[:total - len(head)]


def best(call, runs: int = 5) -> float:
    times = []
    for _ in range(runs):
        started = time.process_time()
        call()
        times.append(time.process_time() - started)
    return min(times)


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    out = {"label": label}
    for name, total in (("budget", BUDGET), ("ten_times", 10 * BUDGET)):
        text = text_of(total)
        assert len(text) == total
        for limit in (None, 2_048):
            out[f"{name}/limit={limit}"] = round(best(lambda: handoff.scrub_bounded(text, limit)), 4)
    for limit in (None, 2_048):
        out[f"ratio/limit={limit}"] = round(out[f"ten_times/limit={limit}"] / out[f"budget/limit={limit}"], 3)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
