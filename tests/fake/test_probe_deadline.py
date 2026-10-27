"""D-PROBE-DEADLINE1: admission deadlines use fake time and fake providers only."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.contracts import Outcome, OutcomeClass, PROBE_RETRY_S
from subfleet.policy import admission_settings, load_policy
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.fake.test_probe_turn import fleet


@pytest.mark.parametrize("deadline_s, expected", [(None, "ok"), (60, "unknown")])
def test_a_probe_answering_at_90_seconds_uses_the_admission_deadline(routing_state, monkeypatch,
                                                                  deadline_s, expected):
    """C-11.4: 90 s succeeds by default; an explicit 60 s stops the same provider."""
    service, harness = routing_state
    _, record, _ = run_probe(service, harness, monkeypatch, deadline_s=deadline_s)
    assert record["outcome"]["cls"] == expected
    assert record["outcome"]["evidence"].get("stopped") == ("deadline" if deadline_s == 60 else None)


def run_probe(service, harness, monkeypatch, *, deadline_s=None, answer_s=90, reload_to=None):
    """Fake guardian, real launch/await/classification; no process is started."""
    if deadline_s is not None:
        service.policy["admission"]["probe_deadline_s"] = deadline_s
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    start = datetime.now(timezone.utc).replace(microsecond=0)
    clock = {"now": start}
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(clock["now"]))
    monkeypatch.setattr(daemon_module, "after", lambda seconds: stamp(clock["now"] + timedelta(seconds=seconds)))
    holders, reserved = [], []

    def wait(seconds):
        clock["now"] += timedelta(seconds=1)
        if reload_to is not None and clock["now"] == start + timedelta(seconds=20):
            path = harness.root / "policy.json"
            value = json.loads(path.read_text())
            value["admission"]["probe_deadline_s"] = reload_to
            path.write_text(json.dumps(value))
            service.policy = load_policy(path)
            service.timers.policy = service.policy
            # Reloading policy cannot rewrite the already durable clocks.
            record = service._probe_record(holders[0])
            assert (record["created_at"], record["deadline_at"]) == reserved[0]
    monkeypatch.setattr(service.stopping, "wait", wait)
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: True)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
    monkeypatch.setattr(service, "_new_group_identities", lambda *args: {})

    # Exercise the real launch, gate, await, classify and finish paths. Only the
    # guardian process is replaced: it publishes its fake receipt at fake t=90.
    def spawn(command, env, read_fd, package_root):
        assert env["SUBFLEET_PROBE"] == "1"
        directory = Path(command[command.index("--attempt-dir") + 1])
        assert (directory / "prompt.md").read_text() == "Reply with exactly OK. Do not use tools.\n"
        holders.append(env["SUBFLEET_ATTEMPT"])
        record = service._probe_record(holders[-1])
        reserved.append((record["created_at"], record["deadline_at"]))

        def poll():
            if clock["now"] >= start + timedelta(seconds=answer_s):
                (directory / "exit.json").write_text(json.dumps(
                    {"rc": 0, "signal": None, "wall_s": answer_s, "child_pid": 900002}))
                return 0
            return None
        return SimpleNamespace(pid=900001, poll=poll), None

    def contain(record):
        record.update(state="contained", containment={"live_pids": [], "unverifiable": False})
        service._save_probe(record)
        return True

    monkeypatch.setattr(service, "_spawn_guardian", spawn)
    monkeypatch.setattr(service, "_contain_probe", contain)
    service._admit()
    record = service._probe_record(holders[0])
    event, = service.store.query("SELECT data_json FROM events WHERE kind='probe.completed' AND job_id=?", (job_id,))
    result = json.loads(event["data_json"])
    assert result["class"] == record["outcome"]["cls"]
    assert result["evidence"] == record["outcome"]["evidence"]
    assert (record["created_at"], record["deadline_at"]) == reserved[0]
    return job_id, record, clock["now"]


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("deadline_s, expected", [(60, "unknown"), (180, "ok")])
def test_policy_reload_cannot_move_a_reserved_probe_deadline(routing_state, monkeypatch, deadline_s, expected):
    """C-11.4: both a longer and a shorter reloaded policy leave the running probe alone."""
    service, harness = routing_state
    _, record, _ = run_probe(service, harness, monkeypatch, deadline_s=deadline_s,
                             reload_to=300 if deadline_s == 60 else 1)
    assert record["outcome"]["cls"] == expected
    assert datetime.fromisoformat(record["deadline_at"]) - datetime.fromisoformat(record["created_at"]) == timedelta(seconds=deadline_s)


def test_a_reloaded_policy_applies_to_the_next_reservation(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, first, _ = run_probe(service, harness, monkeypatch, deadline_s=60, answer_s=1000, reload_to=300)
    service.dispatch("kill", {"job_id": job_id})
    next_job = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    records = []

    def probe(job, lane, model, holder):
        records.append(service._probe_record(holder))
        return Outcome(OutcomeClass.UNKNOWN, "fake spawn failure")
    monkeypatch.setattr(service, "_execute_probe", probe)
    service._admit()
    second, = records
    assert second["job_id"] == next_job
    assert datetime.fromisoformat(second["deadline_at"]) - datetime.fromisoformat(second["created_at"]) == timedelta(seconds=300)
    assert service._probe_record(first["holder"])["deadline_at"] == first["deadline_at"]


@pytest.mark.parametrize("deadline_s", [1, 60, 180, 300, 900])
@pytest.mark.parametrize("lock_wait_s", [0, 45])
def test_both_reservation_sites_use_the_policy_deadline_after_a_writer_wait(
        routing_state, monkeypatch, deadline_s, lock_wait_s):
    """C-11.4: provisional and durable clocks use one key; a writer wait spends neither."""
    service, harness = routing_state
    service.policy["admission"]["probe_deadline_s"] = deadline_s
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    start = datetime.now(timezone.utc).replace(microsecond=0)
    clock, additions = {"now": start}, []
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(clock["now"]))
    transaction, later = service.store.transaction, daemon_module._later

    def adding(created_at, seconds):
        additions.append((created_at, seconds))
        return later(created_at, seconds)
    monkeypatch.setattr(daemon_module, "_later", adding)

    @contextmanager
    def after_writer_wait(kind="state.changed", **kwargs):
        if kind == "probe.reserved":
            clock["now"] += timedelta(seconds=lock_wait_s)
        with transaction(kind, **kwargs) as tx:
            yield tx
    monkeypatch.setattr(service.store, "transaction", after_writer_wait)

    def probe(job, lane, model, holder):
        assert additions == [(stamp(start), deadline_s), (stamp(clock["now"]), deadline_s)]
        record = service._probe_record(holder)
        assert record["created_at"] == stamp(clock["now"])
        assert record["deadline_at"] == stamp(clock["now"] + timedelta(seconds=deadline_s))
        return Outcome(OutcomeClass.UNKNOWN, "fake spawn failure")
    monkeypatch.setattr(service, "_execute_probe", probe)
    service._admit()
    assert service.store.get_job(job_id)["next_check_at"] == stamp(clock["now"] + timedelta(seconds=60))


def test_a_deadline_stopped_probe_is_due_at_once(routing_state, monkeypatch):
    """C-6.10: extending a probe never adds another retry interval after its end."""
    service, harness = routing_state
    job_id, record, now = run_probe(service, harness, monkeypatch, answer_s=1000)
    assert record["outcome"]["evidence"]["stopped"] == "deadline"
    created = datetime.fromisoformat(record["created_at"])
    assert now - created == timedelta(seconds=admission_settings(service.policy)["probe_deadline_s"])
    assert service.store.get_job(job_id)["next_check_at"] == stamp(max(now, created + timedelta(seconds=60)))
    assert PROBE_RETRY_S == 60


@settings(max_examples=60, deadline=None, derandomize=True, database=None)
@example(deadline_s=1, reserved_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
@example(deadline_s=900, reserved_at=datetime(2026, 10, 26, 23, 59, 59, tzinfo=timezone.utc))
@given(deadline_s=st.integers(1, 900),
       reserved_at=st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2099, 1, 1),
                                timezones=st.just(timezone.utc)).map(lambda at: at.replace(microsecond=0)))
def test_any_probe_deadline_is_added_at_reservation_and_retry_stays_independent(deadline_s, reserved_at):
    """C-11.4, C-6.10: a reservation at a whole second adds the whole-second key exactly,
    so `deadline_at` is a whole second too, the precision `utcnow()` is compared at."""
    with fleet() as (service, harness, patch):
        service.policy["admission"]["probe_deadline_s"] = deadline_s
        patch.setattr(daemon_module, "utcnow", lambda: stamp(reserved_at))
        job_id = service.dispatch("submit", harness.submit_args(tier="hard", pinned_lane="codex-1"))["job_id"]
        records = []

        def probe(job, lane, model, holder):
            records.append(service._probe_record(holder))
            return Outcome(OutcomeClass.UNKNOWN, "fake spawn failure")
        patch.setattr(service, "_execute_probe", probe)
        service._admit()
        record, = records
        created = datetime.fromisoformat(record["created_at"])
        assert created == reserved_at
        assert datetime.fromisoformat(record["deadline_at"]) == created + timedelta(seconds=deadline_s)
        # The durable string itself is whole-second: `_await_probe` compares strings.
        assert record["deadline_at"] == stamp(created + timedelta(seconds=deadline_s))
        next_check = datetime.fromisoformat(service.store.get_job(job_id)["next_check_at"])
        assert next_check >= created + timedelta(seconds=60)
        assert next_check == created + timedelta(seconds=60)
