"""C-25.2: archive preserves actual message/event history and round-trips rows."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.conversations.turn import TERMINAL_STATES
from tests.unit.test_conversation_archive import archive, messages_and_events, row
from tests.unit.test_conversation_service import conversation, submit, svc  # noqa: F401
from tests.unit.test_suggestions import people  # noqa: F401


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(history=st.lists(st.tuples(st.sampled_from(sorted(TERMINAL_STATES)),
                                st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=80)),
                       min_size=1, max_size=4))
def test_property_archive_preserves_seeded_history_and_round_trips(people, history):
    cid = conversation(people)
    predecessor = None
    for index, (state, text) in enumerate(history):
        mid = submit(people, cid, text=f"message {index}: {text}", after=predecessor)
        people.store.append_events(conversation_id=cid, message_id=mid, attempt_id=f"{mid}/a1",
                                   stdout_offset=1, stdin_seq=0,
                                   events=[("stdout", "0", 1, "text", {"text": text}),
                                           ("stdout", "0", 2, "turn.completed", {"state": state})])
        people.store.set_state(mid, state)
        predecessor = mid

    original = row(people, cid)
    held = messages_and_events(people, cid)
    assert len(held[0]) == len(history) and len(held[1]) == 2 * len(history)
    start = people.store.one("SELECT COALESCE(MAX(seq),0) AS seq FROM changes")["seq"]

    def feed():
        return people.store.query("SELECT * FROM changes WHERE seq>? ORDER BY seq", (start,))

    archive(people, cid)
    archived = row(people, cid)
    assert archived["archived_at"] is not None
    assert {**archived, "archived_at": None} == original
    assert messages_and_events(people, cid) == held
    assert [(c["conversation_id"], c["message_id"], c["state"]) for c in feed()] == [(cid, None, None)]
    archive(people, cid)
    assert row(people, cid) == archived and len(feed()) == 1
    assert messages_and_events(people, cid) == held

    archive(people, cid, False)
    assert row(people, cid) == original and messages_and_events(people, cid) == held
    assert [(c["conversation_id"], c["message_id"], c["state"]) for c in feed()] == [(cid, None, None)] * 2
    archive(people, cid, False)
    assert row(people, cid) == original and len(feed()) == 2
    assert messages_and_events(people, cid) == held
