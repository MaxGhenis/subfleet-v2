"""The conversation service's control-loop work, driven with a fake daemon object:
the catalog timer (C-30.1, D-23), compaction (C-25.4, IR-6), dispatch refusals and
backoff (C-26.1, C-24.3), worktree conversations (D-16, C-24.1), the turn jobs
retention must keep (C-26.12, IR-17), and what close() leaves running: file ops and
turn runners (C-25.3, C-26.6).

The fake daemon has a real job store (`state.sqlite3`) and a `submit` the test
controls; everything else is the real service and conversation store.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

import hypothesis
import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.conversations import attachments as attachment_module
from subfleet.conversations import catalog as catalog_module
from subfleet.conversations import service as service_module
from subfleet.conversations import store as store_module
from subfleet.conversations.claude_turn import INIT_REQUEST_ID
from subfleet.conversations.launch import TURN_MANIFEST_KEY
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.store import Store
from tests import spellings
from tests.nonblocking import run_child
from tests.unit.test_conversation_handoff import handoff, world  # noqa: F401 (a fixture)
from tests.unit.test_conversation_handoff import source as handoff_source

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
    """What the service reads of a runner: whether it has finished. It has no thread,
    so close() has nothing to wait for."""

    attempt_id = "fake/a1"

    def __init__(self, finished=False):
        self.finished = threading.Event()
        if finished:
            self.finished.set()

    def stop(self):
        pass

    def join(self, timeout):
        return True


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
    """A catalog process that runs until `end` is called or a signal ends it. Waiting
    on one nothing has signalled is a failure: only close() may wait for a run."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.signals: list[int] = []
        self.stubborn = False               # signals reach it but do not end it
        self.fence_fd = None

    def poll(self):
        return self.returncode

    def end(self, rc=0):
        self.returncode = rc

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if not self.signals:
            raise AssertionError("the conversation tick waited for a catalog run")
        raise subprocess.TimeoutExpired("catalog", timeout)


@pytest.fixture
def runs(svc, monkeypatch):
    """Fake catalog runs; a signal to one's process group reaches that fake."""
    started = []
    real_killpg = os.killpg

    def spawn(root, *, fence_fd=None):
        if started and started[-1].returncode is None:
            return None                     # the running one holds the lock
        run = FakeRun(4_000_000 + len(started))
        run.fence_fd = fence_fd
        started.append(run)
        return run

    def killpg(pid, sig):
        run = next((r for r in started if r.pid == pid), None)
        if run is None:
            return real_killpg(pid, sig)
        if run.returncode is not None:
            raise ProcessLookupError(pid)
        run.signals.append(sig)
        if not run.stubborn:
            run.end(-sig)

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    monkeypatch.setattr(catalog_module, "refresh_running",
                        lambda root: bool(started) and started[-1].returncode is None)
    monkeypatch.setattr(service_module.os, "killpg", killpg)
    monkeypatch.setattr(service_module, "CATALOG_STOP_WAIT_S", 0.0)
    yield started
    svc.close()                             # while the fakes still answer signals


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


def test_a_wedged_catalog_run_is_stopped_and_reaped_later(svc, runs):
    """C-30.1: a run alive past three times its own cap is killed by process group, once;
    a later tick reaps it and the timer carries on."""
    svc._catalog_tick()
    runs[0].stubborn = True                 # it takes a while to die
    svc.clock.now += service_module.CATALOG_KILL_AFTER_S - 1
    svc._catalog_tick()
    assert runs[0].signals == []
    svc.clock.now += 2
    svc._catalog_tick()
    svc._catalog_tick()
    assert runs[0].signals == [service_module.signal.SIGKILL]
    runs[0].end(-9)
    svc._catalog_tick()
    assert svc._catalog_proc is not None and len(runs) == 2         # reaped, and the next one due


def test_a_tracked_run_that_declined_is_logged_with_why_and_one_after_close_is_not(svc, runs, caplog):
    """C-30.1, review of #47: a run that declines to write exits nonzero. One this
    service still tracks declined although the service is open, and is logged with
    why, where it had passed for a clean run. After close() nothing tracks the run,
    so its expected decline is quiet."""
    with caplog.at_level(logging.WARNING, logger="test-conversations"):
        for status in (catalog_module.OWNER_GONE, catalog_module.FENCE_BROKEN):
            svc._start_catalog()
            runs[-1].end(status)
            svc._reap_catalog()
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2, said
    assert "stopped publishing (exit 3)" in said[0] and "owner gone" in said[0], said
    assert "stopped publishing (exit 4)" in said[1] and "fence" in said[1], said
    caplog.clear()
    svc._start_catalog()
    runs[-1].stubborn = True                # it outlives close()'s signals, then declines
    with caplog.at_level(logging.WARNING, logger="test-conversations"):
        svc.close()
        runs[-1].end(catalog_module.OWNER_GONE)
        svc._reap_catalog()
        svc.tick()
    assert "did not end" in caplog.text and "stopped publishing" not in caplog.text


def test_catalog_refresh_starts_a_run_without_waiting_and_resets_the_timer(svc, runs):
    """D-23, C-25.3: `catalog.refresh` starts a run and returns; a second request while it
    runs starts nothing; the timer counts from the request."""
    first = svc.handle("catalog.refresh", {}, None)
    assert first == {"requested": True, "running": True, "generated_at": None}
    again = svc.handle("catalog.refresh", {}, None)
    assert again["requested"] is False and again["running"] is True
    svc._catalog_tick()
    assert len(runs) == 1


def test_close_stops_the_catalog_run_and_starts_none_after(svc, runs, caplog):
    """C-30.1: close() ends the run it started (SIGTERM to its process group) before it
    returns, and closes the run's fence; after it, neither the timer, a tick close()
    overtook, nor `catalog.refresh` starts another, and the tick touches nothing."""
    svc._catalog_tick()
    assert runs[0].fence_fd is not None and runs[0].returncode is None
    svc.close()
    assert runs[0].signals == [service_module.signal.SIGTERM] and runs[0].returncode == -15
    assert svc._catalog_proc is None and svc._catalog_fence is None
    svc.clock.now += 1000
    with caplog.at_level(logging.WARNING):
        svc._catalog_tick()
        svc.tick()
    assert svc.handle("catalog.refresh", {}, None)["requested"] is False
    assert len(runs) == 1 and caplog.text == ""


def test_close_escalates_to_sigkill_when_a_run_outlasts_sigterm(svc, runs, caplog):
    """C-30.1: a run still alive once the wait after SIGTERM passes gets SIGKILL and a
    second bounded wait; close() never waits longer, and says so."""
    svc._catalog_tick()
    runs[0].stubborn = True
    with caplog.at_level(logging.WARNING):
        svc.close()
    assert runs[0].signals == [service_module.signal.SIGTERM, service_module.signal.SIGKILL]
    assert "did not end" in caplog.text


def test_close_kills_a_real_run_that_ignores_sigterm(svc, monkeypatch, tmp_path):
    """C-30.1: the same with a real process: close() returns with it killed and reaped."""
    monkeypatch.setattr(service_module, "CATALOG_STOP_WAIT_S", 0.5)
    ready = tmp_path / "ready"
    script = ("import pathlib, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
              "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)")
    monkeypatch.setattr(catalog_module, "spawn_refresh", lambda root, *, fence_fd=None: subprocess.Popen(
        [sys.executable, "-c", script, str(ready)], start_new_session=True))
    svc._catalog_tick()
    process = svc._catalog_proc
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert ready.exists(), "the stand-in run never started"
    started = time.monotonic()
    svc.close()
    assert process.returncode == -service_module.signal.SIGKILL
    assert time.monotonic() - started < 10


def test_a_run_started_while_close_waits_for_the_lock_is_stopped_too(svc, runs, monkeypatch):
    """C-30.1: a start and close() take the same lock. A start already inside it
    finishes; close() then stops the run it made, so none outlives close()."""
    entered, go = threading.Event(), threading.Event()
    fake_spawn = catalog_module.spawn_refresh

    def slow_spawn(root, **kw):
        entered.set()
        assert go.wait(10)
        return fake_spawn(root, **kw)

    monkeypatch.setattr(catalog_module, "spawn_refresh", slow_spawn)
    starter = threading.Thread(target=svc._start_catalog)
    starter.start()
    assert entered.wait(10)
    closer = threading.Thread(target=svc.close)
    closer.start()
    time.sleep(.2)
    assert closer.is_alive() and runs == []
    go.set()
    starter.join(10)
    closer.join(10)
    assert not closer.is_alive() and not starter.is_alive()
    assert len(runs) == 1 and runs[0].signals == [service_module.signal.SIGTERM]
    assert runs[0].returncode == -15 and svc._catalog_proc is None


