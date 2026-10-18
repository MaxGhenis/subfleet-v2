"""A/B measurement (not a test): does `approval.list`, called the moment the events long
poll returns `approval.requested`, list that approval? Run as
`python measure.py <tree> <iterations> <out.json>`; the daemon runs <tree>'s code.
Bounded: <iterations> rounds and a 420 s deadline overall."""

import json
import shutil
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path

tree = Path(sys.argv[1]).resolve()
rounds = int(sys.argv[2])
out_path = Path(sys.argv[3])
sys.path.insert(0, str(tree))

from tests.e2e.conftest import E2E, REPO  # noqa: E402

assert REPO == tree, (REPO, tree)
root = Path(tempfile.mkdtemp(prefix="sf-ab-", dir="/tmp")).resolve()
e2e = E2E(root)
e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(root / "turns.jsonl")


def call(op, **args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(str(root / "daemon.sock"))
        client.sendall((json.dumps({"v": 1, "id": "m", "op": op, "args": args}) + "\n").encode())
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline())
    assert response["ok"], (op, response)
    return response["result"]


results = []
deadline = time.monotonic() + 420
try:
    e2e.start()
    for n in range(rounds):
        if time.monotonic() > deadline:
            break
        cid = call("conversation.create", provider="claude", request_id=str(uuid.uuid4()),
                   workspace=str(e2e.workdir),
                   settings={"model": "opus[1m]", "permission": "ask", "effort": None, "fast": False}
                   )["conversation"]["conversation_id"]
        mid = str(uuid.uuid4())
        text = "[fake:question]" if n % 2 else "[fake:approval]"
        call("message.submit", conversation_id=cid, message_id=mid, text=text)
        after, rid, until = 0, None, time.monotonic() + 30
        while rid is None and time.monotonic() < until:
            page = call("conversation.events", conversation_id=cid, after=after, wait_s=5)
            rid = next((e["data"]["request_id"] for e in page["events"] if e["kind"] == "approval.requested"), None)
            after = page["next"]
        # What the app does next: list the conversation's approvals to answer the card.
        listed = call("approval.list", conversation_id=cid)["approvals"]
        state = call("message.status", message_ids=[mid])["messages"][0]["state"]
        results.append({"kind": text, "event": rid is not None,
                        "listed": any(a["request_id"] == rid for a in listed), "state": state})
        call("turn.interrupt", message_id=mid)
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if call("message.status", message_ids=[mid])["messages"][0]["state"] in (
                    "complete", "failed", "interrupted", "cancelled"):
                break
            time.sleep(0.1)
finally:
    e2e.close()
    shutil.rmtree(root, ignore_errors=True)
    summary = {"tree": str(tree), "rounds": len(results),
               "event_seen": sum(r["event"] for r in results),
               "listed_at_once": sum(r["listed"] for r in results),
               "approval_needed_at_once": sum(r["state"] == "approval-needed" for r in results),
               "missed": [r for r in results if not r["listed"]], "results": results}
    out_path.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("results", "missed")}))
