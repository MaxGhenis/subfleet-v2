"""The fake CLIs exercise the static steer wire contract without credentials."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest


@contextmanager
def wire(provider, tmp_path):
    from subfleet.guard.preflight import HOOK_KEY
    module = f"tests.fake.interactive_{provider}"
    args = ["--session-id", "00000000-0000-4000-8000-000000000001"] if provider == "claude" else [
        'hooks={state={"' + HOOK_KEY + '"={enabled=true,trusted_hash="sha256:fake"}}}']
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE", "CODEX", "ANTHROPIC", "SUBFLEET_FAKE_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["CODEX_HOME"] = str(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", f"import sys; from {module} import main; "
                                "sys.exit(main(sys.argv[1:]))", *args], cwd=tmp_path, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    inbox, seen = queue.Queue(), []

    def reader():
        for line in process.stdout:
            inbox.put(json.loads(line))
    threading.Thread(target=reader, daemon=True).start()

    def send(row):
        process.stdin.write(json.dumps(row) + "\n")
        process.stdin.flush()

    def until(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                row = inbox.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            seen.append(row)
            if predicate(row):
                return row
        raise AssertionError(f"fake {provider} did not emit the expected row: {seen}")

    try:
        yield send, until, seen
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def user(mid, text, priority=None):
    row = {"type": "user", "uuid": mid, "parent_tool_use_id": None,
           "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    if priority:
        row["priority"] = priority
    return row


@pytest.mark.parametrize("late", [False, True])
def test_fake_claude_folds_at_a_boundary_or_runs_its_own_turn(tmp_path, late):
    with wire("claude", tmp_path) as (send, until, seen):
        send(user("host", "[fake:steer-late]" if late else "[fake:steer]"))
        init = until(lambda row: row.get("subtype") == "init")
        assert {"msg_lifecycle_v1", "interrupt_receipt_v1", "interrupt_cancel_queued_v1"} <= set(init["capabilities"])
        send(user("correction", "remember the correction", "next"))
        until(lambda row: row.get("type") == "command_lifecycle" and row.get("command_uuid") == (
            "correction" if late else "host") and row["state"] == "completed")
        results = [row for row in seen if row.get("type") == "result"]
        assert [row["user_message_uuids"] for row in results] == (
            [["host"], ["correction"]] if late else [["host", "correction"]])
        assert [row["result_index"] for row in results] == ([0, 1] if late else [0])
        assert [row["queued_turn_count"] for row in results] == ([1, 0] if late else [0])
        completed = next(i for i, row in enumerate(seen) if row.get("type") == "command_lifecycle"
                         and row["command_uuid"] == "correction" and row["state"] == "completed")
        last_result = max(i for i, row in enumerate(seen) if row.get("type") == "result")
        assert (completed > last_result) == late


def test_fake_claude_cancels_one_queued_message_and_interrupts_the_rest(tmp_path):
    with wire("claude", tmp_path) as (send, until, seen):
        send(user("host", "[fake:slow]"))
        until(lambda row: row.get("subtype") == "init")
        send(user("one", "first", "next"))
        send(user("two", "second", "next"))
        until(lambda row: row.get("command_uuid") == "two" and row["state"] == "queued")
        send({"type": "control_request", "request_id": "cancel-one",
              "request": {"subtype": "cancel_async_message", "message_uuid": "one"}})
        answer = until(lambda row: (row.get("response") or {}).get("request_id") == "cancel-one")
        assert answer["response"]["response"] == {"cancelled": True}
        send({"type": "control_request", "request_id": "stop",
              "request": {"subtype": "interrupt", "cancel_queued": True}})
        answer = until(lambda row: (row.get("response") or {}).get("request_id") == "stop")
        assert answer["response"]["response"] == {"cancelled": ["two"], "still_queued": []}
        result = until(lambda row: row.get("type") == "result")
        assert result["user_message_uuids"] == ["host"] and result["queued_turn_count"] == 0
        assert not any(row.get("command_uuid") in ("one", "two") and row["state"] == "started"
                       for row in seen if row.get("type") == "command_lifecycle")


def test_fake_claude_accepts_a_priority_message_after_its_first_result(tmp_path):
    """An idle stream reader wakes the main loop when a new priority command arrives."""
    with wire("claude", tmp_path) as (send, until, seen):
        send(user("host", "first message"))
        first = until(lambda row: row.get("type") == "result")
        assert first["user_message_uuids"] == ["host"] and first["queued_turn_count"] == 0
        send(user("late", "this arrived after result", "next"))
        second = until(lambda row: row.get("type") == "result")
        assert second["user_message_uuids"] == ["late"] and second["result_index"] == 1
        assert any(row.get("type") == "command_lifecycle" and row.get("command_uuid") == "late"
                   and row["state"] == "started" for row in seen)


@pytest.mark.parametrize("scenario", ["steer", "steer-refuse", "steer-unanswered"])
def test_fake_codex_correlates_steer_with_the_active_turn_and_user_item(tmp_path, scenario):
    with wire("codex", tmp_path) as (send, until, seen):
        send({"id": 1, "method": "thread/start", "params": {"cwd": str(tmp_path)}})
        thread = until(lambda row: row.get("id") == 1)["result"]["thread"]["id"]
        send({"id": 2, "method": "turn/start", "params": {"threadId": thread,
              "clientUserMessageId": "host", "input": [{"type": "text", "text": f"[fake:{scenario}]"}]}})
        turn = until(lambda row: row.get("id") == 2)["result"]["turn"]["id"]
        send({"id": "steer:correction", "method": "turn/steer", "params": {"threadId": thread,
              "expectedTurnId": turn, "clientUserMessageId": "correction",
              "input": [{"type": "text", "text": "correct this"}]}})
        answer = until(lambda row: row.get("id") == "steer:correction")
        if scenario == "steer-refuse":
            assert answer["error"]["code"] == -32600
        else:
            assert answer["result"] == {"turnId": turn}
        until(lambda row: row.get("method") == "turn/completed")
        user_items = [(i, row["params"]["item"]) for i, row in enumerate(seen)
                      if row.get("method") == "item/started" and row["params"]["item"]["type"] == "userMessage"]
        if scenario == "steer-refuse":
            assert user_items == []
        else:
            assert len(user_items) == 1 and user_items[0][1]["clientId"] == "correction"
            after = seen[user_items[0][0] + 1:]
            answered = any(row.get("method") == "item/started" and row["params"]["item"]["type"] == "agentMessage"
                           for row in after)
            assert answered == (scenario == "steer")


@pytest.mark.parametrize("provider,scenario,restart", [
    ("claude", "steer", False), ("claude", "steer-late", False), ("claude", "approval", True),
    ("codex", "steer", False), ("codex", "steer-refuse", False), ("codex", "steer-unanswered", False)])
def test_runner_and_fake_settle_one_steer_across_boundaries_and_restart(tmp_path, provider, scenario, restart):
    """Run the actual driver, outbox, replay and settlement against a fake process.
    Authentication belongs to the daemon e2e cases; no peer identity is fabricated here.
    """
    from subfleet.conversations.runner import TurnRunner
    from subfleet.conversations.turn import TurnSpec
    from subfleet.relay import read_log
    from tests.unit.test_steer_invariants import JournalRelay, children, world

    with world() as w, wire(provider, tmp_path) as (send, until, seen):
        class PipeRelay(JournalRelay):
            def send(self, seq, op, *, line=None, tag=None, sig=None):
                ack = super().send(seq, op, line=line, tag=tag, sig=sig)
                if op == "write":
                    send(json.loads(line))
                return ack

        spec = TurnSpec(provider=provider, message_id=w.host, text=f"[fake:{scenario}]",
                        model_id="opus[1m]" if provider == "claude" else "gpt-6-astra",
                        permission="ask" if provider == "claude" else "read-only",
                        native_session_id="00000000-0000-4000-8000-000000000001" if provider == "claude" else None,
                        cwd=str(tmp_path), guard_hash="sha256:fake")

        def make_runner():
            runner = TurnRunner(store=w.service.store, attempt={"attempt_id": "job/a1", "lane_id": f"{provider}-1"},
                                spec=spec, conversation_id=w.cid, attempt_dir=w.adir,
                                control_socket=str(w.adir / "unused.sock"), on_outcome=lambda _: None,
                                on_contain=lambda _: None)
            runner.relay = PipeRelay(w.adir)
            runner.handshaken = runner.handshake_done_once = True
            runner._restore_steers()
            runner._apply(runner.driver.start())
            runner._read_stdout()
            runner.replay_caught_up = True
            return runner

        runner = make_runner()

        def feed_until(predicate):
            def consume(row):
                with (w.adir / "stdout").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")
                runner._read_stdout()
                return predicate(row)
            return until(consume)

        feed_until(lambda _: bool(runner.driver.pending) if restart else runner.driver.steerable)
        [mid] = children(w, 1)
        runner.steer(mid)
        runner._drain_commands()
        if restart:
            feed_until(lambda row: row.get("command_uuid") == mid and row.get("state") == "queued")
            runner = make_runner()
            assert mid in runner.driver.steers
            [approval] = runner.driver.pending
            runner._apply(runner.driver.respond(approval, "allow", None))
        feed_until(lambda _: runner.driver.outcome is not None)
        # Own-turn lifecycle completion follows result; drain it before settlement.
        if scenario == "steer-late":
            feed_until(lambda row: row.get("command_uuid") == mid and row.get("state") == "completed")
        runner._flush()
        w.service._settle_steers(runner, {"steers": runner.driver.steers}, runner.served)
        assert runner.driver.outcome.state == "complete"
        assert w.service.store.message(mid)["state"] == ("queued" if scenario == "steer-refuse" else "steered")
        if scenario == "steer-unanswered":
            assert w.service.store.message(mid)["state_reason"] == f"steered-unanswered:{w.host}"
        assert len([r for r in read_log(w.adir / "stdin.jsonl") if r["tag"] == f"steer:{mid}"]) == 1
        events = w.service.store.events_after(w.cid, 0)["events"]
        assert len([e for e in events if e["kind"] == "steer.delivered" and e["data"]["message_id"] == mid]) == (
            0 if scenario == "steer-refuse" else 1)
