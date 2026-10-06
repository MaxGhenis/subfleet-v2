"""C-12.10/C-17.8: provider measurements survive the shared acceptance path."""

from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from subfleet import cli, daemon as daemon_module
from subfleet.contracts import attempt_dir
from subfleet.usage import parse_usage
from test_daemon_settle import (ATTEMPT, EMPTY, JOB, StubAdapter, attempt,
                               daemon, publish_receipt, with_launch)


LINE = json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}})


@pytest.mark.parametrize("kind", ["run", "turn"])
@pytest.mark.parametrize("reported", [False, True])
def test_c12_10_shared_finalization_stores_only_reported_usage(daemon, monkeypatch, kind, reported):
    """C-12.10: detached and turn attempts store measurements beside existing evidence."""
    with_launch(daemon, monkeypatch)
    daemon._contain = lambda a: EMPTY
    publish_receipt(daemon)
    measured = parse_usage([LINE], "codex") if reported else None

    class MeasuredAdapter(StubAdapter):
        def classify(self, adir, launch, exit_info):
            return replace(super().classify(adir, launch, exit_info), usage=measured)

    monkeypatch.setattr(daemon_module, "get_adapter", lambda provider: MeasuredAdapter())
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps({"kept": "yes"}))
    if kind == "turn":
        daemon.store.conn.execute("UPDATE jobs SET kind='turn' WHERE job_id=?", (JOB,))
        daemon.conversations = SimpleNamespace(adapter=lambda provider: MeasuredAdapter(),
                                               release_socket=lambda aid: None)
        daemon._turn_trees = lambda job, a: {}
    daemon._finalize(attempt(daemon))
    evidence = json.loads(attempt(daemon)["evidence_json"])
    assert evidence["kept"] == "yes"
    assert ("usage" in evidence) is reported
    if reported:
        assert evidence["usage"] == measured
        assert evidence["usage"]["normalized"]["cache_write"] is None
    assert attempt(daemon)["state"] == "succeeded"


@pytest.mark.parametrize("lost", [False, True])
def test_c12_10_finalization_reads_usage_even_without_a_new_receipt(daemon, monkeypatch, lost):
    """C-12.10: lost guardians and old receipts cannot hide retained provider measurements."""
    with_launch(daemon, monkeypatch)
    daemon._contain = lambda a: EMPTY
    adir = attempt_dir(daemon.root, JOB, 1)
    (adir / "stdout").write_text(LINE + "\n")
    if not lost:
        publish_receipt(daemon)
    daemon._finalize(attempt(daemon))
    evidence = json.loads(attempt(daemon)["evidence_json"])
    assert evidence["usage"] == parse_usage([LINE], "codex")
    assert evidence["usage"]["normalized"]["prompt"] == 100


def test_c17_8_daemon_usage_dispatch_is_read_only(daemon, monkeypatch):
    """C-17.8: the socket operation delegates to the report without writing evidence."""
    from subfleet import usage_report
    seen = []
    monkeypatch.setattr(usage_report, "build_report", lambda store, root, **kwargs:
                        seen.append((store, root, kwargs)) or {"rows": []})
    before = daemon.store.conn.total_changes
    assert daemon.dispatch("usage", {"since": "7d", "by": "session", "backfill": True}) == {"rows": []}
    assert seen == [(daemon.store, daemon.root, {"since": "7d", "by": "session", "backfill": True})]
    assert daemon.store.conn.total_changes == before


def test_c12_10_show_exposes_usage_in_human_and_json(monkeypatch, capsys):
    """C-12.10: each shown attempt carries raw and normalized usage with missing fields null."""
    measured = parse_usage([LINE], "codex")
    row = {"job": {"job_id": JOB, "state": "failed"}, "artifacts": [],
           "attempts": [{"seq": 1, "evidence_json": json.dumps({"usage": measured})},
                        {"seq": 2, "evidence_json": "{}"}]}
    monkeypatch.setattr(cli, "_client", lambda args: SimpleNamespace(call=lambda op, args: row))
    monkeypatch.setattr(cli, "_ack_notices", lambda client, job: None)
    assert cli.main(["runs", "show", JOB, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["attempts"][0]["usage"] == measured
    assert "usage" not in shown["attempts"][1]
    assert cli.main(["runs", "show", JOB]) == 0
    shown = capsys.readouterr().out
    assert "attempt a1 usage" in shown
    assert "cache_write=null" in shown
    assert "cache_hit_share=60%" in shown
    assert "attempt a2 usage" not in shown
