"""C-27.1, C-25.4: an approval is readable as soon as its `approval.requested` event is.

The app draws a card from the event (`conversation.events`) and answers it by the
approval id only `approval.list` (or `conversation.open`) gives. The runner used to
commit the event batch first and the approval row after publishing its request,
two fsyncs later: a client that read `approval.list` on seeing the event missed the
approval (the live probe's "the question card lists its questions" failure), and a
person who clicked the card then was told it was no longer pending.

Every commit is checked, not only the end state: a reader sees exactly the states
between commits.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import hypothesis
from hypothesis import strategies as st
import pytest

from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TurnSpec

CLAUDE = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask"}
CODEX = {"model": "gpt-6-astra", "effort": None, "fast": False, "permission": "ask"}

#: What a reader could meet between commits that it must not: an
#: `approval.requested` event no approval row answers (read `conversation.events`,
#: then `approval.list`: no id to answer by), and a pending approval whose message
#: still reads running (design §8: one transaction).
UNSETTLED = ("SELECT 'event without approval' AS what, e.seq AS at, json_extract(e.data_json, '$.request_id') AS id "
             "FROM events e WHERE e.kind='approval.requested' AND NOT EXISTS (SELECT 1 FROM approvals a "
             "WHERE a.attempt_id=e.attempt_id AND a.provider_request_id=json_extract(e.data_json, '$.request_id')) "
             "UNION ALL SELECT 'pending approval, message ' || m.state, a.approval_id, a.provider_request_id "
             "FROM approvals a JOIN messages m USING(message_id) "
             "WHERE a.state='pending' AND m.state IN ('running','starting')")


def check_every_commit(store: ConversationStore, monkeypatch) -> list[list[dict]]:
    """What `UNSETTLED` finds after each of the store's commits, in order."""
    after_commit: list[list[dict]] = []
    transaction = store.transaction

    @contextlib.contextmanager
    def checked():
        with transaction() as tx:
            yield tx
        after_commit.append(store.query(UNSETTLED))

    monkeypatch.setattr(store, "transaction", checked)
    return after_commit


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "state")
    yield s
    s.close()


def running_message(store: ConversationStore, provider: str, state: str = "running") -> tuple[str, str]:
    conversation, _ = store.create_conversation(provider=provider, workspace="/w", workspace_kind="in-place",
                                                settings=CLAUDE if provider == "claude" else CODEX, origin="new")
    cid, mid = conversation["conversation_id"], str(uuid.uuid4())
    store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="hi", attachments=[],
                         settings=CLAUDE if provider == "claude" else CODEX)
    assert store.set_state(mid, state)
    return cid, mid


def runner_for(store: ConversationStore, tmp_path, provider: str, cid: str, mid: str) -> TurnRunner:
    spec = TurnSpec(provider=provider, message_id=mid, text="hi", model_id="opus[1m]" if provider == "claude"
                    else "gpt-6-astra", permission="ask", native_session_id=None,
                    new_session_id=str(uuid.uuid4()) if provider == "claude" else None)
    runner = TurnRunner(store=store, attempt={"attempt_id": f"job-{provider}/a1", "lane_id": f"{provider}-1"},
                        spec=spec, conversation_id=cid, attempt_dir=tmp_path / f"a-{provider}",
                        control_socket=str(tmp_path / "none.sock"), on_outcome=lambda r: None,
                        on_contain=lambda a: None, clock=Clock())
    if provider == "codex":
        runner.driver.thread_id = "th-1"            # past the handshake, as a live turn is
    return runner


