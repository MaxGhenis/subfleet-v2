"""Fix r1 mutants (sf-eff-f1). Each is applied alone to a scratch copy of HEAD
(`git archive HEAD`), never the worktree; its killers run from the copy; the file is
restored from a pristine copy and its SHA-256 checked against HEAD's blob."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCR = Path("/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-eff-f1")
COPY = SCR / "mut-src"
WT = Path("/Users/maxghenis/.subfleet/worktrees/20261114-190051-eff-fix-r1")
PY = SCR / "venv/bin/python"
E = "tests/unit/test_effort_env.py::"
MUTANTS = [
    ("MH", "subfleet/adapters/claude.py",
     '    if effort:\n        merged["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = effort\n',
     '    if effort is not None:\n        merged["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = effort\n',
     [E + "test_an_empty_effort_pins_nothing_on_any_path"]),
    ("R1-resume-pin-dropped", "subfleet/adapters/claude.py",
     "argv += claude_settings_args(job, sandbox, attempt_dir, settings=lane_settings(effort=effort))  # C-12.4, D-EFFORT-ENV1\n"
     "        from .isolation import claude_env_remove\n"
     "        env_remove = (*ENV_REMOVE, *claude_env_remove(os.environ.keys() | credential_env.keys())) if sandbox == \"read-only\" else ENV_REMOVE\n\n"
     "        stdin_path = self._write_prompt_sent(attempt_dir, Path(prompt_path))\n"
     "        transcript = self.expected_transcript_path(\n",
     "argv += claude_settings_args(job, sandbox, attempt_dir, settings=lane_settings())  # C-12.4, D-EFFORT-ENV1\n"
     "        from .isolation import claude_env_remove\n"
     "        env_remove = (*ENV_REMOVE, *claude_env_remove(os.environ.keys() | credential_env.keys())) if sandbox == \"read-only\" else ENV_REMOVE\n\n"
     "        stdin_path = self._write_prompt_sent(attempt_dir, Path(prompt_path))\n"
     "        transcript = self.expected_transcript_path(\n",
     [E + "test_property_resume_effort_pins_settings_and_preserves_argv",
      E + "test_resumed_attempt_pins_the_effort_its_fresh_launch_pins"]),
    ("R2-daemon-drops-resume-effort", "subfleet/daemon.py",
     'prompt_path, guard_override, model["id"], effort=model.get("effort"))',
     'prompt_path, guard_override, model["id"])',
     ["tests/fake/test_resume_contract.py::test_c12_resume_invokes_native_launch_on_original_lane"]),
    ("R3-resume-flag-dropped", "subfleet/adapters/claude.py",
     '        argv += ["--output-format", "stream-json", "--verbose"]\n        if effort:\n            argv += ["--effort", effort]\n',
     '        argv += ["--output-format", "stream-json", "--verbose"]\n',
     [E + "test_property_resume_effort_pins_settings_and_preserves_argv",
      E + "test_resumed_attempt_pins_the_effort_its_fresh_launch_pins"]),
    ("P1-parity-env-only", "tests/unit/test_claude_v1_parity.py",
     'V1_RUNNER = Path(os.environ["SUBFLEET_V1_RUNNER"]) if os.environ.get("SUBFLEET_V1_RUNNER") else DEFAULT_V1_RUNNER\n\n'
     'pytestmark = pytest.mark.skipif(\n    not V1_RUNNER.is_file(), reason=f"the v1 runner is not present at {V1_RUNNER}"\n)\n',
     'V1_RUNNER = Path(os.environ["SUBFLEET_V1_RUNNER"]) if os.environ.get("SUBFLEET_V1_RUNNER") else None\n\n'
     'pytestmark = pytest.mark.skipif(\n    V1_RUNNER is None or not V1_RUNNER.is_file(),\n'
     '    reason="set SUBFLEET_V1_RUNNER to an available v1 runner to check source parity",\n)\n',
     [E + "test_v1_source_parity_runs_by_default_where_the_v1_runner_exists"]),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def head_blob(path: str) -> bytes:
    return subprocess.run(["git", "-C", str(WT), "show", f"HEAD:{path}"], capture_output=True, check=True).stdout


def pytest(label: str, nodes: list[str]) -> tuple[int, str, float]:
    base = SCR / f"pytest-{label}"
    assert not base.exists(), base
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           # never let a parity run look at the real v1 tree
           "SUBFLEET_V1_RUNNER": str(SCR / "no-such-v1-runner")}
    env.pop("PYTHONPATH", None)
    started = time.monotonic()
    done = subprocess.run([str(PY), "-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                           f"--basetemp={base}", *nodes], cwd=COPY, env=env, capture_output=True, text=True,
                          timeout=1500)
    (SCR / "logs" / f"{label}.log").write_text(done.stdout + done.stderr)
    tail = [line for line in done.stdout.splitlines() if " passed" in line or " failed" in line or " error" in line]
    return done.returncode, (tail[-1] if tail else done.stdout[-300:]), time.monotonic() - started


# The same two resume mutants against review r1's probe P2 test alone (-x stopped at the property).
for source in [m for m in MUTANTS if m[0] in ("R1-resume-pin-dropped", "R3-resume-flag-dropped")]:
    MUTANTS.append((source[0] + "-p2", source[1], source[2], source[3],
                    [E + "test_resumed_attempt_pins_the_effort_its_fresh_launch_pins"]))
only = sys.argv[1:]
results = []
for name, path, old, new, killers in MUTANTS:
    if only and name not in only:
        continue
    target = COPY / path
    pristine = SCR / "pristine" / path
    pristine.parent.mkdir(parents=True, exist_ok=True)
    expected = sha(head_blob(path))
    assert sha(target.read_bytes()) == expected, f"{path} in the copy is not HEAD's"
    shutil.copy2(target, pristine)
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, (name, text.count(old))
    load_start = os.getloadavg()
    target.write_text(text.replace(old, new), encoding="utf-8")
    try:
        rc, summary, seconds = pytest(f"mut-{name}", killers)
    finally:
        shutil.copy2(pristine, target)
    restored = sha(target.read_bytes())
    results.append({"mutant": name, "file": path, "killers": killers, "rc": rc, "killed": rc == 1,
                    "summary": summary, "seconds": round(seconds, 1), "load_start": load_start,
                    "load_end": os.getloadavg(), "restored_sha256": restored,
                    "restored_matches_head": restored == expected})
    print(json.dumps(results[-1]), flush=True)
(SCR / "logs" / f"mutants{'-' + '-'.join(only) if only else ''}.json").write_text(json.dumps(results, indent=1))
