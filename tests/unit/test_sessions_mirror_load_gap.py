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
    """Append lines to the app's log as the app writes them.

    The app appends as it goes, so the file's mtime is its last line's time;
    the reader calibrates each file's zone from exactly that. Here the lines'
    wall clock is UTC.
    """
    with log.open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")
    stamp = datetime.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    os.utime(log, (stamp.timestamp(), stamp.timestamp()))


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
    """C-23.28: a switch passes through transient account and org pairs before
    it settles, so the "Initialization succeeded" lines alone would name the
    wrong folder. The last "Loaded ... from <folder>" or missing-folder line
    does. These are the 2026-09-24 lines from 16:37:53 to 16:38:11 (ids
    replaced): the old account with the new org, missing; the new account with
    that org, loaded; then the org the switch settled on, loaded."""
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
        *loads(store, "acct-new", "org-new", "2026-09-24 16:38:05", count=260),
        "2026-09-24 16:38:11 [info] [LocalAgentModeSessionManager] Org changed from org-new to "
        "org-settled, reinitializing sessions",
        "2026-09-24 16:38:11 [info] [LocalSessionManager] Org changed from org-new to "
        "org-settled, reinitializing sessions",
        # The agent-mode manager logs the same shapes about a different store.
        "2026-09-24 16:38:11 [info] [LocalAgentModeSessionManager] Initialization succeeded — "
        "accountId=acct-new, orgId=org-settled, existingSessions=0",
        *loads(store, "acct-new", "org-settled", "2026-09-24 16:38:11", existing=1795,
               count=361),
        f"2026-09-24 16:38:11 [info] Loaded 72 persisted sessions from "
        f"{tmp_path / 'local-agent-mode-sessions' / 'acct-new' / 'org-settled'}")
    state = desktop.DesktopLog(log, store=store, tz=UTC).poll()
    assert (state.load.account, state.load.org) == ("acct-new", "org-settled")
    assert not state.load.missing and state.load.count == 361
    assert state.load.started_at == datetime(2026, 9, 24, 16, 38, 11, tzinfo=UTC)
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


