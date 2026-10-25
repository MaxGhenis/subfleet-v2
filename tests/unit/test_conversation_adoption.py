"""D-ADOPT-RACE1: an op that needs a live turn's runner adopts the turn itself when the
control loop has not yet (C-27.3, C-24.9, D-13, C-25.3).

After a restart the daemon serves requests before its control loop's first tick runs
`_adopt_runners`. Each test here builds the state such a restart leaves (a turn's
attempt live in the job store, its files on disk, its message bound to its job, no
runner registered) on a fresh service, then sends the op before any pass.

Invariants, for every input and interleaving:
- I1: a pending approval whose attempt the job store holds live is never refused
  `turn-ended`: it is answered, or refused `turn-not-adopted` when no runner can be
  adopted now.
- I2: one started runner per attempt, the one registered, whichever of the control
  loop and an op adopts first.
- I3: a request thread never runs the launch writer check's `ps` (C-25.3).
"""

from __future__ import annotations

import json
import threading
import uuid

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet.conversations import catalog as catalog_module
from subfleet.conversations import service as service_module
from subfleet.conversations.launch import TURN_MANIFEST_KEY
from subfleet.conversations.peers import Verdict
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import (  # noqa: F401 (svc is a fixture)
    ASK, INIT_OK, SETTINGS, FakeDaemon, conversation, submit, svc, turn_attempt)

OPTIONS = ("allow", "deny", "cancel-turn")
REQUEST = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}, "tool_use_id": "tu1"}


def relay_log(*tags: str) -> bytes:
    """`stdin.jsonl` holding each tag's frame, written (relay.py `read_log`)."""
    rows = []
    for seq, tag in enumerate(tags, 1):
        rows += [{"kind": "intent", "seq": seq, "op": "write", "tag": tag}, {"kind": "written", "seq": seq}]
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def left_running(svc, tmp_path, *, state="running", native: str | None = None, written=True, n=0) -> dict:
    """A turn a restart left running (C-26.6): its message `state` and bound to its job,
    its attempt live in the job store, its start record (a relay socket nothing listens
    on, so a runner sends nothing), manifest and stdout so far, and with `written` a
    relay log that shows its message handed over. No runner has it."""
    cid = conversation(svc, **({"origin": "native", "native_session_id": native} if native else {}))
    mid = submit(svc, cid)
    aid = turn_attempt(svc, mid, state="running", n=n)
    job_id = aid.rsplit("/", 1)[0]
    svc.store.set_state(mid, state, job_id=job_id,
                        reason="admission: sent to the daemon, which has not placed it yet" if state == "waiting" else None)
    job = svc.root / "jobs" / job_id
    adir = job / "a1"
    adir.mkdir(parents=True)
    (adir / "start.json").write_text(json.dumps({"control_socket": str(tmp_path / f"relay-{n}.sock")}))
    (adir / "stdout").write_bytes(b"".join(line.encode() + b"\n" for line in (INIT_OK, ASK)))
    if written:
        (adir / "stdin.jsonl").write_bytes(relay_log("initialize", "user-message"))
    turn = {"provider": "claude", "conversation_id": cid, "message_id": mid, "text": "hello", "settings": SETTINGS,
            "cwd": svc.test_workspace}
    turn.update({"native_session_id": native} if native else {"new_session_id": str(uuid.uuid4())})
    (job / "manifest.json").write_text(json.dumps({TURN_MANIFEST_KEY: turn}))
    return {"cid": cid, "mid": mid, "aid": aid, "job_id": job_id, "adir": adir}


def asking(svc, tmp_path, **kw) -> dict:
    """`left_running` with the provider's question pending (C-27.1), as its first runner recorded it."""
    turn = left_running(svc, tmp_path, state="approval-needed", **kw)
    approval, _ = svc.store.add_approval(message_id=turn["mid"], conversation_id=turn["cid"], attempt_id=turn["aid"],
                                         provider_request_id="req-1", kind="tool", request=REQUEST,
                                         display={"tool": "Bash"}, options=OPTIONS)
    return {**turn, "approval": approval}


def as_person(svc):
    svc._person = lambda peer, what: Verdict(True, "test", peer)


def answer(svc, approval: dict, decision="allow") -> dict:
    return svc.op_approval_respond({"approval_id": approval["approval_id"], "decision": decision,
                                    "nonce": approval["nonce"], "request_sha256": approval["request_sha256"]}, None)


def commands(runner) -> list[tuple]:
    return list(runner.commands.queue)


@pytest.fixture
def quiet(monkeypatch):
    """Runners whose threads never start: `start` is recorded, nothing is read or sent."""
    started: list[TurnRunner] = []
    monkeypatch.setattr(TurnRunner, "start", lambda runner: started.append(runner))
    return started


