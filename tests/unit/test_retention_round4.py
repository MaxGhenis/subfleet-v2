"""Round-four retirement accepts only an unchanged, explicitly recorded checkout.

These integration and generated cases use isolated Git repositories and SQLite.
Round-three findings 5 and 6 have separate trash/registration regressions.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import retention
from subfleet.store import Store
from test_retention_progress import add_job, MIB
from test_retention_recovery_properties import case
from test_retention_worktrees import git, record_salvage


@contextmanager
def recorded_case():
    with case(owned=True) as fixture:
        store, _, repo, tree, _ = fixture
        store.update_job("job", workdir_head=git(repo, "rev-parse", "HEAD"))
        yield fixture


def assert_pinned(store, result, tree):
    assert result["pruned"] == [], result
    assert "job" in result["protected"], result
    assert result["errors"], result
    assert store.get_job("job") is not None
    assert tree.exists()
    assert not store.list_leases()


def commit_baseline(store, repo, tree):
    git(tree, "add", ".")
    git(tree, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "recorded baseline")
    head = git(tree, "rev-parse", "HEAD")
    store.update_job("job", workdir_head=head)
    git(repo, "update-ref", "refs/heads/recorded", head)
    return head


def test_r3_finding_1_equal_size_and_mtime_edit_is_pinned():
    """Git's cached status really misses this edit; retention must hash it."""
    with recorded_case() as (store, root, repo, tree, _):
        git(repo, "config", "core.trustctime", "false")
        path = tree / "tracked"
        old = path.read_bytes()
        old_time = 1_600_000_000_000_000_000
        os.utime(path, ns=(old_time, old_time))
        git(tree, "update-index", "--refresh")
        path.write_bytes(b"X" * len(old))
        os.utime(path, ns=(old_time, old_time))
        assert git(tree, "diff-files", "--name-only") == ""
        index = Path(git(tree, "rev-parse", "--absolute-git-dir")) / "index"
        before = index.read_bytes()
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert path.read_bytes() == b"X" * len(old)
        assert index.read_bytes() == before


@pytest.mark.parametrize("name", ["node_modules", ".venv", ".tox", "analysis.egg-info"])
def test_r3_finding_2_dependency_or_environment_work_is_pinned(name):
    with recorded_case() as (store, root, repo, tree, _):
        (repo / ".git/info/exclude").write_text(name + "/\n")
        path = tree / name / "sole-analysis.py"
        path.parent.mkdir()
        path.write_bytes(b"unique local dependency fix")
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert path.read_bytes() == b"unique local dependency fix"


@pytest.mark.parametrize("location", ["tree", "info"])
def test_r3_finding_3_lossy_clean_filter_is_pinned(location):
    with recorded_case() as (store, root, repo, tree, _):
        attribute = tree / ".gitattributes" if location == "tree" else repo / ".git/info/attributes"
        attribute.write_text("tracked filter=strip-results\n")
        git(repo, "config", "filter.strip-results.clean", "sed 's/experiment-[0-9]*/experiment-0/g'")
        (tree / "tracked").write_text("experiment-0\n")
        commit_baseline(store, repo, tree)
        (tree / "tracked").write_text("experiment-7\n")
        assert git(tree, "hash-object", "--path=tracked", "tracked") == git(tree, "rev-parse", "HEAD:tracked")
        assert git(tree, "hash-object", "--no-filters", "tracked") != git(tree, "rev-parse", "HEAD:tracked")
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert (tree / "tracked").read_text() == "experiment-7\n"


@pytest.mark.parametrize("attribute", ["filter=unused", "text", "text=auto", "eol=lf", "eol=crlf"])
def test_conversion_attributes_pin_even_when_current_raw_bytes_match(attribute):
    with recorded_case() as (store, root, repo, tree, _):
        (tree / ".gitattributes").write_text("not-present " + attribute + "\n")
        commit_baseline(store, repo, tree)
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)