def test_a_load_that_found_its_folder_missing_leaves_every_seeded_copy_pending(world):
    """C-23.28: the app initializes an account and org it never saved a session
    under, finds no folder, lists nothing, and creates the folder in the same
    second (writing `scheduled-tasks.json` there; 16:38:05 on 2026-09-24). The
    mirror seeds it after that load, so every seeded session waits for a
    relaunch, and the report says so. The mirror itself never creates a folder."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    say(log,
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Initialization succeeded — "
        "accountId=acct-new, orgId=org-new, existingSessions=0",
        "2026-09-05 11:00:00 [info] [LocalSessionManager] Session storage directory does not "
        f"exist yet, skipping load: {store / 'acct-new' / 'org-new'}")
    running = engine(world)
    assert running.run_once().added == 1
    assert not (store / "acct-new").exists(), "the mirror creates no folder of its own"
    created = store / "acct-new" / "org-new"                     # what the app does
    created.mkdir(parents=True, mode=0o700)
    (created / "scheduled-tasks.json").write_text("{}")
    assert running.run_once().added == 1
    gap = running.load_gap()
    assert gap["status"] == "relaunch" and gap["pending"] == 1
    assert "the running app lists a folder only when it loads it" in gap["detail"]


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
    beside its destination and put in place in one step; it keeps the source's
    mtime (the sidebar's order), and every file the mirror places is owner-only
    like the app's own (0600), whatever the source's mode."""
    home, store, _root, _log = world
    source = openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_shared.json")
    os.chmod(source, 0o600)
    os.utime(source, (1_700_000_000, 1_700_000_000))
    openable(home, store, TWO, ACCOUNT_B, ORG_B, name="local_shared.json")   # a collision
    three = openable(home, store, THREE, ACCOUNT_A, ORG_A, name="local_three.json",
                     settings={"ultracode": True})
    os.chmod(three, 0o644)
    running = engine(world)
    running.run_once()
    fallback = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    copy = store / ACCOUNT_B / ORG_B / "local_three.json"
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o600
    assert fallback.stat().st_mtime == 1_700_000_000
    assert stat.S_IMODE((store / ACCOUNT_A / ORG_A / "local_three.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600, "a copy is owner-only whatever its source"
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

    def vanish(source, destination, **kwargs):
        if ONE in source.name:
            raise FileNotFoundError(source)
        return copy(source, destination, **kwargs)

    monkeypatch.setattr(mirror, "_copy_entry", vanish)
    result = engine(world).run_once()
    assert (result.state, result.added, result.skipped) == ("ok", 1, 1)
    assert len(copies(store, TWO)) == 2


def test_flag_sync_patches_the_newest_record_and_keeps_what_it_does_not_sync(world, monkeypatch):
    """C-23.28: a flag write re-reads the file and patches only the synced
    fields, so what the app saved after the inventory survives."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    source = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(source, {**json.loads(source.read_text()), "isArchived": True})
    target = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    sync = running.sync_flags                            # after the inventory

    def app_saves_after_the_inventory(folder_files, *args, **kwargs):
        rewrite(target, {**json.loads(target.read_text()), "remoteMcpServersConfig": {"new": 1}})
        return sync(folder_files, *args, **kwargs)

    monkeypatch.setattr(running, "sync_flags", app_saves_after_the_inventory)
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


# --- regressions from the adversarial review of 2026-09-24 ------------------------

def at(hour: int, minute: int):
    """A mirror clock at a fixed instant on the fixture day (UTC)."""
    return lambda: datetime(2026, 9, 5, hour, minute, tzinfo=UTC)


def test_the_hot_timer_is_queued_on_the_mirror_worker(tmp_path, monkeypatch):
    """C-23.28: both mirror passes go to the one-worker mirror pool, so a hot
    pass never runs beside a full pass on the same Mirror, and neither takes a
    probe or keepalive slot."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy())
    submitted = []
    try:
        monkeypatch.setattr(timers._mirror, "submit",
                            lambda fn, name, *a: submitted.append(("mirror", name)))
        monkeypatch.setattr(timers._cycles, "submit",
                            lambda fn, name, *a: submitted.append(("cycles", name)))
        timers.start()
        timers._due = {name: 0 for name in timers.intervals}
        timers.tick()
        assert {("mirror", "mirror"), ("mirror", "mirror_hot")} <= set(submitted)
        assert not [item for item in submitted
                    if item[0] == "cycles" and item[1].startswith("mirror")]
    finally:
        timers.stop()
        store.close()


def test_a_record_the_full_pass_read_before_its_transcript_is_retried_by_the_hot_pass(world):
    """C-23.28: whichever pass lists a new record first, a transcript that
    follows it gets the session spread by the next hot pass, not a full pass
    a minute later."""
    home, store, _root, _log = world
    running = engine(world)
    running.run_once()
    fx.index_entry(store, ACCOUNT_A, ORG_A, ONE, settings={"ultracode": True})
    assert running.run_once().added == 0                  # no transcript yet
    fx.transcript(home, ONE, fx.completed())
    assert running.run_hot().added == 1
    assert len(copies(store, ONE)) == 2


def test_a_folder_that_cannot_be_listed_is_skipped_not_taken_as_empty(world, monkeypatch):
    """C-23.28: an unreadable listing is not an empty folder. Taken as one, it
    hid what the folder held, and a later pass copied a session into it beside
    its own fallback-named copy."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_original.json",
             settings={"ultracode": True})
    fx.index_entry(store, ACCOUNT_B, ORG_B, ONE, name=f"local_{ONE}.json",
                   settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    blocked = store / ACCOUNT_B / ORG_B
    scandir = os.scandir

    def unreadable(path):
        if Path(path) == blocked:
            raise PermissionError(path)
        return scandir(path)

    monkeypatch.setattr(mirror, "SWEEP_INTERVAL_S", 0.0)
    monkeypatch.setattr(mirror.os, "scandir", unreadable)
    assert running.run_once().state == "ok"
    monkeypatch.setattr(mirror.os, "scandir", scandir)
    monkeypatch.setattr(mirror, "SWEEP_INTERVAL_S", 600.0)
    running.run_once()
    running.run_hot()
    assert sorted(path.name for path in copies(store, ONE) if path.parent == blocked) == \
        [f"local_{ONE}.json"], "one entry per session per folder"


def test_journal_rows_another_process_wrote_survive_the_daemons_saves(world):
    """C-23.28: the daemon keeps one Mirror for its life; a `sessions mirror`
    pass in another process journals its own copies, and the daemon's next
    save keeps them, so the report counts every late copy."""
    home, store, _root, log = world
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    daemon, other = engine(world), engine(world)
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    daemon.run_once()
    openable(home, store, TWO, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    assert other.run_once().added == 1
    openable(home, store, THREE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    assert daemon.run_hot().added == 1
    assert engine(world).load_gap()["pending"] == 3
    assert daemon.load_gap()["pending"] == 3


def test_a_name_taken_between_the_listing_and_the_copy_is_left_alone(world, monkeypatch):
    """C-23.28: a new copy is placed create-only, so a record the app creates
    under that name meanwhile is never replaced."""
    home, store, _root, _log = world
    running = engine(world)
    running.run_once()
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    target = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    copy2 = mirror.shutil.copy2

    def the_app_creates_it_meanwhile(source, destination, **kwargs):
        result = copy2(source, destination, **kwargs)
        if not target.exists():
            target.write_text(json.dumps({"sessionId": "the app's", "cliSessionId": TWO}))
        return result

    monkeypatch.setattr(mirror.shutil, "copy2", the_app_creates_it_meanwhile)
    assert running.run_hot().added == 0
    assert json.loads(target.read_text())["sessionId"] == "the app's"
    assert not list(store.glob("*/*/*.tmp-subfleet"))


def test_a_replacement_is_abandoned_if_the_target_moved_after_the_decision(tmp_path):
    """C-23.28: a flag write or repair names the signature it decided on; an
    app save that lands before the rename wins, and nothing is left behind."""
    target = tmp_path / "local_x.json"
    target.write_text("{}")
    expect = mirror._signature_of(target)
    rewrite(target, {"app": "newer"})
    assert mirror._write_json(target, {"mirror": "older"}, expect=expect) is None
    assert json.loads(target.read_text()) == {"app": "newer"}
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"mirror": "older"}))
    assert mirror._copy_entry(source, target, expect=expect) is None
    assert json.loads(target.read_text()) == {"app": "newer"}
    assert not list(tmp_path.glob("*.tmp-subfleet"))


def test_a_repair_the_app_overwrote_with_its_stale_record_still_waits(world):
    """C-23.28: the app holds the stale empty record it loaded, so its re-save
    puts the empty id back; that is not the app holding the repair, and the
    session still waits for a fresh load."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    stale = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    body = {"sessionId": f"local_{ONE}", "cliSessionId": "", "title": "stale"}
    stale.write_text(json.dumps(body))
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    assert running.run_once().repaired == 1
    rewrite(stale, body)                          # the app re-saves what it holds
    gap = running.load_gap()
    assert gap["status"] == "relaunch" and gap["pending"] == 1


def test_a_pass_that_copied_says_to_relaunch_even_without_the_apps_log(world, monkeypatch):
    """C-17.4 and C-23.28: with no load in the app's log the mirror cannot tell
    whether a copy reached the loaded folder, so it keeps v1's advice."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from subfleet import cli
    home, store, root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["sessions", "mirror"]) == 0
    assert "added 1" in out.getvalue()
    assert "does not say which folder it loaded" in err.getvalue()
    assert "quit and reopen" in err.getvalue()


def test_a_cold_pass_does_not_queue_long_dead_sessions_for_retry(world):
    """C-23.28: to a cold pass every record is new; only a record written within
    `UNRESOLVED_RETRY_S` can be waiting for its transcript, so an old dead
    session is not retried every 2 s for an hour after each restart."""
    _home, store, _root, _log = world
    old = fx.index_entry(store, ACCOUNT_A, ORG_A, ONE, settings={"ultracode": True})
    os.utime(old, (1_700_000_000, 1_700_000_000))
    fx.index_entry(store, ACCOUNT_A, ORG_A, TWO, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    assert set(running._retry) == {TWO}


# --- regressions from the second review round ---------------------------------------

def test_log_times_are_read_in_the_zone_the_app_wrote_them_in(tmp_path):
    """C-23.28: the log's wall clock has no zone. Read in the reader's zone, a
    trip would move every earlier load by the zone difference; each file's
    offset comes from its newest line against its own mtime instead."""
    import time as clock
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    with log.open("w", encoding="utf-8") as stream:     # written in Los Angeles (PDT)
        for line in loads(store, "a", "o", "2026-09-05 04:00:00"):
            stream.write(line + "\n")
    written = datetime(2026, 9, 5, 11, 0, tzinfo=UTC).timestamp()
    os.utime(log, (written, written))
    before = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Tokyo"                      # read in Tokyo
        clock.tzset()
        load = desktop.DesktopLog(log, store=store).poll().load
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        clock.tzset()
    assert load.started_at == datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


def test_the_report_leaves_the_hot_pass_its_signal(world):
    """C-23.28: measuring the gap must not update the inventory's cache, or a
    record the app rewrote after the listing is never new to the hot pass."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    running.run_once()
    copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    cached = dict(running._entries)
    rewrite(copy, {**json.loads(copy.read_text()), "title": "renamed in B"})
    running.load_gap()
    assert running._entries == cached


def test_a_record_the_app_cleared_is_not_repaired(world):
    """C-23.28 with v1's repair rule: /clear empties the id itself (it records
    the old one in `priorCliSessionIds` first), and a cwd or worktree move does
    too. That record is the app's newest, not a frozen copy: repairing it would
    undo the /clear at the next load, and the report would ask for that load."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    cleared = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    cleared.write_text(json.dumps({"sessionId": f"local_{ONE}", "cwd": fx.WORKDIR,
                                   "priorCliSessionIds": [ONE], "title": "a session"}))
    moved = store / ACCOUNT_B / ORG_B / "local_moved.json"
    openable(home, store, TWO, ACCOUNT_A, ORG_A, name="local_moved.json",
             settings={"ultracode": True})
    moved.write_text(json.dumps({"sessionId": "local_moved", "cliSessionId": "",
                                 "cwd": "/Users/fixture/elsewhere", "lastActivityAt": 5000}))
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    body, other = cleared.read_text(), moved.read_text()
    running = engine(world)
    assert running.run_once().repaired == 0
    assert cleared.read_text() == body and moved.read_text() == other
    assert running.load_gap()["status"] == "ok"


def test_a_hot_pass_does_not_duplicate_a_session_whose_save_raced_its_listing(world, monkeypatch):
    """C-23.28: the app empties then refills a record's id under its name; if
    the refill lands while the hot pass reads the empty version, the folder
    already holds the session and gets no fallback-named second copy."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_x.json",
             settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    running.run_hot()
    b_copy = store / ACCOUNT_B / ORG_B / "local_x.json"
    full = json.loads(b_copy.read_text())
    rewrite(b_copy, {**full, "cliSessionId": ""})
    read = mirror._read_entry
    armed = {"on": True}

    def refilled_just_after_the_read(path):
        raw = read(path)
        if armed["on"] and path == b_copy:
            armed["on"] = False
            rewrite(b_copy, full)
        return raw

    monkeypatch.setattr(mirror, "_read_entry", refilled_just_after_the_read)
    running.run_hot()
    assert sorted(path.name for path in copies(store, ONE)
                  if path.parent == b_copy.parent) == ["local_x.json"]


def test_a_session_whose_copy_moved_mid_pass_is_written_whole_or_not_at_all(world, monkeypatch):
    """C-23.28: if any copy's synced fields moved between the listing and the
    write (an app save), none of that session's copies is written and its merge
    base is held, so no partial write can later outvote the user's reversal."""
    home, store, _root, _log = world
    folders = ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B), ("acct-cccc", "org-cccc"))
    for account, org in folders:
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    c_copy = store / "acct-cccc" / "org-cccc" / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})   # the user stars
    sync = running.sync_flags
    moved = {"once": True}

    def b_saves_mid_pass(folder_files, *args, **kwargs):
        if moved["once"]:
            moved["once"] = False
            rewrite(b_copy, {**json.loads(b_copy.read_text()), "title": "retitled in B"})
        return sync(folder_files, *args, **kwargs)

    monkeypatch.setattr(running, "sync_flags", b_saves_mid_pass)
    running.run_once()
    assert json.loads(c_copy.read_text())["isStarred"] is False, "none written"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": False})  # and un-stars
    running.run_once()
    assert not any(row["isStarred"] for row in copies(store, ONE).values())


