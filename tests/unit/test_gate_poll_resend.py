"""C-15.4: a `gate.poll` sent again never advances a gate twice (D-WT1's gate half).

`subfleet gate` sends `gate.poll` again after a lost answer, a refused or missing
socket, or a daemon still recovering, as `subfleet wait` does. That is safe only if
a poll is a step of the gate's journaled state machine and nothing else: the daemon
may have run the poll whose answer was lost, run part of it before it died, or not
run it at all, and the poll sent again must then take the next step, never the one
before it again. These tests drive the real `GateService` (with the real submit and
admission behind it, a fake peer and a fake `gh`) through every schedule Hypothesis
draws of polls, peer completions, restarts, recovering daemons, concurrent pollers
and deaths at each durable write, and check:

* D1 one peer job per round: a round's submit, sent again after a death between
  the job and the journal entry naming it, finds the same job (C-6.2);
* D2 each transition is journaled at most once per round (`round-submitted`,
  `round-finished`, `certificate-issued`), and `completed` at most once;
* D3 at most one `gh pr merge` and one merge action per gate (C-19.1, C-23.13);
* D4 once a poll has answered a code, more polls change nothing: no journal entry,
  no job, no action, no command but reads, and the same answer;
* D5 a recovering daemon's answer does nothing at all;
* D6 without a death, the gate ends as a lossless run of the same gate does, however
  many polls were sent, lost or doubled; with one, it ends in a code a death may
  leave (blocked 4, or an unverified merge 5) and D1 to D4 still hold.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli
from subfleet.gate.merge import MergeActions
from subfleet.gate.service import RECOVERING, GateService, dispatch
from tests.fake.test_gate_end_to_end import arguments, finish, wire
from tests.unit.test_gate_admission import build_core
from tests.unit.test_gate_merge import BASE, HEAD, FakeGh

_TERMINAL_JOB = {"succeeded", "failed", "cancelled", "lost"}
#: Transitions a resent poll must never journal twice for one round.
_ONCE_PER_ROUND = ("round-submitted", "round-finished", "certificate-issued")


class Died(BaseException):
    """The daemon process ending mid-poll: no handler in the service sees it."""


class PatchGh(FakeGh):
    """`FakeGh` that also answers the PR patch read a round's bundle copies."""

    def __call__(self, command, **kwargs):
        if command[:2] == ["git", "diff"]:
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "diff --git a/file b/file\n+change\n", "")
        return super().__call__(command, **kwargs)

    @property
    def writes(self):
        return [c for c in self.commands if c[:3] == ["gh", "pr", "merge"]]


class Deaths:
    """Arms one death at a durable write of the gate service: before or after a
    journal entry for a named transition, or before the projection that follows a
    journal entry (the commit landed, the files did not)."""

    def __init__(self):
        self.armed: tuple[str, str] | None = None
        self.fired = 0

    def install(self, patch: pytest.MonkeyPatch) -> None:
        deaths = self
        journal, project = GateService._journal, GateService._project

        def _journal(service, state, transition):
            if deaths.armed == ("before", transition):
                deaths._fire()
            journal(service, state, transition)
            if deaths.armed == ("after", transition):
                deaths._fire()

        def _project(service, state):
            if deaths.armed == ("project", "any"):
                deaths._fire()
            project(service, state)

        patch.setattr(GateService, "_journal", _journal)
        patch.setattr(GateService, "_project", _project)

    def _fire(self):
        self.armed = None
        self.fired += 1
        raise Died("the daemon died here")


