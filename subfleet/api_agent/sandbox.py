"""Where the tools may write, the shell's seatbelt profile and its environment (C-31.13).

The build tested two candidates against one matrix (`tools/credit_lane_sandbox_matrix.py`,
`docs/reports/credit-lane-harness-report.md`) and chose this Subfleet-owned profile
for `/usr/bin/sandbox-exec` over `codex sandbox`. Its base sections follow Codex's
(Chrome-derived) profile in codex-cli 0.159.0, without `com.apple.SecurityServer`.

- Reads: everywhere, except the account's keychains and credential files (`DENY_READ`).
- Writes, under `workspace-write`: the worktree except its top-level `.git`, and the
  attempt folder except Subfleet's own files (`PROTECTED_NAMES`, `PROTECTED_DIRS`,
  and whatever the folder held at start). Under `read-only`: the private TMPDIR only,
  which lives inside the attempt folder in both modes.
- Network: none without `--network`; with it, IP traffic, DNS through mDNSResponder
  and the TLS trust services. A unix socket outside the writable roots, such as the
  daemon's, is closed in both modes.
- Environment: the launch environment without the key and without any variable whose
  name holds KEY, TOKEN, SECRET or PASSWORD (`credential_name`).
"""
from __future__ import annotations

import os
import pwd
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from subfleet.api_agent.cli import READ_ONLY, WORKSPACE_WRITE
from subfleet.home_paths import CREDENTIAL_ENV_PATTERN

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
CONTROL_DIR = "harness"
TMP_DIR = "tmp"
#: Files the daemon, the guardian and this harness write in an attempt folder.
PROTECTED_NAMES = (
    "stream.jsonl", "messages.jsonl", "last.md", "stdout", "stderr", "start.json", "exit.json",
    "stdin.jsonl", "launch.json", "stream.freeze.json", "turn.json", "prompt.sent.md", "prompt.md",
    "lane.log", "finalization.json", "trees.json", "salvage.json", "request.json", "held_by.json",
    "guard-preflight.json", "deliverable.md", "abandon.json", "manifest.json", "mcp-config.json",
)
#: Folders of the same kind; `harness` is this harness's control folder.
PROTECTED_DIRS = (CONTROL_DIR, "publish-bin")
CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
#: Variables a non-interactive bash runs or obeys at start.
SHELL_STARTUP = frozenset({"BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "CDPATH",
                           "GLOBIGNORE", "PS4"})
#: Relative to each spelling of the account's home: read refused too.
DENY_READ = ("Library/Keychains", ".config/claude-credits", ".codex/auth.json", ".claude/.credentials.json")


def credential_name(name: str) -> bool:
    """A variable the shell never sees: KEY, TOKEN, SECRET or PASSWORD anywhere in its
    name, case ignored, or the test session's wider credential pattern."""
    upper = name.upper()
    return any(word in upper for word in CREDENTIAL_WORDS) or bool(CREDENTIAL_ENV_PATTERN.search(name))


def secret_name(name: str) -> bool:
    """A variable whose value never reaches a tool: the key and every other
    `ANTHROPIC_*` variable, and every `credential_name`. `__main__` also overwrites
    these values in the environment block copied at exec (`exec_env`)."""
    return name.startswith("ANTHROPIC_") or credential_name(name)


def tool_environment(launch: Mapping[str, str], tmpdir: Path) -> dict[str, str]:
    """The shell's and the hooks' environment."""
    env = {name: value for name, value in launch.items()
           if not secret_name(name) and name not in SHELL_STARTUP and not name.startswith("BASH_FUNC_")}
    env["TMPDIR"] = str(tmpdir)
    return env


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class Layout:
    """The resolved roots of one attempt. Every path here is a real path."""
    workdir: Path
    attempt: Path
    tmp: Path
    control: Path
    sandbox: str
    network: bool
    protected: tuple[Path, ...]
    protected_dirs: tuple[Path, ...]
    homes: tuple[Path, ...]

    @property
    def git(self) -> Path:
        return self.workdir / ".git"

    def readable(self, real: Path) -> bool:
        """The editor's reach: the worktree and the attempt folder."""
        return within(real, self.workdir) or within(real, self.attempt)

    def writable(self, real: Path) -> bool:
        """The same writes the profile allows, for the editor."""
        if within(real, self.tmp):
            return True
        if self.sandbox != WORKSPACE_WRITE:
            return False
        if within(real, self.workdir):
            return not within(real, self.git)
        if within(real, self.attempt):
            return real != self.attempt and real not in self.protected and not any(
                within(real, folder) for folder in self.protected_dirs)
        return False


