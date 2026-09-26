"""The sessions kit against a real daemon and store (C-20.1): milestone 6.

An in-process `Daemon` on a fake adapter, so the `sessions` op, the notice
rows, the revive lease and the resume launch are the real ones. No provider is
called and no session inbox is opened; the only processes started are the local
fixture ones the daemon's own tests already start.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry as adapter_registry
from subfleet.contracts import Credential, Lane, LaneOwner, Sandbox
from subfleet.daemon import Daemon, revive_lease_key
from subfleet.sessions import handoff as handoff_module
from subfleet.sessions import nudge as nudge_module
from subfleet.sessions import revive as revive_module
from tests import sessions_fixtures as fx
from tests.fake_adapter import FakeAdapter

ALICE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
BOB = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
LANE_RUN = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
ACCOUNT, ORG = "acct-aaaa", "org-aaaa"


def until(predicate, timeout=10, describe=None):
    """Poll `predicate` until it is truthy; `describe()` says where things stood if not."""
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError(f"condition timed out after {timeout}s"
                         + (f": {describe()}" if describe else ""))


def stage_s(service: Daemon) -> float:
    """A wait's budget for one stage of a launched job: admission, whose probe
    starts a guardian and the fake provider, or the attempt, which starts both
    again and runs to its release.

    A budget chosen from measurement, not a daemon deadline. It is scaled to
    `start_grace_s`, the time the daemon lets an attempt's guardian take to
    start before it presumes the start failed (C-4.2), and at the default of
    10 s is twice the slowest stage seen: at a load average of 211 on 18 cores,
    admission took 9.3 s and reservation to release 10 s.
    """
    return 2 * service.start_grace_s


class Client:
    """`sessions.client.Sessions` over an in-process daemon rather than a socket.

    The op names, the argument shapes and the results are the wire ones; only
    the transport is short-circuited, which is the same seam
    `tests/fake/test_timers_end_to_end.py` uses.
    """

    def __init__(self, service: Daemon):
        self.service = service

    def state(self, session_ids=None):
        return self.service.dispatch("sessions", {
            "action": "state", "session_ids": list(session_ids or [])})

    def record_nudge(self, session_id, *, dedupe_key, cooldown_s, kind="nudge",
                     force=False, detail=None):
        return self.service.dispatch("sessions", {
            "action": "nudged", "session_id": session_id, "dedupe_key": dedupe_key,
            "cooldown_s": cooldown_s, "kind": kind, "force": force,
            "detail": dict(detail or {})})

    def record_revive(self, session_id, *, dedupe_key, detail=None):
        return self.service.dispatch("sessions", {
            "action": "revived", "session_id": session_id, "dedupe_key": dedupe_key,
            "detail": dict(detail or {})})

    def retire(self, session_id, reason=None):
        return self.service.dispatch("sessions", {
            "action": "retire", "session_id": session_id, "reason": reason})

    def unretire(self, session_id):
        return self.service.dispatch("sessions", {
            "action": "unretire", "session_id": session_id})

    def ping(self, session_id, text):
        return self.service.dispatch("ping", {"session_id": session_id, "text": text})

    def submit(self, args):
        import dataclasses
        return self.service.dispatch("submit", dataclasses.asdict(args))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A daemon, a lane, a `~/.claude`, and a desktop session store."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(adapter_registry, "_factories",
                        {"codex": FakeAdapter, "claude": FakeAdapter})
    home = fx.claude_home(tmp_path, monkeypatch)
    store_dir = fx.desktop_store(tmp_path, monkeypatch)
    # AF_UNIX caps the socket path near 104 bytes and pytest's tmp_path is longer.
    with tempfile.TemporaryDirectory(prefix="sf-sessions-", dir="/tmp") as directory:
        root = Path(directory)
        policy = fx.policy(mirror_interval_s=0)         # the timer is its own test
        (root / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
        service = Daemon(root, tick_s=.02, term_grace_s=.05)
        service.store.put_lane(Lane("codex-1", "codex", "codex:fake",
                                    Credential("codex", str(root / "home"), "home"),
                                    str(root / "home"), LaneOwner.V2, False))
        (root / "home").mkdir(exist_ok=True)
        try:
            # `tmp_path` for the job workdir and `/tmp` for the state root: a
            # unix socket path is capped near 104 bytes, and C-2.4 refuses a
            # workdir under /tmp. `tests/unit/conftest.py` splits them the same
            # way for the same two reasons.
            yield service, Client(service), home, store_dir, root, policy, tmp_path
        finally:
            close_world(service)


def close_world(service: Daemon) -> None:
    """Stop daemon workers, then reap this fixture's detached guardians.

    Production shutdown deliberately leaves guardians running for recovery.
    TemporaryDirectory must wait for their final receipt writes even when the
    test fails before its job completes. Popen handles identify only children
    this fixture launched; no machine-wide process search is involved.
    """
    service.close()
    for child in tuple(service._children.values()):
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:                # failed/blocked fixture
            # Guardians start their own session before spawning providers.
            # An unreaped direct child cannot have had its PID reused.
            try:
                if os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
                else:                                   # before guardian setsid
                    child.kill()
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


def run(service: Daemon) -> Daemon:
    """Start the real control loop — admission, probes, launch, finalization.

    Started exactly as `serve_forever` starts it, minus the socket: the client
    here calls `dispatch` in process, so there is nothing to accept. Tests that
    only need submit-time or admission-time behaviour drive `_admit()` by hand
    instead, so the attempt they are asserting about cannot finish underneath
    them.
    """
    service._control_thread = threading.Thread(             # noqa: SLF001 - the seam
        target=service._control, name="subfleet-control-test", daemon=True)
    service._control_thread.start()
    return service


def workdir(base: Path, branch: str = "work") -> Path:
    """A committed repository on a task branch: what a writable job requires."""
    import subprocess
    path = base / f"repo-{branch}"
    path.mkdir(exist_ok=True)
    for argv in (["init", "-q", "-b", branch], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)
    (path / "PROGRESS.md").write_text("state: mid-flight\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "first"], check=True,
                   capture_output=True)
    return path


@pytest.fixture
def live_pids():
    """Real running processes, because the registry's liveness check is `kill(0)`.

    A session's registry file is keyed by pid, so distinct live sessions need
    distinct live pids; this process alone cannot supply three.
    """
    import subprocess
    children = [subprocess.Popen(["/bin/sleep", "30"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(3)]
    try:
        yield [child.pid for child in children]
    finally:
        for child in children:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:            # pragma: no cover - cleanup
                child.kill()
                child.wait(timeout=5)


def stage(root: Path):
    def write(text: str) -> Path:
        path = root / f"prompt-{uuid.uuid4().hex}.md"
        path.write_text(text, encoding="utf-8")
        return path
    return write


def test_world_shutdown_reaps_a_provider_still_writing_receipts(world, monkeypatch):
    """Fixture cleanup waits for detached writers before deleting their root."""
    service, _client, _home, _store, root, _policy, base = world
    release = root / "release-provider"

    class HeldProvider(FakeAdapter):
        def build_launch(self, *args, **kwargs):
            launch = super().build_launch(*args, **kwargs)
            program = ("from pathlib import Path\nimport sys, time\n"
                       "print('fake provider ready', flush=True)\n"
                       "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
                       "print('fake deliverable', flush=True)\n")
            return replace(launch, argv=(sys.executable, "-c", program, str(release)))

    monkeypatch.setitem(adapter_registry._factories, "codex", HeldProvider)
    run(service)
    prompt = stage(root)("Exercise fixture shutdown.")
    result = service.dispatch("submit", {
        "request_id": str(uuid.uuid4()), "kind": "dispatch", "workdir": str(base),
        "prompt_path": str(prompt), "sandbox": "read-only", "pinned_model": "astra",
        "allow_tmp": True,
    })
    adir = root / "jobs" / result["job_id"] / "a1"
    until(lambda: (adir / "stdout").is_file() and "ready" in (adir / "stdout").read_text())
    child = service._children[result["job_id"] + "/a1"]
    assert child.poll() is None
    wait = child.wait

    def release_and_wait(*args, **kwargs):
        # Hold the writer until cleanup actually waits, regardless of host load.
        release.touch()
        return wait(*args, **kwargs)

    monkeypatch.setattr(child, "wait", release_and_wait)
    close_world(service)
    assert child.poll() == 0
    assert json.loads((adir / "exit.json").read_text())["rc"] == 0


# --- `sessions continue --scope interrupted` (C-23.33, C-23.31) ---------------

def test_a_sweep_writes_one_notice_per_interrupted_session(world, live_pids):
    """C-23.33, C-15.1: one nudge per interrupted session, as a durable notice.

    The duplicate pair is one session id and gets one notice, with both pids
    named (C-23.30); the idle session gets none because it is not interrupted;
    the headless lane run gets none at all (C-23.31).
    """
    service, client, home, _store, _root, policy, base = world
    first, second, third = live_pids
    fx.register(home, ALICE, first, started_at=2.0, name="alice")
    fx.register(home, ALICE, second, started_at=3.0, name="alice restarted")
    fx.transcript(home, ALICE, fx.interrupted(age_s=1800))
    fx.register(home, BOB, third, started_at=1.0, name="bob")
    fx.transcript(home, BOB, fx.completed(age_s=1800))
    fx.register(home, LANE_RUN, os.getpid(), started_at=4.0, name="a lane run")
    fx.transcript(home, LANE_RUN, fx.headless(age_s=1800))

    report = nudge_module.sweep(client, policy, scope="interrupted", manual=False,
                                now=lambda: fx.NOW, sleep=lambda _s: None, delay_s=0)

    notices = service.store.query("SELECT * FROM service_notices ORDER BY notice_id")
    assert [row["session_id"] for row in notices] == [ALICE]
    assert nudge_module.MARKER in notices[0]["text"]
    assert notices[0]["state"] == "pending"
    # C-23.31: the lane run is not in the listing at all, so it has no outcome
    # row and no notice — a sweep that named it explicitly would get the reason.
    considered = {item.session_id for item in report.outcomes}
    assert considered == {ALICE, BOB}
    assert next(item.reason for item in report.outcomes
                if item.session_id == BOB).startswith("completed:")
    assert len(report.duplicates) == 1
    assert str(first) in report.duplicates[0] and str(second) in report.duplicates[0]


def test_the_nudge_record_is_a_durable_event_and_blocks_the_next_sweep(world):
    """C-23.33: the cooldown and the dedupe are recorded in `events`, and the
    reservation that reads them is the transaction that writes the next one."""
    service, client, home, _store, _root, policy, base = world
    fx.register(home, ALICE, os.getpid(), started_at=1.0)
    fx.transcript(home, ALICE, fx.interrupted(age_s=1800))

    first = nudge_module.sweep(client, policy, scope="interrupted", manual=False,
                              now=lambda: fx.NOW, sleep=lambda _s: None, delay_s=0)
    assert first.outcomes[0].delivered is True

    events = service.store.query(
        "SELECT * FROM events WHERE kind=? ORDER BY event_id", (daemon_module.NUDGE_EVENT,))
    assert len(events) == 1
    data = json.loads(events[0]["data_json"])
    assert data["session_id"] == ALICE and data["dedupe_key"] == "cut"

    again = nudge_module.sweep(client, policy, scope="interrupted", manual=False,
                              now=lambda: fx.NOW, sleep=lambda _s: None, delay_s=0)
    assert again.outcomes[0].delivered is False
    assert "already nudged at this interruption point" in again.outcomes[0].reason
    assert len(service.store.query("SELECT * FROM service_notices")) == 1


def test_the_reservation_refuses_a_second_recorder_for_one_interruption(world):
    """C-23.33: the dedupe is enforced inside the transaction that records it,
    so two sweeps racing over one session cannot both reserve."""
    _service, client, _home, _store, _root, _policy, _base = world
    first = client.record_nudge(ALICE, dedupe_key="cut", cooldown_s=90)
    second = client.record_nudge(ALICE, dedupe_key="cut", cooldown_s=90)
    assert first["recorded"] is True
    assert second["recorded"] is False
    assert "already nudged" in second["reason"]


def test_retirement_is_durable_and_the_state_op_reports_it(world):
    """C-23.35: retirement is a durable session flag the operator sets and clears."""
    _service, client, _home, _store, _root, _policy, _base = world
    client.retire(ALICE, "replaced orchestrator")
    state = client.state([ALICE])["sessions"][ALICE]
    assert state["retired"]["reason"] == "replaced orchestrator"
    client.unretire(ALICE)
    assert client.state([ALICE])["sessions"][ALICE]["retired"] is None



def test_retirement_uses_event_order_within_one_second(world, monkeypatch):
    """C-23.35: the last operator action wins even when timestamps tie."""
    _service, client, _home, _store, _root, _policy, _base = world
    monkeypatch.setattr(daemon_module, "utcnow", lambda: fx.iso(fx.NOW))
    client.retire(ALICE, "first retirement")
    client.unretire(ALICE)
    client.retire(ALICE, "retired again")
    state = client.state([ALICE])["sessions"][ALICE]
    assert state["retired"]["reason"] == "retired again"
    client.unretire(ALICE)
    assert client.state([ALICE])["sessions"][ALICE]["retired"] is None

@pytest.mark.parametrize("kind", ["dispatch", "revive"])
def test_the_state_op_reports_the_ledgers_own_lane_sessions(world, kind):
    """C-23.31: a resumed session is not one the daemon created as a lane."""
    service, client, _home, _store, _root, _policy, _base = world
    service.store.add_job({"job_id": "job-x", "request_id": "r-x",
                           "payload_digest": "d", "kind": kind,
                           "workdir": "/tmp", "prompt_path": "/tmp/p.md",
                           "sandbox": "read-only"})
    service.store.add_attempt({"attempt_id": "job-x/a1", "job_id": "job-x", "seq": 1,
                               "lane_id": "codex-1", "model_requested": "m",
                               "native_session_id": LANE_RUN})
    assert (LANE_RUN in client.state()["lane_sessions"]) is (kind == "dispatch")


# --- `sessions revive` (C-23.20, C-23.55, C-6.5) ------------------------------

def cold_desktop_session(home, store_dir, repo: Path, session_id: str = ALICE):
    fx.transcript(home, session_id,
                  fx.with_mode(fx.interrupted(age_s=1800), "bypassPermissions"),
                  cwd=str(repo))
    fx.index_entry(store_dir, ACCOUNT, ORG, session_id, cwd=str(repo),
                   model="claude-fable-5-1")


def test_revive_is_refused_for_a_desktop_owned_session_unless_opted_in(world):
    """Plan decision 7: automatic headless revival of a session the desktop app
    owns is off by default, and the fix names `--revive` and `handoff`."""
    service, client, home, store_dir, root, policy, base = world
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)

    held = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                now=fx.NOW)
    assert held.admitted is False
    assert "--revive" in held.fix and "handoff" in held.fix
    assert service.store.query("SELECT * FROM jobs WHERE kind='revive'") == []



def test_a_revive_records_the_requested_model_substitution(world):
    """C-23.39: the daemon durably records an explicit model substitution."""
    service, client, home, store_dir, root, policy, base = world
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                 opt_in=True, model="astra", now=fx.NOW)
    record = client.state([ALICE])["sessions"][ALICE]["last_revive"]
    assert record["job_id"] == result.job_id
    assert record["recorded_model"] == "claude-fable-5-1"
    assert record["model"] == "astra"
    assert record["substituted"] is True
    events = service.store.query("SELECT data_json FROM events WHERE kind=?",
                                 (daemon_module.REVIVE_EVENT,))
    assert len(events) == 1
    assert json.loads(events[0]["data_json"])["job_id"] == result.job_id

def test_revive_probes_lane_before_launch(world):
    """C-23.20: revive admits a lane only on a `provider` reading taken in the
    same pass — a stored reading never qualifies it on its own.

    Ledger row 189; 2026-08-25, three lanes the ledger called healthy were out
    of Fable. In v2 the daemon owns the probe: `scheduler.probe_required` is
    unconditional for a revive, so the decision the job records is measured.
    """
    from subfleet import scheduler
    from subfleet.contracts import Decision
    decision = Decision(("astra",), ({"model": "astra", "candidates": ["codex-1"],
                                      "candidate_details": {"codex-1": {"measured": True}}},),
                        "codex-1", "astra", "", "hash")
    assert scheduler.probe_required(decision, {"kind": "revive", "sandbox": "read-only"})
    assert not scheduler.probe_required(decision, {"kind": "dispatch",
                                                   "sandbox": "read-only"})

    service, client, home, store_dir, root, policy, base = world
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                  opt_in=True, model="astra", now=fx.NOW)
    assert result.admitted is True
    assert service.store.query("SELECT * FROM events WHERE kind='probe.state'") == [], \
        "nothing is measured until admission runs"

    service._admit()                                    # noqa: SLF001 - one pass

    # The probe ran in the pass that admitted the attempt, against the lane the
    # attempt then reserved — not against a reading the ledger had lying around.
    probes = service.store.query(
        "SELECT * FROM events WHERE kind='probe.state' ORDER BY event_id")
    assert probes, "a revive is not admitted without a probe in the same pass"
    assert {row["lane_id"] for row in probes} == {"codex-1"}
    assert {row["job_id"] for row in probes} == {result.job_id}

    attempt = service.store.list_attempts(result.job_id)[0]
    assert attempt["lane_id"] == "codex-1", "the lane it measured is the lane it took"
    reserved = service.store.one(
        "SELECT MIN(event_id) AS first FROM events WHERE kind='attempt.reserved'")
    assert probes[0]["event_id"] < reserved["first"], "measured, then admitted"
    assert not service.store.query(
        "SELECT * FROM leases WHERE holder LIKE 'probe:%'"), \
        "the probe released its lane reservation before the attempt took it"


def test_revive_census_refreshed_and_skips_running_twin(world):
    """C-23.55: a session has at most one live revive. The lease
    `session:<id>:revive` is taken in the transaction that admits the attempt,
    and a session that already holds it is skipped rather than launched again.

    Ledger row 192: on 2026-09-04 a headless revive twin ran alongside a live
    session and re-dispatched its lanes. The census is the lease rows read
    inside the admitting transaction, not a snapshot from the start of the pass.
    """
    service, client, home, store_dir, root, policy, base = world
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    first = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                 opt_in=True, model="astra", now=fx.NOW)
    assert first.admitted is True
    assert service.store.one("SELECT * FROM leases WHERE lease_key=?",
                             (revive_lease_key(ALICE),)) is None, \
        "the lease is taken at admission, not at submit (C-23.55)"

    service._admit()                                    # noqa: SLF001 - one pass
    lease = service.store.one("SELECT * FROM leases WHERE lease_key=?",
                              (revive_lease_key(ALICE),))
    assert lease is not None and lease["holder"] == first.job_id

    # A second revive of the same session, while the first is live: refused at
    # submit with the holder named, so nothing is queued to launch later.
    with pytest.raises(daemon_module.AdapterError) as raised:
        revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                             opt_in=True, model="astra", now=fx.NOW)
    assert "already has a live revive" in str(raised.value)
    assert raised.value.code == 7
    assert first.job_id in (raised.value.fix or "")
    assert len(service.store.query("SELECT * FROM jobs WHERE kind='revive'")) == 1


def test_a_revive_that_loses_the_lease_race_is_skipped_not_queued(world):
    """C-23.55: "a session that already holds it is skipped rather than launched
    again" — and skipped means terminal, not patient.

    Submit refuses the ordinary second revive; this is the race it cannot see,
    where the lease appears between the submission and the admission. Waiting
    for the other revive to end would launch the twin the moment it did.
    """
    service, client, home, store_dir, root, policy, base = world
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                  opt_in=True, model="astra", now=fx.NOW)
    assert result.admitted is True

    # The race: another revive of the same session takes the lease first.
    assert service.store.acquire_lease(revive_lease_key(ALICE), "another-revive")
    service._admit()                                    # noqa: SLF001 - one pass

    job = service.store.get_job(result.job_id)
    assert (job["state"], job["rc"]) == ("failed", 7)
    assert job["finished_at"], "terminal, so admission never looks at it again"
    assert service.store.list_attempts(result.job_id) == [], "nothing was launched"
    assert service.store.one("SELECT holder FROM leases WHERE lease_key=?",
                             (revive_lease_key(ALICE),))["holder"] == "another-revive"
    notice = service.store.query("SELECT * FROM notices WHERE job_id=?",
                                 (result.job_id,))[0]
    assert "already has a live revive" in notice["text"]
    assert "another-revive" in notice["text"]
    assert notice["session_id"] == ALICE


def test_the_lease_is_session_scoped_and_released_with_the_job(world):
    """C-23.55 and C-6.3: the key names the session, the holder is the job, and
    every existing holder-keyed release site frees it.

    The test waits for the release, not for the terminal state. A revive that
    succeeds becomes terminal in `_finalize`'s transaction, and `_export` frees
    its job-held leases in the next one; `finished` in `tests/fake/conftest.py`
    waits out the same gap for `out:`. A check made when the state was first
    seen landed between the two in a loaded full-suite run on 2026-09-24, and in
    9 of 60 runs at a load average near 80, none of which ran out of time. Every
    release site a revive can reach (`max_attempts` is 1) commits the terminal
    state before or with the delete, so a lease gone while the job is not
    terminal was released early: each poll reads the two in one statement, and
    one that finds that fails.
    """
    service, client, home, store_dir, root, policy, base = world
    run(service)
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                  opt_in=True, model="astra", now=fx.NOW)
    assert result.admitted, result.reason

    def seen():
        return service.store.one(
            "SELECT state, rc, (SELECT holder FROM leases WHERE lease_key=?) AS holder "
            "FROM jobs WHERE job_id=?", (revive_lease_key(ALICE), result.job_id))

    def where():
        attempts = [a["state"] for a in service.store.list_attempts(result.job_id)]
        return f"{seen()}, attempts {attempts}"

    def released():
        now = seen()
        ended = now["state"] in ("succeeded", "failed", "cancelled", "lost")
        assert now["holder"] is not None or ended, f"released before the job ended: {now}"
        return now["holder"] is None

    assert until(lambda: seen()["holder"], timeout=stage_s(service),
                 describe=where) == result.job_id
    until(released, timeout=stage_s(service), describe=where)


def test_a_revive_resumes_the_named_session_rather_than_starting_a_new_one(world):
    """C-23.54: a revive is an ordinary submission whose launch is
    `--resume <session id>`. Starting a fresh conversation would look like a
    revive and not be one."""
    service, client, home, store_dir, root, policy, base = world
    run(service)
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                  opt_in=True, model="astra", now=fx.NOW)
    attempt = until(lambda: (service.store.list_attempts(result.job_id) or [None])[0],
                    timeout=stage_s(service))
    until(lambda: service.store.get_attempt(attempt["attempt_id"])["native_session_id"])
    assert service.store.get_attempt(
        attempt["attempt_id"])["native_session_id"] == ALICE

    job = service.store.get_job(result.job_id)
    assert job["kind"] == "revive"
    assert job["caller_session"] == ALICE
    assert job["sandbox"] == Sandbox.WORKSPACE_WRITE.value
    assert job["in_place"] == 1
    assert job["worktree"] == str(repo), "in place: the session's own worktree"
    until(lambda: service.store.get_job(result.job_id)["state"] in
          ("succeeded", "failed", "cancelled", "lost"), timeout=stage_s(service))


def test_a_revive_of_a_session_on_main_is_refused_like_any_writable_job(world):
    """C-6.5: a revive is a writable job, so the main/master refusal applies.

    Reviving into `main` is exactly what the branch rule exists to stop, and a
    revive is not an exception to it.
    """
    _service, client, home, store_dir, root, policy, base = world
    repo = workdir(base, branch="main")
    cold_desktop_session(home, store_dir, repo)
    with pytest.raises(daemon_module.AdapterError) as raised:
        revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                             opt_in=True, model="astra", now=fx.NOW)
    assert "refused on main" in str(raised.value)
    assert client.state([ALICE])["sessions"][ALICE]["last_revive"] is None


# --- `subfleet handoff` (C-23.14, C-23.36, C-23.54) ---------------------------

def test_handoff_dispatches_detached_through_the_normal_submit_path(world):
    """C-23.54: a handoff is dispatched through the ordinary submit path so it
    inherits routing, the guard, salvage, the ledger and notices.

    Ledger row 211. The caller's session is recorded so the completion notice
    comes back to the session that asked for the handoff.
    """
    service, client, home, _store, root, policy, base = world
    run(service)
    repo = workdir(base)
    fx.transcript(home, ALICE, [
        fx.typed_prompt("port the importer", uuid="p0", at=fx.ago(3600)),
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "agent-secret get token"}),
        fx.user_tool_result(fx.FAKE_SECRET, uuid="r1", at=fx.ago(500)),
        fx.assistant_text("stopped here", uuid="last", at=fx.ago(60))], cwd=str(repo))

    result = handoff_module.handoff(
        client, policy, session_id=ALICE, last=False, model="astra",
        stage_prompt=stage(root), workdir=repo, caller_session="operator-1",
        caller_pid=os.getpid())

    job = service.store.get_job(result.job_id)
    assert job["kind"] == "handoff"
    assert job["caller_session"] == "operator-1"
    assert job["pinned_model"] == "astra"
    assert job["sandbox"] == Sandbox.READ_ONLY.value, "no --task, so read-only"

    prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")
    assert fx.FAKE_SECRET not in prompt
    assert handoff_module.OMITTED_SENSITIVE in prompt
    assert "port the importer" in prompt
    assert "state: mid-flight" in prompt, "PROGRESS.md is a brief section"

    finished = until(lambda: service.store.get_job(result.job_id)["state"]
                     in ("succeeded", "failed", "cancelled", "lost"), timeout=stage_s(service))
    notices = service.store.query("SELECT * FROM notices WHERE job_id=?",
                                  (result.job_id,))
    assert [row["session_id"] for row in notices] == ["operator-1"]


def test_handoff_prompt_created_private_and_unlinked_after_dispatch(world):
    """C-2.3, and ledger row 212's replacement: a handoff dispatches through the
    job API, so its prompt is the job's own `jobs/<job id>/prompt.md` at mode
    0600 inside the 0700 state root.

    The unlink half is superseded: the prompt no longer sits in the system temp
    directory, and it is retained as the immutable record until retention
    removes it (C-8.4).
    """
    service, client, home, _store, root, policy, base = world
    repo = workdir(base)
    fx.transcript(home, ALICE, [
        fx.typed_prompt("port the importer", uuid="p0", at=fx.ago(3600)),
        fx.assistant_text("stopped here", uuid="last", at=fx.ago(60))], cwd=str(repo))
    result = handoff_module.handoff(
        client, policy, session_id=ALICE, last=False, model="astra",
        stage_prompt=stage(root), workdir=repo, caller_session="operator-1")

    job = service.store.get_job(result.job_id)
    prompt = Path(job["prompt_path"])
    assert prompt.parent == (root / "jobs" / result.job_id).resolve()
    assert prompt.is_file(), "retained as the record, not unlinked"
    assert oct(prompt.stat().st_mode & 0o777) == "0o600"
    assert oct(prompt.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(root.stat().st_mode & 0o777) == "0o700"


# --- the op's own contract ----------------------------------------------------

def test_an_unknown_sessions_action_is_invalid_input(world):
    """C-16.2, C-17.3: a typo is exit 2 with the action named, not a silent no-op."""
    from subfleet import protocol
    _service, client, _home, _store, _root, _policy, _base = world
    with pytest.raises(protocol.ProtocolError, match="unknown sessions action"):
        client.service.dispatch("sessions", {"action": "nonsense"})


def test_recording_a_nudge_without_a_session_is_invalid_input(world):
    """C-16.2: a missing required key is exit 2, not a row with no target."""
    from subfleet import protocol
    _service, client, _home, _store, _root, _policy, _base = world
    for action in ("nudged", "revived", "retire", "unretire"):
        with pytest.raises(protocol.ProtocolError, match="session_id is required"):
            client.service.dispatch("sessions", {"action": action})
