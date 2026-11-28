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
import sqlite3
import time
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


def probe_ran(service, monkeypatch, *, rc=1, stderr="", publish_receipt=None):
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
        if publish_receipt is not None:
            publish_receipt(Path(record["directory"]) / "exit.json")
            return recover(record, child)
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
    """C-6.12, C-5.10, C-6.5: a job that needs no probe met the same error at its launch, raised
    out of `_process_attempt`: the worker failed, was paced, and the attempt was ended a pass later
    as `reserved-no-launch`, which says nothing about why. Now the launch is refused as an API-key
    home is (C-6.5), naming the file and the fix, and no provider is started."""
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
    # The launch stand-in forbids a guardian, so finalization has no processes to find.
    monkeypatch.setattr(service, "_contain", lambda attempt: procs.Containment())
    service._schedule(attempt["attempt_id"], service._process_attempt, attempt["attempt_id"], paced=True)
    job = service.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 7)                    # as an API-key home's job ends
    (notice,) = service.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,))
    assert refusal in notice["text"]


@pytest.mark.parametrize("content", [UNREADABLE["torn"], UNREADABLE["list"]], ids=["torn", "list"])
def test_c11_2_a_pin_to_a_lane_whose_auth_json_is_unreadable_is_refused_at_submit(fleet, content):
    """C-11.2, C-6.5: submit checks a pinned Codex lane's home. Before the fix a torn file was
    answered as invalid input with the parser's message (rc 2), and `[]` raised AttributeError
    out of submit. Now both are refused as an API-key home is: code 7, with the fix."""
    service, harness = fleet
    (harness.root / "home" / "auth.json").write_bytes(content)
    with pytest.raises(AdapterError, match="lane auth.json is") as error:
        submit(service, harness, pinned_lane="codex-1")
    assert error.value.code == 7
    assert error.value.fix == "log this lane in again"
    assert not service.store.list_jobs()


def _claude_probe_jobs(service, harness, credential, home=None):
    service.store.put_lane(Lane("claude-1", "claude", "claude:fixture", credential, home,
                                LaneOwner.V2, False))
    stuck = submit(service, harness, pinned_lane="claude-1", pinned_model="opus", tier="hard")
    later = submit(service, harness, pinned_lane="claude-2", pinned_model="opus", tier="hard")
    return stuck, later


def _assert_early_probe_error(service, stuck, later, error_type):
    admission_pass(service)
    assert_pass_went_on(service, later)
    outcome = probe_outcome(service, stuck)
    assert outcome["cls"] == "unknown"
    assert outcome["detail"] == f"probe unavailable: {error_type}"
    assert_probe_settled(service, stuck)


@pytest.mark.parametrize(("stdout", "error_type"), [
    (b"\xff", "UnicodeDecodeError"),
    (b"synthetic\x00token", "ValueError"),
], ids=["non-utf8", "nul"])
def test_c6_12_a_broken_claude_credential_ends_only_the_probe(fleet, monkeypatch, stdout, error_type):
    """The helper's text decode or the guardian's Popen fails before classification."""
    import sys

    service, harness = fleet
    helper = harness.root / "fixture-credential-helper"
    helper.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({stdout!r})\n")
    helper.chmod(0o700)
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", str(helper))
    if error_type == "UnicodeDecodeError":
        guardian_never_spawned(monkeypatch)
    else:
        # Use the real Popen path, which rejects a NUL in the resolved token.
        monkeypatch.setattr(service, "_disclaim_guardians", False)
    stuck, later = _claude_probe_jobs(service, harness,
                                     Credential("claude", "claude-quota-fixture", "keychain-token"))
    _assert_early_probe_error(service, stuck, later, error_type)


def test_c6_12_a_claude_home_symlink_loop_ends_only_the_probe(fleet, monkeypatch):
    import sys

    class HomeCheckingClaude(FakeAdapter):
        def build_launch(self, job, attempt_id, attempt_dir, lane, credential_env, *args):
            Path(credential_env["CLAUDE_CONFIG_DIR"]).stat()
            return super().build_launch(job, attempt_id, attempt_dir, lane, credential_env, *args)

    service, harness = fleet
    home = harness.root / "fixture-claude-home"
    home.mkdir()
    stuck, later = _claude_probe_jobs(service, harness, Credential("claude", str(home), "home"), str(home))
    home.rmdir()
    home.symlink_to(home.name)
    monkeypatch.setitem(registry._factories, "claude", HomeCheckingClaude)
    guardian_never_spawned(monkeypatch)
    # Python 3.12's non-strict resolve raises RuntimeError. Starting with 3.13 it
    # leaves the loop unresolved, and the fake adapter's stat raises OSError (ELOOP).
    error_type = "RuntimeError" if sys.version_info < (3, 13) else "OSError"
    _assert_early_probe_error(service, stuck, later, error_type)


