"""C-24.9 / steer design §5: generated histories exercise the real store,
settlement, drivers and runner. Only relay I/O is replaced by a durable journal,
and the provider is the test, writing what it would write to stdout.
"""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.service import ConversationService
from subfleet.relay import Ack, frame_sha256, read_log
from tests.unit.test_claude_turn import INIT_OK, spec
from tests.unit import test_codex_turn as codex_tests
from tests.unit.test_conversation_service import CODEX_SETTINGS, FakeDaemon, conversation, submit


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix="steer-properties-") as directory:
        root = Path(directory)
        daemon = FakeDaemon(root)
        service = ConversationService(daemon)
        service.test_workspace = directory
        cid = conversation(service)
        host = submit(service, cid, "host")
        service.store.set_state(host, "running")
        adir = root / "attempt"
        adir.mkdir()
        context = SimpleNamespace(service=service, cid=cid, host=host, adir=adir)
        try:
            yield context
        finally:
            service.close()
            daemon.store.close()


def children(w, count):
    mids, previous = [], w.host
    for n in range(count):
        mid = submit(w.service, w.cid, f"correction {n}", after=previous)
        w.service.store.claim_steer(mid, w.host)
        mids.append(mid)
        previous = mid
    return mids


def settlement_runner(w):
    return SimpleNamespace(message_id=w.host, conversation_id=w.cid, attempt_id="job/a1", offset=50, next_seq=2)


# --- invariant 1: generated provider histories through the drivers, runner and store ----------
#
# Each steer gets what the provider does with it; the history runs through the real driver,
# the runner (relay journal, `turn.json`) and the service's settlement. The expectation is the
# provider's side of it, not the settlement's branches: what the provider took is steered and
# never requeued, what never reached it or it refused goes back to the queue, and what it
# took silently (no evidence either way) waits for a person as delivery-unknown.

CLAUDE_FATES = ["fold", "own-turn", "cancelled", "refused", "silent", "withdrawn"]
CODEX_FATES = ["answered", "unanswered", "error", "accepted-silent", "silent", "withdrawn"]


def claude_history(host, fates):
    """stdout for the host's turn and its steers, and what each steer's settlement must be."""
    rows, expected = [], {}
    for mid, fate in fates:
        if fate == "withdrawn":
            expected[mid] = ("cancelled", None)
            continue
        rows.append({"type": "command_lifecycle", "command_uuid": mid, "state": "queued"})
        if fate == "fold":
            rows += [{"type": "command_lifecycle", "command_uuid": mid, "state": s} for s in ("started", "completed")]
            expected[mid] = ("steered", f"steered:{host}")
        elif fate in ("cancelled", "refused"):
            rows.append({"type": "command_lifecycle", "command_uuid": mid, "state": fate})
            expected[mid] = ("queued", None)
        elif fate == "own-turn":
            expected[mid] = ("steered", f"steered:{host}")
        else:
            expected[mid] = ("delivery-unknown", None)
    folds = [mid for mid, fate in fates if fate == "fold"]
    owns = [mid for mid, fate in fates if fate == "own-turn"]
    rows.append({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                 "user_message_uuids": [host, *folds], "queued_turn_count": len(owns)})
    for index, mid in enumerate(owns):                     # each missed the boundary: its own turn
        rows += [{"type": "command_lifecycle", "command_uuid": mid, "state": "started"},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "done",
                  "user_message_uuids": [mid], "queued_turn_count": len(owns) - index - 1},
                 {"type": "command_lifecycle", "command_uuid": mid, "state": "completed"}]
    return rows, expected


