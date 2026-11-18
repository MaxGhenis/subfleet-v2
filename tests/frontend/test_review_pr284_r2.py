"""Review r2: identity round trips and adversarial replay controls for PR 284."""
import copy
import json
import platform
import subprocess

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT
from tests.frontend import test_review_r219_fix10_view as shrink

pytestmark = needs_swift


@pytest.fixture(scope="module")
def identity_roundtrips(tmp_path_factory):
    root = tmp_path_factory.mktemp("r2-roundtrips")
    source = (ROOT / "tests/frontend/ReviewPR284PinProbe.swift").read_text()
    source = source.replace('"identity", "pending_identity",',
                            '"roundtrip", "pending_roundtrip", "roundtrip_new", '
                            '"pending_roundtrip_new", "replace_reveal", "replace_reveal_land", '
                            '"queued_identity_fresh_b", "four_attempts", "identity", "pending_identity",')
    source = source.replace('action.contains("pending_identity")',
                            '(action.contains("pending_identity") || action.hasPrefix("pending_roundtrip") || action == "four_attempts")')
    source = source.replace('var lastRequest: UUID?',
                            'var lastRequest: UUID?\n                var freshRequest: UUID?\n'
                            '                var callbackRequests: [UUID] = []\n'
                            '                var beforeFourth = -1')
    source = source.replace('callbacks += 1;', 'callbacks += 1; callbackRequests.append(request);')
    anchor = '                case "identity": pin.conversationID = "B"'
    assert source.count(anchor) == 1
    source = source.replace(anchor, '''                case "roundtrip", "pending_roundtrip", "roundtrip_new", "pending_roundtrip_new":
                    position(0)
                    pin.conversationID = "B"
                    pin.conversationID = "A"
                    mark.setFrameOrigin(NSPoint(x: 0, y: flipped ? 11500 : 0))
                    ConversationOpeningPin.didRevealRow(in: "A", request: request)
                    if action.hasSuffix("_new") {
                        freshRequest = ConversationOpeningPin.revealRow(in: "A", target: "target")
                        ConversationOpeningPin.didRevealRow(in: "A", request: freshRequest!)
                    }
                case "replace_reveal", "replace_reveal_land":
                    position(0)
                    freshRequest = ConversationOpeningPin.revealRow(in: "A", target: "fresh")
                    if action.hasSuffix("_land") {
                        mark.itemID = "fresh"
                        mark.setFrameOrigin(NSPoint(x: 0, y: flipped ? 11500 : 0))
                    }
                    ConversationOpeningPin.didRevealRow(in: "A", request: freshRequest!)
                case "queued_identity_fresh_b":
                    position(0)
                    pin.conversationID = "B"
                    mark.conversationID = "B"
                    mark.setFrameOrigin(NSPoint(x: 0, y: flipped ? 11500 : 0))
                    freshRequest = ConversationOpeningPin.revealRow(in: "B", target: "target")
                case "four_attempts":
                    // Three completions precede delayed row presentation; the fourth lands.
                    for _ in 0..<2 {
                        let next = ConversationOpeningPin.revealRow(in: "A", target: "target")
                        ConversationOpeningPin.didRevealRow(in: "A", request: next)
                        await drain()
                    }
                    beforeFourth = landed
                    mark.setFrameOrigin(NSPoint(x: 0, y: flipped ? 11500 : 0))
                    freshRequest = ConversationOpeningPin.revealRow(in: "A", target: "target")
                    ConversationOpeningPin.didRevealRow(in: "A", request: freshRequest!)
                case "identity": pin.conversationID = "B"''')
    source = source.replace('"growth_gap": growthGap, "reply_gap": replyGap',
                            '"before_fourth": beforeFourth, "fresh_callback": lastRequest == freshRequest && freshRequest != nil, '
                            '"old_callbacks": callbackRequests.filter { $0 == request }.count, '
                            '"growth_gap": growthGap, "reply_gap": replyGap')
    path = root / "Roundtrip.swift"
    path.write_text(source)
    binary = root / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library",
                            "-target", f"{platform.machine()}-apple-macos14.0",
                            str(ROOT / "app/Sources/ConversationOpeningPin.swift"), str(path),
                            "-o", str(binary)], capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stderr
    result = run_probe(binary, timeout=60)
    print("R2_IDENTITY_INPUTS " + json.dumps(result, sort_keys=True))
    (root / "inputs.json").write_text(json.dumps(result, indent=2))
    return {(r["action"], r["flipped"]): r for r in result}


@pytest.mark.parametrize("flipped", [True, False])
def test_roundtrip_drops_old_requests_and_lands_new_ones(identity_roundtrips, flipped):
    for prefix in ["", "pending_"]:
        old = identity_roundtrips[prefix + "roundtrip", flipped]
        assert old["conversation"] == "A" and old["callbacks"] == 0, old
        assert old["landed_after"] == old["landed"], old
        fresh = identity_roundtrips[prefix + "roundtrip_new", flipped]
        assert fresh["old_callbacks"] == 0 and fresh["fresh_callback"], fresh
        assert fresh["landed_after"] == fresh["landed"] + 1, fresh


