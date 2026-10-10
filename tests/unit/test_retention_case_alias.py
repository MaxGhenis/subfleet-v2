"""C-8.4, C-24.5, C-6.8: a turn row names its folder as the volume stores it.

A native session's recorded cwd may spell a job's tree in another case than its
volume stores (`…/worktrees/jOB/vendor/lib` for the tree `Job`). Submit spells a
turn's folder (`folders.canonical`), but a name it cannot look up stays as typed,
so a submission that retention overtook (strict validation saw the folder, then
the tree went into quarantine before the folder was spelled) recorded `jOB`.
Admission reserved its row on `jOB`; once the turn's job had ended (its attempt
quarantined, `lost`), neither the census, the selecting transaction nor the
commit matched the row, and the tree was pruned and reclaimed under it (review of
8a112986, finding 1; its probe, made the first test here).

Admission now spells the row's folder again right before reserving and reserves a
row only on a folder spelled in full (`folders.present`). A folder that is not
there reserves nothing: a turn waits `lease-held` on a retention fence its folder
is in, compared folded (`folders.retiring(..., folded=True)`), and its queued job
keeps the tree at the commit (`worktree-in-use`, which now compares the tree
itself without ASCII case too); with no fence it waits for its workspace (C-6.8)
and fails after `caps.workspace_retry_max` tries. Real submission, workspace
preparation, admission, `_quarantine` and archive driver; holder scanning is
stubbed and no provider is launched.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module, folders, procs, retention
from subfleet import retention_archive as rarch
from subfleet.adapters.base import AdapterError
from tests.unit.retention_world import World, git, snapshot
from tests.unit.test_retention_shared_folders import nested_repository, run


def tree_and_alias(daemon, harness, where: str) -> tuple[Path, str, Path, str]:
    """A finished job `Job` with its own worktree under the state root, as retention
    retires one, and the folder a conversation works in: the tree itself, or a
    repository nested in it on a task branch. Returns `(tree, folder, alias,
    recorded)`: the tree, the folder's one spelling, the folder spelled `jOB` as a
    native session's cwd may spell it, and that spelling as submit records it while
    the tree is away (the state root spelled, the rest as given)."""
    wt = daemon.root / "worktrees" / "Job"
    git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(wt), "HEAD")
    folder = wt
    if where == "nested":
        folder = wt / "vendor" / "lib"
        folder.mkdir(parents=True)
        git(folder, "init", "--quiet", "-b", "task/n")
        (folder / "f.txt").write_text("n\n")
        git(folder, "add", ".")
        git(folder, "commit", "--quiet", "-m", "n")
    alias = wt.with_name("jOB") / folder.relative_to(wt)
    if not alias.exists() or not alias.samefile(folder):
        pytest.skip("requires a case-insensitive volume")
    daemon.store.add_job(job_id="Job", request_id="Job", payload_digest="d", kind="dispatch",
                         workdir=str(harness.workdir), worktree=str(wt), prompt_path="/prompt",
                         sandbox="workspace-write", state="succeeded", workdir_head=git(wt, "rev-parse", "HEAD"))
    directory = daemon.root / "jobs" / "Job"
    directory.mkdir(parents=True)
    (directory / "stdout").write_text("host output")
    return wt, folders.canonical(folder), alias, os.path.join(folders.canonical(wt.parent), "jOB",
                                                              *folder.relative_to(wt).parts)


def submit_while_moved(daemon, harness, alias: Path, move, options) -> tuple[str, str]:
    """A message in a conversation whose recorded cwd is `alias`, its submission
    overtaken: strict validation sees the folder, then `move()` takes the tree away
    before submit spells the folder (at its first git call on it)."""
    from tests.fake.test_turn_wait_reasons import message_in

    real_head, moved, target = daemon_module.git_head, [], os.path.realpath(alias)

    def head_after_validation(workdir, *args, **kwargs):
        if not moved and str(workdir) == target:
            assert alias.is_dir()                   # strict validation has already passed
            move()
            moved.append(True)
            assert not alias.exists()
        return real_head(workdir, *args, **kwargs)

    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(daemon_module, "git_head", head_after_validation)
        _, mid, turn = message_in(daemon, harness, "Native alias", workspace=alias, settings=options)
    assert moved, "the move did not happen inside submit"
    recorded = daemon._submitted(turn)
    assert "/worktrees/jOB" in (recorded.get("write_target") or recorded.get("folder")), recorded
    return mid, turn


def turn_rows(daemon, turn: str) -> list[str]:
    return [row["lease_key"] for row in daemon.store.list_leases()
            if row["holder"] == turn and folders.parse(row["lease_key"])]


def lose(daemon, turn: str) -> None:
    """The turn's attempt quarantined (containment unverifiable): its job `lost`, its
    rows kept, as after a provider that could not be shown to have stopped."""
    attempt = daemon.store.list_attempts(turn)[-1]
    daemon._quarantine(attempt, procs.Containment(unverifiable=True, errors=("fixture containment",)),
                       "fixture: containment failed; no provider process launched")
    assert daemon.store.get_job(turn)["state"] == "lost"


def retire(daemon) -> dict:
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


@pytest.mark.parametrize("where", ["nested", "tree"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_turn_spelled_while_its_tree_is_in_quarantine_reserves_no_row(tmp_path, writable, where):
    """The probe of review finding 1, made a test. Submit spells the folder while
    retention has the tree in quarantine, so it records `jOB`. Admission reserves
    no row then: the turn waits `lease-held` on the tree's fence, found folded, and
    its message says retention is removing the tree (C-6.11). Its queued job keeps
    the tree at the commit (`worktree-in-use`), so the retirement is rolled back and
    the tree comes back as it was. The next pass reserves the row on the folder's
    one spelling, so when that turn's attempt is quarantined and its job is `lost`,
    the row still keeps the tree (`turn-folder`).

    Failed on 9e159ec9 and on 8a112986 (the nested probe, logs py31*-case-race.*):
    reserved on `jOB` under the fence, then `Job` pruned with the row live."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS, reason

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, folder, alias, recorded = tree_and_alias(daemon, harness, where)
        tree = folders.canonical(wt)
        before = snapshot(wt)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        real_quarantine, seen = rarch.Retirement.quarantine, {}

        def quarantine_during_submit(retirement):
            if retirement.job_id != "Job" or seen:
                return real_quarantine(retirement)
            mid, turn = submit_while_moved(daemon, harness, alias, lambda: real_quarantine(retirement), options)
            daemon._admit_turns()
            seen.update(mid=mid, turn=turn, live=_live(daemon, turn), rows=turn_rows(daemon, turn),
                        hold=dict(daemon._holds.get(turn) or {}), reason=reason(daemon, mid),
                        state=daemon.store.get_job(turn)["state"])
            assert retirement.state == "quarantined"

        patch.setattr(rarch.Retirement, "quarantine", quarantine_during_submit)
        result = retire(daemon)
        assert seen, result
        assert not seen["live"] and seen["rows"] == [], seen
        assert seen["state"] == "waiting", seen
        assert seen["hold"]["reason"] == "lease-held", seen
        assert seen["hold"]["leases"] == [folders.exclusive_key(tree)], seen
        assert seen["hold"]["folder"] == recorded, seen
        where_words = "that this folder is in" if where == "nested" else "in this folder"
        assert seen["reason"].startswith(f"lease: retention is removing a finished job's worktree {where_words}"), seen
        # The retirement is rolled back: the waiting turn's job keeps the tree.
        assert "Job" not in result["pruned"] and "Job" in result["protected"], result
        assert {p: e[:3] for p, e in snapshot(wt).items()} == {p: e[:3] for p, e in before.items()}
        assert daemon.store.get_job("Job")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:Job'") is None
        # Once the fence is gone the turn is reserved on the folder's one spelling.
        turn = seen["turn"]
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert turn_rows(daemon, turn) == [folders.turn_key(folder, turn, writable=writable)]
        # Its job ends with the row kept: the row still keeps the tree.
        lose(daemon, turn)
        assert retention._pin_reasons(daemon.store, set(), None, only="Job", root=daemon.root).get("Job") \
            == "turn-folder"
        result = retire(daemon)
        assert "Job" not in result["pruned"] and wt.is_dir(), result


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_folder_spelled_while_it_was_away_is_spelled_again_at_reservation(tmp_path, writable):
    """The spelling submit recorded is not the one a row is keyed on. The tree is
    held aside while submit spells the folder (`jOB` recorded) and is back before
    admission: the row is keyed on the folder's one spelling, `Job`, so after the
    turn's job ends retention still finds it and keeps the tree. Without spelling
    again the row was `jOB`, and the tree was pruned under it."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, folder, alias, recorded = tree_and_alias(daemon, harness, "nested")
        aside = wt.with_name(".Job.aside")
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        _, turn = submit_while_moved(daemon, harness, alias, lambda: wt.rename(aside), options)
        aside.rename(wt)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert turn_rows(daemon, turn) == [folders.turn_key(folder, turn, writable=writable)]
        lose(daemon, turn)
        result = retire(daemon)
        assert "Job" not in result["pruned"] and wt.is_dir(), result
        assert retention._pin_reasons(daemon.store, set(), None, only="Job", root=daemon.root).get("Job") \
            == "turn-folder"


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
@pytest.mark.parametrize("outcome", ["back", "gone"])
def test_a_folder_that_is_not_there_without_a_retirement_waits_for_its_workspace(tmp_path, writable, outcome):
    """No retention fence explains a folder that is not there (another tool holds
    the tree aside, or it was removed): the turn reserves nothing and waits for its
    workspace (C-6.8, `wait_reason` `workspace`), its message naming the folder.
    When the tree is back the next look reserves the row on the folder's one
    spelling; when it never comes back the turn fails after
    `caps.workspace_retry_max` tries, with no row ever taken. Before, it was
    reserved on `jOB` at the first look."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS, reason

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, folder, alias, recorded = tree_and_alias(daemon, harness, "nested")
        aside = wt.with_name(".Job.aside")
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        mid, turn = submit_while_moved(daemon, harness, alias, lambda: wt.rename(aside), options)
        patch.setitem(daemon.policy["caps"], "workspace_retry_max", 1)
        daemon._admit_turns()
        job, hold = daemon.store.get_job(turn), daemon._holds.get(turn) or {}
        assert not _live(daemon, turn) and turn_rows(daemon, turn) == [], hold
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace"), job
        assert hold["reason"] == "workspace" and hold["error_type"] == "FileNotFoundError", hold
        assert f"its folder {recorded} is not there now" in hold["error"], hold
        assert reason(daemon, mid).startswith("workspace: its folder could not be prepared (FileNotFoundError: "
                                              f"its folder {recorded} is not there now"), reason(daemon, mid)
        if outcome == "back":
            aside.rename(wt)
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        if outcome == "back":
            assert _live(daemon, turn), daemon._holds.get(turn)
            assert turn_rows(daemon, turn) == [folders.turn_key(folder, turn, writable=writable)]
        else:
            job = daemon.store.get_job(turn)
            assert job["state"] == "failed" and turn_rows(daemon, turn) == [], job
            assert daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (turn,)) is None
            # Its workspace was never ready: no look said so.
            assert daemon.store.one("SELECT 1 FROM events WHERE job_id=? AND kind='job.workspace_ready'",
                                    (turn,)) is None