# --- I1: the approval ----------------------------------------------------------------


def test_an_approval_answered_before_the_first_pass_reaches_the_turns_runner(svc, tmp_path, quiet):
    """C-27.3: the answer was refused `turn-ended` until the control loop's pass."""
    as_person(svc)
    turn = asking(svc, tmp_path)
    result = answer(svc, turn["approval"])
    assert result["approval"]["state"] == "answered"
    runner = svc.runners[turn["aid"]]
    assert quiet == [runner]
    assert ("respond", "req-1", "allow", None, None) in commands(runner)
    svc._adopt_runners()                    # the pass, later: it adopts nothing more
    assert svc.runners[turn["aid"]] is runner and quiet == [runner]


@pytest.mark.parametrize("why", ["thread-limit", "no-control-socket", "store-raises"])
def test_a_live_turn_whose_runner_cannot_be_adopted_now_is_not_reported_ended(svc, tmp_path, monkeypatch, why):
    """I1: the attempt is live, so the answer is refused `turn-not-adopted` (exit 7),
    which the person may send again, and the approval stays pending."""
    as_person(svc)
    turn = asking(svc, tmp_path)
    if why == "thread-limit":
        def failing(runner):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(TurnRunner, "start", failing)
    elif why == "no-control-socket":
        (turn["adir"] / "start.json").write_text("{}")
    else:
        def raising(*args, **kwargs):
            raise OSError(5, "Input/output error")
        monkeypatch.setattr(svc, "_writer_check", raising)
    with pytest.raises(ConversationError) as refused:
        answer(svc, turn["approval"])
    assert (refused.value.reason, refused.value.code) == ("turn-not-adopted", 7)
    assert svc.store.approval(turn["approval"]["approval_id"])["state"] == "pending"
    assert turn["aid"] not in svc.runners


@pytest.mark.parametrize("state", ["succeeded", "failed", "lost", "cancelled", "interrupted"])
def test_an_approval_whose_attempt_has_ended_is_refused_turn_ended(svc, tmp_path, quiet, state):
    as_person(svc)
    turn = asking(svc, tmp_path)
    with svc.daemon.store.transaction("test.ended") as tx:
        tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", (state, turn["aid"]))
    with pytest.raises(ConversationError) as refused:
        answer(svc, turn["approval"])
    assert refused.value.reason == "turn-ended" and svc.runners == {} and quiet == []


@pytest.mark.parametrize("state", ["starting", "running", "finalizing"])
def test_every_live_attempt_state_is_adopted_for_an_answer(svc, tmp_path, quiet, state):
    as_person(svc)
    turn = asking(svc, tmp_path)
    with svc.daemon.store.transaction("test.live") as tx:
        tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", (state, turn["aid"]))
    assert answer(svc, turn["approval"])["approval"]["state"] == "answered"
    assert quiet == [svc.runners[turn["aid"]]]


# --- the other ops that need a runner -------------------------------------------------


def test_a_steer_before_the_first_pass_adopts_its_host(svc, tmp_path, quiet, monkeypatch):
    """C-24.9, C-29.7: still `not-steerable` while the runner catches up, which the app
    asks again about; no longer until the control loop's pass."""
    as_person(svc)
    turn = left_running(svc, tmp_path)
    mid = submit(svc, turn["cid"], "steer", after=turn["mid"])
    with pytest.raises(ConversationError) as refused:
        svc.op_message_steer({"message_id": mid}, None)
    assert refused.value.reason == "not-steerable"
    runner = svc.runners[turn["aid"]]
    assert quiet == [runner] and svc.store.message(mid)["state"] == "queued"
    monkeypatch.setattr(TurnRunner, "steerable", property(lambda self: True))   # its replay caught up
    assert svc.op_message_steer({"message_id": mid}, None)["state"] == "steering"
    assert ("steer", mid) in commands(runner)


def test_a_cancel_of_a_steer_never_written_before_the_first_pass_withdraws_it(svc, tmp_path, quiet):
    """C-24.7, C-24.9: with no runner the cancel answered `too-late`; the adopted host's
    relay log shows no frame of it, so it is withdrawn and the host never sends it."""
    turn = left_running(svc, tmp_path)
    mid = submit(svc, turn["cid"], "steer", after=turn["mid"])
    svc.store.claim_steer(mid, turn["mid"])
    assert svc.op_message_cancel({"message_id": mid}, None)["state"] == "cancelled"
    assert quiet == [svc.runners[turn["aid"]]]


