"""Review #195 follow-ups: interrupted finalization never accepts live evidence."""

from dataclasses import asdict, replace
import json
import errno
import hashlib
import os
from pathlib import Path

import pytest

from subfleet import daemon as module
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, ExitInfo, Outcome, OutcomeClass
from subfleet.daemon import Daemon
from tests.conftest import LANE_EMAIL, LANE_IDENTITY
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.frozen_streams import NONREGULAR_KINDS, frozen_identity, make_frozen


def stage_provider(daemon, harness, name, out):
    lane = daemon.store.get_lane("codex-1")
    if name == "claude":
        lane = replace(lane, lane_id="claude-frozen", provider="claude", account_key="claude:fixture",
                       credential=Credential("claude", lane.credential.ref, "home"),
                       identity=LANE_IDENTITY, label=LANE_EMAIL)
        (Path(lane.credential.ref) / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "fixture-home-token"}}))
        daemon.store.put_lane(lane)
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out),
                                  pinned_lane=lane.lane_id, pinned_model="haiku" if name == "claude" else "astra")
    adapter = ClaudeAdapter(projects_dir=adir / "projects") if name == "claude" else CodexAdapter()
    spec = daemon._spec(daemon.store.get_job(job_id))
    launch = adapter.build_launch(spec, attempt["attempt_id"], adir, lane, {},
                                  attempt["model_requested"], None, Path(spec.prompt_path), None)
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    events = ([{"type": "result", "is_error": False, "result": "Finished."}] if name == "claude" else [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Finished."}},
        {"type": "turn.completed"},
    ])
    finalizing = receipt_fixture(daemon, attempt, adir, stdout=(
        "".join(json.dumps(event) + "\n" for event in events).encode()))
    return job_id, attempt, adir, adapter, launch, finalizing


def replay(daemon, harness, attempt, finalizing, restart, monkeypatch):
    if restart:
        daemon.close()
        successor = Daemon(harness.root)
        monkeypatch.setattr(successor, "_launch", lambda *args: pytest.fail("recovery must not launch"))
        try:
            successor._process_attempt(attempt["attempt_id"])
        except BaseException:
            successor.close()
            raise
        return successor
    daemon._finalize(finalizing)
    return daemon


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("checkpoint", ("finalization", "deliverable"))
@pytest.mark.parametrize("kind", ("empty", *NONREGULAR_KINDS))
def test_claude_frozen_stream_survives_interrupted_finalization(
        state_daemon, monkeypatch, restart, checkpoint, kind):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "claude", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    make_frozen(adir / "stream.jsonl", kind)
    identity = frozen_identity(adir / "stream.jsonl")
    if checkpoint == "deliverable":
        # Interrupted older finalization can have an ok verdict but no captured
        # deliverable yet. Extraction must still honor the frozen path.
        verdict = Outcome(OutcomeClass.OK, "cached success", native_session_id=launch.native_session_id)
        attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
        (adir / "finalization.json").write_text(json.dumps({
            "outcome": asdict(verdict), "attestation": asdict(attest)}))
    def crash(role, path):
        if role == checkpoint:
            raise RuntimeError("interrupted finalization")
    daemon.publish_hook = crash
    with pytest.raises(RuntimeError, match="interrupted finalization"):
        daemon._finalize(finalizing)
    daemon.publish_hook = None
    frozen_verdict = (adir / "finalization.json").read_bytes() if checkpoint == "deliverable" else None
    (adir / "stdout").write_text('{"type":"result","is_error":false,"result":"Later summary."}\n')
    successor = None
    try:
        active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
        successor = active if restart else None
        job = active.store.get_job(job_id)
        assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
        row = active.store.get_attempt(attempt["attempt_id"])
        assert row["outcome_class"] == "unknown"
        assert (adir / "deliverable.md").read_bytes() == b""
        assert frozen_identity(adir / "stream.jsonl") == identity
        if frozen_verdict is not None:
            assert (adir / "finalization.json").read_bytes() == frozen_verdict
        assert out.read_bytes() == b"previous output\n"
        assert not active.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                      (attempt["attempt_id"],))
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("raw", ("stream.jsonl", "custom.jsonl", "stdout", "parent-alias",
                                 "same-canonical", "post-copy-link", "unlinked-writer", "fstat-link"))
