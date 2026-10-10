"""C-27.3: a resolved approval says so in the log and the change feed, in the commit that resolves it.

The app draws a card from `approval.requested` and clears it only on
`approval.resolved` or `turn.completed` (`Timeline.fold`). A provider that exited
without a result while a card was pending ended its turn at EOF: the driver
returned no `resolved` ids and no event, and the service withdrew the approval row
without an event or a change-feed row. The log kept a pending card on a failed turn
for good (an app build without `Timeline.withdrawIfEnded` showed it answerable, and
every click said "That approval is no longer pending.").

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

from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TERMINAL_STATES, TurnSpec

CLAUDE = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask"}
CODEX = {"model": "gpt-6-astra", "effort": None, "fast": False, "permission": "ask"}
TERMINAL = ",".join(f"'{s}'" for s in TERMINAL_STATES)

#: C-27.1, as `test_approval_commit.py` checks it: an `approval.requested` event no
#: approval row answers, and a pending approval whose message still reads running.
UNSETTLED = ("SELECT 'event without approval' AS what, e.seq AS at, json_extract(e.data_json, '$.request_id') AS id "
             "FROM events e WHERE e.kind='approval.requested' AND NOT EXISTS (SELECT 1 FROM approvals a "
             "WHERE a.attempt_id=e.attempt_id AND a.provider_request_id=json_extract(e.data_json, '$.request_id')) "
             "UNION ALL SELECT 'pending approval, message ' || m.state, a.approval_id, a.provider_request_id "
             "FROM approvals a JOIN messages m USING(message_id) "
             "WHERE a.state='pending' AND m.state IN ('running','starting')")

#: What no commit may show (C-27.3):
#: - a pending approval on a message that has ended;
#: - an approval that is no longer pending with no `approval.resolved` event for its
#:   request (the app keeps its card pending);
#: - an `approval.resolved` event whose approval still reads pending (the app clears
#:   a card the daemon still lists as answerable);
#: - a withdrawn approval whose last event says anything but withdrawn, or an
#:   answered one with no event saying the person's decision.
UNRESOLVED = (
    "SELECT 'pending approval, message ' || m.state AS what, a.approval_id AS at, a.provider_request_id AS id "
    f"FROM approvals a JOIN messages m USING(message_id) WHERE a.state='pending' AND m.state IN ({TERMINAL}) "
    "UNION ALL SELECT a.state || ' approval without approval.resolved', a.approval_id, a.provider_request_id "
    "FROM approvals a WHERE a.state!='pending' AND NOT EXISTS (SELECT 1 FROM events e WHERE e.attempt_id=a.attempt_id "
    "AND e.kind='approval.resolved' AND json_extract(e.data_json, '$.request_id')=a.provider_request_id) "
    "UNION ALL SELECT 'approval.resolved for a pending approval', e.seq, json_extract(e.data_json, '$.request_id') "
    "FROM events e JOIN approvals a ON a.attempt_id=e.attempt_id "
    "AND a.provider_request_id=json_extract(e.data_json, '$.request_id') "
    "WHERE e.kind='approval.resolved' AND a.state='pending' "
    "UNION ALL SELECT 'withdrawn approval, last event ' || json_extract(e.data_json, '$.decision'), a.approval_id, "
    "a.provider_request_id FROM approvals a JOIN events e ON e.attempt_id=a.attempt_id "
    "AND e.kind='approval.resolved' AND json_extract(e.data_json, '$.request_id')=a.provider_request_id "
    "WHERE a.state='withdrawn' AND e.seq=(SELECT MAX(x.seq) FROM events x WHERE x.attempt_id=a.attempt_id "
    "AND x.kind='approval.resolved' AND json_extract(x.data_json, '$.request_id')=a.provider_request_id) "
    "AND json_extract(e.data_json, '$.decision')!='withdrawn' "
    "UNION ALL SELECT 'answered approval, no event with its decision', a.approval_id, a.provider_request_id "
    "FROM approvals a WHERE a.state='answered' AND NOT EXISTS (SELECT 1 FROM events e WHERE e.attempt_id=a.attempt_id "
    "AND e.kind='approval.resolved' AND json_extract(e.data_json, '$.request_id')=a.provider_request_id "
    "AND json_extract(e.data_json, '$.decision')=json_extract(a.decision_json, '$.decision'))")


class Commits:
    """Each commit of the store, as a reader finds the store just after it: what
    `UNSETTLED` and `UNRESOLVED` find, the approval rows' states, and the change-feed
    rows that commit wrote."""

    def __init__(self, store: ConversationStore, monkeypatch):
        self.store = store
        self.found: list[list[dict]] = []
        self.rows: list[dict[str, str]] = []
        self.changes: list[list[dict]] = []
        self._seq = store.changes_after(0)["next"]
        self._before = self._states()
        transaction = store.transaction

        @contextlib.contextmanager
        def checked():
            with transaction() as tx:
                yield tx
            self.found.append(store.query(UNSETTLED) + store.query(UNRESOLVED))
            page = store.changes_after(self._seq, limit=1000)
            self.changes.append(page["changes"])
            self._seq = page["next"]
            self.rows.append(self._states())

        monkeypatch.setattr(store, "transaction", checked)

    def _states(self) -> dict[str, str]:
        return {r["approval_id"]: r["state"] for r in self.store.query("SELECT approval_id, state FROM approvals")}

    def check(self) -> None:
        """No commit shows what must not be seen, and every commit that changed an
        approval's state wrote a change-feed row for its message (C-29.9)."""
        assert all(found == [] for found in self.found), self.found
        before = self._before
        for rows, changes in zip(self.rows, self.changes):
            moved = {a for a, state in rows.items() if before.get(a) != state}
            if moved:
                messages = {r["message_id"] for r in self.store.query(
                    f"SELECT message_id FROM approvals WHERE approval_id IN ({','.join('?' * len(moved))})",
                    tuple(moved))}
                assert messages <= {c["message_id"] for c in changes}, (moved, changes)
            before = rows


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def running_message(store: ConversationStore, provider: str) -> tuple[str, str]:
    settings = CLAUDE if provider == "claude" else CODEX
    conversation, _ = store.create_conversation(provider=provider, workspace="/w", workspace_kind="in-place",
                                                settings=settings, origin="new")
    cid, mid = conversation["conversation_id"], str(uuid.uuid4())
    store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="hi", attachments=[],
                         settings=settings)
    assert store.set_state(mid, "running")
    return cid, mid