CATALOG_STEPS = ("tick", "refresh", "advance", "exit", "stubborn", "close")


def test_catalog_lifecycle_invariants_hold_for_random_interleavings(tmp_path, monkeypatch):
    """C-30.1, checked after every step of 150 seeded random sequences of timer
    ticks, `catalog.refresh`, clock jumps, run exits, runs that ignore signals and
    close(), with close() at a random point or not at all:

    - before close(), at most one started run is alive, and it is the one the
      service tracks (the lock admits one; nothing alive goes untracked);
    - close() leaves no tracked run and no fence; the run it found alive got
      SIGTERM, then SIGKILL only when SIGTERM did not end it; no other run got a
      signal from it;
    - after close(), nothing starts and nothing is signalled, whatever follows
      (close() again included)."""
    state = {"runs": []}

    def spawn(root, *, fence_fd=None):
        runs = state["runs"]
        if runs and runs[-1].returncode is None:
            return None
        runs.append(FakeRun(5_000_000 + len(runs)))
        return runs[-1]

    def killpg(pid, sig):
        run = next((r for r in state["runs"] if r.pid == pid), None)
        if run is None or run.returncode is not None:
            raise ProcessLookupError(pid)
        run.signals.append(sig)
        if not run.stubborn:
            run.end(-sig)

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    monkeypatch.setattr(catalog_module, "refresh_running",
                        lambda root: bool(state["runs"]) and state["runs"][-1].returncode is None)
    monkeypatch.setattr(service_module.os, "killpg", killpg)
    monkeypatch.setattr(service_module, "CATALOG_STOP_WAIT_S", 0.0)
    TERM, KILL = service_module.signal.SIGTERM, service_module.signal.SIGKILL
    for seed in range(150):
        rng = random.Random(seed)
        steps = [rng.choice(CATALOG_STEPS) for _ in range(rng.randint(1, 14))]
        root = tmp_path / f"s{seed}"
        root.mkdir()
        daemon = FakeDaemon(root)
        svc = ConversationService(daemon)
        svc.clock = Clock()
        state["runs"] = runs = []
        closed_with = None                  # (runs started, signals per run) when close() first ran
        try:
            for n, step in enumerate(steps):
                where = f"seed {seed}, steps {steps[:n + 1]}"
                live = [r for r in runs if r.returncode is None]
                if step == "tick":
                    svc._catalog_tick()
                elif step == "refresh":
                    svc.handle("catalog.refresh", {}, None)
                elif step == "advance":
                    svc.clock.now += rng.choice((1, 30, 61, 200))
                elif step == "exit" and live and not live[0].stubborn:
                    live[0].end(rng.choice((0, 1)))
                elif step == "stubborn" and live:
                    live[0].stubborn = True
                elif step == "close":
                    if closed_with is None:
                        closed_with = (len(runs), [list(r.signals) for r in runs], live)
                    svc.close()
                    assert svc._catalog_proc is None and svc._catalog_fence is None, where
                live = [r for r in runs if r.returncode is None]
                if closed_with is None:
                    assert len(live) <= 1, where
                    assert not live or svc._catalog_proc is live[0] is runs[-1], where
                    continue
                count, signals, found = closed_with
                assert len(runs) == count, f"a run started after close(): {where}"
                for run, before in zip(runs, signals):
                    added = run.signals[len(before):]
                    if run in found:
                        assert added == ([TERM, KILL] if run.stubborn else [TERM]), where
                    else:
                        assert added == [], where
        finally:
            svc.close()
            daemon.store.close()


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
    runs.append(FakeRun(4_000_000 + len(runs)))
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


def _registry_row(session_id: str, pid: int):
    from subfleet.sessions import registry
    return registry.SessionRow(session_id=session_id, pid=pid, socket=None, name=None, cwd=None, started_at=None,
                               alive=True, socket_present=False, registry_path=f"/x/{pid}.json", proc_start=None)


@hypothesis.settings(max_examples=300, deadline=None)
@hypothesis.given(session=spellings.uuids, stored=spellings.masks, registered=spellings.masks)
def test_an_outside_writer_holds_its_session_in_any_spelling(session, stored, registered):
    """C-26.3 (review of 3c1a34e, finding 1): a live Claude process outside
    Subfleet registered for a session holds it whichever case either side spells
    its UUID in (a store written before ids were canonical keeps an upper-case
    one; Claude Code registers the lower-case one), and holds no other session.
    The catalog lists the sessions such processes hold in lower case."""
    from subfleet.sessions import registry
    rows = [_registry_row(spellings.spell(session, registered), 4242)]
    with mock.patch.object(registry, "rows", lambda: rows), \
            mock.patch.object(catalog_module, "_outside_claude", lambda row: True):
        assert catalog_module.external_writers(spellings.spell(session, stored)) == [4242]
        assert catalog_module.external_writers(spellings.other_uuid(session)) == []
        assert catalog_module._live_claude_sessions() == {session}


def _bound_in(svc, session: str, spelled: str) -> str:
    """A Claude conversation bound to `session`, its binding stored as `spelled`
    (a store written before native ids were canonical)."""
    cid = conversation(svc, origin="native", native_session_id=session)
    with svc.store.transaction() as tx:
        tx.execute("UPDATE conversations SET native_session_id=? WHERE conversation_id=?", (spelled, cid))
    return cid


SPELLED_SESSION = "5e551011-abcd-4ef0-8000-0000000000c1"


@pytest.mark.parametrize("direction", sorted(spellings.DIRECTIONS))
def test_an_outside_writer_holds_dispatch_in_either_spelling(svc, monkeypatch, direction):
    """C-26.3, D-17 (review of 3c1a34e, finding 1): dispatch finds the outside
    writer whichever case the conversation's binding and the registry row spell
    the session in: the message waits `external-writer`, with no job, and gets
    its job once the process is gone."""
    from subfleet.sessions import registry
    stored_as, registered_as = spellings.DIRECTIONS[direction]
    cid = _bound_in(svc, SPELLED_SESSION, stored_as(SPELLED_SESSION))
    rows = [_registry_row(registered_as(SPELLED_SESSION), 4242)]
    monkeypatch.setattr(registry, "rows", lambda: rows)
    monkeypatch.setattr(catalog_module, "_outside_claude", lambda row: True)
    mid = submit(svc, cid)
    svc._dispatch()
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"], message["job_id"]) == ("waiting", "external-writer: pid 4242", None)
    assert svc.daemon.submits == []
    rows.clear()
    svc.clock.now += service_module.EXTERNAL_WRITER_RECHECK_S
    svc._dispatch()
    assert svc.store.message(mid)["job_id"] == "job-1"


@pytest.mark.parametrize("direction", sorted(spellings.DIRECTIONS))
def test_an_outside_writer_holds_launch_in_either_spelling(svc, monkeypatch, tmp_path, direction):
    """C-26.3, D-17 (review of 3c1a34e, finding 1): a writer that took the session
    after the job was made is found at launch whichever case the turn's manifest
    (the conversation's binding) and the registry row spell it in; the answer is
    recorded, and the driver ends before `initialize`, writing nothing."""
    from subfleet.conversations.claude_turn import ClaudeTurn
    from subfleet.conversations.turn import TurnSpec
    from subfleet.sessions import registry
    stored_as, registered_as = spellings.DIRECTIONS[direction]
    monkeypatch.setattr(registry, "rows", lambda: [_registry_row(registered_as(SPELLED_SESSION), 4242)])
    monkeypatch.setattr(catalog_module, "_outside_claude", lambda row: True)
    adir = tmp_path / "a1"
    adir.mkdir()
    turn = {"provider": "claude", "native_session_id": stored_as(SPELLED_SESSION)}
    assert svc._writer_check(turn, adir) == [4242]
    assert json.loads((adir / "held_by.json").read_text())["pids"] == [4242]
    driver = ClaudeTurn(TurnSpec(provider="claude", message_id=str(uuid.uuid4()), text="hello", model_id="opus",
                                 permission="ask", native_session_id=turn["native_session_id"], held_by=(4242,)),
                        read_bytes=lambda path: b"")
    step = driver.start()
    assert [frame.tag for frame in step.frames] == ["close"]
    assert (step.outcome.state, step.outcome.reason) == ("failed", "external-writer")


