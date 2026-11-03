"""C-8.6: the daemon's `retire` op records a request that retention retire a finished job
ahead of its pool's budget, or takes it back, and answers for each job what retention will
do; a request makes retention due at once, and one recorded while a pass runs brings the
next pass to `RETENTION_CATCH_UP_S`, never the hour. `subfleet retention retire` sends it.

The op runs on a real daemon over a real store; the CLI tests stand a fake client in for
the socket.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from subfleet import cli, client as client_module
from subfleet import daemon as daemon_module
from subfleet import protocol, refusals, retention
from subfleet.contracts import Exit
from subfleet.client import DaemonError, DaemonUnavailable
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def finished(daemon, harness, state: str = "succeeded") -> str:
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.store.conn.execute("UPDATE jobs SET state=?, finished_at=? WHERE job_id=?",
                              (state, daemon_module.utcnow(), job_id))
    return job_id


def retire(daemon, *ids, **flags) -> dict:
    return daemon.dispatch("retire", {"job_ids": list(ids), **flags})


def recorded(daemon, job_id) -> list[tuple[str, dict]]:
    return [(row["kind"], json.loads(row["data_json"])) for row in daemon.store.query(
        "SELECT kind,data_json FROM events WHERE job_id=? AND kind IN (?,?) ORDER BY event_id",
        (job_id, retention.RETIRE_REQUESTED, retention.RETIRE_WITHDRAWN))]


def due_in(daemon) -> float:
    """Seconds until the control loop offers retention again."""
    return daemon_module.RETENTION_INTERVAL_S - (time.monotonic() - daemon._last_maintenance)


# --- the op -------------------------------------------------------------------------

def test_a_request_records_one_event_that_names_the_job(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    created = daemon.store.get_job(job_id)["created_at"]
    first = daemon.dispatch("retire", {"job_ids": [job_id], "reason": "Track B", "session_id": "s-1"})
    (row,) = first["jobs"]
    assert row["outcome"] == "requested" and row["state"] == "succeeded" and first["recorded"] == 1
    assert recorded(daemon, job_id) == [(retention.RETIRE_REQUESTED,
                                         {"created_at": created, "reason": "Track B", "session_id": "s-1"})]
    assert not daemon.store.query("SELECT 1 FROM events WHERE kind='retire.answered'")    # no audit beside it
    again = retire(daemon, job_id)
    assert again["jobs"][0]["outcome"] == "already-requested" and again["recorded"] == 0
    assert again["jobs"][0]["requested_at"] == row["requested_at"]
    assert len(recorded(daemon, job_id)) == 1
    assert set(retention.retire_requests(daemon.store.query)) == {job_id}


def test_what_a_request_cannot_take_is_answered_and_nothing_recorded(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    running = finished(daemon, harness, state="running")
    daemon.store.conn.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                              (daemon_module.utcnow(), "retention.pruned", "gone-job",
                               json.dumps({"archive": "/state/archive/gone-job"})))
    answer = retire(daemon, running, "gone-job", "never-a-job")
    assert [(row["job_id"], row["outcome"]) for row in answer["jobs"]] == [
        (running, "not-finished"), ("gone-job", "retired"), ("never-a-job", "unknown")]
    assert answer["jobs"][1]["archive"] == "/state/archive/gone-job" and answer["recorded"] == 0
    assert retention.retire_requests(daemon.store.query) == {}


def test_the_answer_reads_each_job_by_its_own_events(state_daemon):  # noqa: F811
    """Inside the write transaction, by kind an unknown id walks every retirement recorded
    (1.8 ms each at 4,777 on 2026-11-02); by the job's own events it reads its own."""
    daemon, harness = state_daemon
    statements: list[str] = []
    daemon.store.conn.set_trace_callback(statements.append)
    try:
        retire(daemon, "never-a-job", finished(daemon, harness))
    finally:
        daemon.store.conn.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT") and "FROM events" in sql
             and ("job.retire_" in sql or "retention.pruned" in sql)]
    assert len(reads) == 2, statements      # the unknown id's retirement, the finished job's requests
    for sql in reads:
        plan = " | ".join(row["detail"] for row in daemon.store.query("EXPLAIN QUERY PLAN " + sql))
        assert "events_job" in plan, (sql, plan)