@pytest.mark.parametrize("outcome", ["back", "away"])
def test_an_in_place_writer_keys_its_lease_on_its_folders_one_spelling(tmp_path, outcome):
    """C-6.5, C-8.4: a detached writer in place holds `worktree:<its checkout>`,
    spelled at submit like a turn's folder, so the same overtaken submission
    recorded `jOB`. Its lease is keyed on the checkout's one spelling, spelled again
    at reservation; while the checkout is away it takes no lease and waits for its
    workspace (C-6.8). Before, the lease was `worktree:…/jOB/vendor/lib`."""
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, folder, alias, recorded = tree_and_alias(daemon, harness, "nested")
        aside, target = wt.with_name(".Job.aside"), os.path.realpath(alias)
        real_top, moved = daemon_module.git_toplevel, []

        def top_after_validation(workdir, *args, **kwargs):
            if not moved and str(workdir) == target:
                wt.rename(aside)
                moved.append(True)
            return real_top(workdir, *args, **kwargs)

        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(daemon_module, "git_toplevel", top_after_validation)
            writer = submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(alias))
        assert moved and daemon._submitted(writer)["write_target"] == recorded
        if outcome == "back":
            aside.rename(wt)
        daemon._admit()
        held = [row["lease_key"] for row in daemon.store.list_leases()
                if row["holder"] == writer and row["lease_key"].startswith(folders.EXCLUSIVE)]
        if outcome == "back":
            assert _live(daemon, writer) and held == [folders.exclusive_key(folder)], (held, daemon._holds.get(writer))
        else:
            job = daemon.store.get_job(writer)
            assert not _live(daemon, writer) and held == [], held
            assert (job["state"], job["wait_reason"]) == ("waiting", "workspace"), job


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_folder_recorded_below_its_checkout_top_while_away_is_found_again(tmp_path, writable):
    """C-6.5, C-24.5: a turn's row is on its checkout's top level. A conversation in
    a plain subdirectory of the tree (`jOB/src`) submitted while the tree was away
    had git find no checkout, so submit recorded the subdirectory, marked
    `unspelled`. Admission finds the folder again as submit would, once the tree is
    back: the row is on `Job`, and a detached writer in place in that checkout is
    refused while the writable turn writes there (C-6.5). Spelling the recorded
    subdirectory again gave `Job/src`, and the writer ran beside the turn (review
    of 3410b4f0, probe P1)."""
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, _, _, _ = tree_and_alias(daemon, harness, "tree")
        (wt / "src").mkdir()
        (wt / "src" / "a.txt").write_text("a\n")
        aside, top = wt.with_name(".Job.aside"), folders.canonical(wt)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        _, turn = submit_while_moved(daemon, harness, wt.with_name("jOB") / "src", lambda: wt.rename(aside), options)
        recorded = daemon._submitted(turn)
        assert recorded.get("unspelled") is True and (recorded.get("write_target") or recorded.get("folder")).endswith(
            "/jOB/src"), recorded
        aside.rename(wt)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert set(turn_rows(daemon, turn)) == {folders.turn_key(top, turn, writable=writable),
                                              folders.turn_key(top + "/src", turn, writable=False)}
        if writable:
            # C-6.5: refused, as a second writer in a checkout a turn writes in is.
            with pytest.raises(AdapterError, match="is being written by a conversation turn"):
                submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(wt))
        else:
            writer = submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(wt))
            daemon._admit()
            assert _live(daemon, writer), daemon._holds.get(writer)       # a reader excludes no writer