def test_the_catalog_matches_a_binding_and_a_live_session_in_either_spelling(svc):
    """C-26.3, C-30.1 (review of 3c1a34e, finding 1): a conversation whose binding
    a store kept in upper case is shown `live_elsewhere` for a live session the
    catalog names in lower case, and its catalog item is left out of the list as
    one the conversation already holds."""
    from datetime import UTC, datetime
    cid = _bound_in(svc, SPELLED_SESSION, SPELLED_SESSION.upper())
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    item = {"provider": "claude", "native_session_id": SPELLED_SESSION, "path": "/x.jsonl", "title": "t",
            "cwd": svc.test_workspace, "mtime": 1.0, "continuable": True, "live_elsewhere": True}
    (svc.root / "catalog.json").write_text(json.dumps({"generated_at": now, "complete": True, "items": [item],
                                                       "live_claude": [SPELLED_SESSION]}))
    listed = svc.handle("conversation.list", {}, None)
    assert [view["live_elsewhere"] for view in listed["conversations"]] == [True]
    assert listed["catalog"]["items"] == []
    assert svc.handle("conversation.open", {"conversation_id": cid}, None)["conversation"]["live_elsewhere"] is True


def test_a_real_catalog_run_writes_the_catalog_the_list_reads(svc, monkeypatch, tmp_path):
    """C-30.1, D-23: the tick's run is `python -m subfleet.conversations.catalog`, out of
    process; once it finishes the list reports a fresh catalog."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "-work"
    projects.mkdir(parents=True)
    (projects / "0f0e0d0c-1111-2222-3333-444455556666.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": str(tmp_path), "message": {"role": "user", "content": "index me"}}) + "\n")
    monkeypatch.setenv("HOME", str(home))
    # conftest points SUBFLEET_CLAUDE_DIR away from the operator's ~/.claude
    # (C-23.28); this test says where its own is.
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home / ".claude"))
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


def test_a_worktree_add_that_quotes_a_name_that_is_not_utf8_fails_with_it(svc, repo, monkeypatch):
    """Review of 43b8bf29, F6: `git worktree add` prints a file it could not check out in
    its own bytes; read as strict UTF-8 that raised `UnicodeDecodeError` out of
    `conversation.create`, not the `worktree-failed` refusal naming the file. The call is
    faked (APFS refuses such names) and decodes as `subprocess` would."""
    real = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[3:5] == ["worktree", "add"]:
            stderr = b"error: unable to create file caf\xe9.txt: Permission denied\nfatal: could not reset\n"
            decoded = stderr.decode("utf-8", kwargs.get("errors") or "strict") if kwargs.get("text") else stderr
            return subprocess.CompletedProcess(cmd, 128, "" if kwargs.get("text") else b"", decoded)
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr(service_module.subprocess, "run", run)
    with pytest.raises(ConversationError) as err:
        create_worktree(svc, repo)
    assert err.value.reason == "worktree-failed"
    assert str(err.value) == "error: unable to create file caf\\xe9.txt: Permission denied\nfatal: could not reset"
    str(err.value).encode("utf-8")                                     # a reply can carry it


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


# --- close() and the file pool (C-25.3, C-28.1) -------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def file_op_args(op, tmp_path, repo) -> dict:
    """An `attachment.add` of a small PNG, or a create that cuts a worktree of `repo`."""
    if op == "attachment.add":
        image = tmp_path / "pixel.png"
        image.write_bytes(PNG)
        return {"path": str(image)}
    return {"provider": "claude", "request_id": "req-file-op", "workspace": str(repo), "workspace_kind": "worktree",
            "settings": SETTINGS}


def held_file_op(svc, monkeypatch, tmp_path, repo, op):
    """Start `op` on the file pool, held inside its work until the returned event is
    set: an attachment in its type check, after it read the file and before it writes
    anything; a create after its row, before it cuts the worktree."""
    entered, go = threading.Event(), threading.Event()

    def hold(real):
        def held(*args, **kwargs):
            entered.set()
            assert go.wait(30), "the test never let the op go on"
            return real(*args, **kwargs)
        return held

    if op == "attachment.add":
        monkeypatch.setattr(attachment_module, "sniff", hold(attachment_module.sniff))
    else:
        monkeypatch.setattr(svc, "_cut_worktree", hold(svc._cut_worktree))
    future = svc.pool_for(op).submit(svc.handle, op, file_op_args(op, tmp_path, repo), None)
    assert entered.wait(30), f"{op} never started"
    return future, go


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("op", ["attachment.add", "conversation.create"])
def test_close_finishes_the_file_ops_already_running_and_the_removed_root_stays_gone(
        svc, repo, monkeypatch, tmp_path, op):
    """C-25.3, C-28.1 (review of #47): close() shut the file pool down without waiting.
    An `attachment.add` still running when the daemon closed finished after its owner
    had removed the state root, and made the root again, holding `attachments/<sha>.png`
    (a worktree create did the same with `worktrees/`). close() now returns only once
    the file ops already running have finished, while the root and the store are still
    there, so removing the root afterwards leaves nothing to bring it back."""
    root = svc.root
    future, go = held_file_op(svc, monkeypatch, tmp_path, repo, op)
    running_at_return = []
    shutdowns: list[dict] = []
    real_shutdown = svc.files.shutdown
    monkeypatch.setattr(svc.files, "shutdown", lambda **kw: (shutdowns.append(kw), real_shutdown(**kw))[1])
    # The op is let go only once close() waits for its thread: a signal before the
    # pool's shutdown call let a shutdown that does not wait, reached late, pass
    # (review of aa41312, finding 5).
    joining = threading.Event()

    class Watched:
        def __init__(self, thread):
            self.thread = thread

        def join(self, timeout=None):
            joining.set()
            return self.thread.join(timeout)

        def __getattr__(self, name):
            return getattr(self.thread, name)

    svc.files._threads = {Watched(thread) for thread in svc.files._threads}

    def owner():                            # as a daemon's owner does: close it, then remove its root
        svc.close()
        running_at_return.append(not future.done())
        shutil.rmtree(root)

    closer = threading.Thread(target=owner)
    closer.start()
    until_true(lambda: joining.is_set() or not closer.is_alive(), "close() to wait for the op or return")
    go.set()
    closer.join(30)
    assert not closer.is_alive(), "close() did not return once the op had finished"
    problems = []
    if not joining.is_set() or [kw.get("wait") for kw in shutdowns] != [True]:
        problems.append(f"close() never waited for the file pool's threads: shutdown{shutdowns}")
    if running_at_return != [False]:
        problems.append(f"close() returned while {op} was still running")
    try:
        result = future.result(30)
    except Exception as exc:
        problems.append(f"{op} failed: {type(exc).__name__}: {exc}")
    else:
        if op == "attachment.add" and set(result) != {"sha256", "media_type", "bytes"}:
            problems.append(f"no receipt: {result}")
        if op == "conversation.create" and not result["conversation"]["worktree"]:
            problems.append(f"no worktree recorded: {result}")
    if root.exists():
        problems.append(f"the removed state root came back holding {tree(root)}")
    assert problems == [], "\n".join(problems)


def test_close_never_runs_a_file_op_it_had_not_started(svc, monkeypatch, tmp_path):
    """C-25.3: close() drops the file ops still queued behind busy threads (they never
    run, so write nothing), and the pool accepts none after it."""
    # The queued image is neither running one, so a copy of it can come only from its own op.
    running = [tmp_path / "one.png", tmp_path / "two.png"]
    for n, path in enumerate(running):
        path.write_bytes(PNG + bytes([n]))
    second = tmp_path / "queued.png"
    second.write_bytes(PNG + b"\x09")
    entered, go = threading.Semaphore(0), threading.Event()
    real = attachment_module.sniff

    def held(head):
        entered.release()
        assert go.wait(30)
        return real(head)

    monkeypatch.setattr(attachment_module, "sniff", held)
    pool = svc.pool_for("attachment.add")
    busy = [pool.submit(svc.handle, "attachment.add", {"path": str(path)}, None) for path in running]
    for _ in busy:
        assert entered.acquire(timeout=30), "the pool's two threads never both started"
    queued = pool.submit(svc.handle, "attachment.add", {"path": str(second)}, None)
    closer = threading.Thread(target=svc.close)
    closer.start()
    until_true(queued.cancelled, "close() to drop the queued op")
    go.set()
    closer.join(30)
    assert not closer.is_alive() and all(f.result(30)["sha256"] for f in busy)
    digest = hashlib.sha256(second.read_bytes()).hexdigest()
    assert not (svc.root / "attachments" / f"{digest}.png").exists()
    with pytest.raises(RuntimeError):
        pool.submit(svc.handle, "attachment.add", {"path": str(second)}, None)


@pytest.mark.parametrize("op", ["attachment.add", "conversation.create"])
def test_a_file_op_never_makes_the_state_root(svc, repo, tmp_path, op):
    """C-28.1, D-16: an attachment's copy and a conversation's worktree go into the state
    root as it stands, and neither makes it again once it is gone. (Only something that
    removed it under an open service can get here: close() lets no file op run after it.)"""
    args = file_op_args(op, tmp_path, repo)
    shutil.rmtree(svc.root)
    with pytest.raises(ConversationError) as err:
        svc.handle(op, args, None)
    assert err.value.reason == "state-root-gone" and err.value.code == 1
    assert not svc.root.exists()



CODEX_SETTINGS = {"model": "gpt-6-astra", "effort": None, "fast": False, "permission": "read-only",
                  "auto_continue": True}


@pytest.mark.parametrize("op", ["conversation.history", "conversation.handoff", "conversation.open"])
def test_an_op_handed_a_fifo_answers_at_once_and_close_returns(tmp_path, op):
    """C-25.3 (both reviews of 39223c9, finding 1): close() waits for the file ops
    already running, so one blocked for good held close() for good. A Codex thread
    whose rollout was a FIFO did that: `conversation.history`, or a handoff reading
    the thread's first record, blocked in open() until a writer came. The daemon now
    opens a transcript or rollout only as a regular file, without ever blocking in
    open(): each op answers at once, and close() returns. `conversation.open` runs on
    the requests pool, which `Daemon.close()` waits for too; it reads the thread
    through the catalog's record reader. In a child process (`tests.nonblocking`),
    killed and reaped if it blocks (review of aa41312, finding 4: the one release
    this test sent could come before the op reached open(), and miss it)."""
    out = run_child(f"""
        import concurrent.futures, json, os, uuid
        from pathlib import Path
        from subfleet.contracts import Credential, Lane, LaneOwner
        from subfleet.conversations.store import ConversationError
        from tests.unit import test_conversation_service as fx
        tmp, op = Path({str(tmp_path)!r}), {op!r}
        os.environ["HOME"] = str(tmp / "user-home")        # no real ~/.codex or ~/.claude
        root = tmp / "state"
        root.mkdir()
        svc = fx.ConversationService(fx.FakeDaemon(root))
        svc.test_workspace = str(tmp)
        home = tmp / "codex-home"
        thread = str(uuid.uuid4())
        day = home / "sessions" / "2026" / "09" / "26"
        day.mkdir(parents=True)
        os.mkfifo(day / f"rollout-2026-09-26T00-00-00-{{thread}}.jsonl")
        svc.daemon.store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", str(home), "home"),
                                       str(home), LaneOwner.V2, False))
        if op == "conversation.open":
            args = {{"native": {{"provider": "codex", "session_id": thread}}}}
        else:
            cid = svc.store.create_conversation(provider="codex", workspace=str(tmp), workspace_kind="in-place",
                                                settings=fx.CODEX_SETTINGS, origin="native", native_session_id=thread,
                                                lane_id="codex-1")[0]["conversation_id"]
            args = ({{"conversation_id": cid}} if op == "conversation.history" else
                    {{"request_id": "h-fifo", "from": {{"conversation_id": cid}},
                     "to": {{"provider": "claude", "settings": fx.SETTINGS}}}})
        requests = concurrent.futures.ThreadPoolExecutor(1)    # the fake daemon has no requests pool
        future = (svc.pool_for(op) or requests).submit(svc.handle, op, args, None)
        try:
            outcome = future.result()
        except ConversationError as exc:
            outcome = exc.reason
        svc.close()
        requests.shutdown(wait=True)
        svc.daemon.store.close()
        print(json.dumps(outcome, default=repr))
    """)
    assert json.loads(out) is not None, out             # the op's answer, or its refusal


# --- close() and the turn runners (C-25.3, C-26.6) ---------------------------------------

INIT_OK = json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
        "account": {"email": "max@example.org"}, "fast_mode_state": "off",
        "models": [{"value": "opus", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
                    "supportedEffortLevels": ["high"]}]}}})
ASK = json.dumps({"type": "control_request", "request_id": "req-1", "request": {
    "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}, "tool_use_id": "tu1"}})
PAD = json.dumps({"type": "system", "subtype": "padding", "text": "x" * 200})


def adopted_runner(svc, tmp_path, stdout=(), *, n=0):
    """The runner the control loop adopts for `launched_turn`."""
    attempt_id = launched_turn(svc, tmp_path, stdout, n=n)
    svc._adopt_runners()
    return svc.runners[attempt_id]


def launched_turn(svc, tmp_path, stdout=(), *, n=0) -> str:
    """A turn as its launch leaves it for the control loop to adopt (C-26.6): its job and
    attempt in the job store, the attempt's start record and manifest, and its stdout
    so far. No relay listens, so nothing is sent (the runner retries its handshake)."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "waiting")
    attempt_id = turn_attempt(svc, mid, state="running", n=n)
    job = svc.root / "jobs" / attempt_id.split("/")[0]
    adir = job / "a1"
    adir.mkdir(parents=True)
    (adir / "start.json").write_text(json.dumps({"control_socket": str(tmp_path / f"relay-{n}.sock")}))
    (adir / "stdout").write_bytes(b"".join(line.encode() + b"\n" for line in stdout))
    turn = {"provider": "claude", "conversation_id": cid, "message_id": mid, "text": "hello", "settings": SETTINGS,
            "cwd": svc.test_workspace, "new_session_id": str(uuid.uuid4())}
    (job / "manifest.json").write_text(json.dumps({TURN_MANIFEST_KEY: turn}))
    return attempt_id


