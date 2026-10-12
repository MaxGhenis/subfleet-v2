"""Revert one round-14 safety fix in a disposable tree and run its witness.

The checkout and Git history are never modified. Each invocation uses one
foreground locked pytest hold, then removes its own temporary directory.
"""
import os
import ast
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
mode = sys.argv[1]
with tempfile.TemporaryDirectory(prefix="sf-r131f14-mutation-", dir=os.environ["TMPDIR"]) as directory:
    tree = Path(directory)
    shutil.copytree(ROOT / "subfleet", tree / "subfleet", ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "tests").symlink_to(ROOT / "tests", target_is_directory=True)
    shutil.copyfile(ROOT / "pyproject.toml", tree / "pyproject.toml")
    shutil.copyfile(ROOT / "uv.lock", tree / "uv.lock")
    daemon = tree / "subfleet/daemon.py"
    source = daemon.read_text()
    if mode == "group":
        needle = "        return procs.signal_process(ident, sig, authorize=lambda: self._signal_claim_authority("
        restored = """        # Restore cached group delivery, keeping the post-scalar claim gate.
        if self._unambiguous_signal_group(attempt_id, ident, census, evidence, foreign):
            if procs.same_process(ident.pid, ident.boot_id, ident.proc_start):
                try:
                    if procs.os.getpgid(ident.pid) == ident.pid:
                        with self._signal_claim_authority(attempt_id, ident, census, evidence, foreign) as allowed:
                            if allowed and procs.same_process(ident.pid, ident.boot_id, ident.proc_start):
                                procs.os.killpg(ident.pid, sig)
                                return True
                except (ProcessLookupError, PermissionError):
                    return False
""" + needle
        assert source.count(needle) == 1
        daemon.write_text(source.replace(needle, restored))
        target = "test_claimed_identity_joining_group_after_census_is_not_signalled"
    elif mode == "confirmation":
        path = tree / "subfleet/procs.py"
        source = path.read_text()
        needle = """            with authorize() as allowed:
                if not allowed:
                    return False
                # Authorization can import receipts. Reconfirm after that I/O
                # while publications remain excluded through the syscall.
                if not same_process(recorded.pid, recorded.boot_id, recorded.proc_start):
                    return False
                os.kill(recorded.pid, sig)"""
        assert source.count(needle) == 1
        path.write_text(source.replace(needle,
            "            if not same_process(recorded.pid, recorded.boot_id, recorded.proc_start):\n"
            "                return False\n            os.kill(recorded.pid, sig)"))
        target = "test_live_claim_during_scalar_confirmation_blocks_delivery"
    elif mode == "receipts":
        # Restore the two incomplete-import bugs, retaining paced queue schema.
        needle = """                    tx.execute("UPDATE process_claim_receipt_pending SET failures=?,retry_at=?,last_error=? WHERE attempt_id=?",
                               (failures, time.time() + delay,
                                "unreadable" if start is None else "awaiting-child-identity", row["attempt_id"]))"""
        assert source.count(needle) == 1
        source = source.replace(needle, '                    tx.execute("DELETE FROM process_claim_receipt_pending WHERE attempt_id=?", (row["attempt_id"],))')
        source = source.replace('"AND NOT EXISTS(SELECT 1 FROM process_claim_receipt_pending)"', '""')
        daemon.write_text(source)
        target = "pending_terminal_receipt or failed_startup_receipt"
    else:
        raise ValueError(mode)
    ast.parse(daemon.read_text())
    ast.parse((tree / "subfleet/procs.py").read_text())
    if "--check" in sys.argv[2:]:
        print("Mutation source parses:", mode)
        sys.exit(0)
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv"), "UV_NO_SYNC": "1"}
    result = subprocess.run([sys.executable, str(ROOT / "tools/review_pr131_r14_locked.py"),
                             str(tree / "mutation.log"), "tests/fake/test_review_pr131_round14.py", "--tb=short", "--show-capture=no", "-k", target],
                            cwd=tree, env=env)
    sys.exit(result.returncode)
