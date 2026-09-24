"""Which registry row speaks, and what is not a session: C-23.30, C-23.31.

Every test names the clause it proves (C-20.5). The registry is keyed by pid,
so a restarted session leaves its old row behind and two rows can name one
session; `os.getpid()` is the only pid these tests can be sure is alive, and an
unallocated high pid is the only one they can be sure is not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from subfleet.sessions import registry
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
DEAD_PID = 4_000_001            # above the default pid_max; never allocated


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    return fx.claude_home(tmp_path, monkeypatch)


@pytest.fixture
def inbox(tmp_path):
    """A real unix socket, because `Path.is_socket()` is what the reader asks.

    AF_UNIX caps `sun_path` near 104 bytes on macOS and pytest's `tmp_path` is
    longer, so socket-bearing paths live directly under /tmp (the same reason
    `tests/unit/conftest.py` puts the state root there).
    """
    import socket as socket_module
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sf-reg-", dir="/tmp") as directory:
        path = Path(directory) / "inbox.sock"
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(str(path))
        try:
            yield str(path)
        finally:
            server.close()


# --- which row speaks (C-23.30) -----------------------------------------------

def test_a_live_pid_outranks_a_dead_row_for_the_same_session(home):
    """C-23.30: the row whose recorded pid is live speaks for the session."""
    fx.register(home, SESSION, DEAD_PID, started_at=9000.0, name="the stale one")
    fx.register(home, SESSION, os.getpid(), started_at=1.0, name="the live one")
    found = registry.find(SESSION)
    assert found is not None
    assert found.pid == os.getpid()
    assert found.row.name == "the live one"


def test_a_present_socket_breaks_a_tie_between_two_dead_rows(home, inbox):
    """C-23.30: then the row whose socket is present."""
    fx.register(home, SESSION, DEAD_PID, started_at=9000.0, name="no socket")
    fx.register(home, SESSION, DEAD_PID + 1, started_at=1.0, name="has a socket",
                socket_path=inbox)
    assert registry.find(SESSION).row.name == "has a socket"


def test_the_newest_start_breaks_a_tie_when_neither_is_live(home):
    """C-23.30: then the newest by start time."""
    fx.register(home, SESSION, DEAD_PID, started_at=1.0, name="older")
    fx.register(home, SESSION, DEAD_PID + 1, started_at=9000.0, name="newer")
    assert registry.find(SESSION).row.name == "newer"


def test_the_loser_is_reported_and_never_removed(home):
    """C-23.30: this module kills nothing it did not launch (D-surface §4)."""
    stale = fx.register(home, SESSION, DEAD_PID, started_at=9000.0)
    fx.register(home, SESSION, os.getpid(), started_at=1.0)
    found = registry.find(SESSION)
    assert [row.pid for row in found.others] == [DEAD_PID]
    assert stale.exists(), "a losing row is evidence, not garbage"


def test_a_row_with_no_session_id_is_ignored(home):
    """C-23.30: a half-written registry file is not a session."""
    (home / "sessions" / "9999.json").write_text('{"pid": 9999}', encoding="utf-8")
    (home / "sessions" / "bad.json").write_text("{not json", encoding="utf-8")
    assert registry.rows() == []


# --- two live instances of one session (the 2026-09-04 amend war) -------------

def test_two_live_instances_of_one_session_are_both_reported(home):
    """C-23.30 and C-6.5: a duplicate live instance is detected and named.

    The plan does not claim a notice stops the second instance's writes, which
    is why the daemon also refuses a second writable job for the session id;
    this is the operator-visible half.
    """
    fx.register(home, SESSION, os.getpid(), started_at=1.0, name="first")
    fx.register(home, SESSION, os.getppid(), started_at=2.0, name="second")
    listing = registry.sessions()
    assert len(listing) == 1, "one session id is one row in a listing"
    item = listing[0]
    assert item.duplicate is True
    assert set(item.live_pids) == {os.getpid(), os.getppid()}
    report = registry.duplicate_report(listing)
    assert len(report) == 1
    assert str(os.getpid()) in report[0] and str(os.getppid()) in report[0]


def test_one_live_row_is_not_a_duplicate(home):
    """C-23.30: a stale row beside a live one is history, not a twin."""
    fx.register(home, SESSION, DEAD_PID, started_at=9000.0)
    fx.register(home, SESSION, os.getpid(), started_at=1.0)
    listing = registry.sessions()
    assert listing[0].duplicate is False
    assert registry.duplicate_report(listing) == []


# --- a headless lane run is not a session (C-23.31) ---------------------------

def test_a_recorded_lane_session_is_hidden_from_the_listing(home):
    """C-23.31: never in a session listing unless lanes are explicitly included."""
    fx.register(home, SESSION, os.getpid(), started_at=2.0)
    fx.register(home, "lane-one", os.getppid(), started_at=1.0)
    fx.transcript(home, SESSION, fx.interrupted())
    fx.transcript(home, "lane-one", fx.headless())
    assert [item.session_id for item in registry.sessions(lane_ids={"lane-one"})] \
        == [SESSION]
    both = registry.sessions(lane_ids={"lane-one"}, include_lanes=True)
    assert {item.session_id: item.lane for item in both} == {
        SESSION: False, "lane-one": True}


def test_the_transcript_shape_catches_a_lane_the_ledger_forgot(home):
    """C-23.31: the recorded marker wins, the shape is the fallback.

    A `claude -p` run subfleet did not launch, or one whose attempt row
    retention has already reaped, has no marker and must still be excluded.
    """
    fx.register(home, "lane-two", os.getpid(), started_at=1.0)
    fx.transcript(home, "lane-two", fx.headless())
    assert registry.is_lane_run("lane-two", lane_ids=set()) is True
    assert registry.sessions(lane_ids=set()) == []


def test_an_interactive_session_is_never_mistaken_for_a_lane(home):
    """C-23.31: a typed prompt is a person, whatever the ledger says."""
    fx.register(home, SESSION, os.getpid(), started_at=1.0)
    fx.transcript(home, SESSION, fx.interrupted())
    assert registry.is_lane_run(SESSION, lane_ids=set()) is False


# --- the listing --------------------------------------------------------------

def test_only_live_rows_are_listed_by_default(home):
    """A nudge or a ping needs a process; a dead row has no inbox to reach."""
    fx.register(home, "dead-one", DEAD_PID, started_at=1.0)
    fx.register(home, SESSION, os.getpid(), started_at=2.0)
    assert [item.session_id for item in registry.sessions()] == [SESSION]
    everything = {item.session_id for item in registry.sessions(live_only=False)}
    assert everything == {SESSION, "dead-one"}


def test_a_missing_sessions_directory_is_an_empty_listing(home):
    """A fresh machine has no registry; that is not an error."""
    import shutil
    shutil.rmtree(home / "sessions")
    assert registry.rows() == []
    assert registry.sessions() == []
    assert registry.find(SESSION) is None


# --- a conversation's session (C-26.13) ---------------------------------------

def test_a_conversations_transcript_stops_looking_like_a_lane_after_two_turns(home):
    """C-26.13's reason: the transcript shape cannot keep a conversation's
    session out of the kit. Each turn adds one `sdk` prompt, so from the third
    turn C-23.31's shape test calls it an interactive session."""
    fx.transcript(home, "two-turns", fx.conversation_turns(turns=2))
    fx.transcript(home, SESSION, fx.conversation_turns(turns=3))
    assert registry.is_lane_run("two-turns", lane_ids=set()) is True
    assert registry.is_lane_run(SESSION, lane_ids=set()) is False


@pytest.mark.parametrize("include_lanes", [False, True])
@pytest.mark.parametrize("live_only", [True, False])
def test_a_conversations_session_is_never_listed(home, include_lanes, live_only):
    """C-26.13: a session the daemon reports as a conversation's is not in the
    kit's listing, whatever the caller includes, while others still are."""
    fx.register(home, SESSION, os.getpid(), started_at=1.0)
    fx.transcript(home, SESSION, fx.conversation_turns(turns=3))
    fx.register(home, "someone-else", os.getpid(), started_at=2.0)
    fx.transcript(home, "someone-else", fx.interrupted())
    listing = registry.sessions(conversation_ids={SESSION}, include_lanes=include_lanes,
                                live_only=live_only)
    assert [item.session_id for item in listing] == ["someone-else"]


def test_the_daemons_conversation_list_is_read_defensively():
    """C-26.13: an older daemon reports no list, and junk is not an id."""
    assert registry.conversation_ids_of({"lane_sessions": ["x"]}) == set()
    assert registry.conversation_ids_of(
        {"conversation_sessions": ["a", "", None, 3, "b"]}) == {"a", "b"}
