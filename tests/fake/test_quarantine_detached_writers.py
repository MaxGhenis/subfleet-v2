"""C-5.5/C-5.7: durable lineage and cwd protect detached macOS writers."""
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as dm, procs
from tests.fake.test_review_pr131_probes import BOOT, NEXT_BOOT, ident, quarantine, script_table
from tests.fake.test_review_pr131_round3 import REAL, held, marker_env, real_ps, resolve
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401


def running(daemon, harness):
    _, a, adir = reserve(daemon, harness)
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=100,
                                pgid=100, boot_id=BOOT, proc_start="guardian")
    (adir / "start.json").write_text(json.dumps({"guardian_pid": 100, "boot_id": BOOT,
                                               "proc_start": "guardian", "pgid": 100}))
    return daemon.store.get_attempt(a["attempt_id"])


@pytest.mark.parametrize("observe", ["inspection", "kill-census", "finalization-census"])
@pytest.mark.parametrize("operator", [False, True])
def test_observed_detached_lineage_and_group_survive_parent_exit(state_daemon, monkeypatch, observe, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = running(daemon, harness)
    rows = {100: (1, 100, "Ss", "guardian"), 200: (100, 200, "Ss", "shell"),
            201: (200, 200, "S", "sleep")}
    script_table(monkeypatch, rows)
    if observe == "inspection":
        daemon._record_owned(a, procs.snapshot())
    elif observe == "kill-census":
        daemon._record_owned(a, procs.snapshot())
        daemon.term_grace_s = daemon.kill_settle_s = 0
        monkeypatch.setattr(procs, "same_process", lambda *args: True)
        monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
        signals = []
        monkeypatch.setattr(procs, "signal_process", lambda identity, sig: signals.append(identity.pid))
        daemon._kill_attempt(a)
        assert not {200, 201}.intersection(signals), "lineage is not signal authority"
    else:
        # _finalize takes this same _contain before any salvage or lease release.
        daemon._contain(a)
    saved = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert {200, 201} <= {value["pid"] for value in saved["lineage_roots"]}
    assert "200" not in saved.get("owned_identities", {})
    # Guardian and shell die. A new member of the retained group remains, and
    # its child has a different group/session and neither marker.
    script_table(monkeypatch, {202: (1, 200, "S", "member"), 203: (202, 203, "Ss", "grandchild")})
    census = daemon._contain(a)  # deliberately reuse the stale pre-observation row
    assert {202, 203} <= census.live_pids, census.to_dict()
    daemon._quarantine(a, census, "detached writers")
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, actual, operator)
    assert actual["state"] == "lost" and not leases


@pytest.mark.parametrize("operator", [False, True])
def test_legacy_kill_owned_provider_discharges_without_force(state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, owned={"100": ident(100, "guardian-start"), "200": ident(200, "provider")})
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    adir = daemon.root / "jobs" / a["job_id"] / "a1"
    (adir / "start.json").write_text(json.dumps({"guardian_pid": 100, "boot_id": BOOT,
                                               "proc_start": "guardian-start", "pgid": 100}))
    daemon.store.update_attempt(a["attempt_id"], killed_by="operator")
    script_table(monkeypatch, {200: (1, 200, "Ss", "provider")})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, actual, operator)
    assert actual["state"] == "lost" and not leases


