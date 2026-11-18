"""C-8.4, C-6.5, C-6.8: a turn submit could not spell in full (`unspelled`) is looked
at for retention's fence on the folder its reservation names.

Review of #141 at 2cbf6fca, P2-A: the look before the workspace (`_fence_hold`) read
the folder submit recorded, while the reserving transaction read the one admission
found again after preparing the workspace (`_row_folder`). A repository nested in a
job's tree, quarantined between submit's validation and its folder lookup and put
back before admission, with a persisted `core.worktree` naming a top elsewhere, was
looked at on the nested folder it recorded (no fence there), its workspace was
prepared, and only the transaction found retention's fence on the external top.
Admission now finds the folder again once per look, before the look
(`_rediscovered`), and hands that one answer to the look and to the transaction.

The reviewer's probes are adopted here byte for byte but for their names and
docstrings (review-141-r2/probes/probe_turns.py, probe_mutations.py and
probe_contract.py). The other tests measure what that lookup touches in a tree being
retired, and pin what a lookup that cannot finish does. Real submission, admission,
Git and spelling; no provider is launched.
"""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, folders, retention
from subfleet.daemon import Daemon
from subfleet.salvage import SalvageError
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in
from tests.unit.retention_world import git, snapshot
from tests.unit.test_retention_shared_folders import ctimes, git_spawns, retiring_tree


def external_unspelled_turn(daemon, harness, patch, writable):
    """Validation sees a cwd whose Git top is external; submit sees it away."""
    tree, nested = retiring_tree(daemon, harness)
    external = daemon.root / "worktrees" / "External"
    git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(external), "HEAD")
    (external / "f.txt").write_text("nested\n")
    git(Path(nested), "config", "core.worktree", str(external))
    outside = folders.canonical(external)
    daemon.store.add_job(
        job_id="External", request_id="External", payload_digest="d", kind="dispatch",
        workdir=str(harness.workdir), worktree=outside, prompt_path="/prompt",
        sandbox="workspace-write", state="succeeded", workdir_head=git(external, "rev-parse", "HEAD"),
    )
    external_job_dir = daemon.root / "jobs" / "External"
    external_job_dir.mkdir()
    (external_job_dir / "stdout").write_text("external finished job output")
    aside = Path(tree).with_name(".retired.review-aside")
    real_head, moved = daemon_module.git_head, []

    def head_after_validation(workdir, *args, **kwargs):
        if not moved and str(workdir) == nested:
            assert Path(nested).is_dir()
            Path(tree).rename(aside)
            moved.append(True)
        return real_head(workdir, *args, **kwargs)

    with pytest.MonkeyPatch.context() as submit_patch:
        submit_patch.setattr(daemon_module, "git_head", head_after_validation)
        _, _, turn = message_in(
            daemon, harness, "External top discovered after quarantine rollback",
            workspace=nested,
            settings={**SETTINGS, "permission": "accept-edits" if writable else "read-only"},
        )
    assert moved
    submitted = daemon._submitted(turn)
    assert submitted.get("unspelled") is True, submitted
    assert (submitted.get("write_target") or submitted.get("folder")) == nested, submitted
    aside.rename(Path(tree))
    assert daemon._row_folder(nested)[0] == outside
    # The queued turn names its cwd, not the external Git top, in jobs.workdir.
    # Thus the real selection pin check permits retirement to fence External.
    assert retention._pin_reasons(daemon.store, set(), None, only="External", root=daemon.root).get("External") is None
    assert folders.turn_holds(daemon.store.query, outside, inside=True) == []
    key = folders.exclusive_key(outside)
    assert daemon.store.acquire_lease(key, "retention:External")
    return turn, outside, key


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_an_unspelled_turns_look_reads_the_external_top_its_reservation_names(tmp_path, writable):
    """The review's probe (probe_turns.py:63): the look, called as the admission pass
    calls it (`_turn_folder`), holds the turn on the fence the reserving transaction
    finds on the external top. Failed on 2cbf6fca: `early_held` False."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        daemon.policy["caps"]["reading_ttl_s"] = 3600
        for lane in CODEX:
            measure(daemon, lane)
        turn, outside, key = external_unspelled_turn(daemon, harness, patch, writable)
        job = daemon._job(turn)
        holds, waiters = {}, {}
        early_held = daemon._fence_hold(
            job, daemon._turn_folder(job), holds, waiters, "standard", None, None,
            folders.canonical(job["workdir"]),
        )
        # Isolate the reserving transaction as the differential property does.
        patch.setattr(daemon, "_fence_hold", lambda *args, **kwargs: False)
        daemon._admit_turns()
        reservation = daemon._holds.get(turn) or {}
        assert not _live(daemon, turn)
        assert reservation.get("reason") == "lease-held", reservation
        assert reservation.get("leases") == [key], reservation
        assert reservation.get("folder") == outside, reservation
        assert early_held, {"early_held": early_held, "early_hold": holds,
                            "reservation": reservation, "submitted": daemon._submitted(turn)}


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_an_unspelled_turn_fenced_on_its_external_top_prepares_no_workspace(tmp_path, writable):
    """The review's probe (probe_turns.py:90): a stable fence on the external top spares
    the workspace snapshot. Failed on 2cbf6fca: `_workspace` ran once."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        daemon.policy["caps"]["reading_ttl_s"] = 3600
        for lane in CODEX:
            measure(daemon, lane)
        turn, outside, key = external_unspelled_turn(daemon, harness, patch, writable)
        real_workspace, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace",
                      lambda job: prepared.append(job["job_id"]) or real_workspace(job))
        daemon._admit_turns()
        hold = daemon._holds.get(turn) or {}
        assert not _live(daemon, turn)
        assert hold.get("reason") == "lease-held" and hold.get("leases") == [key], hold
        assert hold.get("folder") == outside, hold
        assert not prepared, {"prepared": prepared, "hold": hold,
                              "submitted": daemon._submitted(turn)}


