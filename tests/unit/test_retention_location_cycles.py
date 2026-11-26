"""C-8.4: cycles cannot add unspelled places or remove readable storage."""
from pathlib import Path
import json
import os
import shutil

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_location_inputs import git, repo
from tests.unit.test_retention_location_spelling import assert_location_spelling


ROWS = [
    ("control", "self-suffix", 0, 0),
    ("scan", "self-suffix", 0, 0),
    ("alternates", "self-suffix", 0, 2),
    ("alternates-tail", "self-suffix", 0, 2),
    ("gitfile", "self-suffix", 128, 4),
    ("commondir", "self-suffix", 128, 3),
    ("objects-alternates", "self-suffix", 0, 2),
    ("scan", "two-link-suffix", 0, 0),
    ("alternates", "two-link-suffix", 0, 2),
]


def cycle_at(base: Path, cycle: str) -> Path:
    loop = base / "loop"
    if cycle == "self-suffix":
        loop.symlink_to("loop/tail")
    else:
        loop.symlink_to("other/tail")
        (base / "other").symlink_to("loop/extra")
    return loop


def cycle_route(base: Path, route: str, cycle: str):
    base = base.resolve()
    checkout = repo(base / "checkout")
    loop = cycle_at(base, cycle)
    admin = checkout / ".git"
    if route == "scan":
        (admin / "unused").symlink_to(loop)
    elif route in ("alternates", "alternates-tail", "objects-alternates"):
        objects = admin / "objects"
        if route == "objects-alternates":
            objects.rename(base / "store")
            objects.symlink_to(base / "store")
        target = loop / "x" if route == "alternates-tail" else loop
        (objects / "info" / "alternates").write_text(str(target) + "\n")
    elif route == "gitfile":
        shutil.rmtree(admin)
        admin.write_text("gitdir: " + str(loop) + "\n")
    elif route == "commondir":
        (admin / "commondir").write_text(str(loop) + "\n")
    else:
        assert route == "control"
    return checkout


def expected_error_files(base: Path, route: str, cycle: str) -> list[str]:
    """Input-head error files: pin all entries, including repeats, not errno text."""
    base = base.resolve()
    suffix = ["tail"] * 64 if cycle == "self-suffix" else ["extra", "tail"] * 32
    walked = base.joinpath("loop", *suffix)
    if route in ("control", "scan"):
        return []
    if route in ("alternates", "alternates-tail", "objects-alternates"):
        target = walked / "x" if route == "alternates-tail" else walked
        return list(map(str, [target, target / "info" / "alternates"]))
    objects = base.joinpath("loop", *suffix, *suffix, "objects")
    files = [walked, objects, walked / "objects" / "info" / "alternates"]
    if route == "gitfile":
        files.append(walked / "commondir")
    return list(map(str, files))


@pytest.mark.parametrize("route,cycle,git_exit,error_count", ROWS,
                         ids=[r + "-" + c for r, c, _, _ in ROWS])
def test_cycle_routes_keep_spelling_and_evidence(tmp_path, route, cycle, git_exit,
                                                error_count, record_property):
    checkout = cycle_route(tmp_path, route, cycle)
    health = git("-C", checkout, "fsck", "--no-dangling")
    read = git("-C", checkout, "show", "HEAD:f.txt")
    assert health.returncode == read.returncode == git_exit
    recorded = dependencies.record(str(checkout))
    errors = recorded.get("incomplete", [])
    record_property("cycle-route", json.dumps(dict(route=route, cycle=cycle,
        fsck=health.returncode, read=read.returncode, recorded=recorded), sort_keys=True))
    assert len(errors) == error_count, errors
    assert [e["file"] for e in errors] == expected_error_files(tmp_path, route, cycle)
    links = assert_location_spelling(checkout, recorded.get("git", []))
    # Filtering an unreadable target must retain every traversed physical link.
    if route != "control":
        assert str(tmp_path.resolve() / "loop") in links


@pytest.mark.parametrize("route,cycle,git_exit,error_count", ROWS,
                         ids=[r + "-" + c for r, c, _, _ in ROWS])
def test_readable_repository_beside_cycle_keeps_every_place(tmp_path, route, cycle,
                                                          git_exit, error_count):
    checkout = cycle_route(tmp_path, route, cycle)
    stable = repo(tmp_path.resolve() / "stable")
    # The input-head comparison pins these two places for the healthy sibling;
    # an empty result from a broken filter must not become its own oracle.
    before = [folders.canonical(stable / ".git"),
              folders.canonical(stable / ".git" / "objects")]
    assert dependencies.git_storage(str(stable)) == before
    # One discovery includes both inputs, as a job and its review root do.
    recorded = dependencies.record(str(checkout), review_root=str(stable))
    assert set(before) <= set(recorded.get("git", [])), recorded
    assert git("-C", stable, "show", "HEAD:f.txt").returncode == 0
    assert_location_spelling(stable, before)
    # Removing only the cycle leaves the same physical healthy repository.
    (tmp_path.resolve() / "loop").unlink()
    assert dependencies.git_storage(str(stable)) == before


@pytest.mark.parametrize("changed_path", ["scan-link", "resolved-target"])
def test_scan_retargeted_into_cycle_before_spelling_is_incomplete(tmp_path, monkeypatch,
                                                                changed_path):
    base = tmp_path.resolve()
    checkout = repo(base / "checkout")
    owner = base / "owner"
    owner.mkdir()
    (owner / "place").mkdir()
    loop = cycle_at(base, "self-suffix")
    entry = checkout / ".git" / "unused"
    entry.symlink_to(owner / "place")
    original = dependencies._Aliases.locations
    changed = []

    def retarget(aliases, start, recorded):
        # resolve and scan have finished. Make the recorded scan target unreadable
        # exactly as the returned list starts to be built, after the old check.
        if not changed:
            if changed_path == "scan-link":
                entry.unlink()
                entry.symlink_to(loop)
            else:
                (owner / "place").rmdir()
                (owner / "place").symlink_to(loop)
            changed.append(True)
        return original(aliases, start, recorded)

    monkeypatch.setattr(dependencies._Aliases, "locations", retarget)
    recorded = dependencies.record(str(checkout))
    assert changed
    assert any(error["file"] == str(entry) and "alias changed" in error["reason"]
               for error in recorded.get("incomplete", [])), recorded
    if changed_path == "resolved-target":
        assert not any(place.startswith(str(owner) + os.sep) for place in recorded.get("git", []))
    assert git("-C", checkout, "show", "HEAD:f.txt").returncode == 0
    for place in recorded.get("git", []):
        assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place
        assert folders.canonical(place) == place
    # Re-discovery sees the retargeted chain, so its physical entry set differs.
    assert_location_spelling(checkout)
