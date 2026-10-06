"""Exercise the native resume CLI against the real socket and fake provider."""

import json
import os
import sys

from tests import waits
from tests.fake.conftest import REPO


def test_resume_cli_decodes_raw_show_row_and_continues_native_session(daemon):
    """C-12.3/4: JSON stored in a show row crosses the CLI boundary correctly."""
    daemon.start()
    source_id = daemon.submit(kind="revive", caller_session="cli-native-source",
                              exclusions=["unrelated-excluded-lane"])
    assert daemon.finished(source_id)["state"] == "succeeded"
    source_attempt = daemon.attempts(source_id)[0]
    envelope = daemon.call("show", job_id=source_id)
    assert envelope["job"]["exclusions"] == '["unrelated-excluded-lane"]'
    result = waits.run(
        [sys.executable, "-m", "subfleet.cli", "resume", source_id, "Continue.", "--json"],
        cwd=REPO, env={**os.environ, "SUBFLEET_HOME": str(daemon.root), "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=10, watch=daemon.daemon_tree,
    )
    assert result.returncode == 0, result.stderr
    resumed_id = json.loads(result.stdout)["job_id"]
    resumed = daemon.finished(resumed_id)
    assert resumed["state"] == "succeeded"
    assert resumed["parent_job_id"] == source_id and resumed["independent"]
    assert json.loads(resumed["exclusions"]) == ["unrelated-excluded-lane"]
    attempt = daemon.attempts(resumed_id)[0]
    assert attempt["lane_id"] == source_attempt["lane_id"]
    assert attempt["model_requested"] == source_attempt["model_requested"]
    assert attempt["native_session_id"] == source_attempt["native_session_id"] == "cli-native-source"
