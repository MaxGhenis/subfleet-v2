"""C-6.12, C-11.4, C-5.10: what a lane's files make an admission probe raise is the probe's,
never the pass's.

Two errors reached the admission pass from its admission probe and ended it, so C-5.10 paced
the retries and every job after the stuck one waited. Reproduced on main at 3f155e5 on
2026-09-27, and by these cases on release/217 at 0298332 on 2026-10-12:

1. `Daemon._validate_home` parsed a Codex lane's `auth.json` and called `.get` on the result.
   An empty or cut-short file raised `JSONDecodeError`, and one holding `[]` raised
   `AttributeError`, from inside `_execute_probe`. `_probe_candidate` caught only
   `AdapterError`, `OSError` and `SubprocessError`, so the error left `_prepare_route` and the
   pass. The same call raised out of an attempt's launch, and out of submit for a job pinned
   to the lane.
2. The Codex adapter's `_reset` caught only `ZoneInfoNotFoundError`. A reset clock in a zone
   that does not normalize (`America//New_York`, `America/New_York/`) made `ZoneInfo` raise
   `ValueError` out of `adapter.classify` in `_execute_probe`, and out of `_recover_probes` on
   the next pass. That probe's lease was then never released, so every detached pass after it
   ended the same way, at its top.

The roster: the fixture's `codex-1` (unmeasured, so a `hard` Astra job is probed there first,
C-11.4) and `claude-2` (measured, so a `hard` Opus job pinned to it needs no probe).
"""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, procs
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import (ClockSource, Credential, Lane, LaneOwner, Launch, OutcomeClass, Reading,
                                ReadingLabel)
from subfleet.daemon import after, utcnow
from tests.fake.test_admission_visibility import Inline
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake_adapter import FakeAdapter

#: What Codex prints when a subscription limit is reached, with the zone its reset clock names.
LIMIT_LINE = "ERROR: You've hit your usage limit. Upgrade to Pro or try again at 3:00 PM ({zone}).\n"
#: Zones `_reset`'s pattern captures that `ZoneInfo` refuses with ValueError: not normalized.
UNNORMALIZED_ZONES = ["America//New_York", "America/New_York/", "America/Indiana//Indianapolis"]
#: auth.json contents that are not a readable JSON object.
UNREADABLE = {
    "empty": b"",
    "torn": b"{",
    "cut-short": b'{"auth_mode": "chatgpt", "tokens": {"account_id": "acc',
    "list": b"[]",
    "null": b"null",
    "string": b'"chatgpt"',
    "number": b"42",
    "not-utf-8": b'{"auth_mode": "\x80"}',
}


