"""Independent round-three probes for PR #131; preserved as a patch in the review report.

A test that asserts what the contract (C-5.5, C-5.7) promises fails when the
promise does not hold. Real-process tests start their processes here, record
their exact PIDs/groups, and stop and reap them in `finally`.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as dm, procs, protocol
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_review_pr131_probes import (BOOT, NEXT_BOOT, ORIGINAL_CENSUS, confirm_dead, ident,
                                                 quarantine as ro_quarantine, script_table)
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_workspace_contract import repository

ROOT_DIR = Path(__file__).resolve().parents[2]
# The production readers, captured before `state_daemon` replaces them.
REAL = {name: getattr(procs, name) for name in ("boot_id", "proc_start", "same_process", "containment", "liveness", "cwd_pids")}



def _inspection_available():
    try:
        REAL["proc_start"](os.getpid())
        REAL["boot_id"]()
        return True
    except procs.InspectionError:
        return False


REAL_INSPECTION = _inspection_available()

def real_ps(monkeypatch):
    for name, fn in REAL.items():
        monkeypatch.setattr(procs, name, fn)


def held(daemon, a):
    actual = daemon.store.get_attempt(a["attempt_id"])
    leases = [l for l in daemon.store.list_leases() if l["holder"] in {a["job_id"], a["attempt_id"]}]
    return actual, leases


def resolve(daemon, a, operator):
    if operator:
        confirm_dead(daemon, a)
    else:
        daemon._recheck_quarantines()
    return held(daemon, a)


def marker_env(daemon, a, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env.update(SUBFLEET_ATTEMPT=a["attempt_id"], SUBFLEET_ROOT=str(daemon.root), **extra)
    return env


def dead_identity():
    """A real process identity on this boot whose process has exited and been reaped."""
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                         start_new_session=True)
    try:
        found = REAL["proc_start"](p.pid)
        return procs.ProcessIdentity(p.pid, REAL["boot_id"](), found)
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_group(proc):
    """Stop a test-owned new-session process group by its exact recorded pgid."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def writable_attempt(daemon, harness):
    repository(daemon, harness)
    job, a, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True,
                           out_path=str(harness.root / "held-output.md"))
    return a, adir


# --- Check 3: the residual is broader than documented on this host -----------------

