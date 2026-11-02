"""The installed Git is the oracle for retention's process-free census probe."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import retention_git as rgit
from subfleet.launch_env import helper_policy


KINDS = (
    "plain", "subfolder", "linked", "linked-subfolder", "submodule", "bare", "nested",
    "no-repository", "symlinked-folder", "broken-gitfile", "missing-gitfile-target",
    "relative-commondir", "empty-git-folder", "malformed-config", "unsupported-format",
    "worktree-config", "worktree-config-linked", "sparse-checkout", "hooks-path", "gh-branch",
)

#: Config this probe answers for: the shapes the live census meets (2026-11-01).
SAFE_LINES = (
    "[core]\n\thookspath = /dev/null\n",
    "[core]\n\thookspath = ~nosuchuser/x\n",
    "[core]\n\tsparsecheckout = true\n",
    "[core]\n\tsparsecheckoutcone = false\n",
    "[core]\n\tuntrackedcache = maybe\n",
    '[branch "a"]\n\tgithub-pr-owner-number = "owner#repository#1"\n',
    '[branch "a"]\n\tgithub-pr-base-branch = "owner#repository#main"\n',
    '[branch "x"]\n\trebase = maybe\n',
    '[branch "x"]\n\tmerge = \n',
    '[remote "o"]\n\turl = \n',
    '[remote "o"]\n\tfetch = bogus\n',
    '[remote "o"]\n\tmirror = maybe\n',
    '[submodule "m"]\n\tactive = true\n',
    '[submodule "m"]\n\tactive = maybe\n',
    '[submodule "m"]\n\tupdate = bogus\n',
    '[submodule "m"]\n\turl = \n',
    "[submodule]\n\tactive = .\n",
    "[user]\n\tname = x\n",
    "[user]\n\temail = x@example.invalid\n",
    "[pull]\n\trebase = maybe\n",
    "[pull]\n\tff = maybe\n",
    "[http]\n\tpostbuffer = abc\n",
    "[http]\n\tversion = abc\n",
    "[rerere]\n\tenabled = maybe\n",
    "[gc]\n\tauto = abc\n",
    "[gc]\n\treflogexpire = abc\n",
    "[gc]\n\treflogexpireunreachable = abc\n",
    "[blame]\n\tignorerevsfile = abc\n",
    "[extensions]\n\tworktreeconfig = true\n",
    "[extensions]\n\tworktreeconfig = false\n",
    "[extensions]\n\tpartialclone = origin\n",
)
#: Config Git may refuse, or that this probe will not read for itself.
DOUBTFUL_LINES = (
    "[core]\n\tsparsecheckout = maybe\n",
    "[core]\n\tsparsecheckoutcone = maybe\n",
    "[core]\n\tfilemode = maybe\n",
    "[core]\n\tcompression = abc\n",
    "[core]\n\trepositoryformatversion = 2\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tfrobnicate = 1\n",
    "[extensions]\n\tfrobnicate = 1\n",
    "[extensions]\n\tworktreeconfig = maybe\n",
    "[index]\n\tsparse = maybe\n",
    "[push]\n\tdefault = bogus\n",
    "[branch]\n\tautosetupmerge = maybe\n",
    "[user]\n\tuseconfigonly = maybe\n",
    '[branch "a"]\n\tx = "unterminated\n',
    "[core.dotted]\n\tkey = 1\n",
    '[core "sub"]\n\tbare = maybe\n',
    '[color "advice"]\n\thint = notacolor\n',
    '[unknown "sub"]\n\tkey = value\n',
    "[include]\n\tpath = elsewhere\n",
    '[includeIf "gitdir:/"]\n\tpath = elsewhere\n',
    '[include "sub"]\n\tpath = elsewhere\n',
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
                           commit=kind in {"linked", "linked-subfolder", "relative-commondir", "submodule",
                                           "worktree-config-linked", "sparse-checkout"})
        path = owner
        if kind in {"linked", "linked-subfolder", "relative-commondir", "worktree-config-linked"}:
            path = base / ("checkout" + shape.suffix)
            git(owner, "worktree", "add", "--detach", "-q", str(path), "HEAD")
        if kind in {"worktree-config", "worktree-config-linked"}:
            git(owner, "config", "extensions.worktreeConfig", "true")
            git(path, "config", "--worktree", "core.sparseCheckout", "true")
            git(path, "config", "--worktree", "core.untrackedCache", "true")
        elif kind == "sparse-checkout":
            git(owner, "sparse-checkout", "set", "--cone", "kept")
        elif kind == "hooks-path":
            git(owner, "config", "core.hooksPath", "/dev/null")
        elif kind == "gh-branch":
            with (owner / ".git" / "config").open("a") as handle:
                handle.write('[branch "feat"]\n\tgithub-pr-owner-number = "owner#repository#1"\n')
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


def no_process(*args, **kwargs):
    pytest.fail("ordinary repository discovery started a Git subprocess")


@pytest.mark.parametrize("kind", ["plain", "subfolder", "linked", "linked-subfolder", "submodule",
                                  "nested", "symlinked-folder", "relative-commondir", "worktree-config",
                                  "worktree-config-linked", "sparse-checkout", "hooks-path", "gh-branch"])
def test_ordinary_shapes_do_not_start_a_git_subprocess(tmp_path, monkeypatch, kind):
    path = build_shape(tmp_path, Shape(kind))
    expected = rgit.common_dir(path)
    monkeypatch.setattr(rgit, "run", no_process)
    assert expected is not None
    assert rgit.fast_common_dir(path) == expected


def test_no_repository_up_to_the_root_needs_no_process(tmp_path, monkeypatch):
    path = build_shape(tmp_path, Shape("no-repository", children=("deep", "er")))
    assert rgit.common_dir(path) is None
    monkeypatch.setattr(rgit, "run", no_process)
    assert rgit.fast_common_dir(path) is None


def test_a_filesystem_boundary_is_left_to_git(tmp_path, monkeypatch):
    """Only the root proves a negative: another device above asks Git."""
    path = build_shape(tmp_path, Shape("no-repository"))
    real = os.path.realpath(path)
    original = Path.stat

    def other_device(self, **kwargs):
        st = original(self, **kwargs)
        if str(self) == real:
            return SimpleNamespace(st_dev=st.st_dev + 1, st_mode=st.st_mode)
        return st

    calls = []
    monkeypatch.setattr(Path, "stat", other_device)
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]))
    assert rgit.fast_common_dir(path) is None
    assert calls == [path]


def _with_config(base: Path, lines: Iterable[str]) -> Path:
    path = repository(base, "owner")
    with (path / ".git" / "config").open("a") as handle:
        handle.write("".join(lines))
    return path


@pytest.mark.parametrize("line", SAFE_LINES + DOUBTFUL_LINES)
def test_config_line_matches_real_git(tmp_path, line):
    assert_matches_git(_with_config(tmp_path, [line]))


@pytest.mark.parametrize("line", SAFE_LINES)
def test_safe_config_needs_no_process(tmp_path, monkeypatch, line):
    path = _with_config(tmp_path, [line])
    expected = rgit.common_dir(path)
    assert expected is not None
    monkeypatch.setattr(rgit, "run", no_process)
    assert rgit.fast_common_dir(path) == expected


@pytest.mark.parametrize("line", DOUBTFUL_LINES)
def test_doubtful_config_goes_to_git(tmp_path, monkeypatch, line):
    path = _with_config(tmp_path, [line])
    expected = rgit.common_dir(path)
    original = rgit.common_dir
    calls = []
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or original(*args, **kwargs))
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


@pytest.mark.parametrize("line", ["[include]\n\tpath = extra\n", '[includeIf "gitdir:/"]\n\tpath = extra\n'],
                         ids=["include", "include-if"])
def test_config_an_include_brings_in_fails_git_and_the_probe_alike(tmp_path, line):
    path = _with_config(tmp_path, [line])
    (path / ".git" / "extra").write_text("[core]\n\tsparsecheckout = maybe\n")
    assert rgit.common_dir(path) is None
    assert_matches_git(path)


@settings(max_examples=40, deadline=None, derandomize=True)
@given(st.lists(st.sampled_from(SAFE_LINES + DOUBTFUL_LINES), max_size=5),
       st.sampled_from(["", "[core]\n\trepositoryformatversion = 1\n"]), st.booleans())
def test_composed_config_matches_real_git(lines, version, linked):
    with tempfile.TemporaryDirectory(prefix="retention-config-", dir=os.environ.get("TMPDIR")) as folder:
        base = Path(folder)
        owner = _with_config(base, [])
        path = owner
        if linked:
            git(owner, "commit", "--allow-empty", "-qm", "seed")
            path = base / "checkout"
            git(owner, "worktree", "add", "--detach", "-q", str(path), "HEAD")
        with (owner / ".git" / "config").open("a") as handle:
            handle.write(version + "".join(lines))
        assert_matches_git(path)


WORKTREE_FILES = ("[core]\n\tbare = true\n", "[core]\n\tbare = false\n", "[core]\n\tworktree = /nonexistent-ret-walk\n",
                  "[core]\n\tsparsecheckout = maybe\n", "[core]\n\tsparsecheckout = true\n",
                  "[core]\n\tuntrackedcache = maybe\n", "[core\n", "[index]\n\tsparse = maybe\n",
                  "[extensions]\n\tworktreeconfig = true\n",
                  '[color "advice"]\n\thint = notacolor\n',
                  '[remote "o"]\n\tfetch = bogus\n', '[core "sub"]\n\tbare = false\n')


@pytest.mark.parametrize("text", WORKTREE_FILES)
@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
@pytest.mark.parametrize("where", ["admin", "common"])
def test_config_worktree_matches_real_git(tmp_path, text, enabled, where):
    """Git reads the checkout's own file, and only with the extension on."""
    path = repository(tmp_path, "owner", commit=True)
    checkout = tmp_path / "checkout"
    git(path, "worktree", "add", "--detach", "-q", str(checkout), "HEAD")
    if enabled:
        git(path, "config", "extensions.worktreeConfig", "true")
    folder = path / ".git" / "worktrees" / "checkout" if where == "admin" else path / ".git"
    (folder / "config.worktree").write_text(text)
    assert_matches_git(checkout)
    assert_matches_git(path)


