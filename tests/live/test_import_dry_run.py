"""C-20.1: the one import pass over the real v1 state, opt-in only.

Skipped unless `SUBFLEET_LIVE=1`. It reads `~/chief-of-staff/state/subfleet`,
`~/.local/state/delegate` and the v1 roster, and writes nothing: the import runs
against a throwaway snapshot of the store (see `subfleet/importer.py`), and the
report lands in the pytest temp dir, not in `$SUBFLEET_HOME`. The lane that built
the importer never set that variable; the integrator does.

Run it deliberately:

    SUBFLEET_LIVE=1 uv run pytest -q -s tests/live/test_import_dry_run.py

`-s` shows the per-store table, which is the artefact this test exists to
produce; the JSON report path is printed with it. Point it somewhere else with
`SUBFLEET_LIVE_V1_STATE`, `SUBFLEET_LIVE_DELEGATE_STATE`,
`SUBFLEET_LIVE_ROSTER_DIR` and `SUBFLEET_LIVE_STATE_ROOT` (an existing v2 store
to import against; by default the pass starts from an empty one).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subfleet import importer
from subfleet.importer import DELEGATE_STATE, V1_ROSTER_DIR, V1_STATE, import_v1

LIVE = os.environ.get("SUBFLEET_LIVE") == "1"
V1 = Path(os.environ.get("SUBFLEET_LIVE_V1_STATE") or V1_STATE).expanduser()
DELEGATE = Path(os.environ.get("SUBFLEET_LIVE_DELEGATE_STATE") or DELEGATE_STATE).expanduser()
ROSTER = Path(os.environ.get("SUBFLEET_LIVE_ROSTER_DIR") or V1_ROSTER_DIR).expanduser()

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live tests need SUBFLEET_LIVE=1"),
    pytest.mark.skipif(not V1.is_dir(), reason=f"no v1 state at {V1}"),
]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            found[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return found


def test_the_dry_run_reports_the_real_v1_state_and_writes_nothing(tmp_path):
    """The opt-in dry run: an ImportReport, and not one byte written to v1."""
    state_root = Path(os.environ.get("SUBFLEET_LIVE_STATE_ROOT") or (tmp_path / "v2"))
    before = {root: _snapshot(root) for root in (V1, DELEGATE, ROSTER) if root.is_dir()}

    report = import_v1(state_root, v1_state=V1, delegate_state=DELEGATE, roster_dir=ROSTER,
                       dry_run=True, write_report=False)
    path = report.write(tmp_path)

    for root, snapshot in before.items():
        assert _snapshot(root) == snapshot, f"the import modified something under {root}"
    if not os.environ.get("SUBFLEET_LIVE_STATE_ROOT"):
        assert not (state_root / "state.sqlite3").exists()
        assert not (state_root / "jobs").exists()

    print(f"\n{'store':<22} {'disposition':<26} {'seen':>7} {'imported':>9} {'skipped':>8}")
    for key, entry in sorted(report.stores.items()):
        print(f"{key:<22} {entry.disposition:<26} {entry.seen:>7} "
              f"{entry.imported:>9} {entry.skipped:>8}")
        for reason, count in sorted(entry.reasons.items()):
            print(f"{'':<22}   {reason}: {count}")
        for note in entry.notes:
            print(f"{'':<22}   note: {note}")
    print("not in the manifest, left alone: " + ", ".join(report.unmanifested or ["-"]))
    print(f"report: {path}")

    payload = json.loads(path.read_text())
    assert payload["dry_run"] is True
    assert set(payload["stores"]) == {row.key for row in importer.MANIFEST}
    assert payload["stores"]["roster"]["imported"] > 0, "no lane came out of the v1 roster"
    assert payload["stores"]["runs"]["seen"] > 0, "the v1 ledger looked empty"
