"""C-8.4, review r13: the ELOOP-only filter's argument, rows and races, executed."""
import errno
import inspect
import json
import os
from pathlib import Path
import shutil

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r11 import baseline_reader
from tests.unit import test_retention_location_readable as readable
from tests.unit.test_retention_location_cycles import ROWS, cycle_at
from tests.unit.test_retention_location_inputs import git, repo

OUT = None


@pytest.fixture(scope="module", autouse=True)
def evidence_folder(tmp_path_factory):
    """The review's receipts live in its data folder; a rerun writes under pytest's own."""
    global OUT
    OUT = tmp_path_factory.mktemp("r13-evidence")


def stat_errno(path):
    try:
        os.stat(path)
        return 0
    except OSError as exc:
        return exc.errno
    except ValueError as exc:
        return "ValueError:" + str(exc)


class Spelled:
    """Log head's filtered spellings: found (`<genexpr>`) and scan targets (`scan`)."""

    def __init__(self, monkeypatch):
        self.pairs = []
        original = folders.canonical

        def spelling(path):
            value = original(path)
            frame = inspect.currentframe().f_back
            if (frame.f_globals.get("__name__") == "subfleet.dependencies"
                    and frame.f_code.co_name in ("<genexpr>", "scan")):
                before = os.fspath(path)
                self.pairs.append(dict(route=frame.f_code.co_name, before=before, after=value,
                                       before_bytes=len(os.fsencode(before)), after_bytes=len(os.fsencode(value)),
                                       stat_before=stat_errno(before), stat_after=stat_errno(value),
                                       spelled=dependencies._spelled_location(value)))
            return value

        monkeypatch.setattr(folders, "canonical", spelling)


def save(name, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"stat-pairs-{name}.json").write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")


def judge(pairs):
    """The hub's argument: a path the kernel walks has a canonical string stat cannot refuse with ELOOP."""
    for pair in pairs:
        if pair["stat_before"] == 0:
            assert pair["stat_after"] != errno.ELOOP, pair
        if pair["stat_after"] == errno.ELOOP:
            assert not pair["spelled"], pair


def properties():
    found = []
    return found, (lambda key, value: found.append((key, value)))


def test_stat_pairs_fixed_rows(tmp_path_factory, monkeypatch):
    spelled = Spelled(monkeypatch)
    seen, record_property = properties()
    rows = []
    for route, cycle, git_exit, error_count in ROWS:
        start = len(spelled.pairs)
        readable.test_cycle_differential(tmp_path_factory.mktemp("cycle"), route, cycle, git_exit,
                                         error_count, record_property)
        rows.extend(dict(row=f"cycle:{route}-{cycle}", **p) for p in spelled.pairs[start:])
    for layout in ["plain", "worktree", "shared", "new-workdir", "none"]:
        start = len(spelled.pairs)
        readable.test_routine_differential(tmp_path_factory.mktemp("routine"), layout, record_property)
        rows.extend(dict(row=f"routine:{layout}", **p) for p in spelled.pairs[start:])
    for route in ["gitfile", "commondir"]:
        for length in [371, 500]:
            start = len(spelled.pairs)
            readable.test_real_enametoolong_cycle(tmp_path_factory.mktemp("long"), length, route, record_property)
            rows.extend(dict(row=f"long:{route}-{length}", **p) for p in spelled.pairs[start:])
    save("fixed", rows)
    judge(rows)


@pytest.mark.parametrize("strategy", ["location", "spelling"])
def test_stat_pairs_strategies(monkeypatch, strategy):
    spelled = Spelled(monkeypatch)
    seen, record_property = properties()
    # Call the round's own @given tests: the same derandomized draws, unchanged.
    if strategy == "location":
        readable.test_location_strategy_differential(record_property)
    else:
        readable.test_spelling_strategy_differential(record_property)
    inputs = [value for key, value in seen if key == "differential-input"]
    save(strategy, dict(inputs=inputs, pairs=spelled.pairs))
    judge(spelled.pairs)


