"""C-26.12, C-17.1 (review IR-19): a turn job is its conversation's, not detached work.

`list` (and so `runs`, `wait --mine`, `wait --last`) leaves turn jobs out unless
the request asks for them; `why` names a turn as its conversation's; a turn job
writes no notice. Turn rows are inserted as the dispatcher writes them (design
§3: kind `turn`, request id `turn:<message>:<n>`, name `turn-<conversation>`),
so these cases need no provider.
"""

from __future__ import annotations

import pytest

from subfleet import protocol
from tests.fake.test_state_contract import state_daemon  # noqa: F401  (fixture)

CONVERSATION = "cv-1790000000000-0123456789ab"


def add(daemon, job_id, *, kind="dispatch", state="succeeded", created_at, session="sess-1", **extra):
    fields = {"job_id": job_id, "request_id": f"rq-{job_id}", "payload_digest": "d", "kind": kind, "state": state,
              "workdir": str(daemon.root), "prompt_path": str(daemon.root / "prompt.md"), "sandbox": "read-only",
              "caller_session": session, "created_at": created_at}
    if kind == "turn":
        fields.update(request_id=f"turn:{job_id}:0", name=f"turn-{CONVERSATION}", in_place=1, max_attempts=1)
    if state in ("succeeded", "failed", "cancelled", "lost"):
        fields.update(finished_at=created_at, rc=0 if state == "succeeded" else 1)
    daemon.store.add_job({**fields, **extra})
    return job_id


def listed(daemon, **args) -> list[str]:
    return [row["job_id"] for row in daemon.dispatch("list", args)["jobs"]]


@pytest.fixture
def ledger(state_daemon):  # noqa: F811
    daemon, _ = state_daemon
    add(daemon, "j-old", created_at="2026-09-24T10:00:00Z")
    add(daemon, "j-resume", kind="resume", created_at="2026-09-24T10:01:00Z")
    # Newest of all, and even carrying the caller's session: a filter, not luck.
    add(daemon, "t-turn", kind="turn", created_at="2026-09-24T10:02:00Z")
    add(daemon, "t-live", kind="turn", state="queued", created_at="2026-09-24T10:03:00Z", session=None)
    return daemon


def test_c26_12_list_leaves_turn_jobs_out_unless_asked(ledger):
    """C-26.12, C-16.2 (IR-19): the default ledger is detached work; `kind` and `include_turns` ask for more."""
    assert listed(ledger) == ["j-resume", "j-old"]
    assert listed(ledger, running=True) == []
    assert listed(ledger, mine="sess-1") == ["j-resume", "j-old"]
    assert listed(ledger, kind="turn") == ["t-live", "t-turn"]
    assert listed(ledger, kind="turn", running=True) == ["t-live"]
    assert listed(ledger, kind="resume") == ["j-resume"]
    assert listed(ledger, include_turns=True) == ["t-live", "t-turn", "j-resume", "j-old"]
    assert listed(ledger, include_turns=True, last=1) == ["t-live"]
    rows = ledger.dispatch("list", {"kind": "turn"})["jobs"]
    assert {row["kind"] for row in rows} == {"turn"}


@pytest.mark.parametrize("args", [{"kind": ""}, {"kind": 7}, {"include_turns": "yes"}])
def test_c26_12_a_malformed_kind_filter_is_invalid_input(ledger, args):
    """C-16.2, C-17.3 a filter the daemon cannot read is exit 2, never an unfiltered answer."""
    with pytest.raises(protocol.ProtocolError) as error:
        ledger.dispatch("list", args)
    assert error.value.code == 2


def test_c26_12_wait_last_and_mine_never_resolve_to_a_turn(ledger):
    """C-26.12, C-15.4 (IR-19): `wait --last` and `wait --mine` resolve through `list`, so a newer turn
    job never becomes the job a caller waits on."""
    last = ledger.dispatch("wait", {"last": True, "deadline_s": 0})
    assert [job["job_id"] for job in last["jobs"]] == ["j-resume"]
    mine = ledger.dispatch("wait", {"mine": "sess-1", "deadline_s": 0})
    assert sorted(job["job_id"] for job in mine["jobs"]) == ["j-old", "j-resume"]
    named = ledger.dispatch("wait", {"job_ids": ["t-turn"], "deadline_s": 0})
    assert [job["job_id"] for job in named["jobs"]] == ["t-turn"]            # an explicit id is honoured


def test_c26_12_why_names_a_turn_as_its_conversations(ledger):
    """C-26.12, C-6.11 (IR-19): `why` on a turn says whose it is instead of presenting it as a job."""
    result = ledger.dispatch("why", {"job_id": "t-live"})
    assert result["job"]["kind"] == "turn" and result["job"]["conversation_id"] == CONVERSATION
    assert result["text"].splitlines()[0] == f"Conversation turn: t-live of {CONVERSATION} is queued"
    assert "not to a notice or a deliverable" in result["text"]
    detached = ledger.dispatch("why", {"job_id": "j-old"})
    assert detached["job"]["kind"] == "dispatch" and detached["job"]["conversation_id"] is None
    assert detached["text"].startswith("Job: j-old is succeeded")


def test_c26_12_cancelling_a_turn_job_writes_no_notice(ledger):
    """C-26.12, C-15.1 (IR-17, IR-19): the conversation carries a turn's end; `notice.pending` never
    offers one, while a detached job cancelled the same way still gets its notice."""
    add(ledger, "j-queued", state="queued", created_at="2026-09-24T10:04:00Z")
    add(ledger, "t-queued", kind="turn", state="queued", created_at="2026-09-24T10:05:00Z", session="sess-1")
    for job_id in ("j-queued", "t-queued"):
        assert ledger.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    assert [row["job_id"] for row in ledger.store.query("SELECT job_id FROM notices")] == ["j-queued"]
    pending = ledger.dispatch("notice.pending", {"session_id": "sess-1"})["notices"]
    assert [row["job_id"] for row in pending] == ["j-queued"]


def test_c25_1_capabilities_advertise_the_kind_filter(state_daemon):  # noqa: F811
    """C-25.1, C-26.12 the daemon that honours `kind`/`include_turns` advertises `jobs.kind.v1`. The client
    half (`runs` sends the fields only after reading it) is `unit/test_cli_turns.py`."""
    daemon, _ = state_daemon
    assert "jobs.kind.v1" in daemon.conversations.handle("capabilities", {}, None)["capabilities"]
