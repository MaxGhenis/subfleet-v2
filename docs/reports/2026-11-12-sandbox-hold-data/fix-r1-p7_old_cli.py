"""Probe p7: what a CLI from before #309 (release/217 at 82d3dd819) shows for the
replies and waits a new daemon sends about a sandbox hold. Run with cwd = the export,
so `subfleet` is the old package. Prints JSON."""
import json, sys, shlex
import subfleet
from subfleet import render, cli, task_holds

assert "old217" in subfleet.__file__, subfleet.__file__
UNTIL, SINCE = "2026-11-15T06:00:00Z", "2026-11-15T03:00:00Z"
# The new daemon's `tasks list` / status `task_holds` row and the job's `task-held` wait
# for a sandbox hold, as pinned by tests/unit/test_sandbox_hold_contract.py at the fix head.
hold = {"sandbox": "workspace-write", "until": UNTIL, "note": "disk brake", "since": SINCE}
wait = {"reason": "task-held", "sandbox": "workspace-write", "until": UNTIL, "note": "disk brake",
        "next_check_at": UNTIL}
out = {"subfleet_file": subfleet.__file__}
out["task_hold_line"] = render.task_hold_line(hold)
out["task_hold_line_waiting"] = render.task_hold_line({**hold, "waiting": 2})
jobs = [{"job_id": "20261115-030000-review", "kind": "dispatch", "task": "review", "sandbox": "workspace-write",
         "state": "waiting", "wait_reason": "task-held", "next_check_at": UNTIL, "hold": wait}]
status = cli.format_status({"lanes": [], "readings": [], "closures": [], "task_holds": [{**hold, "waiting": 1}],
                            "jobs": jobs})
out["format_status_hold_lines"] = [line for line in status.splitlines() if "disk brake" in line or "None" in line]
out["why_queue"] = render.why_queue({"job_id": "20261115-030000-review", "kind": "dispatch", "state": "waiting",
                                     "wait_reason": "task-held", "next_check_at": UNTIL, "hold": wait})
hint = next((line for line in out["why_queue"] if "tasks release" in line), "")
out["why_release_hint"] = hint
out["hint_argv"] = shlex.split(hint[hint.index("subfleet"):]) if "subfleet" in hint else None
# The old parser: can it name the sandbox at all?
parser = cli.build_parser()
for argv in (["tasks", "release", "--sandbox", "workspace-write"], ["tasks", "release", "--", "?"]):
    try:
        ns = parser.parse_args(argv)
        out[" ".join(argv)] = {"task": getattr(ns, "task", None), "sandbox": getattr(ns, "sandbox", "absent")}
    except SystemExit as exc:
        out[" ".join(argv)] = f"argparse exit {exc.code}"
print(json.dumps(out, indent=1))
