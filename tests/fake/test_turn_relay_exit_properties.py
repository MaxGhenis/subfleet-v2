"""C-26.4: generated relay-loss schedules and the stdout drain deadline."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.turn import Frame
from subfleet.conversations.titles import CLAIMED
from subfleet.relay import Ack
from tests.fake.test_turn_relay_exit_recovery import recovery as _recovery

# This is the public recovery budget, pinned independently of the implementation.
DRAIN_S = 30.0
case = contextmanager(_recovery.__wrapped__)


@st.composite
def schedules(draw):
    """All legal orders: bytes precede EOF; receipt publication follows exit.

    Refusal may precede exit, follow it, or race receipt publication. A receipt
    may never arrive. Byte splits include partial JSON lines and UTF-8 sequences.
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
    chunks = [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)])]
    publishes = draw(st.booleans())
    names = ["exit", "refusal", "eof", *(["receipt"] if publishes else []), *range(len(chunks))]
    order = list(draw(st.permutations(names)))
    # Topological sort of an arbitrary priority order preserves every possible
    # ordering of independent events without rejecting generated examples.
    pending, events, written = list(order), [], 0
    while pending:
        for event in pending:
            if event == "receipt" and "exit" not in events:
                continue
            if isinstance(event, int) and event != written:
                continue
            if event == "eof" and not all(x in events for x in ["exit", "refusal", *range(len(chunks))]):
                continue
            pending.remove(event)
            events.append(event)
            written += isinstance(event, int)
            break
    return text, result, chunks, events, draw(st.booleans())


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(schedule=schedules())
def test_loss_interleavings_keep_one_outcome_and_all_stdout(schedule):
    text, result, chunks, events, exit_evidence_available = schedule
    with tempfile.TemporaryDirectory(prefix="relay-property-") as directory, pytest.MonkeyPatch.context() as patch:
        with case(Path(directory), patch) as r:
            # Use the real relay and decoder, but schedule child exit ourselves.
            original_apply = r.server.apply
            writes_after_loss = []
            loss_unproved = []

            def exchange(payload, *, before_send=None):
                frame = json.loads(payload)
                if frame.get("op") != "status" and loss_unproved:
                    writes_after_loss.append(frame)
                if before_send:
                    before_send()
                return original_apply(frame)

            patch.setattr(r.runner.relay, "_exchange", exchange)
            r.runner._apply(r.runner.driver.start())
            assert r.runner._read_stdout()  # initialize, message and settings all acknowledged
            read = r.runner._read_stdout
            pending = list(events)
            eof = [False]
            started = r.now[0]
            terminal = []
            write_outcome = r.runner._write_outcome

            def record_terminal():
                if not terminal:
                    terminal.append((r.now[0], r.runner.driver.outcome))
                else:
                    assert r.runner.driver.outcome is terminal[0][1]
                write_outcome()

            patch.setattr(r.runner, "_write_outcome", record_terminal)

            def scheduled_read():
                r.now[0] += 0.01
                if pending:
                    event = pending.pop(0)
                    if event == "exit":
                        r.child.returncode = 1
                    elif event == "refusal":
                        proved = ((r.child.returncode is not None and exit_evidence_available)
                                  or (r.adir / "exit.json").exists())
                        loss_unproved.append(not proved)
                        r.runner._relay_lost("scheduled refusal", child_exited=proved)
                    elif event == "receipt":
                        (r.adir / "exit.json").write_text('{"rc":0}')
                    elif event == "eof":
                        eof[0] = True
                    else:
                        with (r.adir / "stdout").open("ab") as stream:
                            stream.write(chunks[event])
                progressed = read()
                # Model a stdout writer still open between chunks. Production
                # tails a regular file; continuous writes exercise the same loop.
                return progressed or not eof[0]

            def advance(_):
                r.now[0] += 1
                # A live-child loss followed by a result retains the existing
                # post-result cleanup clock. The terminal outcome is durable
                # already; final notification follows containment's receipt.
                if r.now[0] - started > DRAIN_S + r.runner.clocks.after_result_s + r.runner.clocks.contain_after_s:
                    r.runner.stop()  # a failing implementation cannot hang the property

            patch.setattr(runner_module.time, "sleep", advance)
            patch.setattr(r.runner, "_read_stdout", scheduled_read)
            r.runner._run()
            assert r.reported == [r.runner]
            r.runner._report()
            assert r.reported == [r.runner]  # notification is idempotent
            assert len(terminal) == 1
            assert terminal[0][0] - started <= DRAIN_S + r.runner.clocks.contain_after_s
            assert r.now[0] - started <= DRAIN_S + r.runner.clocks.after_result_s + r.runner.clocks.contain_after_s
            # The mandatory unproved-exit control requires relay_failed even
            # when the child actually exited but its reply/status proof is lost.
            assert r.runner.relay_failed is loss_unproved[0]
            assert writes_after_loss == []
            assert r.runner.offset == (r.adir / "stdout").stat().st_size
            assert r.runner.final_text == text
            turn = json.loads((r.adir / "turn.json").read_text())
            assert turn["final_text"] == text
            assert r.runner.driver.outcome.reason == (None if result else "ended-without-result")


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
