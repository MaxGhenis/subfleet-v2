"""C-24.9 / steer design §5: generated histories exercise the real store,
settlement, drivers and runner. Only relay I/O is replaced by a durable journal.
"""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st

from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.service import ConversationService
from subfleet.relay import Ack, frame_sha256, read_log
from tests.unit.test_claude_turn import INIT_OK, spec
from tests.unit.test_conversation_service import FakeDaemon, conversation, submit


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


facts = st.tuples(st.sampled_from(["written", "unsent", "refused"]),
                  st.sampled_from(["consumed", "delivered", "unanswered", "cancelled", "refused", "unknown"]),
                  st.booleans())


@settings(max_examples=70, deadline=None)
@given(entries=st.lists(facts, min_size=1, max_size=8), repeats=st.integers(1, 5))
def test_every_claim_settles_exactly_once_and_delivery_never_requeues(entries, repeats):
    """Invariant 1: every claimed message has one disposition; settlement replay
    changes neither its state nor the watch/event history, even with mixed fates."""
    with world() as w:
        mids = children(w, len(entries))
        expected, evidence = {}, {}
        for mid, (frame, fate, cancelled) in zip(mids, entries):
            evidence[mid] = {"frame": frame, "fate": fate, "detail": "generated"}
            if cancelled:
                w.service.store.withdraw(mid, expect=("steering",), stop_at="test")
                expected[mid] = "cancelled"
            elif fate in ("consumed", "delivered", "unanswered"):
                expected[mid] = "steered"
            elif frame != "written" or fate in ("cancelled", "refused"):
                expected[mid] = "queued"
            else:
                expected[mid] = "delivery-unknown"
        runner = settlement_runner(w)
        w.service._settle_steers(runner, {"steers": evidence}, {})
        rows = {mid: w.service.store.message(mid) for mid in mids}
        changes = w.service.store.query("SELECT * FROM changes ORDER BY seq")
        events = w.service.store.events_after(w.cid, 0)
        for _ in range(repeats):
            w.service._settle_steers(runner, {"steers": evidence}, {})
        assert {mid: w.service.store.message(mid)["state"] for mid in mids} == expected
        assert {mid: w.service.store.message(mid) for mid in mids} == rows
        assert w.service.store.query("SELECT * FROM changes ORDER BY seq") == changes
        assert w.service.store.events_after(w.cid, 0) == events
        assert w.service.store.steers(w.host) == []
        for mid in mids:
            assert rows[mid]["job_id"] is None
            if expected[mid] == "steered":
                assert rows[mid]["served"]["steered_into"] == w.host


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
