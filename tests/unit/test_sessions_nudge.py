"""Tickle and muster: when a session may be nudged (C-23.33, C-23.34, C-23.31).

Every test names the clause it proves (C-20.5). `FakeSessions` stands in for the
daemon and enforces the same dedupe and cooldown the `sessions` op enforces, so
"at most once" is proved here without starting one; `tests/fake/
test_sessions_end_to_end.py` proves the store half.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from subfleet.sessions import nudge
from tests import sessions_fixtures as fx
from tests import spellings

ALICE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
BOB = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
LANE = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
DEAD_PID = 4_000_001


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    return fx.claude_home(tmp_path, monkeypatch)


@pytest.fixture
def policy():
    return fx.policy()


def clock():
    return fx.NOW


def sweep(sessions, policy, **kwargs):
    """A sweep with the waits collapsed; the delay is not what is under test."""
    kwargs.setdefault("delay_s", 0)
    return nudge.sweep(sessions, policy, now=clock, sleep=lambda _s: None, **kwargs)


def live(home, session_id: str, *, pid: int | None = None, entries=None,
         name: str = "a session", started_at: float = 1.0):
    fx.register(home, session_id, pid or os.getpid(), name=name,
                started_at=started_at)
    return fx.transcript(home, session_id, entries or fx.interrupted(age_s=1800))


# --- the age cap and the states (C-23.33) -------------------------------------

def test_a_session_interrupted_thirty_minutes_ago_is_nudged_once(home, policy):
    """C-23.33: an interrupted turn inside the age cap earns exactly one nudge."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert [item.delivered for item in report.outcomes] == [True]
    assert [session for session, _text in daemon.pings] == [ALICE]
    assert nudge.MARKER in daemon.pings[0][1]
    assert len(daemon.records) == 1


def test_nudge_skips_interruption_older_than_eight_hours(home, policy):
    """C-23.33: an abandoned turn is not resumed because a tab reopened.

    Ledger row 180; the cap is `sessions.nudge_max_age_h`, default 8 h.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=9 * 3600))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert daemon.pings == []
    assert "older than the" in report.outcomes[0].reason


def test_the_age_cap_is_policy_data_not_a_constant(home, policy):
    """C-6.4: the cap is a `policy.json` key, so raising it admits the same turn."""
    live(home, ALICE, entries=fx.interrupted(age_s=9 * 3600))
    daemon = fx.FakeSessions()
    report = sweep(daemon, fx.policy(nudge_max_age_h=12), scope="interrupted",
                   manual=False)
    assert report.outcomes[0].delivered is True


def test_a_completed_session_is_never_nudged(home, policy):
    """C-23.33: only an interrupted turn is a cut-off turn."""
    live(home, ALICE, entries=fx.completed(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert daemon.pings == []
    assert report.outcomes[0].reason.startswith("completed:")


def test_an_escaped_turn_is_never_nudged(home, policy):
    """C-23.33: the user stopped it; resuming would override a person."""
    live(home, ALICE, entries=fx.stopped(age_s=1800))
    daemon = fx.FakeSessions()
    assert sweep(daemon, policy, scope="interrupted", manual=False).outcomes[0].delivered is False
    assert daemon.pings == []


# --- one per interruption point, and the cooldown (C-23.33) -------------------

def test_nudge_once_per_interruption_point_with_session_cooldown(home, policy):
    """C-23.33: at most once per interruption point, and no more often than the
    cooldown allows.

    Ledger row 181. The second sweep is blocked by the dedupe key; a third,
    after the interruption point moves, is blocked by the cooldown instead —
    which is what absorbs a restart storm.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    assert sweep(daemon, policy, scope="interrupted", manual=False).outcomes[0].delivered
    again = sweep(daemon, policy, scope="interrupted", manual=False)
    assert again.outcomes[0].delivered is False
    assert "already nudged at this interruption point" in again.outcomes[0].reason
    assert len(daemon.pings) == 1

    # A restart moves the interruption point, but the cooldown still holds.
    fx.transcript(home, ALICE, fx.interrupted(age_s=1800) + fx.resume_stub(
        user_uuid="s2-u", assistant_uuid="s2-a"))
    third = sweep(daemon, policy, scope="interrupted", manual=False)
    assert third.outcomes[0].delivered is False
    assert "cooldown" in third.outcomes[0].reason
    assert len(daemon.pings) == 1


