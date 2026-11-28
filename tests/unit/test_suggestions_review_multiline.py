"""Round-2 review probes: these assert the promised behavior and expose gaps."""

from __future__ import annotations

import json
import uuid

import pytest

from subfleet.conversations import suggestions
from subfleet.conversations.store import ConversationError
from tests.unit.test_suggestion_secret_properties import (
    AGENT, PERSON, checkout, everything_shown, fixture, origin_turn, recognized,
)


@pytest.mark.parametrize("source", ["checkout hook", "start error"])
@pytest.mark.parametrize("payload", [
    'password="left-secret\n右秘密"',
    '界' * 460 + ' password="left-secret\n' + '右' * 20 + '"',
])
def test_r2b_multiline_quoted_credentials_never_reach_suggestion_transports(source, payload, monkeypatch):
    with fixture() as service:
        cid, job = origin_turn(service)
        workspace = checkout(service, payload) if source == "checkout hook" else service.folder / "work"
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture",
            "brief": "Fixture brief", "workspace": str(workspace),
            "worktree": source == "checkout hook", "calling_job": job,
        }, AGENT)["suggestion"]
        sid = row["suggestion_id"]
        if source == "start error":
            def fail(**kwargs):
                raise ConversationError("worktree-failed", payload, code=1)
            monkeypatch.setattr(service.store, "submit_message", fail)
        with pytest.raises(ConversationError) as caught:
            service.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
        shown = everything_shown(service, sid, cid, str(caught.value))
        service._resume_start(sid, 1, service.clock())
        shown["retry"] = everything_shown(service, sid, cid, str(caught.value))
        leaked = [name for name, value in shown.items() if "left-secret" in json.dumps(value, ensure_ascii=False)]
        assert not leaked, {"leaked_surfaces": leaked, "recognized": recognized(shown)}


@pytest.mark.parametrize("where", ["error", "preview"])
def test_r2b_unicode_cut_preserves_an_entire_redaction_marker(where):
    secret = "ghp_" + "A" * 36
    if where == "error":
        shown = suggestions.scrub_error("界" * 493 + " " + secret + " end")
    else:
        words = suggestions.normalize("Title", "Summary", "🧪" * 1995 + " " + secret + " end")[0]
        from collections import defaultdict
        row = defaultdict(lambda: None, dict(words, suggestion_id="sg-fixture", state="open",
                                             worktree=False, provider="claude"))
        shown = suggestions.view(row)["brief"]
    assert secret not in shown
    assert "[" not in shown.replace("[REDACTED]", ""), repr(shown[-30:])
