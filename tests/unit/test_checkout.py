"""C-6.14: measuring a tree, reading a brief's paths, planning a cone, cutting it.

The planner's byte accounting is checked three ways: against a hand count, against
`cone_files` (git's cone-mode rule written out directly) for any tree and cone
(Hypothesis), and against what git itself checks out for a cone.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import checkout
from subfleet.checkout import (Cone, CheckoutError, TreeSizes, cone_files, create_worktree, disk_usage,
                               explicit_dirs, measure_tree, named_paths, normalize_explicit, parse_ls_tree,
                               plan_checkout)


def git(cwd, *args, stdin: bytes | None = None) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], input=stdin, capture_output=True, check=True)
    return os.fsdecode(result.stdout).strip()


def make_repo(root: Path, files: dict[str, bytes], branch: str = "task/sparse") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", branch)
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(root, "add", "-A")
    git(root, "commit", "-m", "baseline")
    return root


def ls_tree(repo: Path) -> bytes:
    return subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"],
                          capture_output=True, check=True).stdout


LAYOUT = {
    "README.md": b"r" * 10,
    "pyproject.toml": b"p" * 5,
    "src/app.py": b"a" * 100,
    "src/pkg/mod.py": b"m" * 50,
    "docs/guide.md": b"g" * 30,
    "data/index.json": b"i" * 7,
    "data/big/one.bin": b"1" * 4000,
    "data/big/two.bin": b"2" * 4000,
    "data/big/sub/three.bin": b"3" * 2000,
    "data/small/a.txt": b"s" * 20,
    "tests/test_app.py": b"t" * 40,
}


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", LAYOUT)


# --- measuring ---------------------------------------------------------------------------

def test_c6_14_parse_counts_every_directory_recursively_and_directly(repo):
    """C-6.14 a tree's sizes by directory: recursive and direct, the top included."""
    sizes = parse_ls_tree(ls_tree(repo), "tree")
    assert sizes.bytes == sum(len(data) for data in LAYOUT.values())
    assert sizes.files == len(LAYOUT)
    assert sizes.direct[""] == 15 and sizes.direct_count[""] == 2
    assert sizes.total["data"] == 7 + 4000 + 4000 + 2000 + 20
    assert sizes.direct["data"] == 7 and sizes.direct["data/big"] == 8000
    assert sizes.total["data/big"] == 10000 and sizes.count["data/big"] == 3
    assert sizes.children["data"] == ("data/big", "data/small")
    assert sizes.children["data/big"] == ("data/big/sub",)
    assert sizes.total[""] == sizes.bytes


def test_c6_14_parse_reads_gitlinks_as_empty_and_names_that_are_not_utf8():
    """C-6.14 a submodule's gitlink is a 0-byte file; a non-UTF-8 name is carried."""
    raw = (b"160000 commit " + b"a" * 40 + b"       -\tvendor/lib\0"
           + b"100644 blob " + b"b" * 40 + b"      12\tsrc/caf\xe9.txt\0")
    sizes = parse_ls_tree(raw)
    assert sizes.files == 2 and sizes.bytes == 12
    assert sizes.direct["vendor"] == 0 and sizes.direct_count["vendor"] == 1
    assert sizes.direct["src"] == 12


def test_c6_14_parse_refuses_a_record_git_never_writes():
    with pytest.raises(ValueError):
        parse_ls_tree(b"garbage without a tab\0")


