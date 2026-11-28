"""C-8.4: every accepted spelling records the same physical dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unicodedata
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, event, given, note, settings, strategies as st

from subfleet import dependencies, folders
from tests.unit.test_retention_location_inputs import POSITIONS, git, repo

SPELLINGS = ["data-volume", "other-case", "other-normalization"]
# A finite cross product reaches every position/spelling class in 55 draws.
# Named-admin variants add another Data-volume and case route per position.
SPELLING_INPUTS = st.tuples(
    st.sampled_from(POSITIONS),
    st.sampled_from([(name, named) for named in (False, True)
                     for name in SPELLINGS if not (named and name == "other-normalization")]),
).map(lambda value: dict(position=value[0], spelling=value[1][0], named=value[1][1]))


def spelling_path(path: Path, spelling: str) -> Path | None:
    """An accepted noncanonical spelling, or an explicit unsupported form."""
    if spelling == "data-volume":
        alias = Path("/System/Volumes/Data") / path.relative_to("/")
    elif spelling == "other-case":
        alias = Path(str(path).swapcase())
    else:
        raw = str(path)
        form = "NFC" if unicodedata.normalize("NFD", raw) == raw else "NFD"
        alias = Path(unicodedata.normalize(form, raw))
    try:
        if str(alias) == str(path) or not os.path.samestat(alias.stat(), path.stat()):
            return None
    except OSError:
        return None
    assert folders.canonical(alias) == folders.canonical(path)
    return alias


def assert_location_spelling(workdir, places=None):
    """Check strings as consumers receive them; only physical links are exempt
    from following the final entry. Capture that set from actual discovery."""
    captures = []
    original = dependencies._Aliases

    class Observed(original):
        def __init__(self):
            super().__init__()
            captures.append(self)

    with patch.object(dependencies, "_Aliases", Observed):
        actual = dependencies.git_storage(str(workdir))
    if places is not None:
        assert actual == places, (actual, places)
    links = set().union(*(item.links for item in captures))
    for place in actual:
        assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place
        if place not in links:
            assert folders.canonical(place) == place, place
    return links


def build_spelling_input(base: Path, data: dict) -> dict:
    # Every path used in an equality check is physical, including linked TMPDIR.
    base = Path(folders.canonical(base))
    owner, stable, unrelated, checkout = [base / name for name in ("ownér", "stable", "unrelated", "checkóut")]
    for path in (owner, stable, unrelated, checkout):
        path.mkdir()
    owner = Path(folders.canonical(owner))
    # Check the owner spelling before any Git process, reporting unsupported
    # volume forms as individual generated observations instead of filtering.
    if spelling_path(owner, data["spelling"]) is None:
        return dict(unsupported=True)

    def reach(path):
        alias = spelling_path(path, data["spelling"])
        assert alias is not None, path
        return alias

    checkout = Path(folders.canonical(checkout))
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
        if pointer:
            destination = reach(destination)
        for n in reversed(range(data["chain"])):
            link = directory / f"hop{n}"
            target = os.path.relpath(destination, link.parent) if data["relative"] else str(destination)
            link.symlink_to(target)
            destination = link
        return destination if pointer else reach(destination)

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
        destination = owner / "entry"
        entry.rename(destination)
        entry.symlink_to(through(destination, pointer=True))
    elif position == "objects-pack":
        packed = git("-C", checkout, "gc", "--prune=now")
        assert packed.returncode == 0, packed.stderr
        destination = owner / "packs"
        (admin / "objects" / "pack").rename(destination)
        (admin / "objects" / "pack").symlink_to(through(destination, pointer=True))
    else:
        raise AssertionError(position)
    return dict(base=base, owner=owner, stable=stable, unrelated=unrelated,
                checkout=checkout, admin=admin)


def check_spelling_input(base: Path, data: dict) -> dict:
    parameters = dict(data, chain=2, relative=False, entry="refs")
    paths = build_spelling_input(base, parameters)
    if paths.get("unsupported"):
        return dict(input=data, outcome="skipped", reason="volume rejects " + data["spelling"])
    checkout, owner, unrelated = [paths[key] for key in ("checkout", "owner", "unrelated")]
    before = git("-C", checkout, "show", "HEAD:f.txt")
    assert before.returncode == 0 and before.stdout == b"source", before.stderr
    supplied = spelling_path(checkout, data["spelling"])
    assert supplied is not None, checkout
    recorded = dependencies.record(str(supplied))
    assert not recorded.get("incomplete"), recorded
    canonical_owner = folders.canonical(owner)
    assert not any(folders.within(place, folders.canonical(unrelated)) for place in recorded.get("git", [])), recorded
    assert_location_spelling(supplied, recorded.get("git", []))
    assert str(checkout / ".git") not in recorded.get("git", []), recorded
    # Use precisely the consumer's string comparison, never realpath(place).
    assert any(folders.within(place, canonical_owner) for place in recorded.get("git", [])), recorded
    shutil.rmtree(owner)
    after = git("-C", checkout, "show", "HEAD:f.txt")
    assert after.returncode != 0, (recorded, after.stdout)
    return dict(input=data, outcome="needs-owner", before=before.returncode,
                after=after.returncode, recorded=recorded)


@settings(max_examples=55, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(SPELLING_INPUTS)
def test_spelling_inputs_keep_the_places_real_git_traverses(record_property, data):
    note(data)
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
        observation = check_spelling_input(Path(directory), data)
    record_property("spelling-input", json.dumps(observation, sort_keys=True))
    event("position=" + data["position"])
    event("spelling=" + data["spelling"])
    event(observation["outcome"])


@pytest.mark.parametrize("position", ["gitfile-pointer", "commondir-file", "alternates-file", "objects-pack"])
def test_review_r9_sibling_has_one_spelling(tmp_path, position, record_property):
    observation = check_spelling_input(tmp_path, dict(position=position, spelling="data-volume", named=False))
    record_property("r9-sibling", json.dumps(observation, sort_keys=True))
    if observation["outcome"] == "skipped":
        pytest.skip(observation["reason"])


@pytest.mark.parametrize("layout", ["plain", "worktree"])
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_routine_alias_keeps_the_exact_record(tmp_path, layout, spelling):
    from tests.unit.test_retention_locations import routine_input
    base = tmp_path.resolve() / "ownér"
    base.mkdir()
    base = Path(folders.canonical(base))
    alias = spelling_path(base, spelling)
    if alias is None:
        pytest.skip("volume rejects " + spelling)
    checkout, expected = routine_input(base, layout)
    supplied = alias / checkout.relative_to(base)
    recorded = dependencies.record(str(supplied))
    assert recorded == expected
    assert_location_spelling(supplied, recorded.get("git", []))
