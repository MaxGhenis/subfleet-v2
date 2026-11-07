"""Offline, archived-reference cost of a 300-fresh-job admission pass.

Run with the development Python and a Darwin-user TMPDIR (a literal /tmp
component is rejected by the submission fixture):

    TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache" \
      uv run --no-sync python tools/benchmark_tree_thrash.py --samples 5

Both references are extracted with git archive. Each sample runs in a new
process, with an isolated HOME and state root; no provider or daemon is started.
The default synthetic workspace isolates admission's cost: it creates an empty
directory and returns submit's HEAD/baseline. --workspace git runs real Git on
a one-file repository instead. Instruction counts are proc_pid_rusage V4's
ri_instructions for the worker, and exclude instructions in Git children.
Store read counts cover query/one calls and the rows they return, excluding
direct tx.execute calls. Instrumentation is identical for both references.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
from pathlib import Path
import statistics
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


SCENARIOS = ("closed", "controller", "open")
GIT = "/usr/bin/git"


def git_environment() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(directory: Path, *args: str) -> str:
    return subprocess.run([GIT, "-C", str(directory), *args], check=True,
                          capture_output=True, text=True, env=git_environment()).stdout.strip()


def run_sample(repository: Path, scenario: str, jobs: int, workspace_mode: str) -> dict:
    """Imports happen only after the archived repository and isolated HOME are chosen."""
    sys.path.insert(0, str(repository))
    import pytest
    from tests.cpu_scaling import retired_instructions
    from tests.fake.conftest import Harness
    from tests.fake_adapter import FakeAdapter
    from subfleet import daemon as daemon_module
    from subfleet.adapters import registry
    from subfleet.contracts import Closure, ClosureReason, ClockSource, Reading, ReadingLabel
    from subfleet.daemon import Daemon, after, utcnow
    from subfleet.home_paths import SAFE_HOME_PATHS
    from subfleet.procs import Containment, ProcessTable

    @contextlib.contextmanager
    def fleet_daemon(root):
        # The admission-latency fixture's construction, without importing its
        # unrelated test modules in every fresh measurement process.
        root.mkdir(parents=True)
        harness = Harness(root)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
            patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
            patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
            patch.setattr(daemon_module.procs, "snapshot", lambda: ProcessTable({
                os.getpid(): (1, os.getpgrp(), "S", "fixture-start")}, "fixture-boot"))
            patch.setattr(daemon_module.procs, "identity", lambda pid: daemon_module.procs.snapshot().identity(pid))
            patch.setattr(daemon_module.procs, "group_members", lambda *args: {})
            patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
            patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
            patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
            patch.setattr(Daemon, "_start_machine_sampler", lambda service: None)
            patch.setattr(Daemon, "_machine_reading", lambda service: {
                "load1": .5, "load5": .5, "cpus": 18, "memory_pressure": 1, "observed_at": 0.0})
            service = Daemon(root)
            try:
                yield service, harness, patch
            finally:
                service.close()

    with tempfile.TemporaryDirectory(prefix="tree-thrash-cost-state-") as directory:
        root = Path(directory).resolve()
        if "tmp" in root.parts:
            raise ValueError("use TMPDIR=$(getconf DARWIN_USER_TEMP_DIR), without a /tmp component")
        for variable, relative in SAFE_HOME_PATHS.items():
            os.environ[variable] = str(root / "native" / relative)
        with fleet_daemon(root / "state") as (service, harness, patch):
            # Every identity and every filesystem lookup belongs to this fixture.
            patch.setattr(service, "_desktop_identity", lambda: None)
            patch.setattr(service, "_desktop_in_use", lambda: False)
            patch.setattr(service, "_record_desktop_use", lambda: None)
            patch.setattr(service, "_session_rows", lambda: {"rows": [], "unreadable": [], "groups": {}})
            patch.setattr(service, "_machine_reading", lambda: {
                "load1": .5, "load5": .5, "cpus": 18, "memory_pressure": 1, "observed_at": 0.0})
            patch.setattr(service, "_schedule", lambda *args, **kwargs: None)
            service.policy["caps"].update(max_active_attempts=None, max_in_flight_per_lane=None,
                                          max_in_flight_unmeasured=None, reading_ttl_s=3600,
                                          workspace_git_timeout_s=300)
            service.policy.setdefault("admission", {}).setdefault("disk", {})["enabled"] = False
            service.policy["admission"].setdefault("ramp", {})["enabled"] = False
            with service.store.transaction("fixture.one_lane") as tx:
                tx.execute("DELETE FROM lanes WHERE lane_id <> 'codex-1'")
            service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                              ReadingLabel.PROVIDER, "fixture", utcnow()))

            git(harness.workdir, "init", "-b", "task/benchmark")
            (harness.workdir / "tracked.txt").write_text("baseline\n")
            git(harness.workdir, "add", "tracked.txt")
            git(harness.workdir, "-c", "user.name=Offline Benchmark", "-c",
                "user.email=benchmark@example.invalid", "commit", "-qm", "fixture")
            head, baseline = git(harness.workdir, "rev-parse", "HEAD", "HEAD^{tree}").splitlines()
            # Fixture setup is outside the measured interval. The source is a
            # clean, one-file repository at its top level, so reuse the exact
            # immutable answers here instead of paying for Git again during
            # submit. Restore the real helpers before any measured admission.
            with pytest.MonkeyPatch.context() as submission:
                submission.setattr(daemon_module, "git_head", lambda *args, **kwargs: head)
                submission.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: str(harness.workdir))
                submission.setattr(daemon_module, "git_common_dir", lambda *args, **kwargs: str(harness.workdir / ".git"))
                submission.setattr(daemon_module, "_git_prefix", lambda *args, **kwargs: ".")
                seed = service.dispatch("submit", harness.submit_args(sandbox="workspace-write", caller_session=None,
                                        pinned_lane="codex-1"))["job_id"]
            template = service.store.get_job(seed)
            columns = tuple(template)
            manifest = (service.root / "jobs" / seed / "manifest.json").read_bytes()
            values = []
            for number in range(1, jobs):
                job_id = f"benchmark-{number:04}"
                row = {**template, "job_id": job_id, "request_id": job_id}
                values.append(tuple(row[column] for column in columns))
                jobdir = service.root / "jobs" / job_id
                jobdir.mkdir()
                (jobdir / "manifest.json").write_bytes(manifest)
            with service.store.transaction("fixture.seed") as tx:
                tx.executemany(f"INSERT INTO jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                               values)

            def close_lane() -> None:
                service.store.put_closure(Closure("codex-1", "account", after(3600),
                    ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "offline benchmark controller"))

            if scenario == "closed":
                close_lane()
            reservations = []

            def boundary(name, job_id, attempt_id):
                if name == "reserved":
                    reservations.append((job_id, service.store.get_attempt(attempt_id)["lane_id"]))
                    if scenario == "controller" and len(reservations) == 2:
                        close_lane()
            patch.setattr(service, "_boundary", boundary)

            counts = collections.Counter()
            original_workspace = service._workspace

            def workspace(job):
                tree = service.root / "worktrees" / job["job_id"]
                if not tree.exists():
                    counts["cuts"] += 1
                counts["workspace_calls"] += 1
                if workspace_mode == "git":
                    return original_workspace(job)
                tree.mkdir(parents=True, exist_ok=True)
                return str(tree), job["workdir_head"], baseline, []
            patch.setattr(service, "_workspace", workspace)
            for method in ("query", "one"):
                original = getattr(service.store, method)

                def counted(*args, _method=method, _original=original, **kwargs):
                    rows = _original(*args, **kwargs)
                    counts["store_read_calls"] += 1
                    counts["store_rows"] += (len(rows) if _method == "query" else int(rows is not None))
                    return rows
                patch.setattr(service.store, method, counted)
            original_pick = service._pick

            def pick(*args, **kwargs):
                counts["evaluations"] += 1
                return original_pick(*args, **kwargs)
            patch.setattr(service, "_pick", pick)
            holds, tally = {}, {"placed": 0}
            # Warm only the counter reader; every admission sample starts fresh.
            retired_instructions()
            instruction_start = retired_instructions()
            cpu_start, wall_start = time.thread_time(), time.perf_counter()
            service._admit_pass(holds, tally)
            wall_s, cpu_s = time.perf_counter() - wall_start, time.thread_time() - cpu_start
            instructions = retired_instructions() - instruction_start
            counts["reservations"] = len(reservations)
            assert counts["reservations"] == (0 if scenario == "closed" else 2 if scenario == "controller" else jobs)
            return {"scenario": scenario, "jobs": jobs, "workspace": workspace_mode,
                    "wall_s": wall_s, "thread_cpu_s": cpu_s, "instructions": instructions,
                    **{name: counts[name] for name in ("cuts", "workspace_calls", "evaluations",
                                                      "store_read_calls", "store_rows", "reservations")}}


def archive(repository: Path, reference: str, destination: Path) -> str:
    commit = git(repository, "rev-parse", reference)
    destination.mkdir()
    archive_path = destination.parent / (destination.name + ".tar")
    # Documentation image archives are large and are never read by this fixture.
    git(repository, "archive", "--format=tar", f"--output={archive_path}", commit,
        "subfleet", "tests", "pyproject.toml")
    with tarfile.open(archive_path) as stream:
        stream.extractall(destination, filter="data")
    archive_path.unlink()
    return commit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-ref", default="a8624a489")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--head-overlay", nargs="+", default=[], metavar="RELATIVE_PATH",
                        help="overlay named worktree files on the head archive when commits are denied")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--jobs", type=int, default=300)
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+", default=list(SCENARIOS))
    parser.add_argument("--workspace", choices=("synthetic", "git"), default="synthetic")
    parser.add_argument("--temp-root", type=Path,
                        help="writable directory under Darwin TMPDIR when the sandbox restricts its parent")
    parser.add_argument("--output", type=Path, help="also save the complete JSON result")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.temp_root:
        tempfile.tempdir = str(args.temp_root.resolve())
    if args.jobs < 2 or args.samples < 1:
        parser.error("jobs must be at least two, samples at least one")
    if args.worker:
        with tempfile.TemporaryDirectory(prefix="tree-thrash-cost-home-") as directory:
            os.environ["HOME"] = directory
            print(json.dumps(run_sample(args.repo.resolve(), args.scenarios[0], args.jobs, args.workspace)), flush=True)
        return
    repository = Path(__file__).resolve().parents[1]
    records = []
    with tempfile.TemporaryDirectory(prefix="tree-thrash-cost-") as directory:
        root = Path(directory).resolve()
        if "tmp" in root.parts:
            parser.error("set TMPDIR=$(getconf DARWIN_USER_TEMP_DIR); /tmp is submission scratch")
        refs = {"base": args.base_ref, "head": args.head_ref}
        commits = {label: archive(repository, ref, root / label) for label, ref in refs.items()}
        overlay = {}
        for relative in args.head_overlay:
            source = (repository / relative).resolve()
            if not source.is_relative_to(repository) or not source.is_file():
                parser.error(f"overlay must be a file inside this worktree: {relative}")
            target = root / "head" / source.relative_to(repository)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            overlay[str(source.relative_to(repository))] = hashlib.sha256(source.read_bytes()).hexdigest()
        print(json.dumps({"commits": commits, "head_overlay_sha256": overlay,
                          "python": sys.version, "samples": args.samples}), flush=True)
        for scenario in args.scenarios:
            for sample in range(args.samples):
                # Alternate order so a warming or slowing host affects both references.
                for label in (("base", "head") if sample % 2 == 0 else ("head", "base")):
                    home = root / f"home-{scenario}-{sample}-{label}"
                    home.mkdir()
                    env = {**git_environment(), "HOME": str(home), "PYTHONPATH": str(root / label),
                           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(root)}
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                        "--repo", str(root / label), "--jobs", str(args.jobs), "--scenarios", scenario,
                        "--workspace", args.workspace], cwd=root / label, env=env,
                        text=True, capture_output=True, check=False)
                    if completed.returncode:
                        sys.stderr.write(completed.stderr)
                        raise RuntimeError(f"{label} {scenario} sample {sample} worker exited {completed.returncode}")
                    record = {"label": label, "sample": sample, **json.loads(completed.stdout)}
                    records.append(record)
                    print(json.dumps(record), flush=True)
                    if args.output:
                        args.output.write_text(json.dumps({"commits": commits, "head_overlay_sha256": overlay,
                            "samples": records, "complete": False}, indent=2) + "\n")
        summary = []
        for scenario in args.scenarios:
            for label in refs:
                selected = [record for record in records if record["scenario"] == scenario and record["label"] == label]
                entry = {"label": label, "scenario": scenario}
                for metric in ("wall_s", "thread_cpu_s", "instructions", "cuts", "workspace_calls",
                               "evaluations", "store_read_calls", "store_rows", "reservations"):
                    values = [record[metric] for record in selected]
                    entry[metric] = {"min": min(values), "median": statistics.median(values), "max": max(values)}
                summary.append(entry)
        result = {"commits": commits, "head_overlay_sha256": overlay,
                  "samples": records, "summary": summary, "complete": True}
        print(json.dumps({"summary": summary}), flush=True)
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
