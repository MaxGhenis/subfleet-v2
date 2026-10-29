"""Review #271 r4: the exit-in-progress window, measured on real children.

Run with pytest from the repository root; `testpaths` keeps CI from collecting it, since
it measures timing and asserts only what holds whatever the timing. A child closes its
stdin when the kernel tears its descriptors down, before the kernel makes it waitable,
so a stdin write can meet EPIPE while `waitid` still reads the child running. The relay
journals the failed write (an fsync) before its refusal peeks, and the runner's fallback
`status` follows, so the window matters only when it outlasts both.

Every child is a `/bin/sh` started and reaped here (a thread blocked in `child.wait()`
stands for the guardian's), one at a time.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import errno
import json
import os
import select
import sys
import threading
import time

import pytest

from subfleet import relay
from subfleet.conversations.reconcile import Evidence, settle
from tests.fake import test_turn_relay_exit_peek as peek
from tests.fake import test_turn_relay_exit_recovery as recovery
from tests.fake.test_turn_relay_exit_r4 import _guardian, _relay_for, _until

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="kqueue's EV_EOF on a pipe, and Darwin's exit")
CHILDREN = 40           # per measurement, one live at a time
ATTEMPTS = 8            # runner-level attempts, one child each


def _until_reader_closed(fd: int) -> float:
    """Block until the last read end of the pipe whose write end is `fd` has closed
    (kqueue reports EV_EOF on the write filter); the monotonic time it was seen."""
    kq = select.kqueue()
    try:
        change = [select.kevent(fd, filter=select.KQ_FILTER_WRITE, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)]
        deadline = time.monotonic() + peek.TIMEOUT_S
        while True:
            events = kq.control(change, 1, 0.05)
            change = None
            if any(event.flags & select.KQ_EV_EOF for event in events):
                return time.monotonic()
            assert time.monotonic() < deadline, "the child never closed its stdin"
    finally:
        kq.close()


def _ms(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))] * 1000, 3) if values else None


def test_how_long_a_closed_stdin_precedes_a_waitable_exit():
    """With Popen's lock held (no reaper), the time from the child's stdin closing to the
    peek first reading its exit."""
    gaps = []
    for n in range(CHILDREN):
        child, gate = peek.start("exit 3")
        fd, gate.fd = gate.fd, None
        try:
            with peek.wait_lock_held(child):
                os.write(fd, b"\n")
                closed_at = _until_reader_closed(fd)
                while not relay.child_exited(child):
                    pass
                gaps.append(time.monotonic() - closed_at)
            assert child.wait(peek.TIMEOUT_S) == 3
        finally:
            os.close(fd)
            peek.reap(child)
    print(f"stdin close to waitable exit ({relay.EXIT_PEEK}), ms: p50 {_ms(gaps, .5)} p90 {_ms(gaps, .9)} "
          f"max {_ms(gaps, 1)}; load {os.getloadavg()[0]:.1f}")


@pytest.mark.parametrize("unread", [False, True], ids=["nothing-unread", "a-frame-unread"])
def test_the_relay_reads_a_child_that_ended_on_its_own_at_its_broken_pipe(tmp_path, unread):
    """The refusal of the first write after the child's stdin closed, and a status read at
    once (the runner's fallback, `runner.py:965-972`), are the runner's evidence. The child
    reads one line and exits 3 with nothing else from here; with `a-frame-unread` a second
    frame lands in the pipe while it exits (or is itself refused). Both reading `running`
    is the D-RELAY-EXIT1 outcome (`relay-failed`); the count is reported, not asserted."""
    seen, gaps = Counter(), []
    for n in range(CHILDREN):
        child, gate = peek.start("exit 3")
        thread, waited = _guardian(child)
        (tmp_path / f"c{n}").mkdir()
        server = _relay_for(tmp_path / f"c{n}", child, gate)
        try:
            assert server.apply(peek._frame(1, "write", "{}")) == {"seq": 1, "ok": True}
            seq, refused, closed_at = 2, None, None
            if unread:
                second = server.apply(peek._frame(2, "write", "x" * 64))
                seq, refused = 3, (None if second["ok"] else second)
                seen["second-frame-refused"] += refused is not None
            if refused is None:
                closed_at = _until_reader_closed(server._pipe)
                refused = server.apply(peek._frame(seq, "write", "{}"))
            closed_at = closed_at or time.monotonic()
            status = server.status()["status"]["child"]
            assert refused["error"] == "failed" and refused["errno"] == errno.EPIPE, refused
            flagged = refused.get("child_exited") is True
            seen["refusal-flagged" if flagged else "refusal-unflagged"] += 1
            seen[f"status-{status}"] += 1
            if not flagged and status == "running":
                seen["both-missed"] += 1
                _until(lambda: relay.child_exited(child), "the child never became waitable")
                gaps.append(round((time.monotonic() - closed_at) * 1000, 3))
            thread.join(peek.TIMEOUT_S)
            assert waited == [3]                         # it ended on its own, with its real status
        finally:
            server._close_pipe()
            thread.join(peek.TIMEOUT_S)
            peek.reap(child)
    print(f"exit-in-progress relay reads ({relay.EXIT_PEEK}, {'a frame' if unread else 'nothing'} unread):",
          json.dumps(dict(sorted(seen.items()))),
          "ms from stdin close to waitable exit when both missed:", gaps, f"load {os.getloadavg()[0]:.1f}")


@contextmanager
def _runner(tmp_path, patch):
    with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
        yield r


def test_the_exit_after_ack_ordering_with_a_real_child(tmp_path):
    """The runner end to end, with a real child standing for `[fake:exit-after-ack]`: it
    reads the initialize and user-message lines and exits 1, and the test appends the
    ack the fake would print. The runner's next write lands once stdin has closed; the
    guardian's thread reaps the child. Outcomes are reported; each must be one of the two
    the contract allows for this ordering."""
    outcomes, details = Counter(), []
    for attempt in range(ATTEMPTS):
        root = tmp_path / f"attempt-{attempt}"
        root.mkdir()
        with pytest.MonkeyPatch.context() as patch, _runner(root, patch) as r:
            r.server._close_pipe()
            child, gate = peek.start("read _; exit 1")      # the initialize line, then the message
            thread, waited = _guardian(child)
            r.server._pipe, gate.fd = gate.fd, None
            os.set_blocking(r.server._pipe, False)
            r.server._child = child
            frames, statuses = [], []
            acknowledged, pause = threading.Event(), threading.Event()

            def exchange(payload, *, before_send=None):
                if before_send is not None:
                    before_send()
                frame = json.loads(payload)
                if frame.get("op") == "write" and acknowledged.is_set():
                    _until_reader_closed(r.server._pipe)    # the write lands once stdin has closed
                answer = r.server.apply(frame)
                frames.append((frame.get("tag") or frame.get("op"), answer.get("ok"), answer.get("error"),
                               answer.get("child_exited")))
                if frame.get("op") == "status":
                    statuses.append(answer["status"]["child"])
                if frame.get("tag") == "user-message":
                    with (r.adir / "stdout").open("a") as stream:
                        stream.write(json.dumps(recovery.ACCEPT) + "\n")
                    acknowledged.set()
                return answer

            patch.setattr(r.runner.relay, "_exchange", exchange)
            try:
                r.runner._apply(r.runner.driver.start())
                deadline = time.monotonic() + peek.TIMEOUT_S
                while not (r.runner.relay_io_ended or r.runner.relay_failed):
                    r.runner._read_stdout()
                    assert time.monotonic() < deadline, ("no refusal", frames)
                    pause.wait(0.005)
                thread.join(peek.TIMEOUT_S)
                assert waited == [1], (waited, frames)
            finally:
                r.server._close_pipe()
                thread.join(peek.TIMEOUT_S)
                peek.reap(child)
            (r.adir / "exit.json").write_text(json.dumps({"rc": child.returncode}))
            r.runner._run()
            turn = json.loads((r.adir / "turn.json").read_text())
            outcome = settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
                acknowledged=True, native="found", frame="written", process_gone=True))
            outcomes[outcome.reason] += 1
            details.append({"reason": outcome.reason, "frames": frames, "statuses": statuses})
    print("exit-after-ack outcomes:", json.dumps(dict(outcomes)), f"load {os.getloadavg()[0]:.1f}")
    for d in details:
        if d["reason"] != "ended-without-result":
            print("  relay-failed ordering:", json.dumps(d))
    assert set(outcomes) <= {"ended-without-result", "relay-failed"}, details