def test_c6_12_a_deep_codex_hooks_reply_ends_only_the_probe(fleet, monkeypatch):
    import subprocess
    import sys

    from subfleet.guard import preflight
    from tests.fake.guard import install_guard

    service, harness = fleet
    _, pin = install_guard(service.root)
    version = json.loads(pin.read_text())["codex_version"]
    binary = harness.root / "fixture-codex"
    binary.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      f"if sys.argv[1] == '--version':\n    print({version!r})\n    sys.exit()\n"
                      "assert sys.argv[1] == 'app-server'\n"
                      "for line in sys.stdin:\n"
                      "    if json.loads(line)['method'] == 'hooks/list':\n"
                      "        print('{\"id\":2,\"result\":' + '[' * 10_000 + ']' * 10_000 + '}')\n"
                      "        break\n")
    binary.chmod(0o700)

    def reap_fake(process):
        # This fixture has one self-exiting process and uses its PID for cleanup.
        with process:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                raise

    monkeypatch.setattr(preflight, "_jq_available", lambda: True)
    monkeypatch.setattr(preflight, "_stop_probe", reap_fake)
    monkeypatch.delenv("CODEX_GUARD_PREFLIGHT_TIMEOUT", raising=False)
    monkeypatch.delenv("SUBFLEET_GUARD_TRUST", raising=False)
    monkeypatch.delenv("SUBFLEET_CODEX_GUARD_CACHE", raising=False)
    monkeypatch.setitem(registry._factories, "codex", lambda: CodexAdapter(codex_bin=str(binary)))
    guardian_never_spawned(monkeypatch)
    stuck, later = two_jobs(service, harness)
    try:
        json.loads("[" * 10_000 + "]" * 10_000)
    except RecursionError:
        error_type = "RecursionError"
    else:
        # Python 3.14's JSON decoder accepts this depth. Preflight still refuses
        # the reply because its result is a list, so the adapter raises instead.
        error_type = "AdapterError"
    _assert_early_probe_error(service, stuck, later, error_type)


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


@pytest.mark.parametrize("site", ["execute", "recover"])
@pytest.mark.parametrize("error_type", ["PermissionError", "JSONDecodeError", "UnicodeDecodeError"])
def test_c11_4_an_unreadable_receipt_ends_the_probe_after_containment(
        fleet, monkeypatch, site, error_type):
    service, harness = fleet
    stuck, later = two_jobs(service, harness)
    censuses = []

    def containment(pgid, guardian, child_pid, holder, **kwargs):
        censuses.append(holder)
        assert "its receipt could not be read" not in log_text(service)
        return procs.Containment()

    monkeypatch.setattr(service, "_probe_census", service._contain)
    monkeypatch.setattr(procs, "containment", containment)
    monkeypatch.setattr(procs, "same_process", lambda *args, **kwargs: False)

    def publish(path):
        if error_type == "PermissionError":
            path.write_text('{"rc": 0, "child_pid": 900002}')
            path.chmod(0)
        else:
            path.write_bytes(b'{"synthetic-private-receipt":' if error_type == "JSONDecodeError" else b"\xff")

    probe_ran(service, monkeypatch, publish_receipt=publish if site == "execute" else None)
    if site == "recover":
        record = recovered_probe(service, stuck, monkeypatch)
        publish(Path(record["directory"]) / "exit.json")
    admission_pass(service)
    assert_pass_went_on(service, later)
    outcome = (service._probe_record(record["holder"])["outcome"] if site == "recover"
               else probe_outcome(service, stuck))
    assert censuses
    assert outcome["cls"] == "unknown"
    assert outcome["detail"] == f"probe receipt could not be read: {error_type}"
    assert outcome["evidence"]["error_type"] == error_type
    assert log_text(service).count(f"its receipt could not be read: {error_type}") == 1
    assert "synthetic-private-receipt" not in log_text(service)
    assert_probe_settled(service, stuck)