def test_lineage_limit_keeps_newest_and_overflow_holds_until_proven_reboot(monkeypatch):
    monkeypatch.setattr(dm, "LINEAGE_ROOT_LIMIT", 2)
    roots = [dataclasses.asdict(procs.CensusRoot(pid, BOOT, str(pid), pid)) for pid in (100, 200, 300)]
    evidence = dm._retain_lineage({}, {"identities": {}, "lineage_roots": roots})
    assert evidence["lineage_roots"] == roots[-2:]
    assert evidence["lineage_overflow_boot"] == BOOT
    refreshed = dm._retain_lineage(evidence, {"identities": {}, "lineage_roots": [roots[1]]})
    assert refreshed["lineage_roots"] == [roots[2], roots[1]]
    history, overflow = dm._recent_census_identities({str(p): ident(p, str(p)) for p in (100, 200, 300)})
    assert set(history) == {"200", "300"} and overflow == BOOT
    for boot, empty in ((BOOT, False), ("unknown", False), (NEXT_BOOT, True)):
        monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable({}, boot_id=boot))
        monkeypatch.setattr(procs, "_read", lambda *args, **kwargs: "")
        census = procs.containment(None, None, None, "a1", lineage_overflow_boot=BOOT)
        assert census.verified_empty is empty


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["cwd", "failed-cwd"])
def test_cwd_holds_on_both_paths_and_releases_within_one_pace(state_daemon, monkeypatch, source, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    script_table(monkeypatch, {400: (1, 400, "Ss", "unobserved-shell")})
    def cwd(workdir):
        if source == "failed-cwd":
            raise procs.InspectionError("lsof failed")
        return frozenset({400})
    monkeypatch.setattr(procs, "cwd_pids", cwd)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    census = daemon._contain(actual)
    assert census.unverifiable if source == "failed-cwd" else census.cwd_pids == {400}
    script_table(monkeypatch, {})
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    clock.advance()
    actual, leases = resolve(daemon, actual, operator)
    assert actual["state"] == "lost" and not leases


EVENTS = st.lists(st.tuples(st.sampled_from(("fork", "detach", "regroup", "resession", "exit", "inspect")),
                           st.integers(0, 20), st.integers(0, 20)), min_size=1, max_size=60)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(events=EVENTS)
def test_generated_process_tree_never_releases_visible_writers(state_daemon, monkeypatch, events):
    """An independent reachability oracle follows process-tree events.

    All rows model platform binaries with hidden markers. Session changes do
    not change ownership; the oracle independently expands saved groups and
    parent links and checks both resolver paths after every event.
    """
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = running(daemon, harness)
    # Seed an observed shell detached from the guardian; then kill the guardian
    # so saved identities/groups, rather than its current parent links, hold it.
    rows = {100: (1, 100, "Ss", "guardian"), 200: (100, 200, "Ss", "p200")}
    script_table(monkeypatch, rows)
    daemon._record_owned(a, procs.snapshot())
    daemon._quarantine(a, daemon._contain(a), "generated tree")
    del rows[100]
    rows[200] = (1, 200, "Ss", "p200")
    observed = {200}
    groups = {100, 200}
    next_pid = 201
    for index, (event, pick, target) in enumerate(events):
        pids = sorted(rows)
        if pids:
            pid = pids[pick % len(pids)]
            parent, group, stat, start = rows[pid]
            if event == "fork":
                rows[next_pid] = (pid, group, "S", f"p{next_pid}")
                next_pid += 1
            elif event in {"detach", "resession"}:
                rows[pid] = (parent, pid, "Ss", start)
            elif event == "regroup":
                rows[pid] = (parent, rows[pids[target % len(pids)]][1], "S", start)
            elif event == "exit":
                del rows[pid]
                rows = {p: (1 if row[0] == pid else row[0], *row[1:]) for p, row in rows.items()}
        # Independent transitive closure from observed live identities and
        # retained groups. No PID reuse in this tree; separate tests cover it.
        visible = (observed & rows.keys()) | {p for p, row in rows.items() if row[1] in groups}
        changed = True
        while changed:
            children = {p for p, row in rows.items() if row[0] in visible}
            changed = bool(children - visible)
            visible |= children
        script_table(monkeypatch, rows)
        census = daemon._contain(a)
        assert visible <= census.live_pids, (events, index, visible, census.to_dict())
        # Full censuses retain observations each event; inspection events
        # exercise an unchanged table between mutations of the tree.
        observed |= visible
        groups |= {rows[p][1] for p in visible}
        clock.advance()
        # Resolve this due attempt directly: other Hypothesis examples share
        # the fixture's store and must not consume its eight-row queue budget.
        if index % 2:
            from tests.fake.test_review_pr131_probes import confirm_dead
            confirm_dead(daemon, a)
        else:
            daemon._resolve_quarantine(a, None)
        actual, leases = held(daemon, a)
        if visible:
            assert actual["state"] == "quarantined" and leases
        else:
            assert actual["state"] == "lost" and not leases
            break


def inspection_available():
    try:
        REAL["proc_start"](os.getpid())
        REAL["boot_id"]()
        return True
    except procs.InspectionError:
        return False


REAL_INSPECTION = inspection_available()


def command(kind, log):
    if kind in {"zsh", "setsid-shell"}:
        return ["/bin/zsh", "-c", 'while :; do print -r -- tick >> "$1"; sleep 0.1; done', "writer", str(log)]
    if kind == "sleep":
        return ["/bin/sleep", "120"]
    if kind == "tail":
        log.touch()
        return ["/usr/bin/tail", "-f", str(log)]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    return [node, "-e", "process.title='detached-dev-server'; setInterval(()=>{},1000)"]


@pytest.mark.skipif(not REAL_INSPECTION, reason="sandbox blocks ps/boot inspection; hub must run real census")
@pytest.mark.parametrize("kind", ["zsh", "sleep", "tail", "node-title", "setsid-shell"])
@pytest.mark.parametrize("source", ["lineage", "cwd"])
@pytest.mark.parametrize("operator", [False, True])
def test_real_detached_writers_hold_then_release(state_daemon, monkeypatch, tmp_path, kind, source, operator):
    daemon, harness = state_daemon
    real_ps(monkeypatch)
    clock = Clock(monkeypatch, daemon)
    _, a, adir = reserve(daemon, harness)
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    log = harness.workdir / "writer.log"
    pidfile = tmp_path / "writer.pid"
    writer_cwd = harness.workdir if source == "cwd" else tmp_path
    argv = command(kind, log)
    # The guardian/provider topology is real: observe the detached child before
    # its parent exits, then verify saved lineage with cwd outside the workdir.
    script = ("import os,subprocess,sys\nfrom pathlib import Path\n"
              f"p=subprocess.Popen({argv!r}, cwd={str(writer_cwd)!r}, "
              + ("preexec_fn=os.setsid" if kind == "setsid-shell" else "start_new_session=True")
              + ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
              f"Path({str(pidfile)!r}).write_text(str(p.pid))\nsys.stdin.read()\n")
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              env=marker_env(daemon, a), start_new_session=True, cwd=tmp_path)
    writer = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        writer = int(pidfile.read_text())
        identity = procs.identity(parent.pid)
        assert identity is not None
        daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=parent.pid,
                                    pgid=parent.pid, boot_id=identity.boot_id, proc_start=identity.proc_start)
        a = daemon.store.get_attempt(a["attempt_id"])
        if source == "lineage":
            daemon._record_owned(a, procs.snapshot())
        parent.stdin.close()
        parent.wait(timeout=10)
        census = daemon._contain(a)
        assert writer in census.live_pids, census.to_dict()
        daemon._quarantine(a, census, "real detached writer")
        for _ in range(2):
            clock.advance()
            actual, leases = resolve(daemon, a, operator)
            assert actual["state"] == "quarantined" and leases
        os.killpg(writer, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while procs.identity(writer) is not None and time.monotonic() < deadline:
            time.sleep(.02)
        assert procs.identity(writer) is None
        clock.advance()
        actual, leases = resolve(daemon, a, operator)
        assert actual["state"] == "lost" and not leases
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if writer is not None:
            try:
                os.killpg(writer, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("kind", ["zsh", "sleep", "tail", "node-title", "setsid-shell"])
def test_real_cwd_scan_sees_platform_and_retitled_processes(tmp_path, kind):
    """Exercises real lsof even where the sandbox denies ps; no release claim."""
    workdir = tmp_path / "workdir"
    inside = workdir / "sub directory\nwith newline"
    inside.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(workdir, target_is_directory=True)
    argv = command(kind, inside / "writer.log")
    proc = subprocess.Popen(argv, cwd=alias / inside.name, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert proc.pid in procs.cwd_pids(str(workdir))
        assert proc.pid in procs.cwd_pids(str(alias))
        proc.kill()
        proc.wait(timeout=10)
        assert proc.pid not in procs.cwd_pids(str(workdir))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