def gitfile_retarget(base, variant):
    """The gitfile route: a healthy link or directory, made a cycle once discovery read it."""
    checkout = repo(base / "checkout")
    admin = base / "admin"
    shutil.move(checkout / ".git", admin)
    if variant == "link":
        loop = base / "loop"
        loop.symlink_to(admin)
        (checkout / ".git").write_text("gitdir: " + str(loop) + "\n")
        changed_path = loop
    else:
        cycle_at(base, "self-suffix")
        (checkout / ".git").write_text("gitdir: " + str(admin) + "\n")
        changed_path = admin
    return checkout, admin, changed_path


def retarget(base, admin, variant):
    if variant == "link":
        (base / "loop").unlink()
        cycle_at(base, "self-suffix")
    else:
        admin.rename(base / "admin-moved")
        admin.symlink_to(base / "loop")


@pytest.mark.parametrize("variant", ["link", "resolved-target"])
def test_gitfile_retargeted_into_cycle_between_read_and_filter(tmp_path, monkeypatch, variant, record_property):
    observations = {}
    for name in ("475f63278", "head"):
        base = (tmp_path / name).resolve()
        base.mkdir()
        checkout, admin, changed_path = gitfile_retarget(base, variant)
        before = git("-C", checkout, "show", "HEAD:f.txt")
        assert before.returncode == 0, before.stderr
        module = baseline_reader() if name == "475f63278" else dependencies
        canonical = folders.canonical
        changed = []
        stat_at_filter = []

        def spelling(path):
            value = canonical(path)
            caller = inspect.currentframe().f_back.f_code.co_name
            # The first spelling of a found place: discovery has read every
            # pointer, and head's filter stats this string next.
            if caller == "<genexpr>" and not changed:
                retarget(base, admin, variant)
                changed.append(os.fspath(path))
                stat_at_filter.append(stat_errno(value))
            return value

        with monkeypatch.context() as local:
            local.setattr(folders, "canonical", spelling)
            recorded = module.record(str(checkout))
        assert changed
        after = git("-C", checkout, "show", "HEAD:f.txt")
        observations[name] = dict(recorded=json.loads(json.dumps(recorded).replace(str(base), "<base>")),
                                  changed_at=changed[0].replace(str(base), "<base>"),
                                  stat_at_filter=stat_at_filter[0], read_before=before.returncode,
                                  read_after=after.returncode, changed_path=str(changed_path).replace(str(base), "<base>"))
    record_property("r13-retarget", json.dumps(dict(variant=variant, observations=observations), sort_keys=True))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"retarget-{variant}.json").write_text(json.dumps(observations, indent=1, sort_keys=True) + "\n")
    head = observations["head"]["recorded"]
    assert any(error["file"] == observations["head"]["changed_path"] and "alias changed" in error["reason"]
               for error in head.get("incomplete", [])), observations


def test_embedded_nul_alternates_line(tmp_path, record_property):
    base = tmp_path.resolve()
    source = repo(base / "source")
    checkout = base / "checkout"
    assert git("clone", "-q", "--shared", source, checkout).returncode == 0
    alternates = checkout / ".git" / "objects" / "info" / "alternates"
    alternates.write_bytes(os.fsencode(source / ".git" / "objects") + b"\x00tail\n")
    read = git("-C", checkout, "show", "HEAD:f.txt")
    fsck = git("-C", checkout, "fsck", "--no-dangling")
    outcomes = {}
    for name, module in (("475f63278", baseline_reader()), ("head", dependencies)):
        try:
            outcomes[name] = dict(recorded=json.loads(json.dumps(module.record(str(checkout))).replace(str(base), "<base>")))
        except Exception as exc:  # noqa: BLE001 - the outcome is the observation
            outcomes[name] = dict(error_type=type(exc).__name__, error=str(exc))
    observation = dict(read=read.returncode, read_stdout=read.stdout.decode(), fsck=fsck.returncode, outcomes=outcomes)
    record_property("r13-nul", json.dumps(observation, sort_keys=True))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "nul-alternates.json").write_text(json.dumps(observation, indent=1, sort_keys=True) + "\n")
    assert outcomes["head"] == outcomes["475f63278"], observation


