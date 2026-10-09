"""Run one bounded, supervised slice of the remaining historical real probes.

Use the task TMPDIR and tools/quarantine_fix9_run.py as the outer foreground
bound. Examples: --selection realcwd, --selection round3-extra, or
--selection detachedreal. The full round3 selection is also available.

Nested detached writers and guardian launchers are not supervised by this
in-process runner. Collection must prove their existing skip marks apply;
otherwise the whole slice stops before any test fixture or writer starts.
Direct new-session processes are recorded before interrupts are unblocked,
then their exact groups and direct children are stopped/reaped at teardown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
ROUND3 = "tests/fake/test_review_pr131_round3.py"
DETACHED = "tests/fake/test_quarantine_detached_writers.py"
SELECTIONS = {
    "realcwd": ([DETACHED + "::test_real_cwd_scan_sees_platform_and_retitled_processes"], 5),
    "detachedreal": ([DETACHED + "::test_real_detached_writers_hold_then_release"], 20),
    "round3-extra": ([ROUND3, "-k", "not test_only_the_full_documented_evasive_combination_escapes "
                      "and not test_automatic_and_confirm_dead_take_the_same_census_and_decision"], 7),
    "round3": ([ROUND3], 14),
}
NESTED_TESTS = {
    DETACHED + "::test_real_detached_writers_hold_then_release",
    ROUND3 + "::test_sigkill_during_child_publication_never_executes_the_provider",
    ROUND3 + "::test_kill_escalation_against_each_guardian_version",
}
INTERRUPTS = {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}


@contextmanager
def blocked_interrupts():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTS)
    try:
        yield previous
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@dataclass
class OwnedChild:
    process: subprocess.Popen
    pgid: int | None
    node: str | None
    closed: bool = False


class OwnedProcesses:
    def __init__(self):
        self.original = subprocess.Popen
        self.signature = inspect.signature(self.original)
        self.children: list[OwnedChild] = []
        self.cleanup_records = []
        self.node = None

    def install(self):
        registry = self

        class TrackedPopen(registry.original):
            def __init__(self, *args, **kwargs):
                # An interrupt between fork and registration otherwise loses
                # the only reference to a child created inside test code.
                with blocked_interrupts() as previous_mask:
                    bound = registry.signature.bind(*args, **kwargs)
                    new_session = bool(bound.arguments.get("start_new_session", False))
                    if bound.arguments.get("shell", False):
                        raise ValueError("supervised probes require an argv sequence")
                    argv = bound.arguments["args"]
                    argv = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)
                    argv = [os.fsdecode(value) for value in argv]
                    executable = os.fsdecode(bound.arguments.get("executable") or argv[0])
                    # Restore every child's original mask, including ordinary
                    # bounded inspection commands: lsof must retain SIGALRM.
                    # The exec adapter avoids threaded preexec_fn use.
                    child_code = (
                        "import os,signal,sys; "
                        f"signal.pthread_sigmask(signal.SIG_SETMASK,{sorted(map(int, previous_mask))!r}); "
                        "os.execvpe(sys.argv[1],sys.argv[2:],os.environ)"
                    )
                    bound.arguments["args"] = [sys.executable, "-c", child_code, executable, *argv]
                    if "executable" in bound.arguments:
                        bound.arguments["executable"] = None
                    super().__init__(*bound.args, **bound.kwargs)
                    pgid = self.pid if new_session else None  # setsid completed before Popen returns.
                    if pgid is not None:
                        assert pgid > 1 and pgid != os.getpgrp()
                    registry.children.append(OwnedChild(self, pgid, registry.node))

        subprocess.Popen = TrackedPopen

    @staticmethod
    def group_exists(pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def active(self, child):
        return (self.group_exists(child.pgid) if child.pgid is not None
                else child.process.poll() is None)

    def send(self, child, sig):
        try:
            if child.pgid is not None:
                os.killpg(child.pgid, sig)
            elif child.process.poll() is None:
                child.process.send_signal(sig)
        except ProcessLookupError:
            pass

    def cleanup(self):
        # This bounded cleanup cannot itself be interrupted halfway through
        # killing/reaping the exact registered groups.
        with blocked_interrupts():
            pending = []
            for child in self.children:
                if child.closed:
                    continue
                if not self.active(child):
                    child.process.wait(timeout=1)
                    child.closed = True
                    if child.pgid is not None:
                        self.cleanup_records.append({"pid": child.process.pid, "pgid": child.pgid,
                                                     "node": child.node, "returncode": child.process.returncode,
                                                     "still_active": False, "signals": []})
                        print(f"OWNED REAP pid={child.process.pid} pgid={child.pgid} "
                              "reaped=True active=False", flush=True)
                    continue
                pending.append(child)
                self.send(child, signal.SIGTERM)
            deadline = time.monotonic() + 1
            while pending and time.monotonic() < deadline and any(self.active(child) for child in pending):
                time.sleep(.02)
            for child in pending:
                self.send(child, signal.SIGKILL)
            deadline = time.monotonic() + 3
            for child in pending:
                child.process.wait(timeout=max(.01, deadline - time.monotonic()))
            while pending and time.monotonic() < deadline and any(self.active(child) for child in pending):
                time.sleep(.02)
            remaining = []
            for child in pending:
                active = self.active(child)
                self.cleanup_records.append({"pid": child.process.pid, "pgid": child.pgid,
                                             "node": child.node, "returncode": child.process.returncode,
                                             "still_active": active, "signals": ["SIGTERM", "SIGKILL"]})
                print(f"OWNED CLEANUP pid={child.process.pid} pgid={child.pgid} "
                      f"reaped={child.process.returncode is not None} active={active}", flush=True)
                child.closed = not active
                if active:
                    remaining.append((child.process.pid, child.pgid))
            if remaining:
                raise RuntimeError(f"registered process groups still exist after bounded cleanup: {remaining}")


def workspace_output(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("evidence output must stay in the assigned workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def task_temp():
    parent = Path(os.environ["TMPDIR"]).resolve()
    darwin = Path(subprocess.check_output(
        ["getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=5).strip()).resolve()
    if parent == darwin or not parent.is_relative_to(darwin) or "tmp" in parent.parts:
        raise ValueError("TMPDIR must be a fresh Darwin-user child with no tmp path component")
    return parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", choices=SELECTIONS, default="realcwd")
    parser.add_argument("--timeout-s", type=int, default=1400)
    parser.add_argument("--max-examples", type=int, default=150)
    parser.add_argument("--deadline-ms", type=int, default=60000)
    parser.add_argument("--seed", type=int, default=131)
    parser.add_argument("--junitxml")
    parser.add_argument("--summary")
    args = parser.parse_args()
    if not (1 <= args.timeout_s <= 1400 and 1 <= args.deadline_ms <= 60000 and 1 <= args.max_examples <= 150):
        parser.error("timeout must be <=1400s, deadline <=60000ms, and max_examples <=150")
    os.chdir(ROOT)
    junit = workspace_output(args.junitxml or f"docs/reports/pr131-fix9/real-probes/{args.selection}.xml")
    summary_path = workspace_output(args.summary or f"docs/reports/pr131-fix9/real-probes/{args.selection}.json")
    selected, expected_count = SELECTIONS[args.selection]
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "subfleet/procs.py", "subfleet/daemon.py", ROUND3, DETACHED, "tools/quarantine_fix9_real_probes.py")}
    summary = {"selection": args.selection, "timeout_s": args.timeout_s,
               "deadline_ms": args.deadline_ms, "max_examples": args.max_examples,
               "seed": args.seed, "source_hashes": sources, "collected": [], "skip_preflight": [],
               "outcomes": {}}
    registry = OwnedProcesses()
    interrupted = None
    started = time.monotonic()
    previous_handlers = {sig: signal.getsignal(sig) for sig in INTERRUPTS}

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = signum
        raise KeyboardInterrupt(f"real probes interrupted by signal {signum}")

    for sig in INTERRUPTS:
        signal.signal(sig, interrupt)
    signal.alarm(args.timeout_s)
    registry.install()
    code = 2
    test_directory = None
    try:
        parent = task_temp()
        test_directory = Path(tempfile.mkdtemp(prefix=f"real-probes-{args.selection}-", dir=parent))
        sys.path.insert(0, str(ROOT))
        sys.dont_write_bytecode = True
        os.environ["PYTEST_ADDOPTS"] = ""
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        import pytest
        from _pytest.skipping import evaluate_skip_marks
        from hypothesis import settings

        class Preflight:
            @pytest.hookimpl(trylast=True)
            def pytest_collection_modifyitems(self, items):
                summary["collected"] = [item.nodeid for item in items]
                if len(items) != expected_count:
                    raise pytest.UsageError(f"expected {expected_count} audited cases, collected {len(items)}")
                for item in items:
                    base = item.nodeid.split("[", 1)[0]
                    skipped = evaluate_skip_marks(item)
                    if skipped is not None:
                        summary["skip_preflight"].append({"node": item.nodeid, "reason": skipped.reason})
                        print(f"SKIP PREFLIGHT {item.nodeid}: {skipped.reason}", flush=True)
                    if base in NESTED_TESTS and skipped is None:
                        raise pytest.UsageError(
                            f"nested writer probe is not skipped: {item.nodeid}; "
                            "stop before fixtures and arrange explicit nested-process supervision")
                    prior = getattr(item.obj, "_hypothesis_internal_use_settings", None)
                    if prior is not None:
                        deadline_ms = args.deadline_ms if prior.deadline is None else min(
                            args.deadline_ms, int(prior.deadline.total_seconds() * 1000))
                        item.obj._hypothesis_internal_use_settings = settings(
                            prior, max_examples=min(prior.max_examples, args.max_examples),
                            deadline=deadline_ms, database=None)

            @pytest.hookimpl(tryfirst=True)
            def pytest_runtest_setup(self, item):
                registry.node = item.nodeid
                if item.nodeid.split("[", 1)[0] in NESTED_TESTS and evaluate_skip_marks(item) is None:
                    raise pytest.UsageError("nested-process skip preflight changed after collection")

            @pytest.hookimpl(hookwrapper=True, trylast=True)
            def pytest_runtest_teardown(self, item):
                try:
                    yield
                finally:
                    registry.cleanup()
                    registry.node = None

            def pytest_runtest_logreport(self, report):
                if report.failed:
                    summary["outcomes"][report.nodeid] = "failed" if report.when == "call" else "error"
                elif report.skipped:
                    summary["outcomes"][report.nodeid] = "xfailed" if hasattr(report, "wasxfail") else "skipped"
                elif report.when == "call":
                    summary["outcomes"][report.nodeid] = "xpassed" if hasattr(report, "wasxfail") else "passed"

        code = int(pytest.main(["--assert=plain", "-q", "-ra", "-s", *selected,
                               "--basetemp=" + str(test_directory), "--hypothesis-seed=" + str(args.seed),
                               "--hypothesis-show-statistics", "--junitxml=" + str(junit)], plugins=[Preflight()]))
        if interrupted is not None:
            code = 124 if interrupted == signal.SIGALRM else 130
    except KeyboardInterrupt:
        code = 124 if interrupted == signal.SIGALRM else 130
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        # Cancel the timer, finish all exact ownership cleanup, then restore
        # the public subprocess class and signal handlers.
        signal.alarm(0)
        for sig in INTERRUPTS:
            signal.signal(sig, signal.SIG_IGN)
        try:
            registry.cleanup()
        except Exception as exc:
            summary["cleanup_error"] = str(exc)
            traceback.print_exc()
            code = 1
        finally:
            subprocess.Popen = registry.original
        if test_directory is not None:
            shutil.rmtree(test_directory)
        after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
        summary.update(returncode=code, elapsed_s=round(time.monotonic() - started, 3),
                       interrupted_by=interrupted, source_hashes_restored=after == sources,
                       source_hashes_after=after, counts=dict(Counter(summary["outcomes"].values())),
                       direct_children_registered=len(registry.children),
                       new_session_groups_registered=sum(child.pgid is not None for child in registry.children),
                       owned_cleanup=registry.cleanup_records,
                       remaining_children=[{"pid": child.process.pid, "pgid": child.pgid}
                                           for child in registry.children if not child.closed])
        if after != sources or summary["remaining_children"]:
            summary["returncode"] = code = 1
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        print("REAL PROBES " + json.dumps({key: summary[key] for key in (
            "selection", "returncode", "counts", "source_hashes_restored", "remaining_children")}), flush=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return code


if __name__ == "__main__":
    sys.exit(main())
