#!/usr/bin/env python3
"""Mutation check for fix/daemon-qos-r2: each mutant must fail at least one targeted test.

Usage: python mutate-r2.py <worktree>   (restores every file it touches, even on error)
"""
import subprocess
import sys
from pathlib import Path

tree = Path(sys.argv[1]).resolve()
TESTS = ["tests/unit/test_guardian_qos.py", "tests/unit/test_guardian.py", "tests/unit/test_daemon_qos_tools.py",
         "tests/process/test_guardian_qos_process.py", "tests/fake/test_provider_qos.py",
         "tests/unit/test_conversation_service.py::test_a_worktree_conversation_runs_the_repositorys_hook_at_utility",
         "tests/unit/test_daemon_verbs.py::test_daemon_install_dry_run_prints_the_plist"]
Q, G, S = "subfleet/qos.py", "subfleet/guardian.py", "subfleet/salvage.py"
MUTANTS = [
    ("no QoS attribute", Q, '            check(lib.posix_spawnattr_set_qos_class_np(ctypes.byref(attr), QOS_CLASS_UTILITY), "set_qos_class")\n', ''),
    ("guardian never clamps", G, '    if clamp:\n        fileno', '    if False:\n        fileno'),
    ("inherit opt-out ignored", Q, 'PROVIDER_QOS) == "inherit" or _LIBC is None', 'PROVIDER_QOS) == "never-set" or _LIBC is None'),
    ("descriptors not closed", Q, 'POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT', 'POSIX_SPAWN_SETSIGDEF'),
    ("signals not restored", Q, 'POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT', 'POSIX_SPAWN_CLOEXEC_DEFAULT'),
    ("no chdir", Q, '            check(lib.subfleet_addchdir(ctypes.byref(actions), os.fsencode(cwd)), "addchdir")\n', ''),
    ("cwd not checked first", Q, '    os.stat(cwd)\n', ''),
    ("last error, not first", Q, '                if rc not in (errno.ENOENT, errno.ENOTDIR) and not first:\n                    first = rc\n', ''),
    ("PATH not searched", Q, '    if os.path.dirname(name):\n        return [name]', '    if True:\n        return [name]'),
    ("error names the path tried", Q, '            raise OSError(number, os.strerror(number), argv[0])', '            raise OSError(number, os.strerror(number), executable)'),
    ("poll blocks on a wait", Q, '        if self.returncode is None and self._lock.acquire(False):', '        if self.returncode is None and self._lock.acquire(True):'),
    ("send_signal after exit", Q, '        if self.poll() is None:\n            try:\n                os.kill', '        if True:\n            try:\n                os.kill'),
    ("worktree add unclamped", "subfleet/daemon.py", 'result = subprocess.run(qos.repository_argv(["git", "-C", job["workdir"], "worktree", "add", "--detach",\n                                                                 workdir, job["workdir_head"]]),',
     'result = subprocess.run((["git", "-C", job["workdir"], "worktree", "add", "--detach",\n                                                                 workdir, job["workdir_head"]]),'),
    ("conversation worktree unclamped", "subfleet/conversations/service.py", '                command = qos.repository_argv(command)\n', '                pass\n'),
    ("salvage add unclamped", S, 'REPOSITORY_CODE = frozenset({"add", "update-ref", "update-index"})', 'REPOSITORY_CODE = frozenset({"update-ref", "update-index"})'),
    ("salvage clamps reads", S, 'REPOSITORY_CODE = frozenset({"add", "update-ref", "update-index"})', 'REPOSITORY_CODE = frozenset({"add", "update-ref", "update-index", "rev-parse"})'),
    ("hold timed after release", "tools/store_contention_repro.py", '        admission["all_holds"].append(held)', '        admission["all_holds"].append(time.monotonic() - since)'),
    ("plist stays Standard", "subfleet/cli.py", '"ProcessType": "Interactive"', '"ProcessType": "Standard"'),
]


def run_tests() -> tuple[bool, str]:
    done = subprocess.run([str(tree / ".venv/bin/python"), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                           *TESTS], cwd=tree, capture_output=True, text=True, timeout=1200)
    tail = [line for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))][:2]
    return done.returncode == 0, "; ".join(tail) or done.stdout.strip().splitlines()[-1]


ok, summary = run_tests()
print(f"baseline: {'pass' if ok else 'FAIL'} ({summary})", flush=True)
if not ok:
    raise SystemExit("baseline must pass")
survivors = []
for name, rel, old, new in MUTANTS:
    path = tree / rel
    original = path.read_text()
    if original.count(old) != 1:
        print(f"  SKIP {name}: pattern count {original.count(old)}", flush=True)
        survivors.append(name + " (pattern)")
        continue
    try:
        path.write_text(original.replace(old, new))
        passed, summary = run_tests()
    finally:
        path.write_text(original)
    print(f"  {'SURVIVED' if passed else 'killed  '} {name}: {summary}", flush=True)
    if passed:
        survivors.append(name)
print(f"{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} killed; survivors: {survivors}")
raise SystemExit(1 if survivors else 0)
