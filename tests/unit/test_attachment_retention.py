"""C-28.2 attachment retention, and the guard it shares with C-28.1's `attachment.add`.

Invariants (each has a test below; the state machine checks them after every step):

- A receipt always names a stored copy: when `attachment.add` returns, the row for
  its hash exists and `attachments/<sha256>.<ext>` is a regular file whose bytes
  hash to it. More strongly, whenever no guard is held, every row's copy is there
  and hashes right (retention deletes the row before the file; an add writes the
  file before the row; the hash's guard spans both).
- A message's attachments are never deleted while it is not terminal, nor while a
  handoff fences its conversation (a handoff that does not commit puts the
  messages it withdrew back in the queue).
- Retention deletes exactly the attachments last used at least 30 days ago that no
  message needs, and a second pass at the same moment deletes nothing.
- Retention unlinks only names `attachment.add` makes, directly under
  `<state root>/attachments`: never a path read from a row, never through a
  symlinked directory. A copy no row names, or a temporary copy, goes a day after
  it was last written.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from subfleet import retention
from subfleet.conversations import attachments
from subfleet.conversations.store import ConversationError, ConversationStore
from subfleet.conversations.turn import LIVE_STATES, MESSAGE_STATES, QUEUED, TERMINAL_STATES

SETTINGS = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DAY = 86400


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "state")
    yield s
    s.close()


def conversation(store, **kw) -> str:
    base = dict(provider="claude", workspace="/w", workspace_kind="in-place", settings=SETTINGS, origin="new")
    return store.create_conversation(**{**base, **kw})[0]["conversation_id"]


def image(directory: Path, k: int) -> Path:
    path = directory / f"image-{k}.png"
    path.write_bytes(PNG + k.to_bytes(4, "big"))
    return path


def add(store, directory: Path, k: int = 0) -> str:
    return attachments.add(store, str(image(directory, k)))["sha256"]


def copy_of(store, sha: str) -> Path:
    return store.root / "attachments" / f"{sha}.png"


def intact(store, sha: str) -> bool:
    """The row is there, names the copy retention would unlink, and the copy hashes right."""
    row = store.attachment(sha)
    return (row is not None and row["path"] == str(copy_of(store, sha))
            and attachments._holds(copy_of(store, sha), sha, row["bytes"]))


def stamp(moment: datetime) -> str:
    return retention._store_time(moment)


def last_used(store, sha: str, days_ago: float, now: datetime | None = None) -> None:
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET last_used_at=? WHERE sha256=?",
                   (stamp((now or datetime.now(UTC)) - timedelta(days=days_ago)), sha))


def send(store, cid: str, shas: list[str], state: str = QUEUED) -> str:
    """A person message naming `shas`, moved to `state`."""
    last = store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                     "ORDER BY seq DESC LIMIT 1", (cid,))
    message_id = str(uuid.uuid4())
    store.submit_message(conversation_id=cid, message_id=message_id, after_message_id=last["message_id"] if last else None,
                         text="look", attachments=shas, settings=SETTINGS)
    if state != QUEUED:
        assert store.set_state(message_id, state)
    return message_id


def aged(path: Path, seconds_ago: float) -> None:
    when = time.time() - seconds_ago
    os.utime(path, (when, when), follow_symlinks=False)


# --- what retention deletes ------------------------------------------------------------

@pytest.mark.parametrize("days, deleted", [(29.9, False), (30, True), (45, True)])
def test_an_attachment_no_message_needs_goes_30_days_after_its_last_use(store, tmp_path, days, deleted):
    """C-28.2 unused for 30 days and named by no live message: the row is deleted, then the copy."""
    now = datetime.now(UTC)
    sha = add(store, tmp_path)
    last_used(store, sha, days, now)
    result = retention.prune_attachments(store, now=now)
    assert result["deleted"] == ([sha] if deleted else [])
    assert result["bytes"] == (len(PNG) + 4 if deleted else 0)
    assert (store.attachment(sha) is None) == deleted
    assert copy_of(store, sha).exists() != deleted
    assert result["errors"] == [] and "interrupted" not in result


@pytest.mark.parametrize("state", [QUEUED, *LIVE_STATES])
def test_a_message_that_is_not_terminal_keeps_its_attachments(store, tmp_path, state):
    """C-28.2 however long ago it was used, an attachment a message still needs stays;
    once that message is terminal, the next pass deletes it."""
    cid = conversation(store)
    sha = add(store, tmp_path)
    message_id = send(store, cid, [sha], state)
    last_used(store, sha, 400)
    assert retention.prune_attachments(store)["deleted"] == []
    assert intact(store, sha)
    assert store.set_state(message_id, "complete")
    assert retention.prune_attachments(store)["deleted"] == [sha]


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_a_terminal_message_does_not_keep_them(store, tmp_path, state):
    """C-28.2 a settled message's attachments go with everyone else's."""
    sha = add(store, tmp_path)
    send(store, conversation(store), [sha], state)
    last_used(store, sha, 31)
    assert retention.prune_attachments(store)["deleted"] == [sha]


@pytest.mark.parametrize("blocked_by, kept", [("handoff:h-1", True), ("quarantined-turn", False)])
def test_a_handoff_fence_keeps_its_conversations_attachments(store, tmp_path, blocked_by, kept):
    """C-28.2, C-30.3 a message a handoff withdrew is `cancelled`, but a handoff that
    does not commit puts it back in the queue, so while the fence stands its
    attachments stay; another block keeps nothing, and neither does a lifted fence."""
    cid = conversation(store)
    sha = add(store, tmp_path)
    send(store, cid, [sha], "cancelled")
    store.update_conversation(cid, blocked_by=blocked_by)
    last_used(store, sha, 31)
    assert retention.prune_attachments(store)["deleted"] == ([] if kept else [sha])
    store.update_conversation(cid, blocked_by=None)
    assert store.attachment(sha) is None or retention.prune_attachments(store)["deleted"] == [sha]


@pytest.mark.parametrize("meanwhile", ["sent", "added-again", "used", "fenced", "queued-again"])
def test_the_delete_re_checks_inside_its_transaction(store, tmp_path, monkeypatch, meanwhile):
    """C-28.2 what retention read before its transaction decides nothing. A message
    naming the attachment, an add of it or a use of it after that read keeps it by
    its last use; a handoff fencing a conversation whose withdrawn message names it,
    or that message put back in the queue, keeps it though its last use is old."""
    cid = conversation(store)
    sha = add(store, tmp_path)
    withdrawn = send(store, cid, [sha], "cancelled")
    last_used(store, sha, 31)
    real = store.attachments_used_by

    def candidates(cutoff, **kw):
        rows = real(cutoff, **kw)
        if rows:                                 # after retention chose them, before any delete
            if meanwhile == "sent":
                send(store, cid, [sha])
            elif meanwhile == "added-again":
                add(store, tmp_path)
            elif meanwhile == "used":
                store.touch_attachments([sha])
            elif meanwhile == "fenced":
                assert store.fence(cid, "handoff:h-9")
            else:
                assert store.set_state(withdrawn, QUEUED, reason="handoff-rolled-back", expect=("cancelled",))
        return rows

    monkeypatch.setattr(store, "attachments_used_by", candidates)
    assert retention.prune_attachments(store)["deleted"] == []
    assert intact(store, sha)


def test_attachments_a_message_still_needs_do_not_crowd_out_the_rest(store, tmp_path, monkeypatch):
    """C-28.2 retention pages through every candidate: old attachments that live
    messages still need, sorted first, never stop it reaching the ones it can delete."""
    cid = conversation(store)
    shas = [add(store, tmp_path, k) for k in range(10)]
    send(store, cid, shas[:5])
    for n, sha in enumerate(shas):
        last_used(store, sha, 100 - n)          # the needed five are the oldest
    real = store.attachments_used_by
    monkeypatch.setattr(store, "attachments_used_by", lambda cutoff, **kw: real(cutoff, **{**kw, "limit": 3}))
    assert sorted(retention.prune_attachments(store)["deleted"]) == sorted(shas[5:])
    assert all(intact(store, sha) for sha in shas[:5])


def test_a_second_pass_at_the_same_moment_deletes_nothing(store, tmp_path):
    """C-28.2 retention is idempotent."""
    now = datetime.now(UTC)
    for k in range(3):
        last_used(store, add(store, tmp_path, k), 31, now)
    assert len(retention.prune_attachments(store, now=now)["deleted"]) == 3
    assert retention.prune_attachments(store, now=now) == {"deleted": [], "bytes": 0, "strays": [], "errors": []}


def test_a_row_whose_copy_is_already_gone_is_deleted_quietly(store, tmp_path):
    """C-28.2 a copy someone removed leaves nothing to unlink and no error."""
    sha = add(store, tmp_path)
    copy_of(store, sha).unlink()
    last_used(store, sha, 31)
    result = retention.prune_attachments(store)
    assert result["deleted"] == [sha] and result["errors"] == []


def test_an_attachment_that_cannot_be_dated_is_kept(store, tmp_path):
    """C-28.2 a last use that does not parse is not 30 days ago."""
    sha = add(store, tmp_path)
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET last_used_at='2026-13-45T99:00:00.000Z' WHERE sha256=?", (sha,))
    assert retention.prune_attachments(store)["deleted"] == []
    assert intact(store, sha)


# --- what retention unlinks -------------------------------------------------------------

def test_retention_unlinks_its_own_name_never_a_path_read_from_the_row(store, tmp_path):
    """C-28.2 the copy removed is `attachments/<sha256>.<ext>`; a row pointing elsewhere
    does not make retention remove that file."""
    sha = add(store, tmp_path)
    outside = tmp_path / "keep-me.png"
    outside.write_bytes(PNG)
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET path=? WHERE sha256=?", (str(outside), sha))
    last_used(store, sha, 31)
    assert retention.prune_attachments(store)["deleted"] == [sha]
    assert outside.read_bytes() == PNG
    assert not copy_of(store, sha).exists()


def test_a_row_of_an_unknown_type_is_deleted_and_its_file_left_to_the_sweep(store, tmp_path):
    """C-28.2 no name can be made for a media type `attachment.add` never records: the
    row goes, nothing is unlinked by guesswork, and the error says why."""
    sha = add(store, tmp_path)
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET media_type='image/bmp' WHERE sha256=?", (sha,))
    last_used(store, sha, 31)
    result = retention.prune_attachments(store)
    assert result["deleted"] == [sha] and "image/bmp" in result["errors"][0]["error"]
    assert copy_of(store, sha).exists()           # a stray now: the sweep takes it after the grace
    aged(copy_of(store, sha), 2 * DAY)
    assert retention.prune_attachments(store)["strays"] == [f"{sha}.png"]


def test_a_symlinked_attachments_directory_removes_nothing(store, tmp_path):
    """C-28.2 retention does not follow `attachments` somewhere else."""
    sha = add(store, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    shutil.move(store.root / "attachments", elsewhere)
    (store.root / "attachments").symlink_to(elsewhere)
    last_used(store, sha, 31)
    aged(elsewhere / f"{sha}.png", 2 * DAY)
    result = retention.prune_attachments(store)
    assert result["deleted"] == [] and result["strays"] == [] and "symlink" in result["errors"][0]["error"]
    assert store.attachment(sha) is not None and (elsewhere / f"{sha}.png").exists()


def test_an_unlink_that_fails_leaves_a_stray_the_next_sweep_removes(store, tmp_path, monkeypatch):
    """C-28.2 the row's transaction has committed, so a copy that could not be unlinked
    is reported and becomes a stray, which a later pass removes after the grace."""
    sha = add(store, tmp_path)
    last_used(store, sha, 31)
    real = os.unlink

    def refuse(path, *args, **kwargs):
        if str(path) == str(copy_of(store, sha)):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    result = retention.prune_attachments(store)
    assert result["deleted"] == [sha] and "Operation not permitted" in result["errors"][0]["error"]
    monkeypatch.setattr(os, "unlink", real)
    assert copy_of(store, sha).exists() and store.attachment(sha) is None
    assert retention.prune_attachments(store)["strays"] == []                 # not yet a day old
    later = retention.prune_attachments(store, now=datetime.now(UTC) + timedelta(days=2))
    assert later["strays"] == [f"{sha}.png"] and not copy_of(store, sha).exists()


def test_the_sweep_removes_old_strays_and_nothing_it_did_not_name(store, tmp_path):
    """C-28.2 a copy no row names and a temporary copy go once a day old; a copy with a
    row stays however old; younger strays, other names and directories stay."""
    kept_sha = add(store, tmp_path, 0)
    stray_sha = hashlib.sha256(b"a crash between the copy and its row").hexdigest()
    young_sha = hashlib.sha256(b"an add still finishing").hexdigest()
    directory = store.root / "attachments"
    names = {                                                              # name: (age, removed)
        f"{stray_sha}.png": (2 * DAY, True), f".{stray_sha}.0123abcd.tmp": (2 * DAY, True),
        f"{young_sha}.jpg": (60, False), f".{young_sha}.89abcdef.tmp": (60, False),
        f"{stray_sha}.bmp": (2 * DAY, False), f"{stray_sha[:63]}.png": (2 * DAY, False),     # not names add makes
        "notes.txt": (2 * DAY, False), f".{stray_sha}.tmp": (2 * DAY, False),
    }
    for name, (age, _) in names.items():
        (directory / name).write_bytes(PNG)
        aged(directory / name, age)
    (directory / f"{'e' * 64}.gif").mkdir()
    aged(directory / f"{'e' * 64}.gif", 2 * DAY)
    aged(copy_of(store, kept_sha), 400 * DAY)
    result = retention.prune_attachments(store)
    assert sorted(result["strays"]) == sorted(name for name, (_, removed) in names.items() if removed)
    assert result["deleted"] == [] and result["errors"] == []
    left = set(os.listdir(directory))
    assert left == {f"{kept_sha}.png", f"{'e' * 64}.gif", *(name for name, (_, removed) in names.items() if not removed)}
    assert intact(store, kept_sha)


def test_a_symlink_named_like_a_stray_is_removed_not_followed(store, tmp_path):
    """C-28.2 a link where a stray copy would be is unlinked itself; its target stays."""
    target = tmp_path / "target.png"
    target.write_bytes(PNG)
    link = store.subdirectory("attachments") / f"{'f' * 64}.png"
    link.symlink_to(target)
    aged(link, 2 * DAY)
    assert retention.prune_attachments(store)["strays"] == [link.name]
    assert not link.is_symlink() and target.read_bytes() == PNG


# --- cancellation ---------------------------------------------------------------------------

def test_a_cancelled_pass_stops_between_attachments_and_the_next_finishes(store, tmp_path, monkeypatch):
    """C-28.2, C-16.4 cancellation is checked between attachments; the next pass
    deletes what this one left."""
    shas = [add(store, tmp_path, k) for k in range(3)]
    for sha in shas:
        last_used(store, sha, 31)
    cancel = threading.Event()
    real = store.delete_unused_attachment

    def delete_one_then_cancel(sha, cutoff):
        row = real(sha, cutoff)
        cancel.set()
        return row

    monkeypatch.setattr(store, "delete_unused_attachment", delete_one_then_cancel)
    first = retention.prune_attachments(store, cancel=cancel)
    assert first["interrupted"] == "cancelled" and len(first["deleted"]) == 1
    monkeypatch.setattr(store, "delete_unused_attachment", real)
    second = retention.prune_attachments(store)
    assert sorted(first["deleted"] + second["deleted"]) == sorted(shas)


def test_a_pass_past_its_deadline_changes_nothing(store, tmp_path):
    """C-28.2 a deadline already passed stops the pass before its first delete."""
    sha = add(store, tmp_path)
    last_used(store, sha, 31)
    result = retention.prune_attachments(store, deadline=time.monotonic() - 1)
    assert result["interrupted"] == "deadline" and result["deleted"] == []
    assert intact(store, sha)


# --- attachment.add under the guard -----------------------------------------------------

def test_an_add_points_the_row_at_the_copy_it_checked(store, tmp_path):
    """C-28.1 a row that named another path names the copy the add just checked."""
    sha = add(store, tmp_path)
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET path='/moved/away.png' WHERE sha256=?", (sha,))
    add(store, tmp_path)
    assert intact(store, sha)


# --- interleavings, forced with events --------------------------------------------------

class Threads:
    """Named threads whose results and exceptions are kept, and events for the moments
    a named thread reaches a hash's guard."""

    def __init__(self, store, monkeypatch):
        self.out: dict = {}
        self.at_guard: dict[str, threading.Event] = {}
        real = store.attachment_guard

        @contextlib.contextmanager
        def guard(sha):
            lock = real(sha)
            reached = self.at_guard.get(threading.current_thread().name)
            if reached is not None:
                reached.set()
            with lock:
                yield

        monkeypatch.setattr(store, "attachment_guard", guard)

    def start(self, name: str, fn) -> threading.Thread:
        def run():
            try:
                self.out[name] = fn()
            except BaseException as exc:                  # noqa: BLE001 - reported by the test
                self.out[name] = exc
        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread


