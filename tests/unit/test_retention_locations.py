"""C-8.4: physical entries, pointer files, revalidation and routine records."""
import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_location_inputs import build_location_input, check_location_input, git, repo


@pytest.mark.parametrize("position,entry", [
    ("gitfile-target", "HEAD"), ("intermediate", "HEAD"),
    ("gitfile-pointer", "HEAD"), ("commondir-file", "HEAD"),
    ("alternates-file", "HEAD"), ("git-entry", "HEAD"), ("objects-pack", "HEAD"),
])
def test_required_locations_survive_two_relative_links(tmp_path, position, entry, record_property):
    observation = check_location_input(tmp_path, dict(position=position, chain=2, relative=True,
                                                      named=False, entry=entry))
    assert observation["outcome"] == "needs-owner"
    record_property("location-regression", json.dumps(observation, sort_keys=True))


def test_dotdot_applies_after_the_link_before_it(tmp_path):
    base = tmp_path.resolve()
    paths = build_location_input(base, dict(position="gitfile-target", chain=2, relative=True,
                                            named=False, entry="HEAD"))
    # owner/hop0 -> owner/hop1 -> stable/container/admin. The parent reached by
    # hop0/.. is stable/container, rather than owner. Lexical normalization lies.
    target = str(paths["owner"] / "hop0") + "/../admin"
    (paths["checkout"] / ".git").write_text("gitdir: " + target + "\n")
    assert git("-C", paths["checkout"], "show", "HEAD:f.txt").returncode == 0
    recorded = dependencies.record(str(paths["checkout"]))
    assert not recorded.get("incomplete"), recorded
    assert str(paths["admin"]) in recorded["git"], recorded
    assert str(paths["owner"] / "hop0") in recorded["git"], recorded
    assert str(paths["owner"] / "hop1") in recorded["git"], recorded


def test_changed_link_chain_with_the_same_final_target_is_incomplete(tmp_path):
    base = tmp_path.resolve()
    target = repo(base / "stable") / ".git"
    first, second, alias = [base / name for name in ("first", "second", "alias")]
    first.symlink_to(target)
    second.symlink_to(target)
    alias.symlink_to(first)
    aliases = dependencies._Aliases()
    assert aliases.resolve(str(alias)) == str(target)
    before = []
    aliases.check(before)
    assert not before
    alias.unlink()
    alias.symlink_to(second)
    assert alias.resolve() == target
    errors = []
    aliases.check(errors)
    assert any(error["file"] == str(alias) for error in errors), errors


@pytest.mark.parametrize("failure", ["missing", "not-directory", "loop"])
def test_kernel_walk_failure_does_not_invent_incomplete_evidence(tmp_path, failure):
    base = tmp_path.resolve()
    checkout = base / "checkout"
    checkout.mkdir()
    dotgit = checkout / ".git"
    if failure == "loop":
        dotgit.symlink_to(".git")
    elif failure == "not-directory":
        (base / "file").write_text("ordinary")
        dotgit.symlink_to(base / "file" / "child")
    else:
        dotgit.symlink_to(base / "missing")
    assert dependencies.record(str(checkout)) == {}


