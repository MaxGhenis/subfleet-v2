"""`subfleet doctor`: the checks that gate the cutover.

The `subfleet` symlink is repointed from v1 to v2 exactly once, and everything
that can be wrong about that flip is cheap to look at and expensive to discover
afterwards: a stale compat table, hook entries that do not match what
`daemon install --hooks` would write, a symlink that still resolves into
`chief-of-staff/subfleet`, a second `claude` or `codex` earlier on PATH, a state
root that was never created, a `daemon.lock` naming a process that is gone.

Every row is `{check, status, detail, fix}` with `status` one of:

* `pass` — looked, and it is right.
* `fail` — looked, and it is wrong. The exit code is 1 when any row fails.
* `unknown` — could not look (no permission, no file to compare against, a
  binary that would not answer). Never reported as a pass; C-17.3 keeps exit 0
  for "ok", and an unknown is not a failure either, so it does not change the
  exit code but it always carries a fix line saying how to find out.

`fix` is a command or a sentence naming what to do, on every row including the
passing ones, so the table reads the same whether it is being skimmed or acted
on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .client import Client, DaemonError, DaemonUnavailable, LOCK_NAME, SOCKET_NAME
from .protocol import ProtocolError

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

#: A resolved `subfleet` under this directory is still v1 (plan amendment 8:
#: the shadow period runs both, and the symlink is the visible half of the flip).
V1_MARKER = "chief-of-staff/subfleet"
STATE_ROOT_ENTRIES = ("state.sqlite3", "policy.json", "lanes.json",
                      "jobs", "lanes", "worktrees", "daemon.log")


def row(check: str, status: str, detail: str, fix: str) -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail, "fix": fix}


def _run(argv: list[str], timeout: float = 20.0) -> tuple[int | None, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    text = (done.stdout or done.stderr or "").strip().splitlines()
    return done.returncode, text[0] if text else "(no output)"


def path_matches(binary: str) -> list[str]:
    """Every executable named `binary` on PATH, in PATH order, deduplicated."""
    seen: set[str] = set()
    found: list[str] = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry or entry in seen:
            continue
        seen.add(entry)
        candidate = Path(entry) / binary
        if candidate.exists() and os.access(candidate, os.X_OK):
            found.append(str(candidate))
    return found


# --- the individual checks ----------------------------------------------------

def check_compat_table() -> dict[str, Any]:
    """The compat table loads and every rule targets a verb v2 can reach."""
    try:
        from . import compat
        report = compat.self_check()
    except Exception as exc:                            # noqa: BLE001
        return row("compat table loads", FAIL,
                   f"subfleet.compat did not import: {exc.__class__.__name__}: {exc}",
                   "every v1 invocation goes through this table; the cutover is "
                   "blocked until it imports")
    if report.get("unreachable"):
        return row("compat table loads", FAIL,
                   f"{report['rules']} rules, {len(report['unreachable'])} target a "
                   f"verb the v2 parser does not accept: "
                   f"{', '.join(report['unreachable'][:5])}",
                   "fix the rule or add the verb; "
                   "`uv run pytest tests/unit/test_compat.py` names each one")
    return row("compat table loads", PASS,
               f"{report['rules']} rules, {report['verbs']} v1 verbs, "
               f"{report['env']} env names, all reachable",
               "`uv run pytest tests/unit/test_compat.py` replays every case")


def check_hook_entries(settings: Path | None = None) -> dict[str, Any]:
    """`~/.claude/settings.json` matches what `daemon install --hooks` writes."""
    from . import hooks
    report = hooks.installed(settings)
    path = report.get("path")
    if not report.get("ok"):
        return row("hook entries in ~/.claude/settings.json", UNKNOWN,
                   f"{path}: {report.get('error')}",
                   f"fix or remove {path}, then `subfleet daemon install --hooks`")
    v1 = report.get("v1_entries") or {}
    v1_note = (f"; v1 bin/subfleet-hook still installed for "
               f"{', '.join(sorted(v1))} (left in place on purpose)" if v1 else "")
    if report.get("matches"):
        return row("hook entries in ~/.claude/settings.json", PASS,
                   f"{path}: the three v2 entries are present{v1_note}",
                   "`subfleet daemon install --hooks --dry-run` prints the diff")
    return row("hook entries in ~/.claude/settings.json", FAIL,
               f"{path}: {', '.join(report['missing_events'])} would change{v1_note}",
               "`subfleet daemon install --hooks --dry-run`, then "
               "`subfleet daemon install --hooks`")


def check_symlink() -> dict[str, Any]:
    """Which `subfleet` a session actually runs, and which version it reports."""
    found = shutil.which("subfleet")
    if found is None:
        return row("subfleet on PATH", FAIL, "no `subfleet` on PATH",
                   "put the v2 entry point on PATH before repointing anything")
    real = os.path.realpath(found)
    link = f"{found} -> {real}" if real != found else found
    code, text = _run([found, "-V"])
    version = text if code == 0 else f"`{found} -V` exited {code}: {text}"
    if V1_MARKER in real:
        return row("subfleet symlink target", FAIL,
                   f"{link} — still v1 ({version})",
                   "repoint the symlink at the v2 entry point when the cutover "
                   "gates in docs/release-gates.md are green")
    if code != 0:
        return row("subfleet symlink target", UNKNOWN, f"{link} — {version}",
                   f"run `{found} -V` by hand to see why it did not answer")
    return row("subfleet symlink target", PASS, f"{link} — {version}",
               "`subfleet doctor` after any change to this symlink")


def check_path_shadows(binary: str) -> dict[str, Any]:
    """More than one `claude`/`codex`/`subfleet` on PATH is a silent version flip."""
    matches = path_matches(binary)
    if not matches:
        status = FAIL if binary == "subfleet" else UNKNOWN
        return row(f"PATH shadows for {binary}", status, f"no {binary} on PATH",
                   f"install {binary} or drop it from the fleet's expectations")
    if len(matches) == 1:
        return row(f"PATH shadows for {binary}", PASS, matches[0],
                   f"`which -a {binary}` if that ever changes")
    return row(f"PATH shadows for {binary}", FAIL,
               f"{len(matches)} on PATH, first wins: {' , '.join(matches)}",
               f"remove the shadowing copies or reorder PATH; `which -a {binary}`")


def check_state_root(root: Path) -> dict[str, Any]:
    if not root.exists():
        return row("state root layout", UNKNOWN,
                   f"{root} does not exist yet",
                   "`subfleet daemon start` creates it (C-2.2)")
    present = [name for name in STATE_ROOT_ENTRIES if (root / name).exists()]
    missing = [name for name in STATE_ROOT_ENTRIES if name not in present]
    if "state.sqlite3" not in present:
        return row("state root layout", UNKNOWN,
                   f"{root}: no store yet ({', '.join(present) or 'empty'})",
                   "`subfleet daemon start` creates the store on first run")
    return row("state root layout", PASS,
               f"{root}: {', '.join(present)}"
               + (f" (not yet: {', '.join(missing)})" if missing else ""),
               "C-2.2 lists everything that belongs here")


def check_daemon_lock(root: Path) -> dict[str, Any]:
    """Does `daemon.lock` name a process that is actually alive (C-5.8)?"""
    client = Client(root)
    info = client.lock_info()
    socket_present = client.socket_path.exists()
    if info is None:
        if socket_present:
            return row("daemon.lock names a live process", FAIL,
                       f"{root / SOCKET_NAME} exists with no {LOCK_NAME}",
                       "`subfleet daemon start` (it rewrites both)")
        return row("daemon.lock names a live process", PASS,
                   "no lock and no socket: no daemon is running",
                   "`subfleet daemon start` when you want one")
    alive, reason = client.lock_report()
    if alive is False:
        return row("daemon.lock names a live process", FAIL,
                   f"pid {info.get('pid')} is gone: {reason}",
                   "`subfleet daemon start` — a CLI may start one over a dead "
                   "holder, never over a live one (plan amendment 3)")
    if alive is None:
        return row("daemon.lock names a live process", UNKNOWN,
                   f"pid {info.get('pid')}: {reason}",
                   f"`ps -p {info.get('pid')} -o state=,lstart=` to decide by hand")
    return row("daemon.lock names a live process", PASS,
               f"pid {info.get('pid')}: {reason}"
               + ("" if socket_present else f" (but no {SOCKET_NAME})"),
               "`subfleet daemon status`")


def check_provider(binary: str) -> dict[str, Any]:
    found = shutil.which(binary)
    if found is None:
        return row(f"{binary} --version", FAIL, f"{binary} is not on PATH",
                   f"install {binary}; no lane of that provider can launch without it")
    code, text = _run([found, "--version"])
    if code is None:
        return row(f"{binary} --version", UNKNOWN, f"{found}: {text}",
                   f"run `{found} --version` by hand")
    if code != 0:
        return row(f"{binary} --version", FAIL, f"{found} exited {code}: {text}",
                   f"reinstall or re-authenticate {binary}")
    return row(f"{binary} --version", PASS, f"{found}: {text}",
               "C-12 pins the minimum version each adapter needs")


def check_socket_path(root: Path) -> dict[str, Any]:
    """`sun_path` is capped near 104 bytes; a long `SUBFLEET_HOME` is unfixable
    later, so it is worth one line here."""
    from .cli import AF_UNIX_PATH_MAX
    path = Client(root).socket_path
    encoded = len(str(path).encode())
    if encoded <= AF_UNIX_PATH_MAX:
        return row("socket path fits AF_UNIX", PASS, f"{encoded} bytes",
                   "keep SUBFLEET_HOME short")
    return row("socket path fits AF_UNIX", FAIL,
               f"{path} is {encoded} bytes; the kernel caps a unix socket path "
               f"near {AF_UNIX_PATH_MAX}",
               "set SUBFLEET_HOME to a shorter path — no daemon can ever listen there")


def check_live(root: Path) -> dict[str, Any]:
    """`--live`: one `ping` against the daemon (C-16.2)."""
    try:
        result = Client(root, timeout=5).call("ping", {"text": "doctor"})
    except DaemonUnavailable as exc:
        return row("ping the daemon", FAIL, str(exc), "`subfleet daemon start`")
    except (DaemonError, ProtocolError, OSError) as exc:
        return row("ping the daemon", FAIL, f"{exc.__class__.__name__}: {exc}",
                   "`subfleet daemon logs -n 40`")
    if not result.get("pong"):
        return row("ping the daemon", FAIL, f"unexpected reply: {result}",
                   "`subfleet daemon logs -n 40`")
    return row("ping the daemon", PASS,
               f"pong from subfleet {result.get('version')}",
               "`subfleet daemon status` for the rest")


# --- the table ----------------------------------------------------------------

def checks(root: Path, *, live: bool = False,
           settings: Path | None = None) -> list[dict[str, Any]]:
    rows = [
        check_compat_table(),
        check_hook_entries(settings),
        check_symlink(),
        *(check_path_shadows(binary) for binary in ("subfleet", "claude", "codex")),
        *(check_provider(binary) for binary in ("claude", "codex")),
        check_state_root(root),
        check_socket_path(root),
        check_daemon_lock(root),
    ]
    if live:
        rows.append(check_live(root))
    return rows


def render(rows: list[dict[str, Any]]) -> str:
    width = max((len(item["check"]) for item in rows), default=0)
    lines = []
    for item in rows:
        lines.append(f"{item['status'].upper():<7} {item['check']:<{width}}  "
                     f"{item['detail']}")
        if item["status"] != PASS:
            lines.append(f"{'':<7} {'':<{width}}  fix: {item['fix']}")
    return "\n".join(lines)


def exit_code(rows: list[dict[str, Any]]) -> int:
    """1 when anything failed; an `unknown` never decides the exit status."""
    from .contracts import Exit
    return int(Exit.OPERATIONAL if any(item["status"] == FAIL for item in rows)
               else Exit.OK)
