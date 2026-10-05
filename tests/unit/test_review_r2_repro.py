"""Review r2 of PR #127 (not for merge). Each prior finding's ORIGINAL scenario, asserting
the corrected behaviour (so each fails on 7504f3b8 and passes once fixed), then new
findings asserting the behaviour a sound build must have. Self-contained, so the same
file runs against 7504f3b8 and e3b40d6f."""
import json
import os
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from subfleet.conversations import catalog, wakes
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401

T0 = datetime(2026, 10, 4, 3, 0, tzinfo=UTC).timestamp()


def bound(svc):
    return conversation(svc, native_session_id=str(uuid.uuid4()))


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def wake_rows(svc, cid):
    return svc.store.query("SELECT * FROM messages WHERE conversation_id=? AND origin='wake' ORDER BY seq", (cid,))


def wake_texts(svc, cid):
    return [svc.store.message_text(svc.store.message(r["message_id"])) for r in wake_rows(svc, cid)]


def settle_wakes(svc, cid):
    for m in wake_rows(svc, cid):
        if m["state"] != "complete":
            svc.store.set_state(m["message_id"], "complete")


def turn_job(svc, cid, n, state="succeeded"):
    job_id = f"turn-job-{cid[:8]}-{n}"
    svc.daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest="fixture", kind="turn",
                            name=f"turn-{cid}", state=state, workdir=svc.test_workspace,
                            prompt_path="fixture.md", sandbox="read-only")
    return job_id


def run_under(svc, cid, parent, job_id, state="succeeded"):
    svc.daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest="fixture", kind="dispatch",
                            parent_job_id=parent, caller_session=svc.store.conversation(cid)["native_session_id"],
                            state=state, out_path=f"/work/{job_id}.md", workdir=svc.test_workspace,
                            prompt_path="fixture.md", sandbox="read-only")


