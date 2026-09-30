"""C-25.6: reading the caller's process chain for a person-only op.

The chain is read with `ps`, which a loaded machine can slow or fail to start.
A chain that cannot be read in time decides nothing about the caller: the op
fails closed, exit 1 `person-check-failed`, naming the cause, and asking again
is safe. These tests drive `process_chain` through a scripted `ps` on a fake
clock (every schedule of answers, hangs and failed starts), through a real
`ps` wrapped to hang, and through the service's answer to the client.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import protocol
from subfleet.conversations import peers
from subfleet.conversations.peers import ChainUnreadable, Proc, PsReader, Verdict, judge, process_chain
from subfleet.conversations.store import ConversationError

APP = "/Applications/Subfleet.app/Contents/MacOS/Subfleet"
TABLE = ["-axo", "pid=,ppid=,tty="]


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
    "eagain" fails to start, "empty" lists no processes. A live process always
    has a command line, so "empty" affects only the table read."""

    def __init__(self, procs: list[Proc], clock: Clock, outcomes=(), answer_s: float = 0.01):
        self.procs = {p.pid: p for p in procs}
        self.clock = clock
        self.outcomes = list(outcomes)
        self.answer_s = answer_s
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, capture_output, text, timeout, env, check):
        assert argv[0] == peers.PS and capture_output and text and not check
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
        if outcome == "empty" and table:
            return subprocess.CompletedProcess(argv, 1, "", "ps: no processes\n")
        if table:
            rows = [f"{p.pid:>5} {p.ppid:>5} {p.tty}" for p in self.procs.values()] + ["77777 1 ??"]
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


def reading(procs, clock, outcomes=(), budget_s=None):
    ps = ScriptedPs(procs, clock, outcomes)
    reader = PsReader(budget_s, run=ps, clock=clock, sleep=clock.sleep)
    return ps, (lambda pid: process_chain(pid, read=reader))


# --- the reader ------------------------------------------------------------------


def test_a_ps_that_never_answers_fails_closed_naming_the_cause():
    """C-25.6: two tries at the table, each cut at 5 s, then a verdict that says
    why; nothing is decided in the caller's favour and no exception escapes."""
    clock = Clock()
    procs = chain_procs(("ttys001", "/bin/zsh"), ("ttys001", "login"))
    ps, chain = reading(procs, clock, outcomes=[9.0, 9.0])
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/zsh")
    assert verdict == Verdict(False, "the caller's process chain could not be read: `ps -axo pid=,ppid=,tty=` "
                                     "did not answer within 5 s, then did not answer within 4.9 s", 100,
                              unreadable=True)
    assert [args for args, _ in ps.calls] == [TABLE, TABLE]
    assert clock.now - 1000.0 == pytest.approx(peers.CHAIN_BUDGET_S)
    # With room for two whole tries, the cause says so once.
    _, chain = reading(procs, Clock(), outcomes=[9.0, 9.0], budget_s=30.0)
    assert judge(100, chain=chain).reason.endswith("`ps -axo pid=,ppid=,tty=` did not answer within 5 s, twice")


def test_one_slow_ps_is_tried_again_and_the_verdict_is_the_whole_chains():
    """C-25.6: a single hang is absorbed by the second try."""
    clock = Clock()
    procs = chain_procs(("ttys001", "/bin/zsh"), ("ttys001", "login"))
    ps, chain = reading(procs, clock, outcomes=[9.0])
    assert judge(100, chain=chain, executable=lambda pid: "/bin/zsh") == Verdict(True, "a terminal (ttys001)", 100)
    assert [args for args, _ in ps.calls][:2] == [TABLE, TABLE]


def test_an_ancestor_that_was_not_read_is_never_taken_to_lack_the_markers():
    """C-25.6: the table reads, the caller reads, but the marked ancestor's `ps -E`
    hangs twice. Taking the unread ancestor as "no markers" would accept this
    caller, which has a terminal; the check fails closed instead."""
    marked = ("ttys001", "node claude SUBFLEET_ATTEMPT=j/a1 SUBFLEET_JOB=j")
    procs = chain_procs(("ttys001", "/bin/zsh"), marked, ("ttys001", "login"))
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=["ok", "ok", 9.0, 9.0], budget_s=30.0)
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/zsh")
    assert not verdict.person and verdict.unreadable
    assert "`ps -Ewwp 101 -o command=` did not answer within 5 s, twice" in verdict.reason
    # The same chain with that command read as empty (what swallowing the timeout
    # would give) is accepted, which is why a timeout must never read as "".
    blank = [Proc(p.pid, p.ppid, p.tty, "" if p.pid == 101 else p.command) for p in procs]
    assert judge(100, chain=lambda pid: blank, executable=lambda pid: "/bin/zsh").person
    assert not judge(100, chain=lambda pid: procs, executable=lambda pid: "/bin/zsh").person


