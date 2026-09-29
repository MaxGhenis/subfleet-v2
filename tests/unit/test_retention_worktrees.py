"""Allocated worktrees are retired only by the policy's worktree archiver (C-8.4, C-13.4).

Retention itself never deletes, moves or writes a worktree (invariant W1), and it
prunes a job that owns one only after the worktree has gone (W2). The archiver
here is a small fake with the real one's interface: argv from policy, repeated
`--only <path>`, and its own decision whether to remove each worktree.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from subfleet import retention
from subfleet.store import Store

FAKE_ARCHIVER = textwrap.dedent('''
    import json, os, shutil, sqlite3, subprocess, sys, time
    mode, state_root, log = sys.argv[1], sys.argv[2], sys.argv[3]
    paths = [sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == "--only"]
    db = sqlite3.connect(f"file:{state_root}/state.sqlite3?mode=ro", uri=True)
    leases = [list(row) for row in db.execute("SELECT lease_key, holder FROM leases")]
    with open(log, "a") as out:
        out.write(json.dumps({"argv": sys.argv[1:], "leases": leases, "pid": os.getpid(),
                              "pgid": os.getpgid(0)}) + "\\n")
    if mode == "remove":
        for path in paths:
            aside = os.path.join(os.path.dirname(log), "aside", os.path.basename(path))
            shutil.copytree(path, aside, symlinks=True)
            repo = subprocess.run(["git", "-C", path, "rev-parse", "--git-common-dir"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "--git-dir", repo, "worktree", "remove", "--force", path], check=True)
            print(f"archived and removed {path}")
    elif mode == "refuse":
        print("kept: idle 0.1 d, not 1 d")
    elif mode == "busy":
        print("the guard's lock is held")
        sys.exit(75)
    elif mode == "hang":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        time.sleep(120)
''')


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "feature")
    (repository / "tracked").write_text("base" * 1024)
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "baseline")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    with Store(root / "state.sqlite3") as store:
        yield store, root, repository


def add_job(store, root, repository, identity, *, order=0, worktree=True, state="succeeded", **fields):
    path = None
    if worktree:
        path = root / "worktrees" / identity
        git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
        (path / "untracked-result.csv").write_text(f"{identity},42\n")
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(repository), worktree=str(path) if path else None, prompt_path="/prompt",
                  sandbox="workspace-write", state=state, created_at=f"2026-01-01T00:00:{order:02d}Z",
                  finished_at="2026-01-02T00:00:00Z", **fields)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"output")
    return path


def archiver(tmp_path, root, mode, timeout=30):
    script = tmp_path / "fake_archiver.py"
    script.write_text(FAKE_ARCHIVER)
    return {"argv": [sys.executable, str(script), mode, str(root), str(tmp_path / "archiver.log")],
            "per_worktree": ["--only", "{path}"], "timeout_s": timeout}


def calls(tmp_path):
    log = tmp_path / "archiver.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def forbid_worktree_writes(monkeypatch, worktrees):
    """W1: fail the test if retention (this process) deletes, moves or writes below a worktree."""
    roots = [str(Path(path).resolve()) for path in worktrees]

    def inside(path):
        path = os.fspath(path)
        return any(path == root or path.startswith(root + "/") for root in roots)

    for name in ("unlink", "rmdir", "remove", "rename", "replace", "chmod", "mkdir"):
        original = getattr(os, name)

        def guard(*args, _original=original, _name=name, **kwargs):
            if any(inside(arg) for arg in args if isinstance(arg, (str, os.PathLike))):
                pytest.fail(f"retention called os.{_name} on a worktree path: {args}")
            return _original(*args, **kwargs)
        monkeypatch.setattr(os, name, guard)
    original_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if inside(path):
            pytest.fail(f"retention removed a worktree tree: {path}")
        return original_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", rmtree)


def test_w2_without_an_archiver_a_job_with_its_worktree_is_kept(owned, monkeypatch):
    """C-13.4 W1, W2: with no archiver configured, the worktree and its job stay; nothing runs."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    before = sorted(p.name for p in worktree.iterdir())
    forbid_worktree_writes(monkeypatch, [worktree])
    monkeypatch.setattr(retention.subprocess, "Popen", lambda *a, **k: pytest.fail("no archiver may run"))
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == []
    assert "job" in result["protected"]
    assert "no worktree archiver configured" in result["deferred"]["job"]
    assert sorted(p.name for p in worktree.iterdir()) == before
    assert store.get_job("job") is not None and (root / "jobs" / "job").is_dir()
    assert store.list_leases() == []


