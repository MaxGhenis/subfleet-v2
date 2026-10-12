"""Clicking a notification (C-29.9, design D-24; C-27.5).

A notification the app posts carries its conversation and its kind. Clicking it
opens the main window on that conversation, and an approval's brings the
conversation's oldest waiting card into view. One that arrives while the app is
frontmost shows as it would in the background, unless its conversation is on
screen by then: focused, with the main window visible. Until 2026-10-12 the
delegate was set after launch ended and had no `willPresent`, so the click
that launched the app never reached it, and a notification that arrived while
the app was frontmost was never shown. Which notifications are posted at all
is D-24's rule, tested in test_core_store.py.

What the app's delegate reads from a delivered notification is Foundation code
(`NotificationTarget`), probed here: through the real daemon's feed, for every
kind the app posts (also as a build before this one posted it), for the edge
cases, and over generated notifications against a reference written from the
rule above.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unicodedata
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, claude_assistant, claude_init, claude_result
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift

KINDS = ("completed", "failed", "approval", "delivery-unknown")
# How each kind's request identifier begins (`approval:<seq>`, `complete:<message>`).
PREFIXES = {"approval": "approval", "complete": "completed", "failed": "failed", "delivery-unknown": "delivery-unknown"}


def targets(core_probe, cases: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-notify-") as scratch:
        return run_probe(core_probe, "notification-targets",
                         write_json(Path(scratch) / f"{uuid.uuid4().hex}.json", {"cases": cases}))


def target(core_probe, request_id: str, user_info: dict, on_screen: str | None = None) -> dict | None:
    return targets(core_probe, [{"request_id": request_id, "user_info": user_info, "on_screen": on_screen}])["cases"][0]


@pytest.fixture
def harness():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-nt-", dir="/tmp")))
    yield harness
    harness.close()


def test_c29_9_the_feeds_notifications_open_their_conversation_and_an_approval_its_card(core_probe, tmp_path, harness):
    """Through the real daemon's feed: an approval and then a completion in a
    conversation that is not focused, each read back as its click would be."""
    focused = harness.create(title="Focused")["conversation_id"]
    other = harness.create(title="Elsewhere")["conversation_id"]
    listed = harness.call("conversation.list")
    baseline = harness.call("conversation.watch", after=0)
    quiet = harness.call("conversation.watch", after=baseline["next"])     # the app's baseline ends here
    asking = harness.submit(other, "run")["message_id"]
    ask = harness.attempt(other, asking)
    ask.feed(claude_init(), {"type": "user", "uuid": asking, "isReplay": True, "message": {"role": "user", "content": "x"}},
             claude_assistant("m1", [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]),
             {"type": "control_request", "request_id": "perm-1", "request": {
                 "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_1", "input": {"command": "ls"}}})
    asked = harness.call("conversation.watch", after=quiet["next"])
    ask.respond("perm-1", "deny", "no")
    ask.feed(claude_assistant("m2", [{"type": "text", "text": "Not run."}]), claude_result())
    finished = harness.call("conversation.watch", after=asked["next"])
    steps = [{"list": listed}, {"focus": focused}, {"watch": baseline}, {"watch": quiet}, {"watch": asked},
             {"watch": finished}]

    def store(extra: list[dict]) -> dict:
        return run_probe(core_probe, "store", write_json(tmp_path / f"store-{uuid.uuid4().hex}.json",
                                                         {"steps": steps + extra}))

    posted = store([])["notifications"]
    assert [(n["kind"], n["conversation"]) for n in posted] == [("approval", other), ("completed", other)]
    clicked = {n["kind"]: n["clicked"] for n in posted}
    assert clicked["approval"] == {"conversation": other, "kind": "approval", "reveals": True,
                                   "shows_while_frontmost": True}
    assert clicked["completed"] == {"conversation": other, "kind": "completed", "reveals": False,
                                    "shows_while_frontmost": True}
    # Once the person has that conversation on screen, one arriving late shows nothing.
    opened = store([{"focus": other}, {"on_screen": True}])["notifications"]
    assert [n["clicked"]["shows_while_frontmost"] for n in opened] == [False, False]
    # Focused while the main window is closed or miniaturized (the app still
    # frontmost): it is not on screen, so it shows.
    closed = store([{"focus": other}, {"on_screen": False}])["notifications"]
    assert [n["clicked"]["shows_while_frontmost"] for n in closed] == [True, True]
    # A locked page or no conversation focused: it shows.
    assert all(n["clicked"]["shows_while_frontmost"]
               for n in store([{"focus": None}, {"on_screen": True}])["notifications"])


def test_c29_9_every_kind_round_trips_through_delivery(core_probe):
    """Each kind's notification reads back as its own conversation and kind,
    after a property-list round trip; only an approval's reveals
    a card. One a build before this posted, with `conversation_id` alone, reads
    back the same from its request identifier."""
    intents = {i["kind"]: i for i in targets(core_probe, [])["intents"]}
    assert set(intents) == set(KINDS)
    for kind, intent in intents.items():
        expected = {"conversation": f"cv-{kind}", "kind": kind, "reveals": kind == "approval",
                    "shows_while_frontmost": True}
        assert intent["user_info"] == {"conversation_id": f"cv-{kind}", "kind": kind}
        assert intent["clicked"] == expected
        assert intent["before_kind"] == expected
    # The identifiers the feed has always used (one notification per event).
    assert {k: i["id"] for k, i in intents.items()} == {
        "approval": "approval:42", "completed": "complete:42", "failed": "failed:42",
        "delivery-unknown": "delivery-unknown:42"}


@pytest.mark.parametrize(("request_id", "kind"), [
    ("approval:17", "approval"), ("complete:m-1", "completed"), ("failed:m-1", "failed"),
    ("delivery-unknown:m-1", "delivery-unknown"),
    ("approval:", "approval"),               # an empty subject still names the kind
    ("delivery-unknown:a:b", "delivery-unknown"),
    ("completed:m-1", None),                 # the kind's name is not its prefix
    ("approval", None), ("", None), ("Approval:1", None), ("pending:1", None),
])
def test_c29_9_an_older_notification_reads_its_kind_from_its_identifier(core_probe, request_id, kind):
    assert target(core_probe, request_id, {"conversation_id": "cv-1"}) == {
        "conversation": "cv-1", "kind": kind, "reveals": kind == "approval", "shows_while_frontmost": True}


def test_c29_9_the_userinfo_kind_wins_and_an_unknown_one_falls_back(core_probe):
    assert target(core_probe, "complete:m-1", {"conversation_id": "cv-1", "kind": "approval"})["kind"] == "approval"
    assert target(core_probe, "approval:3", {"conversation_id": "cv-1", "kind": "completed"})["reveals"] is False
    # A kind a later build names, or not a string: the identifier decides.
    assert target(core_probe, "approval:3", {"conversation_id": "cv-1", "kind": "stalled"})["kind"] == "approval"
    assert target(core_probe, "failed:m", {"conversation_id": "cv-1", "kind": 7})["kind"] == "failed"
    # Neither names one: the conversation still opens, with no card to reveal.
    assert target(core_probe, "x:1", {"conversation_id": "cv-1", "kind": "stalled"}) == {
        "conversation": "cv-1", "kind": None, "reveals": False, "shows_while_frontmost": True}


@pytest.mark.parametrize("user_info", [
    {}, {"conversation_id": ""}, {"conversation_id": None}, {"conversation_id": 12}, {"conversation_id": ["cv-1"]},
    {"conversation_id": {"id": "cv-1"}}, {"conversation": "cv-1", "kind": "approval"},
])
def test_c29_9_a_notification_naming_no_conversation_opens_nothing(core_probe, user_info):
    assert target(core_probe, "approval:1", user_info) is None


def test_c29_9_frontmost_shows_unless_its_conversation_is_on_screen(core_probe):
    shown = targets(core_probe, [
        {"request_id": "complete:m", "user_info": {"conversation_id": "cv-1"}, "on_screen": on_screen}
        for on_screen in ("cv-1", "cv-2", None, "")])["cases"]
    assert [case["shows_while_frontmost"] for case in shown] == [False, True, True, True]


# Generated notifications against a reference. Strings stay in NFC with no
# combining marks, so Swift's equality (canonical equivalence, by grapheme)
# and Python's (by code point) agree on every one.
ALPHABET = "abcdeloprtuvwy0123456789-:_ éü中🙂"
WORDS = st.sampled_from(["approval", "complete", "completed", "failed", "delivery-unknown", "Approval", "", "cv-1",
                         "cv-2"])
JUNK = st.text(alphabet=ALPHABET, max_size=12)
TEXT = st.one_of(WORDS, JUNK)
VALUES = st.one_of(st.none(), st.booleans(), st.integers(-3, 3), st.floats(allow_nan=False, allow_infinity=False),
                   TEXT, st.lists(TEXT, max_size=2), st.dictionaries(TEXT, TEXT, max_size=2))
# Mostly the ids the daemon makes, so the conversation on screen matches a notification's often.
IDS = st.sampled_from(["cv-1", "cv-2", "cv-3"])
HEADS = st.one_of(st.sampled_from([*PREFIXES, *KINDS]), TEXT)


def weighted(*choices):
    """One of the strategies, each drawn in proportion to its weight
    (`st.one_of` draws its alternatives evenly, and folds repeats into one)."""
    total = sum(weight for weight, _ in choices)

    def pick(n):
        for weight, strategy in choices:
            if n < weight:
                return strategy
            n -= weight

    return st.integers(0, total - 1).flatmap(pick)


def user_info(cid, kind, rest: dict, keep: int) -> dict:
    """`keep` 0 drops the conversation, 1 the kind; the rest keep both."""
    info = {"conversation_id": cid, "kind": kind, **rest}
    return {k: v for k, v in info.items() if (k, keep) not in {("conversation_id", 0), ("kind", 1)}}


CASES = st.fixed_dictionaries({
    "request_id": st.one_of(st.builds(lambda head, tail: f"{head}:{tail}", HEADS, TEXT), TEXT),
    "user_info": st.builds(user_info, weighted((7, IDS), (3, st.one_of(TEXT, VALUES))),
                           weighted((6, st.sampled_from(KINDS)), (4, st.one_of(WORDS, VALUES))),
                           st.fixed_dictionaries({}, optional={"message_id": TEXT, "other": VALUES}),
                           st.integers(0, 5)),
    "on_screen": weighted((5, IDS), (2, st.none()), (3, TEXT)),
})


def reference(case: dict) -> dict | None:
    """The rule, written separately from the Swift."""
    info = case["user_info"]
    cid = info.get("conversation_id")
    if not isinstance(cid, str) or not cid:
        return None
    kind = info.get("kind")
    if not (isinstance(kind, str) and kind in KINDS):
        head, colon, _ = case["request_id"].partition(":")
        kind = PREFIXES.get(head) if colon else None
    return {"conversation": cid, "kind": kind, "reveals": kind == "approval",
            "shows_while_frontmost": cid != case["on_screen"]}


@settings(max_examples=100, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(cases=st.lists(CASES, min_size=1, max_size=40))
def test_property_a_clicked_notification_reads_back_as_the_rule_says(core_probe, cases):
    """For every notification: a target exactly when it names a conversation; the
    userInfo's kind if this build knows it, or else the identifier's; a card
    revealed exactly for an approval; shown while frontmost exactly when its
    conversation is not the one on screen."""
    assert all(unicodedata.is_normalized("NFC", json.dumps(c, ensure_ascii=False)) for c in cases)
    got = targets(core_probe, cases)["cases"]
    assert got == [reference(case) for case in cases]
    for result in got:
        if result is None:
            event("opens nothing")
        else:
            event(f"kind {result['kind']}")
            event(f"shown while frontmost {result['shows_while_frontmost']}")


# What a click opens, and when (NotificationClicks, as UIModel drives it).

def clicks(core_probe, steps: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-clicks-") as scratch:
        return run_probe(core_probe, "clicks", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json", {"steps": steps}))


def click(kind: str, cid: str) -> dict:
    return {"click": {"request_id": f"{kind}:1", "user_info": {"conversation_id": cid, "kind": kind}}}


def test_c29_9_a_click_before_the_baseline_opens_once_when_it_lands(core_probe):
    out = clicks(core_probe, [{"pending": {"cv-1": 1}}, click("approval", "cv-1"), {"baseline": True},
                              {"baseline": True}])
    assert out["opened"] == [None, {"conversation": "cv-1", "reveals_card": True}, None]
    assert out["held"] is None


def test_c29_9_a_held_click_yields_to_where_the_person_went_since(core_probe):
    """The daemon was down at launch; the person clicked a notification for cv-1,
    then opened cv-2 in the sidebar. When the baseline lands they stay on cv-2."""
    out = clicks(core_probe, [click("approval", "cv-1"), {"navigate": True}, {"pending": {"cv-1": 1}},
                              {"baseline": True}])
    assert out["opened"] == [None, None]
    # A click after the person's own choice is the newer intent, and opens.
    out = clicks(core_probe, [{"navigate": True}, click("completed", "cv-1"), {"baseline": True}])
    assert out["opened"] == [None, {"conversation": "cv-1", "reveals_card": False}]


def test_c29_9_a_conversation_the_person_starts_outranks_a_held_click(core_probe):
    """At launch the person clicks a notification for cv-1, then starts a new
    conversation before the baseline lands: the new one is the newer intent, so
    the held click opens nothing and no longer holds back the new one's focus."""
    out = clicks(core_probe, [click("approval", "cv-1"), {"create": True}, {"pending": {"cv-1": 1}},
                              {"baseline": True}])
    assert out["opened"] == [None, None] and out["held"] is None
    # A click after the person started one is newer again, and opens.
    out = clicks(core_probe, [{"create": True}, click("completed", "cv-2"), {"baseline": True}])
    assert out["opened"] == [None, {"conversation": "cv-2", "reveals_card": False}]