@pytest.mark.parametrize("window", ["git", "git-and-spelling"])
@pytest.mark.parametrize("typed", ["Job", "jOB"])
def test_a_tree_away_only_while_git_looks_is_found_again_from_its_checkout_top(tmp_path, window, typed):
    """C-6.5: the narrower windows of the same race (review of fb674463, Q1 and Q1b).
    With the tree away only for git's look (`git`), git finds no checkout and the
    subdirectory is spelled in full; with it away for git's look and back only for
    the spelling submit records (`git-and-spelling`). Either way submit marks the
    folder `unspelled`: git found no checkout while a `.git` is above the folder
    (`folders.under_git`), and the mark comes from the same spelling it records.
    Admission finds the checkout's top, so a detached writer in place there is
    refused. Before, the row was on `Job/src` and the writer ran beside the turn."""
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS, message_in

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, _, _, _ = tree_and_alias(daemon, harness, "tree")
        (wt / "src").mkdir()
        (wt / "src" / "a.txt").write_text("a\n")
        folder = wt.with_name(typed) / "src"
        aside, target, top = wt.with_name(".Job.aside"), os.path.realpath(folder), folders.canonical(wt)
        real_top, real_present, seen = daemon_module.git_toplevel, folders.present, []

        def top_while_away(workdir, *args, **kwargs):
            if seen or str(workdir) != target:
                return real_top(workdir, *args, **kwargs)
            wt.rename(aside)                         # retention's quarantine...
            try:
                seen.append(real_top(workdir, *args, **kwargs))
            finally:
                if window == "git":
                    aside.rename(wt)                 # ...and its rollback, around git's look only
            return seen[-1]

        def present_after_return(path):
            if window == "git-and-spelling" and aside.exists():
                aside.rename(wt)                     # back only for the spelling submit records
            return real_present(path)

        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(daemon_module, "git_toplevel", top_while_away)
            inner.setattr(folders, "present", present_after_return)
            _, _, turn = message_in(daemon, harness, "Overtaken", workspace=folder,
                                    settings={**SETTINGS, "permission": "accept-edits"})
        assert seen == [None] and wt.is_dir()
        recorded = daemon._submitted(turn)
        assert recorded.get("unspelled") is True and recorded["write_target"].endswith("/src"), recorded
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert set(turn_rows(daemon, turn)) == {folders.turn_key(top, turn, writable=True),
                                              folders.turn_key(top + "/src", turn, writable=False)}
        with pytest.raises(AdapterError, match="is being written by a conversation turn"):
            submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(wt))


