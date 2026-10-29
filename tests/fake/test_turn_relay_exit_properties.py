"""C-26.4: generated relay-loss schedules and the stdout drain deadline.

The schedules (fix r2) draw zero, one or more refused frames (`failed` or `closed`,
with or without the relay's exit evidence), daemon restarts (a runner rebuilt from
the relay log, its status and stdout, some replaying an owner-ended attempt), EOF
before or after the other events, a writer that stays open for a while (a block
of ticks), and readers slow enough that bytes are already on disk when a runner
enters its exit drain.
"""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.store import ConversationError
from subfleet.conversations.turn import Frame
from subfleet.conversations.titles import CLAIMED
from subfleet.relay import Ack
from tests.fake.test_turn_relay_exit_recovery import recovery as _recovery

# This is the public recovery budget, pinned independently of the implementation.
DRAIN_S = 30.0
case = contextmanager(_recovery.__wrapped__)
# A runner still going this long after it started has hung: it is stopped, and the
# report assertion fails. The longest legal runner reads about 1.2 kB at 8 bytes a
# fake second, then waits out `after_result_s` (135 s) and its late containment.
ESCAPE_S = 1000.0
# The fake clock sums 0.01 s steps: the oracle's upper bounds allow its rounding.
EPSILON = 1e-6


class Restart(ConversationError):
    """A daemon restart: the runner's thread ends with nothing reported. It is the
    refusal a closing service's runner meets (`store-closed`), which a runner that
    ends on an exception leaves to a later daemon rather than settling (fix r3)."""

    def __init__(self, message: str):
        super().__init__("store-closed", message, code=1)


@dataclass(frozen=True)
class Schedule:
    text: str
    result: bool
    chunks: tuple
    events: tuple
    evidence: bool          # `child_exited` replies and `status`; False: a version 1 relay with neither
    read_chunk: int         # bytes per stdout read
    read_cost: float        # fake seconds per stdout read
    forced_receipt: bool    # nothing else could end the turn, so the guardian publishes last


def _ends_without_a_receipt(events, result, evidence):
    """Whether the last runner learns the turn ended without a receipt: from the
    provider's result (its late containment), a refused frame after the last
    restart, or at that restart from an owner-ended replay, a status naming the
    child exited, or the log's unwritten frame (the first refusal was `failed`)."""
    if result:
        return True
    restarts = [index for index, event in enumerate(events) if event[0] == "restart"]
    refusals = [index for index, event in enumerate(events) if event[0] == "refusal"]
    last = restarts[-1] if restarts else -1
    if any(index > last for index in refusals):
        return True
    if last < 0:
        return False
    exited = events.index(("exit",)) < last
    return exited and (events[last][2] or evidence) or bool(refusals) and events[refusals[0]][2] == "failed"


