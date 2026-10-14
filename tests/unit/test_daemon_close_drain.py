"""C-5.8a, C-25.3: `daemon.lock` stays held until what the conversation service's
bounded `close()` outlasted has ended.

The service's `close()` waits at most FILE_OPS_STOP_WAIT_S for the ops running on its
worker pools (file ops, history reads, a wake poll) and STORE_CLOSE_WAIT_S for a
write holding its store, then returns with them still running. `Daemon.close()` went
on to free `daemon.lock`, so a second daemon could start on the root while the old op
wrote into it (an attachment's copy) or committed a held transaction (both reviews of
cc7504e reproduced it). `Daemon.close()` now waits for them with no bound
(`ConversationService.drain`) before LOCK_UN; C-5.8a's stop bound ends a stuck owner
with the lock still held. Each case below holds one kind of work past both bounds
(scaled to 0.3 s) and fails if the lock is freed, or a second daemon admitted, before
that work ends.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time

import pytest

from subfleet.conversations import attachments as attachment_module
from subfleet.conversations import service as service_module
from subfleet.conversations import wakes as wakes_module
from subfleet.conversations.store import ConversationError
from subfleet.daemon import Daemon, DaemonUnavailable

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
HOLDERS = ("attachment.add", "file-pool transaction", "history read", "wake poll", "runner's store write")


def lock_is_free(root) -> bool:
    fd = os.open(root / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def until(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.01)


def hold(daemon, kind, monkeypatch, tmp_path):
    """Start `kind` of work in `daemon`'s conversation service, held until the returned
    event is set; return (go, outcome), `outcome` filled as the work ends."""
    svc, entered, go, outcome = daemon.conversations, threading.Event(), threading.Event(), []

    def held(real=None):
        def wait(*args, **kwargs):
            entered.set()
            assert go.wait(60), "the test never let the work go on"
            return real(*args, **kwargs) if real else None
        return wait

    def record(fn):
        def run():
            try:
                outcome.append(("ok", fn()))
            except ConversationError as exc:
                outcome.append(("refused", exc.reason))
            except BaseException as exc:    # noqa: BLE001 - the test reports it
                outcome.append(("raised", repr(exc)))
        return run

    if kind == "attachment.add":
        monkeypatch.setattr(attachment_module, "sniff", held(attachment_module.sniff))
        image = tmp_path / "pixel.png"
        image.write_bytes(PNG)
        svc.files.submit(record(lambda: svc.handle("attachment.add", {"path": str(image)}, None)))
    elif kind == "file-pool transaction":
        def transaction():
            with svc.store.transaction() as tx:
                held()()
                tx.execute("CREATE TABLE late_writer(x)")
            return "committed"
        svc.files.submit(record(transaction))
    elif kind == "history read":
        svc.history_reads.submit(record(held(lambda: "read")))
    elif kind == "wake poll":
        monkeypatch.setattr(wakes_module, "query_prs", held(lambda targets: {}))
        request = {"kind": "pr", "ready_json": None, "observed_json": None, "conversation_id": "c",
                   "request_id": "r", "created_at": "2026-10-14T00:00:00Z",
                   "payload_json": json.dumps({"targets": ["octo/repo#1"]})}
        svc.wakes.poller.submit(record(lambda: svc.wakes._poll_prs([request])))
    else:                                   # a turn runner's write, on no pool at all
        def write():
            with svc.store.writing():
                held()()
            return "written"
        threading.Thread(target=record(write)).start()
    if not entered.wait(30):
        go.set()
        pytest.fail(f"the {kind} never began")
    return go, outcome


@pytest.mark.parametrize("kind", HOLDERS)
def test_daemon_lock_is_held_until_what_the_service_outlasted_has_ended(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(service_module, "FILE_OPS_STOP_WAIT_S", 0.3)
    monkeypatch.setattr(service_module, "STORE_CLOSE_WAIT_S", 0.3)
    root = tmp_path / "state"
    old = Daemon(root)
    go, outcome = hold(old, kind, monkeypatch, tmp_path)
    closer = threading.Thread(target=old.close)
    try:
        closer.start()
        until(lambda: old.conversations.store._closed or not closer.is_alive(),
              "the service's close() to pass its bounds")
        closer.join(1.0)                    # one that frees the lock early has returned by now
        assert closer.is_alive(), f"Daemon.close() returned with the {kind} still running"
        assert not lock_is_free(root), f"daemon.lock was freed with the {kind} still running"
        with pytest.raises(DaemonUnavailable):
            Daemon(root)                    # no second writer on the root meanwhile
        assert outcome == [], outcome
    finally:
        go.set()
        closer.join(60)
    assert not closer.is_alive(), "Daemon.close() did not return once the work had ended"
    assert len(outcome) == 1 and outcome[0][0] in ("ok", "refused") and lock_is_free(root), outcome
    new = Daemon(root)
    try:
        if kind == "file-pool transaction":
            # Committed while the lock was still the old daemon's: one writer at a time.
            assert outcome == [("ok", "committed")]
            assert new.conversations.store.query("SELECT name FROM sqlite_master WHERE name='late_writer'")
        if kind == "attachment.add" and outcome[0][0] == "ok":
            digest = hashlib.sha256(PNG).hexdigest()
            assert (root / "attachments" / f"{digest}.png").exists()
    finally:
        new.close()


def test_a_daemon_whose_service_outlasted_nothing_frees_its_lock_at_once(tmp_path):
    """With nothing held, the drain costs nothing: close() returns and the lock is free."""
    root = tmp_path / "state"
    daemon = Daemon(root)
    started = time.monotonic()
    daemon.close()
    assert lock_is_free(root) and time.monotonic() - started < 30