def test_an_absent_in_place_writer_names_retentions_fence_once(tmp_path):
    """A detached writer in place on the tree itself, its tree in quarantine: the
    fence is its own key, contested, and is not counted again as one found folded
    (review of 3410b4f0, probe P2: listed twice)."""
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, _, _, _ = tree_and_alias(daemon, harness, "tree")
        tree = folders.canonical(wt)
        writer = submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(wt))
        assert daemon._submitted(writer)["write_target"] == tree
        with daemon.store.transaction() as tx:      # retention's fence, then its quarantine
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (folders.exclusive_key(tree), "retention:Job", "2026-10-05T00:00:00Z"))
        wt.rename(wt.with_name(".Job.quarantined"))
        daemon._admit()
        hold = daemon._holds.get(writer) or {}
        assert not _live(daemon, writer) and hold.get("reason") == "lease-held", hold
        assert hold["leases"] == [folders.exclusive_key(tree)], hold


# Both sides of C-8.4 for a folder that could not be spelled: the turn waits on a
# fence (`retiring`, folded) exactly when its queued job keeps the tree at the
# commit (`worktree-in-use`'s comparison, in SQLite). ASCII, as SQLite's NOCASE and
# LIKE fold only ASCII; `%` and `_`, LIKE's wildcards, are left out (with them LIKE
# keeps more than the fence holds, which only keeps a tree longer).
NAME = st.text(st.sampled_from("abJjOoBb.-:"), min_size=1, max_size=4).filter(lambda name: name not in (".", ".."))