def test_w2_a_job_is_pruned_only_after_the_archiver_removed_its_worktree(owned, tmp_path, monkeypatch):
    """C-8.4 W2: one archiver call covers every candidate; each job goes only once its path is gone."""
    store, root, repository = owned
    a = add_job(store, root, repository, "a", order=0)
    b = add_job(store, root, repository, "b", order=1)
    add_job(store, root, repository, "newest", order=2)
    forbid_worktree_writes(monkeypatch, [a, b])
    result = retention.maintenance(store, root, max_jobs=1, max_bytes=10 ** 9,
                                   archiver=archiver(tmp_path, root, "remove"))
    assert result["pruned"] == ["a", "b"]
    assert [call["argv"].count("--only") for call in calls(tmp_path)] == [2]
    assert not a.exists() and not b.exists()
    assert (tmp_path / "aside" / "a" / "untracked-result.csv").read_text() == "a,42\n"
    assert store.get_job("a") is None and store.get_job("b") is None and store.get_job("newest") is not None
    assert (root / "archive" / "a" / "manifest.json").is_file()
    assert store.list_leases() == []


def test_the_archiver_never_sees_a_path_lease_from_retention(owned, tmp_path):
    """Disk-guard treats every `worktree:/...` or `out:/...` lease as live and would refuse
    the worktree; retention fences with `retire:<job>`, which names no path."""
    store, root, repository = owned
    add_job(store, root, repository, "job")
    retention.maintenance(store, root, max_jobs=0, archiver=archiver(tmp_path, root, "refuse"))
    [call] = calls(tmp_path)
    assert call["leases"] == [["retire:job", "retention:job"]]
    assert all(not key.partition(":")[2].startswith("/") for key, _ in call["leases"])


@pytest.mark.parametrize("mode", ["refuse", "busy"])
def test_w2_a_kept_worktree_keeps_its_job_and_defers_it(owned, tmp_path, monkeypatch, mode):
    """C-8.4 W2, G1: a refusal or a held lock keeps the job, with the archiver's reason, and defers it."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    forbid_worktree_writes(monkeypatch, [worktree])
    state = retention.RetentionState()
    config = archiver(tmp_path, root, mode)
    first = retention.maintenance(store, root, max_jobs=0, archiver=config, state=state)
    assert first["pruned"] == []
    reason = first["deferred"]["job"]
    assert ("idle 0.1 d" in reason) if mode == "refuse" else ("rc 75" in reason and "lock" in reason)
    assert worktree.is_dir() and store.get_job("job") is not None
    assert store.list_leases() == []
    second = retention.maintenance(store, root, max_jobs=0, archiver=config, state=state)
    assert len(calls(tmp_path)) == 1                       # deferred, not asked again this hour
    assert second["deferred"]["job"] == reason
    assert any(event["kind"] == "retention.archiver" for event in store.list_events())


def test_a_hung_archiver_is_stopped_with_its_process_group(owned, tmp_path):
    """C-16.4: the policy's timeout ends the archiver and anything it started; the job stays."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    started = time.monotonic()
    result = retention.maintenance(store, root, max_jobs=0, archiver=archiver(tmp_path, root, "hang", timeout=1))
    assert time.monotonic() - started < 30
    assert result["pruned"] == [] and worktree.is_dir()
    [call] = calls(tmp_path)
    with pytest.raises(ProcessLookupError):
        os.killpg(call["pgid"], 0)


