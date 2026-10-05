"""C-23.28: inventory syscall savings preserve cache and regular-file checks."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from subfleet.sessions import mirror


def record(folder: Path, title: str = "before") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "local_one.json"
    path.write_text(json.dumps({"cliSessionId": "one", "title": title}))
    return path


def path_stats(monkeypatch, target):
    calls = []
    original = os.stat

    def counted(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counted)
    return calls


def test_cold_entry_reuses_descriptor_stat_and_only_stats_the_path_after_read(tmp_path, monkeypatch):
    """C-23.28: a cold read needs one path stat, which still rejects app races."""
    path = record(tmp_path / "store")
    running = mirror.Mirror(tmp_path / "state")
    calls = path_stats(monkeypatch, path)
    assert running._entry(path)["title"] == "before"
    assert len(calls) == 1
    assert os.fspath(path) in running._entries


def test_warm_sweep_uses_the_direntry_stat(tmp_path, monkeypatch):
    """C-23.28: the sweep uses scandir's metadata API for a cached entry."""
    path = record(tmp_path / "store")
    running = mirror.Mirror(tmp_path / "state")
    running._scan(path.parent, mirror.Pass("start", kind="hot"), sweep=True)
    calls = path_stats(monkeypatch, path)
    files, fresh = running._scan(path.parent, mirror.Pass("start", kind="hot"), sweep=True)
    assert files[path.name]["title"] == "before" and not fresh
    assert calls == []


@pytest.mark.parametrize("replace", [False, True], ids=["in-place", "rename"])
def test_changed_during_read_is_not_cached(tmp_path, monkeypatch, replace):
    """C-23.28: a read raced by an app save never seeds an unchanged-folder hit."""
    path = record(tmp_path / "store")
    running = mirror.Mirror(tmp_path / "state")
    original = mirror._read_entry

    def raced(target):
        raw = original(target)
        destination = target.with_suffix(".replacement") if replace else target
        destination.write_text(json.dumps({"cliSessionId": "one", "title": "after"}))
        if replace:
            destination.replace(target)
        return raw

    monkeypatch.setattr(mirror, "_read_entry", raced)
    assert running._entry(path)["title"] == "before"
    assert os.fspath(path) not in running._entries
    monkeypatch.setattr(mirror, "_read_entry", original)
    assert running._entry(path)["title"] == "after"


def test_entry_reads_every_chunk(tmp_path):
    """C-23.28: batching reads never truncates a larger app record."""
    path = record(tmp_path / "store", title="large " * 40_000)
    running = mirror.Mirror(tmp_path / "state")
    assert running._entry(path)["title"] == "large " * 40_000


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink-to-fifo"])
def test_cold_entry_rejects_nonregular_files_without_waiting(tmp_path, kind):
    """C-23.28: skipping the preliminary stat cannot open a FIFO for rendezvous."""
    path = tmp_path / "local_one.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "pipe"
        os.mkfifo(target)
        path.symlink_to(target)
    running = mirror.Mirror(tmp_path / "state")
    result = []
    worker = threading.Thread(target=lambda: result.append(running._entry(path)), daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "a nonregular file must not hold the reader"
    assert result == [{}]
    assert not running._unread and not running._entries