def test_c6_14_measure_equals_what_a_full_checkout_writes(repo, tmp_path):
    """C-6.14 differential: the measured bytes and files are a full checkout's, a symlink included."""
    (repo / "src" / "link").symlink_to("app.py")
    (repo / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(repo / "run.sh", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "more")
    sizes = measure_tree(repo, "HEAD", timeout_s=30)
    full = tmp_path / "full"
    git(repo, "worktree", "add", "--detach", str(full), "HEAD")
    written = [path for path in full.rglob("*") if ".git" not in path.relative_to(full).parts
               and (path.is_symlink() or path.is_file())]
    assert sizes.files == len(written)
    assert sizes.bytes == sum(path.lstat().st_size for path in written)


def test_c6_14_measure_is_cached_per_repository_and_tree(repo, monkeypatch):
    """C-6.14 one ls-tree per tree: a second job on the same head reads the cache."""
    checkout._CACHE.clear()
    calls = []
    original = checkout._git_bytes

    def counting(repo_, *args, **kwargs):
        calls.append(args[0])
        return original(repo_, *args, **kwargs)

    monkeypatch.setattr(checkout, "_git_bytes", counting)
    first = measure_tree(repo, "HEAD", timeout_s=30)
    second = measure_tree(repo, "HEAD", timeout_s=30)
    assert first is second
    assert calls.count("ls-tree") == 1


# --- named paths -------------------------------------------------------------------------

@pytest.fixture
def sizes(repo):
    return parse_ls_tree(ls_tree(repo), "tree")


def paths_of(found):
    return [item["path"] for item in found[0]]


def test_c6_14_brief_names_directories_files_and_files_to_create(sizes):
    """C-6.14 a file names its directory, and so does a file not yet written there."""
    brief = ("Edit `src/pkg/mod.py:12` and data/small/a.txt, then write data/small/new.csv; "
             "see docs/ for context and data/big/sub/.")
    assert paths_of(named_paths(brief, sizes)) == ["src/pkg", "data/small", "docs", "data/big/sub"]


def test_c6_14_brief_ignores_urls_bare_words_and_escapes(sizes):
    """C-6.14 prose is not a path: no URL, no bare word, nothing that leaves the repository."""
    brief = ("See https://github.com/org/repo/tree/main/src and git@github.com:org/src.git; the data "
             "and tests folders; ../outside/src and origin/main and and/or.")
    assert paths_of(named_paths(brief, sizes)) == []


def test_c6_14_absolute_paths_count_under_the_callers_checkout_only(sizes, tmp_path):
    """C-6.14 an absolute path names a directory only under the caller's checkout."""
    top = "/Users/someone/repo"
    brief = f"Read {top}/data/small/a.txt and /elsewhere/data/big and {top}/docs/guide.md."
    found = named_paths(brief, sizes, roots={top: "."})
    assert paths_of(found) == ["data/small", "docs"]


def test_c6_14_paths_are_read_from_the_callers_place_first(sizes):
    """C-6.14 from `data`, `big/one.bin` is data/big; a path only the top has is the top's."""
    found = named_paths("Look at big/one.bin and src/app.py", sizes, prefix="data")
    assert paths_of(found) == ["data/big", "src"]
    workdir = "/w/repo/data"
    found = named_paths(f"and {workdir}/small/a.txt", sizes, roots={"/w/repo": ".", workdir: "data"},
                        prefix="data")
    assert paths_of(found) == ["data/small"]


def test_c6_14_named_paths_are_capped_and_the_rest_counted(sizes):
    """C-6.14 no silent cap: past `MAX_NAMED` the brief's directories are counted as dropped."""
    many = {f"d{i:03}/f": b"x" for i in range(checkout.MAX_NAMED + 5)}
    tree = parse_ls_tree(b"".join(b"100644 blob " + b"a" * 40 + b" 1\t" + name.encode() + b"\0"
                                  for name in many))
    found, dropped = named_paths(" ".join(many), tree)
    assert len(found) == checkout.MAX_NAMED and dropped == 5


# --- explicit paths ----------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    (".", "."), ("./", "."), ("/", "."), ("", "."), ("src/", "src"), ("./src//pkg/", "src/pkg"),
])
def test_c6_14_explicit_paths_normalize(raw, expected):
    assert normalize_explicit(raw) == expected


@pytest.mark.parametrize("raw", ["/abs/src", "~/src", "../x", "src/../../x", "src\ndocs", "a\0b"])
def test_c6_14_explicit_paths_stay_in_the_repository(raw):
    with pytest.raises(ValueError):
        normalize_explicit(raw)


def test_c6_14_explicit_paths_resolve_against_the_commit(repo):
    """C-6.14 `--paths`: a directory is itself, a file its directory, `.` everything, a typo refused."""
    names = [normalize_explicit(p) for p in ("data/big", "src/app.py", "README.md", "docs/")]
    kinds = checkout.object_kinds(repo, "HEAD", names, timeout_s=30)
    assert kinds == {"data/big": "tree", "src/app.py": "blob", "README.md": "blob", "docs": "tree"}
    assert explicit_dirs(names, kinds) == ["data/big", "src", "docs"]
    assert explicit_dirs(["src", "."], {"src": "tree"}) is None
    with pytest.raises(ValueError, match="not in the job's commit"):
        explicit_dirs(["nope"], checkout.object_kinds(repo, "HEAD", ["nope"], timeout_s=30))


