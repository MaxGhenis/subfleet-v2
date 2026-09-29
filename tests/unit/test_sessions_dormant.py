"""Dormant sessions: killed mid-turn, their process gone, nothing restarting them
(C-23.56 to C-23.60). Every test names the clause it proves (C-20.5).

The world is built in `tmp_path`: a `~/.claude` (`SUBFLEET_CLAUDE_DIR`), a
desktop session store (`SUBFLEET_SESSION_STORE`) and no app log, so the app's
loaded folder is the one written last. `FakeSessions` stands in for the
daemon's `sessions` op and a recording `FakeConversations` for the
conversation service. No test reads the operator's own files or runs `ps`:
the process table is passed in.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet.sessions import dormant, facts, registry, revive, transcripts
from tests import sessions_fixtures as fx

NOW = fx.NOW
ACCOUNT, ORG = "acct-1", "org-1"
S1 = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
S2 = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
S3 = "0b5d9c7e-1a2b-4c3d-8e9f-a0b1c2d3e4f5"
OPUS = "claude-opus-5-5"
DEAD = dormant.Processes(starts={}, named=frozenset())
EMPTY = registry.Reading()


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_root = tmp_path / "state"
    (state_root / "sessions").mkdir(parents=True)
    return {"home": home, "store": store, "cwd": str(workdir), "root": state_root,
            "tmp": tmp_path}


def dormant_session(world, session_id: str = S1, *, age_s: float = 1800, entries=None,
                    model: str = OPUS, quiet_s: float = 3600, cwd: str | None = None,
                    account: str = ACCOUNT, org: str = ORG, **record) -> Path:
    """A desktop session interrupted `age_s` ago whose transcript has been quiet
    for `quiet_s`: its record in the store, its transcript where the record's
    cwd puts it."""
    where = cwd or world["cwd"]
    rows = entries if entries is not None else fx.interrupted(age_s=age_s)
    path = fx.transcript(world["home"], session_id, rows, cwd=where)
    stamp = NOW.timestamp() - quiet_s
    os.utime(path, (stamp, stamp))
    fx.index_entry(world["store"], account, org, session_id, cwd=where, model=model,
                   title=f"session {session_id[:4]}", last_activity=int(stamp * 1000), **record)
    return path


def scan(world, facts_answer=None, **kwargs):
    kwargs.setdefault("processes", DEAD)
    kwargs.setdefault("reading", EMPTY)
    return dormant.scan(facts_answer or fx.FakeSessions().state(None), fx.policy(), now=NOW,
                        state_root=world["root"], **kwargs)


def row_of(result, session_id=S1):
    return next(item for item in result.rows if item.session_id == session_id)


# --- the scan (C-23.56 to C-23.58) ------------------------------------------------

def test_c23_56_a_dead_quiet_desktop_session_cut_off_mid_turn_is_eligible(world):
    """C-23.56, C-23.58: the 2026-09-27 shape. A desktop session whose last turn
    stopped mid-tool, with no process and a quiet transcript, is a wake candidate."""
    dormant_session(world)
    item = row_of(scan(world))
    assert item.verdict.state == "interrupted" and item.eligible, item.reason
    assert item.record.local_id == f"local_{S1}" and item.record.model == OPUS


@pytest.mark.parametrize("entries, state", [
    (fx.completed(), "completed"),
    (fx.stopped(), "completed"),
    ([fx.typed_prompt("do it", at=fx.ago(1800))], "interrupted"),
    ([fx.typed_prompt("go", at=fx.ago(1900)), fx.assistant_tool_use(at=fx.ago(1850)),
      fx.user_tool_result(at=fx.ago(1800))], "interrupted"),
    ([fx.typed_prompt("go", at=fx.ago(1900)),
      fx.user_text("<task-notification> <task-id>w1</task-id> done", at=fx.ago(1800))], "interrupted"),
])
def test_c23_56_each_tail_shape_gets_its_verdict(world, entries, state):
    """C-23.56: the three mid-turn shapes (a tool call with no result, a result
    never continued from, an unanswered prompt such as a task notification) are
    interrupted; a finished turn and an Esc stop are completed."""
    dormant_session(world, entries=entries)
    assert row_of(scan(world)).verdict.state == state


def test_c23_56_a_recent_write_keeps_a_dead_looking_session_active(world):
    """C-23.56: a process inside a long tool call writes nothing to its main
    transcript, so a death is believed only after the quiet window (600 s)."""
    dormant_session(world, quiet_s=120)
    item = row_of(scan(world))
    assert not item.eligible and item.reason.startswith("active") and "quiet" in item.reason


def test_c23_56_a_subagent_still_writing_keeps_it_active(world):
    """C-23.56: a workflow's agents write under the session's side directory while
    the main transcript stays still; any write there within the window counts."""
    path = dormant_session(world, quiet_s=3600)
    side = path.with_suffix("") / "subagents"
    side.mkdir(parents=True)
    (side / "agent-1.jsonl").write_text("{}\n")
    old = NOW.timestamp() - 3600
    for entry in (side / "agent-1.jsonl", side, side.parent):
        os.utime(entry, (old, old))
    assert row_of(scan(world)).eligible             # quiet everywhere: dead and quiet
    recent = NOW.timestamp() - 30
    os.utime(side / "agent-1.jsonl", (recent, recent))
    assert row_of(scan(world)).reason.startswith("active")
    assert dormant.last_write(path) == pytest.approx(recent)


def test_c23_56_a_side_directory_too_large_to_read_is_not_quiet(world, monkeypatch):
    """C-23.56: past the entry limit the quiet time is unknown, never assumed."""
    path = dormant_session(world)
    side = path.with_suffix("")
    side.mkdir()
    old = NOW.timestamp() - 3600
    for index in range(4):
        (side / f"f{index}").write_text("x")
        os.utime(side / f"f{index}", (old, old))
    os.utime(side, (old, old))
    monkeypatch.setattr(dormant, "SIDE_ENTRY_LIMIT", 2)
    assert dormant.last_write(path) is None
    assert row_of(scan(world)).reason.startswith("active")


@pytest.mark.parametrize("how", ["registry", "argv-equals", "argv-space", "argv-short",
                                 "argv-session-id"])
def test_c23_57_a_running_process_makes_it_active(world, how):
    """C-23.57: a registry row of a live pid, or a command line naming the session
    after --resume=, --resume, -r or --session-id, is a live session."""
    dormant_session(world)
    reading = EMPTY
    processes = DEAD
    if how == "registry":
        reading = registry.Reading(rows=(_row(S1, 4242, "Mon Sep 28 17:19:00 2026"),))
        processes = dormant.Processes(starts={4242: "Mon Sep 28 17:19:00 2026"}, named=frozenset())
    else:
        form = {"argv-equals": "--resume={}", "argv-space": "--resume {}", "argv-short": "-r {}",
                "argv-session-id": "--session-id {}"}[how]
        processes = dormant.parse_processes(
            f"  777 S    Mon Sep 28 17:19:00 2026     /Applications/Claude.app/claude --verbose "
            f"{form.format(S1)}")
    item = row_of(scan(world, reading=reading, processes=processes))
    assert not item.eligible and "running" in item.reason


def _row(session_id, pid, proc_start=None):
    return registry.SessionRow(session_id=session_id, pid=pid, socket=None, name=None,
                               cwd=None, started_at=None, alive=True, socket_present=False,
                               registry_path=f"/r/{pid}.json", proc_start=proc_start)


def test_c23_57_a_registry_row_of_a_reused_pid_is_not_evidence(world):
    """C-23.57: a row whose recorded start differs from the live pid's is stale
    (the pid was reused), so it does not hold the wake back."""
    dormant_session(world)
    reading = registry.Reading(rows=(_row(S1, 4242, "Mon Sep 28 17:19:00 2026"),))
    processes = dormant.Processes(starts={4242: "Tue Sep 29 09:00:00 2026"}, named=frozenset())
    assert row_of(scan(world, reading=reading, processes=processes)).eligible


@pytest.mark.parametrize("processes, reading", [
    (None, EMPTY),
    (DEAD, registry.Reading(error="cannot list: permission denied")),
    (dormant.Processes(starts={31: "Mon Sep 28 17:19:00 2026"}, named=frozenset()),
     registry.Reading(unreadable=(31,))),
    (DEAD, registry.Reading(unreadable=(None,))),
])
def test_c23_57_an_inspection_failure_is_never_a_death(world, processes, reading):
    """C-23.57 (C-4.2): an unreadable process table, an unlistable registry, or an
    unreadable registry file of a running pid leaves liveness unknown, and an
    unknown session is not woken."""
    dormant_session(world)
    item = row_of(scan(world, processes=processes, reading=reading))
    assert not item.eligible and "inspection failure" in item.reason


def test_c23_57_an_unreadable_row_of_a_dead_pid_is_ignored(world):
    """C-23.57: a registry file left by a process that is gone says nothing."""
    dormant_session(world)
    assert row_of(scan(world, reading=registry.Reading(unreadable=(31,)))).eligible


def test_c23_57_registry_read_reports_what_it_could_not_read(tmp_path, monkeypatch):
    """C-23.57: `registry.read` names the pid of a file it could not read and the
    directory it could not list, where `rows()` would have said "no rows"."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.register(home, S1, 4242)
    (home / "sessions" / "31.json").write_text("{torn")
    reading = registry.read()
    assert [item.session_id for item in reading.rows] == [S1]
    assert reading.unreadable == (31,) and reading.error is None
    assert registry.read(tmp_path / "missing") == registry.Reading()


