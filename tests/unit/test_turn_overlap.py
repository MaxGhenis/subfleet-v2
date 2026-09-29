"""I4 (C-26.14, 2026-09-29): a turn's changes never claim sole authorship when another
conversation's turn wrote in the same folder meanwhile.

A turn's window runs from just before admission's start snapshot (`window_start`) to
the recorded end of its attempt (`ended_at`), open while it has none. The store marks
two writable turns of different conversations in one folder as sharing it exactly
when their windows meet, whatever order their starts and ends are recorded in: a
start is recorded when the turn's runner is adopted, which can be after another turn
in the folder has ended, and some attempts are recorded only at their end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet.conversations import store as store_module
from subfleet.conversations.store import ConversationStore

BASE = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TICK = timedelta(milliseconds=250)


def at(tick: int) -> datetime:
    return BASE + tick * TICK


def ms(tick: int) -> str:
    return at(tick).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds(tick: int) -> str:
    """The job store's stamp (`reserved_at`): whole seconds."""
    return at(tick).isoformat(timespec="seconds").replace("+00:00", "Z")


@st.composite
def schedules(draw):
    """Turns with a window start, maybe a separate start record, maybe an end. Each is
    in its own conversation (two turns of one conversation never overlap, I2)."""
    turns = []
    for index in range(draw(st.integers(1, 7))):
        legacy = draw(st.booleans())            # recorded by a build that kept no window start
        start = draw(st.integers(0, 40)) * (4 if legacy else 1)
        recorded = draw(st.one_of(st.none(), st.integers(0, 12).map(lambda d, s=start: s + d)))
        ends = draw(st.booleans()) or recorded is None
        end = (recorded if recorded is not None else start) + draw(st.integers(0, 24)) if ends else None
        turns.append({"index": index, "folder": draw(st.sampled_from(["/repo", "/repo:x", "/other"])),
                      "writable": draw(st.booleans()), "legacy": legacy, "start": start,
                      "recorded": recorded, "end": end})
    events = [(turn["recorded"], "start", turn["index"]) for turn in turns if turn["recorded"] is not None]
    events += [(turn["end"], "end", turn["index"]) for turn in turns if turn["end"] is not None]
    # Events at one instant come in any order.
    order = draw(st.permutations(range(len(events))))
    events = [events[i] for i in order]
    events.sort(key=lambda event: event[0])
    return turns, events


def record(store, turn, *, ended):
    index = turn["index"]
    store.record_trees(attempt_id=f"20260929-120000-turn-{index}/a1", message_id=f"message-{index}",
                       conversation_id=f"conversation-{index}", workspace=turn["folder"] + "/sub",
                       writable=turn["writable"], started_at=seconds(turn["start"]), start_tree="t", ended=ended,
                       target=turn["folder"], window_start=None if turn["legacy"] else ms(turn["start"]))


def meets(a: dict, b: dict) -> bool:
    """The oracle, written apart from the store's: closed windows, an open end reaching on."""
    a_end = float("inf") if a["end"] is None else a["end"]
    b_end = float("inf") if b["end"] is None else b["end"]
    return a["start"] <= b_end and b["start"] <= a_end


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schedule=schedules())
def test_i4_a_turn_is_marked_shared_exactly_when_another_writer_s_window_met_it(tmp_path_factory, schedule):
    turns, events = schedule
    root = tmp_path_factory.mktemp("overlap")
    store = ConversationStore(root)
    clock = {"now": ms(0)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "utcnow", lambda: clock["now"])
        try:
            for when, kind, index in events:
                clock["now"] = ms(when)
                record(store, turns[index], ended=kind == "end")
            marked = 0
            for turn in turns:
                row = store.one("SELECT * FROM turn_trees WHERE attempt_id=?",
                                (f"20260929-120000-turn-{turn['index']}/a1",))
                shared = store_module._decode_trees(row)["shared"]
                want = sorted(f"20260929-120000-turn-{other['index']}/a1" for other in turns
                              if other is not turn and turn["writable"] and other["writable"]
                              and other["folder"] == turn["folder"] and meets(turn, other))
                assert shared == want, (turn, turns, events)
                marked += bool(want)
            event(f"turns marked shared: {min(marked, 3)}{'+' if marked >= 3 else ''}")
        finally:
            store.close()


