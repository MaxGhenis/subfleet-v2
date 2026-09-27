"""C-25.6 person-only operations; C-28.1 attachments by content."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from subfleet.conversations import attachments
from subfleet.conversations.peers import Proc, executable_path, judge
from subfleet.conversations.store import ConversationError, ConversationStore

APP = "/Applications/Subfleet.app/Contents/MacOS/Subfleet"


def chain_of(*procs):
    return lambda pid: list(procs)


def test_the_app_is_a_person():
    """C-25.6 the installed app's executable may approve."""
    v = judge(10, chain=chain_of(Proc(10, 1, "??", f"{APP} HOME=/Users/m")), executable=lambda pid: APP)
    assert v.person and v.reason == "the Subfleet app"


def test_the_app_is_known_by_its_executable_path_even_with_spaces():
    """C-25.6, D-21: the kernel's executable path decides, not the command line, which
    cannot be split back into a path with spaces (reported from the app build)."""
    dev = "/Users/m/build/Subfleet Dev.app/Contents/MacOS/Subfleet Dev"
    command = f"{dev} HOME=/Users/m"
    assert judge(10, chain=chain_of(Proc(10, 1, "??", command)), app_executables=(dev,),
                 executable=lambda pid: dev).person
    # Another program whose command line merely starts like the app is not the app.
    assert not judge(10, chain=chain_of(Proc(10, 1, "??", f"{APP} --flag")),
                     executable=lambda pid: "/usr/bin/python3").person


def test_executable_path_reads_the_running_process():
    """The helper reads a live process's executable (libproc `proc_pidpath`)."""
    import sys
    assert executable_path(os.getpid()) == os.path.realpath(sys.executable)
    assert executable_path(2 ** 22 + 12345) is None


def test_a_terminal_is_a_person_and_a_headless_agent_is_not():
    """C-25.6 a process with a controlling terminal may; one without may not."""
    assert judge(10, chain=chain_of(Proc(10, 9, "ttys003", "/bin/zsh"), Proc(9, 1, "ttys003", "login")),
                 executable=lambda pid: "/bin/zsh").person
    assert not judge(10, chain=chain_of(Proc(10, 9, "??", "/usr/bin/python3 x.py"), Proc(9, 1, "??", "node")),
                     executable=lambda pid: "/usr/bin/python3").person


@pytest.mark.parametrize("ancestor", [
    Proc(9, 8, "??", "/v/python -m subfleet.guardian --attempt-dir /x -- claude -p"),
    Proc(9, 8, "ttys001", "bash SUBFLEET_ATTEMPT=j/a1 SUBFLEET_JOB=j PATH=/bin"),
])
def test_anything_subfleet_launched_is_refused_even_with_a_terminal(ancestor):
    """C-25.6 a guardian's descendant, or a process carrying the attempt markers, may not
    act as the person, whatever its terminal."""
    v = judge(10, chain=chain_of(Proc(10, 9, "ttys001", "/bin/zsh"), ancestor, Proc(8, 1, "ttys001", "login")))
    assert not v.person


def test_an_unidentified_caller_is_refused():
    """C-25.6 no pid, or no process table, decides nothing in the caller's favour."""
    assert not judge(None).person
    assert not judge(10, chain=lambda pid: []).person


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "state")
    yield s
    s.close()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_an_image_is_copied_by_content_and_private(store, tmp_path):
    """C-28.1 the daemon's copy is named by its hash, 0600, and recorded."""
    src = tmp_path / "paste.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    assert out["media_type"] == "image/png" and copy.read_bytes() == PNG
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"
    src.unlink()
    assert attachments.check(store, out["sha256"])[0] == str(copy)


def test_symlinks_fifos_and_non_images_are_refused(store, tmp_path):
    """C-28.1 no symlink is followed, only regular files, only the four image types."""
    real = tmp_path / "real.png"
    real.write_bytes(PNG)
    link = tmp_path / "link.png"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    text = tmp_path / "notes.png"
    text.write_text("not an image")
    for path, reason in ((link, "unreadable"), (fifo, "not-a-file"), (text, "not-an-image")):
        with pytest.raises(ConversationError) as err:
            attachments.add(store, str(path))
        assert err.value.reason == reason
    with pytest.raises(ConversationError) as err:
        attachments.add(store, "relative.png")
    assert err.value.reason == "bad-path"