@st.composite
def tree_and_folder(draw):
    """A tree under `/s/worktrees`, and a folder on it, inside it, beside it, whose
    name extends its own, or above it, with the names below `/s/worktrees` in any
    case."""
    parts = draw(st.lists(NAME, min_size=1, max_size=3))
    shape = draw(st.sampled_from(["tree", "inside", "beside", "extends", "above"]))
    rel = {"tree": parts, "inside": parts + draw(st.lists(NAME, min_size=1, max_size=2)),
           "beside": parts[:-1] + [draw(NAME)], "extends": parts[:-1] + [parts[-1] + draw(NAME)],
           "above": parts[:-1]}[shape]
    swap = lambda name: "".join(c.swapcase() if draw(st.booleans()) else c for c in name)   # noqa: E731
    return "/".join(["/s/worktrees", *parts]), "/".join(["/s/worktrees", *map(swap, rel)])


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@example(("/s/worktrees/Job", "/s/worktrees/jOB"))
@example(("/s/worktrees/Job", "/s/worktrees/jOB/vendor/lib"))
@example(("/s/worktrees/Job", "/s/worktrees/jOB2"))
@given(tree_and_folder())
def test_a_folded_fence_holds_exactly_the_turns_whose_job_keeps_the_tree(case):
    tree, folder = case
    fenced = bool(folders.retiring(lambda sql, params: [("worktree:" + tree, "retention:Job")]
                                   if params[0] <= "worktree:" + tree < params[1] else [], folder, folded=True))
    with sqlite3.connect(":memory:") as db:
        kept = db.execute("SELECT ? = ? COLLATE NOCASE OR ? LIKE ? || '/%'", (folder, tree, folder, tree)).fetchone()[0]
    assert fenced == bool(kept) == folders.within(folder.lower(), tree.lower()), case


