"""C-5.10: a failure the daemon logs by type also says where it was raised, and never what it said.

Incident, 2026-09-22: `daemon.log` read `worker admission failed: ValueError` up to
`(128 in a row, next try in 60 s)` while nothing was placed for 198 minutes, and no line said
where the ValueError came from. The worker loop logs only the exception's type, because a
provider or keychain error can carry a secret in its message. The traceback carries no
message: file, line, function, and a line of this program's source.
"""

import re
import threading

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.daemon import raise_signature, traceback_lines, traceback_text
from tests.fake.test_admission_visibility import Inline, fleet  # noqa: F401  (fixture)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)

#: Built at run time, so that no line of source a traceback shows can contain it.
SECRET = "-".join(["sk", "ant", "fixture", "0123456789"])


class FixtureError(Exception):
    pass


def resolve_pin(detail):
    raise ValueError(f"pinned_lane: ambiguous lane {detail!r}")


def evaluate_job(detail):
    resolve_pin(detail)


def refresh_readings(detail):
    raise ValueError(detail)


def read_view(detail):
    raise RuntimeError(detail)


def write_snapshot(detail):
    raise RuntimeError(detail)


def raised(fn, *args):
    try:
        fn(*args)
    except BaseException as exc:            # noqa: BLE001  (the fixture is the exception)
        return exc
    raise AssertionError("fixture did not raise")


def records(service, prefix):
    """Each daemon.log record whose first line starts with `prefix`, with its indented continuation."""
    service._log_handler.flush()
    found, current = [], None
    for line in (service.root / "daemon.log").read_text().splitlines():
        if line.startswith("  ") and current is not None:
            current.append(line)
            continue
        current = [line] if line.startswith(prefix) else None
        if current is not None:
            found.append(current)
    return found


def traced(record):
    return any("Traceback (most recent call last):" in line for line in record)


# --- what the traceback carries -------------------------------------------------------------------

def test_c5_10_the_traceback_names_every_frame_and_withholds_every_message():
    exc = raised(evaluate_job, SECRET)
    lines = traceback_lines(exc)
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[-1] == "ValueError"                                   # not `ValueError: ...`
    frames = [line for line in lines if line.startswith("  File ")]
    assert [re.search(r", in (\w+)$", line).group(1) for line in frames] == ["raised", "evaluate_job", "resolve_pin"]
    assert all(__file__ in line for line in frames)
    assert '    raise ValueError(f"pinned_lane: ambiguous lane {detail!r}")' in lines   # source, not the value
    assert SECRET not in "\n".join(lines)


def test_c5_10_a_chained_cause_and_context_keep_their_frames_and_lose_their_messages():
    def wrapped():
        try:
            resolve_pin(SECRET)
        except ValueError as cause:
            raise FixtureError(SECRET) from cause

    def during():
        try:
            wrapped()
        except FixtureError:
            exc = KeyError(SECRET)
            exc.add_note(SECRET)
            raise exc

    exc = raised(during)
    text = "\n".join(traceback_lines(exc))
    assert SECRET not in text
    kinds = [line for line in traceback_lines(exc) if not line.startswith(" ") and "Traceback" not in line]
    assert kinds == ["ValueError", daemon_module._CAUSE, f"{__name__}.FixtureError", daemon_module._CONTEXT, "KeyError"]
    assert "in resolve_pin" in text and "in wrapped" in text and "in during" in text


def test_c5_10_an_exception_that_was_never_raised_is_its_type_and_a_cycle_ends():
    assert traceback_lines(ValueError(SECRET)) == ["ValueError"]
    first, second = ValueError(SECRET), TypeError(SECRET)
    first.__context__, second.__context__ = second, first
    assert traceback_lines(first) == ["TypeError", daemon_module._CONTEXT, "ValueError"]


def test_c5_10_the_signature_follows_the_site_and_not_the_message():
    one, two = raised(evaluate_job, "a"), raised(evaluate_job, "b")
    elsewhere = raised(refresh_readings, "a")
    assert raise_signature(one) == raise_signature(two)
    assert raise_signature(one) != raise_signature(elsewhere)


