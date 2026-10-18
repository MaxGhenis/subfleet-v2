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


def test_i4_a_conversation_s_rows_from_before_the_windows_take_its_folder(tmp_path):
    """Review of 52c73076: a row written before `target` took its workspace as its folder,
    so a conversation in `/repo/sub` never met turns in `/repo` in `conversation.diff`.
    Its next turn's record gives its older rows the folder (one workspace for its life,
    C-24.1), and the conversation's diff then names the other conversation."""
    import sqlite3
    root = tmp_path / "state"
    ConversationStore(root).close()
    with sqlite3.connect(root / "conversations.sqlite3") as db:
        db.execute("INSERT INTO turn_trees(attempt_id,message_id,conversation_id,workspace,writable,start_tree,"
                   "started_at,ended_at,target) VALUES ('20260929-110000-old/a1','old','sub','/repo/sub',1,'t',"
                   "'2026-09-29T11:00:00Z','2026-09-29T11:01:00.000Z','/repo/sub')")
    store = ConversationStore(root)
    try:
        store.record_trees(attempt_id="20260929-113000-beside/a1", message_id="beside", conversation_id="top",
                           workspace="/repo", writable=True, started_at="2026-09-29T11:30:00Z", start_tree="t",
                           target="/repo", window_start="2026-09-29T11:30:00.000Z")
        assert store.overlapping("/repo/sub", "2026-09-29T11:00:00Z", besides_conversation="sub") == []
        store.record_trees(attempt_id="20260929-120000-new/a1", message_id="new", conversation_id="sub",
                           workspace="/repo/sub", writable=True, started_at="2026-09-29T12:00:00Z", start_tree="t",
                           target="/repo", window_start="2026-09-29T12:00:00.000Z")
        first = store.first_trees("sub")
        assert first["attempt_id"] == "20260929-110000-old/a1" and first["target"] == "/repo"
        assert [row["conversation_id"] for row in store.overlapping(
            first["target"], first["started_at"], besides_conversation="sub")] == ["top"]
    finally:
        store.close()


@st.composite
def schedules_with_lost_ends(draw):
    """Turns whose start is recorded and whose end is recorded, lost, or not yet come.
    A lost end is an attempt that ended (`finished_at`, whole seconds) with no end
    recorded; the service notices it some ticks later and closes the window then."""
    turns = []
    for index in range(draw(st.integers(1, 6))):
        start = draw(st.integers(0, 60))
        recorded = start + draw(st.integers(0, 6))
        fate = draw(st.sampled_from(["recorded", "lost", "open"]))
        end = recorded + draw(st.integers(0, 12)) if fate != "open" else None
        # Mostly writable turns in one folder, and closes noticed late, so that closes
        # often drop marks that later turns' records made.
        turns.append({"index": index, "folder": draw(st.sampled_from(["/repo", "/repo", "/repo", "/other"])),
                      "writable": draw(st.sampled_from([True, True, True, False])), "legacy": False,
                      "start": start, "recorded": recorded, "fate": fate, "end": end,
                      "noticed": end + draw(st.integers(0, 60)) if fate == "lost" else None})
    events = [(turn["recorded"], 0, "start", turn["index"]) for turn in turns]
    events += [(turn["end"], 1, "end", turn["index"]) for turn in turns if turn["fate"] == "recorded"]
    events += [(turn["noticed"], 2, "close", turn["index"]) for turn in turns if turn["fate"] == "lost"]
    order = draw(st.permutations(range(len(events))))
    events = [events[i] for i in order]
    # One turn's own events keep their order (a close follows the start it closes);
    # different turns' events at one instant come in any order.
    events.sort(key=lambda event: (event[0], event[1]))
    return turns, events


def ms_window(turn: dict) -> tuple[int, float]:
    """The oracle's window in milliseconds: a lost end is the last millisecond of the
    second the job store stamped it in."""
    start = turn["start"] * 250
    if turn["fate"] == "recorded":
        return start, turn["end"] * 250
    if turn["fate"] == "lost":
        return start, (turn["end"] * 250 // 1000) * 1000 + 999
    return start, float("inf")


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schedule=schedules_with_lost_ends())
def test_i4_an_unrecorded_end_closed_later_leaves_exactly_the_marks_its_window_meets(tmp_path_factory, schedule):
    """Review P3-5 of 5e9f2fbd: a turn whose attempt ended with no end recorded kept its
    window open, so every later turn in its folder was marked as sharing it. Closed at
    its attempt's end whenever that is noticed, before or after the others' records,
    the marks are exactly the meetings of the windows the turns really had."""
    turns, events = schedule
    root = tmp_path_factory.mktemp("lost")
    store = ConversationStore(root)
    clock = {"now": ms(0)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "utcnow", lambda: clock["now"])
        try:
            for when, _, kind, index in events:
                clock["now"] = ms(when)
                turn = turns[index]
                if kind == "close":
                    assert store.end_unrecorded(f"20260929-120000-turn-{index}/a1", ended_at=seconds(turn["end"]),
                                                error="ended unrecorded")
                else:
                    record(store, turn, ended=kind == "end")
            dropped = 0
            for turn in turns:
                row = store_module._decode_trees(store.one("SELECT * FROM turn_trees WHERE attempt_id=?",
                                                           (f"20260929-120000-turn-{turn['index']}/a1",)))
                a_start, a_end = ms_window(turn)
                want = sorted(f"20260929-120000-turn-{other['index']}/a1" for other in turns
                              if other is not turn and turn["writable"] and other["writable"]
                              and other["folder"] == turn["folder"]
                              and a_start <= ms_window(other)[1] and ms_window(other)[0] <= a_end)
                assert row["shared"] == want, (turn, turns, events)
                if turn["fate"] == "lost":
                    assert row["error"] == "ended unrecorded" and row["ended_at"]
                    # Marks the open window made that its close had to drop: a turn begun
                    # after the end, whose start was recorded before the close.
                    dropped += sum(other["start"] * 250 > a_end and other["recorded"] <= turn["noticed"]
                                   and other["writable"] and turn["writable"] and other["folder"] == turn["folder"]
                                   for other in turns)
            event(f"marks a close dropped: {min(dropped, 3)}{'+' if dropped >= 3 else ''}")
        finally:
            store.close()