def test_a_restart_after_the_cooldown_earns_a_second_nudge(home, policy):
    """C-23.33: the double sign-in restarts twice minutes apart, and each hop
    deserves its nudge once the cooldown has passed."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions(nudges={ALICE: {"dedupe_key": "cut",
                                             "at": fx.ago(3600)}})
    fx.transcript(home, ALICE, fx.interrupted(age_s=1800) + fx.resume_stub(
        user_uuid="s2-u", assistant_uuid="s2-a"))
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert report.outcomes[0].delivered is True
    assert daemon.records[0]["dedupe_key"] == "s2-a"


def test_the_daemon_has_the_last_word_on_the_dedupe(home, policy):
    """C-23.33: two sweeps racing over one session cannot both send.

    The worker decides eligibility against the transcript (C-23.34), but the
    reservation belongs to the daemon and is taken inside the transaction that
    records it. Here the other sweep lands AFTER this one read `state` and
    before it recorded, so the local view says eligible and the reservation
    still refuses — and nothing is delivered.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()

    def another_sweep_gets_there_first(_seconds):
        daemon.nudges[ALICE] = {"dedupe_key": "cut", "at": fx.ago(0)}

    report = nudge.sweep(daemon, policy, scope="interrupted", manual=False,
                         now=clock, sleep=another_sweep_gets_there_first, delay_s=8)
    assert report.outcomes[0].eligible is False
    assert report.outcomes[0].recorded is False
    assert "already nudged at this interruption point" in report.outcomes[0].reason
    assert daemon.records, "the reservation was attempted"
    assert daemon.pings == [], "and refused, so nothing was delivered"


# --- the worker re-decides after the delay (C-23.34) --------------------------

def test_nudge_skipped_when_last_turn_changed_during_delay(home, policy):
    """C-23.34: a session whose last real turn changed during the wait is skipped.

    Ledger row 182. The CLI's own `--resume`, or a typed ".", may already have
    continued the turn, so the worker re-reads before nudging.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()

    def continue_the_turn(_seconds):
        fx.transcript(home, ALICE, fx.interrupted(age_s=1800) + [
            fx.assistant_text("carrying on", uuid="new", at=fx.ago(1))])

    report = nudge.sweep(daemon, policy, scope="interrupted", manual=False,
                         now=clock, sleep=continue_the_turn, delay_s=8)
    assert daemon.pings == []
    assert daemon.records == [], "an active session is never even reserved"
    assert "a new turn appeared during the wait" in report.outcomes[0].reason


def test_the_apps_resume_stub_during_the_delay_does_not_look_like_activity(home, policy):
    """C-23.34: the stub is not a turn, so it must not cancel the nudge.

    It lands about 0.7 s after the hook, which is inside every delay.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()

    def app_writes_its_stub(_seconds):
        fx.transcript(home, ALICE, fx.interrupted(age_s=1800) + fx.resume_stub())

    report = nudge.sweep(daemon, policy, scope="interrupted", manual=False,
                         now=clock, sleep=app_writes_its_stub, delay_s=8)
    assert report.outcomes[0].delivered is True


def test_manual_sweep_requires_extra_transcript_quiet(home, policy):
    """C-23.34: a sweep started by hand waits for a longer quiet window than a
    `SessionStart` wake does.

    Ledger row 183. Outside a restart an interrupted tail is often a long tool
    call, so a hand-started sweep would otherwise nudge a session mid-work.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=30))
    daemon = fx.FakeSessions()
    manual = sweep(daemon, policy, scope="interrupted", manual=True)
    assert daemon.pings == []
    assert "waits" in manual.outcomes[0].reason and "quiet" in manual.outcomes[0].reason

    woken = sweep(fx.FakeSessions(), policy, scope="interrupted", manual=False)
    assert woken.outcomes[0].delivered is True, "a restart wake has no quiet gate"


def test_nudge_only_on_startup_and_resume_session_sources(home, policy):
    """C-23.33: a nudge is sent only for a `SessionStart` whose source is
    `startup` or `resume`, never `compact` or `clear`.

    Ledger row 179: compaction is not a restart.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    for source in ("startup", "resume"):
        daemon = fx.FakeSessions()
        report = sweep(daemon, policy, scope="interrupted", manual=False,
                       source=source, only=[ALICE])
        assert report.outcomes[0].delivered is True, source
    for source in ("compact", "clear"):
        daemon = fx.FakeSessions()
        report = sweep(daemon, policy, scope="interrupted", manual=False,
                       source=source, only=[ALICE])
        assert daemon.pings == [], source
        assert f"source {source!r} is not a restart" == report.outcomes[0].reason


