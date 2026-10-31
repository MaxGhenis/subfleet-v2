"""C-8.4: omit failed spellings only when the kernel refuses to walk them."""
import errno
import inspect
import json
import os
from pathlib import Path
import tempfile
import types
import unicodedata

import pytest
from hypothesis import HealthCheck, event, given, note, settings

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.fake.test_retention_dependency_review_r11 import baseline_reader, check_case_rename
from tests.unit.test_retention_location_cycles import ROWS, cycle_route
from tests.unit.test_retention_location_inputs import (
    LOCATION_INPUTS, build_location_input, git, repo,
)
from tests.unit.test_retention_location_spelling import SPELLING_INPUTS, build_spelling_input
from tests.unit.test_retention_locations import routine_input

DATA = (Path(__file__).resolve().parents[2] / "docs" / "reports" /
        "2026-10-31-retention-pins-fix-r11-data")


def r10_reader():
    module = types.ModuleType("subfleet.r11_input_head")
    module.__package__ = "subfleet"
    exec(compile((DATA / "ced-dependencies.py.txt").read_text(),
                 "ced02912b:subfleet/dependencies.py", "exec"), module.__dict__)
    return module


def differential(checkout, record_property, **kwargs):
    """Compare the same held-still filesystem, including full ordered evidence."""
    before = baseline_reader().record(str(checkout), **kwargs)
    r10 = r10_reader().record(str(checkout), **kwargs)
    actual = dependencies.record(str(checkout), **kwargs)
    assert actual.get("incomplete", []) == before.get("incomplete", []) == r10.get("incomplete", [])
    assert set(actual.get("git", [])) <= set(before.get("git", []))
    missing = sorted(set(before.get("git", [])) - set(actual.get("git", [])))
    refused = []
    for place in missing:
        with pytest.raises(OSError) as failure:
            os.stat(place)
        assert failure.value.errno in (errno.ELOOP, errno.ENAMETOOLONG)
        refused.append(dict(place=place, errno=failure.value.errno))
    record_property("differential", json.dumps(dict(baseline=before, r10=r10,
        actual=actual, dropped=refused), sort_keys=True))
    return before, actual


@pytest.mark.parametrize("route,cycle,git_exit,error_count", ROWS,
                         ids=[r + "-" + c for r, c, _, _ in ROWS])
def test_cycle_differential(tmp_path, route, cycle, git_exit, error_count, record_property):
    checkout = cycle_route(tmp_path, route, cycle)
    stable = repo(tmp_path.resolve() / "stable")
    differential(checkout, record_property, review_root=str(stable))
    healthy_before = baseline_reader().record(str(stable))
    (tmp_path.resolve() / "loop").unlink()
    assert dependencies.record(str(stable)) == healthy_before


@pytest.mark.parametrize("layout", ["plain", "worktree", "shared", "new-workdir", "none"])
def test_routine_differential(tmp_path, layout, record_property):
    checkout, expected = routine_input(tmp_path, layout)
    before, actual = differential(checkout, record_property)
    assert actual == before == expected


