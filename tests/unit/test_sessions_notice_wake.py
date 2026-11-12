"""C-15.9: completion notices wake only quiet callers, within shared bounds."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from subfleet import descriptors, notify_push, protocol
from subfleet.caller_wakes import CallerWakeEngine
from subfleet.conversations.wakes import COOLDOWN_S, MAX_STREAK
from subfleet.daemon import Daemon
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, SESSION_DEFAULTS, load_policy
from subfleet.sessions import nudge, transcripts
from subfleet.sessions.transcripts import TurnState


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def nudges_on(monkeypatch):
    monkeypatch.setenv(nudge.TICKLE_ENV, "on")


def state(age=300, kind="completed", **changes):
    return TurnState(state=kind, timestamp=(NOW - timedelta(seconds=age)).isoformat(),
                     **changes)


def decide(turn=None, *, limits=None, **kwargs):
    return nudge.notice_decide(state() if turn is None else turn,
                               limits=nudge.caps({}) if limits is None else limits,
                               now=NOW, **kwargs)


def test_notice_wake_defaults_and_caps():
    """C-15.9: the default gate is five minutes and each pass has eight slots."""
    loaded = load_policy(DEFAULT_POLICY_PATH)
    assert {key: loaded["sessions"][key] for key in (
        "notice_wake_enabled", "notice_wake_quiet_s", "nudge_max_sessions")} == {
            "notice_wake_enabled": True, "notice_wake_quiet_s": 300,
            "nudge_max_sessions": 8}
    assert SESSION_DEFAULTS["notice_wake_enabled"] is True
    assert nudge.caps({})["notice_quiet_s"] == 300
    assert nudge.caps({})["max_sessions"] == MAX_STREAK
    custom = {"sessions": {"notice_wake_quiet_s": 600, "nudge_max_sessions": 2}}
    assert nudge.caps(custom)["notice_quiet_s"] == 600
    assert nudge.caps(custom)["max_sessions"] == 2
    assert not decide(state(599), limits=nudge.caps(custom))[0]
    assert decide(state(600), limits=nudge.caps(custom))[0]


@pytest.mark.parametrize("value", [1.5, 1.0, "8", True, -1])
def test_notice_wake_session_cap_is_a_nonnegative_integer(tmp_path, value):
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data.setdefault("sessions", {})["nudge_max_sessions"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError) as refused:
        load_policy(path)
    assert refused.value.key == "sessions.nudge_max_sessions"


@pytest.mark.parametrize("kind", ["interrupted", "completed"])
def test_notice_wake_accepts_quiet_interrupted_and_completed_turns(kind):
    """C-15.9: a caller may have completed its waiting turn or been cut off."""
    assert decide(state(300, kind))[0]
    assert decide(state(25 * 3600, kind))[0], "a notice has no resume-age maximum"


@pytest.mark.parametrize("kind", ["stopped", "unknown", "empty", "active"])
def test_notice_wake_requires_an_eligible_transcript_state(kind):
    assert not decide(state(300, kind))[0]


@pytest.mark.parametrize("timestamp", [None, "", "not-a-time"])
def test_notice_wake_refuses_unknown_activity_time(timestamp):
    assert not decide(TurnState(state="completed", timestamp=timestamp, age_s=1000))[0]


@pytest.mark.parametrize("age", [0, 299, 299.49, 299.999999])
def test_notice_wake_uses_precise_timestamp_not_rounded_age(age):
    """C-15.9 Quiet only: rounding must never open the quiet gate early."""
    assert not decide(state(age, age_s=300))[0]
    assert decide(state(300, age_s=299))[0]


def test_notice_wake_preserves_fractional_transcript_activity(tmp_path):
    """C-15.9 Quiet only: reading a real transcript must not truncate activity."""
    path = tmp_path / "caller.jsonl"
    stamp = (NOW - timedelta(seconds=299.51)).isoformat()
    path.write_text(json.dumps({
        "type": "assistant", "uuid": "finished-turn", "timestamp": stamp,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Waiting."}],
                    "stop_reason": "end_turn"},
    }) + "\n")
    turn = transcripts.turn_state(path, now=NOW)
    assert turn.state == "completed"
    assert turn.age_s == 300, "the display age rounds but eligibility must not"
    assert not decide(turn)[0]


def test_notice_wake_uses_the_latest_transcript_or_recorded_activity():
    """C-15.9 Quiet only: fresh caller activity fences an old transcript."""
    assert not decide(state(3600), active_at=NOW.timestamp() - 299.999)[0]
    assert not decide(state(299), active_at=NOW.timestamp() - 3600)[0]
    assert decide(state(3600), active_at=NOW.timestamp() - 300)[0]


def test_notice_wake_off_switch_source_and_retirement_are_respected(monkeypatch):
    assert not decide(retired={"reason": "replaced"})[0]
    for source in ("compact", "clear", "other"):
        assert not decide(source=source)[0]
    for source in (None, "startup", "resume"):
        assert decide(source=source)[0]
    for off in ("off", "0", "false", "no"):
        monkeypatch.setenv(nudge.TICKLE_ENV, off)
        assert not decide()[0]


@pytest.mark.parametrize("elapsed,expected", [(89.999, False), (90, True)])
def test_notice_wake_observes_resume_nudge_and_completion_cooldowns(elapsed, expected):
    stamp = (NOW - timedelta(seconds=elapsed)).isoformat()
    assert decide(state(3600), last_nudge={"at": stamp})[0] is expected
    assert decide(state(3600), last_wake_at=NOW.timestamp() - elapsed)[0] is expected


def test_notice_wake_borrows_conversation_streak_cap_and_cooldown():
    """C-15.9: unattended wakes share the existing C-24.10 bounds."""
    assert not decide(state(3600), wake_streak=MAX_STREAK,
                      last_wake_at=NOW.timestamp() - COOLDOWN_S + 0.001)[0]
    assert decide(state(3600), wake_streak=MAX_STREAK,
                  last_wake_at=NOW.timestamp() - COOLDOWN_S)[0]
    assert decide(state(3600), wake_streak=MAX_STREAK - 1,
                  last_wake_at=NOW.timestamp() - 90)[0]


def registry_entry(tmp_path, monkeypatch, **activity):
    """The real registry reader, with only fake paths and process evidence."""
    directory = tmp_path / "fake-session-registry"
    directory.mkdir()
    (directory / "42.json").write_text(json.dumps({
        "sessionId": "caller", "pid": 42, "messagingSocketPath": None,
        "startedAt": 1000, **activity,
    }))
    monkeypatch.setattr(notify_push, "sessions_dir", lambda: directory)
    monkeypatch.setattr(notify_push, "_pid_alive", lambda pid: True)
    return notify_push.find_session("caller")


def engine_eligible(entry):
    engine = object.__new__(CallerWakeEngine)
    engine.store = SimpleNamespace(one=lambda *args: None)
    engine.daemon = SimpleNamespace(sessions=lambda *args: {"sessions": {}})
    return engine._eligibility("caller", entry, state(3600), nudge.caps({}), NOW.timestamp())[0]


@pytest.mark.parametrize("activity", [
    {"status": "busy"},
    {"status": "idle", "statusUpdatedAt": (NOW.timestamp() - 299.999) * 1000},
    {"status": "idle", "updatedAt": (NOW.timestamp() - 1) * 1000},
])
def test_notice_wake_real_registry_fresh_or_busy_activity_blocks_old_transcript(tmp_path, monkeypatch, activity):
    """C-15.9 Quiet only: registry activity survives a long tool's quiet transcript."""
    entry = registry_entry(tmp_path, monkeypatch, **activity)
    assert not engine_eligible(entry)


