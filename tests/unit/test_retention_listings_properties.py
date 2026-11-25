"""Invariants 3–5 of one listing a pass (D-HOLDERS-LSOF1), against a plain model.

Hypothesis draws runs of passes over three worktree-less jobs. Each pass draws a
batch, whether the daemon restarted before it (a fresh `RetentionState`), whether
the stamp clock stepped back at that restart, which jobs the listing finds held,
whether the listing fails, and whether the pass is cut short in its middle
(review r1, finding 6): the daemon dies once the first job's journal says
`committing` (recovery puts it back to `archived`), or once a quarantine's moves
are done and its journal still says `quarantining` (recovery finishes the
quarantine), and the next pass is a restart; or a cancel arrives between the
quarantines and the listing (the listing fails, the pass reports `cancelled`, and
every job it was for keeps its journal for a later listing). The model is the order of what the real pass did,
recorded by wrappers round the retirement's steps and the reader: quarantines,
archives, listings (each with the jobs it serves and their journals then),
rollbacks and commits. It knows nothing of stamps or of how the pass decides.
Checked as the events arrive and after every pass:

3. a listing serves a job's check 1 only after its quarantine and its check 2
   only after its archive, in the same retirement; the journal's persisted stamp
   of that event is below the listing's;
4. a job commits only after a listing that began after its archive served its
   check 2 and found it free, and that listing is the pass's own: a job recovered
   from `committing` takes check 2 again;
5. a pass lists at most once, and exactly when a job needed a check (one was
   quarantined in it, or waited archived, or waited for check 1).

And while a job waits archived, the pass reports more work and progress, so the
daemon's next pass comes at `RETENTION_CATCH_UP_S`. A final drain with free
listings retires every job, so no job is stranded short of its check 2.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import threading

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet.retention_holders import ScanFailed
from tests.unit.retention_world import Clock, World, trust_temporary_directories

EXAMPLES = int(os.environ.get("RETENTION_PROP_EXAMPLES", "10"))
JOBS = ("job-a", "job-b", "job-c")

plan = st.fixed_dictionaries({
    "batch": st.integers(1, 3),
    "restart": st.booleans(),
    "step_back": st.booleans(),
    "held": st.sets(st.sampled_from(JOBS), max_size=2),
    "fail": st.sampled_from([False, False, False, True]),
    "cut": st.sampled_from([None, None, None, None, "committing", "quarantining", "cancel"]),
})


class Crash(BaseException):
    """The daemon dies here: nothing in the pass catches it."""


def journal(root: Path, job_id: str) -> dict | None:
    path = rarch.journal_path(root, job_id)
    return json.loads(path.read_text()) if path.exists() else None


class Model:
    """Each job's retirement as the recorded events tell it."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.events: list[tuple] = []
        self.store = None
        #: The pass's drawn cut while it has not happened yet (`plan["cut"]`).
        self.cut: str | None = None

    def fresh(self, job_id: str) -> dict:
        self.jobs[job_id] = {"quarantined": False, "check1": False, "archived": False, "check2": False}
        return self.jobs[job_id]

    def get(self, job_id: str) -> dict:
        return self.jobs.get(job_id) or self.fresh(job_id)

    def needs_check(self) -> bool:
        return any(job["archived"] or (job["quarantined"] and not job["check1"]) for job in self.jobs.values())


