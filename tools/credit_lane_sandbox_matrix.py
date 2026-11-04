"""The API credit lanes' sandbox matrix: two candidates, one set of checks (C-31.13).

Run it outside pytest, on macOS, from the repository root:

    env -u PYTHONPATH .venv/bin/python tools/credit_lane_sandbox_matrix.py [--codex PATH] [--json OUT]

Candidate `subfleet` is the harness's own profile (`subfleet.api_agent.sandbox`) under
`/usr/bin/sandbox-exec`, with the harness's tool environment. Candidate `codex` is
`codex sandbox -P :workspace` (codex-cli 0.159.0) with an empty, temporary CODEX_HOME,
so no person's Codex configuration is read, and the raw environment, so its own
environment policy decides what reaches the command.

Required checks (from the build brief):
- a write inside the worktree works;
- a write to `$HOME/credit-lane-check` fails (noclobber; never touches an existing file);
- `security find-generic-password -s subfleet-sandbox-check-none` fails with a sandbox
  denial, not "could not be found": the CLI prints "could not be found" either way, so
  the check also asks the bootstrap server for the keychain services, where 1100
  (BOOTSTRAP_NOT_PRIVILEGED) is the sandbox's denial;
- `ls ~/Library/Keychains` fails (a preference);
- `curl -sS https://example.com` fails without network and works with it;
- `env` shows no credential variable (two are planted for the check).

Informational checks: a unix socket outside the writable roots (the daemon's socket is
one), the worktree's `.git`, an attempt record, `/private/tmp`, and the environment
block of a process outside the sandbox (this script's), read with
`sysctl(KERN_PROCARGS2)` as `ps -E` reads it. Seatbelt does not mediate that sysctl,
which is why the harness overwrites the key's bytes there (`subfleet/api_agent/exec_env.py`).

Everything the checks write lives under a fresh `.sandbox-matrix-*` folder in the checkout, removed
at the end; `$HOME/credit-lane-check` and the `/private/tmp` probe are removed only if
this run created them.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet.api_agent import sandbox  # noqa: E402
from subfleet.api_agent.cli import WORKSPACE_WRITE  # noqa: E402

KEYCHAIN_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd", "com.apple.securityd.xpc")
PLANTED = {"SUBFLEET_MATRIX_API_KEY": "planted-matrix-value", "MATRIX_ACCESS_TOKEN": "planted-matrix-value"}
CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
TIMEOUT_S = 60

PROBE = r"""
import ctypes, sys
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
for name in sys.argv[1:]:
    found = ctypes.c_uint()
    print(name, libc.bootstrap_look_up(port, name.encode(), ctypes.byref(found)))
"""
PROCARGS = r"""
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
buffer = ctypes.create_string_buffer(size.value)
name = (ctypes.c_int * 3)(1, 49, int(sys.argv[1]))  # CTL_KERN, KERN_PROCARGS2
if libc.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) != 0:
    print("refused", ctypes.get_errno())
else:
    print("read" if sys.argv[2].encode() in buffer.raw[:size.value] else "absent")
"""
CONNECT = r"""
import socket, sys
s = socket.socket(socket.AF_UNIX)
try:
    s.connect(sys.argv[1]); print("connected")
except OSError as exc:
    print("refused", exc.errno)
