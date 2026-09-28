"""C-5.10, C-5.11, C-5.12 as properties of running-attempt inspection, on a fake clock.

From the probe the merge review of 2026-09-26 wrote (`pacing_probe.py`). It drives
the real `Daemon._process_attempt`, `_inspect_running`, `_process_table` and
`_forget_paced`. Concurrency is modelled deterministically: while one inspection's
`ps` read runs, the clock advances tick by tick and every other attempt's pass
runs, as the worker pool would run it, so those passes find the table's lock
held or are given the last table. C-5.10's `_schedule` is modelled: a key is not
run while it runs or before its retry time, a raise sets the retry to
`worker_retry_delay(count)`, a normal return clears the count, and `DEFERRED`
neither clears it nor adds to it.

Hypothesis draws the number of attempts, how long a `ps` read takes (faster and
slower than the interval), when each attempt starts running, whether and when
its guardian dies, whether and when it ends, which of its passes raise (in
`_record_owned`, the store write of an inspection that finds the guardian
alive), including an attempt whose every pass raises, and which table reads fail.

  I1 reads of the shared table begin at least an interval apart;
  I2 an attempt is never given the same table twice, unless the pass it last had
     it in raised;
  I3 a retry is in full: the first pass after one that raised asks for a table;
  I4 a healthy attempt is given a table at least every max(interval, read) + read
     + two ticks;
  I5 a guardian that dies is found `lost` by the first good read that begins
     after the death, and a read begins within max(interval, read) and a tick;
  I6 after each control tick the pacing state holds only live attempts;
  I7 C-5.10's count for an attempt whose every inspection raises only grows. It
     went 1, 0, 1 whenever a retry met another attempt's read (fixed 2026-09-26).
"""

from __future__ import annotations

import itertools
import logging
import threading
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Lane, LaneOwner, attempt_dir
from subfleet.daemon import DEFERRED, LIVE_ATTEMPTS, Daemon, worker_retry_delay
from subfleet.guardian import atomic_publish
from subfleet.procs import Containment, InspectionError, ProcessTable
from subfleet.store import Store

