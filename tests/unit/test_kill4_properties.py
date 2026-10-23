"""D-KILL4, C-5.5, C-5.6: generated process trees through the real finalization and kill paths.

Over generated trees (reparenting to launchd, sessions of their own, hidden
markers, stopped processes, TERM-ignoring processes, respawning drivers, cwds
in and outside the job's worktree, other users' processes, counted seeds and an
operator's processes), the daemon finalizes after an exit receipt or kills a
cancelled attempt on a simulated host (`tests/kill4_world.py`). For every input:

1. an attempt that ends anywhere but `quarantined` has a verified-empty census
   at its end (cwd holders aside, C-5.5): no terminal state with live census pids;
2. every pid the protocol signalled one by one was a live member of a census it
   took, and every group signal went to the attempt's recorded group: nothing
   outside the attempt's census is signalled;
3. a process is continued only right after the protocol sent it TERM;
4. an operator's process, a held parent, another user's process and the daemon
   are never signalled.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, procs
from subfleet.contracts import Credential, Lane, LaneOwner, attempt_dir
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.procs import ProcessTable
from subfleet.store import Store
from tests.kill4_world import World
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, StubAdapter, shared

TERM, KILL, CONT = int(signal.SIGTERM), int(signal.SIGKILL), int(signal.SIGCONT)
OPERATOR = 6000


def build_core(root: Path) -> Daemon:
    """The `tests/unit/test_daemon_settle.py` daemon core, built per example."""
    attempt_dir(root, JOB, 1).mkdir(parents=True)
    core = object.__new__(Daemon)
    core.root, core.store = root, Store(root / "state.sqlite3")
    core.stopping = threading.Event()
    core.term_grace_s, core.kill_settle_s, core.exit_settle_s = .02, .05, 0
    core._exit_settle, core._stream_check_failures = {}, {}
    core._children, core._pending_launches, core._starting_deadlines = {}, set(), {}
    core._inspect_next, core.inspect_interval_s, core._inspect_retry = {}, .5, set()
    core._reset_admission_state()
    core.policy = {}
    core._native, core._worker_failures, core._v1_unread = set(), {}, {}
    core._process_table = shared(ProcessTable({}, "boot"))
    core._launches, core._export_locks = {}, {}
    core.log = logging.getLogger("subfleet.test")
    core._salvage = lambda job, a: ([], None, {})
    core._record_identity = lambda *args: None
    core._export = lambda job_id: None
    core.timers = SimpleNamespace(record_auth_dead=lambda *args: None, metadata={})
    core._notify = lambda: None
    core._boundary = lambda *args: None
    core._publish = lambda role, path, contents: atomic_publish(path, contents)
    core.crash_hook = core.publish_hook = None
    home = root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    tree = root / "worktrees" / JOB
    tree.mkdir(parents=True)
    core.store.add_job(job_id=JOB, request_id="kill4-1", payload_digest="digest", kind="run",
                       state="running", workdir=str(root), prompt_path=str(root / "prompt.md"),
                       sandbox="workspace-write", worktree=str(tree))
    core.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                           model_requested="astra", state="running", guardian_pid=4242,
                           child_pid=4243, pgid=4242, boot_id="boot", proc_start=STARTED,
                           started_at=daemon_module.utcnow(), evidence_json="{}")
    adir = attempt_dir(root, JOB, 1)
    (adir / "stdout").write_text("hello\n")
    (adir / "stream.jsonl").write_text('{"type":"turn.completed"}\n')
    from subfleet.contracts import Launch
    core._launches[ATTEMPT] = Launch(argv=("fake",), env_add={}, env_remove=(), cwd=str(root),
                                     stdin_path=None, stdout_path=str(adir / "stdout"),
                                     stderr_path=str(adir / "stderr"), raw_stream_path=None,
                                     native_session_id=None, lane_id="codex-1")
    return core


@st.composite
def trees(draw):
    """A guardian (alive or gone), its provider, and up to eight more processes."""
    spec = {"guardian": draw(st.booleans()), "ending": draw(st.sampled_from(["receipt", "cancel"])),
            "rows": [], "counted": [], "operator": draw(st.booleans())}
    parents = [1, 4242, 4243]
    for n in range(draw(st.integers(0, 8))):
        pid = 5000 + n
        spec["rows"].append({
            "pid": pid, "ppid": draw(st.sampled_from(parents)),
            "pgid": draw(st.sampled_from(["guardian", "own", "foreign"])),
            "marked": draw(st.booleans()), "hidden": draw(st.booleans()),
            "stopped": draw(st.booleans()), "ignores_term": draw(st.booleans()),
            "respawns": draw(st.booleans()), "cwd": draw(st.sampled_from(["worktree", "elsewhere", None])),
            "other_uid": draw(st.integers(0, 9)) == 0,
            "orphaned": draw(st.booleans()),             # its parent died: reparented to launchd
        })
        parents.append(pid)
    for row in spec["rows"]:
        if draw(st.booleans()):
            spec["counted"].append(row["pid"])
    return spec


def build_world(core, spec) -> World:
    world = World(ATTEMPT, str(core.root))
    tree = str(core.root / "worktrees" / JOB)
    if spec["guardian"]:
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, exits_childless=True)
        world.add(4243, 4242, 4242, start=STARTED, marked=True)
    for row in spec["rows"]:
        ppid = 1 if row["orphaned"] or row["ppid"] not in world.procs else row["ppid"]
        pgid = {"guardian": 4242, "own": row["pid"], "foreign": 7777}[row["pgid"]]
        world.add(row["pid"], ppid, pgid, marked=row["marked"], hidden=row["hidden"],
                  stopped=row["stopped"], ignores_term=row["ignores_term"], respawns=row["respawns"],
                  cwd={"worktree": tree, "elsewhere": "/elsewhere", None: None}[row["cwd"]],
                  **({"uid": 0} if row["other_uid"] else {}))
    if spec["operator"]:
        world.add(OPERATOR, 1, OPERATOR, cwd=tree)          # an operator's shell in the worktree
    return world


@settings(max_examples=120, derandomize=True, deadline=None, database=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(spec=trees())
def test_no_finalized_attempt_keeps_census_pids_and_nothing_outside_the_census_is_signalled(spec):
    with tempfile.TemporaryDirectory(prefix="kill4-") as scratch, pytest.MonkeyPatch.context() as patch:
        root = Path(scratch).resolve() / "state"
        core = build_core(root)
        try:
            world = build_world(core, spec).install(patch)
            patch.setattr(daemon_module, "get_adapter", lambda provider: StubAdapter())
            patch.setattr(procs, "worktree_cwd_pids", lambda path: frozenset(
                pid for pid, proc in world.live().items()
                if proc.cwd and folders.canonical(proc.cwd).startswith(folders.canonical(path))))
            evidence = {}
            if spec["guardian"]:
                # Inspection recorded the provider as owned while the guardian led its group.
                evidence["owned_identities"] = {"4243": {"pid": 4243, "boot_id": "boot", "proc_start": STARTED}}
            if spec["counted"]:
                evidence["counted_identities"] = {
                    str(pid): {"pid": pid, "boot_id": "boot", "proc_start": world.procs[pid].start}
                    for pid in spec["counted"]}
            core.store.update_attempt(ATTEMPT, evidence_json=json.dumps(evidence))
            members: set[int] = set()
            held: set[int] = set()
            contain = core._contain

            def recorded(a):
                census = contain(a)
                members.update(census.live_pids)
                held.update(census.held_pids)
                return census
            core._contain = recorded
            if spec["ending"] == "receipt":
                atomic_publish(attempt_dir(root, JOB, 1) / "exit.json", json.dumps(
                    {"rc": 0, "signal": None, "finished_at": "2026-09-05T14:01:00Z", "wall_s": 1,
                     "child_pid": 4243}).encode())
                core.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
                core._finalize(dict(core.store.get_attempt(ATTEMPT)))
            else:
                core.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
                core._process_attempt(ATTEMPT)
            a = dict(core.store.get_attempt(ATTEMPT))
            individually = {pid for kind, pid, _ in world.signals if kind == "pid"}
            # 2. Only census members, one by one; only the recorded group, as a group.
            assert individually <= members, (individually - members, world.signals)
            assert {pgid for kind, pgid, _ in world.signals if kind == "group"} <= {4242}
            # 4. Never these, whatever the tree.
            never = {OPERATOR, os.getpid(), *held}
            assert not (individually & never), (individually & never, world.signals)
            # 3. CONT only right after the protocol's TERM reached that process.
            for n, (kind, pid, sig) in enumerate(world.signals):
                if sig == CONT:
                    earlier = world.signals[:n]
                    assert ("pid", pid, TERM) in earlier or ("group", world.procs[pid].pgid, TERM) in earlier
            # 1. No terminal state, nor a finalization under way, with live census pids.
            if a["state"] != "quarantined" and a["state"] not in ("running", "starting"):
                if not (a["state"] == "finalizing" and core.__dict__.get("_cwd_settle", {}).get(ATTEMPT)):
                    final = contain(a)
                    assert final.containment_empty, (a["state"], final.to_dict(), world.live())
        finally:
            core.store.close()
