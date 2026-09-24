"""C-25.6 person-only operations; C-28.1 attachments by content."""

from __future__ import annotations

import os

import pytest

from subfleet.conversations import attachments
from subfleet.conversations.peers import Proc, judge
from subfleet.conversations.store import ConversationError, ConversationStore

APP = "/Applications/Subfleet.app/Contents/MacOS/Subfleet"


def chain_of(*procs):
    return lambda pid: list(procs)


def test_the_app_is_a_person():
    """C-25.6 the installed app's executable may approve."""
    v = judge(10, chain=chain_of(Proc(10, 1, "??", f"{APP} HOME=/Users/m")))
    assert v.person and v.reason == "the Subfleet app"


def test_a_terminal_is_a_person_and_a_headless_agent_is_not():
    """C-25.6 a process with a controlling terminal may; one without may not."""
    assert judge(10, chain=chain_of(Proc(10, 9, "ttys003", "/bin/zsh"), Proc(9, 1, "ttys003", "login"))).person
    assert not judge(10, chain=chain_of(Proc(10, 9, "??", "/usr/bin/python3 x.py"), Proc(9, 1, "??", "node"))).person


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
