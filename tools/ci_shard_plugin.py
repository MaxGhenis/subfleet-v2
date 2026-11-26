"""Run one CI shard of the suite: ``pytest -p ci_shard_plugin --ci-shard I/N``.

Each test file belongs to exactly one of N shards, so N jobs with I = 1..N run
every test once, serially, as one job did before (D-CI2). The shard is decided
per file before pytest imports it (``pytest_ignore_collect``), so a shard only
collects its own files. pytest asks that hook only about files it discovers
(``testpaths``), so give a shard run no path arguments: a file named on the
command line always runs.

Where a file goes is a pure function of its path and the checked-in map
(``.github/test-shards.json``): every shard job of a run computes the same
answer from the same checkout, which is what makes the shards a partition.
- Files matching the first ``groups`` prefix move together, so session fixtures such as
  the frontend's compiled Swift probes are built once, not once per shard.
- Every other measured file is its own unit. Units are placed longest first
  on the least-loaded shard (ties: the lower shard), from ``seconds``, the
  measured CI time per file.
- A file the map has not measured yet goes to ``crc32(path) % N``.
Regenerate ``seconds`` from a run's outcome artifacts with
``tools/ci_shard_durations.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Mapping, Sequence
import zlib

import pytest
from _pytest.python import path_matches_patterns

DEFAULT_MAP = ".github/test-shards.json"
MAX_SHARDS = 64
_SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


def parse_shard(text: str) -> tuple[int, int]:
    """'I/N' -> (I - 1, N): a 0-based shard index and the shard count."""
    match = _SHARD.fullmatch(text)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= MAX_SHARDS:
        raise ValueError(f"--ci-shard expects I/N with 1 <= I <= N <= {MAX_SHARDS}, got {text!r}")
    return int(match[1]) - 1, int(match[2])


def group_of(path: str, groups: Sequence[str]) -> str | None:
    return next((group for group in groups if path.startswith(group)), None)


def validate_map(data: object) -> tuple[dict[str, int], list[str]]:
    """The map's measured milliseconds per file and its groups; anything malformed raises."""
    if not isinstance(data, dict) or not isinstance(data.get("seconds"), dict) \
            or not isinstance(data.get("groups"), list):
        raise ValueError("expected an object with 'seconds' (object) and 'groups' (list)")
    groups = data["groups"]
    for index, group in enumerate(groups):
        if (not isinstance(group, str) or not group or group.startswith("/")
                or ".." in group.split("/") or "*" in group
                or not (group.endswith("/") or group.rsplit("/", 1)[-1].startswith("test_"))):
            raise ValueError(f"group {group!r} must be a relative directory or test-file prefix")
        # First-prefix matching permits a narrow test family before its directory.
        # A later prefix already swallowed by an earlier one can never be selected.
        if any(group.startswith(earlier) for earlier in groups[:index]):
            raise ValueError(f"group {group!r} is hidden by an earlier group")
    millis = {}
    for path, seconds in data["seconds"].items():
        if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                or not seconds >= 0 or seconds == float("inf")):
            raise ValueError(f"{path}: seconds must be a finite number >= 0")
        millis[path] = round(seconds * 1000)
    return millis, list(groups)


def plan(millis: Mapping[str, int], groups: Sequence[str], count: int) -> dict[str, int]:
    """Each unit's shard (0-based): longest unit first onto the least-loaded shard."""
    weights = {group: 0 for group in groups}
    for path, cost in millis.items():
        unit = group_of(path, groups) or path
        weights[unit] = weights.get(unit, 0) + cost
    loads = [0] * count
    shards = {}
    for unit in sorted(weights, key=lambda unit: (-weights[unit], unit)):
        shard = min(range(count), key=lambda index: (loads[index], index))
        shards[unit] = shard
        loads[shard] += weights[unit]
    return shards


def shard_of(path: str, shards: Mapping[str, int], groups: Sequence[str], count: int) -> int:
    unit = group_of(path, groups) or path
    if unit in shards:
        return shards[unit]
    return zlib.crc32(path.encode("utf-8")) % count


def predicted_loads(millis: Mapping[str, int], groups: Sequence[str], count: int) -> list[int]:
    shards = plan(millis, groups, count)
    loads = [0] * count
    for path, cost in millis.items():
        loads[shard_of(path, shards, groups, count)] += cost
    return loads


class _Shard:
    def __init__(self, config: pytest.Config, spec: str, map_path: Path):
        try:
            self.index, self.count = parse_shard(spec)
            self.millis, self.groups = validate_map(json.loads(map_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise pytest.UsageError(f"ci shard map {map_path}: {error}") from error
        self.spec, self.map_path, self.root = spec, map_path, config.rootpath
        self.patterns = config.getini("python_files")
        self.shards = plan(self.millis, self.groups, self.count)
        self.kept: list[str] = []
        self.unmeasured: list[str] = []

    def key(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def ignore(self, path: Path) -> bool | None:
        if not path_matches_patterns(path, self.patterns):
            return None                       # conftest.py, helpers, directories: pytest decides
        key = self.key(path)
        if shard_of(key, self.shards, self.groups, self.count) != self.index:
            return True
        self.kept.append(key)
        if group_of(key, self.groups) is None and key not in self.millis:
            self.unmeasured.append(key)
        return None


_KEY = pytest.StashKey[_Shard]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci shard")
    group.addoption("--ci-shard", default=None, metavar="I/N",
                    help="run only shard I of N (test files split by .github/test-shards.json)")
    group.addoption("--ci-shard-map", default=None, metavar="PATH",
                    help=f"shard map (default: <rootdir>/{DEFAULT_MAP})")


def pytest_configure(config: pytest.Config) -> None:
    spec = config.getoption("ci_shard")
    if spec is None:
        return
    map_option = config.getoption("ci_shard_map")
    map_path = Path(map_option) if map_option else config.rootpath / DEFAULT_MAP
    config.stash[_KEY] = _Shard(config, spec, map_path)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    shard = config.stash.get(_KEY, None)
    if shard is None or not collection_path.is_file():
        return None
    return shard.ignore(collection_path)


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    shard = config.stash.get(_KEY, None)
    if shard is None:
        return
    loads = predicted_loads(shard.millis, shard.groups, shard.count)
    terminalreporter.write_line(
        f"ci shard {shard.spec}: {len(shard.kept)} test files, map predicted "
        f"{loads[shard.index] / 60000:.1f} of {sum(loads) / 60000:.1f} min; "
        f"{len(shard.unmeasured)} files not yet measured in {shard.map_path.name}")
    for path in shard.unmeasured:
        terminalreporter.write_line(f"  unmeasured: {path}")