def test_a_same_folder_relogin_does_not_hide_stale_writes(world):
    """C-23.28: a re-login to the same account and org re-reads no record the
    app holds, so writes since the last fresh load still count as stale."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.now = at(10, 30)
    running.run_once()                                    # ONE reaches B
    say(log, *loads(store, ACCOUNT_B, ORG_B, "2026-09-05 11:00:00"))
    source = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(source, {**json.loads(source.read_text()), "isArchived": True})
    running.now = at(11, 30)
    running.run_once()                                    # the archive reaches B
    assert running.load_gap()["stale"] == 1
    say(log, logout("2026-09-05 11:44:00"),
        *loads(store, ACCOUNT_B, ORG_B, "2026-09-05 11:45:00", existing=40))
    assert running.load_gap()["stale"] == 1


def test_sessions_list_reports_the_gap_even_when_nothing_is_listed(world, monkeypatch):
    """C-17.4 and C-23.28: `sessions list` is one of the three places the gap is
    reported, including when no live session is registered; `--json` output
    stays objects only."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from subfleet import cli
    from subfleet.sessions import cli as sessions_cli
    home, store, root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW).run_once()
    monkeypatch.setenv("SUBFLEET_HOME", str(root))

    class Daemon:
        def state(self, _session):
            # Nothing registered; the conversation fence (C-26.13) answered.
            return {"sessions": {}, "lane_sessions": [], "conversation_sessions": []}

    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: Daemon())
    for argv, quiet in ((["sessions", "list"], False), (["sessions", "list", "--json"], True)):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            assert cli.main(argv) == 0
        if quiet:
            assert err.getvalue() == ""
        else:
            assert "sidebar: 1 session copied into" in err.getvalue()
            assert "fix: quit and reopen" in err.getvalue()