def pause(event_reached: threading.Event, go: threading.Event) -> None:
    event_reached.set()
    assert go.wait(10), "the test never released this thread"


def test_retention_waits_for_an_add_between_its_check_of_the_copy_and_its_row(store, tmp_path, monkeypatch):
    """C-28.2: an add has found the stored copy whole and not yet written its row when
    retention comes for that hash (unused for 40 days). Retention waits on the guard,
    then its re-check sees the add's fresh use and keeps the copy: the receipt names a
    stored copy. Without the guard it deleted the row and unlinked the copy, and the
    add then wrote a row for a copy that was gone."""
    sha = add(store, tmp_path)
    last_used(store, sha, 40)
    threads = Threads(store, monkeypatch)
    checked, go, retention_at_guard = threading.Event(), threading.Event(), threading.Event()
    threads.at_guard["retention"] = retention_at_guard
    real = attachments._holds

    def holds(target, digest, size):
        whole = real(target, digest, size)
        if threading.current_thread().name == "add":
            pause(checked, go)
        return whole

    monkeypatch.setattr(attachments, "_holds", holds)
    adder = threads.start("add", lambda: attachments.add(store, str(image(tmp_path, 0))))
    assert checked.wait(10)
    pruner = threads.start("retention", lambda: retention.prune_attachments(store))
    assert retention_at_guard.wait(10)
    pruner.join(0.5)
    assert pruner.is_alive(), f"retention did not wait for the add: {threads.out.get('retention')}"
    go.set()
    adder.join(10)
    pruner.join(10)
    assert threads.out["add"] == {"sha256": sha, "media_type": "image/png", "bytes": len(PNG) + 4}
    assert threads.out["retention"]["deleted"] == []
    assert intact(store, sha)
    assert attachments.check(store, sha) == (str(copy_of(store, sha)), "image/png")