def fake_gh(tmp_path, monkeypatch, body: dict, rc: int = 0, sleep_s: float = 0.0):
    """A real `gh` executable on PATH: query_prs runs it through subprocess as in production."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    reply = tmp_path / "gh-reply.json"
    reply.write_text(json.dumps(body))
    calls = tmp_path / "gh-calls"
    script = bindir / "gh"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\necho call >> '{calls}'\nsleep {sleep_s}\n"
                      f"cat '{reply}'\nexit {rc}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return calls


def pr_node(state="OPEN", checks=(("COMPLETED", "SUCCESS", "2026-10-03T00:00:00Z"),), reviews=(), head="abc",
            merged_at=None, closed_at=None):
    return {"pullRequest": {
        "state": state, "headRefOid": head, "mergedAt": merged_at, "closedAt": closed_at,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
            "nodes": [{"__typename": "CheckRun", "name": f"c{i}", "status": s, "conclusion": c, "completedAt": t}
                      for i, (s, c, t) in enumerate(checks)],
            "pageInfo": {"hasNextPage": False}}}}}]},
        "reviews": {"nodes": [{"id": r, "submittedAt": t, "state": "COMMENTED"} for r, t in reviews]}}}


def calls_of(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


# ================================ prior findings ================================

# --- P1 (F1): an unchanged PR wakes on every re-arm ----------------------------------------

def test_prior_f1_rearmed_wait_on_an_unchanged_pr_never_wakes(svc, tmp_path, monkeypatch):
    """CI went green yesterday; the agent waits for a reviewer and re-arms each turn."""
    cid = bound(svc)
    calls = fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node()}})
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    for turn in range(40):
        svc.wakes.from_final(cid, f"m{turn}", "Waiting for review.\nWAKE-ME: prs=o/r#1")
        for _ in range(2):
            clock[0] += 61
            svc.wakes.tick()
        settle_wakes(svc, cid)
        clock[0] += 120
    print(f"R2 F1: {len(wake_rows(svc, cid))} wakes, {calls_of(calls)} gh calls over {(clock[0]-T0)/3600:.1f} h")
    assert calls_of(calls) >= 40                 # the PR really was polled
    assert wake_rows(svc, cid) == []


def test_prior_f1_control_checks_finishing_after_registration_wake_once(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    svc.wakes.from_final(cid, "m0", "WAKE-ME: prs=o/r#1")
    clock[0] += 61
    svc.wakes.tick()
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(clock[0] + 10)),))}})
    clock[0] += 61
    svc.wakes.tick()
    assert wake_texts(svc, cid) == ["[Subfleet]\nPR state changed: o/r#1"]


# --- P1 (F2): one unresolvable PR silences every conversation's PR wakes --------------------

@pytest.mark.parametrize("rc", [1, 0])
def test_prior_f2_a_missing_pr_does_not_silence_another_conversations_merge(svc, tmp_path, monkeypatch, rc):
    good, bad = bound(svc), bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    svc.wakes.register(good, str(uuid.uuid4()), wakes.normalize(prs=["o/r#1"], now=clock[0]))
    svc.wakes.register(bad, str(uuid.uuid4()), wakes.normalize(prs=["o/r#999999"], now=clock[0]))
    missing = {"p1": {"pullRequest": None}}
    errors = [{"type": "NOT_FOUND", "path": ["p1", "pullRequest"],
               "message": "Could not resolve to a PullRequest with the number of 999999."}]
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),)), **missing},
                                    "errors": errors}, rc=rc)
    clock[0] += 61
    svc.wakes.tick()
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(state="MERGED", merged_at=iso(clock[0] + 5)), **missing},
                                    "errors": errors}, rc=rc)
    for _ in range(3):
        clock[0] += 61
        svc.wakes.tick()
    texts = wake_texts(svc, good)
    print(f"R2 F2 rc={rc}: good={texts} bad={wake_texts(svc, bad)}")
    assert len(texts) == 1 and "PR state changed: o/r#1" in texts[0]


# --- P2 (F3): the PR poll holds a person's dispatch ------------------------------------------

def test_prior_f3_slow_gh_does_not_delay_a_persons_dispatch(svc, tmp_path, monkeypatch):
    waiting, person = bound(svc), bound(svc)
    svc.wakes.now = lambda: T0
    svc.wakes.register(waiting, str(uuid.uuid4()), wakes.normalize(prs=["o/r#1"], now=T0))
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}}, sleep_s=3)
    mid = submit(svc, person, "please run now")
    dispatched = []
    original = svc._dispatch_one
    started = time.monotonic()
    monkeypatch.setattr(svc, "_dispatch_one", lambda m: (dispatched.append((m["message_id"], time.monotonic() - started)),
                                                        original(m)))
    svc.wakes.now = lambda: T0 + 61
    svc.tick()
    print(f"R2 F3: person's message reached dispatch after {dispatched[0][1]:.2f} s (gh sleeps 3 s)")
    assert dispatched and dispatched[0][0] == mid and dispatched[0][1] < 1.5


# --- P2 (F4): a runs= request for runs already announced sends an empty wake ------------------

def test_prior_f4_rerequesting_an_announced_run_sends_no_empty_wake(svc):
    cid = bound(svc)
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "run-x")
    svc.wakes.tick()
    settle_wakes(svc, cid)
    for n in (2, 3):
        svc.wakes.from_final(cid, f"turn-{n}-message", "Checked in.\nWAKE-ME: runs=run-x")
        svc.wakes.tick()
        settle_wakes(svc, cid)
    texts = wake_texts(svc, cid)
    print("R2 F4:", texts)
    assert texts == ["[Subfleet]\nrun-x finished: succeeded; deliverable /work/run-x.md"]


# --- P2 (F5): WAKE-ME requests dropped silently ----------------------------------------------

@pytest.mark.parametrize("text", [
    "Done.\nWAKE-ME: prs=o/r#1\nWAITING ON MAX (d123)",       # close-out after the request
    "- WAKE-ME: prs=o/r#1",                                     # bulleted
    "**WAKE-ME:** prs=o/r#1",                                   # bold
    "WAKE-ME: runs= prs=o/r#1",                                 # empty field, as in the real transcripts
], ids=["closeout", "bullet", "bold", "empty-field"])
def test_prior_f5_forms_register(svc, text):
    cid = bound(svc)
    svc.wakes.from_final(cid, str(uuid.uuid4()), text)
    assert [r["kind"] for r in svc.store.query("SELECT kind FROM wake_requests WHERE conversation_id=?", (cid,))] == ["pr"]


def test_prior_f5_one_bad_line_keeps_the_good_line(svc):
    cid = bound(svc)
    svc.wakes.from_final(cid, str(uuid.uuid4()), "WAKE-ME: prs=o/r#1\nWAKE-ME: runs=b, c")
    assert [r["kind"] for r in svc.store.query("SELECT kind FROM wake_requests WHERE conversation_id=?", (cid,))] == ["pr"]


def test_prior_f5_timer_set_five_minutes_out_during_the_turn_is_kept(svc):
    cid = bound(svc)
    mid = submit(svc, cid, "work, then check back")
    with svc.store.transaction() as tx:                    # the turn started at T0
        tx.execute("UPDATE messages SET created_at=? WHERE message_id=?", (iso(T0), mid))
    svc.wakes.now = lambda: T0 + 180                       # it settles 3 minutes later
    svc.wakes.from_final(cid, mid, f"Back soon.\nWAKE-ME: at={iso(T0 + 310)}")
    assert [r["kind"] for r in svc.store.query("SELECT kind FROM wake_requests WHERE conversation_id=?", (cid,))] == ["time"]


# --- P2 (F6): per-run wakes spend the throttle a fan-out needs ---------------------------------

def test_prior_f6_all_of_fanout_wakes_once(svc):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    ids = [f"fan-{i}" for i in range(10)]
    for job_id in ids:
        run_under(svc, cid, t1, job_id, state="running")
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=ids, now=clock[0]))
    for job_id in ids:
        svc.daemon.store.update_job(job_id, state="succeeded")
        svc.wakes.tick()
        settle_wakes(svc, cid)
        clock[0] += 180
    texts = wake_texts(svc, cid)
    streak = svc.store.conversation(cid)["wake_streak"]
    print(f"R2 F6: {len(texts)} wakes, streak {streak}")
    assert len(texts) == 1 and all(f"{j} finished" in texts[0] for j in ids) and streak == 1


# --- P2 (F8 / P3): the moot-block check runs every tick ----------------------------------------

def test_prior_f8_moot_block_writer_check_is_paced(svc, monkeypatch):
    cid = bound(svc)
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    c = svc.store.conversation(cid)
    blocked = datetime.fromisoformat(c["blocked_at"].replace("Z", "+00:00")).timestamp()
    (svc.root / "catalog.json").write_text(json.dumps({"native_records": {
        f"claude:{c['native_session_id']}": {"path": "/x.jsonl", "mtime": blocked + 5}}}))
    holder = Mock(return_value=[4242])
    monkeypatch.setattr(catalog, "external_writers", holder)
    for _ in range(100):
        svc._moot_blocks()
    print(f"R2 F8: {holder.call_count} writer checks in 100 ticks")
    assert holder.call_count == 1


# --- P2 (F9): a mid-turn user row relabels the rest of a Subfleet turn ---------------------------

def _row(kind, uuid_, text, ts, **extra):
    return json.dumps({"type": kind, "uuid": uuid_, "timestamp": ts,
                       "message": {"role": kind, "content": [{"type": "text", "text": text}]}, **extra})


@pytest.mark.parametrize("marker", [
    {"text": "[Request interrupted by user]"},
    {"text": "This session is being continued from a previous conversation...", "isCompactSummary": True},
    {"text": "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"},
], ids=["interrupt", "compact", "task-notification"])
def test_prior_f9_mid_turn_markers_keep_subfleet_ownership(tmp_path, marker):
    from subfleet.conversations.history import _claude_items
    owned_id = str(uuid.uuid4())
    marker = dict(marker)
    text = marker.pop("text")
    rows = [_row("user", owned_id, "Subfleet prompt", "2026-10-04T03:00:00Z"),
            _row("assistant", str(uuid.uuid4()), "first half", "2026-10-04T03:00:01Z"),
            _row("user", str(uuid.uuid4()), text, "2026-10-04T03:00:02Z", **marker),
            _row("assistant", str(uuid.uuid4()), "second half", "2026-10-04T03:00:03Z")]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(rows) + "\n")
    items, _ = _claude_items(path, None, 50, owned={owned_id})
    by_text = {i["text"]: i.get("source") for i in items}
    assert by_text["first half"] == by_text["second half"] == "subfleet"


def test_prior_f9_control_a_real_other_app_prompt_still_starts_an_other_app_group(tmp_path):
    from subfleet.conversations.history import _claude_items
    owned_id = str(uuid.uuid4())
    rows = [_row("user", owned_id, "Subfleet prompt", "2026-10-04T03:00:00Z"),
            _row("assistant", str(uuid.uuid4()), "subfleet answer", "2026-10-04T03:00:01Z"),
            _row("user", str(uuid.uuid4()), "a prompt typed in the Claude app", "2026-10-04T04:00:00Z"),
            _row("assistant", str(uuid.uuid4()), "claude app answer", "2026-10-04T04:00:01Z")]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(rows) + "\n")
    items, _ = _claude_items(path, None, 50, owned={owned_id})
    by_text = {i["text"]: i.get("source") for i in items}
    assert by_text["subfleet answer"] == "subfleet" and by_text["claude app answer"] == "other-app"


# --- P1 CI proxy (F10): history is empty until the catalog lists the transcript ------------------

def test_prior_f10_fresh_conversation_history_before_the_catalog(svc):
    from subfleet.adapters.claude import encode_project_dir
    from subfleet.sessions import transcripts
    sid = str(uuid.uuid4())
    path = transcripts.projects_dir() / encode_project_dir(svc.test_workspace) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_row("user", str(uuid.uuid4()), "hi", "2026-10-04T03:00:00Z") + "\n" +
                    _row("assistant", str(uuid.uuid4()), "hello", "2026-10-04T03:00:01Z") + "\n")
    cid = conversation(svc, native_session_id=sid)
    page = svc.op_conversation_history({"conversation_id": cid}, None)
    assert not page.get("missing") and [i["text"] for i in page["items"]] == ["hello", "hi"]


# --- P2 (F11): the first tick after upgrade wakes conversations about old runs ---------------------

def test_prior_f11_old_completions_do_not_wake_on_the_first_tick(svc):
    cid = bound(svc)
    with svc.store.transaction() as tx:
        tx.execute("UPDATE conversations SET created_at='2026-09-20T00:00:00.000Z' WHERE conversation_id=?", (cid,))
    svc.daemon.store.add_job(job_id="old-turn", request_id="old-turn", payload_digest="f", kind="turn",
                            name=f"turn-{cid}", state="succeeded", created_at="2026-09-28T10:00:00Z",
                            finished_at="2026-09-28T10:30:00Z", workdir=svc.test_workspace, prompt_path="p",
                            sandbox="read-only")
    svc.daemon.store.add_job(job_id="old-run", request_id="old-run", payload_digest="f", kind="dispatch",
                            caller_session=svc.store.conversation(cid)["native_session_id"], state="succeeded",
                            created_at="2026-09-28T10:05:00Z", finished_at="2026-09-28T10:20:00Z",
                            out_path="/work/old.md", workdir=svc.test_workspace, prompt_path="p", sandbox="read-only")
    svc.wakes.tick()
    assert wake_texts(svc, cid) == []


# --- P2 (F12): per-tick work grows with history ----------------------------------------------------

def test_prior_f12_notice_repair_cost_does_not_grow_with_history(svc):
    cids = [bound(svc) for _ in range(40)]
    mid = submit(svc, cids[0], "person")
    svc.store.set_state(mid, "complete")
    with svc.daemon.store.transaction() as tx:
        for i in range(1_000):
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,created_at,workdir,prompt_path,"
                       "sandbox,caller_session) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"j{i}", f"j{i}", "f", "dispatch", "succeeded", "2026-10-01T00:00:00Z", svc.test_workspace,
                        "p", "read-only", f"other-{i % 300}"))
            tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) VALUES(?,?,?,?,?)",
                       (f"j{i}", f"other-{i % 300}", "t", "acknowledged" if i % 2 else "pending", "2026-10-01T00:00:00Z"))
    with svc.store.transaction() as tx:
        for i in range(5_000):
            tx.execute("INSERT INTO wake_runs VALUES(?,?,?)", (cids[i % 40], f"j{i}", mid))
    svc.wakes._surface_notices()                 # the one upgrade repair, if any
    started = time.process_time()
    for _ in range(20):
        svc.wakes._surface_notices()
    surface = (time.process_time() - started) / 20
    print(f"R2 F12: _surface_notices {surface * 1000:.2f} ms CPU per tick with 5,000 delivered runs")
    assert surface < 0.005


# --- P3 (F13): Codex turns lose their session id ----------------------------------------------------

def test_prior_f13_codex_turn_keeps_its_session_id(tmp_path):
    from types import SimpleNamespace
    from subfleet.conversations import launch as launch_module
    turn = {"conversation_id": "c", "message_id": str(uuid.uuid4()), "provider": "codex", "text": "hi",
            "settings": {**SETTINGS, "model": "codex"}, "native_session_id": "thread-123", "cwd": str(tmp_path)}
    lane = SimpleNamespace(home=str(tmp_path / "home"), credential=SimpleNamespace(ref=None), lane_id="codex-1",
                           identity="i", label="l", email=None)
    launched = launch_module.codex_launch(turn, attempt_id="turn-job/a1", attempt_dir=tmp_path, lane=lane,
                                          credential_env={}, model_id="gpt", executable="codex", override="hooks={}")
    env = {**launched.env_add}
    for key in launched.env_remove:                 # daemon.py `_launch`: update, then pop each removed key
        env.pop(key, None)
    assert env.get("SUBFLEET_SESSION_ID") == "thread-123" and env["SUBFLEET_TURN_JOB"] == "turn-job"


# --- P3 (F14): conversation.open queues behind file work ---------------------------------------------

def test_prior_f14_open_is_not_queued_behind_two_slow_file_ops(svc):
    cid = bound(svc)
    release = threading.Event()
    blockers = [svc.files.submit(release.wait, 30) for _ in range(2)]
    try:
        opened = svc.pool_for("conversation.open").submit(svc.op_conversation_open, {"conversation_id": cid}, None)
        opened.result(timeout=5)
    finally:
        release.set()
        for b in blockers:
            b.result(timeout=5)


# ================================ new findings ================================

# --- N2: a request whose runs were already announced silently consumes its timer and PR watch -------

@pytest.mark.parametrize("alternative", ["at", "prs"])
def test_new_stale_runs_on_a_combined_line_cancel_its_other_triggers(svc, tmp_path, monkeypatch, alternative):
    """The woken turn re-lists the run it was told about (agents do: the hub's 10/4 lines repeat
    5-7 run ids) on the one documented line form, adding a check-back. That check-back must fire."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "run-x")
    svc.wakes.tick()                                       # wake 1: run-x finished
    settle_wakes(svc, cid)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    other = f"at={iso(clock[0] + 1800)}" if alternative == "at" else "prs=o/r#1"
    svc.wakes.from_final(cid, "turn-2", f"Pushed the fix; CI is running.\nWAKE-ME: runs=run-x {other} "
                                        'note="check CI on the fix"')
    svc.wakes.tick()
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "SUCCESS", iso(clock[0] + 600)),))}})
    for _ in range(40):                                    # the next 40 minutes
        clock[0] += 61
        svc.wakes.tick()
    states = svc.store.query("SELECT kind,state FROM wake_requests WHERE conversation_id=? ORDER BY kind", (cid,))
    texts = wake_texts(svc, cid)
    print(f"R2 N2 {alternative}: wakes={len(texts)} requests={states}")
    assert len(texts) == 2, f"the {alternative} check-back never fired: {states}"