@st.composite
def schedules(draw):
    """All legal orders: bytes precede EOF; receipt publication follows exit.

    Refusals, restarts, the exit, the receipt and EOF come in any order, EOF
    included before all the others. A block of reads with the writer still open
    and nothing new (ticks; at 0.5 s or more a read, longer than the bound) can
    come anywhere, often just before EOF or just after a restart. Byte splits
    include partial JSON lines and UTF-8 sequences. A receipt may never arrive,
    unless nothing else could end the turn: a runner with no proof treats the
    child as live (C-26.4).
    """
    # Display text is stripped at its edges by the existing scrubber. Keep
    # generated whitespace inside sentinels so loss checks compare exact text.
    text = "[" + draw(st.text(alphabet="abc λ🙂\n", min_size=1, max_size=60)) + "]"
    result = draw(st.booleans())
    rows = [{"type": "assistant", "message": {"id": "answer", "content": [{"type": "text", "text": text}]}}]
    if result:
        rows.append({"type": "result", "subtype": "success", "is_error": False, "result": text})
    data = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows)
    cuts = sorted(draw(st.sets(st.integers(1, len(data) - 1), max_size=5)))
    chunks = tuple(data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)]))
    refusals = draw(st.lists(st.sampled_from(["failed", "closed"]), max_size=3))
    restarts = draw(st.lists(st.booleans(), max_size=2))   # each: replay owner-ended, once the child exited
    publishes = draw(st.booleans())
    names = [("exit",), ("eof",), *([("receipt",)] if publishes else []),
             *(("refusal", index, kind) for index, kind in enumerate(refusals)),
             *(("restart", index, ended) for index, ended in enumerate(restarts)),
             *(("chunk", index) for index in range(len(chunks)))]
    order = list(draw(st.permutations(names)))
    # Topological sort of an arbitrary priority order preserves every possible
    # ordering of independent events without rejecting generated examples.
    pending, events, written = list(order), [], 0
    while pending:
        for event in pending:
            if event == ("receipt",) and ("exit",) not in events:
                continue
            if event[0] == "chunk" and event[1] != written:
                continue
            if event == ("eof",) and written < len(chunks):
                continue
            pending.remove(event)
            events.append(event)
            written += event[0] == "chunk"
            break
    evidence = draw(st.booleans())
    forced = not publishes and not _ends_without_a_receipt(events, result, evidence)
    if forced:
        events.append(("receipt",))
    # Anywhere, or where it outlasts a drain: just before EOF, or just after the
    # last restart (an owner-ended replay drains from its start).
    spots = [events.index(("eof",)), *[index + 1 for index, event in enumerate(events)
                                       if event[0] == "restart"][-1:]]
    at = draw(st.one_of(st.sampled_from(spots), st.integers(0, len(events))))
    linger = draw(st.sampled_from([0, 70]))
    events[at:at] = [("tick", index) for index in range(linger)]
    return Schedule(text=text, result=result, chunks=chunks, events=tuple(events), evidence=evidence,
                    read_chunk=draw(st.sampled_from([8, 64, 1 << 20])),
                    read_cost=draw(st.sampled_from([0.01, 0.5, 1.0])), forced_receipt=forced)


def _rebuild(r, old, *, ended):
    """The runner a restarted daemon builds for the attempt (C-26.6)."""
    return TurnRunner(store=r.store, attempt=old.attempt, spec=old.spec, conversation_id=r.cid,
                      attempt_dir=r.adir, control_socket=old.control_socket, on_outcome=r.reported.append,
                      on_contain=old.on_contain, clocks=old.clocks, clock=lambda: r.now[0], ended=ended)


def _position(runner):
    return runner.offset + len(runner.partial)


