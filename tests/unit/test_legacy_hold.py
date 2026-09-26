"""One writer per native session: the legacy hold keeps every turn off a
conversation (C-30.4, C-24.5), and nothing else writes in a conversation's
session beside it (C-26.3, design D-17).

Each case runs a real `--legacy-cockpit` pass, as an operator does with the
daemon stopped, over a synthetic v1 state (`tests/legacy_fixtures.py`), and then
drives what the daemon does when it starts again: the conversation service over
the store the pass left, with a job store standing in for the daemon's, and in
one case the daemon's real admission pass. Nothing here reads `~/.claude`, the
v1 state or `~/.subfleet`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, importer, protocol
from subfleet.adapters.base import AdapterError
from subfleet.conversations import runner as runner_mod, service as service_mod
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError, ConversationStore
from subfleet.conversations.turn import DELIVERY_UNKNOWN, RUNNING, STARTING, WAITING
from subfleet.contracts import Credential, Decision, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store
from tests.legacy_fixtures import outbox_row, write_outbox, write_transcript

SESSION = "5e551011-0000-4000-8000-0000000000c1"
HISTORY = "1e9ac700-0000-4000-8000-0000000000c1"
COCKPIT = "1e9ac700-0000-4000-8000-0000000000c2"


class World:
    """A v1 state with one finished cockpit message in SESSION, and a state root."""

    def __init__(self, tmp_path: Path, *, mode: str = "default"):
        self.v1 = tmp_path / "v1-state"
        self.claude = tmp_path / "claude"
        self.root = tmp_path / "state"
        self.workspace = tmp_path / "workspace"
        for directory in (self.v1, self.claude, self.workspace):
            directory.mkdir(parents=True)
        write_transcript(self.claude, SESSION, self.workspace, mode=mode)
        self.cockpit("finished")

    def cockpit(self, status: str | None) -> None:
        """The outbox: the finished history, and the cockpit's next message in the session."""
        rows = [outbox_row(HISTORY, SESSION, "finished", "cockpit history", at=600)]
        if status:
            rows.append(outbox_row(COCKPIT, SESSION, status, "the cockpit again", at=60))
        write_outbox(self.v1, rows)

    def run_pass(self):
        return importer.import_legacy_cockpit(self.root, v1_state=self.v1, claude_projects=self.claude / "projects",
                                              write_report=False)

    def conversation_id(self) -> str:
        store = ConversationStore(self.root)
        try:
            (row,) = store.query("SELECT conversation_id FROM conversations")
            return row["conversation_id"]
        finally:
            store.close()


class FakeJobStore:
    """The daemon's job store as the conversation service reads it."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.attempts: list[dict] = []
        self.quarantined = False

    def one(self, sql, params=()):
        if sql.startswith("SELECT * FROM jobs WHERE request_id=?"):
            return next((job for job in self.jobs.values() if job["request_id"] == params[0]), None)
        if sql.startswith("SELECT * FROM jobs WHERE job_id=?"):
            return self.jobs.get(params[0])
        if "a.state='quarantined'" in sql:
            return {"1": 1} if self.quarantined else None
        if sql.startswith("SELECT count(DISTINCT j.job_id) AS n FROM jobs j JOIN attempts a"):
            # `_provider_tries`: this message's turn jobs a provider reached (C-24.6).
            reached = {attempt["job_id"] for attempt in self.attempts}
            return {"n": sum(1 for job in self.jobs.values()
                             if params[0] <= job["request_id"] < params[1] and job["job_id"] in reached)}
        return None                                           # no lease is held

    def query(self, sql, params=()):
        if sql.startswith("SELECT job_id, state FROM jobs WHERE kind='turn' AND name=?"):
            return [{"job_id": job["job_id"], "state": job["state"]} for job in self.jobs.values()
                    if job["name"] == params[0] and job["job_id"] != params[1]]
        if sql.startswith("SELECT a.*, j.kind"):             # the adoption query (and its job_sandbox)
            return [dict(attempt) for attempt in self.attempts]
        return []

    def get_lane(self, lane_id):
        return None

    def lane_rows(self):
        return []


class FakeDaemon:
    def __init__(self, root: Path):
        self.root = root
        self.log = logging.getLogger("subfleet.test.legacy-hold")
        self.store = FakeJobStore()
        self.policy = load_policy(DEFAULT_POLICY_PATH)
        self.submitted: list[str] = []

    def _notify(self):
        pass

    def submit(self, args, *, turn=None):
        job_id = f"job-{len(self.submitted) + 1}"
        self.submitted.append(args.request_id)
        self.store.jobs[job_id] = {"job_id": job_id, "request_id": args.request_id, "name": args.name,
                                   "state": "queued"}
        return {"job_id": job_id}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def service(world: World) -> tuple[ConversationService, FakeDaemon]:
    daemon = FakeDaemon(world.root)
    return ConversationService(daemon), daemon


def submit(svc: ConversationService, cid: str, after: str | None = None) -> str:
    message_id = str(uuid.uuid4())
    conversation = svc.store.conversation(cid)
    svc.store.submit_message(conversation_id=cid, message_id=message_id, after_message_id=after,
                             text="a person's message", attachments=[], settings=conversation["settings"])
    return message_id


def outcome(svc: ConversationService, tmp: Path, message_id: str, cid: str, **turn) -> None:
    """Settle a message from a turn record, as the runner's outcome does."""
    adir = tmp / f"attempt-{uuid.uuid4().hex[:8]}"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps(turn))
    runner = types.SimpleNamespace(adir=adir, message_id=message_id, conversation_id=cid,
                                   attempt={"lane_id": None}, attempt_id="job-1/a1", offset=0, next_seq=1)
    # What reconciliation (C-24.6, D-14) finds after such a turn: the process has
    # exited, the message frame was logged only if the turn wrote it, and the
    # session's transcript lacks the message (as test_conversation_service stubs it).
    evidence = service_mod.reconcile.Evidence(
        acknowledged=bool(turn.get("accepted")), frame="written" if turn.get("user_frame_written") else "absent",
        process_gone=True, native="absent", session_exists=True)
    real = service_mod.reconcile.gather
    service_mod.reconcile.gather = lambda *a, **k: evidence
    try:
        svc._on_outcome(runner)
    finally:
        service_mod.reconcile.gather = real


def dispatchable(svc: ConversationService) -> list[str]:
    return [message["message_id"] for message in svc.store.next_dispatchable()]


def test_a_readmitted_turn_waits_while_the_conversation_is_held(world, tmp_path):
    """C-24.5, C-30.4 (review H1): a message waiting to be re-admitted after a
    turn that never reached the provider gets no new turn job while a pass holds
    its conversation, and gets one once a pass lifts the hold."""
    world.run_pass()
    cid = world.conversation_id()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc._dispatch()
        assert daemon.submitted == [f"turn:{first}:0"]
        svc.store.set_state(first, RUNNING, expect=(WAITING,))
        outcome(svc, tmp_path, first, cid, state=None, reason="provider-init-failed", user_frame_written=False)
        message = svc.store.message(first)
        assert (message["state"], message["state_reason"], message["job_id"]) == (
            WAITING, "readmit:provider-init-failed", None)
    finally:
        svc.close()

    world.cockpit("queued")                   # the daemon is stopped; the cockpit takes the session up again
    world.run_pass()
    svc, daemon = service(world)
    try:
        svc._dispatch()
        assert daemon.submitted == []
        # Neither submitted nor rewritten while held (the dispatcher leaves it as it is).
        message = svc.store.message(first)
        assert (message["state"], message["state_reason"], message["job_id"]) == (
            WAITING, "readmit:provider-init-failed", None)
    finally:
        svc.close()

    world.cockpit("finished")                 # it settles; the next pass lifts the hold
    world.run_pass()
    svc, daemon = service(world)
    try:
        svc._dispatch()
        assert daemon.submitted == [f"turn:{first}:1"]
    finally:
        svc.close()


