"""C-29.14: the suggestion card and the sidebar's Suggested section render offscreen, as the visual pass does.

`SuggestionSnapshotProbe.swift` renders the real views (the sidebar, the
conversation with its card, a suggestion opened from the sidebar) at 1440 x 900
@2x into PNGs without ordering a window on screen. Set
`SF_SUGGESTION_SNAPSHOTS_OUT` to keep them (the report's screenshots came from it).
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
def suggestion_probe(tmp_path_factory):
    folder = tmp_path_factory.mktemp("suggestion-snapshots")
    return compile_probe(folder / "probe", ROOT / "tests/frontend/SuggestionSnapshotProbe.swift", "SUBFLEET_VIEW_TEST")


def test_c29_14_the_card_and_the_suggested_section_render_offscreen(suggestion_probe, tmp_path):
    out = tmp_path / "png"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env["SUBFLEET_HOME"] = str(tmp_path / "home")
    result = subprocess.run([str(suggestion_probe), str(out)], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout, result.stderr[-2000:])
    assert "visible windows: 0" in result.stdout
    names = sorted(path.name for path in out.glob("*.png"))
    assert names == sorted(f"suggestion-{scene}-{mode}.png" for scene in ("card", "card-started", "detail")
                           for mode in ("dark", "light"))
    for path in out.glob("*.png"):
        assert struct.unpack(">II", path.read_bytes()[16:24]) == (2880, 1800)
    keep = os.environ.get("SF_SUGGESTION_SNAPSHOTS_OUT")
    if keep:
        Path(keep).mkdir(parents=True, exist_ok=True)
        for path in out.glob("*.png"):
            shutil.copy2(path, Path(keep) / path.name)
