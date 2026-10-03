"""C-15.7, C-23.50: the notice push's invariants, for every sequence of events.

`notify_push.plan` and `notify_push.deliver` run here exactly as the daemon runs
them, against a model world: a store of notices, Claude Code's registry, and
the inboxes behind it. Sessions restart under new pids, a pid is reused by
another session, statuses flip, hooks surface and acknowledge, waiters come and
go, and sends fail before or after writing. After every step:

* **at most once**: no notice is in more than one written frame;
* **never a lane, a conversation or a headless run**: no frame is written to,
  or accepted by, such a process;
* **keyed by session id, not pid**: every frame names the session its notices
  belong to, and an inbox accepts a frame only when that is its own session,
  however the registry changed between the plan and the write;
* **offered, never acknowledged**: a push moves a notice only from `pending`
  to `offered` (transport `socket`), or back when it wrote nothing;
* **waiters first** (C-23.50): no frame carries a job that had a live waiter,
  or that a `wait` reported within `push_after_wait_s`, when it was planned;
* **paced**: at most `push_per_minute` frames in any minute, and none to one
  session within `push_session_gap_s` of the last;
* **idle**: a frame is planned only for a session recorded idle (or with no
  status), and always at priority `later`.

A differential test checks the planner's recipient against v1's own ranking,
`notify_push.find_session`, over the same registry files (C-23.30).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from subfleet import notify_push
from subfleet.sessions import registry

SESSIONS = ("s-alpha", "s-beta", "s-gamma", "lane-1", "conv-1", "headless-1")
LANES = ("lane-1",)
CONVERSATIONS = ("conv-1",)
HEADLESS = ("headless-1",)
PIDS = (101, 102, 103, 104, 105, 106)
SETTINGS = notify_push.PushSettings(delay_s=5, max_age_s=900, session_gap_s=45, per_minute=3,
                                    after_wait_s=40, retry_s=20, max_tries=2)


@dataclass
class Proc:
    pid: int
    session_id: str
    socket: str
    alive: bool = True
    status: str | None = "idle"


@dataclass
class Notice:
    notice_id: int
    job_id: str
    session_id: str                     # as the notice recorded it (either case)
    created_at: float
    state: str = "pending"
    transport: str | None = None


@dataclass
class Frame:
    at: float
    socket: str
    session_id: str
    priority: str
    notice_ids: tuple[int, ...]         # the notices whose text the frame carries
    planned_owner: str                  # the row's session when the push was planned
    reserved: tuple[int, ...] = ()      # what the push had moved to `offered` before writing
    ages: tuple[float, ...] = ()        # each carried notice's age when written
    planned_age: float = 0.0            # the oldest planned notice's age (the one that made it due)
    accepted_by: str | None = None      # the inbox's session, if it took the frame


@dataclass
class World:
    now: float = 1_800_000_000.0
    procs: dict[int, Proc] = field(default_factory=dict)
    generation: int = 0
    notices: dict[int, Notice] = field(default_factory=dict)
    watched: set[str] = field(default_factory=set)
    waited: dict[str, float] = field(default_factory=dict)
    frames: list[Frame] = field(default_factory=list)
    transitions: list[tuple[int, str, str, str]] = field(default_factory=list)  # id, from, to, by
    history: notify_push.History = field(default_factory=notify_push.History)
    sockets: dict[str, tuple[int, str]] = field(default_factory=dict)   # path -> (pid, session)

    def rows(self) -> list[registry.SessionRow]:
        return [registry.SessionRow(
            session_id=proc.session_id, pid=proc.pid, socket=proc.socket, name=None, cwd=None,
            started_at=float(proc.pid), alive=proc.alive, socket_present=proc.alive,
            registry_path=f"/model/{proc.pid}.json",
            entrypoint="sdk-cli" if proc.session_id in HEADLESS else "claude-desktop",
            status=proc.status, kind="interactive") for proc in self.procs.values()]

    def pending(self) -> list[notify_push.Pending]:
        return [notify_push.Pending(n.notice_id, n.job_id, n.session_id, f"{n.job_id}: succeeded",
                                    n.created_at)
                for n in self.notices.values() if n.state == "pending"]


class NoticePush(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.w = World()
        self.next_id = 1
        self.planned: list[tuple[notify_push.Push, frozenset[str], dict[str, float]]] = []

    @initialize()
    def every_kind_of_session_is_running(self):
        """Each kind of recipient is live from the start, so passes reach the
        write; the rules below restart, reuse, stop and busy them."""
        for pid, session in zip(PIDS, SESSIONS):
            self.a_session_starts_on_a_pid(pid, session, "idle")

    # --- the world moves -------------------------------------------------------

    @rule(session=st.sampled_from(SESSIONS), upper=st.booleans(), age=st.sampled_from([0, 0, 8, 30]))
    def a_job_ends(self, session, upper, age):
        """The notice is written `age` seconds before the next pass looks."""
        notice_id = self.next_id
        self.next_id += 1
        recorded = session.upper() if upper else session
        self.w.notices[notice_id] = Notice(notice_id, f"job-{notice_id}", recorded, self.w.now - age)

    @rule(seconds=st.sampled_from([1, 4, 6, 6, 15, 31, 45, 70, 1000]))
    def time_passes(self, seconds):
        self.w.now += seconds

    @rule(pid=st.sampled_from(PIDS), session=st.sampled_from(SESSIONS),
          status=st.sampled_from(["idle", "busy", "waiting", None]))
    def a_session_starts_on_a_pid(self, pid, session, status):
        """A restart under a new pid, or a pid reused by another session: the row
        file `<pid>.json` is rewritten and the process has a new inbox."""
        self.w.generation += 1
        path = f"/tmp/cc-socks/{pid}-{self.w.generation}.sock"
        old = self.w.procs.get(pid)
        if old is not None:
            self.w.sockets.pop(old.socket, None)
        self.w.procs[pid] = Proc(pid, session, path, True, status)
        self.w.sockets[path] = (pid, session)

    @rule(pid=st.sampled_from(PIDS))
    def a_process_exits(self, pid):
        proc = self.w.procs.get(pid)
        if proc is not None:
            proc.alive = False
            self.w.sockets.pop(proc.socket, None)

    @rule(pid=st.sampled_from(PIDS), status=st.sampled_from(["idle", "busy", "waiting", None]))
    def a_status_flips(self, pid, status):
        if pid in self.w.procs:
            self.w.procs[pid].status = status

    @rule(session=st.sampled_from(SESSIONS), to=st.sampled_from(["surfaced", "acknowledged", "offered"]))
    def a_hook_or_the_session_reaches_its_notices(self, session, to):
        for n in self.w.notices.values():
            if n.session_id.lower() == session and n.state in ("pending", "offered"):
                self.move(n, to, transport=f"hook:{to}", by="hook")

    @rule(data=st.data())
    def a_waiter_registers(self, data):
        if self.w.notices:
            self.w.watched.add(data.draw(st.sampled_from(sorted(n.job_id for n in self.w.notices.values()))))

    @rule(data=st.data())
    def a_waiter_returns(self, data):
        if self.w.watched:
            job = data.draw(st.sampled_from(sorted(self.w.watched)))
            self.w.watched.discard(job)
            if data.draw(st.booleans()):
                self.w.waited[job] = self.w.now          # it answered the job's end

    # --- the push --------------------------------------------------------------

    @rule(send=st.sampled_from(["ok", "refused", "reset"]), recheck=st.booleans(),
          race=st.sampled_from(["none", "none", "hook", "pid-reuse"]),
          again=st.sampled_from([None, None, 2, 25, 61, 130]),
          then=st.sampled_from(["ok", "refused", "reset"]))
    def a_push_pass(self, send, recheck, race, again, then):
        """One pass, and sometimes the next one `again` seconds later (the
        daemon runs one every `push_interval_s`), after which a job of a
        session pushed to may have ended."""
        self.one_pass(send, recheck, race)
        if again is not None:
            self.w.now += again
            for session in {frame.session_id.lower() for frame in self.w.frames[-2:]}:
                self.a_job_ends(session, False, 8)
            self.one_pass(then, recheck, "none")

    def one_pass(self, send, recheck, race):
        w = self.w
        plan = notify_push.plan(w.pending(), w.rows(), now=w.now, settings=SETTINGS,
                                lane_ids=LANES, conversation_ids=CONVERSATIONS,
                                watched=set(w.watched), waited=dict(w.waited), history=w.history)
        for push in plan.pushes:
            owner = push.row.session_id
            # Facts as the plan saw them, for the invariants below.
            assert push.row.status in (None, "idle")
            for item in push.notices:
                assert item.job_id not in w.watched
                assert not (item.job_id in w.waited and w.now - w.waited[item.job_id] < SETTINGS.after_wait_s)
            if race == "hook" and len(push.notices) > 1:
                first = w.notices[push.notices[0].notice_id]
                if first.state == "pending":
                    self.move(first, "surfaced", transport="hook:UserPromptSubmit", by="hook")
            if race == "pid-reuse":
                # Between the plan and the write, the pid is given to another
                # session, at the same socket path (the worst case).
                proc = w.procs[push.row.pid]
                other = next(s for s in SESSIONS if s != proc.session_id.lower())
                proc.session_id = other
                w.sockets[proc.socket] = (proc.pid, other)

            def check(p, recheck=recheck):
                if not recheck:
                    return None
                proc = w.procs.get(p.row.pid)
                if proc is None or not proc.alive:
                    return "registry row gone"
                if proc.session_id.lower() != p.session_id.lower():
                    return "registry row changed"
                if proc.socket != p.row.socket:
                    return "inbox moved"
                if proc.status not in (None, "idle"):
                    return proc.status
                return None

            def write(path, token, content, *, timeout, priority, session_id, message_uuid,
                      ids=push.notice_ids, owner=owner):
                if send == "refused":
                    raise notify_push.PushError("connect refused", written=False)
                carried = tuple(int(found) for found in re.findall(r"\bjob-(\d+):", content))
                frame = Frame(w.now, path, session_id, priority, carried, owner, self.reserved,
                              ages=tuple(w.now - w.notices[i].created_at for i in carried),
                              planned_age=max(w.now - item.created_at for item in push.notices))
                holder = w.sockets.get(path)
                # The inbox drops a frame naming a session other than its own.
                if holder is not None and holder[1] == session_id:
                    frame.accepted_by = holder[1]
                w.frames.append(frame)
                if send == "reset":
                    raise notify_push.PushError("reset mid-write", written=True)

            self.reserved: tuple[int, ...] = ()
            outcome = notify_push.deliver(push, reserve=self.reserve, release=self.release,
                                          record=lambda kind, data: None, check=check, send=write,
                                          token_of=lambda pid: "tok",
                                          mode_of=lambda session: "bypass")
            notify_push.settle(w.history, push, outcome, w.now)
            event(f"push: {outcome.result}" + (" (race: pid reused)" if race == "pid-reuse" else ""))
        for reason in sorted(set(plan.held.values())):
            event(f"held: {reason}")

    def reserve(self, push, data):
        reserved = []
        for notice_id in push.notice_ids:
            n = self.w.notices[notice_id]
            if n.state == "pending":
                self.move(n, "offered", transport="socket", by="push")
                reserved.append(notice_id)
        self.reserved = tuple(reserved)
        return reserved

    def release(self, push, ids, data):
        for notice_id in ids:
            n = self.w.notices[notice_id]
            if n.state == "offered" and n.transport == "socket":
                self.move(n, "pending", transport=None, by="push")

    def move(self, n: Notice, to: str, *, transport, by):
        self.w.transitions.append((n.notice_id, f"{n.state}/{n.transport}", f"{to}/{transport}", by))
        n.state, n.transport = to, transport

    # --- what must always hold ---------------------------------------------------

    @invariant()
    def each_notice_is_written_at_most_once(self):
        seen: dict[int, int] = {}
        for frame in self.w.frames:
            for notice_id in frame.notice_ids:
                seen[notice_id] = seen.get(notice_id, 0) + 1
        assert all(count == 1 for count in seen.values()), seen

    @invariant()
    def a_frame_waits_for_the_delay_and_skips_the_backlog(self):
        """C-15.7: every notice written is younger than `push_max_age_min`, and
        the push was planned only once the session's oldest notice was
        `push_delay_s` old (all of its pending notices then go together, and a
        hook may take the old one before the write)."""
        for frame in self.w.frames:
            assert frame.planned_age >= SETTINGS.delay_s, frame
            assert all(age <= SETTINGS.max_age_s for age in frame.ages), frame

    @invariant()
    def a_frame_carries_only_what_its_push_reserved(self):
        """Only notices this push moved `pending` to `offered` are written: one a
        hook reached between the plan and the write is left out."""
        for frame in self.w.frames:
            assert frame.notice_ids and set(frame.notice_ids) <= set(frame.reserved), frame

    @invariant()
    def no_lane_conversation_or_headless_process_is_addressed(self):
        refused = {*LANES, *CONVERSATIONS, *HEADLESS}
        for frame in self.w.frames:
            assert frame.planned_owner.lower() not in refused
            assert frame.session_id.lower() not in refused
            assert frame.accepted_by is None or frame.accepted_by.lower() not in refused

    @invariant()
    def frames_are_keyed_by_session_id(self):
        for frame in self.w.frames:
            for notice_id in frame.notice_ids:
                assert self.w.notices[notice_id].session_id.lower() == frame.session_id.lower()
            assert frame.session_id == frame.planned_owner
            if frame.accepted_by is not None:
                assert frame.accepted_by == frame.session_id

    @invariant()
    def a_push_only_offers(self):
        allowed = {("pending/None", "offered/socket"), ("offered/socket", "pending/None")}
        for notice_id, before, after, by in self.w.transitions:
            if by == "push":
                assert (before, after) in allowed, (notice_id, before, after)

    @invariant()
    def frames_are_paced_and_later(self):
        times = sorted(frame.at for frame in self.w.frames)
        for index, start in enumerate(times):
            assert sum(1 for at in times[index:] if at - start < 60) <= SETTINGS.per_minute
        last: dict[str, float] = {}
        for frame in self.w.frames:
            key = frame.session_id.lower()
            if key in last:
                assert frame.at - last[key] >= SETTINGS.session_gap_s
            last[key] = frame.at
            assert frame.priority == "later"


NoticePush.TestCase.settings = settings(max_examples=600, stateful_step_count=50, deadline=None,
                                        suppress_health_check=[HealthCheck.too_slow])
TestNoticePushInvariants = NoticePush.TestCase


# --- one pass, any inputs ---------------------------------------------------------

session_ids = st.sampled_from(["a", "A", "b", "c", "lane", "conv"])
rows_strategy = st.lists(st.builds(
    lambda session, pid, alive, present, status, entry, started: registry.SessionRow(
        session_id=session, pid=pid, socket=f"/s/{pid}", name=None, cwd=None, started_at=started,
        alive=alive, socket_present=present, registry_path=f"/r/{pid}.json", entrypoint=entry,
        status=status, kind="interactive"),
    session_ids, st.integers(1, 6), st.booleans(), st.booleans(),
    st.sampled_from(["idle", "busy", None]), st.sampled_from(["claude-desktop", "sdk-cli", None]),
    st.floats(0, 10)), max_size=8)
pending_strategy = st.lists(st.builds(
    lambda i, session, age: notify_push.Pending(i, f"job-{i}", session, "t", 1000.0 - age),
    st.integers(1, 30), session_ids, st.floats(0, 200)),
    max_size=12, unique_by=lambda item: item.notice_id)


@given(pending=pending_strategy, rows=rows_strategy,
       recent=st.lists(st.floats(0, 120), max_size=4), per_minute=st.integers(1, 4))
@settings(max_examples=400, deadline=None)
def test_one_pass_addresses_each_session_once_through_its_speaking_row(pending, rows, recent, per_minute):
    """C-15.7, C-23.30: for any pending notices and any registry, each push goes
    to the row that speaks for its session (the best-ranked of the rows naming
    it), which is live, interactive, idle and has an inbox; no session gets two
    pushes, no notice is in two, and the pass stays inside the per-minute cap."""
    settings_ = notify_push.PushSettings(delay_s=5, max_age_s=150, per_minute=per_minute,
                                         session_gap_s=0, after_wait_s=0)
    history = notify_push.History(recent=[1000.0 - age for age in recent])
    plan = notify_push.plan(pending, rows, now=1000.0, settings=settings_, lane_ids=["LANE"],
                            conversation_ids=["conv"], history=history)
    sessions = [push.session_id.lower() for push in plan.pushes]
    assert len(sessions) == len(set(sessions))
    ids = [i for push in plan.pushes for i in push.notice_ids]
    assert len(ids) == len(set(ids))
    room = per_minute - sum(1 for age in recent if age < 60)
    assert len(plan.pushes) <= max(0, room)
    for push in plan.pushes:
        key = push.session_id.lower()
        assert key not in ("lane", "conv")
        naming = [row for row in rows if row.session_id.lower() == key]
        assert push.row.rank == max(row.rank for row in naming)
        assert notify_push.row_refusal(push.row) is None
        assert all(item.key == key for item in push.notices)
        assert all(1000.0 - item.created_at <= 150 for item in push.notices)
        assert max(1000.0 - item.created_at for item in push.notices) >= 5


# --- differential: the planner's recipient against v1's ranking (C-23.30) -----------

@given(entries=st.lists(st.tuples(st.sampled_from(["live", "dead"]), st.booleans(),
                                  st.integers(0, 3)), min_size=1, max_size=4))
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_planners_recipient_ranks_as_v1s_find_session_does(tmp_path_factory, monkeypatch, entries):
    """C-23.30 has two implementations: v1's `find_session` (kept for
    `push_to_session`) and the registry's `speaker`, which the planner uses.
    Over the same registry files they choose a row of the same rank: live pid,
    then a present socket, then the newest start."""
    home = tmp_path_factory.mktemp("claude")
    (home / "sessions").mkdir()
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home))
    import socket as socket_module
    import tempfile
    sockdir = tempfile.mkdtemp(prefix="sfd-", dir="/tmp")
    servers = []
    try:
        live_pids = [os.getpid(), os.getppid()]
        dead = 900000
        for index, (liveness, has_socket, started) in enumerate(entries):
            pid = live_pids.pop() if liveness == "live" and live_pids else dead + index
            path = None
            if has_socket:
                path = f"{sockdir}/{index}.sock"
                server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                server.bind(path)
                servers.append(server)
            (home / "sessions" / f"{pid}.json").write_text(json.dumps({
                "sessionId": "S", "pid": pid, "messagingSocketPath": path, "startedAt": started}))
        v1 = notify_push.find_session("S")
        chosen = notify_push.target_row(registry.rows(), "s")
        assert v1 is not None and chosen is not None
        v1_rank = (v1["alive"], v1["socket_present"], float(v1["started_at"] or 0))
        assert chosen.rank == v1_rank
    finally:
        for server in servers:
            server.close()
        import shutil
        shutil.rmtree(sockdir, ignore_errors=True)
