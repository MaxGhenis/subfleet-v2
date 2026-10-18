"""C-27.1, design §8: a provider's request becomes its approval, its `approval.requested`
event and the message's `approval-needed` in one transaction.

Found by `tests/frontend/test_core_live.py` in CI (2026-10-01, about 3 runs in 7 on
release/217): the runner committed the event batch first, then published the exact
request (two fsyncs) and committed the approval, then moved the message. The event's
commit woke the app's `conversation.events` long poll; the `approval.list` the app
sent at once to answer the card could come back without the approval, so the card had
no approval id (`"approval_id": null`, Dock badge 0, in run 36747800501) and Review
would say "That approval is no longer pending."

Every commit the runner makes is checked, as a reader would see it right after it,
so nothing here depends on timing.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import hypothesis
from hypothesis import strategies as st
import pytest

from subfleet.conversations.runner import FLUSH_S, Clocks, TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import Approval, Event, Step, TurnSpec

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"
SETTINGS = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}
QUESTIONS = [{"question": "Which color?", "header": "Color", "multiSelect": False,
              "options": [{"label": "Blue", "description": "calm"}, {"label": "Red", "description": "bold"}]}]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def asking(root: Path):
    """A store holding one running message, and a Claude turn runner for its attempt
    (the relay is never reached: nothing here sends)."""
    store = ConversationStore(root / "state")
    cid = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                    settings=SETTINGS, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None, text="hi",
                         attachments=[], settings=SETTINGS)
    store.set_state(MID, "running")
    runner, clock = runner_for(store, cid, root)
    return store, cid, runner, clock


def runner_for(store: ConversationStore, cid: str, root: Path) -> tuple[TurnRunner, Clock]:
    """A runner for the message's attempt `job/a1`: the first, or one a later daemon adopts."""
    clock = Clock()
    spec = TurnSpec(provider="claude", message_id=MID, text="hi", model_id="opus[1m]", permission="ask",
                    native_session_id=None, new_session_id=SID)
    runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                        conversation_id=cid, attempt_dir=root / "a1", control_socket=str(root / "none.sock"),
                        on_outcome=lambda r: None, on_contain=lambda a: None, clocks=Clocks(), clock=clock)
    return runner, clock


def watch_commits(store: ConversationStore, cid: str) -> list[dict]:
    """After every commit, what the daemon's ops would answer then: the request ids
    `conversation.events` announces, the ones `approval.list` lists (pending) and the
    store holds in any state, the message's state, and the watch feed's newest
    pending count (`conversation.watch`). `transaction()` notifies after each commit."""
    seen: list[dict] = []
    notify = store.notify

    def after_commit() -> None:
        events = store.events_after(cid, 0)["events"]
        changes = store.changes_after(0)["changes"]
        seen.append({
            "asked": sorted(e["data"]["request_id"] for e in events if e["kind"] == "approval.requested"),
            "listed": sorted(a["provider_request_id"] for a in store.approvals(conversation_id=cid)),
            "held": sorted(a["provider_request_id"] for a in store.approvals(conversation_id=cid, state=None)),
            "state": store.message(MID)["state"],
            "feed_pending": changes[-1]["pending_approvals"] if changes else None,
        })
        notify()

    store.notify = after_commit
    return seen


def feed(runner: TurnRunner, row: dict) -> None:
    """One provider stdout line, as the runner reads it (`_read_stdout`)."""
    line = json.dumps(row).encode()
    offset = runner.offset
    runner.offset += len(line) + 1
    runner._apply(runner.driver.feed(line, offset))


@pytest.mark.parametrize("tool,tool_input", [("Bash", {"command": "echo approved-by-person", "description": "Say hello"}),
                                             ("AskUserQuestion", {"questions": QUESTIONS})])
