"""Run PR #194's three requested mutations, restoring source after each run.

Ported from the review script; only the moved SQL site and artifact paths change.
Each pytest process gets a separate shared-lock hold and temporary directory.
No daemon, provider subprocess, or remote repository is started or contacted.
"""

from pathlib import Path
import json
import os
import signal
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "subfleet/scheduler.py", ROOT / "subfleet/daemon.py"]
ORIGINAL = {path: path.read_bytes() for path in FILES}
RESULTS = ROOT / ".git-local/provider-cap-mutations.json"
LOGS = ROOT / ".git-local/provider-cap-mutations"


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


def mutation(name):
    scheduler, daemon = [ORIGINAL[path].decode() for path in FILES]
    if name == "running_only":
        start = scheduler.index("def provider_live(")
        end = scheduler.index("\ndef provider_capacity(", start)
        scheduler = scheduler[:start] + '''def provider_live(view):
    lanes = {lane["lane_id"]: lane["provider"] for lane in view.get("lanes", ())}
    counts = dict.fromkeys(lanes.values(), 0)
    for attempt in view.get("attempts", ()):
        if attempt.get("state") == "running" and attempt.get("lane_id") in lanes:
            counts[lanes[attempt["lane_id"]]] += 1
    return counts

''' + scheduler[end:]
        # The reservation now reads quarantines separately to keep both partial
        # indexes. Mutate both reads; running rows contribute no quarantine count.
        daemon = replace_once(daemon,
            '"WHERE a.state IN (\'reserved\',\'starting\',\'running\',\'finalizing\') "\n                                    "AND l.provider=?",',
            '"WHERE a.state=\'running\' "\n                                    "AND l.provider=?",')
        daemon = replace_once(daemon,
            '"WHERE a.state=\'quarantined\' AND l.provider=?",',
            '"WHERE a.state=\'running\' AND l.provider=?",')
    elif name == "turns_exempt":
        scheduler = replace_once(scheduler,
            'limit = provider_cap(setup["policy"], provider)',
            'limit = None if setup["is_turn"] else provider_cap(setup["policy"], provider)')
        daemon = replace_once(daemon,
            'provider_limit = scheduler.provider_cap(self.policy, provider)',
            'provider_limit = None if pool == "turn" else scheduler.provider_cap(self.policy, provider)')
    elif name == "allow_equal":
        scheduler = replace_once(scheduler,
            'if provider and provider["live"] >= provider["max_active_attempts"]:',
            'if provider and provider["live"] > provider["max_active_attempts"]:')
        daemon = replace_once(daemon,
            'provider_at_limit = provider_count >= provider_limit - bool(provider_kept)',
            'provider_at_limit = provider_count > provider_limit - bool(provider_kept)')
    else:
        raise ValueError(name)
    return [scheduler.encode(), daemon.encode()]


def run_foreground(command, env):
    """Bound the entire task-owned command group, including pytest's children."""
    process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=1100)
        return process.returncode, output
    finally:
        # Killing only lockf must never release its lock and leave pytest alive.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    results = {}
    try:
        for name in ("running_only", "turns_exempt", "allow_equal"):
            for path, content in zip(FILES, mutation(name)):
                path.write_bytes(content)
            temporary = tempfile.mkdtemp(prefix=f"sf-r194-{name}-", dir=os.environ["TMPDIR"])
            env = {**os.environ, "TMPDIR": temporary + "/", "UV_CACHE_DIR": str(ROOT / ".uv-cache"),
                   "HYPOTHESIS_STORAGE_DIRECTORY": temporary + "/hypothesis"}
            command = ["/usr/bin/lockf", "-k", "-t", "45", "/private/tmp/claude-501/subfleet-suites.lock",
                       "uv", "run", "pytest", "-q", "tests/fake/test_provider_active_cap_fake.py", "--tb=short"]
            print(f"MUTATION {name}", flush=True)
            try:
                exit_code, output = run_foreground(command, env)
                failed = [line for line in output.splitlines() if line.startswith("FAILED ")]
                results[name] = {"exit_code": exit_code,
                                 "status": "not_run_lock" if exit_code == 75 else "killed" if failed else "survived_or_error",
                                 "failed_tests": failed,
                                 "summary": output.splitlines()[-1:]}
                (LOGS / f"{name}.log").write_text(output)
                print(output, flush=True)
            finally:
                for path, content in ORIGINAL.items():
                    path.write_bytes(content)
                subprocess.run(["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", temporary], check=True)
            RESULTS.write_text(json.dumps(results, indent=2) + "\n")
    finally:
        for path, content in ORIGINAL.items():
            path.write_bytes(content)
    if any(result["exit_code"] == 75 for result in results.values()):
        raise SystemExit(75)
    assert all(result["failed_tests"] for result in results.values()), results


if __name__ == "__main__":
    main()
