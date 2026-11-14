"""Reproduce the six review-r1 follow-up mutants in one disposable source copy.

Each mutant starts from pristine bytes, runs its named killing test, and is
restored and byte-checked in a finally block. The assigned checkout is read-only
to this runner except for the explicit JSON evidence file.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
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
MUTANTS = {
    "old-conditional-action-encoding": CLI + "::test_condition_uses_refusable_action_without_capability_preflight",
    "hold-if-ignores-condition": FAKE + "::test_foreign_hold_refuses_without_changing_any_row_or_event",
    "json-refusal-without-details-is-silent": CLI + "::test_json_condition_refusal_without_details_prints_null",
    "daemon-accepts-spaced-prefix": FAKE + "::test_raw_protocol_spaced_prefix_is_refused_without_changes_for_both_encodings",
    "absent-release-drops-other-holds": FAKE + "::test_conditional_release_with_no_row_is_successful_and_writes_nothing",
    "invalid-action-always-means-old-daemon": CLI + "::test_invalid_conditional_request_keeps_original_refusal_when_v2_is_advertised",
}


def replace(path: Path, old: str, new: str, *, count=1):
    source = path.read_text()
    assert source.count(old) == count, (path, old, source.count(old), count)
    path.write_text(source.replace(old, new))


def mutate(copy: Path, name: str):
    cli, daemon = copy / "subfleet/cli.py", copy / "subfleet/daemon.py"
    if name == "old-conditional-action-encoding":
        replace(cli, 'payload["action"] = f"{action}-if"', 'payload["action"] = action')
    elif name == "hold-if-ignores-condition":
        replace(daemon,
                'if prefix is not None and replaced is not None and not replaced["note"].startswith(prefix):',
                'if a.action != "hold-if" and prefix is not None and replaced is not None and not replaced["note"].startswith(prefix):')
    elif name == "json-refusal-without-details-is-silent":
        replace(cli, 'if exc.reason == "task-hold-condition":\n            if args.json:',
                'if exc.reason == "task-hold-condition":\n            if args.json and exc.details:')
    elif name == "daemon-accepts-spaced-prefix":
        replace(daemon,
                '            if prefix is not None and prefix != a.if_note_prefix:\n'
                '                raise ValueError("--if-note-prefix must not have surrounding spaces")\n',
                '', count=2)
    elif name == "absent-release-drops-other-holds":
        replace(daemon,
                'return {"released": task, "was_held": False, "woken": 0,\n'
                '                        "task_holds": task_holds.listing(self._task_holds_now())}',
                'return {"released": task, "was_held": False, "woken": 0, "task_holds": []}')
    elif name == "invalid-action-always-means-old-daemon":
        replace(cli, 'if prefix is not None and exc.code == Exit.INVALID_INPUT:',
                'if prefix is not None:')
        replace(cli, 'if capability not in _daemon_capabilities(client):', 'if True:')
    else:
        raise ValueError(name)


def environment(copy: Path, scratch: Path) -> dict[str, str]:
    tree = ast.parse((copy / "subfleet/home_paths.py").read_text())
    overrides = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign)
                     and any(isinstance(name, ast.Name) and name.id == "SAFE_HOME_PATHS"
                             for name in node.targets))
    result = {"PATH": os.defpath, "HOME": str(scratch / "home"), "TMPDIR": str(scratch),
              "PYTHONPATH": str(copy),
              "UV_CACHE_DIR": str(scratch / "uv-cache")}
    result.update({name: str(scratch / relative) for name, relative in overrides.items()})
    return result


def prepare(copy: Path) -> None:
    print(f"Preparing source/config and Python test helpers: {copy}", flush=True)
    copy.mkdir()
    shutil.copytree(ROOT / "subfleet", copy / "subfleet",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # These six tests use synthetic socket replies and the state-only daemon
    # fixture. They load Python helpers, never binary/e2e fixture collections.
    for folder, directories, filenames in os.walk(ROOT / "tests"):
        directories[:] = [name for name in directories if name not in {"__pycache__", "fixtures", "bin"}]
        for name in filenames:
            if name.endswith(".py"):
                source = Path(folder) / name
                destination = copy / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", type=Path,
                        help="copy pristine source now, without applying a mutant or running pytest")
    parser.add_argument("--prepared-source", type=Path,
                        help="use a previously prepared source copy under TMPDIR")
    parser.add_argument("--stop-at-utc", metavar="HH:MM",
                        help="stop this run by the given UTC time today")
    args = parser.parse_args()
    if args.prepare_only is not None:
        assert args.prepared_source is None
        assert args.prepare_only.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        prepare(args.prepare_only)
        print(f"Prepared pristine source: {args.prepare_only}")
        return 0
    results = []
    destination = ROOT / "docs/reports/2026-11-07-task-hold-cas-p3-mutation-results.json"
    deadline = None
    if args.stop_at_utc:
        hour, minute = map(int, args.stop_at_utc.split(":"))
        deadline = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    with tempfile.TemporaryDirectory(prefix="sf-cas-p3-mutants-") as folder:
        scratch = Path(folder)
        if args.prepared_source is None:
            copy = scratch / "source"
            prepare(copy)
        else:
            copy = args.prepared_source.resolve()
            assert copy.is_relative_to(Path(tempfile.gettempdir()).resolve())
            assert copy.is_dir()
        files = ("subfleet/cli.py", "subfleet/daemon.py")
        pristine = {name: (copy / name).read_bytes() for name in files}
        assert all((ROOT / path).read_bytes() == content for path, content in pristine.items())
        for name, test in MUTANTS.items():
            timeout = 900 if deadline is None else min(900, (deadline - datetime.now(timezone.utc)).total_seconds())
            if timeout <= 0:
                print("Stopped at requested UTC deadline", flush=True)
                break
            assert all((copy / path).read_bytes() == content for path, content in pristine.items())
            print(f"Running {name}: {test}", flush=True)
            try:
                mutate(copy, name)
                try:
                    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--tb=short", test,
                                          "--basetemp", str(scratch / (name + "-pytest"))],
                                         cwd=copy, env=environment(copy, scratch), text=True,
                                         capture_output=True, timeout=timeout)
                    output = run.stdout + run.stderr
                    killed = run.returncode == 1 and "FAILED " in output and "ERROR " not in output
                    result = {"mutant": name, "test": test, "exit": run.returncode, "killed": killed,
                              "output": output}
                except subprocess.TimeoutExpired as error:
                    parts = [part.decode(errors="replace") if isinstance(part, bytes) else part or ""
                             for part in (error.stdout, error.stderr)]
                    killed = False
                    result = {"mutant": name, "test": test, "exit": "timeout", "killed": False,
                              "output": "".join(parts), "timeout_seconds": timeout}
            finally:
                for path, content in pristine.items():
                    (copy / path).write_bytes(content)
                assert all((copy / path).read_bytes() == content for path, content in pristine.items())
                assert all((ROOT / path).read_bytes() == content for path, content in pristine.items())
            result["restored_sha256"] = {path: hashlib.sha256(content).hexdigest()
                                         for path, content in pristine.items()}
            results.append(result)
            destination.write_text(json.dumps(results, indent=2) + "\n")
            print(f"{name}: {'KILLED' if killed else 'SURVIVED OR INVALID'}; restored bytes checked", flush=True)
    destination.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if len(results) == len(MUTANTS) and all(row["killed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