def homes(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """HOME and the account's home from the user database, each resolved."""
    found: list[Path] = []
    candidates = [environ.get("HOME")]
    try:
        candidates.append(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    for candidate in candidates:
        if candidate and os.path.isabs(candidate):
            path = Path(os.path.realpath(candidate))
            if path not in found:
                found.append(path)
    return tuple(found)


def layout(*, workdir: Path, attempt: Path, sandbox: str, network: bool, extra_protected: Iterable[Path] = (),
           environ: Mapping[str, str]) -> Layout:
    """Resolve the roots, create the control and tmp folders, and freeze what exists."""
    if sandbox not in (READ_ONLY, WORKSPACE_WRITE):
        raise ValueError(f"unknown sandbox {sandbox!r}")
    real_workdir = Path(os.path.realpath(workdir))
    real_attempt = Path(os.path.realpath(attempt))
    control = real_attempt / CONTROL_DIR
    tmp = real_attempt / TMP_DIR
    for folder in (control, tmp):
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"{folder} is not a folder")
    protected = {real_attempt / name for name in PROTECTED_NAMES}
    folders = {real_attempt / name for name in PROTECTED_DIRS}
    with os.scandir(real_attempt) as entries:
        for entry in entries:
            path = real_attempt / entry.name
            if path in (control, tmp):
                continue
            if entry.is_dir(follow_symlinks=False):
                folders.add(path)
            else:
                protected.add(path)
    for path in extra_protected:
        real = Path(os.path.realpath(path))
        if within(real, real_attempt):
            protected.add(real)
    return Layout(workdir=real_workdir, attempt=real_attempt, tmp=tmp, control=control, sandbox=sandbox,
                  network=bool(network), protected=tuple(sorted(protected)),
                  protected_dirs=tuple(sorted(folders)), homes=homes(environ))


def _quote(path: Path | str) -> str:
    text = str(path)
    if any(ord(char) < 32 for char in text):
        raise ValueError("a sandbox path may not hold control characters")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_BASE = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow file-read*)