@pytest.fixture
def fleet(routing_state, monkeypatch):  # noqa: F811
    service, harness = routing_state
    service.store.put_lane(Lane("claude-2", "claude", "claude:claude-2",
                                Credential("claude", "/fake/claude-2", "home"), "/fake/claude-2",
                                LaneOwner.V2, False))
    service.store.add_reading(Reading("claude-2", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    service.workers = Inline(service.workers)
    # A probe that never started, or whose guardian is gone, is verified empty.
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())
    return service, harness


class CodexReceipts(FakeAdapter):
    """Launches as the fake adapter does and reads a receipt as the Codex adapter does,
    binding its launch to the lane as Codex's `build_launch` does (C-9.6)."""

    def build_launch(self, job, attempt_id, attempt_dir, lane, *args):
        return dataclasses.replace(super().build_launch(job, attempt_id, attempt_dir, lane, *args),
                                   lane_id=lane.lane_id)

    def classify(self, attempt_dir, launch, exit_info):
        return CodexAdapter().classify(attempt_dir, launch, exit_info)


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def two_jobs(service, harness):
    """The job whose probe meets the lane's files, then a job of its tier pinned to claude-2."""
    stuck = submit(service, harness, tier="hard")                            # Astra, probed on codex-1
    later = submit(service, harness, pinned_lane="claude-2", pinned_model="opus", tier="hard")
    return stuck, later


def admission_pass(service):
    """One pass as the control loop runs it (C-5.10): on the worker pool, paced."""
    service._schedule("admission", service._admit, paced=True)


def log_text(service):
    service._log_handler.flush()
    return (service.root / "daemon.log").read_text()


def assert_pass_went_on(service, later):
    assert "admission" not in service._worker_failures
    assert "worker admission failed" not in log_text(service)
    attempts = service.store.list_attempts(later)
    assert [(row["state"], row["lane_id"]) for row in attempts] == [("reserved", "claude-2")]


def probe_outcome(service, job_id):
    """The outcome the job's last probe completed with, from its durable record (C-8.4)."""
    records = [json.loads(row["data_json"]) for row in service.store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' AND job_id=? ORDER BY rowid", (job_id,))]
    completed = [record for record in records if record.get("state") == "completed"]
    assert completed, records
    return completed[-1]["outcome"]


def assert_probe_settled(service, job_id):
    assert not any(row["holder"].startswith("probe:") for row in service.store.list_leases())
    assert not service.store.list_attempts(job_id)
    assert service.store.get_job(job_id)["state"] == "waiting"


def guardian_never_spawned(monkeypatch):
    real = daemon_module.subprocess.Popen

    def popen(command, **kwargs):
        if command[1:3] == ["-m", "subfleet.guardian"]:
            pytest.fail("a probe or attempt was launched")
        return real(command, **kwargs)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", popen)


def probe_ran(service, monkeypatch, *, rc=1, stderr=""):
    """The probe's guardian, mocked as the deadline cases in test_probe_turn.py mock it: it starts,
    its provider writes `stderr` and exits `rc`, and containment is verified."""
    real = daemon_module.subprocess.Popen

    def popen(command, **kwargs):
        if command[1:3] == ["-m", "subfleet.guardian"]:
            return SimpleNamespace(pid=900001, poll=lambda: 0)
        return real(command, **kwargs)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    close, write = daemon_module.os.close, daemon_module.os.write
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else close(fd))
    monkeypatch.setattr(daemon_module.os, "write", lambda fd, value: None if fd == 801 else write(fd, value))

    recover = service._await_probe

    def awaited(record, child=None):
        if child is None:
            return recover(record)               # recovery's: what is on disk (`recovered_probe`)
        Path(record["directory"], "stderr").write_text(stderr)
        record["state"] = "contained"
        service._save_probe(record)
        return True, {"rc": rc, "signal": None, "wall_s": .1, "child_pid": 900002}
    monkeypatch.setattr(service, "_await_probe", awaited)


def recovered_probe(service, job_id, monkeypatch, *, rc=1, stderr=""):
    """A probe a restart finds ended: its guardian gone, its receipt and launch on disk (C-5.3)."""
    directory = service.root / "lanes" / "codex-1" / "probes" / "fixture"
    directory.mkdir(parents=True)
    record = {"holder": "probe:fixture", "job_id": job_id, "lane_id": "codex-1",
              "model_id": "gpt-6-astra", "directory": str(directory), "state": "starting",
              "created_at": utcnow(), "deadline_at": after(60), "guardian_pid": 900001,
              "pgid": 900001, "boot_id": "boot", "proc_start": "start", "owned_identities": {}}
    assert service.store.acquire_lease("lane:codex-1:slot:0", record["holder"])
    service._save_probe(record)
    launch = Launch(("codex",), {}, (), str(directory), None, str(directory / "stdout"),
                    str(directory / "stderr"), None, None, "codex-1")
    value = dataclasses.asdict(launch)
    value.pop("env_add")
    (directory / "launch.json").write_text(json.dumps(value))
    (directory / "stderr").write_text(stderr)
    (directory / "exit.json").write_text(json.dumps({"rc": rc, "signal": None, "wall_s": .1,
                                                     "child_pid": 900002}))
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args, **kwargs: False)
    return record


# --- 1. auth.json -------------------------------------------------------------------------------