class Hold:
    """A point a runner waits at until `go` is set; `entered` once it is there."""

    def __init__(self):
        self.entered, self.go = threading.Event(), threading.Event()

    def __call__(self):
        self.entered.set()
        assert self.go.wait(30), "the test never let the runner go on"


def held_runner(svc, monkeypatch, tmp_path, where):
    """An adopted runner that stops at `where` until the hold lets it go, before any
    check a fix may add there, and a probe for what it was about to write:
    - `catalog`: reporting the model catalog from the provider's initialize answer;
    - `approval`: recording a permission request, after the store read that precedes
      publishing the request's file;
    - `final flush`: writing its last events, after its loop ended (close() stopped it).
    """
    hold = Hold()
    if where == "catalog":
        real = svc._on_catalog
        monkeypatch.setattr(svc, "_on_catalog", lambda *args: (hold(), real(*args))[1])
        runner = adopted_runner(svc, tmp_path, [INIT_OK])
        return runner, hold, lambda: (svc.root / "conversations" / "models.json").exists()
    if where == "approval":
        real_id = store_module.new_id
        monkeypatch.setattr(store_module, "new_id", lambda prefix: (hold() if prefix == "ap" else None,
                                                                    real_id(prefix))[1])
        runner = adopted_runner(svc, tmp_path, [INIT_OK, ASK])
        return runner, hold, lambda: bool(list(svc.root.glob("conversations/*/approvals/*.json")))
    monkeypatch.setattr(service_module.TurnRunner, "_flush_due", lambda self: False)   # its first events wait
    runner = adopted_runner(svc, tmp_path)                                            # for the final flush
    until_true(lambda: runner.batch, "the runner's first events")
    real_append = svc.store.append_events

    def append(**kwargs):
        if runner._stopping.is_set():
            hold()
        return real_append(**kwargs)

    monkeypatch.setattr(svc.store, "append_events", append)

    def stored() -> bool:
        with contextlib.closing(sqlite3.connect(svc.root / "conversations.sqlite3")) as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE attempt_id=?", (runner.attempt_id,)).fetchone()[0] > 0
    return runner, hold, stored


def thread_errors(monkeypatch) -> list[str]:
    errors: list[str] = []
    monkeypatch.setattr(threading, "excepthook",
                        lambda a: errors.append(f"{a.thread.name}: {a.exc_type.__name__}: {a.exc_value}"))
    return errors


