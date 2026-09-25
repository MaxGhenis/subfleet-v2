"""The conversation service's control-loop work, driven with a fake daemon object:
the catalog timer (C-30.1, D-23), compaction (C-25.4, IR-6), dispatch refusals and
backoff (C-26.1, C-24.3), worktree conversations (D-16, C-24.1), and the turn jobs
retention must keep (C-26.12, IR-17).

The fake daemon has a real job store (`state.sqlite3`) and a `submit` the test
controls; everything else is the real service and conversation store.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.conversations import catalog as catalog_module
from subfleet.conversations import service as service_module
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.store import Store

SETTINGS = {"model": "opus", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
POLICY = {"models": {"opus": {"provider": "claude", "id": "claude-opus-5-5"}},
          "caps": {"workspace_git_timeout_s": 30, "worktree_add_timeout_s": 60},
          "conversations": {"catalog_interval_s": 60, "compact_after_s": 0, "compact_per_tick": 20,
                            "approval_wait_s": 3600}}


class FakeDaemon:
    def __init__(self, root: Path):
        self.root = root
        self.log = logging.getLogger("test-conversations")
        self.store = Store(root / "state.sqlite3")
        self.store.put_lane(Lane("claude-1", "claude", "claude:one", Credential("claude", "TOKEN", "env"),
                                 None, LaneOwner.V2, False))
        self.policy = json.loads(json.dumps(POLICY))
        self.requests = None
        self.submits: list[protocol.SubmitArgs] = []
        self.refuse = None                  # an exception submit raises, or a callable deciding one
        self.after_insert = None            # called after the job row exists, before submit returns
        self.notified = 0

    def _notify(self):
        self.notified += 1

    def submit(self, args, *, turn=None):
        self.submits.append(args)
        refuse = self.refuse(args) if callable(self.refuse) else self.refuse
        if refuse is not None:
            raise refuse
        job_id = f"job-{len(self.submits)}"
        self.store.add_job(job_id=job_id, request_id=args.request_id, payload_digest=turn["digest"], kind="turn",
                           workdir=args.workdir, prompt_path=args.prompt_path, sandbox=args.sandbox, name=args.name,
                           in_place=1, max_attempts=1)
        if self.after_insert:
            self.after_insert(job_id)
        return {"job_id": job_id, "request_id": args.request_id, "created": True}


class FakeRunner:
    """What the service reads of a runner: whether it has finished."""

    def __init__(self, finished=False):
        self.finished = threading.Event()
        if finished:
            self.finished.set()

    def stop(self):
        pass


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    daemon = FakeDaemon(root)
    service = ConversationService(daemon)
    service.clock = Clock()
    workspace = tmp_path / "work"
    workspace.mkdir()
    service.test_workspace = str(workspace)
    try:
        yield service
    finally:
        service.close()
        daemon.store.close()


def conversation(service, **kw) -> str:
    base = dict(provider="claude", workspace=service.test_workspace, workspace_kind="in-place", settings=SETTINGS,
                origin="new")
    base.update(kw)
    return service.store.create_conversation(**base)[0]["conversation_id"]


def submit(service, cid, text="hello", after=None) -> str:
    mid = str(uuid.uuid4())
    service.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=after, text=text,
                                 attachments=[], settings=SETTINGS)
    return mid


# --- the catalog timer (C-30.1, design D-23) ------------------------------------


class FakeRun:
    """A catalog process that runs until `end` is called; waiting on it is a failure."""

    def __init__(self):
        self.pid = 4_000_000
        self.returncode = None

    def poll(self):
        return self.returncode

    def end(self, rc=0):
        self.returncode = rc

    def wait(self, timeout=None):
        raise AssertionError("the conversation tick waited for a catalog run")


@pytest.fixture
def runs(monkeypatch):
    started = []

    def spawn(root):
        if started and started[-1].returncode is None:
            return None                     # the running one holds the lock
        run = FakeRun()
        started.append(run)
        return run

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    monkeypatch.setattr(catalog_module, "refresh_running",
                        lambda root: bool(started) and started[-1].returncode is None)
    return started


def test_the_tick_starts_a_catalog_run_every_interval_and_never_waits_for_it(svc, runs):
    """C-30.1, D-23: the first tick starts a run; no second one while it runs or before
    `catalog_interval_s` has passed; the next starts once both hold; a tick returns at
    once whatever the run is doing."""
    svc._catalog_tick()
    assert len(runs) == 1
    started = time.monotonic()
    for _ in range(5):
        svc.tick()
    assert time.monotonic() - started < 5 and len(runs) == 1
    runs[0].end()
    svc.clock.now += 30
    svc._catalog_tick()
    assert len(runs) == 1 and svc._catalog_proc is None            # reaped, not yet due
    svc.clock.now += 31
    svc._catalog_tick()
    assert len(runs) == 2
    svc.daemon.policy["conversations"]["catalog_interval_s"] = 5    # the policy decides
    runs[1].end()
    svc.clock.now += 6
    svc._catalog_tick()
    assert len(runs) == 3


def test_with_the_timer_off_the_catalog_runs_only_on_request(svc, runs):
    """C-30.1: `conversations.catalog_interval_s: 0` starts no timed run; `catalog.refresh`
    still starts one, and the list judges age by the default interval."""
    svc.daemon.policy["conversations"]["catalog_interval_s"] = 0
    for _ in range(3):
        svc._catalog_tick()
        svc.clock.now += 1000
    assert runs == []
    assert svc.handle("catalog.refresh", {}, None)["requested"] is True and len(runs) == 1
    assert svc.handle("conversation.list", {}, None)["catalog"]["stale_after_s"] == 180


def test_a_wedged_catalog_run_is_stopped_and_reaped_later(svc, runs, monkeypatch):
    """C-30.1: a run alive past three times its own cap is killed by process group, once;
    a later tick reaps it and the timer carries on."""
    killed = []
    monkeypatch.setattr(service_module.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    svc._catalog_tick()
    svc.clock.now += service_module.CATALOG_KILL_AFTER_S - 1
    svc._catalog_tick()
    assert killed == []
    svc.clock.now += 2
    svc._catalog_tick()
    svc._catalog_tick()
    assert killed == [(runs[0].pid, service_module.signal.SIGKILL)]
    runs[0].end(-9)
    svc._catalog_tick()
    assert svc._catalog_proc is not None and len(runs) == 2         # reaped, and the next one due


def test_catalog_refresh_starts_a_run_without_waiting_and_resets_the_timer(svc, runs):
    """D-23, C-25.3: `catalog.refresh` starts a run and returns; a second request while it
    runs starts nothing; the timer counts from the request."""
    first = svc.handle("catalog.refresh", {}, None)
    assert first == {"requested": True, "running": True, "generated_at": None}
    again = svc.handle("catalog.refresh", {}, None)
    assert again["requested"] is False and again["running"] is True
    svc._catalog_tick()
    assert len(runs) == 1


def write_catalog(root: Path, generated_at: str, items=None):
    (root / "catalog.json").write_text(json.dumps({"generated_at": generated_at, "complete": True,
                                                   "items": items or []}))


def test_conversation_list_works_with_no_catalog_and_says_so(svc, runs):
    """C-30.1, D-23: with no `catalog.json` yet, the list still answers, with the
    conversations and a catalog marked absent."""
    cid = conversation(svc)
    out = svc.handle("conversation.list", {}, None)
    assert [c["conversation_id"] for c in out["conversations"]] == [cid]
    assert out["catalog"]["state"] == "absent" and out["catalog"]["items"] == []
    assert out["catalog"]["generated_at"] is None and out["catalog"]["refreshing"] is False


def test_conversation_list_reports_a_stale_or_damaged_catalog(svc, runs):
    """C-30.1: a catalog older than three intervals is `stale`, a damaged one
    `unreadable`; neither fails the op, and a fresh one is `fresh` with its age."""
    from datetime import UTC, datetime, timedelta
    item = {"provider": "claude", "native_session_id": "s-1", "title": "old work", "mtime": 1.0}
    old = (datetime.now(UTC) - timedelta(seconds=400)).isoformat(timespec="seconds").replace("+00:00", "Z")
    write_catalog(svc.root, old, [item, {"broken": True}, "not an item"])
    stale = svc.handle("conversation.list", {}, None)["catalog"]
    assert stale["state"] == "stale" and stale["age_s"] >= 400 and stale["stale_after_s"] == 180
    assert [i["native_session_id"] for i in stale["items"]] == ["s-1"]
    write_catalog(svc.root, datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"), [item])
    assert svc.handle("conversation.list", {}, None)["catalog"]["state"] == "fresh"
    (svc.root / "catalog.json").write_text("{not json")
    assert svc.handle("conversation.list", {}, None)["catalog"]["state"] == "unreadable"
    (svc.root / "catalog.json").write_text("[1, 2]")
    assert svc.handle("conversation.list", {}, None)["catalog"]["state"] == "unreadable"
    runs.append(FakeRun())
    assert svc.handle("conversation.list", {}, None)["catalog"]["refreshing"] is True


def test_a_conversation_continuing_a_session_open_elsewhere_says_so(svc, runs):
    """Design §12: a Claude session a live process outside Subfleet holds (the
    catalog's `live_elsewhere`) is flagged on the conversation continuing it, in
    the list and on open, so the app can say both write one history. The flag
    comes from the catalog file; nothing scans (D-23)."""
    from datetime import UTC, datetime
    live = conversation(svc, origin="native", native_session_id="s-live", title="open in the app")
    idle = conversation(svc, origin="native", native_session_id="s-idle", title="closed there")
    fresh = conversation(svc)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    write_catalog(svc.root, now, [
        {"provider": "claude", "native_session_id": "s-live", "mtime": 2.0, "live_elsewhere": True},
        {"provider": "claude", "native_session_id": "s-idle", "mtime": 1.0, "live_elsewhere": False},
        {"provider": "claude", "native_session_id": "s-other", "mtime": 3.0, "live_elsewhere": True}])
    out = svc.handle("conversation.list", {}, None)
    flags = {c["conversation_id"]: c["live_elsewhere"] for c in out["conversations"]}
    assert flags == {live: True, idle: False, fresh: False}
    assert "live_elsewhere" not in out["catalog"]
    assert [i["native_session_id"] for i in out["catalog"]["items"]] == ["s-other"]   # bound ones stay out
    assert svc.handle("conversation.open", {"conversation_id": live}, None)["conversation"]["live_elsewhere"] is True
    assert svc.handle("conversation.open", {"conversation_id": idle}, None)["conversation"]["live_elsewhere"] is False
    (svc.root / "catalog.json").unlink()
    assert svc.handle("conversation.open", {"conversation_id": live}, None)["conversation"]["live_elsewhere"] is False


def test_live_elsewhere_follows_every_view_and_an_old_catalog_says_nothing(svc, runs):
    """A settings change or an unblock returns the same flag as open; the
    catalog's recorded live set covers sessions it does not list; a stale
    catalog flags nothing."""
    from datetime import UTC, datetime, timedelta
    cid = conversation(svc, origin="native", native_session_id="s-born-here")
    (svc.root / "catalog.json").write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"), "complete": True,
        "items": [], "live_claude": ["s-born-here"]}))
    assert svc.handle("conversation.open", {"conversation_id": cid}, None)["conversation"]["live_elsewhere"] is True
    assert svc.handle("conversation.list", {}, None)["conversations"][0]["live_elsewhere"] is True
    changed = svc.handle("conversation.settings", {"conversation_id": cid, "settings": {**SETTINGS, "effort": None}},
                         None)["conversation"]
    assert changed["live_elsewhere"] is True
    old = (datetime.now(UTC) - timedelta(seconds=400)).isoformat(timespec="seconds").replace("+00:00", "Z")
    (svc.root / "catalog.json").write_text(json.dumps({"generated_at": old, "complete": True, "items": [],
                                                       "live_claude": ["s-born-here"]}))
    assert svc.handle("conversation.open", {"conversation_id": cid}, None)["conversation"]["live_elsewhere"] is False
    assert svc.handle("conversation.list", {}, None)["conversations"][0]["live_elsewhere"] is False


def test_a_registry_pid_holds_a_session_only_while_it_is_the_outside_process_that_wrote_it(monkeypatch):
    """C-26.3: a registry file can outlive its process and its pid be reused (the
    row's procStart then differs from the process's start); a Subfleet attempt's
    own Claude carries markers; neither holds a session. A row with no procStart
    needs a Claude executable. When ps cannot answer, the row holds."""
    import subprocess
    from subfleet.sessions import registry

    class Done:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc
    start = "Thu Sep 24 23:18:30 2026"
    procs = {10: (start, "/Applications/Claude.app/Contents/MacOS/claude", ""),
             11: ("Fri Sep 25 01:02:03 2026", "/Applications/Claude.app/Contents/MacOS/claude", ""),
             12: (start, "/Users/x/.local/bin/claude", "SUBFLEET_ATTEMPT=a1 SUBFLEET_JOB=j1"),
             13: (start, "/usr/sbin/cupsd", ""),
             14: (start, "/Users/x/.local/bin/claude", "")}

    def run(argv, **kw):
        pid = int(argv[2])
        if pid == 15:
            raise subprocess.TimeoutExpired(argv, 5)
        if pid not in procs:
            return Done("", 1)
        started, comm, env = procs[pid]
        if "lstart=,comm=" in argv:
            assert kw["env"]["TZ"] == "UTC"
            return Done(f"{started} {comm}\n")
        return Done(f"{comm} {env}\n")
    monkeypatch.setattr(catalog_module.subprocess, "run", run)

    def row(sid, pid, proc_start=start, alive=True):
        return registry.SessionRow(session_id=sid, pid=pid, socket=None, name=None, cwd=None, started_at=None,
                                   alive=alive, socket_present=False, registry_path=f"/x/{pid}.json",
                                   proc_start=proc_start)
    rows = [row("s-1", 10), row("s-1", 11), row("s-2", 12), row("s-3", 10, alive=False),
            row("s-4", 13, proc_start=None), row("s-5", 14, proc_start=None), row("s-6", 15), row("s-7", 16)]
    monkeypatch.setattr(registry, "rows", lambda: rows)
    assert catalog_module.external_writers("s-1") == [10]          # 11's start differs: a reused pid
    assert catalog_module.external_writers("s-2") == []            # Subfleet's own turn
    assert catalog_module.external_writers("s-3") == []            # its process is gone
    assert catalog_module.external_writers("s-4") == []            # no procStart, and not Claude
    assert catalog_module.external_writers("s-5") == [14]          # no procStart, a Claude executable
    assert catalog_module.external_writers("s-6") == [15]          # ps timed out: it holds
    assert catalog_module.external_writers("s-7") == []            # ps says no such process
    assert catalog_module._live_claude_sessions() == {"s-1", "s-5", "s-6"}


def test_a_real_catalog_run_writes_the_catalog_the_list_reads(svc, monkeypatch, tmp_path):
    """C-30.1, D-23: the tick's run is `python -m subfleet.conversations.catalog`, out of
    process; once it finishes the list reports a fresh catalog."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "-work"
    projects.mkdir(parents=True)
    (projects / "0f0e0d0c-1111-2222-3333-444455556666.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": str(tmp_path), "message": {"role": "user", "content": "index me"}}) + "\n")
    monkeypatch.setenv("HOME", str(home))
    svc._catalog_tick()
    assert svc._catalog_proc is not None
    deadline = time.monotonic() + 90
    while svc._catalog_proc is not None and time.monotonic() < deadline:
        time.sleep(0.1)
        svc._reap_catalog()
    assert svc._catalog_proc is None, "the catalog run did not finish"
    catalog = svc.handle("conversation.list", {}, None)["catalog"]
    assert catalog["state"] == "fresh" and catalog["complete"] is True
    assert [i["first_prompt"] for i in catalog["items"]] == ["index me"]