@pytest.mark.parametrize("content", UNREADABLE.values(), ids=UNREADABLE.keys())
def test_c6_12_an_unreadable_auth_json_ends_the_probe_unknown_and_the_pass_goes_on(fleet, monkeypatch, content):
    """C-6.12, C-11.4, C-5.10: before the fix the pass raised JSONDecodeError (AttributeError for
    `[]`), `worker admission failed` was logged and paced, and the job pinned to claude-2 waited.
    Now the probe ends `unknown` before anything is launched, its lease is released, the job waits
    on its probe clock (C-6.10), and the next job of its tier is placed on the same pass."""
    service, harness = fleet
    (harness.root / "home" / "auth.json").write_bytes(content)
    guardian_never_spawned(monkeypatch)
    stuck, later = two_jobs(service, harness)
    admission_pass(service)
    assert_pass_went_on(service, later)
    assert probe_outcome(service, stuck)["cls"] == "unknown"
    assert probe_outcome(service, stuck)["detail"] == "probe unavailable: AdapterError"
    assert_probe_settled(service, stuck)
    assert service.store.get_job(stuck)["wait_reason"] == "capacity"


@pytest.mark.parametrize("content", [UNREADABLE["torn"], UNREADABLE["list"]], ids=["torn", "list"])
def test_c6_12_an_unreadable_auth_json_refuses_the_launch_and_names_the_fix(fleet, monkeypatch, content):
    """C-6.12, C-5.10, C-10.2: a job that needs no probe met the same error at its launch, raised
    out of `_process_attempt`: the worker failed, was paced, and the attempt was ended a pass later
    as `reserved-no-launch`, which says nothing about why. Now the launch is refused as an API-key
    home is (C-10.2), naming the file and the fix, and no provider is started."""
    service, harness = fleet
    job_id = submit(service, harness)                                       # standard: no probe
    admission_pass(service)
    (attempt,) = service.store.list_attempts(job_id)
    assert attempt["lane_id"] == "codex-1"
    (harness.root / "home" / "auth.json").write_bytes(content)
    guardian_never_spawned(monkeypatch)
    service._schedule(attempt["attempt_id"], service._process_attempt, attempt["attempt_id"], paced=True)
    assert attempt["attempt_id"] not in service._worker_failures
    receipt = json.loads((service.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    refusal = ("lane auth.json is unreadable" if content == b"{"
               else "lane auth.json is not an object") + "; fix: log this lane in again"
    assert receipt["rc"] == 7 and receipt["spawn_error"] == refusal
    service._schedule(attempt["attempt_id"], service._process_attempt, attempt["attempt_id"], paced=True)
    job = service.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 7)                    # as an API-key home's job ends
    (notice,) = service.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,))
    assert refusal in notice["text"]


@pytest.mark.parametrize("content", [UNREADABLE["torn"], UNREADABLE["list"]], ids=["torn", "list"])
def test_c11_2_a_pin_to_a_lane_whose_auth_json_is_unreadable_is_refused_at_submit(fleet, content):
    """C-11.2, C-10.2: submit checks a pinned Codex lane's home. Before the fix a torn file was
    answered as invalid input with the parser's message (rc 2), and `[]` raised AttributeError
    out of submit. Now both are refused as an API-key home is: code 7, with the fix."""
    service, harness = fleet
    (harness.root / "home" / "auth.json").write_bytes(content)
    with pytest.raises(AdapterError, match="lane auth.json is") as error:
        submit(service, harness, pinned_lane="codex-1")
    assert error.value.code == 7
    assert error.value.fix == "log this lane in again"
    assert not service.store.list_jobs()


# --- 2. a reset clock in a zone that does not normalize ------------------------------------------


