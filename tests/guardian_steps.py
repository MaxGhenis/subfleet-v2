"""The real `subfleet.guardian`, paused before each step of D-QUAR-START1's handshake.

Run as `python -m tests.guardian_steps <guardian arguments>` with `GUARDIAN_STEPS_DIR`
set. Before each step it touches `<dir>/<n>.<step>` (n counts from 1) and waits for
`<dir>/<n>.go`, so a test can order the guardian's steps against the daemon's:

- `write-start`: publishing `start.json` (`_receipt` with that name);
- `read-abandon`: each check for `abandon.json` (`guardian.abandoned`), wherever the
  guardian makes it: after its launch gate, and before it spawns its provider.

The steps follow the code, not this list: a guardian that checked before writing
`start.json` would pause in that order. Nothing else in the guardian changes.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

from subfleet import guardian

STEPS = Path(os.environ["GUARDIAN_STEPS_DIR"])
COUNT = [0]


def pause(step: str) -> None:
    COUNT[0] += 1
    n = COUNT[0]
    (STEPS / f"{n}.{step}").touch()
    deadline = time.monotonic() + 120
    while not (STEPS / f"{n}.go").exists():
        if time.monotonic() > deadline:
            os._exit(98)                                   # the test is gone: never spawn
        time.sleep(.005)


_receipt = guardian._receipt


def receipt(path, value):
    if Path(path).name == "start.json":
        pause("write-start")
    _receipt(path, value)


guardian._receipt = receipt
_abandoned = getattr(guardian, "abandoned", None)
if _abandoned is not None:
    def abandoned(attempt_dir):
        pause("read-abandon")
        return _abandoned(attempt_dir)
    guardian.abandoned = abandoned

if __name__ == "__main__":
    raise SystemExit(guardian.main(sys.argv[1:]))
