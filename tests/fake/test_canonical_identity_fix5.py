"""Submit regressions and independent realpath/kernel oracles for -o paths."""
import errno
import importlib.util
import os
from pathlib import Path
import tempfile
import sys

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.adapters.base import AdapterError
from subfleet.daemon import _resolve_output_path
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity_fix2 import configure
from tests.fake.test_canonical_identity_fix4 import FIX


@pytest.mark.parametrize("cycle", ["self", "two-node"])
def test_submit_refuses_hidden_loop_before_target_dotdot(tmp_path, cycle):
    (tmp_path / "loop").symlink_to("loop" if cycle == "self" else "second")
    if cycle == "two-node":
        (tmp_path / "second").symlink_to("loop")
    (tmp_path / "alias").symlink_to("Missing/../loop/../Result.md")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=str(tmp_path / "alias"), caller_session=None)
        assert refused.value.code == 7
        assert refused.value.fix == FIX
        assert not service.store.list_jobs()
        assert not service.store.list_leases()


@pytest.mark.parametrize("target", ["Missing/../real/nested", "real/nested"],
                         ids=["review-r5-healthy", "82cc04e47-acceptance"])
def test_submit_accepts_target_parent_beside_unrelated_loop(tmp_path, target):
    (tmp_path / "real" / "nested").mkdir(parents=True)
    healthy = tmp_path / "real" / "loop"
    healthy.write_text("healthy\n")
    (tmp_path / "alias").symlink_to(target)
    (tmp_path / "loop").symlink_to("loop")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(tmp_path / "alias" / ".." / "loop"),
                     caller_session=None)
        assert service.store.get_job(job)["out_path"] == str(healthy)


def test_output_hop_limit_counts_repeated_noncyclic_links(tmp_path):
    # A repeated link is not necessarily a cycle, but still consumes a hop.
    (tmp_path / "again").symlink_to(".")
    path40 = str(tmp_path) + "/again" * 40 + "/Result.md"
    assert _resolve_output_path(path40) == tmp_path / "Result.md"
    with pytest.raises(OSError) as refused:
        _resolve_output_path(str(tmp_path) + "/again" * 41 + "/Result.md")
    assert refused.value.errno == errno.ELOOP
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError) as refused_submit:
            submit(service, harness, out_path=str(tmp_path) + "/again" * 41 + "/Result.md",
                   caller_session=None)
        assert refused_submit.value.code == 7
        assert refused_submit.value.fix == FIX
        assert not service.store.list_jobs()
        assert not service.store.list_leases()


def test_relative_and_absolute_targets_apply_dotdot_after_expansion(tmp_path, monkeypatch):
    (tmp_path / "real" / "nested").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    (tmp_path / "real" / "inner").symlink_to("nested")
    (tmp_path / "other" / "relative").symlink_to("../real/inner")
    (tmp_path / "absolute").symlink_to(tmp_path / "other" / "relative")
    monkeypatch.chdir(tmp_path)
    assert _resolve_output_path("absolute/../Result.md") == tmp_path / "real" / "Result.md"
    assert _resolve_output_path("other/relative/../Result.md") == tmp_path / "real" / "Result.md"


@pytest.mark.parametrize("suffix", ["child", "..", ".", ""])
def test_existing_file_cannot_be_traversed(tmp_path, suffix):
    (tmp_path / "file").write_bytes(b"file")
    with pytest.raises(OSError) as refused:
        _resolve_output_path(str(tmp_path / "file") + "/" + suffix)
    assert refused.value.errno == errno.ENOTDIR


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
def test_resolver_propagates_nonmissing_errors(tmp_path, monkeypatch, error):
    original = os.lstat
    blocked = str(tmp_path / "blocked")

    def fail(path):
        if path == blocked:
            raise OSError(error, "fixture", path)
        return original(path)

    monkeypatch.setattr(os, "lstat", fail)
    with pytest.raises(OSError) as refused:
        _resolve_output_path(blocked + "/../Result.md")
    assert refused.value.errno == error


COMPONENTS = ("real", "nested", "other", "deep", "l0", "l1", "l2", "l3",
              "loop", "cycleA", "cycleB", "Missing", "leaf", ".", "..")
