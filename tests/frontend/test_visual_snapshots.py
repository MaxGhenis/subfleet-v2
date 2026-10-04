"""Snapshot reproduction must work on Retina and settle the refused-folder fixture."""
import os
from pathlib import Path
import struct
import subprocess

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="session")
def snapshot_probe(tmp_path_factory):
    folder = tmp_path_factory.mktemp("snapshots")
    # Exercise the renderer with the natural capture sizes of both display
    # scales, even on a host whose currently attached display is only 1x.
    # Instrument only AppKit's capture factory; leave the renderer unchanged.
    source = (ROOT / "tests/frontend/SnapshotProbe.swift").read_text()
    assert "let host = NSHostingView(" in source
    source = source.replace("let host = NSHostingView(", "let host = SnapshotTestHostingView(")
    source += '''
final class SnapshotTestHostingView<Content: View>: NSHostingView<Content> {
    override func bitmapImageRepForCachingDisplay(in rect: NSRect) -> NSBitmapImageRep? {
        guard let value = ProcessInfo.processInfo.environment["SF_SNAPSHOT_BACKING_SCALE"],
              let scale = Int(value) else { return super.bitmapImageRepForCachingDisplay(in: rect) }
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
            pixelsWide: Int(rect.width) * scale, pixelsHigh: Int(rect.height) * scale,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = rect.size
        return rep
    }
}
'''
    probe = folder / "SnapshotProbe.swift"
    probe.write_text(source)
    return compile_probe(folder / "probe", probe, "SUBFLEET_VIEW_TEST")


def snapshots(probe, out, scenes, capture_scale=None):
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env.update(SUBFLEET_HOME=str(out / ".home"), SF_SNAPSHOT_SCENES=scenes)
    if capture_scale is not None:
        env["SF_SNAPSHOT_BACKING_SCALE"] = str(capture_scale)
    else:
        env.pop("SF_SNAPSHOT_BACKING_SCALE", None)
    result = subprocess.run([str(probe), str(out), str(ROOT / "tests/fixtures/visual/progress.json"),
        str(ROOT / "tests/fixtures/visual/approvals.json")], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout, result.stderr[-2000:])
    for path in out.glob("*.png"):
        assert struct.unpack(">II", path.read_bytes()[16:24]) == (2880, 1800)
    assert "visible windows: 0" in result.stdout
    return result.stdout


def test_snapshot_pixel_size_is_independent_of_display_scale(snapshot_probe, tmp_path):
    for scale in (1, 2):
        snapshots(snapshot_probe, tmp_path / str(scale), "finished", capture_scale=scale)


def test_refused_folder_fixture_settles_and_renders(snapshot_probe, tmp_path):
    snapshots(snapshot_probe, tmp_path, "refused", capture_scale=1)


def test_rerender_all_scenes_including_codex_and_failures(snapshot_probe, tmp_path):
    out = Path(os.environ.get("SF_REVIEW_SNAPSHOTS", tmp_path))
    scenes = "sidebar,finished,live,live-expanded,blocked,new,refused,permission,empty,question,codex-command,codex-file-change,codex-permissions,claude-write,codex-progress,failed,stopped,withdrawn,stop-too-late,text-only"
    snapshots(snapshot_probe, out, scenes)
    assert len(list(out.glob("*.png"))) == 40
