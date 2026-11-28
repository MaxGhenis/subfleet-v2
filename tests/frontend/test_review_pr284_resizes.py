"""Independent base replay and retained-request mutation for review #284."""
import json

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, app_sources, compile_probe
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary
from tests.frontend.test_review_r219_fix10_view import (
    assert_growth_resizes_follow, assert_resize_order, assert_resize_reveal_invariant,
    resize_schedule, test_reveal_invariant_for_resize_schedules as resize_property,
    shrink_replay, assert_landed_shrink_order, assert_landed_shrink_releases,
)

pytestmark = needs_swift


def pin_variant(root, text):
    source = root / "ConversationOpeningPin.swift"
    source.write_text(text)
    sources = [source if p.name == source.name else p for p in app_sources()]
    return compile_probe(root / "probe", ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift",
                         "SUBFLEET_VIEW_TEST", sources=sources, timeout=900)


def test_base_pin_resize_schedule_exposes_the_landed_race(tmp_path):
    base = (ROOT / "tests/fixtures/qt-flake/pr284-base-opening-pin.swift.txt").read_text()
    # Only the new API member is supplied for compilation with today's UIWindow.
    # The base never calls it; all pin execution remains exactly the base source.
    old = "    var onRevealLanded: (UUID) -> Void = { _ in }"
    assert base.count(old) == 2
    base = base.replace(old, old + "\n    var onLandedEndVisible: ((UUID) -> Void)? = nil")
    binary = pin_variant(tmp_path, base)
    for kind in ["pending", "landed", "growth"]:
        root = tmp_path / kind; root.mkdir()
        deltas = [600, -900, 1300] if kind == "growth" else [-11000, 2000, -2500, 1000]
        result = resize_schedule(binary, root, kind, deltas)
        assert_resize_order(result)
        if kind == "landed":
            with pytest.raises(AssertionError):
                assert_resize_reveal_invariant(result)
            assert all(r["visible"] == 0 for r in result["resizes"])
        elif kind == "pending":
            assert_resize_reveal_invariant(result)
        else:
            assert_growth_resizes_follow(result)
    # The baseline app already resumes a landed reader when its marker remounts.
    # Deliver that actual handler explicitly, independently of SwiftUI laziness.
    root = tmp_path / "marker-reader"; root.mkdir()
    result, steps, _ = shrink_replay(binary, root, "landed", replay_marker=True)
    assert_landed_shrink_order(result, steps)
    assert_landed_shrink_releases(result, steps)


def test_generated_property_rejects_dropping_the_landed_request(tmp_path, tmp_path_factory):
    source = (ROOT / "app/Sources/ConversationOpeningPin.swift").read_text()
    old = "            landedRequest = request\n"
    assert source.count(old) == 1
    binary = pin_variant(tmp_path, source.replace(old, "            landedRequest = nil\n"))
    with pytest.raises(AssertionError) as rejected:
        resize_property(fix8_view_binary=binary, tmp_path_factory=tmp_path_factory)
    assert rejected.traceback[-1].name == "assert_resize_reveal_invariant", rejected.getrepr(style="short")


def test_scripted_growth_control_passes_repeatedly(fix8_view_binary, tmp_path):
    for repeat in range(3):
        root = tmp_path / str(repeat); root.mkdir()
        result = resize_schedule(fix8_view_binary, root, "growth", [600, -900, 1300])
        assert_resize_order(result)
        assert_growth_resizes_follow(result)


def test_pending_attempt2_capture_needs_geometry_and_keeps_suspension():
    from tests.frontend.test_review_r219_fix10_view import (
        assert_replayed_shrink_order, assert_shrink_releases_nothing,
    )

    result = json.loads((ROOT / "tests/fixtures/qt-flake/pr284-pending-attempt2.json").read_text())
    steps = {step["step"]: step for step in result["steps"]}
    assert not any(e["event"] == "marker_appeared"
                   and steps["shrinking"]["at"] <= e["at"] <= steps["shrunk"]["at"]
                   for e in result["trace"])
    assert_replayed_shrink_order(result, steps)
    assert_shrink_releases_nothing(result, steps)
    # The missing marker is permitted only while this request remains pending.
    malformed = json.loads(json.dumps(result))
    bad_steps = {step["step"]: step for step in malformed["steps"]}
    bad_steps["shrinking"]["reveal"]["outstanding"] = False
    with pytest.raises(AssertionError):
        assert_replayed_shrink_order(malformed, bad_steps)


@pytest.mark.parametrize("reveals", [1, 2, 3])
def test_landed_replay_accepts_each_permitted_reveal_count(monkeypatch, reveals):
    from tests.frontend import test_review_r219_fix10_view as shrink

    result = json.loads((ROOT / "tests/fixtures/qt-flake/pr284-landed-replay.json").read_text())
    steps = {step["step"]: step for step in result["steps"]}
    # Replay a captured landed scene with each count its three-attempt setup permits.
    steps["landed"]["reveals"] = reveals
    monkeypatch.setattr(shrink, "shrink_replay", lambda *args: (result, steps, json.dumps(result)))
    shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, "larger_text")


def test_landed_replay_requires_a_reveal(monkeypatch):
    from tests.frontend import test_review_r219_fix10_view as shrink

    result = json.loads((ROOT / "tests/fixtures/qt-flake/pr284-landed-replay.json").read_text())
    steps = {step["step"]: step for step in result["steps"]}
    steps["landed"]["reveals"] = 0
    monkeypatch.setattr(shrink, "shrink_replay", lambda *args: (result, steps, json.dumps(result)))
    with pytest.raises(AssertionError):
        shrink.test_document_shrink_after_a_distant_reveal_lands_releases(None, None, "larger_text")
