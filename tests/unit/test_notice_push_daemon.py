"""C-15.7, C-23.50: the daemon's notice push, against a real inbox socket.

An in-process daemon finishes a job for a session whose registry row points at
a stand-in inbox (a unix socket this test serves). The pass (`_push_notices`)
runs as the control loop runs it; what reaches the socket, what the store
records, and when the push stands aside are checked here. The same pass from
a spawned daemon's own control loop is `tests/fake/test_notice_push.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import protocol
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon

SESSION = "0fc08bc0-da8d-426a-8f51-5dfde7e6823a"


class Inbox:
    """A stand-in session inbox: records every line of every connection."""

    def __init__(self, path: Path):
        self.path = str(path)
        self.connections: list[list[dict]] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.server.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            lines: list[dict] = []
            with conn, conn.makefile("rb") as stream:
                for line in stream:
                    if line.strip():
                        lines.append(json.loads(line))
            self.connections.append(lines)

    @property
    def frames(self) -> list[dict]:
        return [line for lines in self.connections for line in lines if line.get("type") == "user"]

    def wait_for(self, count: int, timeout: float = 5.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.frames) < count:
            time.sleep(0.01)
        return self.frames

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.server.close()


@pytest.fixture
def short_dir():
    """Unix socket paths are capped near 104 bytes; pytest's `tmp_path` is past it."""
    path = Path(tempfile.mkdtemp(prefix="sfnp-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def claude(short_dir, monkeypatch):
    """A `~/.claude` holding one idle desktop session, `SESSION`, whose process
    is this test's own (so it is live) and whose inbox is `Inbox`."""
    home = short_dir / "claude"
    (home / "sessions").mkdir(parents=True)
    projects = home / "projects" / "-repo"
    projects.mkdir(parents=True)
    (projects / f"{SESSION}.jsonl").write_text(json.dumps(
        {"type": "user", "permissionMode": "bypassPermissions"}) + "\n")
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home))
    monkeypatch.delenv("SUBFLEET_NOTIFY_MODE", raising=False)
    inbox = Inbox(short_dir / "inbox.sock")
    state = {"home": home, "inbox": inbox}

    def register(*, status="idle", entrypoint="claude-desktop", socket_path=None, session=SESSION):
        pid = os.getpid()
        (home / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session, "cwd": "/repo", "startedAt": 1790995600612,
            "kind": "interactive", "entrypoint": entrypoint, "status": status,
            "messagingSocketPath": socket_path or inbox.path, "name": "a session"}))
        (home / "sessions" / f"{pid}.abc.key").write_text(json.dumps({"peerToken": "peer-tok"}))

    state["register"] = register
    register()
    yield state
    inbox.close()


@pytest.fixture
def core(tmp_path, claude):
    daemon = Daemon(tmp_path / "state")
    home = tmp_path / "home"
    daemon.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                               str(home), LaneOwner.V2, False))
    daemon.policy["notices"] = {**daemon.policy["notices"], "push_delay_s": 0}
    daemon.test_root = tmp_path
    yield daemon
    daemon.close()


def finish(core, job_id: str, *, session: str = SESSION, state: str = "succeeded") -> int:
    """A job of `session` that has ended, with its C-15.1 notice."""
    core.store.add_job(job_id=job_id, request_id="r-" + job_id, payload_digest="d", kind="run",
                       state="running", workdir=str(core.test_root), prompt_path=str(core.test_root / "p.md"),
                       sandbox="read-only", caller_session=session)
    with core.store.transaction("test.ended", job_id=job_id) as tx:
        tx.execute("UPDATE jobs SET state=?,rc=0,finished_at=? WHERE job_id=?",
                   (state, "2026-10-03T03:00:00Z", job_id))
        core._notice(tx, {"job_id": job_id}, "attempt a1: ok, rc=0: done")
    return core.store.one("SELECT notice_id FROM notices WHERE job_id=?", (job_id,))["notice_id"]


def notice(core, notice_id: int) -> dict:
    return dict(core.store.one("SELECT * FROM notices WHERE notice_id=?", (notice_id,)))


def events(core, kind: str) -> list[dict]:
    return [json.loads(row["data_json"]) for row in core.store.query(
        "SELECT data_json FROM events WHERE kind=? ORDER BY event_id", (kind,))]


def fresh_registry(core) -> None:
    """The pass reuses a registry read for two seconds; a test that changes the
    registry asks for a new read."""
    core._registry = (0.0, None)


