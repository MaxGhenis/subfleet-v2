"""C-6.11 and C-5.10: a stalled queue says so, and a failing worker does not spin.

Incident, 2026-09-20: fifteen jobs sat queued for hours beside open lanes and
nothing said why. `subfleet why <queued job>` printed `null` (a job admission
skipped has no decision row, and the CLI printed the missing value rather than
the daemon's text). `subfleet status` said `running jobs: 519`, the size of the
whole store. `daemon.log` had not gained a line since the daemon started.
"""

import linecache
import logging
import re
import subprocess
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol, render
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    service, harness = routing_state
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def age(service, seconds):
    """Move the idle stretch `seconds` into the past: no test here waits on a real clock."""
    state = service._admission
    for key in ("idle_since", "checked_at", "logged_at"):
        if state.get(key) is not None:
            state[key] -= seconds


class Inline:
    """A worker pool that runs the work in the caller, so no test waits on a thread or a clock."""

    def __init__(self, real):
        self.real = real

    def submit(self, fn, *args):
        from concurrent.futures import Future
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **options):
        self.real.shutdown(**options)


def log_lines(service):
    service._log_handler.flush()
    return [line for line in (service.root / "daemon.log").read_text().splitlines() if line.startswith("admission:")]


# --- why (C-6.11) -------------------------------------------------------------------------------

def test_c6_11_the_incident_why_names_the_job_a_queued_job_is_held_behind(fleet):
    """C-6.11 a job admission skipped has no decision row and is answered anyway."""
    service, harness = fleet
    older = submit(service, harness, pinned_model="astra")
    held = submit(service, harness, pinned_model="astra")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service._admit()
    assert not service.store.list_decisions(held)                    # the precondition of the `null`
    answer = service.dispatch("why", {"job_id": held})
    assert answer["decision"] is not None and answer["decision_source"] == "evaluated-now"
    assert answer["job"]["hold"] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert older in answer["text"] and "C-6.9" in answer["text"] and "null" not in answer["text"]
    assert not service.store.list_decisions(held)                    # answering is not admission: nothing is recorded


def test_c6_11_why_for_a_job_nothing_admits_names_the_reason_and_the_rechecks(fleet):
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    for _ in range(3):
        service._admit()
        service.store.update_job(stuck, next_check_at=utcnow())
    answer = service.dispatch("why", {"job_id": stuck})
    assert answer["decision_source"] == "recorded"
    assert answer["job"]["recheck"]["rechecks"] == 2 and "signature" not in answer["job"]["recheck"]
    assert any(line.startswith("Rechecks: same verdict 3 times") for line in answer["queue"])
    assert any(line.startswith("Held: no lane admits it") for line in answer["queue"])


def test_c6_11_why_before_any_pass_and_after_the_job_ended(fleet):
    service, harness = fleet
    fresh = submit(service, harness, pinned_model="astra")
    answer = service.dispatch("why", {"job_id": fresh})
    assert answer["decision"]["chosen_lane"] == "codex-1"
    assert "no admission pass has reached this job yet" in answer["text"]
    service.kill(protocol.KillArgs(fresh))
    ended = service.dispatch("why", {"job_id": fresh})
    assert ended["decision"] is None and ended["job"]["hold"] is None
    assert ended["text"].splitlines() == [f"Job: {fresh} is cancelled", "No decision recorded."]


def test_c6_11_the_full_fleet_is_named(fleet):
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    third = submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._holds[second]["reason"] == "fleet-full"          # evaluated: every lane `no-slot`, the cap the cause
    assert service._holds[third] == {"reason": "fleet-full", "max_active_attempts": 1}     # not evaluated: the pass ended
    assert "max_active_attempts (1)" in service.dispatch("why", {"job_id": third})["text"]
    assert "max_active_attempts" in service.dispatch("why", {"job_id": second})["text"]