@pytest.mark.skipif(sys.platform != "darwin" or not REAL_INSPECTION, reason="sandbox blocks ps/boot inspection")
@pytest.mark.parametrize("operator", [False, True])
def test_a_detached_platform_shell_writer_keeps_its_markers_and_the_quarantine(
        state_daemon, monkeypatch, operator):
    """C-5.7 says ordinary detached children keep their environment markers and
    stay held when visible to the census. The documented residual applies
    only if the platform hides every observation; the macOS CI runner may
    expose the shell's markers. The shell starts outside the workdir. Nothing is
    scrubbed: /bin/zsh is exec'd with both markers in a new session (as a Claude
    Code Bash tool shell runs), and it appends to the job's folder."""
    daemon, harness = state_daemon
    real_ps(monkeypatch)
    clock = Clock(monkeypatch, daemon)
    a, adir = writable_attempt(daemon, harness)
    guardian, child = dead_identity(), dead_identity()
    start = {"guardian_pid": guardian.pid, "pgid": guardian.pid, "boot_id": guardian.boot_id,
             "proc_start": guardian.proc_start, "started_at": dm.utcnow(),
             "child_pid": child.pid, "child_identity": {"pid": child.pid, "boot_id": child.boot_id,
                                                        "proc_start": child.proc_start}}
    (adir / "start.json").write_text(json.dumps(start))
    daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=guardian.pid, pgid=guardian.pid,
                                boot_id=guardian.boot_id, proc_start=guardian.proc_start, child_pid=child.pid)
    a = daemon.store.get_attempt(a["attempt_id"])
    log = harness.workdir / "zsh-writer.log"
    marked = shell = None
    try:
        # A visible marked writer (a non-platform interpreter) causes the quarantine.
        marked = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                  env=marker_env(daemon, a), start_new_session=True)
        shell = subprocess.Popen(["/bin/zsh", "-c", f"while :; do print -r -- $$ >> {log}; sleep 0.2; done"],
                                 env=marker_env(daemon, a), start_new_session=True, cwd=harness.root)
        deadline = time.monotonic() + 10
        while not (log.exists() and log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        census = daemon._contain(a)
        assert marked.pid in census.marker_pids, census.to_dict()
        shell_seen = shell.pid in census.live_pids
        daemon._quarantine(a, census, "termination could not verify containment")
        a, leases = held(daemon, a)
        assert a["state"] == "quarantined" and leases
        marked.kill()
        marked.wait(timeout=10)
        clock.advance()
        actual, leases = resolve(daemon, a, operator)
        before = len(log.read_text().splitlines())
        time.sleep(1.0)
        after = len(log.read_text().splitlines())
        writing = alive(shell.pid) and after > before
        event = daemon.store.one("SELECT kind, data_json FROM events WHERE attempt_id=? AND kind IN "
                                 "('quarantine.self_resolved','quarantine.confirmed_dead')", (a["attempt_id"],))
        evidence = {"state": actual["state"], "leases": leases, "shell_pid": shell.pid,
                    "shell_seen_by_census": shell_seen, "shell_still_writing_after_release": writing,
                    "log_lines_before_after": (before, after), "release_event": event and event["kind"],
                    "release_census": event and json.loads(event["data_json"])["containment"]}
        print("EVIDENCE " + json.dumps(evidence, default=str))
        roots = json.loads(actual["evidence_json"] or "{}").get("lineage_roots", [])
        observed = shell_seen or any(r["pid"] == shell.pid or r["pgid"] == shell.pid for r in roots)
        if (not observed and writing and actual["state"] == "lost" and not leases
                and evidence["release_census"] and not evidence["release_census"]["live_pids"]
                and not evidence["release_census"]["unverifiable"]):
            pytest.xfail("C-5.7 residual: platform hid an entirely unobserved outside-cwd writer")
        assert actual["state"] == "quarantined" and leases, evidence
    finally:
        stop_group(shell)
        if marked is not None and marked.poll() is None:
            marked.kill()
            marked.wait(timeout=10)


@pytest.mark.skipif(not REAL_INSPECTION or not shutil.which("redis-server"), reason="needs ps inspection and redis-server")
@pytest.mark.xfail(strict=True, reason="Accepted residual: never observed, cwd outside attempt, redis rewrites title")
def test_a_dev_server_started_with_both_markers_is_seen_by_the_census(tmp_path, monkeypatch):
    """C-5.7: dev servers keep their environment markers and stay held."""
    real_ps(monkeypatch)
    root = str(tmp_path / "state")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env.update(SUBFLEET_ATTEMPT="probe-job/a1", SUBFLEET_ROOT=root)
    server = subprocess.Popen([shutil.which("redis-server"), "--port", "0", "--unixsocket", "r.sock",
                               "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
                              cwd=tmp_path, env=env, start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "r.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.poll() is None
        census = procs.containment(None, None, None, "probe-job/a1", root=root)
        assert server.pid in census.marker_pids and not census.verified_empty, census.to_dict()
    finally:
        stop_group(server)


def _fork_after_read(daemon, monkeypatch, a, *, child_row, child_markers):
    world = {300: (1, 300, "Ss", "writer-start")}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(dict(world), boot_id=BOOT))

    def marker_read(argv, **kwargs):
        assert "pid=,command=" in argv
        world[301] = child_row
        return f"301 late-child {child_markers}\n" if child_markers else ""
    monkeypatch.setattr(procs, "_read", marker_read)
    monkeypatch.setattr(procs, "_stat", lambda pid: world[pid][2] if pid in world else "")
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, BOOT, world[pid][3])
                        if pid in world else None)
    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)


@pytest.mark.parametrize("variant", ["setpgid-only-same-session", "scrubs-only-SUBFLEET_ROOT",
                                     "scrubs-only-SUBFLEET_ATTEMPT"])