def test_a_finished_job_wakes_its_idle_session_once(core, claude):
    """C-15.7: the pass finds the session by id in the registry, writes the peer
    token and one `later` frame naming the session, records `offered`/`socket`
    and the push's events, and never pushes the same notice again."""
    job = "20261003-030000-review"
    notice_id = finish(core, job)
    core._push_notices()
    frame, = claude["inbox"].wait_for(1)
    assert claude["inbox"].connections[0][0] == {"type": "auth", "token": "peer-tok"}
    assert frame["priority"] == "later" and frame["session_id"] == SESSION
    content = frame["message"]["content"]
    assert content.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">')
    assert f"{job}: succeeded; rc=0; deliverable=-; out=-" in content
    assert "attempt a1: ok, rc=0: done" in content
    row = notice(core, notice_id)
    assert (row["state"], row["transport"]) == ("offered", "socket") and row["offered_at"]
    reserved, = events(core, "notice.push")
    assert reserved["notice_ids"] == [notice_id] and reserved["pid"] == os.getpid()
    assert reserved["uuid"] == frame["uuid"]
    pushed, = events(core, "notice.pushed")
    assert pushed["job_ids"] == [job]
    status = core._notice_push_status()
    assert status["enabled"] and status["pushed"] == 1 and status["last_push"]["result"] == "delivered"

    core._push_history.last_push.clear()          # no gap: only the state may stop it
    core._push_notices()
    time.sleep(0.2)
    assert len(claude["inbox"].frames) == 1, "a notice is pushed at most once"


def test_the_hooks_still_surface_a_pushed_notice(core, claude):
    """C-15.3: a push stops at `offered`; `notice.pending` still returns the
    notice, so the next SessionStart or UserPromptSubmit hook shows it."""
    notice_id = finish(core, "20261003-030000-hooked")
    core._push_notices()
    claude["inbox"].wait_for(1)
    rows = core.dispatch("notice.pending", {"session_id": SESSION})["notices"]
    assert [(row["notice_id"], row["state"]) for row in rows] == [(notice_id, "offered")]


def test_a_busy_session_is_pushed_when_it_is_idle_again(core, claude):
    """C-15.7: a session in a turn is deferred, not skipped."""
    claude["register"](status="busy")
    notice_id = finish(core, "20261003-030000-busy")
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    assert notice(core, notice_id)["state"] == "pending"
    assert core._notice_push_status()["held"] == {SESSION: "busy"}
    claude["register"](status="idle")
    fresh_registry(core)
    core._push_notices()
    assert len(claude["inbox"].wait_for(1)) == 1


def test_lane_session_is_never_pushed(core, claude):
    """C-23.31: a session a lane attempt ran under is a lane's, whatever its
    registry row says; its job's notice is left to its orchestrator."""
    job = "20261003-030000-orchestrated"
    finish(core, job)
    core.store.add_attempt(attempt_id="a-lane", job_id=job, seq=1, lane_id="codex-1",
                           model_requested="astra", native_session_id=SESSION)
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    assert core._notice_push_status()["held"] == {SESSION: "lane session"}


def test_a_headless_run_is_never_pushed(core, claude):
    """C-15.7: an `sdk-*` row is a headless run (a lane or a conversation turn)."""
    claude["register"](entrypoint="sdk-cli")
    finish(core, "20261003-030000-headless")
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []


def test_push_skipped_while_a_waiter_is_live_and_after_a_wait_reported_the_job(core, claude):
    """C-23.50: a registered waiter, and then the `wait` answer that reported
    the job's end, each hold the push; once `push_after_wait_s` has passed with
    the notice still unacknowledged, the push goes."""
    job = "20261003-030000-waited"
    finish(core, job)
    with core.wait_hub.watching([job]):
        core._push_notices()
        assert core._notice_push_status()["held"] == {SESSION: "waiter live"}
    answer = core.wait(protocol.WaitArgs(job_ids=[job], deadline_s=1))
    assert answer["jobs"][0]["state"] == "succeeded"
    core._push_notices()
    assert core._notice_push_status()["held"] == {SESSION: "waiter reported"}
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    core.policy["notices"] = {**core.policy["notices"], "push_after_wait_s": 0}
    core._push_notices()
    assert len(claude["inbox"].wait_for(1)) == 1