@pytest.mark.parametrize("where", ["catalog", "approval", "final flush"])
def test_close_waits_for_a_runner_to_finish_its_iteration_and_the_removed_root_stays_gone(
        svc, monkeypatch, tmp_path, where):
    """C-25.3, C-26.6: close() only asked the turn runners to stop, and returned. A
    runner still in its iteration went on after its owner had removed the state root:
    the model catalog it reported made the root again (`conversations/models.json`),
    so did a permission request it recorded (`conversations/<id>/approvals/`), and its
    last flush raised out of its thread, which then never set `finished`. close() now
    returns once each runner has finished the iteration it was in, while the root and
    the store are still there; what it was writing is there when close() returns."""
    monkeypatch.setattr(service_module, "RUNNER_STOP_WAIT_S", 30.0)   # this test is about waiting, not the bound
    joining = threading.Event()
    real_join = service_module.TurnRunner.join
    monkeypatch.setattr(service_module.TurnRunner, "join", lambda self, timeout: (joining.set(),
                                                                                  real_join(self, timeout))[1])
    root = svc.root
    runner, hold, landed = held_runner(svc, monkeypatch, tmp_path, where)
    errors = thread_errors(monkeypatch)
    if where != "final flush":
        assert hold.entered.wait(30), f"the runner never reached its {where}"
    seen: dict[str, bool] = {}

    def owner():                            # as a daemon's owner does: close it, then remove its root
        svc.close()
        seen["ended"], seen["landed"] = runner.finished.is_set(), landed()
        shutil.rmtree(root)

    closer = threading.Thread(target=owner)
    closer.start()
    assert hold.entered.wait(30), f"the runner never reached its {where}"
    # close() is now waiting for the runner, or has returned without waiting: no timing guess either way.
    until_true(lambda: joining.is_set() or not closer.is_alive(), "close() to wait for the runner or return")
    hold.go.set()
    closer.join(60)
    assert not closer.is_alive(), "close() did not return once the runner had finished"
    runner.join(30)
    problems = []
    if not seen.get("ended"):
        problems.append("close() returned while the runner was still going")
    if not seen.get("landed"):
        problems.append(f"the runner's {where} was not written by the time close() returned")
    if not runner.finished.is_set():
        problems.append("the runner never set `finished`")
    if errors:
        problems.append(f"the runner's thread raised: {errors}")
    if root.exists():
        problems.append(f"the removed state root came back holding {tree(root)}")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("where", ["catalog", "approval", "final flush"])
def test_a_runner_still_going_when_close_stops_waiting_writes_nothing_more(
        svc, monkeypatch, tmp_path, caplog, where):
    """C-25.3: close()'s wait is bounded (a relay that does not answer can hold a runner
    for its 30 s timeout). A runner still in its iteration when the bound runs out is
    named in the log, and every write it would make after that is refused (the store
    has closed: `store-closed`), so the removed root stays gone; the runner still ends
    and sets `finished`."""
    monkeypatch.setattr(service_module, "RUNNER_STOP_WAIT_S", 0.3)
    root = svc.root
    runner, hold, _ = held_runner(svc, monkeypatch, tmp_path, where)
    errors = thread_errors(monkeypatch)
    if where != "final flush":
        # Exercise close() while this write is already in progress. A runner
        # still waiting for its relay handshake may stop before it reads stdout.
        assert hold.entered.wait(30), f"the runner never reached its {where}"
    closer = threading.Thread(target=svc.close)
    with caplog.at_level(logging.INFO, logger="test-conversations"):
        closer.start()
        assert hold.entered.wait(30), f"the runner never reached its {where}"
        closer.join(30)
        assert not closer.is_alive(), "close() waited past its bound"
        held_at_return = not runner.finished.is_set()
        shutil.rmtree(root)
        hold.go.set()
        assert runner.join(30), "the runner never ended"
    problems = []
    if not held_at_return:
        problems.append("the runner ended before close() returned: the test held nothing")
    if not any(r.levelno == logging.WARNING and "still going" in r.getMessage() and runner.attempt_id in r.getMessage()
               for r in caplog.records):
        problems.append("close() did not name the runner it stopped waiting for")
    if not runner.finished.is_set():
        problems.append("the runner never set `finished`")
    if errors:
        problems.append(f"the runner's thread raised: {errors}")
    if root.exists():
        problems.append(f"the removed state root came back holding {tree(root)}")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("when", ["as close() begins", "while close() waits for the file pool"])