def codex_history(host, fates):
    rows, expected, answered_after = [], {}, set()
    for index, (mid, fate) in enumerate(fates):
        if fate == "withdrawn":
            expected[mid] = ("cancelled", None)
            continue
        if fate == "silent":
            expected[mid] = ("delivery-unknown", None)
            continue
        if fate == "error":
            rows.append({"id": f"steer:{mid}", "error": {"code": -32600, "message": "no active turn"}})
            expected[mid] = ("queued", None)
            continue
        rows.append({"id": f"steer:{mid}", "result": {"turnId": "turn-1"}})
        if fate == "accepted-silent":                      # accepted, never echoed; the turn completes
            expected[mid] = ("delivery-unknown", None)
            continue
        rows.append({"method": "item/completed", "params": {"threadId": "thr-1", "turnId": "turn-1", "item": {
            "type": "userMessage", "id": f"u-{index}", "clientId": mid}}})
        if fate == "answered":
            rows.append({"method": "item/completed", "params": {"threadId": "thr-1", "turnId": "turn-1", "item": {
                "type": "agentMessage", "id": f"a-{index}", "text": "on it"}}})
            answered_after.update(m for m, f in expected.items() if f == ("steered", "unanswered"))
        expected[mid] = ("steered", "unanswered" if fate == "unanswered" else None)
    # An agent item after an echo answers it, whichever steer's it is.
    for mid, fate in list(expected.items()):
        if fate == ("steered", "unanswered"):
            expected[mid] = ("steered", f"steered:{host}" if mid in answered_after else f"steered-unanswered:{host}")
        elif fate == ("steered", None):
            expected[mid] = ("steered", f"steered:{host}")
    rows.append({"method": "turn/completed", "params": {"threadId": "thr-1", "turn": {"id": "turn-1",
                                                                                      "status": "completed"}}})
    return rows, expected


def history_world(w, provider):
    if provider == "codex":
        w.cid = conversation(w.service, provider="codex", settings=CODEX_SETTINGS)
        w.host = submit(w.service, w.cid, "host")
        w.service.store.set_state(w.host, "running")
    spec_ = spec(message_id=w.host) if provider == "claude" else codex_tests.spec(message_id=w.host)
    r = TurnRunner(store=w.service.store, attempt={"attempt_id": "job/a1", "lane_id": f"{provider}-1"},
                   spec=spec_, conversation_id=w.cid, attempt_dir=w.adir, control_socket=str(w.adir / "unused.sock"),
                   on_outcome=w.service._on_outcome, on_contain=lambda _: None)
    r.relay = JournalRelay(w.adir)
    r.handshaken = r.handshake_done_once = r.replay_caught_up = True
    r._restore_steers()
    r._apply(r.driver.start())
    provider_ = Provider(w, provider)
    provider_.until_steerable()
    offset = 0
    for line in provider_.stdout.read_text().splitlines():
        r._apply(r.driver.feed(line, offset))
        offset += len(line) + 1
    assert r.steerable
    return r, offset


@settings(max_examples=60, deadline=None)
@given(provider=st.sampled_from(["claude", "codex"]), picks=st.lists(st.integers(0, 5), min_size=1, max_size=4),
       repeats=st.integers(1, 3))
