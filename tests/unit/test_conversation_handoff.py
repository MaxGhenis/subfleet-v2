"""The dispatcher's claim and the withdrawal guard of message.cancel: C-24.7
(review IR-2).

The service runs here against a real conversation store and a real job store
(`subfleet.store.Store`) in a temporary state root, with a stand-in daemon that
has no control loop: each test drives the dispatcher step it is about, so an
interleaving that is a race in the daemon is a fixed order here. The Claude
transcripts are the sessions kit's fixtures under `SUBFLEET_CLAUDE_DIR`.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.conversations import service as service_module
from subfleet.conversations.service import CLAIMED, ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.store import Store
from tests import sessions_fixtures as fx
from tests.conftest import make_lane

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
ASK = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}


class Daemon:
    """The daemon seams the service calls, without a control loop."""

    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root / "state.sqlite3")
        self.store.put_lane(make_lane("claude-1"))
        self.policy = fx.policy()
        self.log = logging.getLogger("test-handoff")
        self.lane_runs: list[str] = []

    def _notify(self) -> None:
        pass

    def _lane_session_ids(self) -> list[str]:
        return list(self.lane_runs)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))         # no real ~/.codex or ~/.claude
    home = fx.claude_home(tmp_path, monkeypatch)
    workspace = tmp_path / "work"
    workspace.mkdir()
    daemon = Daemon(tmp_path / "state")
    service = ConversationService(daemon)
    yield type("World", (), {"home": home, "workspace": workspace, "daemon": daemon, "service": service,
                             "store": service.store})
    service.close()
    daemon.store.close()


def source(world, *texts: str, native: str | None = SESSION) -> tuple[str, list[str]]:
    """A Claude conversation bound to a fixture transcript, with queued messages."""
    if native:
        fx.transcript(world.home, native, [
            fx.typed_prompt("Port the ledger importer to v2.", uuid="p0", at=fx.ago(3600)),
            fx.assistant_text("the manifest is done", uuid="a0", at=fx.ago(60))], cwd=str(world.workspace))
    conversation, _ = world.store.create_conversation(
        provider="claude", workspace=str(world.workspace), workspace_kind="in-place", settings=ASK, origin="native",
        native_session_id=native, title="ledger importer")
    cid, ids, after = conversation["conversation_id"], [], None
    for text in texts:
        mid = str(uuid.uuid4())
        world.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=after, text=text,
                                   attachments=[], settings=ASK)
        ids.append(mid)
        after = mid
    return cid, ids


def turn_job(world, mid: str, cid: str, *, state: str = "queued") -> str:
    message = world.store.message(mid)
    job_id = f"job-{mid[:8]}"
    world.daemon.store.add_job(job_id=job_id, request_id=f"turn:{mid}:{message['turn_seq']}",
                               payload_digest=message["digest"], kind="turn", state=state, workdir=str(world.workspace),
                               prompt_path=message["text_path"], sandbox="read-only", name=f"turn-{cid}")
    return job_id


# --- the dispatcher's claim (C-24.7) ------------------------------------------------

def test_a_withdrawal_and_the_dispatcher_cannot_both_win(world, monkeypatch):
    """C-24.7, IR-2: the dispatcher claims a queued message (`waiting`, reason
    `dispatching`) before it creates the job. While the job is being created a
    cancel is refused ("dispatching"), never reported done for a message that is
    about to get a job; once the job exists, the job store's guard decides."""
    cid, (mid,) = source(world, "hello")
    seen = {}

    def submit(conversation, message):
        claimed = world.store.message(mid)
        seen["claim"] = (claimed["state"], claimed["state_reason"])
        with pytest.raises(ConversationError) as err:
            world.service.op_message_cancel({"message_id": mid}, None)
        seen["cancel"] = err.value.reason
        return world.daemon.store.get_job(turn_job(world, mid, cid))

    monkeypatch.setattr(world.service, "_submit_turn", submit)
    world.service._dispatch()
    assert seen == {"claim": ("waiting", CLAIMED), "cancel": "dispatching"}
    bound = world.store.message(mid)
    assert bound["state"] == "waiting" and bound["job_id"] and bound["state_reason"] is None
    receipt = world.service.op_message_cancel({"message_id": mid}, None)
    assert receipt["state"] == "cancelled"
    assert world.daemon.store.get_job(bound["job_id"])["state"] == "cancelled"


def test_a_deferred_submit_puts_the_claim_back_and_waits(world, monkeypatch):
    """C-24.7: a submit refused before any provider saw the message leaves it
    `queued` (withdrawable) and is not retried on every tick."""
    cid, (mid,) = source(world, "hello")
    calls = []

    def refuse(conversation, message):
        calls.append(message["message_id"])
        raise AdapterError("could not inspect the workdir")

    monkeypatch.setattr(world.service, "_submit_turn", refuse)
    world.service._dispatch()
    world.service._dispatch()
    assert calls == [mid]
    assert world.store.message(mid)["state"] == "queued"
    assert world.service.op_message_cancel({"message_id": mid}, None)["state"] == "cancelled"


def test_a_claim_interrupted_by_a_crash_is_bound_or_submitted_again(world, monkeypatch):
    """Design §4's repair: a claimed message whose job exists is bound; one whose
    job was never created is submitted again."""
    cid, (first,) = source(world, "hello")
    world.store.set_state(first, "waiting", reason=CLAIMED, expect=("queued",))
    job_id = turn_job(world, first, cid)
    monkeypatch.setattr(world.service, "_submit_turn", lambda *a: pytest.fail("the job exists"))
    world.service._dispatch()
    assert world.store.message(first)["job_id"] == job_id

    other, (second,) = source(world, "again", native=None)
    world.store.set_state(second, "waiting", reason=CLAIMED, expect=("queued",))
    monkeypatch.setattr(world.service, "_submit_turn",
                        lambda conversation, message: world.daemon.store.get_job(turn_job(world, second, other)))
    monkeypatch.setattr(service_module.ConversationService, "_previous_released", lambda *a: True)
    world.service._dispatch()
    assert world.store.message(second)["job_id"] == f"job-{second[:8]}"
