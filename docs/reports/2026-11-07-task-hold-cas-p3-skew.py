"""Reproduce review-r1 task-hold skew checks without worktrees or live state.

Run with the development Python and TMPDIR set to permitted scratch. Archives
come only from this repository's objects. Daemon workers load the chosen tree's
real dispatcher/store with synthetic policy and clock; CLI workers load that
tree's real CLI/client. Requests cross a real Unix socket. Every child is joined.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
BASE = "a8624a489"
PREVIOUS = "e9e557b15"
PREFIX = "battery-backoff:"
UNTIL = "2026-10-09T16:43:00Z"
TABLES = ("leases", "jobs", "attempts", "events", "closures")


def environment(source: Path, scratch: Path, socket_root: Path) -> dict[str, str]:
    """Use synthetic paths only; inherit no account credentials or store paths."""
    tree = ast.parse((source / "subfleet/home_paths.py").read_text())
    overrides = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign)
                     and any(isinstance(name, ast.Name) and name.id == "SAFE_HOME_PATHS"
                             for name in node.targets))
    result = {"PATH": os.defpath, "HOME": str(scratch / "home"), "TMPDIR": str(scratch),
              "PYTHONPATH": str(source),
              "SUBFLEET_UNDER_PYTEST": "1", "UV_CACHE_DIR": str(scratch / "uv-cache")}
    result.update({name: str(scratch / relative) for name, relative in overrides.items()})
    result["SUBFLEET_HOME"] = str(socket_root)
    return result


def archive(revision: str, destination: Path) -> str:
    sha = subprocess.check_output(["git", "rev-parse", revision], cwd=ROOT, text=True).strip()
    marker = destination / ".archive-revision"
    if marker.exists():
        assert marker.read_text().strip() == sha
        print(f"Reusing archive {revision}", flush=True)
        return sha
    destination.mkdir()
    print(f"Archiving {revision}", flush=True)
    archive_path = destination.parent / f"{revision}.tar"
    with archive_path.open("wb") as stream:
        subprocess.run(["git", "archive", revision, "subfleet", ":(glob)tests/**/*.py", "pyproject.toml"],
                       cwd=ROOT, stdout=stream, check=True)
    with tarfile.open(archive_path) as stream:
        # The daemon/test fixture imports source modules and default policy;
        # app assets, reports and unrelated binary fixture trees are unused.
        members = [member for member in stream
                   if member.name.startswith("subfleet/")
                   or (member.name.startswith("tests/") and member.name.endswith(".py"))
                   or member.name == "pyproject.toml"]
        stream.extractall(destination, members=members, filter="data")
    archive_path.unlink()
    marker.write_text(sha + "\n")
    print(f"Archive ready {revision}: {len(members)} source/config files", flush=True)
    return sha


def daemon_worker(source: Path, socket_root: Path) -> int:
    sys.dont_write_bytecode = False
    print("worker: importing real daemon and socket fixture", file=sys.stderr, flush=True)
    sys.path.insert(0, str(source))
    from subfleet import capacity, daemon as daemon_module, protocol, scheduler, store as store_module
    from subfleet.daemon import Daemon
    from subfleet.procs import Containment
    from tests.unit.conftest import FakeDaemon
    from unittest.mock import patch

    clock = [60]

    def stamp(seconds):
        return (datetime(2026, 10, 9, 16, 33, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat(stamp(clock[0]).replace("Z", "+00:00"))
            return instant.astimezone(tz) if tz is not None else instant.astimezone().replace(tzinfo=None)

    def refuse_launch(*args, **kwargs):
        raise AssertionError("skew state-only daemon must never launch a provider")

    print("worker: imports ready; constructing isolated daemon", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="sf-skew-state-") as state, ExitStack() as patches:
        state = Path(state)
        policy = json.loads((source / "subfleet/default_policy.json").read_text())
        policy.setdefault("conversations", {})["catalog_interval_s"] = 0
        policy.setdefault("reserve", {})["models"] = []
        policy.setdefault("admission", {})["prove_idle_s"] = None
        policy.pop("requires", None)
        (state / "policy.json").write_text(json.dumps(policy))
        (state / "lanes.json").write_text("[]")
        stubs = {
            "boot_id": lambda: "unit-test-boot", "proc_start": lambda pid: "unit-test-start",
            "same_process": lambda *args: False,
            "snapshot": lambda: daemon_module.procs.ProcessTable(
                {os.getpid(): (1, os.getpgrp(), "S", "unit-test-start")}, "unit-test-boot"),
            "identity": lambda pid: daemon_module.procs.snapshot().identity(pid),
            "group_members": lambda *args: {}, "containment": lambda *args, **kwargs: Containment(),
        }
        for name, replacement in stubs.items():
            patches.enter_context(patch.object(daemon_module.procs, name, replacement))
        for module in (daemon_module, store_module, scheduler, capacity):
            patches.enter_context(patch.object(module, "datetime", ClockDatetime))
        for name, replacement in {
                "git_head": lambda *args, **kwargs: None, "git_toplevel": lambda *args, **kwargs: None,
                "utcnow": lambda: stamp(clock[0]), "after": lambda seconds: stamp(clock[0] + seconds),
        }.items():
            patches.enter_context(patch.object(daemon_module, name, replacement))
        patches.enter_context(patch.object(daemon_module.os, "kill", refuse_launch))
        patches.enter_context(patch.object(daemon_module.os, "killpg", refuse_launch))
        service = Daemon(state)
        service._launch = refuse_launch
        print("worker: isolated daemon ready", file=sys.stderr, flush=True)
        clock[0] = 60

        def forward(request):
            try:
                if service.conversations.owns(request.op):
                    return service.conversations.handle(request.op, request.args, None)
                return service.dispatch(request.op, request.args)
            except protocol.ProtocolError as error:
                return protocol.refuse(request.id, error)

        server = FakeDaemon(socket_root, {"tasks": forward, "capabilities": forward})
        try:
            print(json.dumps({"ready": True}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                if command["op"] == "quit":
                    break
                if command["op"] == "prepare":
                    with service.store.transaction("test.skew_prepare") as tx:
                        tx.execute("DELETE FROM leases WHERE lease_key LIKE 'task-hold:%'")
                    if command["note"] is not None:
                        service.dispatch("tasks", {"action": "hold", "task": "build",
                                                   "until": stamp(command.get("until", 500)),
                                                   "note": command["note"]})
                    server.requests.clear()
                image = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
                         for table in TABLES}
                print(json.dumps({"image": image,
                                  "requests": [{"op": item.op, "args": item.args}
                                               for item in server.requests]}), flush=True)
        finally:
            server.close()
            service.close()
    return 0


def cli_worker(source: Path, argv: list[str]) -> int:
    sys.dont_write_bytecode = False
    sys.path.insert(0, str(source))
    from subfleet import cli
    from subfleet.client import Client
    # Socket fixtures deliberately have no process-identity lock; test only the
    # real request/response path, without probing even the fixture's process.
    cli._client = lambda args: Client(Path(os.environ["SUBFLEET_HOME"]), verify_lock=False)
    return cli.main(argv)


def read_reply(process: subprocess.Popen, log: Path) -> dict:
    with selectors.DefaultSelector() as waiter:
        waiter.register(process.stdout, selectors.EVENT_READ)
        if not waiter.select(600):
            raise AssertionError(f"daemon worker timed out; log: {log.read_text()}")
    line = process.stdout.readline()
    if not line:
        raise AssertionError(f"daemon worker exited {process.poll()}; log: {log.read_text()}")
    return json.loads(line)


@contextmanager
def daemon(source: Path, scratch: Path, socket_root: Path):
    scratch.mkdir()
    socket_root.mkdir()
    log = scratch / "worker.log"
    with log.open("w") as errors:
        process = subprocess.Popen([sys.executable, str(SCRIPT), "daemon-worker", str(source),
                                    str(socket_root)], cwd=source,
                                   env=environment(source, scratch, socket_root),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, text=True)
        try:
            assert read_reply(process, log) == {"ready": True}

            def command(op, **kwargs):
                process.stdin.write(json.dumps({"op": op, **kwargs}) + "\n")
                process.stdin.flush()
                return read_reply(process, log)

            yield command
        finally:
            unwinding = sys.exc_info()[0] is not None
            forced_stop = False
            if process.poll() is None:
                try:
                    process.stdin.write('{"op":"quit"}\n')
                    process.stdin.flush()
                    process.wait(timeout=120)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    forced_stop = True
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            process.stdin.close()
            process.stdout.close()
            if forced_stop:
                print(f"Worker stopped and joined after grace period: {source.name}", flush=True)
            elif not unwinding:
                assert process.returncode == 0, log.read_text()


def invoke(source: Path, scratch: Path, socket_root: Path, action: str, *, json_mode=True):
    argv = ["tasks", action, "build", "--if-note-prefix", PREFIX]
    if action == "hold":
        argv += ["--until", UNTIL, "--note", PREFIX + " hot"]
    if json_mode:
        argv += ["--json"]
    result = subprocess.run([sys.executable, str(SCRIPT), "cli-worker", str(source), *argv],
                            cwd=source, env=environment(source, scratch, socket_root),
                            text=True, capture_output=True, timeout=120)
    return {"exit": result.returncode,
            "stdout": json.loads(result.stdout) if json_mode else result.stdout,
            "stderr": result.stderr}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache", type=Path,
                        help="reuse verified git archives in this permitted temporary directory")
    parser.add_argument("--skip-base", action="store_true",
                        help="resume after the twelve base probes passed in the recorded second run")
    args = parser.parse_args()
    results = {"archives": {}, "new_cli": [], "old_cli": []}
    destination = ROOT / "docs/reports/2026-11-07-task-hold-cas-p3-skew-results.json"
    if args.skip_base:
        evidence = ROOT / "docs/reports/2026-11-07-task-hold-cas-p3-skew-validation-2.txt"
        lines = evidence.read_text().splitlines()
        assert sum(line.startswith("  ") and line.endswith(": 6") for line in lines) == 12
        results["base_evidence"] = evidence.name
    with tempfile.TemporaryDirectory(prefix="sf-cas-p3-skew-") as folder, \
            tempfile.TemporaryDirectory(prefix="sf-hcs-", dir="/private/tmp") as sockets:
        scratch, short = Path(folder), Path(sockets)
        cache = args.source_cache or scratch
        if args.source_cache is not None:
            assert cache.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
            cache.mkdir(exist_ok=True)
        sources = {BASE: cache / "base", PREVIOUS: cache / "previous", "head": ROOT}
        for revision in (BASE, PREVIOUS):
            results["archives"][revision] = archive(revision, sources[revision])
        results["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        for version, source in sources.items():
            if args.skip_base and version == BASE:
                continue
            print(f"New CLI -> {version} daemon", flush=True)
            state, socket_root = scratch / f"new-{version}", short / f"n-{version}"
            with daemon(source, state, socket_root) as command:
                for action in ("hold", "release"):
                    for owner, note in (("foreign", "booking: operator"),
                                        ("own", PREFIX + " previous"), ("absent", None)):
                        for json_mode in (False, True):
                            before = command("prepare", note=note)["image"]
                            result = invoke(ROOT, state, socket_root, action, json_mode=json_mode)
                            after = command("snapshot")
                            row = {"daemon": version, "action": action, "owner": owner,
                                   "json": json_mode, **result, "requests": after["requests"]}
                            expected = 6 if version != "head" else (7 if owner == "foreign" else 0)
                            assert result["exit"] == expected, row
                            if expected in (6, 7) or (action == "release" and owner == "absent"):
                                assert after["image"] == before, row
                            if expected == 6 and json_mode:
                                assert result["stdout"] == {"refused": "task-hold-capability",
                                                             "capability": "tasks.if_note_prefix.v2"}, row
                            requests = after["requests"]
                            assert requests[0]["op"] == "tasks", row
                            assert requests[0]["args"]["action"] == action + "-if", row
                            assert requests[0]["args"]["if_note_prefix"] == PREFIX, row
                            assert [item["op"] for item in requests] == (
                                ["tasks", "capabilities"] if expected == 6 else ["tasks"]), row
                            row["unchanged"] = after["image"] == before
                            results["new_cli"].append(row)
                            destination.write_text(json.dumps(results, indent=2) + "\n")
                            print(f"  {action}/{owner}/{'json' if json_mode else 'text'}: {expected}", flush=True)
        compatibility = {}
        for version in (PREVIOUS, "head"):
            print(f"Old CLI -> {version} daemon", flush=True)
            source, state = sources[version], scratch / f"old-{version}"
            socket_root = short / f"o-{version}"
            with daemon(source, state, socket_root) as command:
                for action in ("hold", "release"):
                    for owner, note in (("foreign", "booking: operator"),
                                        ("own", PREFIX + " previous"), ("absent", None)):
                        before = command("prepare", note=note)["image"]
                        result = invoke(sources[PREVIOUS], state, socket_root, action)
                        after = command("snapshot")
                        expected = 7 if owner == "foreign" else 0
                        assert result["exit"] == expected, result
                        if expected == 7 or (action == "release" and owner == "absent"):
                            assert after["image"] == before
                        case = (action, owner)
                        observed = {**result, "leases": after["image"]["leases"],
                                    "events": [event for event in after["image"]["events"]
                                               if event["kind"].startswith("task.")]}
                        if version == PREVIOUS:
                            compatibility[case] = observed
                        else:
                            assert observed == compatibility[case], (case, observed, compatibility[case])
                        requests = after["requests"]
                        assert [item["op"] for item in requests] == ["capabilities", "tasks"]
                        assert requests[1]["args"]["action"] == action
                        assert requests[1]["args"]["if_note_prefix"] == PREFIX
                        results["old_cli"].append({"daemon": version, "action": action,
                                                   "owner": owner, **result, "same": True})
                        destination.write_text(json.dumps(results, indent=2) + "\n")
        destination.write_text(json.dumps(results, indent=2) + "\n")
    print(f"PASS: {len(results['new_cli'])} new-CLI probes; "
          f"{len(results['old_cli'])} old-CLI probes, 6 matching comparisons")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "daemon-worker":
        raise SystemExit(daemon_worker(Path(sys.argv[2]), Path(sys.argv[3])))
    if len(sys.argv) > 1 and sys.argv[1] == "cli-worker":
        raise SystemExit(cli_worker(Path(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(main())