UNREADABLE_STARTS = [
    pytest.param(None, id="mode-000"),
    pytest.param(b"{", id="torn"),
    pytest.param(b"[1]", id="nonempty-list"),
    pytest.param(b'"x"', id="string"),
    pytest.param(b"\xff", id="not-utf-8"),
]


def write_probe_start(path, content, request):
    if content is not None:
        path.write_bytes(content)
        return
    path.write_bytes(b'{"guardian_pid": 1, "pgid": 1}')
    mode = path.stat().st_mode

    def restore_mode():
        # A completed probe removes its own directory.
        if path.exists():
            path.chmod(mode)

    request.addfinalizer(restore_mode)
    path.chmod(0)


@pytest.mark.parametrize("site", ["execute", "recover"])
@pytest.mark.parametrize("content", [*UNREADABLE_STARTS, pytest.param(b"[]", id="empty-list")])
def test_c11_4_an_unreadable_probe_start_json_counts_as_absent(fleet, monkeypatch, request, site, content):
    service, harness = fleet
    stuck, later = two_jobs(service, harness)
    censuses = []

    def containment(pgid, guardian, child_pid, holder, **kwargs):
        censuses.append((holder, guardian, pgid))
        return procs.Containment()

    monkeypatch.setattr(service, "_probe_census", service._contain)
    monkeypatch.setattr(service, "_disclaim_guardians", False)
    monkeypatch.setattr(procs, "containment", containment)
    monkeypatch.setattr(procs, "same_process", lambda *args, **kwargs: False)

    def publish(receipt):
        receipt.write_text('{"rc": 3, "child_pid": 900002}')
        write_probe_start(receipt.with_name("start.json"), content, request)

    probe_ran(service, monkeypatch, rc=3, publish_receipt=publish if site == "execute" else None)
    if site == "recover":
        record = recovered_probe(service, stuck, monkeypatch, rc=3)
        write_probe_start(Path(record["directory"]) / "start.json", content, request)

    admission_pass(service)
    assert_pass_went_on(service, later)
    assert_probe_settled(service, stuck)
    assert service.store.query("SELECT 1 FROM events WHERE kind='probe.completed' AND job_id=?", (stuck,))
    assert censuses
    holder, guardian, pgid = censuses[0]
    if site == "recover":
        assert holder == record["holder"]
    completed = service._probe_record(holder)
    assert completed["state"] == "completed"
    assert completed["outcome"]["cls"] == OutcomeClass.CONTENT_FILTER
    assert guardian == completed["guardian_pid"] == 900001
    assert pgid == completed["pgid"] == 900001


@pytest.mark.parametrize("content", UNREADABLE_STARTS)
def test_c11_4_startup_recovery_survives_an_unreadable_probe_start_json(fleet, monkeypatch, request, content):
    service, harness = fleet
    job_id = submit(service, harness, tier="hard")
    record = recovered_probe(service, job_id, monkeypatch, rc=0)
    write_probe_start(Path(record["directory"]) / "start.json", content, request)
    service.close()

    monkeypatch.setattr(procs, "containment", lambda *args, **kwargs: procs.Containment())
    restarted = daemon_module.Daemon(service.root)
    try:
        restarted.workers = Inline(restarted.workers)
        assert not restarted._recovery_complete.is_set()
        assert not restarted.timers.started
        assert restarted.store.list_leases(record["holder"])
        restarted._recover_then_start_timers()
        assert restarted._recovery_complete.is_set()
        assert restarted.timers.started
        assert not restarted.store.list_leases(record["holder"])
        assert restarted._probe_record(record["holder"])["outcome"]["cls"] == OutcomeClass.OK
    finally:
        restarted.close()