# --- what is never a target (C-23.31, C-23.35) --------------------------------

def test_a_headless_lane_run_is_never_nudged(home, policy):
    """C-23.31: a lane's deliverable is its last message; a notice would become it."""
    live(home, LANE, entries=fx.headless(age_s=1800))
    daemon = fx.FakeSessions(lane_sessions=[LANE])
    report = sweep(daemon, policy, scope="interrupted", manual=False, only=[LANE])
    assert daemon.pings == []
    assert "headless lane run" in report.outcomes[0].reason
    assert report.skipped_lanes == [LANE]


def test_a_retired_session_is_never_nudged(home, policy):
    """C-23.35: retirement is durable and both the listing and the sweep honour it."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions(retired={ALICE: {"reason": "replaced orchestrator"}})
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert daemon.pings == []
    assert "retired by the operator (replaced orchestrator)" == report.outcomes[0].reason


def test_the_off_switch_stops_every_automatic_nudge(home, policy, monkeypatch):
    """C-6.4: `SUBFLEET_TICKLE=off` is the operator's kill switch, kept from v1."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    monkeypatch.setenv("SUBFLEET_TICKLE", "off")
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert daemon.pings == []
    assert "disabled" in report.outcomes[0].reason


# --- the duplicate pair (C-23.30) ---------------------------------------------

def test_a_duplicate_live_instance_is_nudged_once_and_both_pids_are_named(home, policy):
    """C-23.30 and C-6.5: one session id is one nudge, and the report names both.

    The 2026-09-04 amend war: two live instances of one session, each writing
    the other's worktree. A notice does not stop the second instance, so the
    operator is told which pids are running.
    """
    fx.register(home, ALICE, os.getpid(), started_at=1.0, name="first")
    fx.register(home, ALICE, os.getppid(), started_at=2.0, name="second")
    fx.transcript(home, ALICE, fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert len(daemon.pings) == 1, "one session id, one nudge"
    assert report.outcomes[0].duplicate is True
    assert set(report.outcomes[0].live_pids) == {os.getpid(), os.getppid()}
    assert len(report.duplicates) == 1
    assert str(os.getpid()) in report.duplicates[0]
    assert str(os.getppid()) in report.duplicates[0]
    assert "duplicate" in nudge.render(report)


# --- muster (the idle scope) --------------------------------------------------

def test_muster_reaches_recently_idle_completed_sessions(home, policy):
    """C-23.33's roll call: a switch that killed nothing leaves finished turns.

    Muster asks them to check for standing work; tickle would leave them alone.
    """
    live(home, ALICE, entries=fx.completed(age_s=1800))
    daemon = fx.FakeSessions()
    assert sweep(daemon, policy, scope="interrupted", manual=False).outcomes[0].delivered is False
    roll_call = sweep(fx.FakeSessions(), policy, scope="idle", manual=False)
    assert roll_call.outcomes[0].delivered is True


def test_muster_sends_the_resume_message_to_an_interrupted_session(home, policy):
    """C-23.33: a roll call resumes the interrupted and calls the idle."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    sweep(daemon, policy, scope="idle", manual=False)
    assert nudge.MARKER in daemon.pings[0][1]
    assert nudge.MUSTER_MARKER not in daemon.pings[0][1]


def test_muster_ignores_a_session_outside_the_roll_call_window(home, policy):
    """C-23.33: a session idle since this morning was not orphaned by this switch."""
    live(home, ALICE, entries=fx.completed(age_s=3 * 3600))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="idle", manual=False)
    assert daemon.pings == []
    assert "roll-call window" in report.outcomes[0].reason


# --- the caller's own session, and the wait (v1 parity) -----------------------

def test_a_sweep_never_nudges_the_session_running_it(home, policy):
    """v1's rule, kept: a long tool call writes no turns, so the session running
    the sweep looks interrupted to itself and would nudge itself mid-work."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False, caller=ALICE)
    assert daemon.pings == []
    assert "a sweep never nudges itself" in report.outcomes[0].reason


def test_naming_your_own_session_still_nudges_it(home, policy):
    """The self-exclusion is a sweep rule, not a prohibition: a person who names
    their own session has made a decision."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False,
                   caller=ALICE, only=[ALICE])
    assert report.outcomes[0].delivered is True


def test_a_named_session_waits_for_neither_gate(home, policy):
    """C-17.1 keeps `tickle --session <id>`: no sample or 120 s quiet window.

    v1 applied both only to `--all`. Naming a session is a decision, and the
    person who made it can see the session; a fresh interruption is exactly the
    case they are naming it for.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=5))
    waited: list[float] = []
    daemon = fx.FakeSessions()
    report = nudge.sweep(daemon, policy, scope="interrupted", only=[ALICE],
                         manual=True, now=clock, sleep=waited.append)
    assert report.outcomes[0].delivered is True, "no quiet gate for a named session"
    assert waited == [], "and no sample either"


