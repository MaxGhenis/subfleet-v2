"""C-26.6, C-28.1: replay and final payload reads use content identity."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
from pathlib import Path

import pytest

from subfleet.conversations import attachments
from subfleet.conversations.launch import TURN_MANIFEST_KEY, spec_from_manifest
from subfleet.conversations.runner import TurnRunner
from tests.unit.test_codex_turn import CWD, HASH, MID as CODEX_MID, to_running
from tests.unit.test_turn_replay import INIT_OK, LIFECYCLE, SUCCESS, WRITTEN, ended

IMAGE = b"\x89PNG\r\n\x1a\noriginal-image"


def attach(world):
    original = world.ws / "image.png"
    original.write_bytes(IMAGE)
    receipt = attachments.add(world.svc.store, str(original))
    path, media = attachments.check(world.svc.store, receipt["sha256"])
    manifest = world.adir.parent / "manifest.json"
    data = json.loads(manifest.read_text())
    data[TURN_MANIFEST_KEY]["images"] = [{"sha256": receipt["sha256"], "media_type": media, "path": path}]
    manifest.write_text(json.dumps(data))
    world.svc.store.set_state(world.mid, "starting", job_id=world.job_id)
    return Path(path)


def move_root(world):
    world.svc.close()
    world.svc.daemon.store.close()
    old = world.root
    new = old.with_name("moved-state")
    old.rename(new)
    world.adir = new / world.adir.relative_to(old)
    world.root = new
    world.svc = world.service()


def driver_runner(world, provider="claude"):
    turn = json.loads((world.adir.parent / "manifest.json").read_text())[TURN_MANIFEST_KEY]
    spec = spec_from_manifest(turn, lane_email=None)
    if provider == "codex":
        spec = dataclasses.replace(spec, provider="codex", model_id="gpt-6-astra", guard_hash=HASH,
                                   cwd=CWD, effort="high", message_id=CODEX_MID)
    return TurnRunner(store=world.svc.store, attempt={"attempt_id": world.aid}, spec=spec,
                      conversation_id=world.cid, attempt_dir=world.adir, control_socket="unused.sock",
                      on_outcome=lambda r: None, on_contain=lambda aid: None, ended=True)


@pytest.mark.parametrize("changed", ["missing", "moved-root"])
@pytest.mark.parametrize("logged", [WRITTEN, WRITTEN[:-1]], ids=["written", "pending-write"])
def test_delivered_image_replay_settles_success_without_rebuilding_payload(ended, changed, logged):
    world = ended([INIT_OK, LIFECYCLE, SUCCESS], logged=logged)
    path = attach(world)
    world.end_attempt()
    if changed == "missing":
        path.unlink()
    else:
        move_root(world)
    world.svc._replay_unsettled()
    runner = world.svc.runners[world.aid]
    assert runner.join(10)
    assert runner.outcome_reported
    assert world.message() == ("complete", None)
    assert world.svc.store.conversation(world.cid)["blocked_by"] is None


def test_undelivered_image_resolves_digest_after_state_root_move(ended):
    world = ended([INIT_OK], logged=[])
    old_path = attach(world)
    move_root(world)
    assert not old_path.exists()
    runner = driver_runner(world)
    runner.driver.start()
    step = runner.driver.feed(INIT_OK, 0)
    assert step.outcome is None
    frame = next(f for f in step.frames if f.tag == "user-message")
    image = json.loads(frame.line)["message"]["content"][1]
    assert base64.b64decode(image["source"]["data"]) == IMAGE


@pytest.mark.parametrize("changed", ["missing", "symlink", "bytes", "public", "hardlink", "fifo"])
def test_final_image_read_refuses_changed_copy_before_user_frame(ended, changed):
    world = ended([INIT_OK], logged=[])
    path = attach(world)  # add/check succeeded before the path changed
    private = world.ws / "private.txt"
    private.write_bytes(b"unrelated-private-content")
    if changed in ("missing", "symlink", "fifo"):
        path.unlink()
        if changed == "symlink":
            path.symlink_to(private)
        elif changed == "fifo":
            os.mkfifo(path)
    elif changed == "bytes":
        path.write_bytes(b"x" * len(IMAGE))
    elif changed == "public":
        path.chmod(0o644)
    else:
        os.link(path, world.ws / "second-name")
    world.end_attempt()
    (world.adir / "exit.json").write_text('{"rc": 0}')
    world.svc._replay_unsettled()
    runner = world.svc.runners[world.aid]
    assert runner.join(10)
    assert runner.outcome_reported
    assert runner.driver.outcome.reason == "attachment-missing"
    assert not runner.driver.outcome.accepted
    assert all(frame.tag != "user-message" for frame in runner.outbox)
    assert world.message()[0] == "failed"


@pytest.mark.parametrize("linked_directory", [False, True], ids=["ordinary", "symlink-directory"])
def test_codex_gets_verified_private_attempt_copy(ended, linked_directory):
    world = ended([], logged=[])
    path = attach(world)
    outside = world.ws / "outside"
    outside.mkdir()
    if linked_directory:
        (world.adir / "images").symlink_to(outside, target_is_directory=True)
    runner = driver_runner(world, "codex")
    body = to_running(runner.driver)
    image = next(item for item in body["params"]["input"] if item["type"] == "localImage")
    snapshot = Path(image["path"])
    assert snapshot.parent == world.adir
    assert snapshot.resolve().is_relative_to(world.adir.resolve())
    assert list(outside.iterdir()) == []
    assert snapshot != path
    assert snapshot.read_bytes() == IMAGE
    assert snapshot.stat().st_mode & 0o777 == 0o400
    path.write_bytes(b"changed after frame construction")
    assert snapshot.read_bytes() == IMAGE