def test_an_add_waits_for_retention_between_its_delete_and_its_unlink(store, tmp_path, monkeypatch):
    """C-28.2: retention has deleted the row (committed) and not yet unlinked the copy
    when an add of the same bytes arrives. The add waits on the guard, then finds no
    copy and makes one: the receipt names a stored copy. Without the guard the add found
    the copy about to go, wrote its row, and retention then unlinked the copy."""
    sha = add(store, tmp_path)
    last_used(store, sha, 40)
    threads = Threads(store, monkeypatch)
    deleted, go, add_at_guard = threading.Event(), threading.Event(), threading.Event()
    threads.at_guard["add"] = add_at_guard
    real = store.delete_unused_attachment

    def delete(sha256, cutoff):
        row = real(sha256, cutoff)
        pause(deleted, go)
        return row

    monkeypatch.setattr(store, "delete_unused_attachment", delete)
    pruner = threads.start("retention", lambda: retention.prune_attachments(store))
    assert deleted.wait(10)
    assert store.attachment(sha) is None and copy_of(store, sha).exists()
    adder = threads.start("add", lambda: attachments.add(store, str(image(tmp_path, 0))))
    assert add_at_guard.wait(10)
    adder.join(0.5)
    assert adder.is_alive(), f"the add did not wait for retention: {threads.out.get('add')}"
    go.set()
    pruner.join(10)
    adder.join(10)
    assert threads.out["retention"]["deleted"] == [sha]
    assert threads.out["add"]["sha256"] == sha
    assert intact(store, sha)


