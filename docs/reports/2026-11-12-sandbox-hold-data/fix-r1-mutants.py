"""Fix r1 mutants: apply one mutant to the export at mut/src, run its tests there, restore
the pristine bytes and check them. usage: mutants.py <name>|control|list
Never touches the worktree. Records to mutants.jsonl beside this file."""
import hashlib, json, os, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
assert "sf-sbh-f1" in str(HERE)
SRC = HERE / "mut" / "src"
PY = str(HERE / "venv" / "bin" / "python")
SANDBOX_SET = ["tests/fake/test_sandbox_hold_interleavings.py", "tests/fake/test_sandbox_hold_properties.py",
               "tests/fake/test_sandbox_hold_release_rows.py", "tests/fake/test_sandbox_hold_continuity.py",
               "tests/unit/test_sandbox_hold_contract.py"]

MUTANTS = {
    "Mn-no-row-release-wakes": dict(
        file="subfleet/daemon.py",
        old="            if row is None or (prefix is not None and ended is None):\n",
        new="            if prefix is not None and ended is None:\n",
        tests=["tests/fake/test_sandbox_hold_release_rows.py"]),
    "Mc-capability-check-removed": dict(
        file="subfleet/cli.py",
        old='        if "sandbox" in payload:\n            # D-RO-ADMIT1 (fix r1 of #309)',
        new='        if False:\n            # D-RO-ADMIT1 (fix r1 of #309)',
        tests=["tests/unit/test_sandbox_hold_versions.py", "tests/unit/test_sandbox_hold_cli.py"]),
    "Mp-push-build-before-hold-read": dict(
        file="subfleet/daemon.py",
        old=("                    if self._task_held_at_write(tx, job, None) is None:\n"
             "                        if start:\n"),
        new=("                    if start:\n"
             "                        self._push_workspaces[job_id] = self.pushes.submit(\n"
             "                            self._run_worker, self._prepare_push_workspace, dict(job), destination)\n"
             "                    if self._task_held_at_write(tx, job, None) is None:\n"
             "                        if False:\n"),
        tests=["tests/unit/test_host_push_sandbox_hold.py"]),
    "m1-take-uses-new-hold": dict(
        file="subfleet/daemon.py",
        old=("            # the other target. Replacing one hold must not shorten the other.\n"
             "            current = task_holds.job_held(tx, job_id, now)\n"),
        new=("            # the other target. Replacing one hold must not shorten the other.\n"
             "            current = hold\n"),
        tests=SANDBOX_SET),
    "m2-sandbox-clause-drops-task-guard": dict(
        file="subfleet/daemon.py",
        old="            return (\"COALESCE(task,'')!='' AND COALESCE(NULLIF(sandbox,''),'workspace-write')=?\",\n",
        new="            return (\"COALESCE(NULLIF(sandbox,''),'workspace-write')=?\",\n",
        tests=SANDBOX_SET + ["tests/unit/test_sandbox_hold_versions.py", "tests/unit/test_sandbox_hold_cli.py",
                             "tests/unit/test_host_push_sandbox_hold.py", "tests/fake/test_task_hold_admission.py",
                             "tests/unit/test_task_holds.py"]),
    "m3-release-ignores-remaining-hold": dict(
        file="subfleet/daemon.py",
        old=("                remaining = task_holds.job_held(tx, job_id, now)\n"
             "                if remaining is not None:\n"),
        new=("                remaining = None\n"
             "                if remaining is not None:\n"),
        tests=SANDBOX_SET),
    "m1+m2-both-guards-dropped": dict(
        file="subfleet/daemon.py",
        pairs=[("            # the other target. Replacing one hold must not shorten the other.\n"
                "            current = task_holds.job_held(tx, job_id, now)\n",
                "            # the other target. Replacing one hold must not shorten the other.\n"
                "            current = hold\n"),
               ("            return (\"COALESCE(task,'')!='' AND COALESCE(NULLIF(sandbox,''),'workspace-write')=?\",\n",
                "            return (\"COALESCE(NULLIF(sandbox,''),'workspace-write')=?\",\n")],
        tests=["tests/fake/test_sandbox_hold_release_rows.py"]),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(label, tests, alarm):
    basetemp = HERE / f"pytest-mut-{label}"
    assert not basetemp.exists()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1",
               TMPDIR=os.popen("getconf DARWIN_USER_TEMP_DIR").read().strip(),
               HYPOTHESIS_STORAGE_DIRECTORY=str(HERE / "hypothesis-mut"))
    probe = subprocess.run([PY, "-c", "import subfleet; print(subfleet.__file__)"], cwd=SRC, env=env,
                           capture_output=True, text=True).stdout.strip()
    assert probe.startswith(str(SRC)), probe
    log = HERE / "logs" / f"mut-{label}.log"
    cmd = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp),
           *(["-n", "4"] if len(tests) > 1 else []), *tests]
    up0 = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
    t0 = time.time()
    with open(log, "w") as fh:
        proc = subprocess.Popen(cmd, cwd=SRC, env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc, stopped = proc.wait(timeout=alarm), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                rc = proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); rc = proc.wait()
            stopped = True
    lines = log.read_text(errors="replace").splitlines()
    failed = [l.split(" ", 1)[1].split(" - ")[0] for l in lines if l.startswith(("FAILED ", "ERROR "))]
    summary = next((l for l in reversed(lines) if " passed" in l or " failed" in l or " error" in l), "")
    return dict(cmd=cmd, cwd=str(SRC), imported=probe, rc=rc, stopped_by_alarm=stopped,
                elapsed_s=round(time.time() - t0, 1), uptime_start=up0,
                uptime_end=subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip(),
                summary=summary, failed=failed)


name = sys.argv[1]
alarm = int(os.environ.get("RUN_ALARM", "560"))
if name == "list":
    print("\n".join(MUTANTS)); sys.exit()
if name == "control":
    tests = sorted({t for m in MUTANTS.values() for t in m["tests"]})
    rec = {"mutant": "control", **run("control", tests, alarm)}
else:
    m = MUTANTS[name]
    target = SRC / m["file"]
    pristine = target.read_bytes()
    before = hashlib.sha256(pristine).hexdigest()
    text = pristine.decode()
    pairs = m.get("pairs") or [(m["old"], m["new"])]
    for old, new in pairs:
        assert text.count(old) == 1, f"{name}: anchor found {text.count(old)} times"
        text = text.replace(old, new)
    try:
        target.write_text(text)
        mutated = sha(target)
        assert mutated != before
        rec = {"mutant": name, "file": m["file"], "sha_pristine": before, "sha_mutated": mutated,
               **run(name, m["tests"], alarm)}
    finally:
        target.write_bytes(pristine)
    rec["sha_restored"] = sha(target)
    assert rec["sha_restored"] == before, "restore failed"
    rec["killed"] = rec["rc"] != 0 and bool(rec["failed"])
with open(HERE / "mutants.jsonl", "a") as fh:
    fh.write(json.dumps(rec) + "\n")
print(json.dumps({k: v for k, v in rec.items() if k != "cmd"}, indent=1))
