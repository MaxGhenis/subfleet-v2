"""#269 r3: distinguish admission records from independent timer/enrollment clocks."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, timers as timers_module
from subfleet.adapters.claude import ENROLL_TIMEOUT_S
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


@pytest.mark.parametrize("kind,seconds", [
    ("admission", 900), ("heal", 60), ("keepalive", 30), ("enroll", 180),
])
def test_every_emitted_record_identifies_its_probe_kind(routing_state, monkeypatch, kind, seconds):
    """Run the real writers/launch/finish paths with fake clocks and a fake guardian.

    A short usage cap must not become a heal/keepalive turn's deadline. Readers
    can select admission by the event's job_id throughout the probe lifecycle.
    """
    service, harness = routing_state
    service.policy["admission"]["probe_deadline_s"] = 900
    service.policy["caps"].update(probe_timeout_s=3, keepalive_timeout_s=30)
    start = datetime(2026, 10, 27, 6, tzinfo=timezone.utc)
    stamp = lambda at: at.isoformat(timespec="seconds").replace("+00:00", "Z")
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(start))
    monkeypatch.setattr(daemon_module, "after", lambda delay: stamp(start + timedelta(seconds=delay)))
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=start.timestamp)
    monkeypatch.setattr(daemon_module, "time", clock)
    monkeypatch.setattr(timers_module, "time", clock)
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: True)

    def spawn(command, env, read_fd, package_root):
        directory = Path(command[command.index("--attempt-dir") + 1])
        for option in ("--stdout-path", "--stderr-path"):
            Path(command[command.index(option) + 1]).write_text("")
        (directory / "exit.json").write_text(json.dumps({
            "rc": 1 if kind == "admission" else 0,
            "signal": None, "wall_s": 0, "child_pid": 900002,
        }))
        return SimpleNamespace(pid=900001, poll=lambda: 0), None

    def contain(record):
        record.update(state="contained", containment={"live_pids": [], "unverifiable": False})
        service._save_probe(record)
        return True

    monkeypatch.setattr(service, "_spawn_guardian", spawn)
    monkeypatch.setattr(service, "_contain_probe", contain)
    lane = service.store.get_lane("codex-1")
    job_id = None
    try:
        if kind == "admission":
            job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
            service._admit()
        elif kind == "heal":
            statuses = iter(["expired-token", "ok"])
            monkeypatch.setattr(service.timers, "_read_probe", lambda *args: {
                "status": next(statuses), "readings": (),
            })
            service.timers._probe_lane(lane)
        elif kind == "keepalive":
            assert service.timers._keepalive_lane(lane) == "ok"
        else:
            holder = "probe:timer:enroll:r269r3"
            service.store.acquire_lease("lane:codex-1:slot:0", holder)
            service.timers.active_holders.add(holder)
            done = service._enrollment_turn(lane.lane_id, holder, ["fake-enrollment"],
                                             cwd=str(harness.workdir), env={}, timeout=ENROLL_TIMEOUT_S)
            assert done.returncode == 0

        # Store transactions also emit empty audit events; those are not records.
        rows = service.store.query(
            "SELECT job_id,data_json FROM events WHERE kind='probe.state' AND data_json<>'{}' ORDER BY event_id")
        records = [json.loads(row["data_json"]) for row in rows]
        assert records and {record["state"] for record in records} >= {"reserved", "starting", "contained"}
        assert len({record["holder"] for record in records}) == 1
        for row, record in zip(rows, records):
            assert row["job_id"] == record["job_id"] == job_id
            assert record.get("timer_kind") == (None if kind == "admission" else kind)
            assert record["holder"].startswith("probe:")
            assert record["holder"].startswith("probe:timer:") == (kind != "admission")
            assert datetime.fromisoformat(record["deadline_at"]) - datetime.fromisoformat(
                record["created_at"]) == timedelta(seconds=seconds)
        print(json.dumps({"kind": kind, "job_id": job_id,
                          "timer_kind": records[0].get("timer_kind"),
                          "holder": records[0]["holder"], "clock_difference_s": seconds,
                          "states": [record["state"] for record in records]}))
    finally:
        for holder in tuple(service.timers.active_holders):
            service.timers._release(holder)
