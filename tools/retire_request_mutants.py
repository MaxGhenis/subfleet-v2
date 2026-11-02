"""Opt-in pytest plugin, and its runner, for the named C-8.6 (retire request) mutants.

One mutant, under the suite hold::

    lockf -k <suite-lock> python -m pytest -p tools.retire_request_mutants \
        --retire-request-mutant=no-lift tests/unit/test_retention_retire_requests.py

Every mutant, each in its own pytest, after an unmutated baseline::

    lockf -k <suite-lock> python tools/retire_request_mutants.py --logs <dir> --basetemp <short dir>

A mutant swaps one function for a copy compiled from its production source with
one edit, in its own module's globals; the source files stay unchanged. A copy of
a module-level function also replaces the original wherever another module
imported it by name. A mutant counts as killed only when tests fail, never on a
collection or setup error. `--basetemp` must be short: the daemon tests bind
Unix sockets under it.
"""
from __future__ import annotations

import __future__
import argparse
import ast
import importlib
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
TESTS = ("tests/unit/test_retention_retire_requests.py", "tests/fake/test_retire_request_daemon.py")
# The two lines `_rearm_retention` ends with, at their own indentation.
_REARM = ('            self.timers.mark("retention", error=error, next_due=after(delay))\n'
          '            self._last_maintenance = time.monotonic() - RETENTION_INTERVAL_S + delay\n')

#: name -> (module, function, text it holds once, the mutant's text, what that breaks)
MUTANTS: dict[str, tuple[str, str, str, str, str]] = {
    "no-lift": ("subfleet.retention", "_Pass.run",
                "not (over(pool) or job_id in self.requested)", "not over(pool)",
                "a request retires nothing its pool's budget keeps"),
    "lifts-pins": ("subfleet.retention", "_Pass.run",
                   "if job_id in in_flight or job_id in protected or not",
                   "if job_id in in_flight or job_id in protected - self.requested or not",
                   "invariant 1: a request lifts a pin"),
    "lifts-deferral": ("subfleet.retention", "_Pass.run",
                       "deferral = self.state.deferral(job_id, now)",
                       "deferral = None if job_id in self.requested else self.state.deferral(job_id, now)",
                       "invariant 1: a request lifts a deferral"),
    "lifts-batch": ("subfleet.retention", "_Pass.run",
                    "if len(in_flight) + len(chosen) >= self.batch:",
                    "if len(in_flight) + len(chosen) >= self.batch and job_id not in self.requested:",
                    "invariant 1: a request lifts the batch"),
    "ahead-of-leftovers": ("subfleet.retention", "_Pass._rank",
                           "0 if job_id in self.leftovers else 1 if job_id in self.requested else 2",
                           "0 if job_id in self.requested else 1 if job_id in self.leftovers else 2",
                           "invariant 2: requested jobs go before orphan leases' jobs"),
    "no-priority": ("subfleet.retention", "_Pass._rank",
                    "else 1 if job_id in self.requested else 2", "else 2",
                    "invariant 2: requested jobs wait their turn by age"),
    "withdrawal-ignored": ("subfleet.retention", "retire_requests",
                           'standing.pop(row["job_id"], None)', "pass",
                           "invariant 3: a withdrawal takes nothing back"),
    "unbound-withdraws": ("subfleet.retention", "retire_requests",
                          '            standing[row["job_id"]] = {**data, "at": row["ts"]}\n',
                          '            standing[row["job_id"]] = {**data, "at": row["ts"]}\n'
                          '        else:\n            standing.pop(row["job_id"], None)\n',
                          "invariant 3: an event naming no created_at unbinds the request before it"),
    "not-bound": ("subfleet.retention", "requested",
                  ' and requests[job["job_id"]]["created_at"] == job["created_at"]', "",
                  "invariant 3: a request takes a later job with its id"),
    "survey-blind": ("subfleet.retention_survey", "survey",
                     "requested = ret.requested(jobs, ret.retire_requests(store.query))", "requested = set()",
                     "the survey counts no request"),
    "answer-not-bound": ("subfleet.daemon", "Daemon._retire",
                         'if standing is not None and standing["created_at"] != job["created_at"]:', "if False:",
                         "the answer reads an earlier job's request as standing"),
    "pruned-by-kind": ("subfleet.daemon", "Daemon._retire",
                       "AND +kind='retention.pruned'", "AND kind='retention.pruned'",
                       "an unknown id walks every retirement inside the write transaction"),
    "takes-unfinished": ("subfleet.daemon", "Daemon._retire",
                         'if job["state"] not in TERMINAL:', "if False:",
                         "a job not finished is requested"),
    "dry-run-records": ("subfleet.daemon", "Daemon._retire",
                        'in ("requested", "withdrawn") and not a.dry_run:', 'in ("requested", "withdrawn"):',
                        "a dry run records"),
    "not-due": ("subfleet.daemon", "Daemon._retire",
                "                self._last_maintenance = time.monotonic() - RETENTION_INTERVAL_S\n", "",
                "a request waits out the hour"),
    "withdrawal-due": ("subfleet.daemon", "Daemon._retire",
                       "if recorded and not a.withdraw:", "if recorded:",
                       "a withdrawal brings a pass"),
    "no-catch-up": ("subfleet.daemon", "Daemon._rearm_retention",
                    "RETENTION_INTERVAL_S if self._retire_asked == asked else RETENTION_CATCH_UP_S",
                    "RETENTION_INTERVAL_S", "a request recorded during a pass waits the hour"),
    "asked-at-end": ("subfleet.daemon", "Daemon._retention",
                     "        self._rearm_retention(asked)\n", "        self._rearm_retention(self._retire_asked)\n",
                     "a completed pass forgets a request recorded during it"),
    "clock-first": ("subfleet.daemon", "Daemon._rearm_retention", _REARM,
                    "".join(reversed(_REARM.splitlines(keepends=True))),
                    "C-8.4: a failed mark leaves retention rearmed"),
    "mark-unlocked": ("subfleet.daemon", "Daemon._rearm_retention", _REARM,
                      "".join(line[4:] for line in _REARM.splitlines(keepends=True)),
                      "the pass's hour overwrites a request recorded during its mark"),
    "hides-pin": ("subfleet.daemon", "Daemon._retire_pins",
                  'row["kept"] = pins[row["job_id"]]', "pass", "the answer hides a pin"),
    "pins-each-always": ("subfleet.daemon", "Daemon._retire_pins",
                         "if len(rows) <= RETIRE_PIN_READ_EACH else", "if True else",
                         "a long request reads pins job by job, past the client's timeout"),
    "pins-once-always": ("subfleet.daemon", "Daemon._retire_pins",
                         "if len(rows) <= RETIRE_PIN_READ_EACH else", "if False else",
                         "a short request reads every job's pins"),
    "unread-silent": ("subfleet.daemon", "Daemon._retire_pins",
                      'row["pins_unread"] = unread[row["job_id"]]', "pass",
                      "the answer hides pins it could not read"),
    "unread-spreads": ("subfleet.daemon", "Daemon._retire_pins",
                       'unread.update(dict.fromkeys((row["job_id"] for row in group)',
                       'unread.update(dict.fromkeys((row["job_id"] for row in rows)',
                       "one job's failed read leaves every job's pins unread"),
    "unread-hides-deferral": ("subfleet.daemon", "Daemon._retire_annotate",
                              "if deferred is not None and deferred[0] > now:",
                              'if deferred is not None and deferred[0] > now and "pins_unread" not in row:',
                              "pins it could not read hide a deferral"),
    "cli-retired-refused": ("subfleet.retention_cli", "_retire",
                            'row.get("outcome") in SETTLED for', 'row.get("outcome") in SETTLED - {"retired"} for',
                            "the CLI exits refused for a retired job"),
}