@pytest.mark.parametrize("operator", [False, True])
def test_only_the_full_documented_evasive_combination_escapes(state_daemon, monkeypatch, operator, variant):
    """Partial markers hold. The no-marker setpgid case is an accepted residual:
    it was never observed, never had cwd in the workdir, and scrubbed both
    markers. Starting a new session is not required (session IDs are not read).
    """
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = ro_quarantine(daemon, harness, held={"300": ident(300, "writer-start")})
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    attempt = f"SUBFLEET_ATTEMPT={a['attempt_id']}"
    root = f"SUBFLEET_ROOT={daemon.root}"
    row, kept = {
        # A new process group in the same session (stat has no session-leader `s`).
        "setpgid-only-same-session": ((300, 301, "S", "late-child"), ""),
        "scrubs-only-SUBFLEET_ROOT": ((300, 301, "Ss", "late-child"), attempt),
        "scrubs-only-SUBFLEET_ATTEMPT": ((300, 301, "Ss", "late-child"), root),
    }[variant]
    _fork_after_read(daemon, monkeypatch, a, child_row=row, child_markers=kept)
    clock.advance()
    daemon._recheck_quarantines()
    a = daemon.store.get_attempt(a["attempt_id"])
    assert a["state"] == "quarantined"
    script_table(monkeypatch, {301: (1,) + row[1:]}, markers=f"301 late-child {kept}\n" if kept else "")
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    if variant == "setpgid-only-same-session":
        assert actual["state"] == "lost" and not leases, (variant, actual["state"], leases)
    else:
        assert actual["state"] == "quarantined" and leases, (variant, actual["state"], leases)


# --- Check 1: parity, differential over scripted worlds ---------------------------

PIDS = (100, 200, 300, 301, 500)
ROW = st.tuples(st.sampled_from((1, 100, 200, 300, 500)), st.sampled_from((100, 300, 301, 500)),
                st.sampled_from(("S", "Ss", "Z")), st.sampled_from(("old", "new")))
WORLD = st.fixed_dictionaries({
    "rows": st.dictionaries(st.sampled_from(PIDS), ROW, max_size=5),
    "markers": st.sets(st.sampled_from(PIDS), max_size=2),
    "held": st.sets(st.sampled_from(PIDS), max_size=2),
    "owned": st.sets(st.sampled_from(PIDS), max_size=2),
    "start": st.sampled_from(("none", "legacy", "published")),
    "exit": st.booleans(),
    "db_child": st.booleans(),
    "boot": st.sampled_from((BOOT, NEXT_BOOT, "1700000000")),
    "table_fails": st.booleans(),
    "markers_fail": st.booleans(),
    "operator_first": st.booleans(),
})


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(world=WORLD)
def test_automatic_and_confirm_dead_take_the_same_census_and_decision(state_daemon, monkeypatch, world):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    # Compare independent worlds without repeated admission/manifest writes.
    # Crash durability has separate coverage; this fixture tests decisions.
    daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
    pair = getattr(daemon, "_review_census_pair", None)
    if pair is None:
        pair = [ro_quarantine(daemon, harness) for _ in range(2)]
        daemon._review_census_pair = pair
    attempts = []
    for a in pair:
        daemon.store.update_attempt(a["attempt_id"], state="running", child_pid=None,
                                    evidence_json=json.dumps({"owned_identities": {
                                        str(p): ident(p, "old") for p in world["owned"]}}),
                                    quarantine_reason=None)
        daemon.store.update_job(a["job_id"], state="running", finished_at=None, rc=None)
        daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
        held_ids = {p: procs.ProcessIdentity(**ident(p, "old")) for p in world["held"]}
        daemon._quarantine(a, procs.Containment(marker_pids=frozenset(held_ids), identities=held_ids), "review world")
        adir = daemon.root / "jobs" / a["job_id"] / "a1"
        for name in ("start.json", "exit.json", "quarantine-saved.json"):
            (adir / name).unlink(missing_ok=True)
        start = {"guardian_pid": 100, "pgid": 100, "boot_id": BOOT, "proc_start": "guardian-start"}
        if world["start"] == "published":
            start.update(child_pid=500, child_identity=ident(500, "old"))
        if world["start"] != "none":
            (adir / "start.json").write_text(json.dumps(start))
        if world["exit"]:
            (adir / "exit.json").write_text(json.dumps({"rc": 0, "child_pid": 500}))
        if world["db_child"]:
            daemon.store.update_attempt(a["attempt_id"], child_pid=500)
        attempts.append(daemon.store.get_attempt(a["attempt_id"]))
    automatic, operator = attempts
    text = "".join(f"{pid} w SUBFLEET_ATTEMPT={a['attempt_id']} SUBFLEET_ROOT={daemon.root}\n"
                   for pid in sorted(world["markers"]) for a in attempts)
    script_table(monkeypatch, world["rows"], markers=text, boot=world["boot"],
                 table_fails=world["table_fails"], markers_fail=world["markers_fail"])
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, world["boot"], "old")
                        if pid in world["rows"] and not world["rows"][pid][2].startswith("Z") else None)
    first = daemon._contain(automatic).to_dict()
    second = daemon._contain(operator).to_dict()
    assert first == second
    clock.advance()
    order = [(operator, True), (automatic, False)] if world["operator_first"] else [(automatic, False), (operator, True)]
    for a, by_operator in order:
        if by_operator:
            daemon._resolve_quarantine(a, protocol.KillArgs(a["job_id"], confirm_dead=True))
        else:
            daemon._resolve_quarantine(a, None)      # what `_recheck_quarantines` calls per due attempt
    (sa, la), (so, lo) = held(daemon, automatic), held(daemon, operator)
    assert (sa["state"], bool(la)) == (so["state"], bool(lo)), (world, first)
    if sa["state"] == "quarantined":
        assert json.loads(sa["quarantine_reason"])["errors"] == json.loads(so["quarantine_reason"])["errors"]


