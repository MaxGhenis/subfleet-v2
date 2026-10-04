"""A title reaches the sidebar through the watch feed, before a list refresh."""

import time

from subfleet.conversations.store import TitleUpdate
from tests.frontend.test_core_store import harness, store  # noqa: F401


def test_first_title_and_rename_arrive_on_watch(core_probe, tmp_path, harness):
    conversation = harness.create()
    cid = conversation["conversation_id"]
    listed = harness.call("conversation.list")
    baseline = harness.call("conversation.watch", after=0)
    message = harness.submit(cid, "Please fix Parser.swift decoding errors; preserve identifiers")
    fallback = harness.call("conversation.watch", after=baseline["next"])
    steps = [{"list": listed}, {"watch": baseline}, {"watch": fallback}]

    def sidebar_title():
        result = store(core_probe, tmp_path, steps)
        return next(entry["title"] for section in result["sidebar"] for entry in section["entries"]
                    if entry["id"] == "cv:" + cid)

    assert sidebar_title() == "Parser.swift decoding errors"
    now = time.time()
    # The runner's own path (titles.py): the claim rides the batch that records the first
    # turn's result, and the provider's answer a later batch.
    claim, answer = TitleUpdate(claim_at=now), TitleUpdate(answer=("Parser.swift decoding", now + 1))
    for n, title in enumerate((claim, answer)):
        harness.store.append_events(conversation_id=cid, message_id=message["message_id"], attempt_id="job/a1",
                                    events=[], stdout_offset=n, stdin_seq=n, title=title)
    assert claim.claimed and answer.recorded
    generated = harness.call("conversation.watch", after=fallback["next"])
    steps.append({"watch": generated})
    assert sidebar_title() == "Parser.swift decoding"
    assert generated["changes"][-1]["title_source"] == "generated"

    renamed = harness.call("conversation.rename", conversation_id=cid, title="My parser work")
    assert renamed["conversation"]["title_source"] == "person"
    steps.append({"watch": harness.call("conversation.watch", after=generated["next"])})
    assert sidebar_title() == "My parser work"
