"""Review r6 safety net (not part of the PR): refuse any exec of the system `security`.

Installed at plugin import, before collection. An audit hook cannot be removed by
monkeypatch.undo(). It resolves what the call would execute (argv[0] through the
call's PATH) and refuses `/usr/bin/security` by string, before fork or spawn.
Every refusal is appended to $R6_AUDIT_LOG. `R6_EXTRA_NATIVE` (os.pathsep list)
adds stand-in paths the hook treats as native, to demonstrate the proposed rule
without ever pointing it at the real binary.
"""

import os
import shutil
import sys

NATIVE = {"/usr/bin/security"}
EXTRA = set()


def _resolve(executable, args, env):
    name = executable
    if name is None:
        if isinstance(args, (str, bytes, os.PathLike)):
            name = args
        else:
            name = list(args)[0] if args else None
    if name is None:
        return None
    name = os.fsdecode(name)
    if os.sep not in name:
        path = os.pathsep.join(os.get_exec_path(env))
        found = shutil.which(name, path=path)
        if found is None:
            return name
        name = found
    return os.path.realpath(name)


def _hook(event, details):
    if event == "subprocess.Popen":
        executable, args, _cwd, env = details
    elif event == "os.posix_spawn":
        executable, args, env = details
    elif event == "os.exec":
        executable, args, env = details
    else:
        return
    try:
        target = _resolve(executable, args, env)
    except Exception:
        return
    if target is None:
        return
    native = NATIVE | EXTRA | {p for p in os.environ.get("R6_EXTRA_NATIVE", "").split(os.pathsep) if p}
    if target in native or os.path.basename(target) == "security" and target.startswith("/usr/"):
        log = os.environ.get("R6_AUDIT_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"{event}\t{target}\t{list(args) if not isinstance(args, str) else args}\n")
        raise PermissionError(f"r6 audit: refusing {target} under review")


sys.addaudithook(_hook)