def test_a_notice_the_session_acknowledged_is_never_pushed(core, claude):
    """C-23.50: `wait` acknowledges what it printed (`cli._ack_waited`), and an
    acknowledged notice is not `pending`."""
    notice_id = finish(core, "20261003-030000-acked")
    core.dispatch("notice.ack", {"session_id": SESSION, "notice_ids": [notice_id]})
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []


def test_a_refused_connection_gives_the_notice_back_until_its_tries_are_spent(core, claude, short_dir):
    """C-15.7: a connection refused wrote nothing, so the notice goes back to
    `pending` with a `notice.push_failed` event, is tried again after
    `push_retry_s`, and is left to the hooks after `push_max_tries`."""
    dangling = short_dir / "gone.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(dangling))
    server.close()
    claude["register"](socket_path=str(dangling))
    core.policy["notices"] = {**core.policy["notices"], "push_retry_s": 0, "push_max_tries": 2}
    notice_id = finish(core, "20261003-030000-refused")
    for _ in range(3):
        core._push_notices()
    row = notice(core, notice_id)
    assert (row["state"], row["transport"], row["offered_at"]) == ("pending", None, None)
    assert len(events(core, "notice.push_failed")) == 2
    status = core._notice_push_status()
    assert status["failed"] == 2 and status["held"] == {SESSION: "gave up"}


def test_the_push_switched_off_pushes_nothing(core, claude):
    core.policy["notices"] = {**core.policy["notices"], "push": False}
    finish(core, "20261003-030000-off")
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    assert core._notice_push_status()["enabled"] is False


def test_a_backlog_older_than_the_max_age_stays_asleep(core, claude):
    """C-15.7: an install never wakes every session a past job finished for."""
    notice_id = finish(core, "20261003-030000-old")
    with core.store.transaction("test.aged") as tx:
        tx.execute("UPDATE notices SET created_at='2026-10-01T00:00:00Z' WHERE notice_id=?", (notice_id,))
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []


def test_several_jobs_of_one_session_go_in_one_push(core, claude):
    """C-15.7 coalescing: one message for every pending notice of a session."""
    first = finish(core, "20261003-030000-one")
    second = finish(core, "20261003-030000-two")
    core._push_notices()
    frame, = claude["inbox"].wait_for(1)
    content = frame["message"]["content"]
    assert "2 detached runs" in content and "-one:" in content and "-two:" in content
    assert events(core, "notice.push")[0]["notice_ids"] == [first, second]


def test_daemon_status_reports_the_push(core, claude):
    status = core.dispatch("daemon.status", {})
    assert status["notice_push"]["enabled"] is True
    assert {"passes", "pushed", "failed", "uncertain", "raced", "held"} <= set(status["notice_push"])


def test_the_wait_hub_names_the_jobs_it_watches(core):
    """C-23.50 reads `WaitHub.watched_jobs`: every job a waiter is registered for."""
    assert core.wait_hub.watched_jobs() == frozenset()
    with core.wait_hub.watching(["a", "b"]), core.wait_hub.watching(["b", "c"]):
        assert core.wait_hub.watched_jobs() == frozenset({"a", "b", "c"})
    assert core.wait_hub.watched_jobs() == frozenset()


def test_a_notice_a_hook_reached_after_the_plan_is_not_written(core, claude):
    """C-15.7 at most once across layers: the reservation takes only notices
    still `pending`. Here a UserPromptSubmit hook surfaces one of the session's
    two notices between the plan and the reservation; only the other is
    written, and the surfaced one keeps the hook's state and transport."""
    first = finish(core, "20261003-030000-raced")
    second = finish(core, "20261003-030000-kept")
    reserve = core._push_reserve

    def hook_then_reserve(push, data):
        core.dispatch("notice.mark", {"session_id": SESSION, "notice_ids": [first],
                                      "state": "surfaced", "transport": "hook:UserPromptSubmit"})
        return reserve(push, data)

    core._push_reserve = hook_then_reserve
    core._push_notices()
    frame, = claude["inbox"].wait_for(1)
    assert "-kept:" in frame["message"]["content"] and "-raced:" not in frame["message"]["content"]
    assert (notice(core, first)["state"], notice(core, first)["transport"]) == ("surfaced", "hook:UserPromptSubmit")
    assert notice(core, second)["state"] == "offered"
    assert events(core, "notice.push")[0]["notice_ids"] == [second]