@pytest.mark.parametrize("record, reason", [
    ({"archived": True}, "archived"),
    ({"scheduledTaskId": "task_1"}, "scheduled-task"),
    ({"model": "claude-fable-5-1"}, "model claude-fable-5-1"),
    ({"model": "claude-opus-5-5[1m]"}, "model claude-opus-5-5[1m]"),
])
def test_c23_58_archived_scheduled_and_other_models_are_never_woken(world, record, reason):
    """C-23.58: an archived session, a scheduled-task run and a session on any
    model but claude-opus-5-5 are held, with the reason."""
    dormant_session(world, **record)
    item = row_of(scan(world))
    assert item.verdict.state == "interrupted" and not item.eligible
    assert reason in item.reason


def test_c23_58_the_mirrors_merged_flag_archives_it_too(world):
    """C-23.58: the mirror's merged flags (`sessions/mirror-flags.json`) count: an
    archive it has merged but not yet spread to this copy still holds the wake."""
    dormant_session(world)
    (world["root"] / "sessions" / "mirror-flags.json").write_text(
        json.dumps({S1: {"isArchived": True, "isStarred": False}}))
    assert "archived" in row_of(scan(world)).reason


def test_c23_58_a_missing_cwd_is_reported_and_never_created(world):
    """C-23.58: a session whose cwd is gone is named with the path, and nothing
    creates the directory."""
    gone = str(world["tmp"] / "deleted-worktree")
    Path(gone).mkdir()
    dormant_session(world, cwd=gone)
    Path(gone).rmdir()
    fx.transcript(world["home"], S1, fx.interrupted(age_s=1800), cwd=gone)
    path = world["home"] / "projects" / fx.project_slug(gone) / f"{S1}.jsonl"
    stamp = NOW.timestamp() - 3600
    os.utime(path, (stamp, stamp))
    item = row_of(scan(world))
    assert not item.eligible and "missing (reported, not created)" in item.reason
    assert not Path(gone).exists()


def test_c23_58_an_interruption_older_than_the_window_is_left(world):
    """C-23.58: `sessions.wake_max_age_h` (48 h) bounds what is woken; the
    transcript's own mtime already leaves an older one out of the scan."""
    dormant_session(world, age_s=50 * 3600, quiet_s=50 * 3600)
    assert all(item.session_id != S1 for item in scan(world).rows)


