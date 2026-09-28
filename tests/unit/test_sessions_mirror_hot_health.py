"""C-23.28: hot holds are visible without advancing the full heartbeat."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx


def sidecar(tmp_path, *, full_state="ok", age_min=0, hot_state="ok", held=0):
    running = mirror.Mirror(tmp_path, fx.policy(), now=lambda: fx.NOW)
    running.dir.mkdir()
    started = fx.iso(fx.NOW - timedelta(minutes=age_min))
    data = {
        "pass": {"started_at": started, "finished_at": None if full_state == "running" else started,
                 "state": full_state, "flags_held": 0},
        "updated_at": started,
        "last_ok_at": started,
        "hot": {"kind": "hot", "state": hot_state, "started_at": fx.iso(fx.NOW),
                "finished_at": None if hot_state == "running" else fx.iso(fx.NOW),
                "stage": "reading entries" if hot_state == "running" else "complete",
                "flags_held": held,
                "held_by": [{"path": "account-b/org-b", "reason": "unreadable (EMFILE)"}] if held else [],
                "error": "store not listed: EMFILE" if hot_state == "error" else None},
    }
    running.sidecar_path.write_text(json.dumps(data))
    return running, data


@pytest.mark.parametrize("full_state,age_min,expected", [
    ("ok", 0, "healthy"), ("running", 5, "running"), ("running", 40, "stalled"),
])
def test_hot_holds_are_reported_independently_of_the_full_heartbeat(tmp_path, full_state, age_min, expected):
    """C-23.28: hot hold causes survive status rendering while a full pass runs."""
    running, data = sidecar(tmp_path, full_state=full_state, age_min=age_min, held=2)
    before = running.sidecar_path.read_bytes()
    health = running.health()
    assert health["status"] == expected
    assert health["hot"] == data["hot"]
    assert "hot flags held for 2 sessions" in health["detail"]
    assert "account-b/org-b: unreadable (EMFILE)" in health["detail"]
    if full_state == "ok":
        assert health["flags_held"] == 0, "hot counts must not impersonate the full pass"
    assert running.sidecar_path.read_bytes() == before


@pytest.mark.parametrize("hot_state,detail", [("running", "hot pass in flight"),
                                               ("error", "hot pass error: store not listed: EMFILE")])
def test_hot_progress_and_failure_leave_a_stalled_full_pass_stalled(tmp_path, hot_state, detail):
    """C-23.28: neither a hot start nor a hot error masks the full pass's age."""
    running, data = sidecar(tmp_path, full_state="running", age_min=40, hot_state=hot_state)
    health = running.health()
    assert health["status"] == "stalled" and health["run_min"] == 40
    assert health["hot"] == data["hot"]
    assert detail in health["detail"]


def test_cli_status_reports_hot_holds_in_text_and_json(tmp_path, monkeypatch, capsys):
    """C-23.28: the command names a hot hold and exposes its independent record."""
    from argparse import Namespace
    from subfleet.sessions import cli

    running, data = sidecar(tmp_path, held=1)
    monkeypatch.setattr(cli, "_policy", lambda _args: fx.policy())
    monkeypatch.setattr(cli, "_cli", lambda: Namespace(_root=lambda _args: tmp_path))
    monkeypatch.setattr(cli.mirror_module, "Mirror", lambda *_args, **_kwargs: running)
    monkeypatch.setattr(running, "load_gap", lambda: {"status": "unknown", "detail": "test log unavailable"})
    args = Namespace(status=True, json=False)
    assert cli.cmd_mirror(args) == 0
    text = capsys.readouterr().out
    assert "hot flags held for 1 session" in text
    assert "account-b/org-b: unreadable (EMFILE)" in text
    args.json = True
    assert cli.cmd_mirror(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["hot"] == data["hot"]
    assert report["flags_held"] == 0