@pytest.mark.parametrize(("value", "readable"), [
    pytest.param({"guardian_pid": "x"}, False, id="guardian-pid-string"),
    pytest.param({"guardian_pid": True}, False, id="guardian-pid-bool"),
    pytest.param({"guardian_pid": 1.5}, False, id="guardian-pid-float"),
    pytest.param({"pgid": []}, False, id="pgid-list"),
    pytest.param({"boot_id": 3}, False, id="boot-id-int"),
    pytest.param({"proc_start": True}, False, id="proc-start-bool"),
    pytest.param({"started_at": 5}, False, id="started-at-int"),
    pytest.param({"guardian_pid": 12, "pgid": 13, "boot_id": "boot", "proc_start": "start",
                  "started_at": "2026-11-28T00:00:00Z"}, True, id="well-formed"),
    pytest.param({"guardian_pid": None, "pgid": None, "boot_id": None, "proc_start": None,
                  "started_at": None}, True, id="null-fields"),
    pytest.param({}, True, id="absent-fields"),
])
def test_c11_4_a_probe_start_json_field_of_the_wrong_type_is_unreadable(fleet, tmp_path, value, readable):
    service, _ = fleet
    path = tmp_path / "start.json"
    path.write_text(json.dumps(value))
    expected = (value, None) if readable else (None, "ValueError")
    assert service._read_probe_json(path) == expected


@pytest.mark.parametrize("site", ["execute", "recover", "timer"])
def test_c11_4_an_unreadable_request_is_treated_as_absent(fleet, monkeypatch, site):
    service, harness = fleet
    probe_ran(service, monkeypatch, rc=0)
    if site != "recover":
        awaited = service._await_probe

        def request_then_receipt(record, child=None):
            Path(record["directory"], "request.json").write_bytes(b"\xff")
            return awaited(record, child)

        monkeypatch.setattr(service, "_await_probe", request_then_receipt)
    if site == "timer":
        lane = service.store.get_lane("codex-1")
        holder = "probe:timer"
        outcome = service._timer_turn(lane, "keepalive", holder, cancel=service.timers.cancel,
                                       deadline=time.monotonic() + 60)
        assert outcome.cls == OutcomeClass.OK
        assert outcome.evidence["requested_at"] is None
    else:
        stuck, later = two_jobs(service, harness)
        if site == "recover":
            record = recovered_probe(service, stuck, monkeypatch, rc=0)
            record.update(job_id=None, timer_kind="keepalive")
            service._save_probe(record)
            Path(record["directory"], "request.json").write_bytes(b"\xff")
        admission_pass(service)
        assert_pass_went_on(service, later)
        if site == "recover":
            assert service._probe_record(record["holder"])["state"] == "completed"
        else:
            assert probe_outcome(service, stuck)["cls"] == OutcomeClass.OK
    assert not service.store.query("SELECT 1 FROM events WHERE kind='timer.request'")
    assert not service.store.query("SELECT 1 FROM readings WHERE source='keepalive'")
    assert not any(row["holder"].startswith("probe:") for row in service.store.list_leases())
    assert log_text(service).count("its request could not be read: UnicodeDecodeError") == 1


def test_c11_4_an_unwritable_probe_directory_leaves_no_reservation_and_warns_once(fleet, monkeypatch):
    service, harness = fleet
    stuck, later = two_jobs(service, harness)
    guardian_never_spawned(monkeypatch)
    probes = service.root / "lanes" / "codex-1" / "probes"
    probes.mkdir(parents=True)
    probes.chmod(0o500)
    try:
        admission_pass(service)
        assert_pass_went_on(service, later)
        assert_probe_settled(service, stuck)
        assert not list(probes.iterdir())
        assert not service.store.query("SELECT 1 FROM events WHERE job_id=? AND kind LIKE 'probe.%'", (stuck,))
        warning = "probe directory on codex-1 could not be created: PermissionError"
        assert log_text(service).count(warning) == 1
        with service.store.transaction("fixture") as tx:
            tx.execute("UPDATE jobs SET next_check_at=NULL WHERE job_id=?", (stuck,))
        admission_pass(service)
        assert_pass_went_on(service, later)
        assert_probe_settled(service, stuck)
        assert log_text(service).count(warning) == 1
    finally:
        probes.chmod(0o700)


@pytest.mark.parametrize("site", ["execute", "recover"])
def test_c6_12_a_store_error_from_probe_classify_reaches_the_caller(fleet, monkeypatch, site):
    service, harness = fleet
    stuck = submit(service, harness, tier="hard")
    record = recovered_probe(service, stuck, monkeypatch)
    holder = record["holder"]
    error = sqlite3.OperationalError("synthetic store failure")

    def classify(*args):
        raise error

    monkeypatch.setattr(FakeAdapter, "classify", classify)
    probe_ran(service, monkeypatch)
    with pytest.raises(sqlite3.Error) as raised:
        if site == "recover":
            service._recover_probes()
        else:
            lane = service.store.get_lane(record["lane_id"])
            service._execute_probe(service.store.get_job(stuck), lane, {"id": record["model_id"]}, holder)
    assert raised.value is error
    assert service.store.list_leases(holder)
    assert service._probe_record(holder)["state"] == "contained"


