"""A small world for retention-by-archive tests (design d635): real git
repositories in a temporary directory, a state root with a store, and helpers
to make owned worktrees, snapshot trees byte for byte, and list commits.

The source repository's `origin` URL is a network URL (so its remote-tracking
refs count as held by a remote), while pushes and fetches go to a local bare
repository standing in for the server. Temporary directories normally make a
repository scratch (no omission); `World(trusted=True)` makes this one trusted.
"""
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from subfleet import retention_git as rgit
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(cwd, *args, check=True, env=None) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(IDENTITY)
    if env:
        environment.update(env)
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=environment)
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class Clock:
    """A monotonic clock tests move by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def snapshot(root: Path) -> dict[str, tuple]:
    """rel path -> (type, mode, content or target, mtime_ns), without following links."""
    out: dict[str, tuple] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(directory) / name
            rel = str(path.relative_to(root))
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("l", None, os.readlink(path), None)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("d", stat.S_IMODE(st.st_mode), None, st.st_mtime_ns)
            elif stat.S_ISREG(st.st_mode):
                out[rel] = ("f", stat.S_IMODE(st.st_mode), path.read_bytes(), st.st_mtime_ns)
            elif stat.S_ISFIFO(st.st_mode):
                out[rel] = ("p", stat.S_IMODE(st.st_mode), None, st.st_mtime_ns)
            else:
                out[rel] = ("?", None, None, None)
    return out


def inode_groups(root: Path) -> list[set[str]]:
    groups: dict[tuple[int, int], set[str]] = {}
    for directory, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(directory) / name
            st = path.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                groups.setdefault((st.st_dev, st.st_ino), set()).add(str(path.relative_to(root)))
    return sorted((g for g in groups.values() if len(g) > 1), key=sorted)


@dataclass
class World:
    base: Path
    trusted: bool = True
    remote: Path = field(init=False)
    repo: Path = field(init=False)
    root: Path = field(init=False)
    store: Store = field(init=False)

    def __post_init__(self):
        self.remote = self.base / "server" / "project.git"
        self.remote.parent.mkdir(parents=True)
        git(self.base, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        self.repo = self.base / "home" / "project"
        self.repo.parent.mkdir(parents=True)
        git(self.base, "clone", "--quiet", str(self.remote), str(self.repo))
        git(self.repo, "remote", "set-url", "origin", "https://git.example.invalid/project.git")
        (self.repo / ".gitignore").write_text("out/\n*.log\n")
        (self.repo / "README.md").write_text("# project\n" * 50)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('hello')\n" * 200)
        (self.repo / "data.bin").write_bytes(bytes(range(256)) * 64)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.push()
        self.root = self.base / "state"
        self.root.mkdir()
        (self.root / "jobs").mkdir()
        (self.root / "worktrees").mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                                 "/home/one", LaneOwner.V2, False))

    def push(self, ref: str = "HEAD:main") -> None:
        git(self.repo, "push", "--quiet", str(self.remote), ref)
        git(self.repo, "fetch", "--quiet", str(self.remote), "+refs/heads/*:refs/remotes/origin/*")

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD")

    def job(self, job_id: str, *, worktree: bool = True, created: str | None = None, state: str = "succeeded",
            kind: str = "dispatch", files: dict[str, bytes] | None = None) -> Path | None:
        head = self.head()
        path = None
        if worktree:
            path = self.root / "worktrees" / job_id
            git(self.repo, "worktree", "add", "--quiet", "--detach", str(path), head)
        fields = dict(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind=kind, workdir=str(self.repo),
                      workdir_head=head, worktree=str(path) if path else None, prompt_path="/prompt",
                      sandbox="workspace-write" if worktree else "read-only", state=state)
        if created:
            fields["created_at"] = created
        self.store.add_job(**fields)
        directory = self.root / "jobs" / job_id
        directory.mkdir()
        for name, data in (files or {"stdout": b"output of " + job_id.encode()}).items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        return path

    def attempt(self, job_id: str, state: str = "succeeded") -> str:
        attempt_id = f"{job_id}/a1"
        self.store.add_attempt(attempt_id=attempt_id, job_id=job_id, seq=1, lane_id="codex-1",
                               model_requested="gpt-6-astra", state=state)
        return attempt_id

    def admin(self, job_id: str) -> Path:
        return self.repo / ".git" / "worktrees" / job_id

    def close(self) -> None:
        self.store.close()


def trust_temporary_directories(monkeypatch) -> None:
    """Pytest's directories are temporary; this makes only paths under a
    directory named `scratch` (and the state root) count as scratch."""
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})


def all_commits(git_dir: Path) -> set[str]:
    out = git(git_dir, "rev-list", "--all", "--reflog", check=False)
    return set(out.split())


def objects_present(git_dir: Path, oids) -> dict[str, bool]:
    return {oid: git(git_dir, "cat-file", "-e", oid, check=False) == "" and
            subprocess.run(["git", "-C", str(git_dir), "cat-file", "-e", oid], capture_output=True).returncode == 0
            for oid in oids}