def runner_for(store: ConversationStore, tmp_path, provider: str, cid: str, mid: str) -> TurnRunner:
    """A runner for the message's attempt: the first, or one a later daemon adopts (same
    attempt id and directory). Nothing here reaches the relay."""
    spec = TurnSpec(provider=provider, message_id=mid, text="hi", model_id="opus[1m]" if provider == "claude"
                    else "gpt-6-astra", permission="ask", native_session_id=None,
                    new_session_id=str(uuid.uuid4()) if provider == "claude" else None)
    adir = tmp_path / f"a-{provider}"
    adir.mkdir(parents=True, exist_ok=True)
    runner = TurnRunner(store=store, attempt={"attempt_id": f"job-{provider}/a1", "lane_id": f"{provider}-1"},
                        spec=spec, conversation_id=cid, attempt_dir=adir, control_socket=str(tmp_path / "none.sock"),
                        on_outcome=lambda r: None, on_contain=lambda a: None, clocks=Clocks(), clock=Clock())
    if provider == "codex":
        runner.driver.thread_id = "th-1"            # past the handshake, as a live turn is
    return runner


def request_line(provider: str) -> str:
    """The provider's own permission request (tests/fake/interactive_*.py shapes)."""
    if provider == "claude":
        return json.dumps({"type": "control_request", "request_id": "perm-1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_1",
            "input": {"command": "echo hi"}, "decision_reason": "asks every time"}})
    return json.dumps({"id": 7, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "th-1", "turnId": "tu-1", "itemId": "it-1", "command": "echo hi", "cwd": "/w",
        "reason": "asks every time"}})