def test_a_hash_the_app_sent_must_match(store, tmp_path):
    """C-28.1 an expected hash that differs is refused."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    with pytest.raises(ConversationError) as err:
        attachments.add(store, str(src), expected_sha256="0" * 64)
    assert err.value.reason == "hash-mismatch"


def test_a_changed_copy_is_caught_before_a_frame_is_built(store, tmp_path):
    """C-28.1 the driver's check fails a message whose stored copy changed."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    (store.root / "attachments" / f"{out['sha256']}.png").write_bytes(PNG + b"x")
    with pytest.raises(ConversationError) as err:
        attachments.check(store, out["sha256"])
    assert err.value.reason == "attachment-missing"


def copies(store) -> list[str]:
    """What `attachments/` holds, each file checked to be a whole copy named by its hash
    (a temporary file, named `.<sha>.<token>.tmp`, fails the check)."""
    names = sorted(p.name for p in (store.root / "attachments").iterdir())
    for name in names:
        digest = hashlib.sha256((store.root / "attachments" / name).read_bytes()).hexdigest()
        assert name.split(".")[0] == digest, f"{name} is not a whole copy named by its hash"
    return names


def full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize("step,fake", [("write", full), ("fsync", full), ("rename", full),
                                       ("write", lambda fd, buf: 0)],
                         ids=["write", "fsync", "rename", "write-nothing"])
def test_a_copy_that_fails_leaves_no_temporary_file(store, tmp_path, monkeypatch, step, fake):
    """C-28.1 a copy that fails part way (a full disk, or a write that makes no progress
    and would otherwise loop forever) removes its temporary file, whose name is its own,
    so failed adds leave nothing behind, and the next add makes the copy."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    real = getattr(os, step)
    monkeypatch.setattr(os, step, fake)
    with pytest.raises(OSError):
        attachments.add(store, str(src))
    monkeypatch.setattr(os, step, real)
    assert copies(store) == []
    out = attachments.add(store, str(src))
    assert copies(store) == [f"{out['sha256']}.png"]


def test_a_temporary_name_another_add_holds_is_drawn_again(store, tmp_path, monkeypatch):
    """C-28.1 the temporary name is random and created exclusively; one that is already
    taken (another add drew the same token) is drawn again, never truncated or unlinked,
    even by an add that finds every name it draws taken and fails."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    taken = store.subdirectory("attachments") / f".{digest}.aaaaaaaa.tmp"
    taken.write_bytes(b"another add's copy, half written")
    drawn = []

    def always_taken(n):
        drawn.append(n)
        return "aaaaaaaa"

    monkeypatch.setattr(attachments, "secrets", SimpleNamespace(token_hex=always_taken))   # this module only
    with pytest.raises(FileExistsError):
        attachments.add(store, str(src))
    assert len(drawn) == 8                  # up to 8 draws, then the add fails
    assert sorted(p.name for p in taken.parent.iterdir()) == [taken.name]
    assert taken.read_bytes() == b"another add's copy, half written"
    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(attachments, "secrets", SimpleNamespace(token_hex=lambda n: next(draws)))
    out = attachments.add(store, str(src))
    assert out["sha256"] == digest and next(draws, None) is None
    assert taken.read_bytes() == b"another add's copy, half written"
    assert sorted(p.name for p in taken.parent.iterdir()) == [taken.name, f"{digest}.png"]
    assert (taken.parent / f"{digest}.png").read_bytes() == PNG


def test_a_short_write_still_makes_a_whole_copy(store, tmp_path, monkeypatch):
    """C-28.1 `os.write` may write less than it was given; the copy goes on until it is
    whole rather than renaming a truncated file into place."""
    data = PNG + bytes(range(256)) * 64
    src = tmp_path / "a.png"
    src.write_bytes(data)
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, buf: real(fd, buf[:1000]))
    out = attachments.add(store, str(src))
    assert (store.root / "attachments" / f"{out['sha256']}.png").read_bytes() == data


@pytest.mark.parametrize("n", [3, 8])
def test_adds_at_once_each_get_their_receipt_and_leave_one_copy_per_image(store, tmp_path, monkeypatch, n):
    """C-28.1 any number of adds at once, of the same image and of different ones, each
    return their image's receipt, and what is left is one whole copy per image."""
    images = []
    for k in range(2):
        images.append(tmp_path / f"{k}.png")
        images[k].write_bytes(PNG + bytes([k]) * (1 << 20))
    lined_up = threading.Barrier(n)
    real = attachments.sniff

    def sniff(head):                        # every add has read its image before any writes
        lined_up.wait(30)
        return real(head)

    monkeypatch.setattr(attachments, "sniff", sniff)
    results: list = [None] * n

    def run(i):
        try:
            results[i] = attachments.add(store, str(images[i % 2]))["sha256"]
        except Exception as exc:
            results[i] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    digests = [hashlib.sha256(image.read_bytes()).hexdigest() for image in images]
    assert results == [digests[i % 2] for i in range(n)]
    assert copies(store) == sorted(f"{digest}.png" for digest in digests)