# --- compaction (C-25.4, review IR-6) --------------------------------------------


def turn_attempt(svc, message_id: str, *, state: str, n: int) -> str:
    job_id = f"turn-job-{n}"
    svc.daemon.store.add_job(job_id=job_id, request_id=f"turn:{message_id}:{n}", payload_digest="d", kind="turn",
                             workdir="/w", prompt_path="/p", sandbox="read-only", state="running")
    attempt_id = f"{job_id}/a1"
    svc.daemon.store.add_attempt(attempt_id=attempt_id, job_id=job_id, seq=1, lane_id="claude-1",
                                 model_requested="claude-opus-5-5", state=state)
    return attempt_id


def stream(svc, cid, message_id, attempt_id):
    svc.store.append_events(conversation_id=cid, message_id=message_id, attempt_id=attempt_id, stdout_offset=10,
                            stdin_seq=1, events=[("stdout", "1", 1, "text.delta", {"text": "a"}),
                                                 ("stdout", "2", 1, "text", {"text": "a"})])


def kinds(svc, cid, after=0):
    return [e["kind"] for e in svc.store.events_after(cid, after)["events"]]


def test_compaction_waits_for_the_attempt_to_end_and_the_message_to_settle(svc):
    """C-25.4, IR-6: deltas stay while the attempt is live (even with its message
    complete), while a runner still reads it, and while the message is live; they go
    once all three are over."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    attempt = turn_attempt(svc, mid, state="running", n=0)
    stream(svc, cid, mid, attempt)
    svc.store.set_state(mid, "running")
    svc._compact()
    assert kinds(svc, cid) == ["text.delta", "text"]
    svc.store.set_state(mid, "complete")
    svc._compact()                                   # the job store still says running
    assert kinds(svc, cid) == ["text.delta", "text"]
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (attempt,))
    runner = FakeRunner()
    svc.runners[attempt] = runner
    svc._compact()                                   # a live runner may still write
    assert kinds(svc, cid) == ["text.delta", "text"]
    runner.finished.set()
    svc._compact()
    assert kinds(svc, cid) == ["text"]
    assert svc.store.floor(cid) > 0 and svc.store.mark(attempt)["compacted"] == 1


def test_a_quarantined_attempt_is_not_compacted(svc):
    """IR-6: a quarantined attempt has not ended (its processes may live)."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    attempt = turn_attempt(svc, mid, state="quarantined", n=0)
    stream(svc, cid, mid, attempt)
    svc.store.set_state(mid, "failed")
    svc._compact()
    assert kinds(svc, cid) == ["text.delta", "text"]


