"""No read the conversation service makes waits in open() (C-25.3, review of aa41312).

`ConversationService.close()` waits for the file ops already running, `Daemon.close()`
for the requests pool, and close() for each turn runner's iteration (within 5 s). A
FIFO with no writer where one of their reads opened plainly held that read in open()
until a writer came: a stored attachment's copy, a message's text, `catalog.json`,
the real `.git/index` a diff copies, an attempt's files, a relay log, a Codex rollout
a turn attests. Each reader is checked on its own, in a child process
(`tests.nonblocking`): a regression fails its case and leaves nothing behind.

Each child prints one JSON line with what the reader answered; the FIFO is still a
FIFO afterwards unless the case says a new file replaced it.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from tests.nonblocking import run_child

PRELUDE = """
import json, os, sys, uuid
from pathlib import Path
tmp = Path({tmp!r})

def fifo(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    os.mkfifo(path)
    return path

def outcome(call):
    try:
        return {{"value": call()}}
    except Exception as exc:
        return {{"error": type(exc).__name__, "reason": getattr(exc, "reason", None)}}

def report(**values):
    print(json.dumps(values, default=repr), flush=True)
"""

PNG = "b'\\x89PNG\\r\\n\\x1a\\n' + bytes(64)"

READERS = {
    # --- the conversation store and attachments ---------------------------------------
    "store.message_text": ("""
        from subfleet.conversations.store import ConversationStore
        store = ConversationStore(tmp / "state")
        text = fifo(tmp / "state" / "conversations" / "c" / "messages" / "m.md")
        report(**outcome(lambda: store.message_text({"text_path": str(text)})))
        """, {"error": "NotRegularFile"}),
    "attachments.check": (f"""
        from subfleet.conversations import attachments
        from subfleet.conversations.store import ConversationStore
        store = ConversationStore(tmp / "state")
        (tmp / "pixel.png").write_bytes({PNG})
        digest = attachments.add(store, str(tmp / "pixel.png"))["sha256"]
        fifo(store.attachment(digest)["path"])
        report(**outcome(lambda: attachments.check(store, digest)))
        """, {"error": "ConversationError", "reason": "attachment-missing"}),
    "attachments.add, a FIFO at the stored copy": (f"""
        import hashlib, stat
        from subfleet.conversations import attachments
        from subfleet.conversations.store import ConversationStore
        store = ConversationStore(tmp / "state")
        data = {PNG}
        (tmp / "pixel.png").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        target = fifo(tmp / "state" / "attachments" / f"{{digest}}.png")
        result = outcome(lambda: attachments.add(store, str(tmp / "pixel.png"))["sha256"] == digest)
        report(**result, regular=stat.S_ISREG(os.lstat(target).st_mode), same=target.read_bytes() == data)
        """, {"value": True, "regular": True, "same": True}),
    "attachments.add, a FIFO at the temporary's old name": (f"""
        import hashlib
        from subfleet.conversations import attachments
        from subfleet.conversations.store import ConversationStore
        store = ConversationStore(tmp / "state")
        data = {PNG}
        (tmp / "pixel.png").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        fifo(tmp / "state" / "attachments" / f".{{digest}}.{{os.getpid()}}.tmp")   # the name it had used
        report(**outcome(lambda: attachments.add(store, str(tmp / "pixel.png"))["sha256"] == digest))
        """, {"value": True}),
    # --- the catalog ------------------------------------------------------------------
    "catalog.read_catalog, catalog.json a FIFO": ("""
        from subfleet.conversations import catalog
        (tmp / "state").mkdir()
        fifo(tmp / "state" / "catalog.json")
        report(**outcome(lambda: catalog.read_catalog(tmp / "state")["state"]))
        """, {"value": "unreadable"}),
    "catalog.refresh_running, catalog.lock a FIFO": ("""
        from subfleet.conversations import catalog
        (tmp / "state").mkdir()
        fifo(tmp / "state" / "catalog.lock")
        report(**outcome(lambda: catalog.refresh_running(tmp / "state")))
        """, {"value": None}),
    # --- git: the diff's copy of the real index ---------------------------------------
    "diff.snapshot, .git/index a FIFO": ("""
        import subprocess
        from subfleet.conversations import diff
        repo = tmp / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, timeout=60)
        (repo / "new.txt").write_text("new")
        fifo(repo / ".git" / "index")
        result = diff.snapshot(repo, timeout_s=30)
        report(value=result is not None and len(result) == 2)
        """, {"value": True}),
    # --- the service's own reads and writes -------------------------------------------
    "service._read_json": ("""
        from subfleet.conversations import service
        report(**outcome(lambda: service._read_json(fifo(tmp / "a1" / "start.json"))))
        """, {"value": None}),
    "service._writer_check, held_by.json a FIFO": ("""
        import stat
        from subfleet.conversations import catalog, service
        catalog.external_writers = lambda session_id: []
        path = fifo(tmp / "a1" / "held_by.json")
        turn = {"provider": "claude", "native_session_id": str(uuid.uuid4())}
        result = outcome(lambda: service.ConversationService._writer_check(None, turn, tmp / "a1"))
        report(**result, regular=stat.S_ISREG(os.lstat(path).st_mode))
        """, {"value": [], "regular": True}),
    "service._catalog_cache, models.json a FIFO": ("""
        from types import SimpleNamespace
        from subfleet.conversations import service
        fifo(tmp / "conversations" / "models.json")
        report(**outcome(lambda: service.ConversationService._catalog_cache(SimpleNamespace(root=tmp))))
        """, {"value": {}}),
    "service.op_approval_get, the request a FIFO": ("""
        from tests.unit import test_conversation_service as fx
        (tmp / "state").mkdir()
        daemon = fx.FakeDaemon(tmp / "state")
        svc = fx.ConversationService(daemon)
        svc.test_workspace = str(tmp)
        svc._person = lambda peer, what: None
        cid = fx.conversation(svc)
        mid = fx.submit(svc, cid)
        approval, _ = svc.store.add_approval(message_id=mid, conversation_id=cid, attempt_id="j/a1",
                                             provider_request_id="r1", kind="tool", request={"x": 1}, display={},
                                             options=("allow",))
        fifo(approval["request_path"])
        result = outcome(lambda: svc.op_approval_get({"approval_id": approval["approval_id"]}, None))
        svc.close()
        daemon.store.close()
        report(**result)
        """, {"error": "NotRegularFile"}),
    # --- the turn runner --------------------------------------------------------------
    "runner._read_stdout": ("""
        from subfleet.conversations.runner import Clocks, TurnRunner
        from subfleet.conversations.store import ConversationStore
        from subfleet.conversations.turn import TurnSpec
        store = ConversationStore(tmp / "state")
        spec = TurnSpec(provider="claude", message_id=str(uuid.uuid4()), text="hi", model_id="opus",
                        permission="ask", native_session_id=None, new_session_id=str(uuid.uuid4()))
        fifo(tmp / "a1" / "stdout")
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id="cv-x", attempt_dir=tmp / "a1", control_socket=str(tmp / "none.sock"),
                            on_outcome=lambda r: None, on_contain=lambda a: None, clocks=Clocks())
        report(**outcome(runner._read_stdout))
        """, {"error": "NotRegularFile"}),
    "runner._read_attachment": ("""
        from subfleet.conversations.runner import Clocks, TurnRunner
        from subfleet.conversations.store import ConversationStore
        from subfleet.conversations.turn import TurnSpec
        store = ConversationStore(tmp / "state")
        spec = TurnSpec(provider="claude", message_id=str(uuid.uuid4()), text="hi", model_id="opus",
                        permission="ask", native_session_id=None, new_session_id=str(uuid.uuid4()))
        (tmp / "a1").mkdir()
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id="cv-x", attempt_dir=tmp / "a1", control_socket=str(tmp / "none.sock"),
                            on_outcome=lambda r: None, on_contain=lambda a: None, clocks=Clocks())
        report(**outcome(lambda: runner._read_attachment(str(fifo(tmp / "image.png")))))
        """, {"error": "NotRegularFile"}),
    "relay.read_log": ("""
        from subfleet import relay
        report(**outcome(lambda: relay.read_log(fifo(tmp / "a1" / "stdin.jsonl"))))
        """, {"error": "NotRegularFile"}),                  # an unreadable log, as for any other OSError
    # --- settling a turn --------------------------------------------------------------
    "reconcile.frame_status": ("""
        from subfleet.conversations import reconcile
        fifo(tmp / "a1" / "stdin.jsonl")
        report(**outcome(lambda: reconcile.frame_status(tmp / "a1")))
        """, {"value": "unreadable"}),
    "reconcile._scan": ("""
        from subfleet.conversations import reconcile
        path = fifo(tmp / "session.jsonl")
        report(**outcome(lambda: reconcile._scan(str(path), "m", 0, lambda record: True)))
        """, {"value": "unreadable"}),
    "reconcile._read_json": ("""
        from subfleet.conversations import reconcile
        report(**outcome(lambda: reconcile._read_json(fifo(tmp / "a1" / "turn.json"))))
        """, {"value": None}),
    "classify.read_turn": ("""
        from subfleet.conversations import classify
        fifo(tmp / "a1" / "turn.json")
        report(**outcome(lambda: classify.read_turn(tmp / "a1")))
        """, {"value": None}),
    "classify.codex_readings": ("""
        from subfleet.conversations import classify
        path = fifo(tmp / "a1" / "stdout")
        report(**outcome(lambda: list(classify.codex_readings(path, lane_id="codex-1", attempt_id=None))))
        """, {"value": [[], None]}),
    "classify.codex_attest": ("""
        from subfleet.conversations import classify
        thread = str(uuid.uuid4())
        fifo(tmp / "home" / "sessions" / "2026" / "09" / "26" / f"rollout-2026-09-26T00-00-00-{thread}.jsonl")
        result = outcome(lambda: classify.codex_attest(str(tmp / "home"), thread, "turn-1", "gpt-6-astra"))
        report(done=True, **result)
        """, {"done": True}),
}


@pytest.mark.parametrize("reader", list(READERS))
def test_a_fifo_where_the_service_reads_answers_at_once(reader, tmp_path):
    """C-25.3: each reader answers for a FIFO as for an unreadable file, at once;
    where the service writes a new file of its own (an attachment's copy, a
    writer check's record), that file replaces the FIFO."""
    source, expected = READERS[reader]
    out = run_child(PRELUDE.format(tmp=str(tmp_path)) + textwrap.dedent(source))
    got = json.loads(out.strip().splitlines()[-1])
    assert {k: got.get(k) for k in expected} == expected, got


SERVICE_CASES = ("attachment.add", "handoff moving a message", "handoff moving an attachment",
                 "conversation.open, a message's text", "conversation.open, catalog.json")

SERVICE = """
import concurrent.futures, stat
from subfleet.policy import HANDOFF_CAPS
from tests.unit import test_conversation_service as fx
os.environ["HOME"] = str(tmp / "user-home")
os.environ["SUBFLEET_CLAUDE_DIR"] = str(tmp / "claude")
case = {case!r}
root = tmp / "state"
root.mkdir()
daemon = fx.FakeDaemon(root)
daemon.policy["sessions"] = {{"handoff_caps": dict(HANDOFF_CAPS)}}
svc = fx.ConversationService(daemon)
workspace = tmp / "work"
workspace.mkdir()
svc.test_workspace = str(workspace)
sid = str(uuid.uuid4())
cid = fx.conversation(svc, native_session_id=sid)
transcript = tmp / "claude" / "projects" / "work" / f"{{sid}}.jsonl"
transcript.parent.mkdir(parents=True)
transcript.write_text(json.dumps({{"type": "user", "uuid": "first", "cwd": str(workspace),
                                  "message": {{"content": "Please do the task."}}}}) + "\\n")
image = tmp / "incoming.png"
image.write_bytes(fx.PNG)
digest = None
if "attachment" in case:
    digest = svc.op_attachment_add({{"path": str(image)}}, None)["sha256"]
    held = Path(svc.store.attachment(digest)["path"])
if case.startswith("handoff") or case == "conversation.open, a message's text":
    message, _ = svc.store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=None,
                                          text="moved", attachments=[digest] if digest else [], settings=fx.SETTINGS)
    if "attachment" not in case:
        held = Path(message["text_path"])
if case == "conversation.open, catalog.json":
    held = root / "catalog.json"
fifo(held)
if case == "attachment.add":
    call = lambda: svc.handle("attachment.add", {{"path": str(image)}}, None)["sha256"] == digest
elif case.startswith("handoff"):
    call = lambda: svc.handle("conversation.handoff", {{"request_id": "h-fifo", "from": {{"conversation_id": cid}},
                                                        "to": {{"provider": "claude", "settings": fx.SETTINGS}}}}, None)
else:
    call = lambda: bool(svc.handle("conversation.open", {{"conversation_id": cid}}, None))
op = ("attachment.add" if case == "attachment.add" else "conversation.handoff" if case.startswith("handoff")
      else "conversation.open")
pool = svc.pool_for(op)
if pool is not svc.files:              # the fake daemon has no requests pool
    pool = concurrent.futures.ThreadPoolExecutor(1)
import threading
started = threading.Event()

def run():
    started.set()                       # a file op close() must wait for, not one it may cancel unstarted
    return outcome(call)

future = pool.submit(run)
assert started.wait(60), "the op never started"
print("submitted", flush=True)
if pool is not svc.files:
    # close() waits for the file pool; an op on the requests pool is waited for by
    # Daemon.close() after it, so here it answers before the service closes.
    future.result()
svc.close()                             # waits for a file op already running
print("closed", flush=True)
result = future.result()
if pool is not svc.files:
    pool.shutdown(wait=True)
daemon.store.close()
report(**result, fifo=stat.S_ISFIFO(os.lstat(held).st_mode))
"""


@pytest.mark.parametrize("case", SERVICE_CASES)
def test_an_op_meeting_a_fifo_in_the_state_root_answers_and_close_returns(case, tmp_path):
    """C-25.3 (review of aa41312, finding 1): a stored attachment, a moved message's
    text, a moved attachment's copy, or `catalog.json` replaced by a FIFO held
    `attachment.add`, a handoff or `conversation.open` in open() until a writer came,
    and the file pool with it, so close() too. Each now answers at once: an
    attachment's copy is written again, a handoff fails on what it cannot read, and
    `conversation.open` shows what it can. close() returns."""
    out = run_child(PRELUDE.format(tmp=str(tmp_path)) + SERVICE.format(case=case))
    got = json.loads(out.strip().splitlines()[-1])
    if case == "attachment.add":
        assert got == {"value": True, "fifo": False}, got          # the FIFO was replaced by the copy
    elif case.startswith("handoff"):
        assert "error" in got and got["fifo"], got                  # refused, the FIFO left as it was
    else:
        assert got == {"value": True, "fifo": True}, got
