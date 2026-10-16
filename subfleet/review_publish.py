"""C-12.3/4: launch-scoped publishing restrictions, shared by both providers.

The installed wrappers embed their targets and policy; changing git config or
unsetting a marker cannot grant a wrapped push. Git's inherited push URL rewrite
also denies absolute-path git pushes. These are command-surface restrictions,
not an OS boundary against arbitrary code discarding its environment, alternate
binaries, or raw HTTPS with a credential found on disk. Never-rules are untouched.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import sys

from .contracts import JobSpec, Launch
from .guardian import atomic_publish

# Explicit launch authority, added AFTER #222's ambient environment allowlist.
# Its guardian_pass_args(env) passes these names across the guardian boundary.
GIT_DENIAL = {
    # Git gives this allowlist precedence over protocol.* config, including
    # remotes with an explicit pushurl (which bypass pushInsteadOf). Local git
    # reads work; remote Git transports, including fetch, are unavailable.
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "url.subfleet-publish-denied::.pushInsteadOf",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "protocol.subfleet-publish-denied.allow",
    "GIT_CONFIG_VALUE_1": "never",
}


def publishing_denied(job: JobSpec) -> bool:
    return job.task == "review" or job.kind == "gate-review"


def gh_read(args: list[str]) -> bool:
    """Fail closed on aliases/extensions/unknown verbs and ambiguous API options."""
    args = list(args)
    while args and args[0].startswith('-'):
        flag = args.pop(0)
        if flag in ('-R', '--repo', '--hostname') and args:
            args.pop(0)
        elif flag.startswith(('--repo=', '--hostname=')) or (flag.startswith('-R') and len(flag) > 2):
            continue
        else:
            return False
    if len(args) >= 2 and args[0] == 'pr':
        return args[1] in ('view', 'diff', 'checks')
    if not args or args[0] != 'api':
        return False
    index = 1
    while index < len(args):
        arg = args[index]
        if arg.startswith(('-f', '-F', '--field', '--raw-field', '--input')) or arg == 'graphql':
            return False
        if arg in ('-X', '--method'):
            index += 1
            if index == len(args) or args[index] != 'GET':
                return False
        elif arg.startswith('-X'):
            if arg[2:] != 'GET':
                return False
        elif arg.startswith('--method='):
            if arg.partition('=')[2] != 'GET':
                return False
        elif arg == '--':
            # Do not let a downstream parser reinterpret options after a separator.
            return False
        index += 1
    return True


def wrapper_main(tool: str, target: str | None) -> None:
    args = sys.argv[1:]
    allowed = 'push' not in args if tool == 'git' else gh_read(args)
    if not allowed:
        print(f'subfleet review publish: denied ({tool})', file=sys.stderr)
        raise SystemExit(7)
    if not target:
        print(f'subfleet review: {tool} is not installed', file=sys.stderr)
        raise SystemExit(127)
    if tool == 'git':
        os.environ.update(GIT_DENIAL)
    os.execv(target, [target, *args])


def protect_launch(job: JobSpec, launch: Launch, attempt_dir: Path) -> Launch:
    """Apply before spawn, and on resume. No prompt or deliverable changes."""
    if not publishing_denied(job) or launch.notes.get('publish_denied'):
        return launch
    directory = Path(attempt_dir) / 'publish-bin'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = launch.env_add.get('PATH', os.environ.get('PATH', os.defpath))
    # Embed the installed module path and interpreter; neither relies on an
    # inherited PYTHONPATH or a job-controlled SUBFLEET_* environment variable.
    package = str(Path(__file__).resolve().parent.parent)
    for tool in ('git', 'gh'):
        target = shutil.which(tool, path=path)
        script = (f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {package!r})\n'
                  f'from subfleet.review_publish import wrapper_main\n'
                  f'wrapper_main({tool!r}, {target!r})\n')
        atomic_publish(directory / tool, script.encode())
        (directory / tool).chmod(0o555)
    return replace(launch,
        env_add={**launch.env_add, **GIT_DENIAL, 'PATH': str(directory) + os.pathsep + path},
        notes={**launch.notes, 'publish_denied': True})