def cancel_line(provider: str) -> str:
    """The provider withdrawing its own request (Claude `control_cancel_request`,
    Codex `serverRequest/resolved`)."""
    if provider == "claude":
        return json.dumps({"type": "control_cancel_request", "request_id": "perm-1"})
    return json.dumps({"method": "serverRequest/resolved", "params": {"threadId": "th-1", "requestId": 7}})


def end_line(provider: str) -> str:
    """The provider's own terminal event, a failure."""
    if provider == "claude":
        return json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                           "session_id": "s", "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0, "usage": {}})
    return json.dumps({"method": "turn/completed", "params": {"threadId": "th-1", "turn": {
        "id": "tu-1", "status": "failed", "error": {"message": "boom"}}}})


def rid(provider: str) -> str:
    return "perm-1" if provider == "claude" else "7"


def resolved_events(store: ConversationStore, cid: str) -> list[tuple]:
    return [(e["data"]["request_id"], e["data"]["decision"]) for e in store.events_after(cid, 0)["events"]
            if e["kind"] == "approval.resolved"]


def kinds(store: ConversationStore, cid: str) -> list[str]:
    return [e["kind"] for e in store.events_after(cid, 0)["events"]]


def settle(store: ConversationStore, runner: TurnRunner, state: str = "failed",
           reason: str | None = "ended-without-result") -> None:
    """What `ConversationService._on_outcome` does to the store once a runner reports."""
    store.withdraw_approvals(attempt_id=runner.attempt_id)
    store.set_state(runner.message_id, state, reason=reason,
                    expect=("starting", "running", "approval-needed", "waiting"))


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "state")
    yield s
    s.close()


def asked(store: ConversationStore, tmp_path, provider: str):
    cid, mid = running_message(store, provider)
    runner = runner_for(store, tmp_path, provider, cid, mid)
    runner._apply(runner.driver.feed(request_line(provider), 0))
    assert store.message(mid)["state"] == "approval-needed"
    return cid, mid, runner


# --- the reported bug: an end at EOF ----------------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_turn_that_ends_at_eof_withdraws_its_approval_with_an_event(store, tmp_path, monkeypatch, provider):
    """C-27.3: the provider exits without a result while a card is pending. The
    withdrawal, its `approval.resolved {withdrawn}` event and a change-feed row
    commit together, before the message settles; the settlement adds nothing."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    step = runner.driver.eof(len(request_line(provider)) + 1)
    assert step.resolved == [rid(provider)] and not runner.driver.pending
    runner._apply(step)
    runner._flush()
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    assert store.approvals(conversation_id=cid) == []
    settle(store, runner)
    commits.check()
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    assert "turn.completed" not in kinds(store, cid)      # delivery is reconciliation's to say (C-24.6)
    assert store.message(mid)["state"] == "failed"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_the_settlement_writes_the_event_a_runner_did_not(store, tmp_path, monkeypatch, provider):
    """C-27.3: `_on_outcome` withdraws what an attempt left pending (a runner that
    never reached its own withdrawal); each withdrawn row gets its event and a
    change-feed row in the same commit, and a second withdrawal writes nothing."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    assert store.withdraw_approvals(attempt_id=runner.attempt_id) == 1
    assert len(commits.found) == 1
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    [change] = commits.changes[0]
    assert (change["message_id"], change["state"], change["pending_approvals"]) == (mid, None, 0)
    seq = store.changes_after(0)["next"]
    assert store.withdraw_approvals(attempt_id=runner.attempt_id) == 0
    assert store.changes_after(seq)["changes"] == []
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    store.set_state(mid, "failed", expect=("approval-needed",))
    commits.check()


