"""C-25.6: reading the caller's process chain for a person-only op.

The chain is read with `ps`, which a loaded machine can slow or fail to start.
A chain that cannot be read in time decides nothing in the caller's favour. A
caller that is not the app is refused (exit 7) as it would be anyway; the app's
own request fails closed with exit 1 `person-check-failed`, naming the cause,
and asking again is safe. These tests drive `process_chain` through a scripted
`ps` on a fake clock (every schedule of answers, hangs and failed starts),
through a real `ps` wrapped to hang, and through the service's answer.
"""

from __future__ import annotations

import errno
import functools
import json
import logging
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import protocol
from subfleet.conversations import peers
from subfleet.conversations.peers import (APP_EXECUTABLES, AuditToken, Proc, PsReader, Verdict,
                                          allowed_executable_paths, judge, process_chain)
from subfleet.conversations.store import ConversationError

APP = "/Applications/Subfleet.app/Contents/MacOS/Subfleet"
SHELL = "/bin/zsh"
TABLE = ["-axo", "pid=,ppid=,tty="]
NOT_THE_APP = Verdict(False, "this needs the Subfleet app; if an update moved its running bundle, restart the app", 100)


def token(pid: int) -> AuditToken:
    """A synthetic connection-time identity (no socket to re-read)."""
    return AuditToken(struct.pack("=8I", 0, 0, 0, 0, 0, pid, 0, 1))


def image(path: str):
    return lambda peer: path


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.slept.append(seconds)
        self.now += seconds