def _play(s, seen):
    """Run one schedule through the real runner, relay and decoder, check the four
    properties, and count into `seen` what the schedule exercised."""
    with tempfile.TemporaryDirectory(prefix="relay-property-") as directory, pytest.MonkeyPatch.context() as patch:
        with case(Path(directory), patch) as r:
            stdout = r.adir / "stdout"
            r.child.send_signal = lambda signum: None   # a live child takes the late SIGINT
            original_apply = r.server.apply
            runners, losses, drains, outcomes, frames_after_loss, kinds = [], {}, [], [], [], []
            pending, eof, restart = list(s.events), [False], [None]
            started, current = [r.now[0]], [r.runner]
            exercised = Counter()

            def failed_write(data):
                raise BrokenPipeError(32, "fake stdin reader gone")

            def connect(runner):
                """Every exchange of this runner goes to the one real relay."""
                def exchange(payload, *, before_send=None):
                    frame = json.loads(payload)
                    if frame.get("op") == "status" and not s.evidence:
                        return {"ok": False, "error": "bad-frame"}   # version 1: no status
                    if frame.get("op") != "status" and runner in losses:
                        frames_after_loss.append((runners.index(runner), frame.get("tag")))
                    if before_send:
                        before_send()
                    answer = original_apply(frame)
                    if not s.evidence:
                        answer.pop("child_exited", None)
                    return answer

                patch.setattr(runner.relay, "_exchange", exchange)

            def watch(runner):
                """Spies recording, from the model's own state, what the properties need."""
                relay_lost, drain = runner._relay_lost, runner._drain_stdout
                write_outcome, read = runner._write_outcome, runner._read_stdout

                def lost(why, *, child_exited=False):
                    if runner not in losses:
                        # Proof as the model knows it, not as the runner decided.
                        proved = ((r.child.returncode is not None and s.evidence)
                                  or (r.adir / "exit.json").exists() or runner.ended)
                        losses[runner] = (proved, r.now[0], runner.driver.outcome is None)
                    relay_lost(why, child_exited=child_exited)

                def drained():
                    size = stdout.stat().st_size
                    record = {"runner": runner, "entered": r.now[0], "size": size, "position": _position(runner),
                              "caught_up": r.now[0] if _position(runner) >= size else None, "interrupted": True,
                              "entry": ("proof" if runner.exit_proved_at is not None
                                        else "receipt" if (r.adir / "exit.json").exists() else "owner-ended")}
                    drains.append(record)
                    try:
                        drain()
                        record["interrupted"] = False
                    finally:
                        record.update(left=r.now[0], end=_position(runner), contained=runner.contained)

                def wrote():
                    outcomes.append((runner, r.now[0], runner.driver.outcome))
                    write_outcome()

                def scheduled_read():
                    r.now[0] += s.read_cost
                    if pending:
                        play(pending.pop(0), runner)
                    progressed = read()
                    record = drains[-1] if drains else None
                    if (record is not None and record["runner"] is runner and "left" not in record
                            and record["caught_up"] is None and _position(runner) >= record["size"]):
                        record["caught_up"] = r.now[0]   # measured here, not taken from the runner
                    # Model a stdout writer still open between chunks. Production
                    # tails a regular file; continuous writes exercise the same loop.
                    return progressed or not eof[0]

                patch.setattr(runner, "_relay_lost", lost)
                patch.setattr(runner, "_drain_stdout", drained)
                patch.setattr(runner, "_write_outcome", wrote)
                patch.setattr(runner, "_read_stdout", scheduled_read)

            def play(event, runner):
                if event == ("exit",):
                    r.child.returncode = 1
                elif event == ("receipt",):
                    (r.adir / "exit.json").write_text('{"rc":0}')
                elif event == ("eof",):
                    eof[0] = True
                    exercised["eof-before-exit"] += r.child.returncode is None
                elif event[0] == "tick":
                    pass                                 # the writer is still open
                elif event[0] == "chunk":
                    with stdout.open("ab") as stream:
                        stream.write(s.chunks[event[1]])
                elif event[0] == "refusal":
                    kinds.append(event[2])
                    if event[2] == "failed":
                        patch.setattr(r.server, "_write_line", failed_write)
                    else:
                        r.server._closed = True         # a `closed` refusal logs nothing
                    # The runner's next frame, whatever produced it, meets the refusal.
                    runner.outbox.append(Frame(f"probe:{event[1]}", "write", "{}"))
                    runner._send_outbox()
                else:
                    exercised["restarts"] += 1
                    exercised["restart-after-closed"] += kinds[:1] == ["closed"]
                    restart[0] = event[2] and r.child.returncode is not None
                    raise Restart("SECRET7 simulated daemon restart")

            def advance(_):
                r.now[0] += 1
                if r.now[0] - started[0] > ESCAPE_S:
                    current[0].stop()   # a failing implementation cannot hang the property

            patch.setattr(runner_module.time, "sleep", advance)
            connect(r.runner)
            r.runner._apply(r.runner.driver.start())
            assert r.runner._read_stdout()  # initialize, message and settings all acknowledged
            patch.setattr(runner_module, "READ_CHUNK", s.read_chunk)
            while True:
                runner = current[0]
                runners.append(runner)
                if runner is not r.runner:
                    connect(runner)
                watch(runner)
                started[0], restart[0] = r.now[0], None
                runner._run()
                if restart[0] is None:
                    break
                current[0] = _rebuild(r, runner, ended=restart[0])

            final, last = runners[-1], drains[-1] if drains else None
            exercised["refusals"] = len(kinds)
            exercised["backlog"] = sum(record["size"] > record["position"] for record in drains)
            exercised["contained"] = sum(record["contained"] for record in drains)
            for record in drains:
                exercised[f"contained-after-{record['entry']}"] += record["contained"]
            exercised["owner-ended"] = sum(runner.ended for runner in runners)
            seen.update(name for name, count in exercised.items() if count)
            seen.update([f"refusals={min(len(kinds), 2)}", f"restarts={exercised['restarts']}",
                         "receipt-forced" if s.forced_receipt else "receipt" if ("receipt",) in s.events
                         else "receipt-never", "examples"])

            # 1. Exactly one terminal outcome, within the bound.
            assert r.reported == [final], {"runners": len(runners), "drains": drains, "now": r.now[0] - started[0]}
            final._report()
            assert r.reported == [final]  # notification is idempotent
            turn = json.loads((r.adir / "turn.json").read_text())
            assert all(outcome is final.driver.outcome for runner, _, outcome in outcomes if runner is final)
            assert all((outcome.state, outcome.reason) == (turn["state"], turn["reason"])
                       for _, _, outcome in outcomes)
            assert last is not None and last["runner"] is final and not last["interrupted"]
            assert outcomes[-1][1] <= last["left"]
            for record in drains:
                if record["caught_up"] is not None:
                    assert record["left"] - record["caught_up"] <= DRAIN_S + s.read_cost + EPSILON, record
                if record["contained"]:
                    # Never before the backlog on disk at entry was read in full,
                    # and then only once a writer outlived the bound.
                    assert record["end"] >= record["size"] and record["caught_up"] is not None, record
                    assert record["left"] - record["caught_up"] >= DRAIN_S, record
            proved, lost_at, before_outcome = losses.get(final, (True, None, False))
            if not proved and before_outcome:
                # An unproved loss is stopped as C-24.7 escalates: contained then,
                # unless the provider's result comes first; then D-15's late stop.
                clocks, step = final.clocks, s.read_cost + 1     # one loop pass: a read and a poll
                result_at = next((at for runner, at, _ in outcomes if runner is final), None)
                bound = lost_at + clocks.contain_after_s + step
                if result_at is not None and result_at < bound:
                    late = clocks.after_result_s + clocks.contain_after_s - clocks.sigint_after_s
                    # Two timer thresholds, the late SIGINT and its containment,
                    # each crossed up to one loop pass late.
                    bound = max(bound, result_at + late + 2 * step)
                assert last["entered"] <= bound + EPSILON
            # 2. No frame after a runner's relay I/O ended.
            assert frames_after_loss == []
            # 3. relay_failed iff that runner's loss was unproved: the mandatory
            # unproved-exit control requires it even when the child actually
            # exited but its reply/status proof is lost.
            for runner in runners:
                assert runner.relay_failed is (runner in losses and not losses[runner][0])
            # 4. Every stdout chunk on disk when the final drain began is in the
            # outcome, and all of stdout unless a writer outlived the bound.
            end, data = _position(final), stdout.read_bytes()
            assert end == len(data) if not final.contained else end >= last["size"]
            joined = b"".join(s.chunks)
            init = data.index(b"\n") + 1
            if end >= init + joined.index(b"\n") + 1:
                assert final.final_text == turn["final_text"] == s.text
            result_read = s.result and end >= init + len(joined)
            assert final.driver.outcome.reason == turn["reason"] == (None if result_read else "ended-without-result")