def request_line(provider: str) -> str:
    """The provider's own permission request (tests/fake/interactive_*.py shapes)."""
    if provider == "claude":
        return json.dumps({"type": "control_request", "request_id": "perm-1", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "toolu_1",
            "decision_reason": "asks every time", "permission_suggestions": [],
            "input": {"questions": [{"question": "Which color?", "header": "Color", "multiSelect": False,
                                     "options": [{"label": "Blue"}, {"label": "Red"}]}]}}})
    return json.dumps({"id": 7, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "th-1", "turnId": "tu-1", "itemId": "it-1", "command": "echo hi", "cwd": "/w",
        "reason": "asks every time"}})


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_approval_commits_with_its_approval_requested_event(store, tmp_path, monkeypatch, provider):
    """C-27.1, design §8: the approval, its `approval.requested` event, its
    change-feed row and the message's move to approval-needed commit together;
    no commit shows one without the others."""
    cid, mid = running_message(store, provider)
    runner = runner_for(store, tmp_path, provider, cid, mid)
    start = store.changes_after(0)["next"]
    after_commit = check_every_commit(store, monkeypatch)
    line = request_line(provider)
    runner._apply(runner.driver.feed(line, 0))
    assert len(after_commit) == 1 and after_commit == [[]], after_commit
    requested = [e for e in store.events_after(cid, 0)["events"] if e["kind"] == "approval.requested"]
    approvals = store.approvals(conversation_id=cid)
    assert len(requested) == 1 and len(approvals) == 1
    assert approvals[0]["provider_request_id"] == str(requested[0]["data"]["request_id"])
    assert approvals[0]["display"] == {k: v for k, v in requested[0]["data"].items()
                                       if k not in ("request_id", "kind", "options")}
    assert store.message(mid)["state"] == "approval-needed"
    changes = store.changes_after(start)["changes"]
    assert [(c["state"], c["pending_approvals"]) for c in changes] == [(None, 1), ("approval-needed", 1)], changes


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_replayed_request_adds_no_second_approval(store, tmp_path, monkeypatch, provider):
    """C-26.6, C-27.3: a runner rebuilt after a restart replays stdout from the
    start; the request it meets again is the approval already recorded: no
    second row, request file or change-feed row, and nothing unsettled on the way."""
    cid, mid = running_message(store, provider)
    line = request_line(provider)
    first = runner_for(store, tmp_path, provider, cid, mid)
    first._apply(first.driver.feed(line, 0))
    recorded = store.approvals(conversation_id=cid)
    changes = store.changes_after(0)["next"]
    after_commit = check_every_commit(store, monkeypatch)
    again = runner_for(store, tmp_path, provider, cid, mid)
    again._apply(again.driver.feed(line, 0))
    assert all(found == [] for found in after_commit), after_commit
    assert store.approvals(conversation_id=cid) == recorded
    assert len(list((store.dir / cid / "approvals").iterdir())) == 1
    assert [c for c in store.changes_after(changes)["changes"] if c["state"] is None] == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_replayed_request_a_person_answered_leaves_the_message_running(store, tmp_path, monkeypatch, provider):
    """C-27.3: the request a rebuilt runner meets again was answered before the
    restart; no commit moves the message back to approval-needed with nothing
    pending (the answer is re-applied from the store, `_sync_answers`)."""
    cid, mid = running_message(store, provider)
    line = request_line(provider)
    first = runner_for(store, tmp_path, provider, cid, mid)
    first._apply(first.driver.feed(line, 0))
    [approval] = store.approvals(conversation_id=cid)
    assert store.answer_approval(approval["approval_id"], {"decision": "allow"})
    assert store.set_state(mid, "running", expect=("approval-needed",))
    states: list[str] = []
    after_commit = check_every_commit(store, monkeypatch)
    transaction = store.transaction

    @contextlib.contextmanager
    def watched():
        with transaction() as tx:
            yield tx
        states.append(store.message(mid)["state"])

    monkeypatch.setattr(store, "transaction", watched)
    again = runner_for(store, tmp_path, provider, cid, mid)
    again._apply(again.driver.feed(line, 0))
    assert states and set(states) == {"running"}, states
    assert all(found == [] for found in after_commit), after_commit
    assert store.approvals(conversation_id=cid) == []


# --- the store, over any sequence of batches -----------------------------------------------

REQUESTS = ["r1", "r2", "r3", "r4"]


@st.composite
def batches(draw):
    """Batches as a runner writes them, `(batch, texts, asked)`: text events, then
    the batch's provider requests with their events; a batch may be one written
    before (a replay: the same stdout positions)."""
    written: list[tuple] = []
    for _ in range(draw(st.integers(1, 6))):
        if written and draw(st.booleans()):
            written.append(draw(st.sampled_from(written)))
            continue
        asked = draw(st.lists(st.sampled_from(REQUESTS), max_size=2, unique=True))
        written.append((len(written), draw(st.integers(0, 3)), tuple(asked)))
    return written


@hypothesis.settings(max_examples=60, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture])
@hypothesis.given(batches(), st.sampled_from(["starting", "running"]))
def test_no_commit_shows_an_approval_requested_event_without_its_approval(tmp_path_factory, batches, start):
    """C-27.1, C-26.6: for any sequence of batches and replays, from a starting or
    running message, no commit shows an `approval.requested` event without its
    approval or a pending approval whose message reads starting or running; each
    request ends with one row and one published request file, and a batch writes
    one change-feed row when it brings a new request, none for a replay."""
    store = ConversationStore(tmp_path_factory.mktemp("state"))
    try:
        cid, mid = running_message(store, "claude", start)
        with pytest.MonkeyPatch.context() as patch:
            after_commit = check_every_commit(store, patch)
            write(store, cid, mid, batches, after_commit)
        assert all(found == [] for found in after_commit), after_commit
        asked_ever = {rid for _, _, asked in batches for rid in asked}
        rows = store.approvals(conversation_id=cid)
        assert sorted(a["provider_request_id"] for a in rows) == sorted(asked_ever)
        files = list((store.dir / cid / "approvals").iterdir()) if asked_ever else []
        assert sorted(p.name for p in files) == sorted(f"{a['approval_id']}.json" for a in rows)
        # One change-feed row for each batch that brought a request not seen before.
        seen: set[str] = set()
        bringing = 0
        for _, _, asked in batches:
            bringing += bool(set(asked) - seen)
            seen |= set(asked)
        assert len([c for c in store.changes_after(0)["changes"] if c["message_id"] == mid and c["state"] is None]) == bringing
    finally:
        store.close()


def write(store: ConversationStore, cid: str, mid: str, batches: list[tuple], after_commit: list[list[dict]]) -> None:
    """Each batch as `TurnRunner._flush` writes it; every commit it makes is checked."""
    for batch, texts, asked in batches:
        before = len(after_commit)
        events = [("stdout", f"{batch}:{n}", 0, "text", {"text": f"t{n}"}) for n in range(texts)]
        events += [("stdout", f"{batch}:ask:{rid}", 0, "approval.requested",
                    {"request_id": rid, "tool": "Bash", "input": rid, "kind": "tool", "options": ["allow"]})
                   for rid in asked]
        store.append_events(conversation_id=cid, message_id=mid, attempt_id="j/a1", events=events,
                            stdout_offset=batch, stdin_seq=0,
                            approvals=[{"provider_request_id": rid, "kind": "tool", "request": {"id": rid},
                                        "display": {"tool": "Bash", "input": rid}, "options": ("allow",)}
                                       for rid in asked])
        made = after_commit[before:]
        assert made and all(found == [] for found in made), (batch, made)
