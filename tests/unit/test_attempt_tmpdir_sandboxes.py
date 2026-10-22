"""D-TMP1: Claude's writable tool modes explicitly include attempt scratch."""

from pathlib import Path

import pytest

from subfleet.contracts import Sandbox
from subfleet.conversations.launch import claude_launch
from tests.conftest import make_job, make_lane


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("sandbox", [Sandbox.WORKSPACE_WRITE, Sandbox.READ_ONLY])
def test_claude_job_scratch_directory_follows_its_permission_mode(tmp_path, adapter, resumed, sandbox):
    prompt = tmp_path / "prompt"
    prompt.write_text("Dummy prompt")
    scratch = tmp_path / "private-scratch"
    scratch.mkdir()
    credential = {"CLAUDE_CODE_OAUTH_TOKEN": "dummy", "TMPDIR": str(scratch)}
    job = make_job(str(tmp_path), str(prompt), sandbox=sandbox)
    if resumed:
        launch = adapter.resume_launch(job, "job/a1", tmp_path, make_lane(), credential,
                                       "dummy-session", prompt, None, "dummy-model")
    else:
        launch = adapter.build_launch(job, "job/a1", tmp_path, make_lane(), credential,
                                      "dummy-model", None, prompt, None)
    if sandbox == Sandbox.WORKSPACE_WRITE:
        assert launch.argv[launch.argv.index("--add-dir") + 1] == str(scratch)
    else:
        assert "--add-dir" not in launch.argv


@pytest.mark.parametrize("permission", ["ask", "accept-edits", "bypass", "read-only"])
@pytest.mark.parametrize("resumed", [False, True])
def test_claude_turn_scratch_directory_follows_its_permission_mode(tmp_path, adapter, permission, resumed):
    scratch = tmp_path / "private-scratch"
    scratch.mkdir()
    turn = {"provider": "claude", "conversation_id": "conversation", "message_id": "message",
            "text": "Dummy prompt", "cwd": str(tmp_path),
            "native_session_id" if resumed else "new_session_id": "dummy-session",
            "settings": {"model": "dummy-model", "permission": permission}}
    launch = claude_launch(turn, attempt_id="turn/a1", attempt_dir=tmp_path, lane=make_lane(),
                           credential_env={"CLAUDE_CODE_OAUTH_TOKEN": "dummy", "TMPDIR": str(scratch)},
                           model_id="dummy-model", adapter=adapter)
    if permission != "read-only":
        assert launch.argv[launch.argv.index("--add-dir") + 1] == str(scratch)
    else:
        assert "--add-dir" not in launch.argv
