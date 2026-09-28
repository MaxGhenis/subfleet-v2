"""C-10.3 (2026-09-27): the desktop login's lane is refused only while Claude Code
is using that login, read from Claude Code's own registry, `~/.claude/sessions`.

Max: excluding it "makes sense if we're using sf thru the cc app but not if thru
the sf app". The Claude app's Code sessions (`claude-desktop`) and a terminal
(`cli`) run on the desktop login. A headless run (`sdk-*`) is Subfleet's own,
on its lane's token, only when its process or session is a live attempt's; any
other one may use the desktop login and counts (review of the uncap plan).
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.sessions import registry

NOW_MS = 1_790_560_000_000.0
MINUTE = 60_000


def row(*, alive=True, entrypoint="claude-desktop", status="idle", status_at=None, updated_at=None,
        started_at=NOW_MS - 3_600_000.0, session="s", pid=4242, proc_start=None) -> registry.SessionRow:
    return registry.SessionRow(session_id=session, pid=pid, socket=None, name=None, cwd=None,
                               started_at=started_at, alive=alive, socket_present=False, registry_path="x",
                               proc_start=proc_start, entrypoint=entrypoint, status=status,
                               status_updated_at=status_at, updated_at=updated_at)


def in_use(*rows, recent_s=1800, **options):
    return registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=recent_s, **options)[0]


def test_a_busy_claude_code_session_uses_the_login():
    assert in_use(row(status="busy", status_at=NOW_MS - 90 * MINUTE))            # busy however long
    assert in_use(row(entrypoint="cli", status="busy", status_at=NOW_MS - MINUTE))


def test_an_idle_session_uses_it_for_the_recency_window_only():
    assert in_use(row(status_at=NOW_MS - 29 * MINUTE))
    assert not in_use(row(status_at=NOW_MS - 31 * MINUTE))
    assert not in_use(row(status_at=NOW_MS - 5 * MINUTE), recent_s=60)
    # With no status time, the row's last update stands in; its start never does.
    assert in_use(row(updated_at=NOW_MS - MINUTE))
    assert not in_use(row(updated_at=NOW_MS - 40 * MINUTE))


def test_a_subfleet_run_never_uses_it_and_any_other_headless_run_may():
    """The 2026-09-27 registry held 9 `sdk-cli` rows, all Subfleet lane runs. A
    headless run outside Subfleet may run on the desktop login, so an `sdk-*` row
    is left out only when a live attempt owns its process or its session."""
    lane_run = row(entrypoint="sdk-cli", status="busy", status_at=NOW_MS, pid=77, session="LANE-SESSION")
    assert not in_use(lane_run, owned_pids={77})
    assert not in_use(lane_run, owned_sessions={"lane-session"})                  # either case
    assert in_use(lane_run)                                                       # nobody's: counts
    assert in_use(row(entrypoint="sdk-ts", status="busy", status_at=NOW_MS))
    # Ownership is for headless runs only: the Claude app's own row always counts.
    assert in_use(row(status="busy", status_at=NOW_MS, pid=77), owned_pids={77})


def test_dead_rows_never_use_it():
    assert not in_use(row(alive=False, status="busy", status_at=NOW_MS))
    assert not in_use()


def test_what_the_signal_cannot_read_counts_as_use():
    """It only ever keeps a lane out, so a row it cannot judge is use (review: a
    legacy row with only an hour-old start read as idle while working)."""
    assert in_use(row(entrypoint=None, status_at=NOW_MS - MINUTE))                 # an older Claude Code
    assert in_use(row(status=None, status_at=None, updated_at=None, started_at=NOW_MS - 90 * MINUTE))
    assert in_use(row(status=None, status_at=NOW_MS - 90 * MINUTE))              # no status: unknown
    assert in_use(row(status="idle", status_at=None, updated_at=None))            # no time to judge by
    assert in_use(unreadable=1)                                                   # a live row file we can't read
    assert not in_use(unreadable=0)


def test_the_evidence_counts_the_rows_that_decided_it():
    rows = [row(status="busy", status_at=NOW_MS), row(status_at=NOW_MS - MINUTE),
            row(entrypoint="sdk-cli", status="busy", pid=9), row(status_at=NOW_MS - 50 * MINUTE)]
    answer, evidence = registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=1800, owned_pids={9})
    assert answer and (evidence["busy"], evidence["recent"], evidence["unknown"], evidence["subfleet"]) == (1, 1, 0, 1)
    assert evidence["newest_activity_ms"] == NOW_MS


ROWS = st.lists(st.builds(
    row, alive=st.booleans(), entrypoint=st.sampled_from(["claude-desktop", "cli", "sdk-cli", None]),
    status=st.sampled_from(["busy", "idle", None]),
    status_at=st.one_of(st.none(), st.floats(NOW_MS - 7_200_000, NOW_MS)),
    updated_at=st.one_of(st.none(), st.floats(NOW_MS - 7_200_000, NOW_MS)),
    started_at=st.one_of(st.none(), st.floats(NOW_MS - 7_200_000, NOW_MS)),
    pid=st.sampled_from([1, 2, 3]), session=st.sampled_from(["a", "b"])), max_size=6)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(ROWS, st.integers(0, 7200), st.integers(0, 7200), st.sets(st.sampled_from([1, 2, 3])),
       st.integers(0, 2))
def test_the_signal_is_monotone_and_fails_closed(rows, short, long, owned, unreadable):
    """Adding a row, widening the window, owning fewer processes or reading fewer
    row files never frees the login; a dead row or an owned headless run never
    decides it."""
    short, long = sorted((short, long))
    base = registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=short, owned_pids=owned)[0]
    assert registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=long, owned_pids=owned)[0] >= base
    assert registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=short)[0] >= base
    assert registry.desktop_login_in_use(rows, now_ms=NOW_MS, recent_s=short, owned_pids=owned,
                                         unreadable=unreadable)[0] >= base
    assert registry.desktop_login_in_use([*rows, row(status="busy", status_at=NOW_MS)], now_ms=NOW_MS,
                                         recent_s=short, owned_pids=owned)[0]
    deciding = [r for r in rows if r.alive and not ((r.entrypoint or "").startswith("sdk-") and r.pid in owned)]
    if not deciding:
        assert not base


def test_a_row_is_alive_only_while_its_pid_is_the_process_that_wrote_it():
    """`validated` (review: a stale busy row whose pid a later process reuses kept
    the desktop lane out forever, and made a gone caller look live)."""
    started = "Mon Sep 28 00:23:41 2026"
    rows = [row(pid=10, proc_start=started), row(pid=11, proc_start=started), row(pid=12), row(pid=13)]
    alive = {r.pid: r.alive for r in registry.validated(rows, {10: started, 11: "Tue Sep 29 09:00:00 2026", 12: "x"})}
    assert alive == {10: True, 11: False, 12: True, 13: False}


def test_listing_tells_absent_unreadable_and_unparsable_apart(tmp_path):
    assert registry.listing(tmp_path / "absent") == registry.Listing()
    directory = tmp_path / "sessions"
    directory.mkdir()
    (directory / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "abc", "entrypoint": "claude-desktop", "status": "busy",
        "statusUpdatedAt": NOW_MS, "updatedAt": NOW_MS - 5, "startedAt": NOW_MS - 10,
        "procStart": "Mon Sep 28 00:23:41 2026"}))
    (directory / "4321.json").write_text("{not json")                   # half-written, say
    (directory / "junk.json").write_text("not json")                     # names no pid
    (directory / "4321.abc.key").write_text("")
    found = registry.listing(directory)
    assert [(r.session_id, r.alive, r.entrypoint, r.status, r.status_updated_at, r.proc_start) for r in found.rows] == [
        ("abc", True, "claude-desktop", "busy", NOW_MS, "Mon Sep 28 00:23:41 2026")]
    assert found.unreadable == (4321,)
    directory.chmod(0)
    try:
        if os.access(directory, os.R_OK):
            pytest.skip("this user can read a mode-0 directory")
        assert registry.listing(directory) is None
    finally:
        directory.chmod(stat.S_IRWXU)
