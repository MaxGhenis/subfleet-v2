"""C-8.4: retention keeps a finished job's tree while another job needs a place in it,
and admission starts no job while retention retires a tree a place it needs is in.

A job needs its folder, and may need more (`dependencies`): an isolated review's
root (`-I -D`), its `-o` path, and the git storage its folder's checkout uses. The
pin side (`worktree-in-use`) compared only a job's `workdir` and `worktree` with the
tree, as raw strings, and admission read retention's fence only on the job's
folder. Four gaps, each executed in the evidence of the detached-job fence
(0a91f070):

1. an isolated review queued with `-D` in a tree: the tree was pruned while it
   was queued;
2. a job whose workdir is the tree typed in another case (APFS folds case, and
   submit's `Path.resolve` keeps the typed case): `=` compared case;
3. a checkout outside the tree whose repository is in it (`git -C
   <tree>/vendor/lib worktree add <outside>`): read-only jobs, worktree writers
   and writers in place submitted from `<outside>` after selection were reserved,
   and the repository was pruned while their attempts were live;
4. a job with its cwd outside the tree exporting into it (`-o`): admitted and
   exported under the fence, then the export went with the tree.

Submit now records the review root, the `-o` path and the git storage, spelled
once, as `depends` in `job.submitted`. Retention keeps a tree any of them (or the
job's folder or workdir) is in while their job has not ended, and an `-o` path
while its export is pending, comparing paths folded; the selecting and committing
transactions read the same. Admission reads retention's fence on each place too,
before preparing the workspace and in the reserving transaction. These run the
daemon's own submit and admission in-process against the archive driver's own
retirement; none imports anything this change added, so each runs (and fails) on
the base.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from subfleet import folders, render, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _end, _live
from tests.fake.test_detached_retention_fence import Workspaces
from tests.fake.test_writer_retention_fence import lease, look, measured
from tests.unit.retention_world import git, snapshot
from tests.unit.test_retention_shared_folders import retiring_tree

#: How each job shape is submitted (`subfleet run`): read-only, a writer whose
#: worktree the daemon cuts (C-6.6), and a writer in place.
SHAPES = {"read-only": {"sandbox": "read-only"}, "worktree": {"sandbox": "workspace-write"},
          "in-place": {"sandbox": "workspace-write", "in_place": True}}
#: What `why` calls each place a job needs besides its folder.
WORDS = {"review-root": "review root (-D)", "output": "output path (-o)", "git-storage": "git storage"}


def retire(daemon) -> dict:
    """One retention pass with the daemon's budgets at zero: every finished job is over."""
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})


def external_checkout(tmp_path: Path, nested: str) -> str:
    """A linked checkout of the repository nested in the tree, made outside the tree:
    its `.git` names `<nested>/.git/worktrees/external`, so its repository is in the tree."""
    outside = tmp_path / "external"
    git(nested, "worktree", "add", "--quiet", "--detach", str(outside), "HEAD")
    return folders.canonical(outside)


def registrations(nested: str) -> set[str]:
    admin = Path(nested) / ".git" / "worktrees"
    return {each.name for each in admin.iterdir()} if admin.is_dir() else set()