# --- N3: a PR event during the woken turn is never announced once the agent re-arms ------------------

def test_new_ci_result_for_a_fix_pushed_during_the_woken_turn_is_announced(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),), head="h1")}})
    svc.wakes.from_final(cid, "turn-1", "Waiting for CI.\nWAKE-ME: prs=o/r#1")
    clock[0] += 61
    svc.wakes.tick()                                       # baseline: h1 running
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(clock[0] + 20)),),
                                                           head="h1")}})
    clock[0] += 61
    svc.wakes.tick()                                       # h1 failed: wake 1
    assert len(wake_rows(svc, cid)) == 1
    # Turn 2 (the wake) starts at once, pushes h2 a minute in; h2's CI takes 4 minutes;
    # the agent finishes other work and ends the turn 10 minutes in, re-arming the watch.
    turn_start = clock[0] + 5
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "SUCCESS", iso(turn_start + 300)),),
                                                           head="h2")}})
    clock[0] = turn_start + 600
    settle_wakes(svc, cid)
    svc.wakes.from_final(cid, "turn-2", "Pushed a fix for the failing test; waiting for CI.\nWAKE-ME: prs=o/r#1")
    for _ in range(6 * 60):                                # six hours, nothing else changes
        clock[0] += 61
        svc.wakes.tick()
    texts = wake_texts(svc, cid)
    print(f"R2 N3: wakes={len(texts)} last={texts[-1]!r}")
    assert len(texts) == 2, "h2's CI result reached nobody; the conversation waits for good"