def _symlink_elsewhere(copy, tmp_path):
    elsewhere = tmp_path / "elsewhere.png"
    elsewhere.write_bytes(PNG)
    copy.unlink()
    copy.symlink_to(elsewhere)


def _fifo(copy, tmp_path):
    copy.unlink()
    os.mkfifo(copy)


def _hard_link_elsewhere(copy, tmp_path):
    os.link(copy, tmp_path / "elsewhere.png")            # the same bytes, writable through another name


DAMAGE = {
    "other bytes": lambda copy, tmp_path: copy.write_bytes(b"garbage"),
    "emptied": lambda copy, tmp_path: copy.write_bytes(b""),
    "gone": lambda copy, tmp_path: copy.unlink(),
    "unreadable": lambda copy, tmp_path: copy.chmod(0),
    "a symlink to the same bytes": _symlink_elsewhere,
    "a fifo": _fifo,
    "readable by others": lambda copy, tmp_path: copy.chmod(0o644),
    "a hard link another name writes through": _hard_link_elsewhere,
}
# Root reads a file of mode 0000, so for root "unreadable" is the daemon's own copy.
DAMAGE_CASES = [pytest.param(name, marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file"))
                if name == "unreadable" else name for name in DAMAGE]


@pytest.mark.parametrize("damage", DAMAGE_CASES)
def test_a_re_add_repairs_a_stored_copy_that_changed(store, tmp_path, damage):
    """C-28.1 adding an image again copies it again when its stored copy is not a regular
    file holding its bytes (disk damage, a stray write), and returns the receipt. The
    add copied only when no file was there, so a changed copy failed every re-add with
    `copy-mismatch`, and every message naming it `attachment-missing`, for good."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    DAMAGE[damage](copy, tmp_path)
    results: list = []

    def re_add():
        try:
            results.append(attachments.add(store, str(src)))
        except Exception as exc:
            results.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=re_add, daemon=True)     # a fifo must not hold the add
    worker.start()
    worker.join(10)
    assert results == [out]
    info = os.lstat(copy)
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert copies(store) == [copy.name]
    assert attachments.check(store, out["sha256"])[0] == str(copy)


@pytest.mark.parametrize("damage", DAMAGE_CASES)
def test_the_drivers_check_refuses_a_changed_copy_at_once(store, tmp_path, damage):
    """C-28.1 (review of 1808f61): the driver's check judges the stored copy as an add
    does. It read it with `path.read_bytes()`, which followed a symlink and blocked on a
    FIFO for good, holding the conversation tick that builds turns and every tick after
    it; and it passed a copy others could read or write through another name. It now
    refuses each at once (`attachment-missing`), and adding the image again repairs it."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    DAMAGE[damage](copy, tmp_path)
    results: list = []

    def check():
        try:
            results.append(attachments.check(store, out["sha256"]))
        except ConversationError as exc:
            results.append(exc.reason)

    worker = threading.Thread(target=check, daemon=True)      # a fifo must not hold the check
    worker.start()
    worker.join(10)
    assert results == ["attachment-missing"]
    assert attachments.add(store, str(src)) == out
    assert attachments.check(store, out["sha256"]) == (str(copy), "image/png")


def test_a_copy_another_add_replaces_while_it_is_judged_still_holds(store, tmp_path, monkeypatch):
    """C-28.1 an add renaming its identical copy over the one being judged leaves the
    open file with no name at all: no link but its own, or none, holds; only a second
    name that could write through it does not. (Requiring exactly one link failed adds
    of the same image at once with `copy-mismatch`, about one run in eight.)"""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    real_fstat, replaced = os.fstat, []

    def fstat(fd):
        if not replaced:                    # between the open and the look: another add's rename
            other = copy.with_name(".another-add.tmp")
            other.write_bytes(PNG)
            other.chmod(0o600)
            os.rename(other, copy)
            replaced.append(True)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", fstat)
    assert attachments.check(store, out["sha256"]) == (str(copy), "image/png")
    monkeypatch.setattr(os, "fstat", real_fstat)
    assert replaced == [True] and copies(store) == [copy.name]


def test_a_stored_file_of_the_wrong_size_is_judged_without_reading_it(store, tmp_path, monkeypatch):
    """C-28.1 (review of 1808f61): whatever sits at the copy's name is judged by its size
    before any byte of it is read, so a stray large file costs no read on the file pool
    that `close()` waits for, and no more than the copy's size is ever read."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    copy.write_bytes(os.urandom(3 << 20))
    real_read, real_sniff = os.read, attachments.sniff
    oversized: list[int] = []

    def read(fd, n):
        if os.fstat(fd).st_size != len(PNG):
            oversized.append(os.fstat(fd).st_size)
        return real_read(fd, n)

    def sniff(head):                        # the add has read its source; now watch every read
        monkeypatch.setattr(os, "read", read)
        return real_sniff(head)

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ConversationError):
        attachments.check(store, out["sha256"])
    monkeypatch.setattr(os, "read", real_read)
    monkeypatch.setattr(attachments, "sniff", sniff)
    assert attachments.add(store, str(src)) == out
    assert oversized == [] and copy.read_bytes() == PNG


def test_a_stored_file_that_grows_while_it_is_read_is_read_one_byte_past_its_size(store, tmp_path, monkeypatch):
    """C-28.1 the read is capped: a file of the right size when it was looked at that
    grows while it is read costs at most one byte past its size, and does not hold."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    real_fstat, real_read, read = os.fstat, os.read, []

    def fstat(fd):
        info = real_fstat(fd)
        with open(copy, "ab") as grow:      # after the look, before the read
            grow.write(b"\0" * (8 << 20))
        return info

    def counted(fd, n):
        chunk = real_read(fd, n)
        read.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "read", counted)
    with pytest.raises(ConversationError):
        attachments.check(store, out["sha256"])
    assert sum(read) == len(PNG) + 1