def needing(daemon, harness, tmp_path: Path, tree: str, nested: str, kind: str, shape: str = "read-only",
            **changes) -> tuple[str, str, str | None]:
    """A job of `shape` that needs a place in `tree` as `kind` says, submitted as
    `subfleet run` submits it: `(job id, the place, its role in a hold)`."""
    if kind == "review-root":
        neutral = tmp_path / "neutral"
        neutral.mkdir(exist_ok=True)
        job = submit(daemon, harness, workdir=str(neutral), sandbox="read-only", isolated_review=True,
                     review_root=nested, **changes)
        return job, nested, "review-root"
    if kind == "output":
        out = str(Path(nested) / "result.md")
        job = submit(daemon, harness, workdir=str(harness.workdir), out_path=out, **SHAPES[shape], **changes)
        return job, folders.canonical(out), "output"
    if kind == "linked":
        outside = folders.canonical(tmp_path / "external")
        job = submit(daemon, harness, workdir=outside, **SHAPES[shape], **changes)
        return job, str(Path(nested) / ".git" / "worktrees" / "external"), "git-storage"
    if kind == "case":
        typed = str(Path(tree).parent.parent / "WORKTREES" / Path(tree).name.upper())
        if not Path(typed).is_dir() or not Path(typed).samefile(tree):
            pytest.skip("this volume tells case apart")
        job = submit(daemon, harness, workdir=typed, **SHAPES[shape], **changes)
        return job, tree, None
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["review-root", "case", "linked", "output"])
def test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends(tmp_path, kind):
    """Gaps 1 to 4, the pin side: a job queued before the pass that needs a place in a
    finished job's tree (its review root, its folder typed in another case, the
    repository its outside checkout uses, its `-o` path) keeps the tree whole:
    `worktree-in-use`, nothing moved. Once the job ends the next pass retires it.
    Failed on 0a91f070: `pruned ['retired']` with the job still queued."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        if kind == "linked":
            external_checkout(tmp_path, nested)
        job, place, role = needing(daemon, harness, tmp_path, tree, nested, kind)
        before = snapshot(Path(tree))
        result = retire(daemon)
        assert "retired" not in result["pruned"] and result["pin_reasons"].get("retired") == "worktree-in-use", result
        assert snapshot(Path(tree)) == before and daemon.store.get_job("retired")
        assert daemon._job(job)["state"] in ("queued", "waiting")
        if role is not None:
            needs = daemon._submitted(job)["depends"]
            recorded = needs.get({"review-root": "review_root", "output": "out_path"}.get(role, "")) or needs["git"]
            assert place == recorded or place in recorded, needs
        _end(daemon, job)
        result = retire(daemon)
        assert "retired" in result["pruned"], result         # the job, ended, may go too
        assert not Path(tree).exists()


@pytest.mark.parametrize("kind, shape", [("review-root", "read-only"), ("linked", "read-only"),
                                         ("linked", "worktree"), ("linked", "in-place"), ("output", "read-only")])
def test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired(tmp_path, kind, shape):
    """Gaps 1, 3 and 4, the admission side. While retention holds `worktree:<tree>`
    (after the selecting transaction, before `begin` reads or moves the tree), a job
    is submitted that needs a place in the tree but works outside it. Admission holds
    it `lease-held` on the fence before preparing its workspace: no git runs for it,
    and a worktree writer's worktree is not registered in the repository in the tree.
    The hold names the place and what the job needs it as (`needs`), and `why` says
    retention is removing the tree that place is in. Once the retirement lets go, the
    next pass places it (C-6.10), and only then is a writer's worktree cut.
    Failed on 0a91f070: reserved under the fence (`live`, no hold)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        if kind == "linked":
            external_checkout(tmp_path, nested)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=True)
        registered = registrations(nested)
        seen = {}

        def begin(retirement, job, pool):
            seen["fence"] = lease(daemon, fence)
            seen["job"], seen["place"], seen["role"] = job_id, _, _ = needing(daemon, harness, tmp_path, tree,
                                                                              nested, kind, shape)
            daemon._admit()
            seen.update(live=_live(daemon, job_id), hold=dict(daemon._holds.get(job_id) or {}),
                        prepared=list(workspaces.calls), registered=registrations(nested),
                        row=dict(daemon._job(job_id)), why=daemon._why_job(daemon._job(job_id))["text"])
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retire(daemon)
        job_id, place, role = seen["job"], seen["place"], seen["role"]
        assert seen["fence"] == "retention:retired", seen
        assert not seen["live"] and seen["prepared"] == [] and seen["registered"] == registered, seen
        assert seen["hold"] == {"reason": "lease-held", "leases": [fence], "retiring": [fence], "folder": place,
                                "needs": role, "next_check_at": seen["row"]["next_check_at"]}, seen
        assert seen["row"]["state"] == "waiting", seen["row"]
        assert (f"Held: retention is removing a finished job's worktree that its {WORDS[role]} {place} is in "
                f"({tree}); it waits until retention lets go of it (C-8.4)") in seen["why"], seen["why"]
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()                                 # the fence went: freed capacity, looked at now
        assert _live(daemon, job_id), daemon._holds.get(job_id)
        assert workspaces.calls == [job_id]
        if kind == "linked" and shape == "worktree":
            assert registrations(nested) == registered | {job_id}      # cut once the fence went (N4)
        _end(daemon, job_id)


