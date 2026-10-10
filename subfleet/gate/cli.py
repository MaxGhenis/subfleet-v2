"""Permanent v1 gate command syntax and its distinct exit codes (C-17.1).

`subfleet gate pr|plan|continue` sends one `gate.start` or `gate.continue`, then
polls the gate until it answers a code. A poll rides out a daemon restart
(C-15.4): `gate.poll` names only the gate and advances it from its journaled
state, so a poll sent again after its answer was lost, or sent to the next
daemon, does what the loop's next poll would have done (decision record
`docs/decisions/2026-10-10-gate-survives-restart.md`). `subfleet gate poll <id>`
is that loop alone, for a gate whose `subfleet gate` has gone.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from ..client import (Client, DaemonError, DaemonUnavailable, ResponseLost, RestartWindow,
                      busy_pause, state_root)
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
            child.add_argument("--peer", required=True, choices=("fable", "astra", "sol"))
            child.add_argument("--main-model", help="main model for explicit family validation")
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
    # C-15.4: resume polling a gate whose `subfleet gate` has gone (a restart past
    # the window, an interrupt). It approves nothing: a poll advances the gate only
    # as the loop of the command that started it would have.
    child = commands.add_parser("poll")
    child.set_defaults(handler=run)
    child.add_argument("gate_id")
    child.add_argument("--json", action="store_true")


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"subfleet gate: {result.get('gate_id', '')} · {result['status']}"
              + (f" — {result['message']}" if result.get("message") else ""))


#: How long one `gate.poll` may take, busy answers included: `Client.call` asks
#: again after each (C-16.7), so this is how long a daemon that answers only
#: "busy" is asked.
POLL_TIMEOUT_S = 180


def _poll(client, gate_id: str) -> dict:
    """One `gate.poll`. A daemon at its connection cap answers "busy" before it
    reads the request, and `Client.call` asks again until `POLL_TIMEOUT_S` is
    spent (C-16.7; review of the descriptor hotfix, F8: one busy answer ended
    the loop with exit 1 while the gate carried on in the daemon)."""
    return client.call("gate.poll", {"gate_id": gate_id}, timeout=POLL_TIMEOUT_S)


def _note(text: str) -> None:
    print(f"subfleet gate: {text}", file=sys.stderr)


def _recovering(result: dict) -> bool:
    """C-15.4: a daemon whose startup recovery has not finished answered without
    doing anything (`service.RECOVERING`)."""
    return result.get("recovering") is True


def _identity(client: Any) -> tuple[Any, Any] | None:
    """`daemon.lock`'s pid and start time (C-5.3), or None when it cannot be read.

    A file read and no `ps`, the twin of `subfleet.cli._daemon_identity`, kept here
    so the gate entry point does not import the CLI: it tells one daemon process
    from the next, and only the line `_answered` prints rests on it."""
    read = getattr(client, "lock_info", None)
    try:
        info = read() if callable(read) else None
    except Exception:                                   # noqa: BLE001 - a label, not a verdict
        return None
    if not isinstance(info, dict) or not info.get("pid"):
        return None
    return info.get("pid"), info.get("proc_start")


def _answered(window: RestartWindow, client: Any, identity: tuple[Any, Any] | None,
              gate_id: str) -> tuple[Any, Any] | None:
    """C-15.4: the daemon answered a poll. After an outage, say so once on stderr,
    naming a restart only when `daemon.lock` names another process than at the
    answer before. The lock is read at every answer (a gate polls every 0.25 s, and
    the read is one small file), so a restart no poll noticed is not taken for one
    at the next outage. Returns what the next answer is compared with."""
    now = _identity(client)
    if window.answered():
        restarted = identity is not None and now is not None and now != identity
        _note(f"daemon {'restarted' if restarted else 'answered again'}; still polling gate {gate_id}")
    return now if now is not None else identity


def follow(client, gate_id: str, *, poll_interval: float = .25, reached: bool = True) -> dict:
    """Poll the gate until it answers a code (C-17.1), riding out a restart (C-15.4).

    `gate.poll` carries nothing but the gate id, and `GateService.poll` takes its
    step from the gate's journaled state under the gate's lock. Each step that
    acts is fenced by what it leaves behind: a round's peer job by its submit's
    request id, a verdict by the round lease released in the transaction that
    records it, a certificate by its presence, a merge by its operation key and
    claimed holder, and a gate that has answered a code only reads. So a poll sent
    again after its answer was lost, or sent to the next daemon, takes the step the
    loop's next poll would have taken, never the one before it again.

    A poll that found the daemon unreachable, lost its answer (dropped, later than
    `POLL_TIMEOUT_S` as from a daemon starved of CPU, or cut short), or was answered
    by a daemon still recovering is sent again, after C-16.7's pause, for
    `RESTART_WINDOW_S` from when the first such poll of that outage failed. Any other answer
    ends the outage, and an answer with a code ends the loop, so no poll follows a
    code. Past the window the last failure is raised. `reached` is False only for
    `gate poll`, whose first poll may find no daemon at all: that ends it at once.
    """
    window = RestartWindow()
    identity = _identity(client)
    streak = 0
    served_at = time.monotonic()        # the daemon's last answer that was not `recovering`
    while True:
        try:
            result = _poll(client, gate_id)
        except (DaemonUnavailable, ResponseLost) as exc:
            if isinstance(exc, DaemonUnavailable) and not reached:
                raise               # no daemon has answered: no restart is in progress
            failure: Exception = exc
        else:
            reached = True
            if not _recovering(result):
                identity, served_at = _answered(window, client, identity, gate_id), time.monotonic()
                if result.get("code") is not None:
                    return result
                streak = 0
                time.sleep(poll_interval)
                continue
            failure = GateError(str(result.get("message") or "daemon recovery is in progress"), 1)
        if not window.lost(time.monotonic()):
            _note(f"the daemon has not served gate {gate_id} for "
                  f"{time.monotonic() - served_at:.0f}s, and the {window.seconds:g}s "
                  f"restart window from its first failed poll has passed")
            raise failure
        streak += 1
        time.sleep(busy_pause(streak))


def _first(client, op: str, payload: dict) -> dict:
    """`gate.start` or `gate.continue`, asked again while the daemon answers that its
    startup recovery is still running (it did nothing), for `RESTART_WINDOW_S`.
    Nothing else is sent again: a lost start may have made a gate, and a lost
    continue may have reserved a round (C-16.3)."""
    window, streak = RestartWindow(), 0
    while True:
        result = client.call(op, payload, timeout=180)
        if not _recovering(result) or not window.lost(time.monotonic()):
            return result
        streak += 1
        time.sleep(busy_pause(streak))


def _resume(gate_id: str | None) -> None:
    """C-15.4: an exit 1 that leaves a gate behind names the command that resumes it."""
    if gate_id:
        _note(f"gate {gate_id} stays in the daemon with any peer job it started; "
              f"resume with: subfleet gate poll {shlex.quote(gate_id)}")


def run(args, *, client=None, runner=subprocess.run, root: Path | None = None,
        poll_interval: float = .25) -> int:
    from .service import preview
    root = Path(root) if root is not None else state_root()
    gate_id: str | None = None      # once the daemon may have it, an exit 1 names `gate poll`
    try:
        if args.gate_command not in {"plan", "pr", "continue", "poll"}:
            raise GateError("choose gate pr, plan, continue, or poll")
        if args.gate_command == "poll":
            gate_id = args.gate_id
            result = follow(client or Client(root), gate_id, poll_interval=poll_interval,
                            reached=False)
            _emit(result, args.json)
            if int(result["code"]) == 1:
                _resume(gate_id)
            return int(result["code"])
        if getattr(args, "peer", None) == "sol":
            print("subfleet gate: sol is retired from dispatch; using astra", file=sys.stderr)
        if args.dry_run:
            try:
                policy = json.loads((root / "policy.json").read_text())
            except FileNotFoundError:
                policy = {}
            result = preview(args, root, runner=runner, policy=policy)
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
        try:
            result = _first(client, op, payload)
        except ResponseLost:
            if op == "gate.continue":
                gate_id = args.gate_id  # C-16.3: it may have been done; a poll shows which
            raise
        gate_id = result.get("gate_id")
        if result.get("job_id") and result.get("code") is None:
            print(f"subfleet gate: {result['gate_id']} peer job {result['job_id']}", file=sys.stderr)
        if result.get("code") is None:
            time.sleep(poll_interval)
            result = follow(client, result["gate_id"], poll_interval=poll_interval)
        _emit(result, args.json)
        if int(result["code"]) == 1:
            _resume(gate_id)
        return int(result["code"])
    except GateError as exc:
        print(f"subfleet gate: {exc}", file=sys.stderr)
        if exc.code == 1:
            _resume(gate_id)
        return exc.code
    except (DaemonUnavailable, DaemonError, ProtocolError, OSError, ValueError) as exc:
        print(f"subfleet gate: {exc}", file=sys.stderr)
        _resume(gate_id)
        return 1
    except KeyboardInterrupt:
        print("subfleet gate: interrupted; the peer job remains in the daemon store", file=sys.stderr)
        _resume(gate_id)
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