def test_every_steer_settles_once_as_the_provider_s_evidence_says(provider, picks, repeats):
    """Invariant 1: every steered message ends in exactly one of steered, queued,
    cancelled or delivery-unknown; one the provider took is never requeued, one that
    never reached it never counts as delivered; each is written at most once; and
    settling again changes nothing, neither the rows nor the change and event history."""
    import uuid
    with world() as w:
        r, offset = history_world(w, provider)
        names = CLAUDE_FATES if provider == "claude" else CODEX_FATES
        fates, previous = [], w.host
        for pick in picks:
            mid = str(uuid.uuid4())
            w.service.store.submit_message(conversation_id=w.cid, message_id=mid, after_message_id=previous,
                                           text="correction", attachments=[],
                                           settings=w.service.store.message(w.host)["settings"])
            previous = mid
            w.service.store.claim_steer(mid, w.host)
            if names[pick] == "withdrawn":                 # the person's cancel before handover
                assert w.service.store.withdraw(mid, expect=("steering",), stop_at="test")
            r.steer(mid)
            r._drain_commands()
            fates.append((mid, names[pick]))
        rows, expected = (claude_history if provider == "claude" else codex_history)(w.host, fates)
        for row in rows:
            line = json.dumps(row)
            r._apply(r.driver.feed(line, offset))
            offset += len(line) + 1
        if r.driver.outcome is None:
            r._apply(r.driver.eof(offset))                # the process ended (its steers still held)
        r._report()
        states = {mid: w.service.store.message(mid) for mid, _ in fates}
        assert {mid: (row["state"], row["state_reason"] if row["state"] == "steered" else None)
                for mid, row in states.items()} == expected, fates
        tags = [row["tag"] for row in read_log(w.adir / "stdin.jsonl")]
        for mid, fate in fates:
            assert tags.count(f"steer:{mid}") == (0 if fate == "withdrawn" else 1), (mid, fate)
            assert states[mid]["job_id"] is None
            if states[mid]["state"] == "steered":
                assert states[mid]["served"]["steered_into"] == w.host
        assert w.service.store.steers(w.host) == []
        changes = w.service.store.query("SELECT * FROM changes ORDER BY seq")
        events = w.service.store.events_after(w.cid, 0)
        for _ in range(repeats):
            w.service._on_outcome(r)
        assert {mid: w.service.store.message(mid) for mid, _ in fates} == states
        assert w.service.store.query("SELECT * FROM changes ORDER BY seq") == changes
        assert w.service.store.events_after(w.cid, 0) == events


@settings(max_examples=50, deadline=None)
@given(kinds=st.lists(st.sampled_from(["later", "missed", "steered"]), min_size=1, max_size=10),
       repair=st.booleans())
def test_missed_steers_keep_their_sequence_and_run_next(kinds, repair):
    """Invariant 2 (DESIGN.md sections 5, 8 and 9): a missed steer returns with its
    original seq and runs next: behind a repair message only, ahead of every message
    queued for later (sent before or after it), and in sequence among missed steers.
    A delivered steer never runs again."""
    import uuid
    with world() as w:
        previous, mids = w.host, []
        for kind in kinds:
            mid = submit(w.service, w.cid, kind, after=previous)
            if kind != "later":
                w.service.store.claim_steer(mid, w.host)
            mids.append((mid, kind))
            previous = mid
        seqs = {mid: w.service.store.message(mid)["seq"] for mid, _ in mids}
        evidence = {mid: {"frame": "written", "fate": "refused" if kind == "missed" else "consumed"}
                    for mid, kind in mids if kind != "later"}
        w.service._settle_steers(settlement_runner(w), {"steers": evidence}, {})
        expected = [m for m, k in mids if k == "missed"] + [m for m, k in mids if k == "later"]
        if repair:
            note = str(uuid.uuid4())
            w.service.store.submit_message(conversation_id=w.cid, message_id=note, after_message_id=previous,
                                           text="note", attachments=[], settings=w.service.store.message(w.host)[
                                               "settings"], origin="unblock-note")
            expected.insert(0, note)
        assert w.service.store.next_dispatchable(w.cid) == []  # host still live
        w.service.store.set_state(w.host, "complete")
        order = []
        while nxt := w.service.store.next_dispatchable(w.cid):
            order.append(nxt[0]["message_id"])
            w.service.store.set_state(order[-1], "complete")
        assert order == expected
        assert {mid: w.service.store.message(mid)["seq"] for mid, _ in mids} == seqs


class JournalRelay:
    """The guardian's persisted intent/written records, without a provider pipe."""

    def __init__(self, adir):
        self.path = adir / "stdin.jsonl"

    def send(self, seq, op, *, line=None, tag=None, sig=None):
        logged = read_log(self.path)
        assert seq == len(logged) + 1
        with self.path.open("a") as stream:
            stream.write(json.dumps({"kind": "intent", "seq": seq, "op": op, "tag": tag,
                                     "sha256": frame_sha256(op, line, sig), "line": line}) + "\n")
            stream.write(json.dumps({"kind": "written", "seq": seq}) + "\n")
        return Ack(seq, True)

    def close(self):
        pass


