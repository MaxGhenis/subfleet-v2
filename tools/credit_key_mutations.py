"""The C-31.17 mutants of `docs/reports/credit-lane-integration-report.md`, run on a copy.

Each mutant is one exact source change that undoes part of the hub's ruling of
2026-11-04 (no exec environment block, argv or file of any process holds a credit
key; the key travels only over pipes, and only the harness keeps it). The tool
applies one mutant at a time to a copy of the tree, never to this checkout, runs the
tests named for it with the copy's own interpreter, puts the file back and records
which of those tests failed. A mutant that no named test fails is a gap.

The copy needs its own virtual environment. The harness runs as
`python -I -m subfleet.api_agent`, which ignores PYTHONPATH and the working folder,
so it imports whichever tree its interpreter's editable install names. Make a copy:

    git archive HEAD | tar -x -C <copy>
    (cd <copy> && uv sync --group dev --extra api)

then run, from anywhere:

    python tools/credit_key_mutations.py --tree <copy> --out <results.json> [--only a,c]

Every test this runs uses the local fake Messages API (127.0.0.1) and fake keys
only. The process tests take minutes each on a loaded host, so run a few mutants
per call with `--only`.
"""
from __future__ import annotations

import argparse
import json
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
ONLY_CREDIT = ("tests/unit/test_credit_lane_admission.py::"
               "test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own")
INTERFACE = "tests/unit/test_claude_api_adapter.py::test_build_launch_is_exactly_the_interface"


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
           (Edit("subfleet/daemon.py",
                 '        env = self._launch_environment(lane, launch)\n        env["TMPDIR"] = tmpdir\n',
                 '        env = self._launch_environment(lane, launch)\n        env["TMPDIR"] = tmpdir\n'
                 '        env.update({name: value for name, value in credential_env.items()'
                 ' if name == "ANTHROPIC_API_KEY"})\n'),),
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
           (NO_PIPE,)),
)


def failures(report: Path, tests: tuple[str, ...]) -> dict[str, str]:
    """Each named test's verdict in a JUnit report: passed, failed, error or skipped."""
    verdicts = {test: "not run" for test in tests}
    if not report.exists():
        return verdicts
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        parts = classname.split(".")
        stop = next((index for index, part in enumerate(parts) if part.startswith("test_")), len(parts) - 1)
        node = "/".join(parts[:stop + 1]) + ".py::" + name
        if node not in verdicts:
            continue
        kinds = {child.tag for child in case}
        verdicts[node] = ("failed" if "failure" in kinds else "error" if "error" in kinds
                          else "skipped" if "skipped" in kinds else "passed")
    return verdicts


def run(tree: Path, mutant: Mutant, scratch: Path) -> dict:
    """Apply `mutant` to `tree`, run its tests, and put every file back, whatever raised."""
    originals: dict[str, str] = {}
    report = scratch / f"{mutant.name}.xml"
    began = time.monotonic()
    try:
        for edit in mutant.edits:
            path = tree / edit.path
            current = path.read_text()
            originals.setdefault(edit.path, current)
            if current.count(edit.old) != 1:
                raise SystemExit(f"mutant {mutant.name}: {edit.path} holds its old text "
                                 f"{current.count(edit.old)} times, not once")
            path.write_text(current.replace(edit.old, edit.new, 1))
        proc = subprocess.run([str(tree / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               "--basetemp", str(scratch / f"bt-{mutant.name}"), f"--junitxml={report}",
                               "-o", "junit_family=xunit1", *mutant.tests],
                              cwd=tree, capture_output=True, text=True, timeout=1500)
    finally:
        for relative, text in originals.items():
            (tree / relative).write_text(text)
    verdicts = failures(report, mutant.tests)
    return {"mutant": mutant.name, "what": mutant.what, "rc": proc.returncode,
            "seconds": round(time.monotonic() - began, 1), "tests": verdicts,
            "caught": any(verdict == "failed" for verdict in verdicts.values()),
            "tail": proc.stdout[-1500:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True, help="a copy of the tree with its own .venv")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated mutant names")
    args = parser.parse_args(argv)
    tree = args.tree.resolve()
    if tree == REPO or REPO in tree.parents or (tree / ".git").exists():
        raise SystemExit("credit_key_mutations: --tree must be a copy outside this checkout, not a git checkout")
    if not (tree / ".venv" / "bin" / "python").exists():
        raise SystemExit("credit_key_mutations: the copy has no .venv; run `uv sync --group dev --extra api` there")
    chosen = [mutant for mutant in MUTANTS if not args.only or mutant.name in args.only.split(",")]
    results = []
    with tempfile.TemporaryDirectory(prefix="credit-mutants-") as scratch:
        for mutant in chosen:
            result = run(tree, mutant, Path(scratch))
            results.append(result)
            print(f"{mutant.name}: caught={result['caught']} {result['tests']} ({result['seconds']} s)", flush=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(result["caught"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