def test_an_add_waits_for_the_stray_sweep_between_its_look_and_its_unlink(store, tmp_path, monkeypatch):
    """C-28.2: the sweep has found a day-old copy with no row (a crash between an add's
    copy and its row) and not yet unlinked it when an add of the same bytes arrives.
    The add waits, then makes the copy again and records it."""
    source = image(tmp_path, 0)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    stray = store.subdirectory("attachments") / f"{sha}.png"
    stray.write_bytes(source.read_bytes())
    aged(stray, 2 * DAY)
    threads = Threads(store, monkeypatch)
    looked, go, add_at_guard = threading.Event(), threading.Event(), threading.Event()
    threads.at_guard["add"] = add_at_guard
    real = store.attachment

    def attachment(sha256):
        row = real(sha256)
        if threading.current_thread().name == "retention":
            pause(looked, go)
        return row

    monkeypatch.setattr(store, "attachment", attachment)
    sweeper = threads.start("retention", lambda: retention.prune_attachments(store))
    assert looked.wait(10)
    adder = threads.start("add", lambda: attachments.add(store, str(source)))
    assert add_at_guard.wait(10)
    adder.join(0.5)
    assert adder.is_alive(), f"the add did not wait for the sweep: {threads.out.get('add')}"
    go.set()
    sweeper.join(10)
    adder.join(10)
    assert threads.out["retention"]["strays"] == [stray.name]
    assert threads.out["add"]["sha256"] == sha
    assert intact(store, sha)