def test_i4_closing_an_unrecorded_end_unmarks_later_turns_and_tells_the_app(tmp_path):
    """The reviewer's case: a turn with no end, and one of another conversation a day
    later marked as sharing its folder with it "still running". Closing the first at its
    attempt's end drops the mark on both sides and writes a change-feed row for each
    message, so the app fetches their changes again; a turn that did meet it keeps its
    mark, and a second close changes nothing."""
    store = ConversationStore(tmp_path / "state")
    try:
        common = {"workspace": "/repo", "writable": True, "start_tree": "t", "target": "/repo"}
        store.record_trees(attempt_id="20260928-120000-lost/a1", message_id="lost", conversation_id="a",
                           started_at="2026-09-28T12:00:00Z", window_start="2026-09-28T12:00:00.000Z", **common)
        store.record_trees(attempt_id="20260928-120002-beside/a1", message_id="beside", conversation_id="b",
                           started_at="2026-09-28T12:00:02Z", window_start="2026-09-28T12:00:02.000Z", **common)
        store.record_trees(attempt_id="20260929-120000-later/a1", message_id="later", conversation_id="c",
                           started_at="2026-09-29T12:00:00Z", window_start="2026-09-29T12:00:00.000Z", **common)
        assert store.turn_trees("later")["shared"] == ["20260928-120000-lost/a1", "20260928-120002-beside/a1"]
        feed = store.changes_after(0)["next"]
        assert store.open_windows() == ["20260928-120000-lost/a1", "20260928-120002-beside/a1",
                                        "20260929-120000-later/a1"]
        assert store.end_unrecorded("20260928-120000-lost/a1", ended_at="2026-09-28T12:05:00Z",
                                    error="the turn's attempt ended (failed) with no end recorded; no end snapshot")
        lost = store.turn_trees("lost")
        assert lost["ended_at"] == "2026-09-28T12:05:00.999Z" and lost["shared"] == ["20260928-120002-beside/a1"]
        assert store.turn_trees("later")["shared"] == ["20260928-120002-beside/a1"]
        assert store.turn_trees("beside")["shared"] == ["20260928-120000-lost/a1", "20260929-120000-later/a1"]
        assert {change["message_id"] for change in store.changes_after(feed)["changes"]} == {"lost", "later"}
        assert all(change["state"] is None for change in store.changes_after(feed)["changes"])
        assert store.open_windows() == ["20260928-120002-beside/a1", "20260929-120000-later/a1"]
        after = store.changes_after(feed)["next"]
        assert not store.end_unrecorded("20260928-120000-lost/a1", ended_at="2026-09-28T12:06:00Z", error="again")
        assert store.turn_trees("lost")["ended_at"] == "2026-09-28T12:05:00.999Z"
        assert store.changes_after(after)["changes"] == []
        assert not store.end_unrecorded("20260101-000000-never/a1", ended_at="2026-09-28T12:06:00Z", error="none")
        # A recorded end that arrives afterwards keeps the first end and the error that explained it.
        store.record_trees(attempt_id="20260928-120000-lost/a1", message_id="lost", conversation_id="a",
                           started_at="2026-09-28T12:00:00Z", ended=True, **common)
        assert store.turn_trees("lost")["ended_at"] == "2026-09-28T12:05:00.999Z"
    finally:
        store.close()


@pytest.mark.parametrize("stamp,end", [("2026-09-28T12:05:00Z", "2026-09-28T12:05:00.999Z"),
                                       ("2026-09-28T12:05:00.250Z", "2026-09-28T12:05:00.250Z"),
                                       ("2026-09-28T08:05:00-04:00", "2026-09-28T12:05:00.999Z")])
def test_i4_an_end_in_whole_seconds_reaches_the_second_s_last_millisecond(stamp, end):
    assert store_module._last_millisecond(stamp) == end