PARTS = st.lists(st.sampled_from(COMPONENTS), min_size=1, max_size=4).map("/".join)
TARGET = st.tuples(st.booleans(), PARTS)
TREE = st.tuples(st.lists(TARGET, min_size=4, max_size=4), st.booleans())
PATHS = st.lists(PARTS, min_size=8, max_size=12)
REGRESSION_TREE = ([(False, "Missing/../loop/../leaf"),
                    (False, "Missing/../nested"),
                    (False, "../real/nested"),
                    (True, "real/nested")], True)
REGRESSION_PATHS = ["l0", "real/l1/../leaf", "other/l2/../leaf", "real/nested/l3/../leaf",
                    "Missing/../loop/../leaf", "cycleA", "real/nested/../leaf", "Missing/leaf"]
PROPERTY_SETTINGS = settings(max_examples=300, deadline=None, derandomize=True, database=None)

# 3.12's backport lacks 3.14's ENOTDIR check. Cross-version verification can
# supply the unmodified 3.14 posixpath.py, whose hash is kept in the run report.
ORACLE = os.path
if reference := os.environ.get("SUBFLEET_REALPATH_ORACLE"):
    spec = importlib.util.spec_from_file_location("realpath314_oracle", reference)
    ORACLE = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ORACLE)


def make_tree(root, tree):
    targets, extra_directory = tree
    for directory in ("real", "real/nested", "other", "other/deep"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    if extra_directory:
        (root / "real" / "deep").mkdir()
    for directory in ("", "real", "real/nested", "other", "other/deep"):
        (root / directory / "leaf").write_bytes(b"fixture")
    for name, (absolute, target) in zip(("l0", "real/l1", "other/l2", "real/nested/l3"), targets):
        (root / name).symlink_to(str(root) + "/" + target if absolute else target)
    (root / "loop").symlink_to("loop")
    (root / "cycleA").symlink_to("cycleB")
    (root / "cycleB").symlink_to("cycleA")


def result(call, path):
    try:
        return str(call(path)), None
    except OSError as exc:
        return None, exc.errno


@pytest.mark.skipif(sys.version_info < (3, 14) and not reference,
                    reason="cross-version differential needs the CPython 3.14 oracle source")
@PROPERTY_SETTINGS
@given(tree=TREE, paths=PATHS)
@example(tree=REGRESSION_TREE, paths=REGRESSION_PATHS)
def test_generated_paths_match_allow_missing(tree, paths):
    with tempfile.TemporaryDirectory(prefix="resolver-differential-") as directory:
        root = Path(directory)
        make_tree(root, tree)
        # These anchors guarantee both loop kinds and healthy/missing paths in
        # every generated tree, rather than relying on their random frequency.
        for spelling in [*paths, *REGRESSION_PATHS]:
            path = str(root) + "/" + spelling
            expected = result(lambda p: ORACLE.realpath(p, strict=ORACLE.ALLOW_MISSING), path)
            actual = result(_resolve_output_path, path)
            assert actual == expected, (tree, spelling, expected, actual)


@PROPERTY_SETTINGS
@given(tree=TREE, paths=PATHS)
@example(tree=REGRESSION_TREE, paths=REGRESSION_PATHS)
def test_generated_tree_matches_kernel_stat(tree, paths):
    with tempfile.TemporaryDirectory(prefix="resolver-kernel-") as directory:
        root = Path(directory)
        make_tree(root, tree)
        for spelling in [*paths, *REGRESSION_PATHS]:
            path = str(root) + "/" + spelling
            actual, error = result(_resolve_output_path, path)
            try:
                expected = os.stat(path)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    assert error == exc.errno, (tree, spelling, exc, error)
                # A lexical return can traverse a loop after Missing/..; the
                # kernel stops at Missing with ENOENT. ALLOW_MISSING checks that
                # intentional behavior separately above.
            else:
                assert error is None, (tree, spelling, error)
                resolved_stat = os.stat(actual)
                assert (resolved_stat.st_dev, resolved_stat.st_ino) == (expected.st_dev, expected.st_ino), (tree, spelling)