def test_c23_58_every_fence_holds(world):
    """C-23.58 (C-23.31, C-23.35, C-26.13): a conversation's session, a lane run
    the daemon launched, a retired session and the session running the pass are
    never woken."""
    for session in (S1, S2, S3):
        dormant_session(world, session)
    daemon = fx.FakeSessions(conversation_sessions=[S1], lane_sessions=[S2],
                             retired={S3: {"reason": "done"}})
    result = scan(world, daemon.state(None))
    assert registry.CONVERSATION_REASON in row_of(result, S1).reason
    assert "headless lane run" in row_of(result, S2).reason
    assert "retired" in row_of(result, S3).reason
    assert not result.eligible
    dormant_session(world, S1)
    assert "this session" in row_of(scan(world, caller=S1.upper()), S1).reason


def test_c23_58_a_chip_is_a_desktop_session_not_a_lane_run(world):
    """C-23.58 (C-23.31): a chip's first prompts arrive through the SDK, as a
    lane's do; its desktop record says it is the app's, so it is judged as one."""
    entries = [fx.headless_prompt("the chip's brief", at=fx.ago(1900)),
               fx.assistant_tool_use(at=fx.ago(1800), model=OPUS)]
    dormant_session(world, entries=entries, spawnedFrom={"sessionId": "local_parent"})
    assert transcripts.headless_transcript(
        world["home"] / "projects" / fx.project_slug(world["cwd"]) / f"{S1}.jsonl")
    assert row_of(scan(world)).eligible


@pytest.mark.parametrize("kind", ["wake", "revive"])
def test_c23_58_an_unanswered_wake_or_revive_is_not_woken_again_unasked(world, kind):
    """C-23.58: a process that died again before answering its wake (or a revive's
    prompt) is not woken at every pass; --force does it once more."""
    marker = transcripts.WAKE_MARKER if kind == "wake" else transcripts.REVIVE_MARKER
    entries = fx.interrupted(age_s=3600) + [fx.user_text(f"{marker} ...", uuid="w", at=fx.ago(1800))]
    dormant_session(world, entries=entries)
    item = row_of(scan(world))
    assert item.turn.state == "tickled" and item.turn.nudge == kind and not item.eligible
    assert "--force" in item.fix
    assert row_of(scan(world, force=True)).eligible


def test_c23_58_a_wake_already_sent_at_this_point_is_not_repeated(world):
    """C-23.58 (C-23.33): one wake per interruption point. A reserved wake at the
    same point holds the next pass, whose fix is --force."""
    dormant_session(world)
    turn = transcripts.turn_state(row_of(scan(world)).transcript, now=NOW)
    daemon = fx.FakeSessions(nudges={S1: {"kind": "wake", "dedupe_key": dormant.wake_key(turn),
                                          "transport": "desktop", "at": fx.ago(60)}})
    item = row_of(scan(world, daemon.state(None)))
    assert not item.eligible and "wake was sent at this interruption point" in item.reason


def test_c23_58_a_tickle_nudge_at_the_same_point_does_not_block_the_wake(world):
    """C-23.58: a nudge sent to the live process before it died is not a wake;
    the wake is reserved under its own key."""
    dormant_session(world)
    turn = transcripts.turn_state(row_of(scan(world)).transcript, now=NOW)
    daemon = fx.FakeSessions(nudges={S1: {"kind": "nudge", "dedupe_key": turn.dedupe_key,
                                          "at": fx.ago(60)}})
    assert row_of(scan(world, daemon.state(None))).eligible


def test_c23_58_copies_merge_on_any_archive_and_the_newest_fields(world):
    """C-23.58: across account folders, an archive in any copy wins, and model,
    cwd and title come from the copy active last (`mirror._rank`)."""
    record = dormant.merge_copies("local_x", [
        ("/a", {"cliSessionId": S1, "model": "claude-fable-5-1", "cwd": "/old", "lastActivityAt": 1}),
        ("/b", {"cliSessionId": S1, "model": OPUS, "cwd": "/new", "lastActivityAt": 5}),
        ("/c", {"cliSessionId": S1, "isArchived": True, "lastActivityAt": 2}),
        ("/d", "not a record")])
    assert (record.model, record.cwd, record.archived, record.copies) == (OPUS, "/new", True, 3)
    assert dormant.merge_copies("local_x", []) is None


# --- pacing against a lane (C-23.59) ------------------------------------------------

class Decision:
    def __init__(self, lane, reason="picked"):
        self.chosen_lane, self.reason = lane, reason


def test_c23_59_the_window_is_the_lane_a_turn_would_run_on(monkeypatch):
    """C-23.59: a conversation wake spends the lane admission would pick for a
    turn, never the desktop login; its newest five-hour account reading is read."""
    from subfleet import scheduler
    seen = {}
    monkeypatch.setattr(scheduler, "evaluate",
                        lambda policy, view, job: seen.setdefault("job", job) and Decision("claude-9"))
    view = {"readings": [
        {"lane_id": "claude-9", "scope": "account", "window": "five_hour", "utilization": 0.10,
         "resets_at": "2026-09-05T13:00:00Z", "label": "provider", "observed_at": "2026-09-05T11:00:00Z"},
        {"lane_id": "claude-9", "scope": "account", "window": "five_hour", "utilization": 0.17,
         "resets_at": "2026-09-05T13:00:00Z", "label": "provider", "observed_at": "2026-09-05T11:20:00Z"},
        {"lane_id": "claude-9", "scope": "account", "window": "seven_day", "utilization": 0.9},
        {"lane_id": "claude-1", "scope": "account", "window": "five_hour", "utilization": 0.01}]}
    window = dormant.lane_window(view, fx.policy(), model=OPUS, now=NOW)
    assert window.percent_used == pytest.approx(17) and window.label == "provider"
    assert "claude-9" in window.source and seen["job"]["kind"] == "turn"
    monkeypatch.setattr(scheduler, "evaluate", lambda policy, view, job: Decision(None, "all closed"))
    assert dormant.lane_window(view, fx.policy(), model=OPUS, now=NOW).percent_used is None
    assert dormant.pace(dormant.lane_window(view, fx.policy(), model=OPUS, now=NOW), now=NOW).allowed == 0


