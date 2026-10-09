"""One bounded foreground shard of the serial fixed/fresh process-world proof.

Run fixed batch indices 0..99 and fresh indices 0..24 for the default 2,000/500
worlds. Every shard has a saved deterministic seed and a finite Hypothesis
deadline. The parent waits for its owned child and kills that child's process
group on timeout or interruption; it never leaves a proof detached.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, signal, sys
from pathlib import Path
import pytest
from tests.fake.test_quarantine_process_world import ProcessWorldMachine

def interrupted(signum, frame):
    raise KeyboardInterrupt(f"proof child interrupted by signal {signum}")
signal.signal(signal.SIGTERM, interrupted)

progress = Path(sys.argv[1])
original = ProcessWorldMachine.teardown
completed = 0
def teardown(self):
    global completed
    try:
        return original(self)
    finally:
        completed += 1
        if completed % 20 == 0:
            # Fixture completions include invalid draws. The final Hypothesis
            # statistics are authoritative for the valid world count.
            progress.write_text(json.dumps({"examples_requested": int(os.environ["SF_WORLD_EXAMPLES"]),
                                            "fixtures_completed": completed}))
ProcessWorldMachine.teardown = teardown
try:
    result = pytest.main(["--assert=plain", "-q", "tests/fake/test_quarantine_process_world.py::TestProcessWorld",
                         "--hypothesis-seed=" + sys.argv[2], "--hypothesis-show-statistics"])
finally:
    ProcessWorldMachine.close_pool()
sys.exit(result)
'''


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "subfleet/procs.py", "subfleet/daemon.py", "tests/fake/test_quarantine_process_world.py",
        "tests/fake/quarantine_world_pool.py")}


def shard_seed(master, index):
    # The first fixed shard retains the historical seed. Remaining seeds are
    # reproducible and distinct, so splitting never repeats the same worlds.
    if index == 0:
        return master
    return int.from_bytes(hashlib.sha256(f"{master}:{index}".encode()).digest()[:8], "big")


def stop_owned_child(child, *, grace=True):
    for sig in ((signal.SIGTERM, signal.SIGKILL) if grace else (signal.SIGKILL,)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        # A descendant may outlive the group leader; finish the exact group
        # even when its leader has already reaped after SIGTERM.
        if sig == signal.SIGTERM:
            continue
        return
    child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("fixed", "fresh"), default="fixed")
    parser.add_argument("--batch-index", type=int, default=0)
    parser.add_argument("--batch-examples", type=int, default=20)
    parser.add_argument("--fixed-examples", type=int, default=2000)
    parser.add_argument("--fresh-examples", type=int, default=500)
    parser.add_argument("--fixed-seed", type=int, default=131)
    parser.add_argument("--fresh-seed", type=int)
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--steps", type=int, default=25)
    args = parser.parse_args()
    if not (1 <= args.timeout_s <= 1480 and 1 <= args.deadline_ms <= 60000 and
            min(args.batch_examples, args.fixed_examples, args.fresh_examples, args.steps) > 0):
        parser.error("positive bounds required; timeout <=1480s and deadline <=60000ms")
    evidence = ROOT / os.environ.get("SF_WORLD_EVIDENCE", "docs/reports/pr131-fix9")
    evidence.mkdir(parents=True, exist_ok=True)
    config_path = evidence / "proof-config.json"
    prior = json.loads(config_path.read_text()) if config_path.exists() else {}
    fresh_seed = args.fresh_seed if args.fresh_seed is not None else prior.get("fresh_seed", secrets.randbits(64))
    before = source_hashes()
    config = {"fixed_examples": args.fixed_examples, "fixed_seed": args.fixed_seed,
              "fresh_examples": args.fresh_examples, "fresh_seed": fresh_seed,
              "batch_examples": args.batch_examples, "stateful_step_count": args.steps,
              "deadline_ms": args.deadline_ms, "timeout_s": args.timeout_s, "source_hashes": before}
    shards = []
    for kind in ("fixed", "fresh"):
        for index, offset in enumerate(range(0, config[kind + "_examples"], args.batch_examples)):
            shards.append({"kind": kind, "index": index,
                           "examples": min(args.batch_examples, config[kind + "_examples"] - offset),
                           "seed": shard_seed(config[kind + "_seed"], index)})
    config["shards"] = shards
    if prior and prior != config:
        parser.error("saved proof configuration differs; use a fresh evidence directory")
    selected = [s for s in shards if s["kind"] == args.kind and s["index"] == args.batch_index]
    if not selected:
        parser.error("batch index outside the configured proof")
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    shard = selected[0]
    name = f"model-{args.kind}-{args.batch_index:02d}-{shard['examples']}"
    progress = evidence / (name + "-progress.json")
    filename = evidence / (name + ".txt")
    result_path = evidence / (name + "-result.json")
    progress.write_text(json.dumps({**shard, "fixtures_completed": 0}))
    print(f"RUN {name}: seed {shard['seed']}, wall bound {args.timeout_s}s", flush=True)
    returncode = 124
    try:
        with filename.open("w") as log:
            child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(progress), str(shard["seed"])],
                cwd=ROOT, env={**os.environ, "SF_WORLD_EXAMPLES": str(shard["examples"]),
                               "SF_WORLD_STEPS": str(args.steps), "SF_WORLD_DEADLINE_MS": str(args.deadline_ms),
                               "SF_WORLD_POOL": "1",
                               "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            try:
                returncode = child.wait(timeout=args.timeout_s)
            except subprocess.TimeoutExpired:
                stop_owned_child(child)
                log.write(f"\nPROOF WALL BOUND: {args.timeout_s}s; owned process group reaped\n")
            except BaseException:
                stop_owned_child(child, grace=False)
                raise
        quiet = returncode == 0 and f"{shard['examples']} passing, 0 failing" in filename.read_text()
        result_path.write_text(json.dumps({**shard, "quiet": quiet, "returncode": returncode,
                                          "source_hashes": before}, indent=2) + "\n")
        print(f"{'QUIET' if quiet else 'FAILED'} {name}: exit {returncode}", flush=True)
    finally:
        after = source_hashes()
        assert before == after, "proof changed production or model bytes"
        (evidence / (name + "-restoration.json")).write_text(json.dumps(after, indent=2) + "\n")
        progress.unlink(missing_ok=True)
    totals = {"fixed": 0, "fresh": 0}
    for s in shards:
        result = evidence / f"model-{s['kind']}-{s['index']:02d}-{s['examples']}-result.json"
        if result.exists():
            observed = json.loads(result.read_text())
            if observed.get("quiet") and observed.get("source_hashes") == before and observed.get("seed") == s["seed"]:
                totals[s["kind"]] += s["examples"]
    (evidence / "proof-totals.json").write_text(json.dumps(totals, indent=2) + "\n")
    print(f"VERIFIED fixed {totals['fixed']}/{args.fixed_examples}; fresh {totals['fresh']}/{args.fresh_examples}", flush=True)
    return int(not quiet)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"proof interrupted by signal {signum}")
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    finally:
        signal.signal(signal.SIGTERM, previous)
