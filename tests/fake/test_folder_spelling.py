"""C-6.5, C-24.5, C-26.14: one folder is one folder, however its path is typed.

Review of 5e9f2fbd (P3-4): the daemon keyed a turn's folder by its checkout's top
level, which git spells as the volume stores it, or, outside git, by the folder as
typed. APFS is case-insensitive, so two conversations in one scratch folder, one
opened as `…/Scratch-Folder` and one as `…/scratch-folder`, held rows on two
"folders" and recorded two `target`s, and neither turn was marked as sharing the
folder with the other. Both now go through `folders.canonical`, in the daemon's
submit and in `conversation.create`, and here through the real dispatcher,
submit, admission and the start record a runner writes.
"""

from __future__ import annotations

import json
import uuid

import pytest

from subfleet import folders
from subfleet.conversations.launch import TURN_MANIFEST_KEY
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _live

SETTINGS = {"model": "astra", "effort": None, "fast": False, "permission": "accept-edits", "auto_continue": True}


def message_in(service, workspace: str, title: str) -> tuple[str, str]:
    """A person's message in a new Codex conversation whose workspace was recorded as
    typed (a native session's cwd, or a conversation made before `canonical`),
    dispatched: its turn job is submitted through the ordinary submit path."""
    conversations = service.conversations
    conversation, _ = conversations.store.create_conversation(
        provider="codex", workspace=workspace, workspace_kind="in-place", settings=SETTINGS, origin="new",
        title=title, lane_id=CODEX[0])
    mid = str(uuid.uuid4())
    conversations.store.submit_message(conversation_id=conversation["conversation_id"], message_id=mid,
                                       after_message_id=None, text="edit", attachments=[], settings=SETTINGS)
    conversations._dispatch()
    message = conversations.store.message(mid)
    assert message["job_id"], message
    return mid, message["job_id"]


def test_p3_4_two_spellings_of_one_scratch_folder_hold_and_record_one_folder(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        scratch = harness.root / "Scratch-Folder"
        scratch.mkdir()
        if not (harness.root / "sCRATCH-fOLDER").is_dir():
            pytest.skip("needs a case-insensitive volume, as APFS is by default")
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        first, first_job = message_in(service, str(scratch), "Upper")
        second, second_job = message_in(service, str(harness.root / "scratch-folder"), "Lower")
        service._admit_turns()
        assert _live(service, first_job) and _live(service, second_job), service._holds       # I1
        folder = folders.canonical(scratch)
        assert folder.endswith("/Scratch-Folder")
        assert {holder for _, holder in folders.turn_holds(service.store.query, folder)} == {first_job, second_job}
        for job_id in (first_job, second_job):
            attempt = service.store.one("SELECT a.*, j.sandbox AS job_sandbox FROM attempts a JOIN jobs j "
                                        "USING(job_id) WHERE a.job_id=?", (job_id,))
            assert json.loads(attempt["evidence_json"])["folder"] == folder
            manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
            service.conversations._record_start(manifest[TURN_MANIFEST_KEY], attempt)   # as its runner does
        store = service.conversations.store
        rows = {mid: store.turn_trees(mid) for mid in (first, second)}
        assert {row["target"] for row in rows.values()} == {folder}
        assert rows[first]["shared"] == [rows[second]["attempt_id"]]                    # I4, both sides
        assert rows[second]["shared"] == [rows[first]["attempt_id"]]
