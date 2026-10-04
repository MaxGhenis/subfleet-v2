"""Regressions for the REQUEST CHANGES review of PR 127 at 7504f3b8."""
import json

from subfleet.conversations import history
from subfleet.adapters.claude import encode_project_dir
from tests.unit.test_conversation_service import svc, submit  # noqa: F401
from tests.unit.test_conversation_wakes import bound


def test_history_before_first_catalog_pass(svc, monkeypatch):
    cid = bound(svc)
    mid = submit(svc, cid)
    sid = svc.store.conversation(cid)["native_session_id"]
    projects = svc.root / "native-projects"
    path = projects / encode_project_dir(svc.test_workspace) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "uuid": mid, "message": {"content": "hello"}}) + "\n")
    monkeypatch.setattr(history.transcripts, "projects_dir", lambda: projects)
    monkeypatch.setattr(history.transcripts, "transcript_path", lambda *a: path)
    page = svc.op_conversation_history({"conversation_id": cid}, None)
    assert page["items"] and page["items"][0]["id"] == mid
    assert page["items"][0]["source"] == "subfleet"

