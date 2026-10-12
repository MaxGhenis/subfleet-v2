"""Reviewer probes; deliberately outside the PR's committed test modules."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from tests.unit.test_conversation_service import conversation, svc, write_catalog
from tests.unit.test_suggestions import people
from tests.unit.test_conversation_archive import archive, row
from tests.frontend.conftest import core_probe
from tests.frontend.test_core_triage import triage, watch_from
from tests.frontend import swift
from tests.unit.test_conversation_handoff import world, source, turn_job, handoff
from subfleet.conversations.store import ConversationError


def test_hidden_native_open_keeps_archive_and_history(people):
    session = str(uuid.uuid4())
    cid = conversation(people, origin="native", native_session_id=session)
    write_catalog(people.root, "2026-10-11T12:00:00.000Z", [
        {"provider": "claude", "native_session_id": session,
         "cwd": people.test_workspace, "mtime": 2.0}])
    archive(people, cid)
    before = row(people, cid)
    opened = people.handle("conversation.open", {
        "native": {"provider": "claude", "session_id": session}}, None)
    assert opened["conversation"]["conversation_id"] == cid
    assert opened["conversation"]["archived_at"] == before["archived_at"]
    assert row(people, cid) == before
    listed = people.handle("conversation.list", {}, None)
    assert listed["conversations"] == [] and listed["catalog"]["items"] == []


def test_compiler_timeout_only_signals_its_new_process_group(tmp_path, monkeypatch):
    real_popen, real_killpg = subprocess.Popen, os.killpg
    outsider = real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          start_new_session=True)
    child_file = tmp_path / "child.pid"
    source = tmp_path / "Probe.swift"
    source.write_text("// Reviewer probe\n")
    compiler_code = (
        "import subprocess, sys, time, pathlib; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_file)!r}).write_text(str(child.pid)); "
        "print('ready', flush=True); time.sleep(60)"
    )
    owned = []
    signals = []

    def compiler_popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        compiler = real_popen([sys.executable, "-c", compiler_code], **kwargs)
        owned.append(compiler)
        deadline = time.monotonic() + 10
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_file.exists()
        return compiler

    def killpg(group, sig):
        assert group == owned[0].pid == os.getpgid(owned[0].pid)
        assert os.getsid(owned[0].pid) == group
        assert os.getpgid(int(child_file.read_text())) == group
        assert os.getpgid(outsider.pid) != group
        signals.append((group, sig))
        return real_killpg(group, sig)

    try:
        monkeypatch.setattr(swift, "ROOT", tmp_path)
        monkeypatch.setattr(swift, "app_sources", lambda: [source])
        monkeypatch.setattr(subprocess, "Popen", compiler_popen)
        monkeypatch.setattr(os, "killpg", killpg)
        with pytest.raises(subprocess.TimeoutExpired):
            swift.compile_probe(tmp_path / "binary", source, "SUBFLEET_MODEL_TEST", timeout=0.05)
        assert signals == [(owned[0].pid, signal.SIGKILL)]
        assert owned[0].returncode == -signal.SIGKILL
        assert outsider.poll() is None
    finally:
        for compiler in owned:
            if compiler.poll() is None:
                real_killpg(compiler.pid, signal.SIGKILL)
                compiler.communicate()
        outsider.kill()
        outsider.wait()


def test_remote_archive_watch_updates_existing_conversation(core_probe, tmp_path, people):
    cid = conversation(people)
    listing = people.handle("conversation.list", {"include_catalog": False}, None)
    start = people.store.changes_after(0)["next"]
    archive(people, cid)
    page = people.store.changes_after(start)
    refreshed = people.handle("conversation.list", {"include_catalog": False}, None)
    result = triage(core_probe, tmp_path, [
        {"list": listing}, {"focus": cid}, {"watch": page},
        {"list": refreshed}, {"snapshot": "after_archive"}])
    entries = [e for section in result["snapshots"]["after_archive"]["sections"]
               for e in section["entries"]]
    assert not entries, json.dumps(result)


def test_handoff_retry_cannot_requeue_a_message_in_an_archived_source(world, monkeypatch):
    # A cancelled source from an older unfenced handoff: the recovery supported
    # explicitly by op_conversation_handoff and the existing handoff race tests.
    cid, (mid,) = source(world, "waiting message")
    job_id = turn_job(world, mid, cid, state="waiting")
    world.store.set_state(mid, "waiting", reason="admission: not yet placed", job_id=job_id)
    assert world.service._cancel_job_without_attempt(
        job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
    world.service._settle_unstarted()
    assert world.store.message(mid)["state"] == "cancelled"
    monkeypatch.setattr(world.service, "_person", lambda *args: None)
    world.service.handle("conversation.archive", {"conversation_id": cid}, None)
    # Keep recovery observable by interrupting the retry before it moves the
    # restored source into the newly created target conversation.
    def fail_history(*args, **kwargs):
        raise OSError("review: history temporarily unavailable")
    monkeypatch.setattr(world.service, "_handoff_history", fail_history)
    with pytest.raises((OSError, ConversationError)):
        handoff(world, cid)
    stored = world.store.conversation(cid)
    message = world.store.message(mid)
    assert not (stored["archived_at"] and message["state"] == "queued"), (stored, message)