class ScriptedPs:
    """`ps` answering from `procs`, one scripted outcome per call in order (then
    "ok"): a float is how long the call takes (it times out at its timeout),
    "eagain" fails to start, "empty" answers nothing (exit 1). `missing` leaves
    those pids out of the table, as a parent gone between two reads would be."""

    def __init__(self, procs: list[Proc], clock: Clock, outcomes=(), answer_s: float = 0.01, missing=()):
        self.procs = {p.pid: p for p in procs}
        self.missing = set(missing)
        self.clock = clock
        self.outcomes = list(outcomes)
        self.answer_s = answer_s
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, capture_output, text, errors, timeout, env, check):
        assert argv[0] == peers.PS and capture_output and text and errors == "replace" and not check
        assert env == {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"}
        assert 0 < timeout <= peers.PS_TIMEOUT_S
        self.calls.append((argv[1:], timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        table = argv[1:] == TABLE
        if outcome == "eagain":
            raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        took = outcome if isinstance(outcome, float) else self.answer_s
        if took >= timeout:
            self.clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        self.clock.now += took
        if outcome == "empty":
            return subprocess.CompletedProcess(argv, 1, "", "")
        if table:
            rows = [f"{p.pid:>5} {p.ppid:>5} {p.tty}" for p in self.procs.values() if p.pid not in self.missing]
            rows.append("77777 1 ??")
            return subprocess.CompletedProcess(argv, 0, "\n".join(rows) + "\n", "")
        pid = int(argv[2])
        if pid not in self.procs:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, self.procs[pid].command + "\n", "")


def chain_procs(*links: tuple[str, str]) -> list[Proc]:
    """The caller first, each link (tty, command) the parent of the one before, ending at launchd."""
    procs = [Proc(100 + i, 100 + i + 1, tty, command) for i, (tty, command) in enumerate(links)]
    procs[-1] = Proc(procs[-1].pid, 1, procs[-1].tty, procs[-1].command)
    return procs + [Proc(1, 0, "??", "/sbin/launchd")]


def reading(procs, clock, outcomes=(), budget_s=None, missing=()):
    ps = ScriptedPs(procs, clock, outcomes, missing=missing)
    reader = PsReader(budget_s, run=ps, clock=clock, sleep=clock.sleep)
    return ps, (lambda pid: process_chain(pid, read=reader))


# --- the reader ------------------------------------------------------------------


def test_a_ps_that_never_answers_fails_closed_naming_the_cause():
    """C-25.6: two tries at the table, each cut at 5 s, then a verdict that says
    why; nothing is decided in the caller's favour and no exception escapes."""
    clock = Clock()
    procs = chain_procs(("??", APP))
    ps, chain = reading(procs, clock, outcomes=[9.0, 9.0])
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict == Verdict(False, "the caller's process chain could not be read: `ps -axo pid=,ppid=,tty=` "
                                     "did not answer within 5 s, then did not answer within 4.9 s", 100,
                              unreadable=True)
    assert [args for args, _ in ps.calls] == [TABLE, TABLE]
    assert clock.now - 1000.0 == pytest.approx(peers.CHAIN_BUDGET_S)
    # With room for two whole tries, the cause says so once.
    _, chain = reading(procs, Clock(), outcomes=[9.0, 9.0], budget_s=30.0)
    assert judge(token(100), chain=chain, executable=image(APP)).reason.endswith(
        "`ps -axo pid=,ppid=,tty=` did not answer within 5 s, twice")


def test_a_caller_that_is_not_the_app_is_refused_whatever_its_unread_chain_holds():
    """C-25.6 with D-PB1: only the app can pass, and the vetoes only refuse, so a
    terminal or an agent whose chain cannot be read is refused (exit 7), never told
    to try again; an image the kernel cannot verify is refused as it always was."""
    procs = chain_procs(("ttys001", SHELL), ("ttys001", "login"))
    for path, expected in ((SHELL, NOT_THE_APP), ("/usr/bin/python3", NOT_THE_APP)):
        _, chain = reading(procs, Clock(), outcomes=[9.0, 9.0])
        verdict = judge(token(100), chain=chain, executable=image(path))
        assert verdict == expected and not verdict.unreadable
    _, chain = reading(procs, Clock(), outcomes=[9.0, 9.0])
    unverified = judge(token(100), chain=chain, executable=lambda peer: None)
    assert not unverified.person and not unverified.unreadable
    assert unverified.reason.startswith("the caller's connection-time image could not be verified")


def test_one_slow_ps_is_tried_again_and_the_verdict_is_the_whole_chains():
    """C-25.6: a single hang is absorbed by the second try."""
    clock = Clock()
    procs = chain_procs(("??", APP), ("??", "/sbin/launchd-child"))
    ps, chain = reading(procs, clock, outcomes=[9.0])
    assert judge(token(100), chain=chain, executable=image(APP)) == Verdict(True, "the Subfleet app", 100)
    assert [args for args, _ in ps.calls][:2] == [TABLE, TABLE]


def test_an_ancestor_that_was_not_read_is_never_taken_to_lack_the_markers():
    """C-25.6: the table reads, the caller reads, but the marked ancestor's `ps -E`
    hangs twice. Taking the unread ancestor as "no markers" would accept this
    caller, whose image is the app's; the check fails closed instead."""
    marked = ("??", "node claude SUBFLEET_ATTEMPT=j/a1 SUBFLEET_JOB=j")
    procs = chain_procs(("??", APP), marked, ("??", "login"))
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=["ok", "ok", 9.0, 9.0], budget_s=30.0)
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert not verdict.person and verdict.unreadable
    assert "`ps -Ewwp 101 -o command=` did not answer within 5 s, twice" in verdict.reason
    # The same chain with that command read as empty (what swallowing the timeout
    # would give) is accepted, which is why a timeout must never read as "".
    blank = [Proc(p.pid, p.ppid, p.tty, "" if p.pid == 101 else p.command) for p in procs]
    assert judge(token(100), chain=lambda pid: blank, executable=image(APP)).person
    assert not judge(token(100), chain=lambda pid: procs, executable=image(APP)).person


def test_a_ps_that_cannot_start_is_tried_again_after_a_pause():
    """C-25.6: `fork` failing (EAGAIN) on a loaded machine is tried once more."""
    procs = chain_procs(("??", APP))
    clock = Clock()
    ps, chain = reading(procs, clock, outcomes=["eagain"])
    assert judge(token(100), chain=chain, executable=image(APP)) == Verdict(True, "the Subfleet app", 100)
    assert clock.slept == [peers.RETRY_PAUSE_S]
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=["eagain", "eagain"])
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict.unreadable and not verdict.person
    assert verdict.reason.endswith("`ps -axo pid=,ppid=,tty=` could not start (Resource temporarily unavailable), twice")


