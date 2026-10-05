"""C-6.5, C-24.5, C-26.14: one folder is one folder, however its path is typed.

Review of 5e9f2fbd (P3-4): the daemon keyed a turn's folder by its checkout's top
level, which git spells as the volume stores it, or, outside git, by the folder as
typed. APFS is case-insensitive, so two conversations in one scratch folder, one
opened as `…/Scratch-Folder` and one as `…/scratch-folder`, held rows on two
"folders" and recorded two `target`s, and neither turn was marked as sharing the
folder with the other. Both now go through `folders.canonical`, in the daemon's
submit and in `conversation.create`, and here through the real dispatcher,
submit, admission and the start record a runner writes. So do a handoff's
workspace, a read-only turn's row, and both sides of the C-26.10 refusal of a
workspace that contains a provider home.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from subfleet import folders
from subfleet.conversations.launch import TURN_MANIFEST_KEY
from subfleet.conversations.store import ConversationError
from tests import sessions_fixtures as fx
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _live
from tests.unit import test_conversation_handoff as handoffs
from tests.unit.test_conversation_handoff import world  # noqa: F401 (a fixture)
from tests.unit.test_folders import case_insensitive

SETTINGS = {"model": "astra", "effort": None, "fast": False, "permission": "accept-edits", "auto_continue": True}


def message_in(service, workspace: str, title: str, settings: dict = SETTINGS) -> tuple[str, str]:
    """A person's message in a new Codex conversation whose workspace was recorded as
    typed (a native session's cwd, or a conversation made before `canonical`),
    dispatched: its turn job is submitted through the ordinary submit path."""
    conversations = service.conversations
    conversation, _ = conversations.store.create_conversation(
        provider="codex", workspace=workspace, workspace_kind="in-place", settings=settings, origin="new",
        title=title, lane_id=CODEX[0])
    mid = str(uuid.uuid4())
    conversations.store.submit_message(conversation_id=conversation["conversation_id"], message_id=mid,
                                       after_message_id=None, text="edit", attachments=[], settings=settings)
    conversations._dispatch()
    message = conversations.store.message(mid)
    assert message["job_id"], message
    return mid, message["job_id"]


@pytest.mark.parametrize("above", ["listable", "unlistable"])
def test_p3_4_two_spellings_of_one_scratch_folder_hold_and_record_one_folder(tmp_path, above):
    """Review of b0033e5d (P2): `unlistable` puts the folder under a directory that can
    be searched but not listed (0111), where the spelling read from listings kept each
    as typed: two lease keys, two `target`s, and neither turn marked as sharing."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        parent = harness.root / "Secret-Parent"
        scratch = parent / "Scratch-Folder"
        scratch.mkdir(parents=True)
        if not case_insensitive(harness.root):
            pytest.skip("needs a case-insensitive volume, as APFS is by default")
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        if above == "unlistable":
            parent.chmod(0o111)
        try:
            assert os.access(parent, os.R_OK) is (above == "listable")
            first, first_job = message_in(service, str(scratch), "Upper")
            second, second_job = message_in(service, str(harness.root / "sECRET-pARENT" / "scratch-folder"), "Lower")
            service._admit_turns()
            assert _live(service, first_job) and _live(service, second_job), service._holds   # I1
            folder = folders.canonical(scratch)
            assert folder.endswith("/Secret-Parent/Scratch-Folder")
            assert {holder for _, holder in folders.turn_holds(service.store.query, folder)} == {first_job,
                                                                                                second_job}
            for job_id in (first_job, second_job):
                attempt = service.store.one("SELECT a.*, j.sandbox AS job_sandbox FROM attempts a JOIN jobs j "
                                            "USING(job_id) WHERE a.job_id=?", (job_id,))
                assert json.loads(attempt["evidence_json"])["folder"] == folder
                manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
                service.conversations._record_start(manifest[TURN_MANIFEST_KEY], attempt)   # as its runner does
            store = service.conversations.store
            rows = {mid: store.turn_trees(mid) for mid in (first, second)}
            assert {row["target"] for row in rows.values()} == {folder}
            assert rows[first]["shared"] == [rows[second]["attempt_id"]]                # I4, both sides
            assert rows[second]["shared"] == [rows[first]["attempt_id"]]
        finally:
            parent.chmod(0o755)


def test_p3_4_read_only_turns_in_two_spellings_of_one_folder_hold_rows_on_one_folder(tmp_path):
    """A read-only turn's row (`worktree-read:<folder>:<job id>`, which keeps retention
    from removing the folder it reads, C-8.4) is keyed by the same one spelling as a
    writable turn's (`read_folder`, daemon.py), so `folders.turn_holds` finds both
    conversations' rows on the folder whichever case each was typed in."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        scratch = harness.root / "Scratch-Folder"
        scratch.mkdir()
        if not case_insensitive(harness.root):
            pytest.skip("needs a case-insensitive volume, as APFS is by default")
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        read_only = {**SETTINGS, "permission": "read-only"}
        _, first_job = message_in(service, str(scratch), "Upper", read_only)
        _, second_job = message_in(service, str(harness.root / "sCRATCH-fOLDER"), "Lower", read_only)
        service._admit_turns()
        assert _live(service, first_job) and _live(service, second_job), service._holds
        folder = folders.canonical(scratch)
        assert folder.endswith("/Scratch-Folder")
        assert {holder for _, holder in folders.turn_holds(service.store.query, folder, (folders.READER,))} == {
            first_job, second_job}
        assert folders.turn_folders(service.store.query) == {folder}            # retention's pins: one folder


def test_p3_4_conversation_create_records_one_spelling_and_a_home_in_any_case_is_protected(world, tmp_path,
                                                                                        monkeypatch):
    """`conversation.create` records its folder's one spelling, which every turn's `-C`,
    row and `target` then start from. The C-26.10 refusal of a writable workspace that
    contains a provider home compares one spelling on both sides: `HOME` set in another
    case than the volume stores (`…/uSER-hOME`) still names the home, and a workspace
    typed in a third case that contains its `.claude` is refused; before, `resolve()`
    kept each side's case and the two never matched."""
    if not case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive volume, as APFS is by default")
    service = world.service
    monkeypatch.setattr(service, "_person", lambda peer, what: None)       # a person in the app
    home = tmp_path / "User-Home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "uSER-hOME"))
    canonical_home = folders.canonical(home)
    assert canonical_home.endswith("/User-Home")

    asked = service.op_conversation_create({"provider": "claude", "request_id": "c-ask", "settings": handoffs.ASK,
                                            "workspace": str(tmp_path / "user-HOME")}, None)
    assert asked["conversation"]["workspace"] == canonical_home            # Ask writes nothing: not refused
    work = service.op_conversation_create({"provider": "claude", "request_id": "c-work", "settings": handoffs.ASK,
                                           "workspace": str(world.workspace.parent / "WORK")}, None)
    assert work["conversation"]["workspace"] == folders.canonical(world.workspace)
    assert work["conversation"]["workspace"].endswith("/work")

    widened = {**handoffs.ASK, "permission": "accept-edits"}
    for typed in ("user-home", "USER-HOME", "uSER-hOME"):
        with pytest.raises(ConversationError) as err:
            service.op_conversation_create({"provider": "claude", "request_id": f"c-{typed}", "settings": widened,
                                            "confirm_widen": True, "workspace": str(tmp_path / typed)}, None)
        assert err.value.reason == "protected-workspace" and err.value.code == 7, typed
        assert str(err.value).endswith(os.path.join(canonical_home, ".claude")), str(err.value)


def create(service, request_id: str, settings: dict, workspace) -> dict:
    return service.op_conversation_create({"provider": "claude", "request_id": request_id, "settings": settings,
                                           "confirm_widen": True, "workspace": str(workspace)}, None)


def test_b0033e5d_p2_a_home_typed_in_another_case_under_an_unlistable_directory_is_protected(world, tmp_path,
                                                                                          monkeypatch):
    """Review of b0033e5d (P2), the reviewer's schedule: the directory above `HOME` can
    be searched but not listed (0111) and `HOME` is typed in another case than the
    volume stores (`…/sECRET-pARENT/uSER-hOME`). The spelling read from listings kept
    the names below that directory as typed, so the home and `~/.claude` never matched
    a workspace typed another way, and a writable workspace that is the home was
    allowed. The kernel's spelling needs only search permission: the home and the
    directory above it, typed any way, are refused, and Ask (which writes nothing) is
    still recorded in the one spelling."""
    if not case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive volume, as APFS is by default")
    service = world.service
    monkeypatch.setattr(service, "_person", lambda peer, what: None)       # a person in the app
    parent = tmp_path / "Secret-Parent"
    home = parent / "User-Home"
    (home / ".claude").mkdir(parents=True)
    canonical_home = folders.canonical(home)
    assert canonical_home.endswith("/Secret-Parent/User-Home")
    monkeypatch.setenv("HOME", str(tmp_path / "sECRET-pARENT" / "uSER-hOME"))
    widened = {**handoffs.ASK, "permission": "accept-edits"}
    parent.chmod(0o111)
    try:
        assert not os.access(parent, os.R_OK)
        for n, typed in enumerate((home, parent / "user-home", tmp_path / "SECRET-PARENT" / "USER-HOME",
                                   tmp_path / "sECRET-pARENT" / "uSER-hOME", tmp_path / "secret-parent")):
            with pytest.raises(ConversationError) as err:
                create(service, f"c-{n}", widened, typed)
            assert err.value.reason == "protected-workspace" and err.value.code == 7, typed
            assert str(err.value).endswith(os.path.join(canonical_home, ".claude")), str(err.value)
        asked = create(service, "c-ask", handoffs.ASK, tmp_path / "SECRET-PARENT" / "user-HOME")
        assert asked["conversation"]["workspace"] == canonical_home
        with pytest.raises(ConversationError) as err:
            service.op_conversation_settings({"conversation_id": asked["conversation"]["conversation_id"],
                                              "settings": widened, "confirm_widen": True}, None)
        assert err.value.reason == "protected-workspace" and err.value.code == 7
        assert service.store.conversation(asked["conversation"]["conversation_id"])[
            "settings"]["permission"] == "ask"
    finally:
        parent.chmod(0o755)


def test_b0033e5d_p2_a_protected_path_that_cannot_be_spelled_refuses_a_writable_workspace(world, tmp_path,
                                                                                        monkeypatch):
    """Review of b0033e5d (P2): when a protected path's spelling cannot be established
    (here `HOME` is under a directory that cannot be searched, mode 000), whether a
    workspace contains it cannot be told, so a writable workspace is refused, saying
    which path and why; it is never compared in the spelling it was typed in. Ask
    writes nothing and is not refused."""
    service = world.service
    monkeypatch.setattr(service, "_person", lambda peer, what: None)
    sealed = tmp_path / "Sealed"
    (sealed / "User-Home" / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(sealed / "user-home"))
    widened = {**handoffs.ASK, "permission": "accept-edits"}
    sealed.chmod(0o000)
    try:
        assert create(service, "c-ask", handoffs.ASK, world.workspace)["created"]
        with pytest.raises(ConversationError) as err:
            create(service, "c-work", widened, world.workspace)
        assert err.value.reason == "protected-workspace" and err.value.code == 7
        below = os.path.join(folders.canonical(sealed), "user-home")
        assert str(err.value) == (f"cannot tell whether {folders.canonical(world.workspace)} contains "
                                  f"{os.path.join(sealed, 'user-home', '.claude')}: its spelling on the volume "
                                  f"cannot be read ({below}: Permission denied)")
        assert "searchable" in err.value.fix
    finally:
        sealed.chmod(0o755)


def test_p3_4_a_handoff_s_workspace_is_spelled_one_way(world):
    """D-18: a handoff's workspace, the one its request names or the source session's
    recorded cwd, is spelled as `conversation.create` spells it (`folders.canonical`)."""
    if not case_insensitive(world.workspace.parent):
        pytest.skip("needs a case-insensitive volume, as APFS is by default")
    typed = str(world.workspace.parent / "WORK")
    want = folders.canonical(world.workspace)
    prompt = {**fx.typed_prompt("Port the ledger importer to v2.", uuid="p0", at=fx.ago(3600)), "cwd": typed}
    fx.transcript(world.home, handoffs.SESSION, [prompt], cwd=typed)        # the session's cwd, as it typed it
    out = handoffs.handoff(world, native={"provider": "claude", "session_id": handoffs.SESSION})
    assert out["conversation"]["workspace"] == want
    named = handoffs.handoff(world, request_id="h-2", native={"provider": "claude", "session_id": handoffs.SESSION},
                             to={"provider": "codex", "settings": handoffs.CODEX, "workspace": typed.lower()})
    assert named["conversation"]["workspace"] == want
