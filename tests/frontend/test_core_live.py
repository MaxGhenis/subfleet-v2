"""The app's core against a real development daemon (C-24 to C-29, design §12).

The daemon is the e2e harness's (`tests/e2e/conftest.py`): an isolated state
root under /tmp, fake `claude` and `codex`, two Claude lanes. It starts with
`SUBFLEET_DEV_APP_EXECUTABLE` naming the probe binary, so the daemon's real
peer check (C-25.6) treats the probe as the development app: approvals are
answered as the app would answer them. The probe (`CoreProbeLive.swift`)
drives the daemon through the app's own engine, outbox and store state and
records every exchange; this test checks its results, the daemon's store,
and that every answer decodes into the app's models without losing a field.
Nothing here touches ~/.subfleet. A failed run's state root and the probe's
record are kept under /tmp/sf-failed, which CI uploads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_core_protocol import strip_nulls

pytestmark = [needs_swift, pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")]

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4a50000000049454e44ae426082")


@pytest.fixture
def dev_daemon(request, core_probe, tmp_path):
    from subfleet.procs import InspectionError, boot_id, proc_start
    try:
        boot_id()
        if not proc_start(os.getpid()):
            pytest.skip("the daemon's process checks need a visible current process from ps")
    except InspectionError as exc:
        pytest.skip(f"the daemon's process checks need ps/sysctl: {exc}")
    from tests.e2e.conftest import E2E, _sockets
    from tests.fake.interactive_claude import encode_project_dir
    root = Path(tempfile.mkdtemp(prefix="sf-app-", dir="/tmp")).resolve()
    assert root != (Path.home() / ".subfleet").resolve()
    harness = E2E(root)
    harness.env["SUBFLEET_FAKE_TURN_LOG"] = str(root / "turns.jsonl")
    # A native Claude session outside /tmp (a /tmp workspace is not continuable, C-30.2).
    native_cwd = tmp_path / "native-work"
    native_cwd.mkdir()
    session = str(uuid.uuid4())
    project = root / "user-home" / ".claude" / "projects" / encode_project_dir(str(native_cwd))
    project.mkdir(parents=True)
    rows = [
        {"type": "user", "uuid": str(uuid.uuid4()), "sessionId": session, "cwd": str(native_cwd),
         "timestamp": "2026-09-01T10:00:00.000Z", "message": {"role": "user", "content": "native probe session: list the files"}},
        {"type": "assistant", "uuid": str(uuid.uuid4()), "sessionId": session, "cwd": str(native_cwd),
         "timestamp": "2026-09-01T10:00:05.000Z", "message": {"role": "assistant", "model": "claude-opus-5-5",
                                                              "content": [{"type": "text", "text": "Here are the files."}]}},
    ]
    (project / f"{session}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    started = False
    try:
        harness.start(env={"SUBFLEET_DEV_APP_EXECUTABLE": str(core_probe.resolve())})
        started = True
        yield harness, session
    finally:
        harness.close()
        # Keep the state root of a failed run, with the probe's record beside it (its
        # checks, every exchange, the app's outbox), under /tmp/sf-failed as the e2e and
        # fake harnesses keep theirs; CI uploads that directory. A test's failure is not
        # raised into its fixture, so the call's report (conftest.py) says whether it failed.
        report = getattr(request.node, "rep_call", None)
        if not started or (report is not None and report.failed):
            keep = Path("/tmp/sf-failed") / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(root, keep, symlinks=True, ignore_dangling_symlinks=True, ignore=_sockets)
            for name in ("live-checks.json", "exchanges.jsonl", "app"):
                source = tmp_path / name
                if source.is_dir():
                    shutil.copytree(source, keep / "probe" / name, symlinks=True, ignore=_sockets)
                elif source.exists():
                    (keep / "probe").mkdir(exist_ok=True)
                    shutil.copy2(source, keep / "probe" / name)
            print(f"\n[frontend live] kept state root at {keep}", file=sys.stderr)
        shutil.rmtree(root, ignore_errors=True)


def test_the_app_core_drives_a_development_daemon(core_probe, tmp_path, dev_daemon):
    harness, session = dev_daemon
    image = tmp_path / "pixel.png"
    image.write_bytes(PNG)
    exchanges = tmp_path / "exchanges.jsonl"
    out = run_probe(core_probe, "live", tmp_path / "app", harness.workdir, session, image, exchanges,
                    env={"SUBFLEET_HOME": str(harness.root)}, timeout=900)
    checks = out["checks"]
    failures = [c for c in checks if not c["passed"]]
    (tmp_path / "live-checks.json").write_text(json.dumps(checks, indent=1))
    assert not failures, json.dumps(failures, indent=1)
    names = {c["name"] for c in checks}
    for required in ("the outbox sent create, first and follow-up in order", "approval.get answers the development app",
                     "the card resolves from approval.resolved", "the follow-up ends interrupted (stopped)",
                     "the resend returns the stored receipt", "resent after the right predecessor",
                     "a late copy of the withdrawn send cannot land", "the provider received the image",
                     "an approval in an unfocused conversation notifies", "a message continues the native session",
                     "a newer poll supersedes the waiting one"):
        assert required in names, required

    # The daemon's side: the person's messages chain, and the approvals were the app's.
    cid = out["notes"]["conversation_id"]
    with sqlite3.connect(f"file:{harness.root / 'conversations.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        messages = [dict(r) for r in db.execute("SELECT message_id, seq, after_message_id, origin, state FROM messages "
                                                "WHERE conversation_id=? ORDER BY seq", (cid,))]
        decisions = [json.loads(r["decision_json"]) for r in db.execute(
            "SELECT decision_json FROM approvals WHERE state='answered'")]
    people = [m for m in messages if m["origin"] == "person"]
    assert [m["after_message_id"] for m in people] == [None] + [m["message_id"] for m in people[:-1]]
    assert [m["origin"] for m in messages].count("tombstone") == 1
    assert decisions and all(d["by"]["as"] == "the Subfleet app" for d in decisions), decisions

    # Every answer the app received decodes into its model without losing a field.
    rows = [json.loads(line) for line in exchanges.read_text().splitlines()]
    assert rows and all(row["response"] for row in rows if row["op"] not in ("conversation.events", "conversation.watch"))
    seen: dict[str, int] = {}
    for row in rows:
        response = json.loads(row["response"]) if row["response"] else None
        if not response or not response["ok"] or seen.get(row["op"], 0) >= 4:
            continue
        seen[row["op"]] = seen.get(row["op"], 0) + 1
        path = write_json(tmp_path / "result.json", response["result"])
        back = json.loads(run_probe(core_probe, "roundtrip", row["op"], path, raw=True))
        assert strip_nulls(back) == strip_nulls(response["result"]), row["op"]
    assert {"capabilities", "models.list", "conversation.create", "message.submit", "conversation.open",
            "conversation.events", "conversation.watch", "message.status", "turn.interrupt", "approval.list",
            "approval.get", "approval.respond", "message.cancel", "attachment.add", "conversation.history",
            "conversation.list", "catalog.refresh"} <= set(seen)
