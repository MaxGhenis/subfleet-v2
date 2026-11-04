"""The harness's argv, exactly as the adapter-harness interface fixes it (C-31.7)."""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
SANDBOXES = (READ_ONLY, WORKSPACE_WRITE)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: The test-only endpoint override (C-31.11). `client.endpoint` honors it only in a
#: pytest session's process tree and only for a loopback address; it lives here so
#: the harness can take it out of the environment before the SDK loads.
TEST_URL_ENV = "SUBFLEET_API_AGENT_TEST_URL"


class UsageError(ValueError):
    """The argv does not follow the interface. The harness exits 1, never 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would print usage and exit 2
        raise UsageError(message)


@dataclass(frozen=True)
class Options:
    attempt_dir: Path
    workdir: Path
    prompt: Path
    model: str
    effort: str
    sandbox: str
    max_usd: float
    max_turns: int
    network: bool = False
    guard_hook: Path | None = None
    publish_hook: Path | None = None
    resume: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -I -m subfleet.api_agent", allow_abbrev=False, add_help=False)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True, choices=EFFORTS)
    parser.add_argument("--sandbox", required=True, choices=SANDBOXES)
    parser.add_argument("--max-usd", required=True, type=float)
    parser.add_argument("--max-turns", required=True, type=int)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--guard-hook")
    parser.add_argument("--publish-hook")
    parser.add_argument("--resume", action="store_true")
    return parser


def parse(argv: list[str]) -> Options:
    """Parse the interface's argv. Every path must be absolute; nothing is read here."""
    args = _parser().parse_args(argv)
    paths = {name: getattr(args, name) for name in ("attempt_dir", "workdir", "prompt", "guard_hook",
                                                    "publish_hook")}
    for name, value in paths.items():
        if value is None:
            continue
        if not value or not Path(value).is_absolute() or any(ord(char) < 32 for char in value):
            raise UsageError(f"--{name.replace('_', '-')} must be an absolute path without control characters")
    if not args.model or any(ord(char) < 33 for char in args.model):
        raise UsageError("--model must be a model id")
    if not math.isfinite(args.max_usd) or args.max_usd < 0:
        raise UsageError("--max-usd must be a finite amount of at least 0")
    if args.max_turns < 0:
        raise UsageError("--max-turns must be at least 0")
    return Options(
        attempt_dir=Path(args.attempt_dir), workdir=Path(args.workdir), prompt=Path(args.prompt),
        model=args.model, effort=args.effort, sandbox=args.sandbox, max_usd=args.max_usd,
        max_turns=args.max_turns, network=args.network,
        guard_hook=Path(args.guard_hook) if args.guard_hook else None,
        publish_hook=Path(args.publish_hook) if args.publish_hook else None,
        resume=args.resume)