@pytest.mark.parametrize("kind, shape", [("review-root", "read-only"), ("linked", "read-only"),
                                         ("linked", "worktree"), ("linked", "in-place"), ("output", "read-only")])
def test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction(
        tmp_path, kind, shape):
    """The reserving transaction's own read decides. A fence that lands after the look
    before workspace preparation, on a tree a place the job needs is in, holds the job
    in the transaction with the hold the early look records: the same keys,
    `retiring`, the place and its role. The next look holds it before its workspace is
    prepared.
    Failed on 0a91f070: reserved under the fence."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        if kind == "linked":
            external_checkout(tmp_path, nested)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=False)
        fake = workspaces.__call__

        def selection_lands(job):
            assert daemon.store.acquire_lease(fence, "retention:retired")    # between the two reads
            return fake(job)

        patch.setattr(daemon, "_workspace", selection_lands)
        job_id, place, role = needing(daemon, harness, tmp_path, tree, nested, kind, shape)
        late = look(daemon, job_id)
        assert not _live(daemon, job_id), late
        expected = {"reason": "lease-held", "leases": [fence], "retiring": [fence], "folder": place, "needs": role}
        assert {key: late.get(key) for key in expected} == expected, late
        assert set(late) <= {*expected, "next_check_at"}, late
        patch.setattr(daemon, "_workspace", workspaces)
        assert workspaces.calls == [job_id]                 # prepared once, before the fence landed
        del workspaces.calls[:]
        early = look(daemon, job_id)
        assert {key: early.get(key) for key in expected} == expected and workspaces.calls == [], early
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)
        _end(daemon, job_id)


def test_c8_4_a_fence_on_an_in_place_writers_own_folder_that_its_output_is_in_is_named_once(tmp_path):
    """An in-place writer's own `worktree:` key is a lease it takes, contested, not a
    key it only needs free: a fence on its folder, which its `-o` path is in too, lands
    after the early look and is named once in its hold, with its write target as its
    folder and no other place."""
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        own = folders.exclusive_key(nested)
        workspaces = Workspaces(daemon, patch, real=False)

        def lands(job):
            assert daemon.store.acquire_lease(own, "retention:other")
            return workspaces(job)

        writer = writer_in(daemon, harness, nested, out_path=str(Path(nested) / "out.md"))
        patch.setattr(daemon, "_workspace", lands)
        hold = look(daemon, writer)
        assert not _live(daemon, writer)
        assert {key: hold.get(key) for key in ("leases", "retiring", "folder", "needs")} == {
            "leases": [own], "retiring": [own], "folder": nested, "needs": None}, hold


@pytest.mark.parametrize("kind", ["review-root", "linked", "output"])
def test_c8_4_the_commit_keeps_a_tree_a_job_submitted_after_selection_needs(tmp_path, kind):
    """The commit recheck, end to end: a job that needs a place in the tree but works
    outside it, submitted after selection, is looked at at each step of the
    retirement (begin, lock, quarantine, archive, the final check), its place moved
    away from the quarantine on, and is never reserved or prepared. Its queued job
    keeps the tree at the commit (`worktree-in-use`, read inside the commit
    transaction from what submit recorded), so the retirement is rolled back, the
    tree comes back exactly as it was, the fence goes, and the job runs.
    Failed on 0a91f070: reserved at the first look, and the tree pruned."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        if kind == "linked":
            external_checkout(tmp_path, nested)
        fence = folders.exclusive_key(tree)
        before = snapshot(Path(tree))
        workspaces = Workspaces(daemon, patch, real=True)
        state = {"looked": []}

        def step(at):
            if "job" not in state:
                state["job"], state["place"], _ = needing(daemon, harness, tmp_path, tree, nested, kind)
            hold = look(daemon, state["job"])
            assert not _live(daemon, state["job"]) and workspaces.calls == [], (at, hold)
            assert hold.get("reason") == "lease-held" and hold["leases"] == hold["retiring"] == [fence], (at, hold)
            state["looked"].append((at, Path(tree).is_dir()))

        for method in ("begin", "lock", "quarantine", "archive", "final_check", "reclaim"):
            def wrapped(self, *args, _real=getattr(rarch.Retirement, method), _at=method, **kwargs):
                step(_at)
                return _real(self, *args, **kwargs)
            patch.setattr(rarch.Retirement, method, wrapped)
        result = retire(daemon)
        assert list(dict.fromkeys(state["looked"])) == [
            ("begin", True), ("lock", True), ("quarantine", True), ("archive", False), ("final_check", False)], state
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert "pinned: worktree-in-use" in result["deferred"].get("retired", ""), result["deferred"]
        assert snapshot(Path(tree)) == before and daemon.store.get_job("retired")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()
        assert _live(daemon, state["job"]), daemon._holds.get(state["job"])
        assert workspaces.calls == [state["job"]]
        _end(daemon, state["job"])