def test_a_ps_that_cannot_start_is_tried_again_after_a_pause():
    """C-25.6: `fork` failing (EAGAIN) on a loaded machine is tried once more."""
    procs = chain_procs(("??", APP))
    clock = Clock()
    ps, chain = reading(procs, clock, outcomes=["eagain"])
    assert judge(100, chain=chain, executable=lambda pid: APP) == Verdict(True, "the Subfleet app", 100)
    assert clock.slept == [peers.RETRY_PAUSE_S]
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=["eagain", "eagain"])
    verdict = judge(100, chain=chain, executable=lambda pid: APP)
    assert verdict.unreadable and not verdict.person
    assert verdict.reason.endswith("`ps -axo pid=,ppid=,tty=` could not start (Resource temporarily unavailable), twice")


def test_an_empty_process_table_is_a_failed_try():
    """The table is never empty: `ps` listing nothing is a failure, tried again."""
    procs = chain_procs(("ttys002", "/bin/zsh"))
    _, chain = reading(procs, Clock(), outcomes=["empty"])
    assert judge(100, chain=chain, executable=lambda pid: "/bin/zsh").person
    _, chain = reading(procs, Clock(), outcomes=["empty", 9.0])
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/zsh")
    assert verdict.unreadable
    assert verdict.reason.endswith("listed no processes (exit 1), then did not answer within 5 s")


def test_the_whole_chain_is_read_within_its_budget_however_long():
    """C-25.6: 64 ancestors each answering in 0.9 s (under the per-call 5 s)
    cannot hold the check past its 10 s: the read stops, closed, at the budget."""
    procs = chain_procs(*[("??", f"/bin/sh -c step{i}") for i in range(64)])
    clock = Clock()
    _, chain = reading(procs, clock, outcomes=[0.9] * 200)
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/sh")
    assert verdict.unreadable and not verdict.person
    assert clock.now - 1000.0 <= peers.CHAIN_BUDGET_S + 1e-9
    assert "ran out" in verdict.reason


def test_a_second_try_is_cut_to_what_is_left_of_the_budget():
    clock = Clock()
    procs = chain_procs(("ttys001", "/bin/zsh"))
    ps, chain = reading(procs, clock, outcomes=[9.0, 9.0], budget_s=7.0)
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/zsh")
    assert [round(timeout, 6) for _, timeout in ps.calls] == [5.0, round(7.0 - 5.0 - peers.RETRY_PAUSE_S, 6)]
    assert verdict.reason.endswith("did not answer within 5 s, then did not answer within 1.9 s")
    assert clock.now - 1000.0 == pytest.approx(7.0)
    clock = Clock()
    ps, chain = reading(procs, clock, outcomes=[9.0], budget_s=5.0)
    verdict = judge(100, chain=chain, executable=lambda pid: "/bin/zsh")
    assert len(ps.calls) == 1
    assert verdict.reason.endswith("did not answer within 5 s, and the check's 5 s ran out before a second try")


def test_a_chain_that_was_read_is_unchanged():
    """Only the reading changed: an injected chain and the empty chain decide as before."""
    assert not judge(10, chain=lambda pid: []).person
    assert not judge(10, chain=lambda pid: []).unreadable
    assert judge(None) == Verdict(False, "the caller's process could not be identified", None)


# --- every schedule --------------------------------------------------------------

LINKS = st.tuples(st.sampled_from(["??", "ttys001"]),
                  st.sampled_from(["/bin/zsh", "node /x/claude", "login", "/v/python -m subfleet.guardian --attempt-dir x",
                                   "bash SUBFLEET_ATTEMPT=j/a1 PATH=/bin", "sh SUBFLEET_JOB=j", "zsh SUBFLEET_ROOT=/r/root"]))
OUTCOME = st.one_of(st.just("ok"), st.just("eagain"), st.just("empty"),
                    st.floats(min_value=0.0, max_value=6.0, allow_nan=False))


