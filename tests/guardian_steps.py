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
# The test process that started it: named by `GUARDIAN_STEPS_PARENT`, so a test gone before
# this module ran is caught as well; else its parent when it began.
PARENT = int(os.environ.get("GUARDIAN_STEPS_PARENT") or os.getppid())


def pause(step: str) -> None:
    COUNT[0] += 1
    n = COUNT[0]
    (STEPS / f"{n}.{step}").touch()
    deadline = time.monotonic() + 120
    while not (STEPS / f"{n}.go").exists():
        # The test is gone (#250 fix round 2c: a test process killed before its teardown
        # leaves nothing behind), or never released this step: never spawn.
        if time.monotonic() > deadline or os.getppid() != PARENT:
            os._exit(98)
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

# Fix round 2b's mutation control (`tests/unit/test_kill4_fix2b_mutations.py`): the
# guardian looks for the abandon before it publishes start.json instead of after.
CHECK_FIRST = [
    ('    _receipt(attempt_dir / "start.json", start)\n',
     '    early = abandoned(attempt_dir)\n    _receipt(attempt_dir / "start.json", start)\n'),
    ("        marker = abandoned(attempt_dir)\n        if marker is not None:\n            if relay",
     "        marker = early\n        if marker is not None:\n            if relay"),
]
if os.environ.get("GUARDIAN_STEPS_MUTANT") == "check-first":
    import inspect
    import textwrap
    source = textwrap.dedent(inspect.getsource(guardian.run_guardian))
    for before, after in CHECK_FIRST:
        if before not in source:
            raise RuntimeError(f"mutation target missing: {before}")
        source = source.replace(before, after)
    namespace: dict = {}
    exec(compile(source, "<mutation:run_guardian>", "exec"), guardian.__dict__, namespace)
    guardian.run_guardian = namespace["run_guardian"]

if __name__ == "__main__":
    raise SystemExit(guardian.main(sys.argv[1:]))