def test_c8_4_a_pending_export_keeps_the_tree_and_a_written_one_goes_into_the_archive(tmp_path):
    """Gap 4, after the job ends. A job's `-o` export is written after its job is
    terminal (`_export`, after the transaction that ends it, and again on recovery),
    while the job still holds `out:<path>`. That pending export keeps a tree its path
    is in. Once written, the export owes the tree nothing (C-8.4): retention may
    archive the tree, and the exported file goes into the archive with it, byte for
    byte, while the job's own deliverable stays in its job directory.
    Failed on 0a91f070: the tree pruned while the export was pending."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        out = Path(nested) / "result.md"
        job = submit(daemon, harness, workdir=str(harness.workdir), out_path=str(out))
        daemon._admit()
        assert _live(daemon, job), daemon._holds.get(job)
        attempt = daemon.store.list_attempts(job)[-1]["attempt_id"]
        deliverable = daemon.root / "jobs" / job / "deliverable.md"
        contents = b"the accepted deliverable\n"
        deliverable.write_bytes(contents)
        daemon.store.add_artifact(attempt, role="deliverable", path=str(deliverable),
                                  sha256=hashlib.sha256(contents).hexdigest(), bytes=len(contents))
        daemon.store.update_attempt(attempt, state="succeeded")
        daemon.store.update_job(job, state="succeeded", accepted_attempt_id=attempt)
        assert lease(daemon, f"out:{out}") == job              # ended, its export not written yet
        result = retire(daemon)
        assert "retired" not in result["pruned"] and result["pin_reasons"].get("retired") == "worktree-in-use", result
        assert Path(tree).is_dir() and not out.exists()
        daemon._export(job)
        assert out.read_bytes() == contents and daemon._job(job)["export_error"] is None
        assert lease(daemon, f"out:{out}") is None
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, referenced_job_ids=[job],
                                       holders=lambda watches, **_: {})
        assert result["pruned"] == ["retired"], result
        assert not out.exists() and deliverable.read_bytes() == contents
        manifest = json.loads((daemon.root / "archive" / "retired" / "manifest.json").read_text())
        entries = {entry["p"]: entry for entry in manifest["trees"]["worktree"]["entries"]}
        assert "vendor/lib/result.md" in entries, sorted(entries)


@pytest.mark.parametrize("kind", ["linked", "case"])
def test_c8_4_a_job_queued_before_dependencies_were_recorded_keeps_the_tree(tmp_path, kind):
    """A job a daemon queued before submit recorded `depends` (or its folder): its
    places are read for it once a pass, outside any transaction (the git storage its
    outside checkout uses), and its row's workdir compares folded (typed in another
    case). Either keeps the tree while the job has not ended.
    Failed on 0a91f070: pruned with the job queued."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        if kind == "linked":
            external_checkout(tmp_path, nested)
        job, _, _ = needing(daemon, harness, tmp_path, tree, nested, kind)
        legacy(daemon, job)
        result = retire(daemon)
        assert "retired" not in result["pruned"] and result["pin_reasons"].get("retired") == "worktree-in-use", result
        _end(daemon, job)
        assert "retired" in retire(daemon)["pruned"]


def legacy(daemon, job_id: str) -> None:
    """Make `job_id` a job an older daemon queued: its `job.submitted` without
    `depends` and without the folder (`folder`, `write_target`)."""
    with daemon.store.transaction("test.legacy") as tx:
        row = tx.execute("SELECT event_id, data_json FROM events WHERE job_id=? AND kind='job.submitted'",
                         (job_id,)).fetchone()
        data = {key: value for key, value in json.loads(row[1] or "{}").items()
                if key not in ("depends", "folder", "write_target")}
        tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), row[0]))