(allow file-write-data (require-all (path "/dev/null") (vnode-type CHARACTER-DEVICE)))
(allow file-write* (regex #"^/dev/fd/[0-9]+$"))
(allow sysctl-read
  (sysctl-name "hw.activecpu") (sysctl-name "hw.busfrequency_compat") (sysctl-name "hw.byteorder")
  (sysctl-name "hw.cacheconfig") (sysctl-name "hw.cachelinesize_compat") (sysctl-name "hw.cpufamily")
  (sysctl-name "hw.cpufrequency_compat") (sysctl-name "hw.cputype") (sysctl-name "hw.l1dcachesize_compat")
  (sysctl-name "hw.l1icachesize_compat") (sysctl-name "hw.l2cachesize_compat")
  (sysctl-name "hw.l3cachesize_compat") (sysctl-name "hw.logicalcpu_max") (sysctl-name "hw.machine")
  (sysctl-name "hw.model") (sysctl-name "hw.memsize") (sysctl-name "hw.ncpu") (sysctl-name "hw.nperflevels")
  (sysctl-name-prefix "hw.optional.arm.") (sysctl-name-prefix "hw.optional.armv8_")
  (sysctl-name "hw.packages") (sysctl-name "hw.pagesize_compat") (sysctl-name "hw.pagesize")
  (sysctl-name "hw.physicalcpu") (sysctl-name "hw.physicalcpu_max") (sysctl-name "hw.logicalcpu")
  (sysctl-name "hw.cpufrequency") (sysctl-name "hw.tbfrequency_compat") (sysctl-name "hw.vectorunit")
  (sysctl-name "machdep.cpu.brand_string") (sysctl-name "kern.argmax") (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfilesperproc") (sysctl-name "kern.maxproc") (sysctl-name "kern.osproductversion")
  (sysctl-name "kern.osrelease") (sysctl-name "kern.ostype") (sysctl-name "kern.osvariant_status")
  (sysctl-name "kern.osversion") (sysctl-name "kern.secure_kernel") (sysctl-name "kern.sysv.semmns")
  (sysctl-name "kern.usrstack64") (sysctl-name "kern.version") (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg") (sysctl-name-prefix "hw.perflevel") (sysctl-name-prefix "kern.proc.pgrp.")
  (sysctl-name-prefix "kern.proc.pid.") (sysctl-name-prefix "net.routetable."))
(allow sysctl-write (sysctl-name "kern.grade_cputype"))
(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow ipc-posix-sem)
(allow ipc-posix-shm-read-data ipc-posix-shm-write-create ipc-posix-shm-write-unlink
  (ipc-posix-name-regex #"^/__KMP_REGISTERED_LIB_[0-9]+$"))
(allow mach-lookup (global-name "com.apple.PowerManagement.control"))
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* (require-all (regex #"^/dev/ttys[0-9]+") (extension "com.apple.sandbox.pty")))
(allow file-ioctl (regex #"^/dev/ttys[0-9]+"))
(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))
(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.cfprefsd.agent")
  (local-name "com.apple.cfprefsd.agent"))
(allow user-preference-read)
"""

_NETWORK = """(allow network-outbound (remote ip "*:*"))
(allow network-inbound (local ip "*:*"))
(allow network-bind (local ip "*:*"))
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))
(allow sysctl-read (sysctl-name-regex #"^net.routetable"))
(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.TrustEvaluationAgent"))
"""


def _write_rules(layout: Layout) -> list[str]:
    """`(require-all (subpath ROOT) (require-not …))` filters, one per writable root."""
    rules = [f"(subpath {_quote(layout.tmp)})"]
    if layout.sandbox == WORKSPACE_WRITE:
        rules.append(f"(require-all (subpath {_quote(layout.workdir)}) (require-not (subpath {_quote(layout.git)})))")
        exclusions = [f"(require-not (literal {_quote(layout.attempt)}))"]
        exclusions += [f"(require-not (literal {_quote(path)}))" for path in layout.protected]
        exclusions += [f"(require-not (subpath {_quote(path)}))" for path in layout.protected_dirs]
        rules.append(f"(require-all (subpath {_quote(layout.attempt)}) {' '.join(exclusions)})")
    return rules


def profile(layout: Layout) -> str:
    """The seatbelt profile text for this attempt's shell."""
    lines = [_BASE]
    denied = [layout_home / relative for layout_home in layout.homes for relative in DENY_READ]
    if denied:
        lines.append("(deny file-read* " + " ".join(f"(subpath {_quote(path)})" for path in denied) + ")")
    rules = _write_rules(layout)
    lines.append("(allow file-write* " + " ".join(rules) + ")")
    # Unix sockets only inside the writable roots: never the daemon's socket.
    for root in _socket_roots(layout):
        lines.append(f"(allow network-bind network-inbound (local unix-socket (subpath {_quote(root)})))")
        lines.append(f"(allow network-outbound (remote unix-socket (subpath {_quote(root)})))")
    if layout.network:
        lines.append(_NETWORK)
    return "\n".join(lines) + "\n"


def _socket_roots(layout: Layout) -> list[Path]:
    roots = [layout.tmp]
    if layout.sandbox == WORKSPACE_WRITE:
        roots.append(layout.workdir)
    return roots


def command(profile_path: Path, argv: list[str]) -> list[str]:
    """`argv` under the profile."""
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