def test_an_empty_process_table_is_a_failed_try():
    """The table is never empty: `ps` listing nothing is a failure, tried again."""
    procs = chain_procs(("??", APP))
    _, chain = reading(procs, Clock(), outcomes=["empty"])
    assert judge(token(100), chain=chain, executable=image(APP)).person
    _, chain = reading(procs, Clock(), outcomes=["empty", 9.0])
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict.unreadable
    assert verdict.reason.endswith("answered nothing (exit 1), then did not answer within 5 s")


def test_an_ancestor_whose_command_comes_back_empty_is_not_read():
    """C-25.6: a live process always has a command line, so an empty answer is a
    failed try, not "no markers"; twice, the chain is unreadable."""
    procs = chain_procs(("??", APP), ("??", "node claude SUBFLEET_ATTEMPT=j/a1"), ("??", "login"))
    _, chain = reading(procs, Clock(), outcomes=["ok", "ok", "empty"])
    assert judge(token(100), chain=chain, executable=image(APP)) == judge(
        token(100), chain=lambda pid: procs, executable=image(APP))           # one empty answer absorbed
    _, chain = reading(procs, Clock(), outcomes=["ok", "ok", "empty", "empty"])
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict.unreadable and not verdict.person
    assert verdict.reason.endswith("`ps -Ewwp 101 -o command=` answered nothing (exit 1), twice")


@pytest.mark.parametrize("shape", ["missing parent", "loop", "too long"])
def test_a_walk_that_does_not_reach_launchd_is_unreadable(shape):
    """C-25.6: a chain judged on part of its ancestry could miss a guardian above
    the break, so a walk that stops short of launchd decides nothing for the app,
    and a caller that is not the app is refused as it would be anyway."""
    if shape == "missing parent":
        procs, missing, cause = (chain_procs(("??", APP), ("??", "node"), ("??", "login")), {101},
                                 "pid 101, the parent of pid 100, is not in the process table")
    elif shape == "loop":
        procs = [Proc(100, 101, "??", APP), Proc(101, 102, "??", "node"), Proc(102, 101, "??", "sh"),
                 Proc(1, 0, "??", "/sbin/launchd")]
        missing, cause = (), "the process table loops at pid 101"
    else:
        procs = chain_procs(("??", APP), *[("??", f"/bin/sh -c step{i}") for i in range(peers.MAX_CHAIN)])
        missing, cause = (), f"the chain is longer than {peers.MAX_CHAIN} processes"
    _, chain = reading(procs, Clock(), missing=missing, budget_s=1e9)
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict == Verdict(False, f"the caller's process chain could not be read: {cause}", 100, unreadable=True)
    _, chain = reading(procs, Clock(), missing=missing, budget_s=1e9)
    assert judge(token(100), chain=chain, executable=image(SHELL)) == NOT_THE_APP
    # A caller missing from the table has no chain at all: refused, as before.
    _, chain = reading(procs, Clock(), missing={100})
    assert judge(token(100), chain=chain, executable=image(APP)) == Verdict(
        False, "the caller's process could not be inspected", 100)


def test_the_whole_chain_is_read_within_its_budget_however_long():
    """C-25.6: 64 ancestors each answering in 0.9 s (under the per-call 5 s)
    cannot hold the check past its 10 s: the read stops, closed, at the budget."""
    procs = chain_procs(("??", APP), *[("??", f"/bin/sh -c step{i}") for i in range(peers.MAX_CHAIN - 1)])
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=[0.9] * 200)
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert verdict.unreadable and not verdict.person
    assert clock.now - 1000.0 <= peers.CHAIN_BUDGET_S + 1e-9
    assert "ran out" in verdict.reason


