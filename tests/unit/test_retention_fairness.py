"""C-8.4: slow unknown salvage cannot monopolize every bounded retention pass."""

from types import SimpleNamespace

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store


@pytest.mark.parametrize("proof_end", ["unknown", "deadline"])
@pytest.mark.parametrize("unknown_count,retry_gap", [(4, 0), (20, 60)])
def test_slow_unknown_salvage_does_not_starve_later_prunable_job(tmp_path, monkeypatch, proof_end,
                                                              unknown_count, retry_gap):
    """Repeated proof timeouts retain unknown snapshots while other work progresses."""
    clock = SimpleNamespace(now=0.0, deadline=60.0)
    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        unknown = [f"old-{index}" for index in range(unknown_count)]
        for index, identity in enumerate([*unknown, "later"]):
            store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                          workdir=str(tmp_path), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                          created_at=f"2026-01-01T00:00:{index:02d}Z", finished_at="2026-01-02T00:00:00Z")
            directory = tmp_path / "jobs" / identity
            directory.mkdir(parents=True)
            (directory / "stdout").write_text("retained output")
            if identity in unknown:
                attempt = identity + "/a1"
                store.add_attempt(attempt_id=attempt, job_id=identity, seq=1, lane_id="codex-1",
                                  model_requested="gpt-6-astra", state="succeeded")
                store.add_artifact(attempt, "salvage", "refs/subfleet-salvage/" + identity, "unknown", 0)

        calls_before_pruning = []

        def slow_unknown(artifact):
            assert not store.connection.in_transaction
            if store.get_job("later") is not None:
                calls_before_pruning.append(artifact["attempt_id"])
            clock.now += 15  # the per-Git-command cap in a 60-second pass
            if proof_end == "deadline":
                retention._checkpoint(None, clock.deadline)
            return False

        passes = []
        for _ in range(unknown_count + 2):
            clock.deadline = clock.now + 60
            result = retention.maintenance(store, tmp_path, max_jobs=0,
                                           salvage_referenced_elsewhere=slow_unknown,
                                           deadline=clock.deadline)
            passes.append(result)
            if store.get_job("later") is None:
                break
            clock.now += retry_gap  # daemon worker retry backoff can reach 60 s
        else:
            pytest.fail("retrying the same unknown salvage proofs consumed every deadline")

        assert passes[0]["interrupted"] == "deadline"
        assert passes[-1]["pruned"] == ["later"]
        assert {job["job_id"] for job in store.list_jobs()} == set(unknown)
        assert all((tmp_path / "jobs" / identity / "stdout").exists() for identity in unknown)
        assert len(calls_before_pruning) == unknown_count, "failed proofs must yield to unattempted candidates"