def test_a_hot_pass_skipped_for_the_lock_leaves_no_timer_event(tmp_path, monkeypatch):
    """C-23.28: with a real Mirror, a hot pass that found the lock held changes
    nothing, so the 2 s timer records no `timer.run` event for it."""
    import fcntl
    import time as clock
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy())
    holder = mirror.Mirror(tmp_path, fx.policy())._lock()          # noqa: SLF001
    try:
        timers.start()
        timers._due["mirror_hot"] = 0
        timers.tick()
        deadline = clock.monotonic() + 10
        while "mirror_hot" in timers._running and clock.monotonic() < deadline:
            clock.sleep(0.02)
        events = [json.loads(row["data_json"]) for row in store.query(
            "SELECT data_json FROM events WHERE kind='timer.run'")]
        assert timers.status()["mirror_hot"]["last_run"]
        assert not [event for event in events if event.get("timer") == "mirror_hot"]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        timers.stop()
        store.close()


def test_every_test_starts_without_the_operators_claude_dir():
    """C-23.28: the mirror reads `~/.claude` (options, transcripts); a daemon
    under test runs it within 2 s, so the default is an empty temporary dir."""
    from subfleet.sessions import transcripts
    assert not str(transcripts.claude_dir()).startswith(str(Path.home() / ".claude"))


