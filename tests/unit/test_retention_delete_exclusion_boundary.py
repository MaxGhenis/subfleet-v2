"""Excluded-read regressions on fresh fixtures; never a live store/process.

Commands tested at the guard boundary are data. Functional deletion targets
only the temporary World; no destructive-shaped command probes are executed.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from os.path import relpath
from pathlib import Path

import pytest

from subfleet import dependencies
from subfleet import retention_delete as rd
from subfleet import retention_git as rgit
from subfleet.retention_delete_guard import GitReadGuard, ISOLATED_CONFIG
from tests.unit.retention_delete_world import commit, delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def included_file(w: World, path: Path) -> Path:
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    sentinel = excluded / "unread-config.conf"
    sentinel.write_text("intentionally invalid config that must never reach Git\n")
    with path.open("a") as stream:
        stream.write(f'\n[include]\n\tpath = "{sentinel}"\n')
    return excluded


@pytest.mark.parametrize("per_worktree", [False, True])
def test_an_excluded_config_include_is_refused_before_any_git(world, monkeypatch, per_worktree):
    w = world
    jid = "nonexistent-excluded-include"
    tree = nested_job(w, jid)
    config = w.repo / ".git" / "config"
    if per_worktree:
        with config.open("a") as stream:
            stream.write("\n[extensions]\n\tworktreeConfig = true\n")
        config = w.admin(jid) / "config.worktree"
    excluded = included_file(w, config)
    calls = []
    original = rgit.subprocess.Popen

    def spy(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(rgit.subprocess, "Popen", spy)
    line = results(run(delete_path(w), request(jid, exclude_repositories=[str(excluded)]),
                       mode="dry-run"))[jid]
    assert line["gate"] in {"exclude-top", "exclude-nested"} and "includes" in line["reason"]
    assert tree.exists() and calls == []


@pytest.mark.parametrize("metadata", ["config", "HEAD", "hooks/not-used", "objects/pack/external.pack"])
def test_symlinked_git_metadata_is_refused_without_following_it(world, monkeypatch, metadata):
    w = world
    jid = "nonexistent-excluded-metadata"
    tree = nested_job(w, jid)
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    secret = excluded / "must-not-read"
    secret.write_text("not a Git input\n")
    link = w.repo / ".git" / metadata
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    link.symlink_to(secret)
    calls = []
    original = rgit.subprocess.Popen

    def spy(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(rgit.subprocess, "Popen", spy)
    line = results(run(delete_path(w), request(jid, exclude_repositories=[str(excluded)]),
                       mode="dry-run"))[jid]
    assert line["disposition"] in {"kept", "failed"} and tree.exists()
    assert calls == []


def test_a_metadata_swap_invalidates_the_inventory_before_git(world, monkeypatch):
    w = world
    common = w.repo / ".git"
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    target = excluded / "config.conf"
    target.write_text("unreadable to the guarded operation\n")
    guard = GitReadGuard(rd.Excluder([str(excluded)]))
    calls = []
    original = rgit.subprocess.Popen

    def spy(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    with rgit.command_guard(guard, env=ISOLATED_CONFIG, cwd=w.root):
        rgit.run(["rev-parse", "--git-dir"], git_dir=common)
        (common / "config").unlink()
        (common / "config").symlink_to(target)
        monkeypatch.setattr(rgit.subprocess, "Popen", spy)
        with pytest.raises(rgit.GitError, match="metadata"):
            rgit.run(["rev-parse", "--git-dir"], git_dir=common)
    assert calls == []


def test_legacy_discovery_remains_a_pin_without_reading_its_workdir(world, monkeypatch):
    w = world
    jid = "nonexistent-candidate-with-legacy"
    tree = nested_job(w, jid)
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    w.job("nonexistent-live-legacy", worktree=False, state="running")
    w.store.update_job("nonexistent-live-legacy", workdir=str(excluded))

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy discovery must not run")

    monkeypatch.setattr(dependencies, "legacy", forbidden)
    line = results(run(delete_path(w), request(jid, exclude_repositories=[str(excluded)]),
                       mode="dry-run"))[jid]
    assert line["gate"] == "pins" and "dependency-incomplete" in line["reason"] and tree.exists()


def test_guard_context_is_scoped_to_the_calling_thread(world):
    def refuse(*args):
        raise rgit.GitError("fixture guard")

    with rgit.command_guard(refuse, env=ISOLATED_CONFIG, cwd=world.root):
        with pytest.raises(rgit.GitError, match="fixture guard"):
            rgit.run(["version"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(rgit.run, ["version"], cwd=world.root).result()
        assert result.stdout.startswith(b"git version")
    assert rgit.run(["version"], cwd=world.root).stdout.startswith(b"git version")


def test_guard_pins_config_per_command_even_against_an_explicit_override(world):
    w = world
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    configuration = excluded / "ambient.conf"
    configuration.write_text("[audit]\n\tsentinel = must-not-load\n")
    guard = GitReadGuard(rd.Excluder([str(excluded)]))
    with rgit.command_guard(guard, env=ISOLATED_CONFIG, cwd=w.root):
        result = rgit.run(["config", "--global", "--get", "audit.sentinel"], git_dir=w.repo / ".git",
                          env={"GIT_CONFIG_GLOBAL": str(configuration)}, ok=(0, 1))
    assert result.returncode == 1 and result.stdout == b""


def test_ordinary_roots_and_self_contained_bundles_still_work(world):
    w = world
    jid = "nonexistent-ordinary-guarded"
    tree = nested_job(w, jid)
    inner = tree / "inside"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    oid = commit(inner, "unique.txt", "guarded standalone history\n")
    record = run(delete_path(w), request(jid))
    line = results(record)[jid]
    assert line["disposition"] == "deleted", line
    marker = json.loads((w.root / "jobs" / jid / "tree-deleted.json").read_text())
    assert not tree.exists() and len(marker["bundles"]) == 1
    bundle = Path(marker["bundles"][0]["path"])
    assert oid in git(w.repo, "bundle", "list-heads", str(bundle))


def test_layout_refuses_nested_worktrees_symlink_before_listing(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    common = tree / "vendor" / ".git"
    (common / "objects").mkdir(parents=True)
    (common / "refs").mkdir()
    (common / "HEAD").write_text("ref: refs/heads/main\n")
    excluded = tmp_path / "excluded-repository"
    excluded.mkdir()
    (common / "worktrees").symlink_to(excluded, target_is_directory=True)
    scan = rd.scan_tree(tree)
    real_listdir = rd.os.listdir

    def forbidden_listing(path):
        if isinstance(path, (str, Path)) and str(path) in {str(excluded), str(common / "worktrees")}:
            raise AssertionError("layout entered a forbidden storage directory")
        return real_listdir(path)

    monkeypatch.setattr(rd.os, "listdir", forbidden_listing)
    with pytest.raises(rd._Gate, match="metadata"):
        rd.layout(tree, scan, rd.Excluder([str(excluded)]))


def test_worktree_config_resolves_core_worktree_from_actual_git_dir(world):
    w = world
    jid = "nonexistent-config-worktree-base"
    nested_job(w, jid)
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    with (w.repo / ".git" / "config").open("a") as stream:
        stream.write("\n[extensions]\n\tworktreeConfig = true\n")
    (w.admin(jid) / "config.worktree").write_text(f"[core]\n\tworktree = {relpath(excluded, w.admin(jid))}\n")
    guard = GitReadGuard(rd.Excluder([str(excluded)]))
    with pytest.raises(rgit.GitError, match="excluded"):
        guard.repository(w.admin(jid))


def test_raw_admin_walk_is_guarded_before_reading_a_persisted_alias(world):
    w = world
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    alias = w.base / "saved-admin-parent"
    alias.symlink_to(excluded, target_is_directory=True)
    guard = GitReadGuard(rd.Excluder([str(excluded)]))
    with rgit.command_guard(guard, env=ISOLATED_CONFIG, cwd=w.root):
        with pytest.raises(rgit.GitError, match="excluded"):
            rgit.admin_objects(alias / "admin", "sha1")


def test_persisted_admin_alias_and_claim_are_kept_without_excluded_reads(world):
    w = world
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    original = w.root / "worktrees" / "nonexistent-original"
    original.symlink_to(excluded, target_is_directory=True)
    alias = w.base / "saved-admin-parent"
    alias.symlink_to(excluded, target_is_directory=True)
    admin = alias / "admin"
    path = delete_path(w)
    journal = {"job_id": "nonexistent-admin-cleanup", "worktree": str(original),
               "exclude_repositories": [str(excluded)], "admins": [{"admin": str(admin)}]}
    report = path._remove_admins(journal)
    assert report["removed"] == [] and len(report["kept"]) == 1
    assert path._claimed(str(admin), str(original / ".git"), str(original),
                         rd.Excluder([str(excluded)])) == str(original)
    original.unlink()
    original.mkdir()
    (original / ".git").symlink_to(excluded / "gitfile")
    assert path._claimed(str(admin), str(original / ".git"), str(original),
                         rd.Excluder([str(excluded)])) == str(original)
    (original / ".git").unlink()
    (original / ".git").write_text(f"gitdir: {excluded / 'admin'}\n")
    assert path._claimed(str(admin), str(original / ".git"), str(original),
                         rd.Excluder([str(excluded)])) == str(original)


def test_rollback_does_not_remove_a_folder_reached_through_excluded_alias(world):
    w = world
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    retained = excluded / "must-stay"
    retained.mkdir()
    alias = w.base / "saved-ref-parent"
    alias.symlink_to(excluded, target_is_directory=True)
    path = delete_path(w)
    path.base.mkdir()
    journal = {"job_id": "nonexistent-rollback-cleanup", "worktree": str(w.root / "worktrees" / "absent"),
               "tombstone": str(w.root / "retention-delete" / "nonexistent-rollback-cleanup" / "tree"),
               "exclude_repositories": [str(excluded)], "made_folders": [str(alias / "must-stay")]}
    path._rollback(journal)
    assert retained.is_dir()


def test_null_recorded_worktree_checks_the_allocated_fallback_before_ownership_reads(world, monkeypatch):
    w = world
    jid = "nonexistent-null-allocated-worktree"
    w.job(jid, worktree=False)
    w.store.update_job(jid, sandbox="workspace-write")
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    (excluded / jid).mkdir()
    allocated = w.root / "worktrees"
    allocated.rmdir()
    allocated.symlink_to(excluded, target_is_directory=True)
    fallback = allocated / jid
    original = rd.os.lstat

    def no_ownership_probe(path, *args, **kwargs):
        if str(path) == str(fallback):
            raise AssertionError("ownership probed the excluded allocated fallback")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(rd.os, "lstat", no_ownership_probe)
    line = results(run(delete_path(w), request(jid, exclude_repositories=[str(excluded)]),
                       mode="dry-run"))[jid]
    assert line["gate"] == "exclude-top" and "excluded" in line["reason"]