def test_a_runner_adopted_while_close_runs_is_stopped_or_never_started(svc, monkeypatch, tmp_path, when):
    """C-25.3: the control loop's `_adopt_runners` can be building a runner when close()
    runs (the daemon waits for its control-loop pool only after the service closed).
    close() stopped the runners it found, and a runner registered after that started
    and ran on with nobody to stop it. Now a runner is registered and started only
    while the service is open, in one step close() cannot interleave with taking its
    list of runners, which it does once the service is closed: close() stops the
    runner and waits for it, or it never starts."""
    entered, go = threading.Event(), threading.Event()

    class HeldRunner(service_module.TurnRunner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            entered.set()
            assert go.wait(30)

    monkeypatch.setattr(service_module, "TurnRunner", HeldRunner)
    closing = Hold()                        # where close() waits while the control loop goes on
    file_op = None
    if when == "as close() begins":
        real_stop = svc._stop_catalog
        monkeypatch.setattr(svc, "_stop_catalog", lambda: (closing(), real_stop())[1])
    else:
        file_op, closing.go = held_file_op(svc, monkeypatch, tmp_path, None, "attachment.add")
        real_shutdown = svc.files.shutdown
        monkeypatch.setattr(svc.files, "shutdown", lambda **kw: (closing.entered.set(), real_shutdown(**kw))[1])
    launched_turn(svc, tmp_path, [INIT_OK])
    adopter = threading.Thread(target=svc._adopt_runners)
    adopter.start()
    assert entered.wait(30), "the control loop never built the runner"
    closer = threading.Thread(target=svc.close)
    closer.start()
    assert closing.entered.wait(30), f"close() never got {when}"
    go.set()                                # the control loop registers the runner, or finds the service closed
    adopter.join(30)
    closing.go.set()
    closer.join(30)
    assert not closer.is_alive() and (file_op is None or file_op.result(30)["sha256"])
    try:
        unstopped = [aid for aid, runner in svc.runners.items() if not runner._stopping.is_set()]
        assert unstopped == [], f"close() returned with runners it never stopped: {unstopped}"
        assert all(runner.finished.is_set() for runner in svc.runners.values())
        assert list(svc.runners) == ([] if when != "as close() begins" else ["turn-job-0/a1"])
    finally:
        for runner in svc.runners.values():
            runner.stop()
            runner.join(30)


def test_a_runner_whose_thread_cannot_start_is_adopted_again_and_close_still_closes(svc, tmp_path, monkeypatch):
    """C-25.3 (review of 4d3d3ea, F2): the control loop registers a runner, then starts
    it. When its thread could not start (`RuntimeError` at a thread limit), the runner
    stayed registered: never adopted again or finished, its job pinned for good, and
    close() raised joining it, before the conversation store and the rest of
    `Daemon.close()` were closed. Now such a runner is not kept: the next tick adopts
    the turn again, and close() stops it and closes the store."""
    aid = launched_turn(svc, tmp_path, [INIT_OK])
    real_start = threading.Thread.start

    def failing(self):
        if self.name.startswith("turn:"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        svc._adopt_runners()
    assert aid not in svc.runners
    # Nothing owns a stop meanwhile: the daemon's wall limit and a kill reach the
    # attempt, where a dead runner kept registered answered True for good.
    job_id = aid.rsplit("/", 1)[0]
    assert svc.stop({"attempt_id": aid}, {"job_id": job_id}, "wall-limit") is False
    monkeypatch.setattr(threading.Thread, "start", real_start)
    svc._adopt_runners()                    # the next tick
    runner = svc.runners[aid]
    assert runner._thread is not None and runner._thread.ident is not None
    svc.close()
    assert runner._stopping.is_set() and runner.join(30)
    with pytest.raises(ConversationError) as err:
        svc.store.query("SELECT 1")
    assert err.value.reason == "store-closed"


def test_a_tick_step_close_overtook_is_logged_as_stopped_not_failed(svc, tmp_path, monkeypatch, caplog):
    """C-25.3 (review of 4d3d3ea, F4): `Daemon.close()` waits only 2 s for the control
    loop, so close() can return while `_adopt_runners` is still in its reads before the
    lock (a writer check runs `ps`). Its next store read is refused, and the tick logged
    that as a failed step at ERROR. It is the service closing, not a defect: it is
    logged at info, and no runner is registered or started."""
    svc.daemon.policy["conversations"]["catalog_interval_s"] = 0
    aid = launched_turn(svc, tmp_path, [INIT_OK])
    real = svc._writer_check

    def closing(turn, adir):
        closer = threading.Thread(target=svc.close)
        closer.start()
        closer.join(60)
        assert not closer.is_alive()
        return real(turn, adir)

    monkeypatch.setattr(svc, "_writer_check", closing)
    with caplog.at_level(logging.INFO, logger="test-conversations"):
        svc.tick()
    assert aid not in svc.runners
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any(r.levelno == logging.INFO and "_adopt_runners stopped: the service closed" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_dispatch_close_overtook_is_logged_as_stopped_not_failed(svc, monkeypatch, caplog):
    """C-25.3 (review of the branch): the dispatch step logs each message it could not
    dispatch at ERROR, so close() landing after the step read its candidates logged
    the refused store as a failed dispatch. The step ends, and the tick logs it at
    info as stopped."""
    svc.daemon.policy["conversations"]["catalog_interval_s"] = 0
    cid = conversation(svc)
    submit(svc, cid)
    real = svc._dispatch_one

    def closing(message):
        closer = threading.Thread(target=svc.close)
        closer.start()
        closer.join(60)
        assert not closer.is_alive()
        return real(message)

    monkeypatch.setattr(svc, "_dispatch_one", closing)
    with caplog.at_level(logging.INFO, logger="test-conversations"):
        svc.tick()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any("_dispatch stopped: the service closed" in r.getMessage() for r in caplog.records)


def test_a_closed_store_is_never_read_as_a_message_the_daemon_never_had(svc):
    """C-25.3: `message.status` and `message.cancel` read every refusal of
    `store.message` as "no such message". Once close() had closed the store, an op
    still running answered `unknown` for a message the daemon holds (the app reads
    that as never received) and a cancel without its conversation id failed
    `unknown-message`, exit 2. Only a missing message is unknown; a store that
    refuses fails the op (`store-closed`, exit 1). A malformed id is still unknown."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    answer = svc.handle("message.status", {"message_ids": ["not-a-uuid", mid]}, None)
    assert [m["state"] for m in answer["messages"]] == ["unknown", "queued"]
    svc.close()
    calls = {"status": lambda: svc.handle("message.status", {"message_ids": [mid]}, None),
             "cancel": lambda: svc.handle("message.cancel", {"message_id": mid}, None),
             "cancel naming its conversation": lambda: svc.handle(
                 "message.cancel", {"message_id": mid, "conversation_id": cid}, None)}
    refused = {}
    for name, call in calls.items():
        try:
            refused[name] = ("answered", call())
        except ConversationError as exc:
            refused[name] = (exc.reason, exc.code)
    assert refused == {name: ("store-closed", 1) for name in calls}, refused


def test_containment_after_the_store_closed_is_refused_like_every_other_late_write(svc, tmp_path):
    """C-25.3: a runner still going when close() stops waiting reaches D-13's
    containment through `_on_contain`, which writes only the main store. Every other
    write of such a runner is refused (`store-closed`, logged at info); this one went
    on, and after `Daemon.close()` had closed the main store it raised
    ProgrammingError, logged as a runner failure at ERROR. It is refused once the
    conversation store has closed, which `Daemon.close()` does before the main store;
    before that it is recorded as ever."""
    aid = launched_turn(svc, tmp_path, [INIT_OK])
    svc._on_contain(aid)
    job_id = aid.rsplit("/", 1)[0]
    assert svc.daemon.store.one("SELECT killed_by FROM attempts WHERE attempt_id=?", (aid,))["killed_by"]
    later = launched_turn(svc, tmp_path, [INIT_OK], n=1)
    svc.close()
    with pytest.raises(ConversationError) as err:
        svc._on_contain(later)
    assert err.value.reason == "store-closed"
    assert svc.daemon.store.one("SELECT killed_by FROM attempts WHERE attempt_id=?", (later,))["killed_by"] is None
    assert svc.daemon.store.one("SELECT cancel_requested_at FROM jobs WHERE job_id=?",
                                (later.rsplit("/", 1)[0],))["cancel_requested_at"] is None
    assert job_id != later.rsplit("/", 1)[0]


def test_a_persons_stop_finds_its_runner_while_the_control_loop_adds_and_forgets_others(svc):
    """C-24.7: `turn.interrupt` looks for the message's runner while the control loop
    registers runners (under the service lock) and forgets finished ones. The lookup
    iterated the live dict, so on the free-threaded build it raised "dictionary
    changed size during iteration" after the stop was recorded, and the runner was
    never interrupted. It looks through a copy."""
    class Live(FakeRunner):
        message_id = "the-message"

    live = Live()
    svc.runners["live/a1"] = live
    stop = threading.Event()

    def churn():
        n = 0
        while not stop.is_set():
            with svc._lock:
                svc.runners[f"churn-{n}/a1"] = FakeRunner(finished=True)
            svc.runners.pop(f"churn-{n - 8}/a1", None)
            n += 1

    churner = threading.Thread(target=churn)
    churner.start()
    errors, found = [], 0
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                found += svc._runner_for_message("the-message") is live
            except RuntimeError as exc:
                errors.append(str(exc))
    finally:
        stop.set()
        churner.join(30)
        svc.runners.clear()
    assert errors == [] and found > 0, (errors[:3], len(errors), found)


def test_a_closed_store_refuses_every_read_and_write_and_writes_no_file(svc):
    """C-25.3: after close() the conversation store answers `store-closed` (exit 1) to
    every read, write and file write, where a runner still going had reached a closed
    SQLite handle only after publishing its file (an approval's request, a message's
    text), and the store's own files stay as close() left them."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.close()
    before = tree(svc.root)

    def file_write():
        with svc.store.writing():
            (svc.root / "stray").write_text("x")

    calls = {
        "read": lambda: svc.store.conversation(cid),
        "message": lambda: submit(svc, cid, after=mid),
        "approval": lambda: svc.store.add_approval(message_id=mid, conversation_id=cid, attempt_id="j/a1",
                                                   provider_request_id="r1", kind="tool", request={"x": 1},
                                                   display={}, options=("allow",)),
        "events": lambda: svc.store.append_events(conversation_id=cid, message_id=mid, attempt_id="j/a1",
                                                  events=[("stdout", "1", 0, "text", {"text": "a"})],
                                                  stdout_offset=1, stdin_seq=0),
        "text": lambda: svc.store._publish(svc.store.dir / cid / "messages" / "late.md", b"late"),
        "file": file_write,
    }
    refused = {}
    for name, call in calls.items():
        try:
            call()
        except ConversationError as exc:
            refused[name] = (exc.reason, exc.code)
        except Exception as exc:
            refused[name] = type(exc).__name__
        else:
            refused[name] = None
    assert refused == {name: ("store-closed", 1) for name in calls}
    assert tree(svc.root) == before


def test_close_waits_for_a_store_file_write_under_way(svc, monkeypatch):
    """C-24.3, C-25.3: close() takes the store's write guard, so a message's text being
    published when it runs has landed by the time close() returns, and nothing lands
    after. The message's row is committed before the store closes (its receipt may
    still be refused, as a lost response is: a resend of the same id gets it, C-24.2),
    or refused (`store-closed`) and its text left with no row, as a crash between the
    two would leave it."""
    cid = conversation(svc)
    hold = Hold()
    real = store_module._publish
    monkeypatch.setattr(store_module, "_publish", lambda path, data: (hold(), real(path, data))[1])
    refused, at_return = [], []

    def send():
        try:
            submit(svc, cid)
        except ConversationError as exc:
            refused.append(exc.reason)

    def texts():
        return [p.name for p in svc.root.glob("conversations/*/messages/*.md")]

    closing = threading.Event()
    real_close = svc.store.close
    monkeypatch.setattr(svc.store, "close", lambda: (closing.set(), real_close())[1])
    sender = threading.Thread(target=send)
    sender.start()
    assert hold.entered.wait(30), "the text was never published"
    closer = threading.Thread(target=lambda: (svc.close(), at_return.append(texts())))
    closer.start()
    assert closing.wait(30), "close() never reached the store"
    closer.join(0.5)                        # a store close() that does not wait for the write returns at once
    waited = closer.is_alive()
    hold.go.set()
    closer.join(30)
    sender.join(30)
    assert waited, "close() returned while a text was being published"
    assert len(at_return) == 1 and len(at_return[0]) == 1, at_return
    assert texts() == at_return[0]
    with contextlib.closing(sqlite3.connect(svc.root / "conversations.sqlite3")) as db:
        rows = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
    assert (refused, rows) in ((["store-closed"], 0), (["store-closed"], 1), ([], 1)), (refused, rows)


def test_a_model_catalog_waiting_for_a_file_write_holds_no_service_lock(svc, monkeypatch):
    """C-25.3 (review of 585ea41..4d3d3ea): a turn's model catalog is merged into
    `conversations/models.json` under the store's write guard, which another file
    write (a message's text: two fsyncs) can hold. The merge waited for it holding the
    service lock, so every poll, dispatch claim and adoption waited on an unrelated
    fsync too. The merge has a lock of its own."""
    cid = conversation(svc)
    hold = Hold()
    real = store_module._publish
    monkeypatch.setattr(store_module, "_publish", lambda path, data: (hold(), real(path, data))[1])
    entering = threading.Event()
    real_writing = svc.store.writing

    @contextlib.contextmanager
    def writing():
        entering.set()
        with real_writing():
            yield

    sender = threading.Thread(target=lambda: submit(svc, cid))
    sender.start()
    assert hold.entered.wait(30), "the text was never published"
    monkeypatch.setattr(svc.store, "writing", writing)
    merger = threading.Thread(target=svc._on_catalog, args=("claude", "claude-1",
                                                           [{"model": "claude-opus-5-5", "value": "opus"}]))
    merger.start()
    try:
        assert entering.wait(30), "the merge never reached the write guard"
        took = svc._lock.acquire(timeout=5)
        if took:
            svc._lock.release()
        assert took, "the service lock was held by a merge waiting for another file write"
    finally:
        hold.go.set()
        sender.join(30)
        merger.join(30)
    assert json.loads((svc.root / "conversations" / "models.json").read_text())["claude"]["claude-opus-5-5"]


@pytest.mark.parametrize("write", ["message text", "approval request", "model catalog"])
def test_no_conversation_file_write_makes_the_state_root(svc, write):
    """C-24.3, C-25.3: a message's text, an approval's request and the model catalog go
    into the state root as it stands, making only the directories below it; with the
    root gone they fail `state-root-gone` (exit 1) and make nothing. (After close()
    every write is refused first: only something that removed the root under an open
    service gets here.)"""
    cid = conversation(svc)
    mid = submit(svc, cid)
    shutil.rmtree(svc.root)
    with pytest.raises(ConversationError) as err:
        if write == "message text":
            submit(svc, cid, after=mid)
        elif write == "approval request":
            svc.store.add_approval(message_id=mid, conversation_id=cid, attempt_id="j/a1", provider_request_id="r1",
                                   kind="tool", request={"x": 1}, display={}, options=("allow",))
        else:
            svc._on_catalog("claude", "claude-1", [{"model": "claude-opus-5-5", "value": "opus"}])
    assert (err.value.reason, err.value.code) == ("state-root-gone", 1)
    assert not svc.root.exists()


def test_a_handoff_never_makes_the_state_root(world):
    """C-25.3, C-30.3 (both reviews of 39223c9, finding 2): a `conversation.handoff`,
    a file op, publishes its brief and the moved texts through the store. With the
    root gone it fails `state-root-gone` (exit 1) and makes nothing; the store's
    `mkdir(parents=True)` had made the root again and the handoff had succeeded."""
    cid, _ = handoff_source(world)          # no queued message: its text would go with the root
    shutil.rmtree(world.daemon.root)
    with pytest.raises(ConversationError) as err:
        handoff(world, cid)
    assert (err.value.reason, err.value.code) == ("state-root-gone", 1)
    assert not world.daemon.root.exists()


def test_the_store_makes_every_level_below_the_root_private(svc):
    """C-25.5: 0700 directories. `mkdir(parents=True)` made the levels above the last
    with the default mode (0755 under the usual umask)."""
    cid = conversation(svc)
    submit(svc, cid)
    for directory in (svc.root / "conversations", svc.root / "conversations" / cid,
                      svc.root / "conversations" / cid / "messages"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    with pytest.raises(ValueError):
        svc.store.subdirectory("../outside")


def test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing(tmp_path, monkeypatch):
    """C-24.3, C-25.3, on a real daemon: `Daemon.close()` waits for its requests pool,
    but only after the conversation service has closed. A `message.submit` that had
    read the store and not yet published its text then published it into the root
    after close() (a text no row names), and failed on the closed database. Its text
    is now refused (`store-closed`). Nothing it does outlives `Daemon.close()`, so the
    removed root stays gone either way."""
    from subfleet.daemon import Daemon
    root = tmp_path / "state"
    daemon = Daemon(root, tick_s=.05)
    svc = daemon.conversations
    workspace = tmp_path / "work"
    workspace.mkdir()
    cid = svc.store.create_conversation(provider="claude", workspace=str(workspace), workspace_kind="in-place",
                                        settings=SETTINGS, origin="new")[0]["conversation_id"]
    hold = Hold()
    real_one = svc.store.one

    def one(sql, params=()):
        row = real_one(sql, params)
        if sql == "SELECT * FROM messages WHERE message_id=?" and not hold.entered.is_set():
            hold()                          # submit_message has read the store and not published its text
        return row

    monkeypatch.setattr(svc.store, "one", one)
    message_id = str(uuid.uuid4())
    future = daemon.requests.submit(svc.handle, "message.submit",
                                    {"conversation_id": cid, "message_id": message_id, "text": "hello"}, None)
    seen = {}

    def owner():
        daemon.close()
        seen["done"], seen["texts"] = future.done(), [p.name for p in root.glob("conversations/*/messages/*")]
        shutil.rmtree(root)

    try:
        assert hold.entered.wait(30), "message.submit never read the store"
        closer = threading.Thread(target=owner)
        closer.start()
        until_true(lambda: svc.store._closed, "the conversation store to close")
        hold.go.set()
        closer.join(30)
        assert not closer.is_alive()
    finally:
        hold.go.set()
        daemon.close()
    with pytest.raises(ConversationError) as err:
        future.result(30)
    assert err.value.reason == "store-closed"
    assert seen == {"done": True, "texts": []}
    assert not root.exists()


RUNNER_TRIALS = 20


def test_runner_close_invariants_hold_for_random_timings(tmp_path, monkeypatch):
    """C-25.3, C-26.6, over 20 seeded trials of one to three adopted turns, each with a
    random stdout (padding, the initialize answer with its catalog, sometimes a
    permission request, more padding: up to four 1 MiB reads), closed after a random
    pause:
    - close() returns only once every runner has ended;
    - nothing under the state root changes after close() returns;
    - no runner thread raises, and every runner sets `finished`;
    - once the owner removes the root, it stays gone."""
    errors = thread_errors(monkeypatch)
    for seed in range(RUNNER_TRIALS):
        rng = random.Random(seed)
        root = tmp_path / f"s{seed}"
        root.mkdir()
        daemon = FakeDaemon(root)
        svc = ConversationService(daemon)
        svc.clock = Clock()
        svc.test_workspace = str(tmp_path)
        runners = []
        for n in range(rng.randint(1, 3)):
            stdout = [PAD] * rng.randint(0, 8_000) + [INIT_OK] + ([ASK] if rng.random() < .5 else [])
            runners.append(adopted_runner(svc, tmp_path, stdout + [PAD] * rng.randint(0, 8_000), n=n))
        time.sleep(rng.uniform(0, .08))
        where = f"seed {seed}, {len(runners)} runners"
        try:
            svc.close()
            ended = [r.finished.is_set() for r in runners]
            files = snapshot(root)
            for runner in runners:
                runner.join(30)
            assert ended == [True] * len(runners), f"{where}: close() returned with runners going: {ended}"
            assert snapshot(root) == files, f"{where}: the root changed after close() returned"
            assert errors == [], f"{where}: {errors}"
            shutil.rmtree(root)
            time.sleep(.01)
            assert not root.exists(), f"{where}: the removed root came back holding {tree(root)}"
        finally:
            for runner in runners:
                runner.stop()
                runner.join(30)
            daemon.store.close()


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_two_adds_of_the_same_image_at_once_both_succeed(svc, monkeypatch, tmp_path):
    """C-28.1: the app resending after its request timed out while the first add still
    ran, or two clients, runs two `attachment.add` calls of the same bytes on the file
    pool's two threads at once. Both wrote through the one temporary name
    `.<sha>.<pid>.tmp`: the second open truncated the first's file, one rename found it
    gone (FileNotFoundError) and the other could hash a copy the second had emptied
    (copy-mismatch), in 40 of 40 trials; the stored copy was right and one op failed,
    sometimes both. Each add now writes a temporary file of its own and renames it onto
    the content-addressed name, so both return the same receipt and one copy is left."""
    image = tmp_path / "screenshot.png"
    data = PNG + bytes(range(256)) * 8192                  # 2 MiB: the writes take a while
    image.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    attachments = svc.root / "attachments"
    read, written = threading.Barrier(2), threading.Barrier(2)
    real_sniff, real_rename = attachment_module.sniff, os.rename

    def sniff(head):                        # both have read the image before either writes
        read.wait(30)
        return real_sniff(head)

    def rename(src, dst, *args, **kwargs):  # and both have written before either renames
        if Path(dst).parent == attachments:
            try:
                written.wait(5)
            except threading.BrokenBarrierError:   # an add that found the copy made writes none
                pass
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(attachment_module, "sniff", sniff)
    monkeypatch.setattr(os, "rename", rename)
    pool = svc.pool_for("attachment.add")
    adds = [pool.submit(svc.handle, "attachment.add", {"path": str(image)}, None) for _ in range(2)]
    receipts, problems = [], []
    for add in adds:
        try:
            receipts.append(add.result(60))
        except Exception as exc:
            problems.append(f"an add failed: {type(exc).__name__}: {exc}")
    assert problems == [], "\n".join(problems)
    assert receipts == [{"sha256": digest, "media_type": "image/png", "bytes": len(data)}] * 2
    assert tree(attachments) == [f"{digest}.png"]
    assert (attachments / f"{digest}.png").read_bytes() == data
    assert svc.store.attachment(digest)["path"] == str(attachments / f"{digest}.png")


def until_true(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(.01)


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
        self.offset, self.next_seq = 0, 1          # what settling reads to stamp its reconcile event


@pytest.mark.parametrize("person_stopped", [False, True])
@pytest.mark.parametrize("resolution", ["delivered", "not-delivered"])
def test_resolving_unknown_delivery_honors_a_recorded_personal_stop(svc, tmp_path, monkeypatch,
                                                                  person_stopped, resolution):
    """C-24.6/7/8: resolution preserves the Stop the person already requested;
    other delivered Claude turns still need an unfinished-turn choice."""
    from subfleet.conversations.peers import Verdict

    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "running")
    if person_stopped:
        svc.op_turn_interrupt({"message_id": mid}, None)
    adir = tmp_path / "outcome"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "failed", "ended_by": "eof",
                                                "reason": "ended-without-result"}))
    monkeypatch.setattr(service_module.reconcile, "gather", lambda *a, **k: service_module.reconcile.Evidence(
        acknowledged=False, frame="written", process_gone=True, native="absent"))
    svc._on_outcome(EndedRunner(adir, mid, cid))
    assert svc.store.message(mid)["state"] == "delivery-unknown"
    assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
    monkeypatch.setattr(svc, "_person", lambda *a: Verdict(True, "test-person", 4242))
    svc.op_message_resolve({"message_id": mid, "resolution": resolution, "confirm": True}, None)
    message = svc.store.message(mid)
    expected = ("interrupted", "stopped") if person_stopped else ("failed", f"resolved-{resolution}")
    assert (message["state"], message["state_reason"]) == expected
    assert message["resolution"]["resolution"] == resolution
    assert svc.store.conversation(cid)["blocked_by"] == (
        "unfinished-turn" if resolution == "delivered" and not person_stopped else None)


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
                                               "user_frame_written": False, "accepted": False,
                                               "ended_by": "driver"}))
    # What reconciliation finds after a launch that sent nothing: the process has
    # exited, no message frame was logged, and the session's transcript lacks it.
    monkeypatch.setattr(service_module.reconcile, "gather", lambda *a, **k: service_module.reconcile.Evidence(
        acknowledged=False, frame="absent", process_gone=True, native="absent", session_exists=True))
    svc._on_outcome(EndedRunner(end, mid, cid))
    message = svc.store.message(mid)
    assert (message["state"], message["state_reason"]) == ("waiting", "readmit:external-writer")
    assert message["turn_seq"] == service_module.MAX_READMITS + 3