def runner(w, stdout=()):
    r = TurnRunner(store=w.service.store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"},
                   spec=spec(message_id=w.host), conversation_id=w.cid, attempt_dir=w.adir,
                   control_socket=str(w.adir / "unused.sock"), on_outcome=lambda _: None,
                   on_contain=lambda _: None)
    r.relay = JournalRelay(w.adir)
    r.handshaken = r.handshake_done_once = r.replay_caught_up = True
    r._restore_steers()
    r.driver.start()
    r.driver.feed(INIT_OK, 0)
    r.driver.feed(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5",
                              "capabilities": ["msg_lifecycle_v1", "interrupt_cancel_queued_v1"]}), 1)
    for offset, row in enumerate(stdout, 20):
        r._apply(r.driver.feed(json.dumps(row), offset))
    return r


@settings(max_examples=45, deadline=None)
@given(barrier=st.sampled_from(["close", "stop", "cancel"]), queued=st.integers(1, 6),
       sends=st.integers(1, 5))
def test_no_steer_frame_is_written_after_close_stop_or_its_cancel(barrier, queued, sends):
    """Invariant 3: even already-built outbox frames recheck the durable barrier."""
    with world() as w:
        mids = children(w, queued)
        r = runner(w)
        for mid in mids:
            r.outbox.extend(r.driver.steer(mid, "correction", ()).frames)
        if barrier == "close":
            r.sent["close"] = "written"
        elif barrier == "stop":
            w.service.store.update_message(w.host, stop_requested_at="test")
        else:
            for mid in mids:
                w.service.store.withdraw(mid, expect=("steering",), stop_at="test")
        for _ in range(sends):
            r._send_outbox()
        assert [row for row in read_log(w.adir / "stdin.jsonl") if row["tag"].startswith("steer:")] == []
        assert [w.service.store.message(mid)["state"] for mid in mids] == [
            "cancelled" if barrier == "cancel" else "queued"] * queued


@settings(max_examples=65, deadline=None)
@given(restarts=st.sets(st.integers(0, 4)), delivered=st.booleans(), repeats=st.integers(1, 4))
def test_restarts_at_each_handover_and_settlement_boundary_never_resend(restarts, delivered, repeats):
    """Invariant 4: replay before/after claiming, framing, writing, consuming and
    settlement has the uninterrupted fate and exactly one provider write."""
    with world() as w:
        [mid] = children(w, 1)
        r = runner(w)
        stdout = []
        for boundary in range(5):
            if boundary in restarts:
                r = runner(w, stdout)
            if boundary == 0:
                r.outbox.extend(r.driver.steer(mid, "correction", ()).frames)
            elif boundary == 1:
                if not r.outbox and not r.steer_written(mid):
                    r.outbox.extend(r.driver.steer(mid, "correction", ()).frames)
                r._send_outbox()
            elif boundary == 2:
                stdout.append({"type": "command_lifecycle", "command_uuid": mid,
                               "state": "started" if delivered else "cancelled"})
                r._apply(r.driver.feed(json.dumps(stdout[-1]), 20))
            elif boundary == 3:
                for _ in range(repeats):
                    r.steer(mid)
                r._drain_commands()
            else:
                w.service._settle_steers(r, {"steers": r.driver.steers}, {})
        assert w.service.store.message(mid)["state"] == ("steered" if delivered else "queued")
        assert len([row for row in read_log(w.adir / "stdin.jsonl") if row["tag"] == f"steer:{mid}"]) == 1
        before = w.service.store.message(mid)
        for _ in range(repeats):
            w.service._settle_steers(r, {"steers": r.driver.steers}, {})
        assert w.service.store.message(mid) == before


