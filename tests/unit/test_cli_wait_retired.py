"""C-15.9 (D-WAIT-PRUNED1): `subfleet wait --mine` skips the jobs retention retires under it.

The daemon names a resolved job retention retired during a poll in the answer's
`retired` (`tests/fake/test_wait_retired.py`). A daemon older than that (2.1.12,
live when the hub hit this) answers `unknown job <id>` instead. The CLI skips
either, once, when the call resolved its jobs through `--mine` or `--last` and
did not name the job, and says once, as the wait ends, how many it skipped. A
job the caller named is the caller's word: unknown is still exit 2.

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import types

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import cli, protocol
from subfleet.client import DaemonError
from subfleet.contracts import Exit

OLD = "20261006-004417-pr127-review-r6"
LIVE = "20261102-115300-live-review"


def run_cli(argv: list[str]) -> int:
    return cli.main(argv)


def ended(job_id: str, state: str = "succeeded", rc: int | None = 0) -> dict:
    return {"job_id": job_id, "state": state, "rc": rc}


def skipped_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if "retired job" in line]


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-hub")


def test_c15_9_wait_mine_says_once_which_retired_jobs_the_daemon_skipped(daemon, session, capsys):
    """C-15.9, C-15.4 the answer's `retired` becomes one line; the live job is reported as ever."""
    server = daemon({"wait": lambda request: {"jobs": [ended(LIVE)], "timeout": False, "retired": [OLD]}})
    assert run_cli(["wait", "--mine"]) == 0
    err = capsys.readouterr().err
    assert skipped_lines(err) == [
        f"subfleet wait: skipped 1 retired job, which retention removed from the store while this wait ran: {OLD}"]
    assert f"subfleet wait: {LIVE} SUCCEEDED" in err
    assert server.args("wait")["mine"] == "sess-hub"


def test_c15_9_wait_mine_rides_out_an_older_daemons_unknown_job(daemon, session, capsys):
    """C-15.9 the live 2.1.12 daemon answers `unknown job <id>` for a job its own `--mine`
    listed; the CLI skips that job, asks again, and reports the live one."""
    answers = iter([lambda request: protocol.fail(request.id, Exit.INVALID_INPUT, f"unknown job {OLD}"),
                    lambda request: {"jobs": [ended(LIVE)], "timeout": False}])
    server = daemon({"wait": lambda request: next(answers)(request)})
    assert run_cli(["wait", "--mine"]) == 0
    err = capsys.readouterr().err
    assert len(skipped_lines(err)) == 1 and skipped_lines(err)[0].endswith(OLD)
    assert f"subfleet wait: {LIVE} SUCCEEDED" in err
    assert "unknown job" not in err
    assert [request.args["job_ids"] for request in server.requests if request.op == "wait"] == [[], []]


def test_c15_9_wait_last_rides_out_an_older_daemons_unknown_job(daemon, capsys):
    """C-15.9 `--last` resolves through `list` as `--mine` does."""
    answers = iter([lambda request: protocol.fail(request.id, Exit.INVALID_INPUT, f"unknown job {OLD}"),
                    lambda request: {"jobs": [], "timeout": False}])
    daemon({"wait": lambda request: next(answers)(request)})
    assert run_cli(["wait", "--last"]) == 0
    err = capsys.readouterr().err
    assert len(skipped_lines(err)) == 1
    assert err.splitlines()[-1] == "subfleet wait: nothing to wait for"


@pytest.mark.parametrize("argv", [["wait", OLD], ["wait", OLD, "--mine"], ["wait", OLD, "--last"]],
                         ids=["alone", "beside-mine", "beside-last"])
def test_c15_9_a_named_unknown_job_still_exits_2(daemon, session, capsys, argv):
    """C-15.9, C-17.3 a job the caller named is never skipped, whatever resolver rides beside it."""
    daemon({"wait": lambda request: protocol.fail(request.id, Exit.INVALID_INPUT, f"unknown job {OLD}")})
    assert run_cli(argv) == 2
    err = capsys.readouterr().err
    assert f"subfleet: unknown job {OLD}" in err
    assert not skipped_lines(err)