def test_a_re_add_after_the_state_root_moved_names_the_copy_where_it_is(tmp_path):
    """C-28.1 (review of 1808f61): the row keeps the path of the copy it names. A re-add
    kept the old row's path, so after the state root moved every message naming the
    image failed `attachment-missing` though the add had made the copy again."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    old = ConversationStore(tmp_path / "state-old")
    out = attachments.add(old, str(src))
    old.close()
    (tmp_path / "state-old").rename(tmp_path / "state")
    moved = ConversationStore(tmp_path / "state")
    try:
        with pytest.raises(ConversationError):
            attachments.check(moved, out["sha256"])
        assert attachments.add(moved, str(src)) == out
        copy = tmp_path / "state" / "attachments" / f"{out['sha256']}.png"
        assert attachments.check(moved, out["sha256"]) == (str(copy), "image/png")
    finally:
        moved.close()


def test_a_copy_gone_when_it_is_checked_is_a_mismatch_to_retry(store, tmp_path, monkeypatch):
    """C-28.1 a copy that is gone when the add checks it after copying is `copy-mismatch`
    (try again), not an unhandled FileNotFoundError, and names no row; the next add
    copies it again."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    real = attachments._copy

    def copy_then_lose(data, target):
        real(data, target)
        target.unlink()

    monkeypatch.setattr(attachments, "_copy", copy_then_lose)
    with pytest.raises(ConversationError) as err:
        attachments.add(store, str(src))
    assert err.value.reason == "copy-mismatch"
    assert store.attachment(digest) is None
    monkeypatch.setattr(attachments, "_copy", real)
    assert attachments.add(store, str(src))["sha256"] == digest


