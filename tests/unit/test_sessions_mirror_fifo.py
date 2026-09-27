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
fails the test instead of hanging it. Each FIFO is then opened for reading and
writing, without blocking, until the thread ends, which releases a reader or a
writer stuck in open(). The store, the transcripts and the log all live under
`tmp_path`.

The mirror's own writes (a copy into the store, a revived transcript, a flag
write) make each temporary as a new file under a name of their own, and write,
stamp and fsync it through that one descriptor: a FIFO at the fixed name the
mirror had used blocked its open() for writing, a link there sent the write
through to what it named, and a temporary replaced after the copy blocked the
reopen for fsync, or was put in place as it stood (reviews of 8172685).
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
    """Open the FIFO for reading and writing without blocking, then close it. A
    reader waiting in open() returns and reads end of file; a writer waiting in
    open() returns, and its write finds no reader (EPIPE)."""
    try:
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
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


# --- the mirror's own writes (reviews of 8172685) ------------------------------------

def _dead_session_with_an_archive(world) -> tuple[Path, "mirror.Options"]:
    archive = world.tmp / "archive"
    archive.mkdir()
    entry = archive / f"{DEAD}.jsonl"
    entry.write_text(transcript_lines())
    fx.index_entry(world.store, "acct-a", "org-a", DEAD, settings={"ultracode": True})
    return entry, mirror.Options(archive=str(archive / "*.jsonl"))


def test_revival_never_opens_a_fifo_at_its_old_temporary_name(world):
    """C-23.28: revival copied into `<id>.jsonl.tmp-revive`, a fixed name, with a
    plain open() for writing. A FIFO left there blocked the pass, the mirror's
    worker and so `Timers.stop()`; once released, the FIFO was put in the
    transcript's place and the session spread. The copy is now a new file under
    a name of its own: the FIFO is never opened, and stays."""
    entry, options = _dead_session_with_an_archive(world)
    fifo = make("fifo", world.project / f"{DEAD}.jsonl.tmp-revive")
    result, finished = finishes(lambda: world.running.run_once(options), fifo)
    assert finished, "revival blocked in open() on a FIFO at its temporary name"
    assert result.state == "ok" and result.revived == 1, result
    transcript = world.project / f"{DEAD}.jsonl"
    assert stat.S_ISREG(os.lstat(transcript).st_mode) and transcript.read_bytes() == entry.read_bytes()
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert record(world.store, "acct-b", "org-b", DEAD).exists()


def test_revival_never_truncates_the_archive_through_a_link_at_its_temporary_name(world):
    """C-23.28: with the fixed name hard-linked to the archived transcript, the
    plain open() for writing truncated the archive before copying it into
    itself, and revived an empty transcript; `shutil.copyfile` had refused
    (SameFileError). The archive is never written, and the transcript revived
    is whole."""
    entry, options = _dead_session_with_an_archive(world)
    before = entry.read_bytes()
    os.link(entry, world.project / f"{DEAD}.jsonl.tmp-revive")
    result = world.running.run_once(options)
    assert result.state == "ok" and result.revived == 1, result
    assert entry.read_bytes() == before, "the archive was truncated"
    assert (world.project / f"{DEAD}.jsonl").read_bytes() == before


def test_revival_never_replaces_a_transcript_written_after_its_check(world, monkeypatch):
    """C-23.28 ("revival never replaces what it finds"): revival checked that no
    transcript stood at the path, copied, then `os.replace`d, so a transcript
    another writer made meanwhile was overwritten with the archived one. It is
    now put in place create-only: the other writer's transcript stays, and the
    session is openable by it, not revived."""
    _entry, options = _dead_session_with_an_archive(world)
    transcript = world.project / f"{DEAD}.jsonl"
    theirs = transcript_lines() + json.dumps({"type": "user", "uuid": "late"}) + "\n"
    copy = mirror._copy_regular

    def another_writer_meanwhile(source, out):
        result = copy(source, out)
        if not transcript.exists():
            transcript.write_text(theirs)
        return result

    monkeypatch.setattr(mirror, "_copy_regular", another_writer_meanwhile)
    result = world.running.run_once(options)
    assert result.state == "ok" and result.revived == 0, result
    assert transcript.read_text() == theirs
    assert not list(world.project.glob("*.tmp-revive"))


def test_a_copy_never_reopens_its_temporary_nor_places_what_replaced_it(tmp_path, monkeypatch):
    """C-23.28: `_copy_entry` closed its temporary and opened it again by name to
    fsync it. A FIFO put at that name meanwhile blocked the reopen; released, it
    was linked into the store as the record. The copy is now written, stamped and
    fsynced through its one descriptor, and a name that no longer holds that file
    is never put in place."""
    source = tmp_path / "local_source.json"
    source.write_text(json.dumps({"cliSessionId": SESSION}))
    store = tmp_path / "store"
    store.mkdir()
    destination = store / "local_x.json"
    copy = mirror._copy_regular
    swapped: list[Path] = []

    def swap_the_temporary(source, out):
        result = copy(source, out)
        for temporary in store.glob("*.tmp-subfleet"):
            temporary.unlink()
            os.mkfifo(temporary)
            swapped.append(temporary)
        return result

    monkeypatch.setattr(mirror, "_copy_regular", swap_the_temporary)
    old_name = store / "local_x.json.tmp-subfleet"         # where one that reopens would block
    result, finished = finishes(lambda: mirror._copy_entry(source, destination), old_name)
    assert swapped, "the copy made no temporary beside the destination"
    assert finished, "the copy blocked reopening its temporary"
    assert isinstance(result, OSError), result
    assert not destination.exists() and not list(store.iterdir())