def test_cancelling_the_pass_stops_the_archiver_and_keeps_every_job(owned, tmp_path):
    """C-16.4: daemon shutdown ends the archiver's group; nothing is pruned or half done."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    cancel = threading.Event()
    timer = threading.Timer(1.5, cancel.set)
    timer.start()
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel,
                                   archiver=archiver(tmp_path, root, "hang", timeout=60))
    timer.cancel()
    assert result["interrupted"] == "cancelled"
    assert worktree.is_dir() and store.get_job("job") is not None
    [call] = calls(tmp_path)
    with pytest.raises(ProcessLookupError):
        os.killpg(call["pgid"], 0)
    later = retention.maintenance(store, root, max_jobs=0)          # recovery releases the fence
    assert store.list_leases() == [] and later["pruned"] == []


def test_a_worktree_already_gone_needs_no_archiver(owned, monkeypatch):
    """C-8.4: a worktree the sweep or a person already removed leaves only the job to prune."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    monkeypatch.setattr(retention.subprocess, "Popen", lambda *a, **k: pytest.fail("no archiver needed"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (root / "archive" / "job" / "manifest.json").is_file()


def test_a_legacy_removal_lease_is_released_and_the_job_retired(owned):
    """C-8.4: the installed code's `worktree:` lease held by `retention:<job>` is released,
    and the job is then retired as any other."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    assert store.acquire_lease(f"worktree:{worktree.resolve()}", "retention:job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert store.get_job("job") is None
    assert store.list_leases() == []


def test_a_live_job_working_inside_a_worktree_pins_its_owner(owned, tmp_path):
    """C-8.4: a queued in-place job (a resume, or anything sent into the tree) keeps the tree's job."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "owner", order=0)
    add_job(store, root, repository, "visitor", order=1, worktree=False, state="queued")
    store.update_job("visitor", workdir=str(worktree / "pkg"), in_place=1)
    result = retention.maintenance(store, root, max_jobs=0, archiver=archiver(tmp_path, root, "remove"))
    assert "owner" in result["protected"] and result["pruned"] == []
    assert calls(tmp_path) == [] and worktree.is_dir()


def test_c13_4_retention_never_removes_in_place_workdir(owned, monkeypatch):
    """C-13.4, C-8.4: pruning an in-place job leaves the caller's files and Git index intact."""
    store, root, repository = owned
    add_job(store, root, repository, "job", worktree=False)
    store.update_job("job", in_place=1, worktree=str(repository))
    before = (repository / ".git" / "index").read_bytes()
    monkeypatch.setattr(retention.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("an in-place workdir is never archived"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (repository / "tracked").read_text() == "base" * 1024
    assert (repository / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize("path_kind", ["outside", "symlink", "container-symlink"])
def test_c13_4_retention_rejects_worktree_paths_outside_owned_root(owned, monkeypatch, path_kind):
    """C-13.4, C-2.1: resolved paths and symlinked containers cannot send an external tree to the archiver."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    if path_kind == "outside":
        store.update_job("job", worktree=str(repository))
    elif path_kind == "symlink":
        worktree.symlink_to(repository, target_is_directory=True)
    else:
        (root / "worktrees").rmdir()
        (root / "worktrees").symlink_to(repository.parent, target_is_directory=True)
        store.update_job("job", worktree=str(root / "worktrees" / "repository"))
    monkeypatch.setattr(retention.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("external paths never reach the archiver"))
    result = retention.maintenance(store, root, max_jobs=0, archiver={"argv": ["/usr/bin/false"], "timeout_s": 5})
    assert result["pruned"] == []
    assert "job" in result["protected"]
    assert result["errors"]
    assert store.get_job("job") is not None
    assert (repository / "tracked").exists()
