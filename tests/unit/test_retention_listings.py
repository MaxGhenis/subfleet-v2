"""Retention's holder listings, counted, logged and one a pass (D-HOLDERS-LSOF1,
design 8 and 12).

Each listing lists every process on the machine, so a pass's listings are what its
holder checks cost. Every listing writes exactly one line (which check it served,
how many jobs, the reader, the seconds, ok or failed with the reason), and the
pass's result counts its listings (`listings`) and their seconds (`listing_s`).

A pass takes one listing, after its quarantines: check 1 for the jobs quarantined
before it, check 2 for the jobs archived before it. A job archived in a pass waits
for the next pass's listing. A listing serves a check only if it started after the
check's event, by stamps persisted in the journal. The order of those events, over
drawn runs of passes with restarts, is the property test's
(`test_retention_listings_properties.py`).
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet.retention_holders import Busy, ScanFailed, Watch, parse
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


# --- one listing a pass (change 2) ------------------------------------------------------


def journal(w: World, job_id: str) -> dict | None:
    path = rarch.journal_path(w.root, job_id)
    return json.loads(path.read_text()) if path.exists() else None


def test_one_listing_a_pass_serves_both_checks(world, caplog):
    """Invariant 5: a pass lists once. With a batch of one, the first pass's listing
    serves a's check 1; the second's serves a's check 2 and b's check 1 (`both`), and
    a commits; the third serves b's check 2."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    log = logging.getLogger("test.retention.listings.both")
    reader, state = Reader(), retention.RetentionState()
    with caplog.at_level(logging.INFO, logger=log.name):
        results = [run(w, log, holders=reader, batch=1, state=state) for _ in range(3)]
    assert [r["listings"] for r in results] == [1, 1, 1]
    assert [m.group(1) for m in listing_lines(caplog)] == ["first", "both", "second"]
    assert [sorted(call) for call in reader.calls] == [["job-a"], ["job-a", "job-b"], ["job-b"]]
    assert [sorted(r["listing"]["second"]) for r in results] == [[], ["job-a"], ["job-b"]]
    assert [sorted(r["listing"]["first"]) for r in results] == [["job-a"], ["job-b"], []]
    assert [r["pruned"] for r in results] == [[], ["job-a"], ["job-b"]]
    assert [r["awaiting_check2"] for r in results] == [["job-a"], ["job-b"], []]


def test_a_job_archived_in_a_pass_waits_for_the_next_listing(world):
    """A job archived in a pass takes check 2 from the next pass's listing, not the
    one before its archive. While it waits, the pass reports more work and progress,
    which keeps the daemon's next pass at `RETENTION_CATCH_UP_S` (daemon.py's
    `_retention`; test_review130_ab's churn test sees the 5 s)."""
    w = world
    w.job("job-a", worktree=False)
    reader, state = Reader(), retention.RetentionState()
    log = logging.getLogger("test.retention.listings.wait")
    first = run(w, log, holders=reader, state=state)
    assert first["pruned"] == [] and first["awaiting_check2"] == ["job-a"] and first["listings"] == 1
    assert first["more"] is True and first["progressed"] is True and "job-a" in first["in_flight"]
    assert journal(w, "job-a")["state"] == "archived" and w.store.get_job("job-a") is not None
    second = run(w, log, holders=reader, state=state)
    assert second["pruned"] == ["job-a"] and second["awaiting_check2"] == [] and second["listings"] == 1
    assert list(second["listing"]["second"]) == ["job-a"] and w.store.get_job("job-a") is None
    third = run(w, log, holders=reader, state=state)
    assert third["listings"] == 0 and third["more"] is False and len(reader.calls) == 2