def test_compaction_keeps_a_settled_turns_deltas_for_compact_after_s(svc):
    """C-25.4: a client still reading a turn that just settled is not reset; the delay is
    policy (`conversations.compact_after_s`), and each tick does at most
    `compact_per_tick` attempts."""
    svc.daemon.policy["conversations"].update(compact_after_s=3600, compact_per_tick=1)
    cid = conversation(svc)
    first = submit(svc, cid)
    second = submit(svc, cid, after=first)
    attempts = [turn_attempt(svc, m, state="succeeded", n=i) for i, m in enumerate((first, second))]
    for m, a in zip((first, second), attempts):
        stream(svc, cid, m, a)
        svc.store.set_state(m, "complete")
    svc._compact()
    assert kinds(svc, cid).count("text.delta") == 2
    svc.daemon.policy["conversations"]["compact_after_s"] = 0
    svc._compact()
    assert kinds(svc, cid).count("text.delta") == 1
    svc._compact()
    assert kinds(svc, cid).count("text.delta") == 0


def test_events_reset_a_client_whose_cursor_is_below_the_floor(svc):
    """C-25.4, IR-6: after compaction `conversation.events` says `reset` to a cursor
    below the floor and not to one at or above it."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    attempt = turn_attempt(svc, mid, state="succeeded", n=0)
    stream(svc, cid, mid, attempt)
    svc.store.set_state(mid, "complete")
    delta, text = [e["seq"] for e in svc.store.events_after(cid, 0)["events"]]
    svc._compact()
    assert svc.handle("conversation.events", {"conversation_id": cid, "after": delta - 1}, None)["reset"] is True
    at_floor = svc.handle("conversation.events", {"conversation_id": cid, "after": delta}, None)
    assert at_floor["reset"] is False and [e["seq"] for e in at_floor["events"]] == [text]


# --- dispatch refusals (C-26.1, C-24.3) ------------------------------------------


def test_a_refusal_that_may_pass_keeps_the_message_waiting_with_a_reason_and_a_backoff(svc):
    """C-26.1, C-24.3: an operational refusal (exit 1) before any provider saw the
    message leaves it queued with `deferred: <why>`; it is not submitted again every
    tick, but after 2 s, then 4 s, and goes on normally once submit accepts it."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.daemon.refuse = AdapterError("could not inspect the workdir: git rev-parse timed out after 60 s", code=1)
    svc._dispatch()
    message = svc.store.message(mid)
    assert message["state"] == "queued"
    assert message["state_reason"] == "deferred: could not inspect the workdir: git rev-parse timed out after 60 s"
    for _ in range(5):
        svc._dispatch()
    assert len(svc.daemon.submits) == 1
    svc.clock.now += 2.1
    svc._dispatch()
    assert len(svc.daemon.submits) == 2
    svc.clock.now += 3
    svc._dispatch()
    assert len(svc.daemon.submits) == 2                  # the second wait is 4 s
    svc.clock.now += 1.1
    svc.daemon.refuse = None
    svc._dispatch()
    message = svc.store.message(mid)
    assert len(svc.daemon.submits) == 3
    assert message["state"] == "waiting" and message["state_reason"] is None and message["job_id"] == "job-3"