def test_walk_access_error_is_incomplete(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    target = repo(base / "stable") / ".git"
    real = dependencies.os.lstat
    def denied(path, *args, **kwargs):
        if str(path) == str(target):
            raise PermissionError(errno.EACCES, "controlled walk failure", str(path))
        return real(path, *args, **kwargs)
    aliases = dependencies._Aliases()
    monkeypatch.setattr(dependencies.os, "lstat", denied)
    aliases.resolve(str(target))
    errors = []
    aliases.check(errors)
    assert any(error["file"] == str(target) for error in errors), errors


def test_storage_scan_failure_is_incomplete(tmp_path, monkeypatch):
    checkout = repo(tmp_path.resolve() / "checkout")
    real = dependencies.os.scandir
    def denied(path):
        if str(path) == str(checkout / ".git"):
            raise PermissionError(errno.EACCES, "controlled scan failure", str(path))
        return real(path)
    monkeypatch.setattr(dependencies.os, "scandir", denied)
    recorded = dependencies.record(str(checkout))
    assert any(error["file"] == str(checkout / ".git") for error in recorded["incomplete"])


def test_inferred_parent_is_not_scanned(tmp_path):
    paths = build_location_input(tmp_path, dict(position="gitfile-target", chain=2, relative=True,
                                               named=True, entry="HEAD"))
    # An ordinary top-level link in the unused inferred parent is not a Git read.
    (paths["admin"].parent.parent / "decoy").symlink_to(paths["unrelated"])
    recorded = dependencies.record(str(paths["checkout"]))
    assert not recorded.get("incomplete"), recorded
    assert not any(folders.within(path, str(paths["unrelated"])) for path in recorded["git"]), recorded


def routine_input(base: Path, layout: str):
    base = base.resolve()
    primary = repo(base / "repository")
    common = primary / ".git"
    if layout == "plain":
        checkout = primary
        expected = [common, common / "objects"]
    elif layout == "worktree":
        checkout = base / "linked"
        result = git("-C", primary, "worktree", "add", "-q", "--detach", checkout, "HEAD")
        assert result.returncode == 0, result.stderr
        expected = [common / "worktrees" / "linked", common, common / "objects"]
    elif layout == "shared":
        checkout = base / "shared"
        result = git("clone", "-q", "--shared", primary, checkout)
        assert result.returncode == 0, result.stderr
        expected = [checkout / ".git", checkout / ".git" / "objects", common / "objects"]
    elif layout == "new-workdir":
        checkout = primary / "linked"
        admin = checkout / ".git"
        admin.mkdir(parents=True)
        for name in ("config", "refs", "objects", "info"):
            (admin / name).symlink_to(common / name)
        (admin / "HEAD").write_bytes((common / "HEAD").read_bytes())
        expected = [admin, common / "objects", common]
    else:
        checkout = base / "neutral"
        checkout.mkdir()
        expected = []
    return checkout, {"git": list(map(folders.canonical, expected))} if expected else {}


@pytest.mark.parametrize("layout", ["plain", "worktree", "shared", "new-workdir", "none"])
def test_routine_records_are_exactly_unchanged(tmp_path, layout, record_property):
    checkout, expected = routine_input(tmp_path, layout)
    recorded = dependencies.record(str(checkout))
    assert recorded == expected
    record_property("routine", json.dumps(dict(layout=layout, recorded=recorded), sort_keys=True))


def test_scanned_storage_target_beside_checkout_is_still_recorded(tmp_path):
    base = tmp_path.resolve()
    checkout = repo(base / "checkout")
    owner = base / "owner"
    refs = checkout / ".git" / "refs"
    refs.rename(owner)
    refs.symlink_to(owner)
    before = git("-C", checkout, "show", "HEAD:f.txt")
    assert before.returncode == 0
    recorded = dependencies.record(str(checkout))
    assert not recorded.get("incomplete"), recorded
    assert str(owner) in recorded["git"], recorded
    shutil.rmtree(owner)
    assert git("-C", checkout, "show", "HEAD:f.txt").returncode != 0



def test_component_walk_has_a_64_hop_bound(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    target = repo(base / "stable") / ".git"
    for n in reversed(range(66)):
        link = base / f"hop{n}"
        link.symlink_to(target)
        target = link
    real = dependencies.os.readlink
    calls = []
    def reading(path, *args, **kwargs):
        calls.append(path)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(dependencies.os, "readlink", reading)
    aliases = dependencies._Aliases()
    aliases.resolve(str(target))
    assert len(calls) <= 64
    calls.clear()
    errors = []
    aliases.check(errors)
    assert not errors
    assert len(calls) <= 64