def wrap(monkeypatch, model: Model) -> None:
    real_quarantine, real_archive = rarch.Retirement.quarantine, rarch.Retirement.archive
    real_commit, real_rollback = rarch.Retirement.commit, rarch.Retirement.rollback
    real_save = rarch.Retirement.save

    def quarantine(self, *args, **kwargs):
        out = real_quarantine(self, *args, **kwargs)
        if self.state == "quarantined":
            model.fresh(self.job_id)["quarantined"] = True
            model.events.append(("quarantined", self.job_id))
        return out

    def archive(self, *args, **kwargs):
        out = real_archive(self, *args, **kwargs)
        if self.state == "archived":
            job = model.get(self.job_id)
            assert job["quarantined"] and job["check1"], ("archived without check 1", self.job_id, model.events)
            job.update(archived=True, check2=False)        # a listing before the archive does not count
            model.events.append(("archived", self.job_id))
        return out

    def commit(self, *args, **kwargs):
        job = model.get(self.job_id)
        # Invariant 4: the final check and commit only after a qualifying check 2.
        assert job["archived"] and job["check2"], ("commit without check 2", self.job_id, model.events)
        out = real_commit(self, *args, **kwargs)
        if model.store.get_job(self.job_id) is None:          # committed, not put back
            model.jobs.pop(self.job_id, None)
            model.events.append(("commit", self.job_id))
        return out

    def rollback(self, *args, **kwargs):
        out = real_rollback(self, *args, **kwargs)
        model.fresh(self.job_id)
        model.events.append(("rollback", self.job_id))
        return out

    def save(self, **changes):
        cut = model.cut
        if cut == "committing" and changes.get("state") == "committing":
            real_save(self, **changes)          # the journal says `committing`; the rows are still there
            model.cut = None
            model.events.append(("crash", self.job_id, "committing"))
            raise Crash("after committing")
        if cut == "quarantining" and changes.get("state") == "quarantined" \
                and (self.journal or {}).get("state") == "quarantining":
            model.cut = None                    # the moves are done; `quarantined` is never written
            model.events.append(("crash", self.job_id, "quarantining"))
            raise Crash("before quarantined")
        return real_save(self, **changes)

    real_listing = retention._Pass._holder_listing

    def holder_listing(self, retirements):
        if model.cut == "cancel":
            # Between the pass's quarantines and its listing.
            model.cut = None
            model.events.append(("cancel",))
            self.cancel.set()
        return real_listing(self, retirements)

    monkeypatch.setattr(rarch.Retirement, "save", save)
    monkeypatch.setattr(retention._Pass, "_holder_listing", holder_listing)
    monkeypatch.setattr(rarch.Retirement, "quarantine", quarantine)
    monkeypatch.setattr(rarch.Retirement, "archive", archive)
    monkeypatch.setattr(rarch.Retirement, "commit", commit)
    monkeypatch.setattr(rarch.Retirement, "rollback", rollback)


def _plan(cut=None, *, batch=1, restart=False):
    return {"batch": batch, "restart": restart, "step_back": True, "held": set(), "fail": False, "cut": cut}


