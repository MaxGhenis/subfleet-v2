"""The CI shards partition the suite: every test file runs in exactly one shard (D-CI2).

Invariants, for every map, group list, shard count and set of test files:
- partition: each file has exactly one shard in [0, N), so N shard jobs run
  every collected test once (checked on real pytest collections below);
- pure: a file's shard depends only on its path, the map and N, never on which
  other files exist or the map's key order, so every shard job of a run agrees;
- cohesive: files under one group prefix share a shard, measured or not;
- bounded: the greedy plan's largest predicted shard is at most the mean plus
  the largest unit (list scheduling's bound);
- a shard imports only its own test files.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import zlib

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from tools import ci_shard_durations as durations_tool
from tools import ci_shard_plugin as shard_plugin

pytest_plugins = ["pytester"]
REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
WORKFLOW = REPO / ".github/workflows/tests.yml"
MAP = REPO / ".github/test-shards.json"

segment = st.text("abcdefgh_", min_size=1, max_size=6)
group_names = st.lists(segment, max_size=3, unique=True).map(lambda names: [f"tests/{name}/" for name in names])
file_paths = st.builds(lambda parts, name: "tests/" + "".join(f"{part}/" for part in parts) + f"test_{name}.py",
                       st.lists(segment, max_size=2), segment)
millis_maps = st.dictionaries(file_paths, st.integers(0, 10**7), max_size=40)


@given(millis_maps, group_names, st.integers(1, 9), st.lists(file_paths, max_size=20), st.randoms())
@settings(max_examples=300, deadline=None)
def test_every_file_has_one_shard_whatever_else_exists(millis, groups, count, extra, rng):
    shards = shard_plugin.plan(millis, groups, count)
    for path in [*millis, *extra]:
        index = shard_plugin.shard_of(path, shards, groups, count)
        assert 0 <= index < count
        # Pure: the same answer from a map in another key order.
        shuffled = list(millis.items())
        rng.shuffle(shuffled)
        assert shard_plugin.shard_of(path, shard_plugin.plan(dict(shuffled), groups, count), groups, count) == index


@given(millis_maps, group_names, st.integers(1, 9), st.lists(file_paths, max_size=20))
@settings(max_examples=300, deadline=None)
def test_a_group_keeps_its_files_together(millis, groups, count, extra):
    shards = shard_plugin.plan(millis, groups, count)
    for group in groups:
        members = [path for path in [*millis, *extra] if path.startswith(group)]
        assert len({shard_plugin.shard_of(path, shards, groups, count) for path in members}) <= 1


@given(millis_maps, group_names, st.integers(1, 9))
@settings(max_examples=300, deadline=None)
def test_the_plan_is_within_list_schedulings_bound(millis, groups, count):
    loads = shard_plugin.predicted_loads(millis, groups, count)
    assert sum(loads) == sum(millis.values())
    units = {}
    for path, cost in millis.items():
        unit = shard_plugin.group_of(path, groups) or path
        units[unit] = units.get(unit, 0) + cost
    largest = max(units.values(), default=0)
    assert count * max(loads) <= sum(loads) + count * largest


@given(st.lists(file_paths, max_size=30), st.integers(1, 9))
def test_an_unmeasured_file_falls_back_to_its_checksum(paths, count):
    for path in paths:
        assert shard_plugin.shard_of(path, {}, [], count) == zlib.crc32(path.encode()) % count


@given(st.integers(1, shard_plugin.MAX_SHARDS).flatmap(lambda n: st.tuples(st.integers(1, n), st.just(n))))
def test_shard_specs_round_trip(spec):
    index, count = spec
    assert shard_plugin.parse_shard(f"{index}/{count}") == (index - 1, count)


@pytest.mark.parametrize("text", ["0/4", "5/4", "1/0", "4", "1/4/2", " 1/4", "01/4", "a/b", "1/65", ""])
def test_malformed_shard_specs_are_refused(text):
    with pytest.raises(ValueError):
        shard_plugin.parse_shard(text)


@pytest.mark.parametrize("data", [
    [], {"seconds": {}}, {"groups": []}, {"seconds": [], "groups": []},
    {"seconds": {}, "groups": ["tests/frontend"]},              # not a directory prefix
    {"seconds": {}, "groups": ["/tests/"]},
    {"seconds": {}, "groups": ["tests/", "tests/frontend/"]},  # overlapping
    {"seconds": {}, "groups": ["tests/a/", "tests/a/"]},
    {"seconds": {"tests/test_a.py": -1}, "groups": []},
    {"seconds": {"tests/test_a.py": float("nan")}, "groups": []},
    {"seconds": {"tests/test_a.py": float("inf")}, "groups": []},
    {"seconds": {"tests/test_a.py": True}, "groups": []},
    {"seconds": {"tests/test_a.py": "1"}, "groups": []},
])
def test_malformed_maps_are_refused(data):
    with pytest.raises(ValueError):
        shard_plugin.validate_map(data)


def workflow_shards() -> list[str]:
    [line] = re.findall(r"^ +shard: \[(.*)\]$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    return re.findall(r'"([^"]*)"', line)


def test_the_workflow_matrix_names_every_shard_of_one_count_once():
    specs = [shard_plugin.parse_shard(text) for text in workflow_shards()]
    counts = {count for _, count in specs}
    assert len(counts) == 1, specs
    [count] = counts
    assert sorted(index for index, _ in specs) == list(range(count))
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SHARD: ${{ matrix.shard }}" in text
    assert 'ci_retry_known_flakes.py --shard "$SHARD"' in text


def test_the_checked_in_map_fits_the_workflow_limit():
    """Keep ten minutes of runner margin, and never project more than 35 minutes."""
    millis, groups = shard_plugin.validate_map(json.loads(MAP.read_text(encoding="utf-8")))
    count = len(workflow_shards())
    [limit] = re.findall(r"^    timeout-minutes: (\d+)$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    loads = shard_plugin.predicted_loads(millis, groups, count)
    assert all(load > 0 for load in loads), loads
    assert max(loads) <= min(35, int(limit) - 10) * 60_000, [round(load / 60_000, 1) for load in loads]


def test_review_probes_share_a_group_before_the_other_frontend_tests():
    data = json.loads(MAP.read_text(encoding="utf-8"))
    groups = data["groups"]
    prefix = "tests/frontend/test_review_r219_"
    # Each has a distinct cold source variant. Keeping either in the common
    # review group would hide its compile cost or exceed the prediction guard.
    separate = {prefix + "fix14_setup.py", prefix + "fix8_mutations.py"}
    assert all(groups.index(path) < groups.index(prefix) for path in separate)
    assert groups.index(prefix) < groups.index("tests/frontend/")
    millis, groups = shard_plugin.validate_map(data)
    shards = shard_plugin.plan(millis, groups, len(workflow_shards()))
    files = [path.relative_to(REPO).as_posix() for path in (REPO / "tests/frontend").glob("test_review_r219_*.py")]
    assert separate <= set(files)
    shared = set(files) - separate
    assert shared and all(shard_plugin.group_of(path, groups) == prefix for path in shared)
    assert all(shard_plugin.group_of(path, groups) == path and path in millis for path in separate)
    assert len({shard_plugin.shard_of(path, shards, groups, len(workflow_shards())) for path in shared}) == 1


def test_a_narrow_file_prefix_can_precede_its_directory_group():
    prefix = "tests/frontend/test_review_r219_"
    millis, groups = shard_plugin.validate_map({"groups": [prefix, "tests/frontend/"], "seconds": {
        prefix + "fix8_view.py": 900, prefix + "fix8_mutations.py": 600,
        "tests/frontend/test_other.py": 1200,
    }})
    assert shard_plugin.group_of(prefix + "unmeasured.py", groups) == prefix
    assert shard_plugin.predicted_loads(millis, groups, 2) == [1_500_000, 1_200_000]


# Real collections: a small project whose files each record that they were imported.
PROJECT_FILES = {
    "tests/a/test_slow.py": 900, "tests/a/test_quick.py": 10, "tests/b/test_mid.py": 300,
    "tests/b/test_other.py": 250, "tests/ui/test_view.py": 200, "tests/ui/test_model.py": 100,
    "tests/test_root.py": 50,
}
UNMEASURED = ["tests/b/test_new.py", "tests/ui/test_new_view.py", "tests/c/test_fresh.py"]


@pytest.fixture
def sharded_project(pytester, monkeypatch):
    pytester.makefile(".toml", pyproject="[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    for path in [*PROJECT_FILES, *UNMEASURED]:
        target = pytester.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        stem = path.replace("/", "_").removesuffix(".py")
        target.write_text(
            "from pathlib import Path\n"
            f"Path('imported.txt').open('a').write({path!r} + '\\n')\n"
            f"def test_{stem}_one(): pass\n"
            f"def test_{stem}_two(): pass\n", encoding="utf-8")
    (pytester.path / "tests/helper.py").write_text("raise AssertionError('not a test module')\n")
    (pytester.path / ".github").mkdir()
    (pytester.path / ".github/test-shards.json").write_text(json.dumps(
        {"groups": ["tests/ui/"], "seconds": PROJECT_FILES}), encoding="utf-8")
    monkeypatch.syspath_prepend(str(TOOLS))

    def collect(*args):
        (pytester.path / "imported.txt").unlink(missing_ok=True)
        result = pytester.runpytest_inprocess("--collect-only", "-q", "-p", "no:cacheprovider", *args)
        nodes = [line for line in result.outlines if "::" in line]
        imported_file = pytester.path / "imported.txt"
        imported = imported_file.read_text().split() if imported_file.exists() else []
        return result, nodes, imported
    return collect


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_real_shards_partition_the_collection(sharded_project, count):
    _, everything, _ = sharded_project()
    assert len(everything) == 2 * (len(PROJECT_FILES) + len(UNMEASURED))
    seen = []
    for index in range(1, count + 1):
        result, nodes, imported = sharded_project("-p", "ci_shard_plugin", "--ci-shard", f"{index}/{count}")
        assert result.ret in (0, 5), result.outlines
        files = {node.split("::")[0] for node in nodes}
        assert set(imported) == files, "a shard imported another shard's file"
        assert len({path for path in files if path.startswith("tests/ui/")}) in (0, 3)
        seen += nodes
    assert sorted(seen) == sorted(everything)


def test_a_shard_reports_its_unmeasured_files(sharded_project):
    seen = []
    for index in (1, 2):
        result, _, _ = sharded_project("-p", "ci_shard_plugin", "--ci-shard", f"{index}/2")
        summary = [line for line in result.outlines if line.startswith("ci shard ")]
        assert len(summary) == 1 and summary[0].startswith(f"ci shard {index}/2: "), result.outlines
        seen += [line.split("unmeasured: ")[1] for line in result.outlines if "unmeasured: " in line]
    # A grouped file is placed with its group, so it is not "unmeasured".
    assert sorted(seen) == ["tests/b/test_new.py", "tests/c/test_fresh.py"]


def test_without_the_option_the_plugin_selects_nothing(sharded_project):
    _, everything, _ = sharded_project()
    _, nodes, _ = sharded_project("-p", "ci_shard_plugin")
    assert nodes == everything


@pytest.mark.parametrize("args", [("--ci-shard", "3/2"), ("--ci-shard", "1/2", "--ci-shard-map", "missing.json")])
def test_a_bad_shard_or_map_is_a_usage_error(sharded_project, args):
    result, nodes, _ = sharded_project("-p", "ci_shard_plugin", *args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert nodes == []


# tools/ci_shard_durations.py


def write_report(path: Path, records: list[tuple[str, str, float]], exitstatus: int | None = 0,
                 collected: int | None = None) -> Path:
    """An outcomes report and its session record (none when exitstatus is None);
    by default the session collected exactly the tests that finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"nodeid": node, "when": when, "outcome": "passed", "xfail": False,
                                        "duration": duration}) + "\n" for node, when, duration in records))
    if exitstatus is not None:
        finished = sum(1 for _, when, _ in records if when == "teardown")
        path.with_suffix(".session.json").write_text(json.dumps(
            {"exitstatus": exitstatus, "collected": finished if collected is None else collected}))
    return path


