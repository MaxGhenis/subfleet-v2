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
import types
import uuid
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, importer, protocol
from subfleet.adapters.base import AdapterError
from subfleet.conversations import service as service_mod
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
        return None                                           # no lease is held

    def query(self, sql, params=()):
        if sql.startswith("SELECT job_id, state FROM jobs WHERE kind='turn' AND name=?"):
            return [{"job_id": job["job_id"], "state": job["state"]} for job in self.jobs.values()
                    if job["name"] == params[0] and job["job_id"] != params[1]]
        if sql.startswith("SELECT a.*, j.kind FROM attempts a"):
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
                                   attempt={"lane_id": None}, attempt_id="job-1/a1")
    svc._on_outcome(runner)


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
        assert svc._readmittable() == []
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
        outcome(svc, tmp_path, first, cid, state=None, reason="ended-without-result", user_frame_written=True)
        assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"
        svc.op_conversation_unblock({"conversation_id": cid, "choice": "continue", "confirm": True}, None)
        assert svc.store.conversation(cid)["blocked_by"] is None
        assert dispatchable(svc) == []
        svc._dispatch()
        assert daemon.submitted == []
        assert svc.store.message(second)["state"] == "queued"
        view = svc._view(svc.store.conversation(cid))       # the app sees both, each with its own owner
        assert (view["blocked_by"], view["legacy_hold"]) == (None, f"message {COCKPIT} is dispatched")
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
        outcome(svc, tmp_path, first, cid, state=None, reason=None, user_frame_written=False)
        assert svc.store.message(first)["state"] == DELIVERY_UNKNOWN
        assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
        second = submit(svc, cid, after=first)
        svc.op_message_resolve({"message_id": first, "resolution": "delivered", "confirm": True}, None)
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
        self.started = False
        self.message_id = kwargs["spec"].message_id
        self.finished = threading.Event()
        RecordingRunner.made.append(self)

    def start(self):
        self.started = True

    def interrupt(self, reason="stopped"):
        self.interrupts.append(reason)

    def stop(self):
        pass


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
                              "state": "running", "kind": "turn"}]
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
        assert runner.interrupts == (["legacy-owner"] if held else [])
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
        _legacy_resume_identity=lambda attempt: None)


def test_resume_refuses_a_turn_source_and_a_conversation_bound_session(world):
    """C-26.3, D-17 (review M3): `resume` of a turn job would run `claude --resume`
    on its conversation's session beside the conversation; it is refused, as is
    resuming any job whose session a conversation is bound to. A session no
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
        assert turn.value.code == 7 and "a conversation turn is not resumed" in str(turn.value)
        with pytest.raises(AdapterError) as bound:
            Daemon._resume_submission(resume_daemon(world, svc, "dispatch", attempt), args)
        assert bound.value.code == 7 and "bound to a Subfleet conversation" in str(bound.value)
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


def _registered(world: World, *, subfleet: bool) -> subprocess.Popen:
    """A live process registered for SESSION in `<claude>/sessions`, as the Claude app or a terminal is."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **({"SUBFLEET_ATTEMPT": "turn-x/a1"} if subfleet else {})}
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env)
    (world.claude / "sessions").mkdir(exist_ok=True)
    (world.claude / "sessions" / f"{process.pid}.json").write_text(
        json.dumps({"sessionId": SESSION, "pid": process.pid, "cwd": str(world.workspace)}), encoding="utf-8")
    return process


@pytest.mark.parametrize("subfleet", [False, True])
def test_a_live_claude_process_outside_subfleet_is_an_admission_wait(core, monkeypatch, subfleet):
    """C-26.3, D-17 (review M3): a Claude turn whose session a live process
    outside Subfleet holds (a pid in `~/.claude/sessions` without
    `SUBFLEET_ATTEMPT`) waits at admission, `external-writer`, shown on its
    message, and is placed once that process is gone. A Subfleet process (a
    turn that kept running across a restart) is not an external writer."""
    world, daemon = core
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(world.claude))
    monkeypatch.setattr(service_mod, "EXTERNAL_WRITER_TTL_S", -1.0, raising=False)
    svc = daemon.conversations
    first = submit(svc, world.conversation_id())
    svc._dispatch()
    job = daemon.store.one("SELECT * FROM jobs WHERE kind='turn'")
    process = _registered(world, subfleet=subfleet)
    try:
        daemon._admit()
        if subfleet:
            assert len(daemon.store.list_attempts(job["job_id"])) == 1
            return
        assert daemon.store.list_attempts(job["job_id"]) == []
        hold = daemon._holds[job["job_id"]]
        assert (hold["reason"], hold["pids"], hold["native_session_id"]) == ("external-writer", [process.pid], SESSION)
        assert svc.store.message(first)["state_reason"] == "external-writer"
        assert daemon._admission["pending"] == 0             # the person's to end: close it there
    finally:
        process.kill()
        process.wait()
    daemon._admit()
    assert len(daemon.store.list_attempts(job["job_id"])) == 1
    assert svc.store.message(first)["state_reason"] is None


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