def test_a_cancel_of_a_steer_the_relay_logged_is_too_late_after_adoption_too(svc, tmp_path, quiet):
    turn = left_running(svc, tmp_path)
    mid = submit(svc, turn["cid"], "steer", after=turn["mid"])
    svc.store.claim_steer(mid, turn["mid"])
    (turn["adir"] / "stdin.jsonl").write_bytes(relay_log("initialize", "user-message", f"steer:{mid}"))
    with pytest.raises(ConversationError) as refused:
        svc.op_message_cancel({"message_id": mid}, None)
    assert refused.value.reason == "too-late" and svc.store.message(mid)["state"] == "steering"


def test_a_stop_before_the_first_pass_goes_through_the_runner(svc, tmp_path, quiet):
    """D-13, IR-4: the provider is asked to stop first. The job was cancelled with no
    runner, and the daemon's kill path killed it outright."""
    turn = left_running(svc, tmp_path)
    svc.op_turn_interrupt({"message_id": turn["mid"]}, None)
    runner = svc.runners[turn["aid"]]
    assert quiet == [runner] and ("interrupt",) in commands(runner) and runner.stop_reason == "stopped"
    job = svc.daemon.store.one("SELECT cancel_requested_at FROM jobs WHERE job_id=?", (turn["job_id"],))
    assert job["cancel_requested_at"]
    attempt = svc.daemon.store.one("SELECT * FROM attempts WHERE attempt_id=?", (turn["aid"],))
    assert svc.stop(attempt, svc.daemon._job(turn["job_id"]), "operator-kill") is True


@pytest.mark.parametrize("reason", ["wall-limit", "operator-kill"])
def test_the_daemons_kill_path_before_the_first_pass_stops_through_d13(svc, tmp_path, quiet, reason):
    turn = left_running(svc, tmp_path)
    attempt = svc.daemon.store.one("SELECT * FROM attempts WHERE attempt_id=?", (turn["aid"],))
    assert svc.stop(attempt, svc.daemon._job(turn["job_id"]), reason) is True
    runner = svc.runners[turn["aid"]]
    assert quiet == [runner] and runner.stop_reason == reason and ("interrupt",) in commands(runner)


# --- I3: never `ps` on a request thread -----------------------------------------------


def test_an_op_leaves_a_turn_whose_writer_check_would_run_ps_to_the_control_loop(svc, tmp_path, quiet, monkeypatch):
    """C-25.3, C-26.3: a Claude turn on a native session with nothing written to it
    yet, and no recorded writer decision, is decided by `ps`: only the pass runs it."""
    looked: list[str] = []
    monkeypatch.setattr(catalog_module, "external_writers", lambda session: looked.append(session) or [])
    session = str(uuid.uuid4())
    turn = left_running(svc, tmp_path, state="waiting", native=session, written=False)
    attempt = svc.daemon.store.one("SELECT * FROM attempts WHERE attempt_id=?", (turn["aid"],))
    assert svc.stop(attempt, svc.daemon._job(turn["job_id"]), "operator-kill") is False
    svc._adopt_now(turn["aid"])
    svc.op_turn_interrupt({"message_id": turn["mid"]}, None)
    assert looked == [] and svc.runners == {} and quiet == []
    svc._adopt_runners()                    # the control loop's pass decides
    assert looked == [session] and quiet == [svc.runners[turn["aid"]]]


def test_an_op_adopts_a_turn_whose_writer_decision_is_recorded(svc, tmp_path, quiet, monkeypatch):
    monkeypatch.setattr(catalog_module, "external_writers", lambda session: pytest.fail("ps on a request thread"))
    turn = left_running(svc, tmp_path, state="waiting", native=str(uuid.uuid4()), written=False)
    (turn["adir"] / "held_by.json").write_text(json.dumps({"pids": [], "at": "2026-10-25T00:00:00Z"}))
    svc._adopt_now(turn["aid"])
    assert quiet == [svc.runners[turn["aid"]]]


# --- I2: one runner per attempt, whoever adopts first ---------------------------------


