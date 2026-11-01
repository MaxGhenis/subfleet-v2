"""The installed Git is the oracle for retention's process-free census probe."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import retention_git as rgit
from subfleet.launch_env import helper_policy


KINDS = (
    "plain", "subfolder", "linked", "linked-subfolder", "submodule", "bare", "nested",
    "no-repository", "symlinked-folder", "broken-gitfile", "missing-gitfile-target",
    "relative-commondir", "empty-git-folder", "malformed-config", "unsupported-format",
)


@dataclass(frozen=True)
class Shape:
    kind: str
    children: tuple[str, ...] = ()
    symlink: bool = False
    absolute_gitfile: bool = True
    absolute_commondir: bool = False
    suffix: str = ""


@st.composite
def shapes(draw):
    """Compose repository kinds with nesting, pathname spelling and pointers."""
    return Shape(
        kind=draw(st.sampled_from(KINDS)),
        children=tuple(draw(st.lists(st.sampled_from(["child", "sp ace", "é", "日本", " leading", "trailing "]),
                                   max_size=3))),
        symlink=draw(st.booleans()),
        absolute_gitfile=draw(st.booleans()),
        absolute_commondir=draw(st.booleans()),
        suffix=draw(st.sampled_from(["", " space", " ", "é"])),
    )


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=retention test",
                             "-c", "user.email=retention@example.invalid", *map(str, args)],
                            cwd=cwd, env=rgit.environment(), capture_output=True, timeout=30)
    assert result.returncode == 0, (args, result.stderr)
    return result


def repository(base: Path, name: str = "repo", *, commit: bool = False, bare: bool = False) -> Path:
    path = base / name
    git(base, "init", "-q", *(["--bare"] if bare else []), str(path))
    if commit:
        git(path, "commit", "--allow-empty", "-qm", "seed")
    return path


def build_shape(base: Path, shape: Shape) -> Path:
    kind = shape.kind
    if kind == "bare":
        path = repository(base, "owner.git" + shape.suffix, bare=True)
    elif kind in {"no-repository", "broken-gitfile", "missing-gitfile-target", "empty-git-folder"}:
        path = base / ("checkout" + shape.suffix)
        path.mkdir()
        if kind == "broken-gitfile":
            (path / ".git").write_text("gitdir:without-a-space\n")
        elif kind == "missing-gitfile-target":
            (path / ".git").write_text("gitdir: ../missing\n")
        elif kind == "empty-git-folder":
            (path / ".git").mkdir()
    else:
        owner = repository(base, "owner" + shape.suffix,
                           commit=kind in {"linked", "linked-subfolder", "relative-commondir", "submodule"})
        path = owner
        if kind in {"linked", "linked-subfolder", "relative-commondir"}:
            path = base / ("checkout" + shape.suffix)
            git(owner, "worktree", "add", "--detach", "-q", str(path), "HEAD")
        elif kind == "submodule":
            source = repository(base, "module-source", commit=True)
            git(owner, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "module")
            path = owner / "module"
        elif kind == "nested":
            path = repository(owner, "nested" + shape.suffix)
        elif kind == "malformed-config":
            with (owner / ".git" / "config").open("a") as handle:
                handle.write("[malformed\n")
        elif kind == "unsupported-format":
            config = owner / ".git" / "config"
            config.write_text(config.read_text().replace("repositoryformatversion = 0", "repositoryformatversion = 999"))
        if (path / ".git").is_file():
            admin = Path((path / ".git").read_text().removeprefix("gitdir: ").rstrip("\n"))
            if not admin.is_absolute():
                admin = path / admin
            admin = admin.resolve()
            target = str(admin) if shape.absolute_gitfile else os.path.relpath(admin, path)
            (path / ".git").write_text("gitdir: " + target + "\n")
            if (admin / "commondir").exists():
                common = (admin / (admin / "commondir").read_text().rstrip("\n")).resolve()
                target = (str(common) if shape.absolute_commondir and kind != "relative-commondir"
                          else os.path.relpath(common, admin))
                (admin / "commondir").write_text(target + "\n")
    if kind in {"subfolder", "linked-subfolder"}:
        path = path / "subfolder"
        path.mkdir()
    for name in shape.children:
        path = path / name
        path.mkdir()
    if shape.symlink or kind == "symlinked-folder":
        alias = base / "alias"
        alias.symlink_to(path, target_is_directory=True)
        path = alias
    return path


def assert_matches_git(path: Path) -> None:
    expected = rgit.common_dir(path)
    assert rgit.fast_common_dir(path) == expected
    # The probe has no process-local or filesystem memoization to go stale.
    assert rgit.fast_common_dir(path) == expected


@pytest.mark.parametrize("kind", KINDS)
def test_generated_matrix_matches_real_git(tmp_path, kind):
    assert_matches_git(build_shape(tmp_path, Shape(kind)))


@settings(max_examples=65, deadline=None, derandomize=True)
@given(shapes())
def test_composed_shapes_match_real_git_and_are_deterministic(shape):
    with tempfile.TemporaryDirectory(prefix="retention-common-", dir=os.environ.get("TMPDIR")) as folder:
        assert_matches_git(build_shape(Path(folder), shape))


@pytest.mark.parametrize("kind", ["plain", "subfolder", "linked", "linked-subfolder", "submodule",
                                  "nested", "symlinked-folder", "relative-commondir"])
def test_ordinary_shapes_do_not_start_a_git_subprocess(tmp_path, monkeypatch, kind):
    path = build_shape(tmp_path, Shape(kind))
    expected = rgit.common_dir(path)

    def no_process(*args, **kwargs):
        pytest.fail("ordinary repository discovery started a Git subprocess")

    monkeypatch.setattr(rgit, "run", no_process)
    assert expected is not None
    assert rgit.fast_common_dir(path) == expected


@pytest.mark.parametrize("kind", ["bare", "no-repository", "broken-gitfile", "missing-gitfile-target",
                                  "empty-git-folder", "malformed-config", "unsupported-format"])
def test_uncertain_shapes_execute_the_git_fallback(tmp_path, monkeypatch, kind):
    path = build_shape(tmp_path, Shape(kind))
    expected = rgit.common_dir(path)
    original = rgit.common_dir
    calls = []

    def fallback(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(rgit, "common_dir", fallback)
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


def test_unreadable_pointer_falls_back_without_hanging(tmp_path, monkeypatch):
    path = build_shape(tmp_path, Shape("linked"))
    original = rgit.rfs.read_regular
    expected = rgit.common_dir(path)

    def unreadable(name, **kwargs):
        if name == path / ".git":
            raise PermissionError("synthetic unreadable gitfile")
        return original(name, **kwargs)

    calls = []
    monkeypatch.setattr(rgit.rfs, "read_regular", unreadable)
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or expected)
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


def test_fallback_uses_only_the_remaining_pass_budget(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(kwargs["timeout"]))
    assert rgit.fast_common_dir(tmp_path, timeout=60, deadline=9, clock=lambda: 7) is None
    assert calls == [2]


def test_checkpoint_after_fallback_does_not_publish_expired_answer(tmp_path, monkeypatch):
    now = [0.0]

    class Deferred(Exception):
        pass

    def checkpoint():
        if now[0] >= 1:
            raise Deferred

    def fallback(*args, **kwargs):
        now[0] = 1
        return tmp_path / "must-not-publish"

    monkeypatch.setattr(rgit, "common_dir", fallback)
    with pytest.raises(Deferred):
        rgit.fast_common_dir(tmp_path, checkpoint=checkpoint, deadline=1, clock=lambda: now[0])


def test_cancelled_fast_probe_raises_without_spawning_git(tmp_path, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: pytest.fail("cancelled probe reached Git"))
    with pytest.raises(rgit.Cancelled):
        rgit.fast_common_dir(tmp_path, cancel=cancel)


def test_bare_repository_inside_a_checkout_is_not_skipped(tmp_path):
    outer = repository(tmp_path)
    bare = repository(outer, "nested.git", bare=True)
    child = bare / "objects" / "child"
    child.mkdir()
    assert_matches_git(child)


def test_invalid_inner_dotgit_keeps_gits_parent_discovery_semantics(tmp_path):
    outer = repository(tmp_path)
    inner = outer / "inner"
    inner.mkdir()
    (inner / ".git").mkdir()
    assert_matches_git(inner)


def test_probe_inside_git_admin_directory_uses_git_fallback(tmp_path, monkeypatch):
    path = repository(tmp_path) / ".git" / "objects"
    expected = rgit.common_dir(path)
    original = rgit.common_dir
    calls = []
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or original(*args, **kwargs))
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


@pytest.mark.parametrize("mutation", ["detached-long-head", "missing-common", "symlink-gitdir", "symlink-common",
                                     "bad-boolean", "unknown-extension", "unicode-config-whitespace", "bare-cr-config",
                                     "missing-core-worktree"])
def test_unusual_metadata_is_left_to_git(tmp_path, monkeypatch, mutation):
    path = repository(tmp_path)
    admin = path / ".git"
    if mutation == "detached-long-head":
        (admin / "HEAD").write_text("a" * 64 + "\n")
    elif mutation == "missing-common":
        (admin / "commondir").write_text("../missing\n")
    elif mutation == "symlink-gitdir":
        metadata = tmp_path / "metadata"
        admin.rename(metadata)
        admin.symlink_to(metadata, target_is_directory=True)
    elif mutation == "symlink-common":
        pointer = tmp_path / "common-pointer"
        pointer.write_text(".\n")
        (admin / "commondir").symlink_to(pointer)
    elif mutation == "bad-boolean":
        with (admin / "config").open("a") as handle:
            handle.write("[core]\n bare = unparseable-boolean\n")
    elif mutation == "unknown-extension":
        config = admin / "config"
        config.write_text(config.read_text().replace("repositoryformatversion = 0", "repositoryformatversion = 1")
                          + "[extensions]\n unrecognizedRetWalk = true\n")
    elif mutation == "unicode-config-whitespace":
        (admin / "config").write_text("\u2003[core]\n repositoryformatversion = 0\n")
    elif mutation == "bare-cr-config":
        (admin / "config").write_bytes(b"[core]\rrepositoryformatversion = 0\n")
    elif mutation == "missing-core-worktree":
        with (admin / "config").open("a") as handle:
            handle.write("[core]\n worktree = ../missing\n")
    expected = rgit.common_dir(path)
    original = rgit.common_dir
    calls = []
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or original(*args, **kwargs))
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


@pytest.mark.parametrize("override", ["ceiling", "config"])
def test_explicit_git_discovery_environment_is_left_to_git(tmp_path, monkeypatch, override):
    owner = repository(tmp_path)
    path = owner / "child"
    path.mkdir()
    values = ({"GIT_CEILING_DIRECTORIES": str(owner)} if override == "ceiling"
              else {"GIT_CONFIG_COUNT": "invalid"})
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    original = rgit.common_dir
    calls = []
    with helper_policy({"env": {"pass": list(values)}}):
        expected = rgit.common_dir(path)
        assert expected is None
        monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or original(*args, **kwargs))
        assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


@pytest.mark.parametrize("override", ["ceiling", "config"])
def test_unpassed_ambient_git_settings_do_not_disable_fast_discovery(tmp_path, monkeypatch, override):
    owner = repository(tmp_path)
    path = owner / "child"
    path.mkdir()
    key, value = (("GIT_CEILING_DIRECTORIES", str(owner)) if override == "ceiling"
                  else ("GIT_CONFIG_COUNT", "invalid"))
    monkeypatch.setenv(key, value)
    expected = rgit.common_dir(path)
    assert expected is not None
    monkeypatch.setattr(rgit, "run", lambda *args, **kwargs: pytest.fail("filtered ambient Git setting forced a process"))
    assert rgit.fast_common_dir(path) == expected
