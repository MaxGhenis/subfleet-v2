"""Process-table, marker, cwd and group reads never share PID incarnations."""

import pytest

from subfleet import daemon, procs

BOOT = "6F1C0F2E-1111-4222-8333-944455556666"
NEXT_BOOT = "7F1C0F2E-1111-4222-8333-944455556666"


def test_all_three_reads_retain_distinct_incarnations_of_one_pid(monkeypatch):
    phase = ["table"]
    starts = {"table": "first", "marker": "second", "cwd": "third"}
    groups = {"table": 100, "marker": 200, "cwd": 300}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable({99: (1, 100, "S", "first")}, BOOT))
    def markers(argv, **kwargs):
        phase[0] = "marker"
        return "99 writer SUBFLEET_ATTEMPT=a1\n"
    def cwd(workdir):
        phase[0] = "cwd"
        return {99}
    monkeypatch.setattr(procs, "_read", markers)
    monkeypatch.setattr(procs, "cwd_pids", cwd)
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, BOOT, starts[phase[0]]))
    monkeypatch.setattr(procs, "process_group", lambda pid: groups[phase[0]])
    census = procs.containment(100, None, None, "a1", workdir="/work")
    assert {(root.proc_start, root.pgid) for root in census.lineage_roots} == {
        ("first", 100), ("second", 200), ("third", 300)}
    assert not census.verified_empty and not census.unverifiable
    evidence = daemon._retain_lineage({}, census.to_dict())
    assert {(root["proc_start"], root["pgid"]) for root in evidence["lineage_roots"]} == {
        ("first", 100), ("second", 200), ("third", 300)}


@pytest.mark.parametrize("change", ["start", "boot"])
def test_reuse_during_group_capture_holds_without_attaching_the_wrong_group(monkeypatch, change):
    first = procs.ProcessIdentity(99, BOOT, "first")
    second = procs.ProcessIdentity(99, NEXT_BOOT if change == "boot" else BOOT,
                                   "first" if change == "boot" else "second")
    reads = iter([first, second])
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable({}, BOOT))
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs: "99 writer SUBFLEET_ATTEMPT=a1\n")
    monkeypatch.setattr(procs, "identity", lambda pid: next(reads))
    monkeypatch.setattr(procs, "process_group", lambda pid: 700)
    census = procs.containment(None, None, None, "a1")
    assert census.unverifiable and not census.verified_empty
    assert {root.identity for root in census.lineage_roots} == {first, second}
    assert {root.pgid for root in census.lineage_roots} == {0}


@pytest.mark.parametrize("source", ["group", "descendant", "marker", "cwd"])
def test_listed_missing_start_is_an_uncertain_root_until_absence(monkeypatch, source):
    rows = {99: (100 if source == "descendant" else 1, 100 if source == "group" else 200, "S", "")}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(dict(rows), BOOT))
    monkeypatch.setattr(procs, "identity", lambda pid: None)
    monkeypatch.setattr(procs, "_stat", lambda pid: "S")
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs:
                        "99 writer SUBFLEET_ATTEMPT=a1\n" if source == "marker" else "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: {99} if source == "cwd" else set())
    census = procs.containment(100 if source == "group" else None,
                               100 if source == "descendant" else None, None, "a1", workdir="/work")
    assert 99 in census.live_pids and census.unverifiable and not census.verified_empty
    assert any(root.pid == 99 and not root.proc_start for root in census.lineage_roots)
    roots = census.lineage_roots
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs: "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: set())
    rows[99] = (1, 300, "S", "now-readable")
    assert not procs.containment(None, None, None, "a1", lineage_roots=roots).verified_empty
    rows.clear()
    assert procs.containment(None, None, None, "a1", lineage_roots=roots).verified_empty


def test_snapshot_accepts_missing_start_but_identity_never_means_absent(monkeypatch):
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs: "99 1 100 S\n")
    table = procs.snapshot()
    assert table.live(99)
    with pytest.raises(procs.InspectionError, match="missing process start"):
        table.identity(99)