def test_the_backoff_is_capped(svc):
    """C-26.1: the wait doubles up to five minutes."""
    cid = conversation(svc)
    submit(svc, cid)
    svc.daemon.refuse = OSError("disk full")
    for _ in range(12):
        svc._dispatch()
        svc.clock.now += service_module.DEFER_MAX_S + 0.1
    count, due = next(iter(svc._deferred.values()))
    assert count == 12 and due - svc.clock.now <= service_module.DEFER_MAX_S
    assert svc.store.message(next(iter(svc._deferred)))["state_reason"] == "deferred: OSError: disk full"


@pytest.mark.parametrize("refusal, reason", [
    (AdapterError("writable job refused on main", code=7), "not-delivered: writable job refused on main"),
    (protocol.ProtocolError("workdir must be a directory"), "not-delivered: workdir must be a directory"),
    (ConversationError("attachment-missing", "attachment abc is gone"), "not-delivered: attachment-missing"),
])
def test_a_permanent_refusal_fails_the_message_with_its_reason(svc, refusal, reason):
    """C-26.1, design §9: the daemon's own refusal of the message as it is (exit 2 or 7)
    fails it, never delivered, with the reason; it is not tried again."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.daemon.refuse = refusal
    svc._dispatch()
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"]) == ("failed", reason)
    svc.clock.now += 1000
    svc._dispatch()
    assert len(svc.daemon.submits) == 1


def test_an_unknown_model_fails_before_submit(svc):
    """C-26.8: a model this fleet does not route is refused before any job exists."""
    cid = conversation(svc)
    mid = str(uuid.uuid4())
    svc.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="x", attachments=[],
                             settings={**SETTINGS, "model": "gpt-9"})
    svc._dispatch()
    assert svc.store.message(mid)["state_reason"] == "not-delivered: unknown-model"
    assert svc.daemon.submits == []


def test_one_conversations_defect_never_holds_up_another(svc, caplog):
    """C-26.1: an unexpected error submitting one message defers that message (logged)
    and the next conversation's message is still submitted in the same pass."""
    a, b = conversation(svc), conversation(svc)
    first, second = submit(svc, a), submit(svc, b)
    svc.daemon.refuse = lambda args: KeyError("defect") if args.request_id.startswith(f"turn:{first}") else None
    with caplog.at_level(logging.ERROR, logger="test-conversations"):
        svc._dispatch()
    assert svc.store.message(first)["state_reason"].startswith("deferred: KeyError")
    assert svc.store.message(second)["state"] == "waiting"
    assert any("KeyError" in r.message for r in caplog.records)