# --- regressions from the third review round ----------------------------------------

def test_a_frozen_copy_of_a_once_cleared_session_is_still_repaired(world):
    """C-23.28 with v1's repair rule: `priorCliSessionIds` only grows, so a copy
    that froze empty long after an unrelated /clear still lists an old id; only
    a record that moved off THIS session is the app's own."""
    home, store, _root, _log = world
    old = "0d0d0d0d-0000-4000-8000-000000000000"
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_x.json",
             priorCliSessionIds=[old], settings={"ultracode": True})
    frozen = store / ACCOUNT_B / ORG_B / "local_x.json"
    frozen.write_text(json.dumps({"sessionId": "local_x", "cliSessionId": "", "cwd": fx.WORKDIR,
                                  "priorCliSessionIds": [old], "lastActivityAt": 500}))
    assert engine(world).run_once().repaired == 1
    assert json.loads(frozen.read_text())["cliSessionId"] == ONE


def test_a_torn_record_is_repaired_as_v1_did(world):
    """C-23.28: a copy that does not parse (a torn write) is replaced by the
    openable one, as v1 replaced it."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    torn = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    torn.write_text('{"sessionId": "local_')
    assert engine(world).run_once().repaired == 1
    assert json.loads(torn.read_text())["cliSessionId"] == ONE


def test_the_report_reads_a_pending_copy_only_when_it_changed(world, monkeypatch):
    """C-23.28: the report runs after every 2 s hot pass; an unchanged pending
    copy is read once, not every time."""
    home, store, _root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    running.run_once()
    reads = []
    load = mirror._load
    monkeypatch.setattr(mirror, "_load", lambda path, **kw: reads.append(path) or load(path, **kw))
    assert running.load_gap()["pending"] == 1
    assert running.load_gap()["pending"] == 1
    assert len([path for path in reads if path.name.startswith("local_")]) == 0, \
        "the pass that made the copy already read it"


def test_a_pass_that_cannot_see_the_load_prunes_only_by_age(world):
    """C-23.28: a `sessions mirror` pass without the app's log (or with a log
    that names no load) must not prune the shared journal rows the daemon,
    which can see the load, still needs."""
    home, store, root, log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    engine(world).run_once()
    blind = mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW + timedelta(hours=2))
    blind._desktop = desktop.DesktopLog(log.with_name("absent.log"), store=store, tz=UTC)
    blind.run_once()
    assert engine(world).load_gap()["pending"] == 1


def test_a_load_without_its_initialization_line_counts_as_fresh(tmp_path):
    """C-23.28: without the "Initialization succeeded" line the list's size is
    unknown; the load counts as fresh, as launches and switches are, so a
    relaunch clears the report instead of leaving it stuck."""
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    say(log, *loads(store, "a", "o", "2026-09-24 10:00:00"),
        loads(store, "a", "o", "2026-09-24 11:00:00")[1])
    load = desktop.DesktopLog(log, store=store, tz=UTC).poll().load
    assert load.fresh_started_at == datetime(2026, 9, 24, 11, 0, tzinfo=UTC)


def test_a_reader_in_the_writers_zone_gets_each_lines_dst(tmp_path):
    """C-23.28: in the writer's own zone each line keeps its own date's DST, so
    a log that spans the 2026-11-01 fall-back places both sides correctly."""
    import time as clock
    store = tmp_path / "store"
    log = tmp_path / "main.log"
    with log.open("w", encoding="utf-8") as stream:
        stream.write(loads(store, "a", "o", "2026-10-31 12:00:00")[1] + "\n")   # EDT
        stream.write("2026-11-02 12:00:00 [info] unrelated\n")                    # EST
    written = datetime(2026, 11, 2, 17, 0, tzinfo=UTC).timestamp()
    os.utime(log, (written, written))
    before = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        clock.tzset()
        load = desktop.DesktopLog(log, store=store).poll().load
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        clock.tzset()
    assert load.started_at == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)


def test_the_mirror_syncs_what_it_writes_before_the_rename(world, monkeypatch):
    """C-23.28: like the app's own writes, a copy and a rewrite reach the disk
    before they are renamed into the folder the app lists."""
    home, store, _root, _log = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    synced = []
    fsync = os.fsync
    monkeypatch.setattr(mirror.os, "fsync", lambda fd: synced.append(fd) or fsync(fd))
    result = engine(world).run_once()
    assert result.added == 1 and result.flag_synced == 0
    assert len(synced) == 4, "the copy, the ultracode rewrites of both copies, and the " \
        "merge base; the journal and the sidecar only feed reports and are not synced"


def test_a_batch_split_by_a_racing_save_is_rolled_back(world, monkeypatch):
    """C-23.28: an app save landing between the last check and the rename of
    one copy fails that copy's write; the copies already written in the batch
    are put back, so the held merge base matches every file and a user's
    later reversal is not outvoted by the mirror's half-written batch."""
    home, store, _root, _log = world
    folders = ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B), ("acct-cccc", "org-cccc"))
    for account, org in folders:
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    c_copy = store / "acct-cccc" / "org-cccc" / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})   # the user stars
    install = mirror._install
    raced = {"once": True}

    def the_app_saves_c_first(temporary, destination, **kwargs):
        if raced["once"] and destination == c_copy and kwargs.get("expect") is not None:
            raced["once"] = False
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", the_app_saves_c_first)
    running.run_once()
    assert json.loads(b_copy.read_text())["isStarred"] is False, "rolled back"
    assert json.loads(c_copy.read_text())["isStarred"] is False
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": False})  # and un-stars
    running.run_once()
    assert not any(row["isStarred"] for row in copies(store, ONE).values())


