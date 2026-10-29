"""Review #271 r2: restart recovery and all entry paths to the exit drain."""

import json

import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.store import ConversationError
from subfleet.relay import frame_sha256
from tests.fake.test_turn_relay_exit_recovery import recovery  # noqa: F401


def rebuild(r, *, ended=False):
    old = r.runner
    return TurnRunner(store=r.store, attempt=old.attempt, spec=old.spec,
                      conversation_id=r.cid, attempt_dir=r.adir,
                      control_socket=old.control_socket, on_outcome=r.reported.append,
                      on_contain=old.on_contain, clocks=old.clocks,
                      clock=lambda: r.now[0], ended=ended)


@pytest.mark.parametrize("refusal", ["failed", "closed"])
def test_restart_recovers_exit_without_a_receipt(recovery, monkeypatch, refusal):
    r = recovery

    def exchange(payload, **kwargs):
        answer = r.exchange(payload, **kwargs)
        if json.loads(payload).get("tag") == "user-message" and refusal == "closed":
            r.server._closed = True  # closed refusals create no failed log entry
        return answer

    monkeypatch.setattr(r.runner.relay, "_exchange", exchange)
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # initialize -> message -> refused settings
    assert r.runner.exit_proved_at == r.now[0]
    assert r.runner.driver.outcome is None
    assert not (r.adir / "exit.json").exists()
    r.now[0] += 15  # daemon restarts in the middle of the drain allowance
    again = rebuild(r)
    monkeypatch.setattr(again.relay, "_exchange", exchange)
    started = r.now[0]

    def advance(_):
        r.now[0] += 1
        if r.now[0] - started > 35:
            again.stop()  # deterministic escape for a missing exit transition

    monkeypatch.setattr(runner_module.time, "sleep", advance)
    again._run()
    assert r.reported == [again], {"elapsed": r.now[0] - started,
                                  "exit_proved_at": again.exit_proved_at,
                                  "sent": again.sent}
    assert again.driver.outcome.reason == "ended-without-result"
    assert not again.relay_failed and not r.contained
    assert r.now[0] - started <= runner_module.EXIT_DRAIN_S + again.clocks.contain_after_s


@pytest.mark.parametrize("boundary", ["receipt", "owner-ended"])
def test_every_exit_drain_entry_arms_the_bound(recovery, monkeypatch, boundary):
    r = recovery
    # A replay with only written frames has no reason to call _relay_lost.
    for seq, tag in enumerate(("init", "user-message"), 1):
        r.exchange(json.dumps({"seq": seq, "tag": tag, "op": "write", "line": "{}",
                               "sha256": frame_sha256("write", "{}")}).encode())
    if boundary == "receipt":
        (r.adir / "exit.json").write_text('{"rc":0}')
    again = rebuild(r, ended=boundary == "owner-ended")
    monkeypatch.setattr(again.relay, "_exchange", r.exchange)
    monkeypatch.setattr(again.relay, "status", lambda: None)  # version 1: no status proof
    started = r.now[0]
    read = again._read_stdout

    def continuous_stdout():
        # Fix r2: a reader that reports a chunk has read what is on disk, so the
        # drain's clock, which starts once it catches up, can start (C-26.4).
        read()
        r.now[0] += 1
        # A model of a descendant holding stdout continuously nonempty; escape
        # after three budgets so the regression cannot hang the test process.
        return r.now[0] - started < 3 * runner_module.EXIT_DRAIN_S

    monkeypatch.setattr(again, "_read_stdout", continuous_stdout)
    again._run()
    assert r.contained == [again.attempt_id], {"elapsed": r.now[0] - started,
                                             "exit_proved_at": again.exit_proved_at}
    assert r.now[0] - started <= runner_module.EXIT_DRAIN_S + 1
    assert r.reported == [again]
    assert again.stop_reason == "stdout-drain-timeout"


