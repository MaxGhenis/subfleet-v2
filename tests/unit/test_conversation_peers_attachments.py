"""C-25.6 person-only operations; C-28.1 attachments by content."""

from __future__ import annotations

import errno
import hashlib
import os
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