def test_a_withdrawal_takes_the_request_back_once(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    retire(daemon, job_id)
    assert retire(daemon, job_id, withdraw=True)["jobs"][0]["outcome"] == "withdrawn"
    assert retire(daemon, job_id, withdraw=True)["jobs"][0]["outcome"] == "not-requested"
    assert [kind for kind, _ in recorded(daemon, job_id)] == [retention.RETIRE_REQUESTED,
                                                             retention.RETIRE_WITHDRAWN]
    assert retention.retire_requests(daemon.store.query) == {}
    assert retire(daemon, job_id)["jobs"][0]["outcome"] == "requested"      # asked again, it stands again


def test_a_dry_run_answers_the_same_and_records_nothing(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    daemon._last_maintenance = before = time.monotonic()
    answer = retire(daemon, job_id, dry_run=True)
    assert answer["jobs"][0]["outcome"] == "requested" and answer["dry_run"] is True and answer["recorded"] == 0
    assert recorded(daemon, job_id) == [] and daemon._last_maintenance == before


def test_each_job_is_answered_once(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    answer = retire(daemon, job_id, job_id, job_id)
    assert [row["job_id"] for row in answer["jobs"]] == [job_id] and len(recorded(daemon, job_id)) == 1


def test_a_request_an_earlier_job_with_the_id_left_does_not_stand(state_daemon):  # noqa: F811
    """A retirement keeps the job's events, so its request names its `created_at`."""
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    daemon.store.conn.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                              (daemon_module.utcnow(), retention.RETIRE_REQUESTED, job_id,
                               json.dumps({"created_at": "2026-01-01T00:00:00Z"})))
    assert retire(daemon, job_id, withdraw=True)["jobs"][0]["outcome"] == "not-requested"
    answer = retire(daemon, job_id)
    assert answer["jobs"][0]["outcome"] == "requested" and answer["recorded"] == 1


@pytest.mark.parametrize("args, reason", [
    ({"job_ids": []}, "retire-ids"),
    ({"job_ids": "a-job"}, "retire-ids"),
    ({"job_ids": ["a", 7]}, "retire-ids"),
    ({"job_ids": ["a", ""]}, "retire-ids"),
    ({"job_ids": [f"j{n}" for n in range(protocol.RETIRE_MAX_JOBS + 1)]}, "retire-ids"),
    ({"job_ids": ["a"], "withdraw": "yes"}, "retire-flags"),
    ({"job_ids": ["a"], "dry_run": 1}, "retire-flags"),
    ({"job_ids": ["a"], "reason": "x" * (protocol.RETIRE_REASON_MAX + 1)}, "retire-reason"),
    ({"job_ids": ["a"], "reason": 5}, "retire-reason"),
])
def test_a_malformed_request_is_refused_whole(state_daemon, args, reason):  # noqa: F811
    daemon, _ = state_daemon
    with pytest.raises(protocol.ProtocolError) as refused:
        daemon.dispatch("retire", args)
    assert refused.value.reason == reason
    # Its own words reach the wire, not the fallback a reason missing from the catalog reads as.
    assert protocol.refusal_text(refused.value) == refusals.HANDLER_MESSAGES[reason]


# --- the answer says what keeps a job -----------------------------------------------

def test_the_answer_names_a_pin_a_deferral_and_a_retirement_in_flight(state_daemon, monkeypatch):  # noqa: F811
    daemon, harness = state_daemon
    pinned, deferred, flying, free = (finished(daemon, harness) for _ in range(4))
    monkeypatch.setattr(daemon, "_retention_pins", lambda: {pinned})
    daemon._retention_state.defer(deferred, 600, "holder: pid 4242", time.monotonic())
    daemon.store.conn.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES (?,?,?)",
                              (f"retire:{flying}", f"retention:{flying}", daemon_module.utcnow()))
    rows = {row["job_id"]: row for row in retire(daemon, pinned, deferred, flying, free)["jobs"]}
    assert rows[pinned]["kept"] == "conversation"
    assert rows[deferred]["deferred"]["reason"] == "holder: pid 4242" and 0 < rows[deferred]["deferred"]["for_s"] <= 600
    assert rows[flying]["retiring"] is True
    assert not {"kept", "deferred", "retiring"} & set(rows[free]), rows[free]
    withdrawn = {row["job_id"]: row for row in retire(daemon, flying, withdraw=True)["jobs"]}
    assert withdrawn[flying]["outcome"] == "withdrawn" and withdrawn[flying]["retiring"] is True


def test_only_retentions_own_retire_lease_says_retiring(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    daemon.store.conn.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES (?,?,?)",
                              (f"retire:{job_id}", "other", daemon_module.utcnow()))
    (row,) = retire(daemon, job_id)["jobs"]
    assert row["outcome"] == "requested" and "retiring" not in row, row


def test_a_withdrawal_of_an_unfinished_job_records_nothing(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    running = finished(daemon, harness, state="running")
    daemon._last_maintenance = before = time.monotonic()
    answer = retire(daemon, running, withdraw=True)
    assert [(row["outcome"], row["state"]) for row in answer["jobs"]] == [("not-requested", "running")]
    assert answer["recorded"] == 0 and recorded(daemon, running) == []
    assert daemon._last_maintenance == before


def spy_pin_reads(monkeypatch, fail_for: str | None = None) -> list:
    """Each `only` the answer's pin reads name, in order; the read for `fail_for` fails."""
    reads, real = [], daemon_module._pin_reasons

    def pin_reasons(*args, **kwargs):
        reads.append(kwargs["only"])
        if fail_for is not None and kwargs["only"] == fail_for:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(daemon_module, "_pin_reasons", pin_reasons)
    return reads


@pytest.mark.parametrize("each", [3, 2])
def test_above_the_limit_the_answer_reads_every_jobs_pins_once(state_daemon, monkeypatch, each):  # noqa: F811
    """One job's pin read cost 0.11 s and every job's 3.5 s on the live store, so a request
    naming more than `RETIRE_PIN_READ_EACH` jobs reads them all once; both give one answer."""
    daemon, harness = state_daemon
    pinned, *free = (finished(daemon, harness) for _ in range(3))
    monkeypatch.setattr(daemon_module, "RETIRE_PIN_READ_EACH", each)
    monkeypatch.setattr(daemon, "_retention_pins", lambda: {pinned})
    reads = spy_pin_reads(monkeypatch)
    rows = {row["job_id"]: row for row in retire(daemon, pinned, *free)["jobs"]}
    assert reads == ([pinned, *free] if each == 3 else [None])
    assert rows[pinned]["kept"] == "conversation"
    assert not any({"kept", "pins_unread"} & set(rows[job_id]) for job_id in free), rows


def test_pins_that_could_not_be_read_are_named_and_nothing_else_is_lost(state_daemon, monkeypatch):  # noqa: F811
    daemon, harness = state_daemon
    broken, pinned = (finished(daemon, harness) for _ in range(2))
    daemon._retention_state.defer(broken, 600, "holder: pid 4242", time.monotonic())
    monkeypatch.setattr(daemon, "_retention_pins", lambda: {pinned})
    spy_pin_reads(monkeypatch, fail_for=broken)
    answer = retire(daemon, broken, pinned)
    rows = {row["job_id"]: row for row in answer["jobs"]}
    assert rows[broken]["pins_unread"] == "OperationalError" and "kept" not in rows[broken]
    assert rows[broken]["deferred"]["reason"] == "holder: pid 4242"
    assert rows[pinned]["kept"] == "conversation" and "pins_unread" not in rows[pinned]
    assert answer["recorded"] == 2 and [kind for kind, _ in recorded(daemon, broken)] == [retention.RETIRE_REQUESTED]

    def no_pins():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(daemon, "_retention_pins", no_pins)
    rows = {row["job_id"]: row for row in retire(daemon, broken, pinned)["jobs"]}
    assert all(row["pins_unread"] == "OperationalError" and "kept" not in row for row in rows.values()), rows
    assert rows[broken]["deferred"]["reason"] == "holder: pid 4242"


# --- when retention runs ------------------------------------------------------------

def test_a_request_makes_retention_due_now_and_nothing_else_does(state_daemon):  # noqa: F811
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    daemon._last_maintenance = time.monotonic()
    retire(daemon, job_id, dry_run=True)
    assert due_in(daemon) > 3000 and daemon._retire_asked == 0
    retire(daemon, job_id)
    assert due_in(daemon) <= 0 and daemon._retire_asked == 1
    daemon._last_maintenance = time.monotonic()
    retire(daemon, job_id)                              # already requested: records nothing
    retire(daemon, job_id, withdraw=True)               # a withdrawal needs no pass
    assert due_in(daemon) > 3000 and daemon._retire_asked == 1


#: Every way a pass ends that would rearm the hour, and the error its status mark names
#: (`None`: none). A request recorded during any of them brings the next pass in 5 s.
REARMING = pytest.mark.parametrize("outcome, error", [
    ({"pruned": []}, None),
    ({"pruned": [], "interrupted": "deadline", "progressed": False}, "TimeoutError"),
    ({"pruned": [], "holder_scan_failed": True}, "ScanFailed"),
    ({"pruned": [], "interrupted": "cancelled"}, "CancelledError"),
], ids=["completed", "deadline", "holder-scan-failed", "cancelled"])


@REARMING
def test_a_request_during_a_pass_brings_the_next_pass_to_catch_up(state_daemon, monkeypatch, caplog,  # noqa: F811
                                                                  outcome, error):
    """A pass that read the requests before one arrived would otherwise rearm the hour."""
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: retire(daemon, job_id) and dict(outcome))
    with caplog.at_level("WARNING"):
        daemon._retention()
    assert daemon._retire_asked == 1
    assert 0 < due_in(daemon) <= daemon_module.RETENTION_CATCH_UP_S, due_in(daemon)
    assert daemon.timers.status()["retention"].get("last_error_type") == error
    if error in ("TimeoutError", "ScanFailed"):
        assert "for a retire request" in caplog.text


@REARMING
def test_a_pass_with_no_request_after_it_began_waits_the_hour(state_daemon, monkeypatch, outcome,  # noqa: F811
                                                              error):
    daemon, harness = state_daemon
    retire(daemon, finished(daemon, harness))           # before the pass: the pass reads it
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: dict(outcome))
    daemon._retention()
    assert due_in(daemon) > daemon_module.RETENTION_INTERVAL_S - 60
    assert daemon.timers.status()["retention"].get("last_error_type") == error