def test_an_outcome_never_replaces_the_hold_and_unblock_never_lifts_it(world, tmp_path):
    """C-24.8, C-30.4 (review H1): a turn that was running when the pass held its
    conversation ends unfinished. Its block is recorded beside the hold, and the
    person's `continue` lifts that block only: while the cockpit may be using the
    session, the next message still waits."""
    world.run_pass()
    cid = world.conversation_id()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc._dispatch()
        second = submit(svc, cid, after=first)
    finally:
        svc.close()
    world.cockpit("dispatched")
    world.run_pass()
    svc, daemon = service(world)
    svc._person = lambda peer, what: types.SimpleNamespace(person=True, pid=1, reason="test")
    try:
        svc.store.set_state(first, RUNNING, expect=(WAITING,))
        # Delivered (acknowledged: C-24.6), then no `result` (C-24.8).
        outcome(svc, tmp_path, first, cid, state=None, reason="ended-without-result", user_frame_written=True,
                accepted=True)
        assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"
        svc.op_conversation_unblock({"conversation_id": cid, "choice": "continue", "confirm": True}, None)
        assert svc.store.conversation(cid)["blocked_by"] is None
        assert dispatchable(svc) == []
        svc._dispatch()
        assert daemon.submitted == []
        assert svc.store.message(second)["state"] == "queued"
        # The app sees the hold as the block (it shows a conversation with
        # `blocked_by` as needing a decision), and its reason in `legacy_hold`.
        view = svc._view(svc.store.conversation(cid))
        assert (view["blocked_by"], view["legacy_hold"]) == ("legacy-owner", f"message {COCKPIT} is dispatched")
    finally:
        svc.close()


def test_a_resolved_delivery_and_a_quarantine_leave_the_hold(world, tmp_path):
    """C-24.8, D-14, C-30.4 (review H1): `delivery-unknown` set by an outcome and
    cleared by `message.resolve`, and `quarantined-turn` set by the dispatcher,
    are the service's own block; neither replaces the import's hold."""
    world.run_pass()
    cid = world.conversation_id()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc._dispatch()
    finally:
        svc.close()
    world.cockpit("starting")
    world.run_pass()
    svc, daemon = service(world)
    svc._person = lambda peer, what: types.SimpleNamespace(person=True, pid=1, reason="test")
    try:
        svc.store.set_state(first, RUNNING, expect=(WAITING,))
        # Its frame was written, nothing acknowledged it and no terminal event
        # came: whether the provider has it is unknown (C-24.6, D-14).
        outcome(svc, tmp_path, first, cid, state=None, reason="ended-without-result", user_frame_written=True)
        assert svc.store.message(first)["state"] == DELIVERY_UNKNOWN
        assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
        second = submit(svc, cid, after=first)
        # Not delivered: the resolve clears the service's block (a Claude message
        # resolved as delivered would turn it into `unfinished-turn`, C-24.8).
        svc.op_message_resolve({"message_id": first, "resolution": "not-delivered", "confirm": True}, None)
        assert svc.store.conversation(cid)["blocked_by"] is None
        assert dispatchable(svc) == []                   # still held: the dispatcher never gets to look
        assert svc.store.conversation(cid)["legacy_hold"] == f"message {COCKPIT} is starting"
        daemon.store.jobs["job-1"] = {"job_id": "job-1", "request_id": f"turn:{first}:0", "name": f"turn-{cid}",
                                      "state": "failed"}           # its turn job ended quarantined
        daemon.store.quarantined = True
        assert not svc._previous_released(svc.store.conversation(cid), svc.store.message(second))
        conversation = svc.store.conversation(cid)
        assert conversation["blocked_by"] == "quarantined-turn" and conversation["legacy_hold"]
    finally:
        svc.close()


def test_only_the_import_lifts_its_hold(world):
    """C-30.4, C-25.6 (review H1): `conversation.unblock` of a conversation only
    the import holds is refused with the hold's reason and a fix; the
    service's own update path cannot name the column."""
    world.run_pass()
    cid = world.conversation_id()
    world.cockpit("queued")
    world.run_pass()
    svc, _ = service(world)
    svc._person = lambda peer, what: types.SimpleNamespace(person=True, pid=1, reason="test")
    try:
        with pytest.raises(ConversationError) as refused:
            svc.op_conversation_unblock({"conversation_id": cid, "choice": "leave", "confirm": True}, None)
        assert refused.value.reason == "legacy-owner" and refused.value.code == 7
        assert f"message {COCKPIT} is queued" in str(refused.value) and "--legacy-cockpit" in refused.value.fix
        with pytest.raises(ValueError):
            svc.store.update_conversation(cid, legacy_hold=None)
        assert svc.store.conversation(cid)["legacy_hold"] == f"message {COCKPIT} is queued"
        assert not svc.store.query("SELECT * FROM messages WHERE origin='unblock-note'")
    finally:
        svc.close()