@pytest.mark.parametrize("used, resets_in_s, pending, batch, allowed", [
    (64.0, 1800, 2, 10, 4),        # 90% elapsed: the 70% ceiling binds; running wakes count
    (65.0, 1800, 5, 10, 0),        # the five running wakes already reach 70%
    (40.0, 4 * 3600, 3, 10, 0),    # 20% elapsed + 10 = 30% < 40% used
    (25.0, 4 * 3600, 3, 10, 3),    # 25 + 3 + k <= 30: k in {0, 1, 2}
    (5.0, 3600, 0, 6, 6),          # a cool window: the batch of six binds
    (5.0, 3600, 6, 6, 0),          # six still running: the batch is full
])
def test_c23_59_the_pace_at_its_boundaries(used, resets_in_s, pending, batch, allowed):
    """C-23.59, examples: each running wake has spent its point, and wake k of the
    batch goes out only if used + running + k stays within both limits."""
    reading = dormant.Window(used, NOW + timedelta(seconds=resets_in_s), as_of=NOW, label="provider")
    assert dormant.pace(reading, now=NOW, rule=dormant.PaceRule(batch=batch),
                        pending=pending).allowed == allowed


def test_c23_56_a_quiet_time_equal_to_the_window_is_quiet():
    """C-23.56: the quiet window is inclusive: 600 s of quiet is enough."""
    turn = transcripts.TurnState(state="interrupted", detail="cut")
    assert dormant.classify(turn, "dead", 600.0, window_s=600.0).state == "interrupted"
    assert dormant.classify(turn, "dead", 599.9, window_s=600.0).state == "active"


def test_c23_59_a_zero_window_length_paces_nothing():
    """C-23.59: `wake_window_h: 0` is "no window", which allows no wake."""
    reading = dormant.Window(5.0, NOW + timedelta(hours=1), as_of=NOW, label="provider")
    assert dormant.pace(reading, now=NOW, rule=dormant.PaceRule(window_s=0)).allowed == 0


# --- waking (C-23.60) ----------------------------------------------------------------

class FakeConversations:
    """A recording conversation service: `open` binds, `submit` queues."""

    def __init__(self, *, ready: str | None = None, messages=None, states=None):
        self.opened: list[str] = []
        self.submitted: list[dict] = []
        self._ready = ready
        self.messages = messages or []
        self.states = states or {}

    def ready(self):
        return self._ready

    def open(self, session_id):
        self.opened.append(session_id)
        return {"conversation": {"conversation_id": f"cv-{session_id[:4]}", "native_session_id": session_id,
                                 "settings": {"model": "opus", "effort": None, "fast": False,
                                              "permission": "bypass", "auto_continue": True}},
                "messages": list(self.messages)}

    def submit(self, conversation_id, message_id, text, settings, after):
        self.submitted.append({"conversation_id": conversation_id, "message_id": message_id,
                               "text": text, "settings": dict(settings), "after": after})
        return {"message_id": message_id, "state": "queued"}

    def status(self, ids):
        return [{"message_id": item, "state": self.states.get(item, "complete")} for item in ids]


def probes(**overrides):
    base = dict(processes=lambda: DEAD, reading=lambda: EMPTY,
                copies=lambda store, local, flags: dormant.all_copies(store, local, flags=flags),
                open_facts=lambda path: (True, None, {"cwd": "/w", "permission": "bypass"}),
                branch=lambda workspace, permission: None)
    base.update(overrides)
    return dormant.Probes(**base)


def open_window(used=5.0):
    return dormant.Window(used, NOW + timedelta(hours=1), as_of=NOW, label="provider", source="lane claude-9")


def wake(world, daemon, conversations=None, **kwargs):
    kwargs.setdefault("processes", DEAD)
    kwargs.setdefault("reading", EMPTY)
    kwargs.setdefault("probes", probes())
    kwargs.setdefault("window", open_window())
    transport = kwargs.pop("transport", "conversation")
    return dormant.wake_pass(daemon, fx.policy(), transport=transport,
                             conversations=conversations, now=NOW,
                             state_root=world["root"], **kwargs)


def test_c23_60_a_wake_is_reserved_then_opened_then_submitted(world):
    """C-23.60: the reservation comes first (C-23.33's order), then
    `conversation.open` of the native session, then one message with the full
    settings on claude-opus-5-5 and no predecessor."""
    dormant_session(world)
    daemon, service = fx.FakeSessions(), FakeConversations()
    report = wake(world, daemon, service)
    item = row_of(report.scan)
    assert item.woken and item.conversation_id == "cv-3f9c"
    assert [record["kind"] for record in daemon.records] == ["wake"]
    assert daemon.records[0]["dedupe_key"].startswith("wake:")
    assert daemon.records[0]["detail"]["message_id"] == service.submitted[0]["message_id"]
    assert service.opened == [S1]
    sent = service.submitted[0]
    assert sent["after"] is None and sent["text"].startswith(transcripts.WAKE_MARKER)
    assert sent["settings"] == {"model": OPUS, "effort": None, "fast": False,
                                "permission": "bypass", "auto_continue": True}


def test_c23_60_the_batch_is_six_oldest_first(world):
    """C-23.59, C-23.60: at most six wakes go out, the longest-dead first."""
    ids = [f"{index:08x}-0000-4000-8000-000000000000" for index in range(8)]
    for index, session in enumerate(ids):
        dormant_session(world, session, age_s=1800 + 600 * index)
    service = FakeConversations()
    report = wake(world, fx.FakeSessions(), service)
    assert service.opened == list(reversed(ids))[:6]
    held = [item for item in report.scan.rows if item.eligible and not item.woken]
    assert len(held) == 2 and all(item.reason.startswith("paced") for item in held)


def test_c23_60_running_wakes_take_their_places_in_the_batch(world):
    """C-23.59: a wake whose message is still live has not reported its usage, so
    it counts against both the batch and the window."""
    dormant_session(world)
    running = {f"m{index}": "running" for index in range(6)}
    daemon = fx.FakeSessions(nudges={f"s{index}": {"kind": "wake", "transport": "conversation",
                                                   "message_id": f"m{index}", "at": fx.ago(60),
                                                   "dedupe_key": "wake:x"} for index in range(6)})
    service = FakeConversations(states=running)
    report = wake(world, daemon, service)
    assert report.pace.pending == 6 and report.pace.allowed == 0 and not service.opened