@pytest.fixture
def daemon_store(tmp_path):
    """The census's `worktree-in-use` query against real store rows."""
    from subfleet.store import Store
    store = Store(tmp_path / "state" / "subfleet.db")
    yield store
    store.close()


@pytest.mark.parametrize("spelled", ["Job", "jOB", "JOB", "jOB/vendor/lib", "jOB2", "jO"])
@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "unrecorded"])
@pytest.mark.parametrize("column", ["workdir", "worktree"])
def test_worktree_in_use_keeps_a_tree_a_queued_job_names_in_another_case(tmp_path, daemon_store, spelled, recorded,
                                                                          column):
    """`worktree-in-use` (#76) keeps a finished job's tree while a job not yet ended
    works in it or inside it. Inside it was compared with LIKE, which ignores ASCII
    case, but the tree itself with `=`, which does not: a queued turn whose folder
    was the tree itself, spelled `jOB`, kept nothing. Both now ignore ASCII case,
    for a recorded tree and for one `_workspace` allocated that no row names yet,
    whether the job names it as its directory or as its worktree."""
    root = tmp_path / "state"
    tree = str(root / "worktrees" / "Job")
    daemon_store.add_job(job_id="Job", request_id="Job", payload_digest="d", kind="dispatch", workdir="/repo",
                         worktree=tree if recorded else None, prompt_path="/p", sandbox="workspace-write",
                         state="succeeded")
    named = str(root / "worktrees" / spelled)
    daemon_store.add_job(job_id="turn", request_id="turn", payload_digest="d", kind="turn",
                         workdir=named if column == "workdir" else "/elsewhere", prompt_path="/p",
                         sandbox="read-only", state="queued", **({"worktree": named} if column == "worktree" else {}))
    reason = retention._pin_reasons(daemon_store, set(), None, only="Job", root=root).get("Job")
    inside = spelled.lower() == "job" or spelled.lower().startswith("job/")
    assert reason == ("worktree-in-use" if inside else None), (spelled, reason)


def folded_rows(store, tree: str) -> list[str]:
    """Every live TURN or READER row whose folder is `tree` or inside it, compared
    as the volume compares names (`folders.fold`): the oracle, blind to spelling."""
    return [key for key, *_ in ((row["lease_key"],) for row in store.list_leases())
            if (parsed := folders.parse(key)) and folders.within(folders.fold(parsed[1]), folders.fold(tree))]