class RecordingRunner:
    """A `TurnRunner` that records what the service asks of it."""

    made: list["RecordingRunner"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.interrupts: list[str] = []
        self.withheld: list[str] = []
        self.started = False
        self.message_id = kwargs["spec"].message_id
        self.finished = threading.Event()
        RecordingRunner.made.append(self)

    def start(self):
        self.started = True

    def interrupt(self, reason="stopped"):
        self.interrupts.append(reason)

    def withhold(self, reason):
        assert not self.started, "withheld after the runner started"
        self.withheld.append(reason)

    def stop(self):
        pass

    def join(self, timeout):
        return True                   # no thread: close() has nothing to wait for (C-25.3)


def running_attempt(world: World, daemon: FakeDaemon, cid: str, message_id: str) -> str:
    """A turn attempt the guardian kept running across a daemon restart."""
    job_id = "turn-cv-held-20260925"
    adir = world.root / "jobs" / job_id / "a1"
    adir.mkdir(parents=True)
    (adir / "start.json").write_text(json.dumps({"control_socket": str(world.root / "relay.sock")}))
    turn = {"conversation_id": cid, "message_id": message_id, "provider": "claude", "text": "x",
            "settings": {"model": "opus", "effort": None, "fast": False, "permission": "ask",
                         "auto_continue": True},
            "native_session_id": SESSION, "new_session_id": None, "images": [], "cwd": str(world.workspace),
            "allow_main": False, "affinity_lane": None, "digest": "d"}
    (world.root / "jobs" / job_id / "manifest.json").write_text(json.dumps({"turn": turn}))
    daemon.store.attempts = [{"attempt_id": f"{job_id}/a1", "job_id": job_id, "seq": 1, "lane_id": "claude-1",
                              "state": "running", "kind": "turn", "job_sandbox": "workspace-write"}]
    return f"{job_id}/a1"


@pytest.mark.parametrize("held", [True, False])
def test_a_running_turn_is_stopped_when_the_daemon_adopts_it_on_a_held_conversation(world, monkeypatch, held):
    """C-30.4, D-13, D-17 (review H1): a pass run while the daemon was down that
    finds the cockpit using the session again stops the turn that kept running
    across the restart, through the D-13 escalation, as soon as the daemon adopts
    it; an unheld conversation's turn is adopted and left to run."""
    RecordingRunner.made = []
    monkeypatch.setattr(service_mod, "TurnRunner", RecordingRunner)
    world.run_pass()
    cid = world.conversation_id()
    if held:
        world.cockpit("delivered-live")
        world.run_pass()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
        aid = running_attempt(world, daemon, cid, first)
        svc._adopt_runners()
        (runner,) = RecordingRunner.made
        assert runner.started and aid in svc.runners
        assert svc.store.message(first)["state"] == STARTING
        assert runner.withheld == (["legacy-owner"] if held else []) and runner.interrupts == []
    finally:
        svc.close()


class RecordingRelay:
    """The guardian relay as the runner sees it: every frame accepted and recorded."""

    frames: list[tuple[int, str, str]] = []

    def __init__(self, path, timeout_s=30):
        pass

    def send(self, seq, op, line=None, tag=None, sig=None):
        from subfleet.relay import Ack
        RecordingRelay.frames.append((seq, op, tag))
        return Ack(seq=seq, ok=True)

    def status(self):
        return None                   # a relay before version 2: its log is read as before (IR-27)

    def close(self):
        pass


def _init_answer() -> dict:
    """Claude's answer to `initialize`, as the driver reads it."""
    from subfleet.conversations.claude_turn import INIT_REQUEST_ID
    return {"type": "control_response", "response": {"subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
        "account": {"email": "max@example.org"}, "fast_mode_state": "off",
        "models": [{"value": "opus", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high"]}]}}}


@pytest.mark.parametrize("handed_over", [False, True])
def test_an_adopted_turn_on_a_held_conversation_never_writes_its_message(world, monkeypatch, handed_over):
    """C-30.4, D-13 (reviews H1 and its follow-up): the daemon stopped while a
    turn was starting; the provider answered `initialize` while no daemon ran,
    and a pass held the conversation. The real runner, adopting it, never
    writes the message: the turn is stopped before any stdout is replayed and
    the provider's stdin is closed. A message the relay log shows handed over
    is stopped through D-13 instead, and never written twice."""
    RecordingRelay.frames = []
    monkeypatch.setattr(runner_mod, "RelayClient", RecordingRelay)
    world.run_pass()
    cid = world.conversation_id()
    world.cockpit("dispatched")
    world.run_pass()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
        aid = running_attempt(world, daemon, cid, first)
        adir = world.root / "jobs" / "turn-cv-held-20260925" / "a1"
        log = [{"kind": "intent", "seq": 1, "tag": "init", "op": "write"}, {"kind": "written", "seq": 1}]
        if handed_over:
            log += [{"kind": "intent", "seq": 2, "tag": "user-message", "op": "write"}, {"kind": "written", "seq": 2}]
        (adir / "stdin.jsonl").write_text("".join(json.dumps(row) + "\n" for row in log))
        (adir / "stdout").write_text(json.dumps(_init_answer()) + "\n")
        svc._adopt_runners()
        runner = svc.runners[aid]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (runner.driver.outcome or RecordingRelay.frames):
            time.sleep(0.02)
        time.sleep(0.2)                                   # a frame sent late would show here
        runner.stop()
        assert runner.finished.wait(5)
        tags = [tag for _, _, tag in RecordingRelay.frames]
        assert "user-message" not in tags
        if handed_over:
            assert tags == ["interrupt"] and runner.driver.outcome is None
        else:
            assert tags == ["close"] and runner.driver.outcome.reason == "stopped-before-send"   # stdin closed
            assert json.loads((adir / "turn.json").read_text())["stop_reason"] == "legacy-owner"
    finally:
        svc.close()


def test_a_person_s_stop_stands_over_the_hold(world, tmp_path):
    """C-24.7, C-30.4 (review follow-up F): a turn a person had asked to stop,
    stopped before its message by the hold, settles as the person's stop; it is
    not re-admitted when the hold lifts."""
    world.run_pass()
    cid = world.conversation_id()
    world.cockpit("dispatched")
    world.run_pass()
    svc, _ = service(world)
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, STARTING, expect=("queued",), job_id="job-0")
        svc.store.update_message(first, stop_requested_at="2026-09-25T20:00:00.000Z")
        outcome(svc, tmp_path, first, cid, state="interrupted", reason="stopped-before-send",
                stop_reason="legacy-owner", user_frame_written=False)
        message = svc.store.message(first)
        assert (message["state"], message["state_reason"]) == ("failed", "not-delivered: stopped-before-send")
    finally:
        svc.close()


def test_a_turn_the_hold_stopped_before_its_message_waits_for_the_hold(world, tmp_path):
    """C-30.4, IR-1 (review H1): a turn stopped by the hold before its message
    was written never reached the provider. The message is not failed: it waits
    to be re-admitted, and nothing is admitted until a pass lifts the hold."""
    world.run_pass()
    cid = world.conversation_id()
    world.cockpit("dispatched")
    world.run_pass()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, STARTING, expect=("queued",), job_id="job-0")
        outcome(svc, tmp_path, first, cid, state="interrupted", reason="stopped-before-send",
                stop_reason="legacy-owner", user_frame_written=False)
        message = svc.store.message(first)
        assert (message["state"], message["state_reason"], message["turn_seq"], message["job_id"]) == (
            WAITING, "readmit:legacy-owner", 1, None)
        svc._dispatch()
        assert daemon.submitted == []
    finally:
        svc.close()
    world.cockpit("finished")
    world.run_pass()
    svc, daemon = service(world)
    try:
        svc._dispatch()
        assert daemon.submitted == [f"turn:{first}:1"]
    finally:
        svc.close()


# --- admission: a turn job already queued when the pass ran ----------------------


@pytest.fixture
def core(tmp_path, monkeypatch):
    """The daemon's real submit and admission transaction, no processes (as test_gate_admission)."""
    world = World(tmp_path, mode="plan")                 # a read-only conversation: no git needed
    root = world.root
    (root / "jobs").mkdir(parents=True)
    daemon = object.__new__(Daemon)
    daemon.root, daemon.store = root, Store(root / "state.sqlite3")
    daemon.policy, daemon.policy_digest = load_policy(DEFAULT_POLICY_PATH), "test"
    daemon._submit_lock = threading.Lock()
    daemon._busy_lock, daemon._busy = threading.Lock(), set()
    daemon._pending_launches = set()
    daemon._export_locks = {}
    daemon._exit_settle = {}
    daemon._workspace_deferrals = {}
    daemon._reset_admission_state()
    daemon.log = logging.getLogger("subfleet.test.legacy-hold")
    daemon._desktop_cache = (0.0, daemon_module._UNSET)
    daemon._notify = lambda: None
    daemon._boundary = lambda *args: None
    daemon._publish = lambda role, path, contents: atomic_publish(path, contents)
    daemon._recover_probes = lambda: None
    daemon._prepare_route = lambda *args: (set(), None)
    chosen = Decision(("opus",), (), "claude-1", "opus", "test", "test")
    daemon._pick = lambda *args, **kwargs: chosen
    daemon._pin_roster = daemon.store.lane_rows
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", lambda *args: False)
    daemon.store.put_lane(Lane("claude-1", "claude", "claude:test", Credential("claude", "token", "keychain-token"),
                               None, LaneOwner.V2, False))
    world.run_pass()
    daemon.conversations = ConversationService(daemon)
    yield world, daemon
    daemon.conversations.close()
    daemon.store.close()


def test_a_queued_turn_job_of_a_held_conversation_is_not_admitted(core):
    """C-24.5, C-6.11, C-30.4 (review H1): a turn job created before a pass held
    its conversation reserves no attempt while the hold lasts; admission reports
    why, holds back no other job, and places it once a pass lifts the hold."""
    world, daemon = core
    svc = daemon.conversations
    cid = world.conversation_id()
    first = submit(svc, cid)
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    assert job and svc.store.message(first)["job_id"] == job["job_id"]
    svc.close()                                          # the daemon stops; a pass holds the conversation

    world.cockpit("queued")
    world.run_pass()
    daemon.conversations = svc = ConversationService(daemon)
    daemon._admit()
    assert daemon.store.list_attempts(job["job_id"]) == []
    hold = daemon._holds[job["job_id"]]
    assert hold["reason"] == "conversation-blocked" and hold["conversation_id"] == cid
    assert hold["legacy_hold"] == f"message {COCKPIT} is queued" and hold["blocked_by"] is None
    assert daemon._admission["pending"] == 0             # a person's to end, not admission's
    svc.close()

    world.cockpit("finished")
    world.run_pass()
    daemon.conversations = ConversationService(daemon)
    daemon._admit()
    assert len(daemon.store.list_attempts(job["job_id"])) == 1


# --- the store ------------------------------------------------------------------


def test_a_store_written_before_the_hold_had_its_column_moves_it_there(tmp_path):
    """C-30.4, C-24.1: a schema 1 store in which an earlier build kept the hold in
    `blocked_by` gains `legacy_hold` on open, the hold moves there, and every
    other block stays where it was."""
    root = tmp_path / "state"
    root.mkdir()
    store = ConversationStore(root)
    try:
        for n, blocked in enumerate(("legacy-owner", "unfinished-turn", None)):
            store.create_conversation(provider="claude", workspace=str(tmp_path), workspace_kind="in-place",
                                      settings={"model": "opus", "permission": "ask"}, origin="legacy",
                                      native_session_id=f"5e551011-0000-4000-8000-00000000000{n}")
        store.query("UPDATE conversations SET blocked_by=CASE native_session_id "
                    "WHEN '5e551011-0000-4000-8000-000000000000' THEN 'legacy-owner' "
                    "WHEN '5e551011-0000-4000-8000-000000000001' THEN 'unfinished-turn' END")
    finally:
        store.close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute("ALTER TABLE conversations DROP COLUMN legacy_hold")      # as that build wrote it
    db.commit()
    db.close()

    store = ConversationStore(root)
    try:
        found = [(row["native_session_id"][-1], row["blocked_by"], row["legacy_hold"]) for row in store.query(
            "SELECT native_session_id, blocked_by, legacy_hold FROM conversations ORDER BY native_session_id")]
    finally:
        store.close()
    assert found == [("0", None, "held legacy-owner by an earlier import"), ("1", "unfinished-turn", None),
                     ("2", None, None)]


# --- one writer per native session (C-26.3, design D-17; review M3, L1) --------


def resume_daemon(world: World, svc: ConversationService, kind: str, attempt: dict):
    """What `Daemon._resume_submission` reads, for a terminal source job of `kind`."""
    source = {"job_id": "src", "kind": kind, "state": "succeeded", "isolated_review": 0,
              "accepted_attempt_id": "src/a1", "worktree": None, "workdir": str(world.workspace),
              "sandbox": "read-only", "task": None, "tier": None, "allow_desktop": 0, "exclusions": "[]"}
    return types.SimpleNamespace(
        _job=lambda job_id: source, root=world.root, conversations=svc,
        store=types.SimpleNamespace(one=lambda sql, params=(): None, get_attempt=lambda aid: attempt),
        _legacy_resume_identity=lambda attempt: None, _read_json=Daemon._read_json,
        _source_launch_prefix=lambda source, attempt, manifest: None)


def test_resume_refuses_a_turn_source_and_a_conversation_bound_session(world):
    """C-26.3, C-26.13, D-17 (review M3): `resume` of a turn job would run `claude
    --resume` on its conversation's session beside the conversation; it is
    refused, as is resuming any job whose session a conversation is bound to, in
    any spelling. On the desktop line the second is `submit`'s fence, checked
    after C-6.2's retry lookup (`_refuse_conversation_session`). A session no
    conversation binds resumes as before."""
    world.run_pass()
    svc, _ = service(world)
    args = protocol.SubmitArgs(request_id="resume-1", kind="resume", workdir=str(world.workspace),
                               prompt_path=str(world.root / "prompt.md"), sandbox="read-only", parent_job_id="src")
    attempt = {"attempt_id": "src/a1", "native_session_id": SESSION.upper(), "lane_id": "claude-1",
               "model_requested": "claude-opus-5-5", "evidence_json": "{}"}
    try:
        with pytest.raises(AdapterError) as turn:
            Daemon._resume_submission(resume_daemon(world, svc, "turn", attempt), args)
        assert turn.value.code == 7 and "is a conversation turn" in str(turn.value)
        fake = resume_daemon(world, svc, "dispatch", attempt)
        fake._conversation_binding = lambda session_id: Daemon._conversation_binding(fake, session_id)
        _, resume = Daemon._resume_submission(fake, args)
        with pytest.raises(AdapterError) as bound:
            Daemon._refuse_conversation_session(fake, resume["native_session_id"], "resume")
        assert bound.value.code == 7 and "belongs to conversation" in str(bound.value)
        attempt["native_session_id"] = "5e551011-0000-4000-8000-0000000000ff"
        _, resume = Daemon._resume_submission(resume_daemon(world, svc, "dispatch", attempt), args)
        assert resume["native_session_id"] == attempt["native_session_id"]
    finally:
        svc.close()


def test_the_daemon_never_revives_or_nudges_a_conversation_bound_session(core):
    """C-26.3, D-17 (review M3): `sessions state` names the conversation-bound
    sessions, so the kit skips them; and the daemon itself refuses to record a
    nudge of one or to accept a revive job for one, whoever asks."""
    world, daemon = core
    state = daemon.sessions(protocol.SessionsArgs(action="state", session_ids=[SESSION]))
    assert state["conversation_sessions"] == [SESSION]
    nudged = daemon.sessions(protocol.SessionsArgs(action="nudged", session_id=SESSION.upper(), dedupe_key="k",
                                                   force=True))
    assert nudged["recorded"] is False and "bound to a Subfleet conversation" in nudged["reason"]
    prompt = world.root / "revive.md"
    prompt.write_text("continue", encoding="utf-8")
    with pytest.raises(AdapterError) as refused:
        daemon.submit(protocol.SubmitArgs(request_id="revive-1", kind="revive", workdir=str(world.workspace),
                                          prompt_path=str(prompt), sandbox="read-only", pinned_model="opus",
                                          caller_session=SESSION, in_place=True, allow_tmp=True))
    assert refused.value.code == 7
    assert daemon.store.query("SELECT * FROM jobs WHERE kind='revive'") == []


@pytest.mark.parametrize("recorded_as, asked_as", [(str.upper, str.lower), (str.lower, str.upper)],
                         ids=["upper-recorded", "lower-recorded"])
def test_a_session_only_a_turn_attempt_recorded_is_a_conversation_s_in_either_spelling(core, recorded_as, asked_as):
    """C-26.13, D-17 (review of 3c1a34e, finding 5): a turn ran in a session its
    conversation has not recorded yet (the session is minted, and bound once the
    turn settles), so only its attempt names it, possibly in the other case from
    a request. The daemon still treats it as the conversation's in either
    direction: `sessions state` lists it, a revive of it is refused at submit, one
    accepted before the turn ran is failed at admission, and a nudge of it is
    not recorded."""
    world, daemon = core
    svc = daemon.conversations
    minted = "5e551011-abcd-4ef0-8000-0000000000d7"
    prompt = world.root / "revive.md"
    prompt.write_text("continue", encoding="utf-8")

    def revive(request_id: str) -> dict:
        return daemon.submit(protocol.SubmitArgs(request_id=request_id, kind="revive", workdir=str(world.workspace),
                                                 prompt_path=str(prompt), sandbox="read-only", pinned_model="opus",
                                                 caller_session=asked_as(minted), in_place=True, allow_tmp=True))
    early = revive("revive-before-the-turn")["job_id"]
    submit(svc, world.conversation_id())
    svc._dispatch()
    (turn,) = daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")
    daemon.store.update_job(turn["job_id"], state="failed")
    daemon.store.add_attempt(attempt_id=f"{turn['job_id']}/a1", job_id=turn["job_id"], seq=1, lane_id="claude-1",
                             model_requested="claude-opus-5-5", state="failed", native_session_id=recorded_as(minted))
    assert Daemon._conversation_binding(daemon, asked_as(minted)) == f"turn job {turn['job_id']}"
    state = daemon.sessions(protocol.SessionsArgs(action="state", session_ids=[]))
    assert minted in state["conversation_sessions"]
    with pytest.raises(AdapterError) as refused:
        revive("revive-after-the-turn")
    assert refused.value.code == 7
    daemon._admit()
    job = daemon.store.get_job(early)
    assert (job["state"], job["rc"]) == ("failed", 7) and daemon.store.list_attempts(early) == []
    nudged = daemon.sessions(protocol.SessionsArgs(action="nudged", session_id=asked_as(minted), dedupe_key="k",
                                                   force=True))
    assert nudged["recorded"] is False and "bound to a Subfleet conversation" in nudged["reason"]


def _registered(world: World, *, subfleet: bool, session: str = SESSION) -> subprocess.Popen:
    """A live process registered for SESSION (as `session` spells it) in `<claude>/sessions`, as the
    Claude app or a terminal is."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **({"SUBFLEET_ATTEMPT": "turn-x/a1"} if subfleet else {})}
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env)
    # C-26.3: a row holds its session only while its `procStart` is its pid's
    # start as `TZ=UTC ps -o lstart=` prints it (Claude Code 2.1.280 writes it so).
    started = subprocess.run(["/bin/ps", "-p", str(process.pid), "-o", "lstart="], capture_output=True, text=True,
                             env={**os.environ, "TZ": "UTC"}).stdout.strip()
    (world.claude / "sessions").mkdir(exist_ok=True)
    (world.claude / "sessions" / f"{process.pid}.json").write_text(
        json.dumps({"sessionId": session, "pid": process.pid, "cwd": str(world.workspace), "procStart": started}),
        encoding="utf-8")
    return process


@pytest.mark.parametrize("subfleet", [False, True])
def test_a_live_claude_process_outside_subfleet_is_a_dispatch_wait(core, monkeypatch, subfleet):
    """C-26.3, D-17 (review M3), as the desktop line checks it: a Claude turn whose
    session a live process outside Subfleet holds (a registry row whose
    `procStart` is its pid's, without `SUBFLEET_ATTEMPT`) waits at dispatch,
    `external-writer: pid N` on its message, and gets its turn job once that
    process is gone. A Subfleet process (a turn that kept running across a
    restart) is not an external writer. (The launch-time check is
    test_conversation_service's.)"""
    world, daemon = core
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    svc = daemon.conversations
    first = submit(svc, world.conversation_id())
    process = _registered(world, subfleet=subfleet)
    try:
        svc._dispatch()
        jobs = daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")
        if subfleet:
            assert len(jobs) == 1
            return
        assert jobs == []
        assert svc.store.message(first)["state_reason"] == f"external-writer: pid {process.pid}"
    finally:
        process.kill()
        process.wait()
    with svc._lock:
        svc._deferred.clear()                  # its recheck interval has passed
    svc._dispatch()
    assert len(daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")) == 1


@pytest.mark.parametrize("direction, subfleet", [("same", False), ("same", True), ("upper-stored", False),
                                                 ("lower-stored", False)])
def test_a_live_claude_process_that_takes_the_session_after_its_job_is_made_is_a_launch_wait(
        core, monkeypatch, direction, subfleet):
    """C-26.3, D-17 (review M3's second case, dropped by the merge and restored
    after the review of 3c1a34e): the turn job is made, and only then does a live
    Claude process outside Subfleet register for the session (the Claude app took
    it while the job waited). Admission holds nothing for it and places the job;
    the launch finds the writer, whichever case the conversation's binding and
    the registry row spell the session in, and the attempt ends before
    `initialize`, so the message is never written. It waits again as
    `readmit:external-writer`, and its next launch, once the process is gone,
    finds nobody. A Subfleet process is not an external writer."""
    world, daemon = core
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    RecordingRelay.frames = []
    monkeypatch.setattr(runner_mod, "RelayClient", RecordingRelay)
    svc = daemon.conversations
    cid = world.conversation_id()
    stored, registered = {"same": (SESSION, SESSION), "upper-stored": (SESSION.upper(), SESSION),
                          "lower-stored": (SESSION, SESSION.upper())}[direction]
    with svc.store.transaction() as tx:                  # a binding a store kept as it was first spelled
        tx.execute("UPDATE conversations SET native_session_id=? WHERE conversation_id=?", (stored, cid))
    first = submit(svc, cid)
    svc._dispatch()
    (job,) = daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")
    process = _registered(world, subfleet=subfleet, session=registered)
    try:
        daemon._admit()
        (attempt,) = daemon.store.list_attempts(job["job_id"])      # no admission hold: placed
        adir = world.root / "jobs" / job["job_id"] / f"a{attempt['seq']}"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "start.json").write_text(json.dumps({"control_socket": str(world.root / "relay.sock")}))
        with daemon.store.transaction("attempt.starting", job_id=job["job_id"], attempt_id=attempt["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET state='starting' WHERE attempt_id=?", (attempt["attempt_id"],))
        monkeypatch.setattr(service_mod.reconcile, "gather", lambda *a, **k: service_mod.reconcile.Evidence(
            acknowledged=False, frame="absent", process_gone=True, native="absent", session_exists=True))
        svc._adopt_runners()
        runner = svc.runners[attempt["attempt_id"]]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not RecordingRelay.frames:
            time.sleep(0.02)
        tags = [tag for _, _, tag in RecordingRelay.frames]
        if subfleet:
            assert tags == ["init"] and runner.spec.held_by == ()
            runner.stop()
            assert runner.finished.wait(5)
            return
        assert tags == ["close"] and runner.spec.held_by == (process.pid,)
        assert (runner.driver.outcome.state, runner.driver.outcome.reason) == ("failed", "external-writer")
        (adir / "exit.json").write_text("{}")                 # the guardian reaped the provider
        assert runner.finished.wait(10)
        message = svc.store.message(first)
        assert (message["state"], message["state_reason"], message["job_id"], message["turn_seq"]) == (
            WAITING, "readmit:external-writer", None, 1)
        with daemon.store.transaction("attempt.finalized", job_id=job["job_id"]) as tx:   # as finalization ends it
            tx.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt["attempt_id"],))
            tx.execute("UPDATE jobs SET state='failed', finished_at=? WHERE job_id=?", ("2026-09-26", job["job_id"]))
            tx.execute("DELETE FROM leases WHERE holder=?", (job["job_id"],))
    finally:
        process.kill()
        process.wait()
    with svc._lock:
        svc._deferred.clear()
    svc._dispatch()
    again = daemon.store.one("SELECT * FROM jobs WHERE request_id=?", (f"turn:{first}:1",))
    daemon._admit()
    (attempt,) = daemon.store.list_attempts(again["job_id"])
    later = world.root / "jobs" / again["job_id"] / f"a{attempt['seq']}"
    later.mkdir(parents=True, exist_ok=True)
    turn = json.loads((world.root / "jobs" / again["job_id"] / "manifest.json").read_text())["turn"]
    assert turn["native_session_id"] == stored and svc._writer_check(turn, later) == []


def test_conversation_open_binds_one_spelling_of_a_session(world, monkeypatch):
    """C-24.1 (review L1): `conversation.open` of a session named in upper case
    finds the conversation bound to it in lower case, and a new one binds the
    lower-case id Claude Code names its transcript by."""
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    world.run_pass()
    cid = world.conversation_id()
    other = "5e551011-0000-4000-8000-0000000000c9"
    write_transcript(world.claude, other, world.workspace)
    svc, _ = service(world)
    try:
        opened = svc._open_native({"provider": "claude", "session_id": SESSION.upper()})
        assert [row["conversation_id"] for row in svc.store.query("SELECT * FROM conversations")] == [cid]
        assert opened["conversation_id"] == cid
        opened = svc._open_native({"provider": "claude", "session_id": other.upper()})
        assert opened["native_session_id"] == other
        assert len(svc.store.query("SELECT * FROM conversations")) == 2
    finally:
        svc.close()


# --- review follow-ups: a session opened after the pass, one job's failed check, why --


OPENED_LATER = "5e551011-0000-4000-8000-0000000000c7"
LATER = "1e9ac700-0000-4000-8000-0000000000c7"


def test_a_held_session_opened_after_the_pass_is_bound_held(world, monkeypatch):
    """C-30.4, D-17 (review follow-up B): a session the pass found held that
    had no conversation then gets one held when it is opened later, so no turn
    runs in it; the next pass that finds it settled releases it."""
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    write_transcript(world.claude, OPENED_LATER, world.workspace)

    def cockpit(status: str) -> None:
        write_outbox(world.v1, [outbox_row(HISTORY, SESSION, "finished", "cockpit history", at=600),
                                outbox_row(LATER, OPENED_LATER, status, "the cockpit's", at=60)])
    cockpit("dispatched")
    world.run_pass()
    svc, daemon = service(world)
    try:
        opened = svc._open_native({"provider": "claude", "session_id": OPENED_LATER})
        assert opened["legacy_hold"] == f"message {LATER} is dispatched"
        submit(svc, opened["conversation_id"])
        svc._dispatch()
        assert daemon.submitted == []
    finally:
        svc.close()
    cockpit("finished")
    report = world.run_pass()
    released = [item for item in report.stores["outbox"].items if item["source"] == "conversation"]
    assert [(item["conversation_id"], item["disposition"]) for item in released] == [
        (opened["conversation_id"], "bound-session-released")]
    svc, daemon = service(world)
    try:
        svc._dispatch()
        assert len(daemon.submitted) == 1
    finally:
        svc.close()


def test_while_every_session_is_held_any_session_opened_is_held(world, monkeypatch):
    """C-30.4 (review follow-up B): while a pass holds every session (here, a
    journal it cannot read), a session opened after it is bound held too."""
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    write_transcript(world.claude, OPENED_LATER, world.workspace)
    journal = world.v1 / "cockpit-client" / "pending-messages.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{torn", encoding="utf-8")
    world.run_pass()
    svc, _ = service(world)
    try:
        opened = svc._open_native({"provider": "claude", "session_id": OPENED_LATER})
        assert opened["legacy_hold"].startswith("the cockpit journal could not be read")
    finally:
        svc.close()


def test_one_turn_job_whose_conversation_cannot_be_checked_never_ends_the_pass(core, monkeypatch):
    """C-6.12, C-24.5 (review follow-up E): a turn job whose conversation check
    raises is held and named, and the pass goes on to place other jobs."""
    world, daemon = core
    svc = daemon.conversations
    submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    prompt = world.root / "detached.md"
    prompt.write_text("a detached job", encoding="utf-8")
    other = daemon.submit(protocol.SubmitArgs(request_id="detached-1", kind="dispatch", workdir=str(world.workspace),
                                              prompt_path=str(prompt), sandbox="read-only", pinned_model="opus",
                                              allow_tmp=True))["job_id"]

    def broken(job):
        raise OverflowError("signed integer is greater than maximum")
    monkeypatch.setattr(svc, "admission_hold", broken)
    daemon._admit()
    hold = daemon._holds[job["job_id"]]
    assert (hold["reason"], hold["error_type"]) == ("conversation-blocked", "OverflowError")
    assert daemon.store.list_attempts(job["job_id"]) == []
    assert len(daemon.store.list_attempts(other)) == 1


def test_a_registry_row_with_impossible_numbers_is_not_a_live_process(tmp_path):
    """C-26.3 (review follow-up E): a registry file whose pid or start time no
    process can have reads as not alive, and never raises."""
    from subfleet.sessions import registry
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text(json.dumps({"sessionId": SESSION, "pid": 10 ** 30, "startedAt": 10 ** 400}))
    (row,) = registry.rows(sessions)
    assert (row.alive, row.started_at) == (False, None)


def test_why_says_what_holds_a_turn():
    """C-6.11 (review follow-up G): `why` states the turn holds in sentences. (An outside
    writer is a dispatch and launch wait on the desktop line, C-26.3, not a hold.)"""
    from subfleet.render import why_queue
    held = why_queue({"job_id": "j", "state": "queued", "hold": {
        "reason": "conversation-blocked", "conversation_id": "cv-1", "blocked_by": "unfinished-turn",
        "legacy_hold": "message m is queued"}})
    assert held[1] == ("Held: its conversation cv-1 is blocked (blocked_by unfinished-turn; held by the legacy "
                       "import: message m is queued); the turn is placed once that clears and holds no other job "
                       "back (C-24.5)")
    settled = why_queue({"job_id": "j", "state": "queued", "hold": {"reason": "message-settled", "state": "cancelled"}})
    assert settled[1] == ("Held: its message was withdrawn after the job was made; the job is cancelled while it "
                          "has no attempt, never run (C-24.7)")


@pytest.mark.parametrize("op", ["message.cancel", "turn.interrupt"])
def test_a_message_the_hold_withheld_can_still_be_withdrawn(world, tmp_path, op):
    """C-24.7, IR-2, C-30.4 (second review, finding 2): a message waiting to be
    re-admitted with no turn job yet never reached the provider, so a person
    may withdraw it, by cancelling or stopping it; it is not dispatched when the
    hold lifts."""
    world.run_pass()
    cid = world.conversation_id()
    world.cockpit("dispatched")
    world.run_pass()
    svc, daemon = service(world)
    svc._person = lambda peer, what: types.SimpleNamespace(person=True, pid=1, reason="test")
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, STARTING, expect=("queued",), job_id="job-0")
        outcome(svc, tmp_path, first, cid, state="interrupted", reason="stopped-before-send",
                stop_reason="legacy-owner", user_frame_written=False)
        assert svc.store.message(first)["state_reason"] == "readmit:legacy-owner"
        receipt = svc.handle(op, {"message_id": first, "conversation_id": cid}, None)
        assert (receipt["state"], receipt["state_reason"]) == ("cancelled", "withdrawn")
    finally:
        svc.close()
    world.cockpit("finished")
    world.run_pass()
    svc, daemon = service(world)
    try:
        svc._dispatch()
        assert daemon.submitted == []
    finally:
        svc.close()


@pytest.mark.parametrize("handed_over", [False, True])
def test_a_person_s_stop_before_the_message_was_handed_over_is_never_overridden(world, monkeypatch, handed_over):
    """C-24.7, IR-2 (second review, finding 2): a runner whose message has a
    person's stop recorded, and which the relay log does not show handed over,
    never writes it, whatever the provider has answered meanwhile. One the log
    shows handed over is stopped through D-13 after the replay."""
    RecordingRelay.frames = []
    monkeypatch.setattr(runner_mod, "RelayClient", RecordingRelay)
    world.run_pass()
    svc, daemon = service(world)
    try:
        first = submit(svc, world.conversation_id())
        svc.store.set_state(first, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
        svc.store.update_message(first, stop_requested_at="2026-09-25T20:00:00.000Z")
        aid = running_attempt(world, daemon, world.conversation_id(), first)
        adir = world.root / "jobs" / "turn-cv-held-20260925" / "a1"
        log = [{"kind": "intent", "seq": 1, "tag": "init", "op": "write"}, {"kind": "written", "seq": 1}]
        if handed_over:
            log += [{"kind": "intent", "seq": 2, "tag": "user-message", "op": "write"}, {"kind": "written", "seq": 2}]
        (adir / "stdin.jsonl").write_text("".join(json.dumps(row) + "\n" for row in log))
        (adir / "stdout").write_text(json.dumps(_init_answer()) + "\n")
        svc._adopt_runners()
        runner = svc.runners[aid]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (runner.driver.outcome or runner.offset):
            time.sleep(0.02)
        time.sleep(0.2)
        runner.stop()
        assert runner.finished.wait(5)
        tags = [tag for _, _, tag in RecordingRelay.frames]
        if handed_over:
            # D-13 from the recorded stop (its escalation clock runs), and never written twice.
            assert "user-message" not in tags and not runner.withheld and runner.stop_at is not None
            assert runner.driver.phase == "sent" and runner.driver.outcome is None
        else:
            assert tags == ["close"] and runner.driver.outcome.reason == "stopped-before-send"
    finally:
        svc.close()


def test_a_stop_acknowledged_before_the_provider_answered_initialize_keeps_the_message_unwritten(
        world, tmp_path, monkeypatch):
    """C-24.7 (review of 3c1a34e, finding 2; the reviewer's probe): the runner
    read no stop at its start and sent `initialize`; a person's `turn.interrupt`
    is acknowledged (`stop_requested: true`) before the provider's answer can be
    read. The runner reads that answer before its commands, but the message
    frame is never sent: stdin is closed, the turn ends stopped before sending,
    and the message settles as the person's stop, not re-admitted."""
    world.run_pass()
    svc, daemon = service(world)
    cid = world.conversation_id()
    mid = submit(svc, cid)
    svc.store.set_state(mid, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
    aid = running_attempt(world, daemon, cid, mid)
    adir = world.root / "jobs" / "turn-cv-held-20260925" / "a1"
    tags, receipts = [], []

    class Relay(RecordingRelay):
        def send(self, seq, op, line=None, tag=None, sig=None):
            from subfleet.relay import Ack
            tags.append(tag)
            if tag == "init":
                receipts.append(svc.op_turn_interrupt({"message_id": mid}, None))
                (adir / "stdout").write_text(json.dumps(_init_answer()) + "\n")
            if tag == "close":
                (adir / "exit.json").write_text("{}")         # the provider ends at end of input
            return Ack(seq=seq, ok=True)

    monkeypatch.setattr(runner_mod, "RelayClient", Relay)
    monkeypatch.setattr(service_mod.reconcile, "gather", lambda *a, **k: service_mod.reconcile.Evidence(
        acknowledged=False, frame="absent", process_gone=True, native="absent", session_exists=True))
    try:
        svc._adopt_runners()
        runner = svc.runners[aid]
        assert runner.finished.wait(10)
        assert receipts[0]["stop_requested"] is True
        assert tags == ["init", "close"] and runner.driver.outcome.reason == "stopped-before-send"
        assert json.loads((adir / "turn.json").read_text())["user_frame_written"] is False
        message = svc.store.message(mid)
        assert (message["state"], message["state_reason"]) == ("failed", "not-delivered: stopped-before-send")
    finally:
        svc.close()


def test_a_stop_that_comes_while_the_message_is_handed_over_waits_for_it(world, monkeypatch):
    """C-24.7 (review of 3c1a34e, finding 2): recording a person's stop and handing
    the message frame to the relay are serialized, so neither overtakes the
    other half-way: a `turn.interrupt` made while the frame is being handed over
    is recorded only once the relay has it, and so stops the turn through D-13
    (the provider's interrupt) rather than claiming the message was never
    written."""
    world.run_pass()
    svc, daemon = service(world)
    cid = world.conversation_id()
    mid = submit(svc, cid)
    svc.store.set_state(mid, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
    aid = running_attempt(world, daemon, cid, mid)
    adir = world.root / "jobs" / "turn-cv-held-20260925" / "a1"
    (adir / "stdout").write_text(json.dumps(_init_answer()) + "\n")
    tags, events = [], {"acked": threading.Event(), "recorded": threading.Event()}
    order: list[str] = []

    def interrupt():
        svc.op_turn_interrupt({"message_id": mid}, None)
        order.append("stop recorded")
        events["recorded"].set()

    class Relay(RecordingRelay):
        def send(self, seq, op, line=None, tag=None, sig=None):
            from subfleet.relay import Ack
            tags.append(tag)
            if tag == "user-message":
                threading.Thread(target=interrupt, daemon=True).start()
                # The stop waits for the handover; a wait long enough to see it overtake would show here.
                assert not events["recorded"].wait(0.5)
                order.append("handed over")
            if tag == "interrupt":
                (adir / "exit.json").write_text("{}")
            return Ack(seq=seq, ok=True)

    monkeypatch.setattr(runner_mod, "RelayClient", Relay)
    try:
        svc._adopt_runners()
        runner = svc.runners[aid]
        assert events["recorded"].wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "interrupt" not in tags:
            time.sleep(0.02)
        assert order == ["handed over", "stop recorded"]
        assert tags == ["init", "user-message", "interrupt"] and not runner.withheld
        runner.stop()
        assert runner.finished.wait(5)
    finally:
        svc.close()


@pytest.mark.parametrize("store_error", [sqlite3.OperationalError("database is locked"), OSError(5, "I/O error")])
def test_a_store_error_in_the_conversation_check_is_the_pass_s(core, monkeypatch, caplog, store_error):
    """C-6.12, C-5.10 (second review, finding 4): a store error while checking a
    turn's conversation ends the pass, for C-5.10 to retry; any other error
    holds that job alone and is logged once, not once a pass."""
    world, daemon = core
    svc = daemon.conversations
    submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")

    def locked(job):
        raise store_error
    monkeypatch.setattr(svc, "admission_hold", locked)
    with pytest.raises(type(store_error)):
        daemon._admit()

    def broken(job):
        raise ValueError("a bad registry row")
    monkeypatch.setattr(svc, "admission_hold", broken)
    with caplog.at_level(logging.WARNING, logger="subfleet.test.legacy-hold"):
        for _ in range(3):
            daemon._admit()
    assert sum("could not be checked" in record.getMessage() for record in caplog.records) == 1
    assert daemon._holds[job["job_id"]]["conversation_id"] == world.conversation_id()
    assert svc._cancel_job_without_attempt(job["job_id"])
    daemon._admit()
    assert job["job_id"] not in daemon._turn_check_errors          # forgotten once the job leaves the queue


def test_a_withdrawal_racing_the_dispatcher_leaves_no_turn_to_run(core, tmp_path, monkeypatch):
    """C-24.7, IR-2 (third review, finding A): the dispatcher may submit the next
    turn job of a message waiting to be re-admitted between a person's cancel
    checking for one and committing. The cancel still stands: the stop is
    recorded, the job is cancelled while it has no attempt, and admission never
    places it."""
    world, daemon = core
    svc = daemon.conversations
    cid = world.conversation_id()
    first = submit(svc, cid)
    svc.store.set_state(first, STARTING, expect=("queued",), job_id="job-0")
    outcome(svc, tmp_path, first, cid, state=None, reason="provider-init-failed", user_frame_written=False)
    assert svc.store.message(first)["state_reason"] == "readmit:provider-init-failed"
    real = svc._turn_job
    raced = []

    def racing(message):
        found = real(message)
        if not raced:                                    # the cancel's first look: nothing yet...
            raced.append(True)
            svc._dispatch()                              # ...and the dispatcher submits right after it
        return found
    monkeypatch.setattr(svc, "_turn_job", racing)
    receipt = svc.op_message_cancel({"message_id": first}, None)
    monkeypatch.setattr(svc, "_turn_job", real)
    assert (receipt["state"], receipt["stop_requested"]) == ("cancelled", True)
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    assert job["request_id"] == f"turn:{first}:1" and job["state"] == "cancelled"
    daemon._admit()
    assert daemon.store.list_attempts(job["job_id"]) == []


def test_admission_cancels_a_turn_job_whose_message_is_settled(core):
    """C-24.7, C-6.11 (third review, finding A): a turn job whose message was
    withdrawn after the job was made is cancelled at admission, never run."""
    world, daemon = core
    svc = daemon.conversations
    first = submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    svc.store.set_state(first, "cancelled", reason="withdrawn", expect=("waiting",))
    daemon._admit()
    assert daemon.store.get_job(job["job_id"])["state"] == "cancelled"
    assert daemon.store.list_attempts(job["job_id"]) == []
    assert daemon._holds[job["job_id"]]["reason"] == "message-settled"


@pytest.mark.parametrize("manifest", [None, "[1, 2", '{"turn": [1, 2]}', '{"turn": "a string"}', "7", '{"turn": {}}',
                                      '{"turn": {"conversation_id": [1], "message_id": "m", "provider": "claude"}}'])
def test_a_turn_manifest_that_cannot_be_read_holds_that_job_alone(core, manifest, caplog):
    """C-6.12 (fourth review, finding 1): a turn job whose manifest cannot be read
    as one (not JSON, or no turn object) is held, and the pass goes on to place
    other jobs; it never ends every pass."""
    world, daemon = core
    svc = daemon.conversations
    submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    path = world.root / "jobs" / job["job_id"] / "manifest.json"
    if manifest is None:
        path.unlink()                                    # deleted from under the daemon
    else:
        path.write_text(manifest, encoding="utf-8")
    prompt = world.root / "detached.md"
    prompt.write_text("a detached job", encoding="utf-8")
    other = daemon.submit(protocol.SubmitArgs(request_id="detached-3", kind="dispatch", workdir=str(world.workspace),
                                              prompt_path=str(prompt), sandbox="read-only", pinned_model="opus",
                                              allow_tmp=True))["job_id"]
    with caplog.at_level(logging.WARNING, logger="subfleet.test.legacy-hold"):
        daemon._admit()
        daemon._admit()
    hold = daemon._holds[job["job_id"]]
    assert (hold["reason"], hold["error"]) == ("conversation-blocked", "its turn manifest cannot be read")
    assert daemon.store.list_attempts(job["job_id"]) == []
    assert len(daemon.store.list_attempts(other)) == 1
    assert sum(f"job {job['job_id']} held" in record.getMessage() for record in caplog.records) == 1   # said once


def test_a_failed_check_with_an_unreadable_manifest_still_holds_that_job_alone(core, monkeypatch):
    """C-6.12 (third review, finding C): when a turn job's check fails and its
    manifest cannot be read either, the job is held and the pass goes on."""
    world, daemon = core
    svc = daemon.conversations
    submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    (world.root / "jobs" / job["job_id"] / "manifest.json").write_text("[1, 2", encoding="utf-8")
    prompt = world.root / "detached.md"
    prompt.write_text("a detached job", encoding="utf-8")
    other = daemon.submit(protocol.SubmitArgs(request_id="detached-2", kind="dispatch", workdir=str(world.workspace),
                                              prompt_path=str(prompt), sandbox="read-only", pinned_model="opus",
                                              allow_tmp=True))["job_id"]

    def broken(job):
        raise ValueError("a bad registry row")
    monkeypatch.setattr(svc, "admission_hold", broken)
    daemon._admit()
    assert daemon._holds[job["job_id"]]["conversation_id"] is None
    assert len(daemon.store.list_attempts(other)) == 1


def test_a_runner_the_store_refuses_to_mark_is_still_started(world, monkeypatch):
    """C-30.4, D-17 (second review, finding 5): a runner the service registers is
    always started, even when marking its message raises, so it is never left
    registered and idle, beyond adoption and containment."""
    RecordingRunner.made = []
    monkeypatch.setattr(service_mod, "TurnRunner", RecordingRunner)
    world.run_pass()
    cid = world.conversation_id()
    svc, daemon = service(world)
    try:
        first = submit(svc, cid)
        svc.store.set_state(first, WAITING, expect=("queued",), job_id="turn-cv-held-20260925")
        running_attempt(world, daemon, cid, first)
        real = svc.store.set_state

        def failing(*args, **kwargs):
            monkeypatch.setattr(svc.store, "set_state", real)
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(svc.store, "set_state", failing)
        with pytest.raises(sqlite3.OperationalError):
            svc._adopt_runners()
        (runner,) = RecordingRunner.made
        assert runner.started
    finally:
        svc.close()


def _race_to_an_attempt(svc: ConversationService, daemon, message_id: str, *, adopted: bool):
    """The dispatcher makes the message's turn job and admission reserves an
    attempt, between a cancel's first look and its commit; `adopted`: the
    daemon has also moved the message on to starting."""
    raced = []
    real = svc._turn_job

    def racing(message):
        found = real(message)
        if not raced:
            raced.append(True)
            conversation = svc.store.conversation(message["conversation_id"])
            svc._submit_turn(conversation, svc.store.message(message_id))      # the message is still queued
            daemon._admit()
            if adopted:
                svc.store.set_state(message_id, STARTING, expect=("queued",))
        return found
    return racing


def test_a_cancel_racing_admission_records_the_stop_first(core, monkeypatch):
    """C-24.7, IR-2 (fourth review, finding 2): a queued message whose turn job
    the dispatcher makes, and admission gives an attempt, between a cancel's
    first look and its commit, is cancelled with its stop already recorded, so
    the runner for that attempt never writes it."""
    world, daemon = core
    svc = daemon.conversations
    first = submit(svc, world.conversation_id())
    monkeypatch.setattr(svc, "_turn_job", _race_to_an_attempt(svc, daemon, first, adopted=False))
    receipt = svc.op_message_cancel({"message_id": first}, None)
    assert (receipt["state"], receipt["stop_requested"]) == ("cancelled", True)
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    assert len(daemon.store.list_attempts(job["job_id"])) == 1           # too late to cancel the job: the stop holds


def test_a_refused_cancel_leaves_no_stop_behind(core, monkeypatch):
    """C-24.7 (fourth review, finding 6): a cancel refused as too late does not
    leave the stop it recorded, which a restart would read and act on."""
    world, daemon = core
    svc = daemon.conversations
    first = submit(svc, world.conversation_id())
    monkeypatch.setattr(svc, "_turn_job", _race_to_an_attempt(svc, daemon, first, adopted=True))
    with pytest.raises(ConversationError) as refused:
        svc.op_message_cancel({"message_id": first}, None)
    assert refused.value.reason == "too-late"
    assert svc.store.message(first)["stop_requested_at"] is None


def test_a_stop_is_never_lost_to_a_withdrawal_that_loses_its_race(core, tmp_path, monkeypatch):
    """C-24.7 (fifth review, finding 2): a person's `turn.interrupt` of a message
    waiting to be re-admitted records the stop; if the withdrawal then loses to
    the daemon adopting the message, the stop stays, so the runner never writes
    it. A cancel refused as too late never clears a stop it did not record."""
    world, daemon = core
    svc = daemon.conversations
    cid = world.conversation_id()
    first = submit(svc, cid)
    svc.store.set_state(first, STARTING, expect=("queued",), job_id="job-0")
    outcome(svc, tmp_path, first, cid, state=None, reason="provider-init-failed", user_frame_written=False)
    real = svc._turn_job
    moved = []

    def adopted_meanwhile(message):
        found = real(message)
        if not moved:                                   # the daemon adopts it right after the check
            moved.append(True)
            svc.store.set_state(first, STARTING, expect=(WAITING,))
        return found
    monkeypatch.setattr(svc, "_turn_job", adopted_meanwhile)
    receipt = svc.op_turn_interrupt({"message_id": first}, None)
    monkeypatch.setattr(svc, "_turn_job", real)
    assert receipt["state"] == STARTING and receipt["stop_requested"] is True

    second = submit(svc, cid, after=first)
    interrupted = []

    def interrupted_meanwhile(message):
        found = real(message)
        if not interrupted:                             # a stop lands, and the message moves on, meanwhile
            interrupted.append(True)
            svc.store.update_message(second, stop_requested_at="2026-09-26T00:00:00.000Z")
            svc.store.set_state(second, STARTING, expect=("queued",))
        return found
    monkeypatch.setattr(svc, "_turn_job", interrupted_meanwhile)
    with pytest.raises(ConversationError):
        svc.op_message_cancel({"message_id": second}, None)
    assert svc.store.message(second)["stop_requested_at"] == "2026-09-26T00:00:00.000Z"
