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
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .client import Client, DaemonError, DaemonUnavailable, LOCK_NAME, SOCKET_NAME
from .policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from .protocol import ProtocolError

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

#: A resolved `subfleet` under this directory is still v1 (plan amendment 8:
#: the shadow period runs both, and the symlink is the visible half of the flip).
V1_MARKER = "chief-of-staff/subfleet"
#: Hidden v1 verbs that appear in v1's own usage line and in no v2 parser, so a
#: `subfleet` that answers with them is v1 whatever its path says. Checking the
#: output as well as the path is what catches an entry point that is v2 on disk
#: but imports v1 (see `check_pythonpath`).
V1_OUTPUT_MARKERS = ("_session-hook", "_record-lane-run", "resume-codex,")
STATE_ROOT_ENTRIES = ("state.sqlite3", "policy.json", "lanes.json",
                      "jobs", "lanes", "worktrees", "daemon.log")


def row(check: str, status: str, detail: str, fix: str) -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail, "fix": fix}


def _run_full(argv: list[str], timeout: float = 20.0) -> tuple[int | None, str]:
    """(rc, all of stdout+stderr). `rc is None` means it could not be run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    return done.returncode, f"{done.stdout or ''}\n{done.stderr or ''}".strip()


def _run(argv: list[str], timeout: float = 20.0) -> tuple[int | None, str]:
    """(rc, first line) — the shape the one-line detail columns want."""
    code, text = _run_full(argv, timeout)
    lines = text.splitlines()
    return code, lines[0] if lines else "(no output)"


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
                   f"{path}: all native v2 hook entries are present{v1_note}",
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
    code, text = _run_full([found, "-V"])
    first = (text.splitlines() or ["(no output)"])[0]
    if V1_MARKER in real:
        return row("subfleet symlink target", FAIL,
                   f"{link} — still v1 ({first})",
                   "repoint the symlink at the v2 entry point when the cutover "
                   "gates in docs/release-gates.md are green")
    if code != 0 and any(marker in text for marker in V1_OUTPUT_MARKERS):
        # v2 answers `-V`; v1 has no such flag and prints its own verb table.
        # The path says v2 and the behaviour says v1, so something on the import
        # path is v1 — `check_pythonpath` names the usual culprit.
        return row("subfleet symlink target", FAIL,
                   f"{link} — the path is not v1 but `{found} -V` answered with "
                   f"v1's verb table, so this entry point imports v1",
                   "check PYTHONPATH and `python -c \"import subfleet; "
                   "print(subfleet.__file__)\"` for which package actually wins")
    if code != 0:
        return row("subfleet symlink target", UNKNOWN,
                   f"{link} — `{found} -V` exited {code}: {first}",
                   f"run `{found} -V` by hand to see why it did not answer")
    return row("subfleet symlink target", PASS, f"{link} — {first}",
               "`subfleet doctor` after any change to this symlink")


def check_pythonpath() -> dict[str, Any]:
    """A `PYTHONPATH` entry holding a `subfleet` package outranks the install.

    Found by running this table for real on 2026-09-05: `PYTHONPATH` was set to
    the v1 checkout, so `.venv/bin/subfleet` — a v2 console script, in a v2
    virtualenv — imported v1's `subfleet` package and printed v1's verb table.
    `PYTHONPATH` precedes both site-packages and any `.pth` an editable install
    adds, so this survives every reinstall and is invisible from the symlink,
    which is why it gets a row of its own.
    """
    entries = [item for item in (os.environ.get("PYTHONPATH") or "").split(os.pathsep)
               if item]
    if _ignores_environment():
        return row("PYTHONPATH does not shadow subfleet", PASS,
                   "this interpreter runs with -E and ignores PYTHON* variables"
                   + (f" ({len(entries)} PYTHONPATH entr{'y' if len(entries) == 1 else 'ies'} present, unread)" if entries else ""),
                   "bin/sf2 and the launchd plist start every entry point this way")
    if not entries:
        return row("PYTHONPATH does not shadow subfleet", PASS, "PYTHONPATH is unset",
                   "keep it that way; the runtime is standard library only")
    shadows = [item for item in entries
               if (Path(item).expanduser() / "subfleet" / "__init__.py").exists()]
    if not shadows:
        return row("PYTHONPATH does not shadow subfleet", PASS,
                   f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, "
                   f"none holding a `subfleet` package",
                   "`echo $PYTHONPATH` if that ever changes")
    return row("PYTHONPATH does not shadow subfleet", FAIL,
               f"{', '.join(shadows)} hold{'s' if len(shadows) == 1 else ''} a "
               f"`subfleet` package and PYTHONPATH outranks the install, so every "
               f"`subfleet` entry point imports that one",
               "start subfleet through bin/sf2 (python -E) or unset PYTHONPATH; "
               "`python -c \"import subfleet; print(subfleet.__file__)\"` confirms")


def _ignores_environment() -> bool:
    """True when the interpreter was started with -E (or -I): PYTHON* variables are unread."""
    return bool(sys.flags.ignore_environment)


def check_path_shadows(binary: str) -> dict[str, Any]:
    """Report the effective executable without treating provider installs as faults.

    A provider can intentionally have a routing wrapper, a standalone CLI, and
    an app-bundled CLI. Their names alone cannot establish whether PATH order is
    correct. Multiple Subfleet front doors still fail the cutover check.
    """
    matches = path_matches(binary)
    if not matches:
        status = FAIL if binary == "subfleet" else UNKNOWN
        return row(f"PATH shadows for {binary}", status, f"no {binary} on PATH",
                   f"install {binary} or drop it from the fleet's expectations")
    if len(matches) == 1:
        return row(f"PATH shadows for {binary}", PASS, matches[0],
                   f"`which -a {binary}` if that ever changes")
    if binary in ("claude", "codex"):
        return row(f"PATH shadows for {binary}", UNKNOWN,
                   f"{len(matches)} on PATH; effective: {matches[0]}; "
                   f"alternatives: {' , '.join(matches[1:])}; intended ordering is unverified",
                   f"verify the effective {binary} is intended; reorder PATH only if it is not; "
                   f"multiple installed CLIs can be intentional (`which -a {binary}`)")
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
                   f"pid {info.get('pid')} is gone: {reason}"
                   + (f" — a socket is present but the lock is stale, so "
                      f"{root / SOCKET_NAME} belongs to nobody"
                      if socket_present else ""),
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
               f"near {AF_UNIX_PATH_MAX}, so no daemon can ever listen there — "
               f"set SUBFLEET_HOME to a shorter path",
               "set SUBFLEET_HOME to a shorter path, then `subfleet daemon start`")


def check_never_rules(settings: Path | None = None) -> dict[str, Any]:
    """C-14.3: the never-rules PreToolUse guard is installed for this user.

    v2 never writes this entry — it belongs to the guard, not to subfleet — so
    the honest report when the file cannot be read is `unknown`, not a failure.
    """
    path = (settings if settings is not None
            else Path("~/.claude/settings.json").expanduser())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return row("never-rules hook in ~/.claude/settings.json", UNKNOWN,
                   f"{path}: {exc.__class__.__name__} — cannot tell (C-14.3)",
                   f"read {path} by hand, or reinstall the guard hook")
    if any(marker in text for marker in
           ("never-rules", "never_rules", "subfleet-guard")):
        return row("never-rules hook in ~/.claude/settings.json", PASS,
                   f"{path}: present", "`subfleet doctor` after editing that file")
    return row("never-rules hook in ~/.claude/settings.json", FAIL,
               f"no never-rules hook in {path} (C-14.3)",
               "install the guard's PreToolUse hook; subfleet does not write it")


def check_guard_preflight(root: Path) -> dict[str, Any]:
    """C-14.2, C-23.5: the Codex guard preflight's deadline and cached verdicts.

    Offline, this reports the effective ``CODEX_GUARD_PREFLIGHT_TIMEOUT`` and how
    many verified markers are current; a deadline that cannot be parsed fails
    here rather than at the next launch.
    """
    from .guard import preflight as guard
    check = "codex guard preflight settings"
    try:
        deadline = guard.resolve_timeout()
    except ValueError as exc:
        return row(check, FAIL, str(exc), guard._CONFIG_FIX)
    source = "env" if os.environ.get(guard.TIMEOUT_ENV, "").strip() else "default"
    try:
        directory = guard.cache_dir(root)
    except ValueError as exc:
        return row(check, FAIL, str(exc), guard._CACHE_CONFIG_FIX)
    markers = 0
    try:
        markers = sum(1 for path in directory.glob("guard-ok-*.json")
                      if guard.read_cached_verdict(directory, path.name[len("guard-ok-"):-len(".json")]))
    except OSError:
        pass
    return row(check, PASS,
               f"hooks/list deadline {deadline:g}s ({source}); {markers} current verified marker(s) "
               f"in {directory}",
               f"set {guard.TIMEOUT_ENV} for the daemon to change the deadline; delete "
               f"{directory} to force re-verification")


def check_guard_preflight_live(root: Path) -> list[dict[str, Any]]:
    """C-14.2 (``--live``): run the trust preflight for every enabled Codex lane home."""
    from .guard import preflight as guard
    from .offline import Offline, OfflineUnavailable, SchemaTooNew
    try:
        lanes = Offline(root).lanes()
    except (OfflineUnavailable, SchemaTooNew, OSError, ValueError):
        return [row("codex guard preflight (live)", UNKNOWN,
                    "no readable store yet (the daemon has not run here)",
                    "start the daemon, then rerun subfleet doctor --live")]
    rows = []
    for lane in lanes:
        if lane.get("provider") != "codex" or not lane.get("enabled", 1):
            continue
        home = lane.get("home") or lane.get("credential_ref")
        check = f"codex guard preflight {lane.get('lane_id')}"
        if not home:
            rows.append(row(check, UNKNOWN, "lane has no home directory", "enroll the lane with a home"))
            continue
        # The same `codex` the adapter resolves on PATH; a pass here writes the
        # marker the daemon reuses (same key: version, home, override, seed files).
        result = guard.preflight("codex", home=home, workdir=root, state_root=root)
        deadline = f"{result.timeout_s:g}s" if result.timeout_s is not None else "unset"
        detail = (f"{result.kind or 'refused'} in {result.elapsed_s}s (deadline {deadline}"
                  f"{', cached' if result.cached else ''}"
                  f"{', probe pid ' + str(result.probe_pid) if result.probe_pid else ''}): {result.message}")
        rows.append(row(check, PASS if result.ok else FAIL, detail, result.fix or "rerun subfleet doctor --live"))
    if not rows:
        rows.append(row("codex guard preflight (live)", PASS, "no enabled Codex lane homes to verify",
                        "enroll a Codex lane to verify its guard"))
    return rows


def check_toolchain(binary: str = "uv") -> dict[str, Any]:
    """A dev-time dependency, not a runtime one: absent is `unknown`, not `fail`.

    Nothing the daemon or the adapters do needs `uv` (the runtime is standard
    library only); the tests and `uv sync` do, so it is worth one row.
    """
    found = shutil.which(binary)
    if found is None:
        return row(f"{binary} --version", UNKNOWN, f"{binary} is not on PATH",
                   f"only the test and build paths need {binary}; the runtime "
                   f"is standard library only")
    code, text = _run([found, "--version"])
    if code == 0:
        return row(f"{binary} --version", PASS, f"{found}: {text}",
                   "`uv sync --group dev` before running the suite")
    return row(f"{binary} --version", UNKNOWN, f"{found} exited {code}: {text}",
               f"run `{found} --version` by hand")


def check_module(name: str) -> dict[str, Any]:
    """A module the verbs degrade without. Missing is `unknown`: the verb that
    needs it says so itself, and a partial checkout is not a broken install."""
    try:
        __import__(f"subfleet.{name}")
    except ImportError as exc:
        return row(f"subfleet.{name}", UNKNOWN, f"not importable: {exc}",
                   f"the verbs that need subfleet.{name} degrade rather than fail")
    return row(f"subfleet.{name}", PASS, "importable",
               "nothing to do while this passes")


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


def check_mirror(root: Path) -> dict[str, Any]:
    """C-23.28: mirror health is a state file, never a quiet log.

    Judged only from the mirror's per-pass sidecar: a pass the sidecar records
    as in flight is healthy until thirty minutes after its recorded start, and
    only then is the mirror `stalled`. v1 read the log's mtime instead and
    reported a false "stalled" on 2026-08-19 07:08, because `--quiet` keeps the
    log silent on a no-op pass. An absent sidecar is `unknown`, not a failure:
    a fresh install has simply never run a pass.
    """
    from .sessions import mirror as mirror_module
    try:
        policy = load_policy(root / "policy.json")
    except (PolicyError, OSError):
        try:
            policy = load_policy(DEFAULT_POLICY_PATH)
        except (PolicyError, OSError):
            policy = {}
    health = mirror_module.health(root, policy)
    fix = "`subfleet sessions mirror --once`, then `subfleet daemon logs -n 40`"
    if health["status"] == "absent":
        return row("desktop sidebar mirror", UNKNOWN, health["detail"], fix)
    if health["status"] == "stalled":
        return row("desktop sidebar mirror", FAIL, health["detail"], fix)
    return row("desktop sidebar mirror", PASS, health["detail"],
               "`subfleet sessions mirror --status` for the sidecar")


# --- the table ----------------------------------------------------------------

def _with_fix(row: dict[str, Any]) -> dict[str, Any]:
    """Every doctor row carries a fix line, whatever it reports (C-17.1)."""
    fix = (row.get("fix") or "").strip()
    if not fix:
        detail = str(row.get("detail", ""))
        if row.get("status") == "pass":
            fix = "nothing to do"
        elif "no readable store" in detail or "no store" in detail:
            fix = "start the daemon once (subfleet daemon start) so a store exists, then run doctor again"
        else:
            fix = "read the detail: the identity checks name the lane and the command that releases it"
    return {**row, "fix": fix}


def checks(root: Path, *, live: bool = False,
           settings: Path | None = None) -> list[dict[str, Any]]:
    rows = [
        check_compat_table(),
        check_hook_entries(settings),
        check_never_rules(settings),
        check_symlink(),
        check_pythonpath(),
        *(check_path_shadows(binary) for binary in ("subfleet", "claude", "codex")),
        *(check_provider(binary) for binary in ("claude", "codex")),
        check_toolchain("uv"),
        check_guard_preflight(root),
        check_state_root(root),
        check_socket_path(root),
        check_daemon_lock(root),
        check_mirror(root),
        *(check_module(name) for name in ("store", "procs", "compat", "hooks",
                                          "sessions.mirror")),
    ]
    if live:
        rows.append(check_live(root))
        rows += check_guard_preflight_live(root)
    table = rows
    # C-10.3, C-10.7: the identity checks are rows of this one table. cli imports
    # doctor at module load, so the import is deferred to the call.
    from . import cli as _identity_source
    table.append(_with_fix(_identity_source._identity_roster_check(root)))
    if live:
        table += [_with_fix(row) for row in _identity_source.live_checks(root)]
    return table
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
