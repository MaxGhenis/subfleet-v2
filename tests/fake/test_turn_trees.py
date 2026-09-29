"""C-26.10, C-26.14 (design D-25): a turn's end snapshot, taken by the daemon's own
finalization and quarantine code over a real checkout, with no provider process.

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

from subfleet import protocol
from subfleet.conversations import diff as turn_diff
from subfleet.procs import Containment
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
    """C-26.10, C-26.14: finalization of a turn writes no salvage ref, records HEAD after
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
    """C-6.8, C-26.14: a transient git failure is retried by the worker (the call raises)
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


def writable_turn_again(daemon, harness):
    """A second turn attempt in the same checkout (for tests that need two)."""
    job_id, attempt, adir = reserve(daemon, harness)
    head = git(harness.workdir, "rev-parse", "HEAD")
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET kind='turn', sandbox='workspace-write', in_place=1, worktree=? WHERE job_id=?",
                   (str(harness.workdir), job_id))
        tx.execute("UPDATE attempts SET baseline_tree=?, evidence_json=? WHERE attempt_id=?",
                   (working_tree(harness.workdir, head), json.dumps({"baseline_commit": head}),
                    attempt["attempt_id"]))
    return job_id, daemon.store.get_attempt(attempt["attempt_id"]), adir, None, head, None


def test_c26_10_a_quarantined_turn_is_released_without_a_salvage_ref(state_daemon, monkeypatch):
    """C-26.10, C-26.14, C-5.7: confirming a quarantined turn dead takes its end snapshot
    and writes no salvage ref; a forced release with writers still live records that no
    end snapshot was taken, never one taken while something may still be writing."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, start = writable_turn(daemon, harness)
    (harness.workdir / "tracked.txt").write_text("changed before the quarantine\n")
    daemon._quarantine(attempt, Containment(), "fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    assert git(harness.workdir, "for-each-ref", "refs/subfleet-salvage") == ""
    receipt = json.loads((adir / "trees.json").read_text())
    assert receipt["start_tree"] == start and receipt["end_tree"] and receipt["error"] is None
    assert daemon.conversations.store.turn_trees(mid)["end_tree"] == receipt["end_tree"]

    job2, attempt2, adir2, _, _, _ = writable_turn_again(daemon, harness)
    survivor = Containment(marker_pids=frozenset({42099}))
    daemon._quarantine(attempt2, survivor, "fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: survivor)
    monkeypatch.setattr(turn_diff, "end_snapshot", lambda *a, **k: pytest.fail("no snapshot with writers live"))
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt2["attempt_id"]),
                               protocol.KillArgs(job2, force_release=True, operator_note="fixture"))
    forced = json.loads((adir2 / "trees.json").read_text())
    assert forced["end_tree"] is None and "writers still live" in forced["error"]
    assert git(harness.workdir, "for-each-ref", "refs/subfleet-salvage") == ""


