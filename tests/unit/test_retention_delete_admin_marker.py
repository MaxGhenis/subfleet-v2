"""Disposable fixture regressions for admin identity and marker-only restart.

Synthetic marker refs are test data, not claims about real preserved refs.
No command-guard probes or live stores are used; HOME/CODEX_HOME stay intact.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from subfleet import retention_delete as rd, retention_git as rgit, retention_marks as marks
from tests.unit.retention_delete_world import delete_path
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-admin-marker-temp"})
    fixture = World(tmp_path)
    yield fixture
    fixture.close()


@pytest.mark.parametrize("replace", [False, True], ids=["same-directory", "replacement-directory"])
def test_admin_cleanup_is_bound_to_the_inspected_directory(world, monkeypatch, replace):
    w = world
    job_id = "nonexistent-admin-identity"
    tree = w.job(job_id)
    assert tree is not None
    tree = tree.resolve()
    common = (w.repo / ".git").resolve()
    admin = w.admin(job_id).resolve()
    # Keep the fixture's source tree whole elsewhere; only its registration is
    # under test, in the phase after the owned tree would already be removed.
    tree.rename(w.base / "preserved-fixture-tree")
    old_identity = (admin.stat().st_dev, admin.stat().st_ino)
    preserved = w.base / "preserved-fixture-admin"
    ref = f"refs/subfleet-kept/{job_id}/0/0"
    git(w.repo, "update-ref", ref, w.head())
    journal = {
        "job_id": job_id, "worktree": str(tree),
        "admins": [{"admin": str(admin), "common": str(common), "n": 0, "rel": ".git"}],
        "plan": {"outside": [{"n": 0, "fmt": "sha1"}], "borrower_repositories": [str(common)]},
        "kept": {"refs": [{"repository": str(common), "n": 0, "ref": ref, "commit": w.head()}]},
    }
    read_commits = rd.admin_commits
    observed = []

    def after_commit_read(path, fmt, cancel=None):
        commits = read_commits(path, fmt, cancel=cancel)
        observed.append(str(path))
        if replace:
            admin.rename(preserved)
            admin.mkdir()
            (admin / "unrelated.txt").write_text("replacement data must survive\n")
        return commits

    monkeypatch.setattr(rd, "admin_commits", after_commit_read)
    path = delete_path(w)
    with rgit.command_guard(rd.GitReadGuard(rd.Excluder(())), env=rd.ISOLATED_CONFIG, cwd=path.root):
        report = path._remove_admins(journal)
    assert observed == [str(admin)]
    if replace:
        assert (admin.stat().st_dev, admin.stat().st_ino) != old_identity
        assert (admin / "unrelated.txt").read_text() == "replacement data must survive\n"
        assert preserved.is_dir() and report["removed"] == []
        assert report["kept"] == [{"admin": str(admin), "why": "its directory changed after it was inspected"}]
    else:
        assert not admin.exists() and report["kept"] == []
        assert report["removed"] == [str(admin)]


def test_a_large_marker_survives_a_crash_before_its_event(world):
    w = world
    job_id = "nonexistent-large-marker-restart"
    w.job(job_id, worktree=False)
    now = [datetime(2026, 11, 12, tzinfo=UTC)]

    class Interrupted(BaseException):
        pass

    def stop_after_marker(job, point):
        if job == job_id and point == "marker-written":
            raise Interrupted()

    path = delete_path(w, now=lambda: now[0], on_step=stop_after_marker)
    path._ensure_base()
    path._journal_dir(job_id).mkdir()
    # Phase-only synthetic evidence: many ref records make the exact payload
    # written by _marker exceed the former reader limit, without creating
    # thousands of real refs or performing a tree deletion.
    refs = [{"repository": str(w.repo / ".git"), "n": i,
             "ref": f"refs/subfleet-kept/{job_id}/{i}/0", "commit": "a" * 40}
            for i in range(8192)]
    journal = {"schema": rd.SCHEMA, "job_id": job_id, "ruling": "d-test:marker-recovery",
               "digest": "synthetic-marker-fixture", "worktree": str(w.root / "worktrees" / job_id),
               "started_at": "2026-11-12T00:00:00Z", "tombstone": str(path._journal_dir(job_id) / rd.TOMBSTONE),
               "kept": {"refs": refs}, "keys": []}
    path._saved(journal, "marking")
    with pytest.raises(Interrupted):
        path._finish_deletion(journal)
    marker_path = marks.marker_path(w.root, job_id)
    payload = marker_path.read_bytes()
    assert 1 << 20 < len(payload) < marks.RECORD_LIMIT
    marker = json.loads(payload)
    assert not [event for event in w.store.list_events(job_id) if event["kind"] == rd.EVENT]
    now[0] += timedelta(seconds=60)
    again = delete_path(w, now=lambda: now[0])
    assert again._resume(job_id) == {"finished": "marking"}
    events = [event for event in w.store.list_events(job_id) if event["kind"] == rd.EVENT]
    assert len(events) == 1
    assert marks.marker(w.root, job_id) == marker == json.loads(events[0]["data_json"])
    assert not again._journal_dir(job_id).exists()


def test_marker_and_journal_share_the_record_bound():
    assert rd.JOURNAL_LIMIT == marks.RECORD_LIMIT == 16 << 20


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_marker_reader_has_an_exact_bounded_data_policy(tmp_path, monkeypatch, offset):
    limit = 256
    monkeypatch.setattr(marks, "RECORD_LIMIT", limit)
    job_id = "nonexistent-marker-bound"
    marker_path = marks.marker_path(tmp_path, job_id)
    marker_path.parent.mkdir(parents=True)
    empty = {"synthetic_padding": ""}
    padding = limit + offset - len(rd._canonical(empty))
    expected = {"synthetic_padding": "x" * padding}
    payload = rd._canonical(expected)
    assert len(payload) == limit + offset
    marker_path.write_bytes(payload)
    actual = marks.marker(tmp_path, job_id)
    if offset <= 0:
        assert actual == expected
    else:
        assert "unreadable" in actual
