"""C-8.3/C-8.4: review regressions for dependencies hidden by path encodings."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from pathlib import Path

import pytest

from subfleet import dependencies, folders, retention
from subfleet.store import Store
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _live
from tests.fake.test_retention_dependencies import retire
from tests.fake.test_writer_retention_fence import measured
from tests.unit.retention_world import World, git
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_shared_folders import retiring_tree


@pytest.mark.parametrize("spelling", ["case", "nfc", "nfd", "greek", "sharp-s", "fi", "kelvin",
                                      "firmlink", "symlink", "dotdot", "trailing"])
def test_c8_4_recorded_places_pin_and_fence_through_each_spelling(tmp_path, spelling):
    """Exercise recording and both transactional readers, including absent tails."""
    root = tmp_path / "MiXeD-é-ΐ-ß-ﬁ-K" / "state"
    tree = root / "worktrees" / "owner"
    (tree / "vendor" / "other").mkdir(parents=True)
    canonical = folders.canonical(tree)
    named = unicodedata.normalize("NFC", canonical)
    if spelling == "case":
        named = named.replace("MiXeD", "mIxEd")
    elif spelling in ("nfc", "nfd"):
        named = unicodedata.normalize(spelling.upper(), named)
    elif spelling == "greek":
        named = named.replace("ΐ", "Ϊ\u0301")
    elif spelling == "sharp-s":
        named = named.replace("ß", "SS")
    elif spelling == "fi":
        named = named.replace("ﬁ", "FI")
    elif spelling == "kelvin":
        named = named.replace("K", "k").replace("-K/", "-k/")
    elif spelling == "firmlink":
        named = "/System/Volumes/Data" + canonical
        if not Path(named).is_dir() or not Path(named).samefile(tree):
            pytest.skip("requires the Data volume firmlink")
    elif spelling == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tree.parent)
        named = str(alias / "owner")
    elif spelling == "dotdot":
        named += "/vendor/other/../.."
    elif spelling == "trailing":
        named += "////"

    outside = tmp_path / "outside"
    outside.mkdir()
    # Like a linked checkout during quarantine, the gitfile still names an absent admin.
    (outside / ".git").write_text("gitdir: " + named + "/vendor/.git/worktrees/external\n")
    recorded = dependencies.record(str(outside), review_root=named + "/vendor/not-yet",
                                   out_path=named + "/vendor/result.md")
    places = dependencies.needed({"depends": recorded})
    assert {role for role, _ in places} == {"review-root", "output", "git-storage"}
    assert not Path(recorded["out_path"]).exists()
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="owner", request_id="owner", payload_digest="d", kind="dispatch",
                      state="succeeded", sandbox="workspace-write", worktree=canonical,
                      workdir=str(outside), prompt_path="/prompt")
        store.add_job(job_id="reader", request_id="reader", payload_digest="d", kind="dispatch",
                      state="queued", sandbox="read-only", workdir=str(outside), prompt_path="/prompt")
        store.add_event("job.submitted", job_id="reader", data={"depends": recorded})
        lease = folders.exclusive_key(canonical)
        assert store.acquire_lease(lease, "retention:owner")
        with store.transaction() as tx:
            read = lambda sql, params: tx.execute(sql, params).fetchall()
            assert dependencies.dependents(read, canonical, exclude="owner") == ["reader"]
            assert dependencies.fences(read, places) == [(role, path, [lease]) for role, path in places]
            assert retention._pin_reasons(store, set(), None, root=root)["owner"] == "worktree-in-use"


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads(tmp_path, relative):
    """Git decodes octal escapes before resolving an alternate object directory."""
    world = World(tmp_path)
    try:
        tree = world.job("owner")
        source = repo(tree / "objects-source")
        commit = git(source, "rev-parse", "HEAD")
        borrower = tmp_path / "borrower"
        git(tmp_path, "clone", "--quiet", "--shared", str(source), str(borrower))
        objects = source / ".git" / "objects"
        base = borrower / ".git" / "objects"
        named = os.path.relpath(objects, base) if relative else str(objects)
        quoted = named.replace("/owner/", "/\\157wner/")
        assert quoted != named
        (base / "info" / "alternates").write_text('"' + quoted + '"\n')
        assert git(borrower, "cat-file", "-t", commit) == "commit"

        recorded = dependencies.record(str(borrower))
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state="running", sandbox="read-only",
                            workdir=str(borrower), prompt_path="/prompt")
        world.store.add_event("job.submitted", job_id="borrower",
                              data={"folder": folders.canonical(borrower), "depends": recorded})
        result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                       holders=lambda *args, **kwargs: {})
        assert "owner" not in result["pruned"], (recorded, result)
        assert tree.is_dir() and objects.is_dir()
        assert git(borrower, "cat-file", "-t", commit) == "commit"
    finally:
        world.close()


def test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads(tmp_path):
    """A valid file past the discovery read cap must not silently lose dependencies."""
    world = World(tmp_path)
    try:
        tree = world.job("owner")
        source = repo(tree / "objects-source")
        commit = git(source, "rev-parse", "HEAD")
        borrower = tmp_path / "borrower"
        git(tmp_path, "clone", "--quiet", "--shared", str(source), str(borrower))
        line = str(source / ".git" / "objects") + "\n"
        text = line * ((64 * 1024) // len(line.encode()) + 1)
        assert len(text.encode()) > 64 * 1024
        (borrower / ".git" / "objects" / "info" / "alternates").write_text(text)
        assert git(borrower, "cat-file", "-t", commit) == "commit"

        recorded = dependencies.record(str(borrower))
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state="running", sandbox="read-only",
                            workdir=str(borrower), prompt_path="/prompt")
        world.store.add_event("job.submitted", job_id="borrower", data={"depends": recorded})
        result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                       holders=lambda *args, **kwargs: {})
        assert "owner" not in result["pruned"], (recorded, result)
        assert tree.is_dir()
        assert git(borrower, "cat-file", "-t", commit) == "commit"
    finally:
        world.close()


@pytest.mark.parametrize("root_name", ["dotless-ı", "ignored-\u200b"],
                         ids=["dotless-i", "default-ignorable"])
@pytest.mark.parametrize("legacy", [False, True], ids=["recorded", "legacy"])
def test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity(
        tmp_path, root_name, legacy):
    """An old terminal job still has jobs.out_path when its lease folds differently."""
    with fleet_daemon(tmp_path / root_name / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        out = Path(nested) / "result.md"
        job = submit(daemon, harness, workdir=str(harness.workdir), out_path=str(out))
        if dependencies.key(folders.identity(str(out))) == dependencies.key(str(out)):
            pytest.skip("requires a volume with the conservative output-identity equivalences")
        if legacy:
            with daemon.store.transaction() as tx:
                event = tx.execute("SELECT event_id,data_json FROM events "
                                   "WHERE job_id=? AND kind='job.submitted'", (job,)).fetchone()
                data = json.loads(event[1])
                del data["depends"]
                tx.execute("UPDATE events SET data_json=? WHERE event_id=?",
                           (json.dumps(data), event[0]))
        daemon._admit()
        assert _live(daemon, job), daemon._holds.get(job)
        attempt = daemon.store.list_attempts(job)[-1]["attempt_id"]
        deliverable = daemon.root / "jobs" / job / "deliverable.md"
        contents = b"pending accepted output\n"
        deliverable.write_bytes(contents)
        daemon.store.add_artifact(attempt, role="deliverable", path=str(deliverable),
                                  sha256=hashlib.sha256(contents).hexdigest(), bytes=len(contents))
        daemon.store.update_attempt(attempt, state="succeeded")
        daemon.store.update_job(job, state="succeeded", accepted_attempt_id=attempt)
        assert daemon.store.one("SELECT 1 FROM leases WHERE lease_key=? AND holder=?",
                                ("out:" + folders.identity(str(out)), job))

        result = retire(daemon)
        assert "retired" not in result["pruned"], result
        assert Path(tree).is_dir() and not out.exists()
        daemon._export(job)
        assert out.read_bytes() == contents
        assert daemon._job(job)["export_error"] is None