def test_c8_4_a_legacy_job_submitted_under_the_fence_waits_on_its_git_storage(tmp_path):
    """Admission reads the places of a job queued before they were recorded when it
    looks at the job (outside any transaction): a read-only job in a checkout outside
    the tree whose repository is in it waits on the fence, named as its git storage.
    Failed on 0a91f070: reserved under the fence."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        external_checkout(tmp_path, nested)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=True)
        seen = {}

        def begin(retirement, job, pool):
            seen["job"], seen["place"], _ = needing(daemon, harness, tmp_path, tree, nested, "linked")
            legacy(daemon, seen["job"])
            daemon._admit()
            seen.update(live=_live(daemon, seen["job"]), hold=dict(daemon._holds.get(seen["job"]) or {}),
                        prepared=list(workspaces.calls))
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retire(daemon)
        assert not seen["live"] and seen["prepared"] == [], seen
        assert seen["hold"]["leases"] == [fence] and seen["hold"]["needs"] == "git-storage", seen
        assert seen["hold"]["folder"] == seen["place"], seen
        daemon._admit()
        assert _live(daemon, seen["job"])
        _end(daemon, seen["job"])


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_c8_4_a_turn_in_a_checkout_whose_repository_is_in_the_tree_waits_for_the_fence(tmp_path, writable):
    """A conversation in a checkout outside the tree whose repository is in the tree
    keys its row on that checkout, outside the tree, so neither its row nor its folder
    told retention or admission anything. Its turn, submitted under the fence, waits
    `lease-held` on it, named as its folder's git storage, and its message says
    retention is removing the worktree that holds that storage (C-24.4). Once the
    fence goes, the next turn pass places it.
    Failed on 0a91f070: reserved under the fence."""
    from subfleet.conversations import waits
    from tests.fake.test_turn_wait_reasons import message_in, reason, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        outside = external_checkout(tmp_path, nested)
        git(outside, "checkout", "--quiet", "-b", "task/outside")       # C-13.2: a writable turn is not on main
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        seen = {}

        def begin(retirement, job, pool):
            _, mid, turn = message_in(daemon, harness, "Outside", workspace=outside, settings=options)
            daemon._admit_turns()
            seen.update(mid=mid, turn=turn, live=_live(daemon, turn), hold=dict(daemon._holds.get(turn) or {}),
                        reason=reason(daemon, mid))
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retire(daemon)
        assert not seen["live"], seen
        assert seen["hold"]["reason"] == "lease-held" and seen["hold"]["leases"] == [folders.exclusive_key(tree)]
        assert seen["hold"]["needs"] == "git-storage", seen
        assert seen["hold"]["folder"] == str(Path(nested) / ".git" / "worktrees" / "external"), seen
        assert seen["reason"] == ("lease: retention is removing a finished job's worktree that holds this folder's "
                                  f"git storage ({tree})"), seen["reason"]
        daemon._admit_turns()
        assert _live(daemon, seen["turn"]), daemon._holds.get(seen["turn"])
        assert reason(daemon, seen["mid"]) == waits.PLACED


@pytest.mark.parametrize("needs, place, said", [
    ("review-root", "/s/worktrees/j/vendor/lib", "that its review root (-D) /s/worktrees/j/vendor/lib is in"),
    ("output", "/s/worktrees/j/out.md", "that its output path (-o) /s/worktrees/j/out.md is in"),
    ("git-storage", "/s/worktrees/j/lib/.git", "that its git storage /s/worktrees/j/lib/.git is in"),
    ("review-root", "/s/worktrees/j", ", its review root (-D)"),
])
def test_c6_11_why_names_the_place_a_job_needs_in_the_tree_being_retired(needs, place, said):
    """`why` says what the held job needs in the tree: its review root, its `-o` path
    or its git storage, by the hold's `needs`; a hold without it reads as before."""
    hold = {"reason": "lease-held", "leases": ["worktree:/s/worktrees/j"], "retiring": ["worktree:/s/worktrees/j"],
            "folder": place, "needs": needs}
    text = "\n".join(render.why_queue({"job_id": "j", "state": "waiting", "hold": hold}))
    assert (f"Held: retention is removing a finished job's worktree{' ' if said[0] != ',' else ''}{said} "
            "(/s/worktrees/j); it waits until retention lets go of it (C-8.4)") in text, text