@pytest.mark.parametrize("zone", UNNORMALIZED_ZONES)
def test_c9_4_a_probe_reset_clock_in_an_unnormalized_zone_closes_on_a_guessed_clock(fleet, monkeypatch, zone):
    """C-9.4, C-11.4, C-6.12: before the fix `ZoneInfo` raised ValueError out of the Codex adapter's
    `classify` in `_execute_probe`, and the pass ended. Now the limit closes the lane on a guessed
    clock, as a limit with no reset clock does, and the next job of the tier is placed."""
    service, harness = fleet
    monkeypatch.setattr(registry, "_factories", {"codex": CodexReceipts, "claude": FakeAdapter})
    probe_ran(service, monkeypatch, stderr=LIMIT_LINE.format(zone=zone))
    stuck, later = two_jobs(service, harness)
    admission_pass(service)
    assert_pass_went_on(service, later)
    assert probe_outcome(service, stuck)["cls"] == "limited"
    (closure,) = service.store.list_closures()
    assert (closure["lane_id"], closure["clock_source"]) == ("codex-1", ClockSource.GUESSED.value)
    assert_probe_settled(service, stuck)


@pytest.mark.parametrize("zone", UNNORMALIZED_ZONES)
def test_c5_3_a_recovered_probe_reset_clock_in_an_unnormalized_zone_never_holds_its_lease(
        fleet, monkeypatch, zone):
    """C-5.3, C-9.4, C-6.12: the same receipt, read by `_recover_probes` at the top of the detached
    pass. Before the fix it raised there before any job was looked at, and the probe's lease was
    never released, so every later pass raised the same way. Now it completes `limited`."""
    service, harness = fleet
    monkeypatch.setattr(registry, "_factories", {"codex": CodexReceipts, "claude": FakeAdapter})
    stuck, later = two_jobs(service, harness)
    probe_ran(service, monkeypatch)              # had the job been probed again this pass
    record = recovered_probe(service, stuck, monkeypatch, stderr=LIMIT_LINE.format(zone=zone))
    admission_pass(service)
    assert_pass_went_on(service, later)
    assert service._probe_record(record["holder"])["outcome"]["cls"] == "limited"
    assert service.store.list_closures()[0]["clock_source"] == ClockSource.GUESSED.value
    assert not service.store.list_leases(record["holder"])


# --- any receipt an adapter cannot classify -------------------------------------------------------

#: Each carries a message that must never be recorded: a provider's text can hold a secret (C-5.10).
UNCLASSIFIABLE = [error("never-recorded-message") for error in
                  (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError, RecursionError)]


@pytest.mark.parametrize("site", ["execute", "recover"])
@pytest.mark.parametrize("error", UNCLASSIFIABLE, ids=lambda error: type(error).__name__)
def test_c6_12_a_receipt_its_adapter_cannot_classify_ends_the_probe_unknown(fleet, monkeypatch, site, error):
    """C-6.12, C-11.4, C-5.3: once containment is verified, a receipt the adapter raises on is an
    `unknown` probe that names the error's type (never its message, as C-5.10 logs), at both of the
    probe's classify sites. Before, only an OSError at the first was caught; anything else ended the
    pass, and at the second (recovery) kept the probe's lease, so every later pass ended too."""
    service, harness = fleet

    def classify(*args):
        raise error
    monkeypatch.setattr(FakeAdapter, "classify", classify)
    stuck, later = two_jobs(service, harness)
    probe_ran(service, monkeypatch)              # a recovered `unknown` lets the job probe again this pass
    if site == "recover":
        recovered_probe(service, stuck, monkeypatch)
    admission_pass(service)
    assert_pass_went_on(service, later)
    outcome = (service._probe_record("probe:fixture") if site == "recover" else {}).get("outcome") \
        or probe_outcome(service, stuck)
    assert outcome["cls"] == "unknown"
    assert outcome["detail"] == f"probe receipt could not be classified: {type(error).__name__}"
    assert outcome["evidence"]["error_type"] == type(error).__name__
    assert "never-recorded-message" not in json.dumps(outcome)
    assert "never-recorded-message" not in log_text(service)
    assert_probe_settled(service, stuck)
    assert f"its receipt could not be classified: {type(error).__name__}" in log_text(service)