@contextlib.contextmanager
def gate_world(kind: str, start_death: tuple[str, str] | None = None):
    """A fresh daemon core per example (Hypothesis cannot share a function fixture).
    `start_death` kills the daemon at that write of `gate.start`; its answer is lost,
    and the next daemon is started on the same store."""
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        tmp = Path(directory).resolve()
        core = build_core(tmp, patch)
        deaths = Deaths()
        deaths.install(patch)
        runner = PatchGh()
        core.gate_runner = runner
        try:
            if kind == "plan":
                plan = tmp / "plan.md"
                plan.write_text("A plan whose gate is polled again.\n")
                args = arguments(plan)
            else:
                source = tmp / "source"
                source.mkdir()
                args = cli.build_parser().parse_args([
                    "gate", "pr", "42", "--peer", "astra", "--main-approve", "--expect-head", HEAD,
                    "--expect-base", BASE, "--on-agreement", "merge", "--merge-method", "squash",
                    "-C", str(source)])
            deaths.armed = start_death
            try:
                started = dispatch(core, "gate.start", wire(args))
            except Died:
                # `add_event` also writes a bare audit row of the same kind: the one
                # with the gate's id is the journal's.
                row = core.store.one("SELECT data_json FROM events WHERE kind='gate.state' "
                                     "AND json_extract(data_json,'$.gate_id') IS NOT NULL "
                                     "ORDER BY event_id DESC LIMIT 1")
                world = World(core, runner, deaths, json.loads(row["data_json"])["gate_id"])
                world.died += 1
                world.restart()
                yield world
                return
            deaths.armed = None
            assert started["code"] is None and started["job_id"], started
            yield World(core, runner, deaths, started["gate_id"])
        finally:
            core.store.close()


