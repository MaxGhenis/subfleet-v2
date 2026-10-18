"""C-23.28: unchanged mirror state does not cause a write every hot tick."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from subfleet.sessions import desktop, mirror
from tests import sessions_fixtures as fx


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    log = tmp_path / "main.log"
    log.write_text("")
    monkeypatch.setenv(desktop.LOG_ENV, str(log))
    fx.transcript(home, "session-one", fx.completed())
    paths = [fx.index_entry(store, account, "org", "session-one",
                            settings={"ultracode": True})
             for account in ("account-a", "account-b")]
    clock = [1000.0]
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock[0])
    running = mirror.Mirror(tmp_path / "state", fx.policy(),
                            now=lambda: fx.NOW + timedelta(seconds=clock[0] - 1000))
    assert running.run_once().state == "ok"
    assert running.run_hot().state == "ok"
    return running, paths, clock


def rewrite(path, **changes):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**json.loads(path.read_text()), **changes}))
    temporary.replace(path)


@pytest.mark.parametrize("pass_kind", ["full", "hot"])
def test_focus_saves_do_not_rewrite_an_unchanged_base(world, monkeypatch, pass_kind):
    """C-23.28: a non-flag app save can require inventory work without a new
    flags base; neither kind of pass should replace or fsync that same base.
    """
    running, paths, _clock = world
    before = running.flags_path.read_bytes()
    written = []
    original_write = mirror._write_json

    def counted(path, *args, **kwargs):
        if path == running.flags_path:
            written.append(path)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(mirror, "_write_json", counted)
    run = running.run_once if pass_kind == "full" else running.run_hot
    for focus in range(5):
        rewrite(paths[0], lastFocusedAt=focus)
        result = run()
        assert result.state == "ok", result.error
        assert result.sessions == 1 and not result.changed
    assert written == []
    assert running.flags_path.read_bytes() == before


def test_idle_hot_sidecar_is_throttled_but_changed_work_is_recorded(world, monkeypatch):
    """C-23.28: idle two-second ticks share a 60-second sidecar sample;
    a real flag change is recorded immediately with a completed hot record.
    """
    running, paths, clock = world
    before = running.sidecar_path.read_bytes()
    written = []
    original_write = mirror._write_json

    def counted(path, value, *args, **kwargs):
        if path == running.sidecar_path:
            written.append(value["hot"])
        return original_write(path, value, *args, **kwargs)

    monkeypatch.setattr(mirror, "_write_json", counted)
    for _ in range(10):
        clock[0] += 2
        result = running.run_hot()
        assert result.state == "ok" and not result.changed
    assert written == []
    assert running.sidecar_path.read_bytes() == before

    clock[0] = 1061.0
    assert running.run_hot().state == "ok"
    assert 1 <= len(written) <= 2
    assert written[-1]["state"] == "ok" and written[-1]["finished_at"]
    written.clear()
    clock[0] += 2
    assert running.run_hot().state == "ok"
    assert written == []

    rewrite(paths[0], isArchived=True)
    clock[0] += 2
    changed = running.run_hot()
    assert changed.state == "ok" and changed.flag_synced == 1
    assert all(json.loads(path.read_text())["isArchived"] for path in paths)
    assert written[-1]["state"] == "ok"
    assert written[-1]["flag_synced"] == 1
    assert written[-1]["finished_at"] == changed.finished_at