# Review r2's P2-1: a `closed` refusal proves the exit and logs nothing, no receipt
# ever comes, and the daemon restarts. Only the rebuilt runner's status can end it.
_ANSWER = json.dumps({"type": "assistant", "message": {"id": "answer", "content": [
    {"type": "text", "text": "[a]"}]}}) + "\n"
P2_1 = Schedule(text="[a]", result=False, chunks=(_ANSWER.encode(),),
                events=(("exit",), ("refusal", 0, "closed"), ("restart", 0, False), ("chunk", 0), ("eof",)),
                evidence=True, read_chunk=1 << 20, read_cost=0.01, forced_receipt=False)
# A writer outliving the bound after each kind of drain entry: a proof (a `closed`
# refusal's evidence), the receipt, and an owner-ended replay with no other proof.
_LINGER = tuple(("tick", index) for index in range(70))
BOUND_AFTER = {entry: Schedule(text="[a]", result=False, chunks=(_ANSWER.encode(),),
                               events=(*start, *_LINGER, ("chunk", 0), ("eof",)), evidence=evidence,
                               read_chunk=1 << 20, read_cost=1.0, forced_receipt=False)
               for entry, start, evidence in (
                   ("proof", (("exit",), ("refusal", 0, "closed")), True),
                   ("receipt", (("exit",), ("receipt",)), False),
                   ("owner-ended", (("exit",), ("restart", 0, True)), False))}