# --- planning ----------------------------------------------------------------------------

def test_c6_14_small_trees_and_null_thresholds_are_checked_out_whole(sizes):
    assert plan_checkout(sizes, threshold=sizes.bytes, budget=10)["mode"] == "full"
    assert plan_checkout(sizes, threshold=None, budget=10)["reason"] == "under-threshold"
    assert plan_checkout(sizes, threshold=1, budget=10, explicit=None)["reason"] == "paths-all"
    assert plan_checkout(sizes, threshold=1, budget=10, git_ok=False)["reason"] == "git-too-old"
    unmeasured = plan_checkout(None, threshold=1, budget=10, unmeasured="TimeoutExpired")
    assert unmeasured["mode"] == "full" and unmeasured["error"] == "TimeoutExpired"


def test_c6_14_the_budget_takes_named_paths_in_order_then_the_smallest_directories(sizes):
    """C-6.14 a named directory that does not fit is left out and recorded; the fill is smallest first."""
    named = [{"path": "data/big", "named": "data/big/"}, {"path": "src/pkg", "named": "src/pkg/mod.py"}]
    plan = plan_checkout(sizes, threshold=100, budget=300, named=named)
    assert plan["mode"] == "sparse"
    considered = {item["path"]: item for item in plan["considered"]}
    assert considered["data/big"]["included"] is False and considered["data/big"]["reason"] == "budget"
    assert considered["src/pkg"]["included"] is True and considered["src/pkg"]["bytes"] == 150
    # Root files 15 + src/pkg 50 + src direct 100 = 165; then the smallest top-level
    # directories: docs 30, tests 40 (235); data (10027) does not fit; then data/small
    # needs data's own files too (20 + 7): 262.
    assert plan["cone"] == ["data/small", "docs", "src", "tests"]
    assert plan["cone_bytes"] == 262
    assert plan["largest_excluded"][0] == {"path": "data/big", "bytes": 10000, "files": 3}
    assert checkout.summary(plan)["left_out"] == ["data/big"]


def test_c6_14_explicit_paths_are_checked_out_whatever_they_cost(sizes):
    plan = plan_checkout(sizes, threshold=100, budget=50, explicit=["data/big"])
    assert plan["reason"] == "paths" and "data/big" in plan["cone"]
    assert plan["cone_bytes"] >= 10000


def test_c6_14_the_callers_place_is_kept_within_the_budget_or_left_for_the_top(sizes):
    kept = plan_checkout(sizes, threshold=100, budget=300, prefix="src")
    assert kept["place_checked_out"] is True and "src" in kept["cone"]
    left = plan_checkout(sizes, threshold=100, budget=300, prefix="data/big")
    assert left["place_checked_out"] is False and not Cone(sizes).covered("data/big")
    assert all(not path.startswith("data/big") for path in left["cone"])


def test_c6_14_names_git_would_misread_are_not_put_in_a_cone():
    tree = parse_ls_tree(b"".join(b"100644 blob " + b"a" * 40 + b" 1\t" + name + b"\0"
                                  for name in (b"ok/f", b"back\\slash/f", b'"quoted/f')))
    plan = plan_checkout(tree, threshold=0, budget=100)
    assert plan["cone"] == ["ok"]


# --- properties --------------------------------------------------------------------------

names = st.sampled_from(["a", "b", "c", "d", "data", "src"])
file_paths = st.lists(names, min_size=1, max_size=4).map(lambda parts: "/".join(parts[:-1] + [parts[-1] + ".f"]))
trees = st.dictionaries(file_paths, st.integers(min_value=0, max_value=5000), min_size=1, max_size=40)


def tree_of(files: dict[str, int]) -> TreeSizes:
    # A path cannot be both a file and a directory in one tree.
    return parse_ls_tree(b"".join(b"100644 blob " + b"a" * 40 + f" {size}\t{name}".encode() + b"\0"
                                  for name, size in sorted(files.items())))


