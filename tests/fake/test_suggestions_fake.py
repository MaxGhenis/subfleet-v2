"""Agent turn -> `subfleet suggest` -> card -> person's Start -> worktree conversation -> its first turn.

The real CLI handler (its request goes to the real service instead of a socket),
conversation service, stores, dispatch and Claude driver with scripted provider
frames; the person's peer check is the one stand-in (C-25.6 is tested by
`tests/unit/test_suggestions.py` and, from a terminal, by the e2e test). This
runs inside a Subfleet lane, where the e2e test's person half skips.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from subfleet import cli
from subfleet.conversations import suggestions
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.peers import Verdict
from subfleet.conversations.store import ConversationError
from subfleet.conversations.turn import TurnSpec
from tests.frontend.daemon_harness import claude_init
from tests.unit.test_conversation_service import conversation, git, repo, svc  # noqa: F401

PERSON, AGENT = 1, 2
BRIEF = "Port the importer to the new manifest.\n\nRun the importer tests and report what changed.\n"


def provider_frames(svc, mid: str) -> list[dict]:
    """What a fake Claude reads for message `mid`'s turn: the real driver's frames
    after `initialize` (the first is the user message)."""
    message = svc.store.message(mid)
    conversation = svc.store.conversation(message["conversation_id"])
    driver = ClaudeTurn(TurnSpec(provider="claude", message_id=mid, text=svc.store.message_text(message),
                                 native_session_id=None, new_session_id=str(uuid.uuid4()), model_id="opus[1m]",
                                 permission=message["settings"]["permission"], cwd=conversation["workspace"]),
                        read_bytes=lambda image: b"")
    driver.start()
    step = driver.feed(json.dumps(claude_init()), 0)
    return [json.loads(frame.line) for frame in step.frames if frame.line]


@pytest.fixture
def turn(svc, repo, monkeypatch):
    """A conversation in a git checkout with one running turn whose Bash tool runs
    `subfleet suggest`: the CLI's environment is the turn's (C-26.13)."""
    cid = conversation(svc, workspace=str(repo))
    mid = svc.op_message_submit({"conversation_id": cid, "message_id": str(uuid.uuid4()),
                                 "text": "Fix the exporter"}, None)["message_id"]
    svc._dispatch()
    job = svc.store.message(mid)["job_id"]
    svc.store.set_state(mid, "running")
    monkeypatch.setenv("SUBFLEET_TURN_JOB", job)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-turns-session")
    monkeypatch.chdir(repo)

    def call(self, op, args=None, **kwargs):
        try:
            return svc.handle(op, args or {}, AGENT)
        except ConversationError as exc:
            raise cli.DaemonError(exc.code, f"{exc.reason}: {exc}", exc.fix) from None
    monkeypatch.setattr(cli.Client, "call", call)
    return cid, mid


def test_c25_7_a_turns_suggestion_is_started_by_a_person_into_a_worktree_conversation(svc, repo, turn, tmp_path,
                                                                                         capsys):
    cid, mid = turn
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF)
    # The agent, in its turn's Bash tool.
    args = cli.build_parser().parse_args(["suggest", "--title", "Port the importer", "--tldr",
                                          "Out of scope for this turn; worth its own session",
                                          "--brief-file", str(brief), "--worktree", "--json"])
    assert args.handler(args) == 0
    made = json.loads(capsys.readouterr().out)
    sid = made["suggestion"]["suggestion_id"]
    assert (made["suggestion"]["origin_conversation_id"], made["suggestion"]["origin_message_id"]) == (cid, mid)
    assert made["suggestion"]["creator_session"] == "the-turns-session"
    assert Path(made["suggestion"]["workspace"]) == repo.resolve()
    # Where it starts was decided now, and the card shows it: a worktree of the checkout.
    assert made["suggestion"]["workspace_kind"] == "worktree" and made["suggestion"]["repository"] == str(repo.resolve())

    # The app-facing ops see it: the list, the watch feed, the origin's events.
    assert [s["suggestion_id"] for s in svc.handle("suggestion.list", {}, PERSON)["suggestions"]] == [sid]
    fed = [c for c in svc.handle("conversation.watch", {"after": 0}, PERSON)["changes"] if c.get("suggestion_id")]
    assert [(c["conversation_id"], c["suggestion"]["state"]) for c in fed] == [(cid, "open")]
    card = [e for e in svc.handle("conversation.events", {"conversation_id": cid, "after": 0}, PERSON)["events"]
            if e["kind"] == "suggestion"]
    assert [(e["message_id"], e["data"]["state"], e["data"]["brief"]) for e in card] == [(mid, "open", BRIEF)]

    # The agent cannot start it, and neither can the CLI it runs.
    with pytest.raises(ConversationError) as refused:
        svc.handle("suggestion.start", {"suggestion_id": sid}, AGENT)
    assert refused.value.reason == "person-only"

    # The person's Start, from the app.
    svc._person = lambda peer, what: Verdict(peer == PERSON, "the Subfleet app", peer)
    out = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    new = out["conversation"]
    assert new["workspace_kind"] == "worktree" and new["worktree"]["branch"] == f"subfleet/{new['conversation_id']}"
    assert git(Path(new["worktree"]["path"]), "rev-parse", "--abbrev-ref", "HEAD") == f"subfleet/{new['conversation_id']}"
    first = out["message"]["message_id"]
    assert first == out["suggestion"]["started_message_id"] == svc.suggestions.get(sid)["start_message_id"]
    assert out["message"]["text"] == BRIEF

    # Its first turn: dispatched in the worktree, and the provider reads the brief.
    svc._dispatch()
    submitted = svc.daemon.submits[-1]
    assert submitted.request_id == f"turn:{first}:0" and submitted.workdir == new["workspace"]
    frames = provider_frames(svc, first)
    user = next(frame for frame in frames if frame.get("type") == "user")
    assert user["uuid"] == first
    content = user["message"]["content"]
    text = content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
    assert text == BRIEF

    # The origin's card ends started, naming the new conversation.
    card = [e for e in svc.handle("conversation.events", {"conversation_id": cid, "after": 0}, PERSON)["events"]
            if e["kind"] == "suggestion"]
    assert [e["data"]["state"] for e in card] == ["open", "starting", "started"]
    assert card[-1]["data"]["started_conversation_id"] == new["conversation_id"]
    listed = svc.handle("suggestion.list", {}, PERSON)
    assert listed["open"] == 0 and listed["suggestions"][0]["state"] == "started"
