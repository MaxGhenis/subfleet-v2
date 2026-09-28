"""C-23.28: non-regular reads, listing invalidation, and embedded hot interleavings.

The interleaving model adopts the review's stress_interleave.py invariants:
only folder A changes user intent; other app saves preserve flags. A mirror
write must never change A's intent, and after writes and failures stop every
copy and the merge base must converge to it. A virtual clock schedules real
embedded services, including during inventory refresh, without timing flakes.
"""

from __future__ import annotations

import errno
import json
import os
import random
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet.sessions import desktop, mirror, transcripts
from tests import sessions_fixtures as fx

FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"),
           ("acct-d", "org-d"))
SESSIONS = ("session-000", "session-001", "session-002")
FIELDS = ("isArchived", "isStarred", "title")


def read(path):
    return json.loads(path.read_text())


def rewrite(path, data):
    temporary = path.with_suffix(".app")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


@pytest.mark.parametrize("kind", ["fifo", "directory", "zero-device"])
def test_entry_reader_rejects_nonregular_descriptors_before_any_read(tmp_path, monkeypatch, kind):
    """Even /dev/zero must be rejected before read, without risking an infinite read."""
    path = tmp_path / "local_nonregular.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to("/dev/zero")
    opened = []
    original_open = os.open

    def remember_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def forbidden_read(*args):
        pytest.fail("_read_entry read a non-regular descriptor before rejecting it")

    monkeypatch.setattr(mirror.os, "open", remember_open)
    monkeypatch.setattr(mirror.os, "read", forbidden_read)
    with pytest.raises(transcripts.NotRegularFile):
        mirror._read_entry(path)
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_hot_invalidation_during_a_listing_survives_until_the_next_scan(tmp_path, monkeypatch):
    """Discard the old dirty mark before scandir, preserving a service's new one."""
    folder = tmp_path / "store"
    folder.mkdir()
    (folder / "local_one.json").write_text(json.dumps({"cliSessionId": "one"}))
    running = mirror.Mirror(tmp_path / "state", fx.policy(), now=lambda: fx.NOW)
    current = mirror.Pass(started_at=fx.NOW.isoformat())
    running._scan(folder, current, sweep=True)
    worker = running._fork_hot()
    running._dirty.add(folder)
    scandir = mirror.os.scandir
    during_listing = False
    services = []

    @contextmanager
    def listing(path):
        nonlocal during_listing
        with scandir(path) as entries:
            during_listing = True
            try:
                yield entries
            finally:
                during_listing = False

    def checkpoint(current, stage=None):
        if during_listing and not services:
            assert folder not in running._dirty, "the previous invalidation was not consumed"
            worker._invalidate_folder(folder)
            services.append("hot invalidation")

    monkeypatch.setattr(mirror.os, "scandir", listing)
    monkeypatch.setattr(running, "_checkpoint", checkpoint)
    running._scan(folder, current, sweep=False)
    assert services
    assert folder in running._dirty, "the listing discarded the hot service's invalidation"
    files, _fresh = running._scan(folder, current, sweep=False)
    assert files is not None, "the next scan reused a listing invalidated by hot service"
    assert folder not in running._dirty


