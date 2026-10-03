"""The title wire fixture works mid-turn without process-inspection privileges."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


@pytest.fixture
def quiet_claude(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "bin" / "claude"
    process = subprocess.Popen(
        [sys.executable, str(fixture), "-p", "--input-format", "stream-json", "--session-id", "title-test"],
        cwd=tmp_path, env={"PATH": os.defpath}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    rows = queue.Queue()

    def read_output():
        for raw in process.stdout:
            rows.put(json.loads(raw))

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    def send(row):
        process.stdin.write(json.dumps(row) + "\n")
        process.stdin.flush()

    def receive(timeout=10):
        return rows.get(timeout=timeout)

    try:
        send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})
        assert receive()["response"]["subtype"] == "success"
        send({"type": "user", "uuid": "one", "message": {"role": "user", "content": "[fake:quiet-slow]"}})
        # The fake announces the message's lifecycle before replaying it, as Claude Code
        # 2.1.280 does with msg_lifecycle_v1 (taught to the fake by the steer branch).
        assert [(row["type"], row.get("state")) for row in (receive() for _ in range(4))] == [
            ("command_lifecycle", "queued"), ("command_lifecycle", "started"), ("user", None), ("system", None)]
        yield process, send, receive
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("directive", ["", "title-null", "title-error", "title-timeout", "title-delayed"])
def test_title_control_is_serviced_during_a_turn_and_interrupt_stays_responsive(quiet_claude, directive):
    process, send, receive = quiet_claude
    send({"type": "control_request", "request_id": "title", "request": {
        "subtype": "generate_session_title", "description": f"Fix the importer [fake:{directive}]", "persist": False,
    }})
    if directive not in ("title-timeout", "title-delayed"):
        row = receive()
        assert row["type"] == "control_response", "the title arrives without any assistant reply"
        response = row["response"]
        assert response["request_id"] == "title"
        if directive == "title-error":
            assert response["subtype"] == "error"
        else:
            assert response["subtype"] == "success"
            assert response["response"] == {"title": None if directive == "title-null" else "Fixture session title"}

    # Missing or delayed title responses cannot hold the stdin reader hostage.
    send({"type": "control_request", "request_id": "interrupt", "request": {"subtype": "interrupt"}})
    assert receive()["response"]["request_id"] == "interrupt"
    assert receive()["type"] == "result"
    process.stdin.close()
    assert process.wait(timeout=10) == 0
    assert process.stderr.read() == ""


def test_the_title_control_is_serviced_after_the_result_and_dropped_at_the_end_of_input(quiet_claude):
    """The runner asks for the title once the turn's result is in (titles.py). The fake, like
    Claude Code 2.1.280, services it while idle, and ends at stdin's end without waiting for
    a title still being generated: the runner holds the close until the answer."""
    process, send, receive = quiet_claude
    send({"type": "control_request", "request_id": "interrupt", "request": {"subtype": "interrupt"}})
    assert receive()["response"]["request_id"] == "interrupt"
    assert receive()["type"] == "result"
    assert receive()["type"] == "command_lifecycle"     # the message's own, after its result
    send({"type": "control_request", "request_id": "title", "request": {
        "subtype": "generate_session_title", "description": "Fix the importer", "persist": False}})
    row = receive()
    assert (row["type"], row["response"]["request_id"]) == ("control_response", "title")
    assert row["response"]["response"] == {"title": "Fixture session title"}
    send({"type": "control_request", "request_id": "late", "request": {
        "subtype": "generate_session_title", "description": "Fix it [fake:title-delayed]", "persist": False}})
    process.stdin.close()
    assert process.wait(timeout=10) == 0
    with pytest.raises(queue.Empty):            # the title still being generated was dropped
        receive(timeout=3)