def test_a_dormant_pass_waits_the_hour_whatever_arrives(state_daemon, monkeypatch):  # noqa: F811
    """A dormant pass retires nothing, so a request during it has nothing to catch up on."""
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    monkeypatch.setenv(daemon_module.RETENTION_DORMANT_ENV, "1")
    monkeypatch.setattr(daemon, "_prune_service_notices", lambda: retire(daemon, job_id))
    daemon._retention()
    assert daemon._retire_asked == 1
    assert due_in(daemon) > daemon_module.RETENTION_INTERVAL_S - 60


@REARMING
def test_a_pass_whose_mark_fails_stays_due(state_daemon, monkeypatch, outcome, error):  # noqa: F811
    """C-8.4: `_last_maintenance` is set last, so a failed status mark leaves retention due."""
    daemon, _ = state_daemon
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: dict(outcome))

    def mark(name, **fields):
        raise OSError("disk full")
    monkeypatch.setattr(daemon.timers, "mark", mark)
    daemon._last_maintenance = time.monotonic() - 2 * daemon_module.RETENTION_INTERVAL_S
    with pytest.raises(OSError):
        daemon._retention()
    assert due_in(daemon) <= 0


def test_a_request_while_a_pass_marks_its_end_is_not_lost(state_daemon, monkeypatch):  # noqa: F811
    """The lock spans the mark: a request arriving then sets the clock after the pass does."""
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: {"pruned": []})
    real_mark, asking = daemon.timers.mark, []

    def mark(name, **fields):
        real_mark(name, **fields)
        thread = threading.Thread(target=retire, args=(daemon, job_id))
        thread.start()
        thread.join(0.3)                # it waits for the lock this pass holds
        asking.append(thread)
    monkeypatch.setattr(daemon.timers, "mark", mark)
    daemon._retention()
    (thread,) = asking
    thread.join(10)
    assert not thread.is_alive() and daemon._retire_asked == 1
    assert due_in(daemon) <= 0


