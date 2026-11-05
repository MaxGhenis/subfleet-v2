"""The C-31.17 mutants of `docs/reports/credit-lane-integration-report.md`, run on a copy.

Each mutant is one exact source change that undoes part of the hub's ruling of
2026-11-04 (no exec environment block, argv or file of any process holds a credit
key; the key travels only over pipes, and only the harness keeps it). The tool first
runs every named test on the unchanged copy (the baseline), then applies one mutant at
a time to the copy, never to this checkout, runs the tests named for it with the
copy's own interpreter, puts the files back and records each test's verdict and the
first words of its failure. A test catches a mutant only when it passed at the
baseline and fails with the mutant; a mutant that no named test catches is a gap, and
so is one whose edit no longer applies.

The copy needs its own virtual environment. The harness runs as
`python -I -m subfleet.api_agent`, which ignores PYTHONPATH and the working folder,
so it imports whichever tree its interpreter's editable install names. For the same
reason the tool deletes the bytecode of every file it edits, before and after each
run: `-I` also ignores PYTHONDONTWRITEBYTECODE. Make a copy:

    git archive HEAD | tar -x -C <copy>
    (cd <copy> && uv sync --group dev --extra api)

then run, from anywhere:

    python tools/credit_key_mutations.py --tree <copy> --out <results.json> [--only a,c] [--no-baseline]
        [--baseline-only] [--test-timeout 300] [--run-timeout 540]

Results go to `--out` after the baseline and after each mutant, so a run cut short
keeps what it finished. Every test this runs uses the local fake Messages API
(127.0.0.1) and fake keys only. The process tests take a minute or two each on a
loaded host, so run the baseline alone first (`--baseline-only`), then a few mutants per
call with `--only` and `--no-baseline`, which reuses the baseline already in `--out`
(it covers every mutant's tests). Keep `--run-timeout` under the caller's own limit:
a run killed from outside leaves its pytest behind.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CONTAINMENT = "tests/fake/test_credit_key_containment.py::test_no_process_of_a_credit_attempt_holds_the_key"
SELF_TEST = "tests/fake/test_credit_key_containment.py::test_the_publish_hook_self_test_holds_no_key"
ENV_KEY_PROCESS = "tests/process/test_api_agent_process.py::test_the_key_comes_only_from_the_key_fd"
ENV_KEY_SOURCE = "tests/unit/test_api_agent_units.py::test_the_harness_reads_no_key_from_its_environment"
NO_PIPE = "tests/process/test_credit_key_frame.py::test_the_guardian_holds_no_pipe_while_its_provider_runs"
CHAIN = ("tests/process/test_credit_key_frame.py::"
         "test_any_key_reaches_the_harness_reader_unchanged_through_a_real_guardian")
READ_KEY_CLOSES = ("tests/unit/test_api_agent_units.py::"
                   "test_read_key_returns_the_framed_key_and_closes_the_descriptor")
ONLY_CREDIT = ("tests/unit/test_credit_lane_admission.py::"
               "test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own")
INTERFACE = "tests/unit/test_claude_api_adapter.py::test_build_launch_is_exactly_the_interface"
#: Each test's own bound (pytest-timeout), and the bound on one pytest run. A test past
#: its bound fails with its fixtures torn down; a run past its bound is killed whole, which
#: can leave a fake daemon's guardians behind, so the run's bound is the larger.
TEST_TIMEOUT_S = 300
RUN_TIMEOUT_S = 540

_LAUNCH_ENV = '        env = self._launch_environment(lane, launch)\n        env["TMPDIR"] = tmpdir\n'


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutant:
    name: str
    what: str
    edits: tuple[Edit, ...]
    tests: tuple[str, ...]


MUTANTS = (
    Mutant("a", "the key back in the guardian's environment: the daemon's launch path adds the "
                "resolved key to the environment it starts the guardian with",
           (Edit("subfleet/daemon.py", _LAUNCH_ENV,
                 _LAUNCH_ENV + '        env.update({name: value for name, value in credential_env.items()'
                               ' if name == "ANTHROPIC_API_KEY"})\n'),),
           (CONTAINMENT,)),
    Mutant("a-renamed", "(a) under a name no credential rule knows (SUBFLEET_CREDIT_HANDOFF): only a "
                        "check that reads values, not names, can catch it",
           (Edit("subfleet/daemon.py", _LAUNCH_ENV,
                 _LAUNCH_ENV + '        if "ANTHROPIC_API_KEY" in credential_env:\n'
                               '            env["SUBFLEET_CREDIT_HANDOFF"] = credential_env["ANTHROPIC_API_KEY"]\n'),),
           (CONTAINMENT,)),
    Mutant("a-base", "the design's first path restored: the adapter puts the key in env_add and "
                     "`_launch_environment` puts it back, as at da2eb8b56",
           (Edit("subfleet/daemon.py",
                 '        return self._provider_environment.assemble(\n'
                 '            launch.env_add,\n'
                 '            (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),\n'
                 '            provider=lane.provider)\n',
                 '        environment = self._provider_environment.assemble(\n'
                 '            launch.env_add,\n'
                 '            (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),\n'
                 '            provider=lane.provider)\n'
                 '        key = launch.env_add.get("ANTHROPIC_API_KEY") if lane.provider == API_PROVIDER else None\n'
                 '        if key:\n'
                 '            environment["ANTHROPIC_API_KEY"] = key\n'
                 '        return environment\n'),
            Edit("subfleet/adapters/claude_api.py",
                 '               "SUBFLEET_API_LANE": lane.lane_id}\n',
                 '               "ANTHROPIC_API_KEY": key, "SUBFLEET_API_LANE": lane.lane_id}\n')),
           (ONLY_CREDIT, INTERFACE, CONTAINMENT)),
    Mutant("b", "the key in the publish hook self-test's environment: `_publish_self_test` adds the "
                "lane's resolved credential to the environment it tests the hook in",
           (Edit("subfleet/daemon.py",
                 '        env = self._launch_environment(lane, launch)\n'
                 '        env.update(SUBFLEET_JOB=a["job_id"], SUBFLEET_ATTEMPT=a["attempt_id"], '
                 'SUBFLEET_ROOT=str(self.root))\n        verdicts = []\n',
                 '        env = self._launch_environment(lane, launch)\n'
                 '        env.update(SUBFLEET_JOB=a["job_id"], SUBFLEET_ATTEMPT=a["attempt_id"], '
                 'SUBFLEET_ROOT=str(self.root))\n'
                 '        if lane.provider == API_PROVIDER:\n'
                 '            env.update(self._credential_environment(lane))\n'
                 '        verdicts = []\n'),),
           (SELF_TEST,)),
    Mutant("c", "the harness reads ANTHROPIC_API_KEY from its environment again, when no key "
                "came on --key-fd",
           (Edit("subfleet/api_agent/harness.py",
                 "def main(argv: list[str]) -> int:\n    launch, test_url = _scrub_process_environment()\n",
                 "def main(argv: list[str]) -> int:\n    from_env = os.environ.get(\"ANTHROPIC_API_KEY\")\n"
                 "    launch, test_url = _scrub_process_environment()\n"),
            Edit("subfleet/api_agent/harness.py",
                 "    key, key_problem = read_key(options.key_fd)\n",
                 "    key, key_problem = read_key(options.key_fd)\n"
                 "    if key is None and from_env:\n        key, key_problem = from_env, None\n")),
           (ENV_KEY_SOURCE, ENV_KEY_PROCESS)),
    Mutant("c-hidden", "(c) with the variable's name built at run time, which the source check "
                       "cannot see: only the process test can catch it",
           (Edit("subfleet/api_agent/harness.py",
                 "def main(argv: list[str]) -> int:\n    launch, test_url = _scrub_process_environment()\n",
                 "def main(argv: list[str]) -> int:\n    from_env = os.environ.get(\"ANTHROPIC_\" + \"API_KEY\")\n"
                 "    launch, test_url = _scrub_process_environment()\n"),
            Edit("subfleet/api_agent/harness.py",
                 "    key, key_problem = read_key(options.key_fd)\n",
                 "    key, key_problem = read_key(options.key_fd)\n"
                 "    if key is None and from_env:\n        key, key_problem = from_env, None\n")),
           (ENV_KEY_SOURCE, ENV_KEY_PROCESS)),
    Mutant("d", "the guardian keeps its end of the key's pipe (the read end it passed on) open "
                "past the spawn",
           (Edit("subfleet/guardian.py",
                 "    finally:\n        if key_fd is not None:\n            os.close(key_fd)\n"
                 "        key_frame.wipe(key)\n",
                 "    finally:\n        key_frame.wipe(key)\n"),),
           (NO_PIPE, CONTAINMENT)),
    Mutant("d-write", "the guardian keeps the key pipe's write end open past the spawn",
           (Edit("subfleet/guardian.py",
                 "            raise\n        finally:\n            os.close(write_end)\n    finally:\n"
                 "        key_frame.wipe(frame)\n",
                 "            raise\n        finally:\n            pass\n    finally:\n"
                 "        key_frame.wipe(frame)\n"),),
           (NO_PIPE, CONTAINMENT)),
    Mutant("e", "the harness keeps the key's descriptor open after its one read",
           (Edit("subfleet/api_agent/harness.py",
                 "            key_frame.wipe(payload)\n        try:\n            os.close(fd)\n"
                 "        except OSError:\n            pass\n",
                 "            key_frame.wipe(payload)\n"),),
           (READ_KEY_CLOSES, CHAIN)),
)


def verdicts(report: Path, tests: tuple[str, ...]) -> dict[str, dict]:
    """Each named test's verdict in a JUnit report (passed, failed, error, skipped or not
    run) and, for a failure or an error, the first words of its message."""
    found = {test: {"verdict": "not run", "message": None} for test in tests}
    if not report.exists():
        return found
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        parts = classname.split(".")
        stop = next((index for index, part in enumerate(parts) if part.startswith("test_")), len(parts) - 1)
        node = "/".join(parts[:stop + 1]) + ".py::" + name
        if node not in found:
            continue
        children = {child.tag: child for child in case}
        verdict = ("failed" if "failure" in children else "error" if "error" in children
                   else "skipped" if "skipped" in children else "passed")
        problem = children.get("failure", children.get("error"))
        message = None if problem is None else " ".join((problem.get("message") or "").split())[:400]
        found[node] = {"verdict": verdict, "message": message}
    return found


def _drop_bytecode(tree: Path, relative: str) -> None:
    source = tree / relative
    for cached in (source.parent / "__pycache__").glob(f"{source.stem}.*.pyc"):
        cached.unlink(missing_ok=True)


def _tail(path: Path, size: int = 1500) -> str:
    """The last `size` bytes of `path`: the output stays in a file, never whole in memory."""
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - size))
            return stream.read(size).decode("utf-8", "replace")
    except OSError:
        return ""


def _pytest(tree: Path, tests: tuple[str, ...], report: Path, basetemp: Path,
            limits: tuple[float, float] = (TEST_TIMEOUT_S, RUN_TIMEOUT_S)) -> tuple[int | str, str, float]:
    """One pytest run of `tests` in `tree`: its exit code (or "timeout"), its output's
    tail and its seconds. `limits` is (each test's bound, the run's bound): pytest-timeout
    fails a test past the first, so its fixtures still tear down; past the second the
    run's whole group is killed. The output goes to a file beside the report, which
    keeps a test that prints without end from filling this process (D-CREDIT-MEM1)."""
    test_timeout, run_timeout = limits
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    output = report.with_suffix(".out")
    began = time.monotonic()
    with output.open("wb") as sink:
        process = subprocess.Popen(
            [str(tree / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"--timeout={test_timeout:g}", "--basetemp", str(basetemp), f"--junitxml={report}",
             "-o", "junit_family=xunit1", *tests],
            cwd=tree, env=environment, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True)
    try:
        rc: int | str = process.wait(timeout=run_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        rc = "timeout"
    return rc, _tail(output), round(time.monotonic() - began, 1)


def baseline(tree: Path, tests: tuple[str, ...], scratch: Path,
             limits: tuple[float, float] = (TEST_TIMEOUT_S, RUN_TIMEOUT_S)) -> dict:
    report = scratch / "baseline.xml"
    rc, tail, seconds = _pytest(tree, tests, report, scratch / "bt-baseline", limits)
    return {"rc": rc, "seconds": seconds, "tests": verdicts(report, tests), "tail": tail}


def run(tree: Path, mutant: Mutant, scratch: Path, base: dict[str, dict],
        limits: tuple[float, float] = (TEST_TIMEOUT_S, RUN_TIMEOUT_S)) -> dict:
    """Apply `mutant` to `tree`, run its tests, and put every file back, whatever raised."""
    originals: dict[str, str] = {}
    report = scratch / f"{mutant.name}.xml"
    result = {"mutant": mutant.name, "what": mutant.what}
    try:
        for edit in mutant.edits:
            path = tree / edit.path
            current = path.read_text()
            originals.setdefault(edit.path, current)
            if current.count(edit.old) != 1:
                return {**result, "rc": "edit-missing", "seconds": 0, "caught": False, "caught_by": [],
                        "tests": {}, "tail": f"{edit.path} holds the old text {current.count(edit.old)} times, "
                                             "not once"}
            path.write_text(current.replace(edit.old, edit.new, 1))
        for relative in originals:
            _drop_bytecode(tree, relative)
        rc, tail, seconds = _pytest(tree, mutant.tests, report, scratch / f"bt-{mutant.name}", limits)
    finally:
        for relative, text in originals.items():
            (tree / relative).write_text(text)
            _drop_bytecode(tree, relative)
    found = verdicts(report, mutant.tests)
    caught_by = [test for test, verdict in found.items()
                 if verdict["verdict"] == "failed" and base.get(test, {}).get("verdict") == "passed"]
    return {**result, "rc": rc, "seconds": seconds, "caught": bool(caught_by), "caught_by": caught_by,
            "tests": found, "tail": tail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True, help="a copy of the tree with its own .venv")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated mutant names")
    parser.add_argument("--no-baseline", action="store_true", help="reuse the baseline already in --out")
    parser.add_argument("--baseline-only", action="store_true", help="run the baseline and no mutant")
    parser.add_argument("--test-timeout", type=float, default=TEST_TIMEOUT_S, help="each test's bound, seconds")
    parser.add_argument("--run-timeout", type=float, default=RUN_TIMEOUT_S,
                        help="one pytest run's bound, seconds; keep it under the caller's own")
    args = parser.parse_args(argv)
    limits = (args.test_timeout, args.run_timeout)
    tree = args.tree.resolve()
    if tree == REPO or REPO in tree.parents or (tree / ".git").exists():
        raise SystemExit("credit_key_mutations: --tree must be a copy outside this checkout, not a git checkout")
    if not (tree / ".venv" / "bin" / "python").exists():
        raise SystemExit("credit_key_mutations: the copy has no .venv; run `uv sync --group dev --extra api` there")
    names = [name for name in args.only.split(",") if name]
    unknown = sorted(set(names) - {mutant.name for mutant in MUTANTS})
    if unknown:
        raise SystemExit(f"credit_key_mutations: no mutant named {', '.join(unknown)}")
    chosen = [mutant for mutant in MUTANTS if not names or mutant.name in names]
    previous = json.loads(args.out.read_text()) if args.out.exists() else {}
    results = {"baseline": previous.get("baseline"), "mutants": dict(previous.get("mutants") or {})}

    def save() -> None:
        args.out.write_text(json.dumps(results, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="credit-mutants-") as scratch:
        if not args.no_baseline or results["baseline"] is None:
            # Every mutant's tests, whichever were chosen, so a later `--no-baseline` call can use it.
            tests = tuple(dict.fromkeys(test for mutant in MUTANTS for test in mutant.tests))
            results["baseline"] = baseline(tree, tests, Path(scratch), limits)
            save()
            print(f"baseline: {({test: v['verdict'] for test, v in results['baseline']['tests'].items()})} "
                  f"({results['baseline']['seconds']} s)", flush=True)
        if args.baseline_only:
            return 0 if all(v["verdict"] == "passed" for v in results["baseline"]["tests"].values()) else 1
        base = results["baseline"]["tests"]
        missing = [test for mutant in chosen for test in mutant.tests if base.get(test, {}).get("verdict") != "passed"]
        if missing:
            print(f"not passing at the baseline, so they catch nothing: {sorted(set(missing))}", flush=True)
        for mutant in chosen:
            result = run(tree, mutant, Path(scratch), base, limits)
            results["mutants"][mutant.name] = result
            save()
            print(f"{mutant.name}: caught={result['caught']} by {result['caught_by']} "
                  f"{({test: v['verdict'] for test, v in result['tests'].items()})} ({result['seconds']} s)",
                  flush=True)
    return 0 if all(results["mutants"][mutant.name]["caught"] for mutant in chosen) else 1


if __name__ == "__main__":
    sys.exit(main())
