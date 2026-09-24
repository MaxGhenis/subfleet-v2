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


def test_a_queued_message_cancelled_before_dispatch_stays_cancelled(svc):
    """C-24.7: the ordinary withdrawal of a queued message."""
    cid = conversation(svc)
    mid = submit(svc, cid)
    assert svc.handle("message.cancel", {"message_id": mid}, None)["state"] == "cancelled"
    svc._dispatch()
    assert svc.daemon.submits == []


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
