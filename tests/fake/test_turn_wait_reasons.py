"""C-24.4, C-29.11 (I3), end to end through the daemon's own dispatch and admission.

A conversation message is dispatched by the conversation service (`_dispatch`, which
submits its turn job through the ordinary submit path) and admission's turn pass
looks at it (`_admit_turns`). Whatever holds it, the message's `state_reason` names
it: another job's lease with that job, closed lanes with their reset, a turn cap as
capacity; a placed turn says it is starting. The app then shows the reason, and says
capacity only for `capacity:` (tests/frontend/test_core_timeline.py).

On 2026-09-28 four messages waited 29 minutes to 13 hours for another conversation's
turn in the same folder while their `state_reason` was empty and the app said
"Waiting for capacity".
"""

from __future__ import annotations

import uuid

from subfleet.conversations import waits
from subfleet.conversations.turn import WAITING
from tests.fake.test_admission_latency import commit, fleet_daemon, measure, submit
from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live

SETTINGS = {"model": "astra", "effort": None, "fast": False, "permission": "accept-edits", "auto_continue": True}


def message_in(service, harness, title, *, workspace=None, settings=None):
    """A person's message in a new Codex conversation, dispatched: its turn job exists."""
    conversations = service.conversations
    conversation, _ = conversations.store.create_conversation(
        provider="codex", workspace=str(workspace or harness.workdir), workspace_kind="in-place",
        settings=settings or SETTINGS, origin="new", title=title, lane_id=CODEX[0])
    mid = str(uuid.uuid4())
    conversations.store.submit_message(conversation_id=conversation["conversation_id"], message_id=mid,
                                       after_message_id=None, text="edit", attachments=[],
                                       settings=settings or SETTINGS)
    conversations._dispatch()
    message = conversations.store.message(mid)
    assert message["job_id"], message
    return conversation["conversation_id"], mid, message["job_id"]


def reason(service, mid):
    message = service.conversations.store.message(mid)
    assert message["state"] == WAITING and message["state_reason"], message       # I3
    return message["state_reason"]