# --- N4: a refused PR watch re-armed every turn: how far the throttle lets it run ---------------------

def test_new_refused_pr_rearmed_each_turn_over_one_night(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}},
                                    "errors": [{"type": "NOT_FOUND", "path": ["p0", "pullRequest"],
                                                "message": "Could not resolve to a PullRequest"}]}, rc=1)
    turns = 0
    svc.wakes.from_final(cid, "t0", "Still waiting on the PR.\nWAKE-ME: prs=o/r#7")
    while clock[0] < T0 + 8 * 3600:
        svc.wakes.tick()
        if [m for m in wake_rows(svc, cid) if m["state"] != "complete"]:
            turns += 1
            settle_wakes(svc, cid)
            clock[0] += 120                                # each woken turn runs ~2 minutes, then re-arms
            svc.wakes.from_final(cid, f"t{turns}", "Still waiting on the PR.\nWAKE-ME: prs=o/r#7")
        clock[0] += 61
    print(f"R2 N4: {turns} 'PR watch refused' turns in 8 h for one conversation")


# --- N5: steady-state tick cost while 40 conversations wait overnight ---------------------------------

def test_new_steady_state_tick_cost_with_forty_waiting_conversations(svc, monkeypatch):
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    commits = [0]
    original = svc.store.notify
    monkeypatch.setattr(svc.store, "notify", lambda: (commits.__setitem__(0, commits[0] + 1), original()))
    for n in range(40):
        cid = bound(svc)
        t = turn_job(svc, cid, n)
        ids = [f"w{n}-{i}" for i in range(16)]
        for job_id in ids:
            run_under(svc, cid, t, job_id, state="running")
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=ids, at=iso(clock[0] + 6 * 3600),
                                                                   now=clock[0]))
    svc.wakes.tick()
    commits[0] = 0
    started_cpu, started = time.process_time(), time.monotonic()
    for _ in range(20):                                    # one second of control-loop ticks
        svc.wakes.tick()
    cpu, wall = (time.process_time() - started_cpu) / 20, (time.monotonic() - started) / 20
    print(f"R2 N5: per tick {cpu * 1000:.1f} ms CPU, {wall * 1000:.1f} ms wall, "
          f"{commits[0] / 20:.0f} BEGIN IMMEDIATE commits + notify_all; at 20 ticks/s that is "
          f"{commits[0]:.0f} write transactions per second with nothing ready")


