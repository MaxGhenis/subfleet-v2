"""C-29.15, C-29.16: the sidebar's triage and the Worktree toggle render offscreen, as the visual pass does.

`TriageSnapshotProbe.swift` renders the real views (the sidebar with its Pinned and
Unread sections and a worktree conversation open, the Archived view with an archived
conversation open, and New conversation in a git checkout with Worktree on) at
1440 x 900 @2x into PNGs without ordering a window on screen. Set
`SF_TRIAGE_SNAPSHOTS_OUT` to keep them (the report's screenshots came from it).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="session")
def triage_probe(tmp_path_factory):
    folder = tmp_path_factory.mktemp("triage-snapshots")
    return compile_probe(folder / "probe", ROOT / "tests/frontend/TriageSnapshotProbe.swift", "SUBFLEET_VIEW_TEST")


def test_c29_15_the_sidebar_sections_and_the_toggle_render_offscreen(triage_probe, tmp_path):
    out = tmp_path / "png"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env["SUBFLEET_HOME"] = str(tmp_path / "home")
    result = subprocess.run([str(triage_probe), str(out)], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, (result.stdout, result.stderr[-2000:])
    assert "visible windows: 0" in result.stdout
    names = sorted(path.name for path in out.glob("*.png"))
    assert names == sorted(f"triage-{scene}-{mode}.png" for scene in ("sidebar", "archived", "new-worktree")
                           for mode in ("dark", "light"))
    for path in out.glob("*.png"):
        assert struct.unpack(">II", path.read_bytes()[16:24]) == (2880, 1800)
    keep = os.environ.get("SF_TRIAGE_SNAPSHOTS_OUT")
    if keep:
        Path(keep).mkdir(parents=True, exist_ok=True)
        for path in out.glob("*.png"):
            shutil.copy2(path, Path(keep) / path.name)