@pytest.mark.parametrize("hold,expected", [
    ({"reason": "lease-held", "leases": ["out:/r.md"]}, "a lease this job needs is held by another job: out:/r.md"),
    ({"reason": "probe-pending"}, "its lane is being probed"),
    ({"reason": "reserve:fable:unmeasured"}, "no lane admits it (reserve:fable:unmeasured)"),
    ({"reason": "behind-older-job"}, "held behind ?"),                # a hold missing its fields still renders
    (None, "no admission pass has reached this job yet"),
])
def test_c6_11_every_hold_renders_as_a_sentence(hold, expected):
    lines = render.why_queue({"job_id": "j", "state": "queued", "hold": hold})
    assert expected in "\n".join(lines)


# --- daemon.log and status (C-6.11) -------------------------------------------------------------

def test_c6_11_the_log_says_when_jobs_are_pending_and_nothing_is_placed(fleet):
    """C-6.11 one line after a minute, not one a pass; the open lanes and the reasons on it."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    behind = submit(service, harness, pinned_model="opus")
    service._admit()
    assert log_lines(service) == []                                  # under a minute: nothing yet
    age(service, 61)
    for _ in range(50):
        service._admit()
    lines = log_lines(service)
    assert len(lines) == 1
    assert re.search(r"2 jobs pending, none placed for 6\d s", lines[0])      # 61 s, or 62 on a slow runner
    assert "1 lanes open (codex-1)" in lines[0]
    assert "behind-older-job x1" in lines[0] and "no-lanes x1" in lines[0] and f"first in line {stuck}" in lines[0]
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert len(log_lines(service)) == 2                              # and again every ten minutes while it lasts
    for job_id in (stuck, behind):
        service.kill(protocol.KillArgs(job_id))
    service._admit()
    assert log_lines(service)[-1].startswith("admission: nothing left pending after")
    service._admit()
    assert len(log_lines(service)) == 3


def test_c6_11_open_lanes_make_it_a_warning_and_none_make_it_information(fleet):
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 61)
    service._admit()
    with service.store.transaction("fixture.disable") as tx:
        tx.execute("UPDATE lanes SET enabled=0")
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.WARNING]  # no lane open now: expected, and said within the hour
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_EXPECTED_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.WARNING, logging.INFO]
    assert "0 lanes open (-)" in seen[1].getMessage()


def test_c6_11_a_wait_that_becomes_a_warning_is_one_within_ten_minutes(fleet):
    """C-6.11 review of cb83e1b: the interval was chosen from the last line's severity, so an expected
    wait that turned into the incident stayed silent for the rest of the hour."""
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    with service.store.transaction("fixture.disable") as tx:
        tx.execute("UPDATE lanes SET enabled=0")
    submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 61)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO]     # no lane open: waiting is expected
    with service.store.transaction("fixture.enable") as tx:
        tx.execute("UPDATE lanes SET enabled=1")
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO, logging.WARNING]


def test_c6_11_a_fleet_at_its_cap_is_ordinary_queueing_not_a_warning(fleet):
    """C-6.11 four jobs running and ten behind them, lanes to spare: information, hourly, never a warning."""
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="astra")
    service._admit()
    service._admit()                                                 # a pass that places nothing starts the idle stretch
    assert service._admission["reasons"] == {"fleet-full": 2}
    age(service, 61)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO] and "fleet-full x2" in seen[0].getMessage()
    assert "1 lanes open (codex-1)" in seen[0].getMessage()          # the second slot of a measured lane
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert len(seen) == 1                                            # ten minutes on: looked at, still nothing to say
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_EXPECTED_S)
    service._admit()
    assert len(seen) == 2


def test_c6_11_a_job_whose_earlier_attempt_is_still_live_says_so_and_is_not_counted(fleet):
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    service._admit()
    service.store.update_job(job_id, state="queued")                 # between attempts: the first is still live
    service._admit()
    assert service._holds[job_id] == {"reason": "attempt-live"} and service._admission["pending"] == 0
    assert "an earlier attempt of this job is still live" in service.dispatch("why", {"job_id": job_id})["text"]


def test_c6_11_a_placement_ends_the_idle_stretch_and_waits_a_person_must_end_do_not_start_one(fleet):
    service, harness = fleet
    approval = submit(service, harness, pinned_model="astra")
    service.store.update_job(approval, state="waiting", wait_reason="approval")
    service._admit()
    assert service._admission["pending"] == 0 and service._admission["idle_since"] is None
    submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._admission["placed_at"] and service._admission["idle_since"] is None


def test_c6_11_status_reports_admission_and_counts_only_live_jobs(fleet):
    """C-6.11, C-17.1 `running jobs: 519` was every job in the store."""
    from subfleet import cli
    service, harness = fleet
    done = submit(service, harness, pinned_model="astra")
    service.kill(protocol.KillArgs(done))
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 3700)
    data = service.dispatch("daemon.status", {})
    assert data["admission"]["pending"] == 1 and data["admission"]["open_lanes"] == ["codex-1"]
    assert data["admission"]["reasons"] == {"no-lanes": 1} and data["admission"]["idle_for_s"] >= 3700
    assert {row["job_id"] for row in data["jobs"]} == {done, stuck}  # the capacity view carries them all
    text = cli.format_status(data)
    assert "running jobs: 1" in text and done not in text and stuck in text
    assert "admission: 1 pending, none placed for 37" in text and "no-lanes x1" in text


# --- a worker that raises (C-5.10) --------------------------------------------------------------

#: A secret-shaped value the C-23.14 scrub list removes. Nothing here is a real credential.
FAKE_TOKEN = "sk-ant-FAKEFAKEFAKEFAKEFAKEFAKE1234"
CAUSE = "Traceback (most recent call last)"


def worker_log(service) -> str:
    service._log_handler.flush()
    return (service.root / "daemon.log").read_text()


def summaries(text: str, key: str) -> list[int]:
    """The consecutive-failure count on each `worker <key> failed` line, in order."""
    return [int(m) for m in re.findall(rf"^worker {re.escape(key)} failed: \w+ \((\d+) in a row", text, re.M)]


def fail_repeatedly(service, key, fn, times):
    """Offer `key` once per failure, as the control loop does once each retry time comes."""
    for _ in range(times):
        service._worker_retry_at[key] = 0
        service._schedule(key, fn, paced=True)


def test_c5_10_a_worker_that_raises_is_retried_with_backoff_and_logged_sparsely(fleet, monkeypatch):
    """C-5.10 the control loop offers a live key every 50 ms; forty SalvageError lines were one attempt."""
    service, _ = fleet
    # No thread and no wall clock: the work runs inline, and the retry time is an hour off until the test says it has come.
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)
    calls = []

    def boom():
        calls.append(len(calls))
        raise RuntimeError(f"provider refused: Authorization: Bearer {FAKE_TOKEN}\napi_key={FAKE_TOKEN}")

    for _ in range(40):
        service._schedule("fixture/a1", boom, paced=True)
    assert len(calls) == 1                                           # not forty
    for expected in (2, 3, 4, 5):
        service._worker_retry_at["fixture/a1"] = 0                   # its retry time arrives
        for _ in range(10):
            service._schedule("fixture/a1", boom, paced=True)
        assert len(calls) == expected
    text = worker_log(service)
    assert summaries(text, "fixture/a1") == [1, 2, 4]
    lines = [line for line in text.splitlines() if line.startswith("worker fixture/a1 failed")]
    assert all("RuntimeError (" in line for line in lines)           # the summary line keeps its shape
    # The cause rides the first failure only: its message and traceback, the credential scrubbed.
    assert text.count(CAUSE) == 1 and "RuntimeError: provider refused" in text and "in boom" in text
    assert FAKE_TOKEN not in text and "[REDACTED]" in text
    assert service._worker_failures["fixture/a1"] == 5 and "fixture/a1" not in service._busy


def test_c5_10_the_cause_is_logged_on_the_first_failure_of_a_streak_and_every_32nd_after(fleet, monkeypatch):
    """C-5.10 incident 2026-09-22: `worker admission failed: ValueError (128 in a row, ...)` for three hours,
    and the message that named the cause appeared nowhere. Its message now reaches `daemon.log`,
    on the 1st, 33rd, 65th ... failure, and not on every retry."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)
    incident = "pinned_lane: ambiguous lane 'max@example.org'; use a lane id"

    def stalled_pass(holds, tally):
        raise ValueError(incident)

    monkeypatch.setattr(service, "_admit_pass", stalled_pass)
    fail_repeatedly(service, "admission", service._admit, 70)
    text = worker_log(service)
    assert summaries(text, "admission") == [1, 2, 4, 8, 16, 32, 33, 64, 65]
    records = re.split(r"^(?=worker admission failed)", text, flags=re.M)
    with_cause = [int(re.search(r"\((\d+) in a row", r).group(1)) for r in records if CAUSE in r]
    assert with_cause == [1, 33, 65]
    assert text.count(f"ValueError: {incident}") == 3 and "in stalled_pass" in text
    assert service._worker_failures["admission"] == 70