def test_a_listing_serves_a_check_only_after_its_event(world):
    """Invariant 3: the listing's stamp, taken before its cwd scan, follows the
    persisted stamp of every check it serves (the quarantine for check 1, the end of
    the archive for check 2), read from the journals as the reader runs."""
    w = world
    for name in ("job-a", "job-b", "job-c"):
        w.job(name, worktree=False)
    state, seen = retention.RetentionState(), []
    log = logging.getLogger("test.retention.listings.order")

    def reader(watches, **_):
        seen.append({job: journal(w, job) for job in watches})
        return {}
    served = 0
    for _ in range(4):
        result = run(w, log, holders=reader, batch=2, state=state)
        listing = result["listing"]
        if listing is None:
            continue
        served += 1
        for job, at_listing in seen[-1].items():
            check, key = (("second", "archived_stamp") if at_listing["state"] == "archived"
                          else ("first", "quarantined_stamp"))
            assert at_listing["state"] in ("archived", "quarantined")
            assert job in listing[check] and listing[check][job] == at_listing[key] < listing["stamp"]
    assert served == len(seen) == 3


def test_a_restart_with_the_clock_stepped_back_still_serves_the_waiting_job(world):
    """Stamps live in the journal, and the next stamp follows every stamp a journal
    holds: after a restart (a fresh state) with the clock stepped back, the listing
    still starts after the archive, and the job takes its check 2 and commits."""
    w = world
    w.job("job-a", worktree=False)
    log = logging.getLogger("test.retention.listings.restart")
    reader, late = Reader(), 5 * 10 ** 18
    first = run(w, log, holders=reader, state=retention.RetentionState(), stamp=lambda: late)
    archived = journal(w, "job-a")["archived_stamp"]
    assert first["awaiting_check2"] == ["job-a"] and archived > late
    second = run(w, log, holders=reader, state=retention.RetentionState(), stamp=lambda: 1)
    assert second["listing"]["stamp"] > archived and second["listing"]["second"] == {"job-a": archived}
    assert second["pruned"] == ["job-a"]


def test_a_reader_failure_makes_every_job_of_the_listing_busy(world):
    """Invariant 1: a failed listing serves no check. Every job it was for, at check
    1 or check 2, is put back (its folder restored) and deferred, and none commits."""
    w = world
    for name in ("job-a", "job-b"):
        w.job(name, worktree=False)
    state = retention.RetentionState()
    log = logging.getLogger("test.retention.listings.fail")
    assert run(w, log, holders=Reader(), batch=1, state=state)["awaiting_check2"] == ["job-a"]
    result = run(w, log, holders=Reader(ScanFailed("lsof exited 1: fixture")), batch=1, state=state)
    assert result["holder_scan_failed"] and result["pruned"] == [] and result["listings"] == 1
    assert sorted(result["listing"]["first"]) == ["job-b"] and sorted(result["listing"]["second"]) == ["job-a"]
    for name in ("job-a", "job-b"):
        assert "holder scan failed" in result["deferred"][name]
        assert (w.root / "jobs" / name).is_dir() and w.store.get_job(name) is not None
        assert (journal(w, name) or {}).get("state") not in ("quarantined", "archived")


def test_parse_holds_mapped_files_and_writers_but_not_readers(tmp_path):
    """The per-process rule a narrower reader must match (invariant 2's reference):
    a cwd, a mapped file, a directory descriptor or a descriptor open for writing
    holds a tree; a descriptor open only for reading does not."""
    def proc(pid, command, fd, kind, name, access=""):
        return [f"p{pid}", f"c{command}", f"f{fd}", *([f"a{access}"] if access else []), f"t{kind}", f"n{name}"]
    out = [*proc(101, "reader", 5, "REG", tmp_path / "log", "r"),
           *proc(102, "mapper", "txt", "REG", tmp_path / "lib.dylib"),
           *proc(103, "writer", 7, "REG", tmp_path / "out", "w"),
           *proc(104, "lister", 3, "DIR", tmp_path, "r"),
           *proc(105, "shell", "cwd", "DIR", tmp_path),
           *proc(106, "elsewhere", 4, "REG", "/elsewhere/file", "w")]
    busy = parse("\n".join(out) + "\n", {"job": Watch([str(tmp_path)])})
    assert [reason.split(":")[0] for reason in busy["job"]] == [
        "pid 102 (mapper)", "pid 103 (writer)", "pid 104 (lister)", "pid 105 (shell)"]