def test_c29_9_the_last_click_before_the_baseline_is_the_one_opened(core_probe):
    out = clicks(core_probe, [click("completed", "cv-1"), click("failed", "cv-2"), {"baseline": True}])
    assert out["opened"] == [None, None, {"conversation": "cv-2", "reveals_card": False}]


def test_c29_9_after_the_baseline_a_click_opens_at_once(core_probe):
    out = clicks(core_probe, [{"baseline": True}, {"pending": {"cv-1": 2}}, click("approval", "cv-1"),
                              click("delivery-unknown", "cv-1")])
    assert out["opened"] == [None, {"conversation": "cv-1", "reveals_card": True},
                             {"conversation": "cv-1", "reveals_card": False}]


def test_c29_9_an_approval_reveals_a_card_only_while_one_waits(core_probe):
    """A notification stays in Notification Center after its approval is answered;
    clicking it then opens the conversation with no card to reveal."""
    out = clicks(core_probe, [{"baseline": True}, {"pending": {"cv-1": 0, "cv-2": 1}}, click("approval", "cv-1"),
                              click("approval", "cv-3"), click("completed", "cv-2"), click("approval", "cv-2")])
    assert [o["reveals_card"] for o in out["opened"][1:]] == [False, False, False, True]


def expected_openings(steps: list[dict]) -> tuple[list, str | None]:
    """The rule, read off the whole sequence rather than simulated: a click after
    the baseline opens at once; the baseline opens the last click that came
    before it, unless the person navigated or started a conversation after that
    click; a card is revealed for an approval while its conversation has one
    pending at the moment of opening."""
    def target(step):
        return reference({"request_id": step["click"]["request_id"], "user_info": step["click"]["user_info"],
                          "on_screen": None})

    def opening(found, pending):
        return {"conversation": found["conversation"],
                "reveals_card": found["kind"] == "approval" and pending.get(found["conversation"], 0) > 0}

    first_baseline = next((i for i, s in enumerate(steps) if "baseline" in s), None)
    pending: dict = {}
    out: list = []
    for i, step in enumerate(steps):
        if "pending" in step:
            pending = step["pending"]
        elif "click" in step:
            found = target(step)
            out.append(opening(found, pending) if found and first_baseline is not None and i > first_baseline else None)
        elif "baseline" in step:
            if i != first_baseline:
                out.append(None)
                continue
            before = [j for j in range(i) if "click" in steps[j] and target(steps[j])]
            last = before[-1] if before else None
            moved = last is not None and any("navigate" in steps[j] or "create" in steps[j] for j in range(last, i))
            out.append(opening(target(steps[last]), pending) if last is not None and not moved else None)
    held = None
    if first_baseline is None:
        found = [j for j, s in enumerate(steps) if "click" in s and target(s)]
        if found and not any("create" in s for s in steps[found[-1]:]):
            held = target(steps[found[-1]])["conversation"]
    return out, held