def test_a_sweep_samples_briefly_and_a_hook_wake_waits_for_the_inbox(home, policy):
    """C-23.34: a `SessionStart` wake waits 8 s because the
    inbox binds a moment after the hook runs; a sweep waits 3 s because it is
    only sampling, and eight seconds per session over a fleet is a minute wasted.
    """
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    waited: list[float] = []
    nudge.sweep(fx.FakeSessions(), policy, scope="interrupted", manual=True,
                now=clock, sleep=waited.append)
    nudge.wake(fx.FakeSessions(), policy, ALICE, source="startup",
               now=clock, sleep=waited.append)
    assert waited == [3.0, 8.0]


@pytest.mark.parametrize("scope,named", [("idle", [ALICE]),
                                         ("interrupted", [ALICE, BOB])])
def test_named_roll_calls_and_multi_session_sweeps_keep_the_quiet_window(
        home, policy, scope, named):
    """C-23.34: naming sweep targets does not waive the manual quiet window."""
    live(home, ALICE, entries=fx.interrupted(age_s=5))
    waited: list[float] = []
    daemon = fx.FakeSessions()
    report = nudge.sweep(daemon, policy, scope=scope, only=named,
                         now=clock, sleep=waited.append)
    alice = next(item for item in report.outcomes if item.session_id == ALICE)
    assert "quiet" in alice.reason
    assert daemon.pings == []
    assert waited == [3.0]


def test_an_explicit_delay_overrides_both(home, policy):
    """v1's `_tickle --delay S`, kept: the hook passes the policy value through."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    waited: list[float] = []
    nudge.sweep(fx.FakeSessions(), policy, scope="interrupted", manual=False,
                delay_s=0.5, now=clock, sleep=waited.append)
    assert waited == [0.5]


# --- the sweep's own shape ----------------------------------------------------

def test_a_named_session_that_is_not_live_is_reported_not_silently_dropped(home, policy):
    """A person who names a session gets an answer about that session."""
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", only=[BOB], manual=False)
    assert [item.session_id for item in report.outcomes] == [BOB]
    assert "not a live registered session" in report.outcomes[0].reason


def test_a_dry_run_decides_everything_and_sends_nothing(home, policy):
    """C-17.4: the survey is the whole point of `--dry-run`."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", manual=False, dry_run=True)
    assert report.outcomes[0].eligible is True
    assert daemon.pings == [] and daemon.records == []
    assert "dry run" in report.outcomes[0].reason


def test_force_overrides_the_cap_the_dedupe_and_the_cooldown(home, policy):
    """v1's `--force`, kept: only a session Max names is ever forced."""
    live(home, ALICE, entries=fx.interrupted(age_s=9 * 3600))
    daemon = fx.FakeSessions(nudges={ALICE: {"dedupe_key": "cut", "at": fx.ago(1)}})
    report = sweep(daemon, policy, scope="interrupted", only=[ALICE], force=True)
    assert report.outcomes[0].delivered is True
    assert daemon.records[0]["cooldown_s"] is None


def test_a_transcript_override_reaches_a_session_the_registry_cannot_resolve(home, policy):
    """v1's `tickle --session ID --transcript P`, kept as `sessions continue`."""
    fx.register(home, ALICE, os.getpid(), started_at=1.0)
    elsewhere = home / "elsewhere.jsonl"
    elsewhere.write_text("".join(
        __import__("json").dumps(entry) + "\n"
        for entry in fx.interrupted(age_s=1800)), encoding="utf-8")
    daemon = fx.FakeSessions()
    report = sweep(daemon, policy, scope="interrupted", only=[ALICE],
                   transcript=str(elsewhere), manual=False)
    assert report.outcomes[0].delivered is True
    assert report.outcomes[0].state.path == str(elsewhere)