def test_c11_4_an_incompatible_recovered_launch_ends_the_probe_unknown(fleet, monkeypatch):
    service, harness = fleet
    stuck, later = two_jobs(service, harness)
    probe_ran(service, monkeypatch)
    record = recovered_probe(service, stuck, monkeypatch)
    launch_path = Path(record["directory"], "launch.json")
    launch = json.loads(launch_path.read_text())
    launch["synthetic_unknown_field"] = True
    launch_path.write_text(json.dumps(launch))
    admission_pass(service)
    assert_pass_went_on(service, later)
    outcome = service._probe_record(record["holder"])["outcome"]
    assert outcome["cls"] == "unknown"
    assert outcome["detail"] == "probe receipt could not be classified: TypeError"
    assert outcome["evidence"]["error_type"] == "TypeError"
    assert not service.store.list_leases(record["holder"])


@pytest.mark.parametrize("blocked", ["mkdir-fails", "slot-held"])
def test_c11_4_an_unwritable_probe_directory_sends_the_probe_to_another_lane_of_the_model(
        fleet, monkeypatch, blocked):
    service, harness = fleet
    home = service.root / "home-codex-3"
    home.mkdir()
    service.store.put_lane(Lane("codex-3", "codex", "codex:codex-3",
                                Credential("codex", str(home), "home"), str(home),
                                LaneOwner.V2, False))
    job_id = submit(service, harness, tier="hard")
    probe_ran(service, monkeypatch)
    route = service._route
    timer_holder = None
    probes = service.root / "lanes" / "codex-1" / "probes"
    probes.mkdir(parents=True)

    def timer_wins(job, **kwargs):
        nonlocal timer_holder
        decision = route(job, **kwargs)
        if timer_holder is None and job["job_id"] == job_id and decision.chosen_lane == "codex-1":
            # The slot becomes held after evaluation, before reservation.
            timer_holder = service.timers._reserve(service.store.get_lane("codex-1"), "read")
            assert timer_holder is not None
        return decision

    if blocked == "mkdir-fails":
        probes.chmod(0o500)
    else:
        monkeypatch.setattr(service, "_route", timer_wins)
    probe_lanes = []
    seen = 0
    try:
        for _ in range(3):
            service.store.update_job(job_id, next_check_at=None)
            service._wake_admission()
            admission_pass(service)
            completed = [row["lane_id"] for row in service.store.list_events(job_id)
                         if row["kind"] == "probe.completed"]
            probe_lanes.append(completed[seen:])
            seen = len(completed)
            if timer_holder is not None:
                service.timers._release(timer_holder)
                timer_holder = None
    finally:
        probes.chmod(0o700)
        if timer_holder is not None:
            service.timers._release(timer_holder)
    assert probe_lanes[0] == ["codex-3"], probe_lanes
    assert "worker admission failed" not in log_text(service)
    if blocked == "mkdir-fails":
        assert log_text(service).count("probe directory on codex-1 could not be created: PermissionError") == 1


def test_c11_4_the_probe_directory_warning_returns_after_a_success(fleet, monkeypatch):
    service, harness = fleet
    job_id = submit(service, harness, tier="hard")
    probe_ran(service, monkeypatch)
    probes = service.root / "lanes" / "codex-1" / "probes"
    probes.mkdir(parents=True)
    warning = "probe directory on codex-1 could not be created: PermissionError"
    try:
        probes.chmod(0o500)
        admission_pass(service)
        assert log_text(service).count(warning) == 1
        assert not any(row["kind"] == "probe.completed" for row in service.store.list_events(job_id))

        probes.chmod(0o700)
        service.store.update_job(job_id, next_check_at=None)
        service._wake_admission()
        admission_pass(service)
        assert log_text(service).count(warning) == 1
        assert [row["lane_id"] for row in service.store.list_events(job_id)
                if row["kind"] == "probe.completed"] == ["codex-1"]

        probes.chmod(0o500)
        service.store.update_job(job_id, next_check_at=None)
        service._wake_admission()
        admission_pass(service)
        assert log_text(service).count(warning) == 2
        assert_probe_settled(service, job_id)
    finally:
        probes.chmod(0o700)