@settings(max_examples=300, deadline=None)
@given(files=trees, budget=st.integers(min_value=0, max_value=30000),
       named=st.lists(st.sampled_from(["a", "b", "a/b", "src", "data/src", "c/d/a", "d"]), max_size=4),
       explicit=st.one_of(st.just(False), st.lists(st.sampled_from(["a", "b/c", "src", "data"]), max_size=2)),
       prefix=st.sampled_from([".", "a", "src/b"]))
def test_c6_14_property_plan_accounting_budget_and_minimality(files, budget, named, explicit, prefix):
    """C-6.14 for every tree and request: the plan's bytes and files are exactly what
    git's cone rule checks out; without explicit paths it stays within the budget
    (the top's own files excepted); explicit paths are always checked out; a named
    path is marked checked out exactly when the cone holds it; the cone is minimal."""
    sizes = tree_of(files)
    dirs = [d for d in explicit if sizes.is_dir(d)] if explicit is not False else False
    plan = plan_checkout(sizes, threshold=-1, budget=budget, explicit=dirs,
                         named=[{"path": p} for p in named if sizes.is_dir(p)], prefix=prefix)
    assert plan["mode"] == "sparse"
    chosen = cone_files(files, plan["cone"])
    assert plan["cone_bytes"] == sum(files[name] for name in chosen)
    assert plan["cone_files"] == len(chosen)
    if not dirs:
        assert plan["cone_bytes"] <= max(budget, sizes.direct[""])
    cone = Cone(sizes)
    for directory in plan["cone"]:
        cone.add(directory)
    for directory in dirs or ():
        assert cone.covered(directory)
    for item in plan["considered"]:
        assert item["checked_out"] == cone.covered(item["path"])
    for first in plan["cone"]:
        assert not any(other.startswith(first + "/") for other in plan["cone"])
    assert plan == plan_checkout(sizes, threshold=-1, budget=budget, explicit=dirs,
                                 named=[{"path": p} for p in named if sizes.is_dir(p)], prefix=prefix)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(files=trees, pick=st.lists(st.integers(min_value=0, max_value=100), max_size=3))
def test_c6_14_property_git_checks_out_what_the_cone_rule_says(tmp_path_factory, files, pick):
    """C-6.14 differential: `create_worktree` makes exactly the files `cone_files`
    predicts, and `disk_usage` counts the bytes the plan does."""
    root = tmp_path_factory.mktemp("cone")
    repo = make_repo(root / "repo", {name: b"x" * size for name, size in files.items()})
    sizes = measure_tree(repo, "HEAD", timeout_s=30)
    candidates = sorted(d for d in sizes.total if d)
    cone = sorted({candidates[i % len(candidates)] for i in pick}) if candidates else []
    minimal = [d for d in cone if not any(d.startswith(o + "/") for o in cone)]
    plan = {"mode": "sparse", "cone": minimal}
    target = root / "wt"
    create_worktree(str(repo), str(target), git(repo, "rev-parse", "HEAD"), plan, timeout_s=60)
    on_disk = {str(path.relative_to(target)) for path in target.rglob("*")
               if path.is_file() and ".git" not in path.relative_to(target).parts}
    assert on_disk == cone_files(files, minimal)
    usage = disk_usage(target)
    assert usage["complete"] and usage["files"] == len(on_disk) + 1          # and the `.git` file
    assert usage["apparent_bytes"] - (target / ".git").lstat().st_size == sum(files[n] for n in on_disk)


# --- cutting a worktree ------------------------------------------------------------------

def test_c6_14_sparse_worktree_is_git_sparse_and_leaves_the_source_alone(repo, tmp_path):
    """C-6.14 the cut touches the source repository's config only to allow per-worktree settings."""
    index = (repo / ".git" / "index").read_bytes()
    head = git(repo, "rev-parse", "HEAD")
    target = tmp_path / "wt"
    create_worktree(str(repo), str(target), head, {"mode": "sparse", "cone": ["src"]}, timeout_s=60)
    assert git(target, "rev-parse", "HEAD") == head
    assert git(target, "config", "--type=bool", "core.sparseCheckout") == "true"
    assert git(target, "sparse-checkout", "list") == "src"
    assert git(target, "status", "--porcelain") == ""
    assert (target / "src" / "pkg" / "mod.py").is_file() and not (target / "data").exists()
    # The source: same HEAD, index bytes and files, and not itself sparse.
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git" / "index").read_bytes() == index
    assert subprocess.run(["git", "-C", str(repo), "config", "core.sparseCheckout"],
                          capture_output=True).returncode == 1
    assert git(repo, "config", "extensions.worktreeConfig") == "true"
    assert all((repo / name).is_file() for name in LAYOUT)