def test_an_unchanged_pass_reads_no_registry_and_no_attempts(core, claude, monkeypatch):
    """Review of PR #114: a notice held for hours (its session closed) must not
    cost a `ps` and two attempt scans every two seconds. A pass whose inputs
    have not changed since a full pass that pushed nothing plans nothing; a row
    file that changes, or `PUSH_REPLAN_S`, brings the full pass back."""
    import subfleet.daemon as daemon_module
    claude["register"](status="busy")
    finish(core, "20261003-030000-quiet")
    reads = []
    lanes = core._lane_session_ids
    monkeypatch.setattr(core, "_lane_session_ids", lambda: reads.append("lanes") or lanes())
    fresh_registry(core)
    core._push_notices()                          # full: held busy, and its plan is kept
    assert core._notice_push_status()["held"] == {SESSION: "busy"}
    registry_reads = []
    read = core._registry_read
    monkeypatch.setattr(core, "_registry_read", lambda: registry_reads.append(1) or read())
    core._push_notices()
    core._push_notices()
    assert registry_reads == [], "an unchanged pass reads no registry"
    claude["register"](status="idle")              # the row file changes
    os.utime(claude["home"] / "sessions" / f"{os.getpid()}.json",
             ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    fresh_registry(core)
    core._push_notices()
    assert registry_reads and len(claude["inbox"].wait_for(1)) == 1
    monkeypatch.setattr(daemon_module, "PUSH_REPLAN_S", 0)
    assert core._push_seen is None, "a pass that pushed keeps no plan"


def test_a_plan_made_from_a_stale_registry_read_is_not_kept(core, claude):
    """C-15.7: the registry read is reused for two seconds. A pass that sees the
    row file change but plans from a read that began before it (still `busy`)
    keeps no plan, so the next pass, with a fresh read, pushes at once instead
    of skipping for `PUSH_REPLAN_S`."""
    claude["register"](status="busy")
    finish(core, "20261003-030000-stale")
    fresh_registry(core)
    core._push_notices()                          # full pass, busy, plan kept
    claude["register"](status="idle")
    os.utime(claude["home"] / "sessions" / f"{os.getpid()}.json",
             ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    core._registry = (core._registry[0], {**core._registry[1], "finished": time.monotonic()})
    core._push_notices()                          # inputs changed, read reused: still busy
    assert core._notice_push_status()["held"] == {SESSION: "busy"}
    assert core._push_seen is None
    fresh_registry(core)
    core._push_notices()
    assert len(claude["inbox"].wait_for(1)) == 1


def test_a_notice_whose_id_now_names_another_notice_is_not_written(core, claude):
    """Review of PR #114: SQLite reuses a deleted rowid. If the planned notice is
    gone and its id now names a newer notice (another session's), the
    reservation takes nothing, and nothing is written."""
    notice_id = finish(core, "20261003-030000-reused")
    reserve = core._push_reserve

    def reuse_then_reserve(push, data):
        with core.store.transaction("test.reused") as tx:
            tx.execute("UPDATE notices SET session_id='someone-else',created_at='2026-10-03T23:59:59Z' "
                       "WHERE notice_id=?", (notice_id,))
        return reserve(push, data)

    core._push_reserve = reuse_then_reserve
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    assert notice(core, notice_id)["state"] == "pending"
    assert core._notice_push_status()["raced"] == 1


@pytest.mark.parametrize("became", ["conversation", "lane"])
def test_a_session_that_becomes_a_conversation_or_lane_before_the_write_is_held(
        core, claude, monkeypatch, became):
    """Review of PR #114: the plan read the lane and conversation records; a
    conversation can bind the session, or a lane attempt record it, before the
    write. The check just before the push asks the daemon again."""
    from subfleet import notify_push
    job = "20261003-030000-" + became
    notice_id = finish(core, job)
    recheck = notify_push.recheck

    def bind_then_recheck(push):
        if became == "conversation":
            monkeypatch.setattr(core, "_conversation_binding", lambda session: "conversation c-1")
        else:
            core.store.add_attempt(attempt_id="a-race", job_id=job, seq=1, lane_id="codex-1",
                                   model_requested="astra", native_session_id=SESSION.upper())
        return recheck(push)

    monkeypatch.setattr(notify_push, "recheck", bind_then_recheck)
    core._push_notices()
    time.sleep(0.2)
    assert claude["inbox"].frames == []
    assert notice(core, notice_id)["state"] == "pending"
    assert core._notice_push_status()["held"][SESSION] == f"{became} session"
