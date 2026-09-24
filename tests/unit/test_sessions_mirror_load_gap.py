"""The sidebar mirror against the app's load-time reading of its folders: C-23.28.

The desktop app lists `claude-code-sessions/<account>/<org>/local_*.json` into
memory only when it loads that folder (launch, account or org switch, login),
so a copy that lands after the load is invisible until the next one. On
2026-09-24 nineteen sessions copied into the loaded folder at 17:13-17:14 ET
stayed out of the sidebar until the app relaunched at 17:24:47. These tests
prove the three answers: a hot pass that spreads a record before the next
switch, an incremental inventory that keeps passes to seconds, and a load-gap
report that names what only a relaunch can list.

Every test names the clause it proves (C-20.5). The store, the transcripts and
the app's log all live under `tmp_path`; nothing here reads the operator's own.
The log lines are the app's own format, copied from `main.log` with the ids
replaced.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from subfleet.sessions import desktop, mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
TWO = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
THREE = "0b5e7c11-2d3f-4a55-8e6d-7f8091a2b3c4"
ACCOUNT_A, ORG_A = "acct-aaaa", "org-aaaa"
ACCOUNT_B, ORG_B = "acct-bbbb", "org-bbbb"
UTC = timezone.utc

#: fx.NOW is 2026-09-05 11:30 UTC; every pass in this file runs on that clock.
BEFORE = "2026-09-05 11:00:00"          # a load that precedes the passes
AFTER = "2026-09-05 12:00:00"           # a load that follows them


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A state root, a `~/.claude`, a two-account desktop store, and an app log."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)):
        (store / account / org).mkdir(parents=True, exist_ok=True)
    log = tmp_path / "logs" / "main.log"
    log.parent.mkdir()
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    root = tmp_path / "state"
    root.mkdir()
    return home, store, root, log


def engine(world, **policy_overrides) -> mirror.Mirror:
    """A mirror on the fixture clock that reads the fixture log as UTC."""
    _home, store, root, log = world
    running = mirror.Mirror(root, fx.policy(**policy_overrides), now=lambda: fx.NOW)
    running._desktop = desktop.DesktopLog(log, store=store, tz=UTC)   # noqa: SLF001 - the seam
    return running


def openable(home, store, session_id, account, org, **kwargs) -> Path:
    fx.transcript(home, session_id, fx.completed())
    return fx.index_entry(store, account, org, session_id, **kwargs)


def copies(store: Path, session_id: str) -> dict[Path, dict]:
    found = {}
    for path in store.glob("*/*/local_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cliSessionId") == session_id:
            found[path] = data
    return found


def rewrite(path: Path, data: dict) -> None:
    """Replace an index file the way the app does: write beside it, then rename."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def say(log: Path, *lines: str) -> None:
    """Append lines to the app's log as the app writes them."""
    with log.open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")


def loads(store: Path, account: str, org: str, at: str, *, existing: int = 0,
          count: int = 3) -> list[str]:
    """The two lines the app writes when it lists a session folder."""
    return [
        f"{at} [info] [LocalSessionManager] Initialization succeeded — "
        f"accountId={account}, orgId={org}, existingSessions={existing}",
        f"{at} [info] Loaded {count} persisted sessions from {store / account / org} "
        f"(10 archived deferred, 10 of them unopened; 0 skipped)",
    ]


def logout(at: str) -> str:
    return (f"{at} [info] [LocalSessionManager] Account logged out, marking for "
            "re-init on next login")


def count_entry_reads(monkeypatch) -> list[Path]:
    read = mirror._read_entry
    seen: list[Path] = []

    def counted(path):
        if path.name.startswith("local_"):
            seen.append(path)
        return read(path)

    monkeypatch.setattr(mirror, "_read_entry", counted)
    return seen


# --- what the app's log says it loaded (desktop.py) ---------------------------