def test_c23_60_a_hot_window_wakes_nothing(world):
    """C-23.59: at 70% used, or ahead of the elapsed share by more than ten
    points, no wake goes out."""
    dormant_session(world)
    for used in (70.0, 95.0):
        service = FakeConversations()
        assert not wake(world, fx.FakeSessions(), service, window=open_window(used)).woken
        assert not service.opened


def test_c23_60_the_strict_check_reads_every_copy_before_acting(world):
    """C-23.58, C-23.60: just before a wake, every account folder's copy is read;
    an archive found there holds the session and does not spend the place."""
    dormant_session(world, S1, age_s=4000)
    dormant_session(world, S2, age_s=2000)
    archived = dormant.DesktopRecord(local_id=f"local_{S1}", cli_session_id=S1, model=OPUS,
                                     cwd=world["cwd"], archived=True)
    service = FakeConversations()
    chosen = probes(copies=lambda store, local, flags: archived if S1 in local
                    else dormant.all_copies(store, local, flags=flags))
    report = wake(world, fx.FakeSessions(), service, probes=chosen,
                  window=open_window(), )
    assert "archived" in row_of(report.scan, S1).reason and service.opened == [S2]


def test_c23_60_the_real_copies_are_read_across_accounts(world):
    """C-23.58: `all_copies` finds the session's record in another account's
    folder and merges its archive in."""
    dormant_session(world)
    fx.index_entry(world["store"], "acct-2", "org-2", S1, cwd=world["cwd"], model=OPUS,
                   archived=True, last_activity=1)
    merged = dormant.all_copies(world["store"], f"local_{S1}")
    assert merged.copies == 2 and merged.archived


def test_c23_60_a_session_that_came_back_is_left_to_its_process(world):
    """C-23.60 (C-23.34): liveness and the last turn are read again just before
    the reservation; a process that appeared since the scan holds the wake."""
    dormant_session(world)
    service = FakeConversations()
    back = dormant.Processes(starts={}, named=frozenset({S1}))
    daemon = fx.FakeSessions()
    report = wake(world, daemon, service, probes=probes(processes=lambda: back))
    assert "active" in row_of(report.scan).reason and not service.opened and not daemon.records


@pytest.mark.parametrize("blocker, expect", [
    ("a Subfleet lane run", "--plan"),
    ("its working directory no longer exists", "handoff"),
    ("tmp-workspace", "handoff"),
])
def test_c23_60_open_is_predicted_never_used_to_find_out(world, blocker, expect):
    """C-23.60 (C-26.13): opening binds a session for good, so what open would say
    is read first (`catalog.claude_session`); a session it would refuse is held
    with the fix, and nothing is reserved or opened."""
    dormant_session(world)
    service, daemon = FakeConversations(), fx.FakeSessions()
    report = wake(world, daemon, service,
                  probes=probes(open_facts=lambda path: (False, blocker, {})))
    item = row_of(report.scan)
    assert not item.woken and expect in item.fix and not service.opened and not daemon.records


def test_c23_60_a_checkout_of_main_is_held(world):
    """C-23.60 (C-13.2, C-26.10): a writable turn on main is refused, and allowing
    main is a person's call, so such a session is held before anything is bound."""
    dormant_session(world)
    service = FakeConversations()
    report = wake(world, fx.FakeSessions(), service,
                  probes=probes(branch=lambda workspace, permission: "a checkout of main"))
    assert "main" in row_of(report.scan).reason and not service.opened


def test_c23_60_the_branch_check_reads_git(tmp_path):
    """C-23.60: `branch_refusal` names main and master and nothing else; a
    read-only turn is never refused."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    assert "main" in dormant.branch_refusal(str(repo), "bypass")
    assert dormant.branch_refusal(str(repo), "read-only") is None
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "task"], check=True)
    assert dormant.branch_refusal(str(repo), "bypass") is None
    assert dormant.branch_refusal(str(tmp_path / "nowhere"), "bypass") is None


def test_c23_60_a_dry_run_reserves_and_sends_nothing(world):
    """C-23.60: a survey decides everything and changes nothing."""
    dormant_session(world)
    daemon, service = fx.FakeSessions(), FakeConversations()
    report = wake(world, daemon, service, dry_run=True)
    assert "would wake" in row_of(report.scan).reason
    assert not daemon.records and not service.opened and not service.submitted


def test_c23_60_a_daemon_without_the_conversation_service_wakes_nothing(world):
    """C-23.60 (C-25.1): no conversation op before `capabilities` names it."""
    dormant_session(world)
    service = FakeConversations(ready="this daemon does not offer conversations.v1")
    report = wake(world, fx.FakeSessions(), service)
    assert not service.opened and "conversations.v1" in row_of(report.scan).reason


def test_c23_60_an_existing_conversation_is_left_to_itself(world):
    """C-23.60 (C-26.13): if another pass bound the session first and sent it a
    message, the wake adds nothing to that conversation."""
    dormant_session(world)
    service = FakeConversations(messages=[{"origin": "person", "message_id": "m0"}])
    report = wake(world, fx.FakeSessions(), service)
    assert not service.submitted and "left to it" in row_of(report.scan).reason


def test_c23_60_the_desktop_transport_plans_and_counts_its_recent_wakes(world):
    """C-23.60: `--plan` reserves each wake and hands back the local id and the
    message for the desktop app's send_message; a planned wake counts as running
    for `wake_settle_min`, then stops counting."""
    for session in (S1, S2):
        dormant_session(world, session)
    daemon = fx.FakeSessions()
    report = wake(world, daemon, None, transport="desktop")
    plan = dormant.plan(report)
    assert {item["local_id"] for item in plan} == {f"local_{S1}", f"local_{S2}"}
    assert all(item["message"].startswith(transcripts.WAKE_MARKER) for item in plan)
    assert {record["detail"]["transport"] for record in daemon.records} == {"desktop"}
    answer = daemon.state(None)
    assert dormant.pending_wakes(answer, transport="desktop", now=NOW, settle_s=1800) == 2
    later = NOW + timedelta(minutes=31)
    assert dormant.pending_wakes(answer, transport="desktop", now=later, settle_s=1800) == 0


def test_c23_60_a_reservation_another_pass_took_is_not_sent_twice(world):
    """C-23.60 (C-23.33): two passes race; the daemon's reservation lets one send."""
    dormant_session(world)

    class Racing(fx.FakeSessions):
        def record_nudge(self, session_id, **kwargs):
            return {"recorded": False, "reason": "already nudged at this interruption point"}

    service = FakeConversations()
    report = wake(world, Racing(), service)
    assert not service.opened and "--force" in row_of(report.scan).fix