@pytest.mark.parametrize("in_place", [False, True], ids=["copied", "found in place"])
def test_the_copys_name_is_synced_after_the_rename_and_before_its_row(store, tmp_path, monkeypatch, in_place):
    """C-28.1, C-8.1: `attachments/` is fsynced after the copy is renamed into it and
    before the row names it, as `store._publish` does for message text. An add that
    finds the copy already in place syncs it too, since the add that renamed it there
    may not have synced yet."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    directory = store.subdirectory("attachments")
    if in_place:
        (directory / f"{digest}.png").write_bytes(PNG)   # renamed in by another add, not yet synced
        (directory / f"{digest}.png").chmod(0o600)        # as an add makes it: others cannot read it
    here = os.stat(directory)
    events: list[str] = []
    real_fsync, real_rename, real_row = os.fsync, os.rename, store.add_attachment

    def fsync(fd):
        info = os.fstat(fd)
        synced = (info.st_dev, info.st_ino) == (here.st_dev, here.st_ino)
        events.append("fsync attachments/" if synced else "fsync file")
        return real_fsync(fd)

    def rename(old, new):
        events.append(f"rename onto {os.path.basename(new)}")
        return real_rename(old, new)

    def add_attachment(*args, **kwargs):
        events.append("row")
        return real_row(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(store, "add_attachment", add_attachment)
    attachments.add(store, str(src))
    copied = ["fsync file", f"rename onto {digest}.png"]
    assert events == [*([] if in_place else copied), "fsync attachments/", "row"]


def test_no_row_names_a_copy_whose_directory_could_not_be_synced(store, tmp_path, monkeypatch):
    """C-28.1 an add whose directory sync fails writes no row; the whole copy stays, and
    the next add syncs it and writes the row."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    real = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError):
        attachments.add(store, str(src))
    assert store.attachment(digest) is None
    monkeypatch.setattr(os, "fsync", real)
    assert attachments.add(store, str(src))["sha256"] == digest
    assert store.attachment(digest) is not None and copies(store) == [f"{digest}.png"]


def test_a_stored_copy_whose_read_fails_is_refused_and_repaired(store, tmp_path, monkeypatch):
    """C-28.1 (review of 1808f61): a stored copy whose read fails (EIO: disk damage) is
    not the daemon's own copy. The driver's check refuses it (`attachment-missing`), not
    with an unhandled OSError, and a re-add copies it again to a new file."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    copy = store.root / "attachments" / f"{out['sha256']}.png"
    damaged = os.stat(copy)
    real_read = os.read

    def read(fd, n):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (damaged.st_dev, damaged.st_ino):
            raise OSError(errno.EIO, "Input/output error")
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ConversationError) as err:
        attachments.check(store, out["sha256"])
    assert err.value.reason == "attachment-missing"
    assert attachments.add(store, str(src)) == out
    # The new copy was made while the damaged one still had its name, so its inode differs.
    assert os.stat(copy).st_ino != damaged.st_ino
    assert attachments.check(store, out["sha256"]) == (str(copy), "image/png")


@pytest.mark.parametrize("contents", [[], ["kept.txt"]], ids=["empty", "not empty"])
def test_a_directory_at_the_copys_name_is_named_and_left_alone(store, tmp_path, contents):
    """C-28.1 (review of 1808f61): a rename cannot replace a directory, so a directory at
    the copy's name failed every re-add with an unhandled IsADirectoryError ("operation
    failed"). The add now says what is in the way (`copy-blocked`, with the fix), leaves
    the directory and its contents alone and no temporary file behind; once the
    directory is gone, the next add makes the copy."""
    src = tmp_path / "a.png"
    src.write_bytes(PNG)
    out = attachments.add(store, str(src))
    directory = store.root / "attachments"
    copy = directory / f"{out['sha256']}.png"
    copy.unlink()
    copy.mkdir(mode=0o700)
    for name in contents:
        (copy / name).write_text("someone else's")
    with pytest.raises(ConversationError) as err:
        attachments.add(store, str(src))
    assert (err.value.reason, err.value.code) == ("copy-blocked", 1)
    assert "remove that directory" in err.value.fix
    assert sorted(os.listdir(directory)) == [copy.name] and sorted(os.listdir(copy)) == contents
    with pytest.raises(ConversationError) as err:
        attachments.check(store, out["sha256"])
    assert err.value.reason == "attachment-missing"
    for name in contents:
        (copy / name).unlink()
    copy.rmdir()
    assert attachments.add(store, str(src)) == out
    assert copies(store) == [copy.name]


def test_a_development_build_counts_as_the_app_only_on_a_development_state_root(tmp_path, monkeypatch):
    """C-25.6, D-21: `SUBFLEET_DEV_APP_EXECUTABLE` widens nothing on ~/.subfleet."""
    from types import SimpleNamespace
    from subfleet.conversations.peers import APP_EXECUTABLES
    from subfleet.conversations.service import ConversationService
    dev = "/Users/x/build/Subfleet Dev.app/Contents/MacOS/Subfleet"
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", dev)
    service = ConversationService.__new__(ConversationService)
    service.root = tmp_path
    assert service._app_executables() == (*APP_EXECUTABLES, dev)
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    service.root = tmp_path.parent / ".subfleet"
    service.root.mkdir(exist_ok=True)
    assert service._app_executables() == APP_EXECUTABLES
