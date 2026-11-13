"""D-REVIEW-CONTRACT1: stored results survive online and offline ledger reads."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import cli
from subfleet.client import DaemonUnavailable
from subfleet.offline import Offline
from subfleet.review_record import stored_review_result
from subfleet.review_result import parse_review_result

JOB = "20261113-080000-review"
NOW = "2026-11-13T08:00:00Z"
SCHEMA = Path(__file__).resolve().parents[2] / "subfleet" / "store_schema.sql"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A private store, without creating the shared socket fixture's /tmp root."""
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path))

    def unavailable(args):
        raise DaemonUnavailable("test daemon unavailable")

    monkeypatch.setattr(cli, "_client", unavailable)
    conn = sqlite3.connect(tmp_path / "state.sqlite3")
    conn.executescript(SCHEMA.read_text())
    conn.execute(
        "INSERT INTO jobs (job_id,request_id,payload_digest,kind,state,task,tier,"
        "workdir,prompt_path,sandbox,created_at,finished_at,accepted_attempt_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (JOB, "request-review", "digest", "dispatch", "succeeded", "review", "standard",
         str(tmp_path), str(tmp_path / "prompt.md"), "read-only", NOW, NOW, f"{JOB}/a2"))
    texts = ["VERDICT: REQUEST_CHANGES\nP1: First attempt.\n",
             "VERDICT: APPROVE\nFINDINGS: none\n",
             "VERDICT: REQUEST_CHANGES\nP2: Latest attempt.\n"]
    results = [parse_review_result(text) for text in texts]
    for seq, result in enumerate(results, 1):
        conn.execute(
            "INSERT INTO attempts (attempt_id,job_id,seq,lane_id,model_requested,"
            "state,reserved_at,evidence_json) VALUES (?,?,?,?,?,?,?,?)",
            (f"{JOB}/a{seq}", JOB, seq, "test-lane", "test-model", "succeeded", NOW,
             json.dumps({"other_evidence": "kept", "review": result})))
    conn.commit()
    yield tmp_path, conn, results
    conn.close()


def test_offline_list_and_show_select_the_accepted_review_after_reopening(ledger):
    """I5/I6: reads use the accepted pointer, even between earlier/later attempts."""
    root, conn, results = ledger
    for reader in (Offline(root), Offline(root)):
        listed = reader.list_jobs()[0]
        shown = reader.show_job(JOB)
        assert listed["review"] == shown["review"] == results[1]
        assert "_review_evidence_json" not in listed
        assert "_review_evidence_json" not in shown
        assert shown["attempt_id"] == f"{JOB}/a3"  # Other ledger metadata stays latest.
    assert conn.execute("SELECT evidence_json FROM attempts WHERE seq=2").fetchone()[0] == json.dumps(
        {"other_evidence": "kept", "review": results[1]})


def test_offline_runs_json_and_show_json_carry_the_same_review(ledger, capsys):
    _, _, results = ledger
    assert cli.main(["runs", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert cli.main(["runs", "show", JOB, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert listed["review"] == shown["review"] == results[1]


@pytest.mark.parametrize("task,kind", [("review", "dispatch"), ("build", "gate-review")])
def test_offline_review_task_and_gate_review_kind_expose_the_saved_result(ledger, task, kind):
    root, conn, results = ledger
    conn.execute("UPDATE jobs SET task=?,kind=?", (task, kind))
    conn.commit()
    assert Offline(root).list_jobs()[0]["review"] == results[1]


@pytest.mark.parametrize("change", ["non-review", "not-finished", "not-accepted", "old-record", "bad-evidence"])
def test_offline_other_unfinished_and_older_jobs_have_null_review(ledger, change):
    root, conn, _ = ledger
    if change == "non-review":
        conn.execute("UPDATE jobs SET task='build'")
    elif change == "not-finished":
        conn.execute("UPDATE jobs SET state='running'")
    elif change == "not-accepted":
        conn.execute("UPDATE jobs SET accepted_attempt_id=NULL")
    else:
        conn.execute("UPDATE attempts SET evidence_json=? WHERE seq=2",
                     ("{}" if change == "old-record" else "broken json",))
    conn.commit()
    assert Offline(root).list_jobs()[0]["review"] is None
    assert Offline(root).show_job(JOB)["review"] is None


@pytest.mark.parametrize("evidence", [None, "{}", "null", "[]", "broken", '{"review":[]}'])
def test_stored_review_result_tolerates_old_or_malformed_evidence(evidence):
    job = {"task": "review", "state": "succeeded", "accepted_attempt_id": "a2"}
    assert stored_review_result(job, evidence) is None


def _online(monkeypatch, job):
    def call(op, args):
        return {"jobs": [job]} if op == "list" else job

    monkeypatch.setattr(cli, "_client", lambda args: SimpleNamespace(call=call))
    monkeypatch.setattr(cli, "_daemon_capabilities", lambda client: set())


@pytest.mark.parametrize("command", [["runs", "--json"], ["runs", "show", JOB, "--json"]])
def test_older_online_daemon_emits_null_without_reparsing_artifacts(monkeypatch, tmp_path, capsys, command):
    artifact = tmp_path / "deliverable.md"
    artifact.write_text("VERDICT: APPROVE\nFINDINGS: none\n")
    _online(monkeypatch, {"job_id": JOB, "task": "review", "state": "succeeded",
                          "accepted_attempt_id": f"{JOB}/a2", "out_path": str(artifact)})
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["review"] is None


def test_online_show_preserves_the_review_in_its_nested_job(monkeypatch, capsys):
    result = parse_review_result("VERDICT: APPROVE\nFINDINGS: none\n")
    _online(monkeypatch, {"job": {"job_id": JOB, "review": result}})
    assert cli.main(["runs", "show", JOB, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["review"] == shown["job"]["review"] == result


@pytest.mark.parametrize("text,expected", [
    ("VERDICT: REQUEST_CHANGES\nP1: Wrong.\nP2: Fragile.\nP2: Missing test.\n",
     "review: REQUEST_CHANGES (P1 1, P2 2); round valid"),
    ("VERDICT: APPROVE\nP1: Wrong.\n",
     "review: APPROVE (P1 1); round valid; verdict inconsistent with levels"),
    ("REQUEST CHANGES\n", "review: UNKNOWN (no findings); round invalid"),
])
def test_readable_show_has_one_review_line(monkeypatch, capsys, text, expected):
    review = parse_review_result(text)
    _online(monkeypatch, {"job_id": JOB, "state": "succeeded", "review": review})
    assert cli.main(["runs", "show", JOB]) == 0
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert lines[0].startswith(expected)
    if not review["valid"]:
        assert review["reason"] in lines[0]
    assert sum(line.startswith("review:") for line in lines) == 1
    metadata, result = captured.out.split("\n--- out.md ---\n")
    assert json.loads(metadata)["review"] == review
    assert result == ""


def test_show_out_streams_only_the_deliverable(monkeypatch, tmp_path, capsys):
    text = "VERDICT: APPROVE\nFINDINGS: none\n"
    path = tmp_path / "deliverable.md"
    path.write_text(text)
    _online(monkeypatch, {"job_id": JOB, "state": "succeeded", "review": parse_review_result(text),
                          "accepted_attempt_id": f"{JOB}/a2", "out_path": str(path)})
    assert cli.main(["runs", "show", JOB, "--out"]) == 0
    assert capsys.readouterr().out == text
