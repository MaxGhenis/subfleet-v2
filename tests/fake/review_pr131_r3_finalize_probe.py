"""A/B probe: does the finalization census see a tool shell that outlives its provider?

Run with PYTHONPATH=<tree> so the guardian and `procs` come from that tree.
A real guardian runs a provider (a non-platform Python) that starts `/bin/zsh`
in a new session with both markers, as a Claude Code Bash tool shell runs, and
exits 0. After exit.json, the C-5.5 census runs. The shell's session is stopped
here by its exact recorded pgid.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from subfleet import procs

base = Path(sys.argv[1]).resolve()
base.mkdir(parents=True, exist_ok=True)
adir, root, attempt = base / "a1", str(base / "state"), "probe-final/a1"
log, shell_pid_file = base / "shell.log", base / "shell.pid"
env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
env.update(SUBFLEET_ATTEMPT=attempt, SUBFLEET_ROOT=root)
provider = [sys.executable, "-c",
            "import subprocess, sys; p = subprocess.Popen(['/bin/zsh', '-c', "
            f"'while :; do print -r -- tick >> {log}; sleep 0.2; done'], start_new_session=True); "
            f"open({str(shell_pid_file)!r}, 'w').write(str(p.pid))"]
g = subprocess.Popen([sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(adir), "--cwd", str(base),
                      "--stdout-path", str(adir / "stdout"), "--stderr-path", str(adir / "stderr"), "--", *provider],
                     env=env)
shell = None
try:
    assert g.wait(timeout=60) == 0
    exit_receipt = json.loads((adir / "exit.json").read_text())
    start = json.loads((adir / "start.json").read_text())
    shell = int(shell_pid_file.read_text())
    time.sleep(1.0)
    census = procs.containment(start["pgid"], start["guardian_pid"], exit_receipt.get("child_pid"), attempt, root=root)
    before = len(log.read_text().splitlines())
    time.sleep(1.0)
    after = len(log.read_text().splitlines())
    shape = subprocess.run(["/bin/ps", "-o", "ppid=,pgid=,stat=", "-p", str(shell)], capture_output=True, text=True).stdout.split()
    print(json.dumps({"tree": procs.__file__, "guardian_pgid": start["pgid"], "shell_pid": shell,
                      "shell_ppid/pgid/stat": shape, "census_verified_empty": census.verified_empty,
                      "census": census.to_dict(), "shell_writing": after > before, "log_lines": [before, after]}))
finally:
    if shell:
        try:
            os.killpg(shell, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if g.poll() is None:
        g.kill()
        g.wait()
