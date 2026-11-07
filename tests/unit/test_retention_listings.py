"""Retention's holder listings, counted and logged (D-HOLDERS-LSOF1, design 8 and 12).

Each listing lists every process on the machine, so a pass's listings are what its
holder checks cost. Every listing writes exactly one line (which check it served,
how many jobs, the reader, the seconds, ok or failed with the reason), and the
pass's result counts its listings (`listings`) and their seconds (`listing_s`).
"""
from __future__ import annotations

import logging
import re

import pytest

from subfleet import retention
from subfleet.retention_holders import Busy, ScanFailed
from tests.unit.retention_world import Clock, World, trust_temporary_directories

LINE = re.compile(r"retention: holder listing check=(first|second|both) jobs=(\d+) reader=(\S+) "
                  r"seconds=(\d+\.\d\d) (ok(?: \S+=\S+)*|failed: .*)$")


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


class Reader:
    """A stand-in reader that answers from a script and remembers each call."""
    reader = "scripted"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, watches, **_):
        self.calls.append({job: bool(watch.inodes) for job, watch in watches.items()})
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer


def listing_lines(caplog) -> list[re.Match]:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("retention: holder listing")]
    matches = [LINE.match(line) for line in lines]
    assert all(matches), lines
    return matches


def run(w: World, log: logging.Logger, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    return retention.maintenance(w.store, w.root, log=log, **kwargs)


def test_every_listing_writes_one_line_and_the_result_counts_them(world, caplog):
    """Invariant 6: one line per listing, with its check, jobs, reader, seconds and
    outcome; the result's `listings` equals the lines and `listing_s` their sum."""
    w = world
    w.job("job-a", created="2026-09-01T00:00:00Z")
    w.job("job-b", created="2026-09-01T00:01:00Z")
    log = logging.getLogger("test.retention.listings")
    reader = Reader(Busy({}, {"processes": 7}))
    results = []
    with caplog.at_level(logging.INFO, logger=log.name):
        for _ in range(3):
            results.append(run(w, log, holders=reader, clock=Clock()))
    lines = listing_lines(caplog)
    assert sum(r["listings"] for r in results) == len(lines) == len(reader.calls) > 0
    for match, call in zip(lines, reader.calls):
        assert match.group(3) == "scripted" and match.group(5).startswith("ok")
        assert int(match.group(2)) == len(call)
    assert lines[0].group(5) == "ok processes=7"          # what the reader asked about, counts only
    total = sum(float(m.group(4)) for m in lines)
    assert abs(sum(r["listing_s"] for r in results) - total) < 0.05 * len(lines) + 0.01
    assert all(isinstance(r["listing_s"], float) for r in results)


def test_a_failed_listing_writes_one_line_with_its_reason(world, caplog):
    w = world
    w.job("job-scan")
    log = logging.getLogger("test.retention.listings.failed")
    with caplog.at_level(logging.INFO, logger=log.name):
        result = run(w, log, holders=Reader(ScanFailed("lsof exited 1: no\nmemory")))
    (line,) = listing_lines(caplog)
    assert line.group(5) == "failed: lsof exited 1: no memory"
    assert [r.levelno for r in caplog.records if r.getMessage().startswith("retention: holder listing")] \
        == [logging.WARNING]
    assert result["listings"] == 1 and result["holder_scan_failed"]
    assert "holder scan failed" in result["deferred"]["job-scan"]


def test_no_job_needs_a_check_no_listing(world, caplog):
    """Invariant 5's second half: nothing to check, nothing listed or logged."""
    w = world
    w.job("job-kept")
    log = logging.getLogger("test.retention.listings.none")
    reader = Reader()
    with caplog.at_level(logging.INFO, logger=log.name):
        result = run(w, log, holders=reader, max_jobs=10, max_bytes=10 ** 12)
    assert reader.calls == [] and result["listings"] == 0 and result["listing_s"] == 0.0
    assert listing_lines(caplog) == []


def test_the_daemons_pass_logs_to_daemon_log(monkeypatch):
    """The daemon hands its own logger to the pass, so the lines land in daemon.log."""
    import inspect
    from subfleet import daemon as daemon_module
    source = inspect.getsource(daemon_module.Daemon._retention)
    assert "log=self.log" in source