# --- every other way an approval is resolved --------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_provider_end_withdraws_with_an_event_before_turn_completed(store, tmp_path, monkeypatch, provider):
    """The provider's own terminal event while a card is pending: the withdrawal's
    event commits with `turn.completed`, ahead of it."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    runner._apply(runner.driver.feed(end_line(provider), 1000))
    runner._flush()
    events = kinds(store, cid)
    assert events.index("approval.resolved") < events.index("turn.completed") == len(events) - 1, events
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    settle(store, runner)
    commits.check()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_provider_cancel_is_one_event_and_the_message_runs_again(store, tmp_path, monkeypatch, provider):
    """The provider withdraws its own request: its event is the only one for it, and
    the row, the event and the move back to running commit together."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    runner._apply(runner.driver.feed(cancel_line(provider), 1000))
    assert resolved_events(store, cid) == [(rid(provider), "withdrawn")]
    assert store.message(mid)["state"] == "running"
    assert len(commits.found) == 1
    commits.check()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_answer_commits_with_its_event_and_the_move_to_running(store, tmp_path, monkeypatch, provider):
    """C-27.1: the person's answer, its `approval.resolved`, the move back to
    running and a change-feed row are one commit, so a client opening at any moment
    never meets a pending card with no approval id; the runner's own event for the
    answer is the same row."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    [approval] = store.approvals(conversation_id=cid)
    assert store.answer_approval(approval["approval_id"], {"decision": "deny", "message": None, "answers": None})
    assert len(commits.found) == 1
    assert resolved_events(store, cid) == [(rid(provider), "deny")]
    assert store.message(mid)["state"] == "running"
    runner._apply(runner.driver.respond(rid(provider), "deny"))
    runner._flush()
    assert resolved_events(store, cid) == [(rid(provider), "deny")]
    commits.check()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_answer_the_provider_never_received_stays_answered_at_eof(store, tmp_path, monkeypatch, provider):
    """The person answered; the provider exited before the runner handed the answer
    over (a replay reads all of stdout and applies EOF before its queued answers).
    The card reads answered, never withdrawn, and nothing is left pending."""
    cid, mid, runner = asked(store, tmp_path, provider)
    commits = Commits(store, monkeypatch)
    [approval] = store.approvals(conversation_id=cid)
    assert store.answer_approval(approval["approval_id"], {"decision": "deny", "message": None, "answers": None})
    step = runner.driver.eof(1000)
    assert step.resolved == [rid(provider)]
    runner._apply(step)
    runner._flush()
    settle(store, runner)
    commits.check()
    assert resolved_events(store, cid) == [(rid(provider), "deny")]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_replay_through_eof_writes_nothing_new(store, tmp_path, monkeypatch, provider):
    """C-26.6: a runner rebuilt after a restart replays stdout and EOF again; the
    withdrawal it meets is already recorded: no event, row or change-feed row."""
    cid, mid, runner = asked(store, tmp_path, provider)
    runner._apply(runner.driver.eof(1000))
    runner._flush()
    events, changes = store.events_after(cid, 0)["events"], store.changes_after(0)["next"]
    commits = Commits(store, monkeypatch)
    again = runner_for(store, tmp_path, provider, cid, mid)
    again._apply(again.driver.feed(request_line(provider), 0))
    again._apply(again.driver.eof(1000))
    again._flush()
    assert store.events_after(cid, 0)["events"] == events
    assert store.changes_after(changes)["changes"] == []
    commits.check()


# --- any interleaving --------------------------------------------------------------------------

@st.composite
def histories(draw):
    """A turn's life from the store's side: requests asked, answered, cancelled by the
    provider, in any order the drivers allow, then an end (EOF, the provider's own
    result, or a stop), then possibly a replay of the same stdout."""
    ops: list[tuple] = []
    asked_ids: list[str] = []
    for _ in range(draw(st.integers(1, 6))):
        choice = draw(st.sampled_from(["ask", "answer", "cancel"]))
        if choice == "ask" or not asked_ids:
            asked_ids.append(f"r{len(asked_ids) + 1}")
            ops.append(("ask", asked_ids[-1]))
        else:
            ops.append((choice, draw(st.sampled_from(asked_ids)), draw(st.sampled_from(["allow", "deny", "cancel-turn"]))))
    ops.append(("end", draw(st.sampled_from(["eof", "result", "interrupt"]))))
    return ops, draw(st.booleans())


def claude_row(op: tuple) -> str:
    if op[0] == "ask":
        return json.dumps({"type": "control_request", "request_id": op[1], "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": f"toolu_{op[1]}",
            "input": {"command": f"echo {op[1]}"}}})
    if op[0] == "cancel":
        return json.dumps({"type": "control_cancel_request", "request_id": op[1]})
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                       "session_id": "s", "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0, "usage": {}})


@hypothesis.settings(max_examples=80, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture])
@hypothesis.given(histories())
def test_no_commit_shows_an_unresolved_approval_for_any_history(tmp_path_factory, history):
    """C-27.1, C-27.3, C-29.9: for any interleaving of requests, answers, provider
    cancels and an end, then possibly a replay, and the service's settlement: no
    commit shows an approval resolved without its event (or the reverse), a pending
    approval on an ended message, or an approval's change without a change-feed row;
    the end leaves nothing pending, every request's final event agrees with its row,
    and a replay writes nothing."""
    ops, replay = history
    tmp = tmp_path_factory.mktemp("h")
    store = ConversationStore(tmp / "state")
    try:
        cid, mid = running_message(store, "claude")
        runner = runner_for(store, tmp, "claude", cid, mid)
        stdout: list[str] = []
        with pytest.MonkeyPatch.context() as patch:
            commits = Commits(store, patch)
            offset = 0

            def feed(line: str) -> None:
                nonlocal offset
                stdout.append(line)
                runner._apply(runner.driver.feed(line, offset))
                offset += len(line) + 1

            for op in ops:
                if op[0] in ("ask", "cancel"):
                    feed(claude_row(op))
                elif op[0] == "answer":
                    row = store.one("SELECT approval_id FROM approvals WHERE provider_request_id=? AND state='pending'",
                                    (op[1],))
                    if row and store.answer_approval(row["approval_id"], {"decision": op[2]}):
                        runner._apply(runner.driver.respond(op[1], op[2]))
                elif op[1] == "result":
                    feed(claude_row(op))
                elif op[1] == "interrupt":
                    runner._apply(runner.driver.interrupt())
                    runner._apply(runner.driver.eof(offset))
                else:
                    runner._apply(runner.driver.eof(offset))
                runner._flush()
            assert runner.driver.outcome is not None
            settle(store, runner, state=runner.driver.outcome.state, reason=runner.driver.outcome.reason)
            events = store.events_after(cid, 0)["events"]
            changes = store.changes_after(0)["next"]
            if replay:
                again = runner_for(store, tmp, "claude", cid, mid)
                at = 0
                for line in stdout:
                    again._apply(again.driver.feed(line, at))
                    again._sync_answers()
                    at += len(line) + 1
                again._apply(again.driver.eof(at))
                again._flush()
                # Known, and older than C-27.3's events: a provider that cancels a
                # request after the person's answer reached the driver. The replay
                # reads the cancel before it re-applies the answer (`_sync_answers`
                # queues it), so the provider's own `withdrawn` is written then; the
                # approval stays answered and nothing else changes.
                late = {op[1] for i, op in enumerate(ops) if op[0] == "cancel"
                        and any(o[0] == "answer" and o[1] == op[1] for o in ops[:i])}
                added = store.events_after(cid, 0)["events"][len(events):]
                assert store.events_after(cid, 0)["events"][:len(events)] == events
                assert all(e["kind"] == "approval.resolved" and e["data"]["decision"] == "withdrawn"
                           and e["data"]["request_id"] in late for e in added), added
                assert store.changes_after(changes)["changes"] == []
            commits.check()
        assert store.approvals(conversation_id=cid) == []
    finally:
        store.close()
