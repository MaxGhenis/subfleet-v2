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


def _base_source_of_gone_tree():
    repository = Path(__file__).resolve().parents[2]
    source = subprocess.run(["git", "show", "18aa2a695:subfleet/retention_archive.py"],
                            cwd=repository, env=rgit.environment(), check=True,
                            capture_output=True, text=True).stdout
    functions = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "source_of_gone_tree"]
    assert len(functions) == 1
    scope = dict(rarch.__dict__)
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 "18aa2a695:retention_archive.py", "exec"), scope)
    return scope["source_of_gone_tree"]


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