# --- N6: a turn that waited for capacity may set a timer that is already in the past ------------

def test_new_final_text_timer_in_the_past_after_a_capacity_wait(svc):
    cid = bound(svc)
    mid = submit(svc, cid, "work, then check back")
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:            # the turn job was created at T0 and waited 3 h for a lane
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET job_id=?, state='complete' WHERE message_id=?", (job, mid))
    svc.wakes.now = lambda: T0 + 3 * 3600
    svc.wakes.from_final(cid, mid, f"Check again in ten minutes.\nWAKE-ME: at={iso(T0 + 600)}")
    kinds = [r["kind"] for r in svc.store.query("SELECT kind FROM wake_requests WHERE conversation_id=?", (cid,))]
    svc.wakes.tick()
    print(f"R2 N6: requests={kinds} wakes={len(wake_rows(svc, cid))} (timer {(T0 + 600) - (T0 + 3 * 3600):.0f} s from now)")


# --- N5b: what the per-tick commits cost a long-poll reader (the app's watch) ----------------------

def test_new_waiting_conversations_wake_every_long_poll_reader_each_tick(svc):
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    for n in range(40):
        cid = bound(svc)
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(clock[0] + 6 * 3600), now=clock[0]))
    svc.wakes.tick()
    checks, stop = [0], threading.Event()

    def predicate():                                       # what a conversation.watch long poll re-evaluates
        checks[0] += 1
        svc.store.query("SELECT MAX(seq) FROM changes")
        return stop.is_set()
    reader = threading.Thread(target=svc.store.wait, args=(predicate, 30))
    reader.start()
    time.sleep(0.2)
    checks[0] = 0
    started = time.monotonic()
    ticks = 0
    while time.monotonic() - started < 1.0:
        svc.wakes.tick()
        ticks += 1
    seen = checks[0]
    stop.set()
    svc.store.notify()
    reader.join(5)
    print(f"R2 N5b: 40 conversations waiting on timers 6 h away: {ticks} ticks in 1 s; one idle long-poll "
          f"reader re-ran its predicate {seen} times (nothing changed)")


# --- N7: retention prunes one target of an all-of request; the rest never wake anyone --------------

def test_new_pruned_target_of_an_all_of_request_strands_the_others(svc):
    cid = bound(svc)
    t1 = turn_job(svc, cid, 1)
    for job_id in ("quick", "slow"):
        run_under(svc, cid, t1, job_id, state="running")
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=["quick", "slow"], now=svc.wakes.now()))
    svc.daemon.store.update_job("quick", state="succeeded")
    svc.wakes.tick()
    with svc.daemon.store.transaction() as tx:             # retention removes the finished job's row
        tx.execute("DELETE FROM jobs WHERE job_id='quick'")
    svc.daemon.store.update_job("slow", state="succeeded")
    for _ in range(3):
        svc.wakes.tick()
    print(f"R2 N7: wakes={len(wake_rows(svc, cid))} after both runs finished and one was pruned")