@settings(max_examples=45, deadline=None)
@given(ack=st.booleans(), success=st.booleans(), replay=st.integers(0, 4))
def test_hosts_without_steers_keep_their_delivery_and_outcome_rules(ack, success, replay):
    """Invariant 5: an ordinary host still closes on its first result, and replay
    preserves its acknowledgement, outcome, empty steer set and exactly one close."""
    rows = [INIT_OK]
    if ack:
        rows.append(json.dumps({"type": "user", "uuid": spec().message_id,
                                "message": {"role": "user", "content": "host"}}))
    rows.append(json.dumps({"type": "result", "subtype": "success", "is_error": not success,
                            "result": "done" if success else "provider failed"}))
    reference = None
    for _ in range(replay + 1):
        turn = ClaudeTurn(spec(), read_bytes=lambda _: b"")
        frames = list(turn.start().frames)
        for offset, row in enumerate(rows):
            frames.extend(turn.feed(row, offset).frames)
        outcome = asdict(turn.outcome)
        assert outcome["state"] == ("complete" if success else "failed")
        assert outcome["accepted"] == ack and outcome["steers"] == {}
        assert len([f for f in frames if f.tag == "user-message"]) == 1
        assert len([f for f in frames if f.tag == "close"]) == 1
        reference = reference or outcome
        assert outcome == reference


# --- invariant 4 through the runner's own loop (`TurnRunner._run`) ------------------------------
#
# A daemon restart is a new runner for the same attempt: it replays stdout from the start,
# primes steers the relay log shows written, and re-issues a claim whose frame was never
# written only once the replay has caught up (runner.py, `_run`). Each case runs the real
# loop on its thread, with the test as the provider, and compares a run restarted at a
# boundary with an uninterrupted one.

import threading
import time

from tests.unit import test_claude_turn as claude_tests

CLAUDE_CAPS = {"type": "system", "subtype": "init", "model": "claude-opus-5-5",
               "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1", "interrupt_cancel_queued_v1"]}


def wait_for(predicate, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class Provider:
    """The test as the provider: stdout lines it writes, and the steer's answers."""

    def __init__(self, w, provider):
        self.w, self.provider = w, provider
        self.stdout = w.adir / "stdout"
        self.stdout.write_text("")

    def write(self, *rows):
        with self.stdout.open("a") as stream:
            for row in rows:
                stream.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def until_steerable(self):
        if self.provider == "claude":
            self.write(claude_tests.INIT_OK, CLAUDE_CAPS,
                       {"type": "user", "uuid": self.w.host, "message": {"role": "user", "content": "host"}})
        else:
            self.write(codex_tests.resp(1, {"userAgent": "x"}), codex_tests.resp(2, codex_tests.hooks_ok()),
                       codex_tests.resp(3, codex_tests.MODELS),
                       codex_tests.resp(4, {"thread": {"id": "thr-1", "status": {"type": "idle"}},
                                            "model": "gpt-6-astra"}),
                       codex_tests.resp(5, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}))

    def takes(self, mid):
        """The provider takes the steer into the running turn."""
        if self.provider == "claude":
            self.write({"type": "command_lifecycle", "command_uuid": mid, "state": "started"})
        else:
            self.write(codex_tests.resp(f"steer:{mid}", {"turnId": "turn-1"}),
                       codex_tests.note("item/completed", threadId="thr-1", turnId="turn-1",
                                        item={"type": "userMessage", "id": "u-1", "clientId": mid}))

    def finishes(self, mid):
        if self.provider == "claude":
            self.write({"type": "command_lifecycle", "command_uuid": mid, "state": "completed"},
                       {"type": "result", "subtype": "success", "is_error": False, "result": "done",
                        "user_message_uuids": [self.w.host, mid], "queued_turn_count": 0})
        else:
            self.write(codex_tests.note("item/completed", threadId="thr-1", turnId="turn-1",
                                        item={"type": "agentMessage", "id": "a-1", "text": "done"}),
                       codex_tests.note("turn/completed", threadId="thr-1",
                                        turn={"id": "turn-1", "status": "completed"}))
        (self.w.adir / "exit.json").write_text(json.dumps({"rc": 0}))


