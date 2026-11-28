"""C-12.3/4: launch-scoped publishing restrictions, shared by both providers.

Three layers, none of which edits the prompt, the deliverable or the
never-rules guard:

1. **A PreToolUse command rule** (`publish_rule`), the layer that does not
   depend on PATH order. Both providers run commands through zsh, whose
   startup files put Homebrew or `/usr/bin` ahead of any wrapper directory
   (#221 review r1b, P1). Claude gets the rule as a `--settings` hook on a
   writable launch (`claude_settings_args`), and `subfleet hook PreToolUse`
   applies it too wherever the launch marker is set. Codex gets it as a second
   PreToolUse entry beside the guard in the `-c hooks=` override, which the
   guard preflight verifies with Codex's own `hooks/list` (`publish_hook`).
2. **Git configuration in the environment.** `GIT_ALLOW_PROTOCOL=""` refuses
   every remote transport, push and fetch alike, whatever config or absolute
   binary is used, and survives shell startup. A push URL rewrite to a refused
   transport backs it up. No name matches Codex's default `*KEY*`, `*SECRET*`
   or `*TOKEN*` excludes, so a lane config that turns them on keeps both.
3. **PATH wrappers** for `git` and `gh`, which hold when nothing reorders PATH.

These are command-surface restrictions, not an OS boundary: code that
discards its environment and calls an absolute git, that runs gh from another
language or a script on disk, or that makes raw HTTPS calls with a credential
found on disk is not stopped.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from .contracts import JobSpec, Launch, Sandbox
from .guardian import atomic_publish
from .publish_rule import PUBLISH_MARKER, gh_read, git_publishes  # noqa: F401 - gh_read is this module's API too

# Explicit launch authority, added AFTER #222's ambient environment allowlist.
# Its guardian_pass_args(env) passes these names across the guardian boundary.
GIT_DENIAL = {
    # Git gives this allowlist precedence over protocol.* config, including
    # remotes with an explicit pushurl (which bypass pushInsteadOf). Local git
    # reads work; remote Git transports, including fetch, are unavailable.
    "GIT_ALLOW_PROTOCOL": "",
    # `git -c`'s own form. GIT_CONFIG_COUNT/KEY_<n>/VALUE_<n> broke every git
    # command once Codex's documented `*KEY*` exclude stripped the keys
    # (`missing config key GIT_CONFIG_KEY_0`; #221 review r1b, P3-1).
    "GIT_CONFIG_PARAMETERS": ("'url.subfleet-publish-denied::.pushInsteadOf'='' "
                              "'protocol.subfleet-publish-denied.allow'='never'"),
}
#: The hook script's name, in a launch's `publish-bin` and in the state root's
#: `publish-guard` directory (the stable path a Codex trust hash is taken of).
HOOK_NAME = "review-publish-hook"
HOOK_DIRECTORY = "publish-guard"
#: Claude's hook budget; the rule itself takes milliseconds.
CLAUDE_HOOK_TIMEOUT_S = 30
#: What the launch self-test asks the installed hook to judge (#221 review r2, P3-7).
SELF_TEST_COMMAND = "gh pr comment 1 --body x"


def publishing_denied(job: JobSpec) -> bool:
    return job.task == "review" or job.kind == "gate-review"


def pinned_environment() -> dict[str, str]:
    """The variables every publish-denied launch sets: the git denial and the marker."""
    return {**GIT_DENIAL, PUBLISH_MARKER: "1"}


def codex_environment_overrides() -> list[str]:
    """`-c shell_environment_policy.set.<name>=<value>` for each pinned variable (#221 review r2, P3-3).

    Codex hands a command the environment its `shell_environment_policy`
    builds, so a lane config with `inherit="core"` (or an exclude) dropped the
    git denial, and real `codex sandbox` 0.159.0 then pushed to a local bare
    repo. `set` is applied after inheritance and excludes, so these hold
    whatever the lane's policy says. Each is a dotted key, which leaves any
    other `set` entry the lane has in place.
    """
    overrides: list[str] = []
    for name, value in pinned_environment().items():
        text = value.replace("\\", "\\\\").replace('"', '\\"')
        overrides += ["-c", f'shell_environment_policy.set.{name}="{text}"']
    return overrides


def wrapper_main(tool: str, target: str | None) -> None:
    args = sys.argv[1:]
    allowed = not git_publishes(args) if tool == 'git' else gh_read(args)
    if not allowed:
        print(f'subfleet review publish: denied ({tool})', file=sys.stderr)
        raise SystemExit(7)
    if not target:
        print(f'subfleet review: {tool} is not installed', file=sys.stderr)
        raise SystemExit(127)
    if tool == 'git':
        os.environ.update(GIT_DENIAL)
    os.execv(target, [target, *args])


def _package() -> str:
    # Embed the installed module path and interpreter; neither relies on an
    # inherited PYTHONPATH or a job-controlled SUBFLEET_* environment variable.
    return str(Path(__file__).resolve().parent.parent)


def install_hook(directory: Path) -> Path:
    """The PreToolUse hook script in `directory`, written when absent or stale.

    `-I` keeps the hook's interpreter from reading PYTHON* variables or the
    user site; the package path is in the script, not the environment.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / HOOK_NAME
    script = (f'#!{sys.executable} -I\nimport sys\nsys.path.insert(0, {_package()!r})\n'
              'from subfleet.publish_rule import hook_main\n'
              'raise SystemExit(hook_main())\n').encode()
    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if current != script:
        atomic_publish(path, script)
    if path.stat().st_mode & 0o777 != 0o555:
        path.chmod(0o555)
    return path


def publish_hook(state_root: Path) -> str:
    """The command of a Codex launch's publish hook (C-12.3, C-14.2).

    One stable path per state root, so the trust hash Codex keeps for it, and
    the guard preflight's cached verdict, survive from launch to launch.
    """
    from .guard.preflight import _hook_command
    return _hook_command(install_hook(Path(state_root) / HOOK_DIRECTORY))


def claude_settings_args(job: JobSpec, sandbox: str, attempt_dir: Path, *, settings: dict) -> list[str]:
    """The one `--settings` of a Claude job launch: `settings`, the launch's own
    (the adapter's `lane_settings()`, D-WATCHDOG1), with the publish hook merged
    in and `--setting-sources user` before it for a writable review launch
    (C-12.4). The hook joins those settings rather than riding a second flag.

    A read-only Claude launch has no Bash tool and runs with `--safe-mode`,
    which turns hooks off, so it gets no hook. The entry has no matcher: every
    tool call whose input carries a `command` is judged.

    `--setting-sources user` keeps the user's own settings (the never-rules
    guard's hook) and drops the reviewed tree's `.claude/settings.json` and
    `settings.local.json`, which could add hooks the rule never sees or set
    `disableAllHooks` (#221 review r2, P3-6). The `--settings` hook still
    loads: Claude Code 2.1.284 always adds `flagSettings` (the `--settings`
    source) and `policySettings` to the enabled sources, whatever
    `--setting-sources` names (its `fs()`; `docs/reference/claude-help.txt`'s
    `--restricted` entry says the same: "managed settings and --settings still
    apply"). The launch self-test (`hook_self_test`) runs the hook itself; that
    Claude loads it is shown by those references, not by a Claude run.
    """
    if not publishing_denied(job) or sandbox != Sandbox.WORKSPACE_WRITE.value:
        return ["--settings", json.dumps(settings, separators=(",", ":"))]
    command = hook_command(install_hook(Path(attempt_dir) / 'publish-bin'))
    hooked = {**settings, "hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": command, "timeout": CLAUDE_HOOK_TIMEOUT_S}]}]}}
    return ["--setting-sources", "user", "--settings", json.dumps(hooked, separators=(",", ":"))]