def test_a_request_whose_answer_fails_to_read_still_makes_retention_due(state_daemon, monkeypatch):  # noqa: F811
    """The answer's reads run after the commit: one that fails loses the reply, never the wake."""
    daemon, harness = state_daemon
    job_id = finished(daemon, harness)
    daemon._last_maintenance = time.monotonic()

    def one(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    with monkeypatch.context() as patched:
        patched.setattr(daemon.store, "one", one)
        with pytest.raises(sqlite3.OperationalError):
            retire(daemon, job_id)
    assert [kind for kind, _ in recorded(daemon, job_id)] == [retention.RETIRE_REQUESTED]
    assert daemon._retire_asked == 1 and due_in(daemon) <= 0, due_in(daemon)


def test_the_next_pass_retires_a_requested_job_and_keeps_the_rest(state_daemon, monkeypatch, caplog):  # noqa: F811
    daemon, harness = state_daemon
    asked, other = finished(daemon, harness), finished(daemon, harness)
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    real = daemon_module.maintenance
    monkeypatch.setattr(daemon_module, "maintenance",
                        lambda *args, **kwargs: real(*args, **{**kwargs, "holders": lambda watches, **_: {}}))
    retire(daemon, asked)
    with caplog.at_level("INFO"):
        daemon._retention()
    assert daemon.store.get_job(asked) is None and daemon.store.get_job(other) is not None
    assert daemon.store.query("SELECT 1 FROM events WHERE job_id=? AND kind='retention.pruned'", (asked,))
    assert "retired 1 jobs, 1 on request" in caplog.text
    assert retire(daemon, asked)["jobs"][0]["outcome"] == "retired"


# --- the CLI ------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch, tmp_path):
    """`call_settled` recorded, answering with `reply` (or raising it)."""
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    calls: list[tuple[str, dict]] = []
    reply: dict = {}

    def call_settled(self, op, args=None, **_):
        calls.append((op, args))
        if isinstance(reply.get("raise"), Exception):
            raise reply["raise"]
        return reply["result"]
    monkeypatch.setattr(client_module.Client, "call_settled", call_settled)
    return calls, reply


def test_the_cli_sends_each_job_once_with_its_flags(sent, capsys, monkeypatch):
    calls, reply = sent
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("SUBFLEET_SESSION_ID", "s-cli")
    reply["result"] = {"jobs": [{"job_id": "a", "outcome": "requested", "state": "succeeded"},
                                {"job_id": "b", "outcome": "already-requested", "requested_at": "T1",
                                 "kept": "unread-notice"}],
                       "recorded": 1, "withdraw": False, "dry_run": False, "retention": {"dormant": False}}
    assert cli.main(["retention", "retire", "a", "b", "a", "--reason", "Track B"]) == Exit.OK
    assert calls == [("retire", {"job_ids": ["a", "b"], "withdraw": False, "dry_run": False,
                                 "reason": "Track B", "session_id": "s-cli"})]
    shown = capsys.readouterr()
    assert shown.out.splitlines() == ["a  requested", "b  already requested at T1; kept while pinned: unread-notice"]
    assert "next pass, which starts now" in shown.err


def test_the_cli_exits_refused_when_a_job_could_not_be_taken(sent, capsys):
    calls, reply = sent
    reply["result"] = {"jobs": [{"job_id": "r", "outcome": "not-finished", "state": "running"}],
                       "recorded": 0, "withdraw": False, "dry_run": False, "retention": {"dormant": True}}
    assert cli.main(["retention", "retire", "r", "--json"]) == Exit.REFUSED
    shown = capsys.readouterr()
    assert json.loads(shown.out)["jobs"][0]["outcome"] == "not-finished"


def test_the_cli_says_a_daemon_too_old_for_requests(sent, capsys):
    _, reply = sent
    reply["raise"] = DaemonError(int(Exit.INVALID_INPUT), f"{protocol.UNKNOWN_OP}: op must name a supported operation")
    assert cli.main(["retention", "retire", "a"]) == Exit.DAEMON_UNAVAILABLE
    err = capsys.readouterr().err
    assert "predates retire requests" in err and cli.RESTART_DAEMON in err


def test_the_cli_counts_a_retired_job_as_settled_and_a_dry_run_as_unrecorded(sent, capsys):
    _, reply = sent
    reply["result"] = {"jobs": [{"job_id": "g", "outcome": "retired", "retired_at": "T2", "archive": "/a/g"},
                                {"job_id": "h", "outcome": "requested", "state": "failed",
                                 "deferred": {"reason": "holder: pid 9", "for_s": 30}}],
                       "recorded": 0, "withdraw": False, "dry_run": True, "retention": {"dormant": False}}
    assert cli.main(["retention", "retire", "g", "h", "--dry-run"]) == Exit.OK
    shown = capsys.readouterr()
    assert shown.out.splitlines() == ["g  retired at T2 (archive /a/g)",
                                      "h  requested; deferred 30 s more: holder: pid 9"]
    assert "dry run: nothing was recorded" in shown.err and "starts now" not in shown.err


def test_the_cli_says_an_unanswered_request_is_safe_to_send_again(sent, capsys):
    _, reply = sent
    reply["raise"] = client_module.OutcomeUnknown("retire", "r-1", ["timed out", "connection reset"])
    assert cli.main(["retention", "retire", "a", "--json"]) == Exit.OPERATIONAL
    shown = capsys.readouterr()
    assert json.loads(shown.out)["outcome"] == "unknown"
    assert "timed out; then connection reset" in shown.err and "records nothing twice" in shown.err


@pytest.mark.parametrize("as_json", [False, True])
def test_the_cli_after_a_lost_answer_and_a_gone_daemon_says_the_outcome_is_unknown(monkeypatch, tmp_path,
                                                                                    capsys, as_json):
    """The first request may have been recorded, so this is not 69, which says nothing was."""
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    tries: list[str] = []

    def call(self, op, args=None, **_):
        tries.append(op)
        if len(tries) == 1:
            raise client_module.ResponseLost("connection reset after the request was sent")
        raise DaemonUnavailable("no daemon is listening")
    monkeypatch.setattr(client_module.Client, "call", call)
    code = cli.main(["retention", "retire", "a", *(["--json"] if as_json else [])])
    shown = capsys.readouterr()
    assert tries == ["retire", "retire"] and code == Exit.OPERATIONAL, shown.err
    assert "the daemon went away before the re-sent request" in shown.err and "records nothing twice" in shown.err
    if as_json:
        assert json.loads(shown.out)["outcome"] == "unknown"


def test_the_cli_records_nothing_without_a_daemon(sent, capsys):
    _, reply = sent
    reply["raise"] = DaemonUnavailable("no daemon is listening")
    assert cli.main(["retention", "retire", "a", "--withdraw"]) == Exit.DAEMON_UNAVAILABLE
    assert "only the daemon records a request" in capsys.readouterr().err