def test_writable_stream_alias_cannot_supply_later_completion(
        state_daemon, monkeypatch, restart, raw):
    """Port #209's hard-link input: cached auth-dead, rc 0, interim text."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "codex", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    live = adir / "stdout"
    raw_path = adir / raw
    if raw == "parent-alias":
        (adir / "alias").symlink_to(adir, target_is_directory=True)
        raw_path = adir / "alias" / "stdout"
    elif raw == "same-canonical":
        live = raw_path = adir / "stream.jsonl"
    elif raw in ("post-copy-link", "unlinked-writer", "fstat-link"):
        raw_path = adir / "stream.jsonl"
    launch = replace(launch, stdout_path=str(live), raw_stream_path=str(raw_path))
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    live.write_bytes(b"")
    (adir / "stderr").write_bytes(b"Your refresh token was revoked.\n")
    if raw in ("stream.jsonl", "custom.jsonl", "post-copy-link", "unlinked-writer"):
        os.link(live, adir / "stream.jsonl")
    elif raw == "fstat-link":
        (adir / "stream.jsonl").write_bytes(b"")
    verdict = Outcome(OutcomeClass.AUTH_DEAD, "cached old authentication rejection")
    attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
    (adir / "finalization.json").write_text(json.dumps({
        "outcome": asdict(verdict), "attestation": asdict(attest)}))
    (adir / "deliverable.md").write_bytes(b"Interim progress, not a finished turn.")
    captured = {p: (adir / p).read_bytes() for p in ("finalization.json", "deliverable.md", "exit.json")}
    observed = []
    answered = Daemon._answered
    def observe(outcome, cls):
        observed.append(outcome.cls.value)
        return answered(outcome, cls)
    monkeypatch.setattr(Daemon, "_answered", staticmethod(observe))
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    # Hold the writer descriptor across write-then-rename, as a provider would.
    with live.open("ab", buffering=0) as writer:
        writer_identity = os.fstat(writer.fileno())
        if raw == "unlinked-writer":
            live.unlink()
        with pytest.MonkeyPatch.context() as race:
            if raw == "fstat-link":
                original_fstat = os.fstat
                candidate_identity = (adir / "stream.jsonl").stat().st_ino
                checks = []
                def link_at_decision(fd):
                    info = original_fstat(fd)
                    if info.st_ino == candidate_identity:
                        checks.append(fd)
                        if len(checks) == 2:
                            live.unlink()
                            os.link(adir / "stream.jsonl", live)
                    return info
                race.setattr(os, "fstat", link_at_decision)
            with pytest.raises(RuntimeError, match="interrupted salvage"):
                daemon._finalize(finalizing)
        monkeypatch.setattr(daemon, "_salvage", salvage)
        if raw == "post-copy-link":
            (adir / "stream.jsonl").unlink()
            os.link(live, adir / "stream.jsonl")
        if raw == "fstat-link":
            with live.open("ab") as linked_writer:
                linked_writer.write(b'{"type":"turn.completed"}\n')
        else:
            writer.write(b'{"type":"turn.completed"}\n')
    active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
    try:
        assert observed == ["auth-dead", "auth-dead"]
        assert active.store.get_job(job_id)["accepted_attempt_id"] is None
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "auth-dead"
        assert out.read_bytes() == b"previous export\n"
        artifacts = [row for row in active.store.list_artifacts(attempt["attempt_id"])
                     if row["role"] == "raw-stream"]
        assert len(artifacts) == 1
        frozen = Path(artifacts[0]["path"])
        assert frozen.read_bytes() == b""
        assert frozen.stat().st_nlink == 1
        assert (frozen.stat().st_dev, frozen.stat().st_ino) != (writer_identity.st_dev, writer_identity.st_ino)
        assert {p: (adir / p).read_bytes() for p in captured} == captured
        assert not active.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                      (attempt["attempt_id"],))
        if raw == "custom.jsonl":
            assert not os.path.lexists(launch.raw_stream_path)
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("raw", ("custom.jsonl", None))
def test_authoritative_canonical_stream_is_raw_artifact(state_daemon, monkeypatch, name, raw):
    daemon, harness = state_daemon
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(
        daemon, harness, name, harness.root / "out.md")
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    frozen = adir / "stream.jsonl"
    payload = (adir / "stdout").read_bytes()
    frozen.write_bytes(payload)
    launch = replace(launch, raw_stream_path=str(adir / raw) if raw else None)
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    artifacts = [row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                 if row["role"] == "raw-stream"]
    assert len(artifacts) == 1
    assert (artifacts[0]["bytes"], artifacts[0]["sha256"]) == (
        len(payload), hashlib.sha256(payload).hexdigest())
    assert Path(artifacts[0]["path"]).read_bytes() == payload
    if raw:
        assert not os.path.lexists(launch.raw_stream_path)


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("name,contents,expected", [
    ("codex", b" \n\t\r\v\f", "unknown"),
    ("claude", b" \n\t\r\v\f", "unknown"),
    ("claude", "\u2003\u00a0".encode(), "unknown"),
    # The fresh Codex check is on bytes, where these are nonblank; preserve it.
    ("codex", "\u2003\u00a0".encode(), "ok"),
    ("codex", b" \n Finished. \t", "ok"),
    ("claude", b" \n Finished. \t", "ok"),
])
def test_cached_ok_uses_fresh_deliverable_content_boundary(
        state_daemon, monkeypatch, restart, name, contents, expected):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    verdict = adapter.classify(adir, launch, ExitInfo(0, None, .1, None, None))
    assert verdict.cls == OutcomeClass.OK
    attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
    (adir / "finalization.json").write_text(json.dumps({
        "outcome": asdict(verdict), "attestation": asdict(attest), "rule": module.FINALIZATION_RULE}))
    (adir / "deliverable.md").write_bytes(contents)
    captured = {name: (adir / name).read_bytes() for name in ("deliverable.md", "finalization.json", "exit.json")}
    salvage = daemon._salvage
    monkeypatch.setattr(daemon, "_salvage", lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted salvage")))
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    monkeypatch.setattr(daemon, "_salvage", salvage)
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
    monkeypatch.setattr(adapter, "deliverable", lambda *args: pytest.fail("captured deliverable extracted again"))
    successor = None
    try:
        active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
        successor = active if restart else None
        job = active.store.get_job(job_id)
        row = active.store.get_attempt(attempt["attempt_id"])
        assert row["outcome_class"] == expected
        assert {name: (adir / name).read_bytes() for name in captured} == captured
        if expected == "unknown":
            assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
            assert row["outcome_detail"] == "empty deliverable with rc 0"
            assert json.loads(row["evidence_json"])["provider_verdict"]["class"] == "ok"
            assert out.read_bytes() == b"previous output\n"
            assert active.store.get_lane(attempt["lane_id"]).enabled
            assert not active.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                          (attempt["attempt_id"],))
        else:
            assert (job["state"], job["accepted_attempt_id"]) == ("succeeded", attempt["attempt_id"])
            assert out.read_bytes() == contents
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("damage", ("delete", "replace", "in-place", "live-alias", "post-copy-link"))
def test_lost_frozen_copy_records_failure_without_reading_live_candidate(
        state_daemon, monkeypatch, restart, damage):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "codex", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    salvage = daemon._salvage
    monkeypatch.setattr(daemon, "_salvage", lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted salvage")))
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    monkeypatch.setattr(daemon, "_salvage", salvage)
    frozen = daemon._freeze_stream(adir, launch)
    fresh = b'{"type":"item.completed","item":{"type":"agent_message","text":"Fresh interim."}}\n'
    if damage == "post-copy-link":
        # Preserve the published inode, then give the provider a writable name
        # for it. Restart must invalidate both cached classification and text.
        identity = frozen.stat().st_ino
        (adir / "stdout").unlink()
        os.link(frozen, adir / "stdout")
        (adir / "stdout").write_bytes(fresh)
        assert frozen.stat().st_ino == identity
        assert os.path.samefile(frozen, adir / "stdout")
    else:
        (adir / "stdout").write_bytes(fresh)
        if damage != "in-place":
            frozen.unlink()
        if damage in ("replace", "in-place"):
            frozen.write_bytes(b"")
        elif damage == "live-alias":
            os.link(adir / "stdout", frozen)
    if Path(launch.raw_stream_path) != frozen:
        Path(launch.raw_stream_path).write_bytes(fresh)
    classified, extracted = [], []
    classify, deliverable = adapter.classify, adapter.deliverable
    def classify_again(*args):
        classified.append(True)
        return classify(*args)
    def extract_again(*args):
        extracted.append(True)
        return deliverable(*args)
    monkeypatch.setattr(adapter, "classify", classify_again)
    monkeypatch.setattr(adapter, "deliverable", extract_again)
    successor = None
    try:
        active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
        successor = active if restart else None
        assert classified == extracted == []
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "transient"
        assert (adir / "deliverable.md").read_bytes() == b"Finished."
        result = json.loads((adir / "finalization.json").read_text())
        assert result["stream_capture"]["status"] == "unreadable"
        assert not [r for r in active.store.list_artifacts(attempt["attempt_id"])
                    if r["role"] in ("raw-stream", "deliverable", "export")]
        assert active.store.get_job(job_id)["accepted_attempt_id"] is None
        assert out.read_bytes() == b"previous export\n"
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("failure", ("eio", "legacy-eio", "stderr-permission"))
@pytest.mark.parametrize("complete", (False, True), ids=("late-completion", "already-complete"))
@pytest.mark.parametrize("name,expected", (("codex", "transient"), ("claude", "unknown")))
def test_capture_failure_is_sticky_after_io_recovers(
        state_daemon, monkeypatch, restart, failure, complete, name, expected):
    """C-4.3/C-9.2: neither a replay nor a new daemon may retry failed capture."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    if not complete:
        (adir / "stdout").write_bytes(
            b'{"type":"item.completed","item":{"type":"agent_message","text":"Interim progress only."}}\n')
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    stderr = adir / "stderr"
    try:
        with pytest.MonkeyPatch.context() as patch:
            if failure in ("eio", "legacy-eio"):
                def unreadable(*args):
                    raise OSError(errno.EIO, "capture failed")
                patch.setattr(daemon, "_freeze_stream", unreadable)
            else:
                stderr.chmod(0)  # Real kernel PermissionError, no capture stub.
            with pytest.raises(RuntimeError, match="interrupted salvage"):
                daemon._finalize(finalizing)
    finally:
        stderr.chmod(0o600)
    if failure == "legacy-eio":
        result = json.loads((adir / "finalization.json").read_text())
        result.pop("stream_capture", None)
        (adir / "finalization.json").write_text(json.dumps(result))
    saved = (adir / "finalization.json").read_bytes()
    assert json.loads(saved)["outcome"]["cls"] == expected
    if not complete:
        with (adir / "stdout").open("ab") as stream:
            stream.write(b'{"type":"turn.completed"}\n' if name == "codex" else
                         b'{"type":"result","is_error":false,"result":"Late answer."}\n')
    monkeypatch.setattr(daemon, "_salvage", salvage)
    active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
    try:
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == expected
        assert active.store.get_job(job_id)["state"] == "failed"
        assert (adir / "finalization.json").read_bytes() == saved
        result = json.loads(saved)
        capture = (result["outcome"]["evidence"]["stream_capture"] if failure == "legacy-eio"
                   else result["stream_capture"])
        assert capture["status"] == "unreadable"
        assert not (adir / "deliverable.md").exists()
        assert not (adir / "stream.freeze.json").exists()
        assert not list(adir.glob("stream.frozen-*"))
        assert not [r for r in active.store.list_artifacts(attempt["attempt_id"])
                    if r["role"] in ("raw-stream", "deliverable", "export")]
        assert out.read_bytes() == b"previous export\n"
        active._process_attempt(attempt["attempt_id"])
        assert (adir / "finalization.json").read_bytes() == saved
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("name,failure,legacy", (
    ("codex", "copy-eio", False), ("codex", "copy-emfile", False),
    ("codex", "stdout-permission", False), ("codex", "stderr-permission", False),
    ("codex", "last-permission", False), ("claude", "copy-eio", False),
    ("codex", "copy-eio", True), ("claude", "copy-eio", True),
))
def test_valid_verdict_survives_a_later_freeze_failure_and_last_md(
        state_daemon, monkeypatch, restart, name, failure, legacy):
    """C-4.3/C-9.2: an interrupted real restore cannot replace a valid verdict."""
    from subfleet.frozen_stream import _restored_copy, read_freeze_receipt
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    saved = (adir / "finalization.json").read_bytes()
    captured = (adir / "deliverable.md").read_bytes()
    frozen = adir / json.loads(saved)["stream"]
    if legacy:
        result = json.loads(saved)
        result.pop("stream")  # A legacy verdict can still have a valid copy receipt.
        (adir / "finalization.json").write_text(json.dumps(result))
        saved = (adir / "finalization.json").read_bytes()
    opened = []
    denied = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            if failure.startswith("copy-"):
                original_open = os.open
                error = errno.EIO if failure == "copy-eio" else errno.EMFILE
                def unreadable(path, *args, **kwargs):
                    if os.fspath(path) == str(frozen) and not opened:
                        opened.append(path)
                        raise OSError(error, "recorded copy cannot be checked")
                    return original_open(path, *args, **kwargs)
                patch.setattr(os, "open", unreadable)
            else:
                role = failure.removesuffix("-permission")
                denied = adir / ("last.md" if role == "last" else role)
                if role == "last":
                    denied.write_bytes(b"Late last.md before restore.")
                denied.chmod(0)  # Exercise _writable_aliases through the kernel.
                error = errno.EACCES
            with pytest.raises(OSError) as caught:
                daemon._finalize(finalizing)
            assert caught.value.errno == error
    finally:
        if denied is not None:
            denied.chmod(0o600)
    assert (adir / "finalization.json").read_bytes() == saved
    assert _restored_copy(adir, read_freeze_receipt(adir), launch) == frozen
    assert len(opened) == int(failure.startswith("copy-"))
    (adir / "last.md").write_bytes(b"Written after the attempt exited.")
    monkeypatch.setattr(daemon, "_salvage", salvage)
    active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
    try:
        assert active.store.get_job(job_id)["state"] == "succeeded"
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "ok"
        assert (adir / "deliverable.md").read_bytes() == captured == b"Finished."
        assert out.read_bytes() == captured
        assert (adir / "finalization.json").read_bytes() == saved
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("name,expected", (("codex", "transient"), ("claude", "unknown")))
def test_legacy_verdict_cannot_retry_a_failed_first_capture(
        state_daemon, monkeypatch, restart, name, expected):
    """C-4.3: a cached verdict without any copy cannot authorize a later live read."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    (adir / "stdout").write_bytes(b"")
    (adir / "deliverable.md").write_bytes(b"Cached interim.")
    (adir / "finalization.json").write_text(json.dumps({
        "outcome": {"cls": "ok", "detail": "legacy cached success"},
        "attestation": {"status": "unattested", "served_model": None},
        "rule": module.FINALIZATION_RULE,
    }))
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    original_open = os.open
    failed = []
    with pytest.MonkeyPatch.context() as patch:
        def unreadable(path, *args, **kwargs):
            if os.fspath(path) == launch.stdout_path and not failed:
                failed.append(path)
                raise OSError(errno.EIO, "first capture failed")
            return original_open(path, *args, **kwargs)
        patch.setattr(os, "open", unreadable)
        with pytest.raises(RuntimeError, match="interrupted salvage"):
            daemon._finalize(finalizing)
    assert len(failed) == 1
    saved = (adir / "finalization.json").read_bytes()
    assert json.loads(saved)["outcome"]["cls"] == expected
    assert json.loads(saved)["stream_capture"]["status"] == "unreadable"
    (adir / "stdout").write_bytes(
        b'{"type":"turn.completed"}\n' if name == "codex" else
        b'{"type":"result","is_error":false,"result":"Late answer."}\n')
    monkeypatch.setattr(daemon, "_salvage", salvage)
    active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
    try:
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == expected
        assert active.store.get_job(job_id)["state"] == "failed"
        assert (adir / "finalization.json").read_bytes() == saved
        assert not (adir / "stream.freeze.json").exists()
        assert not list(adir.glob("stream.frozen-*"))
        assert not [r for r in active.store.list_artifacts(attempt["attempt_id"])
                    if r["role"] in ("raw-stream", "deliverable", "export")]
        assert out.read_bytes() == b"previous export\n"
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize("failure", ("copy-eio", "copy-emfile", "stderr-permission"))
def test_uncheckable_recorded_copy_retries_then_records_sticky_failure(
        state_daemon, monkeypatch, failure):
    """C-4.3: a persistently uncheckable copy releases its lane after three tries."""
    from subfleet.frozen_stream import _restored_copy, read_freeze_receipt
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "codex", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    monkeypatch.setattr(daemon, "_salvage", salvage)
    saved = (adir / "finalization.json").read_bytes()
    frozen = adir / json.loads(saved)["stream"]
    original_open = os.open
    stderr = adir / "stderr"
    try:
        with pytest.MonkeyPatch.context() as patch:
            if failure.startswith("copy-"):
                error = errno.EIO if failure == "copy-eio" else errno.EMFILE
                def unreadable(path, *args, **kwargs):
                    if os.fspath(path) == str(frozen):
                        raise OSError(error, "recorded copy cannot be checked")
                    return original_open(path, *args, **kwargs)
                patch.setattr(os, "open", unreadable)
            else:
                stderr.chmod(0)
                error = errno.EACCES
            for _ in range(2):
                with pytest.raises(OSError) as caught:
                    daemon._finalize(finalizing)
                assert caught.value.errno == error
                assert (adir / "finalization.json").read_bytes() == saved
                assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
            daemon._finalize(finalizing)
    finally:
        stderr.chmod(0o600)
    failed = (adir / "finalization.json").read_bytes()
    result = json.loads(failed)
    assert result["stream_capture"]["status"] == "unreadable"
    assert result["stream_capture"]["reason"] == "copy cannot be checked"
    expected_detail = "provider stream copy could not be checked: retry checks exhausted"
    assert result["outcome"]["detail"] == expected_detail
    assert daemon.store.get_attempt(attempt["attempt_id"])["outcome_detail"] == expected_detail
    assert daemon.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "transient"
    assert daemon.store.get_job(job_id)["state"] == "failed"
    assert _restored_copy(adir, read_freeze_receipt(adir), launch) == frozen
    daemon._process_attempt(attempt["attempt_id"])
    assert (adir / "finalization.json").read_bytes() == failed
    assert not [r for r in daemon.store.list_artifacts(attempt["attempt_id"])
                if r["role"] in ("raw-stream", "deliverable", "export")]
    assert out.read_bytes() == b"previous export\n"


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
def test_legacy_recapture_cannot_rebind_an_unversioned_verdict(state_daemon, monkeypatch, restart):
    """C-4.3: an older daemon's replacement copy cannot inherit a cached verdict."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous export\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "codex", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    monkeypatch.setattr(daemon, "_salvage", salvage)
    result = json.loads((adir / "finalization.json").read_text())
    result.pop("stream")  # A verdict from before stream tokens were recorded.
    (adir / "finalization.json").write_text(json.dumps(result))
    receipt_path = adir / "stream.freeze.json"
    receipt = json.loads(receipt_path.read_text())
    original = adir / receipt["file"]
    replacement = adir / "stream.frozen-legacy-recapture.jsonl"
    replacement.write_bytes(original.read_bytes())
    original.unlink()
    info = replacement.stat()
    receipt.update(file=replacement.name, device=info.st_dev, inode=info.st_ino, reset_finalization=True)
    receipt_path.write_text(json.dumps(receipt))
    (adir / "last.md").write_bytes(b"Written after the attempt exited.")
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("reclassified replacement copy"))
    monkeypatch.setattr(adapter, "deliverable", lambda *args: pytest.fail("extracted late last.md"))
    active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
    try:
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "transient"
        assert active.store.get_job(job_id)["accepted_attempt_id"] is None
        result = json.loads((adir / "finalization.json").read_text())
        assert result["stream_capture"]["status"] == "unreadable"
        assert (adir / "deliverable.md").read_bytes() == b"Finished."
        assert out.read_bytes() == b"previous export\n"
        assert not [r for r in active.store.list_artifacts(attempt["attempt_id"])
                    if r["role"] in ("raw-stream", "deliverable", "export")]
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("interruption,failure", (
    ("crash", "copy-eio"), ("crash", "copy-emfile"), ("crash", "stderr-permission"),
    ("enospc", "copy-eio"), ("classify", "copy-eio"), ("attest", "copy-eio"),
))
def test_receipted_copy_retries_a_check_before_any_verdict(
        state_daemon, monkeypatch, restart, name, interruption, failure):
    """C-4.3: a receipt proves capture even when verdict publication never finished."""
    from subfleet.frozen_stream import read_freeze_receipt

    class Crash(BaseException):
        pass

    daemon, harness = state_daemon
    out = harness.root / "out.md"
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    with pytest.MonkeyPatch.context() as patch:
        if interruption in ("crash", "enospc"):
            def interrupt(role, path):
                if role == "finalization":
                    if interruption == "crash":
                        raise Crash("crashed before verdict publication")
                    raise OSError(errno.ENOSPC, "verdict disk full")
            patch.setattr(daemon, "publish_hook", interrupt)
        else:
            def interrupt(*args):
                raise RuntimeError("adapter interrupted before verdict publication")
            patch.setattr(adapter, interruption, interrupt)
        with pytest.raises(Crash if interruption == "crash" else
                           OSError if interruption == "enospc" else RuntimeError):
            daemon._finalize(finalizing)
    assert not (adir / "finalization.json").exists()
    receipt = (adir / "stream.freeze.json").read_bytes()
    frozen = adir / read_freeze_receipt(adir)["file"]
    payload = frozen.read_bytes()
    active = daemon
    if restart:
        daemon.close()
        active = Daemon(harness.root)
    denied = adir / "stderr"
    opened = []
    try:
        with pytest.MonkeyPatch.context() as patch:
            error = errno.EACCES
            if failure.startswith("copy-"):
                original_open = os.open
                error = errno.EIO if failure == "copy-eio" else errno.EMFILE
                def unreadable(path, *args, **kwargs):
                    if os.fspath(path) == str(frozen) and not opened:
                        opened.append(path)
                        raise OSError(error, "saved copy check interrupted")
                    return original_open(path, *args, **kwargs)
                patch.setattr(os, "open", unreadable)
            else:
                denied.chmod(0)
            try:
                with pytest.raises(OSError) as caught:
                    active._finalize(finalizing)
                assert caught.value.errno == error
            finally:
                denied.chmod(0o600)
        assert not (adir / "finalization.json").exists()
        assert (adir / "stream.freeze.json").read_bytes() == receipt
        assert frozen.read_bytes() == payload
        assert active.store.get_job(job_id)["accepted_attempt_id"] is None
        assert active._stream_check_failures[attempt["attempt_id"]] == 1
        (adir / "stdout").write_bytes(b"later live output\n")
        active._finalize(finalizing)
        assert active.store.get_job(job_id)["state"] == "succeeded"
        assert active.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "ok"
        assert out.read_bytes() == b"Finished."
        assert json.loads((adir / "finalization.json").read_text())["stream"] == frozen.name
        assert attempt["attempt_id"] not in active._stream_check_failures
    finally:
        if restart:
            active.close()


