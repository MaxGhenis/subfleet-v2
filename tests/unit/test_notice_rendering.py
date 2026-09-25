"""One terminal state, one rendering: the notice header and what the CLI points at.

C-15.1 (incident: 2026-09-24): a cancelled job's notice said `ok; rc=0` with
its `-o` path while the job row said `cancelled`, rc 130, and nothing had been
exported; the hook fallback said `cancelled; rc=130`, and `wait` pointed at
the `-o` file. These tests pin the shared header renderer, the hook fallback
that uses it, and the CLI presentations that name a job's result.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from subfleet import cli, hooks, render
from subfleet.contracts import Exit

JOB = "20260924-115800-fv3-s2-axiom-encoding"
ROOT = Path("/state")
OUT = "/repo/review.md"


def row(state: str, rc: int | None, **extra) -> dict:
    return {"job_id": JOB, "state": state, "rc": rc, "out_path": OUT,
            "accepted_attempt_id": None, **extra}


# --- render.notice_header ------------------------------------------------------

def test_c15_1_an_accepted_job_names_its_deliverable_and_out():
    """C-8.2, C-8.3, C-15.1 the accepted attempt's deliverable.md and the `-o` export."""
    header = render.notice_header(row("succeeded", 0, accepted_attempt_id=f"{JOB}/a2"), ROOT)
    assert header == (f"{JOB}: succeeded; rc=0; "
                      f"deliverable=/state/jobs/{JOB}/a2/deliverable.md; out={OUT}")


def test_c15_1_an_accepted_job_without_an_o_path_prints_a_dash_for_it():
    """C-15.1 `-o` is optional; its absence is `-`, never `None`."""
    header = render.notice_header(row("succeeded", 0, out_path=None,
                                      accepted_attempt_id=f"{JOB}/a1"), ROOT)
    assert header.endswith(f"deliverable=/state/jobs/{JOB}/a1/deliverable.md; out=-")


@pytest.mark.parametrize("state,rc", [("cancelled", 130), ("failed", 1), ("failed", 7),
                                      ("lost", 125), ("failed", 4)])
def test_c15_1_a_job_that_accepted_nothing_names_no_file(state, rc):
    """C-7.2, C-8.3, C-15.1 the job's state and rc, and `-` for both paths, even with an
    `-o` path on the row: nothing was exported there."""
    assert render.notice_header(row(state, rc), ROOT) == (
        f"{JOB}: {state}; rc={rc}; deliverable=-; out=-")


def test_c15_1_a_missing_rc_is_a_dash_not_python_none():
    """C-15.1 the header is text for a session, not a repr."""
    assert render.notice_header(row("lost", None), ROOT) == f"{JOB}: lost; rc=-; deliverable=-; out=-"


def test_c15_1_success_is_the_accepted_attempt_not_the_state_word_alone():
    """C-4.3, C-15.1 paths need the accepted attempt: a row without one names no file."""
    assert render.notice_header(row("succeeded", 0), ROOT) == (
        f"{JOB}: succeeded; rc=0; deliverable=-; out=-")


def test_c15_1_an_accepted_attempt_on_a_job_that_did_not_succeed_names_no_file():
    """C-15.1 only `succeeded` with an accepted attempt is a result."""
    assert render.notice_header(row("cancelled", 130, accepted_attempt_id=f"{JOB}/a1"), ROOT) == (
        f"{JOB}: cancelled; rc=130; deliverable=-; out=-")


# --- the PostToolUse fallback (C-15.2 layer 2) ---------------------------------

@pytest.mark.parametrize("job", [
    row("cancelled", 130),
    row("succeeded", 0, accepted_attempt_id=f"{JOB}/a1"),
    row("failed", 5),
])
def test_c15_1_the_hook_fallback_header_is_the_notice_header(job, tmp_path):
    """C-15.1, C-15.2 the hook's line for a job with no notice row is the daemon's header
    over the same row, then the pointer to `runs show`."""
    header, pointer = hooks.job_summary(job, tmp_path).splitlines()
    assert header == render.notice_header(job, tmp_path.resolve())
    assert pointer == f"no notice row for this job — subfleet runs show {JOB}"