@settings(max_examples=EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(plans=st.lists(plan, min_size=1, max_size=6))
# Each cut at least once, whatever is drawn (review r1, finding 6): a's commit dies
# at `committing` and the restart's listing serves its check 2 again; a's
# quarantine dies with its moves done and recovery finishes it; a cancel stops the
# listing for b's check 1 and a's check 2.
@example(plans=[_plan(), _plan("committing"), _plan()])
@example(plans=[_plan("quarantining"), _plan(), _plan()])
@example(plans=[_plan(), _plan("cancel"), _plan()])
def test_listings_follow_their_events_and_every_commit_takes_check_2(tmp_path, monkeypatch, plans):
    # One patch context an example: the function's monkeypatch outlives examples,
    # and wrappers stacked on an earlier example's would report to its model.
    with monkeypatch.context() as patch:
        trust_temporary_directories(patch)
        model = Model()
        wrap(patch, model)
        _run(Path(tempfile.mkdtemp(dir=tmp_path)), model, plans)


def _run(base: Path, model: Model, plans: list[dict]) -> None:
    w = World(base)
    model.store = w.store
    try:
        for index, name in enumerate(JOBS):
            w.job(name, worktree=False, created=f"2026-09-01T00:0{index}:00Z")
        clock, stamps = Clock(), [10 ** 15]
        state = retention.RetentionState()
        current: dict = {}

        def stamp() -> int:
            stamps[0] += 1000
            return stamps[0]

        def reader(watches, cancel=None, **_):
            at_listing = {job: journal(w.root, job) for job in watches}
            listing = {"first": set(), "second": set(), "journals": at_listing}
            for job_id, record in at_listing.items():
                job = model.get(job_id)
                if record["state"] == "archived":
                    # Invariant 3, check 2: served only after this retirement's archive.
                    assert job["archived"], ("check 2 before the archive", job_id, model.events)
                    listing["second"].add(job_id)
                else:
                    # Invariant 3, check 1: served only after this retirement's quarantine.
                    assert record["state"] == "quarantined" and job["quarantined"] and not job["archived"], \
                        ("check 1 out of order", job_id, record["state"], model.events)
                    listing["first"].add(job_id)
            current.setdefault("listings", []).append(listing)
            model.events.append(("listing", tuple(sorted(watches))))
            if cancel is not None and cancel.is_set():
                raise ScanFailed("lsof cancelled")          # as `lsof_holders` does
            if current["plan"]["fail"]:
                raise ScanFailed("lsof exited 1: fixture")
            held = {job_id: [f"pid 1 (fixture): open for writing {job_id}"]
                    for job_id in watches if job_id in current["plan"]["held"]}
            for job_id in set(watches) - set(held):
                model.get(job_id)["check2" if job_id in listing["second"] else "check1"] = True
            return held

        crashed = [False]

        def one_pass(plan: dict) -> dict | None:
            nonlocal state
            if plan["restart"] or crashed[0]:
                state = retention.RetentionState()      # a crash is always followed by a restart
                if plan["step_back"]:
                    stamps[0] -= 10 ** 12
            crashed[0] = False
            current.clear()
            current["plan"] = plan
            model.cut = plan["cut"]
            for job in model.jobs.values():
                job["check2"] = False       # invariant 4: only this pass's listing lets a job finish
            needed_before = model.needs_check()
            mark = len(model.events)
            cancel = threading.Event()
            try:
                result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=reader,
                                               clock=clock, batch=plan["batch"], state=state, stamp=stamp,
                                               cancel=cancel)
            except Crash:
                crashed[0] = True
                assert len(current.get("listings", [])) <= 1
                clock.now += 1
                return None
            listings = current.get("listings", [])
            quarantined = any(event[0] == "quarantined" for event in model.events[mark:])
            # Invariant 5: at most one listing a pass, and one exactly when a check was needed.
            assert len(listings) == result["listings"] <= 1, (listings, result["listings"])
            assert result["listings"] == int(needed_before or quarantined), (needed_before, quarantined, result)
            if ("cancel",) in model.events[mark:] and listings:
                assert result.get("interrupted") == "cancelled", result
            if result.get("interrupted") == "cancelled":
                # A cancel before the listing: it fails, serves nothing, and the pass
                # stops; every job it was for keeps its journal for a later listing.
                assert ("cancel",) in model.events[mark:] and result["more"] is True, result
                for only in listings:
                    for job_id in only["first"]:
                        record = journal(w.root, job_id)
                        assert record["state"] == "quarantined" and not record.get("check1"), (job_id, record)
                    for job_id in only["second"]:
                        assert journal(w.root, job_id)["state"] == "archived", job_id
                clock.now += 1
                return result
            if listings:
                record, (only,) = result["listing"], listings
                assert set(record["first"]) == only["first"] and set(record["second"]) == only["second"]
                for job_id, at_listing in only["journals"].items():
                    key = "archived_stamp" if job_id in only["second"] else "quarantined_stamp"
                    # Invariant 3 by stamps: the persisted event stamp precedes the listing's.
                    assert isinstance(at_listing[key], int) and at_listing[key] < record["stamp"]
            waiting = [job_id for job_id in JOBS if (journal(w.root, job_id) or {}).get("state") == "archived"]
            assert sorted(result["awaiting_check2"]) == waiting
            if waiting:
                assert result["more"] is True and result["progressed"] is True
            clock.now += 1
            return result

        for drawn in plans:
            one_pass(drawn)
        # Drain: deferrals expire, listings find nothing held, and every job retires.
        free = {"batch": 3, "restart": False, "step_back": False, "held": set(), "fail": False, "cut": None}
        for _ in range(2 * len(JOBS) + 2):
            if not w.store.list_jobs():
                break
            # Every deferral has expired, also one a restart after a crash recalled
            # from an idle journal (its wall-clock time left, on the pass's clock).
            clock.now += 10 ** 7
            one_pass(free)
        assert w.store.list_jobs() == [], model.events
        assert sorted(event[1] for event in model.events if event[0] == "commit") == sorted(JOBS)
    finally:
        w.close()