def test_completed_copy_check_resets_the_consecutive_retry_budget(state_daemon, monkeypatch):
    """C-4.3: successful validation between faults grants a fresh retry budget."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "codex", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    salvage = daemon._salvage
    def interrupt(*args):
        raise RuntimeError("interrupted salvage after completed copy check")
    monkeypatch.setattr(daemon, "_salvage", interrupt)
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    saved = (adir / "finalization.json").read_bytes()
    frozen = adir / json.loads(saved)["stream"]
    original_open = os.open
    def unreadable(path, *args, **kwargs):
        if os.fspath(path) == str(frozen):
            raise OSError(errno.EIO, "copy check interrupted")
        return original_open(path, *args, **kwargs)
    for _ in range(2):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", unreadable)
            for count in range(1, module.STREAM_CHECK_TRIES):
                with pytest.raises(OSError) as caught:
                    daemon._finalize(finalizing)
                assert caught.value.errno == errno.EIO
                assert daemon._stream_check_failures[attempt["attempt_id"]] == count
                assert (adir / "finalization.json").read_bytes() == saved
        with pytest.raises(RuntimeError, match="interrupted salvage"):
            daemon._finalize(finalizing)
        assert attempt["attempt_id"] not in daemon._stream_check_failures
    monkeypatch.setattr(daemon, "_salvage", salvage)
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert out.read_bytes() == b"Finished."
