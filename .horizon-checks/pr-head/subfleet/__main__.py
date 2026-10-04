"""`python -m subfleet ...` — the same front door the console script uses.

The hook entries `daemon install --hooks` writes run `<this interpreter> -m
subfleet hook <Event>` rather than a bare `subfleet` on PATH, because during
the shadow period a bare `subfleet` may still resolve to v1 (and, as
`doctor.check_pythonpath` reports, may resolve to v1's *package* even when the
path says v2). Without this module that command line does not run at all, so it
is part of the delivery path and not a convenience.

`compat.dispatch` is the entry, not `cli.main`: every v1 invocation goes through
the compatibility layer first (`subfleet/compat.py`), which maps or delegates it
and then hands `cli.main` a v2 argv.
"""

from __future__ import annotations

import sys

from .compat import dispatch

if __name__ == "__main__":
    sys.exit(dispatch())