def reference(procs: list[Proc], outcomes: list) -> bool:
    """Whether the chain can be read with no deadline: every read gets two tries,
    and a try fails when it hangs past 5 s, cannot start, or (the table only) lists
    nothing. Independent of `PsReader`: it walks the reads the chain needs."""
    pending = list(outcomes)
    reads = [True] + [False] * len(procs)               # the table, then each process's command
    for table in reads:
        for _try in range(2):
            outcome = pending.pop(0) if pending else "ok"
            failed = (outcome == "eagain" or (isinstance(outcome, float) and outcome >= peers.PS_TIMEOUT_S)
                      or (outcome == "empty" and table))
            if not failed:
                break
        else:
            return False
    return True


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(links=st.lists(LINKS, min_size=1, max_size=8), outcomes=st.lists(OUTCOME, max_size=30),
       executable=st.sampled_from([APP, "/bin/zsh"]),
       budget=st.one_of(st.just(10.0), st.floats(min_value=0.0, max_value=40.0, allow_nan=False)))
def test_no_schedule_of_ps_failures_makes_the_check_more_permissive(links, outcomes, executable, budget):
    """C-25.6 invariants over every chain and every schedule of `ps` answers:
    (1) the verdict is the fully read chain's, or unreadable; never another;
    (2) so a failure never accepts a caller the whole chain would refuse;
    (3) the check ends within its budget; (4) at most two tries per read, each
    cut to at most 5 s and to what is left of the budget."""
    procs = chain_procs(*links)
    whole = judge(100, chain=lambda pid: procs, root="/r/root", executable=lambda pid: executable)
    clock = Clock()
    ps, chain = reading(procs, clock, outcomes, budget_s=budget)
    verdict = judge(100, chain=chain, root="/r/root", executable=lambda pid: executable)
    assert verdict == whole or (verdict.unreadable and not verdict.person)
    if verdict.person:
        assert whole.person
    assert clock.now - 1000.0 <= budget + 1e-9
    assert len(ps.calls) <= 2 * (1 + len(procs))
    for args, timeout in ps.calls:
        assert 0 < timeout <= peers.PS_TIMEOUT_S


@settings(max_examples=400, deadline=None)
@given(links=st.lists(LINKS, min_size=1, max_size=8), outcomes=st.lists(OUTCOME, max_size=30),
       executable=st.sampled_from([APP, "/bin/zsh"]))
def test_with_time_to_spare_the_chain_is_unreadable_exactly_when_a_read_fails_twice(links, outcomes, executable):
    """C-25.6 against an independent model: with no deadline, one failed try per
    read is absorbed (the verdict is the whole chain's) and two fail it closed."""
    procs = chain_procs(*links)
    whole = judge(100, chain=lambda pid: procs, root="/r/root", executable=lambda pid: executable)
    _, chain = reading(procs, Clock(), outcomes, budget_s=1e9)
    verdict = judge(100, chain=chain, root="/r/root", executable=lambda pid: executable)
    if reference(procs, outcomes):
        assert verdict == whole
    else:
        assert verdict.unreadable and not verdict.person


# --- the real `ps`, wrapped --------------------------------------------------------


def fake_ps(tmp_path: Path, *, hang_first: int) -> tuple[Path, Path, str]:
    """A `ps` whose first `hang_first` runs hang (as a sleep with a unique
    argument), the rest the real /bin/ps."""
    count = tmp_path / "runs"
    token = f"600.{uuid.uuid4().int % 10**9:09d}"
    script = tmp_path / "ps"
    script.write_text(f"""#!/bin/sh
echo run >> '{count}'
n=$(/usr/bin/wc -l < '{count}' | /usr/bin/tr -d ' ')
if [ "$n" -le {hang_first} ]; then exec /bin/sleep {token}; fi
exec /bin/ps "$@"
""")
    script.chmod(0o755)
    return script, count, token