@pytest.mark.parametrize("name", [".DS_Store", "__pycache__"])
def test_ignored_symlinks_cannot_borrow_allowed_cache_names(name):
    with recorded_case() as (store, root, repo, tree, _):
        (repo / ".git/info/exclude").write_text(name + "\n")
        (tree / name).symlink_to("../unique-target")
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert os.readlink(tree / name) == "../unique-target"


def test_worktree_config_cannot_redirect_untracked_scan_to_clean_caller():
    with recorded_case() as (store, root, repo, tree, _):
        git(repo, "config", "extensions.worktreeConfig", "true")
        admin = Path(git(tree, "rev-parse", "--absolute-git-dir"))
        (admin / "config.worktree").write_text(f"[core]\n\tworktree = {repo}\n")
        output = tree / "unique-output"
        output.write_bytes(b"only in allocated tree")
        assert not git(tree, "ls-files", "--others", "--exclude-standard")
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert output.read_bytes() == b"only in allocated tree"


@pytest.mark.parametrize("state", ["refs/worktree/scratch", "refs/stash", "BISECT_LOG", "MERGE_HEAD",
                                    "rebase-merge/head-name", "rebase-apply/patch"])
def test_r3_finding_7_private_refs_and_admin_state_are_pinned(state):
    with recorded_case() as (store, root, _, tree, _):
        admin = Path(git(tree, "rev-parse", "--absolute-git-dir"))
        path = admin / state
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(git(tree, "rev-parse", "HEAD") + "\n")
        before = path.read_bytes()
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert path.read_bytes() == before


VIOLATIONS = ["missing-baseline", "unrecorded-head", "unheld-baseline", "staged", "unmerged",
              "raw-edit", "deleted", "executable", "untracked", "ignored", "cache-name-file",
              "nested-git", "bare-repo", "private-ref", "merge", "attributes"]


def violate(store, repo, tree, kind, payload):
    payload = b"unique work: " + payload
    path = tree / "tracked"
    if kind == "missing-baseline":
        store.update_job("job", workdir_head=None)
    elif kind in {"unrecorded-head", "unheld-baseline"}:
        path.write_bytes(payload)
        git(tree, "add", "tracked")
        git(tree, "-c", "user.name=test", "-c", "user.email=t@example.invalid", "commit", "-m", "new head")
        head = git(tree, "rev-parse", "HEAD")
        if kind == "unrecorded-head":
            git(repo, "update-ref", "refs/heads/held", head)
        else:
            store.update_job("job", workdir_head=head)
    elif kind in {"staged", "unmerged"}:
        original = path.read_bytes()
        path.write_bytes(payload)
        git(tree, "add", "tracked")
        if kind == "unmerged":
            blob = git(tree, "rev-parse", ":tracked")
            git(tree, "update-index", "--force-remove", "tracked")
            subprocess.run(["git", "-C", str(tree), "update-index", "--index-info"],
                           input=f"100644 {blob} 1\ttracked\n100644 {blob} 2\ttracked\n",
                           text=True, check=True, capture_output=True)
        path.write_bytes(original)
    elif kind == "raw-edit":
        path.write_bytes(payload)
    elif kind == "deleted":
        path.unlink()
    elif kind == "executable":
        git(repo, "config", "core.filemode", "false")
        path.chmod(0o755)
    elif kind == "untracked":
        (tree / "sole-output").write_bytes(payload)
    elif kind in {"ignored", "cache-name-file", "nested-git", "bare-repo"}:
        (repo / ".git/info/exclude").write_text("output/\n__pycache__\n")
        if kind == "cache-name-file":
            (tree / "__pycache__").write_bytes(payload)
        else:
            cache = tree / ("output" if kind == "ignored" else "__pycache__")
            cache.mkdir()
            if kind == "nested-git":
                (cache / ".GiT").write_bytes(payload)
            elif kind == "bare-repo":
                git(cache, "init", "--bare")
            else:
                (cache / "result").write_bytes(payload)
    elif kind in {"private-ref", "merge"}:
        admin = Path(git(tree, "rev-parse", "--absolute-git-dir"))
        path = admin / ("refs/worktree/scratch" if kind == "private-ref" else "MERGE_HEAD")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(git(tree, "rev-parse", "HEAD") + "\n")
    elif kind == "attributes":
        (repo / ".git/info/attributes").write_text("tracked filter=unused\n")
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("violation", VIOLATIONS)
@settings(max_examples=4, deadline=None, database=None)
@given(payload=st.binary(max_size=48))
def test_any_violation_of_conditions_two_through_six_is_pinned(violation, payload):
    with recorded_case() as (store, root, repo, tree, directory):
        violate(store, repo, tree, violation, payload)
        contents = {p.relative_to(tree): p.read_bytes() for p in tree.rglob("*") if p.is_file()}
        result = retention.maintenance(store, root, max_jobs=0)
        assert_pinned(store, result, tree)
        assert contents == {p.relative_to(tree): p.read_bytes() for p in tree.rglob("*") if p.is_file()}
        assert (directory / "stdout").read_bytes() == b"job output"