@pytest.mark.parametrize("copies, why", [
    (lambda store, local, flags: None, "could not be read"),
    (lambda store, local, flags: dormant.DesktopRecord(local_id=local, cli_session_id=S1, model=OPUS,
                                                      cwd="/w", unreadable=1), "could not be read"),
])
def test_c23_58_an_unread_copy_holds_the_wake(world, copies, why):
    """C-23.58, review r1 (blocking): a copy that cannot be read may be the archived
    one, so the strict check holds the wake rather than trusting the rest."""
    dormant_session(world)
    service, daemon = FakeConversations(), fx.FakeSessions()
    report = wake(world, daemon, service, probes=probes(copies=copies))
    assert why in row_of(report.scan).reason and not service.opened and not daemon.records


def test_c23_58_unreadable_mirror_flags_hold_every_wake(world):
    """C-23.58, review r1: the mirror's merged flags that cannot be read leave the
    archive state unknown for every session, so the pass sends nothing."""
    dormant_session(world)
    (world["root"] / "sessions" / "mirror-flags.json").write_text("{torn")
    service = FakeConversations()
    report = wake(world, fx.FakeSessions(), service)
    assert not service.opened and "flags" in row_of(report.scan).reason


def test_c23_56_a_write_after_the_scan_holds_the_wake(world):
    """C-23.56, review r1: the quiet window is read again just before the
    reservation; a resume stub or a subagent's file written since the scan
    leaves the fingerprint alone but not the quiet time."""
    path = dormant_session(world)
    service = FakeConversations()

    def touched():
        stamp = NOW.timestamp() - 5
        os.utime(path, (stamp, stamp))
        return DEAD

    report = wake(world, fx.FakeSessions(), service, probes=probes(processes=touched))
    assert "written since the scan" in row_of(report.scan).reason and not service.opened


def test_c23_59_one_pass_at_a_time(world):
    """C-23.59, review r1: a second pass while one holds the lock sends nothing,
    so two passes cannot each count the same pending wakes and each send a batch."""
    import fcntl
    dormant_session(world)
    fd = dormant.transcripts.lock_fd(world["root"] / "sessions" / dormant.LOCK_NAME)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        service = FakeConversations()
        report = wake(world, fx.FakeSessions(), service)
        assert not service.opened and "another wake pass" in report.pace.reason
        assert wake(world, fx.FakeSessions(), service, dry_run=True).scan.rows
    finally:
        os.close(fd)
    assert wake(world, fx.FakeSessions(), FakeConversations()).woken


def test_c23_59_a_reserved_wake_not_yet_submitted_counts_as_running():
    """C-23.59, review r1: a reservation whose message the store has not seen yet
    is a wake in flight; it counts while recent."""
    answer = {"sessions": {S1: {"last_nudge": {"kind": "wake", "transport": "conversation",
                                                "message_id": "m1", "at": fx.ago(60)}},
                           S2: {"last_nudge": {"kind": "wake", "transport": "conversation",
                                                "message_id": "m2", "at": fx.ago(7200)}}}}
    unknown = lambda ids: [{"message_id": item, "state": "unknown"} for item in ids]
    assert dormant.pending_wakes(answer, transport="conversation", now=NOW, settle_s=1800,
                                 status=unknown) == 1


@pytest.mark.parametrize("readings, allowed", [
    ([("claude-9", 0.10), ("claude-1", 0.75)], 0),     # a turn may fall to the 75% lane
    ([("claude-9", 0.10), ("claude-1", 0.20)], 6),
    ([("claude-9", 0.10)], 6),
])
def test_c23_59_the_pace_is_the_tightest_lane_a_turn_may_land_on(monkeypatch, readings, allowed):
    """C-23.59, review r1: Claude turns are not pinned, and admission falls to the
    next candidate when the first cannot take a turn; every candidate with a
    current reading can only lower the count."""
    from subfleet import scheduler

    class Picked:
        chosen_lane, chosen_model, reason = readings[0][0], "opus", "ranked"
        evaluations = ({"model": "opus", "candidates": [lane for lane, _u in readings]},)

    monkeypatch.setattr(scheduler, "evaluate", lambda policy, view, job: Picked())
    view = {"readings": [{"lane_id": lane, "scope": "account", "window": "five_hour",
                          "utilization": used, "resets_at": fx.iso(NOW + timedelta(hours=1)),
                          "label": "provider", "observed_at": fx.iso(NOW)} for lane, used in readings]}
    windows = dormant.lane_windows(view, fx.policy(), model=OPUS, now=NOW)
    assert [window.source.split()[1] for window in windows] == [lane for lane, _u in readings]
    assert dormant.pace_lanes(windows, now=NOW).allowed == allowed


def test_c23_59_a_lane_without_a_reading_is_named_not_trusted(monkeypatch):
    """C-23.59: the first lane must have a reading; a fallback without one cannot
    be measured and is named in the reason."""
    first = dormant.Window(10.0, NOW + timedelta(hours=1), as_of=NOW, label="provider", source="lane a")
    blank = dormant.Window(None, None, source="lane b: no five-hour reading")
    decision = dormant.pace_lanes([first, blank], now=NOW)
    assert decision.allowed == 6 and "not measured: lane b" in decision.reason
    assert dormant.pace_lanes([blank, first], now=NOW).allowed == 0
    assert dormant.pace_lanes([], now=NOW).allowed == 0