def whole_test(node: str, seconds: float) -> list[tuple[str, str, float]]:
    return [(node, "setup", 0.0), (node, "call", seconds), (node, "teardown", 0.0)]


def test_file_seconds_sum_every_phase_of_a_file(tmp_path):
    report = write_report(tmp_path / "first.jsonl", [
        ("tests/test_a.py::test_x", "setup", 1.0), ("tests/test_a.py::test_x", "call", 2.5),
        ("tests/test_a.py::test_x", "teardown", 0.5), ("tests/test_a.py::test_y[p::q]", "call", 1.0),
        ("tests/b/test_b.py::C::test_z", "call", 4.0)])
    with report.open("a") as stream:   # a failed collection carries no duration
        stream.write(json.dumps({"nodeid": "tests/test_c.py", "when": "collect", "outcome": "failed",
                                 "xfail": False}) + "\n")
    assert durations_tool.file_seconds(report) == {"tests/test_a.py": 5.0, "tests/b/test_b.py": 4.0}


def test_a_test_phase_without_a_duration_is_refused(tmp_path):
    report = tmp_path / "first.jsonl"
    report.write_text(json.dumps({"nodeid": "tests/test_a.py::t", "when": "call", "outcome": "passed",
                                  "xfail": False}) + "\n")
    with pytest.raises(ValueError, match="duration"):
        durations_tool.file_seconds(report)


