"""The legacy cockpit import end to end (C-30.4, design §13).

`python -m subfleet.importer --legacy-cockpit` runs against a synthetic v1 state
directory whose outbox has the cockpit's real schema and shapes
(`tests/legacy_fixtures.py`), before the daemon starts; then the
real daemon, guardian, relay and driver, with the fake interactive Claude on
PATH, open the imported conversation by its native session and continue it.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from tests.e2e.test_conversations import Conversations
from tests.legacy_fixtures import outbox_row, write_outbox, write_transcript

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


def git(env: dict, cwd: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def test_imported_history_reopens_and_its_session_continues_without_resending_it(e2e, tmp_path):
    """C-30.4, C-30.2, C-24.5, C-26.1: the import writes terminal history into a
    `legacy` conversation bound to the native session; the daemon opens it by
    that session, shows the history, dispatches none of it, and a person's next
    message resumes the same native session as the only message sent."""
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    # A workspace outside /tmp (IR-15), on a feature branch (C-26.10).
    workspace = (tmp_path / "work").resolve()
    workspace.mkdir()
    git(e2e.env, workspace, "init", "-b", "feature/legacy")
    (workspace / "tracked.txt").write_text("baseline\n")
    git(e2e.env, workspace, "add", "tracked.txt")
    git(e2e.env, workspace, "commit", "-m", "C-30.4 legacy e2e baseline")

    claude = Path(e2e.env["HOME"]) / ".claude"
    session, other = str(uuid.uuid4()), str(uuid.uuid4())
    write_transcript(claude, session, workspace)
    first, second, queued = (str(uuid.uuid4()) for _ in range(3))
    v1 = tmp_path / "v1-state"
    v1.mkdir()
    write_outbox(v1, [
        outbox_row(first, session, "finished", "the first cockpit message", at=900),
        outbox_row(second, session, "error", "the second cockpit message", at=600),
        outbox_row(queued, other, "queued", "never sent", at=300),
    ])

    imported = subprocess.run(
        [sys.executable, "-m", "subfleet.importer", "--legacy-cockpit", "--state-root", str(e2e.root),
         "--v1-state", str(v1), "--claude-dir", str(claude), "--json"],
        cwd=Path(__file__).resolve().parents[2], env=e2e.env, capture_output=True, text=True, timeout=60)
    assert imported.returncode == 0, imported.stderr
    report = json.loads(imported.stdout)
    assert {item["message_id"]: item["disposition"] for item in report["stores"]["outbox"]["items"]} == {
        first: "history", second: "history", queued: "legacy-owned"}

    e2e.start()
    conv = Conversations(e2e)
    opened = conv.call("conversation.open", native={"provider": "claude", "session_id": session})
    conversation = opened["conversation"]
    assert conversation["origin"] == "legacy" and conversation["native_session_id"] == session
    assert conversation["workspace"] == str(workspace) and conversation["active"] is False
    assert [(m["message_id"], m["origin"], m["state"]) for m in opened["messages"]] == [
        (first, "legacy", "complete"), (second, "legacy", "failed")]
    cid = conversation["conversation_id"]

    follow = conv.submit(cid, "continue from where the cockpit left off")
    done = conv.until_state(follow, "complete", "failed", "delivery-unknown")
    assert done["state"] == "complete", done

    launches = [row["argv"] for row in conv.turn_log() if "argv" in row and "--input-format" in row["argv"]]
    assert len(launches) == 1 and launches[0][launches[0].index("--resume") + 1] == session
    sent = [row["uuid"] for row in conv.stdin_rows() if row.get("type") == "user"]
    assert sent == [follow]                                  # history is never sent again
    turns = [row["request_id"] for row in e2e.rows("SELECT request_id FROM jobs WHERE kind='turn'")]
    assert turns == [f"turn:{follow}:0"]
    states = {m["message_id"]: m["state"] for m in conv.call("message.status",
                                                              message_ids=[first, second, queued])["messages"]}
    assert states == {first: "complete", second: "failed", queued: "unknown"}