# --- handoffs ---------------------------------------------------------------------------------

def prepare(store, moves):
    return store.prepare_handoff(request_id=f"h-{uuid.uuid4()}", provider="claude", workspace="/w", settings=SETTINGS,
                                 title=None, allow_main=False, handoff_from={},
                                 brief={"message_id": str(uuid.uuid4()), "text": "brief"}, moves=moves)


def test_a_handoff_uses_what_it_moves_so_a_withdrawn_message_keeps_it(store, tmp_path):
    """C-28.2, C-30.3: a moved message is `cancelled` for a moment when the tick
    settles the job the handoff cancelled, before the commit re-queues it in the new
    conversation. Preparing the handoff used its attachments, so retention keeps them
    meanwhile and the moved message can be sent."""
    cid = conversation(store)
    sha = add(store, tmp_path)
    message_id = send(store, cid, [sha])
    last_used(store, sha, 40)
    prepared = prepare(store, [{"message_id": str(uuid.uuid4()), "text": "look", "attachments": [sha]}])
    assert store.set_state(message_id, "cancelled", reason="cancelled: by handoff")
    assert retention.prune_attachments(store)["deleted"] == []
    target, created = store.commit_handoff(prepared, withdrawals=[{"message_id": message_id, "expect": ["cancelled"]}])
    assert created
    moved = store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person'",
                      (target["conversation_id"],))
    assert store.message(moved["message_id"])["attachments"] == [sha]
    assert attachments.check(store, sha) == (str(copy_of(store, sha)), "image/png")