def live_runner(w, provider):
    spec_ = (spec(message_id=w.host) if provider == "claude"
             else codex_tests.spec(message_id=w.host))
    settled = threading.Event()

    def on_outcome(r):
        w.service._on_outcome(r)
        settled.set()
    r = TurnRunner(store=w.service.store, attempt={"attempt_id": "job/a1", "lane_id": f"{provider}-1"},
                   spec=spec_, conversation_id=w.cid, attempt_dir=w.adir,
                   control_socket=str(w.adir / "unused.sock"), on_outcome=on_outcome, on_contain=lambda _: None)
    r.relay = JournalRelay(w.adir)
    r.handshaken = r.handshake_done_once = True          # a journal relay has no status to ask
    r.settled = settled
    r.start()
    return r


def crash(r):
    """The daemon stops: the runner's thread ends where it is, nothing is settled."""
    r.stop()
    assert r.join(30)


def steer_run(provider, restart_at):
    """One steer, claimed while the host runs; the daemon restarts at `restart_at`
    (None: never). Returns what the store and the relay log end with."""
    with world() as w:
        if provider == "codex":
            w.cid = conversation(w.service, provider="codex", settings=CODEX_SETTINGS)
            w.host = submit(w.service, w.cid, "host")
            w.service.store.set_state(w.host, "running")
        mid = str(__import__("uuid").uuid4())
        w.service.store.submit_message(conversation_id=w.cid, message_id=mid, after_message_id=w.host,
                                       text="correction", attachments=[],
                                       settings=w.service.store.message(w.host)["settings"])
        provider_ = Provider(w, provider)
        provider_.until_steerable()
        r = live_runner(w, provider)
        wait_for(lambda: r.steerable)
        w.service.store.claim_steer(mid, w.host)           # the op's durable claim ...
        if restart_at == "claimed":
            crash(r)                                         # ... and the daemon stops before the command
            r = live_runner(w, provider)
        else:
            r.steer(mid)
        wait_for(lambda: any(row["tag"] == f"steer:{mid}" for row in read_log(w.adir / "stdin.jsonl")))
        if restart_at == "written":
            crash(r)
            r = live_runner(w, provider)
        provider_.takes(mid)
        wait_for(lambda: r.driver.steers.get(mid, {}).get("fate") in ("delivered", "unanswered", "consumed"))
        if restart_at == "delivered":
            crash(r)
            r = live_runner(w, provider)
            wait_for(lambda: r.driver.steers.get(mid, {}).get("fate") in ("delivered", "unanswered", "consumed"))
        provider_.finishes(mid)
        assert r.settled.wait(90) and r.join(30)
        tags = [row["tag"] for row in read_log(w.adir / "stdin.jsonl")]
        message, host = w.service.store.message(mid), w.service.store.message(w.host)
        return {"steer": (message["state"], message["state_reason"] == f"steered:{w.host}", message["seq"]),
                "host": host["state"], "steer_frames": tags.count(f"steer:{mid}"),
                "tags": sorted({tag.replace(mid, "<steer>") for tag in tags}), "user_frames": tags.count("user-message")}


@pytest.mark.parametrize("restart_at", ["claimed", "written", "delivered"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_restart_at_each_steer_boundary_through_the_runner_loop_settles_as_an_uninterrupted_run(provider,
                                                                                                  restart_at):
    """Invariant 4 (C-26.6): a restart after the claim and before the runner took the
    command (the claim is re-issued once replay catches up), after the frame was written
    (it is primed, never written again), or after the provider took it, settles as a run
    with no restart, with exactly one steer frame and one message frame."""
    reference = steer_run(provider, None)
    assert reference["steer"][:2] == ("steered", True) and reference["host"] == "complete"
    assert reference["steer_frames"] == 1 and reference["user_frames"] == 1
    restarted = steer_run(provider, restart_at)
    assert restarted["steer"][:2] == reference["steer"][:2] and restarted["host"] == reference["host"]
    assert restarted["steer_frames"] == 1 and restarted["user_frames"] == 1
    assert restarted["tags"] == reference["tags"]