ACCOUNT_C, ORG_C = "acct-cccc", "org-cccc"


def the_app_saves_first(monkeypatch, victim: Path, before=None) -> None:
    """Fail the next flag write into `victim`, as an app save landing between
    the pass's last check and its rename would; `before` runs first."""
    install = mirror._install
    raced = {"once": True}

    def racing(temporary, destination, **kwargs):
        if raced["once"] and destination == victim and kwargs.get("expect") is not None:
            raced["once"] = False
            if before is not None:
                before()
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", racing)


@pytest.mark.parametrize("race", [False, True])
def test_a_rolled_back_copy_still_waits_for_the_load(world, monkeypatch, race):
    """C-23.28: rolling back a split batch rewrites the copies it put back. The
    report must still read each one as the mirror's copy the running app has
    not listed, not as the app rewriting it (review round 4)."""
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    assert running.run_once().added == 1
    assert running.load_gap()["pending"] == 1
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    if race:
        the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    running.run_once()
    assert json.loads(b_copy.read_text())["isStarred"] is (not race)
    assert running.load_gap()["pending"] == 1


@pytest.mark.parametrize("race", [False, True])
def test_a_rolled_back_flag_write_still_counts_as_stale(world, monkeypatch, race):
    """C-23.28: the same for a flag write the running app has not seen: the
    rollback of a later batch restores it and keeps it counted."""
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.now = lambda: fx.NOW - timedelta(hours=1)
    running.run_once()
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running.now = lambda: fx.NOW
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isArchived": True})
    running.run_once()
    assert running.load_gap()["stale"] == 1
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    if race:
        the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    running.run_once()
    assert running.load_gap()["stale"] >= 1