#: How a turn that reached the provider may end before its message was written:
#: the waits for the session's other writer, and failures another admission may pass.
ENDINGS = {"legacy-owner": {"state": "interrupted", "reason": "stopped-before-send", "stop_reason": "legacy-owner"},
           "external-writer": {"state": "failed", "reason": "external-writer"},
           "provider-init-failed": {"state": "failed", "reason": "provider-init-failed"},
           "fast-unavailable": {"state": "failed", "reason": "fast-unavailable"},
           "guard-refused": {"state": "failed", "reason": "guard-refused"}}
WAIT_ENDINGS = ("legacy-owner", "external-writer")


@hypothesis.settings(max_examples=60, deadline=None)
@hypothesis.given(endings=hypothesis.strategies.lists(hypothesis.strategies.sampled_from(sorted(ENDINGS)),
                                                      min_size=1, max_size=9),
                  unread=hypothesis.strategies.sets(hypothesis.strategies.integers(0, 8)))
def test_only_chargeable_failures_use_up_readmissions(endings, unread):
    """C-24.6, D-17 (review of 3c1a34e, finding 4), over any sequence of turns of
    one message that each reached the provider and ended before its message was
    written: a wait for the session's other writer is always re-admitted and
    counts for nothing; a failure another admission may pass is re-admitted
    while fewer than MAX_READMITS such failures came before it, and otherwise
    fails the message. A turn whose outcome cannot be read afterwards (in
    `unread`) counts as a failure, whatever it was."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="readmits-", dir=Path(__file__).parent) as directory:
        root = Path(directory) / "state"
        root.mkdir()
        workspace = Path(directory) / "work"
        workspace.mkdir()
        daemon = FakeDaemon(root)
        service = ConversationService(daemon)
        real = service_module.reconcile.gather
        service_module.reconcile.gather = lambda *a, **k: service_module.reconcile.Evidence(
            acknowledged=False, frame="absent", process_gone=True, native="absent", session_exists=True)
        try:
            service.test_workspace = str(workspace)
            cid = conversation(service)
            mid = submit(service, cid)
            charged = 0
            for n, ending in enumerate(endings):
                job_id = f"job-{n}"
                daemon.store.add_job(job_id=job_id, request_id=f"turn:{mid}:{n}", payload_digest="d", kind="turn",
                                     workdir=str(workspace), prompt_path="p", sandbox="workspace-write",
                                     name=f"turn-{cid}", in_place=1, max_attempts=1, state="failed")
                daemon.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="claude-1",
                                         model_requested="claude-opus-5-5", state="failed")
                adir = root / "jobs" / job_id / "a1"
                adir.mkdir(parents=True)
                (adir / "turn.json").write_text(json.dumps({"ended_by": "driver", "user_frame_written": False,
                                                            **ENDINGS[ending]}))
                service.store.set_state(mid, "starting", job_id=job_id)
                runner = EndedRunner(adir, mid, cid)
                runner.attempt_id = f"{job_id}/a1"
                service._on_outcome(runner)
                message = service.store.message(mid)
                waits = ending in WAIT_ENDINGS
                if waits or charged < service_module.MAX_READMITS:
                    assert (message["state"], message["state_reason"]) == ("waiting", f"readmit:{ending}")
                else:
                    assert (message["state"], message["state_reason"]) == ("failed", f"not-delivered: {ending}")
                    break
                if n in unread:
                    (adir / "turn.json").unlink()        # its outcome is lost before the next settles
                    charged += 1
                elif not waits:
                    charged += 1
        finally:
            service_module.reconcile.gather = real
            service.close()
            daemon.store.close()


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
    job("j-revive", "s-conv", "2026-09-25T03:30:00Z", kind="revive")
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


def test_an_approval_view_names_the_providers_request_under_both_names(svc):
    """C-27.1 and C-27.5, merged for 2.1.10: `approval.list`, `conversation.open` and
    `approval.get` name the provider's request as `provider_request_id` (the inline
    approval card's field) and as `request_id` (approvals within reach), the same id,
    so either client joins its card exactly."""
    from subfleet.conversations.peers import Verdict
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc.store.set_state(mid, "running")
    approval, _ = svc.store.add_approval(message_id=mid, conversation_id=cid, attempt_id="j/a1",
                                         provider_request_id="perm-7", kind="tool", request={"tool": "Bash"},
                                         display={"tool": "Bash"}, options=("allow", "deny"))
    svc._person = lambda peer, what: Verdict(True, "test", peer)
    views = [svc.handle("approval.list", {"conversation_id": cid}, None)["approvals"][0],
             svc.handle("conversation.open", {"conversation_id": cid}, None)["pending_approvals"][0],
             svc.handle("approval.get", {"approval_id": approval["approval_id"]}, None)["approval"]]
    for view in views:
        assert view["approval_id"] == approval["approval_id"]
        assert view["request_id"] == view["provider_request_id"] == "perm-7"