def test_the_sweep_asks_the_daemon_for_state_once(home, policy):
    """A forty-session sweep is one `sessions state` call, not forty.

    The registry is keyed by pid, so two sessions need two live pids; this
    process and its parent are the two a test can be sure of.
    """
    live(home, ALICE, pid=os.getpid(), entries=fx.interrupted(age_s=1800))
    live(home, BOB, pid=os.getppid(), entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions()
    sweep(daemon, policy, scope="interrupted", manual=False)
    assert len(daemon.state_calls) == 1
    assert {session for session, _text in daemon.pings} == {ALICE, BOB}


# --- a conversation's session (C-26.13) ---------------------------------------

CONVERSATION = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def test_a_conversations_session_is_never_nudged_by_a_sweep(home, policy):
    """C-26.13: a live, interrupted session a conversation binds looks like any
    other after its third turn, and the sweep still leaves it alone: its next
    message comes from the Subfleet app."""
    live(home, CONVERSATION, entries=fx.conversation_turns(turns=3))
    live(home, ALICE, entries=fx.interrupted(age_s=1800), started_at=2.0)
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert [session for session, _text in daemon.pings] == [ALICE]
    assert CONVERSATION not in {item.session_id for item in report.outcomes}
    assert all(record["session_id"] != CONVERSATION for record in daemon.records)


@pytest.mark.parametrize("force", [False, True])
def test_naming_a_conversations_session_is_refused_with_the_reason(home, policy, force):
    """C-26.13: a person who names one gets the reason, and `--force` does not
    override it; neither a nudge record nor a notice is written."""
    live(home, CONVERSATION, entries=fx.conversation_turns(turns=3))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    report = sweep(daemon, policy, scope="interrupted", manual=True,
                   only=[CONVERSATION], force=force)
    assert daemon.pings == [] and daemon.records == []
    assert [item.session_id for item in report.outcomes] == [CONVERSATION]
    assert report.outcomes[0].reason.startswith("bound to a Subfleet conversation")
    assert "C-26.13" in report.outcomes[0].reason


def test_a_session_start_wake_for_a_conversations_session_sends_nothing(home, policy):
    """C-26.13 behind the hook: even a wake that reached the worker (a hook
    configured by other means, or an older hook) nudges nobody."""
    live(home, CONVERSATION, entries=fx.conversation_turns(turns=3, age_s=60))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    report = nudge.wake(daemon, policy, CONVERSATION, source="resume",
                        now=clock, sleep=lambda _s: None, delay_s=0)
    assert daemon.pings == []
    assert report.outcomes[0].reason.startswith("bound to a Subfleet conversation")


@pytest.mark.parametrize("direction", sorted(spellings.DIRECTIONS))
def test_a_conversation_bound_session_is_never_nudged_in_either_direction(home, policy, direction):
    """C-26.3, C-26.13 (review of 3c1a34e, finding 5): the daemon lists the
    session as its store recorded it and the registry row and transcript name
    it in another case, either way round. No sweep nudges it, and a person
    naming it in either spelling (forced) is refused with the reason; nothing
    is recorded or sent."""
    listed_as, registered_as = spellings.DIRECTIONS[direction]
    live(home, registered_as(ALICE), entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions(conversation_sessions=[listed_as(ALICE)])
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert report.outcomes == []
    for named in (registered_as(ALICE), listed_as(ALICE)):
        report = sweep(daemon, policy, scope="interrupted", manual=True, only=[named], force=True)
        assert [item.session_id for item in report.outcomes] == [named]
        assert report.outcomes[0].reason.startswith("bound to a Subfleet conversation")
    assert daemon.pings == [] and daemon.records == []


def test_a_conversation_bound_session_is_never_nudged_in_any_spelling(home, policy):
    """C-26.13 with review L1 and M3: a session the daemon lists in another case is
    still its conversation's. No sweep nudges it, a person naming it (forced) is
    refused with the reason, and nothing is recorded or sent."""
    live(home, ALICE, entries=fx.interrupted(age_s=1800))
    daemon = fx.FakeSessions(conversation_sessions=[ALICE.upper()])
    report = sweep(daemon, policy, scope="interrupted", manual=False)
    assert ALICE not in {item.session_id for item in report.outcomes}
    report = sweep(daemon, policy, scope="interrupted", manual=True, only=[ALICE], force=True)
    assert [item.session_id for item in report.outcomes] == [ALICE]
    assert report.outcomes[0].reason.startswith("bound to a Subfleet conversation")
    assert daemon.pings == [] and daemon.records == []
