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

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys

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


def publishing_denied(job: JobSpec) -> bool:
    return job.task == "review" or job.kind == "gate-review"


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


def claude_settings_args(job: JobSpec, sandbox: str, attempt_dir: Path) -> list[str]:
    """`--settings` with the publish hook for a writable Claude review launch (C-12.4).

    A read-only Claude launch has no Bash tool and runs with `--safe-mode`,
    which turns hooks off, so it gets none. The entry has no matcher: every
    tool call whose input carries a `command` is judged.
    """
    if not publishing_denied(job) or sandbox != Sandbox.WORKSPACE_WRITE.value:
        return []
    from .guard.preflight import _hook_command
    command = _hook_command(install_hook(Path(attempt_dir) / 'publish-bin'))
    settings = {"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": command, "timeout": CLAUDE_HOOK_TIMEOUT_S}]}]}}
    return ["--settings", json.dumps(settings, separators=(",", ":"))]


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
        env_add={**launch.env_add, **GIT_DENIAL, PUBLISH_MARKER: '1',
                 'PATH': str(directory) + os.pathsep + path},
        notes={**launch.notes, 'publish_denied': True})
