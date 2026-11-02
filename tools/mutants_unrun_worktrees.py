"""Remove C-6.20's rules (unrun worktrees) in a disposable copy and require tests to fail.

Run from the repository with ``uv run python tools/mutants_unrun_worktrees.py``.
The source defaults to this script's repository, regardless of the working directory;
``--scratch DIR`` chooses the parent of an automatically removed scratch directory.
Tests run serially, with no xdist. Each mutant first runs its focused tests, then
the complete C-6.20 test files if needed. JSON lines record every result, and
the command fails for any survivor, unapplied mutation or infrastructure error.
Use ``--only NAME ...`` to reproduce selected mutants, including their baseline.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DAEMON, SCHEDULER = "subfleet/daemon.py", "subfleet/scheduler.py"
GIVES_BACK = 'return hold.get("reason") == "disk" or bool(hold.get("unprepared"))'
STATE_RULE = 'if job["state"] not in ("queued", "waiting") or job["cancel_requested_at"] or not _gives_back(hold):'
JOB_RULE = 'if (not job or job["kind"] == "turn" or job["sandbox"] != "workspace-write" or job["in_place"]'
RECORDED_RULE = '                or job["worktree"] or job.get("push_branch") is not None\n'
PATH_RULE = "or not os.path.lexists(path) or os.path.islink(path) or self.store.list_attempts(job_id)):"
LEASE_RULE = '"SELECT 1 FROM leases WHERE lease_key=? OR holder=? LIMIT 1", (f"retire:{job_id}", job_id))'
SCOPE = 'if (kind == "detached" and not previous and job["sandbox"] == "workspace-write"'
SCOPE_TAIL = '                    and not job["in_place"] and not job.get("worktree") and job.get("push_branch") is None):'
HOLDS_BACK = ('                    if not for_good:\n'
              '                        wait(tier, klass, (job["job_id"], models, lanes, frozenset()))\n'
              '                    holds[job["job_id"]] = {**hold, "next_check_at": next_check}')
PASS_LOCKED = ('            if self.stopping.is_set() or not lock.acquire(timeout=UNRUN_PASS_WAIT_S):\n'
               '                return False\n'
               '            try:\n'
               '                if self._give_back_unrun(row["job_id"], legacy):\n'
               '                    given.append(row["job_id"])\n'
               '            finally:\n'
               '                lock.release()\n')

CANCEL_OFFER = ("        if unrun:\n"
                "            # C-6.20: a job cancelled before any attempt gives back the tree admission cut;\n"
                "            # a cancel outside the sweep's scope, a turn's above all, offers nothing.\n"
                "            self._offer_unrun_sweep()\n")
SCOPE = ('    return (job["sandbox"] == "workspace-write" and not job["in_place"] and job["worktree"] is None\n'
         '            and job["push_branch"] is None and job["kind"] != "turn")\n')

MUTANTS = [
    # Which holds give a waiting job's tree back.
    ("gives-back-disk-only", DAEMON, GIVES_BACK, 'return hold.get("reason") == "disk"'),
    ("gives-back-unprepared-only", DAEMON, GIVES_BACK, 'return bool(hold.get("unprepared"))'),
    ("gives-back-any-hold", DAEMON, GIVES_BACK, "return True"),
    # When a sweep is offered.
    ("cancel-offer-removed", DAEMON, CANCEL_OFFER, ""),
    ("cancel-offer-ungated", DAEMON, "        if unrun:\n            # C-6.20: a job cancelled",
     "        if changed:\n            # C-6.20: a job cancelled"),
    ("cancel-scope-dropped", DAEMON, "unrun = unrun or _unrun_scope(row)", "unrun = True"),
    # Which cancels offer one: `_unrun_scope` against `UNRUN_TREES`, a conjunct at a time.
    ("scope-sandbox-unchecked", DAEMON, SCOPE, SCOPE.replace('job["sandbox"] == "workspace-write" and ', "")),
    ("scope-in-place-unchecked", DAEMON, SCOPE, SCOPE.replace('not job["in_place"] and ', "")),
    ("scope-worktree-unchecked", DAEMON, SCOPE, SCOPE.replace(' and job["worktree"] is None', "")),
    ("scope-push-unchecked", DAEMON, SCOPE, SCOPE.replace('job["push_branch"] is None and ', "")),
    ("scope-turn-unchecked", DAEMON, SCOPE, SCOPE.replace(' and job["kind"] != "turn"', "")),
    ("start-offer-removed", DAEMON,
     "        # C-6.20: trees cut for jobs that ended unrun before this start.\n"
     "        self._offer_unrun_sweep()\n", ""),
    ("pass-offer-removed", DAEMON, "            if fresh:\n                self._offer_unrun_sweep()\n",
     "            if False:\n                self._offer_unrun_sweep()\n"),
    ("pass-offer-every-pass", DAEMON, "fresh, self._unrun_offered = held - self._unrun_offered, held",
     "fresh, self._unrun_offered = held, held"),
    # Retention.
    ("retention-first-sweep-removed", DAEMON,
     "        # C-6.20: trees no attempt used go before retention looks, and never beside it.\n"
     "        self._sweep_unrun_worktrees()\n", ""),
    ("retention-unlocked", DAEMON, "        with self._unrun_lock:\n            result = maintenance(",
     "        if True:\n            result = maintenance("),
    ("retention-dirty-sweep-removed", DAEMON,
     "        if self._unrun_dirty:\n"
     "            # C-6.20: a sweep offered while this pass ran found the lock taken.\n"
     "            self._sweep_unrun_worktrees()\n", ""),
    # The sweep's loop, flag and locks.
    ("busy-not-dirty", DAEMON,
     "            self._unrun_dirty = True    # retention is running; it sweeps once its pass ends\n", ""),
    ("dirty-loop-single-read", DAEMON, "                if not self._unrun_dirty:\n                    break\n",
     "                break\n"),
    ("pass-incomplete-not-dirty", DAEMON,
     "                if not self._unrun_pass(given):\n                    self._unrun_dirty = True\n                    break\n",
     "                if not self._unrun_pass(given):\n                    break\n"),
    ("failure-not-dirty", DAEMON,
     "        except Exception as exc:  # noqa: BLE001\n            self._unrun_dirty = True\n",
     "        except Exception as exc:  # noqa: BLE001\n"),
    ("failure-raised", DAEMON, '            self.log.warning("unrun worktrees: sweep failed: %s"',
     '            raise\n            self.log.warning("unrun worktrees: sweep failed: %s"'),
    ("pass-unlocked", DAEMON, PASS_LOCKED,
     "            if self.stopping.is_set():\n"
     "                return False\n"
     '            if self._give_back_unrun(row["job_id"], legacy):\n'
     '                given.append(row["job_id"])\n'),
    ("pass-ignores-stopping", DAEMON,
     "if self.stopping.is_set() or not lock.acquire(timeout=UNRUN_PASS_WAIT_S):",
     "if not lock.acquire(timeout=UNRUN_PASS_WAIT_S):"),
    ("pass-filter-everything", DAEMON, 'if (row["state"] in TERMINAL or row["job_id"] in held)', "if True"),
    ("legacy-dropped", DAEMON, '                if self._give_back_unrun(row["job_id"], legacy):',
     '                if self._give_back_unrun(row["job_id"]):'),
    # One tree's rule.
    ("rule-turn-allowed", DAEMON, JOB_RULE,
     'if (not job or job["sandbox"] != "workspace-write" or job["in_place"]'),
    ("rule-sandbox-unchecked", DAEMON, JOB_RULE, 'if (not job or job["kind"] == "turn" or job["in_place"]'),
    ("rule-in-place-unchecked", DAEMON, JOB_RULE,
     'if (not job or job["kind"] == "turn" or job["sandbox"] != "workspace-write"'),
    ("rule-recorded-unchecked", DAEMON, RECORDED_RULE, '                or job.get("push_branch") is not None\n'),
    ("rule-push-unchecked", DAEMON, RECORDED_RULE, '                or job["worktree"]\n'),
    ("rule-link-followed", DAEMON, PATH_RULE, "or not os.path.lexists(path) or self.store.list_attempts(job_id)):"),
    ("rule-attempt-unchecked", DAEMON, PATH_RULE, "or not os.path.lexists(path) or os.path.islink(path)):"),
    ("rule-live-state-unchecked", DAEMON, STATE_RULE, 'if job["cancel_requested_at"] or not _gives_back(hold):'),
    ("rule-cancel-unchecked", DAEMON, STATE_RULE,
     'if job["state"] not in ("queued", "waiting") or not _gives_back(hold):'),
    ("rule-hold-unchecked", DAEMON, STATE_RULE,
     'if job["state"] not in ("queued", "waiting") or job["cancel_requested_at"]:'),
    ("rule-why-disk-only", DAEMON, 'why = "disk hold" if hold.get("reason") == "disk" else "no lane"',
     'why = "disk hold"'),
    ("rule-retention-dir-unchecked", DAEMON,
     'if os.path.lexists(self.root / "retention" / job_id) or self.store.one(', "if self.store.one("),
    ("rule-retire-lease-unchecked", DAEMON, LEASE_RULE, '"SELECT 1 FROM leases WHERE holder=? LIMIT 1", (job_id,))'),
    ("rule-own-lease-unchecked", DAEMON, LEASE_RULE,
     '"SELECT 1 FROM leases WHERE lease_key=? LIMIT 1", (f"retire:{job_id}",))'),
    ("rule-dependents-unchecked", DAEMON,
     "        if (dependencies.dependents(self.store.query, tree, exclude=job_id, legacy_places=legacy)\n",
     "        if (False\n"),
    ("rule-own-evidence-counted", DAEMON, "exclude=job_id, legacy_places=legacy)", "exclude=None, legacy_places=legacy)"),
    ("rule-turn-folders-unchecked", DAEMON, "*folders.turn_folders(self.store.query),", "*(),"),
    ("rule-writer-folders-unchecked", DAEMON,
     "                    *(folder for folder, _ in folders.exclusive_folders(self.store.query))))):",
     "                    ))):"),
    ("rule-removal-unverified", DAEMON,
     '        if os.path.lexists(path):\n            return False\n'
     '        with self.store.transaction("job.worktree_given_back.recorded"',
     '        with self.store.transaction("job.worktree_given_back.recorded"'),
    ("rule-event-unrecorded", DAEMON, 'tx.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",\n'
     '                       (utcnow(), "job.worktree_given_back", job_id,',
     'tx.execute("SELECT ?,?,?,?",\n                       (utcnow(), "job.worktree_given_back", job_id,'),
    # Admission: no tree for a job no lane takes now.
    ("unprepared-check-removed", DAEMON, "                if refused:\n                    for_good = unadmittable_now()[0]",
     "                if False:\n                    for_good = unadmittable_now()[0]"),
    ("unprepared-default-horizon", DAEMON, "refused = unadmittable_now(far_s=0)[0]", "refused = unadmittable_now()[0]"),
    ("unprepared-retry-included", DAEMON, SCOPE, 'if (kind == "detached" and job["sandbox"] == "workspace-write"'),
    ("unprepared-read-only-included", DAEMON, SCOPE, 'if (kind == "detached" and not previous'),
    ("unprepared-turn-pass-included", DAEMON, SCOPE, 'if (not previous and job["sandbox"] == "workspace-write"'),
    ("unprepared-in-place-included", DAEMON, SCOPE_TAIL,
     '                    and not job.get("worktree") and job.get("push_branch") is None):'),
    ("unprepared-recorded-included", DAEMON, SCOPE_TAIL,
     '                    and not job["in_place"] and job.get("push_branch") is None):'),
    ("unprepared-push-included", DAEMON, SCOPE_TAIL,
     '                    and not job["in_place"] and not job.get("worktree")):'),
    ("unprepared-for-good-dropped", DAEMON,
     "                    for_good = unadmittable_now()[0]\n"
     '                    hold = {"reason": scheduler.refusal_label(refused)',
     "                    for_good = None\n"
     '                    hold = {"reason": scheduler.refusal_label(refused)'),
    ("unprepared-holds-back-for-good", DAEMON, HOLDS_BACK,
     '                    wait(tier, klass, (job["job_id"], models, lanes, frozenset()))\n'
     '                    holds[job["job_id"]] = {**hold, "next_check_at": next_check}'),
    ("unprepared-holds-back-nobody", DAEMON, HOLDS_BACK,
     '                    holds[job["job_id"]] = {**hold, "next_check_at": next_check}'),
    ("unprepared-task-hold-ignored", DAEMON,
     'with self.store.transaction("job.unprepared_wait", job_id=job["job_id"]) as tx:\n'
     "                        taken = self._task_held_at_write(tx, job, holds)            # C-11.10 (R3-1)\n",
     'with self.store.transaction("job.unprepared_wait", job_id=job["job_id"]) as tx:\n'
     "                        taken = None\n"),
    ("unprepared-not-demand", DAEMON, "                                                   lane_wait=True)",
     "                                                   lane_wait=False)"),
    ("unprepared-flag-dropped", DAEMON, '"refused_now": refused, "unprepared": True,', '"refused_now": refused,'),
    ("unprepared-label-raw", DAEMON, 'hold = {"reason": scheduler.refusal_label(refused),',
     'hold = {"reason": refused[0],'),
    ("unprepared-clock-dropped", DAEMON,
     '"AND cancel_requested_at IS NULL", (next_check, job["job_id"]))\n'
     "                    if taken is not None:\n                        continue\n                    if not for_good:",
     '"AND cancel_requested_at IS NULL", (None, job["job_id"]))\n'
     "                    if taken is not None:\n                        continue\n                    if not for_good:"),
    # The scheduler's horizon and label.
    ("far-ignored", SCHEDULER,
     '        far = admission_settings(policy)["pin_hold_far_s"] if far_s is None else far_s\n',
     '        far = admission_settings(policy)["pin_hold_far_s"]\n'),
    ("pin-far-ignored", SCHEDULER, 'admission_settings(policy)["pin_hold_far_s"] if far_s is None else far_s)',
     'admission_settings(policy)["pin_hold_far_s"])'),
    ("pin-far-unforwarded", SCHEDULER, "found = pin_unadmittable(policy, view, job, far_s=far_s)",
     "found = pin_unadmittable(policy, view, job)"),
    ("memo-key-without-horizon", SCHEDULER, "                   permanent_only, far)", "                   permanent_only)"),
    ("label-unscoped", SCHEDULER,
     'label = ":".join(reason.split(":")[:2]) if reason.startswith("closed:") else reason', "label = reason"),
    ("label-minority", SCHEDULER, "return max(counts, key=counts.__getitem__)", "return min(counts, key=counts.__getitem__)"),
]
TESTS = ["tests/fake/test_unrun_worktrees.py", "tests/unit/test_unadmittable_now.py"]

EQUIVALENTS = {
    "busy-not-dirty": (
        "Every caller that can find the lock taken sets the flag first (`_offer_unrun_sweep`, "
        "retention's check of it) or is retention's own first sweep, which the running sweep "
        "has just made; the running sweep reads its rows after either, so no tree waits longer."
    ),
    "pass-filter-everything": (
        "`_give_back_unrun` reads the job again under the lock and keeps every tree the filter "
        "drops; the filter only spares it those reads."
    ),
    "unprepared-recorded-included": (
        "`jobs.worktree` is set only at reservation, which makes an attempt, so `not previous` "
        "already keeps every job with a recorded tree out."
    ),
    "unprepared-turn-pass-included": (
        "Only the dispatcher submits a turn (`submit` refuses `kind=\"turn\"` without its turn "
        "block), and `ConversationService._submit_turn` always sets `in_place=True`, so "
        "`not job[\"in_place\"]` already keeps every turn out."
    ),
}

# Focus each mutation on its rule. A survivor still runs all of TESTS, so these
# selections improve cost without hiding a kill elsewhere in the C-6.20 suite.
UNPREPARED = "no_lane_takes or tree_cut_before or retry_keeps or cuts_no_tree or task_hold or c6_9 or c23_16"
FOCUSED = {
    "gives-back-disk-only": (TESTS[0], "property or tree_cut_before"),
    "gives-back-unprepared-only": (TESTS[0], "property or disk_hold_takes"),
    "gives-back-any-hold": (TESTS[0], "property or not_disk_held"),
    "cancel-offer-removed": (TESTS[0], "cancelled_before_any_attempt"),
    "cancel-offer-ungated": (TESTS[0], "outside_the_sweeps_scope"),
    "cancel-scope-dropped": (TESTS[0], "outside_the_sweeps_scope"),
    "scope-sandbox-unchecked": (TESTS[0], "scope_check_is or outside_the_sweeps_scope"),
    "scope-in-place-unchecked": (TESTS[0], "scope_check_is or outside_the_sweeps_scope"),
    "scope-worktree-unchecked": (TESTS[0], "scope_check_is or outside_the_sweeps_scope"),
    "scope-push-unchecked": (TESTS[0], "scope_check_is or outside_the_sweeps_scope"),
    "scope-turn-unchecked": (TESTS[0], "scope_check_is or outside_the_sweeps_scope"),
    "start-offer-removed": (TESTS[0], "offers_a_sweep_at_start"),
    "pass-offer-removed": (TESTS[0], "disk_hold_takes or tree_cut_before"),
    "pass-offer-every-pass": (TESTS[0], "disk_hold_takes"),
    "retention-first-sweep-removed": (TESTS[0], "beside_retention"),
    "retention-unlocked": (TESTS[0], "beside_retention"),
    "retention-dirty-sweep-removed": (TESTS[0], "beside_retention"),
    "busy-not-dirty": (TESTS[0], "beside_retention"),
    "dirty-loop-single-read": (TESTS[0], "read_again"),
    "pass-incomplete-not-dirty": (TESTS[0], "detached_pass_may_be_cutting"),
    "failure-not-dirty": (TESTS[0], "sweep_that_fails"),
    "failure-raised": (TESTS[0], "sweep_that_fails"),
    "pass-unlocked": (TESTS[0], "detached_pass_may_be_cutting"),
    "pass-ignores-stopping": (TESTS[0], "stopping"),
    "pass-filter-everything": (TESTS[0], "property"),
    "legacy-dropped": (TESTS[0], "legacy_read"),
    "rule-dependents-unchecked": (TESTS[0], "another_live_job or between_two or incomplete_evidence"),
    "rule-own-evidence-counted": (TESTS[0], "incomplete_evidence"),
    "rule-turn-folders-unchecked": (TESTS[0], "lease_holds"),
    "rule-writer-folders-unchecked": (TESTS[0], "lease_holds"),
    "rule-removal-unverified": (TESTS[0], "did_not_go"),
    "rule-event-unrecorded": (TESTS[0], "cancelled_before_any_attempt"),
    **{name: (TESTS[0], "property or claim_alone or link_in_place") for name, *_ in MUTANTS
       if name.startswith("rule-") and name not in {
           "rule-dependents-unchecked", "rule-own-evidence-counted", "rule-turn-folders-unchecked",
           "rule-writer-folders-unchecked", "rule-removal-unverified", "rule-event-unrecorded"}},
    **{name: (TESTS[0], UNPREPARED) for name, *_ in MUTANTS if name.startswith("unprepared-")},
    **{name: (TESTS[1], "c6_20") for name, file, *_ in MUTANTS if file == SCHEDULER},
}


def run(copy: Path, env: dict[str, str], tests: list[str]) -> tuple[int, str, float]:
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
                          cwd=copy, env=env, capture_output=True, text=True)
    lines = [l for l in done.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    output = (done.stdout or done.stderr).strip().splitlines()
    note = lines[0] if lines else (output[-1] if output else "no pytest output")
    return done.returncode, note[:200], round(time.perf_counter() - started, 1)


def emit(**result) -> None:
    print(json.dumps(result), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--only", nargs="+", default=[])
    args = parser.parse_args()
    source_root = args.repository.resolve()
    only = set(args.only)
    unknown = only - {name for name, *_ in MUTANTS}
    if unknown:
        parser.error("unknown mutants: " + ", ".join(sorted(unknown)))

    with tempfile.TemporaryDirectory(prefix="unrun-mutants-", dir=args.scratch) as folder:
        scratch = Path(folder).resolve()
        copy = scratch / "copy"
        copy.mkdir()
        shutil.copytree(source_root / "subfleet", copy / "subfleet", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(source_root / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__", "e2e"))
        shutil.copy2(source_root / "pyproject.toml", copy / "pyproject.toml")
        (scratch / "tmp").mkdir()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(copy),
               "TMPDIR": str(scratch / "tmp") + "/", "UV_CACHE_DIR": str(scratch / "tmp" / "uv-cache"),
               "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hyp")}
        where = subprocess.run([sys.executable, "-c", "import subfleet; print(subfleet.__file__)"],
                               cwd=copy, env=env, capture_output=True, text=True)
        if where.returncode or not where.stdout.strip().startswith(str(copy) + os.sep):
            emit(name="import-check", rc=where.returncode, note="tests must import the scratch package")
            return 1

        rc, note, seconds = run(copy, env, TESTS)
        emit(name="baseline", rc=rc, note=note, seconds=seconds)
        if rc:
            return 1
        killed = equivalent = survived = unapplied = errors = 0
        for name, file, old, new in MUTANTS:
            if only and name not in only:
                continue
            path = copy / file
            source = path.read_text()
            if source.count(old) != 1:
                emit(name=name, killed=False, rc=None, note=f"pattern matched {source.count(old)} times")
                unapplied += 1
                continue
            path.write_text(source.replace(old, new))
            try:
                selection = FOCUSED.get(name)
                tests = [selection[0], "-k", selection[1]] if selection else TESTS
                rc, note, seconds = run(copy, env, tests)
                if selection and rc == 0:
                    rc, note, complete_seconds = run(copy, env, TESTS)
                    seconds = round(seconds + complete_seconds, 1)
            finally:
                path.write_text(source)
            equivalence = EQUIVALENTS.get(name) if rc == 0 else None
            result = dict(name=name, killed=rc == 1, rc=rc, note=note, seconds=seconds)
            if equivalence:
                result.update(equivalent=True, reason=equivalence)
            emit(**result)
            killed += rc == 1
            equivalent += bool(equivalence)
            survived += rc == 0 and not equivalence
            errors += rc not in (0, 1)
        emit(name="summary", killed=killed, equivalent=equivalent, survived=survived, unapplied=unapplied, errors=errors)
        return int(bool(survived or unapplied or errors))


if __name__ == "__main__":
    raise SystemExit(main())
