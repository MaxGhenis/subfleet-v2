"""The sidebar mirror never blocks in open(): C-23.28.

The daemon runs the mirror on the timers' own worker, `Timers.stop()` waits for
that worker, and `Daemon.close()` calls `Timers.stop()`. The mirror opened what
it reads outside its state root plainly: the desktop store's index records
(listed by name, `local_*.json`), a transcript's tail for its custom title, the
app's log, `cc-mirror.json`, and the files its copies read. A FIFO with no
writer at any of those paths made open() wait for a writer, and held the pass,
the worker and `Daemon.close()` with it, as a FIFO where a Codex rollout
belonged had held the conversation service's `close()` (C-25.3).

Every call under test runs on a helper thread with a timeout, so a regression
fails the test instead of hanging it. Each FIFO's write end is then opened
without blocking until the thread ends, which releases a reader stuck in
open(). The store, the transcripts and the log all live under `tmp_path`.
"""

from __future__ import annotations

import itertools
import json
import os
import stat
import threading
import time
from concurrent.futures import wait
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet.sessions import desktop, mirror
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
DEAD = "0b5e7c11-2d3f-4a55-8e6d-7f8091a2b3c4"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: A pass over this store takes milliseconds; one blocked in open() never ends.
#: Generous, because the machine these run on can be heavily loaded.
WAIT_S = 15.0


def release(fifo: Path) -> None:
    """Open the FIFO's write end without blocking, then close it. A reader
    waiting in open() returns and reads end of file; with no reader waiting,
    the open fails (ENXIO) and nothing changes."""
    try:
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(descriptor)


def finishes(call, *fifos: Path, timeout: float = WAIT_S):
    """`call()` on a helper thread: (its result or exception, whether it finished
    within `timeout`). The FIFOs are released until the thread ends, so a call
    that blocked in open() fails its test without hanging the run."""
    out: list = []

    def run():
        try:
            out.append(call())
        except BaseException as exc:        # noqa: BLE001 - the outcome under test
            out.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        thread.join(timeout)
        finished = not thread.is_alive()
    finally:
        deadline = time.monotonic() + timeout
        while thread.is_alive() and time.monotonic() < deadline:
            for fifo in fifos:
                release(fifo)
            thread.join(0.05)
    return (out[0] if out else None), finished


def make(kind: str, path: Path) -> Path:
    """Put something other than a regular file at `path`: a FIFO with no writer,
    a directory, or a link to a device."""
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(os.devnull)
    return path