def test_a_handoff_naming_an_unknown_attachment_uses_nothing(store, tmp_path):
    """C-28.2, C-30.3: the uses are one transaction; a missing attachment refuses the
    handoff and leaves every last use as it was."""
    sha = add(store, tmp_path)
    last_used(store, sha, 40)
    before = store.attachment(sha)["last_used_at"]
    with pytest.raises(ConversationError) as refused:
        prepare(store, [{"message_id": str(uuid.uuid4()), "text": "look", "attachments": [sha, "0" * 64]}])
    assert refused.value.reason == "unknown-attachment"
    assert store.attachment(sha)["last_used_at"] == before


# --- the re-check reads only open messages -----------------------------------------------

def test_the_re_check_reads_only_open_messages_and_fenced_conversations(store):
    """C-28.2 the re-check inside the delete transaction uses `messages_open` and the
    fenced conversations, never a scan of settled history."""
    from subfleet.conversations.store import _NEEDED, _needed_params
    plan = [row["detail"] for row in store.query(
        "EXPLAIN QUERY PLAN " + _NEEDED.format(sha="j.value=? AND ") + " LIMIT 1", _needed_params("a" * 64))]
    assert any("USING INDEX messages_open" in step for step in plan), plan
    assert any("USING INDEX messages_by_state" in step for step in plan), plan
    assert not any(step == "SCAN m" for step in plan), plan


# --- a model of the whole, driven by Hypothesis ---------------------------------------------

IMAGES = 4