def test_loss_interleavings_keep_one_outcome_and_all_stdout():
    seen = Counter()

    @settings(max_examples=150, deadline=None, derandomize=True, database=None)
    @given(schedule=schedules())
    @example(schedule=P2_1)
    @example(schedule=BOUND_AFTER["proof"])
    @example(schedule=BOUND_AFTER["receipt"])
    @example(schedule=BOUND_AFTER["owner-ended"])
    def check(schedule):
        _play(schedule, seen)

    check()
    print("relay-exit schedules exercised:", json.dumps(dict(sorted(seen.items()))))
    # The widened generator must reach what the properties are for: a restart
    # (one after a `closed` refusal, which logs nothing), a backlog on disk at a
    # drain's entry, and (pinned above too) a writer outliving the bound after
    # each kind of entry.
    assert seen["restarts"] and seen["backlog"], dict(seen)
    assert seen["restart-after-closed"], dict(seen)
    assert all(seen[f"contained-after-{entry}"] for entry in ("proof", "receipt", "owner-ended")), dict(seen)


@pytest.mark.parametrize("reaches_eof", [True, False], ids=["before-bound", "at-bound"])
@settings(max_examples=20, deadline=None, derandomize=True, database=None)
@given(step=st.sampled_from([0.25, 0.5, 1.0, 2.0]))
def test_proved_exit_drain_is_bounded_on_the_fake_clock(reaches_eof, step):
    with tempfile.TemporaryDirectory(prefix="relay-bound-") as directory, pytest.MonkeyPatch.context() as patch:
        with case(Path(directory), patch) as r:
            # No real receipt, no live writer, and no real wait. The fake read
            # stays nonempty as a descendant writing stdout would keep it.
            r.runner._relay_lost("proved exit", child_exited=True)
            started = r.now[0]
            reads = []
            read = r.runner._read_stdout

            def still_writing():
                # Fix r2: a reader that reports a chunk has read what is on disk,
                # so the drain's clock, which starts once it catches up, can start.
                read()
                reads.append(r.now[0])
                r.now[0] += step
                if reaches_eof:
                    return r.now[0] - started < DRAIN_S - step
                # Escape even a removed bound; failure cannot hang pytest.
                return r.now[0] - started < DRAIN_S * 3

            patch.setattr(r.runner, "_read_stdout", still_writing)
            r.runner._run()
            assert r.runner._process_gone()
            assert r.reported == [r.runner]
            assert len(r.contained) == (0 if reaches_eof else 1)
            assert r.now[0] - started <= DRAIN_S + step
            assert not r.runner.relay_failed
            assert r.runner.driver.outcome.reason == "ended-without-result"
            assert r.runner.stop_reason == (None if reaches_eof else "stdout-drain-timeout")


@settings(max_examples=30, deadline=None, derandomize=True, database=None)
@given(exited=st.booleans(), tag=st.sampled_from([
    "init", "user-message", "settings", "steer:later", "approval:later", "close", "signal:int"]))
def test_every_frame_path_stays_closed_after_relay_loss(exited, tag):
    with tempfile.TemporaryDirectory(prefix="relay-writes-") as directory, pytest.MonkeyPatch.context() as patch:
        with case(Path(directory), patch) as r:
            sent = []
            def send(seq, *args, **kwargs):
                sent.append((seq, args, kwargs))
                return Ack(seq, False, error="failed", child_exited=exited)

            patch.setattr(r.runner.relay, "send", send)
            r.runner._relay_lost("scheduled loss", child_exited=exited)
            op = "close" if tag == "close" else "signal" if tag.startswith("signal:") else "write"
            frame = Frame(tag, op, "INT" if op == "signal" else "{}")
            r.runner.outbox = [frame]
            r.runner._send_outbox()
            r.runner._transmit(frame)  # also guard direct transmission and durable stop callbacks
            r.runner.title.state = CLAIMED
            r.runner._send_title()
            assert sent == []
            assert r.runner.relay_io_ended
            assert r.runner.relay_failed is (not exited)
            assert not r.runner.steerable