def test_no_reader_sees_approval_requested_before_its_approval(tmp_path, tool, tool_input):
    """C-27.1, design §8: the rows the fake Claude sends for `[fake:approval]` and
    `[fake:question]` (a tool_use, then its `can_use_tool` request). At every commit,
    a reader that sees the `approval.requested` event finds the approval in
    `approval.list`, the message `approval-needed`, and the watch feed counting it;
    before that commit it sees none of them."""
    store, cid, runner, _ = asking(tmp_path)
    seen = watch_commits(store, cid)
    feed(runner, {"type": "assistant", "parent_tool_use_id": None, "message": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": tool, "input": tool_input}]}})
    feed(runner, {"type": "control_request", "request_id": "perm-1", "request": {
        "subtype": "can_use_tool", "tool_name": tool, "input": tool_input, "tool_use_id": "toolu_1",
        "permission_suggestions": [], "decision_reason": "fake: asks every time"}})
    try:
        assert seen, "the request was never committed"
        for view in seen:
            assert view["asked"] == view["listed"] == view["held"], seen
            if view["asked"]:
                assert view["state"] == "approval-needed" and view["feed_pending"] == 1, seen
        assert seen[-1]["asked"] == ["perm-1"]
        # The tool call the turn made before asking is in the same commit, above the card.
        kinds = [e["kind"] for e in store.events_after(cid, 0)["events"]]
        assert kinds[-2:] == ["tool.started", "approval.requested"], kinds
        [approval] = store.approvals(conversation_id=cid)
        assert approval["kind"] == ("question" if tool == "AskUserQuestion" else "tool")
        assert json.loads(Path(approval["request_path"]).read_text())["input"] == tool_input
        assert store.mark("job/a1")["stdout_offset"] == runner.offset      # the watermark moved with them
    finally:
        store.close()


def test_a_replayed_request_keeps_its_approval_and_adds_nothing(tmp_path):
    """C-27.3: a runner a later daemon adopts re-derives the request from stdout; the
    approval it finds keeps its id and nonce, and the event is not written twice."""
    store, cid, runner, _ = asking(tmp_path)
    ask = {"type": "control_request", "request_id": "perm-1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}, "tool_use_id": "toolu_1"}}
    try:
        feed(runner, ask)
        [first] = store.approvals(conversation_id=cid)
        changes = store.changes_after(0)["next"]
        replay, _ = runner_for(store, cid, tmp_path)
        feed(replay, ask)
        [again] = store.approvals(conversation_id=cid)
        assert (again["approval_id"], again["nonce"]) == (first["approval_id"], first["nonce"])
        assert [e["kind"] for e in store.events_after(cid, 0)["events"]].count("approval.requested") == 1
        assert store.message(MID)["state"] == "approval-needed"
        assert store.changes_after(changes)["changes"] == []          # nothing new for a watcher
    finally:
        store.close()


def test_an_approval_already_recorded_commits_nothing(tmp_path):
    """C-27.1: `add_approval` for a request the store holds returns its approval and
    writes nothing, so no long poll is woken for it."""
    store = ConversationStore(tmp_path / "state")
    try:
        cid = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                        settings=SETTINGS, origin="new")[0]["conversation_id"]
        store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None, text="hi",
                             attachments=[], settings=SETTINGS)
        kw = dict(message_id=MID, conversation_id=cid, attempt_id="job/a1", provider_request_id="r1", kind="tool",
                  request={"input": {"command": "ls"}}, display={"tool": "Bash"}, options=("allow", "deny"))
        first, created = store.add_approval(**kw)
        commits = watch_commits(store, cid)
        again, created_again = store.add_approval(**kw)
        assert created and not created_again and again == first and commits == []
    finally:
        store.close()


def test_a_request_named_twice_in_one_call_is_one_approval(tmp_path):
    """C-27.1: one approval per attempt and provider request id, however often a call
    names it: one row, one request file, one change row; the second is not made."""
    store, cid, runner, _ = asking(tmp_path)
    try:
        start = store.changes_after(0)["next"]
        request = {"provider_request_id": "r1", "kind": "tool", "request": {"id": "r1"}, "display": {"tool": "Bash"},
                   "options": ("allow", "deny")}
        (first, made), (second, made_again) = store.add_approvals(
            message_id=MID, conversation_id=cid, attempt_id="job/a1", approvals=[request, dict(request)],
            events=[], expect=("running", "starting"))
        assert made and not made_again and first == second
        assert len(store.approvals(conversation_id=cid, state=None)) == 1
        assert len(list((tmp_path / "state" / "conversations" / cid / "approvals").iterdir())) == 1
        assert [(c["state"], c["pending_approvals"]) for c in store.changes_after(start)["changes"]] == [
            (None, 1), ("approval-needed", 1)]
    finally:
        store.close()


# --- the invariant over any interleaving the runner can be given ----------------------------

KINDS = ("tool", "question", "command", "file-change", "permissions")
ACTIONS = st.lists(st.one_of(
    st.tuples(st.just("text"), st.integers(1, 3)),
    st.tuples(st.just("ask"), st.lists(st.sampled_from(KINDS), min_size=1, max_size=3)),
    st.tuples(st.just("resolve"), st.integers(0, 7)),
    st.tuples(st.just("answer"), st.integers(0, 7)),
    st.tuples(st.just("tick"), st.just(0)),
), max_size=14)


@hypothesis.settings(max_examples=60, deadline=None)
@hypothesis.given(actions=ACTIONS)
def test_events_and_approvals_agree_at_every_commit(actions):
    """C-27.1, design §8, for every interleaving of streamed text, requests (one or
    several in a step, of every driver's kinds), withdrawals, a person's answers and
    batch flushes: after every commit the requests the event log announces are
    exactly the approvals the store holds, and while one is pending the message is
    `approval-needed`."""
    with tempfile.TemporaryDirectory() as tmp:
        store, cid, runner, clock = asking(Path(tmp))
        runner._send_outbox = lambda: runner.outbox.clear()     # nothing is sent
        seen = watch_commits(store, cid)
        asked: list[str] = []
        try:
            for n, (action, arg) in enumerate(actions):
                source = f"{n * 100}"
                if action == "text":
                    runner._apply(Step(events=[Event("text.delta", {"block": "b", "text": "x"}, f"{source}:{i}")
                                               for i in range(1, arg + 1)]))
                elif action == "ask":
                    rids = [f"req-{len(asked) + i}" for i in range(len(arg))]
                    asked += rids
                    runner._apply(Step(
                        events=[Event("approval.requested", {"request_id": rid, "kind": kind, "options": ["allow", "deny"]},
                                      f"{source}:{i + 1}") for i, (rid, kind) in enumerate(zip(rids, arg))],
                        approvals=[Approval(rid, kind, {"tool": "Bash"}, ("allow", "deny"), request={"id": rid})
                                   for rid, kind in zip(rids, arg)]))
                elif action == "resolve" and arg < len(asked):
                    rid = asked[arg]
                    runner._apply(Step(resolved=[rid], events=[Event("approval.resolved",
                                                                     {"request_id": rid, "decision": "withdrawn"},
                                                                     f"cmd:approval:{rid}")]))
                elif action == "answer" and arg < len(asked):
                    pending = {a["provider_request_id"]: a for a in store.approvals(conversation_id=cid)}
                    if asked[arg] in pending:
                        store.answer_approval(pending[asked[arg]]["approval_id"], {"decision": "allow"})
                elif action == "tick":
                    clock.now += FLUSH_S
                    if runner._flush_due():
                        runner._flush()
            runner._flush()
            for view in seen:
                assert view["asked"] == view["held"], (actions, seen)
                if view["listed"]:
                    assert view["state"] == "approval-needed", (actions, seen)
            assert seen[-1]["asked"] == sorted(asked) if seen else not asked
        finally:
            store.close()