@settings(max_examples=55, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(LOCATION_INPUTS)
def test_location_strategy_differential(record_property, data):
    note(data)
    record_property("differential-input", json.dumps(data, sort_keys=True))
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
        paths = build_location_input(Path(directory), data)
        before, actual = differential(paths["checkout"], record_property)
        assert actual == before
    event("position=" + data["position"])


@settings(max_examples=55, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(SPELLING_INPUTS)
def test_spelling_strategy_differential(record_property, data):
    note(data)
    record_property("differential-input", json.dumps(data, sort_keys=True))
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
        paths = build_spelling_input(Path(directory), dict(data, chain=2, relative=False, entry="refs"))
        if paths.get("unsupported"):
            record_property("unsupported-spelling", json.dumps(data, sort_keys=True))
            event("unsupported=" + data["spelling"])
            return
        before, actual = differential(paths["checkout"], record_property)
        assert actual == before
    event("position=" + data["position"])
    event("spelling=" + data["spelling"])


@pytest.mark.parametrize("outcome", ["success", "ENOENT", "ENOTDIR", "EACCES", "ELOOP", "ENAMETOOLONG"])
@pytest.mark.parametrize("route", ["found", "entries"])
def test_filter_stat_table(tmp_path, monkeypatch, outcome, route, record_property):
    base = tmp_path.resolve()
    checkout = repo(base / "checkout")
    original_canonical = folders.canonical
    denied = None
    if outcome == "success":
        place = base / "readable"
        place.mkdir()
        stale = str(place).upper()
        if not os.path.exists(stale):
            pytest.skip("requires a volume accepting case aliases")
        place = Path(stale)
    elif outcome == "ENOENT":
        place = base / "missing"
    elif outcome == "ENOTDIR":
        (base / "file").write_text("ordinary")
        place = base / "file" / "child"
    elif outcome == "EACCES":
        denied = base / "denied"
        denied.mkdir()
        place = denied / "child"
        place.mkdir()
        denied.chmod(0)
    elif outcome == "ELOOP":
        (base / "loop").symlink_to("loop/tail")
        place = base / "loop" / "tail"
    else:
        place = base / ("long" * 1024)
    place = str(place)
    try:
        expected_errno = 0 if outcome == "success" else getattr(errno, outcome)
        try:
            os.stat(place)
            actual_errno = 0
        except OSError as exc:
            actual_errno = exc.errno
        if outcome == "EACCES" and actual_errno == 0:
            pytest.skip("effective user can bypass mode bits")
        assert actual_errno == expected_errno
        # Missing/non-directory/denied/long strings need a controlled spelling
        # failure: real canonical preserves these unresolved names. The success
        # row uses a real accepted case alias, and the loop really grows a suffix.
        controlled = outcome in ("ENOENT", "ENOTDIR", "EACCES", "ENAMETOOLONG")
        def spelling(path):
            if os.fspath(path) == place:
                return place + "/changed" if controlled else original_canonical(path)
            return original_canonical(path)
        monkeypatch.setattr(folders, "canonical", spelling)
        assert not dependencies._spelled_location(place)
        if route == "found":
            # Only inject the discovery result; stat and the filter stay real.
            monkeypatch.setattr(dependencies, "_storage", lambda *a, **k: [place])
            def final_spelling(path):
                caller = inspect.currentframe().f_back.f_code.co_name
                if os.fspath(path) == place and caller == "<genexpr>":
                    return place
                return spelling(path)
            monkeypatch.setattr(folders, "canonical", final_spelling)
            result = dependencies.git_storage(str(checkout))
        else:
            aliases = dependencies._Aliases()
            aliases.entries.add(place)
            result = aliases.locations(str(checkout), [])
        record_property("stat-table", json.dumps(dict(route=route, outcome=outcome,
            stat_errno=actual_errno, controlled_failed_equation=controlled,
            controlled_final_spelling=(route == "found"), kept=place in result)))
        assert (place in result) == (outcome not in ("ELOOP", "ENAMETOOLONG"))
    finally:
        if denied is not None:
            denied.chmod(0o700)


def base_of_length(root, length):
    base = root.resolve()
    while length - len(os.fsencode(base)) > 0:
        remaining = length - len(os.fsencode(base))
        assert remaining > 1, (base, length)
        # Leave room for the final slash and component, never a 1-byte gap.
        size = min(80, remaining - 1)
        if remaining - size - 1 == 1:
            size -= 1
        base /= "p" * size
    base.mkdir(parents=True)
    assert len(os.fsencode(base)) == length
    return base


@pytest.mark.parametrize("length", [371, 500])
@pytest.mark.parametrize("route", ["gitfile", "commondir"])
def test_real_enametoolong_cycle(tmp_path, length, route, record_property):
    base = base_of_length(tmp_path, length)
    checkout = cycle_route(base, route, "self-suffix")
    refused = base.joinpath("loop", *(["tail"] * 128), "objects")
    with pytest.raises(OSError) as failure:
        os.stat(refused)
    assert failure.value.errno == errno.ENAMETOOLONG
    outcomes = {}
    records = {}
    for name, module in (("475f63278", baseline_reader()), ("ced02912b", r10_reader()),
                         ("head", dependencies)):
        try:
            value = module.record(str(checkout))
            records[name] = value
            outcomes[name] = dict(recorded=value)
        except OSError as exc:
            outcomes[name] = dict(error_type=type(exc).__name__, errno=exc.errno, file=exc.filename)
    record_property("long-cycle", json.dumps(dict(length=length, route=route,
        refused=str(refused), stat_errno=failure.value.errno, outcomes=outcomes), sort_keys=True))
    if "head" not in records:
        # A shared canonical/discovery exception is a listed difference, not a
        # demand to widen the reader in this round.
        assert not records
        assert outcomes["head"] == outcomes["475f63278"] == outcomes["ced02912b"]
        return
    assert len(records) == 3
    differential(checkout, record_property)


@pytest.mark.parametrize("form", ["parent-case", "unicode-normalization"])
def test_more_readable_rename_forms(world, monkeypatch, form, record_property):
    tree = world.job("owner").resolve()
    owner, commit = source(world, tree)
    target = owner / ".git" / "objects"
    if form == "unicode-normalization":
        # Give the alternates store a Unicode name without adding a symlink
        # in the owner's tree that would independently pin it.
        moved = tree / "sourcé"
        owner.rename(moved)
        owner = moved
        target = owner / ".git" / "objects"
        rename_from = owner
        renamed = owner.with_name(unicodedata.normalize("NFD", owner.name))
        if os.fspath(renamed) == folders.canonical(owner):
            renamed = owner.with_name(unicodedata.normalize("NFC", owner.name))
    else:
        rename_from = target.parent
        renamed = rename_from.with_name(".GIT")
    if not os.path.exists(renamed):
        pytest.skip("volume rejects this spelling alias")
    # The same fixture is restored between old and current discovery, so both
    # readers observe precisely the same race and full records can be compared.
    checkout = world.base / "outside"
    cloned = git("clone", "-q", "--shared", owner, checkout)
    assert cloned.returncode == 0, cloned.stderr
    canonical = folders.canonical
    target = Path(canonical(target))
    original_name = Path(canonical(rename_from))
    assert str(original_name) != str(renamed)
    before = git("-C", checkout, "show", commit + ":owner-only.txt")
    assert before.returncode == 0
    results = []
    for name, module in (("475f63278", baseline_reader()), ("head", dependencies)):
        changed = []
        def spelling(path):
            value = canonical(path)
            caller = inspect.currentframe().f_back.f_code.co_name
            if os.fspath(path) == str(target) and caller == "<genexpr>" and not changed:
                original_name.rename(renamed)
                changed.append(True)
            return value
        with monkeypatch.context() as local:
            local.setattr(folders, "canonical", spelling)
            recorded = module.record(str(checkout))
        assert changed
        assert not recorded.get("incomplete"), recorded
        assert len(recorded["git"]) == 3
        assert git("-C", checkout, "fsck", "--no-dangling").returncode == 0
        middle = git("-C", checkout, "show", commit + ":owner-only.txt")
        assert middle.returncode == 0 and middle.stdout == before.stdout
        observation = dict(reader=name, form=form, recorded=recorded,
                           before=before.returncode, middle=middle.returncode)
        record_property("rename-form", json.dumps(observation, sort_keys=True))
        results.append(recorded)
        renamed.rename(original_name)
    assert results[0] == results[1]
    # Equality makes the retention input identical for either reader. Leave the
    # filesystem renamed for the actual maintenance/read assertion.
    original_name.rename(renamed)
    reader(world, results[1])
    result = maintain(world)
    after = git("-C", checkout, "show", commit + ":owner-only.txt")
    record_property("rename-retention", json.dumps(dict(form=form, result=result,
        before=before.returncode, after=after.returncode), sort_keys=True))
    assert "owner" not in result["pruned"] and tree.is_dir(), (results, result)
    assert after.returncode == 0 and after.stdout == before.stdout


@pytest.mark.parametrize("route", ["alternates", "scan"])
def test_original_rename_record_equals_baseline(world, monkeypatch, route, record_property):
    canonical = folders.canonical

    def compared(workdir):
        tree = world.root.resolve() / "worktrees" / "owner"
        target = tree / "source" / ".git" / "objects" if route == "alternates" else tree / "packs"
        renamed = target.with_name(target.name.upper())
        actual = dependencies.record(workdir)
        # Restore precisely the same fixture and replay the same boundary race
        # against the old reader, rather than comparing path-normalized copies.
        renamed.rename(target)
        changed = []
        def spelling(path):
            value = canonical(path)
            caller = inspect.currentframe().f_back.f_code.co_name
            wanted = "<genexpr>" if route == "alternates" else "scan"
            if os.fspath(path) == str(target) and caller == wanted and not changed:
                target.rename(renamed)
                changed.append(True)
            return value
        with monkeypatch.context() as local:
            local.setattr(folders, "canonical", spelling)
            before = baseline_reader().record(workdir)
        assert changed
        record_property("original-rename-comparison", json.dumps(dict(route=route,
            baseline=before, actual=actual), sort_keys=True))
        assert actual == before
        assert len(actual["git"]) == 3 and not actual.get("incomplete"), actual
        return actual

    check_case_rename(world, monkeypatch, route, types.SimpleNamespace(record=compared), record_property)
