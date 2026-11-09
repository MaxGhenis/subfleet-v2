"""The d2660 brief's 15 mutants of the retention delete path, and four more for
the defects its tests found, each applied alone and killed by a named test.

Each lane is a copy of the committed tree (`git archive HEAD`: `subfleet/`,
`tests/`, `pyproject.toml`) in a folder named `sf-d2660-b1.mut.*` under the
temporary folder. For each mutant a lane reads the pristine bytes of each file
it changes, checks that every edit's old text occurs exactly once, writes the
mutated text, runs the killer alone (`pytest -x`, no xdist, no bytecode), then
writes the pristine bytes back and checks their SHA-256. A mutant is killed
when its killer fails (pytest exit 1), survives when it passes (exit 0), and is
an error otherwise. The lanes are removed at the end with a guarded rmtree.

Run from the repository root, after committing, with the project's Python:

    uv run --no-sync python docs/reports/2026-11-09-retention-delete-d2660/mutants.py --lanes 4

`--only 3,14` runs some; the results go to `mutants.json` beside this file.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RD = "subfleet/retention_delete.py"
APPLY = "tests/unit/test_retention_delete_apply.py"
RESUME = "tests/unit/test_retention_delete_resume.py"
REQUEST = "tests/unit/test_retention_delete_request.py"


def lines(*parts: str) -> str:
    return "\n".join(parts)


MUTANTS = [
    {"id": 1, "name": "Skip the owned-path gate", "file": RD,
     "edits": [('        if job.get("in_place"):', '        if False:'),
               ('        if job.get("sandbox") != "workspace-write":', '        if False:'),
               ("        if tree == allocated or allocated not in tree.parents:", "        if False:"),
               ("        if not _inside(folder, str(allocated)) or _key(folder) == _key(str(allocated)):",
                "        if False:")],
     "killer": f"{APPLY}::test_gates_one_to_four_keep_their_jobs_whole"},
    {"id": 2, "name": "Skip the top-level excluded-repository check", "file": RD,
     "edits": [(lines('            if why == "excluded":',
                      '                raise _Gate("exclude-top", f".git names {target}, in an excluded repository")'),
                lines("            if False:",
                      '                raise _Gate("exclude-top", f".git names {target}, in an excluded repository")'))],
     "killer": f"{APPLY}::test_a_tree_whose_gitfile_names_an_excluded_repository_is_not_read_beyond_it"},
    {"id": 3, "name": "Skip the nested excluded-repository check", "file": RD,
     "edits": [(lines('            if why == "excluded":',
                      '                raise _Gate(gate, f"{root.rel} names {target}, in an excluded repository")'),
                lines('            if why == "excluded" and gate == "exclude-top":',
                      '                raise _Gate(gate, f"{root.rel} names {target}, in an excluded repository")'))],
     "killer": f"{APPLY}::test_gates_five_to_twelve_keep_their_jobs_whole"},
    {"id": 4, "name": "Skip the holder scan after the rename", "file": RD,
     "edits": [('        self._holders(job_id, [str(tombstone), str(tree), journal["folder"]], plan["scan"].identity)',
                "        pass")],
     "killer": f"{APPLY}::test_a_holder_seen_after_the_rename_puts_the_tree_back"},
    {"id": 5, "name": "Ignore a nested linked worktree when keeping commits", "file": RD,
     "edits": [('        for root in keep["outside"]:\n            self._check()\n            if not root["tips"]:',
                '        for root in [r for r in keep["outside"] if r["rel"].lower() == ".git"]:\n'
                '            self._check()\n            if not root["tips"]:')],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
    {"id": 6, "name": "Carry on after a failed git bundle verify", "file": RD,
     "edits": [('            rgit.verify_bundle(scratch, temporary, expected, repo["fmt"], verify, cancel=self.cancel)',
                lines("            try:",
                      '                rgit.verify_bundle(scratch, temporary, expected, repo["fmt"], verify, '
                      "cancel=self.cancel)",
                      "            except rgit.GitError:",
                      "                pass"))],
     "killer": f"{APPLY}::test_a_failure_on_one_job_fails_it_whole_and_the_run_goes_on"},
    {"id": 7, "name": "Remove the tree before the kept-commits step is recorded", "file": RD,
     "edits": [('        kept = self._keep(journal, job_id, plan["keep"])',
                lines("        remove_tree(tree)", '        kept = self._keep(journal, job_id, plan["keep"])'))],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
    {"id": 8, "name": "Let a dry run write the ref", "file": RD,
     "edits": [(lines("            if dry:",
                      '                result.update(disposition="would-delete", ended_at=_utc(self.now()))'),
                lines("            if dry:",
                      '                for root in plan["keep"]["outside"]:',
                      '                    if root["tips"]:',
                      '                        rgit.create_ref(Path(root["common"]), KEPT_NAMESPACE + "/" + job_id '
                      '+ "/0/0",',
                      '                                        root["tips"][0], cancel=self.cancel)',
                      '                result.update(disposition="would-delete", ended_at=_utc(self.now()))'))],
     "killer": f"{APPLY}::test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do"},
    {"id": 9, "name": "Let a normal pass retire a marked job", "file": "subfleet/retention.py",
     "edits": [("        for job_id, why in marks.kept_for_good(self.root, by_id).items():",
                "        for job_id, why in {}.items():")],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
    {"id": 10, "name": "Drop the age gate", "file": RD,
     "edits": [("            if days < request.min_age_days:", "            if False:")],
     "killer": f"{APPLY}::test_gates_one_to_four_keep_their_jobs_whole"},
    {"id": 11, "name": "Ignore --limit", "file": RD,
     "edits": [('        return record["mode"] == "apply" and record.get("limit") is not None \\',
                '        return False and record["mode"] == "apply" and record.get("limit") is not None \\')],
     "killer": f"{APPLY}::test_limit_ends_the_run_and_the_same_request_continues_it"},
    {"id": 12, "name": "Remove an admin directory whose gitdir points elsewhere", "file": RD,
     "edits": [("            if where is None or not _inside(where, original):", "            if where is None:")],
     "killer": f"{APPLY}::test_an_admin_directory_that_names_another_place_stays"},
    {"id": 13, "name": "Skip gate 11 (a registration whose working directory lies outside the tree)", "file": RD,
     "edits": [(lines("            if where is None or not _inside(where, str(tree)):",
                      '                raise _Gate("outside", f"{admin} registers {back}, outside the tree")'),
                lines("            if False:",
                      '                raise _Gate("outside", f"{admin} registers {back}, outside the tree")'))],
     "killer": f"{APPLY}::test_gates_five_to_twelve_keep_their_jobs_whole"},
    {"id": 14, "name": "Keep HEAD only, losing a commit only a reflog or a per-worktree ref names", "file": RD,
     "edits": [('    objects = rgit.admin_objects(admin, fmt, cancel=cancel)\n    commits = set(objects["commit"])',
                "    objects = rgit.admin_objects(admin, fmt, cancel=cancel)\n"
                '    commits = {rgit.run(["rev-parse", "--verify", "HEAD"], git_dir=admin, '
                "cancel=cancel).stdout.decode().strip()}")],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
    {"id": 15, "name": "Put the journal under <state>/retention/", "file": "subfleet/retention_marks.py",
     "edits": [('DELETE_DIR = "retention-delete"', 'DELETE_DIR = "retention"')],
     "killer": f"{APPLY}::test_the_journal_lives_under_retention_delete_never_under_retention"},
    # The defects the tests found, put back one at a time.
    {"id": 16, "name": "Finish a path lexically after a missing part (Excluder)", "file": RD,
     "edits": [(lines("            except FileNotFoundError:",
                      "                # As `os.path.realpath` does: a missing part is no link, and",
                      "                # what follows it is resolved part by part, so a `..` back out",
                      "                # of it and on through a link is still checked before it is read.",
                      "                current = candidate",
                      "                continue"),
                lines("            except FileNotFoundError:",
                      "                return os.path.normpath(os.path.join(candidate, *reversed(pending))), None"))],
     "killer": f"{REQUEST}::test_resolve_never_touches_an_excluded_place_and_agrees_with_realpath"},
    {"id": 17, "name": "Write the event with Store.add_event (two rows)", "file": RD,
     "edits": [("            self._record_event(job_id, marker)",
                "            self.store.add_event(EVENT, job_id=job_id, data=marker)")],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
    {"id": 18, "name": "Leave the ref namespace folder a rollback made", "file": RD,
     "edits": [("                    made += [str(f) for f in _ref_folders(common, ref) if str(f) not in made and "
                "not os.path.lexists(f)]",
                "                    pass")],
     "killer": f"{RESUME}::test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept[checked-1]"},
    {"id": 19, "name": "Label a deleted job by the budget (survey)", "file": "subfleet/retention_survey.py",
     "edits": [('        if job["job_id"] in kept:', "        if False:")],
     "killer": f"{APPLY}::test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left"},
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_lane(repo: Path, temp: Path, i: int) -> Path:
    lane = Path(tempfile.mkdtemp(prefix=f"sf-d2660-b1.mut.{i}.", dir=temp))
    archive = subprocess.run(["git", "archive", "HEAD", "subfleet", "tests", "pyproject.toml"], cwd=repo,
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(lane)], input=archive, check=True)
    return lane


def remove_lane(lane: Path) -> None:
    assert "sf-d2660-b1" in str(lane), lane
    shutil.rmtree(lane, ignore_errors=True)


def run_one(mutant: dict, lane: Path, python: str, timeout: float) -> dict:
    target = lane / mutant["file"]
    pristine = target.read_bytes()
    digest = sha(pristine)
    text = pristine.decode()
    out = {"id": mutant["id"], "name": mutant["name"], "file": mutant["file"], "killer": mutant["killer"]}
    for old, new in mutant["edits"]:
        count = text.count(old)
        if count != 1:
            out.update(verdict="error", why=f"an edit's old text occurs {count} times, not once: {old[:80]!r}")
            return out
        text = text.replace(old, new)
    assert text.encode() != pristine
    started = time.monotonic()
    basetemp = Path(tempfile.mkdtemp(prefix=f"bt-{mutant['id']}.", dir=lane))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        target.write_bytes(text.encode())
        try:
            done = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:xdist",
                                   "--basetemp", str(basetemp / "t"), mutant["killer"]],
                                  cwd=lane, env=env, capture_output=True, timeout=timeout)
            code, tail = done.returncode, (done.stdout + done.stderr).decode(errors="replace").splitlines()[-25:]
        except subprocess.TimeoutExpired:
            code, tail = None, [f"timed out after {timeout:.0f} s"]
    finally:
        target.write_bytes(pristine)
        assert sha(target.read_bytes()) == digest, f"{target} was not restored"
        for directory, dirnames, _ in os.walk(basetemp):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(directory, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(basetemp, ignore_errors=True)
    failed = [line for line in tail if line.startswith(("FAILED", "ERROR", "E   "))][:6]
    summary = next((line for line in reversed(tail) if " in " in line and ("passed" in line or "failed" in line
                                                                           or "error" in line)), None)
    out.update(exit=code, seconds=round(time.monotonic() - started, 1), restored_sha256=digest,
               verdict="killed" if code == 1 else "survived" if code == 0 else "error",
               summary=summary, evidence=failed)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lanes", type=int, default=4)
    parser.add_argument("--only", default="", help="comma-separated mutant ids")
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()
    if not 1 <= args.lanes <= 4:
        parser.error("--lanes: 1 to 4 (the host's local cap)")
    repo = Path(__file__).resolve().parents[3]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "subfleet", "tests"], cwd=repo, check=True,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print(f"refusing: subfleet/ or tests/ differ from HEAD, which is what the lanes copy:\n{dirty}")
        return 2
    wanted = {int(x) for x in args.only.split(",") if x.strip()} or {m["id"] for m in MUTANTS}
    todo = [m for m in MUTANTS if m["id"] in wanted]
    temp = Path(tempfile.gettempdir())
    lanes = [make_lane(repo, temp, i) for i in range(min(args.lanes, len(todo)))]
    free: queue.Queue[Path] = queue.Queue()
    for lane in lanes:
        free.put(lane)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def work(mutant: dict) -> dict:
        lane = free.get()
        try:
            result = run_one(mutant, lane, sys.executable, args.timeout)
        finally:
            free.put(lane)
        print(f"M{result['id']:>2} {result['verdict']:<8} {result.get('seconds', '-'):>7} s  {result['name']}",
              flush=True)
        return result

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            results = sorted(pool.map(work, todo), key=lambda r: r["id"])
    finally:
        for lane in lanes:
            remove_lane(lane)
    record = {"head": head, "started": started, "ended": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "lanes": len(lanes), "results": results}
    path = Path(__file__).with_name("mutants.json")
    if wanted != {m["id"] for m in MUTANTS} and path.exists():
        previous = json.loads(path.read_text())
        merged = {r["id"]: r for r in previous.get("results", [])}
        merged.update({r["id"]: r for r in results})
        record["results"] = [merged[k] for k in sorted(merged)]
        record["partial_runs"] = previous.get("partial_runs", []) + [{"head": head, "ids": sorted(wanted)}]
    path.write_text(json.dumps(record, indent=1) + "\n")
    killed = sum(r["verdict"] == "killed" for r in results)
    print(f"{killed}/{len(results)} killed at {head[:12]}; {path}")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
