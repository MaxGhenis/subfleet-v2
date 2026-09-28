"""The fake CLI uses the shipped stdio host and actual socket framing."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading

import pytest

from subfleet.conversations.chip_host import mcp_config
from tests.fake.interactive_claude import Fake


@pytest.mark.parametrize("dismiss", [False, True])
def test_fake_chip_calls_real_host_over_stdio_and_socket(monkeypatch, capsys, dismiss):
    # A protocol responder, not a daemon: no process identities or peer rules are
    # replaced. The full daemon path belongs to tests/e2e/test_task_chips.py.
    with tempfile.TemporaryDirectory(prefix="sf-chip-wire-") as directory:
        root = Path(directory)
        config = mcp_config({"root": directory, "conversation_id": "parent", "message_id": "origin",
                             "token": "private-fixture-capability"})
        proposal = {"title": "Check fixture", "tldr": "A separate check.", "prompt": "  Exact café.\n", "cwd": directory}
        monkeypatch.setenv("SUBFLEET_FAKE_CHIP", json.dumps(proposal))
        monkeypatch.setenv("SUBFLEET_FAKE_TURN_LOG", str(root / "log.jsonl"))
        monkeypatch.delenv("CLAUDE_FAKE_PROJECTS_DIR", raising=False)
        requests, errors = [], []
        stop = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(str(root / "daemon.sock"))
            except PermissionError:
                pytest.skip("sandbox forbids disposable Unix-socket listeners")
            listener.listen()
            listener.settimeout(0.1)

            def respond():
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except TimeoutError:
                            continue
                        with connection, connection.makefile("rb") as stream:
                            request = json.loads(stream.readline())
                            requests.append(request)
                            chip = {"chip_id": "chip-fixture", "title": proposal["title"],
                                    "state": "pending" if request["op"] == "chip.spawn" else "dismissed"}
                            connection.sendall((json.dumps({"v": 1, "id": request["id"], "ok": True,
                                                            "result": {"chip": chip}}) + "\n").encode())
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                fake = Fake(["--mcp-config", json.dumps(config)])
                fake.chip("claude-opus-5-5", dismiss=dismiss)
            finally:
                stop.set()
                thread.join(timeout=3)
            assert not thread.is_alive() and not errors
        emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert emitted[-1]["type"] == "result" and not emitted[-1]["is_error"], emitted
        assert [request["op"] for request in requests] == (["chip.spawn", "chip.dismiss"] if dismiss else ["chip.spawn"])
        assert {key: requests[0]["args"][key] for key in proposal} == proposal
        assert requests[0]["args"]["host_token"] == "private-fixture-capability"
        if dismiss:
            assert requests[1]["args"]["chip_id"] == "chip-fixture"
        log = (root / "log.jsonl").read_text()
        assert "private-fixture-capability" not in log
        rows = [json.loads(line) for line in log.splitlines()]
        assert [row["mcp_method"] for row in rows if "mcp_method" in row][:2] == ["initialize", "tools/list"]


def test_fake_chip_scenario_reports_missing_mcp_config(monkeypatch, capsys):
    monkeypatch.delenv("SUBFLEET_FAKE_TURN_LOG", raising=False)
    monkeypatch.delenv("CLAUDE_FAKE_PROJECTS_DIR", raising=False)
    Fake([]).chip("claude-opus-5-5", dismiss=False)
    emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert emitted[-1]["is_error"] and "--mcp-config" in emitted[-1]["errors"][0]