class World:
    def __init__(self, core, runner, deaths, gate_id):
        self.core, self.runner, self.deaths, self.gate_id = core, runner, deaths, gate_id
        self.answers: list[dict] = []
        self.attestations: list[str] = []          # per round, in order; "attested" after
        self.died = 0

    # --- what a daemon and its peer do -------------------------------------------

    def poll(self) -> dict | None:
        try:
            answer = dispatch(self.core, "gate.poll", {"gate_id": self.gate_id})
        except Died:
            self.died += 1
            self.restart()
            return None
        self.answers.append(answer)
        return answer

    def restart(self) -> None:
        """What the next daemon has: the store, a new gate service (its locks and
        nothing else), and startup recovery's `MergeActions.recover` (C-19.1)."""
        self.core._gate_service = GateService(self.core)
        MergeActions(self.core.store).recover()

    def finish_peer(self) -> bool:
        state = self.core._gate_service._load(self.gate_id)
        record = (state.get("rounds") or [{}])[-1]
        job = self.core.store.get_job(record.get("peer_run_id")) if record.get("peer_run_id") else None
        if job is None or job["state"] in _TERMINAL_JOB:
            return False
        number = len(state["rounds"])
        attestation = self.attestations[number - 1] if number <= len(self.attestations) else "attested"
        finish(self.core, {"job_id": job["job_id"]}, attestation=attestation)
        return True

    def recovering_poll(self) -> None:
        before = self.snapshot()
        self.core._recovery_complete = threading.Event()
        try:
            assert dispatch(self.core, "gate.poll", {"gate_id": self.gate_id}) == RECOVERING
        finally:
            self.core._recovery_complete.set()
        assert self.snapshot() == before, "D5: a recovering daemon's answer did something"

    def concurrent_polls(self) -> None:
        """Two pollers at once (a `subfleet gate` and a `gate poll`): the gate lock
        serializes them, and each takes its step from what the other left."""
        results: list = []

        def one():
            try:
                results.append(dispatch(self.core, "gate.poll", {"gate_id": self.gate_id}))
            except Died:
                results.append(Died)
        threads = [threading.Thread(target=one) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        if Died in results:
            self.died += 1
            self.restart()
        self.answers.extend(r for r in results if isinstance(r, dict))

    def kill_at_merge(self, landed: bool) -> None:
        def die():
            if landed:
                self.runner.landed()
            self.runner.on_merge = None
            raise Died("the daemon died during gh pr merge")
        self.runner.on_merge = die

    def drain(self, limit: int = 40) -> dict:
        """Poll as `subfleet gate` does, completing peers as they are submitted."""
        for _ in range(limit):
            self.finish_peer()
            answer = self.poll()
            if answer is not None and answer.get("code") is not None:
                return answer
        raise AssertionError(f"the gate never answered a code: {self.answers[-3:]}")

    # --- what is checked ---------------------------------------------------------

    def snapshot(self) -> tuple:
        store = self.core.store
        events = store.query("SELECT event_id FROM events WHERE kind='gate.state' ORDER BY event_id")
        jobs = store.query("SELECT job_id, state FROM jobs ORDER BY job_id")
        actions = store.query("SELECT action_id, state FROM actions ORDER BY action_id")
        return (tuple(e["event_id"] for e in events), tuple(map(tuple, (j.values() for j in jobs))),
                tuple(map(tuple, (a.values() for a in actions))), len(self.runner.writes))

    def transitions(self) -> list[tuple[int, str]]:
        rows = self.core.store.query("SELECT data_json FROM events WHERE kind='gate.state' ORDER BY event_id")
        out = []
        for row in rows:
            data = json.loads(row["data_json"])
            if data.get("gate_id") == self.gate_id and "state" in data:
                out.append((len(data["state"].get("rounds") or []), data["transition"]))
        return out

    def check_d1_to_d3(self, *, settled: bool = True) -> None:
        """D1 needs the gate settled: mid-schedule, a death may have left the last
        round's job made and not yet named, which the next poll names (D1's test)."""
        state = self.core._gate_service._load(self.gate_id)
        rounds = state["rounds"]
        ids = [r.get("peer_run_id") for r in rounds if r.get("peer_run_id")]
        jobs = self.core.store.query("SELECT job_id, request_id FROM jobs")
        own = {f"gate:{self.gate_id}:round:{r['number']}:{r['attempt_id']}" for r in rounds}
        assert {j["request_id"] for j in jobs} <= own, ("D1: a job outside the rounds' own request ids", jobs)
        assert len(jobs) == len({j["request_id"] for j in jobs}) <= len(rounds), ("D1", jobs, rounds)
        if settled:
            assert len(ids) == len(set(ids)) == len(jobs), ("D1", ids, jobs)
            assert {j["job_id"] for j in jobs} == set(ids), ("D1", ids, jobs)
        seen: dict[tuple[int, str], int] = {}
        for key in self.transitions():
            seen[key] = seen.get(key, 0) + 1
        twice = {key: n for key, n in seen.items() if n > 1 and key[1] in _ONCE_PER_ROUND}
        assert not twice, ("D2", twice)
        assert sum(n for (_, name), n in seen.items() if name == "completed") <= 1, ("D2", seen)
        assert len(self.runner.writes) <= 1, ("D3", self.runner.writes)
        assert len(self.core.store.query("SELECT * FROM actions")) <= 1, "D3"

    def check_d4(self, final: dict) -> None:
        """D4. One status re-reads by design: `action_attempting` (another holder's
        merge in flight) reconciles and journals what it read on every poll; even
        there no job, action or merge is made."""
        before, commands = self.snapshot(), len(self.runner.commands)
        for _ in range(3):
            self.finish_peer()
            again = dispatch(self.core, "gate.poll", {"gate_id": self.gate_id})
            assert (again["code"], again["status"]) == (final["code"], final["status"]), ("D4", again, final)
        after = self.snapshot()
        if final["status"] == "action_attempting":
            assert after[1:] == before[1:], "D4: a poll after a code made a job, an action or a merge"
        else:
            assert after == before, "D4: a poll after a code changed the gate"
        writes = [c for c in self.runner.commands[commands:] if c[:3] == ["gh", "pr", "merge"]]
        assert not writes, "D4: a poll after a code merged"


def lossless(kind: str, attestations: list[str]) -> tuple[int, str]:
    """The reference: the same gate, every poll answered once and seen."""
    with gate_world(kind) as world:
        world.attestations = attestations
        final = world.drain()
        world.check_d1_to_d3()
        return final["code"], final["status"]


_LOSSLESS: dict[tuple[str, tuple[str, ...]], tuple[int, str]] = {}


def reference(kind: str, attestations: list[str]) -> tuple[int, str]:
    key = (kind, tuple(attestations))
    if key not in _LOSSLESS:
        _LOSSLESS[key] = lossless(kind, attestations)
    return _LOSSLESS[key]


_DEATHS = [("before", "round-submitted"), ("after", "round-submitted"),
           ("before", "round-prepared"), ("after", "round-prepared"),
           ("before", "round-finished"), ("after", "round-finished"),
           ("before", "certificate-issued"), ("after", "certificate-issued"),
           ("before", "completed"), ("after", "completed"),
           ("before", "merge-result"), ("after", "merge-result"),
           ("project", "any")]

STEP = st.one_of(
    st.just(("poll",)), st.just(("poll",)), st.just(("poll",)),    # a poll, its answer seen or lost
    st.just(("finish",)), st.just(("finish",)),
    st.just(("restart",)),
    st.just(("recovering",)),
    st.just(("concurrent",)),
    st.tuples(st.just("die"), st.sampled_from(_DEATHS)),
)
MERGE_DEATH = st.sampled_from([None, None, "before-landing", "after-landing"])
ATTESTATIONS = st.sampled_from([[], ["mismatch"]])     # [] = attested; mismatch is re-run (C-23.43)
#: A start that dies after its round is journaled is followed by the next daemon's
#: polls; one that dies before leaves a gate with no round (`ready`), answered 4.
_START_DEATHS = {None: "followed", ("before", "round-submitted"): "followed",
                 ("after", "round-submitted"): "followed", ("after", "round-prepared"): "followed",
                 ("before", "round-prepared"): "ready", ("after", "created"): "ready"}
START_DEATH = st.sampled_from(list(_START_DEATHS))
#: Where a death loses the rerun of a discarded round (C-23.43): after the round's
#: verdict-or-discard commits and before the rerun's round is journaled. The gate is
#: left blocked with the discard's reason, never rerun twice (D2).
_LOSES_RERUN = {("project", "any"), ("before", "round-prepared")}


def run_schedule(world: World, steps) -> None:
    for step in steps:
        if step[0] == "poll":
            world.poll()
        elif step[0] == "finish":
            world.finish_peer()
        elif step[0] == "restart":
            world.restart()
        elif step[0] == "recovering":
            world.recovering_poll()
        elif step[0] == "concurrent":
            world.concurrent_polls()
        elif step[0] == "die":
            world.deaths.armed = step[1]
            world.poll()
            world.deaths.armed = None


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(kind=st.sampled_from(["plan", "pr"]), steps=st.lists(STEP, max_size=14),
       attestations=ATTESTATIONS, merge_death=MERGE_DEATH, start_death=START_DEATH)
def test_c15_4_polls_sent_again_never_advance_a_gate_twice(kind, steps, attestations, merge_death,
                                                           start_death):
    """D1 to D6 over every schedule of polls, peers, restarts and deaths."""
    with gate_world(kind, start_death) as world:
        world.attestations = attestations
        if kind == "pr" and merge_death:
            world.kill_at_merge(landed=merge_death == "after-landing")
        run_schedule(world, steps)
        world.check_d1_to_d3(settled=False)
        final = world.drain()
        if _START_DEATHS[start_death] == "ready":
            assert final["code"] == 4 and "no peer round" in final["message"], final
            assert world.core.store.query("SELECT * FROM jobs") == []
        world.check_d1_to_d3()
        world.check_d4(final)
        assert all(a == RECOVERING or "code" in a for a in world.answers)
        if _START_DEATHS[start_death] == "ready":
            return
        if not world.deaths.fired and not world.died:
            assert (final["code"], final["status"]) == reference(kind, attestations), "D6"
        else:
            # A death may end a gate short of the lossless result, never past it:
            # blocked (a rerun lost, _LOSES_RERUN), or a merge left unverified (5). It
            # never merges twice (D3) or reruns a round twice (D2).
            assert final["code"] in {reference(kind, attestations)[0], 4, 5}, ("D6", final)


# --- the windows the properties found worth pinning, one by one --------------------

@pytest.mark.parametrize("policy_edited", [False, True])
def test_d1_a_death_between_the_peer_job_and_its_journal_entry_finds_the_same_job(policy_edited):
    """The submit committed the job; the daemon died before `round-submitted`, so
    the start's answer was lost. The next daemon's poll adopts the job that carries
    the round's own request id: one job, and the gate follows it. Submitted again
    instead, it was refused as a different payload when the next daemon had read
    an edited policy (C-6.2 hashes it), as a kickstart after a policy edit has, and
    the round ended blocked with its job left running."""
    with gate_world("plan", ("before", "round-submitted")) as world:
        if policy_edited:
            world.core.policy_digest = "edited before the kickstart"
        job, = world.core.store.query("SELECT * FROM jobs")
        assert world.core._gate_service._load(world.gate_id)["rounds"][-1]["peer_run_id"] is None
        answer = world.poll()
        assert answer["code"] is None and answer["job_id"] == job["job_id"]
        assert len(world.core.store.query("SELECT * FROM jobs")) == 1
        final = world.drain()
        assert final["code"] == 0
        world.check_d1_to_d3()
        world.check_d4(final)


def test_a_start_that_died_before_its_round_answers_a_code_not_none_for_ever():
    """The start journaled its gate and died before reserving a round: no poll can
    advance it, so a poll answers 4 and names the continuation, where it had
    answered no code at all (and `subfleet gate poll` would have asked for ever)."""
    with gate_world("plan", ("before", "round-prepared")) as world:
        answer = world.poll()
        assert answer["code"] == 4 and answer["status"] == "ready"
        assert f"subfleet gate continue {world.gate_id} --main-approve" in answer["message"]
        world.check_d4(answer)


@pytest.mark.parametrize("landed", [False, True])
def test_d3_a_daemon_killed_during_the_merge_never_merges_again(landed):
    """C-19.1, C-23.13: the holder died during `gh pr merge`. Recovery marks the
    action unknown; the polls sent to the next daemon reconcile it from GitHub and
    never call `gh pr merge` again. A landed merge completes the gate (0); one that
    did not land is an unverified merge action (5), left for the main to decide."""
    with gate_world("pr") as world:
        world.kill_at_merge(landed=landed)
        world.finish_peer()
        assert world.poll() is None                 # died; `restart` ran recovery
        action, = world.core.store.query("SELECT * FROM actions")
        assert action["state"] == "unknown"
        final = world.drain()
        assert len(world.runner.writes) == 1
        assert (final["code"], final["status"]) == ((0, "completed") if landed else (5, "action_failed"))
        world.check_d1_to_d3()
        world.check_d4(final)


@pytest.mark.parametrize("death", _DEATHS)
def test_d6_one_death_at_each_durable_write(death):
    """Every durable write of a plan gate and a merge gate, with the daemon dying
    just before or just after it: the next daemon's polls end the gate as a
    lossless run does, or blocked where the death fell between a discarded round
    and its rerun's reservation (an attestation retry lost, never doubled)."""
    for kind in ("plan", "pr"):
        for attestations in ([], ["mismatch"]):
            with gate_world(kind) as world:
                world.attestations = attestations
                world.deaths.armed = death
                final = world.drain()
                world.check_d1_to_d3()
                world.check_d4(final)
                expected = reference(kind, attestations)
                if attestations and death in _LOSES_RERUN and final["code"] == 4 != expected[0]:
                    assert "mismatch" in final["message"], (kind, death, final)
                    assert len(world.core._gate_service._load(world.gate_id)["rounds"]) == 1
                else:
                    assert (final["code"], final["status"]) == expected, (kind, attestations, death, final)


def test_d5_a_recovering_daemon_answers_without_touching_the_gate():
    """The first poll a new daemon reads may come before its startup recovery has
    run (`MergeActions.recover` among it): it is answered `recovering` and nothing
    is done, so the CLI asks again (C-15.4)."""
    with gate_world("pr") as world:
        world.finish_peer()
        world.recovering_poll()
        assert world.drain()["code"] == 0
