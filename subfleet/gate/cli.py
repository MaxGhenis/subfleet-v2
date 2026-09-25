"""Permanent v1 gate command syntax and its distinct exit codes (C-17.1)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from ..client import Client, DaemonError, DaemonUnavailable, state_root
from ..protocol import ProtocolError
from .errors import GateError

EXIT_CODES = {0: "agreement/completion", 1: "operational error", 2: "invalid input",
              3: "changes requested", 4: "blocked/invalid review",
              5: "failed, unverified, or queued merge action"}
MANAGED = ("CLAUDE_CODE_MANAGED_SETTINGS_PATH", "CLAUDE_CODE_REMOTE_SETTINGS_PATH",
           "CLAUDE_CODE_MOCK_REMOTE_SETTINGS")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(handler=run, gate_command=None)
    commands = parser.add_subparsers(dest="gate_command")
    for kind in ("pr", "plan", "continue"):
        child = commands.add_parser(kind)
        child.set_defaults(handler=run)
        child.add_argument("gate_id" if kind == "continue" else "target")
        child.add_argument("--main-approve", action="store_true")
        child.add_argument("--peer-account")
        child.add_argument("--exclude-account", action="append", default=[])
        child.add_argument("--max-rounds", type=int, default=None,
                           help="round limit within policy cap (0 uses the policy cap)")
        child.add_argument("--json", action="store_true")
        child.add_argument("--dry-run", action="store_true")
        if kind != "continue":
            child.add_argument("--peer", required=True, choices=("fable", "opus", "astra", "sol"))
            child.add_argument("--main-model", help="main model, recorded in the gate state (any family may review any main)")
            child.add_argument("--brief")
            child.add_argument("-C", dest="workdir", default=None)
            child.add_argument("--on-agreement", choices=("proceed", "merge") if kind == "pr" else ("proceed",), default="proceed")
            if kind == "pr":
                child.add_argument("--merge-method", choices=("merge", "squash"), default="merge")
            else:
                child.set_defaults(merge_method=None)
        else:
            child.add_argument("--response")
        if kind in ("pr", "continue"):
            child.add_argument("--expect-head")
            child.add_argument("--expect-base")
        if kind in ("plan", "continue"):
            child.add_argument("--expect-sha256")


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"subfleet gate: {result.get('gate_id', '')} · {result['status']}"
              + (f" — {result['message']}" if result.get("message") else ""))


def _preview_policy(root: Path) -> dict:
    """C-19.1, C-11.1: a dry run checks against the validated live policy, or the shipped
    one when the live file is missing or invalid, so a preview refuses what start refuses."""
    from ..policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
    for path in (root / "policy.json", DEFAULT_POLICY_PATH):
        try:
            return load_policy(path)
        except (PolicyError, OSError):
            continue
    return {}


def run(args, *, client=None, runner=subprocess.run, root: Path | None = None,
        poll_interval: float = .25) -> int:
    from .service import preview
    root = Path(root) if root is not None else state_root()
    try:
        if args.gate_command not in {"plan", "pr", "continue"}:
            raise GateError("choose gate pr, plan, or continue")
        if getattr(args, "peer", None) == "sol":
            print("subfleet gate: sol is retired from dispatch; using astra", file=sys.stderr)
        if args.dry_run:
            result = preview(args, root, runner=runner, policy=_preview_policy(root))
            print(json.dumps(result, sort_keys=True, indent=None if args.json else 2))
            return 0
        for key in MANAGED:
            if key in os.environ:
                raise GateError(f"isolated review refused: inherited {key}; review the managed policy before retrying", 4)
        # Validate input requiring no artifact/store before attempting transport.
        if args.gate_command != "continue" and not args.main_approve:
            raise GateError("--main-approve is required; approval cannot be inferred from invocation")
        client = client or Client(root)
        payload = {k: v for k, v in vars(args).items() if k not in {"handler", "command", "version"}}
        if args.gate_command != "continue":
            payload["workdir"] = str(Path(args.workdir or Path.cwd()).expanduser().resolve())
        # Files named by the caller are read by the daemon from absolute paths.
        for key in ("brief", "response"):
            if payload.get(key):
                payload[key] = str(Path(payload[key]).expanduser().resolve())
        op = "gate.continue" if args.gate_command == "continue" else "gate.start"
        result = client.call(op, payload, timeout=180)
        if result.get("job_id") and result.get("code") is None:
            print(f"subfleet gate: {result['gate_id']} peer job {result['job_id']}", file=sys.stderr)
        while result.get("code") is None:
            time.sleep(poll_interval)
            result = client.call("gate.poll", {"gate_id": result["gate_id"]}, timeout=180)
        _emit(result, args.json)
        return int(result["code"])
    except GateError as exc:
        print(f"subfleet gate: {exc}", file=sys.stderr)
        return exc.code
    except (DaemonUnavailable, DaemonError, ProtocolError, OSError, ValueError) as exc:
        print(f"subfleet gate: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("subfleet gate: interrupted; the peer job remains in the daemon store", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet-gate")
    configure(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