class InterleavedMirror:
    """Independent user-intent oracle for several sequential flag transactions."""

    def __init__(self, tmp_path, monkeypatch, seed, failures):
        self.rng = random.Random(seed)
        self.failures = failures
        self.failed_reads = self.failed_listings = self.services = self.actions = 0
        self.active = False
        self.clock = 0.0
        self.home = fx.claude_home(tmp_path, monkeypatch)
        self.store = fx.desktop_store(tmp_path, monkeypatch)
        self.root = tmp_path / "state"
        self.root.mkdir()
        log = tmp_path / "main.log"
        log.write_text("")
        monkeypatch.setenv(desktop.LOG_ENV, str(log))
        for identity in SESSIONS:
            fx.transcript(self.home, identity, fx.completed())
            for account, org in FOLDERS:
                fx.index_entry(self.store, account, org, identity,
                               settings={"ultracode": True})
        self.intent = {identity: {key: read(self.path(0, identity))[key] for key in FIELDS}
                       for identity in SESSIONS}
        self.running = self.engine()

    def engine(self):
        return mirror.Mirror(self.root, fx.policy(mirror_hot_interval_s=2),
                             now=lambda: fx.NOW + timedelta(seconds=self.clock))

    def path(self, folder, identity):
        return self.store.joinpath(*FOLDERS[folder], f"local_{identity}.json")

    def act(self):
        if not self.active:
            return
        if self.rng.random() < 0.08:
            identity = self.rng.choice(SESSIONS)
            key = self.rng.choice(FIELDS)
            body = read(self.path(0, identity))
            self.actions += 1
            body[key] = f"rename {self.actions}" if key == "title" else not body[key]
            if key == "title":
                body["titleSource"] = "manual"
            rewrite(self.path(0, identity), body)
            self.intent[identity][key] = body[key]
        if self.rng.random() < 0.15:
            path = self.path(self.rng.randrange(len(FOLDERS)), self.rng.choice(SESSIONS))
            body = read(path)
            body["lastFocusedAt"] = body.get("lastFocusedAt", 0) + 1
            rewrite(path, body)
        if self.rng.random() < 0.3:
            self.clock += 3

    def install_hooks(self, monkeypatch):
        checkpoint, entry = mirror.Mirror._checkpoint, mirror._read_entry
        scandir, install = mirror.os.scandir, mirror._install
        service = mirror.Mirror._service_hot

        def at_checkpoint(engine, current, stage=None):
            self.act()
            return checkpoint(engine, current, stage)

        def at_read(path):
            if self.active and self.failures and self.rng.random() < 0.08:
                self.failed_reads += 1
                raise OSError(errno.EMFILE, "injected read failure", str(path))
            result = entry(path)
            self.act()
            return result

        def at_listing(path="."):
            if (self.active and self.failures and Path(path).is_relative_to(self.store)
                    and Path(path) != self.store and self.rng.random() < 0.08):
                self.failed_listings += 1
                raise OSError(errno.EMFILE, "injected listing failure", str(path))
            return scandir(path)

        def at_install(temporary, destination, **kwargs):
            installed = install(temporary, destination, **kwargs)
            # Check successful writes, not stale proposals rejected by the
            # destination signature check. No environment hook runs in publish.
            if installed and Path(destination).parent == self.path(0, SESSIONS[0]).parent:
                body = read(destination)
                assert {key: body[key] for key in FIELDS} == self.intent[body["cliSessionId"]]
            return installed

        def at_service(engine):
            result = service(engine)
            self.services += 1
            return result

        monkeypatch.setattr(mirror.time, "monotonic", lambda: self.clock)
        # Durability is covered separately; this checks ordering and values.
        monkeypatch.setattr(mirror.os, "fsync", lambda fd: None)
        monkeypatch.setattr(mirror.Mirror, "_checkpoint", at_checkpoint)
        monkeypatch.setattr(mirror, "_read_entry", at_read)
        monkeypatch.setattr(mirror.os, "scandir", at_listing)
        monkeypatch.setattr(mirror, "_install", at_install)
        monkeypatch.setattr(mirror.Mirror, "_service_hot", at_service)

    def assert_converged(self):
        self.active = False
        for _ in range(3):
            assert self.running.run_once().state == "ok"
            assert self.running.run_hot().state == "ok"
        base = mirror._load(self.running.flags_path)
        for identity in SESSIONS:
            for folder in range(len(FOLDERS)):
                body = read(self.path(folder, identity))
                assert {key: body[key] for key in FIELDS} == self.intent[identity]
                assert body["titleSource"] == read(self.path(0, identity))["titleSource"]
            assert {key: base[identity][key] for key in FIELDS} == self.intent[identity]


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("failures", [False, True], ids=["readable", "transient-failures"])
def test_embedded_services_preserve_user_intent_and_converge(tmp_path, monkeypatch, seed, failures):
    """C-23.28: randomized saves and clock jumps interleave full/hot inventories."""
    world = InterleavedMirror(tmp_path, monkeypatch, seed, failures)
    world.install_hooks(monkeypatch)
    assert world.running.run_once().state == "ok"
    world.active = True
    for step in range(8):
        if step == 4:
            world.running = world.engine()  # restart loses only in-memory state
        world.running._last_sweep = None
        result = world.running.run_once() if step % 2 == 0 else world.running.run_hot()
        assert result.state == "ok", result.error
    world.assert_converged()
    assert world.services > 0, "this trace must actually exercise embedded hot service"
    assert world.actions > 0
    if failures:
        assert world.failed_reads > 0 and world.failed_listings > 0
