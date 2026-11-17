"""D-REVIEW-FAMILY1 fix round 1, through a real daemon (review-a3-r1.md).

P2 daemon.py:3989: the Codex writer exemption follows the session id's kind.
P2 cli.py:3660 / daemon.py:3195: same-family consent is refused from inside a
job, and who gave it is recorded and shown. P2 policy.py:77: a review whose
policy names no other-family reviewer is accepted and waits. P3: a same-family
lane pin is `review-same-family` at submit.
"""
from __future__ import annotations

import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from tests.fake.test_routing_end_to_end import claude_lane  # noqa: F401
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_writable_fanout import fleet, make_repo, writable  # noqa: F401


def test_claude_session_declaring_codex_keeps_the_writer_instance_check(fleet):
    """The reviewer's case: a Claude session id with `--caller-family codex`."""
    daemon, harness, _ = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"), pid=None,
                     caller_family="codex", caller_session_kind="claude")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, make_repo(harness.root / "b"), pid=None,
                 caller_family="codex", caller_session_kind="claude")
    assert error.value.reason == "unidentified-writer"
    assert daemon._submitted(first)["caller_family_disagreement"] == {
        "declared": "codex", "session_kind": "claude", "writer_check": "applied"}
    shown = daemon.dispatch("show", {"job_id": first})
    assert shown["caller_family_disagreement"]["declared"] == "codex"
    assert shown["caller_session_kind"] == "claude"


def test_codex_family_without_a_codex_session_id_keeps_the_check(fleet):
    """An older client sends no kind: no exemption, whatever family it declares."""
    daemon, harness, _ = fleet
    writable(daemon, harness, make_repo(harness.root / "a"), pid=None, caller_family="codex")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, make_repo(harness.root / "b"), pid=None, caller_family="codex")
    assert error.value.reason == "unidentified-writer"


def test_codex_thread_id_declaring_claude_is_a_recorded_disagreement_and_checked(fleet):
    daemon, harness, _ = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"), pid=None,
                     caller_family="claude", caller_session_kind="codex")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, make_repo(harness.root / "b"), pid=None,
                 caller_family="claude", caller_session_kind="codex")
    assert error.value.reason == "unidentified-writer"
    assert daemon._submitted(first)["caller_family_disagreement"]["session_kind"] == "codex"


def test_a_codex_thread_id_keeps_its_exemption(fleet):
    daemon, harness, _ = fleet
    jobs = [writable(daemon, harness, make_repo(harness.root / name), pid=None,
                     caller_family="codex", caller_session_kind="codex") for name in ("a", "b")]
    assert len(set(jobs)) == 2
    assert "caller_family_disagreement" not in daemon._submitted(jobs[0])


@pytest.mark.parametrize("kind", ["codex-cli", 1, True])
def test_an_invalid_session_kind_is_refused(state_daemon, kind):
    service, harness = state_daemon
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(task="review", caller_family="codex",
                                                       caller_session_kind=kind))
    assert error.value.reason == "invalid-args"


def test_same_family_consent_from_inside_a_job_is_refused_before_insertion(state_daemon):
    service, harness = state_daemon
    parent = service.dispatch("submit", harness.submit_args(task="research"))["job_id"]
    before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
              for table in ("jobs", "events", "attempts")}
    with pytest.raises(AdapterError) as error:
        service.dispatch("submit", harness.submit_args(task="review", caller_family="codex", pinned_model="astra",
                                                       allow_same_family=True, parent_job_id=parent))
    assert error.value.code == 7 and error.value.reason == "same-family-consent"
    assert protocol.refusal_text(error.value)[0].startswith("review: --allow-same-family is refused")
    assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
            for table in before} == before


def test_same_family_consent_records_who_gave_it_and_show_says_so(state_daemon):
    service, harness = state_daemon
    reply = service.dispatch("submit", harness.submit_args(
        task="review", caller_family="codex", pinned_model="astra", allow_same_family=True,
        caller_session="synthetic-codex-thread", caller_session_kind="codex"))
    shown = service.dispatch("show", {"job_id": reply["job_id"]})
    assert shown["same_family_consent"] == {"session": "synthetic-codex-thread", "session_kind": "codex"}
    assert shown["job"]["allow_same_family"] is True
    plain = service.dispatch("submit", harness.submit_args(task="review", caller_family="codex",
                                                           pinned_model="opus"))
    assert "same_family_consent" not in service.dispatch("show", {"job_id": plain["job_id"]})


def test_a_review_with_no_other_family_reviewer_is_accepted_and_waits(state_daemon):
    """P2 policy.py:77: never `pinned_model: unknown model 'sol61'`."""
    service, harness = state_daemon
    service.policy.pop("review_family_defaults", None)
    service.policy["models"].pop("sol61", None)
    service.policy["chains"]["review"] = ["haiku", "sonnet", "opus", "opus"]
    reply = service.dispatch("submit", harness.submit_args(task="review", tier="standard", caller_family="claude",
                                                           pinned_model=None))
    job = service.store.get_job(reply["job_id"])
    assert job["state"] == "queued" and job["caller_family"] == "claude"
    why = service.dispatch("why", {"job_id": reply["job_id"]})
    text = str(why)
    assert "policy names no codex reviewer" in text and "unknown model" not in text


@pytest.mark.parametrize("allow", [False, True])
def test_a_same_family_lane_pin_is_refused_at_submit(state_daemon, allow):
    """P3 scheduler.py:822: the refusal is `review-same-family`, not 'different providers'."""
    service, harness = state_daemon
    service.store.put_lane(claude_lane("claude-1"))
    with pytest.raises(AdapterError) as error:
        service.dispatch("submit", harness.submit_args(task="review", tier="standard", caller_family="claude",
                                                       pinned_model=None, pinned_lane="claude-1",
                                                       allow_same_family=allow))
    assert error.value.code == 7 and error.value.reason == "review-same-family"