def test_c15_1_the_hook_fallback_never_offers_a_cancelled_jobs_o_path(daemon, root):
    """C-15.1 incident 2026-09-24: the fallback said `cancelled; rc=130`; it must not also
    point at an `-o` file the cancelled job never wrote."""
    daemon({"list": lambda request: {"jobs": [{"job_id": JOB, "request_id": "req-1",
                                                "state": "running", "rc": None}]},
            "wait": lambda request: {"jobs": [row("cancelled", 130)]},
            "notice.pending": lambda request: {"notices": []}})
    stderr = io.StringIO()
    clock = [1000.0]                         # a clock the sleeps advance; no real seconds
    assert hooks.post_tool_use(
        {"session_id": "sess-hook", "hook_event_name": "PostToolUse", "cwd": "/repo",
         "transcript_path": "/dev/null", "tool_name": "Bash",
         "tool_input": {"command": "subfleet run -p p.md"}, "tool_response": JOB},
        root, budget_s=30, stderr=stderr, now=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + max(0.0, seconds))) == 2
    assert stderr.getvalue().splitlines()[0] == f"{JOB}: cancelled; rc=130; deliverable=-; out=-"
    assert OUT not in stderr.getvalue()


# --- the CLI's pointers to a result --------------------------------------------

def test_c15_1_wait_points_a_cancelled_job_at_nothing():
    """C-8.3, C-15.1 `wait` names the `-o` file of an accepted job only."""
    summary = cli._wait_summary({**row("cancelled", 130), "attempt": {
        "outcome_class": "unknown", "lane_id": "codex-3",
        "outcome_detail": "stopped by operator: exit 0 after the daemon's signal "
                          "is not a finished deliverable"},
        "artifacts": [{"role": "deliverable", "path": f"/state/jobs/{JOB}/a1/deliverable.md"}]})
    assert "CANCELLED" in summary and summary.endswith("out=-")
    assert OUT not in summary and "deliverable.md" not in summary


def test_c15_1_wait_still_points_an_accepted_job_at_its_o_file():
    """C-17.4 unchanged for the job that has a result."""
    summary = cli._wait_summary(row("succeeded", 0, accepted_attempt_id=f"{JOB}/a1"))
    assert summary.endswith(f"out={OUT}")


def test_c15_1_a_failed_jobs_wait_line_keeps_its_detail_and_names_no_file():
    """C-15.1, C-17.4 a failed job keeps its attempt's detail; its `-o` path is not a result."""
    summary = cli._wait_summary({**row("failed", 1), "attempt": {"outcome_detail": "fixture failure"}})
    assert "FAILED rc=1" in summary and "out=- · fixture failure" in summary


@pytest.mark.parametrize("envelope", [False, True])
def test_c8_3_runs_show_out_never_prints_the_o_file_of_a_job_that_accepted_nothing(
        daemon, root, capsys, envelope):
    """C-8.3, C-15.1 `runs show --out` of a cancelled job with no deliverable recorded
    reports none; a file at its `-o` path is not its output. The flat row is the
    offline store's shape (C-17.5), the envelope the daemon's."""
    stale = root / "review.md"
    stale.write_text("an earlier run's review\n")
    job = {**row("cancelled", 130), "out_path": str(stale)}
    shown = ({"job": job, "artifacts": [], "attempts": [], "notices": []} if envelope
             else {**job, "artifacts": [], "attempts": [], "notices": []})
    daemon({"show": lambda request: shown})
    assert cli.main(["runs", "show", JOB, "--out"]) == int(Exit.OPERATIONAL)
    captured = capsys.readouterr()
    assert "an earlier run's review" not in captured.out
    assert "no deliverable recorded" in captured.err


def test_c8_3_runs_show_out_prints_an_accepted_jobs_export(daemon, root, capsys):
    """C-8.3, C-17.4 an accepted job's `-o` export still stands in for a missing artifact row."""
    export = root / "review.md"
    export.write_text("the accepted review\n")
    daemon({"show": lambda request: {**row("succeeded", 0, accepted_attempt_id=f"{JOB}/a1"),
                                     "out_path": str(export)}})
    assert cli.main(["runs", "show", JOB, "--out"]) == 0
    assert capsys.readouterr().out == "the accepted review\n"
