"""C-17.7: the daemon records a batch label beside the job and reports it, with no new column."""

import pytest

from subfleet import protocol
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository

BATCH = {"id": "handoff-0920", "label": "codex handoff", "index": 2, "size": 5}


def test_c17_7_the_label_is_recorded_listed_and_shown(state_daemon):
    """C-17.7 `list` and `show` carry the batch; a job without one carries nothing."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    labelled = daemon.dispatch("submit", harness.submit_args(batch=BATCH))["job_id"]
    plain = daemon.dispatch("submit", harness.submit_args())["job_id"]
    rows = {row["job_id"]: row for row in daemon.dispatch("list", {})["jobs"]}
    assert rows[labelled]["batch"] == BATCH and "batch" not in rows[plain]
    assert daemon.dispatch("show", {"job_id": labelled})["batch"] == BATCH
    assert daemon.dispatch("show", {"job_id": plain})["batch"] is None
    assert "batch" not in daemon.store.get_job(labelled)                  # no schema change
    manifest = daemon._read_json(daemon.root / "jobs" / labelled / "manifest.json")
    assert manifest["batch"] == BATCH and "batch" not in manifest["job"]


def test_c17_7_the_label_is_not_part_of_the_payload(state_daemon):
    """C-6.2 a label never changes what a request id means."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    args = harness.submit_args()
    first = daemon.dispatch("submit", {**args, "batch": BATCH})
    again = daemon.dispatch("submit", {**args, "batch": {**BATCH, "index": 3}})
    assert again == {**first, "created": False}


@pytest.mark.parametrize("bad", [
    "handoff", {"id": "", "label": "x", "index": 1, "size": 1}, {"id": "x", "label": "x" * 81, "index": 1, "size": 1},
    {"id": "x", "label": "x", "index": 0, "size": 1}, {"id": "x", "label": "x", "index": 3, "size": 2},
    {"id": "x", "label": "x", "index": True, "size": 1}, {"id": "x", "label": "x", "index": 1, "size": 257},
])
def test_c17_7_a_malformed_label_is_invalid_input(state_daemon, bad):
    """C-17.7 exit 2 and nothing stored."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    with pytest.raises(protocol.ProtocolError) as error:
        daemon.dispatch("submit", harness.submit_args(batch=bad))
    assert error.value.code == 2 and daemon.store.list_jobs() == []