STARTED = "Sat Sep  5 10:00:00 2026"
TICK, INTERVAL, HORIZON = .05, 1.0, 9.0
EPS = 1e-9


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A hand-built daemon core on a fake clock (this module's `time` only); no process is signalled."""
    clock = [1000.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(daemon_module.procs, "signal_group", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module.procs, "signal_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args, **kwargs: False)
    root = tmp_path / "state"
    root.mkdir()
    core = object.__new__(Daemon)
    core.root, core.store = root, Store(root / "state.sqlite3")
    core.stopping = threading.Event()
    core.term_grace_s, core.kill_settle_s, core.exit_settle_s, core.start_grace_s = .05, .3, .3, 10
    core._exit_settle, core._children, core._pending_launches, core._starting_deadlines = {}, {}, set(), {}
    core._inspect_next, core.inspect_interval_s, core._inspect_retry = {}, INTERVAL, set()
    core._launches, core._export_locks = {}, {}
    core.log = logging.getLogger("subfleet.test")
    core._salvage = lambda job, a: ([], None, {})
    core._record_identity = lambda *args: None
    core._export = lambda job_id: None
    core.timers = SimpleNamespace(record_auth_dead=lambda *args: None, metadata={})
    core._notify = lambda: None
    core._boundary = lambda *args: None
    core._publish = lambda role, path, contents: atomic_publish(path, contents)
    core._contain = lambda a: Containment()
    home = root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    yield core, clock, itertools.count()
    core.store.close()


attempt_plan = st.fixed_dictionaries({
    "start": st.floats(0, 3),
    "dies": st.one_of(st.none(), st.floats(.5, 6)),
    "ends": st.one_of(st.none(), st.floats(.5, 7)),
    "fail": st.sampled_from(["never", "some", "always"]),
})


@settings(max_examples=25, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(plans=st.lists(attempt_plan, min_size=1, max_size=4),
       read_s=st.sampled_from([.02, .3, .72, 1.0, 1.5]),
       fail_mask=st.lists(st.booleans(), min_size=16, max_size=16),
       table_fails=st.lists(st.booleans(), min_size=8, max_size=8))
def test_c5_12_inspection_pacing_holds_its_properties(world, monkeypatch, plans, read_s, fail_mask, table_fails):
    core, clock, serials = world
    serial = next(serials)
    core._inspect_next, core._inspect_retry = {}, set()
    core._table, core._table_lock = (None, 0.0), threading.Lock()
    t0 = clock[0]
    ids, guardians, plan_of = [], {}, {}
    for n, plan in enumerate(plans):
        aid = f"20260926-{serial:06d}-p{n}/a1"
        ids.append(aid)
        guardians[aid] = 10000 + serial * 10 + n
        plan_of[aid] = plan
    inserted, inserted_at = set(), {}
    reads = []                          # (began, expiry, failed) for each read of the shared table
    shared_callers = set()
    handed = {aid: [] for aid in ids}  # (when, table expiry) each time the attempt is given a table
    runs = {aid: [] for aid in ids}    # (asked for a table, raised) for each pass
    record_calls = {aid: 0 for aid in ids}
    violations = []
    current = []                        # the passes running: nested while a read runs
    busy, failures, retry_at, asked, lost_at = set(), {}, {}, {}, {}
    raise_counts = {aid: [] for aid in ids}

    def guardian_alive(aid, at):
        dies = plan_of[aid]["dies"]
        return dies is None or at < t0 + dies

    def snapshot():
        began = clock[0]
        shared_read = bool(current) and current[-1] in shared_callers
        index = len(reads)
        end = began + read_s
        while clock[0] + TICK <= end + EPS:            # every other attempt's passes, while `ps` runs
            clock[0] += TICK
            for other in list(ids):
                if other in inserted:
                    run(other)
        clock[0] = end
        rows = {guardians[a]: (1, guardians[a], "Ss", STARTED) for a in inserted if guardian_alive(a, began)}
        if not shared_read:                            # a table read for one guardian the shared one did not show
            return ProcessTable(rows, "boot")
        failed = bool(table_fails[index % len(table_fails)] and index % 3 == 2)
        reads.append((began, began + INTERVAL, failed))
        if failed:
            raise InspectionError("ps timed out")
        return ProcessTable(rows, "boot")
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)

    def liveness(pid, boot, start):
        aid = next(a for a in ids if guardians[a] == pid)
        return "alive" if guardian_alive(aid, clock[0]) else "dead"
    monkeypatch.setattr(daemon_module.procs, "liveness", liveness)

    def process_table(due):
        aid = current[-1]
        asked[aid] = True
        shared_callers.add(aid)
        try:
            shared = Daemon._process_table(core, due)
        finally:
            shared_callers.discard(aid)
        if shared is not None:
            # I2, as it happens: a table given before may be given again only to the pass after one that raised.
            if any(expiry == shared[1] for _, expiry in handed[aid]) and not (runs[aid] and runs[aid][-1][1]):
                violations.append(("I2 the same table twice without a raise", aid, round(clock[0] - t0, 3)))
            handed[aid].append((clock[0], shared[1]))
        return shared
    core._process_table = process_table

    def record_owned(a, table):
        aid = a["attempt_id"]
        k, record_calls[aid] = record_calls[aid], record_calls[aid] + 1
        mode = plan_of[aid]["fail"]
        if mode == "always" or (mode == "some" and fail_mask[k % len(fail_mask)]):
            raise RuntimeError("database or disk is full")
    core._record_owned = record_owned

    def run(aid):
        """One pass of `aid` as C-5.10's `_schedule` would run it."""
        if aid in busy or clock[0] < retry_at.get(aid, 0):
            return
        busy.add(aid)
        current.append(aid)
        asked[aid] = False
        deferred = raised = False
        try:
            deferred = core._process_attempt(aid) is DEFERRED
        except Exception:
            raised = True
        finally:
            current.pop()
        a = core.store.get_attempt(aid)
        if raised:
            count = failures[aid] = failures.get(aid, 0) + 1
            raise_counts[aid].append(count)
            retry_at[aid] = clock[0] + worker_retry_delay(count)
        else:
            if not deferred:
                failures.pop(aid, None)
            retry_at.pop(aid, None)
        # I3: the pass after a raise asks for a table while the attempt still runs.
        if runs[aid] and runs[aid][-1][1] and a["state"] == "running" and not asked[aid]:
            violations.append(("I3 a retry returned at the pacing gate", aid, round(clock[0] - t0, 3)))
        runs[aid].append((asked[aid], raised))
        if a["state"] == "lost" and aid not in lost_at:
            lost_at[aid] = clock[0]
        busy.discard(aid)

    horizon = t0 + HORIZON
    while clock[0] < horizon:
        now = clock[0] - t0
        for aid in ids:
            plan = plan_of[aid]
            if aid not in inserted and now >= plan["start"]:
                job = aid.split("/")[0]
                core.store.add_job(job_id=job, request_id=job, payload_digest="d", kind="run", state="running",
                                   workdir=str(core.root), prompt_path=str(core.root / "p.md"), sandbox="read-only",
                                   max_wall_s=10**6)
                core.store.add_attempt(attempt_id=aid, job_id=job, seq=1, lane_id="codex-1", model_requested="astra",
                                       state="running", guardian_pid=guardians[aid], child_pid=guardians[aid] + 1,
                                       pgid=guardians[aid], boot_id="boot", proc_start=STARTED,
                                       started_at="2026-09-26T00:00:00Z", evidence_json="{}")
                attempt_dir(core.root, job, 1).mkdir(parents=True, exist_ok=True)
                inserted.add(aid)
                inserted_at[aid] = clock[0]
            if (aid in inserted and plan["ends"] is not None and now >= plan["ends"]
                    and core.store.get_attempt(aid)["state"] == "running"):
                core.store.update_attempt(aid, state="succeeded")
        live = {a["attempt_id"] for a in core.store.query(LIVE_ATTEMPTS)}
        core._forget_paced(live)
        stray = (set(core._inspect_next) | core._inspect_retry) - live
        if stray:
            violations.append(("I6 pacing state for an attempt that is not live", sorted(stray), round(now, 3)))
        for aid in ids:
            if aid in live:
                run(aid)
        clock[0] += TICK
    for aid in ids:                                    # I6 once every attempt has ended: one more control tick
        if aid in inserted and core.store.get_attempt(aid)["state"] == "running":
            core.store.update_attempt(aid, state="succeeded")
    core._forget_paced({a["attempt_id"] for a in core.store.query(LIVE_ATTEMPTS)})
    if (set(core._inspect_next) | core._inspect_retry) & set(ids):
        violations.append(("I6 pacing state left after every attempt ended", sorted(core._inspect_next)))

    for (b1, _, _), (b2, _, _) in zip(reads, reads[1:]):
        if b2 - b1 < INTERVAL - EPS:
            violations.append(("I1 reads less than an interval apart", round(b1 - t0, 3), round(b2 - t0, 3)))
    slowest = max(INTERVAL, read_s)
    bound = slowest + read_s + 2 * TICK + EPS
    for aid in ids:
        plan = plan_of[aid]
        if aid not in inserted:
            continue
        if plan["fail"] == "never":                    # I4
            stop = t0 + min(x for x in (plan["dies"], plan["ends"], HORIZON) if x is not None)
            times = [t0 + plan["start"]] + [at for at, _ in handed[aid]]
            times = [t for t in times if t <= stop] + [stop]
            for earlier, later in zip(times, times[1:]):
                if later - earlier > bound and later - (t0 + plan["start"]) > bound:
                    violations.append(("I4 a healthy attempt not given a table in time", aid,
                                       round(earlier - t0, 3), round(later - t0, 3)))
                    break
        if (plan["fail"] == "never" and plan["dies"] is not None      # I5
                and (plan["ends"] is None or plan["ends"] > plan["dies"] + 3 * bound)):
            died = max(t0 + plan["dies"], inserted_at[aid])
            after = [r for r in reads if r[0] >= died - EPS]
            found = lost_at.get(aid)
            if found is not None and found <= died + slowest + read_s + TICK + EPS:
                pass                                   # found within the bound, perhaps by a fresh read
            elif died + 3 * bound < horizon and after:
                if after[0][0] - died > slowest + TICK + EPS:
                    violations.append(("I5 no read began in time after a death", aid, round(died - t0, 3)))
                good = next((r for r in after if not r[2]), None)
                if good is not None and (found is None or found > good[0] + read_s + TICK + EPS):
                    violations.append(("I5 a death not found by the first good read after it", aid,
                                       round(died - t0, 3), round(good[0] - t0, 3)))
        if plan["fail"] == "always":                   # I7
            if raise_counts[aid] != list(range(1, len(raise_counts[aid]) + 1)):
                violations.append(("I7 C-5.10's count restarted between raises", aid, raise_counts[aid][:12]))
    clock[0] += 100
    assert not violations, (dict(read_s=read_s, plans=plans), violations[:6])