def test_c26_13_a_quarantine_release_tries_the_end_snapshot_once(state_daemon, monkeypatch):
    """C-26.14, C-26.10, C-5.10: nothing offers an operator's `kill --confirm-dead`
    again, so a transient git failure at its end snapshot is recorded at once and the
    release still completes: the leases go, the attempt leaves quarantine, and the
    receipt and the conversation store record the failure."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    assert daemon.store.acquire_lease(f"worktree:{harness.workdir}", job_id)
    daemon._quarantine(attempt, Containment(), "fixture")
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    calls: list[int] = []

    def late(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)

    monkeypatch.setattr(turn_diff, "end_snapshot", late)
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    assert len(calls) == 1
    receipt = json.loads((adir / "trees.json").read_text())
    assert receipt["end_tree"] is None and receipt["error"].startswith("end snapshot failed: git add timed out")
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] in ("lost", "interrupted")
    assert not daemon.store.query("SELECT 1 FROM leases WHERE holder IN (?,?)", (job_id, attempt["attempt_id"]))
    row = daemon.conversations.store.turn_trees(mid)
    assert row["error"] == receipt["error"] and row["ended_at"]
    result = daemon.conversations.op_turn_diff({"message_id": mid}, None)
    assert (result["available"], result["reason"]) == (False, "snapshot-failed")
    assert git(harness.workdir, "for-each-ref", "refs/subfleet-salvage") == ""


def test_c26_13_a_workspace_git_cannot_open_is_gone_for_both_diffs(state_daemon):
    """C-26.14: once the workspace is moved away, a finished turn's `turn.diff` (two
    stored snapshots) and the live `conversation.diff` both answer `workspace-gone`,
    never `snapshot-pruned`; back in place, both answer again."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    (harness.workdir / "made-by-the-turn.txt").write_text("one\n")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    service = daemon.conversations
    cid = service.store.message(mid)["conversation_id"]
    assert service.op_turn_diff({"message_id": mid}, None)["available"]
    assert service.op_conversation_diff({"conversation_id": cid}, None)["available"]
    away = harness.workdir.with_name(harness.workdir.name + "-moved")
    harness.workdir.rename(away)
    try:
        for result in (service.op_turn_diff({"message_id": mid}, None),
                       service.op_conversation_diff({"conversation_id": cid}, None)):
            assert (result["available"], result["reason"]) == (False, "workspace-gone"), result
            assert result["files"] == [] and result["diff"] == ""
    finally:
        away.rename(harness.workdir)
    back = service.op_turn_diff({"message_id": mid}, None)
    assert back["available"] and back["root"] == str(harness.workdir.resolve())
    assert service.op_conversation_diff({"conversation_id": cid}, None)["available"]


@pytest.mark.parametrize("turn_state,ok", [("complete", True), ("interrupted", False)])
def test_c24_4_a_signal_after_a_complete_turn_does_not_undo_it(state_daemon, turn_state, ok):
    """C-24.4 with C-9.2 (merge review 2026-09-25, finding 2): a turn's `ok` is the driver's
    recorded `complete`, so the daemon's signal after it (a stop that came too late, or
    containment of a process that lingered) leaves the turn succeeded; main's killed_by
    rule had made it `unknown` and the job `cancelled`, rc 130, while its message was
    complete. A turn the driver did not record complete stays not ok."""
    from subfleet.daemon import utcnow
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, head, start = writable_turn(daemon, harness)
    (adir / "turn.json").write_text(json.dumps({"state": turn_state, "reason": None if ok else "stopped",
                                                "final_text": "done"}))
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE attempts SET killed_by='operator' WHERE attempt_id=?", (attempt["attempt_id"],))
        tx.execute("UPDATE jobs SET cancel_requested_at=? WHERE job_id=?", (utcnow(), job_id))
    daemon._finalize(receipt_fixture(daemon, daemon.store.get_attempt(attempt["attempt_id"]), adir, rc=-15))
    job, finished = daemon._job(job_id), daemon.store.get_attempt(attempt["attempt_id"])
    if ok:
        assert (job["state"], finished["state"], finished["outcome_class"]) == ("succeeded", "succeeded", "ok")
        assert "provider_verdict" not in json.loads(finished["evidence_json"])
    else:
        assert job["state"] == "cancelled" and finished["outcome_class"] != "ok"