def test_c5_10_a_success_starts_a_new_streak_that_logs_its_cause_again(fleet, monkeypatch):
    service, _ = fleet
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("the store is locked")

    fail_repeatedly(service, "fixture/a3", flaky, 3)
    state["fail"] = False
    fail_repeatedly(service, "fixture/a3", flaky, 1)
    state["fail"] = True
    fail_repeatedly(service, "fixture/a3", flaky, 2)
    text = worker_log(service)
    assert summaries(text, "fixture/a3") == [1, 2, 1, 2]
    assert text.count(CAUSE) == 2 and text.count("OSError: the store is locked") == 2


def test_c5_10_a_cause_that_cannot_be_rendered_is_logged_as_its_type(fleet, monkeypatch):
    """The done callback must never raise: it releases the key in `finally`, and a lost key never runs again."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module.traceback, "format_exception",
                        lambda exc: (_ for _ in ()).throw(RuntimeError("formatter broke")))

    def boom():
        raise KeyError("lane")

    fail_repeatedly(service, "fixture/a4", boom, 1)
    text = worker_log(service)
    assert summaries(text, "fixture/a4") == [1]
    assert "KeyError (cause could not be rendered)" in text and "fixture/a4" not in service._busy


def test_c5_10_a_long_cause_is_bounded(fleet, monkeypatch):
    service, _ = fleet
    service.workers = Inline(service.workers)

    def boom():
        raise ValueError("a lane row " * 5_000 + "end of message")      # words: no scrub pattern takes it

    fail_repeatedly(service, "fixture/a5", boom, 1)
    record = worker_log(service).split("worker fixture/a5 failed", 1)[1]
    assert len(record) < daemon_module.WORKER_CAUSE_MAX_CHARS + 200
    assert CAUSE in record and "end of message" in record and "characters omitted" in record


#: A credential with no shape of its own: only the name before it marks it. Not a real one.
PLAIN_CREDENTIAL = "q7Wd9Zk2Lp4Xv8Nm3Rt6Yh1Bs5Gc0Jf"


def windows(secret: str, width: int = 8) -> list[str]:
    return [secret[i:i + width] for i in range(len(secret) - width + 1)]


def chained() -> Exception:
    try:
        try:
            raise OSError(f"proxy said Authorization: Basic {PLAIN_CREDENTIAL}")
        except OSError as inner:
            raise RuntimeError("the provider call failed") from inner
    except RuntimeError as outer:
        return outer


def grouped() -> Exception:
    group = ExceptionGroup("two lanes failed", [ValueError("first"), KeyError("second")])
    group.add_note(f"retry with Authorization: Token {PLAIN_CREDENTIAL}")
    return group


@pytest.mark.parametrize("make", [
    lambda: RuntimeError(f"refused: Authorization: Basic {PLAIN_CREDENTIAL}"),
    chained,
    grouped,
    lambda: subprocess.CalledProcessError(
        22, ["curl", "-fsS", "-H", f"Authorization: Basic {PLAIN_CREDENTIAL}", "https://x.test"]),
    lambda: OSError(f"git fetch https://max:{PLAIN_CREDENTIAL}@github.test/r.git: timed out"),
    lambda: ValueError(f"lane config {{'client_secret': '{PLAIN_CREDENTIAL}', 'lane': 'codex-2'}}"),
    lambda: ValueError(f"AUTHORIZATION={PLAIN_CREDENTIAL} COOKIE=session={PLAIN_CREDENTIAL}"),
    lambda: KeyError(f"response headers\nAuthorization: Basic {PLAIN_CREDENTIAL}"),
    lambda: subprocess.CalledProcessError(
        22, ["curl", "-H", f"X-Request-ID: 7\nAuthorization: Basic {PLAIN_CREDENTIAL}", "https://x.test"]),
], ids=["header-mid-line", "chained-cause", "group-note", "subprocess-command", "url-password",
        "dict-repr", "env-style", "escaped-newline-repr", "escaped-newline-command"])
def test_c5_10_a_credential_in_the_cause_never_reaches_the_log(fleet, make):
    """C-5.10 review of PR #26: the scrub list matched a header only at the start of a line, and a
    traceback puts `RuntimeError: ` (or a group's margin, or a command repr) in front of it."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    exc = make()

    def boom():
        raise exc

    fail_repeatedly(service, "fixture/a6", boom, 1)
    text = worker_log(service)
    assert CAUSE in text and type(exc).__name__ in text
    assert not [w for w in windows(PLAIN_CREDENTIAL) if w in text], text


def test_c5_10_the_cause_is_scrubbed_before_it_is_cut():
    """C-5.10 review of PR #26: cut first, and the cut at the start of the kept tail can split a
    credential from its name, which leaves the rest of it bare. Scrubbed first, every copy is
    replaced whole. The padding moves the cut through every part of the repeated assignment."""
    credential = PLAIN_CREDENTIAL + PLAIN_CREDENTIAL[::-1]                 # 64 characters, no shape
    unit = f"api_key={credential} "
    for pad in range(0, len(unit), 4):
        body = "p" * pad + unit * 700
        assert daemon_module.WORKER_CAUSE_MAX_CHARS < len(body) < daemon_module.WORKER_CAUSE_SCRUB_MAX_CHARS
        try:
            raise ValueError(body)
        except ValueError as exc:
            cause = daemon_module.worker_failure_cause(exc)
        assert "characters omitted" in cause and "api_key=[REDACTED]" in cause
        assert not [w for w in windows(credential) if w in cause], pad


@pytest.mark.parametrize("unit", ["x_", "a-", "a", "eyJa."])
def test_c5_10_scrubbing_a_long_identifier_run_takes_milliseconds(unit):
    """C-5.10 review of PR #26: `ValueError("x_" * 3000)` took 4.5 s to scrub on the worker's completion
    path, and the time grew with the square of the length. Under the bound it is now linear."""
    message = unit * ((daemon_module.WORKER_CAUSE_SCRUB_MAX_CHARS - 2000) // len(unit))
    try:
        raise ValueError(message)                                    # raised, so it has frames
    except ValueError as exc:
        error = exc
    started = time.perf_counter()
    cause = daemon_module.worker_failure_cause(error)
    assert time.perf_counter() - started < 2.0                      # quadratic took minutes at this size
    assert cause.startswith(CAUSE) and len(cause) <= daemon_module.WORKER_CAUSE_MAX_CHARS


def test_c5_10_a_cause_over_the_scrub_bound_keeps_its_frames_and_omits_its_message(fleet):
    service, _ = fleet
    service.workers = Inline(service.workers)
    message = "salvage stderr: " + "fatal: unable to read tree " * 5000 + f"password={PLAIN_CREDENTIAL}"

    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise OSError(message)

    fail_repeatedly(service, "fixture/a8", lambda: recurse(60), 1)
    text = worker_log(service)
    assert summaries(text, "fixture/a8") == [1]
    assert "OSError: message omitted: the cause is" in text and "scrub bound" in text
    assert "in recurse" in text and "frames omitted" in text          # 40 of the 60+ frames, and the gap said
    assert "unable to read tree" not in text and PLAIN_CREDENTIAL not in text
    assert "return recurse(depth - 1)" not in text                    # where, not what: no source lines


def test_c5_10_a_huge_source_line_is_reduced_to_where_it_was_raised(fleet):
    """C-5.10 review round 2 of PR #26: the reduced cause kept its frames' source lines and scrubbed them
    whatever their size (23 s for a 256 KB generated line), and cutting frames before the scrub could
    drop a private key's opening marker and keep its body. It now keeps file, line and function only."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    filename = "<generated worker source>"
    source = ("def boom():\n    raise ValueError('oversized source')  # "
              + "-----BEGIN PRIVATE KEY-----" * 3000 + f" {PLAIN_CREDENTIAL}\n")
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    try:
        namespace = {}
        exec(compile(source, filename, "exec"), namespace)
        started = time.perf_counter()
        fail_repeatedly(service, "fixture/a9", namespace["boom"], 1)
        assert time.perf_counter() - started < 2.0
    finally:
        linecache.cache.pop(filename, None)
    text = worker_log(service)
    assert f'File "{filename}", line 2, in boom' in text and "message omitted" in text
    assert "PRIVATE KEY" not in text and PLAIN_CREDENTIAL not in text


@pytest.mark.parametrize("failures,expected", [(1, .5), (2, 1), (3, 2), (4, 4), (7, 32), (8, 60), (500, 60), (0, .5)])
def test_c5_10_retry_delay(failures, expected):
    assert daemon_module.worker_retry_delay(failures) == expected


def test_c5_10_a_success_forgives_the_failures(fleet):
    service, _ = fleet
    service.workers = Inline(service.workers)
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("transient")

    def run_once():
        service._worker_retry_at.pop("fixture/a2", None)
        service._schedule("fixture/a2", flaky, paced=True)

    run_once(); run_once()
    assert service._worker_failures["fixture/a2"] == 2
    state["fail"] = False
    run_once()
    assert "fixture/a2" not in service._worker_failures and "fixture/a2" not in service._worker_retry_at


def test_c5_10_a_one_shot_request_is_never_held_back(fleet):
    """C-5.10 review of cb83e1b: `kill --confirm-dead` schedules `resolve:<job>` once and answers
    "resolution requested". Pacing it dropped the operator's retry: nothing offers that key again."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    calls = []

    def resolve():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("the first resolution fails")

    for _ in range(2):
        service._schedule("resolve:fixture", resolve)                # as `kill` calls it: not paced
    assert calls == [0, 1]                                           # the retry ran at once
    assert "resolve:fixture" not in service._worker_retry_at
    text = worker_log(service)                                       # its failure logs its cause
    assert "worker resolve:fixture failed: RuntimeError\n" + CAUSE in text
    assert "RuntimeError: the first resolution fails" in text


def test_c5_10_every_failure_of_a_one_shot_request_logs_its_cause(fleet):
    """C-5.10 review of PR #26: nothing paces a one-shot key, so no failure of it is a retry."""
    service, _ = fleet
    service.workers = Inline(service.workers)

    def resolve():
        raise RuntimeError("the operator's resolution fails")

    for _ in range(3):
        service._schedule("resolve:fixture2", resolve)
    text = worker_log(service)
    assert text.count("worker resolve:fixture2 failed: RuntimeError\n" + CAUSE) == 3
    assert text.count("RuntimeError: the operator's resolution fails") == 3
    assert "resolve:fixture2" not in service._worker_failures


# --- what another thread reads (C-6.11) ---------------------------------------------------------

def test_c6_11_status_reads_one_snapshot_that_admission_never_changes_under_it(fleet):
    """C-6.11 review of cb83e1b: `daemon.status` read `idle_since` twice and admission could clear it
    between the reads (`float - None`). The snapshot is replaced whole and the old one is left alone."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    service._admit()
    before = service._admission
    frozen = dict(before)
    assert before["idle_since"] is not None
    service.kill(protocol.KillArgs(stuck))
    service._admit()                                                 # the stretch ends: idle_since goes to None
    assert service._admission is not before and service._admission["idle_since"] is None
    assert before == frozen                                          # a reader holding the old one saw no half-update
    assert service.dispatch("daemon.status", {})["admission"]["idle_for_s"] is None


def test_c6_11_a_pass_that_does_not_look_keeps_the_whole_hold(fleet):
    """C-6.11 review of cb83e1b: the pass after the look kept only the label, and `why` printed
    `held by another job: -` and `max_active_attempts (?)`."""
    service, harness = fleet
    out = str(harness.root / "report.md")
    waiting = submit(service, harness, pinned_model="terra", out_path=out)
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    service._admit()
    service.store.update_job(waiting, next_check_at=after(3600))
    service._admit()                                                 # not due: nothing is looked at
    assert service._holds[waiting]["leases"] == [f"out:{out}"]
    assert f"held by another job: out:{out}" in service.dispatch("why", {"job_id": waiting})["text"]


def test_c6_11_the_reason_is_the_latest_looks_even_when_the_verdict_repeats(fleet, monkeypatch):
    """C-6.11 review of cb83e1b: a probe counts toward the fleet cap, so one verdict read `fleet-full`
    on one look and the real reason on the next; the first label stuck and hid the warning."""
    service, harness = fleet
    labels = iter(["fleet-full", "reserve:fable:unmeasured"])
    monkeypatch.setattr(daemon_module.scheduler, "dominant_rejection", lambda decision: next(labels))
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._holds[stuck]["reason"] == "fleet-full"
    service.store.update_job(stuck, next_check_at=utcnow())
    service._admit()
    assert service._capacity_waits[stuck]["rechecks"] == 1           # the same verdict: no new row
    assert len(service.store.list_decisions(stuck)) == 1
    service.store.update_job(stuck, next_check_at=after(3600))
    service._admit()                                                 # a pass that does not look reports the last look
    assert service._holds[stuck]["reason"] == "reserve:fable:unmeasured"
    assert service._admission["reasons"] == {"reserve:fable:unmeasured": 1}



# --- review of be171d5 --------------------------------------------------------------------------

def test_c6_11_the_slot_kept_for_an_older_job_is_not_called_a_full_fleet(fleet):
    """C-6.11, C-6.9: with a cap of 2, one attempt running and an older job waiting, a later job is
    held one short of the cap. `why` said "the fleet is at max_active_attempts (2)" beside a free slot."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 2
    submit(service, harness, pinned_model="terra")
    service._admit()
    older = submit(service, harness, pinned_model="astra")
    passer = submit(service, harness, pinned_model="terra")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(3600))
    service._admit()
    hold = service._holds[passer]
    assert (hold["reason"], hold["kept_for"], hold["live"], hold["max_active_attempts"]) == ("slot-kept", older, 1, 2)
    text = service.dispatch("why", {"job_id": passer})["text"]
    assert f"1 of 2 attempts are running and the last slot is kept for {older}" in text and "fleet is at" not in text
    assert "slot-kept" in daemon_module.EXPECTED_HOLDS               # ordinary queueing: never a warning


def test_c6_11_a_workspace_retry_is_reported_as_one_even_behind_a_full_fleet(fleet):
    """C-6.11: the full-fleet branch came first, so a C-6.8 retry was counted as `fleet-full`, and as pending."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")                   # takes the only slot
    blocked = submit(service, harness, pinned_model="terra")         # looked at, finds the fleet full: the pass ends
    retrying = submit(service, harness, pinned_model="astra")
    service.store.update_job(retrying, state="waiting", wait_reason="workspace", next_check_at=after(3600))
    service._admit()
    assert service._holds[blocked]["reason"] == "fleet-full"
    assert service._holds[retrying] == {"reason": "workspace"}       # reached by the full-fleet branch, reported as itself
    assert service._admission["pending"] == 1                        # `blocked` only: a retry is not admission's to place


def test_c6_11_a_pass_that_raises_publishes_nothing(fleet):
    """C-6.11: half a hold set read as "nothing left pending" and ended the idle stretch, queue untouched."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 61)
    service._admit()
    assert len(log_lines(service)) == 1
    holds, snapshot = service._holds, service._admission
    real = service._admit_pass

    def raising(holds, tally):
        raise OSError("no space left on device")

    service._admit_pass = raising
    with pytest.raises(OSError):
        service._admit()
    assert service._holds is holds and service._admission is snapshot
    assert len(log_lines(service)) == 1                              # no "nothing left pending"
    service._admit_pass = real
    service._admit()
    assert service._holds[stuck]["reason"] == "no-lanes"