def test_a_receipt_after_the_drain_bound_does_not_extend_it(recovery, monkeypatch):
    r = recovery
    r.child.returncode = 1
    # Containment is requested but publishes no synthetic receipt in this case:
    # the only exit.json genuinely arrives after the deadline and settlement.
    monkeypatch.setattr(r.runner, "on_contain", r.contained.append)
    r.runner._relay_lost("proved exit", child_exited=True)
    started = r.now[0]
    read = r.runner._read_stdout

    def continuous_stdout():
        # Fix r2: a reader that reports a chunk has read what is on disk, so the
        # drain's clock, which starts once it catches up, can start (C-26.4).
        read()
        r.now[0] += 1
        if r.now[0] - started == runner_module.EXIT_DRAIN_S + 5:
            (r.adir / "exit.json").write_text('{"rc":0}')
        return r.now[0] - started < 3 * runner_module.EXIT_DRAIN_S

    monkeypatch.setattr(r.runner, "_read_stdout", continuous_stdout)
    r.runner._run()
    assert r.now[0] - started <= runner_module.EXIT_DRAIN_S + 1
    assert r.contained == [r.runner.attempt_id]
    assert r.reported == [r.runner]
    assert not (r.adir / "exit.json").exists()
    # Publish the genuinely late guardian receipt after the runner has settled.
    r.now[0] += 5
    (r.adir / "exit.json").write_text('{"rc":0}')
    r.runner._report()
    assert r.reported == [r.runner]


@pytest.mark.parametrize("has_result", [False, True], ids=["eof", "provider-result"])
def test_continuous_stdout_bytes_are_kept_until_containment(recovery, monkeypatch, has_result):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # a real refused settings frame proves exit
    if has_result:
        with (r.adir / "stdout").open("a") as stream:
            stream.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                     "result": "[finished before containment]"}) + "\n")
    started = r.now[0]
    read = r.runner._read_stdout
    chunks = []

    def write_then_read():
        r.now[0] += 1
        chunks.append(f"chunk-{len(chunks)}")
        # Each complete assistant row refines one answer, retaining its earlier
        # text. This exercises the real file reader, JSON decoder and driver.
        row = {"type": "assistant", "message": {"id": "answer", "content": [
            {"type": "text", "text": "[" + "|".join(chunks) + "]"}]}}
        with (r.adir / "stdout").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        progressed = read()
        # A missing deadline cannot hang this regression.
        return progressed and r.now[0] - started < 3 * runner_module.EXIT_DRAIN_S

    monkeypatch.setattr(r.runner, "_read_stdout", write_then_read)
    r.runner._run()
    assert r.now[0] - started <= runner_module.EXIT_DRAIN_S + 1
    assert r.contained == [r.runner.attempt_id] and r.reported == [r.runner]
    assert r.runner.offset == (r.adir / "stdout").stat().st_size
    assert r.runner.final_text == "[" + "|".join(chunks) + "]"
    assert r.runner.stop_reason == "stdout-drain-timeout" and not r.runner.relay_failed
    turn = json.loads((r.adir / "turn.json").read_text())
    from subfleet.conversations.reconcile import Evidence, settle
    settled = settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
        acknowledged=True, frame="written", process_gone=True, native="found"))
    assert settled.state == ("complete" if has_result else "failed")
    assert settled.reason == (None if has_result else "stdout-drain-timeout")


def test_restart_in_the_active_drain_rearms_from_available_proof(recovery, monkeypatch):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()
    first_proof = r.now[0]
    read = r.runner._read_stdout

    def before_restart():
        # Fix r2: a reader that reports a chunk has read what is on disk, so the
        # drain's clock, which starts once it catches up, can start (C-26.4).
        read()
        r.now[0] += 1
        if r.now[0] - first_proof >= 15:
            # Fix r3: the refusal a closing service's runner meets, which the runner
            # leaves to a later daemon; any other exception now settles the turn.
            raise ConversationError("store-closed", "SECRET7 simulated daemon restart during stdout drain", code=1)
        return True

    monkeypatch.setattr(r.runner, "_read_stdout", before_restart)
    r.runner._run()
    assert r.runner.finished.is_set() and r.runner.driver.outcome is None
    assert not r.reported and not r.contained
    again = rebuild(r)
    monkeypatch.setattr(again.relay, "_exchange", r.exchange)
    restarted = r.now[0]
    read_again = again._read_stdout

    def after_restart():
        # Fix r2: a reader that reports a chunk has read what is on disk, so the
        # drain's clock, which starts once it catches up, can start (C-26.4).
        read_again()
        r.now[0] += 1
        return r.now[0] - restarted < 3 * runner_module.EXIT_DRAIN_S

    monkeypatch.setattr(again, "_read_stdout", after_restart)
    again._run()
    assert again.exit_proved_at == restarted
    assert r.now[0] - restarted <= runner_module.EXIT_DRAIN_S + 1
    assert r.contained == [again.attempt_id] and r.reported == [again]
    assert again.stop_reason == "stdout-drain-timeout" and not again.relay_failed


