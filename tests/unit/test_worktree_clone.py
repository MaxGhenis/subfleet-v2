"""C-6.6a: a worktree allocated from clones of the caller's checkout is the
worktree `git worktree add --detach` makes, and shares the caller's blocks.

Every case builds both: the plain add as the reference, then `add_worktree`
from the same checkout at the same commit, and compares them path by path
(type, permission bits, bytes, link target), index entry by index entry (mode,
blob, stage, flags), and by HEAD, worktree config and status. The caller's
checkout is snapshotted before and after: allocation never changes it.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import retention_fs as rfs
from subfleet import worktree_clone as wc
from subfleet.salvage import _git_env

pytestmark = pytest.mark.skipif(rfs._fclonefileat is None, reason="needs fclonefileat (macOS)")

BIG = 64 * 1024     # big enough that a clone's shared blocks are measurable


def git(path, *args, check=True, env=None):
    return subprocess.run(["git", "-C", str(path), *args], check=check, capture_output=True, text=True,
                          env=env).stdout


def make_repo(root: Path, files: dict[str, bytes | tuple], attributes: str | None = None,
              object_format: str = "sha1") -> Path:
    """A repository with one commit. A value is bytes (a file), ("x", bytes)
    (an executable) or ("link", target) (a symlink)."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "task/example", f"--object-format={object_format}")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    if attributes is not None:
        files = {**files, ".gitattributes": attributes.encode()}
    write_files(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return root


def write_files(root: Path, files: dict) -> None:
    for rel, value in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        if isinstance(value, tuple) and value[0] == "link":
            path.symlink_to(value[1])
        elif isinstance(value, tuple):
            path.write_bytes(value[1])
            path.chmod(0o755)
        else:
            path.write_bytes(value)


def tree_state(root: Path) -> dict:
    """Every path but `.git`: type, permission bits, and bytes or link target."""
    state = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if rel == ".git":
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                body = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                body = path.read_bytes()
            else:
                body = None
            state[rel] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), body)
    return state


