"""The store's state, fed with the daemon's own results (design §12, D-19, D-24;
C-26.8, C-29.6, C-29.9, C-29.11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import uuid

import pytest

from subfleet.conversations.claude_turn import observed_catalog
from subfleet.status_json import build_status
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import CLAUDE_CATALOG, ServiceHarness, claude_assistant, claude_init, claude_result

pytestmark = needs_swift


@pytest.fixture
def harness():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-st-", dir="/tmp")))
    yield harness
    harness.close()


def store(core_probe, tmp_path, steps: list[dict], now: float | None = None) -> dict:
    return run_probe(core_probe, "store", write_json(tmp_path / f"store-{uuid.uuid4().hex}.json",
                                                     {"now": now or time.time(), "steps": steps}))


def catalog_item(provider: str, prompt: str, cwd: str, mtime: float, **extra) -> dict:
    return {"provider": provider, "native_session_id": str(uuid.uuid4()), "path": f"/x/{uuid.uuid4()}.jsonl",
            "home": None, "title": None, "first_prompt": prompt, "cwd": cwd, "model": None, "permission_mode": None,
            "mtime": mtime, "continuable": True, "continue_blocker": None, "archived": False, "live_elsewhere": False,
            **extra}


def test_design_12_sidebar_groups_searches_and_filters(core_probe, tmp_path, harness):
    now = time.time()
    first = harness.create(title="Fix the parser")
    second = harness.create(title=None)
    codex = harness.create(provider="codex", settings={"model": "gpt-6-astra", "permission": "read-only"})
    other = tmp_path / "elsewhere"
    other.mkdir()
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")   # a stale one flags nothing
    (harness.root / "catalog.json").write_text(json.dumps({"generated_at": fresh, "complete": True, "items": [
        catalog_item("claude", "fix the build on main", str(other), now - 2 * 86400, live_elsewhere=True),
        catalog_item("codex", "explore the data", str(other), now - 40 * 86400, continuable=False,
                     continue_blocker="codex-app thread: continue by handoff"),
    ]}))
    listed = harness.call("conversation.list")
    out = store(core_probe, tmp_path, [{"list": listed}], now)
    sections = {s["title"]: s["entries"] for s in out["sidebar"]}
    assert list(sections) == ["Today", "Previous 7 days", "Older"]
    assert {e["title"] for e in sections["Today"]} == {"Fix the parser", "work", codex["workspace"].rsplit("/", 1)[-1]}
    native = sections["Previous 7 days"][0]
    assert native["title"] == "fix the build on main" and native["live_elsewhere"] is True
    assert native["target"]["native"]["provider"] == "claude"
    blocked = sections["Older"][0]
    assert blocked["continuable"] is False and blocked["continue_blocker"].startswith("codex-app thread")
    assert all(e["target"]["conversation"] for e in sections["Today"])

    searched = store(core_probe, tmp_path, [{"list": listed}, {"search": "FIX"}], now)
    titles = [e["title"] for s in searched["sidebar"] for e in s["entries"]]
    assert titles == ["Fix the parser", "fix the build on main"]
    codex_only = store(core_probe, tmp_path, [{"list": listed}, {"provider_filter": "codex"}], now)
    assert [e["provider"] for s in codex_only["sidebar"] for e in s["entries"]] == ["codex", "codex"]
    by_place = store(core_probe, tmp_path, [{"list": listed}, {"grouping": "workspace"}], now)
    assert [len(s["entries"]) for s in by_place["sidebar"]] == [3, 2]
    del first, second


def watch(harness, after: int) -> dict:
    return harness.call("conversation.watch", after=after)


def test_d24_the_watch_feed_notifies_for_unfocused_conversations(core_probe, tmp_path, harness):
    focused = harness.create(title="Focused")["conversation_id"]
    other = harness.create(title="Elsewhere")["conversation_id"]
    listed = harness.call("conversation.list")
    baseline = watch(harness, 0)
    quiet = watch(harness, baseline["next"])
    # Activity after launch: a turn completes in each; the unfocused one asks first.
    done = harness.submit(focused, "hello")["message_id"]
    turn = harness.attempt(focused, done)
    turn.feed(claude_init(), {"type": "user", "uuid": done, "isReplay": True, "message": {"role": "user", "content": "x"}},
              claude_assistant("m1", [{"type": "text", "text": "ok"}]), claude_result())
    asking = harness.submit(other, "run")["message_id"]
    ask = harness.attempt(other, asking)
    ask.feed(claude_init(), {"type": "user", "uuid": asking, "isReplay": True, "message": {"role": "user", "content": "x"}},
             claude_assistant("m2", [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]),
             {"type": "control_request", "request_id": "perm-1", "request": {
                 "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_1", "input": {"command": "ls"}}})
    asked = watch(harness, quiet["next"])
    ask.respond("perm-1", "deny", "no")
    ask.feed(claude_assistant("m3", [{"type": "text", "text": "Not run."}]), claude_result())
    finished = watch(harness, asked["next"])
    steps = [{"list": listed}, {"focus": focused}, {"watch": baseline}, {"watch": quiet}]
    during = store(core_probe, tmp_path, steps + [{"watch": asked}])
    after = store(core_probe, tmp_path, steps + [{"watch": asked}, {"watch": finished}])
    silent = store(core_probe, tmp_path, [{"list": listed}, {"focus": focused}, {"watch": asked}])

    assert during["watch_baselined"] is True and during["badge"] == 1
    assert [(n["kind"], n["conversation"], n["title"], n["body"]) for n in during["notifications"]] == [
        ("approval", other, "Approval needed", "Elsewhere")]
    conversations = {c["id"]: c for c in during["conversations"]}
    assert conversations[other]["pending"] == 1 and conversations[other]["active"] is True
    assert conversations[focused]["last_state"] == "complete" and conversations[focused]["active"] is False
    kinds = [(n["kind"], n["conversation"]) for n in after["notifications"]]
    assert kinds == [("approval", other), ("completed", other)]        # the focused one never notifies
    assert after["badge"] == 0
    assert silent["notifications"] == []                                # nothing before the baseline


def test_d24_a_list_refresh_keeps_a_conversation_with_a_running_turn_active(core_probe, tmp_path, harness):
    """`_view.active` is false while the newest message is queued behind a running
    turn; the watch feed knows the running one, and a refresh keeps it active."""
    cid = harness.create(title="Busy")["conversation_id"]
    running = harness.submit(cid, "long job")["message_id"]
    assert harness.store.set_state(running, "running", expect=("queued",))
    queued = harness.submit(cid, "follow-up", after=running)["message_id"]
    listed = harness.call("conversation.list")
    assert [c["active"] for c in listed["conversations"]] == [False]           # the daemon's view
    feed = watch(harness, 0)
    assert [(c["message_id"], c["state"]) for c in feed["changes"]][-2:] == [(running, "running"), (queued, "queued")]
    out = store(core_probe, tmp_path, [{"watch": feed}, {"list": listed}])
    assert [(c["id"], c["active"], c["last_state"]) for c in out["conversations"]] == [(cid, True, "queued")]
    idle = store(core_probe, tmp_path, [{"list": listed}])
    assert [c["active"] for c in idle["conversations"]] == [False]             # without the feed, the daemon's word


def test_d19_composer_options_follow_models_and_capabilities(core_probe, tmp_path, harness):
    claude = harness.create()
    codex = harness.create(provider="codex", settings={"model": "gpt-6-astra", "permission": "read-only"})
    listed = harness.call("conversation.list")
    capabilities = harness.call("capabilities")
    unobserved = store(core_probe, tmp_path, [
        {"list": listed}, {"capabilities": capabilities},
        {"models": harness.call("models.list", provider="claude"), "provider": "claude"},
        {"models": harness.call("models.list", provider="codex"), "provider": "codex"},
    ])
    options = unobserved["composer"][claude["conversation_id"]]
    assert [m["label"] for m in options["models"]] == ["Fable", "Opus", "Sonnet", "Haiku"]
    assert options["selected"] is None and options["efforts_observed"] is False
    assert options["fast_note"] == "Bills usage credits"
    assert [(p["policy"], p["enabled"], p["widens"]) for p in options["permissions"]] == [
        ("read-only", True, False), ("ask", True, False), ("accept-edits", True, True), ("bypass", True, True)]
    codex_options = unobserved["composer"][codex["conversation_id"]]
    assert codex_options["selected"] == "astra" and codex_options["efforts"] == ["ultra"]
    assert codex_options["fast_note"] == "Draws on plan limits"
    assert [(p["policy"], p["enabled"]) for p in codex_options["permissions"]] == [
        ("read-only", True), ("ask", False), ("accept-edits", False), ("bypass", False)]
    assert all(p["reason"].startswith("Codex conversations are read-only") for p in codex_options["permissions"][1:])

    # After a turn reported the account's catalog (the service's own merge into models.json).
    harness.service._on_catalog("claude", "claude-1", observed_catalog(CLAUDE_CATALOG))
    observed = store(core_probe, tmp_path, [
        {"list": listed}, {"capabilities": {**capabilities, "codex_writable": True}},
        {"models": harness.call("models.list", provider="claude"), "provider": "claude"},
        {"models": harness.call("models.list", provider="codex"), "provider": "codex"},
    ])
    options = observed["composer"][claude["conversation_id"]]
    assert {"label": "Opus", "value": "opus[1m]"} in options["models"]
    # C-26.8: ultracode is offered wherever xhigh is.
    assert options["selected"] == "opus" and options["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultracode"]
    assert options["efforts_observed"] is True and options["fast_supported"] is True
    assert all(p["enabled"] for p in observed["composer"][codex["conversation_id"]]["permissions"])


def test_c24_8_blocked_conversations_offer_the_persons_choices(core_probe, tmp_path, harness):
    unfinished = harness.create()["conversation_id"]
    unknown = harness.create()["conversation_id"]
    quarantined = harness.create()["conversation_id"]
    harness.store.update_conversation(unfinished, blocked_by="unfinished-turn")
    message = harness.submit(unknown, "hello")["message_id"]
    harness.store.set_state(message, "delivery-unknown", reason="no-evidence")
    harness.store.update_conversation(unknown, blocked_by="delivery-unknown")
    harness.store.update_conversation(quarantined, blocked_by="quarantined-turn")
    out = store(core_probe, tmp_path, [{"list": harness.call("conversation.list")},
                                       {"open": harness.call("conversation.open", conversation_id=unknown)}])
    banners = out["banners"]
    assert [c["action"] for c in banners[unfinished]["choices"]] == [{"unblock": "continue"}, {"unblock": "leave"}]
    assert [c["action"] for c in banners[unknown]["choices"]] == [
        {"resolve": "delivered", "message_id": message}, {"resolve": "not-delivered", "message_id": message}]
    assert banners[quarantined]["choices"] == [] and "quarantined" in banners[quarantined]["title"]
    assert out["stops"][message] == {"action": "none"}


def test_c26_8_the_served_chip_shows_what_served_the_turn(core_probe, tmp_path, harness):
    cid = harness.create(settings={"fast": True})["conversation_id"]
    mid = harness.call("message.submit", conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=None,
                       text="go", attachments=[], settings=harness.settings(fast=True))["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(email="served@example.invalid"),
              {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": "go"}},
              {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "permissionMode": "default",
               "fast_mode_state": "off"},
              claude_assistant("m1", [{"type": "text", "text": "ok"}]), claude_result())
    queued = harness.submit(cid, "later", after=mid)["message_id"]
    status = build_status({"lanes": [{"lane_id": "claude-1", "provider": "claude", "owner": "v2",
                                      "account_key": "claude:lane@example.invalid", "email": "lane@example.invalid"}]},
                          now=datetime.now(timezone.utc))
    out = store(core_probe, tmp_path, [
        {"list": harness.call("conversation.list")}, {"status": status},
        {"open": harness.call("conversation.open", conversation_id=cid)},
        {"events": harness.call("conversation.events", conversation_id=cid, after=0), "conversation_id": cid},
    ])
    chip = out["chips"][mid]
    assert chip["account"] == "served@example.invalid" and chip["model"] == "claude-opus-5-5"
    assert chip["fast"] == "off" and chip["warnings"] == ["Fast was asked for; this turn ran at standard speed"]
    assert out["stops"][queued] == {"action": "cancel", "message_id": queued}
    assert out["stops"][mid] == {"action": "none"}


def test_c26_8_a_codex_chip_names_the_lane_account_from_status(core_probe, tmp_path, harness):
    """Codex reports no account; the lane's label in status.json names it."""
    cid = "cv-codex"
    mid = str(uuid.uuid4())
    conversation = {"conversation_id": cid, "provider": "codex", "native_session_id": "th-1", "title": None,
                    "workspace": "/w", "workspace_kind": "in-place", "allow_main": False, "lane_id": "codex-2",
                    "settings": {"model": "gpt-6-astra", "effort": "high", "fast": False, "permission": "read-only",
                                 "auto_continue": True},
                    "origin": "new", "handoff_from": None, "blocked_by": None, "created_at": "2026-09-24T10:00:00.000Z",
                    "updated_at": "2026-09-24T10:00:00.000Z", "last_message": None, "pending_approvals": 0,
                    "active": True}
    receipt = {"message_id": mid, "conversation_id": cid, "seq": 1, "origin": "person", "state": "running",
               "settings": conversation["settings"], "served": None, "stop_requested": False}
    events = {"events": [{"seq": 1, "message_id": mid, "kind": "served", "ts": "2026-09-24T10:00:01.000Z",
                          "data": {"model": "gpt-6-astra", "effort": "high", "service_tier": None, "sandbox": None,
                                   "approval_policy": "never", "native_session_id": "th-1"}}], "next": 1, "reset": False}
    served = {"events": [{"seq": 2, "message_id": mid, "kind": "served", "ts": None, "data": {"lane_id": "codex-2"}}],
              "next": 2, "reset": False}
    status = build_status({"lanes": [{"lane_id": "codex-2", "provider": "codex", "owner": "v2", "home": "/h/2",
                                      "account_key": "codex:fake-2", "email": "codex-two@example.invalid"}]},
                          now=datetime.now(timezone.utc))
    out = store(core_probe, tmp_path, [
        {"list": {"conversations": [conversation]}}, {"status": status},
        {"open": {"conversation": conversation, "messages": [receipt], "events_cursor": 0, "pending_approvals": []}},
        {"events": events, "conversation_id": cid}, {"events": served, "conversation_id": cid},
    ])
    assert out["chips"][mid] == {"account": "codex-two@example.invalid", "model": "gpt-6-astra", "effort": "high",
                                 "fast": "off", "warnings": []}
    assert out["stops"][mid] == {"action": "interrupt", "message_id": mid}


