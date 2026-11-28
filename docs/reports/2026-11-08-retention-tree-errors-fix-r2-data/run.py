"""Run job-only pytest commands with isolation enabled and fresh scratch paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

scratch = Path("/private/tmp/sf-rte-fix2")
name, *arguments = sys.argv[1:]
assert re.fullmatch(r"[a-z0-9-]+", name)
base = scratch / f"pytest-{name}"
assert not base.exists(), "each run needs a new basetemp"
if arguments[:1] == ["--importers"]:
    arguments = Path(__file__).with_name("importers.txt").read_text().splitlines() + arguments[1:]
if arguments[:1] == ["--mutant"]:
    mode, *arguments = arguments[1:]
    target = [str(Path(__file__).with_name("verify.py")), mode]
else:
    target = ["-m", "pytest"]
command = ["uv", "run", "--default-index", "https://pypi.org/simple", "--no-sync", "--with", "pytest-xdist>=3.6",
           "python", "-u", *target, "-p", "sf_rte_safe", "-q", "-ra",
           "--durations=10", "--timeout=600", "--timeout-method=signal",
           f"--basetemp={base}", "-o", f"cache_dir={scratch / ('pytest-cache-' + name)}",
           *arguments]
environment = dict(os.environ)
for key in ("UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX"):
    environment.pop(key, None)
environment.update(TMPDIR=str(scratch), UV_CACHE_DIR=str(scratch / "uv-cache"),
                   PYTHONPATH=str(scratch), PYTHONDONTWRITEBYTECODE="1",
                   HYPOTHESIS_STORAGE_DIRECTORY=str(scratch / "hypothesis"))
print(shlex.join(command), flush=True)
started = time.monotonic()
log = scratch / f"{name}.log"
with log.open("w") as output:
    result = subprocess.run(command, env=environment, stdout=output, stderr=subprocess.STDOUT)
elapsed = time.monotonic() - started
(scratch / f"{name}.json").write_text(json.dumps(
    {"command": command, "exit_code": result.returncode, "elapsed_s": round(elapsed, 2)}, indent=2) + "\n")
print("\n".join(log.read_text().splitlines()[-100:]), flush=True)
print(f"exit={result.returncode}; wall={elapsed:.2f}s; log={log}", flush=True)
raise SystemExit(result.returncode)
