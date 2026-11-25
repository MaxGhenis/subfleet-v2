"""Run isolated pytest commands with fresh basetemps and per-run disk checks."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


workspace = Path.cwd().resolve()
scratch = Path(__file__).resolve().parent
cache = "/private/tmp/sf-rte-fix3-uv"
start = datetime(2026, 11, 25, 21, 26, 45, tzinfo=timezone.utc)
name, *arguments = sys.argv[1:]
assert re.fullmatch(r"[a-z0-9-]+", name)
base = scratch / f"pytest-{name}"
assert not base.exists(), "each run needs a fresh basetemp"
assert all(part.lower() not in {"tmp", "temp", ".tmp"} and "scratch" not in part.lower()
           for part in base.parts), base

disk = subprocess.check_output(["df", "-H", "/"], text=True)
footprint = subprocess.check_output(["du", "-sh", str(scratch)], text=True)
usage_kib = int(subprocess.check_output(["du", "-sk", str(scratch)], text=True).split()[0])
disk_bytes = os.statvfs("/")
free_bytes = disk_bytes.f_bavail * disk_bytes.f_frsize
print(disk + footprint, end="", flush=True)
assert free_bytes >= 60_000_000_000, "less than 60 GB free"
assert usage_kib * 1024 < 200_000_000, "scratch usage reached 200 MB"
assert (datetime.now(timezone.utc) - start).total_seconds() < 9_000, "test-start cutoff passed"

if arguments[:1] == ["--mutant"]:
    mode, *arguments = arguments[1:]
    target = [str(scratch / "verify.py"), mode]
else:
    target = ["-m", "pytest"]
command = [
    "uv", "run", "--default-index", "https://pypi.org/simple", "--no-sync",
    "--with", "pytest-xdist>=3.6", "python", "-u", *target,
    "-p", "sf_rte_safe", "-q", "-ra", "--durations=5", "--timeout=600",
    "--timeout-method=signal", f"--basetemp={base}", "-o",
    f"cache_dir={scratch / ('pytest-cache-' + name)}", *arguments,
]
environment = dict(os.environ)
for key in ("UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX"):
    environment.pop(key, None)
environment.update(
    TMPDIR=str(scratch),
    UV_CACHE_DIR=cache,
    PYTHONPATH=str(scratch),
    PYTHONDONTWRITEBYTECODE="1",
    HYPOTHESIS_STORAGE_DIRECTORY=str(scratch / "hypothesis"),
)
print(shlex.join(command), flush=True)
python = subprocess.check_output([str(workspace / ".venv/bin/python"), "--version"], text=True).strip()
log = scratch / f"{name}.log"
started = time.monotonic()
with log.open("w") as output:
    print(python, file=output, flush=True)
    result = subprocess.run(command, env=environment, stdout=output, stderr=subprocess.STDOUT)
elapsed = round(time.monotonic() - started, 2)
(scratch / f"{name}.json").write_text(json.dumps({
    "command": command, "exit_code": result.returncode, "wall_s": elapsed,
    "python": python, "free_bytes": free_bytes, "scratch_bytes": usage_kib * 1024,
}, indent=2) + "\n")
print("\n".join(log.read_text().splitlines()[-16:]), flush=True)
print(f"exit={result.returncode}; wall={elapsed}s; log={log}", flush=True)
raise SystemExit(result.returncode)