def test_the_loaded_folder_is_the_one_the_loaded_line_names(tmp_path):
    """C-23.28: mid-switch the app initializes pairs it never loads (the old
    account with the new org); only the "Loaded ... from <folder>" line, or a
    missing-folder line, says which folder the sidebar lists. This is the
    2026-09-24 16:38:05 sequence."""
    store = tmp_path / "claude-code-sessions"
    log = tmp_path / "main.log"
    say(log,
        "2026-09-24 16:37:53 [info] [account] Navigated to /logout, synthesizing logged-out",
        logout("2026-09-24 16:37:53"),
        "2026-09-24 16:38:05 [info] [LocalSessionManager] Org changed from org-old to org-new, "
        "reinitializing sessions",
        "2026-09-24 16:38:05 [info] [LocalSessionManager] Initialization succeeded — "
        "accountId=acct-old, orgId=org-new, existingSessions=1794",
        "2026-09-24 16:38:05 [info] [LocalSessionManager] Session storage directory does not "
        f"exist yet, skipping load: {store / 'acct-old' / 'org-new'}",
        *loads(store, "acct-new", "org-new", "2026-09-24 16:38:05"),
        # The agent-mode manager logs the same shapes about a different store.
        "2026-09-24 16:38:11 [info] [LocalAgentModeSessionManager] Initialization succeeded — "
        "accountId=acct-new, orgId=org-other, existingSessions=0",
        f"2026-09-24 16:38:11 [info] Loaded 72 persisted sessions from "
        f"{tmp_path / 'local-agent-mode-sessions' / 'acct-new' / 'org-other'}")
    state = desktop.DesktopLog(log, store=store, tz=UTC).poll()
    assert state.load is not None
    assert (state.load.account, state.load.org) == ("acct-new", "org-new")
    assert not state.load.missing and state.load.count == 3
    assert state.logged_out_at is None, "a load after the logout ends the logout"


def test_a_relaunch_is_a_fresh_load_and_a_same_folder_relogin_is_not(tmp_path):
    """C-23.28: a load that starts from a non-empty list of the same folder only
    adds ids it does not hold (read from the app bundle), so a record the app
    already holds is re-read only by a fresh load."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    say(log, *loads(store, "a", "o", "2026-09-24 15:52:17", existing=0),
        logout("2026-09-24 16:33:56"),
        *loads(store, "a", "o", "2026-09-24 16:35:47", existing=260))
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    load = reader.poll().load
    assert load.started_at == datetime(2026, 9, 24, 16, 35, 47, tzinfo=UTC)
    assert load.fresh_started_at == datetime(2026, 9, 24, 15, 52, 17, tzinfo=UTC)
    say(log, *loads(store, "a", "o", "2026-09-24 16:59:47", existing=0))
    assert reader.poll().load.fresh_started_at == datetime(2026, 9, 24, 16, 59, 47, tzinfo=UTC)


def test_a_logout_is_the_state_until_the_next_load(tmp_path):
    """C-23.28: at a logout the app keeps its list and re-initializes at login."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    say(log, *loads(store, "a", "o", "2026-09-24 15:52:17"), logout("2026-09-24 16:33:56"))
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    state = reader.poll()
    assert state.load.folder == "a/o"
    assert state.logged_out_at == datetime(2026, 9, 24, 16, 33, 56, tzinfo=UTC)
    say(log, *loads(store, "b", "p", "2026-09-24 16:35:46"))
    assert reader.poll().logged_out_at is None


def test_polls_read_only_what_was_appended_and_follow_a_rotation(tmp_path):
    """C-23.28: the hot pass polls every few seconds, so a poll reads the tail
    only, keeps a half-written line for the next poll, and finishes the old file
    when the app rotates `main.log` to `main1.log`."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    first, second = loads(store, "a", "o", "2026-09-24 10:00:00")
    say(log, first)
    reader = desktop.DesktopLog(log, store=store, tz=UTC)
    assert reader.poll().load is None
    with log.open("a", encoding="utf-8") as stream:
        stream.write(second[:40])                   # the app is mid-write
    assert reader.poll().load is None
    with log.open("a", encoding="utf-8") as stream:
        stream.write(second[40:] + "\n")
    assert reader.poll().load.folder == "a/o"

    # Rotation: the app renames the full log aside after appending to it.
    say(log, *loads(store, "b", "p", "2026-09-24 11:00:00"))
    log.rename(tmp_path / desktop.ROTATED_NAME)
    say(log, "2026-09-24 11:00:01 [info] unrelated")
    assert reader.poll().load.folder == "b/p", "the rotated file's tail is not lost"
    say(log, *loads(store, "c", "q", "2026-09-24 12:00:00"))
    assert reader.poll().load.folder == "c/q"


def test_a_cold_poll_finds_the_last_load_in_the_rotated_file(tmp_path):
    """C-23.28: right after a rotation the live log may hold no load at all."""
    store = tmp_path / "store"
    say(tmp_path / desktop.ROTATED_NAME, *loads(store, "a", "o", "2026-09-24 10:00:00"))
    say(tmp_path / "main.log", "2026-09-24 10:05:00 [info] unrelated")
    state = desktop.DesktopLog(tmp_path / "main.log", store=store, tz=UTC).poll()
    assert state.load.folder == "a/o"


def test_a_missing_log_is_reported_not_guessed(tmp_path):
    """C-23.28: without the log the mirror says it cannot tell."""
    state = desktop.DesktopLog(tmp_path / "absent.log", store=tmp_path, tz=UTC).poll()
    assert state.load is None and "cannot read" in state.error


def test_log_times_are_the_apps_local_wall_clock(tmp_path):
    """C-23.28: the app logs local time without a zone; 16:38:11 in New York on
    2026-09-24 (EDT) is 20:38:11 UTC."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    say(log, *loads(store, "a", "o", "2026-09-24 16:38:11"))
    load = desktop.DesktopLog(log, store=store, tz=ZoneInfo("America/New_York")).poll().load
    assert load.started_at == datetime(2026, 9, 24, 20, 38, 11, tzinfo=UTC)