def hook_command(path: Path) -> str:
    """The shell command that runs the hook at `path` (the guard's quoting)."""
    from .guard.preflight import _hook_command
    return _hook_command(path)


def protect_launch(job: JobSpec, launch: Launch, attempt_dir: Path) -> Launch:
    """Apply before spawn, and on resume. No prompt or deliverable changes."""
    if not publishing_denied(job) or launch.notes.get('publish_denied'):
        return launch
    directory = Path(attempt_dir) / 'publish-bin'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = launch.env_add.get('PATH', os.environ.get('PATH', os.defpath))
    package = _package()
    for tool in ('git', 'gh'):
        target = shutil.which(tool, path=path)
        script = (f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {package!r})\n'
                  f'from subfleet.review_publish import wrapper_main\n'
                  f'wrapper_main({tool!r}, {target!r})\n')
        atomic_publish(directory / tool, script.encode())
        (directory / tool).chmod(0o555)
    return replace(launch,
        env_add={**launch.env_add, **pinned_environment(),
                 'PATH': str(directory) + os.pathsep + path},
        notes={**launch.notes, 'publish_denied': True})


def installed_publish_hooks(argv) -> list[str]:
    """The publish-hook commands a launch's own argv installs: Claude's
    `--settings` PreToolUse hooks, and the group of Codex's `-c hooks=`
    override whose status is the publish guard's."""
    from .guard.preflight import PUBLISH_HOOK_STATUS
    import tomllib
    argv = [str(arg) for arg in argv]
    commands: list[str] = []
    for index, arg in enumerate(argv):
        groups = []
        if arg == "--settings" and index + 1 < len(argv):
            try:
                groups = json.loads(argv[index + 1]).get("hooks", {}).get("PreToolUse", [])
            except (ValueError, AttributeError):
                continue
            wanted = lambda hook: True                                   # noqa: E731
        elif arg.startswith("hooks="):
            try:
                groups = tomllib.loads(arg)["hooks"].get("PreToolUse", [])
            except (tomllib.TOMLDecodeError, KeyError, AttributeError):
                continue
            wanted = lambda hook: hook.get("statusMessage") == PUBLISH_HOOK_STATUS  # noqa: E731
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(hook, dict) and hook.get("type") == "command" and isinstance(hook.get("command"), str) \
                        and wanted(hook):
                    commands.append(hook["command"])
    return commands