@pytest.mark.parametrize("change", ["head", "staged", "tracked", "untracked", "ignored", "private-ref", "nested-git"])
@settings(max_examples=5, deadline=None, database=None)
@given(payload=st.binary(max_size=48))
def test_r3_finding_4_change_after_atomic_rename_is_always_restored(change, payload):
    with recorded_case() as (store, root, repo, tree, directory):
        (repo / ".git/info/exclude").write_text("output/\n__pycache__/\n")
        original = Path.rename
        mutations = []
        unique = b"post-rename work: " + payload

        def rename(source, destination):
            result = original(source, destination)
            if source == tree:
                retired = Path(destination)
                if change == "head":
                    git(retired, "-c", "user.name=test", "-c", "user.email=t@example.invalid",
                        "commit", "--allow-empty", "-m", "post-rename unique commit")
                    target = retired / "tracked"
                elif change == "staged":
                    target = retired / "tracked"
                    before = target.read_bytes()
                    target.write_bytes(unique)
                    git(retired, "add", "tracked")
                    target.write_bytes(before)
                elif change == "private-ref":
                    target = Path(git(retired, "rev-parse", "--absolute-git-dir")) / "refs/worktree/scratch"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(git(retired, "rev-parse", "HEAD") + "\n")
                else:
                    relative = {"tracked": "tracked", "untracked": "sole-output", "ignored": "output/result",
                                "nested-git": "__pycache__/.GIT"}[change]
                    target = retired / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(unique)
                mutations.append(target)
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "rename", rename)
            result = retention.maintenance(store, root, max_jobs=0)
        assert len(mutations) == 1, result
        assert_pinned(store, result, tree)
        assert (directory / "stdout").read_bytes() == b"job output"
        if change == "head":
            assert git(tree, "rev-parse", "HEAD") != store.get_job("job")["workdir_head"]
        elif change == "staged":
            assert subprocess.run(["git", "-C", str(tree), "show", ":tracked"],
                                  check=True, capture_output=True).stdout == unique
        elif change == "private-ref":
            assert mutations[0].exists()
        else:
            assert (tree / mutations[0].relative_to(root / "trash/job/worktree")).read_bytes() == unique
        assert not (root / "trash/job").exists()


@pytest.mark.parametrize("holding", ["branch", "tag", "descendant"])
def test_unchanged_recorded_baseline_held_by_branch_or_tag_retires(holding):
    with recorded_case() as (store, root, repo, tree, _):
        if holding == "tag":
            git(repo, "tag", "saved-baseline")
            git(repo, "update-ref", "-d", "refs/heads/main")
        elif holding == "descendant":
            (repo / "later").write_text("later independent work")
            git(repo, "add", "later")
            git(repo, "-c", "user.name=test", "-c", "user.email=t@example.invalid", "commit", "-m", "later")
        result = retention.maintenance(store, root, max_jobs=0)
        assert result["pruned"] == ["job"], result
        assert not tree.exists()


def test_exact_recorded_salvage_commit_held_by_salvage_ref_retires():
    with recorded_case() as (store, root, _, tree, _):
        (tree / "tracked").write_text("recorded salvage")
        saved = record_salvage(store, tree)
        git(tree, "reset", "--hard", saved.commit)
        result = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda _: True)
        assert result["pruned"] == ["job"], result
        assert not tree.exists()


