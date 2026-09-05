#!/usr/bin/env python3
"""Submit the 100 read-only canary jobs (decisions memo, "Read-only canary execution").

Samples 100 prompt.md files deterministically from the v1 ledger (sorted run ids, evenly
spaced), prefixes each with the canary preamble, writes them under the canary directory, and
submits each with `sf2 run -m astra -s read-only -I -D <clone> -C <clone> -p <prompt> -n canary-NNN`:
the isolated-review path (C-23.2, C-23.4) launches Codex with `--ephemeral --ignore-user-config
--ignore-rules` and every MCP server, plugin, and hook disabled, so read-only is a verified
capability restriction and not only the shell sandbox.
Records the job ids and the clone's baseline HEAD for canary_check.py. Nothing here writes
outside the canary directory and the v2 store.

    uv run python tools/canary_submit.py [--count 100] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

PREAMBLE = ("Canary run, read-only. Answer from the repository in the working directory; do not write "
            "files or run commands that change state. The task text below is from an earlier run and is "
            "here for its shape only: read it, then give a short analysis of how you would approach it in "
            "this repository.\n\n---\n\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--v1-runs", default="~/chief-of-staff/state/subfleet/runs")
    ap.add_argument("--canary-dir", default="~/subfleet-v2-canary")
    ap.add_argument("--sf2", default=str(Path(__file__).resolve().parents[1] / "bin" / "sf2"))
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--max-bytes", type=int, default=200_000)
    ap.add_argument("--dry-run", action="store_true", help="write prompts and print the commands; submit nothing")
    args = ap.parse_args()
    runs = Path(args.v1_runs).expanduser()
    canary = Path(args.canary_dir).expanduser()
    clone = canary / "work"
    if not clone.is_dir():
        print(f"canary clone missing at {clone}; create it first", file=sys.stderr)
        return 2
    prompts = sorted(p for p in runs.glob("*/prompt.md") if 0 < p.stat().st_size <= args.max_bytes)
    if len(prompts) < args.count:
        print(f"only {len(prompts)} usable prompts", file=sys.stderr)
        return 2
    step = len(prompts) / args.count
    chosen = [prompts[int(i * step)] for i in range(args.count)]
    prompt_dir = canary / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True).stdout.strip()
    since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (canary / "baseline.json").write_text(json.dumps({"head": head, "count": args.count, "since": since}, indent=2) + "\n")
    submitted = []
    for i, source in enumerate(chosen, 1):
        name = f"canary-{i:03d}"
        target = prompt_dir / f"{name}.md"
        target.write_text(PREAMBLE + source.read_text(errors="replace"))
        cmd = [args.sf2, "run", "-m", "astra", "-s", "read-only", "-I", "-D", str(clone), "-C", str(clone),
               "-p", str(target), "-n", name, "--no-wait-queue"]
        if args.dry_run:
            print(" ".join(cmd))
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        job_id = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else None
        submitted.append({"name": name, "source": str(source), "job_id": job_id, "rc": result.returncode,
                          "stderr": result.stderr.strip()[-400:]})
        print(f"{name} -> {job_id or 'rc ' + str(result.returncode)}")
    if not args.dry_run:
        (canary / "jobs.json").write_text(json.dumps(submitted, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
