"""Codex's repository check through the real CLI, daemon, guardian, and Codex adapter.

The fake provider refuses an `exec` launch without `--skip-git-repo-check`
whose cwd is outside a Git repository, as codex-cli 0.153.3 does (C-12.3,
C-12.8), so these cases fail wherever a launch omits the flag it needs.
"""

import json
from pathlib import Path
import re
import subprocess


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "success"
FLAG = "--skip-git-repo-check"


def job_id(result):
    value = result.stdout.strip()
    assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]+", value), result
    return value


def launch_argv(e2e, identity):
    artifacts = {artifact["role"]: artifact for artifact in e2e.show(identity)["artifacts"]}
    return json.loads(Path(artifacts["launch"]["path"]).read_text())["argv"]


def test_read_only_job_outside_a_repository_delivers(e2e):
    """C-6.7, C-12.3, C-12.6: a read-only Codex job rooted in a plain folder passes the flag once and delivers (incident: 2026-09-24)."""
    folder = e2e.root / "organisation"
    folder.mkdir()
    outside = subprocess.run(["git", "-C", str(folder), "rev-parse", "--git-dir"], env=e2e.env,
                             capture_output=True, text=True, timeout=10)
    assert outside.returncode != 0, "the research folder must not be inside a repository"
    out = folder / "answer.md"
    e2e.start()
    submitted = e2e.cli("run", "-m", "astra", "-s", "read-only", "-C", folder, "-p", e2e.prompt,
                        "--allow-tmp", "-o", out, "--wait")
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    assert e2e.job(identity)["state"] == "succeeded"
    attempt, = e2e.attempts(identity)
    assert attempt["outcome_class"] == "ok" and attempt["rc"] == 0
    assert out.read_bytes() == (FIXTURE / "last.md").read_bytes()
    argv = launch_argv(e2e, identity)
    assert argv.count(FLAG) == 1
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    diagnostics = json.loads((e2e.root / "codex-env.json").read_text())
    assert Path(diagnostics["cwd"]).resolve() == folder.resolve()
    assert diagnostics["argv"].count(FLAG) == 1


def test_writable_job_keeps_codex_repository_check_and_its_probe_passes(e2e):
    """C-6.6, C-11.4, C-12.3: a writable launch omits the flag and runs in its worktree; the read-only probe before it passes the flag and succeeds."""
    e2e.start()
    submitted = e2e.cli(*e2e.run_args("astra", "-s", "workspace-write", "--wait"))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    job = e2e.job(identity)
    assert job["state"] == "succeeded"
    assert Path(job["worktree"]) == e2e.root / "worktrees" / identity
    assert (Path(job["worktree"]) / ".git").is_file()   # a linked worktree, not a copy
    argv = launch_argv(e2e, identity)
    assert FLAG not in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    # C-11.4: writable work on an unmeasured lane is probed first, read-only, in a
    # private directory under the state root that is not a repository. Before
    # the fix every such probe on a Codex lane exited 1 and the job waited.
    probes = [json.loads(row["data_json"]) for row in e2e.rows(
        "SELECT data_json FROM events WHERE kind='probe.completed' AND lane_id='codex-1'")]
    assert probes and all(probe["class"] == "ok" for probe in probes), probes
    assert not e2e.rows("SELECT * FROM events WHERE kind='job.probe_waiting' AND job_id=?", (identity,))