def test_a_backlog_on_disk_at_the_proof_is_read_in_full(recovery, monkeypatch):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # a real refused settings frame proves exit
    assert r.runner.exit_proved_at == r.now[0]
    # Everything below is on disk at the proof and no writer is left: only a
    # backlog that a loaded host reads slowly ("a replay can be far behind",
    # C-26.6). The bound is for a live writer, not for reading speed.
    rows = [{"type": "assistant", "message": {"id": "answer", "content": [
        {"type": "text", "text": f"[part-{index}]"}]}} for index in range(100)]
    rows.append({"type": "result", "subtype": "success", "is_error": False, "result": "[done]"})
    with (r.adir / "stdout").open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    size = (r.adir / "stdout").stat().st_size
    monkeypatch.setattr(runner_module, "READ_CHUNK", 256)
    read = r.runner._read_stdout

    def slow_read():
        r.now[0] += 1  # one fake second per chunk read and applied
        return read()

    monkeypatch.setattr(r.runner, "_read_stdout", slow_read)
    r.runner._run()
    assert r.runner.offset == size, {"read": r.runner.offset, "size": size,
                                     "stop_reason": r.runner.stop_reason}
    assert r.reported == [r.runner] and not r.contained
    assert r.runner.driver.outcome.reason is None  # the provider's result decides


# Fix r2 (hub decision 1): a fresh status naming the child exited is exit proof on
# every handshake, a first runner's (asked again after a lost answer) and a rebuilt
# one's, whatever the log shows. Here no frame is refused and no receipt arrives.
@pytest.mark.parametrize("which", ["first", "rebuilt"])
def test_a_status_naming_the_child_exited_proves_the_exit_on_every_handshake(recovery, monkeypatch, which):
    r = recovery

    def relay(payload, **kwargs):
        return r.server.apply(json.loads(payload))   # the real relay; the child exits only below

    monkeypatch.setattr(r.runner.relay, "_exchange", relay)
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # initialize, message and settings all written
    assert r.runner.exit_proved_at is None and not r.runner.relay_io_ended
    r.child.returncode = 1  # the guardian never publishes a receipt
    r.now[0] += 15
    runner = r.runner if which == "first" else rebuild(r)
    if which == "rebuilt":
        monkeypatch.setattr(runner.relay, "_exchange", relay)
    runner.handshaken = False  # a lost answer asks again (`_handover_verdict`); a rebuilt runner asks first
    started = r.now[0]
    runner._run()
    assert runner.exit_proved_at is not None
    assert r.reported == [runner], {"elapsed": r.now[0] - started, "polls": len(r.polls)}
    assert runner.driver.outcome.reason == "ended-without-result"
    assert not runner.relay_failed and runner.stop_reason is None and not r.contained
    assert not (r.adir / "exit.json").exists()


# Fix r2 (hub decision 2): a runner rebuilt after a restart replays stdout from the
# start and records its own entry size, so everything already on disk is read,
# however slowly, before its own bound starts. Release had no bound here at all.
def test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound(recovery, monkeypatch):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # a real refused settings frame proves exit
    rows = [{"type": "assistant", "message": {"id": "answer", "content": [
        {"type": "text", "text": f"[part-{index}]"}]}} for index in range(100)]
    rows.append({"type": "result", "subtype": "success", "is_error": False, "result": "[done]"})
    with (r.adir / "stdout").open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    size = (r.adir / "stdout").stat().st_size
    r.now[0] += 15  # the daemon restarts before the first runner drains anything
    again = rebuild(r)
    monkeypatch.setattr(again.relay, "_exchange", r.exchange)
    monkeypatch.setattr(runner_module, "READ_CHUNK", 256)
    read = again._read_stdout

    def slow_read():
        r.now[0] += 1  # one fake second per chunk read and applied: ~42 s for the backlog
        return read()

    monkeypatch.setattr(again, "_read_stdout", slow_read)
    again._run()
    assert again.exit_proved_at is not None and again.drain_entry_size == size
    assert again.offset == size, {"read": again.offset, "size": size, "stop_reason": again.stop_reason}
    assert r.reported == [again] and not r.contained
    assert again.driver.outcome.reason is None  # the provider's result decides