"""


def run(argv: list[str], env: dict[str, str], cwd: Path) -> dict:
    try:
        proc = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": "", "err": "timed out"}
    return {"rc": proc.returncode, "out": proc.stdout.strip(), "err": proc.stderr.strip()}


class Candidate:
    def __init__(self, name: str, root: Path, network: bool, codex: str | None) -> None:
        self.name = name
        self.network = network
        self.work = root / "work"
        self.attempt = root / "attempt"
        self.codex = codex
        self.codex_home = root / "codex-home"
        if name == "subfleet":
            self.layout = sandbox.layout(workdir=self.work, attempt=self.attempt, sandbox=WORKSPACE_WRITE,
                                         network=network, environ=os.environ)
            self.profile = self.layout.control / "sandbox.sb"
            self.profile.write_text(sandbox.profile(self.layout), encoding="utf-8")

    def env(self) -> dict[str, str]:
        launch = dict(os.environ, **PLANTED)
        if self.name == "subfleet":
            return sandbox.tool_environment(launch, self.layout.tmp)
        return dict(launch, CODEX_HOME=str(self.codex_home))

    def argv(self, command: list[str]) -> list[str]:
        if self.name == "subfleet":
            return sandbox.command(self.profile, command)
        argv = [self.codex, "sandbox", "-P", ":workspace", "-C", str(self.work)]
        if self.network:
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        return [*argv, "--", *command]

    def bash(self, script: str) -> dict:
        return run(self.argv(["/bin/bash", "--noprofile", "--norc", "-c", script]), self.env(), self.work)

    def python(self, script: str, *args: str) -> dict:
        return run(self.argv([sys.executable, "-I", "-c", script, *args]), self.env(), self.work)


def checks(candidate: Candidate, home: Path, socket_path: str) -> list[dict]:
    rows = []

    def row(check: str, passed: bool | None, detail: str, required: bool = True) -> None:
        rows.append({"candidate": candidate.name, "network": candidate.network, "check": check,
                     "passed": passed, "required": required, "detail": detail[:300]})

    target = candidate.work / "matrix-write.txt"
    result = candidate.bash(f"printf ok > '{target}' && cat '{target}'")
    row("write inside the worktree", result["rc"] == 0 and result["out"] == "ok", f"rc={result['rc']}")
    if target.exists():
        target.unlink()

    probe = home / "credit-lane-check"
    if probe.exists() or probe.is_symlink():
        row("write to $HOME/credit-lane-check", None, "skipped: the file already exists, left untouched")
    else:
        result = candidate.bash(f"set -o noclobber; printf x > '{probe}'")
        created = probe.exists()
        if created:
            probe.unlink()
        row("write to $HOME/credit-lane-check", result["rc"] != 0 and not created,
            f"rc={result['rc']}, created={created}, {result['err'][-160:]}")

    result = candidate.bash("/usr/bin/security find-generic-password -s subfleet-sandbox-check-none")
    lookups = candidate.python(PROBE, *KEYCHAIN_SERVICES)
    codes = dict(line.rsplit(" ", 1) for line in lookups["out"].splitlines() if " " in line)
    denied = bool(codes) and all(code == "1100" for code in codes.values())
    row("security find-generic-password", result["rc"] != 0 and denied,
        f"rc={result['rc']}, said {result['err'][-90:]!r}; bootstrap look-ups {codes}")

    result = candidate.bash(f"ls '{home}/Library/Keychains'")
    row("ls ~/Library/Keychains", result["rc"] != 0, f"rc={result['rc']}, {result['err'][-120:]}")

    result = candidate.bash("curl -sS --max-time 20 -o /dev/null -w '%{http_code}' https://example.com")
    if candidate.network:
        row("curl https://example.com, network on", result["rc"] == 0 and result["out"] == "200",
            f"rc={result['rc']}, http={result['out']}, {result['err'][-120:]}")
    else:
        row("curl https://example.com, network off", result["rc"] != 0,
            f"rc={result['rc']}, {result['err'][-120:]}")

    result = candidate.bash("env")
    names = [line.split("=", 1)[0] for line in result["out"].splitlines() if "=" in line]
    leaked = sorted(name for name in names if any(word in name.upper() for word in CREDENTIAL_WORDS))
    planted = [name for name in leaked if name in PLANTED]
    # The launching session's own credential-shaped names stay out of the record: count them only.
    others = len(leaked) - len(planted)
    row("env shows no credential variable", result["rc"] == 0 and not leaked,
        f"credential-shaped names: {planted or 'none'} of the planted {sorted(PLANTED)}, "
        f"and {others} of the launching session's own (names withheld)")

    result = candidate.python(CONNECT, socket_path)
    row("unix socket outside the writable roots", result["out"].startswith("refused"), result["out"] or result["err"],
        required=False)

    (candidate.work / ".git").mkdir(exist_ok=True)
    result = candidate.bash(f"printf x > '{candidate.work}/.git/matrix-probe'")
    row("write in the worktree's .git", result["rc"] != 0, f"rc={result['rc']}", required=False)

    if candidate.name == "subfleet":
        record = candidate.attempt / "stream.jsonl"
        record.touch()
        result = candidate.bash(f"printf x >> '{record}'")
        row("append to an attempt record", result["rc"] != 0 and record.read_text() == "", f"rc={result['rc']}",
            required=False)

    result = candidate.python(PROCARGS, str(os.getpid()), Path(__file__).name)
    row("read an outside process's exec block (KERN_PROCARGS2)", result["out"] != "read",
        result["out"] or result["err"][-120:], required=False)

    tmp_probe = Path(f"/private/tmp/sf-matrix-{os.getpid()}-{candidate.name}-{int(candidate.network)}")
    result = candidate.bash(f"set -o noclobber; printf x > '{tmp_probe}'")
    made = tmp_probe.exists()
    if made:
        tmp_probe.unlink()
    row("write in /private/tmp", not made, f"rc={result['rc']}, created={made}", required=False)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--json")
    args = parser.parse_args()
    home = Path(os.path.realpath(os.path.expanduser("~")))
    base = Path(tempfile.mkdtemp(prefix=".sandbox-matrix-", dir=REPO))
    socket_dir = Path(tempfile.mkdtemp(prefix="sfm", dir="/private/tmp"))
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_dir / "s"))
    server.listen(8)
    stop = threading.Event()

    def accept():
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                server.accept()[0].close()
            except OSError:
                continue

    threading.Thread(target=accept, daemon=True).start()
    rows: list[dict] = []
    try:
        for name in ("subfleet", "codex"):
            if name == "codex" and not args.codex:
                rows.append({"candidate": "codex", "network": None, "check": "codex binary", "passed": None,
                             "required": True, "detail": "codex not found"})
                continue
            for network in (False, True):
                root = base / f"{name}-{int(network)}"
                for folder in ("work", "attempt", "codex-home"):
                    (root / folder).mkdir(parents=True)
                rows += checks(Candidate(name, root, network, args.codex), home, str(socket_dir / "s"))
    finally:
        stop.set()
        server.close()
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)
    print("| candidate | network | check | required | result | detail |")
    print("|---|---|---|---|---|---|")
    for item in rows:
        verdict = {True: "pass", False: "FAIL", None: "skipped"}[item["passed"]]
        detail = item["detail"].replace("|", "/").replace("\n", " ")
        print(f"| {item['candidate']} | {'on' if item['network'] else 'off'} | {item['check']} | "
              f"{'yes' if item['required'] else 'no'} | {verdict} | {detail} |")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
