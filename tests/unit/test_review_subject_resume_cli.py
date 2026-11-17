"""Review continuations retain the original subject and round at the CLI boundary."""

from subfleet import cli


def test_resume_forwards_the_source_review_subject_and_round(daemon, root, workdir):
    source_id = "20261113-120000-synthetic-review"
    source = {"job_id": source_id, "state": "failed", "task": "review", "tier": "standard",
              "review_subject": "synthetic-pr", "review_round": 3, "workdir": str(workdir),
              "lane_id": "codex-1"}
    server = daemon({"show": lambda request: {"job": source},
                     "submit": lambda request: {"job_id": "20261113-120001-synthetic-resume",
                                                "created": True, "state": "queued"},
                     "capabilities": lambda request: {"capabilities": ["review.subject.v1"]}})
    assert cli.main(["resume", source_id]) == 0
    sent = server.args("submit")
    assert sent["review_subject"] == "synthetic-pr" and sent["review_round"] == 3