class RetentionModel(RuleBasedStateMachine):
    """Random adds, sends, settlements, handoff fences, stray files, the passing of
    time and retention passes. After every step every row's copy is whole and every
    message that needs its attachments has them; each pass deletes exactly what a
    reference model says it should, and a second pass at the same moment nothing."""

    def __init__(self):
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp(prefix="attachment-retention-"))
        self.store = ConversationStore(self.tmp / "state")
        self.cids = [conversation(self.store) for _ in range(2)]
        self.sources = [image(self.tmp, k) for k in range(IMAGES)]
        self.shas = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.sources]
        self.messages: list[str] = []

    def teardown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fenced(self, cid: str) -> bool:
        return str(self.store.conversation(cid).get("blocked_by") or "").startswith("handoff:")

    def needed(self) -> set[str]:
        """The reference: what a message still needs, from the rows, in Python."""
        out = set()
        for row in self.store.query("SELECT conversation_id, state, attachments_json FROM messages"):
            if row["state"] not in TERMINAL_STATES or self.fenced(row["conversation_id"]):
                out.update(json.loads(row["attachments_json"]))
        return out

    @rule(k=st.integers(0, IMAGES - 1))
    def add(self, k):
        receipt = attachments.add(self.store, str(self.sources[k]))
        assert receipt["sha256"] == self.shas[k] and intact(self.store, self.shas[k])

    @rule(c=st.integers(0, 1), ks=st.sets(st.integers(0, IMAGES - 1), max_size=3), state=st.sampled_from(MESSAGE_STATES))
    def send(self, c, ks, state):
        shas = [self.shas[k] for k in sorted(ks)]
        stored = all(self.store.attachment(sha) is not None for sha in shas)
        try:
            message_id = send(self.store, self.cids[c], shas, state)
        except ConversationError as exc:
            assert exc.reason == "unknown-attachment" and not stored
            return
        assert stored
        self.messages.append(message_id)

    @precondition(lambda self: self.messages)
    @rule(data=st.data(), state=st.sampled_from(MESSAGE_STATES))
    def settle(self, data, state):
        """Any move production makes: a live message goes anywhere; a settled one comes
        back only in a fenced conversation (a handoff put back)."""
        message = self.store.message(data.draw(st.sampled_from(self.messages)))
        if message["state"] in TERMINAL_STATES and not self.fenced(message["conversation_id"]):
            return
        self.store.set_state(message["message_id"], state)

    @rule(c=st.integers(0, 1), fence=st.sampled_from([None, "handoff:h-1", "quarantined-turn"]))
    def block(self, c, fence):
        self.store.update_conversation(self.cids[c], blocked_by=fence)

    @rule(days=st.sampled_from([1, 10, 29, 31, 90]))
    def time_passes(self, days):
        with self.store.transaction() as tx:
            for row in tx.execute("SELECT sha256, last_used_at FROM attachments").fetchall():
                used = datetime.fromisoformat(row["last_used_at"].replace("Z", "+00:00")) - timedelta(days=days)
                tx.execute("UPDATE attachments SET last_used_at=? WHERE sha256=?", (stamp(used), row["sha256"]))

    @rule(k=st.integers(0, IMAGES - 1), temporary=st.booleans(), old=st.booleans())
    def crash_leftover(self, k, temporary, old):
        sha = self.shas[k]
        directory = self.store.subdirectory("attachments")
        if temporary:
            path = directory / f".{sha}.{random.Random(k).getrandbits(32):08x}.tmp"
            path.write_bytes(b"part of a copy")
        elif self.store.attachment(sha) is None:
            path = directory / f"{sha}.png"
            path.write_bytes(self.sources[k].read_bytes())
        else:
            return
        aged(path, 2 * DAY if old else 60)

    @rule()
    def prune(self):
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=30)
        needed = self.needed()
        expected = {row["sha256"] for row in self.store.query("SELECT sha256, last_used_at FROM attachments")
                    if datetime.fromisoformat(row["last_used_at"].replace("Z", "+00:00")) <= cutoff
                    and row["sha256"] not in needed}
        result = retention.prune_attachments(self.store, now=now)
        assert set(result["deleted"]) == expected and len(result["deleted"]) == len(expected)
        assert result["errors"] == [] and "interrupted" not in result
        again = retention.prune_attachments(self.store, now=now)
        assert again["deleted"] == [] and again["strays"] == []
        for entry in os.scandir(self.store.subdirectory("attachments")):
            match = retention._COPY.fullmatch(entry.name) or retention._TEMPORARY.fullmatch(entry.name)
            old = now.timestamp() - entry.stat(follow_symlinks=False).st_mtime >= retention.ATTACHMENT_STRAY_GRACE_S
            if match and old:
                assert retention._COPY.fullmatch(entry.name) and self.store.attachment(match.group(1)) is not None

    @invariant()
    def every_row_has_its_copy(self):
        for row in self.store.query("SELECT sha256 FROM attachments"):
            assert intact(self.store, row["sha256"])

    @invariant()
    def every_needed_attachment_is_stored(self):
        for sha in self.needed():
            assert intact(self.store, sha)