def test_a_second_try_is_cut_to_what_is_left_of_the_budget():
    clock = Clock()
    procs = chain_procs(("??", APP))
    ps, chain = reading(procs, clock, outcomes=[9.0, 9.0], budget_s=7.0)
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert [round(timeout, 6) for _, timeout in ps.calls] == [5.0, round(7.0 - 5.0 - peers.RETRY_PAUSE_S, 6)]
    assert verdict.reason.endswith("did not answer within 5 s, then did not answer within 1.9 s")
    assert clock.now - 1000.0 == pytest.approx(7.0)
    clock = Clock()
    ps, chain = reading(procs, clock, outcomes=[9.0], budget_s=5.0)
    verdict = judge(token(100), chain=chain, executable=image(APP))
    assert len(ps.calls) == 1
    assert verdict.reason.endswith("did not answer within 5 s, and the check's 5 s ran out before a second try")


def test_a_chain_that_was_read_is_unchanged():
    """Only the reading changed: an injected chain and the empty chain decide as before."""
    assert not judge(token(10), chain=lambda pid: [], executable=image(APP)).person
    assert not judge(token(10), chain=lambda pid: [], executable=image(APP)).unreadable
    assert not judge(None).person and not judge(None).unreadable


# --- every schedule --------------------------------------------------------------

LINKS = st.tuples(st.sampled_from(["??", "ttys001"]),
                  st.sampled_from(["/bin/zsh", "node /x/claude", "login", "/v/python -m subfleet.guardian --attempt-dir x",
                                   "bash SUBFLEET_ATTEMPT=j/a1 PATH=/bin", "sh SUBFLEET_JOB=j", "zsh SUBFLEET_ROOT=/r/root"]))
OUTCOME = st.one_of(st.just("ok"), st.just("eagain"), st.just("empty"),
                    st.floats(min_value=0.0, max_value=6.0, allow_nan=False))


def reference(procs: list[Proc], outcomes: list) -> bool:
    """Whether the chain can be read with no deadline: every read gets two tries,
    and a try fails when it hangs past 5 s, cannot start, or answers nothing.
    Independent of `PsReader`: it walks the reads the chain needs."""
    pending = list(outcomes)
    reads = [True] + [False] * len(procs)               # the table, then each process's command
    for table in reads:
        for _try in range(2):
            outcome = pending.pop(0) if pending else "ok"
            failed = (outcome == "eagain" or outcome == "empty"
                      or (isinstance(outcome, float) and outcome >= peers.PS_TIMEOUT_S))
            if not failed:
                break
        else:
            return False
    return True


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(links=st.lists(LINKS, min_size=1, max_size=8), outcomes=st.lists(OUTCOME, max_size=30),
       executable=st.sampled_from([APP, SHELL]),
       budget=st.one_of(st.just(10.0), st.floats(min_value=0.0, max_value=40.0, allow_nan=False)),
       gone=st.one_of(st.none(), st.integers(min_value=1, max_value=8)))
def test_no_schedule_of_ps_failures_makes_the_check_more_permissive(links, outcomes, executable, budget, gone):
    """C-25.6 invariants over every chain, caller image and schedule of `ps` answers:
    (1) a caller accepted is accepted with the fully read chain's verdict, so a
    failure never accepts a caller the whole chain would refuse; (2) only the app
    is ever told its chain was unreadable; (3) otherwise the verdict is the whole
    chain's, or, for a caller that is not the app, the plain "needs the app"
    refusal; (4) the check ends within its budget; (5) at most two tries per read,
    each cut to at most 5 s and to what is left of the budget. `gone` takes an
    ancestor out of the table, as a parent that exited between reads."""
    procs = chain_procs(*links)
    whole = judge(token(100), chain=lambda pid: procs, root="/r/root", executable=image(executable))
    clock = Clock()
    missing = {procs[gone].pid} if gone is not None and gone < len(procs) else set()
    ps, chain = reading(procs, clock, outcomes, budget_s=budget, missing=missing)
    verdict = judge(token(100), chain=chain, root="/r/root", executable=image(executable))
    if verdict.person:
        assert verdict == whole
    if verdict.unreadable:
        assert executable == APP and not verdict.person
    else:
        assert verdict == whole or (executable != APP and verdict == NOT_THE_APP and not whole.person)
    assert clock.now - 1000.0 <= budget + 1e-9
    assert len(ps.calls) <= 2 * (1 + len(procs))
    for args, timeout in ps.calls:
        assert 0 < timeout <= peers.PS_TIMEOUT_S