def refusing(times: int, code: int, message: str):
    """A `wait` handler that refuses `times` polls with `message`, then answers the live job:
    a wait that wrongly skips the refusal ends 0 instead of failing with it."""
    polls = []

    def handler(request):
        polls.append(request)
        if len(polls) <= times:
            return protocol.fail(request.id, code, message)
        return {"jobs": [ended(LIVE)], "timeout": False}
    return handler, polls


def test_c15_9_the_same_unknown_job_twice_is_not_a_retirement(daemon, session, capsys):
    """C-15.9 a retired job leaves the next `list`; a daemon that names the same job unknown
    again is reporting something else, so the wait ends with it (exit 2), never spins."""
    handler, polls = refusing(5, Exit.INVALID_INPUT, f"unknown job {OLD}")
    daemon({"wait": handler})
    assert run_cli(["wait", "--mine"]) == 2
    assert f"unknown job {OLD}" in capsys.readouterr().err
    assert len(polls) == 2


@pytest.mark.parametrize("message", ["unknown job", "unknown job two words", f"unknown job {OLD} "])
def test_c15_9_an_unknown_job_naming_no_one_job_is_not_guessed(daemon, session, capsys, message):
    """C-15.9 only `unknown job <one id>` names a job; anything else is the daemon's error."""
    handler, polls = refusing(1, Exit.INVALID_INPUT, message)
    daemon({"wait": handler})
    assert run_cli(["wait", "--mine"]) == 2
    assert not skipped_lines(capsys.readouterr().err)
    assert len(polls) == 1


def test_c15_9_an_unknown_job_under_another_exit_code_is_not_a_retirement(daemon, session, capsys):
    """C-15.9, C-17.3 an unknown job is invalid input (2); the same words under another code
    are not the refusal the daemon gives for a missing row."""
    handler, polls = refusing(1, Exit.OPERATIONAL, f"unknown job {OLD}")
    daemon({"wait": handler})
    assert run_cli(["wait", "--mine"]) == 1
    assert not skipped_lines(capsys.readouterr().err)
    assert len(polls) == 1


def test_c15_9_a_named_wait_never_skips_even_a_job_it_did_not_name(daemon, capsys):
    """C-15.9, C-17.3 without `--mine` or `--last` nothing was resolved, so nothing is skipped:
    an unknown job is the daemon's error even when it names a job the caller did not."""
    handler, polls = refusing(1, Exit.INVALID_INPUT, f"unknown job {OLD}")
    daemon({"wait": handler})
    assert run_cli(["wait", LIVE]) == 2
    assert f"unknown job {OLD}" in capsys.readouterr().err
    assert len(polls) == 1


def test_c15_9_a_timeout_still_says_once_what_it_skipped(daemon, session, capsys):
    """C-15.9, C-17.3 every poll may name the same retired job; the wait says so once, and
    the timeout is still exit 124."""
    daemon({"wait": lambda request: {"timeout": True, "retired": [OLD]}})
    assert run_cli(["wait", "--mine", "--timeout", "1"]) == 124
    err = capsys.readouterr().err
    assert len(skipped_lines(err)) == 1
    assert "nothing reached a terminal state" in err


def test_c15_9_every_job_retired_is_nothing_to_wait_for(daemon, session, capsys):
    """C-15.9 a session whose only listed job retention retired has nothing to wait for (0)."""
    daemon({"wait": lambda request: {"jobs": [], "timeout": False, "retired": [OLD]}})
    assert run_cli(["wait", "--mine"]) == 0
    err = capsys.readouterr().err.splitlines()
    assert len(skipped_lines("\n".join(err))) == 1
    assert err[-1] == "subfleet wait: nothing to wait for"