def test_a_readmitted_message_is_deferred_and_submitted_again_as_waiting(svc):
    """IR-1, C-26.1: a waiting message with no job (re-admitted) that submit defers stays
    waiting, says why, and is submitted as turn_seq+1 later."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "waiting", reason="readmit:external-writer", turn_seq=1, job_id=None)
    svc.daemon.refuse = AdapterError("could not inspect the workdir", code=1)
    svc._dispatch()
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"]) == ("waiting", "deferred: could not inspect the workdir")
    svc.daemon.refuse = None
    svc.clock.now += 3
    svc._dispatch()
    message = svc.store.message(mid)
    assert message["state"] == "waiting" and message["job_id"] == "job-2"
    assert svc.daemon.submits[-1].request_id == f"turn:{mid}:1"


def test_a_message_withdrawn_while_its_job_is_submitted_leaves_no_live_job(svc):
    """IR-2, C-24.7: a person's cancel that lands between the job insert and the binding
    wins; the new job is cancelled before it has an attempt."""
    cid = conversation(svc)
    mid = submit(svc, cid)

    def cancel_now(job_id):
        receipt = svc.handle("message.cancel", {"message_id": mid}, None)
        assert receipt["state"] == "cancelled"
    svc.daemon.after_insert = cancel_now
    svc._dispatch()
    assert svc.store.message(mid)["state"] == "cancelled"
    job = svc.daemon.store.one("SELECT state, cancel_requested_at FROM jobs WHERE job_id='job-1'")
    assert job["state"] == "cancelled" and job["cancel_requested_at"]


def test_stopping_a_message_that_waits_to_be_submitted_again_withdraws_it(svc):
    """C-24.7: a re-admitted or deferred message with no job is withdrawn by a stop,
    and the dispatcher never submits it."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "waiting", reason="readmit:fast-unavailable", turn_seq=1, job_id=None)
    receipt = svc.handle("turn.interrupt", {"message_id": mid}, None)
    assert receipt["state"] == "cancelled"
    svc._dispatch()
    assert svc.daemon.submits == []
    other = submit(svc, conversation(svc))
    svc.store.set_state(other, "waiting", reason="readmit:external-writer", turn_seq=1, job_id=None)
    svc.store.update_message(other, stop_requested_at="2026-09-24T00:00:00.000Z")
    svc._dispatch()
    assert svc.store.message(other)["state"] == "cancelled" and svc.daemon.submits == []