@settings(max_examples=400, deadline=None)
@given(links=st.lists(LINKS, min_size=1, max_size=8), outcomes=st.lists(OUTCOME, max_size=30),
       executable=st.sampled_from([APP, SHELL]), gone=st.one_of(st.none(), st.integers(min_value=1, max_value=8)))
def test_with_time_to_spare_the_chain_is_unreadable_exactly_when_a_read_fails_twice(links, outcomes, executable,
                                                                                    gone):
    """C-25.6 against an independent model: with no deadline, one failed try per
    read is absorbed (the verdict is the whole chain's); two fail it closed, the
    app's request as unreadable and anyone else's as a refusal. So does a walk
    that cannot reach launchd because an ancestor left the table."""
    procs = chain_procs(*links)
    whole = judge(token(100), chain=lambda pid: procs, root="/r/root", executable=image(executable))
    missing = {procs[gone].pid} if gone is not None and gone < len(procs) else set()
    _, chain = reading(procs, Clock(), outcomes, budget_s=1e9, missing=missing)
    verdict = judge(token(100), chain=chain, root="/r/root", executable=image(executable))
    if not missing and reference(procs, outcomes):
        assert verdict == whole
    elif executable == APP:
        assert verdict.unreadable and not verdict.person
    else:
        assert verdict == NOT_THE_APP


# --- the real `ps`, wrapped --------------------------------------------------------


def fake_ps(tmp_path: Path, *, hang_first: int) -> tuple[Path, Path, str]:
    """A `ps` whose first `hang_first` runs hang (as a sleep with a unique
    argument), the rest the real /bin/ps."""
    count = tmp_path / "runs"
    hung = f"600.{uuid.uuid4().int % 10**9:09d}"
    script = tmp_path / "ps"
    script.write_text(f"""#!/bin/sh
echo run >> '{count}'
n=$(/usr/bin/wc -l < '{count}' | /usr/bin/tr -d ' ')
if [ "$n" -le {hang_first} ]; then exec /bin/sleep {hung}; fi
exec /bin/ps "$@"
""")
    script.chmod(0o755)
    return script, count, hung


def sleepers(hung: str) -> str:
    return subprocess.run(["/usr/bin/pgrep", "-f", f"sleep {hung}"], capture_output=True, text=True).stdout.strip()


