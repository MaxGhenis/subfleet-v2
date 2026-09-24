"""C-26.10, C-26.13 (design D-25): a turn's end snapshot, taken by the daemon's own
finalization code over a real checkout, with no provider process.

The attempt is reserved by the ordinary admission path and then given what
admission records for a writable turn (kind `turn`, `workspace-write`, the
working-tree snapshot as `baseline_tree`, a manifest `turn` block), so the code
under test is the daemon's, not a copy of it.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from subfleet.conversations import diff as turn_diff
from subfleet.salvage import SalvageError, working_tree
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (fixture)

SETTINGS = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}


def git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def writable_turn(daemon, harness):
    work = harness.workdir
    git(work, "init", "-b", "feature/turn")
    git(work, "config", "user.name", "Test User")
    git(work, "config", "user.email", "test@example.invalid")
    (work / "tracked.txt").write_text("baseline\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "baseline")
    job_id, attempt, adir = reserve(daemon, harness)
    head = git(work, "rev-parse", "HEAD")
    start = working_tree(work, head)
    conversations = daemon.conversations.store
    conversation, _ = conversations.create_conversation(provider="claude", workspace=str(work),
                                                        workspace_kind="in-place", settings=SETTINGS, origin="new")
    mid = str(uuid.uuid4())
    conversations.submit_message(conversation_id=conversation["conversation_id"], message_id=mid,
                                 after_message_id=None, text="edit", attachments=[], settings=SETTINGS)
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET kind='turn', sandbox='workspace-write', in_place=1, worktree=? WHERE job_id=?",
                   (str(work), job_id))
        tx.execute("UPDATE attempts SET baseline_tree=?, evidence_json=? WHERE attempt_id=?",
                   (start, json.dumps({"baseline_commit": head}), attempt["attempt_id"]))
    manifest = daemon.root / "jobs" / job_id / "manifest.json"
    data = json.loads(manifest.read_text())
    data["turn"] = {"conversation_id": conversation["conversation_id"], "message_id": mid, "provider": "claude",
                    "cwd": str(work), "settings": SETTINGS}
    manifest.write_text(json.dumps(data))
    return job_id, daemon.store.get_attempt(attempt["attempt_id"]), adir, mid, head, start


def test_c26_13_finalization_takes_the_end_snapshot_while_the_turn_holds_its_leases(state_daemon, monkeypatch):
    """C-26.10, C-26.13: finalization of a turn writes no salvage ref, records HEAD after
    and the end snapshot in the receipt and in the conversation store, before the leases
    are released; a replayed finalization takes nothing twice."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, head, start = writable_turn(daemon, harness)
    (harness.workdir / "made-by-the-turn.txt").write_text("one\ntwo\n")
    leases_at_snapshot = []
    real = turn_diff.end_snapshot

    def observed(*args, **kwargs):
        leases_at_snapshot.extend(daemon.store.query("SELECT lease_key FROM leases WHERE holder IN (?,?)",
                                                     (job_id, attempt["attempt_id"])))
        return real(*args, **kwargs)

    monkeypatch.setattr(turn_diff, "end_snapshot", observed)
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    assert leases_at_snapshot, "the end snapshot is taken while the turn still holds its leases"
    receipt = json.loads((adir / "trees.json").read_text())
    assert (receipt["head_before"], receipt["head_after"], receipt["start_tree"]) == (head, head, start)
    assert receipt["end_tree"] and receipt["end_tree"] != start and receipt["error"] is None
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["turn_trees"]["end_tree"] == receipt["end_tree"]
    assert git(harness.workdir, "for-each-ref", "refs/subfleet-salvage") == ""
    row = daemon.conversations.store.turn_trees(mid)
    assert (row["start_tree"], row["end_tree"], row["writable"]) == (start, receipt["end_tree"], True)
    result = daemon.conversations.op_turn_diff({"message_id": mid}, None)
    assert [(f["path"], f["status"], f["additions"]) for f in result["files"]] == [
        ("made-by-the-turn.txt", "added", 2)]
    assert result["to"]["live"] is False
    monkeypatch.setattr(turn_diff, "end_snapshot", lambda *a, **k: pytest.fail("snapshot taken twice"))
    assert daemon._turn_trees(daemon._job(job_id), attempt) == receipt


def test_c26_13_transient_snapshot_failures_retry_then_the_failure_is_recorded(state_daemon, monkeypatch):
    """C-6.8, C-26.13: a transient git failure is retried by the worker (the call raises)
    up to TURN_TREE_TRIES tries, then recorded; any other failure is recorded at once;
    either way finalization goes on without an end snapshot."""
    from subfleet.daemon import TURN_TREE_TRIES
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    job = daemon._job(job_id)
    calls: list[int] = []

    def late(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)

    monkeypatch.setattr(turn_diff, "end_snapshot", late)
    for _ in range(TURN_TREE_TRIES - 1):
        with pytest.raises(SalvageError):
            daemon._turn_trees(job, attempt)
        assert not (adir / "trees.json").exists()
    receipt = daemon._turn_trees(job, attempt)
    assert len(calls) == TURN_TREE_TRIES and receipt["error"].startswith("end snapshot failed: git add timed out")
    assert daemon.conversations.store.turn_trees(mid)["error"] == receipt["error"]
    result = daemon.conversations.op_turn_diff({"message_id": mid}, None)
    assert (result["available"], result["reason"]) == (False, "snapshot-failed")

    # Not transient: recorded on the first try.
    (adir / "trees.json").unlink()
    calls.clear()

    def broken(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git write-tree failed: corrupt")

    monkeypatch.setattr(turn_diff, "end_snapshot", broken)
    assert daemon._turn_trees(job, attempt)["error"] == "end snapshot failed: git write-tree failed: corrupt"
    assert len(calls) == 1