# --- the load gap: what only a relaunch lists ---------------------------------

def test_copies_after_the_apps_load_are_reported_until_the_next_load(world):
    """C-23.28, the 2026-09-24 regression: the app loaded account B's folder,
    then the mirror copied sessions into it. The running app cannot list them,
    and the mirror says so, by name, until the app loads the folder again."""
    home, store, _root, log = world
    for session, title in ((ONE, "Operationalizing certified components"),
                           (TWO, "Fix PE-US state sales tax table"),
                           (THREE, "Eight Sleep Pod 6 upgrade")):
        openable(home, store, session, ACCOUNT_A, ORG_A, title=title)
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE, existing=1795))
    running = engine(world)
    assert running.run_once().added == 3
    gap = running.load_gap()
    assert gap["status"] == "relaunch" and gap["pending"] == 3
    assert "3 sessions copied into" in gap["detail"]
    assert {item["title"] for item in gap["sessions"]} == {
        "Operationalizing certified components", "Fix PE-US state sales tax table",
        "Eight Sleep Pod 6 upgrade"}
    assert running.sidecar()["load_gap"]["pending"] == 3, "the pass records what it measured"

    say(log, *loads(store, ACCOUNT_B, ORG_B, AFTER))             # the relaunch
    gap = running.load_gap()
    assert gap["status"] == "ok" and gap["pending"] == 0


def test_a_copy_that_precedes_the_load_is_listed(world):
    """C-23.28: the whole point of copying early: a load lists what is there."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    say(log, *loads(store, ACCOUNT_B, ORG_B, AFTER))
    assert running.load_gap()["status"] == "ok"


def test_a_copy_the_app_has_rewritten_is_one_it_holds(world):
    """C-23.28: the app writes only records it holds, so a rewrite after the
    copy (a new inode and ctime) means the sidebar lists it. On 2026-09-24 the
    11 records the app rewrote after its load were all listed."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    running.run_once()
    assert running.load_gap()["pending"] == 2
    copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(copy, {**json.loads(copy.read_text()), "lastFocusedAt": 5})
    gap = running.load_gap()
    assert gap["pending"] == 1 and gap["sessions"][0]["name"] == f"local_{TWO}.json"


def test_copies_into_folders_the_app_has_not_loaded_are_not_pending(world):
    """C-23.28: a folder the app has not loaded lists everything when it does."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_B, ORG_B)
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    assert running.run_once().added == 1                         # into A, not loaded
    assert running.load_gap()["status"] == "ok"


def test_a_repaired_record_waits_for_a_fresh_load(world):
    """C-23.28: replacing a stale empty record the app already holds needs a
    load that starts from an empty list; a same-folder re-login adds new ids
    and re-reads nothing it holds."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    stale = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    stale.write_text(json.dumps({"sessionId": f"local_{ONE}", "cliSessionId": ""}))
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    assert running.run_once().repaired == 1
    assert running.load_gap()["pending"] == 1
    say(log, logout(AFTER), *loads(store, ACCOUNT_B, ORG_B, AFTER, existing=40))
    assert running.load_gap()["pending"] == 1, "a re-login does not re-read held records"
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-05 12:30:00", existing=0))
    assert running.load_gap()["pending"] == 0


def test_an_archived_copy_is_counted_apart_from_the_sidebar(world):
    """C-23.28: an archived session is not in the sidebar list either way."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, archived=True)
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    running.run_once()
    gap = running.load_gap()
    assert (gap["pending"], gap["archived"]) == (1, 1)


def test_flag_writes_into_the_loaded_folder_are_stale_not_missing(world):
    """C-23.28: the app saves each record from memory, so a flag the mirror
    writes into the loaded folder does not reach the running app either."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.now = lambda: fx.NOW - timedelta(hours=1)           # 10:30: the copy
    running.run_once()
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))             # 11:00: B lists ONE
    running.now = lambda: fx.NOW                                # 11:30: the archive
    source = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(source, {**json.loads(source.read_text()), "isArchived": True})
    assert running.run_once().flag_synced == 1
    gap = running.load_gap()
    assert (gap["status"], gap["pending"], gap["stale"]) == ("ok", 0, 1)


