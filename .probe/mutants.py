"""Mutation probe for PR #47: apply one mutant at a time to the reviewed sources,
run the two catalog/service test files, record which tests fail, restore.

Run from the worktree root:  .venv/bin/python .probe/mutants.py [name ...]
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "subfleet/conversations/service.py"
CATALOG = ROOT / "subfleet/conversations/catalog.py"
TESTS = ["tests/unit/test_conversation_catalog.py", "tests/unit/test_conversation_service.py"]

MUTANTS = {
    "close_no_stop": (SERVICE, "        self._stop_catalog()\n        for runner", "        for runner"),
    "fence_write_kept_open": (SERVICE, "        if fence is not None:\n            os.close(fence[1])\n",
                              "        if fence is not None:\n            pass\n"),
    "owner_no_identity": (CATALOG,
                          "return (here.st_dev, here.st_ino) == (held.st_dev, held.st_ino) and fence_open(self.fence_fd)",
                          "return fence_open(self.fence_fd)"),
    "owner_always_true": (CATALOG, "    def __call__(self) -> bool:\n        try:",
                          "    def __call__(self) -> bool:\n        return True\n        try:"),
    "owner_fence_only": (CATALOG, "    def __call__(self) -> bool:\n        try:",
                         "    def __call__(self) -> bool:\n        return fence_open(self.fence_fd)\n        try:"),
    "no_sigterm_handler": (CATALOG, "    signal.signal(signal.SIGTERM, _terminated)\n", "    pass\n"),
    "start_no_closed_check": (SERVICE, "            if self._closed:\n                # A tick or request",
                              "            if False:\n                # A tick or request"),
    "tick_no_closed_check": (SERVICE, "            if self._closed:\n                return                          # a tick",
                             "            if False:\n                return                          # a tick"),
    "main_no_fence_precheck": (CATALOG, "    if not fence_open(args.fence_fd):\n        return 0",
                               "    if False:\n        return 0"),
    "atomic_mkdir": (CATALOG, "    from ..guardian import atomic_publish\n    atomic_publish(path",
                     "    from ..guardian import atomic_publish\n    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
                     "    atomic_publish(path"),
    "stop_no_wait": (SERVICE, "                try:\n                    process.wait(timeout=CATALOG_STOP_WAIT_S)\n                    return\n"
                              "                except subprocess.TimeoutExpired:\n                    continue\n",
                     "                return\n"),
    "stop_kill_only": (SERVICE, "for sig in (signal.SIGTERM, signal.SIGKILL):", "for sig in (signal.SIGKILL,):"),
    "stop_term_only": (SERVICE, "for sig in (signal.SIGTERM, signal.SIGKILL):", "for sig in (signal.SIGTERM,):"),
    "build_ignores_may_write": (CATALOG, "        if may_write is not None and not may_write():\n            break",
                                "        if False:\n            break"),
    "no_new_session": (CATALOG, "start_new_session=True, env=env", "start_new_session=False, env=env"),
    "no_pass_fds": (CATALOG, "pass_fds=() if fence_fd is None else (fence_fd,))", "pass_fds=())"),
    "closed_flag_never_set": (SERVICE, "            self._closed = True\n            process, self._catalog_proc",
                              "            process, self._catalog_proc"),
}


def run(name: str, timeout: float) -> tuple[str, list[str], float]:
    path, old, new = MUTANTS[name]
    original = path.read_text()
    assert original.count(old) == 1, f"{name}: anchor found {original.count(old)} times"
    path.write_text(original.replace(old, new))
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x" if False else "-q",
                               *TESTS], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        out = done.stdout + done.stderr
        failed = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)))
        summary = (out.strip().splitlines() or ["(no output)"])[-1]
    except subprocess.TimeoutExpired:
        failed, summary = ["<timeout>"], f"timed out after {timeout:g} s"
    finally:
        path.write_text(original)
    return summary, failed, time.monotonic() - started


def main() -> int:
    names = sys.argv[1:] or list(MUTANTS)
    for name in names:
        summary, failed, took = run(name, timeout=420)
        verdict = "KILLED" if failed else "SURVIVED"
        print(f"## {name}: {verdict} ({took:.0f} s) -- {summary}")
        for test in failed:
            print(f"   - {test}")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