def sleepers(token: str) -> str:
    return subprocess.run(["/usr/bin/pgrep", "-f", f"sleep {token}"], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def quick(monkeypatch):
    """A hang is detected in 1 s a try; the chain keeps its whole budget, so a real
    `ps` that answers slowly on a loaded host still reads it."""
    monkeypatch.setattr(peers, "PS_TIMEOUT_S", 1.0)


def test_process_chain_reads_this_process_with_the_real_ps():
    chain = process_chain(os.getpid())
    assert chain[0].pid == os.getpid() and chain[0].command
    assert all(later.pid == earlier.ppid for earlier, later in zip(chain, chain[1:]))


def test_a_real_ps_that_hangs_is_killed_and_the_request_is_unreadable(tmp_path, monkeypatch, quick):
    script, count, token = fake_ps(tmp_path, hang_first=99)
    monkeypatch.setattr(peers, "PS", str(script))
    started = time.monotonic()
    verdict = judge(os.getpid())
    elapsed = time.monotonic() - started
    assert verdict.unreadable and not verdict.person, verdict
    assert "did not answer within 1 s, twice" in verdict.reason
    assert len(count.read_text().splitlines()) == 2
    assert elapsed < peers.CHAIN_BUDGET_S + 5          # spawning on a loaded host is not the check's time
    assert sleepers(token) == ""                       # each hung `ps` was killed, none left behind


def test_a_real_ps_that_hangs_once_reads_the_same_chain_as_the_real_ps(tmp_path, monkeypatch, quick):
    """Differential: one hang absorbed, the chain equals /bin/ps's own."""
    script, count, token = fake_ps(tmp_path, hang_first=1)
    real = process_chain(os.getpid())
    monkeypatch.setattr(peers, "PS", str(script))
    assert process_chain(os.getpid()) == real
    assert len(count.read_text().splitlines()) == 2 + len(real)      # the table twice, then each command once
    assert sleepers(token) == ""


# --- what the client hears ----------------------------------------------------------


def bare_service(tmp_path):
    from subfleet.conversations.service import ConversationService
    service = ConversationService.__new__(ConversationService)
    service.root = tmp_path
    service.log = logging.getLogger("peer-chain-test")
    return service


def answer(service, op: str, args: dict, peer: int | None) -> dict:
    left, right = socket.socketpair()
    try:
        service.respond(left, threading.Lock(), protocol.Request(op=op, args=args, id="t"), peer)
        with right.makefile("rb") as reader:
            return json.loads(reader.readline())
    finally:
        left.close()
        right.close()


def test_the_service_fails_an_unreadable_chain_closed_with_the_cause(tmp_path, monkeypatch, quick, caplog):
    """C-25.6, C-17.3: through the service's own answer, an unreadable chain is
    exit 1 `person-check-failed` naming the cause, before anything is read or
    changed (the approval id need not exist), with no nonce; the daemon log names
    it too. Never the bare "operation failed" of an unexpected exception."""
    script, _, token = fake_ps(tmp_path, hang_first=99)
    monkeypatch.setattr(peers, "PS", str(script))
    service = bare_service(tmp_path)
    with caplog.at_level(logging.WARNING, logger="peer-chain-test"):
        response = answer(service, "approval.get", {"approval_id": "no-such"}, os.getpid())
    assert response["ok"] is False
    assert response["error"] == {
        "code": 1,
        "message": "person-check-failed: reading an approval is a person's decision, and the caller's process chain "
                   "could not be read: `ps -axo pid=,ppid=,tty=` did not answer within 1 s, twice",
        "fix": "nothing was done; try again"}
    assert "nonce" not in json.dumps(response)
    assert any("person check for reading an approval could not run" in r.getMessage() for r in caplog.records)
    assert sleepers(token) == ""


def test_the_service_keeps_refusing_an_agent_with_exit_7(tmp_path, monkeypatch):
    """A chain that was read and refuses stays exit 7 `person-only` (unchanged)."""
    from subfleet.conversations import service as service_module
    monkeypatch.setattr(service_module, "judge", lambda pid, **kw: Verdict(False, "the caller carries Subfleet's "
                                                                                  "attempt markers", pid))
    with pytest.raises(ConversationError) as refused:
        bare_service(tmp_path)._person(10, "answering an approval")
    assert (refused.value.code, refused.value.reason, refused.value.fix) == (7, "person-only",
                                                                             "answer it in the Subfleet app")
    monkeypatch.setattr(service_module, "judge", lambda pid, **kw: Verdict(False, "x", pid, unreadable=True))
    with pytest.raises(ConversationError) as failed:
        bare_service(tmp_path)._person(10, "answering an approval")
    assert (failed.value.code, failed.value.reason) == (1, "person-check-failed")
