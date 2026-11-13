"""Bounded request persistence/restart counts on data and temporary fixtures.

No live paths, HOME changes, control commands or executed destructive-shaped
guard probes. Functional DeletePath calls target only freshly created Worlds.
"""
from __future__ import annotations

import errno
import json

import pytest

from subfleet import retention_delete as rd
from subfleet import retention_marks as marks
from tests.unit.retention_delete_world import STEP, delete_path, finish, nested_job, request, results
from tests.unit.retention_world import World, trust_temporary_directories


class CapturedStore:
    def __init__(self):
        self.events = {}

    def one(self, sql, params):
        assert "FROM events" in sql and params[0] == rd.EVENT
        event = self.events.get(params[1])
        return {"event_id": 1, "data_json": event} if event is not None else None


@pytest.fixture
def data_path(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    store = CapturedStore()
    return rd.DeletePath(store, root), store


def record_file(path):
    return path.base / rd.RECORD


def marker(path, store, req, job_id, *, event=True):
    facts = {"schema": rd.SCHEMA, "job_id": job_id, "ruling": req.ruling, "request_digest": req.digest,
             "class": "nested-linked-worktree", "worktree": str(path.root / "worktrees" / job_id),
             "started_at": "2026-11-12T01:00:00Z", "ended_at": "2026-11-12T01:01:00Z",
             "logical_bytes": 100, "allocated_bytes": 80, "entries": 3, "free_before": 1000, "free_after": 1080,
             "refs": [{"ref": "refs/subfleet-kept/nonexistent-job/0/0"}],
             "bundles": [{"bytes": 23}], "admins_removed": [], "admins_kept": []}
    target = marks.marker_path(path.root, job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(rd._canonical(facts))
    if event:
        store.events[job_id] = json.dumps(facts)
    return facts


def test_accumulated_results_are_compacted_on_disk_and_keep_totals(data_path):
    path, store = data_path
    jobs = [f"nonexistent-large-{n}" for n in range(100)]
    path.start(request(*jobs), mode="apply")
    for n, job_id in enumerate(jobs):
        path.apply["results"][job_id] = {"job_id": job_id, "run": 1, "disposition": "deleted", "gate": None,
            "reason": None, "started_at": "start", "ended_at": "end", "bytes": {"logical_bytes": n,
            "allocated_bytes": n * 2}, "roots": ["fixture-detail" * 16000], "notes": ["note"],
            "keep": {"refs": [{"ref": "first"}, {"ref": "second"}], "bundles": [{"bytes": 37}]},
            "admins": {"kept": ["fixture"]}}
    before = path.status()["apply"]["totals"]
    assert len(rd._canonical(path.apply)) > rd.APPLY_RECORD_LIMIT
    path._save()
    raw = record_file(path).read_bytes()
    assert len(raw) <= rd.APPLY_RECORD_LIMIT == 16 << 20
    disk = json.loads(raw)
    assert disk["details_compacted"] and len(disk["results"]) == 100
    assert "roots" in path.apply["results"][jobs[0]] and "details_compacted" not in path.apply
    line = disk["results"][jobs[0]]
    assert line["disposition"] == "deleted" and line["run"] == 1 and line["started_at"] == "start"
    assert line["marker"] == str(marks.marker_path(path.root, jobs[0])) and line["details_compacted"]
    assert "keep" not in line and "roots" not in line
    again = rd.DeletePath(store, path.root)
    assert again.status()["apply"]["totals"] == before
    assert again.apply["deleted_this_run"] == 100


def test_rollback_notes_are_counted_without_persisting_their_bulk(data_path, monkeypatch):
    path, store = data_path
    jid = "nonexistent-notes"
    path.start(request(jid), mode="apply")
    path.apply["results"][jid] = {"job_id": jid, "run": 1, "disposition": "interrupted", "gate": None,
        "reason": "the tree is whole", "rollback_notes": ["fixture" * 20000], "bytes": {"entries": 1}}
    monkeypatch.setattr(rd, "APPLY_RECORD_LIMIT", 2000)
    path._save()
    saved = json.loads(record_file(path).read_bytes())["results"][jid]
    assert saved["rollback_notes_count"] == 1 and "rollback_notes" not in saved
    assert saved["reason"] == "the tree is whole" and saved["bytes"] == {"entries": 1}


def test_core_overflow_preserves_previous_valid_disk_record_and_live_details(data_path, monkeypatch):
    path, store = data_path
    jid = "nonexistent-core-overflow"
    path.start(request(jid), mode="apply")
    previous = record_file(path).read_bytes()
    path.apply["results"][jid] = {"job_id": jid, "run": 1, "disposition": "failed", "gate": "keep",
                                  "reason": "essential reason " * 1000, "roots": ["fixture"]}
    monkeypatch.setattr(rd, "APPLY_RECORD_LIMIT", 2000)
    with pytest.raises(OSError) as caught:
        path._save()
    assert caught.value.errno == errno.EFBIG and record_file(path).read_bytes() == previous
    assert path.apply["results"][jid]["roots"] == ["fixture"] and "details_compacted" not in path.apply


@pytest.mark.parametrize("same_request", [False, True])
def test_rejected_start_preserves_previous_memory_and_disk(data_path, monkeypatch, same_request):
    path, store = data_path
    req = request("nonexistent-prior")
    path.start(req, mode="apply")
    path._finish(path.apply, "stopped")
    previous = path.apply
    content = rd._canonical(previous)
    saved = record_file(path).read_bytes()

    def fail_write(*args, **kwargs):
        raise OSError(errno.ENOSPC, "fixture write refusal")

    monkeypatch.setattr(rd.rfs, "write_atomic", fail_write)
    with pytest.raises(OSError, match="fixture write refusal"):
        path.start(req if same_request else request("nonexistent-next"), mode="apply")
    assert path.apply is previous and rd._canonical(previous) == content
    assert record_file(path).read_bytes() == saved and path._active() is None


def test_only_old_run_history_is_trimmed_and_omission_is_explicit(data_path, monkeypatch):
    path, store = data_path
    path.start(request("nonexistent-history"), mode="apply")
    path.apply["runs"] = [{"run": n, "status": "stopped", "started_at": "s" * 30,
                          "ended_at": "e" * 30, "limit": 1, "deleted": 0} for n in range(2000)]
    path.apply["runs_omitted"] = 7
    monkeypatch.setattr(rd, "APPLY_RECORD_LIMIT", 10_000)
    path._save()
    disk = json.loads(record_file(path).read_bytes())
    assert disk["runs"] == path.apply["runs"][-32:] and disk["runs_omitted"] == 1975
    assert len(path.apply["runs"]) == 2000 and path.apply["runs_omitted"] == 7


def test_new_matching_marker_event_is_counted_once_before_limit_selection(data_path):
    path, store = data_path
    req = request("nonexistent-first", "nonexistent-next")
    path.start(req, mode="apply", limit=1)
    marker(path, store, req, req.jobs[0])
    again = rd.DeletePath(store, path.root)
    assert again.apply["deleted_this_run"] == 1 and again._limit_reached(again.apply)
    assert again.apply["results"][req.jobs[0]]["disposition"] == "deleted"
    assert again.status()["apply"]["totals"]["bundle_bytes"] == 23
    again._save()
    third = rd.DeletePath(store, path.root)
    assert third.apply["deleted_this_run"] == 1
    third._resumed(req.jobs[0], {"disposition": "deleted"}, digest=req.digest)
    assert third.apply["deleted_this_run"] == 1


def test_preexisting_markers_and_old_matching_journals_do_not_count_in_a_new_run(data_path):
    path, store = data_path
    req = request("nonexistent-marked", "nonexistent-journal", "nonexistent-next")
    marker(path, store, req, req.jobs[0])
    journal = path._journal_dir(req.jobs[1]) / marks.JOURNAL
    journal.parent.mkdir(parents=True)
    journal.write_text("{}")
    path.start(req, mode="apply", limit=1)
    assert set(path.apply["deletions_before_run"]) == set(req.jobs[:2])
    again = rd.DeletePath(store, path.root)
    assert again.apply["deleted_this_run"] == 0 and not again._limit_reached(again.apply)
    again._resumed(req.jobs[1], {"job_id": req.jobs[1], "run": 1, "disposition": "deleted"}, digest=req.digest)
    assert again.apply["deleted_this_run"] == 0
    assert rd.DeletePath(store, path.root).apply["deleted_this_run"] == 0


def test_other_request_evidence_never_counts_or_overwrites_current_results(data_path):
    path, store = data_path
    req = request("nonexistent-other-request")
    path.start(req, mode="apply", limit=1)
    other = request(*req.jobs, ruling="different fixture ruling")
    marker(path, store, other, req.jobs[0])
    again = rd.DeletePath(store, path.root)
    again._resumed(req.jobs[0], {"disposition": "deleted"}, digest=other.digest)
    assert again.apply["deleted_this_run"] == 0 and again.apply["results"][req.jobs[0]] is None


@pytest.mark.parametrize("legacy,alter_event", [(True, False), (False, True), (False, False)])
def test_unprovable_active_restart_fails_closed(data_path, legacy, alter_event):
    path, store = data_path
    req = request("nonexistent-unprovable")
    path.start(req, mode="apply", limit=1)
    if legacy:
        del path.apply["deletions_before_run"]
        path._save()
    else:
        facts = marker(path, store, req, req.jobs[0], event=alter_event)
        if alter_event:
            store.events[req.jobs[0]] = json.dumps({**facts, "ended_at": "different"})
    again = rd.DeletePath(store, path.root)
    assert again.apply["status"] == "failed" and "reconciliation refused" in again.apply["error"]
    assert again._active() is None


class Killed(BaseException):
    pass


@pytest.mark.parametrize("seam", ["after-drop", "marker-written"])
def test_canary_crash_keeps_the_next_fixture_tree(seam, tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    try:
        jobs = ("nonexistent-canary-first", "nonexistent-canary-next")
        first, second = (nested_job(w, job_id) for job_id in jobs)
        path = delete_path(w)
        path.start(request(*jobs), mode="apply", limit=1)
        if seam == "after-drop":
            original = path._drop

            def drop_then_crash(journal):
                original(journal)
                raise Killed()

            path._drop = drop_then_crash
        else:
            def kill_at_marker(job, point):
                if point == seam:
                    raise Killed()
            path.on_step = kill_at_marker
        with pytest.raises(Killed):
            path.step(**STEP)
        assert not first.exists() and second.exists()
        again = delete_path(w)
        record = finish(again)
        assert record["status"] == "done-limit" and record["deleted_this_run"] == 1
        assert results(record)[jobs[0]]["disposition"] == "deleted" and second.exists()
        assert results(record).get(jobs[1]) is None
    finally:
        w.close()