def _segment(path: Path, qualname: str) -> tuple[int, str]:
    """Where the function starts (a 0-based line) and its source as the file holds it."""
    source = path.read_text()
    body = ast.parse(source).body
    *owners, name = qualname.split(".")
    for owner in owners:
        body = next(node for node in body if isinstance(node, ast.ClassDef) and node.name == owner).body
    node = next(node for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)
    start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]) - 1
    return start, "".join(source.splitlines(keepends=True)[start:node.end_lineno])


def install(name: str) -> None:
    module_name, qualname, old, new, _ = MUTANTS[name]
    module = importlib.import_module(module_name)
    start, segment = _segment(Path(module.__file__), qualname)
    assert segment.count(old) == 1, (name, segment.count(old))
    # Blank lines first, so a traceback names the file's own line numbers.
    code = compile("\n" * start + textwrap.dedent(segment.replace(old, new)), module.__file__, "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    made: dict = {}
    exec(code, module.__dict__, made)
    *owners, attr = qualname.split(".")
    if owners:
        owner = module
        for part in owners:
            owner = getattr(owner, part)
        setattr(owner, attr, made[attr])
        return
    original = getattr(module, attr)
    for other in list(sys.modules.values()):
        if getattr(other, "__dict__", {}).get(attr) is original:
            setattr(other, attr, made[attr])


def pytest_addoption(parser):
    parser.addoption("--retire-request-mutant", choices=sorted(MUTANTS))


def pytest_configure(config):
    name = config.getoption("--retire-request-mutant")
    if name:
        install(name)


def pytest_report_header(config):
    name = config.getoption("--retire-request-mutant")
    return f"retire request mutant: {name}: {MUTANTS[name][4]}" if name else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", type=Path, required=True, help="one log a run")
    parser.add_argument("--basetemp", type=Path, required=True, help="a short private directory")
    parser.add_argument("--only", nargs="+", choices=sorted(MUTANTS))
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)

    def run(index: int, name: str | None) -> tuple[int, str]:
        command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                   "-p", "tools.retire_request_mutants", f"--basetemp={args.basetemp / f'm{index:02d}'}",
                   *([f"--retire-request-mutant={name}"] if name else []), *TESTS]
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (args.logs / f"{name or 'baseline'}.log").write_text(result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return result.returncode, lines[-1] if lines else ""

    code, last = run(0, None)
    print(f"baseline: exit {code}; {last}", flush=True)
    if code != 0:
        return 2
    survived = []
    for index, name in enumerate(args.only or MUTANTS, start=1):
        code, last = run(index, name)
        killed = code == 1 and "failed" in last and "error" not in last
        print(f"{name}: {'KILLED' if killed else 'SURVIVED'}; exit {code}; {last}; {MUTANTS[name][4]}", flush=True)
        if not killed:
            survived.append(name)
    print(f"{len(args.only or MUTANTS) - len(survived)} killed, {len(survived)} survived"
          + (f": {', '.join(survived)}" if survived else ""), flush=True)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