# --- Check 2: provider execution waits for publication, under a real hard kill ---

@pytest.mark.skipif(sys.platform != "darwin" or not REAL_INSPECTION, reason="sandbox blocks ps/boot inspection")
def test_sigkill_during_child_publication_never_executes_the_provider(tmp_path):
    """A real guardian is SIGKILLed while reading its launcher's identity (no
    cleanup code runs). EOF must stop the launcher before any provider code."""
    attempt = tmp_path / "a1"
    ran, launcher_file = tmp_path / "provider-ran", tmp_path / "launcher.pid"
    script = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from subfleet import guardian\n"
        "real = guardian.proc_start\n"
        "def slow(pid):\n"
        "    if pid != os.getpid():\n"
        f"        Path({str(launcher_file)!r}).write_text(str(pid))\n"
        "        time.sleep(120)\n"
        "    return real(pid)\n"
        "guardian.proc_start = slow\n"
        f"sys.exit(guardian.run_guardian(['/usr/bin/touch', {str(ran)!r}], attempt_dir=Path({str(attempt)!r}),\n"
        f"    cwd={str(tmp_path)!r}, stdin_path=None, stdout_path={str(tmp_path / 'out')!r},\n"
        f"    stderr_path={str(tmp_path / 'err')!r}))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT_DIR)
    g = subprocess.Popen([sys.executable, "-c", script], env=env)
    launcher = None
    try:
        deadline = time.monotonic() + 20
        while not (launcher_file.exists() and launcher_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        launcher = int(launcher_file.read_text())
        assert alive(launcher)
        os.kill(g.pid, signal.SIGKILL)
        g.wait(timeout=10)
        deadline = time.monotonic() + 10
        while alive(launcher) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(launcher), "launcher outlived its guardian"
        time.sleep(0.5)
        assert not ran.exists(), "provider executed without durable publication"
        receipt = json.loads((attempt / "start.json").read_text())
        assert "child_pid" not in receipt and not (attempt / "exit.json").exists()
    finally:
        if g.poll() is None:
            g.kill()
            g.wait(timeout=10)
        if launcher and alive(launcher):
            os.kill(launcher, signal.SIGKILL)


# --- Check 4: a still-running 2.1.10 guardian beside this daemon -------------------

GUARDIAN_2110 = "f832e6b812bb94ed59318344992d53437010649c"    # installed release.json source_commit


def _legacy_tree(tmp_path_factory):
    tree = tmp_path_factory.mktemp("v2110")
    archive = subprocess.run(["git", "-C", str(ROOT_DIR), "archive", GUARDIAN_2110, "subfleet"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    return tree


@pytest.mark.skipif(sys.platform != "darwin" or not REAL_INSPECTION, reason="sandbox blocks ps/boot inspection")
@pytest.mark.parametrize("guardian_version", ["2.1.10", "head"])
def test_kill_escalation_against_each_guardian_version(state_daemon, monkeypatch, tmp_path_factory,
                                                       guardian_version):
    """The daemon's own SIGKILL of the recorded group ends a provider that
    ignores SIGTERM. A 2.1.10 guardian publishes no child, so the attempt
    discharges from its recorded provider identities without force release;
    a head guardian's attempt finalizes."""
    daemon, harness = state_daemon
    real_ps(monkeypatch)
    clock = Clock(monkeypatch, daemon)
    daemon.term_grace_s, daemon.kill_settle_s = 1.0, 1.0
    a, adir = writable_attempt(daemon, harness)
    tree = ROOT_DIR if guardian_version == "head" else _legacy_tree(tmp_path_factory)
    env = marker_env(daemon, a, PYTHONPATH=str(tree))
    provider = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"]
    g = subprocess.Popen([sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(adir),
                          "--cwd", str(harness.workdir), "--stdout-path", str(adir / "stdout"),
                          "--stderr-path", str(adir / "stderr"), "--", *provider], env=env, cwd=tree)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            start = json.loads((adir / "start.json").read_text()) if (adir / "start.json").exists() else {}
            if start and (guardian_version != "head" or "child_pid" in start):
                break
            time.sleep(0.05)
        assert ("child_pid" in start) is (guardian_version == "head"), start
        time.sleep(0.5)
        daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=start["guardian_pid"],
                                    pgid=start["pgid"], boot_id=start["boot_id"], proc_start=start["proc_start"],
                                    started_at=start["started_at"])
        a = daemon.store.get_attempt(a["attempt_id"])
        providers = [pid for pid, row in procs.snapshot().rows.items() if row[0] == start["guardian_pid"]]
        assert len(providers) == 1, providers
        daemon._kill_attempt(a)
        g.wait(timeout=20)
        actual, leases = held(daemon, a)
        if guardian_version == "head":
            assert actual["state"] == "finalizing", actual["quarantine_reason"]
            return
        assert actual["state"] in {"finalizing", "quarantined"}
        # The kill's first census, taken while the 2.1.10 guardian was verified
        # alive, already recorded its only child as an owned identity.
        owned = json.loads(actual["evidence_json"])["owned_identities"]
        print("LEGACY " + json.dumps({"provider": providers[0], "owned": sorted(owned),
                                      "errors": json.loads(actual["quarantine_reason"] or "{}").get("errors", [])}))
        assert str(providers[0]) in owned
        for operator in (False, True, False):
            clock.advance()
            actual, leases = resolve(daemon, actual, operator)
            assert actual["state"] in {"finalizing", "lost"}
            if actual["state"] == "lost":
                assert not leases
    finally:
        if g.poll() is None:
            os.killpg(g.pid, signal.SIGKILL)
            g.wait(timeout=10)


# --- Check 3: what the retention lsof backstop covers ------------------------------

from tests.unit.test_retention_archive import world  # noqa: E402,F401
from subfleet import retention  # noqa: E402


@pytest.mark.real_lsof
@pytest.mark.skipif(not os.access("/usr/sbin/lsof", os.X_OK), reason="needs lsof")
@pytest.mark.xfail(strict=True, reason="Accepted residual: never observed, cwd outside workdir, no open file between appends")
def test_real_lsof_protects_a_worktree_from_an_intermittent_invisible_writer(world):
    """C-5.7 cites the lsof holder check as the backstop for invisible writers.
    This writer appends and closes every 0.2 s from a cwd outside the tree."""
    w = world
    wt = w.job("job-intermittent")
    log = wt / "progress.log"
    shell = subprocess.Popen(["/bin/zsh", "-c", f"while :; do print -r -- tick >> {log}; sleep 0.2; done"],
                             cwd=w.root, start_new_session=True,
                             env={k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")},
                             stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not (log.exists() and log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0)
        print("RETENTION " + json.dumps(result, default=str)[:600])
        assert result["pruned"] == [] and "busy" in result["deferred"].get("job-intermittent", ""), result
    finally:
        stop_group(shell)