class ThreeWay:
    """Compare head's record with 475f63278's and ced02912b's on the same filesystem, entry for entry."""

    def __init__(self, monkeypatch):
        self.rows = []
        self.node = ""
        head = dependencies.record
        base = baseline_reader()
        ced = readable.r10_reader()

        def record(*args, **kwargs):
            actual = head(*args, **kwargs)
            before = base.record(*args, **kwargs)
            old = ced.record(*args, **kwargs)
            missing = sorted(set(before.get("git", [])) - set(actual.get("git", [])))
            self.rows.append(dict(node=self.node, args=[str(a) for a in args],
                incomplete_equal=actual.get("incomplete", []) == before.get("incomplete", []) == old.get("incomplete", []),
                incomplete_count=[len(r.get("incomplete", [])) for r in (before, old, actual)],
                record_equal_base=actual == before, subset=set(actual.get("git", [])) <= set(before.get("git", [])),
                missing=[dict(place=p, stat=stat_errno(p)) for p in missing],
                extra=sorted(set(actual.get("git", [])) - set(before.get("git", [])))))
            return actual

        monkeypatch.setattr(dependencies, "record", record)


@pytest.mark.parametrize("module", ["cycles", "routine", "inputs", "spelling"])
def test_evidence_three_way(tmp_path_factory, monkeypatch, module):
    from tests.unit import test_retention_location_cycles as cycles
    from tests.unit import test_retention_location_inputs as inputs
    from tests.unit import test_retention_location_spelling as spelling
    from tests.unit import test_retention_locations as locations
    three = ThreeWay(monkeypatch)
    seen, record_property = properties()
    if module == "cycles":
        for route, cycle, git_exit, error_count in ROWS:
            three.node = f"{route}-{cycle}"
            cycles.test_cycle_routes_keep_spelling_and_evidence(tmp_path_factory.mktemp("c"), route, cycle,
                                                                git_exit, error_count, record_property)
            three.node = f"beside:{route}-{cycle}"
            cycles.test_readable_repository_beside_cycle_keeps_every_place(tmp_path_factory.mktemp("b"), route,
                                                                           cycle, git_exit, error_count)
    elif module == "routine":
        for layout in ["plain", "worktree", "shared", "new-workdir", "none"]:
            three.node = layout
            locations.test_routine_records_are_exactly_unchanged(tmp_path_factory.mktemp("r"), layout, record_property)
    elif module == "inputs":
        three.node = "location-inputs"
        inputs.test_location_inputs_keep_the_places_real_git_traverses(record_property)
    else:
        three.node = "spelling-inputs"
        spelling.test_spelling_inputs_keep_the_places_real_git_traverses(record_property)
        for position in ["gitfile-pointer", "commondir-file", "alternates-file", "objects-pack"]:
            three.node = "r9-sibling:" + position
            spelling.test_review_r9_sibling_has_one_spelling(tmp_path_factory.mktemp("s"), position, record_property)
        for layout in ["plain", "worktree"]:
            for name in spelling.SPELLINGS:
                three.node = f"routine-alias:{layout}-{name}"
                spelling.test_routine_alias_keeps_the_exact_record(tmp_path_factory.mktemp("a"), layout, name)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"evidence-{module}.json").write_text(json.dumps(three.rows, indent=1, sort_keys=True) + "\n")
    assert three.rows
    assert all(row["incomplete_equal"] and row["subset"] and not row["extra"] for row in three.rows)
    assert all(m["stat"] == errno.ELOOP for row in three.rows for m in row["missing"])