def overtaken(daemon, harness, folder: str, tree: str, writable: bool) -> str:
    """A turn in `folder` whose submit retention overtook: validation saw the folder,
    then `tree`, the tree it is in, went into quarantine before submit spelled it
    (at submit's first git call on it), and came back (the retirement rolled back)
    before admission looks. Its row is `unspelled`."""
    aside = Path(tree).with_name(Path(tree).name + ".quarantine")
    real_head, moved = daemon_module.git_head, []

    def head_after_validation(workdir, *args, **kwargs):
        if not moved and str(workdir) == folder:
            Path(tree).rename(aside)
            moved.append(True)
        return real_head(workdir, *args, **kwargs)

    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(daemon_module, "git_head", head_after_validation)
        _, _, turn = message_in(daemon, harness, "Overtaken", workspace=folder,
                                settings={**SETTINGS, "permission": "accept-edits" if writable else "read-only"})
    assert moved, "the move did not happen inside submit"
    aside.rename(Path(tree))
    assert daemon._submitted(turn).get("unspelled") is True, daemon._submitted(turn)
    return turn


@pytest.mark.parametrize("where", ["tree", "nested", "external"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_finding_an_unspelled_turns_folder_again_writes_nothing_where_it_looks(tmp_path, writable, where):
    """What `_rediscovered` touches, measured: an `unspelled` turn, on a job's tree
    (a linked worktree), in a repository nested in it, or in one whose
    `core.worktree` names a top elsewhere, is held by retention's fence on that
    tree (on the external top, for `external`) with its workspace never prepared.
    The only git that ran is `git rev-parse --show-toplevel` (once, or twice when
    git finds no checkout under a `.git`), in the turn's own folder, and nothing in
    the tree, its nested `.git` (index included), the linked worktree's admin
    directory or the external top moved: contents, modes, mtimes and ctimes as
    before. Retention signs mtimes and ctimes (C-8.4); a read can move an atime,
    which it does not sign."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        daemon.policy["caps"]["reading_ttl_s"] = 3600
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        folder = tree if where == "tree" else nested
        roots = [Path(tree), Path(git(Path(tree), "rev-parse", "--absolute-git-dir"))]
        fence, named = folders.exclusive_key(tree), folder
        if where == "external":
            external = daemon.root / "worktrees" / "External"
            git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(external), "HEAD")
            git(Path(nested), "config", "core.worktree", str(external))
            roots += [external, Path(git(external, "rev-parse", "--absolute-git-dir"))]
            named = folders.canonical(external)
            fence = folders.exclusive_key(named)
        turn = overtaken(daemon, harness, folder, tree, writable)
        assert daemon.store.acquire_lease(fence, "retention:fixture")
        before = {str(root): (snapshot(root), ctimes(root)) for root in roots}
        prepare, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or prepare(job))
        spawned = git_spawns(patch)
        daemon._admit_turns()
        hold = dict(daemon._holds.get(turn) or {})
        hold.pop("next_check_at", None)
        assert not _live(daemon, turn) and prepared == [], (hold, prepared)
        assert hold == {"reason": "lease-held", "leases": [fence],
                        "retiring": [fence], "folder": named}, hold
        assert spawned and len(spawned) <= 2, spawned
        assert all(argv[argv.index("-C") + 2:] == ["rev-parse", "--show-toplevel"] for argv, _ in spawned), spawned
        assert {folders.canonical(at) for _, at in spawned} == {folder}, spawned
        assert {str(root): (snapshot(root), ctimes(root)) for root in roots} == before


@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "free"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_turn_whose_folder_cannot_be_found_again_still_waits_on_its_fence(tmp_path, writable, fenced):
    """C-6.8, C-8.4: `_rediscovered`'s git did not finish (its cap, here a timeout
    raised at once). A turn whose recorded folder is in a tree retention is retiring
    waits `lease-held` on that fence as it did before git was asked, its message's
    folder its cwd; with no fence it waits for its workspace (C-6.8), as when the
    workspace's own git did not finish. Neither prepares the workspace."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        daemon.policy["caps"]["reading_ttl_s"] = 3600
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        turn = overtaken(daemon, harness, nested, tree, writable)
        fence = folders.exclusive_key(tree)
        if fenced:
            assert daemon.store.acquire_lease(fence, "retention:retired")

        def stuck(workdir, *args, **kwargs):
            raise SalvageError("git rev-parse timed out after 60 s", timed_out=True)

        patch.setattr(daemon_module, "git_toplevel", stuck)
        prepare, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or prepare(job))
        daemon._admit_turns()
        job, hold = daemon._job(turn), dict(daemon._holds.get(turn) or {})
        assert not _live(daemon, turn) and prepared == [], (hold, prepared)
        if fenced:
            hold.pop("next_check_at")
            assert hold == {"reason": "lease-held", "leases": [fence],
                            "retiring": [fence], "folder": nested}, hold
            assert (job["state"], job["wait_reason"]) == ("waiting", "capacity"), dict(job)
        else:
            assert hold["reason"] == "workspace" and hold["error_type"] == "SalvageError", hold
            assert (job["state"], job["wait_reason"]) == ("waiting", "workspace"), dict(job)