@pytest.mark.parametrize("as_of", [None, NOW + timedelta(hours=1)])
def test_c23_59_a_reading_with_no_time_or_a_future_one_paces_nothing(as_of):
    """C-23.59, review r1: a reading that does not say when it was taken, or says
    a time well in the future, is not a fresh one."""
    reading = dormant.Window(5.0, NOW + timedelta(hours=2), as_of=as_of, label="provider")
    assert dormant.pace(reading, now=NOW).allowed == 0


class Boom(Exception):
    pass


@pytest.mark.parametrize("output", ["", "   \n", "garbage row\n", "  12 S    Mon Sep 28 17:19:00 2026 x\n"])
def test_c23_57_a_table_without_this_process_is_no_table(output):
    """C-23.57, review r1: `ps` output that is empty, malformed, or does not list
    the process reading it is not a reading of this machine."""
    assert dormant.read_processes(lambda argv: output, own_pid=4242) is None


def test_c23_57_the_real_reader_turns_every_failure_into_unknown(monkeypatch):
    """C-23.57, review r1: the reader itself (not a stub of it) answers None for
    an inspection failure, an OS error and a malformed table, and liveness is
    then unknown."""
    from subfleet import procs

    def failing(error):
        def read(argv):
            raise error
        return read

    for reader in (failing(procs.InspectionError("ps inspection failed (1)")),
                   failing(OSError("fork")), lambda argv: "not a table\n"):
        table = dormant.read_processes(reader, own_pid=4242)
        assert table is None
        assert dormant.liveness(S1, reading=EMPTY, processes=table) == "unknown"
    good = f"  4242 S    Mon Sep 28 17:19:00 2026     python -m subfleet\n"
    assert dormant.read_processes(lambda argv: good, own_pid=4242).starts == {4242: "Mon Sep 28 17:19:00 2026"}


@pytest.mark.parametrize("body", ["{}", json.dumps({"sessionId": S2}), json.dumps({"pid": 31})])
def test_c23_57_an_uninterpretable_registry_row_is_unreadable(tmp_path, monkeypatch, body):
    """C-23.57, review r1: a registry file that is valid JSON but names no session
    or no pid still names a process (its file name), whose session is unknown."""
    home = fx.claude_home(tmp_path, monkeypatch)
    (home / "sessions" / "31.json").write_text(body)
    reading = registry.read()
    assert reading.unreadable == (31,) and reading.rows == ()
    live = dormant.Processes(starts={31: "Mon Sep 28 17:19:00 2026"}, named=frozenset())
    assert dormant.liveness(S1, reading=reading, processes=live) == "unknown"


def test_c23_60_a_branch_that_cannot_be_read_holds(monkeypatch, tmp_path):
    """C-23.60, review r1: a git failure is not "no branch"; the wake is held
    rather than bind a session whose turn the daemon may refuse on main."""
    from subfleet import salvage

    def slow(workdir, *, timeout_s=None):
        raise TimeoutError("git timed out")

    monkeypatch.setattr(salvage, "git_branch", slow)
    assert "could not be read" in dormant.branch_refusal(str(tmp_path), "bypass")


# --- the automatic pass (C-23.60) ---------------------------------------------------

def test_c23_60_the_automatic_pass_records_what_it_did(tmp_path):
    """C-23.60: the daemon's pass runs the same command a person runs, as a child,
    and writes a summary `doctor` reads."""
    output = {"sessions": [{"session_id": S1, "title": "t", "woken": True, "eligible": True,
                            "conversation_id": "cv-1", "verdict": {"state": "interrupted"}}],
              "pace": {"allowed": 1}, "errors": []}
    command = [sys.executable, "-c", f"import json; print(json.dumps({output!r}))"]
    summary = dormant.automatic_pass(tmp_path, cancel=threading.Event(), command=command)
    assert summary["outcome"] == "finished" and summary["exit"] == 0
    assert summary["woken"] == [{"session_id": S1, "title": "t", "conversation_id": "cv-1"}]
    assert dormant.last_pass(tmp_path)["woken"] == summary["woken"]


def test_c23_60_a_grandchild_holding_the_pipe_cannot_hold_a_stop(tmp_path):
    """C-23.60 (C-5.8a), review r1: the stop ends the pass's whole process group,
    and no wait is unbounded, even when a grandchild holds the child's stdout."""
    cancel = threading.Event()
    script = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "time.sleep(60)")
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    summary = dormant.automatic_pass(tmp_path, cancel=cancel, command=[sys.executable, "-c", script])
    assert summary["outcome"] == "stopped" and time.monotonic() - started < 10


def test_c23_60_a_daemon_stop_ends_the_automatic_pass_in_seconds(tmp_path):
    """C-23.60 (C-5.8a): a stop is never held by a wake pass; its child is ended."""
    cancel = threading.Event()
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    summary = dormant.automatic_pass(tmp_path, cancel=cancel, command=command)
    assert summary["outcome"] == "stopped" and time.monotonic() - started < 10


def test_c23_60_the_timer_is_off_until_policy_turns_it_on(tmp_path):
    """C-23.60: `sessions.wake_interval_s` is 0 by default, so the daemon wakes
    nothing by itself; set, the pass has a timer and a worker of its own."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    with Store(tmp_path / "state.sqlite3") as store:
        off = Timers(store, tmp_path, fx.policy())
        on = Timers(store, tmp_path, fx.policy(wake_interval_s=900))
        try:
            assert "wake" not in off.intervals and on.intervals["wake"] == 900
            assert "wake" in on.status()
        finally:
            off.stop()
            on.stop()


# --- the facts, online and offline -------------------------------------------------

def test_c23_58_doctor_reads_the_facts_the_daemon_answers(tmp_path):
    """C-23.58 (C-17.5), differential: `facts.offline_state` over the stores on
    disk equals `facts.state` over the daemon's own store for the same rows."""
    from subfleet.store import Store
    root = tmp_path / "state"
    root.mkdir()
    with Store(root / "state.sqlite3") as store:
        store.add_event(facts.NUDGE_EVENT, data={"session_id": S1, "kind": "wake", "dedupe_key": "wake:a"})
        store.add_event(facts.RETIRE_EVENT, data={"session_id": S2, "reason": "done"})
        store.add_event(facts.RETIRE_EVENT, data={"session_id": S3})
        store.add_event(facts.UNRETIRE_EVENT, data={"session_id": S3})
        online = facts.state(store.query, None, bound=[S1.upper()])
    import sqlite3
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute("CREATE TABLE conversations (native_session_id TEXT)")
    db.execute("INSERT INTO conversations VALUES (?)", (S1.upper(),))
    db.commit()
    db.close()
    offline = facts.offline_state(root)
    assert offline == online
    assert offline["sessions"][S2]["retired"] and not offline["sessions"][S3]["retired"]
    assert S1 in offline["conversation_sessions"]


