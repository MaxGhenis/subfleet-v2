"""The 2026-09-24 cancel, end to end: real CLI, daemon, guardian and Codex adapter.

Three running Codex jobs were cancelled; Codex exited 0 after the daemon's
SIGTERM and left its last interim progress message in `last.md`. The adapter
classified each attempt `ok`, and the notice, written from the attempt, told
the session `ok; rc=0` with a deliverable and an `-o` path, while the job was
`cancelled` with rc 130 and nothing had been exported (C-9.2, C-15.1).

The `sigterm-exit-0` scenario of `tests/bin/codex` behaves as Codex did. The
workdir is the harness's committed git repository, so the test does not depend
on how a read-only Codex launch outside a repository is treated.
"""

import json
import re


def job_id(result):
    value = result.stdout.strip()
    assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]+", value), result
    return value


def test_c9_2_c15_1_a_codex_job_cancelled_while_running_is_announced_cancelled(e2e):
    """C-5.6, C-7.2, C-8.3, C-9.2, C-15.1, C-17.3: the attempt that exited 0 on the
    daemon's SIGTERM is `unknown`, the job `cancelled` with rc 130, no `-o` file and no
    export, and the notice header says `cancelled; rc=130` with no paths."""
    e2e.start(scenario="sigterm-exit-0")
    submitted = e2e.cli(*e2e.run_args("astra", "-o", e2e.out))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    e2e.until(lambda: (attempts := e2e.attempts(identity)) and attempts[0]["state"] == "running")
    # The fake prints `thread.started` once its SIGTERM handler is installed.
    stdout = e2e.root / "jobs" / identity / "a1" / "stdout"
    e2e.until(lambda: stdout.is_file() and b"thread.started" in stdout.read_bytes())
    killed = e2e.cli("kill", identity, "--wait", timeout=25)
    assert killed.rc == 130, killed

    job = e2e.job(identity)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("cancelled", 130, None)
    attempt, = e2e.attempts(identity)
    assert (attempt["state"], attempt["rc"], attempt["killed_by"]) == ("interrupted", 0, "operator")
    assert attempt["outcome_class"] == "unknown"
    assert attempt["outcome_detail"].startswith("stopped by operator: exit 0 after the daemon's signal")
    verdict = json.loads(attempt["evidence_json"])["provider_verdict"]
    assert verdict == {"class": "ok", "detail": "Codex completed with a deliverable",
                       "killed_by": "operator"}

    assert not e2e.out.exists()
    assert not e2e.rows("SELECT 1 FROM events WHERE kind='job.exported' AND job_id=?", (identity,))
    notice, = e2e.rows("SELECT * FROM notices WHERE job_id=?", (identity,))
    header, summary, kept = notice["text"].splitlines()
    assert header == f"{identity}: cancelled; rc=130; deliverable=-; out=-"
    assert summary.startswith("attempt a1: unknown, rc=0: stopped by operator")
    assert kept.startswith("output kept, not accepted: ") and kept.endswith(
        f"; -o {e2e.out} was not written")
    deliverable = next(row for row in e2e.show(identity)["artifacts"] if row["role"] == "deliverable")
    assert "before finalizing" in open(deliverable["path"]).read()

    waited = e2e.cli("wait", identity)
    assert waited.rc == 130, waited
    assert "CANCELLED" in waited.stderr and "out=-" in waited.stderr
    assert str(e2e.out) not in waited.stderr