def test_i4_a_turn_found_to_share_later_tells_the_app_to_look_again(tmp_path):
    """A turn that has ended is marked when a turn whose window began before its end is
    recorded afterwards; the change feed gets a row for the ended turn's message (state
    null, as C-26.14's end does), so the app fetches its changes again."""
    store = ConversationStore(tmp_path / "state")
    try:
        first = {"index": 0, "folder": "/repo", "writable": True, "legacy": False, "start": 0}
        second = {"index": 1, "folder": "/repo", "writable": True, "legacy": False, "start": 2}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(store_module, "utcnow", lambda: ms(4))
            record(store, first, ended=False)
            record(store, first, ended=True)
            feed = store.changes_after(0)["next"]
            patch.setattr(store_module, "utcnow", lambda: ms(9))
            record(store, second, ended=False)                  # its runner adopted after the first ended
        assert store.turn_trees("message-0")["shared"] == ["20260929-120000-turn-1/a1"]
        assert store.turn_trees("message-1")["shared"] == ["20260929-120000-turn-0/a1"]
        assert {change["message_id"] for change in store.changes_after(feed)["changes"]} == {"message-0", "message-1"}
    finally:
        store.close()


def test_i4_turns_of_one_conversation_are_never_marked_as_each_other_s(tmp_path):
    """Two attempts of one conversation run one after the other (I2); recorded windows
    that meet (a re-admission's clock) are not another writer."""
    store = ConversationStore(tmp_path / "state")
    try:
        for n, start in enumerate(("2026-09-29T12:00:00.000Z", "2026-09-29T12:00:00.500Z")):
            store.record_trees(attempt_id=f"20260929-120000-turn-{n}/a1", message_id="message", conversation_id="c",
                               workspace="/repo", writable=True, started_at="2026-09-29T12:00:00Z",
                               start_tree="t", target="/repo", window_start=start)
        assert store.turn_trees("message")["shared"] == []
    finally:
        store.close()


def test_i4_a_store_from_before_the_windows_gains_them_and_keeps_its_rows(tmp_path):
    """The columns are additive (as `worktree_json`): a store written without them opens,
    its rows take their workspace as their folder, and new rows are compared with them."""
    import sqlite3
    root = tmp_path / "state"
    ConversationStore(root).close()
    with sqlite3.connect(root / "conversations.sqlite3") as db:
        db.execute("DROP INDEX turn_trees_by_target")
        db.execute("ALTER TABLE turn_trees DROP COLUMN shared_json")
        db.execute("ALTER TABLE turn_trees DROP COLUMN window_start")
        db.execute("ALTER TABLE turn_trees DROP COLUMN target")
        db.execute("INSERT INTO turn_trees(attempt_id,message_id,conversation_id,workspace,writable,started_at) "
                   "VALUES ('20260929-115959-old/a1','old','old-conversation','/repo',1,'2026-09-29T11:59:59Z')")
    store = ConversationStore(root)
    try:
        assert store.turn_trees("old")["target"] == "/repo" and store.turn_trees("old")["shared"] == []
        store.record_trees(attempt_id="20260929-120000-new/a1", message_id="new", conversation_id="c",
                           workspace="/repo", writable=True, started_at="2026-09-29T12:00:00Z", start_tree="t",
                           target="/repo", window_start="2026-09-29T12:00:00.000Z")
        assert store.turn_trees("new")["shared"] == ["20260929-115959-old/a1"]      # still open: it met it
    finally:
        store.close()