def test_c23_58_an_unreadable_conversation_store_is_not_an_empty_one(tmp_path):
    """C-23.58 (C-26.13): a conversation store that cannot be read raises; it is
    never read as "no conversations", which would hand their sessions back."""
    from subfleet.store import Store
    root = tmp_path / "state"
    root.mkdir()
    with Store(root / "state.sqlite3"):
        pass
    os.mkfifo(root / "conversations.sqlite3")
    with pytest.raises(OSError):
        facts.offline_state(root)


# --- doctor ------------------------------------------------------------------------

def test_c23_56_doctor_warns_about_dormant_sessions(world, monkeypatch):
    """C-23.56: `doctor` names the dormant sessions as a warning (it never decides
    the exit status) with the command that wakes them."""
    from subfleet import doctor
    dormant_session(world)
    monkeypatch.setattr(facts, "offline_state", lambda root: fx.FakeSessions().state(None))
    monkeypatch.setattr(dormant, "read_processes", lambda read=None: DEAD)
    monkeypatch.setattr(registry, "read", lambda directory=None: EMPTY)
    result = doctor.check_dormant_sessions(world["root"], now=NOW)
    assert set(result) == {"check", "status", "detail", "fix"}
    assert result["status"] == doctor.WARN and "1 desktop session was killed" in result["detail"]
    assert "sessions wake --all" in result["fix"]


def test_c23_56_doctor_passes_with_no_desktop_store(tmp_path, monkeypatch):
    """C-23.56: a machine with no desktop app has nothing to wake."""
    from subfleet import doctor
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(tmp_path / "none"))
    assert doctor.check_dormant_sessions(tmp_path)["status"] == doctor.PASS


def test_c23_56_doctor_is_unknown_when_ps_fails(world, monkeypatch):
    """C-23.57 (C-4.2): with no process table nothing is judged dead, and doctor
    says it could not tell rather than that all is well."""
    from subfleet import doctor
    dormant_session(world)
    monkeypatch.setattr(facts, "offline_state", lambda root: fx.FakeSessions().state(None))
    monkeypatch.setattr(dormant, "read_processes", lambda read=None: None)
    monkeypatch.setattr(registry, "read", lambda directory=None: EMPTY)
    assert doctor.check_dormant_sessions(world["root"], now=NOW)["status"] == doctor.UNKNOWN


# --- the markers (C-23.58) -----------------------------------------------------------

def test_c23_58_the_revive_prompt_opens_with_its_marker():
    """C-23.58: `turn_state` recognises a revive's prompt by `REVIVE_MARKER`."""
    assert revive.REVIVE_MESSAGE.startswith(transcripts.REVIVE_MARKER)


def test_c23_58_a_cold_sweep_does_not_revive_an_unanswered_revive_again(tmp_path, monkeypatch):
    """C-23.58: an unanswered revive prompt reads as subfleet's own message, so the
    cold sweep no longer lists it as a fresh interruption at every pass."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.transcript(home, S1, fx.interrupted(age_s=3600)
                  + [fx.user_text(revive.REVIVE_MESSAGE, uuid="rv", at=fx.ago(1800))])
    assert transcripts.cold_sessions(live_ids=set(), lane_ids=set(), max_age_s=10 ** 9,
                                     now=datetime.now(timezone.utc)) == []


# --- the verb (C-23.60) --------------------------------------------------------------

def test_c23_60_wake_with_no_target_surveys(world, daemon, capsys):
    """C-23.60: like `tickle`, `sessions wake` with no session and no --all only
    looks: nothing is reserved, opened or sent."""
    from subfleet.sessions import cli
    dormant_session(world)
    server = daemon({"sessions": lambda request: fx.FakeSessions().state(None),
                     "capabilities": lambda request: {"capabilities": ["conversations.v1"]},
                     "daemon.status": lambda request: {"readings": [], "lanes": []}})
    code = cli.main(["wake", "--json"])
    assert code == 0
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["dry_run"] is True and answer["wake"] == "conversation"
    assert "conversation.open" not in server.ops() and "message.submit" not in server.ops()
    assert not any(request.args.get("action") == "nudged" for request in server.requests
                   if request.op == "sessions")


def test_c23_60_plan_needs_the_window_it_paces_against(world, daemon, capsys):
    """C-23.60: `--plan` paces against the desktop login's window, which subfleet
    cannot read, so it needs --window-used and --window-resets."""
    from subfleet.sessions import cli
    daemon({"sessions": lambda request: fx.FakeSessions().state(None)})
    assert cli.main(["wake", "--plan", "--all"]) == 2
    assert "--window-used" in capsys.readouterr().err
    assert cli.main(["wake", "--plan", "--all", "--window-used", "5"]) == 2
    capsys.readouterr()
    assert cli.main(["wake", "--all", "--window-used", "5", "--window-resets",
                     "2026-09-05T13:00:00Z"]) == 2
    assert "pace a --plan" in capsys.readouterr().err


# --- policy -------------------------------------------------------------------------

@pytest.mark.parametrize("key, value", [("wake_batch", -1), ("wake_batch", 2.5), ("wake_model", ""),
                                        ("wake_model", 5), ("wake_quiet_s", float("nan")),
                                        ("wake_quiet_s", 0), ("wake_cost_pct", 0),
                                        ("wake_ceiling_pct", 101), ("wake_headroom_pct", 150)])
def test_c23_59_wake_policy_is_validated(tmp_path, key, value):
    """C-23.59 (C-6.4): the wake caps are policy data, validated like the rest."""
    from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data["sessions"][key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError):
        load_policy(path)