def git_state(root: Path) -> dict:
    git_path = lambda name: Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", name).strip())
    read = lambda p: p.read_bytes() if p.exists() else None
    admin = Path(git(root, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    # Old and new head and the message of each reflog line; who and when are the clock's.
    reflog = [(line.split(" ", 2)[:2], line.partition("\t")[2])
              for line in (read(admin / "logs" / "HEAD") or b"").decode().splitlines()]
    return {
        # A split index's file is named by a hash of its stat data, which two plain adds differ in too.
        "admin": sorted("sharedindex.*" if p.name.startswith("sharedindex.") else p.name for p in admin.iterdir()),
        "orig_head": read(admin / "ORIG_HEAD"),
        "reflog": reflog,
        "head": git(root, "rev-parse", "HEAD").strip(),
        "branch": git(root, "symbolic-ref", "-q", "HEAD", check=False).strip(),
        "index": git(root, "ls-files", "-s", "-z"),
        "flags": git(root, "ls-files", "-t", "-v", "-z"),
        "status": git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"),
        "sparse": read(git_path("info/sparse-checkout")),
        "config.worktree": read(git_path("config.worktree")),
    }


def checkout_state(root: Path) -> tuple[dict, dict]:
    return tree_state(root), git_state(root)


def source_state(root: Path) -> dict:
    """The caller's checkout, to the inode and nanosecond: nothing may change it."""
    state = {}
    for folder, dirs, files in os.walk(root):
        if Path(folder).name == "worktrees" and Path(folder).parent.name == ".git":
            dirs[:] = []
            continue
        for name in dirs + files:
            path = Path(folder) / name
            rel = str(path.relative_to(root))
            if rel in (".git", ".git/worktrees"):
                continue        # `worktree add`'s own administrative entries, as a plain add makes them
            info = os.lstat(path)
            state[rel] = (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
                          path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return state


def plain_add(source: Path, path: Path, commit: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach", str(path), commit],
                          capture_output=True, text=True, env=_git_env(None))


def clone_add(source: Path, path: Path, commit: str, **kwargs) -> wc.Allocation:
    return wc.add_worktree(str(source), str(path), commit, env=_git_env(None), timeout_s=120, **kwargs)


def both(tmp_path: Path, source: Path, commit: str = "HEAD", **kwargs):
    """(plain worktree state, clone worktree state, allocation), with the
    caller's checkout checked unchanged around the clone allocation."""
    commit = git(source, "rev-parse", commit).strip()
    reference = tmp_path / "plain"
    assert plain_add(source, reference, commit).returncode == 0
    expected = checkout_state(reference)
    before = source_state(source)
    allocation = clone_add(source, tmp_path / "cloned", commit, **kwargs)
    assert allocation.result.returncode == 0, allocation.result.stderr
    assert source_state(source) == before
    return expected, checkout_state(tmp_path / "cloned"), allocation


def private(path: Path) -> int:
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return rfs.private_bytes(fd, path.name, os.lstat(path))
    finally:
        os.close(fd)


FILES = {
    "README.md": b"hello\n",
    "big.bin": bytes(range(256)) * (BIG // 256),
    "docs/deep/nested/notes.txt": b"one\ntwo\n",
    "empty": b"",
    "bin/run.sh": ("x", b"#!/bin/sh\necho hi\n"),
    "latest": ("link", "README.md"),
    "ledger/objects/abc": b"\x00\x01binary\r\n" * 5000,
}


def test_c6_6a_a_cloned_worktree_is_the_plain_worktree_and_shares_its_blocks(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected
    assert allocation.method == "clone" and allocation.reason is None
    assert allocation.cloned == allocation.candidates == 6     # every regular file; the link is git's
    for rel in ("big.bin", "ledger/objects/abc"):
        assert private(tmp_path / "cloned" / rel) == 0
        assert private(tmp_path / "plain" / rel) > 0
    assert os.stat(tmp_path / "cloned" / "bin/run.sh").st_mode & 0o111


def test_c6_6a_a_caller_in_a_subdirectory_clones_from_its_checkouts_top(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    expected, got, allocation = both(tmp_path, source / "docs" / "deep")
    assert got == expected and allocation.cloned == 6


def test_c6_6a_files_the_caller_changed_are_written_by_git(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    write_files(source, {"README.md": b"HELLO\n",                       # same size, other bytes
                         "docs/deep/nested/notes.txt": b"longer now\n",   # other size
                         "ledger/objects/abc": ("link", "README.md"),     # a link where a file was
                         "untracked.txt": b"not tracked\n"})
    (source / "big.bin").unlink()
    expected, got, allocation = both(tmp_path, source)
    assert got == expected
    assert allocation.cloned == 2           # empty, bin/run.sh
    assert allocation.skipped == {"not the blob's bytes": 1, "changed in the caller's checkout": 1,
                                  "missing or not a file in the caller's checkout": 2}


def test_c6_6a_a_caller_at_another_commit_shares_only_the_blobs_both_hold(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    first = git(source, "rev-parse", "HEAD").strip()
    write_files(source, {"README.md": b"second\n", "new.txt": b"new\n"})
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "second")
    expected, got, allocation = both(tmp_path, source, first)
    assert got == expected
    assert allocation.candidates == 5 and allocation.cloned == 5     # README.md differs


def test_c6_6a_a_file_mode_the_caller_changed_is_restored_by_git(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    (source / "bin/run.sh").chmod(0o644)
    (source / "README.md").chmod(0o600)
    expected, got, _ = both(tmp_path, source)
    assert got == expected


@pytest.mark.parametrize("attributes,autocrlf,clones", [
    (None, None, True),
    ("* -text\n", None, True),
    ("* -text\n", "true", True),
    ("* binary\n", "true", True),
    ("* -crlf\n", "true", True),
    ("* text\n", None, False),
    ("* text=auto\n", None, False),
    ("* eol=crlf\n", None, False),
    ("* eol=lf\n", None, False),
    ("* crlf\n", None, False),
    ("* ident\n", None, False),
    ("* filter=upper\n", None, False),
    ("*.txt working-tree-encoding=UTF-16\n", None, False),
    (None, "true", False),
    (None, "yes", False),
    (None, "1", False),
    (None, "input", False),
    (None, "false", True),
    (None, "off", True),
])
def test_c6_6a_a_path_git_converts_on_checkout_is_never_cloned(tmp_path, attributes, autocrlf, clones):
    """The case that would differ is a checkout made before the config changed:
    its LF files are the blobs' bytes, a refresh calls them clean, but git now
    writes CRLF. Only paths git writes unchanged are cloned."""
    files = {"a.txt": b"one\ntwo\n" * 2000, "b.txt": b"$Id$\nthree\n"}
    if attributes and "working-tree-encoding" in attributes:
        files = {name: body.decode().encode("utf-16") for name, body in files.items()}
    source = make_repo(tmp_path / "repo", files, attributes)
    git(source, "config", "filter.upper.smudge", "tr a-z A-Z")
    git(source, "config", "filter.upper.clean", "tr A-Z a-z")
    if autocrlf is not None:
        git(source, "config", "core.autocrlf", autocrlf)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected
    converted = {"a.txt", "b.txt"}
    if clones:
        assert converted <= set(allocation.cloned_paths)
    else:
        assert not converted & set(allocation.cloned_paths)
        assert allocation.skipped.get("converted by git on checkout", 0) >= 2


@pytest.mark.parametrize("config", [
    {"core.ignoreStat": "true"},
    {"core.trustctime": "false"},
    {"core.checkStat": "minimal"},
    {"index.version": "4"},
    {"feature.manyFiles": "true"},
    {"core.untrackedCache": "true"},
    {"core.preloadIndex": "false"},
    {"core.symlinks": "false"},
    {"core.filemode": "false"},
    {"index.skipHash": "true"},
    {"core.splitIndex": "true"},
    {"core.fsmonitor": "false"},
])
def test_c6_6a_index_and_stat_config_never_keeps_a_clone_that_is_not_the_blob(tmp_path, config):
    """Whatever the index and stat settings, a clone of a file the caller rewrote
    in place (same size, other bytes) is written by git, and the worktree is the
    plain add's."""
    source = make_repo(tmp_path / "repo", FILES)
    for key, value in config.items():
        git(source, "config", key, value)
    (source / "big.bin").write_bytes(bytes(reversed(range(256))) * (BIG // 256))
    expected, got, allocation = both(tmp_path, source)
    assert got == expected
    assert "big.bin" not in allocation.cloned_paths
    assert private(tmp_path / "cloned" / "big.bin") > 0
    if config == {"core.ignoreStat": "true"}:
        assert allocation.reason == "core.ignoreStat" and allocation.cloned == 0
    else:
        assert allocation.cloned == 5


@pytest.mark.parametrize("value", ["true", "/no/such/hook"])
def test_c6_6a_an_fsmonitor_leaves_the_checkout_to_git(tmp_path, value):
    """git's own checkout marks entries fsmonitor-valid, which a refresh would
    not: the stage stands aside, and the worktree (and any fsmonitor daemon git
    starts for it) is the plain add's."""
    source = make_repo(tmp_path / "repo", FILES)
    git(source, "config", "core.fsmonitor", value)
    try:
        expected, got, allocation = both(tmp_path, source)
        assert got[0] == expected[0] and got[1]["index"] == expected[1]["index"]
        assert allocation.reason == "core.fsmonitor" and allocation.cloned == 0
        watching = [("is watching" in git(tmp_path / name, "fsmonitor--daemon", "status", check=False))
                    for name in ("plain", "cloned")]
        assert watching[0] == watching[1]
    finally:
        for name in ("plain", "cloned"):
            if (tmp_path / name).exists():
                git(tmp_path / name, "fsmonitor--daemon", "stop", check=False)


def test_c6_6a_a_caller_outside_any_worktree_is_left_to_git(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)
    commit = git(bare, "rev-parse", "HEAD").strip()
    assert plain_add(bare, tmp_path / "plain", commit).returncode == 0
    allocation = clone_add(bare, tmp_path / "cloned", commit)
    assert allocation.result.returncode == 0
    assert allocation.reason == "the caller's directory is in no worktree"
    assert checkout_state(tmp_path / "cloned") == checkout_state(tmp_path / "plain")


def test_c6_6a_a_sparse_checkout_is_left_to_git(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    git(source, "sparse-checkout", "set", "docs")
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.reason == "sparse checkout" and allocation.cloned == 0
    assert git_state(tmp_path / "cloned")["sparse"] is not None


def test_c6_6a_worktree_config_git_copies_is_kept(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    git(source, "config", "extensions.worktreeConfig", "true")
    git(source, "config", "--worktree", "core.sparseCheckout", "false")
    git(source, "config", "--worktree", "user.name", "Worktree User")
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.cloned == 6


def test_c6_6a_a_volume_that_cannot_clone_gets_the_plain_worktree(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "repo", FILES)

    def no_clones(src, folder, name, flags):
        ctypes.set_errno(errno.ENOTSUP)
        return -1
    monkeypatch.setattr(rfs, "_fclonefileat", no_clones)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.cloned == 0 and allocation.reason == "this volume cannot clone"


def test_c6_6a_another_volume_gets_the_plain_worktree(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "repo", FILES)
    real_stat = os.stat

    def stat_(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.path.realpath(path) == os.path.realpath(source):
            values = list(result)
            values[stat.ST_DEV] = result.st_dev + 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(wc.os, "stat", stat_)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.cloned == 0
    assert allocation.reason == "the caller's checkout is on another volume"


@pytest.mark.parametrize("mask", [0o022, 0o027, 0o002, 0o000, 0o077])
def test_c6_6a_modes_follow_the_index_and_the_umask(tmp_path, mask):
    source = make_repo(tmp_path / "repo", FILES)
    old = os.umask(mask)
    try:
        expected, got, allocation = both(tmp_path, source)
    finally:
        os.umask(old)
    assert got == expected and allocation.cloned == 6


def test_c6_6a_the_post_checkout_hook_runs_once_as_worktree_add_runs_it(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    log = tmp_path / "hook.log"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho "$1 $2 $3 $(basename "$(pwd)")" >> "{log}"\n')
    hook.chmod(0o755)
    expected, got, _ = both(tmp_path, source)
    assert got == expected
    head = git(source, "rev-parse", "HEAD").strip()
    assert log.read_text().splitlines() == [f"{'0' * 40} {head} 1 plain", f"{'0' * 40} {head} 1 cloned"]


def test_c6_6a_a_failing_post_checkout_hook_fails_the_allocation_as_it_fails_the_add(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 3\n")
    hook.chmod(0o755)
    assert plain_add(source, tmp_path / "plain", "HEAD").returncode != 0
    assert clone_add(source, tmp_path / "cloned", "HEAD").result.returncode != 0


def test_c6_6a_a_sha256_repository(tmp_path):
    source = make_repo(tmp_path / "repo", FILES, object_format="sha256")
    log = tmp_path / "hook.log"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\n')
    hook.chmod(0o755)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.cloned == 6
    assert log.read_text().splitlines() == ["0" * 64, "0" * 64]


def test_c6_6a_a_status_that_is_not_clean_hands_the_job_the_plain_add(tmp_path):
    """Two names one case-insensitive volume folds together: git's own checkout
    is not clean either, so the plain add decides, as it always did."""
    probe = tmp_path / "Case"
    probe.write_text("")
    if not (tmp_path / "case").exists():
        pytest.skip("case-sensitive volume")
    probe.unlink()
    source = tmp_path / "repo"
    source.mkdir()
    git(source, "init", "-q", "-b", "task/example")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.invalid")
    blob_a = subprocess.run(["git", "-C", str(source), "hash-object", "-w", "--stdin"], input=b"a\n",
                            capture_output=True, check=True).stdout.decode().strip()
    blob_b = subprocess.run(["git", "-C", str(source), "hash-object", "-w", "--stdin"], input=b"bb\n",
                            capture_output=True, check=True).stdout.decode().strip()
    git(source, "update-index", "--add", "--cacheinfo", f"100644,{blob_a},Name.txt")
    git(source, "update-index", "--add", "--cacheinfo", f"100644,{blob_b},name.txt")
    git(source, "commit", "-q", "-m", "case pair")
    git(source, "checkout", "-q", "-f", "HEAD")
    log = tmp_path / "hook.log"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho ran >> "{log}"\n')
    hook.chmod(0o755)
    log.unlink(missing_ok=True)
    allocation = clone_add(source, tmp_path / "cloned", "HEAD")
    assert allocation.result.returncode == 0 and allocation.method == "plain"
    assert "not clean" in allocation.reason
    assert log.read_text().splitlines() == ["ran"]          # the plain add's own run, once
    plain_add(source, tmp_path / "plain", "HEAD")
    assert checkout_state(tmp_path / "cloned")[0] == checkout_state(tmp_path / "plain")[0]


def test_c6_6a_a_refused_add_is_returned_as_the_plain_add_returns_it(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    plain = plain_add(source, tmp_path / "plain", "no-such-commit")
    allocation = clone_add(source, tmp_path / "cloned", "no-such-commit")
    assert plain.returncode == allocation.result.returncode != 0
    assert not (tmp_path / "cloned").exists()


def test_c6_6a_a_timeout_names_the_add_and_the_whole_cap(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "repo", FILES)
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "reset" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(wc.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        clone_add(source, tmp_path / "cloned", "HEAD")
    assert caught.value.cmd[3:5] == ["worktree", "add"] and caught.value.timeout == 120


def test_c6_6a_a_refresh_cut_short_leaves_git_to_write_everything(tmp_path, monkeypatch):
    """The stage's share of the cap ends inside the refresh: its lock is cleared,
    and the reset writes every file the refresh never marked up to date."""
    source = make_repo(tmp_path / "repo", FILES)
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "update-index" in cmd:
            git_dir = git(cmd[2], "rev-parse", "--path-format=absolute", "--git-dir").strip()
            Path(git_dir, "index.lock").write_bytes(b"")
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(wc.subprocess, "run", run)
    expected, got, allocation = both(tmp_path, source)
    assert got == expected and allocation.cloned == 0
    assert allocation.reason == "clone stage reached its share of the cap"
    for rel in ("big.bin", "ledger/objects/abc"):
        assert private(tmp_path / "cloned" / rel) > 0


def test_c6_6a_off_runs_the_one_plain_add(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "repo", FILES)
    commands = []
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        commands.append(cmd)
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(wc.subprocess, "run", run)
    monkeypatch.setenv(wc.CLONE_ENV, "0")
    allocation = clone_add(source, tmp_path / "cloned", "HEAD")
    assert allocation.result.returncode == 0 and allocation.method == "plain"
    assert commands == [["git", "-C", str(source), "worktree", "add", "--detach", str(tmp_path / "cloned"), "HEAD"]]


def _setxattr(path: Path, name: str, value: bytes) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    assert libc.setxattr(os.fsencode(path), name.encode(), value, len(value), 0, 0) == 0


def test_c6_6a_metadata_git_would_not_write_is_never_cloned(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    _setxattr(source / "big.bin", "org.example.tag", b"x")
    os.chflags(source / "README.md", stat.UF_NODUMP)
    try:
        expected, got, allocation = both(tmp_path, source)
    finally:
        os.chflags(source / "README.md", 0)
    assert got == expected
    assert allocation.skipped == {"metadata git would not write": 1, "file flags in the caller's checkout": 1}
    fd = os.open(tmp_path / "cloned" / "big.bin", os.O_RDONLY)
    try:
        assert wc._has_xattrs(fd) is False
    finally:
        os.close(fd)


def test_c6_6a_a_fifo_or_directory_where_a_file_was_never_blocks_or_clones(tmp_path):
    source = make_repo(tmp_path / "repo", FILES)
    (source / "README.md").unlink()
    os.mkfifo(source / "README.md")
    (source / "empty").unlink()
    (source / "empty").mkdir()
    expected_dir = tmp_path / "plain"
    git(source, "worktree", "add", "--detach", str(expected_dir), "HEAD")
    allocation = clone_add(source, tmp_path / "cloned", "HEAD")
    assert allocation.result.returncode == 0
    assert checkout_state(tmp_path / "cloned") == checkout_state(expected_dir)
    assert allocation.skipped == {"changed in the caller's checkout": 2}     # the fifo and the directory


# --- properties ------------------------------------------------------------------

NAMES = st.sampled_from(["a", "b", "c.txt", "d/e", "d/f.bin", "g/h/i", "g/j", "k l", "ü.txt", "m.sh"])
CONTENT = st.one_of(st.binary(max_size=64), st.binary(min_size=4096, max_size=9000),
                    st.sampled_from([b"", b"one\ntwo\n", b"crlf\r\nline\r\n", b"$Id$\n"]))
ENTRY = st.one_of(CONTENT, st.tuples(st.just("x"), CONTENT), st.tuples(st.just("link"), st.sampled_from(["a", "zz"])))
CHANGE = st.sampled_from(["keep", "rewrite", "delete", "chmod", "link", "grow"])
ATTRIBUTES = st.sampled_from([None, "* -text\n", "*.txt text\n", "*.bin binary\n", "d/** -text\n*.txt eol=crlf\n"])
AUTOCRLF = st.sampled_from([None, "true", "false", "input"])


def _valid(files: dict) -> bool:
    """No name is both a file and a folder."""
    names = set(files)
    return not any(other.startswith(name + "/") for name in names for other in names)


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(files=st.dictionaries(NAMES, ENTRY, min_size=1, max_size=8).filter(_valid),
       changes=st.lists(CHANGE, min_size=8, max_size=8), attributes=ATTRIBUTES, autocrlf=AUTOCRLF,
       mask=st.sampled_from([0o022, 0o027, 0o002]))
def test_c6_6a_property_whatever_the_caller_holds_the_worktree_is_the_plain_worktree(
        tmp_path_factory, files, changes, attributes, autocrlf, mask):
    """For every tree, attribute set, `core.autocrlf` and umask, and whatever the
    caller's checkout has done to its files since: the allocated worktree is the
    plain add's, the caller's checkout is untouched, and every kept clone shares
    all its blocks."""
    tmp_path = tmp_path_factory.mktemp("prop")
    source = make_repo(tmp_path / "repo", files, attributes)
    if autocrlf is not None:
        git(source, "config", "core.autocrlf", autocrlf)
    for (rel, value), change in zip(sorted(files.items()), changes):
        path = source / rel
        if change == "rewrite" and not path.is_symlink():
            path.write_bytes(path.read_bytes()[::-1] + b"!")
        elif change == "delete":
            path.unlink()
        elif change == "chmod" and not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) ^ 0o111)
        elif change == "link":
            path.unlink()
            path.symlink_to("elsewhere")
        elif change == "grow" and not path.is_symlink():
            with open(path, "ab") as handle:
                handle.write(b"more")
    old = os.umask(mask)
    try:
        expected, got, allocation = both(tmp_path, source)
    finally:
        os.umask(old)
    assert got == expected
    assert allocation.cloned == len(allocation.cloned_paths)
    for rel in allocation.cloned_paths:
        assert got[0][rel][2] == expected[0][rel][2]
        if len(got[0][rel][2]) >= 4096:
            assert private(tmp_path / "cloned" / rel) == 0