def turn_of(daemon, harness, title, baseline_at, *, first=False):
    """A writable turn of a conversation of its own in the harness's checkout, reserved by
    admission and given what admission records for a turn: its start snapshot, and in its
    evidence the folder it writes in and when that snapshot began (C-26.14)."""
    work = harness.workdir
    if first:
        git(work, "init", "-b", "feature/turn")
        git(work, "config", "user.name", "Test User")
        git(work, "config", "user.email", "test@example.invalid")
        (work / "tracked.txt").write_text("baseline\n")
        git(work, "add", ".")
        git(work, "commit", "-m", "baseline")
    job_id, attempt, adir = reserve(daemon, harness)
    head = git(work, "rev-parse", "HEAD")
    conversations = daemon.conversations.store
    conversation, _ = conversations.create_conversation(provider="claude", workspace=str(work), title=title,
                                                        workspace_kind="in-place", settings=SETTINGS, origin="new")
    mid = str(uuid.uuid4())
    conversations.submit_message(conversation_id=conversation["conversation_id"], message_id=mid,
                                 after_message_id=None, text="edit", attachments=[], settings=SETTINGS)
    evidence = {"baseline_commit": head, "baseline_at": baseline_at, "folder": str(work.resolve())}
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET kind='turn', sandbox='workspace-write', in_place=1, worktree=? WHERE job_id=?",
                   (str(work), job_id))
        tx.execute("UPDATE attempts SET baseline_tree=?, evidence_json=? WHERE attempt_id=?",
                   (working_tree(work, head), json.dumps(evidence), attempt["attempt_id"]))
    manifest = daemon.root / "jobs" / job_id / "manifest.json"
    data = json.loads(manifest.read_text())
    data["turn"] = {"conversation_id": conversation["conversation_id"], "message_id": mid, "provider": "claude",
                    "cwd": str(work), "settings": SETTINGS}
    manifest.write_text(json.dumps(data))
    return conversation["conversation_id"], mid, daemon.store.get_attempt(attempt["attempt_id"]), adir


def test_c26_14_i4_a_turn_s_changes_name_the_conversation_that_wrote_beside_it(state_daemon):
    """I4 end to end, through the daemon's finalization and the diff ops: conversation B's
    turn began its snapshot before A's turn ended, but was recorded only after (its runner
    adopted late). Each turn's `turn.diff` names the other conversation, with its title and
    when it wrote; A's diff holds B's edit and says it may. C's turn, begun after both ended,
    names nobody. `conversation.diff`, which runs to the working tree now, names every other
    conversation that wrote there since its first turn began."""
    from subfleet.daemon import utcnow_ms
    daemon, harness = state_daemon
    service = daemon.conversations
    a_cid, a_mid, a_attempt, a_dir = turn_of(daemon, harness, "Alpha", utcnow_ms(), first=True)
    (harness.workdir / "by-alpha.txt").write_text("a\n")
    b_began = utcnow_ms()                          # B's snapshot begins while A is running
    (harness.workdir / "by-beta.txt").write_text("b\n")
    daemon._finalize(receipt_fixture(daemon, a_attempt, a_dir))
    assert service.store.turn_trees(a_mid)["shared"] == []           # B not recorded yet
    b_cid, b_mid, b_attempt, b_dir = turn_of(daemon, harness, "Beta", b_began)
    daemon._finalize(receipt_fixture(daemon, b_attempt, b_dir))
    c_cid, c_mid, c_attempt, c_dir = turn_of(daemon, harness, "Gamma", utcnow_ms())
    daemon._finalize(receipt_fixture(daemon, c_attempt, c_dir))

    a_diff = service.op_turn_diff({"message_id": a_mid}, None)
    assert {f["path"] for f in a_diff["files"]} == {"by-alpha.txt", "by-beta.txt"}
    assert [(s["conversation_id"], s["title"], s["message_ids"]) for s in a_diff["shared"]] == [(b_cid, "Beta", [b_mid])]
    assert a_diff["shared"][0]["from"] == b_began and a_diff["shared"][0]["to"]
    b_diff = service.op_turn_diff({"message_id": b_mid}, None)
    assert [(s["conversation_id"], s["title"]) for s in b_diff["shared"]] == [(a_cid, "Alpha")]
    assert service.op_turn_diff({"message_id": c_mid}, None)["shared"] == []
    whole = service.op_conversation_diff({"conversation_id": a_cid}, None)
    assert [s["title"] for s in whole["shared"]] == ["Beta", "Gamma"]
    assert service.op_conversation_diff({"conversation_id": c_cid}, None)["shared"] == []
