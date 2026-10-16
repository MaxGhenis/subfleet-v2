"""D-ST1: production behaviour outside pytest, observed on two source trees.

    python3 tools/live_store_guard_production_differential.py BASE_ROOT HEAD_ROOT

Each root is a checkout (or `git archive`) holding `subfleet/`. For each, a
fresh child with no pytest marker, a synthetic HOME and a recording `security`
first on PATH prints one JSON observation per line:

- `import`: whether importing installs the exec guard, and the refusal texts;
- `keychain_command`: the argv for each reference, reader spelling and runner;
- `resolve_credential` and `_keychain_blob`: the result or refusal through a
  recording runner, and the argv and options it received;
- `refusal`: the rendered public text of every handler reason;
- `guard`: the exec guard's verdict on synthetic launch events, uninstalled;
- `launch`: real wrapper launches with the guard installed, which print only
  where `sw_vers` would be found.

The driver compares the two outputs line by line and exits non-zero on any
difference. No observation runs the keychain reader: runners record and
return canned results, and the launches run `command -v sw_vers` or `true`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

CHILD = r'''
import json, os, subprocess, sys
from pathlib import Path

scratch, recorder = Path(sys.argv[1]), sys.argv[2]
observations = []


def emit(kind, case, value):
    observations.append(json.dumps({"kind": kind, "case": case, "value": value}, sort_keys=True,
                                   ensure_ascii=False, default=repr))


def outcome(call):
    try:
        return {"returned": call()}
    except BaseException as error:  # noqa: BLE001 - every refusal is an observation
        return {"raised": type(error).__name__, "text": str(error), "code": getattr(error, "code", None),
                "fix": getattr(error, "fix", None), "reason": getattr(error, "reason", None)}


from subfleet import credentials, refusals
from subfleet.adapters import claude
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential

emit("import", "guard-installed", hasattr(sys, credentials._EXEC_GUARD))
emit("import", "texts", [credentials._REFUSAL, credentials._REFUSAL_FIX, credentials.KEYCHAIN_BINARY])


class Recording:
    """A runner that records each call and returns, or raises, a canned result."""

    def __init__(self, returncode=0, stdout="", raises=None):
        self.returncode, self.stdout, self.raises, self.calls = returncode, stdout, raises, []

    def __call__(self, argv, **options):
        self.calls.append([list(argv), sorted(options.items())])
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


READERS = ["security", "Security", "SECURITY", "ſecurity", "/usr/bin/security", "/usr/bin/../bin/security",
           "//usr/bin/security", "/USR/BIN/security", "/System/Volumes/Data/usr/bin/security",
           recorder + "/security", "sh", "/bin/sh"]
REFERENCES = ["fixture-ref", "Claude Code-credentials", "claude-quota-1", "claude-quota-lane"]
helper = scratch / "home" / "bin" / "agent-secret"
for setting in ("absent", "variable", "home-file"):
    os.environ.pop("CLAUDE_LANE_AGENT_SECRET", None)
    if setting == "variable":
        os.environ["CLAUDE_LANE_AGENT_SECRET"] = str(scratch / "variable-agent-secret")
    if setting == "home-file":
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/bin/sh\nexit 3\n")
    for reference in REFERENCES:
        for reader in READERS:
            for label, runner in (("none", None), ("subprocess.run", subprocess.run),
                                  ("recording", Recording())):
                emit("keychain_command", [setting, reference, reader, label],
                     outcome(lambda: credentials.keychain_command(reference, reader, runner=runner)))
helper.unlink()
os.environ.pop("CLAUDE_LANE_AGENT_SECRET", None)

RUNNERS = {
    "blob": lambda: Recording(0, "blob-value\n"),
    "empty": lambda: Recording(0, "  \n"),
    "missing": lambda: Recording(44, ""),
    "oserror": lambda: Recording(raises=OSError("no such file")),
    "timeout": lambda: Recording(raises=subprocess.TimeoutExpired(["x"], 10)),
    "adapter": lambda: Recording(raises=AdapterError("runner refused", code=7, reason="credential-keychain")),
}
os.environ["SF_DIFF_TOKEN"], os.environ["SF_DIFF_BLANK"] = "token-value", "   "
CREDENTIALS = [("codex", "~/codex-home", "home"), ("claude", str(scratch / "a" / ".." / "b"), "home"),
               ("claude", "SF_DIFF_TOKEN", "env"), ("claude", "SF_DIFF_BLANK", "env"),
               ("claude", "SF_DIFF_UNSET", "env"), ("claude", "fixture-ref", "keychain-token"),
               ("claude", "claude-quota-2", "keychain-token"), ("codex", "fixture-ref", "keychain-token"),
               ("claude", "fixture-ref", "file")]
for provider, ref, kind in CREDENTIALS:
    for label, make in RUNNERS.items():
        runner = make()
        result = outcome(lambda: credentials.resolve_credential(Credential(provider, ref, kind), runner=runner))
        emit("resolve_credential", [provider, ref, kind, label], {"outcome": result, "calls": runner.calls})
for service in ("Claude Code-credentials", "Claude Code-credentials-1a2b3c4d", "claude-quota-3"):
    for reader in ("security", "/usr/bin/security", recorder + "/security"):
        for label, make in RUNNERS.items():
            runner = make()
            result = outcome(lambda: claude._keychain_blob(service, security_bin=reader, runner=runner))
            emit("_keychain_blob", [service, reader, label], {"outcome": result, "calls": runner.calls})

DETAILS = [None, {}, {"lane_id": "claude-1"}, {"job_id": "20261016-120000-fixture"}, {"count": 3, "limit": 5},
           {"task": "review", "by_policy": True}]
for reason in sorted(refusals.HANDLER_MESSAGES):
    for index, details in enumerate(DETAILS):
        emit("refusal", [reason, index], outcome(lambda: refusals.render(reason, details)))
refused = credentials.KeychainExecRefused(credentials._REFUSAL, code=7, fix=credentials._REFUSAL_FIX,
                                          reason="keychain-test-guard")
emit("refusal", "keychain-exec-refused", [refusals.handler_refusal(refused), refusals.refusal_detail(refused),
                                         isinstance(refused, AdapterError)])

SYSTEM = {"PATH": "/usr/bin:/bin"}
EVENTS = [
    ("subprocess.Popen", ("security", ["security", "x"], None, None)),
    ("subprocess.Popen", ("/usr/bin/security", ["/usr/bin/security", "x"], None, None)),
    ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "sw_vers"], None, SYSTEM)),
    ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "sw_vers"], None, {"PATH": "//usr/bin"})),
    ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "sw_vers"], None, {"PATH": "x", b"PATH": b"/usr/bin"})),
    ("subprocess.Popen", ("/usr/bin/env", ["/usr/bin/env", "-vu", "PATH", "sw_vers"], None, None)),
    ("subprocess.Popen", ("/usr/bin/env", ["/usr/bin/env", "-S", 'PATH=/usr/bin sw_vers #"'], None, None)),
    ("subprocess.Popen", ("/usr/bin/env", ["/usr/bin/env", "-x"], None, None)),
    ("subprocess.Popen", ("/bin/ksh", ["/bin/ksh", "-c", "sw_vers"], None, SYSTEM)),
    ("subprocess.Popen", ("/usr/bin/caffeinate", ["/usr/bin/caffeinate", "sw_vers"], None, SYSTEM)),
    ("subprocess.Popen", ("/usr/bin/arch", ["/usr/bin/arch", "-c", "sw_vers"], None, None)),
    ("os.posix_spawn", ("security", ["security"], {"PATH": os.environ["PATH"]})),
    ("os.system", ("sw_vers",)),
]
for pins in ([], [(str(scratch / "no-session"), "no-token")]):
    guard = credentials._exec_guard(pins)
    for event, details in EVENTS:
        emit("guard", [len(pins), event, repr(details)], outcome(lambda: guard(event, details)))

emit("launch", "installed", outcome(credentials.install_keychain_exec_guard))
LAUNCHES = [
    (["/bin/sh", "-c", "command -v sw_vers"], SYSTEM),
    (["/bin/sh", "-c", "command -v sw_vers"], {"PATH": "//usr/bin"}),
    (["/usr/bin/env", "-vu", "PATH", "/bin/sh", "-c", "command -v sw_vers"], None),
    (["/usr/bin/env", "-i", "PATH=/usr/bin", "/bin/sh", "-c", "command -v sw_vers"], None),
    (["/bin/ksh", "-c", "command -v sw_vers"], SYSTEM),
    (["/usr/bin/time", "/bin/sh", "-c", "command -v sw_vers"], SYSTEM),
    (["/usr/bin/caffeinate", "/bin/sh", "-c", "command -v sw_vers"], SYSTEM),
    (["/usr/bin/arch", "-arm64", "-e", "PATH=/usr/bin", "/bin/sh", "-c", "command -v sw_vers"], None),
    (["/bin/sh", "-c", "true"], None),
]
for argv, env in LAUNCHES:
    def launch():
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        return [done.returncode, done.stdout.strip()]
    emit("launch", argv, outcome(launch))
emit("launch", "os.system", outcome(lambda: os.system("true")))
emit("import", "guard-installed-at-end", hasattr(sys, credentials._EXEC_GUARD))
print("\n".join(observations))
'''


def observe(root: Path, scratch: Path) -> list[str]:
    """One root's observations, from a child with no pytest marker."""
    scratch.mkdir()
    (scratch / "home").mkdir()
    recorder = scratch / "keychain-bin"
    recorder.mkdir()
    record = scratch / "security-calls"
    (recorder / "security").write_text(f'#!/bin/sh\necho "$@" >> "{record}"\nexit 44\n')
    (recorder / "security").chmod(0o700)
    env = {"HOME": str(scratch / "home"), "PATH": f"{recorder}:/usr/bin:/bin", "TMPDIR": str(scratch) + "/",
           "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"}
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                           "exec(sys.argv.pop(1))", str(root), CHILD, str(scratch), str(recorder)],
                          env=env, cwd=scratch, capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise SystemExit(f"{root}: child failed\n{done.stderr}")
    if record.exists():
        raise SystemExit(f"{root}: the recording reader ran: {record.read_text()!r}")
    # The scratch path differs per root; observations name it only through it.
    return [line.replace(str(scratch), "<scratch>") for line in done.stdout.splitlines()]


def main() -> int:
    base, head = (Path(arg).resolve() for arg in sys.argv[1:3])
    scratch = Path(tempfile.mkdtemp(prefix="sf-diff-"))
    try:
        before, after = observe(base, scratch / "base"), observe(head, scratch / "head")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    counts: dict[str, int] = {}
    for line in after:
        kind = json.loads(line)["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    print(f"observations: base {len(before)}, head {len(after)}; by kind (head): {counts}")
    differing = [(index, old, new) for index, (old, new) in enumerate(zip(before, after)) if old != new]
    if len(before) != len(after):
        differing.append((min(len(before), len(after)), "<length>", "<length>"))
    for index, old, new in differing[:20]:
        print(f"differs at {index}:\n  base {old}\n  head {new}")
    print("identical" if not differing else f"{len(differing)} differences")
    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