@pytest.mark.parametrize("section", ["remote", "branch", "submodule", "unknown", "core"])
def test_config_worktree_subsections_are_always_left_to_git(tmp_path, monkeypatch, section):
    path = repository(tmp_path, "owner", commit=True)
    git(path, "config", "extensions.worktreeConfig", "true")
    (path / ".git" / "config.worktree").write_text(f'[{section} "sub"]\n\tkey = value\n')
    expected = rgit.common_dir(path)
    original = rgit.common_dir
    calls = []
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(args[0]) or original(*args, **kwargs))
    assert rgit.fast_common_dir(path) == expected
    assert calls == [path]


@pytest.mark.parametrize("kind", ["bare", "broken-gitfile", "missing-gitfile-target",
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
    path = repository(tmp_path, bare=True)
    monkeypatch.setattr(rgit, "common_dir", lambda *args, **kwargs: calls.append(kwargs["timeout"]))
    assert rgit.fast_common_dir(path, timeout=60, deadline=9, clock=lambda: 7) is None
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

    path = repository(tmp_path, bare=True)
    monkeypatch.setattr(rgit, "common_dir", fallback)
    with pytest.raises(Deferred):
        rgit.fast_common_dir(path, checkpoint=checkpoint, deadline=1, clock=lambda: now[0])


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


SALVAGE = "refs/subfleet-salvage/"
REF_STEPS = ("add", "add-beside", "add-prefix-tail", "pack", "delete", "delete-beside")


def _apply_refs(path: Path, steps: Iterable[str]) -> None:
    head = git(path, "rev-parse", "HEAD").stdout.decode().strip()
    names = {"add": SALVAGE + "job-a1", "add-beside": "refs/subfleet-salvage-other/job-a1",
             "add-prefix-tail": "refs/heads/refs/subfleet-salvage/job-a1", "delete": SALVAGE + "job-a1",
             "delete-beside": "refs/subfleet-salvage-other/job-a1"}
    for step in steps:
        if step == "pack":
            git(path, "pack-refs", "--all", "--prune")
        elif step.startswith("delete"):
            subprocess.run(["git", "update-ref", "-d", names[step]], cwd=path, env=rgit.environment(), capture_output=True)
        else:
            git(path, "update-ref", names[step], head)


@settings(max_examples=30, deadline=None, derandomize=True)
@given(st.lists(st.sampled_from(REF_STEPS), max_size=6))
def test_no_refs_under_never_contradicts_git(steps):
    with tempfile.TemporaryDirectory(prefix="retention-refs-", dir=os.environ.get("TMPDIR")) as folder:
        path = repository(Path(folder), commit=True)
        _apply_refs(path, steps)
        common = path / ".git"
        listed = rgit.refs_under(common, SALVAGE)
        if rgit.no_refs_under(common, SALVAGE):
            assert listed == {}


@pytest.mark.parametrize("steps,proved,empty", [
    ((), True, True), (("add",), False, False), (("add", "pack"), False, False),
    (("add-beside",), True, True), (("add-beside", "pack"), True, True), (("add-prefix-tail", "pack"), True, True),
    # A delete may leave an empty folder, which proves nothing: None takes either
    # answer, and Git says none. Whether the folder stays is Git's own business.
    (("add", "delete"), None, True), (("add", "pack", "delete"), None, True),
], ids=lambda value: "-".join(value) if isinstance(value, tuple) else None)
def test_no_refs_under_answers_the_plain_store(tmp_path, steps, proved, empty):
    path = repository(tmp_path, commit=True)
    _apply_refs(path, steps)
    if proved is not None:
        assert rgit.no_refs_under(path / ".git", SALVAGE) is proved
    assert (rgit.refs_under(path / ".git", SALVAGE) == {}) is empty


@pytest.mark.parametrize("steps,ref,proved", [
    (("add",), SALVAGE + "job-a1", False), (("add", "pack"), SALVAGE + "job-a1", False),
    (("add",), SALVAGE + "job-a2", True), (("add", "pack"), SALVAGE + "job-a2", True),
    (("add", "delete"), SALVAGE + "job-a1", True), (("add", "pack"), SALVAGE + "job-a", False),
], ids=str)
def test_no_refs_under_takes_a_whole_ref_name(tmp_path, steps, ref, proved):
    """`job-a` is only a prefix of a packed `job-a1`, so that one stays with Git."""
    path = repository(tmp_path, commit=True)
    _apply_refs(path, steps)
    assert rgit.no_refs_under(path / ".git", ref) is proved


def test_no_refs_under_leaves_a_reftable_store_to_git(tmp_path):
    path = tmp_path / "reftable"
    git(tmp_path, "init", "-q", "--ref-format=reftable", str(path))
    git(path, "commit", "--allow-empty", "-qm", "seed")
    head = git(path, "rev-parse", "HEAD").stdout.decode().strip()
    git(path, "update-ref", SALVAGE + "job-a1", head)
    assert rgit.no_refs_under(path / ".git", SALVAGE) is False
    assert set(rgit.refs_under(path / ".git", SALVAGE)) == {SALVAGE + "job-a1"}


def test_no_refs_under_leaves_a_redirected_common_directory_to_git(tmp_path):
    """Git's --git-dir follows commondir again even after census discovery."""
    owner = repository(tmp_path, commit=True)
    _apply_refs(owner, ("add",))
    common = tmp_path / "redirect.git"
    (common / "objects").mkdir(parents=True)
    (common / "refs").mkdir()
    (common / "HEAD").write_text("ref: refs/heads/main\n")
    (common / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (common / "commondir").write_text(str(owner / ".git") + "\n")
    admin = common / "worktrees" / "checkout"
    admin.mkdir(parents=True)
    (admin / "HEAD").write_bytes(git(owner, "rev-parse", "HEAD").stdout)
    (admin / "commondir").write_text("../..\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "gitdir").write_text(str(checkout / ".git") + "\n")

    assert rgit.fast_common_dir(checkout) == rgit.common_dir(checkout) == common.resolve()
    assert rgit.no_refs_under(common, SALVAGE + "job-a1") is False
    assert set(rgit.refs_under(common, SALVAGE)) == {SALVAGE + "job-a1"}


@pytest.mark.parametrize("spelling", ["NFC", "NFD"])
def test_no_refs_under_leaves_unicode_normalization_to_git(tmp_path, spelling):
    path = repository(tmp_path, commit=True)
    git(path, "config", "core.precomposeunicode", "true")
    ref = unicodedata.normalize("NFC", SALVAGE + "café-a1")
    git(path, "update-ref", ref, "HEAD")
    git(path, "pack-refs", "--all", "--prune")
    prefix = unicodedata.normalize(spelling, ref)
    assert rgit.no_refs_under(path / ".git", prefix) is False
    # Git normalizes the NFD argument on macOS; other platforms may leave it
    # unmatched. In both cases Git owns the answer for this spelling.
    assert set(rgit.refs_under(path / ".git", prefix)).issubset({ref})
    assert set(rgit.refs_under(path / ".git", ref)) == {ref}


@pytest.mark.parametrize("tail", ["job-a*", "job-a?", "job-a[1]", r"job-a\1"])
def test_no_refs_under_leaves_pattern_matching_to_git(tmp_path, tail):
    path = repository(tmp_path, commit=True)
    _apply_refs(path, ("add", "pack"))
    prefix = SALVAGE + tail
    assert rgit.no_refs_under(path / ".git", prefix) is False
    assert set(rgit.refs_under(path / ".git", prefix)) == {SALVAGE + "job-a1"}


def test_no_refs_under_refuses_even_a_dangling_commondir(tmp_path):
    path = repository(tmp_path)
    common = path / ".git"
    (common / "commondir").symlink_to(tmp_path / "missing")
    assert rgit.no_refs_under(common, SALVAGE) is False


def test_no_refs_under_leaves_an_oversized_or_unreadable_store_to_git(tmp_path, monkeypatch):
    path = repository(tmp_path, commit=True)
    _apply_refs(path, ("add-beside", "pack"))
    assert rgit.no_refs_under(path / ".git", SALVAGE) is True
    monkeypatch.setattr(rgit, "PACKED_REFS_LIMIT", 4)
    assert rgit.no_refs_under(path / ".git", SALVAGE) is False


@pytest.mark.parametrize("where", ["main", "main-subfolder", "linked", "linked-subfolder"])
def test_core_bare_true_in_shared_config_matches_real_git(tmp_path, where):
    owner = repository(tmp_path, commit=True)
    checkout = tmp_path / "checkout"
    git(owner, "worktree", "add", "--detach", "-q", str(checkout), "HEAD")
    for folder in (owner, checkout):
        (folder / "subfolder").mkdir()
    git(owner, "config", "core.bare", "true")
    base = owner if where.startswith("main") else checkout
    path = base / "subfolder" if where.endswith("subfolder") else base
    assert rgit.common_dir(path) is not None
    assert_matches_git(path)
