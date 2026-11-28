"""Adding review labels preserves the existing readable ledger columns."""

from subfleet import cli


def test_nonreview_runs_preserve_the_existing_workdir_column():
    text = cli.format_runs([{"job_id": "synthetic-build", "task": "build", "name": "build-name",
                             "state": "succeeded", "rc": 0, "workdir": "/synthetic/workdir"}])
    header, row = text.splitlines()
    assert header.split() == ["id", "family", "model", "lane", "rc", "bytes", "seconds", "notice", "caller", "workdir"]
    assert row.split()[-1] == "/synthetic/workdir"
    assert "build-name" not in row