def test_a_claude_turn_waits_while_another_process_holds_its_session(svc, monkeypatch):
    """C-26.3, design D-17: a live Claude process outside Subfleet holding the
    session is an admission wait `external-writer`, shown to the person, looked at
    again every few seconds without backoff; the turn goes once it is gone, and a
    stop withdraws it meanwhile. Queued follow-ups stay behind it."""
    held = {"s-held": [4242]}
    calls = []

    def writers(session_id):
        calls.append(session_id)
        return list(held.get(session_id, []))
    monkeypatch.setattr(catalog_module, "external_writers", writers)
    cid = conversation(svc, origin="native", native_session_id="s-held")
    mid = submit(svc, cid)
    follow = submit(svc, cid, text="and then", after=mid)
    svc._dispatch()
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"], message["job_id"]) == ("waiting", "external-writer: pid 4242", None)
    assert svc.daemon.submits == [] and svc.store.message(follow)["state"] == "queued"
    change = svc.handle("conversation.watch", {"after": 0}, None)["changes"][-1]
    assert (change["message_id"], change["state"], change["state_reason"]) == (mid, "waiting", "external-writer: pid 4242")
    svc.clock.now += 3
    svc._dispatch()
    assert calls == ["s-held"]                        # not looked at again before the recheck interval
    for _ in range(4):                                # a long hold never backs off past the interval
        svc.clock.now += service_module.EXTERNAL_WRITER_RECHECK_S
        svc._dispatch()
    assert len(calls) == 5 and svc.daemon.submits == []
    held.clear()
    svc.clock.now += service_module.EXTERNAL_WRITER_RECHECK_S
    svc._dispatch()
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"], message["job_id"]) == ("waiting", None, "job-1")
    assert svc.daemon.submits[-1].request_id == f"turn:{mid}:0"
    # A stop while held withdraws it; nothing is submitted.
    other = conversation(svc, origin="native", native_session_id="s-other")
    held["s-other"] = [77]
    waiting = submit(svc, other)
    svc._dispatch()
    assert svc.store.message(waiting)["state_reason"] == "external-writer: pid 77"
    assert svc.handle("turn.interrupt", {"message_id": waiting}, None)["state"] == "cancelled"
    svc.clock.now += service_module.EXTERNAL_WRITER_RECHECK_S
    svc._dispatch()
    assert [a.request_id for a in svc.daemon.submits] == [f"turn:{mid}:0"]
    # Codex threads and conversations with no session yet are not looked up.
    submit(svc, conversation(svc, provider="codex", origin="native", native_session_id="s-held",
                             settings={**SETTINGS, "model": "astra", "permission": "read-only"}))
    submit(svc, conversation(svc))
    before = len(calls)
    svc._dispatch()
    assert len(calls) == before