def test_r3_finding_8_size_measurements_survive_long_catchup(tmp_path, monkeypatch):
    """A scan longer than the old 60s TTL converges; the explicitly pinned job stays."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original = retention._size

    def slow(path, **kwargs):
        result = original(path, **kwargs)
        clock.now += 1
        return result

    monkeypatch.setattr(retention, "_size", slow)
    with Store(tmp_path / "state.sqlite3") as store:
        for index in range(180):
            add_job(store, tmp_path, f"job-{index:03d}", order=index, size=MIB, nested=False)
        pruned = []
        for _ in range(20):
            result = retention.maintenance(store, tmp_path, max_bytes=170 * MIB,
                                           referenced_job_ids={"job-000"}, deadline=clock.now + 60)
            pruned.extend(result["pruned"])
            if len(store.list_jobs()) <= 170:
                break
            clock.now += 5
        assert len(store.list_jobs()) <= 170, result
        assert len(pruned) == 10
        assert store.get_job("job-000") is not None
        assert (tmp_path / "jobs/job-000/payload-0.bin").stat().st_size == MIB


def test_r3_finding_9_stale_size_cannot_prune_within_old_ttl(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with Store(tmp_path / "state.sqlite3") as store:
        add_job(store, tmp_path, "j1", order=0, size=MIB)
        big = add_job(store, tmp_path, "j2", order=1, size=3 * MIB)
        add_job(store, tmp_path, "j3", order=2, size=MIB)
        original = retention._size

        def interrupt(path, **kwargs):
            if Path(path).name == "j3":
                raise retention._Interrupted("deadline")
            return original(path, **kwargs)

        monkeypatch.setattr(retention, "_size", interrupt)
        assert retention.maintenance(store, tmp_path, max_bytes=10 * MIB)["interrupted"]
        before = big.stat().st_mtime_ns
        with (big / "part-0/payload-0.bin").open("wb") as stream:
            stream.truncate(MIB)
        assert big.stat().st_mtime_ns == before
        clock.now += 5
        monkeypatch.setattr(retention, "_size", original)
        result = retention.maintenance(store, tmp_path, max_bytes=4 * MIB)
        assert result["pruned"] == [], result
        assert result["bytes_after"] == 3 * MIB
        assert len(store.list_jobs()) == 3


def test_expensive_cached_metadata_validation_resumes_and_reaches_budget(tmp_path, monkeypatch):
    """Even cached-stat validation can exceed a pass; retries must move forward."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with Store(tmp_path / "state.sqlite3") as store:
        large = add_job(store, tmp_path, "large", order=0, parts=12, size=MIB)
        add_job(store, tmp_path, "last", order=1, size=MIB)
        original_size = retention._size

        def interrupt_last(path, **kwargs):
            if Path(path).name == "last":
                raise retention._Interrupted("deadline")
            return original_size(path, **kwargs)

        monkeypatch.setattr(retention, "_size", interrupt_last)
        assert retention.maintenance(store, tmp_path, max_bytes=100 * MIB)["interrupted"]
        monkeypatch.setattr(retention, "_size", original_size)
        original_stat = os.stat
        validated = []

        def expensive_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)):
                target = Path(path)
                if target.is_relative_to(large) and target.name.startswith("payload-"):
                    validated.append(target)
                    clock.now += 1
            return result

        monkeypatch.setattr(retention.os, "stat", expensive_stat)
        interruptions = 0
        for _ in range(6):
            started = clock.now
            result = retention.maintenance(store, tmp_path, max_bytes=MIB, deadline=clock.now + 3.5)
            assert clock.now - started <= 4, "validation ignored the cooperative deadline"
            if result.get("interrupted"):
                interruptions += 1
                assert result["made_progress"]
            if store.get_job("large") is None:
                break
        else:
            pytest.fail("cached metadata restarted each pass and starved pruning")
        assert interruptions >= 2
        assert len(validated) == len(set(validated)) == 12
        assert result["pruned"] == ["large"]
        assert result["bytes_after"] == MIB