def test_without_a_load_in_the_log_the_gap_is_unknown(world):
    """C-23.28: a log that names no load is `unknown`, never `ok`."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    gap = running.load_gap()
    assert gap["status"] == "unknown" and "no session-folder load" in gap["detail"]


def test_a_logout_is_reported_as_such(world):
    """C-23.28: logged out, the app lists a folder again at its next login."""
    _home, store, _root, log = world
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE), logout(AFTER))
    gap = engine(world).load_gap()
    assert gap["status"] == "logged-out" and gap["logged_out_at"] == "2026-09-05T12:00:00Z"


def test_the_journal_keeps_only_what_can_still_matter(world):
    """C-23.28: rows for folders the app has not loaded age out; rows for the
    loaded folder live until the load they follow is superseded."""
    home, store, root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    openable(home, store, TWO, ACCOUNT_B, ORG_B, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    running.run_once()
    rows = json.loads((root / "sessions" / mirror.JOURNAL_NAME).read_text())["writes"]
    assert {(row[0], row[1]) for row in rows} == {
        (f"{ACCOUNT_B}/{ORG_B}", f"local_{ONE}.json"),
        (f"{ACCOUNT_A}/{ORG_A}", f"local_{TWO}.json")}

    later = fx.NOW + timedelta(seconds=mirror.JOURNAL_WINDOW_S + 60)
    running.now = lambda: later
    running.run_once()
    rows = json.loads((root / "sessions" / mirror.JOURNAL_NAME).read_text())["writes"]
    assert [(row[0], row[1]) for row in rows] == [(f"{ACCOUNT_B}/{ORG_B}", f"local_{ONE}.json")]

    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-05 11:59:00"))
    running.run_once()
    rows = json.loads((root / "sessions" / mirror.JOURNAL_NAME).read_text())["writes"]
    assert rows == []


def test_a_folder_the_app_loaded_but_found_missing_is_created_and_seeded(world):
    """C-23.28: the app lists nothing for an account and org it never saved a
    session under, and a relaunch would list nothing either until something
    creates the folder. The mirror creates the folder the app's latest load
    named, owner-only like the app's own, and seeds it. The pairs the app tries
    mid-switch are never created."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    say(log,
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Initialization succeeded — "
        f"accountId={ACCOUNT_A}, orgId=org-new, existingSessions=12",
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Session storage directory does not "
        f"exist yet, skipping load: {store / ACCOUNT_A / 'org-new'}",
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Initialization succeeded — "
        "accountId=acct-new, orgId=org-new, existingSessions=0",
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Session storage directory does not "
        f"exist yet, skipping load: {store / 'acct-new' / 'org-new'}")
    running = engine(world)
    assert running.run_once().added == 2
    created = store / "acct-new" / "org-new"
    assert (created / f"local_{ONE}.json").is_file()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    assert not (store / ACCOUNT_A / "org-new").exists(), "a mid-switch pair is not created"
    gap = running.load_gap()
    assert gap["status"] == "relaunch" and gap["pending"] == 1


# --- the hot pass: spread before the next switch ------------------------------

def test_a_new_record_reaches_every_folder_in_one_hot_pass(world):
    """C-23.28: a session started a moment ago under account A is in account
    B's folder before the app could switch to B."""
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    result = running.run_hot()
    assert (result.kind, result.state, result.added) == ("hot", "ok", 1)
    assert len(copies(store, TWO)) == 2


def test_the_hot_pass_prevents_the_gap_a_late_copy_leaves(world):
    """C-23.28, the 2026-09-24 sequence with the fix: a session under A, a
    logout, the hot pass that the timer runs every 2 s, then the login that
    loads B. B's load lists the session, so nothing waits for a relaunch."""
    home, store, _root, log = world
    say(log, *loads(store, ACCOUNT_A, ORG_A, "2026-09-05 10:00:00"))
    running = engine(world)
    running.run_once()
    openable(home, store, ONE, ACCOUNT_A, ORG_A, title="Operationalizing certified components")
    say(log, logout("2026-09-05 11:29:50"))
    assert running.run_hot().added == 1
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-05 11:30:05", existing=1797))
    gap = running.load_gap()
    assert gap["status"] == "ok" and gap["pending"] == 0