TestRetentionModel = RetentionModel.TestCase
TestRetentionModel.settings = settings(max_examples=60, stateful_step_count=30, deadline=None,
                                       suppress_health_check=[HealthCheck.too_slow])


# --- everything at once ------------------------------------------------------------------------

def test_adds_sends_and_retention_at_once_never_leave_a_row_or_a_needed_message_without_its_copy(store, tmp_path):
    """C-28.1, C-28.2 under contention: adders re-adding the same few images, a sender
    naming them in messages it then settles, and retention running continuously with
    no keep time and no grace. A checker holding each hash's guard finds, every time,
    that a row's copy is whole and a message not yet terminal has its attachments."""
    sources = [image(tmp_path, k) for k in range(3)]
    shas = [hashlib.sha256(path.read_bytes()).hexdigest() for path in sources]
    cid = conversation(store)
    stop, failures = threading.Event(), []
    counts = {"adds": 0, "sends": 0, "refused": 0, "passes": 0, "deleted": 0, "checks": 0}

    def loop(name, step):
        def run():
            rng = random.Random(name)
            try:
                while not stop.is_set():
                    step(rng)
            except BaseException as exc:                  # noqa: BLE001 - reported below
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                stop.set()
        return threading.Thread(target=run, name=name, daemon=True)

    def adder(rng):
        k = rng.randrange(len(sources))
        assert attachments.add(store, str(sources[k]))["sha256"] == shas[k]
        counts["adds"] += 1

    def sender(rng):
        k = rng.randrange(len(sources))
        attachments.add(store, str(sources[k]))
        try:
            message_id = send(store, cid, [shas[k]])
        except ConversationError as exc:                  # retention took it between the add and the send
            assert exc.reason == "unknown-attachment", exc
            counts["refused"] += 1
            return
        counts["sends"] += 1
        time.sleep(rng.random() / 500)
        store.set_state(message_id, rng.choice(TERMINAL_STATES))

    def pruner(rng):
        result = retention.prune_attachments(store, keep_s=0, stray_grace_s=0)
        assert result["errors"] == [], result["errors"]
        counts["passes"] += 1
        counts["deleted"] += len(result["deleted"])

    def checker(rng):
        for sha in shas:
            with store.attachment_guard(sha):
                if store.attachment(sha) is not None:
                    assert intact(store, sha), f"a row without its copy: {sha}"
        for row in store.query("SELECT message_id, attachments_json FROM messages"):
            for sha in json.loads(row["attachments_json"]):
                with store.attachment_guard(sha):
                    if store.message(row["message_id"])["state"] not in TERMINAL_STATES:
                        assert intact(store, sha), f"a live message lost {sha}"
        counts["checks"] += 1

    threads = [loop(f"adder-{n}", adder) for n in range(3)] + [loop("sender", sender), loop("retention", pruner),
                                                               loop("checker", checker)]
    for thread in threads:
        thread.start()
    stop.wait(1.5)
    stop.set()
    for thread in threads:
        thread.join(10)
    assert failures == [], failures
    assert all(not thread.is_alive() for thread in threads)
    # The run exercised what it claims to: adds, sends, deletions and checks all happened.
    assert counts["adds"] and counts["sends"] and counts["deleted"] and counts["checks"], counts
    for sha in shas:
        assert store.attachment(sha) is None or intact(store, sha)