@pytest.mark.parametrize("flipped", [True, False])
def test_new_reveals_end_old_watches_and_queued_work_uses_b(identity_roundtrips, flipped):
    pending = identity_roundtrips["replace_reveal", flipped]
    assert pending["callbacks"] == 0 and pending["landed_after"] == 1, pending
    for action in ["replace_reveal_land", "queued_identity_fresh_b"]:
        fresh = identity_roundtrips[action, flipped]
        assert fresh["old_callbacks"] == 0 and fresh["fresh_callback"], fresh
        assert fresh["landed_after"] == 2, fresh


def replay():
    result = json.loads((ROOT / "tests/fixtures/qt-flake/pr284-landed-replay.json").read_text())
    return result, {s["step"]: s for s in result["steps"]}


@pytest.mark.parametrize("reveals", [0, 4])
def test_reveal_range_rejects_counts_outside_the_scene(monkeypatch, reveals):
    result, steps = replay()
    steps["landed"]["reveals"] = reveals
    monkeypatch.setattr(shrink, "shrink_replay", lambda *args: (result, steps, "range replay"))
    with pytest.raises(AssertionError):
        shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, "larger_text")


def test_reveal_range_does_not_accept_a_reveal_that_never_lands(monkeypatch):
    result, steps = replay()
    steps["landed"]["reveals"] = 2
    steps["shrinking"]["reveal"].update(outstanding=True, ended_by="")
    monkeypatch.setattr(shrink, "shrink_replay", lambda *args: (result, steps, "unlanded replay"))
    with pytest.raises(AssertionError):
        shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, "larger_text")


def test_release_counter_detects_a_second_transition_and_ignores_initial_resumed():
    trace = [
        {"at": 0, "event": "marker_appeared", "suspended": False},
        {"at": 1, "event": "reveal", "suspended": True},
        {"at": 2, "event": "reveal_end_visible", "suspended": False},
        {"at": 3, "event": "reveal", "suspended": True},
        {"at": 4, "event": "marker_appeared", "suspended": False},
    ]
    assert [e["at"] for e in shrink.releases_after({"trace": trace}, 1.5)] == [2, 4]
    assert shrink.releases_after({"trace": trace[:1]}, 0) == []


def test_pending_behavior_rejects_a_second_release_and_initially_resumed_malformed_trace():
    result = json.loads((ROOT / "tests/fixtures/qt-flake/pr284-pending-attempt2.json").read_text())
    steps = {s["step"]: s for s in result["steps"]}
    shrink.assert_shrink_releases_nothing(result, steps)
    changed = copy.deepcopy(result)
    at = steps["shrinking"]["at"]
    changed["trace"].extend([
        {"at": at, "event": "reveal", "suspended": True},
        {"at": at + .001, "event": "marker_appeared", "suspended": False},
        {"at": at + .002, "event": "reveal", "suspended": True},
        {"at": at + .003, "event": "marker_appeared", "suspended": False},
    ])
    changed["trace"].sort(key=lambda e: e["at"])
    with pytest.raises(AssertionError):
        shrink.assert_shrink_releases_nothing(changed, steps)
    changed = copy.deepcopy(result)
    for entry in changed["trace"]:
        if "suspended" in entry:
            entry["suspended"] = False
    with pytest.raises(AssertionError):
        shrink.assert_shrink_releases_nothing(changed, steps)


def test_landed_behavior_requires_a_transition_even_if_the_trace_starts_resumed():
    result, steps = replay()
    shrink.assert_landed_shrink_order(result, steps)
    shrink.assert_landed_shrink_releases(result, steps)
    for entry in result["trace"]:
        if "suspended" in entry:
            entry["suspended"] = False
    assert shrink.releases_after(result, steps["shrinking"]["at"]) == []
    with pytest.raises(AssertionError):
        shrink.assert_landed_shrink_releases(result, steps)


@pytest.mark.parametrize("flipped", [True, False])
def test_delayed_row_presentation_can_require_a_fourth_attempt(identity_roundtrips, flipped):
    result = identity_roundtrips["four_attempts", flipped]
    assert result["before_fourth"] == 0 and result["landed_after"] == 1, result
    assert result["fresh_callback"] and result["old_callbacks"] == 0, result


def test_landed_replay_is_not_a_search_repetition_guard(monkeypatch):
    from tests.frontend import test_review_r219_fix8_view as search

    result, steps = replay()
    steps["landed"]["reveals"] = 2
    # Model a spurious extra reveal and landing before the measured shrink.
    at = steps["shrinking"]["at"] - .002
    result["trace"].extend([
        {"event": "reveal", "at": at, "suspended": True},
        {"event": "reveal_landed", "at": at + .001, "suspended": True},
    ])
    result["trace"].sort(key=lambda entry: entry["at"])
    monkeypatch.setattr(shrink, "shrink_replay", lambda *args: (result, steps, "duplicate reveal replay"))
    shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, "larger_text")
    sample = {"busy_200ms": {"found": True, "consumed": True, "reveals_at": [0, .15, .3],
                             "reveal_starts": 1, "target_stayed_present": True}}
    with pytest.raises(AssertionError):
        search.test_continuous_rows_reveal_at_most_twice(sample, "busy_200ms")