# Mostly clicks this build posts, some it cannot read; now and then the person
# navigates or starts a conversation, the baseline lands, or the feed reports
# pending approvals.
CLICKS = st.builds(lambda cid, kind, head: {"click": {"request_id": f"{head}:1",
                                                      "user_info": {"conversation_id": cid, "kind": kind}}},
                   IDS, st.sampled_from(KINDS), st.sampled_from(list(PREFIXES)))
STEPS = weighted(
    (5, CLICKS), (1, CASES.map(lambda case: {"click": {"request_id": case["request_id"], "user_info": case["user_info"]}})),
    (1, st.just({"navigate": True})), (1, st.just({"create": True})), (1, st.just({"baseline": True})),
    (2, st.fixed_dictionaries({cid: st.integers(0, 2) for cid in ("cv-1", "cv-2", "cv-3")}).map(
        lambda counts: {"pending": counts})),
)


@settings(max_examples=100, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(steps=st.lists(STEPS, max_size=16))
def test_property_clicks_open_once_in_order_and_yield_to_navigation(core_probe, steps):
    out = clicks(core_probe, steps)
    assert (out["opened"], out["held"]) == expected_openings(steps)
    for opened in out["opened"]:
        event("nothing" if opened is None else f"opened, card {opened['reveals_card']}")
    first_baseline = next((i for i, s in enumerate(steps) if "baseline" in s), len(steps))
    clicked = [i for i in range(first_baseline) if "click" in steps[i] and expected_openings([steps[i], {"baseline": True}])[0][-1]]
    event(f"a held click, then navigation or a new conversation before the baseline: "
          f"{bool(clicked) and any('navigate' in steps[j] or 'create' in steps[j] for j in range(clicked[-1], first_baseline))}")


# A real UIModel, built without its views (SUBFLEET_UI_MODEL_TEST): what a click
# focuses, reveals and reasserts (UIModel.show, openClicked).

@pytest.fixture(scope="module")
def notification_model_probe(tmp_path_factory) -> Path:
    return compile_probe(tmp_path_factory.mktemp("subfleet-notification-model") / "probe",
                         [ROOT / "tests/frontend/NotificationModelProbe.swift"], "SUBFLEET_UI_MODEL_TEST")


def model_clicks(probe, tmp_path, steps: list[dict], focused: str | None = "cv-1", baselined: bool = True,
                 pending: dict | None = None, detail: dict | None = None,
                 posted_detail: dict | None = None) -> list[dict]:
    root = tmp_path / f"model-{uuid.uuid4().hex}"
    root.mkdir()
    data = {"root": str(root), "focused": focused, "baselined": baselined, "pending": pending or {}, "steps": steps,
            "detail": detail, "posted_detail": posted_detail}
    return model_run(probe, tmp_path, data)["steps"]


def model_run(probe, tmp_path, data: dict) -> dict:
    return run_probe(probe, write_json(tmp_path / f"{uuid.uuid4().hex}.json", data))


def test_c29_9_a_click_for_the_focused_conversation_reasserts_it(notification_model_probe, tmp_path):
    """Round 4 of #62's review: a click for the conversation already focused moves
    no focus, so the sidebar's highlight (perhaps on a native session the click's
    reopen dropped) is put back through `focusReasserted`. A click for another
    conversation moves the focus, which moves the highlight itself."""
    out = model_clicks(notification_model_probe, tmp_path,
                       [click("completed", "cv-1"), click("failed", "cv-2"), click("delivery-unknown", "cv-2")])
    assert [(o["focused"], o["focus_reasserted"]) for o in out] == [("cv-1", 1), ("cv-2", 1), ("cv-2", 2)]


def test_c29_9_a_click_held_for_the_baseline_moves_nothing_yet(notification_model_probe, tmp_path):
    out = model_clicks(notification_model_probe, tmp_path, [click("approval", "cv-2"), click("completed", "cv-1")],
                       baselined=False, pending={"cv-2": 1})
    assert [(o["focused"], o["focus_reasserted"], o["reveal"]) for o in out] == [("cv-1", 0, None), ("cv-1", 0, None)]


def test_c29_9_an_approval_click_reveals_its_card_only_while_one_waits(notification_model_probe, tmp_path):
    out = model_clicks(notification_model_probe, tmp_path, [click("approval", "cv-2"), click("approval", "cv-1")],
                       pending={"cv-1": 0, "cv-2": 1})
    # cv-1 has none waiting: its click asks for no card (cv-2's request stands
    # until that conversation's view has brought its card into view).
    assert [(o["focused"], o["reveal"]) for o in out] == [("cv-2", "cv-2"), ("cv-1", "cv-2")]


def test_c29_9_a_click_leaves_a_new_conversation_draft_for_its_conversation(notification_model_probe, tmp_path):
    out = model_clicks(notification_model_probe, tmp_path, [{"new_draft": True}, click("completed", "cv-1")])
    assert [(o["focused"], o["new_draft"]) for o in out] == [(None, True), ("cv-1", False)]


# Allow once, as release/217's delegate answered it (c3763edba, #65), now through
# the one merged delegate: UIModel.handleNotification and allowOnce, with the
# userInfo post() builds (NotificationIntent.userInfo(allowing:)).

def approval_detail(**approval) -> dict:
    """approval.get's answer for one pending, unmasked tool request with an allow option."""
    return {"approval": {"approval_id": "ap-one", "message_id": "msg-one", "conversation_id": "cv-one",
                         "kind": "tool", "display": {"tool": "Bash", "command": "pwd"}, "options": ["allow", "deny"],
                         "created_at": "2026-09-28T12:00:00Z", "state": "pending", **approval},
            "request": {"name": "Bash", "input": {"command": "pwd"}}, "masked": [],
            "request_sha256": "original-sha", "nonce": "fresh-nonce"}


APPROVAL = {"kind": "approval", "subject": "7", "conversation": "cv-one", "message": "msg-one"}
ALLOWING = {"intent": APPROVAL, "allowing": True}
REVIEW = "This request needs review. Use the approval card's details in the conversation."


def test_c29_9_allow_once_allows_the_request_it_advertised(notification_model_probe, tmp_path):
    """The request is read again (a fresh nonce) and allowed with the hash the
    notification carried, then the pending set is read; nothing is left to review."""
    out = model_clicks(notification_model_probe, tmp_path, [{"allow": ALLOWING}], detail=approval_detail())
    assert out[-1]["calls"] == [
        {"op": "approval.get", "args": {"approval_id": "ap-one"}},
        {"op": "approval.respond", "args": {"approval_id": "ap-one", "nonce": "fresh-nonce",
                                            "request_sha256": "original-sha", "decision": "allow"}},
        {"op": "approval.list", "args": {"conversation_id": "cv-one"}}]
    assert out[-1]["problem"] is None


@pytest.mark.parametrize("changed", [
    {"request_sha256": "replaced-sha"},
    {"masked": [{"path": "input.token", "rule": "token", "length": 24, "sha256": "masked-sha"}]},
    {"approval": {"state": "answered"}},
    {"approval": {"approval_id": "ap-two"}},
])
def test_c29_9_allow_once_leaves_a_changed_request_for_review(notification_model_probe, tmp_path, changed):
    """A request that is no longer the one advertised (replaced, masked, answered,
    another approval) is read and left alone: no answer is sent."""
    fresh = approval_detail(**changed.get("approval", {}))
    fresh.update({k: v for k, v in changed.items() if k != "approval"})
    out = model_clicks(notification_model_probe, tmp_path, [{"allow": ALLOWING}], detail=fresh,
                       posted_detail=approval_detail())
    assert [c["op"] for c in out[-1]["calls"]] == ["approval.get"]
    assert out[-1]["problem"] == REVIEW


@pytest.mark.parametrize("posted", [
    {"intent": APPROVAL, "allowing": False},                         # posted with no exact request
    {"user_info": {"conversation_id": "cv-one", "kind": "approval"}},
])
def test_c29_9_allow_once_without_an_advertised_request_asks_for_review(notification_model_probe, tmp_path, posted):
    out = model_clicks(notification_model_probe, tmp_path, [{"allow": posted}], detail=approval_detail())
    assert out[-1]["calls"] == [] and out[-1]["problem"] == "Review the pending request in the conversation."


def test_c29_9_every_response_but_a_dismissal_opens_its_conversation(notification_model_probe, tmp_path):
    """Through handleNotification, the one delegate's response: Allow once opens the
    conversation (a click before the baseline would wait) and answers; a plain
    click opens it and sends nothing; a dismissal does neither."""
    allow = model_clicks(notification_model_probe, tmp_path,
                         [{"respond": {"action": "allow-once", "request_id": "approval:7", **ALLOWING}}],
                         detail=approval_detail())
    assert allow[-1]["focused"] == "cv-one"
    assert [c["op"] for c in allow[-1]["calls"]] == ["approval.get", "approval.respond", "approval.list"]
    click = model_clicks(notification_model_probe, tmp_path,
                         [{"respond": {"action": "default", "request_id": "approval:7", **ALLOWING}}],
                         detail=approval_detail(), pending={"cv-one": 1})
    assert (click[-1]["focused"], click[-1]["reveal"], click[-1]["calls"]) == ("cv-one", "cv-one", [])
    dismissed = model_clicks(notification_model_probe, tmp_path,
                             [{"respond": {"action": "dismiss", "request_id": "approval:7", **ALLOWING}}],
                             detail=approval_detail())
    assert (dismissed[-1]["focused"], dismissed[-1]["calls"]) == ("cv-1", [])


def test_c29_9_the_merged_userinfo_reads_back_as_both_targets(notification_model_probe, tmp_path):
    """What post() puts on an allowable approval's notification still opens its
    conversation and kind: one arriving while frontmost for another conversation
    shows, and one for the focused conversation does not."""
    out = model_clicks(notification_model_probe, tmp_path, [
        {"frontmost": {"request_id": "approval:7", **ALLOWING}},
        {"frontmost": {"request_id": "approval:7", "intent": {**APPROVAL, "conversation": "cv-1"}, "allowing": False}},
    ], detail=approval_detail())
    assert [o["shows"] for o in out] == [True, False]


# The delegate itself, the one the model owns, as the notification center calls it
# (ApprovalNotificationDelegate.respond and present): a later edit cannot drop the
# forwarding to the model, the presentation decision or the Allow once action.

def test_c29_9_the_delegate_hands_each_response_to_the_model(notification_model_probe, tmp_path):
    allow = model_clicks(notification_model_probe, tmp_path,
                         [{"deliver": {"action": "allow-once", "request_id": "approval:7", **ALLOWING}}],
                         detail=approval_detail())
    assert allow[-1]["focused"] == "cv-one"
    assert [c["op"] for c in allow[-1]["calls"]] == ["approval.get", "approval.respond", "approval.list"]
    click = model_clicks(notification_model_probe, tmp_path,
                         [{"deliver": {"action": "default", "request_id": "approval:7", **ALLOWING}}],
                         detail=approval_detail())
    assert (click[-1]["focused"], click[-1]["calls"]) == ("cv-one", [])
    dismissed = model_clicks(notification_model_probe, tmp_path,
                             [{"deliver": {"action": "dismiss", "request_id": "approval:7", **ALLOWING}}],
                             detail=approval_detail())
    assert (dismissed[-1]["focused"], dismissed[-1]["calls"]) == ("cv-1", [])


def test_c29_9_the_delegate_presents_a_banner_unless_its_conversation_is_focused(notification_model_probe, tmp_path):
    out = model_clicks(notification_model_probe, tmp_path, [
        {"present": {"request_id": "approval:7", **ALLOWING}},
        {"present": {"request_id": "complete:m", "intent": {"kind": "completed", "subject": "m", "conversation": "cv-1"},
                     "allowing": False}},
    ], detail=approval_detail())
    assert [o["shows"] for o in out] == [["banner", "list"], []]


def test_c29_9_the_allow_once_category_is_release_217s(notification_model_probe, tmp_path):
    root = tmp_path / "category"
    root.mkdir()
    category = model_run(notification_model_probe, tmp_path, {"root": str(root), "steps": []})["category"]
    assert category == {"id": "org.maxghenis.subfleet.tool-approval", "actions": [
        {"id": "org.maxghenis.subfleet.allow-once", "title": "Allow once", "foreground": True}]}