@pytest.mark.parametrize("write", ["copy", "flag write"])
def test_a_write_leaves_a_file_at_its_old_temporary_name_alone(tmp_path, write):
    """C-23.28: a write made its temporary at the fixed name `<record>.tmp-subfleet`,
    unlinking whatever stood there first, which could be another mirror's
    temporary in flight (a second state root mirrors the same store). Each write
    now makes a name of its own, and never removes one it did not make."""
    destination = tmp_path / "local_x.json"
    theirs = tmp_path / "local_x.json.tmp-subfleet"
    theirs.write_text("another writer's")
    source = tmp_path / "local_source.json"
    source.write_text(json.dumps({"cliSessionId": SESSION}))
    if write == "copy":
        mirror._copy_entry(source, destination)
    else:
        mirror._write_json(destination, {"cliSessionId": SESSION})
    assert json.loads(destination.read_text()) == {"cliSessionId": SESSION}
    assert theirs.read_text() == "another writer's"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["local_source.json", "local_x.json",
                                                          "local_x.json.tmp-subfleet"]


def test_a_sweep_removes_the_temporaries_a_killed_pass_left(world, monkeypatch):
    """C-23.28: a temporary is made under a name of its own, so one a daemon
    killed mid-write leaves is never reused: a sweep removes it once it is
    stale. One still being written is not stale."""
    folder = world.store / "acct-b" / "org-b"
    left = folder / f"local_{SESSION}.json.k1ll3d00.tmp-subfleet"
    left.write_text("{")
    revive_left = world.project / f"{DEAD}.jsonl.k1ll3d00.tmp-revive"
    revive_left.write_text("{")
    assert world.running.run_once().swept
    assert left.exists() and revive_left.exists(), "a fresh temporary was removed"
    monkeypatch.setattr(mirror, "TEMPORARY_STALE_S", -1, raising=False)
    monkeypatch.setattr(mirror, "SWEEP_INTERVAL_S", 0)
    assert world.running.run_once().swept
    assert not left.exists() and not revive_left.exists()


def test_revival_on_a_volume_without_hard_links_still_places_the_transcript(world, monkeypatch):
    """C-23.28: revival puts its copy in place create-only, by a hard link; on a
    filesystem that refuses one (ENOTSUP, EPERM), where `os.replace` had worked, it
    renames after a fresh look instead of silently reviving nothing."""
    import errno as errno_module
    entry, options = _dead_session_with_an_archive(world)

    def no_links(*args, **kwargs):
        raise OSError(errno_module.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(mirror.os, "link", no_links)
    result = world.running.run_once(options)
    assert result.state == "ok" and result.revived == 1, result
    assert (world.project / f"{DEAD}.jsonl").read_bytes() == entry.read_bytes()
    assert not list(world.project.glob("*.tmp-revive"))


def test_a_revived_transcript_is_synced_before_it_is_put_in_place(world, monkeypatch):
    """C-23.28: a revival writes, stamps and fsyncs its copy through its one
    descriptor before the link puts it in place."""
    _entry, options = _dead_session_with_an_archive(world)
    synced, placed = set(), []
    real_fsync, real_link = mirror.os.fsync, mirror.os.link

    def fsync(fd):
        synced.add(os.fstat(fd).st_ino)
        return real_fsync(fd)

    def link(source, destination, **kwargs):
        if str(destination).endswith(f"{DEAD}.jsonl"):   # the revived transcript, not a spread record
            placed.append(os.stat(source).st_ino in synced)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(mirror.os, "fsync", fsync)
    monkeypatch.setattr(mirror.os, "link", link)
    assert world.running.run_once(options).revived == 1
    assert placed == [True], "the revived transcript was not synced before it was put in place"


def test_a_sweep_removes_the_leftovers_of_the_mirrors_own_state_files(world, monkeypatch):
    """C-23.28: the mirror's own state files (its sidecar, ledger, flags) are written
    through temporaries of their own too; one a killed write left is swept."""
    running = world.running
    assert running.run_once().state == "ok"
    left = running.dir / f"{mirror.SIDECAR_NAME}.k1ll3d00{mirror.TEMPORARY_SUFFIX}"
    left.write_text("{")
    monkeypatch.setattr(mirror, "TEMPORARY_STALE_S", -1)
    monkeypatch.setattr(mirror, "SWEEP_INTERVAL_S", 0)
    assert running.run_once().swept
    assert not left.exists()


def test_a_linked_transcript_whose_target_became_a_fifo_is_not_spread(world):
    """C-23.28 ("one where a transcript belongs is no transcript"): a project
    directory's listing is kept until its mtime moves, and replacing a linked
    transcript's target elsewhere does not move it. A session whose link had come
    to name a FIFO stayed openable between sweeps, and was spread to a folder
    that appeared meanwhile. A kept link is checked again on every pass."""
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
    (world.store / "acct-d" / "org-d").mkdir(parents=True)
    result, finished = finishes(world.running.run_once, target)
    assert finished, "a pass blocked on the FIFO a linked transcript names"
    assert result.state == "ok" and not result.swept, result
    assert result.added == 0 and not record(world.store, "acct-d", "org-d").exists(), result


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
