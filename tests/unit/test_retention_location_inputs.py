"""C-8.4: removing a required traversal location breaks the same real Git read."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from hypothesis import HealthCheck, event, given, note, settings, strategies as st

from subfleet import dependencies, folders
from tests.unit.test_retention_pointer_whitespace import git as raw_git


def git(*args, **kwargs):
    # Keep every Git process synchronous, including automatic maintenance.
    return raw_git("-c", "maintenance.auto=false", "-c", "gc.auto=0",
                   "-c", "core.hooksPath=/dev/null", *args, **kwargs)


def repo(path):
    path.mkdir(parents=True)
    assert git("init", "-q", "-b", "main", path).returncode == 0
    (path / "f.txt").write_text(path.name)
    assert git("-C", path, "add", "f.txt").returncode == 0
    committed = git("-C", path, "-c", "user.name=Location test", "-c",
                    "user.email=location@example.invalid", "commit", "-q", "-m", "fixture")
    assert committed.returncode == 0, committed.stderr
    return path


POSITIONS = ["gitfile-target", "intermediate", "gitfile-pointer", "commondir-file",
             "commondir-target", "objects", "objects-info", "alternates-file",
             "alternates-line", "git-entry", "objects-pack"]
LOCATION_INPUTS = st.fixed_dictionaries({
    "position": st.sampled_from(POSITIONS),
    "chain": st.integers(min_value=1, max_value=3),
    "relative": st.booleans(),
    "named": st.booleans(),
    "entry": st.sampled_from(["HEAD", "refs"]),
})


def build_location_input(base: Path, data: dict) -> dict:
    # Every path used in an equality check is physical, including linked TMPDIR.
    base = base.resolve()
    owner, stable, unrelated, checkout = [base / name for name in ("owner", "stable", "unrelated", "checkout")]
    for path in (owner, stable, unrelated, checkout):
        path.mkdir()
    source = repo(stable / "source")
    admin = stable / "container" / "worktrees" / "slot" if data["named"] else stable / "container" / "admin"
    shutil.copytree(source / ".git", admin)
    if data["named"]:
        # Git uses the admin's own store. The parent pointer is unrelated input,
        # deliberately unreadable as a file; it must never become global evidence.
        (admin.parent.parent / "commondir").mkdir()
    (checkout / ".git").write_text("gitdir: " + str(admin) + "\n")

    def through(destination: Path, *, pointer: bool = False) -> Path:
        # In file-location classes the owner holds only the physical pointer;
        # all the symlink entries are stable. This distinguishes decisions 1/2.
        directory = stable if pointer else owner
        for n in reversed(range(data["chain"])):
            link = directory / f"hop{n}"
            target = os.path.relpath(destination, link.parent) if data["relative"] else str(destination)
            link.symlink_to(target)
            destination = link
        return destination

    def alternate_store():
        shutil.rmtree(admin / "objects")
        info = admin / "objects" / "info"
        info.mkdir(parents=True)
        (info / "alternates").write_text(str(source / ".git" / "objects") + "\n")
        return info

    position = data["position"]
    if position == "gitfile-target":
        (checkout / ".git").write_text("gitdir: " + str(through(admin)) + "\n")
    elif position == "intermediate":
        (checkout / ".git").write_text("gitdir: " + str(through(admin.parent) / admin.name) + "\n")
    elif position == "gitfile-pointer":
        pointer = owner / "pointer"
        (checkout / ".git").rename(pointer)
        (checkout / ".git").symlink_to(through(pointer, pointer=True))
    elif position.startswith("commondir-"):
        common = stable / "common"
        shutil.copytree(admin, common)
        if position == "commondir-file":
            pointer = owner / "pointer"
            pointer.write_text(str(common) + "\n")
            (admin / "commondir").symlink_to(through(pointer, pointer=True))
        else:
            (admin / "commondir").write_text(str(through(common)) + "\n")
    elif position == "objects":
        objects = stable / "object-store"
        (admin / "objects").rename(objects)
        (admin / "objects").symlink_to(through(objects))
    elif position == "objects-info":
        info = alternate_store()
        destination = stable / "object-info"
        info.rename(destination)
        info.symlink_to(through(destination))
    elif position == "alternates-file":
        info = alternate_store()
        pointer = owner / "pointer"
        (info / "alternates").rename(pointer)
        (info / "alternates").symlink_to(through(pointer, pointer=True))
    elif position == "alternates-line":
        info = alternate_store()
        (info / "alternates").write_text(str(through(source / ".git" / "objects")) + "\n")
    elif position == "git-entry":
        entry = admin / data["entry"]
        if data["entry"] == "HEAD":
            # Git's HEAD symlink must name refs/... (legacy symbolic HEAD).
            # An arbitrary absolute symlink to a HEAD file is rejected by Git.
            entry.unlink()
            entry.symlink_to("refs/heads/main")
            entry = admin / "refs" / "heads" / "main"
        destination = stable / "entry"
        entry.rename(destination)
        entry.symlink_to(through(destination))
    elif position == "objects-pack":
        packed = git("-C", checkout, "gc", "--prune=now")
        assert packed.returncode == 0, packed.stderr
        destination = stable / "packs"
        (admin / "objects" / "pack").rename(destination)
        (admin / "objects" / "pack").symlink_to(through(destination))
    else:
        raise AssertionError(position)
    return dict(base=base, owner=owner, stable=stable, unrelated=unrelated,
                checkout=checkout, admin=admin)


def check_location_input(base: Path, data: dict) -> dict:
    paths = build_location_input(base, data)
    checkout, owner, unrelated = [paths[key] for key in ("checkout", "owner", "unrelated")]
    before = git("-C", checkout, "show", "HEAD:f.txt")
    assert before.returncode == 0 and before.stdout == b"source", before.stderr
    recorded = dependencies.record(str(checkout))
    assert not recorded.get("incomplete"), recorded
    assert not any(folders.within(path, str(unrelated)) for path in recorded.get("git", [])), recorded
    held = any(folders.within(path, str(owner)) for path in recorded.get("git", []))
    shutil.rmtree(owner)
    after = git("-C", checkout, "show", "HEAD:f.txt")
    if not held and after.returncode == 0:
        outcome = "independent"
        assert after.stdout == before.stdout
    else:
        assert held, recorded
        assert after.returncode != 0, (recorded, after.stdout)
        outcome = "needs-owner"
    return dict(input=data, outcome=outcome, before=before.returncode, after=after.returncode,
                incomplete=bool(recorded.get("incomplete")), recorded=recorded)


@settings(max_examples=55, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(LOCATION_INPUTS)
def test_location_inputs_keep_the_places_real_git_traverses(record_property, data):
    note(data)
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
        observation = check_location_input(Path(directory), data)
    record_property("location-input", json.dumps(observation, sort_keys=True))
    event("position=" + data["position"])
    event(observation["outcome"])