def test_a_rollback_leaves_an_app_save_made_after_the_mirror_wrote(world, monkeypatch):
    """C-23.28: the rollback puts back only a copy still as the mirror wrote
    it. A copy the app saved after the mirror's write keeps that save."""
    home, store, _root, _log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})

    def the_app_saves_b():
        rewrite(b_copy, {**json.loads(b_copy.read_text()), "lastFocusedAt": 42})

    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json",
                        before=the_app_saves_b)
    running.run_once()
    b = json.loads(b_copy.read_text())
    assert b["lastFocusedAt"] == 42 and b["isStarred"] is True, "the app's save stands"


def test_a_rollback_that_cannot_write_journals_the_write_it_left(world, monkeypatch):
    """C-23.28: if putting a copy back fails, the mirror's write stands, and
    the journal says so, so the report can count it."""
    home, store, _root, _log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    write = mirror._write_json
    written = []

    def refuse_the_rollback(path, value, **kwargs):
        if path == b_copy and written.count(path):
            raise OSError("disk full")
        written.append(path)
        return write(path, value, **kwargs)

    monkeypatch.setattr(mirror, "_write_json", refuse_the_rollback)
    running.run_once()
    assert json.loads(b_copy.read_text())["isStarred"] is True
    rows = [row for row in running.journal.rows()
            if row.name == b_copy.name and row.folder == f"{ACCOUNT_B}/{ORG_B}"]
    assert rows[-1].kind == "updated" and rows[-1].ctime_ns == os.stat(b_copy).st_ctime_ns


def test_starred_anywhere_wins_with_no_base(world):
    """C-23.28: the bootstrap rule is set per flag. With no merge base, a star
    in one copy wins, as an archive does, and the base records it."""
    home, store, root, _log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    running = engine(world)
    running.run_once()
    assert all(row["isStarred"] for row in copies(store, ONE).values())
    assert mirror._load(running.flags_path)[ONE]["isStarred"] is True


