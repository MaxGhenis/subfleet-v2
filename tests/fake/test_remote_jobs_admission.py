"""C-17.10: Remote records leave local job admission, retention and wait alone."""

from contextlib import closing
import json

import pytest

from subfleet import cli, daemon as daemon_module, retention
from subfleet.disk import GB
from tests.fake.conftest import Harness
from tests.fake.test_state_contract import state_daemon  # noqa: F401


SYNTHETIC_URL = "https://chatgpt.com/codex/tasks/task_Synthetic_1"
CALLER = "fake-session"


def use_cli(daemon, monkeypatch):
    """Keep the CLI and daemon handlers real and replace only socket transport."""
    monkeypatch.setenv("SUBFLEET_HOME", str(daemon.root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)

    class LocalClient:
        def call(self, op, args, **kwargs):
            if op == "wait":
                # Check the resolved set without sleeping if a live job leaks in.
                answer = daemon.dispatch(op, {**args, "deadline_s": 0})
                assert not answer["timeout"], answer
                return answer
            return daemon.dispatch(op, args)

    def client(args, **kwargs):
        assert cli._root(args) == daemon.root
        return LocalClient()

    monkeypatch.setattr(cli, "_client", client)


def adopt():
    assert cli.main(["remote", "adopt", SYNTHETIC_URL, "--account", "codex-1"]) == 0


def completed_job(daemon, harness):
    ended = "2026-09-24T10:00:00Z"
    return daemon.store.add_job(job_id="completed-local", request_id="completed-local",
                                payload_digest="digest", kind="dispatch", state="succeeded",
                                sandbox="read-only", workdir=str(harness.workdir),
                                prompt_path=str(harness.root / "prompt.md"), caller_session=CALLER,
                                created_at=ended, finished_at=ended, rc=0)


def test_remote_adoption_keeps_local_admission_and_disk_reserve(state_daemon, tmp_path, monkeypatch):
    control, control_harness = state_daemon
    other_root = tmp_path / "with-remote"
    other_root.mkdir()
    other_harness = Harness(other_root)
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)
    monkeypatch.setattr("subfleet.disk.free_bytes", lambda path: 46 * GB)

    def place(daemon, harness, *, with_remote):
        daemon.policy["caps"]["max_in_flight_per_lane"] = 1
        daemon.policy["admission"]["disk"]["enabled"] = True
        workspace_ids = []

        def workspace(job):
            workspace_ids.append(job["job_id"])
            return job["workdir"], None, None, []

        monkeypatch.setattr(daemon, "_workspace", workspace)
        job_ids = [daemon.dispatch("submit", harness.submit_args(name=f"queued-{n}"))["job_id"]
                   for n in range(3)]
        if with_remote:
            leases = daemon.store.query("SELECT * FROM leases ORDER BY lease_key")
            use_cli(daemon, monkeypatch)
            adopt()
            assert daemon.store.query("SELECT * FROM leases ORDER BY lease_key") == leases
        assert {row["job_id"] for row in daemon.store.list_jobs()} == set(job_ids)
        daemon._admit()
        attempts = daemon.store.query("SELECT job_id, lane_id, state FROM attempts")
        assert workspace_ids[0] == job_ids[0]
        assert set(workspace_ids) <= set(job_ids)
        assert daemon.store.get_job(job_ids[0])["state"] == "running"
        assert all(daemon.store.get_job(job_id)["state"] in {"queued", "waiting"} for job_id in job_ids[1:])
        return ([(job_ids.index(row["job_id"]), row["lane_id"], row["state"]) for row in attempts],
                daemon._disk.reserved_bytes)

    expected = place(control, control_harness, with_remote=False)
    assert expected == ([(0, "codex-1", "reserved")], 1.5 * GB)
    with closing(daemon_module.Daemon(other_root)) as with_remote:
        monkeypatch.setattr(with_remote, "_launch", control._launch)
        assert place(with_remote, other_harness, with_remote=True) == expected


def test_remote_adoption_is_invisible_to_retention(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    local = completed_job(daemon, harness)
    directory = daemon.root / "jobs" / local
    directory.mkdir(parents=True)
    (directory / "stdout").write_text("local output\n")

    def retain():
        return retention.maintenance(daemon.store, daemon.root, max_jobs=1, holders=lambda *args, **kwargs: {})

    before = retain()
    assert before["pools"]["detached"]["jobs_before"] == before["jobs_after"] == 1
    assert before["pruned"] == []
    use_cli(daemon, monkeypatch)
    adopt()
    after = retain()
    for field in ("pools", "jobs_after", "pruned", "protected"):
        assert after[field] == before[field]
    assert (directory / "stdout").read_text() == "local output\n"
    assert len(daemon.dispatch("remote.list", {})["remote_jobs"]) == 1


def test_wait_mine_returns_the_same_job_after_remote_adoption(state_daemon, monkeypatch, capsys):
    daemon, harness = state_daemon
    local = completed_job(daemon, harness)
    use_cli(daemon, monkeypatch)

    assert cli.main(["wait", "--mine", "--json"]) == 0
    before = capsys.readouterr()
    assert json.loads(before.out)["job_id"] == local
    adopt()
    capsys.readouterr()
    assert cli.main(["wait", "--mine", "--json"]) == 0
    assert capsys.readouterr() == before