def test_c15_9_the_line_names_five_and_counts_the_rest(daemon, session, capsys):
    """C-15.9 the one line stays one line however many jobs were skipped."""
    many = [f"20261006-0000{k:02d}-old-{k}" for k in range(7)]
    daemon({"wait": lambda request: {"jobs": [ended(LIVE)], "timeout": False, "retired": many}})
    assert run_cli(["wait", "--mine"]) == 0
    (line,) = skipped_lines(capsys.readouterr().err)
    assert line.startswith("subfleet wait: skipped 7 retired jobs, which retention removed")
    assert line.endswith(", ".join(many[:cli.RETIRED_SHOWN]) + " and 2 more")


def test_c15_9_json_keeps_stdout_to_the_jobs(daemon, session, capsys):
    """C-17.4, C-15.9 with `--json` stdout carries one object per job and no prose; the
    skipped line is human progress, on stderr."""
    daemon({"wait": lambda request: {"jobs": [ended(LIVE)], "timeout": False, "retired": [OLD]}})
    assert run_cli(["wait", "--mine", "--json"]) == 0
    captured = capsys.readouterr()
    assert [json.loads(line)["job_id"] for line in captured.out.splitlines()] == [LIVE]
    assert len(skipped_lines(captured.err)) == 1


def test_c15_9_a_named_wait_ignores_a_retired_field(daemon, capsys):
    """C-15.9, C-17.3 only a resolver's wait skips: a named wait never reports skipping."""
    daemon({"wait": lambda request: {"jobs": [ended(LIVE)], "timeout": False, "retired": [OLD]}})
    assert run_cli(["wait", LIVE]) == 0
    assert not skipped_lines(capsys.readouterr().err)


@pytest.mark.parametrize("retired", [None, "x", [None, 7, ""], {"a": 1}])
def test_c15_9_a_malformed_retired_field_skips_nothing(daemon, session, capsys, retired):
    """C-16.2 a field the CLI cannot read is ignored, never trusted."""
    daemon({"wait": lambda request: {"jobs": [ended(LIVE)], "timeout": False, "retired": retired}})
    assert run_cli(["wait", "--mine"]) == 0
    assert not skipped_lines(capsys.readouterr().err)


# --- the property ----------------------------------------------------------------

class Ledger:
    """One session's jobs as a daemon answers `wait --mine`, polled by the CLI.

    `plan` is the batches retention retires, one batch under each poll in turn. A
    fixed daemon names a poll's batch in `retired` (a timeout until the last batch,
    then its answer); an older one answers `unknown job` for the batch's first id
    in its read order, and the rest of the batch simply leaves the next `list`."""

    def __init__(self, live: list[dict], plan: list[list[str]], *, fixed: bool):
        self.rows = {job["job_id"]: job for job in live}
        self.rows.update({job_id: ended(job_id) for batch in plan for job_id in batch})
        self.plan = [list(batch) for batch in plan]
        self.fixed = fixed
        self.polls = 0

    def call(self, op, args, timeout=None, retry_busy=True):
        assert op == "wait" and self.polls < 50, "the CLI kept polling"
        self.polls += 1
        named = args.get("job_ids") or []
        if named:
            missing = [job_id for job_id in named if job_id not in self.rows]
            if missing:
                raise DaemonError(int(Exit.INVALID_INPUT), f"unknown job {missing[0]}")
            return {"jobs": [self.rows[job_id] for job_id in named], "timeout": False}
        if not self.plan:
            return {"jobs": list(self.rows.values()), "timeout": False}
        batch = self.plan.pop(0)
        for job_id in batch:
            del self.rows[job_id]
        if not self.fixed:
            raise DaemonError(int(Exit.INVALID_INPUT), f"unknown job {sorted(batch)[0]}")
        if self.plan:
            return {"timeout": True, "retired": sorted(batch)}
        return {"jobs": list(self.rows.values()), "timeout": False, "retired": sorted(batch)}


