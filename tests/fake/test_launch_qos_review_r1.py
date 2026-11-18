"""D-QOS-BOOKED1 review: persisted ancestry cycles and explicit gate requests."""

import pytest

from tests.fake.test_daemon_launch import launch_state  # noqa: F401
from tests.fake.test_launch_provider_qos import asks_inherit, launch_first, submit


CHOSEN = "0a1b2c3d-0000-4000-8000-00000000c0de"
OTHER = "0a1b2c3d-0000-4000-8000-0000000000ff"


@pytest.mark.parametrize("listed", [False, True], ids=["cycle-unlisted", "cycle-listed"])
def test_launch_walks_a_persisted_ancestor_cycle_once(launch_state, listed):
    daemon, harness, calls = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]
    parent = submit(daemon, harness, caller_session=CHOSEN if listed else OTHER)
    daemon.store.update_job(parent, state="succeeded")
    child = submit(daemon, harness, caller_session=OTHER, parent_job_id=parent, provider_qos="default")
    daemon.store.update_job(parent, parent_job_id=child)
    _, command = launch_first(daemon, calls, child)
    assert asks_inherit(command) is listed


@pytest.mark.parametrize("kind, requested, inherited", [
    ("turn", "default", False),
    ("gate-review", None, False),
    ("gate-review", "utility", False),
    ("gate-review", "default", True),
])
def test_turns_and_gate_rounds_follow_the_request_and_class_rule(launch_state, kind, requested, inherited):
    daemon, harness, _ = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos=requested)
    # Socket submission deliberately refuses turns; the store represents the
    # trusted conversation/gate producer's row at this decision boundary.
    daemon.store.update_job(job_id, kind=kind)
    assert (daemon._launch_qos(daemon.store.get_job(job_id))[0] == "inherit") is inherited
