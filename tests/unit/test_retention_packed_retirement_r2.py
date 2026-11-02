"""Review r2: accepted packed-ref separators must preserve retirement decisions."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from subfleet import retention, retention_archive as rarch, retention_git as rgit
from tests.unit.retention_world import World, git, trust_temporary_directories
from tests.unit.test_retention_repository_walk import _gone_job, _maintenance


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    monkeypatch.setattr(retention.procs, "worktree_cwd_pids", lambda folder: set())
    value = World(tmp_path)
    yield value
    value.close()


# `source_of_gone_tree` at 18aa2a695, the base these cases compare with, less its docstring. A copy,
# since CI's checkout holds no history to read it from; the test below holds it to that commit.
BASE_SOURCE_OF_GONE_TREE = '''
def source_of_gone_tree(job: dict[str, Any], worktree: Path, known: Callable[[], list[Path]],
                        salvage_refs: Callable[[Path], dict[str, str]], wanted: dict[str, str], *,
                        cancel: threading.Event | None = None,
                        workdir_common: Any = _UNRESOLVED_COMMON) -> tuple[rgit.Registration | None, Path | None, str | None]:
    workdir = job.get("workdir")
    if workdir and os.path.isdir(workdir):
        common = (rgit.common_dir(Path(workdir), cancel=cancel)
                  if workdir_common is _UNRESOLVED_COMMON else workdir_common)
        if common is not None:
            reg = rgit.registration_in(common, worktree)
            if reg is not None:
                return reg, common, None
            away = rgit.moved_tree(common, worktree)
            if away is not None:
                return None, None, f"tree away: {worktree} is registered at {away}; kept until it is back or gone"
            admin = rgit.named_admin(common, worktree)
            if admin is not None:
                return None, None, f"tree away: {worktree} may still belong to {admin}; read its registration again"
            return None, common, None
    candidates: list[Path] = []
    near = rgit.repository_near(Path(workdir), cancel=cancel) if workdir else None
    for common in ([near] if near is not None else []) + list(known()):
        if common not in candidates:
            candidates.append(common)
    for common in candidates:
        reg = rgit.registration_in(common, worktree, named=True)
        if reg is not None:
            return reg, common, None
    for common in candidates:
        away = rgit.moved_tree(common, worktree)
        if away is not None:
            return None, None, f"tree away: {worktree} is registered at {away}; kept until it is back or gone"
        admin = rgit.named_admin(common, worktree)
        if admin is not None:
            return None, None, f"tree away: {worktree} may still belong to {admin}; read its registration again"
    if wanted:
        for common in candidates:
            listing = salvage_refs(common)
            if all(ref in listing and salvage_digest(listing[ref]) == digest for ref, digest in wanted.items()):
                return None, common, None
    return None, None, f"repository not found (workdir {workdir} is gone)"
'''


def _base_source_of_gone_tree():
    scope = dict(rarch.__dict__)
    exec(compile(BASE_SOURCE_OF_GONE_TREE, "18aa2a695:retention_archive.py", "exec"), scope)
    return scope["source_of_gone_tree"]


def test_the_base_copy_is_that_commits_function():
    shown = subprocess.run(["git", "show", "18aa2a695:subfleet/retention_archive.py"],
                           cwd=Path(__file__).resolve().parents[2], env=rgit.environment(),
                           capture_output=True, text=True)
    if shown.returncode != 0:
        pytest.skip("this checkout holds no history")
    functions = [node for node in ast.parse(shown.stdout).body
                 if isinstance(node, ast.FunctionDef) and node.name == "source_of_gone_tree"]
    assert len(functions) == 1
    del functions[0].body[0]        # its docstring
    assert ast.dump(ast.parse(BASE_SOURCE_OF_GONE_TREE).body[0]) == ast.dump(functions[0])


@pytest.mark.parametrize("separator", [b"\t", b"\r"])
@pytest.mark.parametrize("lookup", ["base", "head"])
def test_noncanonical_packed_salvage_ref_preserves_retirement(world, monkeypatch, lookup, separator):
    w = world
    _gone_job(w, "job-gone", salvage=True)
    shutil.rmtree(w.admin("job-gone"))  # The salvage search must place this job.
    w.job("job-evidence", worktree=False)  # Its read-only workdir supplies the census.
    common = Path(os.path.realpath(w.repo / ".git"))
    ref = "refs/subfleet-salvage/repository-walk-a1"
    private = git(w.repo, "rev-parse", ref)

    # Preserve all refs Git writes, changing only the salvage record's separator.
    git(w.repo, "pack-refs", "--all", "--prune")
    packed = common / "packed-refs"
    original = packed.read_bytes()
    needle = b" " + ref.encode() + b"\n"
    assert original.count(needle) == 1
    packed.write_bytes(original.replace(needle, separator + ref.encode() + b"\n"))
    assert not os.path.lexists(common / ref)
    assert rgit.refs_under(common, ref) == {ref: private}
    assert rgit.no_refs_under(common, ref) is False
    assert rarch.known_repositories(w.store.list_jobs(), w.root) == [common]

    if lookup == "base":
        monkeypatch.setattr(rarch, "source_of_gone_tree", _base_source_of_gone_tree())
    result = _maintenance(w, referenced_job_ids=["job-evidence"])
    print(lookup, "pruned:", result["pruned"], "deferred:", result["deferred"],
          "errors:", result["errors"])
    assert result["errors"] == [], result
    assert result["pruned"] == ["job-gone"], result
    assert w.store.get_job("job-gone") is None
    bundle = w.root / "archive" / "job-gone" / "commits.bundle"
    assert bundle.is_file()
    restore = w.base / "restored.git"
    git(w.base, "clone", "--quiet", "--bare", str(w.remote), str(restore))
    git(restore, "fetch", str(bundle), "refs/*:refs/restored/*")
    assert private in git(restore, "rev-list", "--all").split()
