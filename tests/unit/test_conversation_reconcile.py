"""C-24.4, C-24.6, C-24.8, C-26.7, C-26.8: how a turn's message settles, and the
reconciliation of a message whose delivery is uncertain (design D-12 to D-14)."""

from __future__ import annotations

import json

import pytest

from subfleet.conversations import reconcile
from subfleet.conversations.reconcile import (
    DELIVERED, MAX_READMITS, NOT_DELIVERED, UNKNOWN, Evidence, claude_record, codex_record, decide, frame_status,
    gather, settle,
)
from subfleet.relay import line_sha256

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"

GONE_ABSENT = Evidence(acknowledged=False, frame="absent", process_gone=True, native="absent")


# --- decide (D-14) ----------------------------------------------------------------------


def test_an_acknowledgement_or_the_native_record_means_delivered():
    """C-24.6, D-14 steps 1 and 2."""
    assert decide(Evidence(acknowledged=True, frame="written", process_gone=False, native="unreadable")) == DELIVERED
    assert decide(Evidence(acknowledged=False, frame="written", process_gone=True, native="found")) == DELIVERED


def test_absence_is_not_delivered_only_with_all_three_proofs():
    """C-24.6: the process verified gone, the native record read without the message, and
    the relay log showing the frame not written."""
    assert decide(GONE_ABSENT) == NOT_DELIVERED


@pytest.mark.parametrize("change", [
    {"process_gone": False},
    {"native": "unreadable"},
    {"frame": "written"},          # written, and neither acknowledged nor in the transcript
    {"frame": "failed"},           # the provider may have read part of it
    {"frame": "pending"},          # a write that never finished
    {"frame": "unreadable"},
])
def test_any_missing_proof_leaves_delivery_unknown(change):
    """C-24.6: otherwise the message is delivery-unknown and is never sent again."""
    assert decide(Evidence(**{**GONE_ABSENT.as_dict(), **change})) == UNKNOWN


# --- settle -----------------------------------------------------------------------------


def never(*_):
    raise AssertionError("no reconciliation is needed for a provider's terminal event")


def turn(**fields):
    return {"state": "failed", "reason": None, "ended_by": "eof", **fields}


def test_the_providers_terminal_event_settles_without_reconciliation():
    """C-24.4, C-26.7, D-12: success is complete even after a late stop; a reported stop is
    interrupted with its reason; a limit fails and asks for a continuation."""
    done = settle(turn(state="complete", ended_by="provider", stop_too_late=True), provider="claude", turn_seq=0,
                  gather=never)
    assert (done.state, done.reason, done.block) == ("complete", "stop-too-late", None)
    stopped = settle(turn(state="interrupted", reason="stopped", ended_by="provider", stop_reason="approval-timeout"),
                     provider="claude", turn_seq=0, gather=never)
    assert (stopped.state, stopped.reason, stopped.block) == ("interrupted", "approval-timeout", None)
    limited = settle(turn(reason="limited", ended_by="provider", limited=True), provider="claude", turn_seq=0,
                     gather=never)
    assert (limited.state, limited.reason, limited.continue_elsewhere) == ("failed", "limited", True)
    other = settle(turn(reason="error_during_execution", ended_by="provider"), provider="claude", turn_seq=0,
                   gather=never)
    assert (other.state, other.reason, other.block) == ("failed", "error_during_execution", None)


def test_a_refusal_before_sending_is_readmitted_a_bounded_number_of_times():
    """IR-23, C-24.6: a refusal another admission may pass carries the same message again,
    at most MAX_READMITS times, and only when the evidence says it was never delivered."""
    for seq in range(MAX_READMITS):
        again = settle(turn(reason="fast-unavailable", ended_by="driver"), provider="claude", turn_seq=seq,
                       gather=lambda: GONE_ABSENT)
        assert again.readmit and again.state == "waiting" and again.reason == "readmit:fast-unavailable"
    last = settle(turn(reason="fast-unavailable", ended_by="driver"), provider="claude", turn_seq=MAX_READMITS,
                  gather=lambda: GONE_ABSENT)
    assert not last.readmit and (last.state, last.reason) == ("failed", "not-delivered: fast-unavailable")
    identity = settle(turn(reason="identity", ended_by="driver"), provider="claude", turn_seq=0,
                      gather=lambda: GONE_ABSENT)
    assert (identity.state, identity.reason, identity.readmit) == ("failed", "not-delivered: identity", False)


def test_a_refusal_whose_evidence_is_unreadable_is_not_readmitted():
    """C-24.6: without a readable native record, not even a refusal before sending is
    called not-delivered; the message is delivery-unknown and blocks."""
    unknown = settle(turn(reason="fast-unavailable", ended_by="driver"), provider="claude", turn_seq=0,
                     gather=lambda: Evidence(False, "absent", True, "unreadable"))
    assert (unknown.state, unknown.block, unknown.readmit) == ("delivery-unknown", "delivery-unknown", False)


