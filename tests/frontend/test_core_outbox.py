"""The outbox (design D-22; C-24.2, C-28.3): order, retries by key, restarts, withdrawal.

The Swift outbox talks over a real AF_UNIX socket to the daemon's own
conversation service and store (`daemon_harness.ServiceServer`), which can lose
one answer ("drop": handled, no answer) or one request ("refuse": never
handled). What the store holds afterwards is read directly.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer

pytestmark = needs_swift


@pytest.fixture
def daemon():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-ob-", dir="/tmp")))
    server = ServiceServer(harness)
    yield harness, server
    server.close()
    harness.close()


def run_steps(core_probe, tmp_path, server, steps, journal: Path | None = None) -> dict:
    journal = journal or tmp_path / "support" / "outbox.json"
    return run_probe(core_probe, "outbox", server.path, journal, write_json(tmp_path / f"steps-{uuid.uuid4().hex}.json", steps),
                     timeout=120)


def ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def entries(out: dict) -> dict:
    return {entry["key"]: entry for entry in out["entries"]}


def create_step(harness, request_id: str) -> dict:
    return {"do": "create", "request_id": request_id, "workspace": str(harness.workspace)}


def person_rows(harness, cid: str) -> list[dict]:
    return harness.store.query("SELECT message_id, seq, after_message_id, origin, state, state_reason FROM messages "
                               "WHERE conversation_id=? ORDER BY seq", (cid,))


def test_c28_3_sends_are_journaled_and_chained_in_order(core_probe, tmp_path, daemon):
    harness, server = daemon
    m1, m2, m3 = ids(3)
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-1"),
        *({"do": "submit", "conversation": "@draft:req-1", "message_id": m, "text": f"message {i}"}
          for i, m in enumerate((m1, m2, m3))),
        {"do": "pump"},
    ])
    report = out["results"][-1]["report"]
    assert report["acknowledged"] == ["req-1", m1, m2, m3]
    assert out["calls"] == ["conversation.create req-1 answered",
                            f"message.submit {m1} after=null answered",
                            f"message.submit {m2} after={m1} answered",
                            f"message.submit {m3} after={m2} answered"]
    cid = entries(out)["req-1"]["conversation_id"]
    assert all(entries(out)[m]["conversation"] == cid for m in (m1, m2, m3))
    rows = person_rows(harness, cid)
    assert [(r["message_id"], r["seq"], r["after_message_id"]) for r in rows] == [(m1, 1, None), (m2, 2, m1), (m3, 3, m2)]
    assert out["chains"] == {cid: m3}
    assert out["journal_mode"] == "600" and out["directory_mode"] == "700"


def test_d22_a_lost_answer_is_retried_with_the_same_key(core_probe, tmp_path, daemon):
    harness, server = daemon
    m1, m2 = ids(2)
    server.faults[("message.submit", m2)] = "drop"
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-2"),
        {"do": "submit", "conversation": "@draft:req-2", "message_id": m1, "text": "one"},
        {"do": "submit", "conversation": "@draft:req-2", "message_id": m2, "text": "two"},
        {"do": "pump"}, {"do": "sendable"}, {"do": "advance", "seconds": 1}, {"do": "sendable"}, {"do": "pump"},
    ])
    first, before, after, second = out["results"][3], out["results"][4], out["results"][6], out["results"][7]
    assert first["report"]["acknowledged"] == ["req-2", m1] and first["report"]["retrying"] == [m2]
    assert before["keys"] == [] and after["keys"] == [m2]          # a 0.5 s backoff first
    assert second["report"]["acknowledged"] == [m2]
    entry = entries(out)[m2]
    assert entry["attempts"] == 2 and entry["receipt"]["created"] is False and entry["receipt"]["seq"] == 2
    cid = entry["conversation"]
    assert [r["message_id"] for r in person_rows(harness, cid)] == [m1, m2]      # stored once


def test_c28_3_a_restart_resends_what_was_under_way(core_probe, tmp_path, daemon):
    harness, server = daemon
    m1, m2, m3 = ids(3)
    journal = tmp_path / "support" / "outbox.json"
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-3"),
        {"do": "submit", "conversation": "@draft:req-3", "message_id": m1, "text": "one"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-3", "message_id": m2, "text": "two"},
        {"do": "send-then-crash", "key": m2},
    ], journal)
    assert entries(out)[m2]["state"] == "sending"          # the journal says so; the daemon has it
    # A new process: the journal is read again.
    out = run_steps(core_probe, tmp_path, server, [
        {"do": "reload"}, {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-3", "message_id": m3, "text": "three"},
        {"do": "begin", "key": m3},
        {"do": "reload"}, {"do": "pump"},
    ], journal)
    assert out["results"][0]["states"] == ["acknowledged", "acknowledged", "queued"]
    assert out["results"][1]["report"]["acknowledged"] == [m2]
    assert entries(out)[m2]["receipt"]["created"] is False and entries(out)[m2]["attempts"] == 2
    assert out["results"][5]["report"]["acknowledged"] == [m3]
    assert entries(out)[m3]["receipt"]["created"] is True and entries(out)[m3]["last_after"] == m2
    cid = entries(out)[m1]["conversation"]
    assert [r["seq"] for r in person_rows(harness, cid)] == [1, 2, 3]


def test_c24_2_out_of_order_rereads_the_conversation_and_resends(core_probe, tmp_path, daemon):
    harness, server = daemon
    m1, m2, stale = ids(3)
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-4"),
        {"do": "submit", "conversation": "@draft:req-4", "message_id": m1, "text": "one"},
        {"do": "pump"},
        {"do": "know_chain", "conversation": "@conv:req-4", "last": stale},
        {"do": "submit", "conversation": "@conv:req-4", "message_id": m2, "text": "two"},
        {"do": "pump"}, {"do": "advance", "seconds": 1}, {"do": "pump"},
    ])
    cid = entries(out)[m1]["conversation"]
    refused, resent = out["results"][5]["report"], out["results"][7]["report"]
    assert refused["retrying"] == [m2] and refused["resynced"] == [cid]
    assert resent["acknowledged"] == [m2]
    assert out["calls"][-3:] == [f"message.submit {m2} after={stale} answered", "conversation.open answered",
                                 f"message.submit {m2} after={m1} answered"]
    assert entries(out)[m2]["last_after"] == m1 and entries(out)[m2]["waiting_for_predecessor"] is False


def test_c24_2_a_refused_message_holds_the_next_until_the_person_decides(core_probe, tmp_path, daemon):
    """A message the daemon refuses on its merits is not skipped: the next one
    waits. Withdrawing it (a tombstone) lets the next go, after the last person message."""
    harness, server = daemon
    m1, m2 = ids(2)
    bypass = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "bypass", "auto_continue": True}
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-5"),
        {"do": "submit", "conversation": "@draft:req-5", "message_id": m1, "text": "wider", "settings": bypass},
        {"do": "submit", "conversation": "@draft:req-5", "message_id": m2, "text": "next"},
        {"do": "pump"}, {"do": "sendable"},
        {"do": "retry", "key": m1}, {"do": "pump"},
        {"do": "withdraw", "key": m1}, {"do": "sendable"}, {"do": "pump"},
    ])
    first = out["results"][3]["report"]
    assert first["acknowledged"] == ["req-5"] and first["failed"] == [m1]
    assert entries(out)[m1]["failure"]["reason"] == "settings-mismatch"
    assert out["results"][4]["keys"] == []                           # m2 waits behind the refusal
    assert out["results"][6]["report"]["failed"] == [m1]             # retried unchanged, refused again
    withdrawn = out["results"][7]["result"]
    assert withdrawn["outcome"] == "withdrawn" and withdrawn["receipt"]["state_reason"] == "withdrawn-before-receipt"
    assert out["results"][8]["keys"] == [m2]
    assert out["results"][9]["report"]["acknowledged"] == [m2] and entries(out)[m2]["last_after"] is None
    cid = entries(out)[m2]["conversation"]
    rows = person_rows(harness, cid)
    assert [(r["message_id"], r["origin"], r["state"]) for r in rows] == [(m1, "tombstone", "cancelled"),
                                                                          (m2, "person", "queued")]


def test_d22_withdraw_only_after_the_daemon_says_it_never_had_it(core_probe, tmp_path, daemon):
    harness, server = daemon
    local, lost, landed, after = ids(4)
    server.faults[("message.submit", lost)] = "refuse"          # never reached the handler
    server.faults[("message.submit", landed)] = "drop"          # stored, answer lost
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-6"), {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-6", "message_id": local, "text": "not sent"},
        {"do": "withdraw", "key": local},
        {"do": "submit", "conversation": "@conv:req-6", "message_id": lost, "text": "lost"},
        {"do": "pump"}, {"do": "withdraw", "key": lost},
        {"do": "raw-submit", "key": lost},
        {"do": "submit", "conversation": "@conv:req-6", "message_id": landed, "text": "landed"},
        {"do": "pump"}, {"do": "withdraw", "key": landed},
        {"do": "submit", "conversation": "@conv:req-6", "message_id": after, "text": "after"},
        {"do": "pump"},
    ])
    results = out["results"]
    assert results[3]["result"] == {"outcome": "withdrawn", "receipt": None}
    assert not any(local in call for call in out["calls"])           # never sent, never asked about
    tomb = results[6]["result"]
    assert tomb["outcome"] == "withdrawn" and tomb["receipt"]["state"] == "cancelled"
    assert [c.split(" ")[0] for c in out["calls"] if lost in c or "message.status" in c or "message.cancel" in c][:3] == [
        "message.submit", "message.status", "message.cancel"]
    late = results[7]["receipt"]
    assert late["state"] == "cancelled" and late["created"] is False        # a late copy cannot land
    in_daemon = results[10]["result"]
    assert in_daemon["outcome"] == "in-daemon" and in_daemon["receipt"]["state"] == "queued"
    assert entries(out)[landed]["state"] == "acknowledged"
    assert results[12]["report"]["acknowledged"] == [after] and entries(out)[after]["last_after"] == landed
    cid = entries(out)[after]["conversation"]
    assert [(r["message_id"], r["origin"]) for r in person_rows(harness, cid)] == [
        (lost, "tombstone"), (landed, "person"), (after, "person")]


def test_d22_a_conversation_withdrawn_before_it_was_sent_takes_its_messages(core_probe, tmp_path, daemon):
    harness, server = daemon
    (m1,) = ids(1)
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-7"),
        {"do": "submit", "conversation": "@draft:req-7", "message_id": m1, "text": "first"},
        {"do": "withdraw", "key": "req-7"}, {"do": "pump"},
    ])
    assert entries(out)["req-7"]["state"] == "withdrawn" and entries(out)[m1]["state"] == "withdrawn"
    assert out["calls"] == [] and server.requests == []


def test_d22_a_message_waits_for_its_conversation(core_probe, tmp_path, daemon):
    """Until the create is acknowledged, its messages are not sendable; an
    unavailable daemon keeps everything queued."""
    harness, server = daemon
    (m1,) = ids(1)
    server.faults[("conversation.create", None)] = "refuse"
    out = run_steps(core_probe, tmp_path, server, [
        create_step(harness, "req-8"),
        {"do": "submit", "conversation": "@draft:req-8", "message_id": m1, "text": "first"},
        {"do": "pump"}, {"do": "sendable"}, {"do": "advance", "seconds": 1}, {"do": "pump"},
    ])
    assert out["results"][2]["report"]["retrying"] == ["req-8"] and out["results"][3]["keys"] == []
    assert out["results"][5]["report"]["acknowledged"] == ["req-8", m1]
