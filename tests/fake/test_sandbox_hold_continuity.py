"""C-11.10: overlapping task and sandbox holds preserve one continuous pause.

An admission look can have passed the door before a hold committed, then open
its pin episode before a later sandbox hold commits. Its delayed writer must
preserve all 60 seconds even after the earlier task hold has expired.
"""

from __future__ import annotations

import contextlib

import pytest

from tests.disk_floor_model import stamp
from tests.task_hold_support import attempts, fresh_daemon, hold, job, submit


@pytest.mark.parametrize("writer_at", [21, 110], ids=["both-holds-active", "earlier-hold-expired"])
def test_stale_pin_opening_preserves_the_pause_across_overlapping_holds(monkeypatch, writer_at):
    with fresh_daemon() as (service, harness, clock, signals):
        service.policy["admission"]["pin_grace_s"] = 60
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "overlapping-holds", pinned_lane="codex-1")
        # Supply a valid persisted writable job without allocating a worktree:
        # this regression exercises only the stale admission writer and clocks.
        service.store.update_job(build, sandbox="workspace-write")
        stale = job(service, build)
        hold(service, "build", stamp(100))
        clock[0] = 10
        original = service.store.transaction
        intercepted = False

        @contextlib.contextmanager
        def delayed_writer(kind, *args, **kwargs):
            nonlocal intercepted
            if kind == "job.pin_unadmittable" and not intercepted:
                intercepted = True
                # _stuck_pin has formed an episode since 10, due 70, but has
                # not acquired the writer. A later hold now extends coverage.
                assert kwargs["data"]["since"] == stamp(10)
                assert kwargs["data"]["fail_at"] == stamp(70)
                clock[0] = 20
                service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write",
                                            "until": stamp(200), "note": "disk pressure booking"})
                clock[0] = writer_at
            with original(kind, *args, **kwargs) as tx:
                yield tx

        monkeypatch.setattr(service.store, "transaction", delayed_writer)
        service._stuck_pin(stale, {"lane_id": "codex-1", "reasons": ["disabled"], "closures": []}, {})
        assert intercepted
        assert (job(service, build)["wait_reason"], job(service, build)["next_check_at"]) == (
            "task-held", stamp(200))
        episode = service._load_pin_episodes(build)[build]
        assert (episode["since"], episode["paused_s"]) == (stamp(10), 60)
        # Internal composition must preserve each operator row's own origin.
        rows = service.store.query("SELECT lease_key, acquired_at FROM leases WHERE holder LIKE 'operator: %'")
        assert {row["lease_key"]: row["acquired_at"] for row in rows} == {
            "task-hold:build": stamp(0), "sandbox-hold:workspace-write": stamp(20)}
        clock[0] = 120
        released = service.dispatch("tasks", {"action": "release", "sandbox": "workspace-write"})
        assert released["woken"] == 1
        assert (job(service, build)["wait_reason"], job(service, build)["next_check_at"]) == (
            "capacity", stamp(180))
        assert service._load_pin_episodes(build)[build]["fail_at"] == stamp(180)
        assert attempts(service, build) == []
        assert signals == []