def test_c6_14_full_plan_and_no_plan_are_a_plain_worktree(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    for name, plan in (("a", {"mode": "full"}), ("b", None)):
        target = tmp_path / name
        create_worktree(str(repo), str(target), head, plan, timeout_s=60)
        assert all((target / path).is_file() for path in LAYOUT)
    assert subprocess.run(["git", "-C", str(repo), "config", "extensions.worktreeConfig"],
                          capture_output=True).returncode == 1


def test_c6_14_the_post_checkout_hook_runs_as_a_full_worktree_add_runs_it(repo, tmp_path):
    """C-6.14 `--no-checkout` skips the hook; the sparse cut runs it with the same arguments."""
    log = tmp_path / "hook.log"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\necho "$(basename "$PWD") $*" >> {log}\n')
    hook.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    create_worktree(str(repo), str(tmp_path / "full"), head, {"mode": "full"}, timeout_s=60)
    create_worktree(str(repo), str(tmp_path / "sparse"), head, {"mode": "sparse", "cone": ["src"]}, timeout_s=60)
    assert log.read_text().splitlines() == [f"full {'0' * 40} {head} 1", f"sparse {'0' * 40} {head} 1"]
    hook.write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(CheckoutError, match="git hook failed"):
        create_worktree(str(repo), str(tmp_path / "refused"), head, {"mode": "sparse", "cone": []}, timeout_s=60)


def test_c6_14_a_refused_cut_raises_and_a_held_lock_is_transient(repo, tmp_path):
    with pytest.raises(CheckoutError) as refused:
        create_worktree(str(repo), str(tmp_path / "x"), "0" * 40, {"mode": "sparse", "cone": []}, timeout_s=30)
    assert refused.value.transient is False
    (repo / ".git" / "config.lock").write_text("")
    try:
        with pytest.raises(CheckoutError) as locked:
            create_worktree(str(repo), str(tmp_path / "y"), git(repo, "rev-parse", "HEAD"),
                            {"mode": "sparse", "cone": ["src"]}, timeout_s=30)
    finally:
        (repo / ".git" / "config.lock").unlink()
    assert locked.value.transient is True


def test_c6_14_the_cut_stops_at_its_deadline(repo, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        create_worktree(str(repo), str(tmp_path / "z"), git(repo, "rev-parse", "HEAD"),
                        {"mode": "sparse", "cone": ["src"]}, timeout_s=0)


# --- disk usage --------------------------------------------------------------------------

def test_c6_14_disk_usage_counts_files_once_and_follows_no_link(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a").write_bytes(b"x" * 5000)
    os.link(root / "a", root / "sub" / "hard")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"y" * 100000)
    (root / "link").symlink_to(outside)
    usage = disk_usage(root)
    assert usage["complete"] is True
    assert usage["files"] == 2                         # a (once) and the link itself
    assert usage["apparent_bytes"] == 5000 + len(str(outside))
    assert usage["bytes"] >= 4096 and usage["dirs"] == 2


def test_c6_14_disk_usage_says_when_it_stopped_short(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    assert disk_usage(tmp_path, cap_s=0)["complete"] is False
    missing = disk_usage(tmp_path / "gone")
    assert missing["complete"] is False and "FileNotFoundError" in missing["error"]


def test_c6_14_describe_and_human_bytes():
    assert checkout.human_bytes(0) == "0 B" and checkout.human_bytes(15_042_700_000) == "15.0 GB"
    assert checkout.describe({"mode": "sparse", "cone": ["a", "b"], "cone_bytes": 124_600_000,
                              "tree_bytes": 15_042_700_000}) == "sparse (2 dirs, 124.6 MB of the 15.0 GB tree)"
    assert checkout.describe({"mode": "full", "reason": "under-threshold", "tree_bytes": 5}) == \
        "full (under-threshold, 5 B tree)"
    assert checkout.describe(None) == "full (no plan)"
