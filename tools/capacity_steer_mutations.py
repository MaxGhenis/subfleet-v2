"""Mutation check for the model-capacity steer (C-9.5, D-CAP1).

Each mutation removes one rule of the steer from the code, and the steer's tests must fail
under it. Run from the repository root (or from a `git archive` of it), with the mutated
files clean:

    uv run python tools/capacity_steer_mutations.py

With `SUBFLEET_SUITE_LOCK` set to a lock file path (the hub's
`/private/tmp/claude-501/subfleet-suites.lock`), each pytest run waits for its own hold of
that lock (`/usr/bin/lockf -k`). It rewrites `subfleet/daemon.py`, `subfleet/scheduler.py`
or `subfleet/adapters/codex.py` in place, one mutation at a time, and restores the file
whatever happens. Only exit 1 with a named selected pytest regression failing counts as a
kill; infrastructure failures stop the tool with a nonzero exit. Every run gets its own
temporary directory and bounded Hypothesis profile. E2E tests that detach processes are
excluded. It exits 1 if a mutation survives or no longer applies. It is a
development check, not part of the suite: it runs the tests once per mutation.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

DAEMON, SCHEDULER, CODEX = Path("subfleet/daemon.py"), Path("subfleet/scheduler.py"), Path("subfleet/adapters/codex.py")
FAKE, ROUTE = "tests/fake/test_capacity_steer_daemon.py", "tests/unit/test_capacity_steer_route.py"
ADAPTER = "tests/unit/test_codex_adapter.py"

ROUTE_JOB = "        steer = self._capacity_steer(job, previous)\n        return {**job, \"capacity_steer\": steer} if steer else job\n"
NO_PIN = "and not _capacity_steer_of(last[\"evidence_json\"]) and self._retry_pair_routable(last)"
STEERED = "            steered = (not lost and outcome.cls == OutcomeClass.TRANSIENT\n"
FILTER = "               if steer and capacity_steerable(job) else {})\n"
STEERABLE = ("    return not job.get(\"pinned_model\") and not job.get(\"pinned_lane\") "
             "and job.get(\"kind\") not in _UNSTEERED_KINDS\n")
EXPIRY = "            if found is None or _epoch(found[\"until\"]) is None or _epoch(found[\"until\"]) <= now.timestamp():\n"

#: name: ([(file, the rule's code, what replaces it)], the tests that must fail)
MUTATIONS = {
    # The whole steer, classification kept: the incident's behaviour.
    "no steer: the same pair 60 s on, as before D-CAP1": (
        [(DAEMON, ROUTE_JOB, "        return job\n"), (DAEMON, NO_PIN, "and self._retry_pair_routable(last)"),
         (DAEMON, STEERED, "            steered = False and (not lost and outcome.cls == OutcomeClass.TRANSIENT\n")],
        (FAKE,)),
    "the refused model is not excluded": ([(SCHEDULER, FILTER, FILTER.replace("if steer and capacity_steerable(job)", "if False"))],
                                  (FAKE, ROUTE)),
    "the route is never handed the steer": ([(DAEMON, ROUTE_JOB, "        return job\n")], (FAKE,)),
    "a steered refusal keeps the same-lane pin": ([(DAEMON, NO_PIN, "and self._retry_pair_routable(last)")], (FAKE,)),
    "a steered refusal waits 60 s": (
        [(DAEMON, "TRANSIENT_RETRY_DELAY_S if outcome.cls == OutcomeClass.TRANSIENT and not steered\n",
          "TRANSIENT_RETRY_DELAY_S if outcome.cls == OutcomeClass.TRANSIENT\n")], (FAKE,)),
    "a pinned job is steered": ([(SCHEDULER, STEERABLE, "    return True\n")], (FAKE, ROUTE)),
    "a steer never ends": ([(DAEMON, EXPIRY, "            if found is None:\n")], (FAKE,)),
    "the refused model is eligible as a fallback again": (
        [(SCHEDULER, "        chain = [short for short in chain if short not in steered]\n",
          "        chain = [short for short in chain if short not in steered] + list(steered)\n")], (FAKE, ROUTE)),
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
    with tempfile.TemporaryDirectory(prefix="sf-r207f2-") as directory:
        env = {**os.environ, "TMPDIR": directory + "/",
               "HYPOTHESIS_STORAGE_DIRECTORY": str(Path(directory) / "hyp"),
               "HYPOTHESIS_PROFILE": "ci", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(["/usr/bin/lockf", "-k", "-t", "120", lock, *argv] if lock else argv,
                              env=env, capture_output=True, text=True)


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
            except OSError as exc:
                print(f"INFRASTRUCTURE ERROR  {name}: {exc}", file=sys.stderr)
                return 2
            finally:
                for path in paths:
                    path.write_text(originals[path])
            failed = next((line[7:].split(" - ")[0] for line in run.stdout.splitlines()
                           if line.startswith("FAILED ")
                           and any(line[7:].startswith(test + "::test_") for test in tests)), "")
            if run.returncode and (run.returncode != 1 or not failed):
                print(f"INFRASTRUCTURE ERROR  {name}: exit {run.returncode}; no verified regression failure\n"
                      + run.stdout + run.stderr, file=sys.stderr, flush=True)
                return run.returncode if run.returncode > 0 else 2
            killed = run.returncode == 1 and bool(failed)
            print(f"{'killed  ' if killed else 'SURVIVED'}  {name}" + (f"  ({failed})" if killed else ""), flush=True)
            if not killed:
                survived.append(name)
    finally:
        for path, text in originals.items():
            path.write_text(text)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