def test_i3_a_bound_message_says_so_before_admission_looks(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        _, mid, _ = message_in(service, harness, "First")
        assert reason(service, mid) == waits.SUBMITTED


def test_i3_a_turn_behind_a_detached_writer_names_it_and_is_not_capacity(tmp_path):
    """A detached writer holds the checkout (C-6.5): the message names that job, never
    capacity; once the job ends the turn is placed and says it is starting."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        writer = submit(service, harness, sandbox="workspace-write", in_place=True)
        service._admit()
        assert _live(service, writer)
        _, mid, job_id = message_in(service, harness, "Beside the writer")
        service._admit_turns()
        text = reason(service, mid)
        assert text == f"lease: detached job {writer} is writing in this folder, and a detached writer works alone"
        _end(service, writer)
        service.store.update_job(job_id, next_check_at=None)
        service._admit_turns()
        assert _live(service, job_id)
        assert reason(service, mid) == waits.PLACED


def test_i1_i3_two_conversations_in_one_folder_are_both_placed(tmp_path):
    """I1 through the service: the second conversation's message in the folder is not held
    by the first's turn; both are placed by one turn pass and both say they are starting."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        _, first, first_job = message_in(service, harness, "One")
        _, second, second_job = message_in(service, harness, "Two")
        service._admit_turns()
        assert _live(service, first_job) and _live(service, second_job), service._holds
        assert reason(service, first) == reason(service, second) == waits.PLACED


def test_i3_closed_lanes_name_their_reset_and_a_turn_cap_is_capacity(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        commit(service, "close", CODEX[0], 1)             # the conversation's lane (a Codex thread's home)
        _, mid, _ = message_in(service, harness, "Closed")
        service._admit_turns()
        text = reason(service, mid)
        assert text.startswith("closed: every Codex lane that could take it is closed until ") and "UTC" in text
    with fleet_daemon(tmp_path / "capped") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        service.policy["conversations"].update({"max_active_turns": 1})
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        _, first, first_job = message_in(service, harness, "Running")
        service._admit_turns()
        assert _live(service, first_job)
        _, second, _ = message_in(service, harness, "Capped")
        service._admit_turns()
        assert reason(service, second).startswith("capacity: the conversations' turn pool is ")


def test_i3_an_older_pass_s_notes_never_land_over_a_newer_one_s(tmp_path):
    """Review of 63698f1e: two workers run turn passes and note after the pass lock is
    released, so a slower older pass could write `lease` over the newer pass's `placed`.
    Notes carry the pass's number and an older one is dropped whole."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        _, mid, job_id = message_in(service, harness, "Raced")
        notes = service.conversations
        notes.note_holds({}, placed=[job_id], seq=10**6)
        assert reason(service, mid) == waits.PLACED
        notes.note_holds({job_id: {"reason": "fleet-full", "max_active_attempts": 1}}, seq=10**6 - 1)
        assert reason(service, mid) == waits.PLACED


def test_i3_a_note_before_its_message_is_bound_is_tried_again_soon(tmp_path):
    """Review of 63698f1e: a pass can look at a turn job before the dispatcher binds its
    message; that note reaches nothing, so it is not remembered for NOTE_REFRESH_S: the
    next pass a second later writes it. One job's failure leaves the others' notes."""
    from subfleet.conversations import service as service_module
    from subfleet.conversations.service import CLAIMED
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        _, mid, job_id = message_in(service, harness, "Early")
        _, other, other_job = message_in(service, harness, "Other")
        notes = service.conversations
        now = [1000.0]
        patch.setattr(notes, "clock", lambda: now[0])
        notes.store.set_state(mid, WAITING, reason=CLAIMED, job_id=None)             # not bound yet
        hold = {"reason": "fleet-full", "max_active_attempts": 1}
        notes.note_holds({job_id: hold}, seq=10**6)
        assert reason(service, mid) == CLAIMED
        notes.store.set_state(mid, WAITING, reason=CLAIMED, job_id=job_id)           # the dispatcher binds it
        now[0] += service_module.NOTE_UNBOUND_RETRY_S + .1
        notes.note_holds({job_id: hold}, seq=10**6 + 1)
        assert reason(service, mid).startswith("capacity: the conversations' turn pool is full")
        real = notes._who

        def broken(job):
            raise RuntimeError("a store hiccup")
        patch.setattr(notes, "_who", broken)
        failing = {"reason": "slot-kept", "kept_for": other_job}
        now[0] += service_module.NOTE_REFRESH_S + 1
        import pytest
        with pytest.raises(RuntimeError):
            notes.note_holds({job_id: failing, other_job: {"reason": "route-moved"}}, seq=10**6 + 2)
        assert reason(service, other).startswith("admission: its lane changed")
        patch.setattr(notes, "_who", real)


def test_i3_a_turn_whose_lane_can_never_take_it_names_the_lane_and_why(tmp_path):
    """C-11.8 (release/217's #85) meets I3: a Codex conversation keeps its lane (C-26.2),
    so its turn is pinned there, and a pin that lane can never admit is held
    `pin-unadmittable`. The message names the lane and the refusal, is not capacity, and
    the turn is placed once the lane is usable again (it is never failed, C-26.12)."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        service.store.update_lane(CODEX[0], enabled=0)
        _, mid, job_id = message_in(service, harness, "Pinned")
        assert service.store.get_job(job_id)["pinned_lane"] == CODEX[0]
        service._admit_turns()
        assert service._holds[job_id]["reason"] == "pin-unadmittable"
        assert reason(service, mid) == (f"no-lane: this conversation's lane {CODEX[0]} cannot take it "
                                        f"({CODEX[0]} is disabled); it waits until that changes")
        service.store.update_lane(CODEX[0], enabled=1)
        service._admit_turns()
        assert _live(service, job_id), service._holds.get(job_id)
        assert reason(service, mid) == waits.PLACED
