"""How many process starts one inspection interval costs a UUID-recorded attempt and a
legacy (kern.boottime) attempt on the PR head, with `procs._read` faked so nothing
real is spawned. Mirrors tests/unit/test_daemon_settle.py's daemon fixture.

The probe of the final review of PR #37 (2026-09-26), committed as it was run for
`docs/reports/2026-09-24-process-inspection-cost/legacy-reads.txt`, only its location
(and so `REPO`) and this docstring changed. It imports the tree it sits in.

Usage, from anywhere: uv run python tools/probe_legacy_reads.py"""
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import daemon as daemon_module          # noqa: E402
from subfleet import procs                           # noqa: E402
from subfleet.contracts import Credential, Lane, LaneOwner, attempt_dir  # noqa: E402
from subfleet.daemon import Daemon                   # noqa: E402
from subfleet.store import Store                     # noqa: E402

STARTED = "Sat Sep  5 10:00:00 2026"
UUID = "11111111-1111-4111-8111-111111111111"
SECONDS = "1726000000"
reads: list[str] = []


def fake_read(argv, *, empty_ok=False):
    name = os.path.basename(argv[0])
    tail = argv[-1]
    reads.append(f"{name} {' '.join(argv[1:])}")
    if name == "sysctl" and tail == "kern.bootsessionuuid":
        return UUID + "\n"
    if name == "sysctl" and tail == "kern.boottime":
        return "{ sec = 1726000000, usec = 0 } Sat Sep 10 10:00:00 2024\n"
    if name == "ps" and argv[1] == "-axo":
        return (f"4242 1 4242 Ss {STARTED}\n4243 4242 4242 S {STARTED}\n"
                f"5252 1 5252 Ss {STARTED}\n5253 5252 5252 S {STARTED}\n")
    if name == "ps" and argv[1] == "-p":
        if tail == "lstart=":
            return STARTED + "\n"
        if tail == "stat=":
            return "S\n"
    raise AssertionError(argv)


def make_core(root: Path) -> Daemon:
    core = object.__new__(Daemon)
    core.root, core.store = root, Store(root / "state.sqlite3")
    core.stopping = threading.Event()
    core.term_grace_s, core.kill_settle_s, core.exit_settle_s = .05, .3, .3
    core._exit_settle = {}
    core._children, core._pending_launches, core._starting_deadlines = {}, set(), {}
    core._inspect_next, core.inspect_interval_s = {}, 1.0
    core._table, core._table_lock = (None, 0.0), threading.Lock()
    core._launches, core._export_locks = {}, {}
    core.log = logging.getLogger("probe")
    core._salvage = lambda job, a: ([], None)
    core._export = lambda job_id: None
    core.timers = SimpleNamespace(record_auth_dead=lambda *args: None, metadata={})
    core._notify = lambda: None
    core._boundary = lambda *args: None
    core._contain = lambda a: (_ for _ in ()).throw(AssertionError("no census expected"))
    home = root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    return core


def add_attempt(core, job, guardian, boot):
    core.store.add_job(job_id=job, request_id=job, payload_digest="d", kind="run", state="running",
                       workdir=str(core.root), prompt_path=str(core.root / "p.md"), sandbox="read-only")
    aid = job + "/a1"
    core.store.add_attempt(attempt_id=aid, job_id=job, seq=1, lane_id="codex-1", model_requested="astra",
                           state="running", guardian_pid=guardian, child_pid=guardian + 1, pgid=guardian,
                           boot_id=boot, proc_start=STARTED, started_at="2026-09-05T14:00:00Z", evidence_json="{}")
    attempt_dir(core.root, job, 1).mkdir(parents=True)
    return aid


def main():
    procs._read = fake_read
    procs.forget_boot_id()
    with tempfile.TemporaryDirectory(prefix="sfp-", dir="/tmp") as d:
        root = Path(d) / "state"
        root.mkdir()
        core = make_core(root)
        uuid_aid = add_attempt(core, "20260905-100000-uuid", 4242, UUID)
        legacy_aid = add_attempt(core, "20260905-100000-legacy", 5252, SECONDS)
        for label, aid in (("uuid", uuid_aid), ("legacy", legacy_aid)):
            for interval in range(3):
                reads.clear()
                core._inspect_next.pop(aid, None)
                core._table = (None, 0.0)                  # a new interval: the shared table is read afresh
                procs.forget_boot_id()                     # worst case: the 5 s boot cache has expired
                core._process_attempt(aid)
                shared = [r for r in reads if r.startswith("ps -axo")]
                print(f"{label} interval {interval}: {len(reads)} process starts; "
                      f"shared table reads={len(shared)}; all={reads}")
            a = core.store.get_attempt(aid)
            print(f"   state={a['state']} owned={sorted(json.loads(a['evidence_json']).get('owned_identities', {}))}")
        # And with the boot cache warm (the common case within 5 s):
        for label, aid in (("uuid", uuid_aid), ("legacy", legacy_aid)):
            reads.clear()
            core._inspect_next.pop(aid, None)
            core._table = (None, 0.0)
            core._process_attempt(aid)
            print(f"{label} warm boot cache: {len(reads)} process starts: {reads}")
        core.store.close()


if __name__ == "__main__":
    main()