LIVE_JOBS = st.lists(st.sampled_from([("succeeded", 0), ("failed", 1), ("failed", 3), ("cancelled", None),
                                      ("lost", None)]), max_size=5)
PLANS = st.lists(st.integers(min_value=1, max_value=3), max_size=4)
EXAMPLES = iter(range(10**9))


@pytest.fixture
def ledger_cli(monkeypatch, session):
    """`cli.main` against a `Ledger` (no socket), with the CLI's backoff pauses skipped."""
    holder: dict = {}
    monkeypatch.setattr(cli, "_client", lambda args, timeout=None: holder["ledger"])
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=cli.time.monotonic, time=cli.time.time,
                                                           sleep=lambda seconds: None))
    return holder


@given(live=LIVE_JOBS, plan=PLANS, fixed=st.booleans())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_c15_9_property_wait_mine_waits_exactly_on_the_live_jobs(ledger_cli, capsys, live, plan, fixed):
    """C-15.9 for any mix of live jobs and jobs retention retires under the polls, from a fixed
    daemon or an older one: `wait --mine` exits as its live jobs do, reports exactly them, and
    says once exactly which jobs the daemon told it were retired."""
    n = next(EXAMPLES)
    jobs = [ended(f"20261102-120000-p{n}-live{k}", state, rc) for k, (state, rc) in enumerate(live)]
    batches = [[f"20261006-000000-p{n}-old{b}-{k}" for k in range(size)] for b, size in enumerate(plan)]
    ledger_cli["ledger"] = Ledger(jobs, batches, fixed=fixed)
    capsys.readouterr()
    code = run_cli(["wait", "--mine"])
    err = capsys.readouterr().err
    assert code == max((cli.exit_for_job(job, quiet=True) for job in jobs), default=0)
    reported = {line.split()[2] for line in err.splitlines() if line.startswith("subfleet wait: 2026")}
    assert reported == {job["job_id"] for job in jobs}
    told = sorted(job_id for batch in batches for job_id in batch) if fixed \
        else sorted(sorted(batch)[0] for batch in batches)
    lines = skipped_lines(err)
    if not told:
        assert lines == []
        return
    assert len(lines) == 1
    shown = ", ".join(told[:cli.RETIRED_SHOWN])
    more = f" and {len(told) - cli.RETIRED_SHOWN} more" if len(told) > cli.RETIRED_SHOWN else ""
    plural = "" if len(told) == 1 else "s"
    assert lines[0] == (f"subfleet wait: skipped {len(told)} retired job{plural}, which retention removed "
                        f"from the store while this wait ran: {shown}{more}")


@given(live=LIVE_JOBS.filter(bool), plan=PLANS.filter(bool), fixed=st.booleans(), data=st.data())
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_c15_9_property_a_named_retired_job_exits_2(ledger_cli, capsys, live, plan, fixed, data):
    """C-15.9, C-17.3 naming a job retention retired is exit 2 with its id, with or without
    `--mine`, from either daemon; the CLI never skips a job its caller named."""
    n = next(EXAMPLES)
    jobs = [ended(f"20261102-120000-q{n}-live{k}", state, rc) for k, (state, rc) in enumerate(live)]
    ledger = Ledger(jobs, [], fixed=fixed)
    gone = [f"20261006-000000-q{n}-old{k}" for k in range(sum(plan))]
    ledger_cli["ledger"] = ledger
    named = data.draw(st.lists(st.sampled_from(gone + [job["job_id"] for job in jobs]), min_size=1, unique=True)
                      .filter(lambda ids: any(job_id in gone for job_id in ids)))
    argv = ["wait", *named] + (["--mine"] if data.draw(st.booleans()) else [])
    capsys.readouterr()
    assert run_cli(argv) == 2
    err = capsys.readouterr().err
    assert any(f"subfleet: unknown job {job_id}" in err for job_id in named if job_id in gone)
    assert not skipped_lines(err)