def test_a_queued_message_cancelled_before_dispatch_stays_cancelled(svc):
    """C-24.7: the ordinary withdrawal of a queued message."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    assert svc.handle("message.cancel", {"message_id": mid}, None)["state"] == "cancelled"
    svc._dispatch()
    assert svc.daemon.submits == []


# --- worktree conversations (D-16, D-25, C-24.1) ---------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                       "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "repo"
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "file.txt").write_text("base\n")
    for argv in (["init", "-b", "feature/x"], ["add", "."], ["commit", "-m", "base"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)
    return path


def git(path, *argv) -> str:
    return subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True, text=True).stdout.strip()


def create_worktree(svc, workspace, request_id="req-1"):
    return svc.handle("conversation.create", {"provider": "claude", "request_id": request_id,
                                              "workspace": str(workspace), "workspace_kind": "worktree",
                                              "settings": SETTINGS}, None)


def test_a_worktree_conversation_records_and_returns_its_path_and_branch(svc, repo):
    """D-16, D-25, C-24.1: the worktree is cut on its own branch under the state root;
    the conversation's workspace moves there through the store's update (the change
    feed sees it), and the result carries path, branch, source and base."""
    start = svc.store.changes_after(0)["next"]
    out = create_worktree(svc, repo)
    view = out["conversation"]
    cid = view["conversation_id"]
    worktree = view["worktree"]
    expected = svc.root.resolve() / "worktrees" / f"conversation-{cid}"
    assert out["created"] is True
    assert Path(worktree["path"]).resolve() == expected and view["workspace"] == worktree["path"]
    assert worktree["branch"] == f"subfleet/{cid}"
    assert worktree["source"] == os.path.realpath(repo) and worktree["base"] == git(repo, "rev-parse", "HEAD")
    assert git(expected, "symbolic-ref", "--short", "HEAD") == f"subfleet/{cid}"
    assert svc.store.conversation(cid)["worktree"] == worktree
    assert len(svc.store.changes_after(start)["changes"]) == 2       # the row, then its worktree
    listed = svc.handle("conversation.list", {"include_catalog": False}, None)["conversations"][0]
    assert listed["worktree"] == worktree

    again = create_worktree(svc, repo)
    assert again["created"] is False and again["conversation"]["worktree"] == worktree
    assert len([l for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]) == 2


def test_a_worktree_started_in_a_subdirectory_works_in_it(svc, repo):
    """D-16: the conversation works in the same subdirectory of its own worktree."""
    view = create_worktree(svc, repo / "pkg")["conversation"]
    assert view["workspace"] == str(Path(view["worktree"]["path"]) / "pkg")
    assert view["worktree"]["repository"] == os.path.realpath(repo)


def test_a_worktree_cut_short_is_finished_by_repeating_the_create(svc, repo, monkeypatch):
    """D-16: a create interrupted after `git worktree add` (here the record's write fails)
    leaves no usable conversation; repeating it with the same request id adopts the
    worktree it already added instead of failing on it."""
    original = svc.store.update_conversation
    monkeypatch.setattr(svc.store, "update_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        create_worktree(svc, repo)
    row = svc.store.one("SELECT conversation_id FROM conversations")
    cid = row["conversation_id"]
    assert svc.store.conversation(cid)["worktree"] is None
    with pytest.raises(ConversationError) as err:
        svc.handle("message.submit", {"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "hi"}, None)
    assert err.value.reason == "worktree-missing"
    monkeypatch.setattr(svc.store, "update_conversation", original)
    out = create_worktree(svc, repo)
    assert out["created"] is False and out["conversation"]["worktree"]["branch"] == f"subfleet/{cid}"


def test_a_worktree_conversation_without_its_worktree_never_runs_in_the_source(svc, repo):
    """D-16: a queued message of a worktree conversation whose worktree was never
    recorded is deferred, not submitted in the checkout it was to be cut from."""
    cid = conversation(svc, workspace=str(repo), workspace_kind="worktree")
    mid = submit(svc, cid)
    svc._dispatch()
    message = svc.store.message(mid)
    assert message["state"] == "queued" and message["state_reason"].startswith("deferred: the conversation's worktree")
    assert svc.daemon.submits == []


def test_a_worktree_needs_a_repository_and_creates_nothing_without_one(svc, tmp_path):
    """D-16: outside git the create is refused and no conversation is left behind."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConversationError) as err:
        create_worktree(svc, plain)
    assert err.value.reason == "not-a-repository"
    assert svc.store.one("SELECT COUNT(*) n FROM conversations")["n"] == 0


def test_conversation_create_runs_on_the_file_pool(svc):
    """C-25.3: the op that may run `git worktree add` never holds the request pool."""
    assert svc.pool_for("conversation.create") is svc.files


# --- turn jobs retention must keep (C-26.12, IR-17) -------------------------------


def test_retention_pins_name_the_turn_jobs_a_conversation_still_needs(svc):
    """C-26.12, IR-17: every turn job of a message that is not terminal (all its
    `turn:<id>:<n>` jobs), of a blocked conversation, and of a live runner; none of a
    settled message in an unblocked conversation."""
    store = svc.daemon.store

    def job(job_id, request_id, name):
        store.add_job(job_id=job_id, request_id=request_id, payload_digest="d", kind="turn", workdir="/w",
                      prompt_path="/p", sandbox="read-only", state="failed", name=name)

    live_cv, blocked_cv, done_cv = conversation(svc), conversation(svc), conversation(svc)
    live = submit(svc, live_cv)
    job("live-0", f"turn:{live}:0", f"turn-{live_cv}")
    job("live-1", f"turn:{live}:1", f"turn-{live_cv}")
    svc.store.set_state(live, "waiting", job_id="live-1", turn_seq=1)
    settled = submit(svc, blocked_cv)
    job("blocked-0", f"turn:{settled}:0", f"turn-{blocked_cv}")
    svc.store.set_state(settled, "failed")
    svc.store.update_conversation(blocked_cv, blocked_by="unfinished-turn")
    done = submit(svc, done_cv)
    job("done-0", f"turn:{done}:0", f"turn-{done_cv}")
    job("runner-0", f"turn:{uuid.uuid4()}:0", f"turn-{done_cv}")
    svc.store.set_state(done, "complete")
    svc.runners["runner-0/a1"] = FakeRunner()
    svc.runners["done-0/a1"] = FakeRunner(finished=True)
    assert svc.retention_pins() == {"live-0", "live-1", "blocked-0", "runner-0"}


def test_finished_runners_are_forgotten_only_after_their_attempt_ends(svc):
    """IR-17: a finished runner stays known while the job store still calls its attempt
    live (so it is not adopted twice), and is dropped once the attempt has ended."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    attempt = turn_attempt(svc, mid, state="finalizing", n=0)
    svc.runners[attempt] = FakeRunner(finished=True)
    svc._reap_runners()
    assert attempt in svc.runners
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (attempt,))
    svc._reap_runners()
    assert attempt not in svc.runners


# --- a limited turn's continuation (C-26.7) ---------------------------------------


class EndedRunner(FakeRunner):
    def __init__(self, adir: Path, message_id: str, conversation_id: str):
        super().__init__(finished=True)
        self.adir, self.message_id, self.conversation_id = adir, message_id, conversation_id
        self.attempt_id = "turn-job-0/a1"
        self.attempt = {"attempt_id": self.attempt_id, "lane_id": "claude-1"}


def test_another_writer_at_launch_is_decided_once_and_never_uses_up_readmissions(svc, tmp_path, monkeypatch):
    """C-26.3: the launch looks again (a job can wait in admission while the
    Claude app takes the session), records its answer for a replay, and an
    `external-writer` end waits again however many times it happens; a Codex
    one is spaced out, since only a provider start can see it."""
    looked = []
    monkeypatch.setattr(catalog_module, "external_writers", lambda sid: looked.append(sid) or [4242])
    adir = tmp_path / "a1"
    adir.mkdir()
    turn = {"provider": "claude", "native_session_id": "s-1"}
    assert svc._writer_check(turn, adir) == [4242] and svc._writer_check(turn, adir) == [4242]
    assert looked == ["s-1"]                                        # recorded, not asked again
    fresh = tmp_path / "a2"
    fresh.mkdir()
    (fresh / "stdin.jsonl").write_text("")
    assert svc._writer_check(turn, fresh) == []                     # already writing: a replay, left alone
    assert svc._writer_check({"provider": "codex", "native_session_id": "t"}, tmp_path) == []
    cid = conversation(svc, origin="native", native_session_id="s-1")
    mid = submit(svc, cid)
    svc.store.set_state(mid, "running", turn_seq=service_module.MAX_READMITS + 2)
    end = tmp_path / "a3"
    end.mkdir()
    (end / "turn.json").write_text(json.dumps({"state": "failed", "reason": "external-writer", "served": {},
                                               "user_frame_written": False, "accepted": False}))
    svc._on_outcome(EndedRunner(end, mid, cid))
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"]) == ("waiting", "readmit:external-writer")
    assert message["turn_seq"] == service_module.MAX_READMITS + 3


def test_a_limited_turns_continuation_exists_before_the_failure_is_visible(svc, tmp_path, monkeypatch):
    """C-26.7, D-6: whoever sees the limited message failed also sees its continuation,
    and settling the same outcome again (a replay) adds no second one."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "running")
    adir = tmp_path / "a1"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "failed", "reason": "limited", "served": {},
                                                "user_frame_written": True, "accepted": True}))
    runner = EndedRunner(adir, mid, cid)
    original = svc.store.set_state
    seen = []

    def watched(message_id, state, **kw):
        if message_id == mid and state == "failed":
            seen.append(svc.store.one("SELECT COUNT(*) n FROM messages WHERE continues=?", (mid,))["n"])
        return original(message_id, state, **kw)
    monkeypatch.setattr(svc.store, "set_state", watched)
    svc._on_outcome(runner)
    assert seen == [1]
    assert svc.store.message(mid)["state_reason"] == "limited"
    svc._on_outcome(runner)
    follow = svc.store.query("SELECT origin, state FROM messages WHERE continues=?", (mid,))
    assert follow == [{"origin": "failover", "state": "queued"}]


