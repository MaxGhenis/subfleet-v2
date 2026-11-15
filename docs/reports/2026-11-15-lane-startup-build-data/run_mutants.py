"""Apply D-LANE-STARTUP1's mutants M1-M15, one at a time, to a copy of the tree and run the tests.

    python3 run_mutants.py WORKTREE COPY RESULTS.json [MUTANT ...]

COPY is a `git archive HEAD` extraction made beforehand (never the worktree). For each
mutant: hash the copy's file, replace exactly the named text (the count must match),
run both lane-startup test modules in the copy, record the failing tests, write the
original bytes back and check their hash, and check the worktree's file hash too.
`M0` is the unmutated copy, which must pass. The tests run with the worktree's
venv (`uv run --project WORKTREE`) from the copy, so the copy's package is imported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ADAPTER = "subfleet/adapters/claude.py"
STARTUP = ('        startup = (job_startup_settings(job.workdir, self.provider_environment.assemble(credential_env))\n'
           '                   if sandbox == Sandbox.WORKSPACE_WRITE.value else None)  # D-LANE-STARTUP1\n')
SETTINGS_LINE = ('        argv += claude_settings_args(job, sandbox, attempt_dir, settings=lane_settings(startup))'
                 '  # C-12.4, D-WATCHDOG1\n')
WRITABLE_FLAGS = '            return ("--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", mcp_config,\n'
RETURN = ('    return {"env": dict(JOB_STARTUP_ENV), "autoMemoryEnabled": False, "disableAllHooks": False,\n'
          '            "claudeMdExcludes": list(dict.fromkeys(excludes))}\n')
FOLDERS = ('    folders = [parent for spelling in spellings for parent in map(str, Path(spelling).parents)'
           ' if parent != "/"]\n')

#: name -> (description, [(old, new, expected count)])
MUTANTS: dict[str, tuple[str, list[tuple[str, str, int]]]] = {
    "M0": ("no change (the copy must pass)", []),
    "M1": ("writable argv gains --setting-sources project,local",
           [(WRITABLE_FLAGS, WRITABLE_FLAGS.replace('return ("--dangerously', 'return ("--setting-sources", '
                                                    '"project,local", "--dangerously'), 1)]),
    "M2": ("disableAllHooks:false omitted", [(RETURN, RETURN.replace(' "disableAllHooks": False,', ''), 1)]),
    "M3": ("--safe-mode added to writable argv",
           [(WRITABLE_FLAGS, WRITABLE_FLAGS.replace('return ("--dangerously', 'return ("--safe-mode", "--dangerously'),
             1)]),
    "M4": ("claudeMdExcludes omitted",
           [(RETURN, RETURN.replace(',\n            "claudeMdExcludes": list(dict.fromkeys(excludes))}', '}'), 1)]),
    "M5": ("ancestors taken from job.workdir instead of R",
           [(FOLDERS, '    folders = [parent for parent in map(str, Path(os.path.abspath(workdir)).parents)'
                      ' if parent != "/"]\n', 1)]),
    "M6a": ("ancestor walk off by one: includes R",
            [(FOLDERS, FOLDERS.replace("map(str, Path(spelling).parents)",
                                       "map(str, [Path(spelling), *Path(spelling).parents])"), 1)]),
    "M6b": ("ancestor walk off by one: stops before the top folder",
            [(FOLDERS, FOLDERS.replace("map(str, Path(spelling).parents)",
                                       "map(str, list(Path(spelling).parents)[:-2])"), 1)]),
    "M7": ("R_real spelling dropped",
           [("    spellings = list(dict.fromkeys((root, os.path.realpath(root))))\n", "    spellings = [root]\n", 1)]),
    "M8": ("two separate negations",
           [('    excludes.append("!" + (spared[0] if len(spared) == 1 else "{" + ",".join(spared) + "}") + "/**")\n',
             '    excludes += ["!" + item + "/**" for item in spared]\n', 1)]),
    "M9a": ("config-home entries dropped",
            [('    excludes = [f"{glob_literal(config)}/{name}" for config in configs if os.path.isabs(config)\n'
              '                for name in ("CLAUDE.md", "rules/**")]\n', '    excludes = []\n', 1)]),
    "M9b": ("$HOME entries dropped",
            [('    for folder in dict.fromkeys([*folders, *([home] if os.path.isabs(home) else [])]):\n',
              '    for folder in dict.fromkeys(folders):\n', 1)]),
    "M10a": ("autoMemoryEnabled dropped", [(RETURN, RETURN.replace(' "autoMemoryEnabled": False,', ''), 1)]),
    "M10b": ("CLAUDE_CODE_DISABLE_AUTO_MEMORY dropped",
             [(RETURN, RETURN.replace('"env": dict(JOB_STARTUP_ENV), ', ''), 1)]),
    "M11": ("job-startup settings applied to read-only launches too",
            [(STARTUP, STARTUP.replace("\n                   if sandbox == Sandbox.WORKSPACE_WRITE.value else None)",
                                       ")"), 2)]),
    "M12": ("the keys put into lane_settings() (reaches conversation turns)",
            [('    merged = dict(settings or {})\n    merged["env"] = {**dict(merged.get("env") or {}), **LANE_SETTINGS_ENV}\n',
              '    merged = {"autoMemoryEnabled": False, "disableAllHooks": False, **dict(settings or {})}\n'
              '    merged["env"] = {**dict(merged.get("env") or {}), **JOB_STARTUP_ENV, **LANE_SETTINGS_ENV}\n', 1)]),
    "M13": ("CLAUDE_CODE_OAUTH_TOKEN replaced by CLAUDE_CONFIG_DIR",
            [("            env_add=dict(credential_env),\n            env_remove=env_remove,\n",
              "            env_add={**{k: v for k, v in credential_env.items() if k != 'CLAUDE_CODE_OAUTH_TOKEN'},\n"
              "                     'CLAUDE_CONFIG_DIR': str(_launch_home(credential_env) / '.claude')},\n"
              "            env_remove=env_remove,\n", 2)]),
    "M14": ("a second --settings added instead of merging",
            [(SETTINGS_LINE, SETTINGS_LINE.replace("lane_settings(startup))", "lane_settings())")
              + "        argv += ['--settings', json.dumps(startup)] if startup else []\n", 2)]),
    "M15": ("only build_launch updated (resume_launch keeps the old payload)", "resume"),
}
TESTS = ["tests/unit/test_lane_startup.py", "tests/unit/test_lane_startup_glob.py"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate(text: str, name: str) -> str:
    spec = MUTANTS[name][1]
    if spec == "resume":        # M15: the second (resume_launch) startup line only
        first = text.index(STARTUP)
        second = text.index(STARTUP, first + len(STARTUP))
        return text[:second] + "        startup = None  # mutant M15\n" + text[second + len(STARTUP):]
    for old, new, count in spec:
        found = text.count(old)
        if found != count:
            raise SystemExit(f"{name}: expected {count} of {old!r}, found {found}")
        text = text.replace(old, new)
    return text


def main(argv: list[str]) -> int:
    worktree, copy, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    names = argv[3:] or list(MUTANTS)
    target, mine = copy / ADAPTER, worktree / ADAPTER
    original, original_hash, worktree_hash = target.read_bytes(), sha(copy / ADAPTER), sha(mine)
    if original != mine.read_bytes():
        raise SystemExit("the copy's adapter is not the worktree's")
    results = json.loads(out.read_text()) if out.exists() else {}
    for name in names:
        text = original.decode("utf-8")
        mutated = mutate(text, name)
        if name != "M0" and mutated == text:
            raise SystemExit(f"{name} changed nothing")
        target.write_bytes(mutated.encode("utf-8"))
        basetemp = Path(os.environ["TMPDIR"]) / "sf-lsb" / f"pytest-mutant-{name}"
        started = time.time()
        uptime_start = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
        try:
            done = subprocess.run(
                ["uv", "run", "--project", str(worktree), "--no-sync", "--with", "pytest-xdist>=3.6", "python", "-m",
                 "pytest", *TESTS, "-q", "-n", "4", "-p", "no:cacheprovider", f"--basetemp={basetemp}", "-rf"],
                cwd=copy, capture_output=True, text=True, timeout=900)
        finally:
            target.write_bytes(original)
        restored = sha(target) == original_hash
        untouched = sha(mine) == worktree_hash
        if not (restored and untouched):
            raise SystemExit(f"{name}: restore {restored}, worktree untouched {untouched}")
        failed = sorted(set(re.findall(r"^FAILED (\S+?)(?: - .*)?$", done.stdout, re.M)))
        summary = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr[-300:]
        results[name] = {"mutant": MUTANTS[name][0], "rc": done.returncode, "summary": summary,
                         "failed": failed, "seconds": round(time.time() - started, 1),
                         "uptime_start": uptime_start,
                         "uptime_end": subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip(),
                         "restored_sha256": original_hash}
        out.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
        print(f"{name}: rc={done.returncode} {summary} failed={len(failed)} restored={restored}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
