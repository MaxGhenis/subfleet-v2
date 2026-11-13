"""Reproduce D-TASKHOLD-CAS1 mutation checks in temporary source copies.

Run with the worktree's development Python. Each mutation gets its own copy,
pytest basetemp, and process. The production worktree is never mutated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
FAKE = "tests/fake/test_task_hold_cas.py"
CLI = "tests/unit/test_task_hold_cas_cli.py"
FOREIGN = FAKE + "::test_foreign_hold_refuses_without_changing_any_row_or_event"
RACE = FAKE + "::test_foreign_commit_before_writer_is_seen_by_condition"
JSON_REFUSAL = CLI + "::test_condition_refusal_names_all_blocking_hold_fields_and_exit_7"


def replace(path, old, new, count=1):
    source = path.read_text()
    assert source.count(old) == count, (path, old, source.count(old), count)
    path.write_text(source.replace(old, new))


def mutate(root, name):
    daemon = root / "subfleet/daemon.py"
    cli = root / "subfleet/cli.py"
    if name == "new-note-instead-of-held-note":
        replace(daemon, 'not replaced["note"].startswith(prefix)', 'not note.startswith(prefix)')
    elif name == "expired-row-in-force":
        replace(daemon, "if not task_holds.in_force_now(replaced, now):", "if prefix is None and not task_holds.in_force_now(replaced, now):")
    elif name == "release-deletes-foreign-row":
        replace(daemon, 'if prefix is not None and ended is not None and not ended["note"].startswith(prefix):',
                'if False and prefix is not None and ended is not None and not ended["note"].startswith(prefix):')
    elif name.startswith("read-before-writer-"):
        hold = name.endswith("hold")
        kind = "task.held" if hold else "task.released"
        key = "key" if hold else "task_holds.lease_key(task)"
        anchor = f'        with self.store.transaction("{kind}", data=data) as tx:'
        replace(daemon, anchor,
                f'        condition_row = self.store.one(task_holds.ROW_SQL, ({key},))\n' + anchor)
        replace(daemon, f'            row = tx.execute(task_holds.ROW_SQL, ({key},)).fetchone()',
                '            row = condition_row')
    elif name in {"condition-exit-zero", "condition-exit-two"}:
        value = "OK" if name.endswith("zero") else "INVALID_INPUT"
        replace(daemon, 'Exit.REFUSED, reason="task-hold-condition",',
                f'Exit.{value}, reason="task-hold-condition",', count=2)
    elif name.startswith("substring-"):
        value = "replaced" if name.endswith("hold") else "ended"
        replace(daemon, f'not {value}["note"].startswith(prefix)', f'prefix not in {value}["note"]')
    elif name == "send-to-unsupported-daemon":
        replace(cli, 'if protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY not in _daemon_capabilities(client):',
                'if False:')
    elif name.startswith("event-on-refusal-"):
        action = name.removeprefix("event-on-refusal-")
        method = "_hold_task" if action == "hold" else "_release_task"
        kind = "task.held" if action == "hold" else "task.released"
        replace(daemon, f'            return self.{method}(a)',
                f'            try:\n'
                f'                return self.{method}(a)\n'
                f'            except protocol.ProtocolError as exc:\n'
                f'                if exc.reason == "task-hold-condition":\n'
                f'                    self.store.add_event("{kind}", data={{"task": a.task}})\n'
                f'                raise')
    elif name == "same-exit-code":
        replace(cli, 'return fail(Exit.CLI_TOO_OLD,\n                            "task-hold-capability:',
                'return fail(Exit.REFUSED,\n                            "task-hold-capability:')
    elif name == "json-hold-only-on-stderr":
        replace(cli, 'emit({"refused": exc.reason, "task_hold": exc.details["task_hold"]})',
                'note(json.dumps({"refused": exc.reason, "task_hold": exc.details["task_hold"]}))')
    elif name == "json-hold-without-since":
        replace(cli, 'emit({"refused": exc.reason, "task_hold": exc.details["task_hold"]})',
                'emit({"refused": exc.reason, "task_hold": {key: value for key, value in exc.details["task_hold"].items() if key != "since"}})')
    else:
        raise ValueError(name)


MUTANTS = {
    "new-note-instead-of-held-note": FOREIGN,
    "expired-row-in-force": FAKE + "::test_expired_foreign_hold_allows_conditional_hold_but_blocks_release",
    "release-deletes-foreign-row": FOREIGN,
    "read-before-writer-hold": RACE,
    "read-before-writer-release": RACE,
    "condition-exit-zero": FOREIGN,
    "condition-exit-two": FOREIGN,
    "substring-hold": FOREIGN,
    "substring-release": FOREIGN,
    "send-to-unsupported-daemon": CLI + "::test_condition_is_never_sent_without_advertised_capability",
    "event-on-refusal-hold": FOREIGN,
    "event-on-refusal-release": FOREIGN,
    "same-exit-code": CLI + "::test_tasks_outcome_codes_are_distinct_by_outcome_category",
    "json-hold-only-on-stderr": JSON_REFUSAL,
    "json-hold-without-since": JSON_REFUSAL,
}


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="sf-task-hold-cas-mutants-") as folder:
        base = Path(folder)
        for name, test in MUTANTS.items():
            copy = base / name
            copy.mkdir()
            for package in ("subfleet", "tests"):
                shutil.copytree(ROOT / package, copy / package,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")
            mutate(copy, name)
            env = {**os.environ, "PYTHONPATH": str(copy), "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", test,
                                  "--basetemp", str(base / (name + "-pytest"))],
                                 cwd=copy, env=env, text=True, capture_output=True, timeout=120)
            # Require a real assertion failure, not an import, syntax or setup error.
            killed = run.returncode == 1 and "FAILED " in run.stdout and "ERROR " not in run.stdout
            result = {"mutant": name, "test": test, "exit": run.returncode, "killed": killed,
                      "output": run.stdout + run.stderr}
            results.append(result)
            print(f"{name}: {'KILLED' if killed else 'SURVIVED OR INVALID'}", flush=True)
    destination = ROOT / "docs/reports/2026-11-07-task-hold-cas-mutation-results.json"
    destination.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(row["killed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