def node(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    return stat.S_IFMT(info.st_mode), info.st_ino


def record(store: Path, account: str, org: str, session_id: str = SESSION) -> Path:
    return store / account / org / f"local_{session_id}.json"


def rewrite(path: Path, **fields) -> None:
    """Change an index record as the app does: write beside it, then rename."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(path.read_text()), **fields}))
    temporary.replace(path)


def archived(store: Path) -> tuple[bool, ...]:
    return tuple(json.loads(record(store, *folder).read_text())["isArchived"]
                 for folder in FOLDERS)


def transcript_lines() -> str:
    return "".join(json.dumps(entry) + "\n" for entry in fx.completed())


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A `~/.claude` with one transcript, a three-account store holding its
    session, an empty app log, and a mirror on a ticking fixture clock."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    fx.transcript(home, SESSION, fx.completed())
    for account, org in FOLDERS:
        fx.index_entry(store, account, org, SESSION, settings={"ultracode": True})
    log = tmp_path / "logs" / "main.log"
    log.parent.mkdir()
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    root = tmp_path / "state"
    root.mkdir()
    ticks = itertools.count()
    running = mirror.Mirror(root, fx.policy(),
                            now=lambda: fx.NOW + timedelta(seconds=next(ticks)))
    return SimpleNamespace(tmp=tmp_path, home=home, store=store, log=log, root=root,
                           running=running,
                           project=home / "projects" / fx.project_slug())


# --- an index record in the desktop store ---------------------------------------

def test_a_fifo_named_like_a_record_neither_blocks_a_pass_nor_holds_flag_sync(world):
    """C-23.28: the store is listed by name, so a FIFO named like a record reached
    the entry read, which blocked in open(). Nothing the app could list is
    there, so like a directory of that name it is in no sidebar and holds
    nothing, and the mirror leaves it where it is."""
    fifo = world.store / "acct-b" / "org-b" / "local_odd.json"
    os.mkfifo(fifo)
    before = node(fifo)
    rewrite(record(world.store, "acct-a", "org-a"), isArchived=True)
    for _ in range(2):
        result, finished = finishes(world.running.run_once, fifo)
        assert finished, "a full pass blocked in open() on a FIFO in the store"
        assert result.state == "ok" and result.flags_held == 0, result
    assert archived(world.store) == (True,) * 3
    assert node(fifo) == before


def test_a_fifo_record_after_the_inventory_does_not_block_the_hot_pass(world):
    """C-23.28: the hot pass re-lists a folder whose directory changed and reads
    each new name in it."""
    assert world.running.run_once().state == "ok"
    fifo = world.store / "acct-c" / "org-c" / "local_new.json"
    os.mkfifo(fifo)
    result, finished = finishes(world.running.run_hot, fifo)
    assert finished, "a hot pass blocked in open() on a FIFO in the store"
    assert result.state == "ok" and result.kind == "hot", result


# --- a transcript -------------------------------------------------------------

@pytest.mark.parametrize("kind", ["fifo", "directory", "device"])
def test_the_title_read_answers_nothing_at_once_for_a_non_regular_file(tmp_path, kind):
    """C-23.28: `transcript_title` answers None (no signal) at once."""
    path = make(kind, tmp_path / f"{SESSION}.jsonl")
    title, finished = finishes(lambda: mirror.Mirror.transcript_title(path), path)
    assert finished, f"the title read blocked in open() on a {kind}"
    assert title is None


def test_a_transcript_that_became_a_fifo_does_not_block_flag_sync(world):
    """C-23.28: flag sync reads a transcript's last custom title whenever its
    mtime moved. A transcript that is a link keeps its project directory's
    listing unchanged when its target is replaced, so between sweeps the pass
    still holds it and reads it: a FIFO there blocked in open()."""
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "kept.jsonl"
    target.write_text(transcript_lines())
    link = world.project / f"{SESSION}.jsonl"
    link.unlink()
    link.symlink_to(target)
    assert world.running.run_once().state == "ok"          # inventories the link
    target.unlink()
    os.mkfifo(target)
    result, finished = finishes(world.running.run_once, target)
    assert finished, "flag sync blocked in open() reading a FIFO's title"
    assert result.state == "ok" and not result.swept and result.transcript_retitled == 0, result
    assert {json.loads(record(world.store, *folder).read_text())["title"]
            for folder in FOLDERS} == {"a session"}


def test_a_fifo_where_a_dead_sessions_transcript_belongs_is_no_transcript(world):
    """C-23.28: revival took whatever stood at a dead session's transcript path
    for another writer's transcript, so a FIFO there made the session openable:
    it was spread to every folder, then its title read blocked in open(). A
    FIFO is no transcript: revival leaves it, and the session stays dead and
    unspread, as the listing of transcripts already treats it."""
    archive = world.tmp / "archive"
    archive.mkdir()
    (archive / f"{DEAD}.jsonl").write_text(transcript_lines())
    fifo = world.project / f"{DEAD}.jsonl"
    os.mkfifo(fifo)
    fx.index_entry(world.store, "acct-a", "org-a", DEAD, settings={"ultracode": True})
    options = mirror.Options(archive=str(archive / "*.jsonl"))
    result, finished = finishes(lambda: world.running.run_once(options), fifo)
    assert finished, "a pass blocked in open() on a FIFO where a transcript belongs"
    assert result.state == "ok" and result.revived == 0 and result.added == 0, result
    assert not record(world.store, "acct-b", "org-b", DEAD).exists()
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)


@pytest.mark.parametrize("kind", ["fifo", "device"])
def test_revival_copies_only_a_regular_archived_transcript(world, kind):
    """C-23.28: an archived transcript is read only as a regular file. shutil
    refused a FIFO only by a stat before its own open(), and copied a device:
    an archive entry linked to /dev/null revived an empty transcript."""
    archive = world.tmp / "archive"
    archive.mkdir()
    entry = make(kind, archive / f"{DEAD}.jsonl")
    fx.index_entry(world.store, "acct-a", "org-a", DEAD, settings={"ultracode": True})
    options = mirror.Options(archive=str(archive / "*.jsonl"))
    result, finished = finishes(lambda: world.running.run_once(options), entry)
    assert finished, f"revival blocked in open() on a {kind}"
    assert result.state == "ok" and result.revived == 0, result
    assert not (world.project / f"{DEAD}.jsonl").exists()
    assert not list(world.project.glob("*.tmp-revive"))


@pytest.mark.parametrize("kind", ["fifo", "device"])
def test_a_copy_reads_its_source_only_as_a_regular_file(tmp_path, kind):
    """C-23.28: a copy into the store refuses a source that is not a regular
    file, at once, and places nothing."""
    source = make(kind, tmp_path / "local_source.json")
    destination = tmp_path / "store" / "local_x.json"
    destination.parent.mkdir()
    result, finished = finishes(lambda: mirror._copy_entry(source, destination), source)
    assert finished, f"a copy blocked in open() on a {kind}"
    assert isinstance(result, OSError), result
    assert not destination.exists() and not list(destination.parent.iterdir())


def test_a_copy_keeps_the_sources_bytes_and_times_and_is_owner_only(tmp_path):
    """C-23.28: as `shutil.copy2` did, so a copy keeps its sidebar order."""
    source = tmp_path / "local_source.json"
    source.write_text(json.dumps({"cliSessionId": SESSION}))
    os.chmod(source, 0o644)
    os.utime(source, ns=(1_700_000_000_123_456_789, 1_700_000_100_987_654_321))
    destination = tmp_path / "local_x.json"
    inode = mirror._copy_entry(source, destination)
    info = os.stat(destination)
    assert inode == info.st_ino
    assert destination.read_bytes() == source.read_bytes()
    assert info.st_mtime_ns == 1_700_000_100_987_654_321
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp-subfleet"))


# --- the app's log --------------------------------------------------------------

def test_a_fifo_for_the_apps_log_is_reported_not_waited_on(tmp_path):
    """C-23.28: the log is a diagnostic; a FIFO in its place reads as unreadable."""
    log = make("fifo", tmp_path / "main.log")
    state, finished = finishes(
        lambda: desktop.DesktopLog(log, store=tmp_path, tz=timezone.utc).poll(), log)
    assert finished, "the log poll blocked in open() on a FIFO"
    assert state.load is None and state.error == f"cannot read {log}: not a regular file"


def test_a_fifo_for_the_rotated_log_does_not_hide_the_live_one(tmp_path):
    """C-23.28: the first poll reads the rotated file, then the live one."""
    store = tmp_path / "store"
    rotated = make("fifo", tmp_path / desktop.ROTATED_NAME)
    log = tmp_path / "main.log"
    log.write_text(f"2026-09-24 10:00:00 [info] Loaded 3 persisted sessions from "
                   f"{store / 'a' / 'o'} (0 archived deferred)\n")
    state, finished = finishes(
        lambda: desktop.DesktopLog(log, store=store, tz=timezone.utc).poll(), rotated)
    assert finished, "the log poll blocked in open() on a FIFO for the rotated log"
    assert state.error is None and state.load.folder == "a/o"


def test_a_fifo_for_the_apps_log_does_not_block_a_pass(world):
    """C-23.28: every pass measures the load gap from the log as it ends."""
    world.log.unlink()
    os.mkfifo(world.log)
    result, finished = finishes(world.running.run_once, world.log)
    assert finished, "a pass blocked in open() on a FIFO for the app's log"
    assert result.state == "ok", result
    gap = world.running.sidecar()["load_gap"]
    assert gap["status"] == "unknown" and "not a regular file" in gap["detail"]


# --- the saved options and the daemon's worker -------------------------------------

def test_a_fifo_for_the_saved_options_does_not_block_the_timer(world):
    """C-23.28: the daemon's timer reads v1's `cc-mirror.json` before each pass."""
    fifo = make("fifo", world.home / mirror.CONFIG_NAME)
    options, finished = finishes(lambda: mirror.options_from(fx.policy()), fifo)
    assert finished, "reading the saved options blocked in open() on a FIFO"
    assert options.archive == "" and options.dead_home == "" and options.exclude == ()


def test_timers_stop_is_not_held_by_a_mirror_pass_and_a_fifo(world):
    """C-23.28: `Daemon.close()` calls `Timers.stop()`, which waits for the
    mirror's worker. A pass blocked in open() on a FIFO in the store held it."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    fifo = make("fifo", world.store / "acct-b" / "org-b" / "local_odd.json")
    store = Store(world.tmp / "state.sqlite3")
    timers = Timers(store, world.root, fx.policy())
    try:
        pending = timers._mirror.submit(timers._run, "mirror")   # noqa: SLF001 - as tick() does
        wait([pending], timeout=WAIT_S)
        _result, finished = finishes(timers.stop, fifo)
        assert finished, "Timers.stop() waited on a mirror pass blocked in open()"
        status = timers.status()["mirror"]
        assert status["last_run"] and status["last_error_type"] is None, status
        assert mirror.Mirror(world.root).sidecar()["pass"]["state"] == "ok"
    finally:
        finishes(timers.stop, fifo)
        store.close()


# --- every read site, every kind of file ----------------------------------------------

def _record_site(world, kind):
    return make(kind, world.store / "acct-b" / "org-b" / "local_odd.json"), {}


def _transcript_site(world, kind):
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "kept.jsonl"
    target.write_text(transcript_lines())
    link = world.project / f"{SESSION}.jsonl"
    link.unlink()
    link.symlink_to(target)
    assert world.running.run_once().state == "ok"
    target.unlink()
    return make(kind, target), {}


def _log_site(world, kind):
    world.log.unlink()
    return make(kind, world.log), {}


def _rotated_log_site(world, kind):
    return make(kind, world.log.with_name(desktop.ROTATED_NAME)), {}


def _options_site(world, kind):
    return make(kind, world.home / mirror.CONFIG_NAME), {}


def _archive_site(world, kind):
    archive = world.tmp / "archive"
    archive.mkdir()
    fx.index_entry(world.store, "acct-a", "org-a", DEAD, settings={"ultracode": True})
    return make(kind, archive / f"{DEAD}.jsonl"), {"archive": str(archive / "*.jsonl")}


def _dead_transcript_site(world, kind):
    archive = world.tmp / "archive"
    archive.mkdir()
    (archive / f"{DEAD}.jsonl").write_text(transcript_lines())
    fx.index_entry(world.store, "acct-a", "org-a", DEAD, settings={"ultracode": True})
    return make(kind, world.project / f"{DEAD}.jsonl"), {"archive": str(archive / "*.jsonl")}


SITES = {"record": _record_site, "transcript": _transcript_site, "log": _log_site,
         "rotated-log": _rotated_log_site, "options": _options_site,
         "archive": _archive_site, "dead-transcript": _dead_transcript_site}


@pytest.mark.parametrize("kind", ["fifo", "directory", "device"])
@pytest.mark.parametrize("site", sorted(SITES))
def test_no_read_site_blocks_or_holds_on_a_non_regular_file(world, site, kind):
    """C-23.28, for every place the mirror reads outside its state root and every
    kind of non-regular file: the timer's work (its saved options, a full pass,
    then a hot pass) finishes, holds no flag sync, and leaves the file as it
    found it. The tests above say what each site then reports."""
    path, overrides = SITES[site](world, kind)
    before = node(path)

    def cycle():
        options = mirror.options_from(fx.policy(), **overrides)
        return world.running.run_once(options), world.running.run_hot(options)

    passes, finished = finishes(cycle, path)
    assert finished, f"the mirror blocked in open() on a {kind} at the {site} site"
    assert not isinstance(passes, BaseException), passes
    full, hot = passes
    assert full.state == "ok" and full.flags_held == 0, full
    assert hot.state == "ok", hot
    assert node(path) == before
