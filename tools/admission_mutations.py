"""Put each admission bug back and see whether the stateful search catches it.

Run from the repository root, inside one hold of the host's shared suite lock,
with TMPDIR and HYPOTHESIS_STORAGE_DIRECTORY isolated
(`docs/reports/2026-10-12-admission-properties.md`, "Running it"):

    python tools/admission_mutations.py BUDGET_S NAME [NAME ...]
    python tools/admission_mutations.py --list

NAME `baseline` runs the code as it is (the control). For each mutation: apply it
to the source (its anchor must occur once), run the stateful machine with the
random `dev` profile, no shrinking and no example database for at most BUDGET_S
seconds, then the scenario files, restore the source, and print one line. The
source is restored in `finally`, and the script refuses a dirty `subfleet/`.
`HYPOTHESIS_MAX_EXAMPLES` (default 400) caps the search inside the budget.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
D, S = "subfleet/daemon.py", "subfleet/scheduler.py"

MUTATIONS = {
    "fix1": ("Fix 1 reverted: a lane-pinned task competes on its whole chain", [
        (S, '            if job.get("pinned_lane"):\n                chain = mcp_chain(policy, chain, job)[:1]\n', "")]),
    "fix2": ("Fix 2 reverted: occupancy suffix back in the wait key", [
        (D, 'rechecks = self._capacity_wait(job["job_id"], scheduler.verdict_signature(decision), hold,',
            'rechecks = self._capacity_wait(job["job_id"], f"{scheduler.verdict_signature(decision)}:{at_limit}", hold,')]),
    "fix3": ("Fix 3 reverted: a quarantined probe's job is a waiter", [
        (D, '                if current["wait_reason"] in ("approval", "uncertain"):\n',
            '                if False:\n')]),
    "fix4-merge": ("Fix 4 reverted: the exclusions merge not isolated", [
        (D, '                if extra_exclusions:\n                    # C-6.12: checked where the route is prepared',
            '                if False:\n                    # C-6.12: checked where the route is prepared'),
        (D, '                            job["exclusions"] = self._merged_exclusions(job, extra_exclusions)',
            '                            job["exclusions"] = json.dumps(sorted(set(json.loads(job["exclusions"])) | set(extra_exclusions)))')]),
    "fix4-submit": ("Fix 4 reverted: submit accepts exclusions that are not names", [
        (D, 'if not isinstance(args.exclusions, list) or any(not isinstance(name, str) for name in args.exclusions):',
            'if False:')]),
    "b841d0d": ("b841d0d: one job's route error ends the pass", [
        (D, '        try:\n            yield route\n        except Unroutable as exc:',
            '        try:\n            yield route\n        except ZeroDivisionError as exc:'),
        (D, '                approved, desktop_account = self._prepare_route(job, decision_job, extra_exclusions)\n'
            '            except Unroutable as exc:',
            '                approved, desktop_account = self._prepare_route(job, decision_job, extra_exclusions)\n'
            '            except ZeroDivisionError as exc:')]),
    "cb83e1b": ("cb83e1b: every look is a new verdict", [
        (D, '        same = bool(wait) and wait["signature"] == signature', '        same = False')]),
    "034d3d3": ("034d3d3: a waiter holds its whole tier", [
        (D, '                             and scheduler.competes(models, waiter[1], lanes, waiter[2])\n',
            '                             and True\n')]),
    "de95879": ("de95879: lane pins do not narrow competition", [
        (S, '    return lanes is None or other_lanes is None or bool(lanes & other_lanes)', '    return True')]),
    "slot-keeping": ("slot-keeping dropped", [
        (D, 'limit = None if pool_cap is None else pool_cap - 1 if kept else pool_cap',
            'limit = None if pool_cap is None else pool_cap')]),
    "uncapped-holds": ("217: an uncapped pool holds jobs back", [
        (D, '            scope = scheduler.hold_scope(self.policy, job)\n',
            '            scope = scheduler.hold_scope(self.policy, job) or "pool"\n')]),
    "lease-holds": ("217: a lease waiter holds others back", [
        (D, '                             if not waiter[3]\n', '                             if True\n')]),
    "lease-keeps-none": ("217: a lease waiter keeps no slot", [
        (D, 'kept = [waiter for waiter in older_waiters(tier) if blocks(waiter)]',
            'kept = [waiter for waiter in older_waiters(tier) if blocks(waiter) and not waiter[3]]')]),
    "for-good-waits": ("217: a job no lane can admit is registered as a waiter", [
        (D, '                if job["wait_reason"] == "capacity" and not for_good:',
            '                if job["wait_reason"] == "capacity":'),
        (D, '                            if not for_good:\n                                wait(',
            '                            if True:\n                                wait(')]),
    "probe-waiter-dropped": ("217: a job waiting on a probe is no waiter", [
        (D, '                wait(tier, klass, (job["job_id"], models, lanes, frozenset()))\n'
            '                # C-6.11: which probe, and whose turn it is when it is not this job\'s.',
            '                # C-6.11: which probe, and whose turn it is when it is not this job\'s.')]),
    "probe-turn-any-model": ("217: a probe turn ignores the probe's model", [
        (S, "                 if wanted == model and (lanes is None or lane_id in lanes)), None)",
            "                 if (lanes is None or lane_id in lanes)), None)")]),
}

FIRST = re.compile(r"^E\s+(AssertionError: )?(.*)$|^(FAILED .*)$", re.M)


def run(args, budget, env):
    started = time.monotonic()
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-x", *args],
                             capture_output=True, text=True, timeout=budget, env=env)
        text, rc = out.stdout + out.stderr, out.returncode
    except subprocess.TimeoutExpired as exc:
        text, rc = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""), "timeout"
    return rc, text, time.monotonic() - started


def why(text):
    for match in FIRST.finditer(text):
        line = (match.group(2) or match.group(3) or "").strip()
        if line:
            return line[:220]
    return text.strip().splitlines()[-1][:220] if text.strip() else ""


def main():
    if sys.argv[1:] == ["--list"]:
        for name, (label, _) in MUTATIONS.items():
            print(f"{name}\t{label}")
        return
    budget, names = float(sys.argv[1]), sys.argv[2:]
    dirty = subprocess.run(["git", "status", "--porcelain", "subfleet/"], capture_output=True, text=True).stdout
    assert not dirty, f"subfleet/ is dirty: {dirty}"
    env = {**os.environ, "HYPOTHESIS_PROFILE": "dev", "HYPOTHESIS_NO_SHRINK": "1",
           "HYPOTHESIS_MAX_EXAMPLES": os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "400")}
    env.pop("CI", None)
    for name in names:
        label, edits = MUTATIONS.get(name, ("clean baseline", []))
        saved = {path: (ROOT / path).read_text() for path, _, _ in edits}
        try:
            for path, old, new in edits:
                text = (ROOT / path).read_text()
                assert text.count(old) == 1, f"{name}: anchor occurs {text.count(old)} times in {path}"
                (ROOT / path).write_text(text.replace(old, new))
            rc, text, seconds = run(["tests/fake/test_admission_stateful.py"], budget, env)
            search = "caught" if rc not in (0, "timeout") else ("not caught (budget)" if rc == "timeout" else "not caught")
            src, stext, _ = run(["tests/fake/test_admission_properties.py", "tests/fake/test_tier_hold_by_demand.py",
                                 "tests/fake/test_capacity_wait_backoff.py"], 300, env)
            scenarios = "fail" if src not in (0, "timeout") else ("timeout" if src == "timeout" else "pass")
            print(f"{name}\t{label}\tsearch={search}\t{seconds:.0f}s\t{why(text) if search == 'caught' else ''}"
                  f"\tscenarios={scenarios}\t{why(stext) if scenarios == 'fail' else ''}", flush=True)
        finally:
            for path, text in saved.items():
                (ROOT / path).write_text(text)
    dirty = subprocess.run(["git", "status", "--porcelain", "subfleet/"], capture_output=True, text=True).stdout
    assert not dirty, f"subfleet/ left dirty: {dirty}"


if __name__ == "__main__":
    main()