def test_notice_wake_real_registry_old_idle_activity_allows_quiet_transcript(tmp_path, monkeypatch):
    entry = registry_entry(tmp_path, monkeypatch, status="idle",
                           statusUpdatedAt=(NOW.timestamp() - 300) * 1000,
                           updatedAt=(NOW.timestamp() - 3600) * 1000)
    assert engine_eligible(entry)


@pytest.mark.parametrize("field", ["statusUpdatedAt", "updatedAt"])
@pytest.mark.parametrize("bad", [
    True, "123", float("nan"), float("inf"), float("-inf"),
    pytest.param(10**1000, id="huge-integer"), pytest.param(-1, id="negative"),
])
def test_notice_wake_real_registry_malformed_activity_fails_closed(tmp_path, monkeypatch, field, bad):
    entry = registry_entry(tmp_path, monkeypatch, status="idle", **{field: bad})
    assert entry["activity_invalid"] is True
    internal_field = "status_updated_at" if field == "statusUpdatedAt" else "updated_at"
    assert entry[internal_field] is None
    assert not engine_eligible(entry)


@pytest.mark.parametrize("older_activity", [
    {"status": "busy"},
    {"status": "idle", "statusUpdatedAt": (NOW.timestamp() - 1) * 1000},
    {"status": "idle", "updatedAt": (NOW.timestamp() - 1) * 1000},
])
def test_notice_wake_real_registry_older_live_sibling_activity_blocks_delivery(
        tmp_path, monkeypatch, older_activity):
    """C-15.9: choosing one inbox must retain every live instance's activity."""
    registry_entry(tmp_path, monkeypatch, **older_activity)
    directory = tmp_path / "fake-session-registry"
    (directory / "43.json").write_text(json.dumps({
        "sessionId": "caller", "pid": 43, "messagingSocketPath": None,
        "startedAt": 2000, "status": "idle",
        "statusUpdatedAt": (NOW.timestamp() - 3600) * 1000,
    }))
    entry = notify_push.find_session("caller")
    assert entry["pid"] == 43, "the newer inbox still wins the address ranking"
    assert not engine_eligible(entry), "the older live instance is active"


def test_notice_hook_claim_is_not_read_only_but_plain_pending_is():
    """C-15.9: a disconnected hook claim must retain its durable reservation."""
    assert descriptors.read_only("notice.pending", {"session_id": "caller"})
    assert not descriptors.read_only("notice.pending", {
        "session_id": "caller", "surface": "UserPromptSubmit"})


@pytest.mark.parametrize("surface,pool", [(None, "lookups"), ("UserPromptSubmit", "requests")])
def test_notice_hook_claim_uses_the_request_pool(surface, pool):
    """A write cannot execute inside the lookup pool's read-only snapshot."""
    submitted = []

    def recorder(name):
        def submit(*_args, **_kwargs):
            submitted.append(name)
            return name
        return SimpleNamespace(submit=submit)

    daemon = SimpleNamespace(conversations=SimpleNamespace(owns=lambda op: False),
                             lookups=recorder("lookups"), requests=recorder("requests"),
                             _run_worker=None, _respond=None)
    args = {"session_id": "caller"}
    if surface:
        args["surface"] = surface
    result = Daemon._start_request(daemon, None, None,
                                   protocol.Request(op="notice.pending", args=args), 0, None)
    assert result == pool and submitted == [pool]