@pytest.mark.parametrize("held", ["the control loop", "the op"])
def test_an_op_and_the_control_loop_adopting_at_once_start_one_runner(svc, tmp_path, monkeypatch, quiet, held):
    """I2: both build a runner; the first to register owns the attempt, the other's is
    never started, and the answer reaches the registered one."""
    as_person(svc)
    turn = asking(svc, tmp_path)
    entered, go = threading.Event(), threading.Event()
    built: list[TurnRunner] = []

    class HeldFirst(service_module.TurnRunner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            built.append(self)
            if len(built) == 1:
                entered.set()
                assert go.wait(30)

    monkeypatch.setattr(service_module, "TurnRunner", HeldFirst)
    outcome: dict = {}

    def op():
        outcome["answer"] = answer(svc, turn["approval"])

    first, second = (svc._adopt_runners, op) if held == "the control loop" else (op, svc._adopt_runners)
    holder = threading.Thread(target=first)
    holder.start()
    assert entered.wait(30)
    second()                                # adopts and registers while the first is held
    go.set()
    holder.join(30)
    assert not holder.is_alive()
    (registered,) = svc.runners.values()
    assert quiet == [registered] and len(built) == 2 and registered is built[1]
    assert outcome["answer"]["approval"]["state"] == "answered"
    assert ("respond", "req-1", "allow", None, None) in commands(registered)


def test_concurrent_adoptions_of_one_attempt_start_exactly_one_runner(svc, tmp_path, quiet):
    """I2 under real threads: every round, eight threads adopt one fresh attempt at once,
    through the pass, an op, a stop and a steer's host lookup."""
    as_person(svc)
    for n in range(40):
        turn = left_running(svc, tmp_path, n=n)
        attempt = svc.daemon.store.one("SELECT * FROM attempts WHERE attempt_id=?", (turn["aid"],))
        barrier = threading.Barrier(8)
        paths = [svc._adopt_runners, lambda: svc._adopt_now(turn["aid"]),
                 lambda: svc.stop(attempt, svc.daemon._job(turn["job_id"]), "wall-limit"),
                 lambda: svc._adopt_message_turn(svc.store.message(turn["mid"]))] * 2
        errors: list[BaseException] = []

        def run(path):
            try:
                barrier.wait(10)
                path()
            except BaseException as exc:   # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(path,)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        mine = [runner for runner in quiet if runner.attempt_id == turn["aid"]]
        assert mine == [svc.runners[turn["aid"]]], f"round {n}: {len(mine)} runners started"


# --- I1 and I2 over random sequences --------------------------------------------------

STEPS = st.lists(st.sampled_from(["pass", "answer", "restart", "end", "thread-limit", "threads-back", "reap",
                                  "steer", "stop"]), min_size=1, max_size=12)


@hypothesis.settings(max_examples=60, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture])
@hypothesis.given(steps=STEPS)
def test_a_live_attempts_pending_approval_is_never_refused_turn_ended(tmp_path_factory, monkeypatch, steps):
    """I1 and I2 for any order of the control loop's pass, the person's answer, a
    restart (a new service on the same state root, no runner), the attempt ending,
    a thread limit coming and going, reaping, a steer and a stop."""
    tmp_path = tmp_path_factory.mktemp("adopt")
    root = tmp_path / "state"
    root.mkdir()
    started: list[TurnRunner] = []
    limited = {"on": False}

    def start(runner):
        if limited["on"]:
            raise RuntimeError("can't start new thread")
        started.append(runner)

    monkeypatch.setattr(TurnRunner, "start", start)
    daemon = FakeDaemon(root)
    services: list[ConversationService] = []

    def new_service() -> ConversationService:
        service = ConversationService(daemon)
        service.test_workspace = str(tmp_path)
        as_person(service)
        services.append(service)
        return service

    svc = new_service()
    try:
        turn = asking(svc, tmp_path)
        live, answered, last = True, False, turn["mid"]
        for step in steps:
            if step == "pass":
                try:
                    svc._adopt_runners()
                except RuntimeError:
                    assert limited["on"]
            elif step == "answer":
                try:
                    result = answer(svc, turn["approval"])
                except ConversationError as exc:
                    assert exc.reason != "turn-ended" or not live, (steps, "I1")
                    if live and not answered:
                        assert exc.reason == "turn-not-adopted" and limited["on"], (steps, exc.reason)
                else:
                    assert result["approval"]["state"] == "answered"
                    answered = True
            elif step == "restart":
                svc.close()
                svc = new_service()
                turn["approval"] = svc.store.approval(turn["approval"]["approval_id"])
            elif step == "end":
                with daemon.store.transaction("test.ended") as tx:
                    tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (turn["aid"],))
                live = False
            elif step == "thread-limit":
                limited["on"] = True
            elif step == "threads-back":
                limited["on"] = False
            elif step == "reap":
                svc._reap_runners()
            elif step == "steer":
                last = submit(svc, turn["cid"], "steer", after=last)
                with pytest.raises(ConversationError):
                    svc.op_message_steer({"message_id": last}, None)     # the runner has not caught up
                svc.store.set_state(last, "cancelled")
            elif step == "stop":
                attempt = daemon.store.one("SELECT * FROM attempts WHERE attempt_id=?", (turn["aid"],))
                svc.stop(attempt, daemon._job(turn["job_id"]), "wall-limit")
            # I2: in this service, the only runner started for the attempt is the registered one.
            mine = [r for r in started if r.attempt_id == turn["aid"] and r.store is svc.store]
            assert mine == ([svc.runners[turn["aid"]]] if turn["aid"] in svc.runners else []), (steps, "I2")
    finally:
        for service in services:
            service.close()
        daemon.store.close()