def test_design_9_staged_images_and_drafts_are_private(core_probe, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                                  "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4a50000000049454e44ae426082"))
    caches = tmp_path / "caches" / "attachments"
    staged = run_probe(core_probe, "stage", png, caches)
    import hashlib
    digest = hashlib.sha256(png.read_bytes()).hexdigest()
    assert staged["staged"] == {"path": str(caches / f"{digest}.png"), "sha256": digest, "media_type": "image/png",
                                "bytes": png.stat().st_size}
    assert staged["mode"] == "600" and staged["directory_mode"] == "700"
    text = tmp_path / "note.txt"
    text.write_text("not an image")
    assert "notAnImage" in run_probe(core_probe, "stage", text, caches)["error"]
    drafts = run_probe(core_probe, "drafts", tmp_path / "support" / "drafts", "cv-123")
    assert drafts["path"].endswith("/drafts/cv-123.json") and drafts["mode"] == "600"
    assert drafts["round_trip"] is True and drafts["deleted"] is True
    odd = run_probe(core_probe, "drafts", tmp_path / "support" / "drafts", "../../etc/passwd")
    assert "/drafts/" in odd["path"] and ".." not in Path(odd["path"]).name


def test_c26_3_a_hold_reaches_the_live_strip_through_the_change_feed(core_probe, tmp_path, harness, monkeypatch):
    """C-26.3, design §12: a message held for another writer says so on a live
    timeline, from the watch feed alone (no second conversation.open)."""
    from subfleet.conversations import catalog as catalog_module
    cid = harness.create()["conversation_id"]
    harness.store.update_conversation(cid, native_session_id="s-held")
    mid = harness.submit(cid, "go on")["message_id"]
    opened = harness.call("conversation.open", conversation_id=cid)
    baseline = harness.call("conversation.watch", after=0)
    monkeypatch.setattr(catalog_module, "external_writers", lambda sid: [4242] if sid == "s-held" else [])
    harness.service._dispatch()
    held = harness.call("conversation.watch", after=baseline["next"])
    assert [(c["state"], c["state_reason"]) for c in held["changes"]] == [("waiting", "external-writer: pid 4242")]
    out = store(core_probe, tmp_path, [{"open": opened}, {"watch": baseline}, {"watch": held}])
    assert out["statuses"][mid] == "Waiting: open in the Claude app or a terminal; close it there to continue here"