def test_c5_10_the_block_is_stamped_and_indented_so_line_filters_skip_it():
    block = traceback_text(raised(evaluate_job, SECRET))
    assert block.startswith("\n  ")
    header, *rest = block[1:].split("\n")
    assert re.fullmatch(r"  \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ traceback of ValueError, messages withheld \(C-5\.10\):", header)
    assert rest and all(line.startswith("  ") for line in rest) and SECRET not in block


# --- the worker loop ------------------------------------------------------------------------------

def test_c5_10_the_incident_the_first_admission_failure_says_where_it_was_raised(fleet, monkeypatch):
    """C-5.10 incident 2026-09-22: 128 `worker admission failed: ValueError` lines, none saying where."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)

    def ordered_jobs(policy, queued):
        evaluate_job(SECRET)

    monkeypatch.setattr(daemon_module.scheduler, "ordered_jobs", ordered_jobs)
    for _ in range(8):
        service._worker_retry_at.pop("admission", None)              # its retry time arrives
        service._schedule("admission", service._admit, paced=True)
    found = records(service, "worker admission failed")
    assert [record[0] for record in found] == [
        f"worker admission failed: ValueError ({n} in a row, next try in 3600 s)" for n in (1, 2, 4, 8)]
    assert [traced(record) for record in found] == [True, False, False, False]   # once per streak
    frames = [line for line in found[0] if line.startswith("    File ")]
    names = [re.search(r", in (\w+)$", line).group(1) for line in frames]
    assert names[-5:] == ["_admit", "_admit_pass", "ordered_jobs", "evaluate_job", "resolve_pin"]
    assert found[0][-1] == "  ValueError"
    assert SECRET not in (service.root / "daemon.log").read_text()


def test_c5_10_a_new_cause_in_a_streak_is_traced_at_once_and_a_success_starts_a_new_streak(fleet, monkeypatch):
    service, _ = fleet
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)
    plan = iter(["pin", "pin", "pin", "pin", "readings", "pin", "readings", "ok", "pin"])

    def work():
        step = next(plan)
        if step == "pin":
            evaluate_job(SECRET)
        elif step == "readings":
            refresh_readings(SECRET)

    for _ in range(9):
        service._worker_retry_at.pop("fixture/a1", None)
        service._schedule("fixture/a1", work, paced=True)
    found = records(service, "worker fixture/a1 failed")
    heads = [re.search(r"\((\d+) in a row", record[0]).group(1) for record in found]
    # 1, 2, 4 by count; 5 because it was raised somewhere new; 1 again after the success.
    assert heads == ["1", "2", "4", "5", "1"]
    assert [traced(record) for record in found] == [True, False, False, True, True]
    assert "in refresh_readings" in "\n".join(found[3]) and "in resolve_pin" not in "\n".join(found[3])
    assert service._worker_failures["fixture/a1"] == 1
    assert SECRET not in (service.root / "daemon.log").read_text()


def test_c5_10_the_success_forgets_what_the_streak_traced(fleet):
    service, _ = fleet
    service.workers = Inline(service.workers)
    service._schedule("fixture/a2", lambda: evaluate_job(SECRET), paced=True)
    assert service._worker_traced["fixture/a2"]
    service._worker_retry_at.pop("fixture/a2", None)
    service._schedule("fixture/a2", lambda: None, paced=True)
    assert "fixture/a2" not in service._worker_traced


def test_c5_10_a_one_shot_request_that_fails_is_traced_each_time(fleet):
    service, _ = fleet
    service.workers = Inline(service.workers)
    for _ in range(2):
        service._schedule("resolve:fixture", lambda: evaluate_job(SECRET))
    found = records(service, "worker resolve:fixture failed")
    assert [record[0] for record in found] == ["worker resolve:fixture failed: ValueError"] * 2
    assert all(traced(record) for record in found)
    assert SECRET not in (service.root / "daemon.log").read_text()


# --- the control loop and requests ----------------------------------------------------------------

def test_c5_10_a_control_iteration_is_traced_once_per_cause_until_one_is_clean(fleet, monkeypatch):
    service, _ = fleet
    plan = ["pin", "pin", "pin", "ok", "pin", "readings", "pin"]
    step = {"n": 0}

    def query(sql, *args, **kwargs):
        what = plan[step["n"]]
        if what == "pin":
            evaluate_job(SECRET)
        if what == "readings":
            refresh_readings(SECRET)
        return []

    def wait(timeout=None):
        step["n"] += 1
        if step["n"] == len(plan):
            service.stopping.set()
        return service.stopping.is_set()

    monkeypatch.setattr(service.store, "query", query)
    monkeypatch.setattr(service, "_schedule", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.stopping, "wait", wait)
    service._control()
    found = records(service, "control iteration failed")
    assert [record[0] for record in found] == ["control iteration failed: ValueError"] * 6
    assert [traced(record) for record in found] == [True, False, False, True, True, False]
    assert "in refresh_readings" in "\n".join(found[4])
    assert SECRET not in (service.root / "daemon.log").read_text()


class Connection:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


def test_c5_10_a_failed_request_is_traced_once_per_operation_and_cause(fleet, monkeypatch):
    service, _ = fleet

    # ValueError, TypeError and KeyError are the caller's (`invalid arguments`); anything else is the daemon's.
    def dispatch(op, args):
        read_view(SECRET) if op != "fixture.other" else write_snapshot(SECRET)

    monkeypatch.setattr(service, "dispatch", dispatch)
    for op in ("fixture.status", "fixture.status", "fixture.why", "fixture.other"):
        conn = Connection()
        service._respond(conn, threading.Lock(), protocol.Request(op=op, id="1"))
        assert b"operation failed; inspect daemon status" in conn.sent and SECRET.encode() not in conn.sent
    found = records(service, "request fixture.")
    assert [record[0] for record in found] == [
        "request fixture.status failed: RuntimeError", "request fixture.status failed: RuntimeError",
        "request fixture.why failed: RuntimeError", "request fixture.other failed: RuntimeError"]
    assert [traced(record) for record in found] == [True, False, True, True]
    assert "in write_snapshot" in "\n".join(found[3]) and "in read_view" not in "\n".join(found[3])
    assert SECRET not in (service.root / "daemon.log").read_text()


def test_c5_10_a_traceback_that_cannot_be_read_still_paces_the_worker_and_keeps_the_loop(fleet, monkeypatch):
    """The diagnostic runs where a failure is paced; if it raised, the worker would spin, or the
    exception would leave `_control`'s handler and end the control thread."""
    service, _ = fleet
    service.workers = Inline(service.workers)

    def unreadable(tb, limit=None):
        raise OSError("source unavailable")

    monkeypatch.setattr(daemon_module.traceback, "extract_tb", unreadable)
    service._schedule("fixture/a4", lambda: evaluate_job(SECRET), paced=True)
    assert service._worker_failures["fixture/a4"] == 1 and service._worker_retry_at["fixture/a4"] > 0
    found = records(service, "worker fixture/a4 failed")
    assert found[0][0] == "worker fixture/a4 failed: ValueError (1 in a row, next try in 0.5 s)"
    assert found[0][-2:] == ["  traceback unavailable: OSError", "  ValueError"]
    steps = {"n": 0}

    def query(sql, *args, **kwargs):
        evaluate_job(SECRET)

    def wait(timeout=None):
        steps["n"] += 1
        if steps["n"] == 3:
            service.stopping.set()
        return service.stopping.is_set()

    monkeypatch.setattr(service.store, "query", query)
    monkeypatch.setattr(service.stopping, "wait", wait)
    service._control()                                               # returns only because stopping was set
    assert len(records(service, "control iteration failed")) == 3
    assert SECRET not in (service.root / "daemon.log").read_text()


@pytest.mark.parametrize("prefix", ["admission:", "worker ", "guard preflight "])
def test_c5_10_a_traced_record_adds_no_line_a_prefix_filter_would_match(fleet, prefix):
    """C-6.11's `admission:` filter and the others in this suite read line starts; the block is indented."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    service._schedule("fixture/a3", lambda: evaluate_job(SECRET), paced=True)
    service._log_handler.flush()
    lines = (service.root / "daemon.log").read_text().splitlines()
    starts = [line for line in lines if line.startswith(prefix)]
    assert len(starts) == (1 if prefix == "worker " else 0)
    assert any(line.startswith("  Traceback") for line in lines)