@pytest.mark.parametrize("git_missing", [False, True])
def test_identical_paths_with_different_absence_flags_use_both_queries(git_missing):
    """The review's regression (probe_mutations.py:11): a move between filesystem
    spelling calls can leave identical strings but different absence flags. Only
    the absent lookup may fold the other case, so the two folders share no fence
    query. Killed the review's fourth mutant (one query for identical strings),
    which the generated property missed."""
    # A move between filesystem spelling calls can leave identical strings but
    # different absence flags. Only the absent lookup may fold the other case.
    folder, tree = "/Review/jOB", "/Review/Job"
    key = folders.exclusive_key(tree)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE leases(lease_key TEXT PRIMARY KEY, holder TEXT)")
        db.execute("INSERT INTO leases VALUES (?, ?)", (key, "retention:review"))
        def read(sql, params):
            return db.execute(sql, params).fetchall()

        absent = FileNotFoundError(errno.ENOENT, "moved between spelling calls", folder)
        fences, held_folder = Daemon._turn_fences(
            read, folder, absent if git_missing else None,
            folder, None if git_missing else absent,
        )
        assert fences == [key]
        assert held_folder == folder


def test_the_look_holds_on_retentions_fences_alone_where_the_transaction_names_every_blocker(tmp_path):
    """The review's probe (probe_contract.py:10), the limit C-8.4 states: what the
    look's hold shares with the reserving transaction's is retention's fences, the
    folder it names, the job's `waiting` row and clock. An ancestor retirement fence
    and an exact-folder detached writer both block this writable turn. The early
    look reports only the retirement fence; forced reservation reports both keys
    and uses a different capacity signature. Both behaviors, and the contract's
    wording before this round, already existed in release/217 at 4c6a8a9c9."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        daemon.policy["caps"]["reading_ttl_s"] = 3600
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        writer = folders.exclusive_key(nested)
        assert daemon.store.acquire_lease(fence, "retention:retired")
        assert daemon.store.acquire_lease(writer, "detached-review-writer")
        _, _, turn = message_in(
            daemon, harness, "Fence and detached writer", workspace=nested,
            settings={**SETTINGS, "permission": "accept-edits"},
        )

        job = daemon._job(turn)
        early_holds, waiters = {}, {}
        assert daemon._fence_hold(
            job, daemon._turn_folder(job), early_holds, waiters,
            "standard", None, None, folders.canonical(job["workdir"]),
        )
        early = dict(early_holds[turn])
        early.pop("next_check_at")
        early_signature = daemon._capacity_waits[turn]["signature"]
        assert early == {"reason": "lease-held", "leases": [fence],
                         "retiring": [fence], "folder": nested}
        assert early_signature == "lease-held:" + fence

        # Compare the same job and leases with the reserving transaction,
        # excluding the workspace snapshot as the generated differential does.
        daemon.store.update_job(turn, state="queued", wait_reason=None, next_check_at=None)
        daemon._capacity_waits.pop(turn)
        patch.setattr(daemon, "_fence_hold", lambda *args, **kwargs: False)
        patch.setattr(daemon, "_workspace", lambda current: (nested, None, None, []))
        daemon._admit_turns()
        reserved = dict(daemon._holds[turn])
        reserved.pop("next_check_at")
        reserved_signature = daemon._capacity_waits[turn]["signature"]

        assert not _live(daemon, turn)
        assert reserved == {
            "reason": "lease-held", "leases": [writer, fence],
            "retiring": [fence], "folder": nested,
        }
        assert reserved_signature == "lease-held:" + ",".join(sorted([writer, fence]))
        assert reserved != early and reserved_signature != early_signature
        current = daemon._job(turn)
        assert (current["state"], current["wait_reason"]) == ("waiting", "capacity")
