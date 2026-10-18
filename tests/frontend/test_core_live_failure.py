"""The live probe stops at a failed dependency, without needing a daemon."""

from pathlib import Path
import tempfile

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.test_core_live import PNG

pytestmark = needs_swift


def test_a_failed_live_dependency_stops_and_dumps_the_app_state(core_probe, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(PNG)
    exchanges = tmp_path / "exchanges.jsonl"
    # A fresh, short development root has no daemon socket. The capability check
    # therefore fails immediately, before any conversation or turn can be started.
    with tempfile.TemporaryDirectory(prefix="sf-live-stop-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        out = run_probe(core_probe, "live", tmp_path / "app", tmp_path / "workspace", "unused-session",
                        image, exchanges, env={"SUBFLEET_HOME": str(root)})

    checks = out["checks"]
    assert [check["name"] for check in checks] == [
        "endpoint resolves for the development build",
        "capabilities: the daemon speaks conversations.v1",
    ]
    assert [check["passed"] for check in checks] == [True, False]
    assert out["notes"] == {
        "stopped_at": "capabilities: the daemon speaks conversations.v1",
        "state": {
            "app": {"timelines": {}, "pending_approvals": {}, "focused": None, "watch_cursor": 0},
            "daemon": {},
        },
    }