def test_the_hot_pass_lists_only_folders_that_changed(world, monkeypatch):
    """C-23.28: a hot pass runs every 2 s over 120 folders of ~1,800 files each
    (2026-09-24), so it re-lists only folders whose directory moved and opens
    only the files that are new there."""
    home, store, _root, _log = world
    folders = [(f"account-{n}", f"org-{n}") for n in range(6)]
    for account, org in folders:
        (store / account / org).mkdir(parents=True)
    for n in range(20):
        openable(home, store, f"session-{n}", ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    running.run_hot()                     # re-lists what the full pass wrote
    reads = count_entry_reads(monkeypatch)
    quiet = running.run_hot()
    assert (quiet.folders_scanned, quiet.added, reads) == (0, 0, [])

    openable(home, store, TWO, ACCOUNT_B, ORG_B)
    result = running.run_hot()
    assert result.folders_scanned == 1 and reads == [store / ACCOUNT_B / ORG_B / f"local_{TWO}.json"]
    assert result.added == 7 and len(copies(store, TWO)) == 8
    assert result.entries_scanned == 8 * 20 + 1, "every entry is still accounted for"


def test_a_hot_pass_never_moves_the_full_pass_heartbeat(world):
    """C-23.28: health is judged from the full pass's record; a hot pass
    every 2 s must not keep a stalled full pass looking healthy."""
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    before = json.loads((root / "sessions" / mirror.SIDECAR_NAME).read_text())
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    running.now = lambda: fx.NOW + timedelta(minutes=45)
    assert running.run_hot().added == 1
    after = json.loads((root / "sessions" / mirror.SIDECAR_NAME).read_text())
    assert after["pass"] == before["pass"] and after["updated_at"] == before["updated_at"]
    assert after["hot"]["kind"] == "hot" and after["hot"]["added"] == 1
    assert running.health()["status"] == "stalled", "the full pass is 45 min old"


def test_the_first_hot_pass_in_a_process_is_a_full_pass(world):
    """C-23.28: spreading needs to know what every folder holds, so a fresh
    process inventories everything first."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    result = running.run_hot()
    assert result.kind == "full" and result.added == 1
    assert running.run_hot().kind == "hot"


def test_a_transcript_that_follows_its_record_is_spread_by_a_later_hot_pass(world):
    """C-23.28: the app writes a new session's record before its transcript
    exists, and a dead session is never spread; the hot pass retries the
    record until the transcript appears, without the record changing."""
    home, store, _root, _log = world
    running = engine(world)
    running.run_once()
    fx.index_entry(store, ACCOUNT_A, ORG_A, ONE)
    assert running.run_hot().added == 0
    fx.transcript(home, ONE, fx.completed())
    assert running.run_hot().added == 1
    assert len(copies(store, ONE)) == 2


def test_the_hot_pass_does_not_duplicate_a_session_held_under_its_fallback_name(world):
    """C-23.28 with v1's collision rule: an account that holds the session
    as `local_<cliSessionId>.json` already has it."""
    home, store, _root, _log = world
    running = engine(world)
    running.run_once()
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_original.json")
    fx.index_entry(store, ACCOUNT_B, ORG_B, ONE, name=f"local_{ONE}.json")
    assert running.run_hot().added == 0
    assert {path.name for path in copies(store, ONE)} == {"local_original.json",
                                                         f"local_{ONE}.json"}


def test_the_hot_pass_falls_back_on_a_name_collision(world):
    """v1's rule, kept in the hot pass: two new sessions that share a filename
    in two accounts each reach the other account under a cli-derived name, and
    neither resident is clobbered."""
    home, store, _root, _log = world
    running = engine(world)
    running.run_once()
    openable(home, store, TWO, ACCOUNT_B, ORG_B, name="local_shared.json")
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_shared.json")
    assert running.run_hot().added == 2
    for folder, resident, newcomer in ((store / ACCOUNT_B / ORG_B, TWO, ONE),
                                       (store / ACCOUNT_A / ORG_A, ONE, TWO)):
        assert json.loads((folder / "local_shared.json").read_text())["cliSessionId"] == resident
        fallback = json.loads((folder / f"local_{newcomer}.json").read_text())
        assert fallback["cliSessionId"] == newcomer
        assert fallback["sessionId"] == f"local_{newcomer}"


def test_a_hot_pass_that_finds_the_lock_held_touches_nothing(world):
    """C-23.28: a hot pass shares the full pass's lock and skips, silently."""
    import fcntl
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    sidecar = (root / "sessions" / mirror.SIDECAR_NAME).read_text()
    held = engine(world)._lock()                     # noqa: SLF001 - the seam
    try:
        result = running.run_hot()
        assert result.state == "ok" and "holds the lock" in result.error
        assert result.added == 0 and len(copies(store, TWO)) == 1
        assert (root / "sessions" / mirror.SIDECAR_NAME).read_text() == sidecar
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_a_dry_hot_pass_writes_nothing(world):
    """C-17.4: a preview that writes is not a preview."""
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    sidecar = (root / "sessions" / mirror.SIDECAR_NAME).read_text()
    assert running.run_hot(mirror.Options(dry_run=True)).added == 1
    assert len(copies(store, TWO)) == 1
    assert (root / "sessions" / mirror.SIDECAR_NAME).read_text() == sidecar


def test_a_hot_pass_stops_on_shutdown(world):
    """C-23.28: the daemon's stop cancels a hot pass like a full one."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    running.cancel = threading.Event()
    running.cancel.set()
    assert running.run_hot().state == "cancelled"


# --- incremental passes -------------------------------------------------------

def test_equal_bytes_are_parsed_once(world, monkeypatch):
    """C-23.28: 217,706 entries held 9,619 distinct contents on 2026-09-24, so a
    cold pass parses each distinct content once, and caches only the fields a
    pass reads."""
    home, store, _root, _log = world
    folders = [(ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)] + [
        (f"account-{n}", f"org-{n}") for n in range(4)]
    for n in range(10):
        fx.transcript(home, f"session-{n}", fx.completed())
        for account, org in folders:
            fx.index_entry(store, account, org, f"session-{n}", settings={"ultracode": True},
                           remoteMcpServersConfig={"blob": "x" * 20_000})
    parsed = []
    project = mirror._project
    monkeypatch.setattr(mirror, "_project", lambda value: parsed.append(1) or project(value))
    running = engine(world)
    assert running.run_once().entries_scanned == 60
    assert len(parsed) == 10
    payload = next(iter(running._payloads.values())).value
    assert "remoteMcpServersConfig" not in payload
    assert running._payload_bytes < 10 * 20_000, "the MCP blob is never retained"


def test_an_unchanged_folder_is_not_relisted(world, monkeypatch):
    """C-23.28: between sweeps a full pass skips a folder whose directory has
    not moved, and diffs a moved one by inode."""
    home, store, _root, _log = world
    for n in range(5):
        openable(home, store, f"session-{n}", ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    running.run_once()                                  # re-lists what the first wrote
    reads = count_entry_reads(monkeypatch)
    warm = running.run_once()
    assert (warm.swept, warm.folders_scanned, reads) == (False, 0, [])
    assert warm.entries_scanned == 10
    path = store / ACCOUNT_A / ORG_A / "local_session-0.json"
    rewrite(path, {**json.loads(path.read_text()), "isStarred": True})
    result = running.run_once()
    assert result.folders_scanned == 1 and reads == [path]
    assert all(row["isStarred"] for row in copies(store, "session-0").values())


def test_an_in_place_write_is_caught_by_the_next_sweep(world, monkeypatch):
    """C-23.28: the app never rewrites in place, but its fallback path can, and
    so can a person; a stat sweep every `SWEEP_INTERVAL_S` bounds the delay."""
    home, store, _root, _log = world
    path = openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running = engine(world)
    running.run_once()
    running.run_once()
    path.write_text(json.dumps({**json.loads(path.read_text()), "isArchived": True}))
    assert running.run_once().flag_synced == 0, "same inode, same directory: unseen"
    monkeypatch.setattr(mirror, "SWEEP_INTERVAL_S", 0.0)
    result = running.run_once()
    assert result.swept and result.flag_synced == 1
    assert all(row["isArchived"] for row in copies(store, ONE).values())


def test_transcripts_are_listed_one_level_down_and_cached(world, monkeypatch):
    """C-23.28: session transcripts are `projects/<slug>/<id>.jsonl`; deeper
    files are subagent logs (34k on 2026-09-24). A project directory is
    re-listed only when its mtime moves."""
    home, _store, _root, _log = world
    fx.transcript(home, ONE, fx.completed())
    deeper = home / "projects" / fx.project_slug() / ONE / "subagents"
    deeper.mkdir(parents=True)
    (deeper / "agent-a1.jsonl").write_text("{}\n")
    running = engine(world)
    assert set(running.transcript_stems(sweep=False)) == {ONE}
    listed = []
    scandir = os.scandir
    monkeypatch.setattr(mirror.os, "scandir", lambda path: listed.append(path) or scandir(path))
    assert set(running.transcript_stems(sweep=False)) == {ONE}
    assert [str(path) for path in listed] == [str(home / "projects")], \
        "an unchanged project dir is not re-listed"
    fx.transcript(home, TWO, fx.completed())
    assert set(running.transcript_stems(sweep=False)) == {ONE, TWO}


def test_the_archive_is_walked_again_only_for_a_new_dead_session(world, monkeypatch, tmp_path):
    """C-23.28: on 2026-09-24 the archive glob covered 56k files and none of
    the 126 dead sessions, and every pass walked it; now a pass walks it only
    when the set of dead sessions grows or `ARCHIVE_RESCAN_S` has passed."""
    _home, store, _root, _log = world
    archive = tmp_path / "archive"
    archive.mkdir()
    fx.index_entry(store, ACCOUNT_A, ORG_A, ONE)                # dead, not archived
    walks = []
    iglob = mirror.globbing.iglob
    monkeypatch.setattr(mirror.globbing, "iglob",
                        lambda *a, **k: walks.append(a[0]) or iglob(*a, **k))
    running = engine(world)
    options = mirror.Options(archive=str(archive / "*.jsonl"))
    running.run_once(options)
    running.run_once(options)
    assert len(walks) == 1
    fx.index_entry(store, ACCOUNT_A, ORG_A, TWO)                # a new dead session
    (archive / f"{TWO}.jsonl").write_text("archived transcript\n")
    assert running.run_once(options).revived == 1
    assert len(walks) == 2


# --- writes into the store ------------------------------------------------------

def test_copies_are_atomic_owner_only_and_keep_the_sidebar_order(world):
    """C-23.28: the app lists a folder in one sweep, so a copy is assembled
    beside its destination and renamed in; it keeps the source's mtime (the
    sidebar's order) and mode, and a written record is owner-only like the
    app's own (0600)."""
    home, store, _root, _log = world
    source = openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_shared.json")
    os.chmod(source, 0o600)
    os.utime(source, (1_700_000_000, 1_700_000_000))
    openable(home, store, TWO, ACCOUNT_B, ORG_B, name="local_shared.json")   # a collision
    openable(home, store, THREE, ACCOUNT_A, ORG_A, name="local_three.json")
    running = engine(world)
    running.run_once()
    fallback = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    copy = store / ACCOUNT_B / ORG_B / "local_three.json"
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o600
    assert fallback.stat().st_mtime == 1_700_000_000
    assert stat.S_IMODE(copy.stat().st_mode) == stat.S_IMODE(
        (store / ACCOUNT_A / ORG_A / "local_three.json").stat().st_mode)
    assert not list(store.glob("*/*/*.tmp-subfleet")), "no temporary file is left"
    assert not mirror._temporary(copy).name.endswith((".json", ".json.tmp")), \
        "the app lists *.json and promotes *.json.tmp; the mirror's temporaries are neither"


def test_a_copy_that_fails_is_skipped_and_the_pass_goes_on(world, monkeypatch):
    """C-23.28: a source the app deletes mid-pass used to abort the whole pass
    with an OSError; now that one copy waits for the next pass."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    openable(home, store, TWO, ACCOUNT_A, ORG_A)
    copy = mirror._copy_entry

    def vanish(source, destination):
        if ONE in source.name:
            raise FileNotFoundError(source)
        return copy(source, destination)

    monkeypatch.setattr(mirror, "_copy_entry", vanish)
    result = engine(world).run_once()
    assert (result.state, result.added, result.skipped) == ("ok", 1, 1)
    assert len(copies(store, TWO)) == 2


def test_flag_sync_patches_the_newest_record_and_keeps_what_it_does_not_sync(world, monkeypatch):
    """C-23.28: a flag write re-reads the file and patches only the synced
    fields, so what the app saved since the inventory survives."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    source = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(source, {**json.loads(source.read_text()), "isArchived": True})
    target = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    load = mirror._load

    def app_saves_first(path, **kwargs):
        if path == target and kwargs.get("strict"):
            rewrite(path, {**json.loads(path.read_text()), "remoteMcpServersConfig": {"new": 1}})
        return load(path, **kwargs)

    monkeypatch.setattr(mirror, "_load", app_saves_first)
    assert running.run_once().flag_synced == 1
    written = json.loads(target.read_text())
    assert written["isArchived"] is True and written["remoteMcpServersConfig"] == {"new": 1}


def test_a_copy_whose_flags_moved_under_the_pass_is_left_and_the_base_waits(world, monkeypatch):
    """C-23.28: if the synced fields changed after the inventory (a user acting
    in that account), the write is skipped and that session's merge base is
    not advanced, so the next pass decides again instead of reading the
    mirror's own half-finished sync as a user action."""
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    base_before = json.loads((root / "sessions" / mirror.FLAGS_NAME).read_text())[ONE]
    source = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(source, {**json.loads(source.read_text()), "isArchived": True})
    target = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    load = mirror._load

    def user_stars_it(path, **kwargs):
        if path == target and kwargs.get("strict"):
            rewrite(path, {**json.loads(path.read_text()), "isStarred": True})
        return load(path, **kwargs)

    monkeypatch.setattr(mirror, "_load", user_stars_it)
    running.run_once()
    assert json.loads(target.read_text())["isArchived"] is False, "left for the next pass"
    base = json.loads((root / "sessions" / mirror.FLAGS_NAME).read_text())[ONE]
    assert base == base_before
    monkeypatch.setattr(mirror, "_load", load)
    running.run_once()
    assert all(row["isArchived"] and row["isStarred"] for row in copies(store, ONE).values())


# --- the timer and the policy ---------------------------------------------------

def test_the_hot_pass_is_a_two_second_timer_on_the_mirror_worker(tmp_path):
    """C-23.28 and C-6.4: `sessions.mirror_hot_interval_s` (2 s) runs on the
    mirror's own worker, so it never runs beside a full pass; it is off with
    the mirror, and off on its own at 0."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    try:
        for overrides, expected in (({}, 2), ({"mirror_hot_interval_s": 0}, None),
                                    ({"mirror_interval_s": 0}, None)):
            timers = Timers(store, tmp_path, fx.policy(**overrides))
            try:
                assert timers.intervals.get("mirror_hot") == expected
                assert "mirror_hot" in timers.status()
                assert hasattr(timers, "mirror_hot_cycle")
            finally:
                timers.stop()
    finally:
        store.close()


def test_a_hot_pass_that_changes_nothing_records_no_timer_event(tmp_path, monkeypatch):
    """C-23.28: the store keeps timer events forever and replays them at start,
    so a 2 s timer records only the runs that changed or failed something."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy())
    try:
        def recorded():
            return [json.loads(row["data_json"]) for row in store.query(
                "SELECT data_json FROM events WHERE kind='timer.run'")
                if json.loads(row["data_json"]).get("timer") == "mirror_hot"]
        monkeypatch.setattr(timers, "mirror_hot_cycle", lambda: False)
        timers._run("mirror_hot")
        assert recorded() == [] and timers.status()["mirror_hot"]["last_run"]
        monkeypatch.setattr(timers, "mirror_hot_cycle", lambda: True)
        timers._run("mirror_hot")
        assert len(recorded()) == 1
    finally:
        timers.stop()
        store.close()


def test_the_timer_reuses_one_mirror_for_both_passes(tmp_path, monkeypatch):
    """C-23.28: the hot pass needs the full pass's inventory, in memory."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    made = []

    class Engine:
        def __init__(self, root, policy, *, now, cancel):
            made.append(self)

        def run_once(self, options):
            return mirror.Pass(started_at="x", state="ok")

        def run_hot(self, options):
            return mirror.Pass(started_at="x", state="ok", kind="hot", added=1)

    monkeypatch.setattr("subfleet.sessions.mirror.Mirror", Engine)
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy())
    try:
        timers.mirror_cycle()
        assert timers.mirror_hot_cycle() is True
        assert len(made) == 1
    finally:
        timers.stop()
        store.close()


@pytest.mark.parametrize("value, ok", [(0, True), (2, True), (0.5, True), (-1, False)])
def test_the_hot_interval_is_a_policy_cap(tmp_path, value, ok):
    """C-6.4: a nonnegative number; 0 switches the hot pass off."""
    from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
    policy = json.loads(Path(DEFAULT_POLICY_PATH).read_text())
    policy["sessions"]["mirror_hot_interval_s"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    if ok:
        assert load_policy(path)["sessions"]["mirror_hot_interval_s"] == value
    else:
        with pytest.raises(PolicyError):
            load_policy(path)


# --- where the operator reads it ---------------------------------------------------

def test_status_names_the_sessions_a_relaunch_would_list(world, monkeypatch):
    """C-17.4 and C-23.28: `sessions mirror --status` prints the health line and
    the load gap; `--json` stays one object."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from subfleet import cli
    home, store, root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, title="Operationalizing certified components")
    # A whole day before the pass, so the local zone the CLI reads it in cannot
    # reorder the two.
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-04 11:30:00"))
    mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW).run_once()
    monkeypatch.setenv("SUBFLEET_HOME", str(root))

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    code, out, _err = run(["sessions", "mirror", "--status"])
    assert "sidebar relaunch needed: 1 session copied into acct-bbb" in out
    assert "Operationalizing certified components" in out and "fix: quit and reopen" in out
    code, out, err = run(["sessions", "mirror", "--status", "--json"])
    value = json.loads(out)
    assert value["load_gap"]["status"] == "relaunch" and value["load_gap"]["pending"] == 1
    assert err == ""


def test_doctor_warns_when_the_running_app_cannot_list_mirrored_sessions(world):
    """C-17.3 and C-23.28: a `warn` row with a fix, which never fails doctor."""
    from subfleet import doctor
    home, store, root, log = world

    def row():
        return next(item for item in doctor.checks(root)
                    if item["check"] == "desktop sidebar load")

    assert row()["status"] == doctor.UNKNOWN, "no load in the log yet"
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-04 11:30:00"))
    mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW).run_once()
    item = row()
    assert item["status"] == doctor.WARN and "1 session copied" in item["detail"]
    assert "quit and reopen" in item["fix"]
    assert doctor.exit_code([item]) == 0
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-06 11:30:00"))
    assert row()["status"] == doctor.PASS