#: SHA-256 of a file, by its identity (real path, device, inode, size, mtime, ctime):
#: an 18 MB interpreter is hashed once per daemon run, not once per launch. The
#: ctime is there because `utime` can put an mtime back after an in-place rewrite
#: of the same size, but nothing but the kernel sets ctime (#221 review r3 P3).
_DIGESTS: dict[tuple, str] = {}


def _digest(path: str | Path) -> str:
    info = os.stat(path)
    key = (os.path.realpath(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    found = _DIGESTS.get(key)
    if found is None:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        found = _DIGESTS[key] = digest.hexdigest()
    return found


def hook_files(command: str) -> list[str]:
    """The files a hook command runs: the script it names, the interpreter on
    its `#!` line, and the modules the package path it inserts supplies
    (`subfleet/__init__.py` and `subfleet/publish_rule.py`, as `install_hook`
    writes it)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if not words:
        return []
    files = [words[0]]
    try:
        head = Path(words[0]).read_bytes()[:4096].decode("utf-8", "replace")
    except OSError:
        return files
    if head.startswith("#!") and head[2:].split():
        files.append(head[2:].split()[0])
    package = re.search(r"sys\.path\.insert\(0, (['\"])(.*?)\1\)", head)
    if package:
        files += [str(Path(package[2]) / "subfleet" / "__init__.py"),
                  str(Path(package[2]) / "subfleet" / "publish_rule.py")]
    return files


@dataclass(frozen=True)
class SelfTest:
    """A launch self-test's verdict: whether the hook denied, from the cache or not, and how long it took."""
    ok: bool
    cached: bool
    elapsed_s: float
    detail: str = ""


def _denies(stdout: str) -> bool:
    try:
        answer = json.loads(stdout.strip() or "{}")
    except ValueError:
        return False
    output = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
    return isinstance(output, dict) and output.get("permissionDecision") == "deny"


def hook_self_test(command: str, env: dict[str, str], *, cwd: str | None = None, cache: dict | None = None,
                   timeout_s: float = CLAUDE_HOOK_TIMEOUT_S) -> SelfTest:
    """Feed the hook command a launch will install a PreToolUse payload for
    `gh pr comment 1 --body x`, in the launch's environment, and say whether it
    answers with the deny JSON (#221 review r2, P3-7).

    The repo's references say a hook that exits without JSON lets the call
    through (`docs/reference/claude-hooks.md` section 3), and the guard
    preflight trusts the command string without running it, so a hook that
    cannot start (a moved interpreter, a package path that is gone) would
    allow every command. A pass is cached in `cache` (one per daemon run),
    keyed by the command and the SHA-256 of each file it runs (`hook_files`);
    a failure is not cached, so the next launch tries again.
    """
    started = time.monotonic()
    try:
        key = (command, *(_digest(path) for path in hook_files(command)))
    except OSError:
        key = None
    if cache is not None and key is not None and cache.get(key):
        return SelfTest(True, True, round(time.monotonic() - started, 6))
    payload = json.dumps({"session_id": "subfleet-publish-self-test", "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": SELF_TEST_COMMAND}, "cwd": cwd or "/"})
    try:
        result = subprocess.run(["/bin/sh", "-c", command], input=payload, env=env, cwd=cwd,
                                capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return SelfTest(False, False, round(time.monotonic() - started, 6), type(exc).__name__)
    ok = result.returncode == 0 and _denies(result.stdout)
    if ok and cache is not None and key is not None:
        cache[key] = True
    return SelfTest(ok, False, round(time.monotonic() - started, 6),
                    "" if ok else f"exit {result.returncode}, {'no deny' if result.returncode == 0 else 'failed'}")