def test_a_model_mismatch_is_named_as_such():
    """C-26.8: failed (model-mismatch) whether or not the message was sent. After sending, a
    Claude turn whose provider did not finish it after the driver's stop blocks (C-24.8)."""
    codex = settle(turn(reason="model-mismatch", ended_by="driver"), provider="codex", turn_seq=0,
                   gather=lambda: GONE_ABSENT)
    assert (codex.state, codex.reason, codex.block) == ("failed", "model-mismatch", None)
    sent = Evidence(acknowledged=True, frame="written", process_gone=True, native="found")
    finished = settle(turn(reason="model-mismatch", ended_by="driver", terminal_after_end=True), provider="claude",
                      turn_seq=0, gather=lambda: sent)
    assert (finished.state, finished.reason, finished.block) == ("failed", "model-mismatch", None)
    cut = settle(turn(reason="model-mismatch", ended_by="driver"), provider="claude", turn_seq=0,
                 gather=lambda: sent)
    assert cut.block == "unfinished-turn"


def test_a_delivered_claude_turn_without_a_terminal_event_blocks_unfinished():
    """C-24.8, IR-5: acknowledged or in the transcript, then no `result`: the next resume
    could continue it. A stop that got no result is interrupted, still blocked."""
    for evidence in (Evidence(True, "written", True, "absent"), Evidence(False, "written", True, "found")):
        crashed = settle(turn(reason="ended-without-result"), provider="claude", turn_seq=0, gather=lambda: evidence)
        assert (crashed.state, crashed.reason, crashed.block, crashed.delivery) == (
            "failed", "ended-without-result", "unfinished-turn", DELIVERED)
    stopped = settle(turn(state="interrupted", reason="stopped", stop_reason="stopped"), provider="claude",
                     turn_seq=0, gather=lambda: Evidence(True, "written", True, "found"))
    assert (stopped.state, stopped.reason, stopped.block) == ("interrupted", "stopped", "unfinished-turn")
    relay = settle(turn(reason="ended-without-result", stop_reason="relay-failed"), provider="claude", turn_seq=0,
                   gather=lambda: Evidence(True, "written", True, "found"))
    assert (relay.state, relay.reason) == ("failed", "relay-failed")
    codex = settle(turn(reason="ended-without-result"), provider="codex", turn_seq=0,
                   gather=lambda: Evidence(True, "written", True, "found"))
    assert codex.block is None and codex.state == "failed"


def test_an_eof_with_nothing_delivered_or_nothing_known():
    """C-24.6: not delivered with all three proofs (a frame over the relay's cap is named);
    otherwise delivery-unknown, blocking the conversation, whatever the stop."""
    before = settle(turn(reason="ended-without-result"), provider="claude", turn_seq=0, gather=lambda: GONE_ABSENT)
    assert (before.state, before.reason, before.block) == ("failed", "not-delivered: ended-before-send", None)
    big = settle(turn(reason="ended-without-result", frame_refused="user-message"), provider="claude", turn_seq=0,
                 gather=lambda: GONE_ABSENT)
    assert big.reason == "not-delivered: frame-too-large"
    unknown = settle(turn(reason="ended-without-result"), provider="claude", turn_seq=0,
                     gather=lambda: Evidence(False, "written", True, "absent"))
    assert (unknown.state, unknown.block, unknown.delivery) == ("delivery-unknown", "delivery-unknown", UNKNOWN)
    assert unknown.reason == "ended-without-result: frame written, native record absent"


def test_a_claude_session_is_known_only_once_it_exists():
    """D-4: a session id Subfleet minted is kept (and resumed) only when the provider
    created the session; a refusal before sending leaves the next turn to start it."""
    fresh = settle(turn(reason="fast-unavailable", ended_by="driver"), provider="claude", turn_seq=0,
                   gather=lambda: GONE_ABSENT)
    assert not fresh.session_known
    existing = settle(turn(reason="fast-unavailable", ended_by="driver"), provider="claude", turn_seq=0,
                      gather=lambda: Evidence(False, "absent", True, "absent", "/p/s.jsonl", session_exists=True))
    assert existing.session_known


# --- evidence ---------------------------------------------------------------------------