def ps_runs() -> bool:
    try:
        return bool(subprocess.run(["/bin/ps", "-Ewwp", str(os.getpid()), "-o", "command="], capture_output=True,
                                   text=True, timeout=30).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_ps = pytest.mark.skipif(not ps_runs(), reason="this sandbox does not let /bin/ps inspect processes")


@pytest.fixture
def quick(monkeypatch):
    """A hang is detected in 1 s a try; the chain keeps its whole budget, so a real
    `ps` that answers slowly on a loaded host still reads it."""
    monkeypatch.setattr(peers, "PS_TIMEOUT_S", 1.0)


@needs_ps
def test_process_chain_reads_this_process_with_the_real_ps():
    chain = process_chain(os.getpid())
    assert chain[0].pid == os.getpid() and chain[0].command
    assert all(later.pid == earlier.ppid for earlier, later in zip(chain, chain[1:]))


@needs_ps
def test_a_real_ps_that_hangs_is_killed_and_the_request_is_unreadable(tmp_path, monkeypatch, quick):
    script, count, hung = fake_ps(tmp_path, hang_first=99)
    monkeypatch.setattr(peers, "PS", str(script))
    started = time.monotonic()
    verdict = judge(token(os.getpid()), executable=image(APP))
    elapsed = time.monotonic() - started
    assert verdict.unreadable and not verdict.person, verdict
    assert "did not answer within 1 s, twice" in verdict.reason
    assert len(count.read_text().splitlines()) == 2
    assert elapsed < peers.CHAIN_BUDGET_S + 5          # spawning on a loaded host is not the check's time
    assert sleepers(hung) == ""                       # each hung `ps` was killed, none left behind


@needs_ps
def test_a_real_ps_that_hangs_once_reads_the_same_chain_as_the_real_ps(tmp_path, monkeypatch, quick):
    """Differential: one hang absorbed, the chain equals /bin/ps's own."""
    script, count, hung = fake_ps(tmp_path, hang_first=1)
    real = process_chain(os.getpid())
    monkeypatch.setattr(peers, "PS", str(script))
    assert process_chain(os.getpid()) == real
    assert len(count.read_text().splitlines()) == 2 + len(real)      # the table twice, then each command once
    assert sleepers(hung) == ""


# --- what the client hears ----------------------------------------------------------


def bare_service(tmp_path):
    from subfleet.conversations.service import ConversationService
    service = ConversationService.__new__(ConversationService)
    service.root = tmp_path
    service.log = logging.getLogger("peer-chain-test")
    service._allowed_app_paths = allowed_executable_paths(APP_EXECUTABLES)
    service.daemon = SimpleNamespace(stopping=threading.Event(), _count_connection=lambda *args: None)
    return service


def answer(service, op: str, args: dict, peer: AuditToken | None) -> dict:
    left, right = socket.socketpair()
    try:
        service.respond(left, threading.Lock(), protocol.Request(op=op, args=args, id="t"), peer)
        with right.makefile("rb") as reader:
            return json.loads(reader.readline())
    finally:
        left.close()
        right.close()


@needs_ps
def test_the_service_fails_an_unreadable_chain_closed_with_the_cause(tmp_path, monkeypatch, quick, caplog):
    """C-25.6, C-17.3: through the service's own answer, the app's request whose
    chain cannot be read (the real `ps`, wrapped to hang) is exit 1
    `person-check-failed` naming the cause, before anything is read or changed
    (the approval id need not exist), with no nonce; the daemon log names it too.
    Never the bare "operation failed" of an unexpected exception."""
    from subfleet.conversations import service as service_module
    script, _, hung = fake_ps(tmp_path, hang_first=99)
    monkeypatch.setattr(peers, "PS", str(script))
    monkeypatch.setattr(service_module, "judge", functools.partial(peers.judge, executable=image(APP)))
    service = bare_service(tmp_path)
    with caplog.at_level(logging.WARNING, logger="peer-chain-test"):
        response = answer(service, "approval.get", {"approval_id": "no-such"}, token(os.getpid()))
    assert response["ok"] is False
    assert response["error"] == {
        "code": 1,
        "message": "person-check-failed: reading an approval is a person's decision, and the caller's process chain "
                   "could not be read: `ps -axo pid=,ppid=,tty=` did not answer within 1 s, twice",
        "fix": "nothing was done; try again"}
    assert "nonce" not in json.dumps(response)
    assert any("person check for reading an approval could not run" in r.getMessage() for r in caplog.records)
    assert sleepers(hung) == ""


@needs_ps
def test_the_service_refuses_a_caller_that_is_not_the_app_even_when_its_chain_hangs(tmp_path, monkeypatch, quick):
    """C-25.6 with D-PB1: a terminal or an agent is never told to try again; with
    its chain unread it gets the same exit 7 it would get with the chain read."""
    from subfleet.conversations import service as service_module
    script, _, hung = fake_ps(tmp_path, hang_first=99)
    monkeypatch.setattr(peers, "PS", str(script))
    monkeypatch.setattr(service_module, "judge", functools.partial(peers.judge, executable=image(SHELL)))
    response = answer(bare_service(tmp_path), "approval.respond", {"approval_id": "no-such"}, token(os.getpid()))
    assert response["error"] == {
        "code": 7,
        "message": "person-only: answering an approval is a person's decision: this needs the Subfleet app; "
                   "if an update moved its running bundle, restart the app",
        "fix": "answer it in the Subfleet app"}
    assert sleepers(hung) == ""


def test_the_service_keeps_refusing_an_agent_with_exit_7(tmp_path, monkeypatch):
    """A chain that was read and refuses stays exit 7 `person-only` (unchanged)."""
    from subfleet.conversations import service as service_module
    monkeypatch.setattr(service_module, "judge", lambda pid, **kw: Verdict(False, "the caller carries Subfleet's "
                                                                                  "attempt markers", pid))
    with pytest.raises(ConversationError) as refused:
        bare_service(tmp_path)._person(token(10), "answering an approval")
    assert (refused.value.code, refused.value.reason, refused.value.fix) == (7, "person-only",
                                                                             "answer it in the Subfleet app")
    monkeypatch.setattr(service_module, "judge", lambda pid, **kw: Verdict(False, "x", pid, unreadable=True))
    with pytest.raises(ConversationError) as failed:
        bare_service(tmp_path)._person(token(10), "answering an approval")
    assert (failed.value.code, failed.value.reason) == (1, "person-check-failed")


# --- through the real service ---------------------------------------------------------

from tests.unit.test_conversation_service import SETTINGS, conversation, svc  # noqa: E402,F401 (a fixture)


def unreadable_for_the_app(monkeypatch, calls: list | None = None):
    """The service's own `judge` on the app's image, whose chain cannot be read."""
    from subfleet.conversations import service as service_module

    def hung(pid):
        raise peers.ChainUnreadable("`ps -axo pid=,ppid=,tty=` did not answer within 5 s, twice")

    def counted(peer, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        return peers.judge(peer, chain=hung, executable=image(APP), **kwargs)
    monkeypatch.setattr(service_module, "judge", counted)


@pytest.mark.parametrize("op", ["suggestion.start", "suggestion.dismiss"])
def test_a_suggestion_op_keeps_the_retry_when_the_chain_cannot_be_read(svc, monkeypatch, op):
    """C-25.6 with C-25.7: a suggestion op answers only from fixed templates, and
    `person-check-failed` is one of them, so the app still hears exit 1 with the
    retry, not "the suggestion operation failed"."""
    unreadable_for_the_app(monkeypatch)
    response = answer(svc, op, {"suggestion_id": str(uuid.uuid4())}, token(10))
    assert response["error"] == {
        "code": 1,
        "message": "person-check-failed: starting or dismissing a suggestion is a person's decision, and the "
                   "caller's process chain could not be read in time",
        "fix": "nothing was done; try again"}


def test_a_settings_change_that_widens_and_allows_main_is_checked_once(svc, monkeypatch):
    """C-25.6: one request, one chain read within one budget. Two checks, each up
    to the budget, could together pass the app's 15 s."""
    calls: list = []
    unreadable_for_the_app(monkeypatch, calls)
    cid = conversation(svc)
    widened = {**SETTINGS, "permission": "bypass"}
    with pytest.raises(ConversationError) as failed:
        svc.handle("conversation.settings", {"conversation_id": cid, "settings": widened, "confirm_widen": True,
                                             "allow_main": True}, token(10))
    assert (failed.value.code, failed.value.reason) == (1, "person-check-failed")
    assert str(failed.value).startswith("widening a conversation's permissions and working on main is a "
                                        "person's decision, and ")
    assert len(calls) == 1
    view = svc.store.conversation(cid)
    assert view["settings"]["permission"] == "ask" and not view["allow_main"]       # nothing was done
