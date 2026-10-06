"""C-26.10, C-26.14 (design D-25): a turn's end snapshot, taken by the daemon's own
finalization and quarantine code over a real checkout, with no provider process.

The attempt is reserved by the ordinary admission path and then given what
admission records for a writable turn (kind `turn`, `workspace-write`, the
working-tree snapshot as `baseline_tree`, a manifest `turn` block), so the code
under test is the daemon's, not a copy of it.
"""

from __future__ import annotations

import json
import shutil
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


def test_c13_1_a_turns_end_snapshot_lists_the_nested_repositories_it_left_out(state_daemon):
    """C-13.1, C-26.14 (review of c1f95838, F7): a turn's snapshots leave out a nested
    repository with no commit, as salvage does, so its diff does not show one; the
    receipt and the attempt's evidence list what the end snapshot left out."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    nested = harness.workdir / "scratch" / "repo "
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "inside.txt").write_text("never committed\n")
    (harness.workdir / "made-by-the-turn.txt").write_text("one\n")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    receipt = json.loads((adir / "trees.json").read_text())
    assert receipt["skipped"] == ["scratch/repo /"] and receipt["error"] is None and receipt["end_tree"]
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["turn_trees"]["skipped"] == ["scratch/repo /"]
    result = daemon.conversations.op_turn_diff({"message_id": mid}, None)
    assert [f["path"] for f in result["files"]] == ["made-by-the-turn.txt"]


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


def test_c26_14_an_end_snapshot_failure_that_quotes_a_name_that_is_not_utf8_is_recorded(state_daemon, monkeypatch):
    """Review of cda4c161, N1, for turns: the end snapshot's error reaches `trees.json`, the
    attempt's evidence and the conversation store, and a live diff's error reaches the
    caller; git quoted the name in its own bytes, carried as a surrogate, which none of
    them could encode, so the turn's finalization raised on every try. Only `add -A` is
    faked (APFS refuses such names); the rest is real."""
    from subfleet.conversations.store import ConversationError
    from tests.unit.test_salvage_unindexable import fake_add
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    (harness.workdir / "made-by-the-turn.txt").write_text("one\n")
    fake_add(monkeypatch, 128, b'error: open("caf\xe9.txt"): Permission denied\nfatal: adding files failed\n')
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    error = 'end snapshot failed: git add failed: error: open("caf\\xe9.txt"): Permission denied\nfatal: adding files failed'
    receipt = json.loads((adir / "trees.json").read_text())
    assert receipt["error"] == error and receipt["end_tree"] is None
    assert json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])["turn_trees"]["error"] == error
    assert daemon.conversations.store.turn_trees(mid)["error"] == error
    cid = daemon.conversations.store.message(mid)["conversation_id"]
    with pytest.raises(ConversationError) as caught:
        daemon.conversations.op_conversation_diff({"conversation_id": cid}, None)
    assert str(caught.value) == error.removeprefix("end snapshot failed: ")


def test_c26_14_a_live_diff_says_which_nested_repositories_it_does_not_show(state_daemon):
    """C-13.1, C-26.14 (review of cda4c161, N3): the live `to` (conversation.diff, and
    turn.diff before a turn's end exists; both go through `_compare`) is a snapshot that
    leaves out a nested repository with no commit, and it said nothing. It lists them."""
    daemon, harness = state_daemon
    job_id, attempt, adir, mid, _, _ = writable_turn(daemon, harness)
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    service = daemon.conversations
    cid = service.store.message(mid)["conversation_id"]
    nested = harness.workdir / "scratch" / "repo "
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "inside.txt").write_text("never committed\n")
    (harness.workdir / "after-the-turn.txt").write_text("one\n")
    live = service.op_conversation_diff({"conversation_id": cid}, None)
    assert live["to"]["live"] is True and live["to"]["skipped"] == ["scratch/repo /"]
    assert [f["path"] for f in live["files"]] == ["after-the-turn.txt"]
    ended = service.op_turn_diff({"message_id": mid}, None)
    assert ended["to"]["live"] is False and "skipped" not in ended["to"]    # the receipt lists the end's
    shutil.rmtree(nested)
    assert service.op_conversation_diff({"conversation_id": cid}, None)["to"]["skipped"] == []


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


def started(daemon, attempt: dict, adir) -> dict:
    """What `service._adopt` does for a turn attempt in `starting`: its runner records the
    start (C-26.14), from the attempt joined with its job's sandbox."""
    with daemon.store.transaction() as tx:
        tx.execute("UPDATE attempts SET state='starting' WHERE attempt_id=?", (attempt["attempt_id"],))
        tx.execute("UPDATE jobs SET max_attempts=1 WHERE job_id=?", (attempt["job_id"],))   # as a turn job is
    turn = json.loads((daemon.root / "jobs" / attempt["job_id"] / "manifest.json").read_text())["turn"]
    daemon.conversations._record_start(turn, {**daemon.store.get_attempt(attempt["attempt_id"]),
                                              "job_sandbox": "workspace-write"})
    return daemon.store.get_attempt(attempt["attempt_id"])


def test_c26_14_i4_a_turn_that_ended_unrecorded_stops_sharing_its_folder(state_daemon, monkeypatch):
    """Review P3-5 of 5e9f2fbd, through the daemon's own paths: Alpha's runner recorded
    its start, then its guardian never answered and nothing was there, so the attempt
    ended `failed` (`starting-no-receipt`, `_unlaunched`) with no end recorded. Beta,
    begun seconds after it ended, was marked as sharing the folder with Alpha "still
    running". The service's tick closes Alpha's window at its attempt's `finished_at`
    and unmarks Beta; Gamma, begun while Alpha was starting, keeps its mark; Alpha's
    `turn.diff` says why it has no end snapshot. A quarantined turn's window stays open
    (its writers may be live), and a row whose job retention has removed is closed now.
    The step is one of the tick's."""
    from datetime import datetime, timedelta
    from subfleet.daemon import utcnow_ms
    daemon, harness = state_daemon
    service = daemon.conversations
    alpha_began = utcnow_ms()
    a_cid, a_mid, a_attempt, a_dir = turn_of(daemon, harness, "Alpha", alpha_began, first=True)
    a_attempt = started(daemon, a_attempt, a_dir)
    g_cid, g_mid, g_attempt, g_dir = turn_of(daemon, harness, "Gamma", alpha_began)
    started(daemon, g_attempt, g_dir)
    daemon._unlaunched(a_attempt, "starting-no-receipt")
    ended = daemon.store.get_attempt(a_attempt["attempt_id"])
    assert ended["state"] == "failed" and ended["finished_at"]
    assert service.store.turn_trees(a_mid)["ended_at"] is None             # nothing recorded its end
    later = (datetime.fromisoformat(ended["finished_at"].replace("Z", "+00:00")) + timedelta(seconds=2)
             ).isoformat(timespec="milliseconds").replace("+00:00", "Z")        # past the second it ended in
    b_cid, b_mid, b_attempt, b_dir = turn_of(daemon, harness, "Beta", later)
    started(daemon, b_attempt, b_dir)
    assert [s["title"] for s in service.op_turn_diff({"message_id": b_mid}, None)["shared"]] == ["Alpha", "Gamma"]
    assert service.op_turn_diff({"message_id": b_mid}, None)["shared"][0]["to"] is None      # "still running"

    q_cid, q_mid, q_attempt, q_dir = turn_of(daemon, harness, "Held", later)
    q_attempt = started(daemon, q_attempt, q_dir)
    daemon._quarantine(q_attempt, Containment(marker_pids=frozenset({42099})), "fixture")
    service.store.record_trees(attempt_id="20260901-000000-pruned/a1", message_id="pruned", conversation_id="gone",
                               workspace=str(harness.workdir), writable=True, started_at="2026-09-01T00:00:00Z",
                               start_tree="t", target="/a/folder/of/its/own")
    feed = service.store.changes_after(0)["next"]
    swept: list[str] = []
    real = service._close_ended_windows
    for name in ("_lift_stale_fences", "_catalog_tick", "_dispatch", "_adopt_runners", "_replay_unsettled",
                 "_settle_unstarted", "_reap_runners", "_compact"):
        monkeypatch.setattr(service, name, lambda: None)
    monkeypatch.setattr(service, "_close_ended_windows", lambda: (swept.append("tick"), real()))
    service.tick()
    assert swept == ["tick"]

    alpha = service.store.turn_trees(a_mid)
    assert alpha["ended_at"] == ended["finished_at"].replace("Z", ".999Z")
    assert alpha["error"] == "the turn's attempt ended (failed) with no end recorded; no end snapshot"
    a_diff = service.op_turn_diff({"message_id": a_mid}, None)
    assert (a_diff["available"], a_diff["reason"]) == (False, "snapshot-failed")
    assert [s["title"] for s in a_diff["shared"]] == ["Gamma"]
    assert [s["title"] for s in service.op_turn_diff({"message_id": b_mid}, None)["shared"]] == ["Gamma", "Held"]
    assert "Alpha" in [s["title"] for s in service.op_turn_diff({"message_id": g_mid}, None)["shared"]]
    assert service.store.turn_trees(q_mid)["ended_at"] is None                 # quarantined: not ended
    pruned = service.store.turn_trees("pruned")
    assert pruned["ended_at"] and "no longer in the job store" in pruned["error"]
    changes = service.store.changes_after(feed)
    changed = {c["message_id"]: c for c in changes["changes"]}
    assert {a_mid, b_mid, "pruned"} <= changed.keys()
    assert changed[a_mid]["title"] == "Alpha"
    assert changed["pruned"]["title"] is None
    assert changes["next"] > feed
    assert service.store.open_windows() == [g_attempt["attempt_id"], b_attempt["attempt_id"],
                                            q_attempt["attempt_id"]]