alias_phase = st.sampled_from(["select", "begin", "lock", "archive", "verify", "delete"])
alias_operation = st.tuples(alias_phase, st.sampled_from(["start", "end"]), st.booleans(),
                            st.sampled_from(["tree", "nested"]), st.sampled_from(["canonical", "alias"]))


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@example([("archive", "start", True, "nested", "alias")])
@example([("verify", "start", False, "tree", "alias")])
@example([("lock", "start", True, "nested", "alias"), ("delete", "start", False, "nested", "alias")])
@example([("select", "start", False, "nested", "alias"), ("archive", "end", False, "nested", "alias")])
@given(st.lists(alias_operation, min_size=0, max_size=20))
def test_interleavings_with_case_aliases_never_delete_a_folder_a_row_names(schedule):
    """Starts and ends of turns at each real archive-driver boundary, on the tree and
    in a repository nested in it, each spelled as the volume stores it or in
    another case (`JOB` for `job`). The turn actor follows admission's rule with
    the real building blocks, atomically in a store transaction: spell the folder
    again (`folders.present`); spelled in full, reserve a row on that spelling
    unless retention's fence is on it or above it (`folders.retiring`); not there,
    reserve nothing. Before the quarantine and before the verified reclaim the
    oracle reads every row and forbids a move or delete while any row names the
    tree or a folder inside it, compared folded, whatever its spelling; at the end
    a tree a row names is there and its job kept. Keyed on the alias while the tree
    was in quarantine (the base's rule), a row slipped past every check."""
    with tempfile.TemporaryDirectory(prefix="retention-alias-") as temporary, pytest.MonkeyPatch.context() as patch:
        w = World(Path(temporary))
        try:
            wt = w.job("job")
            if not wt.with_name("JOB").exists() or not wt.with_name("JOB").samefile(wt):
                pytest.skip("requires a case-insensitive volume")
            tree = folders.canonical(wt)
            places = {"tree": tree}
            if any(where == "nested" for *_, where, _ in schedule):
                places["nested"] = nested_repository(wt)
            typed = {(where, how): (spot if how == "canonical" else spot.replace("/worktrees/job", "/worktrees/JOB", 1))
                     for where, spot in places.items() for how in ("canonical", "alias")}
            executed = set()

            def step(at):
                if at in executed:
                    return
                executed.add(at)
                for when, action, writable, where, how in schedule:
                    if when != at:
                        continue
                    holder = f"{'writer' if writable else 'reader'}-{where}-{how}"
                    if action == "end":
                        w.store.release_leases(holder)
                        continue
                    spelled, missing = folders.present(typed[(where, how)])
                    with w.store.transaction() as conn:
                        read = lambda sql, params: conn.execute(sql, params).fetchall()   # noqa: E731
                        if missing is None and not folders.retiring(read, spelled):
                            conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                         (folders.turn_key(spelled, holder, writable=writable), holder, "now"))

            def oracle():
                assert folded_rows(w.store, tree) == [], schedule

            def wrap(cls, method, at, check=False):
                real = getattr(cls, method)

                def wrapped(self, *args, **kwargs):
                    step(at)
                    if check:
                        oracle()
                    return real(self, *args, **kwargs)
                patch.setattr(cls, method, wrapped)

            wrap(retention._Pass, "_start", "begin")
            wrap(rarch.Retirement, "lock", "lock")
            wrap(rarch.Retirement, "archive", "archive")
            wrap(rarch.Retirement, "final_check", "verify")
            wrap(rarch.Retirement, "quarantine", "quarantine", check=True)
            wrap(rarch.Retirement, "reclaim", "delete", check=True)
            step("select")
            result = run(w)
            if folded_rows(w.store, tree):
                assert wt.is_dir() and w.store.get_job("job"), (schedule, result)
                assert "job" in result["protected"], (schedule, result)
            elif "job" in result["pruned"]:
                assert not wt.exists() and not w.admin("job").exists()
        finally:
            w.close()
