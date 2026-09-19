#!/usr/bin/env python3
"""Submit the 100 read-only canary jobs (decisions memo, "Read-only canary execution").

Samples 100 prompt.md files deterministically from the v1 ledger (sorted run ids, evenly
spaced), prefixes each with the canary preamble, writes them under the canary directory, and
submits each with `sf2 run -d -m astra -s read-only -I -D <clone> -C <clone> -p <prompt> -n canary-NNN`:
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
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PREAMBLE = ("Canary run, read-only. Answer from the repository in the working directory; do not write "
            "files or run commands that change state. The task text below is from an earlier run and is "
            "here for its shape only: read it, then give a short analysis of how you would approach it in "
            "this repository.\n\n---\n\n")


def save_bytes(path: Path, content: bytes) -> None:
    """Keep prompts and acknowledged receipts intact across interruption."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def save_json(path: Path, value) -> None:
    save_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


def frozen_prompts(baseline: dict, canary: Path, count: int) -> list[dict]:
    """Require the complete initial inventory and verify its exact submitted bytes."""
    inventory = baseline.get("prompts")
    if not isinstance(inventory, list) or len(inventory) != count:
        raise ValueError("baseline has no complete frozen prompt inventory")
    for i, row in enumerate(inventory, 1):
        name = f"canary-{i:03d}"
        if (not isinstance(row, dict) or row.get("name") != name
                or not isinstance(row.get("source"), str) or not row["source"]):
            raise ValueError("invalid frozen prompt inventory")
        content = (canary / "prompts" / f"{name}.md").read_bytes()
        if hashlib.sha256(content).hexdigest() != row.get("sha256"):
            raise ValueError(f"frozen prompt changed: {name}")
    return inventory


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--v1-runs", default="~/chief-of-staff/state/subfleet/runs")
    ap.add_argument("--canary-dir", default="~/subfleet-v2-canary")
    ap.add_argument("--sf2", default=str(Path(__file__).resolve().parents[1] / "bin" / "sf2"))
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--max-bytes", type=int, default=200_000)
    ap.add_argument("--dry-run", action="store_true", help="write prompts and print the commands; submit nothing")
    args = ap.parse_args()
    if args.count < 1 or args.max_bytes < 1:
        ap.error("--count and --max-bytes must be positive")
    runs = Path(args.v1_runs).expanduser()
    canary = Path(args.canary_dir).expanduser()
    clone = canary / "work"
    if not clone.is_dir():
        print(f"canary clone missing at {clone}; create it first", file=sys.stderr)
        return 2
    prompt_dir = canary / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        print("canary clone is dirty; the baseline must be clean", file=sys.stderr)
        return 2
    since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    baseline_file = canary / "baseline.json"
    try:
        if baseline_file.exists():
            baseline = json.loads(baseline_file.read_text())
            if not isinstance(baseline, dict) or baseline.get("head") != head or baseline.get("count") != args.count:
                raise ValueError("existing canary baseline differs; use a new canary directory")
        elif (canary / "jobs.json").exists():
            raise ValueError("existing cohort has no baseline; cannot reconstruct its evidence")
        else:
            prompts = sorted(p for p in runs.glob("*/prompt.md") if 0 < p.stat().st_size <= args.max_bytes)
            if len(prompts) < args.count:
                raise ValueError(f"only {len(prompts)} usable prompts")
            step = len(prompts) / args.count
            chosen = [prompts[int(i * step)] for i in range(args.count)]
            inventory = []
            for i, source in enumerate(chosen, 1):
                name = f"canary-{i:03d}"
                content = (PREAMBLE + source.read_text(errors="replace")).encode()
                save_bytes(prompt_dir / f"{name}.md", content)
                inventory.append({"name": name, "source": str(source), "sha256": hashlib.sha256(content).hexdigest()})
            baseline = {"head": head, "count": args.count, "since": since, "prompts": inventory}
            # No submission can happen until every sampled prompt is durable and
            # the complete inventory has been atomically committed.
            save_json(baseline_file, baseline)
        inventory = frozen_prompts(baseline, canary, args.count)
    except (OSError, ValueError) as exc:
        print(f"canary evidence cannot be resumed: {exc}", file=sys.stderr)
        return 2
    # Safe to re-run: the cohort file is rewritten after every submission, a name already
    # recorded with a job id is skipped, and each request id is deterministic
    # (canary-NNN plus the baseline head), so a repeated submission of the same name
    # is the daemon's duplicate-request case (C-6.2) and yields the same job, not a second one.
    jobs_file = canary / "jobs.json"
    recorded = {row["name"]: row for row in json.loads(jobs_file.read_text())} if jobs_file.exists() else {}
    failed = False
    for frozen in inventory:
        name, source = frozen["name"], frozen["source"]
        if recorded.get(name, {}).get("job_id") and recorded[name].get("rc", 0) == 0:
            print(f"{name} already {recorded[name]['job_id']}")
            continue
        target = prompt_dir / f"{name}.md"
        # -d: outside a Claude session `run` would otherwise wait for the job to finish (cli.launch_mode).
        cmd = [args.sf2, "run", "-d", "-m", "astra", "-s", "read-only", "-I", "-D", str(clone), "-C", str(clone),
               "-p", str(target), "-n", name, "--request-id", f"{name}-{head[:8]}", "--no-wait-queue"]
        if args.dry_run:
            print(" ".join(cmd))
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        job_id = result.stdout.strip().splitlines()[0].strip() if result.returncode == 0 and result.stdout.strip() else None
        failed = failed or not job_id
        recorded[name] = {"name": name, "source": str(source), "job_id": job_id, "rc": result.returncode,
                          "stderr": result.stderr.strip()[-400:]}
        save_json(jobs_file, sorted(recorded.values(), key=lambda r: r["name"]))
        print(f"{name} -> {job_id or 'rc ' + str(result.returncode)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