@given(st.lists(st.dictionaries(file_paths, st.floats(0, 1e4)), max_size=4),
       st.dictionaries(file_paths, st.floats(0, 1e4)), st.sets(file_paths))
def test_merge_takes_the_slowest_report_and_keeps_only_existing_unmeasured_files(reports, old, existing):
    seconds, kept = durations_tool.merge(reports, old, existing.__contains__)
    measured = {path for totals in reports for path in totals}
    assert set(seconds) == measured | (set(old) & existing)
    assert set(kept) == (set(old) & existing) - measured
    for path in measured:
        assert seconds[path] == round(max(totals.get(path, 0.0) for totals in reports), 1)
    for path in kept:
        assert seconds[path] == round(old[path], 1)


def test_the_durations_tool_rewrites_only_seconds_and_source(tmp_path, capsys):
    root = tmp_path / "checkout"
    for path in ("tests/test_a.py", "tests/test_kept.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("")
    (root / ".github").mkdir()
    map_path = root / ".github/test-shards.json"
    original = {"comment": "kept", "groups": ["tests/ui/"],
                "seconds": {"tests/test_a.py": 1.0, "tests/test_kept.py": 7.0, "tests/test_gone.py": 3.0}}
    map_path.write_text(json.dumps(original))
    write_report(tmp_path / "art/py312-shard-1/first.jsonl", whole_test("tests/test_a.py::t", 10.0))
    write_report(tmp_path / "art/py314-shard-1/first.jsonl", whole_test("tests/test_a.py::t", 12.04))

    assert durations_tool.main([str(tmp_path / "art"), "--root", str(root), "--shards", "2", "--dry-run"]) == 0
    assert json.loads(map_path.read_text()) == original
    assert durations_tool.main([str(tmp_path / "art"), "--root", str(root), "--source", "run 1"]) == 0
    assert json.loads(map_path.read_text()) == {
        "comment": "kept", "groups": ["tests/ui/"], "source": "run 1",
        "seconds": {"tests/test_a.py": 12.0, "tests/test_kept.py": 7.0}}
    assert "kept: tests/test_kept.py" in capsys.readouterr().out


def checkout_with_map(tmp_path: Path, seconds: dict[str, float]) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    for path in seconds:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("")
    (root / ".github").mkdir()
    map_path = root / ".github/test-shards.json"
    map_path.write_text(json.dumps({"groups": [], "seconds": seconds}))
    return root, map_path


@pytest.mark.parametrize("exitstatus, collected, reason", [
    (None, None, "no session record"),                  # killed before the session ended
    (2, None, "exit 2"),                                # interrupted (a cancelled job)
    (1, 3, "1 of 3 collected tests finished"),          # stopped early, e.g. --maxfail
])
def test_a_partial_report_never_replaces_a_files_time(tmp_path, capsys, exitstatus, collected, reason):
    """Review r1 P3: a cancelled shard's report held only part of a file's time,
    and regeneration replaced the file's full time with it."""
    root, map_path = checkout_with_map(tmp_path, {"tests/test_long.py": 900.0, "tests/test_b.py": 5.0})
    write_report(tmp_path / "art/cancelled/first.jsonl",
                 [*whole_test("tests/test_long.py::test_one", 0.1), ("tests/test_long.py::test_two", "setup", 0.1)],
                 exitstatus, collected)
    write_report(tmp_path / "art/whole/first.jsonl", whole_test("tests/test_b.py::t", 6.0))
    assert durations_tool.main([str(tmp_path / "art"), "--root", str(root), "--shards", "1"]) == 0
    assert json.loads(map_path.read_text())["seconds"] == {"tests/test_b.py": 6.0, "tests/test_long.py": 900.0}
    out = capsys.readouterr().out
    assert "skipped" in out and reason in out and "1 reports (1 skipped)" in out


def test_a_whole_session_with_failures_still_counts(tmp_path):
    root, map_path = checkout_with_map(tmp_path, {"tests/test_long.py": 900.0})
    write_report(tmp_path / "first.jsonl", whole_test("tests/test_long.py::t", 30.0), exitstatus=1)
    assert durations_tool.main([str(tmp_path / "first.jsonl"), "--root", str(root)]) == 0
    assert json.loads(map_path.read_text())["seconds"] == {"tests/test_long.py": 30.0}


def test_no_whole_report_is_an_error_and_writes_nothing(tmp_path):
    root, map_path = checkout_with_map(tmp_path, {"tests/test_long.py": 900.0})
    before = map_path.read_text()
    write_report(tmp_path / "first.jsonl", whole_test("tests/test_long.py::t", 0.1), exitstatus=2)
    with pytest.raises(SystemExit) as stopped:
        durations_tool.main([str(tmp_path / "first.jsonl"), "--root", str(root)])
    assert stopped.value.code == 2
    assert map_path.read_text() == before
