"""Review 4: C-8.4 distinguishes workspace and job-directory readers."""
from pathlib import Path

import pytest

from subfleet import folders, retention
from subfleet.store import Store


@pytest.mark.parametrize("location", ["workspace", "job-directory"])
def test_c84_claim_that_a_turn_row_keeps_no_in_place_job(tmp_path, location):
    """The in-place exception concerns the workspace, not the job directory."""
    root = tmp_path / "state"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_folder = root / "jobs" / "retired"
    job_folder.mkdir(parents=True)
    store = Store(root / "state.sqlite3")
    try:
        store.add_job(job_id="retired", request_id="retired", payload_digest="d",
                      kind="dispatch", state="succeeded", workdir=str(workspace),
                      prompt_path="/prompt", sandbox="workspace-write", in_place=1)
        folder = workspace if location == "workspace" else job_folder
        store.acquire_lease(folders.turn_key(str(folder), "live-turn", writable=False), "live-turn")
        reasons = retention._pin_reasons(store, set(), None, only="retired", root=root)
        assert reasons == ({"retired": "turn-folder"} if location == "job-directory" else {})
        contract = Path("docs/acceptance-contract.md").read_text()
        c84 = next(line for line in contract.splitlines() if line.startswith("- **C-8.4**"))
        assert "a row on the in-place workspace keeps no in-place job" in c84
        assert "Writer reservation, like a turn's" not in contract
    finally:
        store.close()
