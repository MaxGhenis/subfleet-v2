"""Differential check for C-8.6 against the code before it.

On pairs of identical worlds, a pass of this tree's retention and a pass of the
retention at `--base` must give the same result in two cases:

- no retire request stands (invariant 4: without requests a pass is unchanged);
- requests stand only for jobs a pin, a deferral or an unfinished state keeps
  (invariant 1: a request lifts no pin, deferral or state). Here one difference
  is intended: a pass now looks at a requested job its pool's budget would keep,
  so a deferral on it shows in `deferred` and `protected`. Those entries, and only
  for requested jobs, are set aside before the comparison.

The worlds are random: up to nine jobs in both pools, any state, some pinned by
the conversation service, some deferred, with random budgets and batch. Their
jobs have job folders and no worktree, so no git runs. Results compare whole,
with each world's root spelled `<root>`, except the `requested` key C-8.6 adds.
Run it under the suite hold, with a private temporary directory::

    lockf -k <suite-lock> python tools/retire_requests_differential.py --base 18aa2a695 --tmp <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import types

from subfleet import retention
from subfleet.store import Store, utc_now
from tests.unit.retention_world import Clock

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = ("succeeded", "failed", "cancelled", "lost")
HUGE = 10 ** 15


def at_revision(revision: str) -> types.ModuleType:
    """`subfleet/retention.py` as it was at `revision`, beside this tree's other modules."""
    source = subprocess.run(["git", "-c", "core.fsmonitor=false", "show", f"{revision}:subfleet/retention.py"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    module = types.ModuleType("subfleet._retention_at_" + revision)
    module.__package__ = "subfleet"
    module.__file__ = str(ROOT / "subfleet" / f"retention@{revision}.py")
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def build(folder: Path, jobs: list[dict], requests: set[str]) -> tuple[Path, Store]:
    root = folder / "state"
    (root / "jobs").mkdir(parents=True)
    (root / "worktrees").mkdir()
    store = Store(root / "state.sqlite3")
    for n, job in enumerate(jobs):
        created = f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00Z"
        store.add_job(job_id=job["id"], request_id="req-" + job["id"], payload_digest="d",
                      kind="turn" if job["pool"] == "turn" else "dispatch", workdir="/nonexistent-workdir",
                      prompt_path="/prompt", sandbox="read-only", state=job["state"], created_at=created)
        (root / "jobs" / job["id"]).mkdir()
        (root / "jobs" / job["id"] / "stdout").write_bytes(b"output of " + job["id"].encode() * (n + 1))
        if job["id"] in requests:
            store.conn.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                               (utc_now(), retention.RETIRE_REQUESTED, job["id"], json.dumps({"created_at": created})))
    return root, store


def one_pass(module: types.ModuleType, folder: Path, jobs: list[dict], requests: set[str],
             budgets: dict[str, int], batch: int) -> str:
    root, store = build(folder, jobs, requests)
    try:
        state, clock = module.RetentionState(), Clock()
        for job in jobs:
            if job["deferred"]:
                state.defer(job["id"], 600, "busy", clock())
        pinned = {job["id"] for job in jobs if job["pinned"]}
        result = module.maintenance(store, root, max_jobs=budgets["detached"], max_bytes=HUGE,
                                    turn_max_jobs=budgets["turn"], turn_max_bytes=HUGE, turn_keep_s=0,
                                    pins=lambda: pinned, state=state, clock=clock, batch=batch,
                                    holders=lambda watches, **_: {})
        result.pop("requested", None)
        result["left"] = sorted(row["job_id"] for row in store.query("SELECT job_id FROM jobs"))
        return json.dumps(result, sort_keys=True, default=str).replace(str(root), "<root>")
    finally:
        store.close()


def set_aside(now: str, before: str, requests: set[str]) -> tuple[str, int]:
    """`now` without the deferrals a pass reports only because a job was requested."""
    result, earlier = json.loads(now), json.loads(before)
    seen = {job_id for job_id in set(result["deferred"]) - set(earlier["deferred"]) if job_id in requests}
    for job_id in seen:
        del result["deferred"][job_id]
        result["protected"].remove(job_id)
    return json.dumps(result, sort_keys=True), len(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="the revision before C-8.6")
    parser.add_argument("--tmp", type=Path, required=True, help="a private directory; emptied per world")
    parser.add_argument("--examples", type=int, default=200)
    parser.add_argument("--seed", type=int, default=86)
    parser.add_argument("--control", action="store_true",
                        help="request jobs a pass may retire instead, and pass only if some world differs "
                             "(the comparison can see a difference)")
    args = parser.parse_args()
    base = at_revision(args.base)
    work = args.tmp / "retire-requests-differential"
    work.mkdir(parents=True)            # this run's own: it deletes only worlds it made here
    counts: dict[str, int] = {}
    pruned = differs = reported = 0
    for index in range(args.examples):
        rng = random.Random(args.seed * 1_000_003 + index)
        jobs = [{"id": f"job-{n}", "state": rng.choice([*TERMINAL, "running", "queued"]),
                 "pool": rng.choice(["detached", "turn"]), "pinned": rng.random() < .25,
                 "deferred": rng.random() < .2} for n in range(rng.randint(1, 9))]
        kept = [job["id"] for job in jobs if job["pinned"] or job["deferred"] or job["state"] not in TERMINAL]
        free = [job["id"] for job in jobs if job["id"] not in kept]
        if args.control:
            case, pool = "requests on retirable jobs", free
        else:
            case, pool = ("no request", []) if index % 2 == 0 or not kept else ("requests only on kept jobs", kept)
        requests = {job_id for job_id in pool if rng.random() < .7} or set(pool[:1])
        budgets = {"detached": rng.randint(0, 9), "turn": rng.randint(0, 9)}
        batch = rng.randint(1, 9)
        world = work / f"w{index}"
        now = one_pass(retention, world / "a", jobs, requests, budgets, batch)
        before = one_pass(base, world / "b", jobs, requests, budgets, batch)
        shutil.rmtree(world)
        counts[case] = counts.get(case, 0) + 1
        pruned += len(json.loads(now)["pruned"])
        if case == "requests only on kept jobs":
            now, seen = set_aside(now, before, requests)
            before = json.dumps(json.loads(before), sort_keys=True)
            reported += seen
        if now != before:
            differs += 1
            if not args.control:
                print(f"DIFFERS at example {index} ({case}): jobs={jobs} requests={sorted(requests)} "
                      f"budgets={budgets} batch={batch}\n  now:    {now}\n  before: {before}", flush=True)
                return 1
    work.rmdir()
    cases = ", ".join(f"{count} {case}" for case, count in counts.items())
    if args.control:
        print(f"control: {differs} of {args.examples} worlds differ from {args.base} ({cases}; "
              f"{pruned} jobs retired in all)", flush=True)
        return 0 if differs else 1
    print(f"{args.examples} worlds agree with {args.base} ({cases}; {pruned} jobs retired in all; "
          f"{reported} deferrals of requested jobs set aside)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
