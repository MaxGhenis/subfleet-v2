"""C-5.5: a parent's turn and waiter must not hold a completed child run."""
import json

import pytest

from subfleet import procs

BOOT = "6F1C0F2E-1111-4222-8333-944455556666"


def world(monkeypatch, markers, *, rows=None):
    rows = rows or {77: (1, 77, "S", "parent-turn"), 78: (77, 77, "S", "waiter")}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows, boot_id=BOOT))
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, BOOT, rows[pid][3]))
    monkeypatch.setattr(procs, "process_group", lambda pid: rows[pid][1], raising=False)
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs: markers)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset(), raising=False)


@pytest.mark.parametrize("other", ["parent/a1", "child/a10", "child/a2"])
def test_other_attempt_under_same_root_does_not_hold_child_run(monkeypatch, other):
    world(monkeypatch, f"77 turn SUBFLEET_ATTEMPT={other} SUBFLEET_ROOT=/fixture SECRET=private\n"
                       f"78 wait SUBFLEET_ROOT=/fixture SUBFLEET_ATTEMPT={other}\n")
    found = procs.containment(None, None, None, "child/a1", root="/fixture")
    assert found.verified_empty, found.to_dict()
    assert not found.marker_pids
    assert "private" not in json.dumps(found.to_dict())


@pytest.mark.parametrize("markers", [
    "SUBFLEET_ATTEMPT=child/a1 SUBFLEET_ROOT=/fixture",
    "SUBFLEET_ATTEMPT=child/a1",
    "SUBFLEET_ATTEMPT=child/a1 SUBFLEET_ROOT=/other",
    "SUBFLEET_ROOT=/fixture",
    "SUBFLEET_ROOT=/fixture SUBFLEET_ATTEMPT=",
    "SUBFLEET_ROOT=/fixture SUBFLEET_ATTEMPT=other/a1 SUBFLEET_ATTEMPT=child/a1",
])
def test_partial_or_matching_attempt_markers_still_hold(monkeypatch, markers):
    world(monkeypatch, f"77 writer {markers}\n")
    found = procs.containment(None, None, None, "child/a1", root="/fixture")
    assert found.marker_pids == {77}
    assert not found.verified_empty


def test_different_marker_does_not_discard_a_recorded_writer(monkeypatch):
    world(monkeypatch, "77 writer SUBFLEET_ATTEMPT=other/a1 SUBFLEET_ROOT=/fixture\n")
    found = procs.containment(None, None, None, "child/a1", root="/fixture",
                              recorded={77: procs.ProcessIdentity(77, BOOT, "parent-turn")})
    assert 77 in found.live_pids
    assert not found.verified_empty


@pytest.mark.parametrize("visible", [False, True], ids=["hidden-platform-environment", "visible-platform-environment"])
def test_platform_writer_marker_visibility_controls_the_documented_residual(monkeypatch, visible):
    world(monkeypatch, "77 zsh SUBFLEET_ATTEMPT=child/a1 SUBFLEET_ROOT=/fixture\n" if visible else "77 zsh\n")
    found = procs.containment(None, None, None, "child/a1", root="/fixture")
    assert found.verified_empty is (not visible)
    assert found.marker_pids == ({77} if visible else set())


@pytest.mark.parametrize("foreign_visible", [False, True])
def test_shared_cwd_excludes_only_an_identified_different_attempt(monkeypatch, foreign_visible):
    markers = "77 parent SUBFLEET_ATTEMPT=parent/a1 SUBFLEET_ROOT=/fixture\n" if foreign_visible else ""
    world(monkeypatch, markers)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({77}))
    found = procs.containment(None, None, None, "child/a1", root="/fixture", workdir="/work")
    assert found.verified_empty is foreign_visible
    assert found.cwd_pids == (set() if foreign_visible else {77})


@pytest.mark.parametrize("change", ["reused-before-marker", "reused-before-cwd", "unreadable-marker"])
def test_shared_cwd_never_excludes_an_unqualified_or_reused_pid(monkeypatch, change):
    world(monkeypatch, "77 parent SUBFLEET_ATTEMPT=parent/a1 SUBFLEET_ROOT=/fixture\n")
    old = procs.ProcessIdentity(77, BOOT, "parent-turn")
    new = procs.ProcessIdentity(77, BOOT, "new-writer")
    calls = []
    def identify(pid):
        calls.append(pid)
        if change == "unreadable-marker" and len(calls) == 1:
            raise procs.InspectionError("identity unavailable")
        return new if change == "reused-before-marker" or len(calls) > 1 else old
    monkeypatch.setattr(procs, "identity", identify)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({77}))
    found = procs.containment(None, None, None, "child/a1", root="/fixture", workdir="/work")
    assert 77 in found.cwd_pids
    assert not found.verified_empty
