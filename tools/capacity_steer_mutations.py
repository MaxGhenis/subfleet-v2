"""Mutation check for the model-capacity steer (C-9.5, D-CAP1).

Each mutation removes one rule of the steer from the code, and the steer's tests must fail
under it. Run from the repository root (or from a `git archive` of it), with the mutated
files clean:

    uv run python tools/capacity_steer_mutations.py

With `SUBFLEET_SUITE_LOCK` set to a lock file path (the hub's
`/private/tmp/claude-501/subfleet-suites.lock`), each pytest run waits for its own hold of
that lock (`/usr/bin/lockf -k`). It rewrites `subfleet/daemon.py`, `subfleet/scheduler.py`
or `subfleet/adapters/codex.py` in place, one mutation at a time, and restores the file
whatever happens. It exits 1 if a mutation survives or no longer applies. It is a
development check, not part of the suite: it runs the tests once per mutation.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

DAEMON, SCHEDULER, CODEX = Path("subfleet/daemon.py"), Path("subfleet/scheduler.py"), Path("subfleet/adapters/codex.py")
FAKE, ROUTE = "tests/fake/test_capacity_steer_daemon.py", "tests/unit/test_capacity_steer_route.py"
ADAPTER, E2E = "tests/unit/test_codex_adapter.py", "tests/e2e/test_capacity_steer_e2e.py"

ROUTE_JOB = "        steer = self._capacity_steer(job, previous)\n        return {**job, \"capacity_steer\": steer} if steer else job\n"
NO_PIN = "and not _capacity_steer_of(last[\"evidence_json\"]) and self._retry_pair_routable(last)"
STEERED = "            steered = (not lost and outcome.cls == OutcomeClass.TRANSIENT\n"
REORDER = "                if policy[\"models\"][short][\"id\"] in steer} if steer and capacity_steerable(job) else {})\n"
STEERABLE = ("    return not job.get(\"pinned_model\") and not job.get(\"pinned_lane\") "
             "and job.get(\"kind\") not in _UNSTEERED_KINDS\n")
EXPIRY = "            if found is None or _epoch(found[\"until\"]) is None or _epoch(found[\"until\"]) <= now.timestamp():\n"

#: name: ([(file, the rule's code, what replaces it)], the tests that must fail)
MUTATIONS = {
    # The whole steer, classification kept: the incident's behaviour.
    "no steer: the same pair 60 s on, as before D-CAP1": (
        [(DAEMON, ROUTE_JOB, "        return job\n"), (DAEMON, NO_PIN, "and self._retry_pair_routable(last)"),
         (DAEMON, STEERED, "            steered = False and (not lost and outcome.cls == OutcomeClass.TRANSIENT\n")],
        (FAKE, E2E)),
    "the walk is not reordered": ([(SCHEDULER, REORDER, REORDER.replace("if steer and capacity_steerable(job)", "if False"))],
                                  (FAKE, ROUTE)),
    "the route is never handed the steer": ([(DAEMON, ROUTE_JOB, "        return job\n")], (FAKE,)),
    "a steered refusal keeps the same-lane pin": ([(DAEMON, NO_PIN, "and self._retry_pair_routable(last)")], (FAKE,)),
    "a steered refusal waits 60 s": (
        [(DAEMON, "TRANSIENT_RETRY_DELAY_S if outcome.cls == OutcomeClass.TRANSIENT and not steered\n",
          "TRANSIENT_RETRY_DELAY_S if outcome.cls == OutcomeClass.TRANSIENT\n")], (FAKE,)),
    "a pinned job is steered": ([(SCHEDULER, STEERABLE, "    return True\n")], (FAKE, ROUTE)),
    "a steer never ends": ([(DAEMON, EXPIRY, "            if found is None:\n")], (FAKE,)),
    "a steer holds a job when no other model can take it": (
        [(SCHEDULER, "        chain = [short for short in chain if short not in steered] + list(steered)\n",
          "        chain = [short for short in chain if short not in steered] or chain\n")], (FAKE, ROUTE)),
    "the classifier misses the refusal": ([(CODEX, "            if MODEL_CAPACITY_RE.search(text):\n",
                                            "            if False and MODEL_CAPACITY_RE.search(text):\n")], (ADAPTER, FAKE)),
    "the first transient text answers admission": (
        [(CODEX, "            if MODEL_CAPACITY_RE.search(text):\n                evidence[\"admission\"] = event or text\n",
          "            if MODEL_CAPACITY_RE.search(text):\n                evidence[\"admission\"] = signals[0][0] or signals[0][1]\n")],
        (ADAPTER,)),
}


def pytest(tests: tuple[str, ...]) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]
    lock = os.environ.get("SUBFLEET_SUITE_LOCK")
    return subprocess.run(["/usr/bin/lockf", "-k", lock, *argv] if lock else argv, capture_output=True, text=True)


def main() -> int:
    paths = (DAEMON, SCHEDULER, CODEX)
    if Path(".git").exists() and subprocess.run(["git", "diff", "--quiet", "--", *map(str, paths)]).returncode:
        print("the mutated files have uncommitted changes; commit them first", file=sys.stderr)
        return 2
    originals, survived = {path: path.read_text() for path in paths}, []
    try:
        for name, (edits, tests) in MUTATIONS.items():
            texts = dict(originals)
            if any(texts[path].count(rule) != 1 for path, rule, _ in edits):
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            for path, rule, replacement in edits:
                texts[path] = texts[path].replace(rule, replacement)
            for path in paths:
                path.write_text(texts[path])
            try:
                run = pytest(tests)
            finally:
                for path in paths:
                    path.write_text(originals[path])
            failed = next((line[7:] for line in run.stdout.splitlines() if line.startswith("FAILED ")), "")
            print(f"{'killed  ' if run.returncode else 'SURVIVED'}  {name}" + (f"  ({failed.split(' - ')[0]})" if failed else ""))
            if not run.returncode:
                survived.append(name)
    finally:
        for path, text in originals.items():
            path.write_text(text)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