def test_a_rollback_to_the_apps_save_is_not_restamped(world, monkeypatch):
    """C-23.28: when the rollback restores the app's own save, the report reads
    it as the app's, not as the mirror's old write (review round 5)."""
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.now = lambda: fx.NOW - timedelta(hours=1)
    running.run_once()                        # B copied at 10:30
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))   # app loads B at 11:00
    running.now = lambda: fx.NOW
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isArchived": True})
    running.run_once()                        # flag write into B at 11:30
    assert running.load_gap()["stale"] == 1
    rewrite(b_copy, {**json.loads(b_copy.read_text()), "lastFocusedAt": 42})  # app re-saves B
    assert running.load_gap()["stale"] == 0
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    running.run_once()                        # B written, C raced, B rolled back to app save
    b = json.loads(b_copy.read_text())
    assert b["isStarred"] is False and b["lastFocusedAt"] == 42, "rolled back to app save"
    assert running.load_gap()["stale"] == 0


def test_a_restamped_row_keeps_its_original_time(world, monkeypatch):
    """C-23.28: a restamp keeps the copy's original time, so a copy the app
    already lists does not read as pending again."""
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.now = lambda: fx.NOW - timedelta(hours=1)
    assert running.run_once().added == 1      # B copied at 10:30
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))   # app loads B at 11:00
    running.now = lambda: fx.NOW
    assert running.load_gap()["pending"] == 0
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    running.run_once()
    assert json.loads(b_copy.read_text())["isStarred"] is False, "rolled back"
    assert running.load_gap()["pending"] == 0


def three_copies(world):
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    return store, running


def test_an_app_save_right_after_the_rename_is_not_rolled_back(world, monkeypatch):
    """C-23.28: a copy the app replaced right after the mirror's rename is not
    the mirror's write any more; the rollback leaves it."""
    store, running = three_copies(world)
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    write = mirror._write_json
    state = {"done": False}

    def app_saves_b_after_rename(path, value, **kwargs):
        result = write(path, value, **kwargs)
        if path == b_copy and not state["done"] and kwargs.get("expect") is not None:
            state["done"] = True
            rewrite(b_copy, {**json.loads(b_copy.read_text()), "lastFocusedAt": 42})
        return result

    monkeypatch.setattr(mirror, "_write_json", app_saves_b_after_rename)
    running.run_once()
    b = json.loads(b_copy.read_text())
    assert b.get("lastFocusedAt") == 42, "the app's save stands"


def test_a_failed_rollback_does_not_journal_the_apps_save(world, monkeypatch):
    """C-23.28: if the rollback fails after the app replaced the copy, the
    journal does not record the app's save as the mirror's."""
    store, running = three_copies(world)
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    write = mirror._write_json
    written = []

    def app_saves_b_then_disk_full(path, value, **kwargs):
        if path == b_copy and written.count(path):
            rewrite(b_copy, {**json.loads(b_copy.read_text()), "lastFocusedAt": 42})
            raise OSError("disk full")
        written.append(path)
        return write(path, value, **kwargs)

    monkeypatch.setattr(mirror, "_write_json", app_saves_b_then_disk_full)
    running.run_once()
    rows = [row for row in running.journal.rows()
            if row.name == b_copy.name and row.folder == f"{ACCOUNT_B}/{ORG_B}"]
    assert all(row.ctime_ns != os.stat(b_copy).st_ctime_ns for row in rows), "app save not journaled as ours"


def test_an_app_save_after_the_rollback_is_not_restamped(world, monkeypatch):
    """C-23.28: a copy the app replaced after the rollback is the app's."""
    home, store, _root, log = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_C, ORG_C)):
        openable(home, store, ONE, account, org, settings={"ultracode": True})
    say(log, *loads(store, ACCOUNT_B, ORG_B, BEFORE))
    running = engine(world)
    assert running.run_once().added == 1
    assert running.load_gap()["pending"] == 1
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    b_copy = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    rewrite(a_copy, {**json.loads(a_copy.read_text()), "isStarred": True})
    the_app_saves_first(monkeypatch, store / ACCOUNT_C / ORG_C / f"local_{ONE}.json")
    write = mirror._write_json
    written = []

    def app_saves_b_after_rollback(path, value, **kwargs):
        result = write(path, value, **kwargs)
        if path == b_copy and written.count(path):
            rewrite(b_copy, {**json.loads(b_copy.read_text()), "lastFocusedAt": 42})
        written.append(path)
        return result

    monkeypatch.setattr(mirror, "_write_json", app_saves_b_after_rollback)
    running.run_once()
    assert json.loads(b_copy.read_text())["lastFocusedAt"] == 42
    assert running.load_gap()["pending"] == 0, "the app rewrote it, so it holds it"