def test_the_catalog_is_parsed_once_per_version_of_the_file(svc, runs, monkeypatch):
    """D-23: the app lists every 30 s and opens read the live set too; parsing a
    ~3 MB catalog per call fed the collector passes that stalled the daemon
    under swap (2026-09-25). One parse per version of the file; a rewrite, even
    at the same size, is read again."""
    from datetime import UTC, datetime
    parses = []
    real = catalog_module.json.loads
    monkeypatch.setattr(catalog_module.json, "loads", lambda text: parses.append(1) or real(text))
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    write_catalog(svc.root, now, [{"provider": "claude", "native_session_id": "s-1", "mtime": 1.0}])
    for _ in range(3):
        svc.handle("conversation.list", {}, None)
    assert len(parses) == 1
    write_catalog(svc.root, now, [{"provider": "claude", "native_session_id": "s-2", "mtime": 1.0}])
    assert [i["native_session_id"] for i in svc.handle("conversation.list", {}, None)["catalog"]["items"]] == ["s-2"]
    assert len(parses) == 2


def test_a_conversation_lists_the_runs_its_turns_dispatched(svc):
    """Design §12: a turn's `subfleet run` records the turn's session as its
    caller; the conversation shows those jobs with the lane and model each
    landed on, newest first, and leaves out turn jobs and other callers'."""
    store = svc.daemon.store
    cid = conversation(svc, origin="native", native_session_id="s-conv")
    assert svc.handle("conversation.runs", {"conversation_id": conversation(svc)}, None) == {"runs": []}

    def job(job_id, caller, created, kind="dispatch", state="running", **extra):
        store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind=kind, workdir="/w",
                      prompt_path="/p", sandbox="read-only", name=job_id, caller_session=caller, state=state,
                      created_at=created, task=extra.get("task"), tier=extra.get("tier"))
    job("j-old", "s-conv", "2026-09-25T01:00:00Z", state="succeeded", task="review", tier="standard")
    job("j-new", "s-conv", "2026-09-25T02:00:00Z", task="build", tier="hard")
    job("j-turn", "s-conv", "2026-09-25T03:00:00Z", kind="turn")
    job("j-other", "s-else", "2026-09-25T04:00:00Z")
    store.put_lane(Lane("codex-2", "codex", "codex:two", Credential("codex", "/h", "home"), "/h", LaneOwner.V2, False))
    store.add_attempt(attempt_id="j-new/a1", job_id="j-new", seq=1, lane_id="claude-1", model_requested="claude-opus-5-5",
                      state="failed")
    store.add_attempt(attempt_id="j-new/a2", job_id="j-new", seq=2, lane_id="codex-2", model_requested="gpt-6-astra",
                      model_served="gpt-6-astra", state="running")
    runs = svc.handle("conversation.runs", {"conversation_id": cid}, None)["runs"]
    assert [r["job_id"] for r in runs] == ["j-new", "j-old"]
    new = runs[0]
    assert (new["lane_id"], new["model_served"], new["attempt_state"], new["attempts"], new["task"], new["tier"]) == (
        "codex-2", "gpt-6-astra", "running", 2, "build", "hard")
    assert runs[1]["lane_id"] is None and runs[1]["attempts"] == 0