def log_lines(*records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def intent(seq, tag, line="x"):
    return {"kind": "intent", "seq": seq, "op": "write", "tag": tag, "line": line, "sha256": line_sha256(line)}


def test_the_relay_log_is_read_whole(tmp_path):
    """C-24.6, C-26.4: `absent` needs the whole log read and no record of the message frame."""
    assert frame_status(tmp_path) == "absent"                  # the relay never logged a frame
    log = tmp_path / "stdin.jsonl"
    log.write_text(log_lines(intent(1, "init"), {"kind": "written", "seq": 1}))
    assert frame_status(tmp_path) == "absent"
    log.write_text(log.read_text() + log_lines(intent(2, "user-message")))
    assert frame_status(tmp_path) == "pending"
    log.write_text(log.read_text() + log_lines({"kind": "written", "seq": 2}))
    assert frame_status(tmp_path) == "written"
    log.write_text(log_lines(intent(1, "init"), {"kind": "written", "seq": 1}) + '{"kind": "intent", "seq": 2')
    assert frame_status(tmp_path) == "absent"                  # a torn tail was never written to the pipe
    log.write_text(log_lines(intent(1, "init"), {"kind": "written", "seq": 1}) + "garbage\n"
                   + log_lines(intent(2, "user-message"), {"kind": "written", "seq": 2}))
    assert frame_status(tmp_path) == "unreadable"              # a log read only in part proves nothing


def test_the_claude_transcript_decides_from_the_attempts_offset(tmp_path):
    """C-24.6, D-14: a `user` record with the message id as its uuid, after the attempt's
    transcript offset, in the session's transcript under the projects directory."""
    projects = tmp_path / "projects"
    notes = {"projects_dir": str(projects)}
    assert claude_record(MID, session_id=SID, notes=notes) == ("unreadable", None, False)   # no projects dir
    (projects / "-work").mkdir(parents=True)
    assert claude_record(MID, session_id=SID, notes=notes) == ("absent", None, False)
    transcript = projects / "-work" / f"{SID}.jsonl"
    earlier = json.dumps({"type": "assistant", "uuid": "other", "message": {"content": MID}}) + "\n"
    transcript.write_text(earlier)
    assert claude_record(MID, session_id=SID, notes=notes)[:1] == ("absent",)
    ours = json.dumps({"type": "user", "uuid": MID, "message": {"role": "user", "content": "hi"}}) + "\n"
    transcript.write_text(earlier + ours)
    found = claude_record(MID, session_id=SID, notes={**notes, "transcript_path": str(transcript),
                                                       "transcript_offset": len(earlier)})
    assert found == ("found", str(transcript), True)
    past = claude_record(MID, session_id=SID, notes={**notes, "transcript_path": str(transcript),
                                                      "transcript_offset": len(earlier + ours)})
    assert past[0] == "absent"                                  # before this attempt: not its delivery
    transcript.write_text(earlier + ours[:ours.index(MID) + len(MID) + 3])   # torn, naming the message
    assert claude_record(MID, session_id=SID, notes=notes)[0] == "unreadable"


def test_the_codex_rollout_decides_by_client_id(tmp_path):
    """C-24.6, D-14: a rollout record whose client_id is the message id."""
    home = tmp_path / "codex-1"
    assert codex_record(MID, home=str(home), thread_id="t-1")[0] == "unreadable"   # no sessions dir
    day = home / "sessions" / "2026" / "09" / "24"
    day.mkdir(parents=True)
    assert codex_record(MID, home=str(home), thread_id="t-1") == ("absent", None, False)
    assert codex_record(MID, home=str(home), thread_id=None) == ("absent", None, False)
    rollout = day / "rollout-2026-09-24T12-00-00-t-1.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "t-1"}}) + "\n")
    assert codex_record(MID, home=str(home), thread_id="t-1") == ("absent", str(rollout), True)
    rollout.write_text(rollout.read_text() + json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user", "client_id": MID, "content": []}}) + "\n")
    assert codex_record(MID, home=str(home), thread_id="t-1") == ("found", str(rollout), True)


def test_gather_reads_the_attempts_own_files(tmp_path):
    """C-24.6: the exit receipt, the relay log and the launch notes' transcript."""
    adir = tmp_path / "a1"
    adir.mkdir()
    projects = tmp_path / "projects"
    (projects / "-w").mkdir(parents=True)
    (adir / "launch.json").write_text(json.dumps({"notes": {"projects_dir": str(projects), "session_id": SID}}))
    (adir / "stdin.jsonl").write_text(log_lines(intent(1, "init"), {"kind": "written", "seq": 1},
                                                intent(2, "user-message"), {"kind": "written", "seq": 2}))
    evidence = gather("claude", MID, adir, {"accepted": False, "native_session_id": SID})
    assert evidence == Evidence(acknowledged=False, frame="written", process_gone=False, native="absent")
    (adir / "exit.json").write_text("{}")
    assert decide(gather("claude", MID, adir, {"native_session_id": SID})) == UNKNOWN
    (adir / "stdin.jsonl").write_text(log_lines(intent(1, "init"), {"kind": "written", "seq": 1}))
    assert decide(gather("claude", MID, adir, {"native_session_id": SID})) == NOT_DELIVERED
    assert reconcile.USER_FRAME == "user-message"
